# tests/test_auth.py
import os
import pytest
from unittest.mock import patch, MagicMock
from src.auth import AuthManager

FAKE_USERS_DIR = "/fake/users"
FAKE_BOT_HOME = "/fake/bot-home"

# lark-cli auth login --recommend --no-wait now returns device-code JSON
_MOCK_AUTH_JSON = '{"device_code": "abc123", "verification_url": "https://auth.example.com/oauth", "user_code": "ABCD-1234"}'


def _make_mock_store():
    """Create a mock UserStore that tracks user state in-memory."""
    store = MagicMock()
    _users = {}

    def upsert_user(open_id, **kwargs):
        if open_id not in _users:
            _users[open_id] = {"open_id": open_id, "auth_status": "pending",
                               "pending_code": None, "pending_url": None,
                               "pending_at": None, "authorized_at": None,
                               "meegle_auth_status": "none",
                               "meegle_pending_code": None,
                               "meegle_pending_client_id": None,
                               "meegle_pending_url": None,
                               "meegle_pending_at": None,
                               "meegle_authorized_at": None}
        _users[open_id].update(kwargs)

    def get_user(open_id):
        return _users.get(open_id)

    def mark_authorized(open_id):
        if open_id in _users:
            _users[open_id]["auth_status"] = "authorized"
            import datetime
            _users[open_id]["authorized_at"] = datetime.datetime.now(datetime.UTC).isoformat()
            _users[open_id]["pending_at"] = None

    def reset_auth(open_id):
        upsert_user(open_id, auth_status="pending", authorized_at=None, pending_at=None)

    def mark_meegle_authorized(open_id):
        upsert_user(open_id, meegle_auth_status="authorized",
                    meegle_pending_code=None, meegle_pending_client_id=None,
                    meegle_pending_url=None, meegle_pending_at=None)

    def reset_meegle_auth(open_id):
        upsert_user(open_id, meegle_auth_status="none", meegle_authorized_at=None,
                    meegle_pending_code=None, meegle_pending_client_id=None,
                    meegle_pending_url=None, meegle_pending_at=None)

    store.upsert_user = upsert_user
    store.get_user = get_user
    store.mark_authorized = mark_authorized
    store.reset_auth = reset_auth
    store.mark_meegle_authorized = mark_meegle_authorized
    store.reset_meegle_auth = reset_meegle_auth
    store.close = MagicMock()
    return store


@pytest.fixture
def store():
    return _make_mock_store()


@pytest.fixture
def auth(store):
    return AuthManager(store, users_dir=FAKE_USERS_DIR, bot_home=FAKE_BOT_HOME)

