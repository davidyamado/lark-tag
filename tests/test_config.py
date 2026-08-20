import os
import pytest
from unittest.mock import patch


def test_config_raises_if_missing_required_vars(monkeypatch):
    """缺少必要环境变量时应抛出 ValueError"""
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)
    monkeypatch.delenv("POSTGRES_URL", raising=False)
    import importlib
    import src.config as cfg_module
    importlib.reload(cfg_module)
    # Patch load_dotenv so it doesn't restore vars from .env file
    with patch("src.config.load_dotenv"):
        with pytest.raises(ValueError):
            _ = cfg_module.Config()


def test_config_loads_all_vars(monkeypatch):
    """所有变量存在时应正确加载"""
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("FEISHU_APP_ID", "app-id")
    monkeypatch.setenv("FEISHU_APP_SECRET", "app-secret")
    monkeypatch.setenv("POSTGRES_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("LARK_BOT_HOME", "/tmp/lark-bot-home")
    monkeypatch.setenv("LARK_USERS_DIR", "/tmp/lark-users")
    import importlib
    import src.config as cfg_module
    importlib.reload(cfg_module)
    with patch("src.config.load_dotenv"):
        cfg = cfg_module.Config()
    assert cfg.anthropic_auth_token == "test-token"
    assert cfg.feishu_app_id == "app-id"
    assert cfg.feishu_app_secret == "app-secret"
    assert cfg.postgres_url == "postgresql://test:test@localhost:5432/test"
    assert cfg.lark_bot_home == "/tmp/lark-bot-home"
    assert cfg.lark_users_dir == "/tmp/lark-users"


def test_config_defaults_event_ingress_to_lark_cli(monkeypatch):
    monkeypatch.setenv("FEISHU_APP_ID", "app-id")
    monkeypatch.setenv("FEISHU_APP_SECRET", "app-secret")
    monkeypatch.setenv("POSTGRES_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.delenv("BOT_EVENT_INGRESS", raising=False)

    import importlib
    import src.config as cfg_module
    importlib.reload(cfg_module)
    with patch("src.config.load_dotenv"):
        cfg = cfg_module.Config()

    assert cfg.bot_event_ingress == "lark_cli"


def test_config_accepts_sdk_event_ingress(monkeypatch):
    monkeypatch.setenv("FEISHU_APP_ID", "app-id")
    monkeypatch.setenv("FEISHU_APP_SECRET", "app-secret")
    monkeypatch.setenv("POSTGRES_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("BOT_EVENT_INGRESS", "sdk")

    import importlib
    import src.config as cfg_module
    importlib.reload(cfg_module)
    with patch("src.config.load_dotenv"):
        cfg = cfg_module.Config()

    assert cfg.bot_event_ingress == "sdk"


def test_config_rejects_invalid_event_ingress(monkeypatch):
    monkeypatch.setenv("FEISHU_APP_ID", "app-id")
    monkeypatch.setenv("FEISHU_APP_SECRET", "app-secret")
    monkeypatch.setenv("POSTGRES_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("BOT_EVENT_INGRESS", "both")

    import importlib
    import src.config as cfg_module
    importlib.reload(cfg_module)
    with patch("src.config.load_dotenv"):
        with pytest.raises(ValueError, match="BOT_EVENT_INGRESS"):
            cfg_module.Config()
