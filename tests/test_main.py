# tests/test_main.py
"""
Tests for handle_message routing logic.

handle_message signature:
    handle_message(event, store, auth, agent, app_id, app_secret,
                   executor=None, bot_open_id="")

Routing:
  - New user (user is None) + not already authenticated → start_auth(), executor.submit(auth-poll)
  - Pending expired + not authenticated → same
  - Pending not expired → poll_once() or start_auth() again
  - Authorized → _stream_claude()
  - TokenExpiredError from _stream_claude → reset_auth() + start_auth()
"""
import datetime
import logging
import os
import pytest
from contextlib import contextmanager
from unittest.mock import patch, MagicMock

from src.main import handle_message

APP_ID = "app_id_test"
APP_SECRET = "app_secret_test"


@pytest.fixture(autouse=True)
def clear_module_state():
    """Reset module-level state between tests to prevent cross-test interference."""
    import src.main as main
    main._unmatched_group_log_at.clear()
    yield


# A fake device-code JSON matching lark-cli --recommend --no-wait output
_FAKE_AUTH_DATA = {
    "device_code": "dev_abc",
    "verification_url": "https://auth.example.com/oauth",
    "user_code": "TEST-1234",
}


def _make_mock_store():
    """In-memory mock of UserStore for testing routing logic."""
    store = MagicMock()
    _users = {}
    _sessions = {}
    _seen = set()
    _auth_resume_jobs = []

    def upsert_user(open_id, **kwargs):
        if open_id not in _users:
            _users[open_id] = {
                "open_id": open_id, "auth_status": "pending",
                "pending_code": None, "pending_url": None,
                "pending_at": None, "authorized_at": None,
                "session_id": None, "pending_session_reset": 0,
                "display_name": "",
                "meegle_auth_status": "none",
                "meegle_authorized_at": None,
                "meegle_pending_code": None,
                "meegle_pending_client_id": None,
                "meegle_pending_url": None,
                "meegle_pending_at": None,
            }
        _users[open_id].update(kwargs)

    def get_user(open_id):
        return _users.get(open_id)

    def mark_authorized(open_id):
        if open_id in _users:
            _users[open_id]["auth_status"] = "authorized"
            _users[open_id]["authorized_at"] = datetime.datetime.now(datetime.UTC).isoformat()
            _users[open_id]["pending_at"] = None

    def reset_auth(open_id):
        upsert_user(open_id, auth_status="pending", authorized_at=None, pending_at=None)

    def reset_meegle_auth(open_id):
        upsert_user(open_id, meegle_auth_status="none", meegle_authorized_at=None,
                    meegle_pending_code=None, meegle_pending_client_id=None,
                    meegle_pending_url=None, meegle_pending_at=None)

    def mark_meegle_authorized(open_id):
        upsert_user(open_id, meegle_auth_status="authorized",
                    meegle_authorized_at=datetime.datetime.now(datetime.UTC).isoformat(),
                    meegle_pending_code=None, meegle_pending_client_id=None,
                    meegle_pending_url=None, meegle_pending_at=None)

    def is_pending_expired(open_id):
        u = _users.get(open_id)
        if not u or u["auth_status"] != "pending":
            return False
        if u["pending_at"] is None:
            return True
        from src.user_store import _as_aware_dt
        pa = _as_aware_dt(u["pending_at"])
        return (datetime.datetime.now(datetime.UTC) - pa).total_seconds() > 600

    def is_meegle_pending_expired(open_id):
        u = _users.get(open_id)
        if not u or u.get("meegle_auth_status") != "pending":
            return False
        if u.get("meegle_pending_at") is None:
            return True
        from src.user_store import _as_aware_dt
        pa = _as_aware_dt(u["meegle_pending_at"])
        return (datetime.datetime.now(datetime.UTC) - pa).total_seconds() > 600

    def get_session_id(open_id):
        u = _users.get(open_id)
        return u["session_id"] if u else None

    def set_session_id(open_id, sid):
        if open_id in _users:
            _users[open_id]["session_id"] = sid

    def mark_message_seen(msg_id):
        if msg_id in _seen:
            return False
        _seen.add(msg_id)
        return True

    def claim_message(msg_id, reclaim_after_seconds=1800, owner=""):
        return mark_message_seen(msg_id)

    def complete_message(msg_id):
        pass

    @contextmanager
    def conversation_lock(key):
        yield

    def get_thread_session(key):
        return _sessions.get(key)

    def set_thread_session(key, sid):
        _sessions[key] = sid

    def create_auth_resume_job(**kwargs):
        job = {
            "id": f"job_{len(_auth_resume_jobs) + 1}",
            "status": "pending",
            "existing_msg_id": "",
            **kwargs,
        }
        _auth_resume_jobs.append(job)
        return job["id"]

    def claim_auth_resume_job(context_id, provider, device_code, owner):
        for job in reversed(_auth_resume_jobs):
            if (
                job.get("status") == "pending"
                and job.get("context_id") == context_id
                and job.get("provider") == provider
                and job.get("device_code") == device_code
            ):
                job["status"] = "claimed"
                job["claimed_by"] = owner
                return dict(job)
        return None

    def consume_auth_resume_job(job_id):
        for job in _auth_resume_jobs:
            if job["id"] == job_id:
                job["status"] = "consumed"
                return

    def fail_auth_resume_job(job_id, error):
        for job in _auth_resume_jobs:
            if job["id"] == job_id:
                job["status"] = "failed"
                job["error"] = error
                return

    def set_auth_resume_existing_msg_id(context_id, provider, device_code, existing_msg_id):
        for job in _auth_resume_jobs:
            if (
                job.get("status") == "pending"
                and job.get("context_id") == context_id
                and job.get("provider") == provider
                and job.get("device_code") == device_code
            ):
                job["existing_msg_id"] = existing_msg_id

    store.upsert_user = upsert_user
    store.get_user = get_user
    store.mark_authorized = mark_authorized
    store.reset_auth = reset_auth
    store.reset_meegle_auth = reset_meegle_auth
    store.mark_meegle_authorized = mark_meegle_authorized
    store.is_pending_expired = is_pending_expired
    store.is_meegle_pending_expired = is_meegle_pending_expired
    store.get_session_id = get_session_id
    store.set_session_id = set_session_id
    store.mark_message_seen = mark_message_seen
    store.claim_message = claim_message
    store.complete_message = complete_message
    store.conversation_lock = conversation_lock
    store.get_thread_session = get_thread_session
    store.set_thread_session = set_thread_session
    store.create_auth_resume_job = create_auth_resume_job
    store.claim_auth_resume_job = claim_auth_resume_job
    store.consume_auth_resume_job = consume_auth_resume_job
    store.fail_auth_resume_job = fail_auth_resume_job
    store.set_auth_resume_existing_msg_id = set_auth_resume_existing_msg_id
    store.close = MagicMock()
    return store


@pytest.fixture
def store():
    return _make_mock_store()


@pytest.fixture
def auth(store):
    from src.auth import AuthManager
    return AuthManager(store, users_dir="/fake/users", bot_home="/fake/bot")


