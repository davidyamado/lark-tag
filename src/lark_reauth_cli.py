#!/usr/bin/env python3
# src/lark_reauth_cli.py
"""
CLI tool for Claude to revoke and re-initiate per-user lark-cli device-code OAuth.
Called by Claude when lark-cli returns permission errors (missing OAuth scope).
Writes pending auth state to the DB so the bot can poll and auto-resume the original
request — the user never needs to resend their message.

Output is always a single JSON line to stdout.

Usage:
  python src/lark_reauth_cli.py --open-id OPEN_ID
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
    parser.add_argument("--open-id", required=True,
                        help="context_id used as home dir key (open_id for P2P, g_<chat>_<user> for group)")
    args = parser.parse_args()

    _expected = os.environ.get("FEISHU_OPEN_ID", "")
    if _expected and args.open_id != _expected:
        _err("open-id mismatch: you can only operate on your own session")
        return

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

    # Step 1: revoke old token (non-fatal)
    try:
        auth.revoke_token(args.open_id)
    except Exception:
        pass  # non-fatal — proceed to re-init regardless

    # Step 2: start a new device-code auth flow via AuthManager
    try:
        data = auth.start_auth(args.open_id)
    except FileNotFoundError:
        store.close()
        _err("lark-cli not found; ensure @larksuite/cli is installed globally")
        return
    except RuntimeError as e:
        store.close()
        _err(str(e))
        return
    except Exception as e:
        store.close()
        _err(f"lark-cli auth login failed: {e}")
        return

    store.close()

    url = data.get("verification_url", "")
    device_code = data.get("device_code", "")
    _ok(
        url=url,
        device_code=device_code,
        message=(
            f"飞书授权 token 权限不足，已发起重新授权。\n"
            f"请点击以下链接完成授权，授权完成后将自动继续处理您的请求：\n{url}"
        ),
    )


if __name__ == "__main__":
    main()
