#!/usr/bin/env python3
"""
Migrate bot data from SQLite to PostgreSQL.

Usage:
    SQLITE_PATH=/var/lark-bot/bot.db \
    POSTGRES_URL="postgresql://user:pass@host:5432/dbname" \
    python scripts/migrate_sqlite_to_postgres.py

If the SQLite file is corrupted, pass RECOVER=1 to attempt low-level recovery:
    RECOVER=1 \
    SQLITE_PATH=/var/lark-bot/bot.db \
    POSTGRES_URL="postgresql://..." \
    python scripts/migrate_sqlite_to_postgres.py

RECOVER=1 shells out to `sqlite3 <file> ".recover"` which can extract rows
even from heavily corrupted pages.  Requires the sqlite3 CLI to be installed.

Tables migrated: scheduled_jobs
Tables skipped:  users, monthly_usage (not needed), sessions, seen_messages (ephemeral)

The script is idempotent: existing rows in PostgreSQL are updated via ON CONFLICT.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

import psycopg2
import psycopg2.extras


def _require(var: str) -> str:
    val = os.environ.get(var, "")
    if not val:
        print(f"ERROR: {var} is not set", file=sys.stderr)
        sys.exit(1)
    return val


# ---------------------------------------------------------------------------
# SQLite recovery helpers
# ---------------------------------------------------------------------------

def _check_integrity(conn: sqlite3.Connection) -> bool:
    """Run PRAGMA integrity_check. Returns True if the DB is clean."""
    try:
        rows = conn.execute("PRAGMA integrity_check").fetchall()
        if rows and rows[0][0] == "ok":
            print("  integrity_check: ok")
            return True
        print(f"  integrity_check WARNINGS ({len(rows)} issues):")
        for r in rows[:10]:
            print(f"    {r[0]}")
        if len(rows) > 10:
            print(f"    ... ({len(rows) - 10} more)")
        return False
    except sqlite3.DatabaseError as e:
        print(f"  integrity_check failed: {e}")
        return False


def _recover_to_temp(sqlite_path: str) -> str:
    """
    Use the sqlite3 CLI `.recover` command to dump a corrupted DB into a
    fresh temporary file.  Returns the path to the recovered DB.
    Raises RuntimeError if sqlite3 CLI is not available or recovery fails.
    """
    if not shutil.which("sqlite3"):
        raise RuntimeError(
            "sqlite3 CLI not found. Install it (apt install sqlite3 / brew install sqlite3) "
            "or manually run: sqlite3 corrupted.db '.recover' | sqlite3 recovered.db"
        )
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    recovered_path = tmp.name
    print(f"  Running: sqlite3 {sqlite_path} '.recover' | sqlite3 {recovered_path}")
    try:
        dump = subprocess.run(
            ["sqlite3", sqlite_path, ".recover"],
            capture_output=True, text=True, timeout=120,
        )
        if dump.returncode != 0 and not dump.stdout.strip():
            raise RuntimeError(f"sqlite3 .recover failed: {dump.stderr[:500]}")
        load = subprocess.run(
            ["sqlite3", recovered_path],
            input=dump.stdout,
            capture_output=True, text=True, timeout=120,
        )
        if load.returncode != 0:
            raise RuntimeError(f"Loading recovered SQL failed: {load.stderr[:500]}")
    except Exception:
        os.unlink(recovered_path)
        raise
    print(f"  Recovery complete → {recovered_path}")
    return recovered_path


def _open_sqlite(sqlite_path: str, force_recover: bool) -> tuple[sqlite3.Connection, str | None]:
    """
    Open the SQLite file.  If the file is unreadable or integrity_check fails
    and force_recover=True, runs .recover and opens the recovered copy instead.
    Returns (connection, temp_path_or_None).
    """
    temp_path = None
    try:
        conn = sqlite3.connect(sqlite_path)
        conn.row_factory = sqlite3.Row
        # Quick sanity check
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
    except sqlite3.DatabaseError as e:
        if not force_recover:
            print(
                f"ERROR: Cannot open {sqlite_path}: {e}\n"
                "Re-run with RECOVER=1 to attempt low-level recovery.",
                file=sys.stderr,
            )
            sys.exit(1)
        print(f"  DB unreadable ({e}), attempting .recover ...")
        temp_path = _recover_to_temp(sqlite_path)
        conn = sqlite3.connect(temp_path)
        conn.row_factory = sqlite3.Row
        return conn, temp_path

    clean = _check_integrity(conn)
    if not clean and force_recover:
        conn.close()
        print("  Integrity issues found, attempting .recover ...")
        temp_path = _recover_to_temp(sqlite_path)
        conn = sqlite3.connect(temp_path)
        conn.row_factory = sqlite3.Row

    return conn, temp_path


# ---------------------------------------------------------------------------
# Per-table migration
# ---------------------------------------------------------------------------

def _fetch_table(conn: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
    """Fetch all rows from a table, returning [] if the table doesn't exist or is corrupt."""
    try:
        return conn.execute(f"SELECT * FROM {table}").fetchall()
    except sqlite3.OperationalError as e:
        print(f"  {table}: table not found or unreadable ({e}), skipping")
        return []
    except sqlite3.DatabaseError as e:
        print(f"  {table}: read error ({e}), skipping")
        return []