@pytest.fixture
def agent(tmp_path):
    from src.agent import Agent
    users_dir = str(tmp_path / "users")
    bot_home = str(tmp_path / "bot")
    import os
    os.makedirs(users_dir, exist_ok=True)
    os.makedirs(bot_home, exist_ok=True)
    return Agent(users_dir=users_dir, bot_home=bot_home, model="test")


def _event(open_id="ou_test", text="你好", message_id="mid_1"):
    """Minimal P2P message event that passes startup + dedup checks."""
    return {
        "open_id": open_id,
        "text": text,
        "message_id": message_id,
        "chat_id": "oc_p2p_fake",
        "chat_type": "p2p",
    }


def test_new_user_triggers_auth(store, auth, agent):
    """新用户应触发授权流程：start_auth 被调用，auth_executor.submit 被调用"""
    mock_executor = MagicMock()
    mock_auth_executor = MagicMock()

    with patch.object(auth, "is_authenticated", return_value=False), \
         patch.object(auth, "start_auth", return_value=_FAKE_AUTH_DATA) as mock_start:
        handle_message(
            _event("ou_new"), store, auth, agent,
            APP_ID, APP_SECRET, mock_executor,
            auth_executor=mock_auth_executor,
        )

    mock_start.assert_called_once_with("ou_new")
    mock_auth_executor.submit.assert_called_once()


def test_new_user_auth_persists_resume_job(store, auth, agent):
    event = _event("ou_new_resume", text="original lark request", message_id="mid_resume")
    mock_auth_executor = MagicMock()

    with patch.object(auth, "is_authenticated", return_value=False), \
         patch.object(auth, "start_auth", return_value=_FAKE_AUTH_DATA):
        handle_message(
            event, store, auth, agent,
            APP_ID, APP_SECRET, MagicMock(),
            auth_executor=mock_auth_executor,
        )

    claimed = store.claim_auth_resume_job(
        context_id="ou_new_resume",
        provider="lark",
        device_code=_FAKE_AUTH_DATA["device_code"],
        owner="test",
    )
    assert claimed is not None
    assert claimed["resume_text"] == "original lark request"


def test_new_user_already_authenticated_skips_auth_flow(store, auth, agent):
    """如果 lark-cli 已有令牌，应直接标记授权并继续（不调用 start_auth）"""
    mock_executor = MagicMock()

    with patch.object(auth, "is_authenticated", return_value=True), \
         patch.object(auth, "start_auth") as mock_start, \
         patch("src.main._stream_claude", return_value="msg_card_1"):
        handle_message(
            _event("ou_new2"), store, auth, agent,
            APP_ID, APP_SECRET, mock_executor,
        )

    mock_start.assert_not_called()


def test_expired_pending_triggers_reauth(store, auth, agent):
    """pending 超时的用户应重新触发授权"""
    old_time = (datetime.datetime.now(datetime.UTC)
                - datetime.timedelta(minutes=11)).isoformat()
    store.upsert_user("ou_exp", auth_status="pending",
                      pending_code="old", pending_at=old_time)

    mock_executor = MagicMock()
    mock_auth_executor = MagicMock()

    with patch.object(auth, "is_authenticated", return_value=False), \
         patch.object(auth, "start_auth", return_value=_FAKE_AUTH_DATA) as mock_start:
        handle_message(
            _event("ou_exp"), store, auth, agent,
            APP_ID, APP_SECRET, mock_executor,
            auth_executor=mock_auth_executor,
        )

    mock_start.assert_called_once()
    mock_auth_executor.submit.assert_called_once()


def test_authorized_user_calls_stream_claude(store, auth, agent):
    """已授权用户发消息应调用 _stream_claude"""
    store.upsert_user("ou_auth", auth_status="authorized")
    mock_executor = MagicMock()

    with patch.object(auth, "is_authenticated", return_value=True), \
         patch("src.main._stream_claude", return_value="msg_card_1") as mock_stream:
        handle_message(
            _event("ou_auth"), store, auth, agent,
            APP_ID, APP_SECRET, mock_executor,
        )

    mock_stream.assert_called_once()
    kwargs = mock_stream.call_args
    assert kwargs.args[0] == "ou_auth" or kwargs.kwargs.get("open_id") == "ou_auth"


def test_meegle_related_request_resets_stale_db_authorized_state(store, auth, agent):
    """Meegle 请求前如果 DB authorized 但 CLI 无凭证，应只清 DB 状态。"""
    store.upsert_user("ou_meegle_stale", auth_status="authorized",
                      meegle_auth_status="authorized")
    auth.meegle_auth_status = MagicMock(return_value={
        "authenticated": False,
        "retryable": False,
        "reason": "no_local_token",
    })

    with patch.object(auth, "is_authenticated", return_value=True), \
         patch("src.main._stream_claude", return_value="msg_card_1") as mock_stream:
        handle_message(
            _event("ou_meegle_stale", text="create a requirement in feishu project"),
            store, auth, agent, APP_ID, APP_SECRET, MagicMock(),
        )

    mock_stream.assert_called_once()
    assert store.get_user("ou_meegle_stale")["meegle_auth_status"] == "none"


def test_meegle_related_request_keeps_db_state_on_retryable_probe(store, auth, agent):
    """Meegle status 网络/服务端探测失败时，不应把 DB 状态误清成未授权。"""
    store.upsert_user("ou_meegle_retry", auth_status="authorized",
                      meegle_auth_status="authorized")
    auth.meegle_auth_status = MagicMock(return_value={
        "authenticated": False,
        "retryable": True,
        "reason": "server_unreachable_or_error",
    })

    with patch.object(auth, "is_authenticated", return_value=True), \
         patch("src.main._stream_claude", return_value="msg_card_1"):
        handle_message(
            _event("ou_meegle_retry", text="飞书项目里创建一个需求", message_id="mid_retry"),
            store, auth, agent, APP_ID, APP_SECRET, MagicMock(),
        )

    assert store.get_user("ou_meegle_retry")["meegle_auth_status"] == "authorized"


def test_meegle_related_request_marks_db_authorized_when_cli_has_valid_token(store, auth, agent):
    """Meegle 请求前如果 DB 非 authorized 但 CLI 真实已授权，应回填 DB，避免重复 OAuth。"""
    store.upsert_user("ou_meegle_cli_valid", auth_status="authorized",
                      meegle_auth_status="none")
    auth.meegle_auth_status = MagicMock(return_value={
        "authenticated": True,
        "retryable": False,
        "host": "project.feishu.cn",
        "expires_in_minutes": 119,
    })

    with patch.object(auth, "is_authenticated", return_value=True), \
         patch("src.main._stream_claude", return_value="msg_card_1") as mock_stream:
        handle_message(
            _event("ou_meegle_cli_valid", text="飞书项目里创建一个需求", message_id="mid_cli_valid"),
            store, auth, agent, APP_ID, APP_SECRET, MagicMock(),
        )

    mock_stream.assert_called_once()
    user = store.get_user("ou_meegle_cli_valid")
    assert user["meegle_auth_status"] == "authorized"
    assert user["meegle_pending_code"] is None
    auth.meegle_auth_status.assert_called_once_with("ou_meegle_cli_valid")


