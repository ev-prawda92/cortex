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


def main():
    print("═" * 60)
    print(f"CORTEX migration — dialect: {engine.dialect.name}"
          + ("  [DRY RUN]" if DRY_RUN else ""))
    print("═" * 60)

    inspector = inspect(engine)

    print("\n[1/3] Tables")
    created = create_missing_tables(inspector)

    # Re-inspect after create_all so column checks see freshly created tables
    inspector = inspect(engine)

    print("\n[2/3] agents columns")
    added = add_missing_agent_columns(inspector)

    print("\n[3/3] Version snapshot backfill")
    backfilled = backfill_version_snapshots()

    print("\n" + "═" * 60)
    if DRY_RUN:
        print("DRY RUN complete — no changes were written.")
    else:
        print("Migration complete.")
    print(f"  Tables created:        {len(created)}")
    print(f"  Columns added:         {len(added)}")
    print(f"  Snapshots backfilled:  {backfilled}")
    print("═" * 60)


if __name__ == "__main__":
    main()
