# tests/test_user_store.py
import os
import pytest

_PG_URL = os.environ.get("POSTGRES_TEST_URL", "")
pytestmark = pytest.mark.skipif(not _PG_URL, reason="POSTGRES_TEST_URL not set — skipping integration tests")

from src.user_store import UserStore

@pytest.fixture
def store():
    s = UserStore(_PG_URL)
    conn = s._conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM auth_resume_jobs")
            cur.execute("DELETE FROM sessions")
            cur.execute("DELETE FROM seen_messages")
            cur.execute("DELETE FROM monthly_usage")
            cur.execute("DELETE FROM users")
        conn.commit()
    finally:
        s._put(conn)
    yield s
    s.close()

def test_get_user_returns_none_for_unknown(store):
    assert store.get_user("unknown_id") is None

def test_upsert_and_get_user(store):
    store.upsert_user("u1", auth_status="pending", pending_code="abc", pending_url="http://example.com")
    user = store.get_user("u1")
    assert user["auth_status"] == "pending"
    assert user["pending_code"] == "abc"

def test_mark_authorized(store):
    store.upsert_user("u1", auth_status="pending")
    store.mark_authorized("u1")
    user = store.get_user("u1")
    assert user["auth_status"] == "authorized"
    assert user["authorized_at"] is not None

def test_is_auth_pending_timeout(store):
    """pending_at 超过 10 分钟视为超时"""
    import datetime
    old_time = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=11)).isoformat()
    store.upsert_user("u1", auth_status="pending", pending_at=old_time)
    assert store.is_pending_expired("u1") is True

def test_is_auth_pending_not_timeout(store):
    import datetime
    recent = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=3)).isoformat()
    store.upsert_user("u1", auth_status="pending", pending_at=recent)
    assert store.is_pending_expired("u1") is False

def test_reset_auth(store):
    """reset_auth 应将已授权用户状态重置为 pending"""
    store.upsert_user("u1", auth_status="authorized")
    store.reset_auth("u1")
    user = store.get_user("u1")
    assert user["auth_status"] == "pending"
    assert user["authorized_at"] is None

def test_mark_authorized_clears_pending_at(store):
    import datetime
    old_time = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=11)).isoformat()
    store.upsert_user("u1", auth_status="pending", pending_at=old_time)
    store.mark_authorized("u1")
    assert store.is_pending_expired("u1") is False
    user = store.get_user("u1")
    assert user["pending_at"] is None


def test_is_pending_expired_zombie_state(store):
    """reset_auth 后遗留的僵尸态（pending + no pending_at）视为已过期，触发重新授权"""
    store.reset_auth("u1")
    user = store.get_user("u1")
    assert user["auth_status"] == "pending"
    assert user["pending_at"] is None
    assert store.is_pending_expired("u1") is True


def test_is_pending_expired_authorized_no_pending_at(store):
    """authorized 用户即使 pending_at 为空，也不视为过期"""
    store.upsert_user("u1", auth_status="authorized")
    assert store.is_pending_expired("u1") is False


# --- Thread session tests (group chat) ---

def test_get_thread_session_returns_none_for_unknown(store):
    assert store.get_thread_session("g_oc_123_ou_xyz:om_root") is None


def test_set_and_get_thread_session(store):
    key = "g_oc_123_ou_xyz:om_root_abc"
    store.set_thread_session(key, "session_111")
    assert store.get_thread_session(key) == "session_111"


def test_set_thread_session_overwrites(store):
    key = "g_oc_123_ou_xyz:om_root_abc"
    store.set_thread_session(key, "session_111")
    store.set_thread_session(key, "session_222")
    assert store.get_thread_session(key) == "session_222"


def test_thread_sessions_are_independent(store):
    key1 = "g_oc_123_ou_xyz:om_root_A"
    key2 = "g_oc_123_ou_xyz:om_root_B"
    store.set_thread_session(key1, "sess_A")
    store.set_thread_session(key2, "sess_B")
    assert store.get_thread_session(key1) == "sess_A"
    assert store.get_thread_session(key2) == "sess_B"


def test_thread_session_does_not_affect_p2p_session(store):
    store.upsert_user("ou_xyz", auth_status="authorized")
    store.set_session_id("ou_xyz", "p2p_sess")
    store.set_thread_session("g_oc_123_ou_xyz:om_root", "group_sess")
    assert store.get_session_id("ou_xyz") == "p2p_sess"
    assert store.get_thread_session("g_oc_123_ou_xyz:om_root") == "group_sess"


def test_create_and_claim_auth_resume_job_once(store):
    job_id = store.create_auth_resume_job(
        context_id="ou_u1",
        provider="meegle",
        device_code="dev1",
        client_id="client1",
        resume_text="create a requirement",
        reply_id="om_reply",
        thread_key="thread_1",
        root_id="root_1",
        chat_id="oc_chat",
        chat_type="p2p",
        existing_msg_id="om_auth_card",
    )

    first = store.claim_auth_resume_job(
        context_id="ou_u1",
        provider="meegle",
        device_code="dev1",
        owner="pod-a",
    )
    second = store.claim_auth_resume_job(
        context_id="ou_u1",
        provider="meegle",
        device_code="dev1",
        owner="pod-b",
    )

    assert first is not None
    assert first["id"] == job_id
    assert first["resume_text"] == "create a requirement"
    assert first["existing_msg_id"] == "om_auth_card"
    assert second is None


def test_claim_auth_resume_job_rejects_wrong_device_code(store):
    store.create_auth_resume_job(
        context_id="ou_u1",
        provider="lark",
        device_code="dev1",
        client_id="",
        resume_text="original request",
        reply_id="",
        thread_key="",
        root_id="",
        chat_id="",
        chat_type="p2p",
        existing_msg_id="",
    )

    claimed = store.claim_auth_resume_job(
        context_id="ou_u1",
        provider="lark",
        device_code="other-dev",
        owner="pod-a",
    )

    assert claimed is None