def test_reset_does_not_change_meegle_auth_state(store, auth, agent):
    """/reset 只重置对话，不应修改 Meegle 授权状态。"""
    store.upsert_user("ou_meegle_reset", auth_status="authorized",
                      meegle_auth_status="authorized", session_id="old-session")
    auth.meegle_auth_status = MagicMock(return_value={
        "authenticated": False,
        "retryable": False,
        "reason": "no_local_token",
    })

    with patch.object(auth, "is_authenticated", return_value=True), \
         patch("src.main.send_feishu_message"):
        handle_message(
            _event("ou_meegle_reset", text="/reset", message_id="mid_reset"),
            store, auth, agent, APP_ID, APP_SECRET, MagicMock(),
        )

    user = store.get_user("ou_meegle_reset")
    assert user["session_id"] is None
    assert user["meegle_auth_status"] == "authorized"
    auth.meegle_auth_status.assert_not_called()


def test_meegle_reauth_resets_meegle_auth_state(store, auth, agent):
    """/meegle-reauth 才是 Meegle 授权重置入口。"""
    store.upsert_user("ou_meegle_reauth", auth_status="authorized",
                      meegle_auth_status="authorized", session_id="keep-session",
                      meegle_pending_code="old-code", meegle_pending_client_id="old-client",
                      meegle_pending_url="https://old.example", meegle_pending_at="2026-01-01T00:00:00+00:00")

    with patch.object(auth, "is_authenticated", return_value=True), \
         patch.object(auth, "revoke_meegle_token") as revoke, \
         patch("src.main.send_feishu_message"):
        handle_message(
            _event("ou_meegle_reauth", text="/meegle-reauth", message_id="mid_meegle_reauth"),
            store, auth, agent, APP_ID, APP_SECRET, MagicMock(),
        )

    revoke.assert_called_once_with("ou_meegle_reauth")
    user = store.get_user("ou_meegle_reauth")
    assert user["session_id"] == "keep-session"
    assert user["meegle_auth_status"] == "none"
    assert user["meegle_pending_code"] is None
    assert user["meegle_pending_client_id"] is None
    assert user["meegle_pending_url"] is None
    assert user["meegle_pending_at"] is None


def test_token_expired_resets_and_reauths(store, auth, agent):
    """_stream_claude 抛出 TokenExpiredError 时应重置状态并重新发起授权"""
    from src.lark_runner import TokenExpiredError

    store.upsert_user("ou_exp2", auth_status="authorized")
    mock_executor = MagicMock()

    with patch("src.main._stream_claude", side_effect=TokenExpiredError("expired")), \
         patch.object(auth, "is_authenticated", return_value=False), \
         patch.object(auth, "start_auth", return_value=_FAKE_AUTH_DATA) as mock_start:
        handle_message(
            _event("ou_exp2"), store, auth, agent,
            APP_ID, APP_SECRET, mock_executor,
        )

    mock_start.assert_called_once_with("ou_exp2")
    user = store.get_user("ou_exp2")
    assert user["auth_status"] == "pending"


def test_start_auth_and_poll_chains_reauth_if_claude_triggers_it(store, auth, agent):
    """
    回归测试：_start_auth_and_poll 完成初始授权后，若 Claude 在 _stream_claude 中
    触发了 lark 重授权（写入新 pending 状态），应自动启动 _poll_lark_reauth_and_resume。
    """
    from src.main import _start_auth_and_poll

    open_id = "ou_9f7ac0026a8f33fa82f818d880ca8b21"
    context_id = f"g_oc_54afb0130474afdd4fe07ed82179cbff_{open_id}"
    initial_code = "device_code_initial"
    reauth_code = "device_code_reauth"
    verify_url = "https://open.feishu.cn/open-apis/auth/v3/device/code"

    def fake_stream_claude(*args, **kwargs):
        now = datetime.datetime.now(datetime.UTC).isoformat()
        store.upsert_user(context_id, auth_status="pending",
                          pending_code=reauth_code,
                          pending_url=verify_url,
                          pending_at=now)
        return "card_id_generated"

    store.create_auth_resume_job(
        context_id=context_id,
        provider="lark",
        device_code=initial_code,
        client_id="",
        resume_text="查找文档",
        reply_id="",
        thread_key="",
        root_id="",
        chat_id="oc_54afb0130474afdd4fe07ed82179cbff",
        chat_type="group",
        existing_msg_id="",
    )

    with patch("src.main.feishu_api") as mock_api, \
         patch.object(auth, "poll_once", return_value=True), \
         patch("src.main._stream_claude", side_effect=fake_stream_claude), \
         patch("src.main._poll_lark_reauth_and_resume") as mock_reauth:

        mock_api.get_tenant_access_token.return_value = "fake_token"
        mock_api.send_text_card.return_value = "card_initial"
        mock_api.update_card_text.return_value = None

        _start_auth_and_poll(
            open_id, "查找文档", initial_code, verify_url,
            auth, store, agent, APP_ID, APP_SECRET,
            context_id=context_id,
            chat_id="oc_54afb0130474afdd4fe07ed82179cbff",
            chat_type="group",
        )

    mock_reauth.assert_called_once()

    call_args = mock_reauth.call_args.args
    assert context_id in call_args
    assert reauth_code in call_args
    assert call_args[8] == context_id


def test_lark_reauth_resume_preserves_existing_p2p_session(store, auth, agent):
    from src.main import _poll_lark_reauth_and_resume

    context_id = "ou_pdf_reauth"
    device_code = "dev_pdf_reauth"
    existing_session = "sess_existing_pdf"
    store.upsert_user(
        context_id,
        auth_status="pending",
        pending_code=device_code,
        pending_url="https://auth.example.com/pdf",
        pending_at=datetime.datetime.now(datetime.UTC).isoformat(),
        session_id=existing_session,
    )
    store.create_auth_resume_job(
        context_id=context_id,
        provider="lark",
        device_code=device_code,
        client_id="",
        resume_text="content can be exported as PDF?",
        reply_id="",
        thread_key="",
        root_id="",
        chat_id="oc_p2p_fake",
        chat_type="p2p",
        existing_msg_id="om_pdf_card",
    )
    captured = {}

    def fake_stream_claude(*args, **kwargs):
        captured["session_before_stream"] = store.get_session_id(context_id)
        return "om_pdf_card"

    with patch("src.main.time.sleep"), \
         patch.object(auth, "poll_once", return_value=True), \
         patch("src.main.feishu_api.get_tenant_access_token", return_value="token"), \
         patch("src.main.feishu_api.update_card_text"), \
         patch("src.main._stream_claude", side_effect=fake_stream_claude):
        _poll_lark_reauth_and_resume(
            context_id,
            "content can be exported as PDF?",
            device_code,
            auth,
            store,
            agent,
            APP_ID,
            APP_SECRET,
            context_id,
            auth_card_id="om_pdf_card",
        )

    assert captured["session_before_stream"] == existing_session
    assert store.get_session_id(context_id) == existing_session


