# src/user_store.py
import datetime
import hashlib
import logging
import uuid
from contextlib import contextmanager
from typing import Optional

import psycopg2
import psycopg2.extras
import psycopg2.pool

logger = logging.getLogger(__name__)

ALLOWED_USER_COLS = {
    "auth_status", "pending_code", "pending_url", "pending_at", "authorized_at", "session_id",
    "display_name", "pending_session_reset",
    # meegle auth
    "meegle_auth_status", "meegle_pending_code", "meegle_pending_client_id",
    "meegle_pending_url", "meegle_pending_at", "meegle_authorized_at",
}

CREATE_USERS = """
CREATE TABLE IF NOT EXISTS users (
    open_id                  TEXT PRIMARY KEY,
    auth_status              TEXT NOT NULL DEFAULT 'pending',
    pending_code             TEXT,
    pending_url              TEXT,
    pending_at               TIMESTAMPTZ,
    authorized_at            TIMESTAMPTZ,
    session_id               TEXT,
    pending_session_reset    INTEGER NOT NULL DEFAULT 0,
    meegle_auth_status       TEXT NOT NULL DEFAULT 'none',
    meegle_pending_code      TEXT,
    meegle_pending_client_id TEXT,
    meegle_pending_url       TEXT,
    meegle_pending_at        TIMESTAMPTZ,
    meegle_authorized_at     TIMESTAMPTZ,
    display_name             TEXT DEFAULT ''
)
"""

CREATE_SESSIONS = """
CREATE TABLE IF NOT EXISTS sessions (
    key        TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    updated_at TEXT
)
"""

CREATE_SEEN_MESSAGES = """
CREATE TABLE IF NOT EXISTS seen_messages (
    message_id TEXT PRIMARY KEY,
    seen_at    TEXT NOT NULL
)
"""

CREATE_MONTHLY_USAGE = """
CREATE TABLE IF NOT EXISTS monthly_usage (
    open_id                    TEXT             NOT NULL,
    year_month                 TEXT             NOT NULL,
    display_name               TEXT             NOT NULL DEFAULT '',
    input_tokens               INTEGER          NOT NULL DEFAULT 0,
    output_tokens              INTEGER          NOT NULL DEFAULT 0,
    cache_read_tokens          INTEGER          NOT NULL DEFAULT 0,
    cost_usd                   DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    request_count              INTEGER          NOT NULL DEFAULT 0,
    personal_input_tokens      INTEGER          NOT NULL DEFAULT 0,
    personal_output_tokens     INTEGER          NOT NULL DEFAULT 0,
    personal_cache_read_tokens INTEGER          NOT NULL DEFAULT 0,
    personal_cost_usd          DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    personal_request_count     INTEGER          NOT NULL DEFAULT 0,
    updated_at                 TEXT,
    PRIMARY KEY (open_id, year_month)
)
"""

CREATE_AUTH_RESUME_JOBS = """
CREATE TABLE IF NOT EXISTS auth_resume_jobs (
    id              TEXT PRIMARY KEY,
    context_id      TEXT NOT NULL,
    provider        TEXT NOT NULL,
    device_code     TEXT NOT NULL,
    client_id       TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'pending',
    resume_text     TEXT NOT NULL DEFAULT '',
    reply_id        TEXT NOT NULL DEFAULT '',
    thread_key      TEXT NOT NULL DEFAULT '',
    root_id         TEXT NOT NULL DEFAULT '',
    chat_id         TEXT NOT NULL DEFAULT '',
    chat_type       TEXT NOT NULL DEFAULT 'p2p',
    existing_msg_id TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL,
    claimed_at      TIMESTAMPTZ,
    claimed_by      TEXT NOT NULL DEFAULT '',
    consumed_at     TIMESTAMPTZ,
    error           TEXT NOT NULL DEFAULT ''
)
"""

