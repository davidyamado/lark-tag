import os
import pytest
from unittest.mock import patch, MagicMock
from src.lark_runner import run_lark_cli

FAKE_USERS_DIR = "/fake/users"

def test_rejects_non_lark_cli_command():
    result = run_lark_cli("rm -rf /", "user123", users_dir=FAKE_USERS_DIR)
    assert "错误" in result

def test_rejects_empty_command():
    result = run_lark_cli("", "user123", users_dir=FAKE_USERS_DIR)
    assert "错误" in result

def test_injects_as_user_flag():
    """--as user 应自动追加到命令末尾"""
    with patch("src.lark_runner.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="ok", stderr="")
        run_lark_cli("lark-cli calendar +agenda", "user123", users_dir=FAKE_USERS_DIR)
        args = mock_run.call_args[0][0]
        as_idx = args.index("--as")
        assert args[as_idx + 1] == "user"

def test_sets_home_env():
    """HOME 应设置为 users_dir/open_id"""
    with patch("src.lark_runner.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="ok", stderr="")
        run_lark_cli("lark-cli calendar +agenda", "user123", users_dir=FAKE_USERS_DIR)
        env = mock_run.call_args[1]["env"]
        assert env["HOME"] == os.path.join(FAKE_USERS_DIR, "user123")

def test_returns_stdout_on_success():
    with patch("src.lark_runner.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="result text", stderr="")
        result = run_lark_cli("lark-cli calendar +agenda", "user123", users_dir=FAKE_USERS_DIR)
        assert result == "result text"

def test_returns_stderr_when_no_stdout():
    with patch("src.lark_runner.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="", stderr="some error")
        result = run_lark_cli("lark-cli calendar +agenda", "user123", users_dir=FAKE_USERS_DIR)
        assert result == "some error"

def test_handles_timeout():
    import subprocess
    with patch("src.lark_runner.subprocess.run", side_effect=subprocess.TimeoutExpired("lark-cli", 30)):
        result = run_lark_cli("lark-cli calendar +agenda", "user123", users_dir=FAKE_USERS_DIR)
        assert "超时" in result

def test_raises_token_expired_on_expired_output():
    """输出包含 token_expired 时应抛出 TokenExpiredError"""
    from src.lark_runner import TokenExpiredError
    with patch("src.lark_runner.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="", stderr="token_expired: please login again")
        with pytest.raises(TokenExpiredError):
            run_lark_cli("lark-cli calendar +agenda", "user123", users_dir=FAKE_USERS_DIR)

def test_raises_token_expired_on_unauthorized_output():
    """输出包含 401 unauthorized 时应抛出 TokenExpiredError"""
    from src.lark_runner import TokenExpiredError
    with patch("src.lark_runner.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="", stderr="401 unauthorized")
        with pytest.raises(TokenExpiredError):
            run_lark_cli("lark-cli calendar +agenda", "user123", users_dir=FAKE_USERS_DIR)

def test_does_not_duplicate_as_flag():
    """如果命令中已含 --as，应被替换而不是重复"""
    with patch("src.lark_runner.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="ok", stderr="")
        run_lark_cli("lark-cli calendar +agenda --as bot", "user123", users_dir=FAKE_USERS_DIR)
        args = mock_run.call_args[0][0]
        # Should have exactly one --as flag
        assert args.count("--as") == 1
        assert "user" in args[args.index("--as") + 1]