def test_complete_auth_and_resume_claims_original_text_once(store, auth, agent):
    from src.main import _complete_auth_and_resume

    store.create_auth_resume_job(
        context_id="ou_u1",
        provider="meegle",
        device_code="dev1",
        client_id="client1",
        resume_text="original request",
        reply_id="reply1",
        thread_key="thread1",
        root_id="root1",
        chat_id="oc_chat",
        chat_type="p2p",
        existing_msg_id="om_auth",
    )

    with patch("src.main._stream_claude", return_value="om_auth") as stream:
        first = _complete_auth_and_resume(
            open_id="ou_u1",
            context_id="ou_u1",
            provider="meegle",
            device_code="dev1",
            auth=auth,
            store=store,
            agent=agent,
            app_id=APP_ID,
            app_secret=APP_SECRET,
            owner="pod-a",
        )
        second = _complete_auth_and_resume(
            open_id="ou_u1",
            context_id="ou_u1",
            provider="meegle",
            device_code="dev1",
            auth=auth,
            store=store,
            agent=agent,
            app_id=APP_ID,
            app_secret=APP_SECRET,
            owner="pod-b",
        )

    assert first is True
    assert second is False
    stream.assert_called_once()
    assert stream.call_args.args[1] == "original request"


def test_duplicate_message_is_ignored(store, auth, agent):
    """重复的 message_id 不应触发第二次处理"""
    store.upsert_user("ou_dup", auth_status="authorized")
    mock_executor = MagicMock()

    with patch.object(auth, "is_authenticated", return_value=True), \
         patch("src.main._stream_claude", return_value="c1") as mock_stream:
        handle_message(_event("ou_dup", message_id="mid_dup"), store, auth, agent,
                       APP_ID, APP_SECRET, mock_executor)
        handle_message(_event("ou_dup", message_id="mid_dup"), store, auth, agent,
                       APP_ID, APP_SECRET, mock_executor)

    assert mock_stream.call_count == 1


def test_group_thread_reply_without_mention_is_ignored_even_with_active_session(store, auth, agent, caplog):
    """群话题里的普通回复即使已有会话，也必须 @ 机器人后才触发。"""
    open_id = "ou_thread_user"
    chat_id = "oc_group"
    root_id = "om_root"
    context_id = f"g_{chat_id}_{open_id}"
    store.upsert_user(context_id, auth_status="authorized")
    store.set_thread_session(f"{chat_id}:{root_id}", "sess_shared_thread")

    event = {
        "open_id": open_id,
        "text": "这条只是话题里的普通回复",
        "message_id": "om_reply_no_at",
        "chat_id": chat_id,
        "chat_type": "group",
        "root_id": root_id,
        "mentioned": False,
    }

    with caplog.at_level(logging.INFO, logger="src.main"), \
         patch("src.main.feishu_api.get_tenant_access_token", return_value="token"), \
         patch("src.main.feishu_api.get_message", return_value={"root_id": root_id}), \
         patch.object(auth, "is_authenticated", return_value=True), \
         patch("src.main._stream_claude", return_value="card_1") as mock_stream:
        handle_message(
            event, store, auth, agent,
            APP_ID, APP_SECRET, MagicMock(),
            bot_open_id="ou_bot",
        )

    mock_stream.assert_not_called()
    assert "[收到]" in caplog.text
    assert "Ignored unmatched group message" in caplog.text
    assert chat_id in caplog.text


def test_group_text_at_prefix_without_matching_metadata_is_ignored(store, auth, agent):
    open_id = "ou_group_at_fallback"
    chat_id = "oc_group"
    context_id = f"g_{chat_id}_{open_id}"
    store.upsert_user(context_id, auth_status="authorized")

    event = {
        "open_id": open_id,
        "text": "@Bot please help",
        "message_id": "om_at_prefix",
        "chat_id": chat_id,
        "chat_type": "group",
        "root_id": "om_root",
        "mentioned": False,
    }

    with patch("src.main.feishu_api.get_tenant_access_token", return_value="token"), \
         patch("src.main.feishu_api.get_message", return_value={"root_id": "om_root"}), \
         patch.object(auth, "is_authenticated", return_value=True), \
         patch("src.main._stream_claude", return_value="card_1") as mock_stream:
        handle_message(
            event, store, auth, agent,
            APP_ID, APP_SECRET, MagicMock(),
            bot_open_id="ou_bot",
        )

    mock_stream.assert_not_called()


def test_group_api_nested_mention_is_treated_as_bot_mention(store, auth, agent):
    open_id = "ou_group_nested_mention"
    chat_id = "oc_group"
    context_id = f"g_{chat_id}_{open_id}"
    store.upsert_user(context_id, auth_status="authorized")

    event = {
        "open_id": open_id,
        "text": "please help",
        "message_id": "om_nested_mention",
        "chat_id": chat_id,
        "chat_type": "group",
        "root_id": "om_root",
        "mentioned": False,
    }

    with patch("src.main.feishu_api.get_tenant_access_token", return_value="token"), \
         patch("src.main.feishu_api.get_message", return_value={
             "root_id": "om_root",
             "mentions": [{"id": {"open_id": "ou_bot"}, "key": "@_user_1"}],
         }), \
         patch.object(auth, "is_authenticated", return_value=True), \
         patch("src.main._stream_claude", return_value="card_1") as mock_stream:
        handle_message(
            event, store, auth, agent,
            APP_ID, APP_SECRET, MagicMock(),
            bot_open_id="ou_bot",
        )

    mock_stream.assert_called_once()
    assert mock_stream.call_args.args[1] == "please help"


def test_expired_pending_group_reauth_command_is_not_resumed(store, auth, agent):
    open_id = "ou_pending_reauth"
    chat_id = "oc_group"
    context_id = f"g_{chat_id}_{open_id}"
    old_time = (
        datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=11)
    ).isoformat()
    store.upsert_user(
        context_id,
        auth_status="pending",
        pending_code="old_code",
        pending_url="https://auth.example.com/old",
        pending_at=old_time,
    )
    mock_auth_executor = MagicMock()

    event = {
        "open_id": open_id,
        "text": "/reauth",
        "message_id": "om_group_reauth",
        "chat_id": chat_id,
        "chat_type": "group",
        "root_id": "om_root",
        "mentioned": True,
    }

    with patch("src.main.feishu_api.get_tenant_access_token", return_value="token"), \
         patch("src.main.feishu_api.get_message", return_value={"root_id": "om_root"}), \
         patch.object(auth, "is_authenticated", return_value=False), \
         patch.object(auth, "revoke_token") as revoke, \
         patch.object(auth, "start_auth", return_value=_FAKE_AUTH_DATA):
        handle_message(
            event, store, auth, agent,
            APP_ID, APP_SECRET, MagicMock(),
            auth_executor=mock_auth_executor,
            bot_open_id="ou_bot",
        )

    revoke.assert_called_once_with(context_id)
    mock_auth_executor.submit.assert_called_once()
    assert mock_auth_executor.submit.call_args.args[2] == ""
    assert store.claim_auth_resume_job(
        context_id=context_id,
        provider="lark",
        device_code=_FAKE_AUTH_DATA["device_code"],
        owner="test",
    ) is None


