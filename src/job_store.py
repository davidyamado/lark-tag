# src/job_store.py
"""
Persistent storage for scheduled jobs (cron / one-shot reminders and AI tasks).
"""
import json
import os
import socket
import time
import uuid
from typing import Optional

import psycopg2
import psycopg2.extras
import psycopg2.pool

CREATE_SCHEDULED_JOBS = """
CREATE TABLE IF NOT EXISTS scheduled_jobs (
    id               TEXT   PRIMARY KEY,
    open_id          TEXT   NOT NULL,
    chat_id          TEXT   NOT NULL,
    job_type         TEXT   NOT NULL,
    content          TEXT   NOT NULL,
    schedule_type    TEXT   NOT NULL,
    schedule_spec    TEXT   NOT NULL,
    next_run_at      BIGINT NOT NULL,
    status           TEXT   NOT NULL DEFAULT 'active',
    last_run_at      BIGINT,
    run_count        INTEGER NOT NULL DEFAULT 0,
    created_at       BIGINT NOT NULL,
    mention_open_id  TEXT,
    lease_until      BIGINT,
    locked_by        TEXT
)
"""

CREATE_IDX_JOBS_DUE = """
CREATE INDEX IF NOT EXISTS idx_jobs_due ON scheduled_jobs(next_run_at, status)
"""


class JobStore:
    def __init__(self, postgres_url: str):
        self._pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=2, maxconn=20,
            dsn=postgres_url,
            cursor_factory=psycopg2.extras.RealDictCursor,
        )
        self._init_schema()

    def _conn(self):
        return self._pool.getconn()

    def _put(self, conn):
        self._pool.putconn(conn)

    def _init_schema(self):
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(CREATE_SCHEDULED_JOBS)
                cur.execute(CREATE_IDX_JOBS_DUE)
                # Migration: add mention_open_id if absent
                cur.execute(
                    "SELECT 1 FROM information_schema.columns"
                    " WHERE table_name = 'scheduled_jobs' AND column_name = 'mention_open_id'"
                )
                if not cur.fetchone():
                    cur.execute("ALTER TABLE scheduled_jobs ADD COLUMN mention_open_id TEXT")
                cur.execute(
                    "SELECT 1 FROM information_schema.columns"
                    " WHERE table_name = 'scheduled_jobs' AND column_name = 'lease_until'"
                )
                if not cur.fetchone():
                    cur.execute("ALTER TABLE scheduled_jobs ADD COLUMN lease_until BIGINT")
                cur.execute(
                    "SELECT 1 FROM information_schema.columns"
                    " WHERE table_name = 'scheduled_jobs' AND column_name = 'locked_by'"
                )
                if not cur.fetchone():
                    cur.execute("ALTER TABLE scheduled_jobs ADD COLUMN locked_by TEXT")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._put(conn)

    def close(self):
        self._pool.closeall()

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create_job(
        self,
        open_id: str,
        chat_id: str,
        job_type: str,
        content: str,
        schedule_type: str,
        schedule_spec: dict,
        next_run_at: int,
        mention_open_id: Optional[str] = None,
    ) -> str:
        """Insert a new job and return its id."""
        job_id = str(uuid.uuid4())
        now_ms = int(time.time() * 1000)
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO scheduled_jobs"
                    " (id, open_id, chat_id, job_type, content,"
                    "  schedule_type, schedule_spec, next_run_at, status,"
                    "  last_run_at, run_count, created_at, mention_open_id)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'active', NULL, 0, %s, %s)",
                    (job_id, open_id, chat_id, job_type, content,
                     schedule_type, json.dumps(schedule_spec, ensure_ascii=False),
                     next_run_at, now_ms, mention_open_id),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._put(conn)
        return job_id

    def get_job(self, job_id: str) -> Optional[dict]:
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM scheduled_jobs WHERE id = %s", (job_id,))
                row = cur.fetchone()
            return self._row_to_dict(dict(row)) if row else None
        finally:
            self._put(conn)

    def list_jobs(self, open_id: str) -> list[dict]:
        """Return all active jobs for a user, ordered by next_run_at."""
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM scheduled_jobs WHERE open_id = %s AND status = 'active'"
                    " ORDER BY next_run_at ASC",
                    (open_id,),
                )
                rows = cur.fetchall()
            return [self._row_to_dict(dict(r)) for r in rows]
        finally:
            self._put(conn)

    def get_due_jobs(self, now_ms: int) -> list[dict]:
        """Return all active jobs whose next_run_at <= now_ms."""
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM scheduled_jobs WHERE status = 'active' AND next_run_at <= %s",
                    (now_ms,),
                )
                rows = cur.fetchall()
            return [self._row_to_dict(dict(r)) for r in rows]
        finally:
            self._put(conn)

    def claim_due_jobs(self, now_ms: int, limit: int = 50,
                       lease_seconds: int = 300) -> list[dict]:
        """Atomically claim due jobs for this worker across scheduler pods."""
        lease_until = now_ms + lease_seconds * 1000
        owner = f"{socket.gethostname()}:{os.getpid()}"
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    WITH due AS (
                        SELECT id
                        FROM scheduled_jobs
                        WHERE (
                            status = 'active' AND next_run_at <= %s
                        ) OR (
                            status = 'running' AND lease_until IS NOT NULL AND lease_until < %s
                        )
                        ORDER BY next_run_at ASC
                        FOR UPDATE SKIP LOCKED
                        LIMIT %s
                    )
                    UPDATE scheduled_jobs AS j
                    SET status = 'running',
                        lease_until = %s,
                        locked_by = %s
                    FROM due
                    WHERE j.id = due.id
                    RETURNING j.*
                    """,
                    (now_ms, now_ms, limit, lease_until, owner),
                )
                rows = cur.fetchall()
            conn.commit()
            return [self._row_to_dict(dict(r)) for r in rows]
        except Exception:
            conn.rollback()
            raise
        finally:
            self._put(conn)

    def mark_completed(self, job_id: str) -> None:
        now_ms = int(time.time() * 1000)
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE scheduled_jobs SET status = 'completed', last_run_at = %s,"
                    " run_count = run_count + 1, lease_until = NULL, locked_by = NULL WHERE id = %s",
                    (now_ms, job_id),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._put(conn)

    def update_next_run(self, job_id: str, next_run_at: int) -> None:
        now_ms = int(time.time() * 1000)
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE scheduled_jobs SET next_run_at = %s, last_run_at = %s,"
                    " run_count = run_count + 1, status = 'active',"
                    " lease_until = NULL, locked_by = NULL WHERE id = %s",
                    (next_run_at, now_ms, job_id),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._put(conn)

    def cancel_job(self, job_id: str, open_id: str) -> bool:
        """Cancel a job. Returns True if a row was updated (ownership verified)."""
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE scheduled_jobs SET status = 'cancelled', lease_until = NULL, locked_by = NULL"
                    " WHERE id = %s AND open_id = %s AND status IN ('active', 'running')",
                    (job_id, open_id),
                )
                updated = cur.rowcount > 0
            conn.commit()
            return updated
        except Exception:
            conn.rollback()
            raise
        finally:
            self._put(conn)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_dict(d: dict) -> dict:
        try:
            d["schedule_spec"] = json.loads(d["schedule_spec"])
        except (json.JSONDecodeError, KeyError):
            pass
        return d
