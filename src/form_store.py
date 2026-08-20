import datetime
import hashlib
import json
import uuid
from typing import Any

import psycopg2
import psycopg2.extras
import psycopg2.pool

from src.card_forms import validate_form_schema


CREATE_FORM_SESSIONS = """
CREATE TABLE IF NOT EXISTS form_sessions (
    id                 TEXT PRIMARY KEY,
    context_id         TEXT NOT NULL,
    operator_open_id   TEXT NOT NULL,
    chat_id            TEXT NOT NULL DEFAULT '',
    chat_type          TEXT NOT NULL DEFAULT 'p2p',
    reply_msg_id       TEXT NOT NULL DEFAULT '',
    root_id            TEXT NOT NULL DEFAULT '',
    thread_session_key TEXT NOT NULL DEFAULT '',
    message_id         TEXT NOT NULL DEFAULT '',
    card_id            TEXT NOT NULL DEFAULT '',
    card_sequence      INTEGER NOT NULL DEFAULT 0,
    status             TEXT NOT NULL DEFAULT 'active',
    current_index      INTEGER NOT NULL DEFAULT 0,
    questions_json     JSONB NOT NULL,
    answers_json       JSONB NOT NULL DEFAULT '{}'::jsonb,
    original_text      TEXT NOT NULL DEFAULT '',
    created_at         TIMESTAMPTZ NOT NULL,
    updated_at         TIMESTAMPTZ NOT NULL,
    completed_at       TIMESTAMPTZ
)
"""