def test_group_lark_auth_sends_waiting_card_in_thread_and_link_in_private_chat(store, auth, agent):
    """群话题授权时，群内卡片不暴露链接，授权链接只通过私聊发送给触发人。"""
    from src.main import _start_auth_and_poll

    with patch("src.main.feishu_api.get_tenant_access_token", return_value="token"), \
         patch("src.main.feishu_api.get_chat_info", return_value={"name": "项目群"}), \
         patch("src.main.feishu_api.reply_card_in_thread", return_value="om_group_wait") as reply_card, \
         patch("src.main.feishu_api.send_text_card", return_value="om_private_auth") as private_card, \
         patch("src.main.feishu_api.update_card_text") as update_card, \
         patch("src.main.time.sleep"), \
         patch.object(auth, "poll_once", return_value=True), \
         patch("src.main._complete_auth_and_resume", return_value=True):
        _start_auth_and_poll(
            "ou_needs_auth",
            "帮我查一下文档",
            "dev_code",
            "https://auth.example.com/oauth",
            auth,
            store,
            agent,
            APP_ID,
            APP_SECRET,
            context_id="g_oc_group_ou_needs_auth",
            reply_msg_id="om_trigger",
            thread_session_key="g_oc_group_ou_needs_auth:om_root",
            root_id="om_root",
            chat_id="oc_group",
            chat_type="group",
        )

    group_text = reply_card.call_args.args[1]
    private_text = private_card.call_args.args[1]
    assert "https://auth.example.com/oauth" not in group_text
    assert "等待获取" in group_text
    assert "ou_needs_auth" in group_text
    assert "https://auth.example.com/oauth" in private_text
    assert "项目群" in private_text
    assert "允许机器人在项目群回复消息吗" in private_text
    private_updates = [
        call.args[1]
        for call in update_card.call_args_list
        if call.args and call.args[0] == "om_private_auth"
    ]
    assert private_updates
    assert "授权已完成" in private_updates[-1]
    assert "项目群" in private_updates[-1]


def test_pending_group_lark_auth_reminder_keeps_link_out_of_thread(store, auth, agent):
    """群话题里授权 pending 时，再次提醒也只在私聊里带授权链接。"""
    open_id = "ou_pending_group"
    chat_id = "oc_group"
    context_id = f"g_{chat_id}_{open_id}"
    store.upsert_user(
        context_id,
        auth_status="pending",
        pending_code="dev_pending",
        pending_url="https://auth.example.com/pending",
        pending_at=datetime.datetime.now(datetime.UTC).isoformat(),
    )

    event = {
        "open_id": open_id,
        "text": "再帮我处理一下",
        "message_id": "om_pending_at",
        "chat_id": chat_id,
        "chat_type": "group",
        "root_id": "om_root",
        "mentioned": True,
    }

    with patch("src.main.feishu_api.get_tenant_access_token", return_value="token"), \
         patch("src.main.feishu_api.get_message", return_value={"root_id": "om_root"}), \
         patch("src.main.feishu_api.get_chat_info", return_value={"name": "项目群"}), \
         patch("src.main.feishu_api.reply_card_in_thread", return_value="om_group_wait") as reply_card, \
         patch("src.main.feishu_api.send_text_card", return_value="om_private_auth") as private_card, \
         patch.object(auth, "poll_once", return_value=False), \
         patch.object(auth, "is_authenticated", return_value=False):
        handle_message(
            event, store, auth, agent,
            APP_ID, APP_SECRET, MagicMock(),
            auth_executor=None,
            bot_open_id="ou_bot",
        )

    group_text = reply_card.call_args.args[1]
    private_text = private_card.call_args.args[1]
    assert "https://auth.example.com/pending" not in group_text
    assert "等待获取" in group_text
    assert "https://auth.example.com/pending" in private_text
    assert "允许机器人在项目群回复消息吗" in private_text


def test_stream_claude_injects_recent_group_chat_context(store, agent):
    """群聊请求进入 Claude 前，应自动带上当前群的近邻消息上下文。"""
    store.upsert_user("g_oc_group_ou_auth", auth_status="authorized")
    captured = {}

    def fake_stream_chat(open_id, text, **kwargs):
        captured["text"] = text
        from src.agent import StreamResult
        yield "ok"
        yield StreamResult(session_id="sess_group", full_text="ok")

    recent_messages = [
        {
            "message_id": "om_prev",
            "sender": {"sender_type": "user", "id": "ou_other"},
            "msg_type": "text",
            "body": {"content": '{"text":"这里是具体的问题内容"}'},
        },
        {
            "message_id": "om_current",
            "sender": {"sender_type": "user", "id": "ou_auth"},
            "msg_type": "text",
            "body": {"content": '{"text":"@Bot 看看上面这个问题"}'},
        },
    ]

    with patch.object(agent, "stream_chat", side_effect=fake_stream_chat), \
         patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}, clear=False), \
         patch("src.main.feishu_api.get_tenant_access_token", return_value="token"), \
         patch("src.main.feishu_api.reply_card_in_thread", return_value="card_1"), \
         patch("src.main.feishu_api.update_card_text", return_value=None), \
         patch("src.main.feishu_api.list_chat_messages_around", return_value=recent_messages):
        from src.main import _stream_claude
        _stream_claude(
            "ou_auth", "看看上面这个问题", agent, store, APP_ID, APP_SECRET,
            context_id="g_oc_group_ou_auth",
            reply_msg_id="om_current",
            chat_id="oc_group",
            chat_type="group",
            root_id="om_current",
        )

    assert "以下是当前飞书群聊最近的上下文" in captured["text"]
    assert "这里是具体的问题内容" in captured["text"]
    assert "[群聊上文结束，用户的新消息如下]" in captured["text"]


def test_stream_claude_does_not_inject_recent_context_for_p2p(store, agent):
    """私聊请求不应走群聊近邻消息预取。"""
    store.upsert_user("ou_auth", auth_status="authorized")
    captured = {}

    def fake_stream_chat(open_id, text, **kwargs):
        captured["text"] = text
        from src.agent import StreamResult
        yield "ok"
        yield StreamResult(session_id="sess_p2p", full_text="ok")

    with patch.object(agent, "stream_chat", side_effect=fake_stream_chat), \
         patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}, clear=False), \
         patch("src.main.feishu_api.get_tenant_access_token", return_value="token"), \
         patch("src.main.feishu_api.send_text_card", return_value="card_1"), \
         patch("src.main.feishu_api.update_card_text", return_value=None), \
         patch("src.main.feishu_api.list_chat_messages_around") as mock_recent:
        from src.main import _stream_claude
        _stream_claude(
            "ou_auth", "看看上面这个问题", agent, store, APP_ID, APP_SECRET,
            context_id="ou_auth",
            chat_id="oc_p2p",
            chat_type="p2p",
        )

    mock_recent.assert_not_called()
    assert captured["text"] == "看看上面这个问题"


