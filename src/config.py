import os
from dotenv import load_dotenv

class Config:
    def __init__(self):
        load_dotenv(".env.local", override=True)
        load_dotenv()
        required = {
            "FEISHU_APP_ID": "feishu_app_id",
            "FEISHU_APP_SECRET": "feishu_app_secret",
            "POSTGRES_URL": "postgres_url",
        }
        optional = {
            # Empty string means "not set" — main.py only passes these to the
            # Claude subprocess when non-empty (local dev uses OAuth instead).
            "ANTHROPIC_AUTH_TOKEN": ("anthropic_auth_token", ""),
            "ANTHROPIC_BASE_URL": ("anthropic_base_url", ""),
            "CLAUDE_MODEL": ("claude_model", "anthropic/claude-sonnet-4.6"),
            "LARK_BOT_HOME": ("lark_bot_home", "/var/lark-bot/config"),
            "LARK_USERS_DIR": ("lark_users_dir", "/var/lark-bot/users"),
            "FEISHU_BOT_OPEN_ID": ("feishu_bot_open_id", ""),
            "CLAUDE_HOME": ("claude_home", ""),
            "OA_API_KEY": ("oa_api_key", ""),
            "BOT_EVENT_INGRESS": ("bot_event_ingress", "lark_cli"),
        }
        missing = [k for k in required if not os.environ.get(k)]
        if missing:
            raise ValueError(f"缺少必要环境变量: {', '.join(missing)}")
        for env_key, attr in required.items():
            setattr(self, attr, os.environ[env_key])
        for env_key, (attr, default) in optional.items():
            setattr(self, attr, os.environ.get(env_key, default))
        self.bot_event_ingress = str(self.bot_event_ingress or "lark_cli").strip().lower()
        if self.bot_event_ingress not in ("lark_cli", "sdk"):
            raise ValueError("BOT_EVENT_INGRESS must be 'lark_cli' or 'sdk'")