def test_start_auth_returns_url(auth):
    """start_auth 应调用 lark-cli auth login --no-wait 并返回 device-code JSON"""
    with patch("src.auth.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout=_MOCK_AUTH_JSON, stderr="")
        result = auth.start_auth("user1")
        assert result["verification_url"] == "https://auth.example.com/oauth"
        assert result["device_code"] == "abc123"
        args = mock_run.call_args[0][0]
        assert "--domain" in args
        assert "--no-wait" in args

def test_start_auth_saves_pending(auth, store):
    """start_auth 应将 device_code 写入数据库 pending_code"""
    with patch("src.auth.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout=_MOCK_AUTH_JSON, stderr="")
        auth.start_auth("user1")
        user = store.get_user("user1")
        assert user["auth_status"] == "pending"
        assert user["pending_code"] == "abc123"


def test_start_auth_reuses_fresh_pending_code(auth, store):
    """Concurrent pods should reuse an existing fresh pending code instead of overwriting it."""
    import datetime
    store.upsert_user(
        "user1",
        auth_status="pending",
        pending_code="existing-code",
        pending_url="https://auth.example.com/existing",
        pending_at=datetime.datetime.now(datetime.UTC).isoformat(),
    )

    with patch("src.auth.subprocess.run") as mock_run:
        result = auth.start_auth("user1")

    assert result["device_code"] == "existing-code"
    assert result["verification_url"] == "https://auth.example.com/existing"
    mock_run.assert_not_called()

def test_poll_success_marks_authorized(auth, store):
    """device-code 轮询成功时，应标记用户为 authorized"""
    store.upsert_user("user1", auth_status="pending", pending_code="abc123")
    with patch("src.auth.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout='{"ok": true}', stderr="")
        result = auth.poll_once("user1", "abc123")
        assert result is True
        user = store.get_user("user1")
        assert user["auth_status"] == "authorized"

def test_poll_failure_returns_false(auth, store):
    """device-code 尚未授权时，返回 False"""
    store.upsert_user("user1", auth_status="pending", pending_code="abc123")
    with patch("src.auth.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="", stderr="authorization_pending")
        result = auth.poll_once("user1", "abc123")
        assert result is False

def test_start_auth_raises_on_cli_error(auth):
    """lark-cli 返回空输出时应抛出 RuntimeError"""
    with patch("src.auth.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="", stderr="error")
        with pytest.raises(RuntimeError):
            auth.start_auth("user1")

def test_start_auth_creates_user_dir(auth):
    """start_auth 应以 0o700 权限创建用户目录"""
    with patch("src.auth.subprocess.run") as mock_run:
        with patch("src.auth.os.makedirs") as mock_makedirs:
            mock_run.return_value = MagicMock(stdout=_MOCK_AUTH_JSON, stderr="")
            auth.start_auth("user1")
            mock_makedirs.assert_any_call(
                os.path.join(FAKE_USERS_DIR, "user1"), mode=0o700, exist_ok=True
            )

def test_poll_empty_output_falls_back_to_disk_check(auth, store):
    """lark-cli 返回空输出时，应通过 is_authenticated() 磁盘检查来判断授权状态"""
    store.upsert_user("user1", auth_status="pending", pending_code="abc123")
    with patch("src.auth.subprocess.run") as mock_run:
        mock_run.side_effect = [
            MagicMock(stdout="", stderr=""),
            MagicMock(stdout='{"tokenStatus":"valid"}', stderr=""),
        ]
        result = auth.poll_once("user1", "abc123")
        assert result is True
        assert store.get_user("user1")["auth_status"] == "authorized"

def test_poll_timeout_falls_back_to_disk_check(auth, store):
    """lark-cli 超时时，应通过 is_authenticated() 磁盘检查来判断授权状态"""
    import subprocess
    store.upsert_user("user1", auth_status="pending", pending_code="abc123")
    with patch("src.auth.subprocess.run") as mock_run:
        mock_run.side_effect = [
            subprocess.TimeoutExpired(cmd="lark-cli", timeout=15),
            MagicMock(stdout='{"tokenStatus":"valid"}', stderr=""),
        ]
        result = auth.poll_once("user1", "abc123")
        assert result is True
        assert store.get_user("user1")["auth_status"] == "authorized"


def test_is_authenticated_accepts_lark_cli_1_0_54_nested_user_status(auth):
    """lark-cli 1.0.54 nests tokenStatus under identities.user."""
    with patch("src.auth.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            stdout='{"identities":{"user":{"tokenStatus":"valid"}}}',
            stderr="",
        )

        assert auth.is_authenticated("user1") is True


def test_meegle_auth_status_classifies_retryable_probe_failure(auth):
    """Meegle 远端探测失败应被标记为 retryable，而不是未授权。"""
    with patch.object(auth, "_run_meegle") as mock_run:
        mock_run.return_value = MagicMock(
            stdout='{"authenticated": false, "host": "project.feishu.cn", "reason": "server_unreachable_or_error"}',
            stderr="",
            returncode=2,
        )

        status = auth.meegle_auth_status("user1")

    assert status["authenticated"] is False
    assert status["retryable"] is True
    assert status["reason"] == "server_unreachable_or_error"


def test_meegle_poll_retries_status_before_marking_authorized(auth, store):
    """device-code poll 后应短重试 auth status，避免 token 刚落盘时误判失败。"""
    store.upsert_user("user1", meegle_auth_status="pending",
                      meegle_pending_code="dev1", meegle_pending_client_id="client1")

    with patch.object(auth, "_run_meegle") as mock_run, \
         patch("time.sleep"):
        mock_run.side_effect = [
            MagicMock(stdout='{"ok": true}', stderr="", returncode=0),
            MagicMock(stdout='{"authenticated": false, "reason": "no_local_token"}', stderr="", returncode=1),
            MagicMock(stdout='{"authenticated": true, "host": "project.feishu.cn"}', stderr="", returncode=0),
        ]

        result = auth.poll_meegle_once("user1", "client1", "dev1")

    assert result is True
    assert store.get_user("user1")["meegle_auth_status"] == "authorized"