def test_stream_claude_no_key_prompt_does_not_start_progress_ticker(store, agent):
    """No-key users should get a fresh prompt card and never enter Claude."""
    store.upsert_user("ou_no_key", auth_status="authorized")
    sent_cards = []
    started_threads = []

    class FakeThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            started_threads.append("started")

        def join(self, timeout=None):
            pass

    with patch.object(agent, "stream_chat") as mock_stream, \
         patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}, clear=False), \
         patch("src.main.threading.Thread", FakeThread), \
         patch("src.main.feishu_api.get_tenant_access_token", return_value="token"), \
         patch("src.main.feishu_api.send_text_card", side_effect=lambda open_id, text, token: sent_cards.append(text) or "card_1"), \
         patch("src.main.feishu_api.update_card_text") as update_card_text, \
         patch("src.main.feishu_api.get_user_email", return_value="user@example.com"), \
         patch("src.main.get_user_personal_key", return_value=""):
        from src.main import _stream_claude

        result = _stream_claude(
            "ou_no_key", "hello", agent, store, APP_ID, APP_SECRET,
            context_id="ou_no_key",
            chat_id="oc_p2p",
            chat_type="p2p",
            oa_api_key="oa-key",
        )

    assert result == "card_1"
    mock_stream.assert_not_called()
    assert started_threads == []
    assert len(sent_cards) == 1
    update_card_text.assert_called_once()
    assert "https://aq.yostar.net/openrouter/my-keys" in update_card_text.call_args.args[1]


def test_stream_claude_sends_placeholder_before_personal_key_lookup(store, agent):
    """The first card should not wait on Feishu email/OA key lookups."""
    from src.agent import StreamResult
    from src.main import _stream_claude

    store.upsert_user("ou_slow_key", auth_status="authorized")
    events = []

    def fake_stream_chat(*args, **kwargs):
        yield StreamResult(session_id="sess_fast_card", full_text="ok")

    def fake_get_user_email(*args, **kwargs):
        events.append("email")
        return "user@example.com"

    def fake_get_user_personal_key(*args, **kwargs):
        events.append("key")
        return "personal-key"

    def fake_send_text_card(*args, **kwargs):
        events.append("card")
        return "card_1"

    with patch.object(agent, "stream_chat", side_effect=fake_stream_chat), \
         patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}, clear=False), \
         patch("src.main.feishu_api.get_tenant_access_token", return_value="token"), \
         patch("src.main.feishu_api.send_text_card", side_effect=fake_send_text_card), \
         patch("src.main.feishu_api.update_card_text", return_value=None), \
         patch("src.main.feishu_api.get_user_email", side_effect=fake_get_user_email), \
         patch("src.main.get_user_personal_key", side_effect=fake_get_user_personal_key):
        _stream_claude(
            "ou_slow_key", "hello", agent, store, APP_ID, APP_SECRET,
            context_id="ou_slow_key",
            chat_id="oc_p2p",
            chat_type="p2p",
            oa_api_key="oa-key",
        )

    assert events[:3] == ["card", "email", "key"]


def test_stream_claude_key_source_is_debug_log(store, agent, caplog):
    from src.agent import StreamResult
    from src.main import _stream_claude

    store.upsert_user("ou_key_log", auth_status="authorized", display_name="Tester")

    def fake_stream_chat(*args, **kwargs):
        yield StreamResult(session_id="sess_key_log", full_text="ok")

    patches = (
        patch.object(agent, "stream_chat", side_effect=fake_stream_chat),
        patch.dict(os.environ, {"ANTHROPIC_API_KEY": "shared-key"}, clear=False),
        patch("src.main.feishu_api.get_tenant_access_token", return_value="token"),
        patch("src.main.feishu_api.send_text_card", return_value="card_1"),
        patch("src.main.feishu_api.update_card_text", return_value=None),
    )

    with patches[0], patches[1], patches[2], patches[3], patches[4], \
         caplog.at_level(logging.INFO, logger="src.main"):
        _stream_claude(
            "ou_key_log",
            "hello",
            agent,
            store,
            APP_ID,
            APP_SECRET,
            context_id="ou_key_log",
            chat_id="oc_p2p",
            chat_type="p2p",
        )

    assert "[key]" not in caplog.text

    caplog.clear()
    with patch.object(agent, "stream_chat", side_effect=fake_stream_chat), \
         patch.dict(os.environ, {"ANTHROPIC_API_KEY": "shared-key"}, clear=False), \
         patch("src.main.feishu_api.get_tenant_access_token", return_value="token"), \
         patch("src.main.feishu_api.send_text_card", return_value="card_1"), \
         patch("src.main.feishu_api.update_card_text", return_value=None), \
         caplog.at_level(logging.DEBUG, logger="src.main"):
        _stream_claude(
            "ou_key_log",
            "hello",
            agent,
            store,
            APP_ID,
            APP_SECRET,
            context_id="ou_key_log",
            chat_id="oc_p2p",
            chat_type="p2p",
        )

    assert "[key]" in caplog.text


def test_stream_claude_records_usage_when_turn_limit_reached(store, agent):
    from src.agent import StreamResult
    from src.main import _stream_claude

    store.upsert_user("ou_limit", auth_status="authorized", display_name="Tester")

    def fake_stream_chat(*args, **kwargs):
        yield StreamResult(
            session_id="sess_limit",
            full_text="partial result",
            turn_limit_reached=True,
            cost_usd=0.1234,
            input_tokens=11,
            output_tokens=22,
            cache_read_tokens=33,
        )

    with patch.object(agent, "stream_chat", side_effect=fake_stream_chat), \
         patch.dict(os.environ, {"ANTHROPIC_API_KEY": "shared-key"}, clear=False), \
         patch("src.main.feishu_api.get_tenant_access_token", return_value="token"), \
         patch("src.main.feishu_api.send_text_card", return_value="card_1"), \
         patch("src.main.feishu_api.update_card_text", return_value=None):
        _stream_claude(
            "ou_limit",
            "do a long task",
            agent,
            store,
            APP_ID,
            APP_SECRET,
            context_id="ou_limit",
            chat_id="oc_p2p",
            chat_type="p2p",
        )

    store.add_usage.assert_called_once()
    kwargs = store.add_usage.call_args.kwargs
    assert kwargs["open_id"] == "ou_limit"
    assert kwargs["input_tokens"] == 11
    assert kwargs["output_tokens"] == 22
    assert kwargs["cache_read_tokens"] == 33
    assert kwargs["cost_usd"] == 0.1234
    assert kwargs["using_personal_key"] is False


def test_stream_claude_does_not_persist_session_on_claude_error(store, agent):
    from src.agent import StreamResult
    from src.main import _stream_claude

    store.upsert_user("ou_error", auth_status="authorized", display_name="Tester")

    def fake_stream_chat(*args, **kwargs):
        yield StreamResult(
            session_id="failed-session",
            full_text="API Error: Unable to connect to API (ECONNRESET)",
            is_error=True,
        )

    with patch.object(agent, "stream_chat", side_effect=fake_stream_chat), \
         patch.dict(os.environ, {"ANTHROPIC_API_KEY": "shared-key"}, clear=False), \
         patch("src.main.feishu_api.get_tenant_access_token", return_value="token"), \
         patch("src.main.feishu_api.send_text_card", return_value="card_1"), \
         patch("src.main.feishu_api.update_card_text", return_value=None):
        _stream_claude(
            "ou_error",
            "hello",
            agent,
            store,
            APP_ID,
            APP_SECRET,
            context_id="ou_error",
            chat_id="oc_p2p",
            chat_type="p2p",
        )

    assert store.get_user("ou_error")["session_id"] is None


