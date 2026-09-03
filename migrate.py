"""
CORTEX Database Migration
══════════════════════════
Idempotent, zero-dependency schema upgrade for existing databases.

`Base.metadata.create_all` (run at app startup) creates any *new* tables, but it
never ALTERs an existing table — so a live database that predates the recovery
feature is missing the soft-delete columns on `agents`. This script:

  1. Creates any missing tables (agent_versions, workspaces, workspace_members,
     invitations, and anything else new).
  2. Adds the soft-delete columns to `agents` if they're absent.
  3. Backfills a v1 version snapshot for every existing agent so its history and
     rollback work immediately.

Safe to run multiple times — every step checks current state first.
Works on PostgreSQL and SQLite.

Usage:
    python migrate.py                 # uses $DATABASE_URL (or the db.py default)
    DATABASE_URL=postgresql://... python migrate.py
    python migrate.py --dry-run       # show what would change, make no writes
"""

import sys
from sqlalchemy import inspect, text

import db as db_mod
from db import (Base, engine, SessionLocal, Agent, AgentVersion,
                snapshot_agent_version)
# Importing phase2 registers its models on Base.metadata so create_missing_tables
# below picks them up. Without this import the Phase 2 tables are never created.
import phase2  # noqa: F401

DRY_RUN = "--dry-run" in sys.argv


# Columns added to `agents` by the recovery feature: name -> DDL type snippet
AGENTS_NEW_COLUMNS = {
    # Recovery / recycle bin
    "is_deleted":  "BOOLEAN DEFAULT FALSE",
    "deleted_at":  "TIMESTAMP WITH TIME ZONE",
    "deleted_by":  "VARCHAR(64)",
    "purge_after": "TIMESTAMP WITH TIME ZONE",
    # Lifecycle stage and ownership
    "lifecycle":            "VARCHAR(16) DEFAULT 'active'",
    "lifecycle_note":       "VARCHAR(512) DEFAULT ''",
    "lifecycle_changed_at": "TIMESTAMP WITH TIME ZONE",
    "contact":              "VARCHAR(255) DEFAULT ''",
}

APPROVAL_NEW_COLUMNS = {
    "consumed_at": "TIMESTAMP WITH TIME ZONE",
}


def _is_sqlite() -> bool:
    return engine.dialect.name == "sqlite"


def _type_for(dialect_snippet: str) -> str:
    """SQLite doesn't support 'TIMESTAMP WITH TIME ZONE' — normalize types."""
    if _is_sqlite():
        s = dialect_snippet.replace("TIMESTAMP WITH TIME ZONE", "TIMESTAMP")
        s = s.replace("BOOLEAN DEFAULT FALSE", "BOOLEAN DEFAULT 0")
        return s
    return dialect_snippet


def log(msg: str):
    print(f"  {msg}")


def create_missing_tables(inspector) -> list:
    """Create any tables defined on the models but absent in the DB."""
    existing = set(inspector.get_table_names())
    defined = set(Base.metadata.tables.keys())
    missing = sorted(defined - existing)
    if not missing:
        log("Tables: all present, nothing to create.")
        return []
    log(f"Tables to create: {', '.join(missing)}")
    if not DRY_RUN:
        # create_all only creates what's missing; existing tables are untouched
        Base.metadata.create_all(engine)
        log("Created missing tables.")
    return missing


def add_missing_agent_columns(inspector) -> list:
    """ALTER TABLE agents to add soft-delete columns if absent."""
    if "agents" not in inspector.get_table_names():
        log("agents table does not exist yet — create_all will handle it.")
        return []
    existing_cols = {c["name"] for c in inspector.get_columns("agents")}
    to_add = [(name, ddl) for name, ddl in AGENTS_NEW_COLUMNS.items()
              if name not in existing_cols]
    if not to_add:
        log("agents columns: all soft-delete columns present.")
        return []
    added = []
    for name, ddl in to_add:
        col_type = _type_for(ddl)
        stmt = f"ALTER TABLE agents ADD COLUMN {name} {col_type}"
        log(f"ALTER: {stmt}")
        if not DRY_RUN:
            with engine.begin() as conn:
                conn.execute(text(stmt))
        added.append(name)
    # Index on is_deleted for fast recycle-bin filtering (Postgres only; SQLite auto-handles small tables)
    if "is_deleted" in added and not DRY_RUN and not _is_sqlite():
        try:
            with engine.begin() as conn:
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_agents_is_deleted ON agents (is_deleted)"))
            log("Created index ix_agents_is_deleted.")
        except Exception as e:
            log(f"(index creation skipped: {e})")
    return added


def add_missing_approval_columns(inspector) -> list:
    """Add replay-protection state to existing approval tables."""
    if "approval_requests" not in inspector.get_table_names():
        log("approval_requests does not exist yet — create_all will handle it.")
        return []
    existing = {c["name"] for c in inspector.get_columns("approval_requests")}
    added = []
    for name, ddl in APPROVAL_NEW_COLUMNS.items():
        if name in existing:
            continue
        stmt = f"ALTER TABLE approval_requests ADD COLUMN {name} {_type_for(ddl)}"
        log(f"ALTER: {stmt}")
        if not DRY_RUN:
            with engine.begin() as conn:
                conn.execute(text(stmt))
        added.append(name)
    if not added:
        log("approval_requests columns: all present.")
    return added


