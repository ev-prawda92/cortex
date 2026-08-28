"""
CORTEX Multi-Tenant Team Management
════════════════════════════════════
Workspaces, team membership, roles, invitations, and resource scoping.

Design: CORTEX stays lightweight and does NOT own customer data or billing.
A "workspace" is a tenant boundary — agents, usage, and integrations are scoped
to it. Members have roles (owner, admin, operator, viewer). Owners/admins invite
members; everything else is access control the customer manages themselves.

Tables (added to db.py via ensure_team_tables):
  - Workspace       : the tenant
  - WorkspaceMember : user <-> workspace with a role
  - Invitation      : pending email invites with tokens

Usage:
    from teams import team_manager
    team_manager.create_workspace(db, name="Acme", owner_id="u1")
    team_manager.invite(db, workspace_id="w1", email="x@acme.com", role="operator", invited_by="u1")
"""

import secrets
import hashlib
from datetime import datetime, timezone, timedelta
from typing import Optional, List

from sqlalchemy import (Column, String, Boolean, DateTime, ForeignKey,
                        Integer, Text, Index)
from sqlalchemy.orm import relationship, Session

from db import Base, gen_id, utcnow


# ═══════════════════════════════════════════════════════════════
#  ROLE DEFINITIONS
# ═══════════════════════════════════════════════════════════════

ROLES = {
    "owner":    {"level": 4, "label": "Owner",
                 "can": ["*"],
                 "desc": "Full control including workspace deletion and billing"},
    "admin":    {"level": 3, "label": "Admin",
                 "can": ["agents:*", "members:invite", "members:remove",
                         "integrations:*", "settings:write", "usage:read"],
                 "desc": "Manage agents, members, and integrations"},
    "operator": {"level": 2, "label": "Operator",
                 "can": ["agents:read", "agents:run", "agents:write",
                         "runs:read", "usage:read"],
                 "desc": "Create and run agents, view runs and usage"},
    "viewer":   {"level": 1, "label": "Viewer",
                 "can": ["agents:read", "runs:read", "usage:read"],
                 "desc": "Read-only access to agents, runs, and dashboards"},
}


def role_can(role: str, permission: str) -> bool:
    """Check whether a role grants a permission (supports wildcards)."""
    spec = ROLES.get(role)
    if not spec:
        return False
    perms = spec["can"]
    if "*" in perms:
        return True
    if permission in perms:
        return True
    # Wildcard: "agents:*" grants "agents:run"
    resource = permission.split(":")[0]
    if f"{resource}:*" in perms:
        return True
    return False


# ═══════════════════════════════════════════════════════════════
#  MODELS
# ═══════════════════════════════════════════════════════════════

class Workspace(Base):
    """A tenant boundary — agents/usage/integrations are scoped to a workspace."""
    __tablename__ = "workspaces"

    id = Column(String(64), primary_key=True, default=gen_id)
    name = Column(String(255), nullable=False)
    slug = Column(String(128), unique=True, nullable=False, index=True)
    owner_id = Column(String(64), ForeignKey("users.id"), nullable=False, index=True)
    plan = Column(String(32), default="free")  # informational only — CORTEX doesn't bill
    settings = Column(Text, default="{}")      # JSON blob (default provider, limits, etc.)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), default=utcnow)

    members = relationship("WorkspaceMember", back_populates="workspace",
                           cascade="all, delete-orphan")


class WorkspaceMember(Base):
    """Membership of a user in a workspace, with a role."""
    __tablename__ = "workspace_members"

    id = Column(String(64), primary_key=True, default=gen_id)
    workspace_id = Column(String(64), ForeignKey("workspaces.id"), nullable=False, index=True)
    user_id = Column(String(64), ForeignKey("users.id"), nullable=False, index=True)
    role = Column(String(32), nullable=False, default="operator")
    added_by = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow)

    workspace = relationship("Workspace", back_populates="members")

    __table_args__ = (
        Index("ix_wsmember_ws_user", "workspace_id", "user_id", unique=True),
    )