def test_usage_command_sends_current_user_usage(store):
    from src.main import _handle_usage_command

    row = {
        "open_id": "ou_usage",
        "display_name": "Tester",
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_read_tokens": 25,
        "cost_usd": 1.23,
        "request_count": 3,
        "personal_input_tokens": 70,
        "personal_output_tokens": 30,
        "personal_cache_read_tokens": 10,
        "personal_cost_usd": 0.9,
        "personal_request_count": 2,
    }
    store.get_user_usage.return_value = row

    with patch("src.main.feishu_api.get_tenant_access_token", return_value="token"), \
         patch("src.main.feishu_api.send_text_card") as send:
        handled = _handle_usage_command("ou_usage", "/usage", APP_ID, APP_SECRET, store)

    assert handled is True
    store.get_user_usage.assert_called_once()
    msg = send.call_args.args[1]
    assert "Tester" in msg
    assert "输入：100 tokens" in msg
    assert "输出：50 tokens" in msg
    assert "$1.2300" in msg
    assert "个人 key" in msg
    assert "公共 key" in msg


def test_usage_command_admin_sends_all_usage_ranking(store):
    from src.main import _ADMIN_OPEN_IDS, _handle_usage_command

    admin_id = next(iter(_ADMIN_OPEN_IDS))
    store.get_all_usage.return_value = [
        {
            "open_id": "ou_1",
            "display_name": "Alice",
            "input_tokens": 10,
            "output_tokens": 20,
            "cache_read_tokens": 0,
            "cost_usd": 0.5,
            "request_count": 1,
            "personal_input_tokens": 10,
            "personal_output_tokens": 20,
            "personal_cache_read_tokens": 0,
            "personal_cost_usd": 0.5,
            "personal_request_count": 1,
        }
    ]

    with patch("src.main.feishu_api.get_tenant_access_token", return_value="token"), \
         patch("src.main.feishu_api.send_text_card") as send:
        handled = _handle_usage_command(admin_id, "本月用量", APP_ID, APP_SECRET, store)

    assert handled is True
    store.get_all_usage.assert_called_once()
    msg = send.call_args.args[1]
    assert "全员用量排行" in msg
    assert "Alice" in msg
    assert "总 30 tok / $0.5000" in msg


def test_stream_claude_binds_interactive_form_context_to_internal_api_token(store, agent):
    """_stream_claude 创建内部 API token 时，应绑定表单创建所需的飞书上下文。"""
    store.upsert_user("g_oc_group_ou_auth", auth_status="authorized")
    registry = MagicMock()
    registry.create.return_value = "tok_1"
    registry.revoke.return_value = None

    def fake_stream_chat(open_id, text, **kwargs):
        from src.agent import StreamResult
        yield StreamResult(session_id="sess_group", full_text="ok")

    with patch.object(agent, "stream_chat", side_effect=fake_stream_chat), \
         patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}, clear=False), \
         patch("src.main._api_registry", registry), \
         patch("src.main.feishu_api.get_tenant_access_token", return_value="token"), \
         patch("src.main.feishu_api.reply_card_in_thread", return_value="card_1"), \
         patch("src.main.feishu_api.update_card_text", return_value=None), \
         patch("src.main.feishu_api.list_chat_messages_around", return_value=[]):
        from src.main import _stream_claude
        _stream_claude(
            "ou_auth", "创建需求", agent, store, APP_ID, APP_SECRET,
            context_id="g_oc_group_ou_auth",
            reply_msg_id="om_current",
            chat_id="oc_group",
            chat_type="group",
            root_id="om_root",
            thread_session_key="thread_key",
            image_message_id="om_current",
        )

    kwargs = registry.create.call_args.kwargs
    assert registry.create.call_args.args[0] == "g_oc_group_ou_auth"
    assert kwargs["metadata"]["operator_open_id"] == "ou_auth"
    assert kwargs["metadata"]["reply_msg_id"] == "om_current"
    assert kwargs["metadata"]["root_id"] == "om_root"
    assert kwargs["metadata"]["thread_session_key"] == "thread_key"
    assert kwargs["metadata"]["original_text"] == "创建需求"


def test_form_completion_runner_streams_followup_and_marks_completed(store, agent):
    from src.main import _make_form_completion_runner

    executor = MagicMock()
    executor.submit.side_effect = lambda fn, *args: fn(*args)
    form_session = {
        "id": "form_1",
        "context_id": "ou_1",
        "operator_open_id": "ou_1",
        "reply_msg_id": "",
        "thread_session_key": "",
        "root_id": "",
        "chat_id": "oc_1",
        "chat_type": "p2p",
        "original_text": "创建需求",
        "schema": {
            "title": "补充信息",
            "questions": [
                {
                    "id": "priority",
                    "title": "优先级？",
                    "type": "single",
                    "options": [{"label": "P0"}],
                }
            ],
        },
        "answers": {
            "priority": {
                "question_id": "priority",
                "type": "single",
                "values": ["P0"],
                "selected_options": ["P0"],
                "custom_value": "",
            }
        },
    }

    with patch("src.main._stream_claude", return_value="card_1") as stream:
        runner = _make_form_completion_runner(agent, store, APP_ID, APP_SECRET, executor)
        runner(form_session)

    stream.assert_called_once()
    assert "用户已经通过飞书交互表单补充了信息" in stream.call_args.kwargs["user_text"]


def test_form_completion_runner_persists_auth_resume_job_when_followup_triggers_auth(store, auth, agent):
    from src.main import _make_form_completion_runner

    executor = MagicMock()
    executor.submit.side_effect = lambda fn, *args: fn(*args)
    auth_executor = MagicMock()
    form_session = {
        "id": "form_auth_1",
        "context_id": "ou_form_auth",
        "operator_open_id": "ou_form_auth",
        "reply_msg_id": "",
        "thread_session_key": "",
        "root_id": "",
        "chat_id": "oc_form",
        "chat_type": "p2p",
        "original_text": "创建需求",
        "schema": {
            "title": "补充信息",
            "questions": [
                {
                    "id": "priority",
                    "title": "优先级？",
                    "type": "single",
                    "options": [{"label": "P0"}],
                }
            ],
        },
        "answers": {
            "priority": {
                "question_id": "priority",
                "type": "single",
                "values": ["P0"],
                "selected_options": ["P0"],
                "custom_value": "",
            }
        },
    }

    def fake_stream(*args, **kwargs):
        now = datetime.datetime.now(datetime.UTC).isoformat()
        store.upsert_user(
            "ou_form_auth",
            auth_status="pending",
            pending_code="form_lark_code",
            pending_url="https://auth.example.com/form",
            pending_at=now,
        )
        return "form_card"

    with patch("src.main._stream_claude", side_effect=fake_stream):
        runner = _make_form_completion_runner(
            agent, store, APP_ID, APP_SECRET, executor,
            auth=auth,
            auth_executor=auth_executor,
        )
        runner(form_session)

    claimed = store.claim_auth_resume_job(
        context_id="ou_form_auth",
        provider="lark",
        device_code="form_lark_code",
        owner="test",
    )
    assert claimed is not None
    assert "用户已经通过飞书交互表单补充了信息" in claimed["resume_text"]
    assert claimed["existing_msg_id"] == "form_card"
    auth_executor.submit.assert_called_once()