def migrate_users(sq: sqlite3.Connection, pg: psycopg2.extensions.connection):
    rows = _fetch_table(sq, "users")
    print(f"  users: {len(rows)} rows")
    if not rows:
        return
    with pg.cursor() as cur:
        ok = skip = 0
        for r in rows:
            d = dict(r)
            try:
                cur.execute(
                    """
                    INSERT INTO users
                        (open_id, auth_status, pending_code, pending_url, pending_at,
                         authorized_at, session_id, pending_session_reset,
                         meegle_auth_status, meegle_pending_code, meegle_pending_client_id,
                         meegle_pending_url, meegle_pending_at, meegle_authorized_at, display_name)
                    VALUES
                        (%(open_id)s, %(auth_status)s, %(pending_code)s, %(pending_url)s, %(pending_at)s,
                         %(authorized_at)s, %(session_id)s, %(pending_session_reset)s,
                         %(meegle_auth_status)s, %(meegle_pending_code)s, %(meegle_pending_client_id)s,
                         %(meegle_pending_url)s, %(meegle_pending_at)s, %(meegle_authorized_at)s, %(display_name)s)
                    ON CONFLICT (open_id) DO UPDATE SET
                        auth_status              = EXCLUDED.auth_status,
                        pending_code             = EXCLUDED.pending_code,
                        pending_url              = EXCLUDED.pending_url,
                        pending_at               = EXCLUDED.pending_at,
                        authorized_at            = EXCLUDED.authorized_at,
                        session_id               = EXCLUDED.session_id,
                        pending_session_reset    = EXCLUDED.pending_session_reset,
                        meegle_auth_status       = EXCLUDED.meegle_auth_status,
                        meegle_pending_code      = EXCLUDED.meegle_pending_code,
                        meegle_pending_client_id = EXCLUDED.meegle_pending_client_id,
                        meegle_pending_url       = EXCLUDED.meegle_pending_url,
                        meegle_pending_at        = EXCLUDED.meegle_pending_at,
                        meegle_authorized_at     = EXCLUDED.meegle_authorized_at,
                        display_name             = EXCLUDED.display_name
                    """,
                    {
                        "open_id":                  d.get("open_id"),
                        "auth_status":              d.get("auth_status", "pending"),
                        "pending_code":             d.get("pending_code"),
                        "pending_url":              d.get("pending_url"),
                        "pending_at":               d.get("pending_at"),
                        "authorized_at":            d.get("authorized_at"),
                        "session_id":               d.get("session_id"),
                        "pending_session_reset":    d.get("pending_session_reset", 0),
                        "meegle_auth_status":       d.get("meegle_auth_status", "none"),
                        "meegle_pending_code":      d.get("meegle_pending_code"),
                        "meegle_pending_client_id": d.get("meegle_pending_client_id"),
                        "meegle_pending_url":       d.get("meegle_pending_url"),
                        "meegle_pending_at":        d.get("meegle_pending_at"),
                        "meegle_authorized_at":     d.get("meegle_authorized_at"),
                        "display_name":             d.get("display_name") or "",
                    },
                )
                ok += 1
            except Exception as e:
                print(f"    skip user {d.get('open_id')!r}: {e}")
                pg.rollback()
                skip += 1
    pg.commit()
    print(f"  users: {ok} migrated, {skip} skipped")


