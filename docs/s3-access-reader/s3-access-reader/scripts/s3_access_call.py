from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


DEFAULT_BASE_URL = "https://ai-agent.yo-star.com"


def _env_first(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return None


def _secret_first(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()

        file_env_name = f"{name}_FILE"
        file_path = os.environ.get(file_env_name)
        if not file_path or not file_path.strip():
            continue

        path = file_path.strip()
        try:
            with open(path, encoding="utf-8") as token_file:
                file_value = token_file.read().strip()
        except OSError as exc:
            raise RuntimeError(f"{file_env_name} points to an unreadable file: {path}") from exc
        if not file_value:
            raise RuntimeError(f"{file_env_name} points to an empty file: {path}")
        return file_value
    return None


def _build_query(params: dict[str, Any]) -> str:
    clean = {
        key: value
        for key, value in params.items()
        if value is not None
    }
    return urllib.parse.urlencode(clean)


def _request_json(base_url: str, path: str, params: dict[str, Any], headers: dict[str, str], timeout: float) -> int:
    url = f"{base_url.rstrip('/')}{path}"
    query = _build_query(params)
    if query:
        url = f"{url}?{query}"

    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            **headers,
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            raw_body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        status = exc.code
        raw_body = exc.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        print(f"Request failed: {exc}", file=sys.stderr)
        return 1
    except TimeoutError:
        print(f"Request timed out after {timeout} seconds.", file=sys.stderr)
        return 1

    print(f"GET {url}")
    print(f"Status: {status}")
    if raw_body:
        try:
            print(json.dumps(json.loads(raw_body), indent=2, ensure_ascii=False))
        except json.JSONDecodeError:
            print(raw_body)
    return 0 if 200 <= status < 300 else 1


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Call access-checked yo-agent S3 list/read routes.")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("YO_AGENT_BASE_URL", DEFAULT_BASE_URL),
        help=f"yo-agent base URL. Defaults to YO_AGENT_BASE_URL or {DEFAULT_BASE_URL}.",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="yo-agent API key. Defaults to AGENT-KEY or YO_AGENT_API_KEY.",
    )
    parser.add_argument(
        "--user-access-token",
        default=None,
        help="User access token. Defaults to USER_ACCESS_TOKEN, LARKSUITE_CLI_USER_ACCESS_TOKEN, or FEISHU_USER_ACCESS_TOKEN.",
    )
    parser.add_argument("--bucket", default=None, help="Optional S3 bucket query parameter.")
    parser.add_argument("--timeout", type=float, default=30.0, help="Request timeout in seconds.")

    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="Call GET /s3/list.")
    list_parser.add_argument("--prefix", default="", help="S3 key prefix to list.")
    list_parser.add_argument("--delimiter", default="/", help="S3 delimiter. Use an empty string for flat listing.")
    list_parser.add_argument("--max-keys", type=int, default=1000, help="Maximum keys, 1 to 1000.")
    list_parser.add_argument("--continuation-token", default=None, help="Pagination token from a previous response.")

    read_parser = subparsers.add_parser("read", help="Call GET /s3/read.")
    read_parser.add_argument("--key", required=True, help="Full S3 key to read.")
    read_parser.add_argument("--range-start", type=int, default=None, help="Optional byte range start.")
    read_parser.add_argument("--range-end", type=int, default=None, help="Optional byte range end.")
    read_parser.add_argument("--max-bytes", type=int, default=None, help="Optional maximum bytes to read.")
    read_parser.add_argument("--encoding", default="utf-8", help="Text encoding, default utf-8.")

    args = parser.parse_args(argv)

    try:
        if not args.api_key:
            args.api_key = _secret_first("AGENT-KEY", "AGENT_KEY", "YO_AGENT_API_KEY")
        if not args.user_access_token:
            args.user_access_token = _secret_first(
                "USER_ACCESS_TOKEN",
                "LARKSUITE_CLI_USER_ACCESS_TOKEN",
                "FEISHU_USER_ACCESS_TOKEN",
            )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    missing = []
    if not args.api_key:
        missing.append("AGENT-KEY, AGENT_KEY, YO_AGENT_API_KEY, or matching *_FILE")
    if not args.user_access_token:
        missing.append("USER_ACCESS_TOKEN, LARKSUITE_CLI_USER_ACCESS_TOKEN, FEISHU_USER_ACCESS_TOKEN, or matching *_FILE")
    if missing:
        print(f"Missing required environment/token values: {', '.join(missing)}", file=sys.stderr)
        return 2

    headers = {
        "X-API-Key": args.api_key,
        "X-User-Access-Token": args.user_access_token,
    }

    if args.command == "list":
        params = {
            "prefix": args.prefix,
            "delimiter": args.delimiter,
            "max_keys": args.max_keys,
            "continuation_token": args.continuation_token,
            "bucket": args.bucket,
        }
        return _request_json(args.base_url, "/s3/list", params, headers, args.timeout)

    if args.command == "read":
        params = {
            "key": args.key,
            "range_start": args.range_start,
            "range_end": args.range_end,
            "max_bytes": args.max_bytes,
            "encoding": args.encoding,
            "bucket": args.bucket,
        }
        return _request_json(args.base_url, "/s3/read", params, headers, args.timeout)

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
