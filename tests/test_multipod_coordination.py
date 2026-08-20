"""Unit tests for multi-pod coordination behavior."""
import time
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from src.main import handle_message


class LockingStore:
    def __init__(self):
        self.users = {"ou_test": {"auth_status": "authorized", "pending_at": None, "session_id": None}}
        self.seen = set()
        self.completed = []
        self.locked_keys = []

    def claim_message(self, message_id, reclaim_after_seconds=1800, owner=""):
        if message_id in self.seen:
            return False
        self.seen.add(message_id)
        return True

    def complete_message(self, message_id):
        self.completed.append(message_id)

    @contextmanager
    def conversation_lock(self, key):
        self.locked_keys.append(key)
        yield

    def get_user(self, open_id):
        return self.users.get(open_id)

    def get_display_name(self, open_id):
        return ""

    def set_display_name(self, open_id, name):
        pass

    def is_meegle_pending_expired(self, open_id):
        return False

    def is_pending_expired(self, open_id):
        return False

    def get_session_id(self, open_id):
        return None

    def get_thread_session(self, key):
        return None


def _event(message_id="mid_1"):
    return {
        "open_id": "ou_test",
        "text": "hello",
        "message_id": message_id,
        "chat_id": "oc_p2p",
        "chat_type": "p2p",
        "create_time": str(int(time.time() * 1000) + 60_000),
    }


def test_handle_message_uses_reclaimable_claim_and_completes_after_processing():
    store = LockingStore()
    auth = MagicMock()
    agent = MagicMock()

    with patch("src.main._stream_claude_inner", return_value="card_1") as stream:
        handle_message(_event("mid_claim"), store, auth, agent, "app", "secret")

    stream.assert_called_once()
    assert store.completed == ["mid_claim"]


def test_handle_message_serializes_claude_by_context_id():
    store = LockingStore()
    auth = MagicMock()
    agent = MagicMock()

    with patch("src.main._stream_claude_inner", return_value="card_1"):
        handle_message(_event("mid_lock"), store, auth, agent, "app", "secret")

    assert store.locked_keys == ["ou_test"]


def test_inline_pending_poll_resumes_original_job_not_current_message():
    store = LockingStore()
    store.users["ou_test"] = {
        "auth_status": "pending",
        "pending_code": "dev1",
        "pending_url": "http://auth",
        "pending_at": "2026-06-08T00:00:00+00:00",
        "session_id": None,
        "meegle_auth_status": "none",
    }
    store.auth_jobs = [
        {
            "id": "job1",
            "context_id": "ou_test",
            "provider": "lark",
            "device_code": "dev1",
            "resume_text": "original message",
            "reply_id": "",
            "thread_key": "",
            "root_id": "",
            "chat_id": "oc_p2p",
            "chat_type": "p2p",
            "existing_msg_id": "",
            "status": "pending",
        }
    ]

    def claim_auth_resume_job(context_id, provider, device_code, owner):
        for job in store.auth_jobs:
            if (
                job["status"] == "pending"
                and job["context_id"] == context_id
                and job["provider"] == provider
                and job["device_code"] == device_code
            ):
                job["status"] = "claimed"
                return dict(job)
        return None

    store.claim_auth_resume_job = claim_auth_resume_job
    store.consume_auth_resume_job = lambda job_id: None
    store.fail_auth_resume_job = lambda job_id, error: None
    auth = MagicMock()
    auth.poll_once.return_value = True
    agent = MagicMock()
    event = _event("mid_current")
    event["text"] = "[image]"

    with patch("src.main._stream_claude_inner", return_value="card_1") as stream:
        handle_message(event, store, auth, agent, "app", "secret")

    stream.assert_called_once()
    assert stream.call_args.args[1] == "original message"