def test_form_card_update_runner_patches_card_in_executor():
    from src.main import _make_form_card_update_runner

    executor = MagicMock()
    submitted = []
    executor.submit.side_effect = lambda fn, *args: submitted.append((fn, args))
    form_store = MagicMock()
    form_store.next_card_sequence.return_value = 7
    feishu_api = MagicMock()
    feishu_api.get_tenant_access_token.return_value = "tenant_token"
    runner = _make_form_card_update_runner(
        form_store, feishu_api, APP_ID, APP_SECRET, executor, delay_seconds=0.5,
    )
    session = {"id": "form_1", "card_id": "om_card", "message_id": "om_user", "current_index": 1}
    card = {"schema": "2.0", "body": {"elements": []}}

    runner(session, card)

    executor.submit.assert_called_once()
    feishu_api.update_interactive_card.assert_not_called()

    fn, args = submitted[0]
    with patch("src.main.time.sleep") as sleep:
        fn(*args)

    sleep.assert_called_once_with(0.5)
    feishu_api.get_tenant_access_token.assert_called_once_with(APP_ID, APP_SECRET)
    form_store.next_card_sequence.assert_called_once_with("form_1")
    feishu_api.update_interactive_card.assert_called_once_with(
        "om_card",
        card,
        "tenant_token",
        sequence=7,
    )
    feishu_api.update_interactive_card_by_token.assert_not_called()


def test_form_card_update_runner_uses_callback_token_when_available():
    from src.main import _make_form_card_update_runner

    form_store = MagicMock()
    form_store.next_card_sequence.return_value = 2
    feishu_api = MagicMock()
    feishu_api.get_tenant_access_token.return_value = "tenant_token"
    runner = _make_form_card_update_runner(form_store, feishu_api, APP_ID, APP_SECRET)
    session = {"id": "form_1", "card_id": "om_card", "current_index": 1}
    card = {"schema": "2.0", "body": {"elements": []}}

    with patch("src.main.time.sleep") as sleep:
        runner(session, card, "c_update_token")

    sleep.assert_called_once_with(0.3)
    feishu_api.get_tenant_access_token.assert_called_once_with(APP_ID, APP_SECRET)
    form_store.next_card_sequence.assert_called_once_with("form_1")
    feishu_api.update_interactive_card_by_token.assert_called_once_with(
        "c_update_token",
        card,
        "tenant_token",
        sequence=2,
    )
    feishu_api.update_interactive_card.assert_not_called()


def test_form_card_update_runner_callback_token_delay_can_be_tuned_with_env():
    from src.main import _make_form_card_update_runner

    form_store = MagicMock()
    form_store.next_card_sequence.return_value = 2
    feishu_api = MagicMock()
    feishu_api.get_tenant_access_token.return_value = "tenant_token"
    runner = _make_form_card_update_runner(form_store, feishu_api, APP_ID, APP_SECRET)
    session = {"id": "form_1", "card_id": "om_card", "current_index": 1}
    card = {"schema": "2.0", "body": {"elements": []}}

    with patch.dict(os.environ, {"BOT_FORM_CALLBACK_TOKEN_UPDATE_DELAY_SECONDS": "0.6"}), \
         patch("src.main.time.sleep") as sleep:
        runner(session, card, "c_update_token")

    sleep.assert_called_once_with(0.6)


def test_form_card_update_runner_default_does_not_delay_card_update():
    from src.main import _make_form_card_update_runner

    form_store = MagicMock()
    form_store.next_card_sequence.return_value = 1
    feishu_api = MagicMock()
    feishu_api.get_tenant_access_token.return_value = "tenant_token"
    runner = _make_form_card_update_runner(form_store, feishu_api, APP_ID, APP_SECRET)
    session = {"id": "form_1", "card_id": "om_card", "current_index": 1}
    card = {"schema": "2.0", "body": {"elements": []}}

    with patch("src.main.time.sleep") as sleep:
        runner(session, card)

    sleep.assert_not_called()


def test_form_card_update_runner_delay_can_be_tuned_with_env():
    from src.main import _make_form_card_update_runner

    form_store = MagicMock()
    form_store.next_card_sequence.return_value = 1
    feishu_api = MagicMock()
    feishu_api.get_tenant_access_token.return_value = "tenant_token"
    runner = _make_form_card_update_runner(form_store, feishu_api, APP_ID, APP_SECRET)
    session = {"id": "form_1", "card_id": "om_card", "current_index": 1}
    card = {"schema": "2.0", "body": {"elements": []}}

    with patch.dict(os.environ, {"BOT_FORM_CARD_UPDATE_DELAY_SECONDS": "0.25"}), \
         patch("src.main.time.sleep") as sleep:
        runner(session, card)

    sleep.assert_called_once_with(0.25)


def test_handle_bot_added_sends_group_privacy_warning_to_operator():
    from src.main import handle_bot_added

    expected = (
        "⚠️  我刚刚被添加到了群聊「项目群」，请注意安全隐私风险。\n"
        "我在群内回复你的消息时，可能会涉及你的相关数据。\n"
        "如果你只是想让我总结这个群的信息，可以直接单聊告诉我，无需进群即可完成。"
    )

    with patch("src.main.feishu_api.get_tenant_access_token", return_value="tenant_token"), \
         patch("src.main.feishu_api.send_text_card") as send_text_card:
        handle_bot_added(
            {"operator_id": "ou_inviter", "chat_id": "oc_1", "chat_name": "项目群"},
            APP_ID,
            APP_SECRET,
        )

    send_text_card.assert_called_once_with("ou_inviter", expected, "tenant_token")


def test_start_event_ingress_sdk_mode_starts_sdk_listener():
    from src import main

    cfg = MagicMock()
    cfg.feishu_app_id = "app"
    cfg.feishu_app_secret = "secret"
    cfg.feishu_bot_open_id = "ou_bot"

    with patch("src.sdk_event_listener.SdkEventListener") as sdk_cls:
        sdk_listener = sdk_cls.return_value

        listener, card_listener = main._start_event_ingress(
            mode="sdk",
            cfg=cfg,
            on_message=MagicMock(),
            on_poll=MagicMock(),
            on_bot_added=MagicMock(),
            card_action_handler=MagicMock(),
        )

    assert listener is sdk_listener
    assert card_listener is None
    sdk_listener.start.assert_called_once()


def test_start_event_ingress_lark_cli_mode_starts_legacy_listener_and_card_listener():
    from src import main

    cfg = MagicMock()
    cfg.feishu_app_id = "app"
    cfg.feishu_app_secret = "secret"
    cfg.lark_bot_home = "bot-home"
    cfg.feishu_bot_open_id = "ou_bot"

    with patch("src.main.EventListener") as event_listener_cls, \
         patch("src.main.start_card_action_listener") as start_card:
        legacy_listener = event_listener_cls.return_value
        legacy_card_listener = start_card.return_value

        listener, card_listener = main._start_event_ingress(
            mode="lark_cli",
            cfg=cfg,
            on_message=MagicMock(),
            on_poll=MagicMock(),
            on_bot_added=MagicMock(),
            card_action_handler=MagicMock(),
        )

    assert listener is legacy_listener
    assert card_listener is legacy_card_listener
    legacy_listener.start.assert_called_once()
    start_card.assert_called_once()