CREATE_AUTH_RESUME_JOBS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_auth_resume_pending
ON auth_resume_jobs (context_id, provider, status, created_at DESC)
"""

# Columns added after initial schema — applied idempotently via information_schema check
_USER_MIGRATIONS = [
    ("users", "session_id",               "TEXT"),
    ("users", "meegle_auth_status",       "TEXT NOT NULL DEFAULT 'none'"),
    ("users", "meegle_pending_code",      "TEXT"),
    ("users", "meegle_pending_client_id", "TEXT"),
    ("users", "meegle_pending_url",       "TEXT"),
    ("users", "meegle_pending_at",        "TIMESTAMPTZ"),
    ("users", "meegle_authorized_at",     "TIMESTAMPTZ"),
    ("users", "display_name",             "TEXT DEFAULT ''"),
    ("users", "pending_session_reset",    "INTEGER NOT NULL DEFAULT 0"),
    ("monthly_usage", "personal_input_tokens",      "INTEGER NOT NULL DEFAULT 0"),
    ("monthly_usage", "personal_output_tokens",     "INTEGER NOT NULL DEFAULT 0"),
    ("monthly_usage", "personal_cache_read_tokens", "INTEGER NOT NULL DEFAULT 0"),
    ("monthly_usage", "personal_cost_usd",          "DOUBLE PRECISION NOT NULL DEFAULT 0.0"),
    ("monthly_usage", "personal_request_count",     "INTEGER NOT NULL DEFAULT 0"),
    ("seen_messages", "status",       "TEXT NOT NULL DEFAULT 'done'"),
    ("seen_messages", "owner",        "TEXT"),
    ("seen_messages", "completed_at", "TEXT"),
]


def _as_aware_dt(value) -> datetime.datetime:
    """Coerce a TIMESTAMPTZ value (datetime or ISO string) to a tz-aware datetime."""
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=datetime.timezone.utc)
        return value
    dt = datetime.datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


class UserStore:
    def __init__(self, postgres_url: str):
        self._pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=2, maxconn=25,
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
                cur.execute(CREATE_USERS)
                cur.execute(CREATE_SESSIONS)
                cur.execute(CREATE_SEEN_MESSAGES)
                cur.execute(CREATE_MONTHLY_USAGE)
                cur.execute(CREATE_AUTH_RESUME_JOBS)
                cur.execute(CREATE_AUTH_RESUME_JOBS_INDEX)
                for table, col, typedef in _USER_MIGRATIONS:
                    cur.execute(
                        "SELECT 1 FROM information_schema.columns "
                        "WHERE table_name = %s AND column_name = %s",
                        (table, col),
                    )
                    if not cur.fetchone():
                        cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typedef}")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._put(conn)

    def close(self):
        self._pool.closeall()

    def get_user(self, open_id: str) -> Optional[dict]:
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM users WHERE open_id = %s", (open_id,))
                row = cur.fetchone()
            return dict(row) if row else None
        finally:
            self._put(conn)

    def upsert_user(self, open_id: str, **kwargs):
        invalid = set(kwargs) - ALLOWED_USER_COLS
        if invalid:
            raise ValueError(f"Invalid column(s): {invalid}")
        insert_kwargs = {"auth_status": "pending", **kwargs}
        insert_cols = ["open_id"] + list(insert_kwargs.keys())
        placeholders = ", ".join("%s" for _ in insert_cols)
        insert_vals = [open_id] + list(insert_kwargs.values())
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                if kwargs:
                    update_clause = ", ".join(f"{k} = EXCLUDED.{k}" for k in kwargs)
                    cur.execute(
                        f"INSERT INTO users ({', '.join(insert_cols)}) VALUES ({placeholders})"
                        f" ON CONFLICT (open_id) DO UPDATE SET {update_clause}",
                        insert_vals,
                    )
                else:
                    cur.execute(
                        "INSERT INTO users (open_id) VALUES (%s) ON CONFLICT (open_id) DO NOTHING",
                        (open_id,),
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._put(conn)

    def _name(self, open_id: str) -> str:
        try:
            return self.get_display_name(open_id) or open_id
        except Exception:
            return open_id

    def mark_authorized(self, open_id: str):
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        _n = self._name(open_id)
        logger.info(f"[store] mark_authorized: {_n}")
        self.upsert_user(open_id, auth_status="authorized", authorized_at=now,
                         pending_code=None, pending_url=None, pending_at=None)
        logger.info(f"[store] mark_authorized: {_n} written to DB")

    def is_pending_expired(self, open_id: str) -> bool:
        user = self.get_user(open_id)
        if not user:
            return False
        if not user.get("pending_at"):
            return user.get("auth_status") == "pending"
        elapsed = (datetime.datetime.now(datetime.timezone.utc) - _as_aware_dt(user["pending_at"])).total_seconds()
        return elapsed > 600  # 10 minutes

    def get_session_id(self, open_id: str) -> Optional[str]:
        user = self.get_user(open_id)
        return user.get("session_id") if user else None

    def set_session_id(self, open_id: str, session_id: Optional[str]):
        self.upsert_user(open_id, session_id=session_id)

    def clear_thread_session(self, key: str):
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM sessions WHERE key = %s", (key,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._put(conn)

    def mark_meegle_authorized(self, open_id: str):
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.upsert_user(
            open_id,
            meegle_auth_status="authorized",
            meegle_authorized_at=now,
            meegle_pending_code=None,
            meegle_pending_client_id=None,
            meegle_pending_url=None,
            meegle_pending_at=None,
        )

    def is_meegle_pending_expired(self, open_id: str) -> bool:
        user = self.get_user(open_id)
        if not user or not user.get("meegle_pending_at"):
            return False
        elapsed = (datetime.datetime.now(datetime.timezone.utc) - _as_aware_dt(user["meegle_pending_at"])).total_seconds()
        return elapsed > 600

    def reset_auth(self, open_id: str):
        import inspect as _inspect
        _frame = _inspect.stack()[1]
        _caller = f"{_frame.filename.rsplit('/', 1)[-1].rsplit(chr(92), 1)[-1]}:{_frame.lineno}"
        _n = self._name(open_id)
        logger.info(f"[store] reset_auth: {_n} called from {_caller}")
        self.upsert_user(
            open_id,
            auth_status="pending",
            authorized_at=None,
            pending_code=None,
            pending_url=None,
            pending_at=None,
        )
        logger.info(f"[store] reset_auth: {_n} written to DB")

    def reset_meegle_auth(self, open_id: str):
        self.upsert_user(
            open_id,
            meegle_auth_status="none",
            meegle_authorized_at=None,
            meegle_pending_code=None,
            meegle_pending_client_id=None,
            meegle_pending_url=None,
            meegle_pending_at=None,
        )

    def create_auth_resume_job(
        self,
        context_id: str,
        provider: str,
        device_code: str,
        client_id: str,
        resume_text: str,
        reply_id: str,
        thread_key: str,
        root_id: str,
        chat_id: str,
        chat_type: str,
        existing_msg_id: str = "",
    ) -> str:
        if provider not in ("lark", "meegle"):
            raise ValueError(f"invalid auth provider: {provider}")
        now = datetime.datetime.now(datetime.timezone.utc)
        job_id = str(uuid.uuid4())
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO auth_resume_jobs (
                        id, context_id, provider, device_code, client_id,
                        status, resume_text, reply_id, thread_key, root_id,
                        chat_id, chat_type, existing_msg_id, created_at
                    )
                    VALUES (%s, %s, %s, %s, %s, 'pending', %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        job_id, context_id, provider, device_code, client_id or "",
                        resume_text or "", reply_id or "", thread_key or "", root_id or "",
                        chat_id or "", chat_type or "p2p", existing_msg_id or "", now,
                    ),
                )
            conn.commit()
            logger.info(
                "[auth-resume] created provider=%s ctx=%s code=%s job=%s",
                provider, context_id, (device_code or "")[:8], job_id,
            )
            return job_id
        except Exception:
            conn.rollback()
            raise
        finally:
            self._put(conn)

    def claim_auth_resume_job(
        self,
        context_id: str,
        provider: str,
        device_code: str,
        owner: str,
    ) -> Optional[dict]:
        now = datetime.datetime.now(datetime.timezone.utc)
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    WITH picked AS (
                        SELECT id
                        FROM auth_resume_jobs
                        WHERE context_id = %s
                          AND provider = %s
                          AND device_code = %s
                          AND status = 'pending'
                        ORDER BY created_at DESC
                        LIMIT 1
                        FOR UPDATE SKIP LOCKED
                    )
                    UPDATE auth_resume_jobs j
                    SET status = 'claimed',
                        claimed_at = %s,
                        claimed_by = %s
                    FROM picked
                    WHERE j.id = picked.id
                    RETURNING j.*
                    """,
                    (context_id, provider, device_code, now, owner or ""),
                )
                row = cur.fetchone()
            conn.commit()
            if row:
                logger.info(
                    "[auth-resume] claimed provider=%s ctx=%s job=%s owner=%s",
                    provider, context_id, row["id"], owner or "",
                )
            return dict(row) if row else None
        except Exception:
            conn.rollback()
            raise
        finally:
            self._put(conn)

    def consume_auth_resume_job(self, job_id: str) -> None:
        now = datetime.datetime.now(datetime.timezone.utc)
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE auth_resume_jobs SET status = 'consumed', consumed_at = %s WHERE id = %s",
                    (now, job_id),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._put(conn)

    def fail_auth_resume_job(self, job_id: str, error: str) -> None:
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE auth_resume_jobs SET status = 'failed', error = %s WHERE id = %s",
                    ((error or "")[:1000], job_id),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._put(conn)

    def set_auth_resume_existing_msg_id(
        self,
        context_id: str,
        provider: str,
        device_code: str,
        existing_msg_id: str,
    ) -> None:
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE auth_resume_jobs
                    SET existing_msg_id = %s
                    WHERE context_id = %s
                      AND provider = %s
                      AND device_code = %s
                      AND status = 'pending'
                    """,
                    (existing_msg_id or "", context_id, provider, device_code),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._put(conn)

    def get_thread_session(self, key: str) -> Optional[str]:
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT session_id FROM sessions WHERE key = %s", (key,))
                row = cur.fetchone()
            return row["session_id"] if row else None
        finally:
            self._put(conn)

    def set_thread_session(self, key: str, session_id: str):
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO sessions (key, session_id, updated_at) VALUES (%s, %s, %s)"
                    " ON CONFLICT (key) DO UPDATE SET"
                    " session_id = EXCLUDED.session_id, updated_at = EXCLUDED.updated_at",
                    (key, session_id, now),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._put(conn)

    def get_display_name(self, open_id: str) -> str:
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT display_name FROM users WHERE open_id = %s", (open_id,))
                row = cur.fetchone()
            return (row["display_name"] or "") if row else ""
        finally:
            self._put(conn)

    def set_display_name(self, open_id: str, name: str):
        self.upsert_user(open_id, display_name=name)

    def add_usage(self, open_id: str, year_month: str, display_name: str,
                  input_tokens: int, output_tokens: int, cache_read_tokens: int,
                  cost_usd: float, using_personal_key: bool = False):
        """Record usage. Total columns always incremented; personal_* columns
        additionally incremented when using_personal_key=True."""
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        p_in    = input_tokens       if using_personal_key else 0
        p_out   = output_tokens      if using_personal_key else 0
        p_cache = cache_read_tokens  if using_personal_key else 0
        p_cost  = cost_usd           if using_personal_key else 0.0
        p_req   = 1                  if using_personal_key else 0
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO monthly_usage"
                    " (open_id, year_month, display_name,"
                    "  input_tokens, output_tokens, cache_read_tokens, cost_usd, request_count,"
                    "  personal_input_tokens, personal_output_tokens, personal_cache_read_tokens,"
                    "  personal_cost_usd, personal_request_count, updated_at)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, 1, %s, %s, %s, %s, %s, %s)"
                    " ON CONFLICT (open_id, year_month) DO UPDATE SET"
                    "  display_name = CASE WHEN EXCLUDED.display_name != '' THEN EXCLUDED.display_name"
                    "                      ELSE monthly_usage.display_name END,"
                    "  input_tokens               = monthly_usage.input_tokens               + EXCLUDED.input_tokens,"
                    "  output_tokens              = monthly_usage.output_tokens              + EXCLUDED.output_tokens,"
                    "  cache_read_tokens          = monthly_usage.cache_read_tokens          + EXCLUDED.cache_read_tokens,"
                    "  cost_usd                   = monthly_usage.cost_usd                   + EXCLUDED.cost_usd,"
                    "  request_count              = monthly_usage.request_count              + 1,"
                    "  personal_input_tokens      = monthly_usage.personal_input_tokens      + EXCLUDED.personal_input_tokens,"
                    "  personal_output_tokens     = monthly_usage.personal_output_tokens     + EXCLUDED.personal_output_tokens,"
                    "  personal_cache_read_tokens = monthly_usage.personal_cache_read_tokens + EXCLUDED.personal_cache_read_tokens,"
                    "  personal_cost_usd          = monthly_usage.personal_cost_usd          + EXCLUDED.personal_cost_usd,"
                    "  personal_request_count     = monthly_usage.personal_request_count     + EXCLUDED.personal_request_count,"
                    "  updated_at                 = EXCLUDED.updated_at",
                    (open_id, year_month, display_name,
                     input_tokens, output_tokens, cache_read_tokens, cost_usd,
                     p_in, p_out, p_cache, p_cost, p_req, now),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._put(conn)

    def get_user_usage(self, open_id: str, year_month: str) -> dict:
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM monthly_usage WHERE open_id = %s AND year_month = %s",
                    (open_id, year_month),
                )
                row = cur.fetchone()
            return dict(row) if row else {}
        finally:
            self._put(conn)

    def get_all_usage(self, year_month: str) -> list:
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM monthly_usage WHERE year_month = %s ORDER BY cost_usd DESC",
                    (year_month,),
                )
                rows = cur.fetchall()
            return [dict(r) for r in rows]
        finally:
            self._put(conn)

    # ------------------------------------------------------------------
    # Cross-pod message deduplication
    # ------------------------------------------------------------------

    def mark_message_seen(self, message_id: str) -> bool:
        """Atomically record message_id as seen.
        Returns True if this call was first (proceed), False if duplicate."""
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO seen_messages (message_id, seen_at) VALUES (%s, %s)"
                    " ON CONFLICT (message_id) DO NOTHING",
                    (message_id, now),
                )
                inserted = cur.rowcount == 1
            conn.commit()
            return inserted
        except Exception:
            conn.rollback()
            raise
        finally:
            self._put(conn)

    def claim_message(self, message_id: str, reclaim_after_seconds: int = 1800,
                      owner: str = "") -> bool:
        """Atomically claim a message for processing across pods.

        A completed message is never re-claimed. A processing message can be
        reclaimed after the lease window, which lets another pod recover work
        abandoned by a crashed worker without duplicating active processing.
        """
        now = datetime.datetime.now(datetime.timezone.utc)
        now_iso = now.isoformat()
        stale_before = (now - datetime.timedelta(seconds=reclaim_after_seconds)).isoformat()
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO seen_messages (message_id, seen_at, status, owner)
                    VALUES (%s, %s, 'processing', %s)
                    ON CONFLICT (message_id) DO UPDATE
                      SET seen_at = EXCLUDED.seen_at,
                          status = 'processing',
                          owner = EXCLUDED.owner,
                          completed_at = NULL
                      WHERE seen_messages.status = 'processing'
                        AND seen_messages.seen_at < %s
                    """,
                    (message_id, now_iso, owner or None, stale_before),
                )
                claimed = cur.rowcount == 1
            conn.commit()
            return claimed
        except Exception:
            conn.rollback()
            raise
        finally:
            self._put(conn)

    def complete_message(self, message_id: str) -> None:
        """Mark a claimed message as fully handled."""
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE seen_messages SET status = 'done', completed_at = %s "
                    "WHERE message_id = %s",
                    (now, message_id),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._put(conn)

    def claim_or_reclaim(self, message_id: str, reclaim_after_seconds: int = 180) -> bool:
        """Like mark_message_seen but allows poll-recovery to re-claim abandoned messages.

        Returns True if this pod should process the message.
        Returns False if another pod claimed it recently (within reclaim_after_seconds).

        A pod that claimed a message but crashed before replying leaves a stale row.
        After reclaim_after_seconds have elapsed, the next caller wins the re-claim
        by updating seen_at, preventing the silent failure from persisting.
        """
        now = datetime.datetime.now(datetime.timezone.utc)
        now_iso = now.isoformat()
        stale_before = (now - datetime.timedelta(seconds=reclaim_after_seconds)).isoformat()
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                # INSERT wins on first claim.
                # ON CONFLICT UPDATE wins if the existing row is stale (pod crashed).
                # ON CONFLICT no-op (rowcount=0) if the row is fresh (actively processing).
                cur.execute(
                    """
                    INSERT INTO seen_messages (message_id, seen_at) VALUES (%s, %s)
                    ON CONFLICT (message_id) DO UPDATE
                      SET seen_at = EXCLUDED.seen_at
                      WHERE seen_messages.seen_at < %s
                    """,
                    (message_id, now_iso, stale_before),
                )
                claimed = cur.rowcount == 1
            conn.commit()
            return claimed
        except Exception:
            conn.rollback()
            raise
        finally:
            self._put(conn)

    @staticmethod
    def _advisory_lock_id(key: str) -> int:
        digest = hashlib.sha256(key.encode("utf-8")).digest()
        return int.from_bytes(digest[:8], byteorder="big", signed=True)

    @contextmanager
    def conversation_lock(self, key: str):
        """Cross-pod mutex for one conversation/session key."""
        lock_id = self._advisory_lock_id(key)
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_lock(%s)", (lock_id,))
            try:
                yield
            finally:
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(%s)", (lock_id,))
        finally:
            self._put(conn)

    def cleanup_seen_messages(self, max_age_hours: int = 48) -> int:
        cutoff = (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(hours=max_age_hours)
        ).isoformat()
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM seen_messages WHERE seen_at < %s", (cutoff,))
                deleted = cur.rowcount
            conn.commit()
            return deleted
        except Exception:
            conn.rollback()
            raise
        finally:
            self._put(conn)
