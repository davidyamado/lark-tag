#!/usr/bin/env python3
# src/meegle_auth_cli.py
"""
CLI tool for Claude to initiate per-user Meegle device-code OAuth.
Called by Claude when `meegle auth status` shows the user is not authenticated.
Writes pending auth state to the DB so the bot can poll and auto-resume.
Output is always a single JSON line to stdout.

Usage:
  python src/meegle_auth_cli.py --open-id OPEN_ID
"""
import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)


def _ok(**kwargs) -> None:
    print(json.dumps({"ok": True, **kwargs}, ensure_ascii=False), flush=True)


def _err(msg: str) -> None:
    print(json.dumps({"ok": False, "error": msg}, ensure_ascii=False), flush=True)
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--open-id", required=True, help="User open_id (used as home dir key)")
    args = parser.parse_args()

    postgres_url = os.environ.get("POSTGRES_URL", "")
    if not postgres_url:
        _err("POSTGRES_URL env var not set")
        return
    users_dir = os.environ.get("LARK_USERS_DIR", os.path.join(os.path.expanduser("~"), "users"))
    bot_home = os.environ.get("LARK_BOT_HOME", os.path.join(os.path.expanduser("~"), "lark-bot-home"))
    app_id = os.environ.get("FEISHU_APP_ID", "")
    app_secret = os.environ.get("FEISHU_APP_SECRET", "")

    try:
        from src.user_store import UserStore
        from src.auth import AuthManager
    except ImportError as e:
        _err(f"Could not import project modules: {e}")
        return

    store = UserStore(postgres_url)
    auth = AuthManager(store, users_dir=users_dir, bot_home=bot_home,
                       app_id=app_id, app_secret=app_secret)

    # Start meegle device-code auth flow via AuthManager
    try:
        data = auth.start_meegle_auth(args.open_id)
    except FileNotFoundError:
        store.close()
        _err("meegle CLI not found; ensure @lark-project/meegle is installed globally")
        return
    except RuntimeError as e:
        store.close()
        _err(str(e))
        return
    except Exception as e:
        store.close()
        _err(f"meegle auth init failed: {e}")
        return

    store.close()

    _ok(
        url=data["url"],
        device_code=data["device_code"],
        client_id=data["client_id"],
        message=(
            f"请点击以下链接完成 Meegle（飞书项目）授权，授权完成后将自动继续处理你的请求：\n{data['url']}"
        ),
    )


if __name__ == "__main__":
    main()