def migrate_monthly_usage(sq: sqlite3.Connection, pg: psycopg2.extensions.connection):
    rows = _fetch_table(sq, "monthly_usage")
    print(f"  monthly_usage: {len(rows)} rows")
    if not rows:
        return
    with pg.cursor() as cur:
        ok = skip = 0
        for r in rows:
            d = dict(r)
            try:
                cur.execute(
                    """
                    INSERT INTO monthly_usage
                        (open_id, year_month, display_name,
                         input_tokens, output_tokens, cache_read_tokens, cost_usd, request_count,
                         personal_input_tokens, personal_output_tokens, personal_cache_read_tokens,
                         personal_cost_usd, personal_request_count, updated_at)
                    VALUES
                        (%(open_id)s, %(year_month)s, %(display_name)s,
                         %(input_tokens)s, %(output_tokens)s, %(cache_read_tokens)s,
                         %(cost_usd)s, %(request_count)s,
                         %(personal_input_tokens)s, %(personal_output_tokens)s,
                         %(personal_cache_read_tokens)s,
                         %(personal_cost_usd)s, %(personal_request_count)s, %(updated_at)s)
                    ON CONFLICT (open_id, year_month) DO UPDATE SET
                        display_name               = EXCLUDED.display_name,
                        input_tokens               = EXCLUDED.input_tokens,
                        output_tokens              = EXCLUDED.output_tokens,
                        cache_read_tokens          = EXCLUDED.cache_read_tokens,
                        cost_usd                   = EXCLUDED.cost_usd,
                        request_count              = EXCLUDED.request_count,
                        personal_input_tokens      = EXCLUDED.personal_input_tokens,
                        personal_output_tokens     = EXCLUDED.personal_output_tokens,
                        personal_cache_read_tokens = EXCLUDED.personal_cache_read_tokens,
                        personal_cost_usd          = EXCLUDED.personal_cost_usd,
                        personal_request_count     = EXCLUDED.personal_request_count,
                        updated_at                 = EXCLUDED.updated_at
                    """,
                    {
                        "open_id":                   d.get("open_id"),
                        "year_month":                d.get("year_month"),
                        "display_name":              d.get("display_name") or "",
                        "input_tokens":              d.get("input_tokens", 0),
                        "output_tokens":             d.get("output_tokens", 0),
                        "cache_read_tokens":         d.get("cache_read_tokens", 0),
                        "cost_usd":                  d.get("cost_usd", 0.0),
                        "request_count":             d.get("request_count", 0),
                        "personal_input_tokens":     d.get("personal_input_tokens", 0),
                        "personal_output_tokens":    d.get("personal_output_tokens", 0),
                        "personal_cache_read_tokens":d.get("personal_cache_read_tokens", 0),
                        "personal_cost_usd":         d.get("personal_cost_usd", 0.0),
                        "personal_request_count":    d.get("personal_request_count", 0),
                        "updated_at":                d.get("updated_at"),
                    },
                )
                ok += 1
            except Exception as e:
                print(f"    skip {d.get('open_id')!r}/{d.get('year_month')!r}: {e}")
                pg.rollback()
                skip += 1
    pg.commit()
    print(f"  monthly_usage: {ok} migrated, {skip} skipped")


def migrate_scheduled_jobs(sq: sqlite3.Connection, pg: psycopg2.extensions.connection):
    rows = _fetch_table(sq, "scheduled_jobs")
    print(f"  scheduled_jobs: {len(rows)} rows")
    if not rows:
        return
    with pg.cursor() as cur:
        ok = skip = 0
        for r in rows:
            d = dict(r)
            try:
                cur.execute(
                    """
                    INSERT INTO scheduled_jobs
                        (id, open_id, chat_id, job_type, content, schedule_type, schedule_spec,
                         next_run_at, status, last_run_at, run_count, created_at, mention_open_id)
                    VALUES
                        (%(id)s, %(open_id)s, %(chat_id)s, %(job_type)s, %(content)s,
                         %(schedule_type)s, %(schedule_spec)s,
                         %(next_run_at)s, %(status)s, %(last_run_at)s, %(run_count)s,
                         %(created_at)s, %(mention_open_id)s)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    {
                        "id":             d.get("id"),
                        "open_id":        d.get("open_id"),
                        "chat_id":        d.get("chat_id"),
                        "job_type":       d.get("job_type"),
                        "content":        d.get("content"),
                        "schedule_type":  d.get("schedule_type"),
                        "schedule_spec":  d.get("schedule_spec"),
                        "next_run_at":    d.get("next_run_at"),
                        "status":         d.get("status", "active"),
                        "last_run_at":    d.get("last_run_at"),
                        "run_count":      d.get("run_count", 0),
                        "created_at":     d.get("created_at"),
                        "mention_open_id":d.get("mention_open_id"),
                    },
                )
                ok += 1
            except Exception as e:
                print(f"    skip job {d.get('id')!r}: {e}")
                pg.rollback()
                skip += 1
    pg.commit()
    print(f"  scheduled_jobs: {ok} migrated, {skip} skipped")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    sqlite_path  = _require("SQLITE_PATH")
    postgres_url = _require("POSTGRES_URL")
    force_recover = os.environ.get("RECOVER", "").strip() in ("1", "true", "yes")

    print(f"Source SQLite  : {sqlite_path}")
    print(f"Target Postgres: {postgres_url.split('@')[-1]}")
    if force_recover:
        print("Recovery mode  : ON")

    # Open SQLite (with optional .recover fallback)
    sq, temp_path = _open_sqlite(sqlite_path, force_recover)

    # Connect to PostgreSQL and ensure schema exists
    pg = psycopg2.connect(postgres_url, cursor_factory=psycopg2.extras.RealDictCursor)

    _HERE = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.dirname(_HERE))
    from src.user_store import UserStore
    from src.job_store import JobStore
    print("Initialising PostgreSQL schema...")
    UserStore(postgres_url).close()
    JobStore(postgres_url).close()
    print("Schema ready.")

    print("Migrating data...")
    try:
        migrate_scheduled_jobs(sq, pg)
    finally:
        sq.close()
        pg.close()
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)
            print(f"  Removed temp file: {temp_path}")

    print("Migration complete.")


if __name__ == "__main__":
    main()
