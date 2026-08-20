#!/usr/bin/env python3
# src/session_reset_cli.py
"""
CLI tool for Claude to request a session reset on behalf of the user.
Called when the user confirms they want to start a fresh conversation.

Sets a pending_session_reset flag in the DB. The bot detects this after
the current Claude run finishes and clears the session instead of saving it,
so the next message starts a completely fresh conversation.

Output is always a single JSON line to stdout.

Usage:
  python src/session_reset_cli.py --open-id OPEN_ID
"""
import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--open-id", required=True,
                        help="context_id (open_id for P2P, g_<chat>_<user> for group)")
    args = parser.parse_args()

    _expected = os.environ.get("FEISHU_OPEN_ID", "")
    if _expected and args.open_id != _expected:
        print(json.dumps({"ok": False, "error": "open-id mismatch: you can only operate on your own session"}, ensure_ascii=False), flush=True)
        sys.exit(1)

    try:
        from src.user_store import UserStore
        postgres_url = os.environ.get("POSTGRES_URL", "")
        if not postgres_url:
            print(json.dumps({"ok": False, "error": "POSTGRES_URL env var not set"}, ensure_ascii=False), flush=True)
            sys.exit(1)
        store = UserStore(postgres_url)
        store.upsert_user(args.open_id, pending_session_reset=1)
        store.close()
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False), flush=True)
        sys.exit(1)

    print(json.dumps({"ok": True}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
