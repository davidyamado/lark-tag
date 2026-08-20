"""Small CLI helper used by Claude subprocesses to request Feishu form cards."""

import json
import os
import sys
import urllib.request


def _die(message: str, code: int = 2) -> None:
    print(json.dumps({"ok": False, "error": message}, ensure_ascii=False), file=sys.stderr)
    raise SystemExit(code)


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if len(argv) != 1:
        _die("usage: python request_interactive_form.py FORM_SCHEMA_JSON_FILE")

    port = os.environ.get("INTERNAL_API_PORT")
    token = os.environ.get("INTERNAL_API_TOKEN")
    open_id = os.environ.get("FEISHU_OPEN_ID")
    if not port or not token or not open_id:
        _die("missing INTERNAL_API_PORT, INTERNAL_API_TOKEN, or FEISHU_OPEN_ID")

    with open(argv[0], "r", encoding="utf-8") as f:
        payload = json.load(f)
    if not isinstance(payload, dict):
        _die("schema file must contain a JSON object")
    payload.setdefault("open_id", open_id)

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/interactive-form/create",
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