class Invitation(Base):
    """Pending email invitation to join a workspace."""
    __tablename__ = "invitations"

    id = Column(String(64), primary_key=True, default=gen_id)
    workspace_id = Column(String(64), ForeignKey("workspaces.id"), nullable=False, index=True)
    email = Column(String(255), nullable=False, index=True)
    role = Column(String(32), nullable=False, default="operator")
    token_hash = Column(String(255), nullable=False)  # SHA-256 of the invite token
    invited_by = Column(String(64), nullable=True)
    status = Column(String(32), default="pending")  # pending | accepted | expired | revoked
    expires_at = Column(DateTime(timezone=True), nullable=True)
    accepted_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("ix_invite_ws_email", "workspace_id", "email"),
    )


# ═══════════════════════════════════════════════════════════════
#  TEAM MANAGER
# ═══════════════════════════════════════════════════════════════

def _slugify(name: str) -> str:
    import re
    s = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')
    return s or "workspace"


class TeamManager:
    """Business logic for workspaces, membership, and invitations."""

    INVITE_TTL_DAYS = 14

    def create_workspace(self, db: Session, name: str, owner_id: str,
                         plan: str = "free") -> dict:
        """Create a workspace and add the owner as a member."""
        slug = _slugify(name)
        # Ensure unique slug
        base, i = slug, 1
        while db.query(Workspace).filter(Workspace.slug == slug).first():
            slug = f"{base}-{i}"
            i += 1
        ws = Workspace(name=name, slug=slug, owner_id=owner_id, plan=plan)
        db.add(ws)
        db.flush()
        member = WorkspaceMember(workspace_id=ws.id, user_id=owner_id,
                                 role="owner", added_by=owner_id)
        db.add(member)
        db.commit()
        db.refresh(ws)
        return {"ok": True, "workspace": self._ws_dict(db, ws)}

    def _ws_dict(self, db: Session, ws: Workspace) -> dict:
        member_count = db.query(WorkspaceMember).filter(
            WorkspaceMember.workspace_id == ws.id).count()
        return {
            "id": ws.id, "name": ws.name, "slug": ws.slug,
            "owner_id": ws.owner_id, "plan": ws.plan,
            "is_active": ws.is_active, "member_count": member_count,
            "created_at": ws.created_at.isoformat() if ws.created_at else None,
        }

    def list_workspaces_for_user(self, db: Session, user_id: str) -> List[dict]:
        """All workspaces a user is a member of, with their role."""
        memberships = db.query(WorkspaceMember).filter(
            WorkspaceMember.user_id == user_id).all()
        result = []
        for m in memberships:
            ws = db.query(Workspace).filter(Workspace.id == m.workspace_id).first()
            if ws and ws.is_active:
                d = self._ws_dict(db, ws)
                d["my_role"] = m.role
                result.append(d)
        return result

    def get_member_role(self, db: Session, workspace_id: str, user_id: str) -> Optional[str]:
        m = db.query(WorkspaceMember).filter(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == user_id).first()
        return m.role if m else None

    def can(self, db: Session, workspace_id: str, user_id: str, permission: str) -> bool:
        """Check whether a user has a permission in a workspace."""
        role = self.get_member_role(db, workspace_id, user_id)
        return role_can(role, permission) if role else False

    def list_members(self, db: Session, workspace_id: str) -> List[dict]:
        from db import User
        members = db.query(WorkspaceMember).filter(
            WorkspaceMember.workspace_id == workspace_id).all()
        result = []
        for m in members:
            user = db.query(User).filter(User.id == m.user_id).first()
            result.append({
                "id": m.id, "user_id": m.user_id,
                "email": user.email if user else "unknown",
                "name": (user.name if user and hasattr(user, "name") else "") or "",
                "role": m.role, "role_label": ROLES.get(m.role, {}).get("label", m.role),
                "added_by": m.added_by,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            })
        # Sort by role level desc
        result.sort(key=lambda x: ROLES.get(x["role"], {}).get("level", 0), reverse=True)
        return result

    def add_member(self, db: Session, workspace_id: str, user_id: str,
                   role: str = "operator", added_by: str = None) -> dict:
        if role not in ROLES:
            return {"ok": False, "error": f"invalid role '{role}'"}
        existing = db.query(WorkspaceMember).filter(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == user_id).first()
        if existing:
            existing.role = role
            db.commit()
            return {"ok": True, "action": "role_updated", "role": role}
        member = WorkspaceMember(workspace_id=workspace_id, user_id=user_id,
                                 role=role, added_by=added_by)
        db.add(member)
        db.commit()
        return {"ok": True, "action": "added", "role": role}

    def update_member_role(self, db: Session, workspace_id: str, user_id: str,
                           role: str, actor_id: str = None) -> dict:
        if role not in ROLES:
            return {"ok": False, "error": f"invalid role '{role}'"}
        member = db.query(WorkspaceMember).filter(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == user_id).first()
        if not member:
            return {"ok": False, "error": "member not found"}
        # Protect the last owner
        if member.role == "owner" and role != "owner":
            owner_count = db.query(WorkspaceMember).filter(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.role == "owner").count()
            if owner_count <= 1:
                return {"ok": False, "error": "cannot demote the last owner"}
        member.role = role
        db.commit()
        return {"ok": True, "role": role}

    def remove_member(self, db: Session, workspace_id: str, user_id: str) -> dict:
        member = db.query(WorkspaceMember).filter(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == user_id).first()
        if not member:
            return {"ok": False, "error": "member not found"}
        if member.role == "owner":
            owner_count = db.query(WorkspaceMember).filter(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.role == "owner").count()
            if owner_count <= 1:
                return {"ok": False, "error": "cannot remove the last owner"}
        db.delete(member)
        db.commit()
        return {"ok": True}

    # ── Invitations ──

    def invite(self, db: Session, workspace_id: str, email: str,
               role: str = "operator", invited_by: str = None) -> dict:
        """Create an invitation. Returns the raw token (shown once)."""
        if role not in ROLES:
            return {"ok": False, "error": f"invalid role '{role}'"}
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        inv = Invitation(
            workspace_id=workspace_id, email=email.lower().strip(), role=role,
            token_hash=token_hash, invited_by=invited_by,
            expires_at=utcnow() + timedelta(days=self.INVITE_TTL_DAYS),
        )
        db.add(inv)
        db.commit()
        db.refresh(inv)
        return {
            "ok": True, "invitation_id": inv.id, "token": token,
            "email": inv.email, "role": role,
            "expires_at": inv.expires_at.isoformat(),
            "note": "Share this invite token with the invitee (shown once).",
        }

    def accept_invite(self, db: Session, token: str, user_id: str) -> dict:
        """Accept an invitation, adding the user to the workspace."""
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        inv = db.query(Invitation).filter(
            Invitation.token_hash == token_hash,
            Invitation.status == "pending").first()
        if not inv:
            return {"ok": False, "error": "invalid or already-used invite"}
        if inv.expires_at and inv.expires_at < utcnow():
            inv.status = "expired"
            db.commit()
            return {"ok": False, "error": "invite expired"}
        self.add_member(db, inv.workspace_id, user_id, inv.role, added_by=inv.invited_by)
        inv.status = "accepted"
        inv.accepted_at = utcnow()
        db.commit()
        return {"ok": True, "workspace_id": inv.workspace_id, "role": inv.role}

    def revoke_invite(self, db: Session, invitation_id: str) -> dict:
        inv = db.query(Invitation).filter(Invitation.id == invitation_id).first()
        if not inv:
            return {"ok": False, "error": "not found"}
        inv.status = "revoked"
        db.commit()
        return {"ok": True}

    def list_invites(self, db: Session, workspace_id: str,
                     status: str = "pending") -> List[dict]:
        q = db.query(Invitation).filter(Invitation.workspace_id == workspace_id)
        if status:
            q = q.filter(Invitation.status == status)
        invites = q.order_by(Invitation.created_at.desc()).all()
        return [{
            "id": i.id, "email": i.email, "role": i.role, "status": i.status,
            "invited_by": i.invited_by,
            "expires_at": i.expires_at.isoformat() if i.expires_at else None,
            "created_at": i.created_at.isoformat() if i.created_at else None,
        } for i in invites]

    def roles_catalog(self) -> List[dict]:
        return [{"role": k, "label": v["label"], "level": v["level"],
                 "description": v["desc"], "permissions": v["can"]}
                for k, v in sorted(ROLES.items(), key=lambda x: -x[1]["level"])]


# Singleton
team_manager = TeamManager()
