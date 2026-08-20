# src/audit_store.py
"""
Cross-pod audit log: every conversation event (user message received, Claude
reply, security refusal, etc.) is INSERTed into a shared PostgreSQL table so
operators can query a user's full history regardless of which pod processed
each turn.

Design notes:
- Independent connection pool (small: 1-5 conns) so audit traffic can't
  starve the main business pool (UserStore/JobStore).
- Fail-OPEN: any INSERT exception is caught + WARNING-logged (rate-limited)
  and we return normally. Audit failure must NOT block message processing.
- Daily cleanup uses pg_try_advisory_lock so only one pod actually runs the
  DELETE — others see lock contention and skip.
- BIGSERIAL primary key + NOW() timestamps from the PG server (not pod clock)
  give a consistent total order across all pods.

Disabled with BOT_AUDIT_LOG=0 (e.g. when a deploy needs to bypass audit).
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import socket
import threading
import time
from typing import Any, Optional

import psycopg2
import psycopg2.extras
import psycopg2.pool

logger = logging.getLogger(__name__)

# Schema: append-only audit table. JSONB extra avoids future ALTER TABLEs for
# small new fields.
_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS audit_log (
    id            BIGSERIAL PRIMARY KEY,
    ts            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    pod_name      TEXT,
    event_type    TEXT NOT NULL,
    open_id       TEXT,
    display_name  TEXT,
    chat_type     TEXT,
    chat_id       TEXT,
    message_id    TEXT,
    is_at_bot     BOOLEAN,
    content       TEXT NOT NULL,
    extra         JSONB
);
CREATE INDEX IF NOT EXISTS idx_audit_ts       ON audit_log (ts DESC);
CREATE INDEX IF NOT EXISTS idx_audit_user_ts  ON audit_log (open_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_audit_chat_ts  ON audit_log (chat_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_audit_msg      ON audit_log (message_id);
"""

_CLEANUP_LOCK_KEY = 0xA1D17  # arbitrary 32-bit key for pg_try_advisory_lock

_DEFAULT_RETENTION_DAYS = 90
_WARN_INTERVAL_SECONDS = 30   # don't spam stdout when PG is down


def is_enabled() -> bool:
    return os.environ.get("BOT_AUDIT_LOG", "1").strip().lower() not in ("0", "off", "false", "no")


