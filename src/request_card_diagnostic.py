"""Small CLI helper used by Claude subprocesses to send minimal diagnostic cards."""

import json
import os
import sys
import urllib.request


def _die(message: str, code: int = 2) -> None:
    print(json.dumps({"ok": False, "error": message}, ensure_ascii=False), file=sys.stderr)
    raise SystemExit(code)


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if len(argv) > 2:
        _die("usage: python request_card_diagnostic.py [ack|toast|sync_card] [nonce]")

    port = os.environ.get("INTERNAL_API_PORT")
    token = os.environ.get("INTERNAL_API_TOKEN")
    open_id = os.environ.get("FEISHU_OPEN_ID")
    if not port or not token or not open_id:
        _die("missing INTERNAL_API_PORT, INTERNAL_API_TOKEN, or FEISHU_OPEN_ID")

    payload = {
        "open_id": open_id,
        "response_mode": argv[0] if argv else "ack",
        "nonce": argv[1] if len(argv) > 1 else "",
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/interactive-form/diagnostic/minimal-card",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        print(resp.read().decode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