def backfill_version_snapshots() -> int:
    """Write a v1 snapshot for every agent that has no version history yet."""
    count = 0
    # In dry-run the ORM would SELECT columns that don't exist yet, so use raw SQL
    # to identify which agents need a backfill without touching the new columns.
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT id, name FROM agents")).fetchall()
        try:
            versioned = {r[0] for r in conn.execute(
                text("SELECT DISTINCT agent_id FROM agent_versions")).fetchall()}
        except Exception:
            versioned = set()  # table doesn't exist yet (dry-run before create)
    pending = [(rid, rname) for rid, rname in rows if rid not in versioned]

    if not pending:
        log("Version snapshots: every agent already has history.")
        return 0

    if DRY_RUN:
        for rid, rname in pending:
            log(f"Backfill v1 snapshot for agent '{rname}' ({rid})")
        return len(pending)

    # Real run: use the ORM (columns exist by now) so hashing/chaining is consistent
    s = SessionLocal()
    try:
        for rid, rname in pending:
            a = s.query(Agent).filter(Agent.id == rid).first()
            if not a:
                continue
            log(f"Backfill v1 snapshot for agent '{a.name}' ({a.id})")
            snapshot_agent_version(
                s, a, changed_by=a.owner_id, changer_email="",
                change_type="create", prev_config={},
                change_summary="Initial snapshot (migration backfill)",
            )
            count += 1
    finally:
        s.close()
    return count


def migrate_plaintext_credentials():
    """Move any plaintext credential out of config and into the secrets table.

    Two places hold them today:

      agents.config          — the live config, fixable in place
      agent_versions.config  — every snapshot ever taken, which is where the
                               real damage is: one credential entered once was
                               copied into a new immutable row on every
                               subsequent config change

    Live configs get their secret encrypted and replaced with a reference.
    Snapshots get the field stripped outright — a historical version does not
    need a working credential, it needs to not be carrying one. Rolling back to
    an old version restores its settings and leaves the current credential
    alone, which is the behaviour you want anyway: rollback is for config, not
    for re-injecting a key someone rotated.
    """
    from db import SessionLocal, Agent, AgentVersion, Secret, gen_id
    import secrets_store

    db = SessionLocal()
    moved = stripped = 0
    try:
        for a in db.query(Agent).all():
            cfg = a.config or {}
            sources = cfg.get("data_sources")
            if not isinstance(sources, list):
                continue
            changed = False
            new_sources = []
            for ds in sources:
                if not isinstance(ds, dict):
                    new_sources.append(ds)
                    continue
                secret = ds.get("auth_value")
                if secret:
                    ds = dict(ds)
                    if not DRY_RUN:
                        row = Secret(id=gen_id(), agent_id=a.id,
                                     label=ds.get("name", ""),
                                     ciphertext=secrets_store.encrypt(secret),
                                     hint=secrets_store.hint(secret))
                        db.add(row)
                        db.flush()
                        ds["auth_ref"] = row.id
                        ds["auth_hint"] = row.hint
                    ds.pop("auth_value", None)
                    changed = True
                    moved += 1
                new_sources.append(ds)
            if changed:
                print(f"  agent {a.id}: moved {moved} credential(s) into the secret store")
                if not DRY_RUN:
                    cfg = dict(cfg)
                    cfg["data_sources"] = new_sources
                    a.config = cfg
                    from sqlalchemy.orm.attributes import flag_modified
                    flag_modified(a, "config")

        for v in db.query(AgentVersion).all():
            cfg = v.config or {}
            sources = cfg.get("data_sources")
            if not isinstance(sources, list):
                continue
            if not any(isinstance(d, dict) and d.get("auth_value") for d in sources):
                continue
            cleaned = []
            for ds in sources:
                if isinstance(ds, dict) and ds.get("auth_value"):
                    ds = {k: val for k, val in ds.items() if k != "auth_value"}
                    stripped += 1
                cleaned.append(ds)
            print(f"  version {v.agent_id} v{v.version}: stripped credential(s) from snapshot")
            if not DRY_RUN:
                cfg = dict(cfg)
                cfg["data_sources"] = cleaned
                v.config = cfg
                from sqlalchemy.orm.attributes import flag_modified
                flag_modified(v, "config")

        if not DRY_RUN:
            db.commit()
    finally:
        db.close()

    if moved == 0 and stripped == 0:
        print("  No plaintext credentials found.")
    return moved, stripped


def main():
    print("═" * 60)
    print(f"CORTEX migration — dialect: {engine.dialect.name}"
          + ("  [DRY RUN]" if DRY_RUN else ""))
    print("═" * 60)

    inspector = inspect(engine)

    print("\n[1/5] Tables")
    created = create_missing_tables(inspector)

    # Re-inspect after create_all so column checks see freshly created tables
    inspector = inspect(engine)

    print("\n[2/5] agents columns")
    added = add_missing_agent_columns(inspector)

    print("\n[3/5] approval_requests columns")
    approval_added = add_missing_approval_columns(inspector)

    print("\n[4/5] Version snapshot backfill")
    backfilled = backfill_version_snapshots()

    print("\n[5/5] Plaintext credentials")
    moved, stripped = migrate_plaintext_credentials()

    print("\n" + "═" * 60)
    if DRY_RUN:
        print("DRY RUN complete — no changes were written.")
    else:
        print("Migration complete.")
    print(f"  Tables created:        {len(created)}")
    print(f"  Columns added:         {len(added) + len(approval_added)}")
    print(f"  Snapshots backfilled:  {backfilled}")
    print(f"  Credentials encrypted: {moved}")
    print(f"  Snapshots scrubbed:    {stripped}")
    print("═" * 60)


if __name__ == "__main__":
    main()