CREATE_FORM_ACTION_EVENTS = """
CREATE TABLE IF NOT EXISTS form_action_events (
    event_id      TEXT PRIMARY KEY,
    session_id    TEXT NOT NULL,
    action        TEXT NOT NULL,
    received_at   TIMESTAMPTZ NOT NULL
)
"""


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def stable_event_id(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    return f"form_evt_{digest}"


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return json.loads(value)
    return dict(value or {})


class FormStore:
    def __init__(self, postgres_url: str):
        self._pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=1,
            maxconn=10,
            dsn=postgres_url,
            cursor_factory=psycopg2.extras.RealDictCursor,
        )
        self._init_schema()

    def _conn(self):
        return self._pool.getconn()

    def _put(self, conn):
        self._pool.putconn(conn)

    def close(self):
        self._pool.closeall()

    def _init_schema(self):
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(CREATE_FORM_SESSIONS)
                cur.execute(CREATE_FORM_ACTION_EVENTS)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._put(conn)

    @staticmethod
    def _row_to_session(row: dict[str, Any] | None) -> dict[str, Any] | None:
        if not row:
            return None
        data = dict(row)
        data["schema"] = _json_dict(data.pop("questions_json"))
        data["answers"] = _json_dict(data.pop("answers_json"))
        return data

    def create_session(
        self,
        *,
        context_id: str,
        operator_open_id: str,
        chat_id: str,
        chat_type: str,
        reply_msg_id: str,
        root_id: str,
        thread_session_key: str,
        message_id: str,
        card_id: str,
        original_text: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        schema = validate_form_schema(schema)
        session_id = f"form_{uuid.uuid4().hex}"
        now = _now()
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO form_sessions
                    (id, context_id, operator_open_id, chat_id, chat_type, reply_msg_id,
                     root_id, thread_session_key, message_id, card_id, status,
                     current_index, questions_json, answers_json, original_text,
                     created_at, updated_at)
                    VALUES
                    (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'active',
                     0, %s, '{}'::jsonb, %s, %s, %s)
                    RETURNING *
                    """,
                    (
                        session_id,
                        context_id,
                        operator_open_id,
                        chat_id or "",
                        chat_type or "p2p",
                        reply_msg_id or "",
                        root_id or "",
                        thread_session_key or "",
                        message_id or "",
                        card_id or "",
                        psycopg2.extras.Json(schema),
                        original_text or "",
                        now,
                        now,
                    ),
                )
                row = cur.fetchone()
            conn.commit()
            return self._row_to_session(row)
        except Exception:
            conn.rollback()
            raise
        finally:
            self._put(conn)

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM form_sessions WHERE id = %s", (session_id,))
                row = cur.fetchone()
            return self._row_to_session(row)
        finally:
            self._put(conn)

    def record_event(self, event_id: str, session_id: str, action: str) -> bool:
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO form_action_events (event_id, session_id, action, received_at)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (event_id) DO NOTHING
                    """,
                    (event_id, session_id, action, _now()),
                )
                inserted = cur.rowcount == 1
            conn.commit()
            return inserted
        except Exception:
            conn.rollback()
            raise
        finally:
            self._put(conn)

    def next_card_sequence(self, session_id: str) -> int:
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE form_sessions
                    SET card_sequence = card_sequence + 1, updated_at = %s
                    WHERE id = %s
                    RETURNING card_sequence
                    """,
                    (_now(), session_id),
                )
                row = cur.fetchone()
            conn.commit()
            if not row:
                raise KeyError(session_id)
            return int(row["card_sequence"])
        except Exception:
            conn.rollback()
            raise
        finally:
            self._put(conn)

    def set_card_id(self, session_id: str, card_id: str) -> dict[str, Any]:
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE form_sessions
                    SET card_id = %s, updated_at = %s
                    WHERE id = %s
                    RETURNING *
                    """,
                    (card_id or "", _now(), session_id),
                )
                row = cur.fetchone()
            conn.commit()
            if not row:
                raise KeyError(session_id)
            return self._row_to_session(row)
        except Exception:
            conn.rollback()
            raise
        finally:
            self._put(conn)

    def apply_previous(self, session_id: str) -> dict[str, Any]:
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM form_sessions WHERE id = %s FOR UPDATE", (session_id,))
                row = cur.fetchone()
                if not row:
                    raise KeyError(session_id)
                if row["status"] != "active":
                    conn.commit()
                    return self._row_to_session(row)
                next_index = max(0, int(row["current_index"]) - 1)
                cur.execute(
                    """
                    UPDATE form_sessions
                    SET current_index = %s, updated_at = %s
                    WHERE id = %s
                    RETURNING *
                    """,
                    (next_index, _now(), session_id),
                )
                updated = cur.fetchone()
            conn.commit()
            return self._row_to_session(updated)
        except Exception:
            conn.rollback()
            raise
        finally:
            self._put(conn)

    def apply_submit(
        self,
        session_id: str,
        question_index: int,
        answer: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM form_sessions WHERE id = %s FOR UPDATE", (session_id,))
                row = cur.fetchone()
                if not row:
                    raise KeyError(session_id)
                if row["status"] != "active":
                    conn.commit()
                    return self._row_to_session(row), False

                schema = _json_dict(row["questions_json"])
                if question_index < 0 or question_index >= len(schema["questions"]):
                    raise ValueError("question_index out of range")
                answers = _json_dict(row["answers_json"])
                question = schema["questions"][question_index]
                answers[question["id"]] = answer
                is_last = question_index >= len(schema["questions"]) - 1
                status = "returning" if is_last else "active"
                next_index = question_index if is_last else question_index + 1
                completed_at = _now() if is_last else None
                cur.execute(
                    """
                    UPDATE form_sessions
                    SET answers_json = %s,
                        current_index = %s,
                        status = %s,
                        updated_at = %s,
                        completed_at = COALESCE(%s, completed_at)
                    WHERE id = %s
                    RETURNING *
                    """,
                    (
                        psycopg2.extras.Json(answers),
                        next_index,
                        status,
                        _now(),
                        completed_at,
                        session_id,
                    ),
                )
                updated = cur.fetchone()
            conn.commit()
            return self._row_to_session(updated), is_last
        except Exception:
            conn.rollback()
            raise
        finally:
            self._put(conn)

    def mark_completed(self, session_id: str) -> None:
        self._set_status(session_id, "completed")

    def mark_failed(self, session_id: str) -> None:
        self._set_status(session_id, "failed")

    def _set_status(self, session_id: str, status: str) -> None:
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE form_sessions SET status = %s, updated_at = %s WHERE id = %s",
                    (status, _now(), session_id),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._put(conn)