class AuditStore:
    def __init__(self, postgres_url: str):
        self._enabled = is_enabled()
        self._pod = os.environ.get("HOSTNAME") or socket.gethostname() or ""
        self._last_warn_ts = 0.0
        self._warn_lock = threading.Lock()
        if not self._enabled:
            logger.info("[audit] disabled via BOT_AUDIT_LOG=0")
            self._pool = None
            return
        # Independent small pool so a slow audit DB can't starve the main pool.
        try:
            self._pool = psycopg2.pool.ThreadedConnectionPool(
                minconn=1, maxconn=5,
                dsn=postgres_url,
                cursor_factory=psycopg2.extras.RealDictCursor,
            )
            self._init_schema()
            logger.info(f"[audit] connected (pod={self._pod}, pool=1-5)")
        except Exception as e:
            # Don't take the bot down because the audit DB is unreachable;
            # subsequent inserts will retry and warn-rate-limit.
            logger.warning(f"[audit] init failed (fail-open): {e}")
            self._pool = None

    def close(self) -> None:
        if self._pool is not None:
            try:
                self._pool.closeall()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(_CREATE_SQL)
            conn.commit()
        finally:
            self._pool.putconn(conn)

    def _warn_rate_limited(self, msg: str) -> None:
        with self._warn_lock:
            now = time.time()
            if now - self._last_warn_ts > _WARN_INTERVAL_SECONDS:
                logger.warning(msg)
                self._last_warn_ts = now

    def _insert(self, event_type: str, *, open_id: str = "", display_name: str = "",
                chat_type: str = "", chat_id: str = "", message_id: str = "",
                is_at_bot: Optional[bool] = None, content: str = "",
                extra: Optional[dict[str, Any]] = None) -> None:
        if not self._enabled or self._pool is None:
            return
        try:
            conn = self._pool.getconn()
        except Exception as e:
            self._warn_rate_limited(f"[audit] pool getconn failed: {e}")
            return
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO audit_log "
                    "(pod_name, event_type, open_id, display_name, chat_type, "
                    " chat_id, message_id, is_at_bot, content, extra) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (self._pod, event_type, open_id, display_name, chat_type,
                     chat_id, message_id, is_at_bot, content,
                     psycopg2.extras.Json(extra) if extra else None),
                )
            conn.commit()
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            self._warn_rate_limited(
                f"[audit] insert failed ({event_type}, open_id={open_id}, "
                f"message_id={message_id}): {e}"
            )
        finally:
            try:
                self._pool.putconn(conn)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # public log_* helpers (called from main.py)
    # ------------------------------------------------------------------

    def log_received(self, open_id: str, display_name: str, chat_type: str,
                     chat_id: str, message_id: str, is_at_bot: bool,
                     content: str) -> None:
        self._insert("received", open_id=open_id, display_name=display_name,
                     chat_type=chat_type, chat_id=chat_id, message_id=message_id,
                     is_at_bot=is_at_bot, content=content)

    def log_replied(self, open_id: str, display_name: str, chat_type: str,
                    chat_id: str, message_id: str, content: str,
                    cost_usd: Optional[float] = None,
                    input_tokens: Optional[int] = None,
                    output_tokens: Optional[int] = None) -> None:
        extra: dict[str, Any] = {}
        if cost_usd is not None:
            extra["cost_usd"] = cost_usd
        if input_tokens is not None:
            extra["input_tokens"] = input_tokens
        if output_tokens is not None:
            extra["output_tokens"] = output_tokens
        self._insert("replied", open_id=open_id, display_name=display_name,
                     chat_type=chat_type, chat_id=chat_id, message_id=message_id,
                     content=content, extra=extra or None)

    def log_poll_recovered(self, open_id: str, display_name: str,
                           chat_type: str, chat_id: str, message_id: str,
                           is_at_bot: bool, content: str) -> None:
        self._insert("poll_recovered", open_id=open_id, display_name=display_name,
                     chat_type=chat_type, chat_id=chat_id, message_id=message_id,
                     is_at_bot=is_at_bot, content=content)

    def log_security_refusal(self, open_id: str, display_name: str,
                             chat_type: str, chat_id: str, message_id: str,
                             user_text: str, refusal_text: str) -> None:
        self._insert("security_refusal", open_id=open_id, display_name=display_name,
                     chat_type=chat_type, chat_id=chat_id, message_id=message_id,
                     content=user_text,
                     extra={"refusal_reply": refusal_text})

    def log_claude_error(self, open_id: str, display_name: str, chat_type: str,
                         chat_id: str, message_id: str, error_text: str) -> None:
        self._insert("claude_error", open_id=open_id, display_name=display_name,
                     chat_type=chat_type, chat_id=chat_id, message_id=message_id,
                     content=error_text)

    # ------------------------------------------------------------------
    # query helpers (used by scripts/audit_query.py and tests)
    # ------------------------------------------------------------------

    def query(self, *, open_id: str = "", chat_id: str = "",
              message_id: str = "", event_type: str = "",
              since: Optional[datetime.datetime] = None,
              grep: str = "", limit: int = 100) -> list[dict]:
        if not self._enabled or self._pool is None:
            return []
        sql = "SELECT * FROM audit_log WHERE TRUE"
        args: list[Any] = []
        if open_id:
            sql += " AND open_id = %s"; args.append(open_id)
        if chat_id:
            sql += " AND chat_id = %s"; args.append(chat_id)
        if message_id:
            sql += " AND message_id = %s"; args.append(message_id)
        if event_type:
            sql += " AND event_type = %s"; args.append(event_type)
        if since:
            sql += " AND ts >= %s"; args.append(since)
        if grep:
            sql += " AND content ILIKE %s"; args.append(f"%{grep}%")
        sql += " ORDER BY ts DESC LIMIT %s"
        args.append(limit)

        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, args)
                rows = cur.fetchall()
            return [dict(r) for r in rows]
        finally:
            self._pool.putconn(conn)

    # ------------------------------------------------------------------
    # cleanup (single-leader via advisory lock)
    # ------------------------------------------------------------------

    def cleanup_older_than(self, days: int) -> int:
        """Delete rows older than `days` days. Returns the row count actually
        deleted, or 0 if another pod is already running cleanup.

        Uses pg_try_advisory_lock so only one pod across the fleet performs
        the DELETE per scheduling tick — the rest see the lock taken and skip.
        """
        if not self._enabled or self._pool is None or days <= 0:
            return 0
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_lock(%s)", (_CLEANUP_LOCK_KEY,))
                got_lock = cur.fetchone()
                got = bool(list(got_lock.values())[0]) if isinstance(got_lock, dict) else bool(got_lock[0])
                if not got:
                    return 0
                try:
                    cur.execute(
                        "DELETE FROM audit_log WHERE ts < NOW() - %s::interval",
                        (f"{days} days",),
                    )
                    deleted = cur.rowcount
                    conn.commit()
                    return deleted
                finally:
                    cur.execute("SELECT pg_advisory_unlock(%s)", (_CLEANUP_LOCK_KEY,))
                    conn.commit()
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            self._warn_rate_limited(f"[audit] cleanup failed: {e}")
            return 0
        finally:
            try:
                self._pool.putconn(conn)
            except Exception:
                pass
