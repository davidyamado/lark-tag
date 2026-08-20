#!/usr/bin/env python3
"""
scripts/audit_query.py — Operator-facing helper to query the cross-pod audit
log table from inside any bot pod.

Usage examples:
  kubectl exec -ti <pod> -- python /app/scripts/audit_query.py --user 戴维 --limit 20
  kubectl exec -ti <pod> -- python /app/scripts/audit_query.py --chat oc_xxx --since "1 hour ago"
  kubectl exec -ti <pod> -- python /app/scripts/audit_query.py --grep "lark-cli"
  kubectl exec -ti <pod> -- python /app/scripts/audit_query.py --msg om_xxx
  kubectl exec -ti <pod> -- python /app/scripts/audit_query.py --type security_refusal --since "1 day ago"
  kubectl exec -ti <pod> -- python /app/scripts/audit_query.py --user 戴维 --json   # JSONL output

Filters can be combined freely; all results are ordered by ts DESC.

Identity options:
  --user accepts either a display name (resolved via users table) or an open_id
         (starts with "ou_"). If a name matches multiple users you'll be asked
         to pick by open_id.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
from typing import Optional

# Load .env.local then .env when running locally (no-op in containers).
try:
    from dotenv import load_dotenv
    _root = os.path.join(os.path.dirname(__file__), "..")
    load_dotenv(os.path.join(_root, ".env.local"))
    load_dotenv(os.path.join(_root, ".env"))
except ImportError:
    pass

# In containers, kubectl exec sessions may not inherit all env vars from PID 1.
# Read /proc/1/environ as a fallback so POSTGRES_URL is always reachable.
try:
    with open("/proc/1/environ", "rb") as _f:
        for _item in _f.read().split(b"\x00"):
            if b"=" in _item:
                _k, _v = _item.split(b"=", 1)
                _key = _k.decode(errors="replace")
                if _key not in os.environ:
                    os.environ[_key] = _v.decode(errors="replace")
except Exception:
    pass

import psycopg2
import psycopg2.extras


def _resolve_user(conn, who: str) -> str:
    """Accept either 'ou_xxx' or a display name; return one open_id."""
    if who.startswith("ou_"):
        return who
    with conn.cursor() as cur:
        cur.execute(
            "SELECT open_id, display_name FROM users WHERE display_name = %s",
            (who,),
        )
        rows = cur.fetchall()
    if not rows:
        print(f"No user matches name {who!r}", file=sys.stderr)
        sys.exit(2)
    if len(rows) > 1:
        print(f"Multiple users named {who!r}; specify --user with an open_id:", file=sys.stderr)
        for r in rows:
            print(f"  {r['open_id']}  {r['display_name']}", file=sys.stderr)
        sys.exit(2)
    return rows[0]["open_id"]


# Very rough natural-language duration parser — covers "30 minutes ago",
# "1 hour ago", "2 days ago", "1 week ago". Anything else gets treated as an
# ISO timestamp.
_DUR_RE = re.compile(r"^\s*(\d+)\s*(minute|hour|day|week)s?\s*(?:ago)?\s*$", re.IGNORECASE)


def _parse_since(s: str) -> datetime.datetime:
    m = _DUR_RE.match(s)
    if m:
        amt = int(m.group(1))
        unit = m.group(2).lower()
        delta = {"minute": datetime.timedelta(minutes=amt),
                 "hour": datetime.timedelta(hours=amt),
                 "day": datetime.timedelta(days=amt),
                 "week": datetime.timedelta(weeks=amt)}[unit]
        return datetime.datetime.now(datetime.timezone.utc) - delta
    return datetime.datetime.fromisoformat(s)


def _format_row(row: dict, content_width: int = 100) -> str:
    ts = row["ts"].strftime("%Y-%m-%d %H:%M:%S")
    et = row["event_type"]
    name = row["display_name"] or row["open_id"]
    chat = row["chat_type"] or ""
    if row["chat_type"] == "group" and row.get("chat_id"):
        chat = f"group {row['chat_id'][:10]}…"
    mid = (row["message_id"] or "")[:14]
    at = ""
    if row.get("is_at_bot") is True: at = " @bot"
    elif row.get("is_at_bot") is False and row["chat_type"] == "group": at = " (idle)"
    body = (row["content"] or "").replace("\n", " ").strip()
    if len(body) > content_width:
        body = body[:content_width] + "…"
    return f"{ts}  {et:>16}  {name:<12}  {chat:<22}  {mid}{at}  {body}"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--user", help="display name or ou_xxx open_id")
    p.add_argument("--chat", dest="chat_id", help="filter by chat_id (oc_xxx)")
    p.add_argument("--msg", dest="message_id", help="filter by message_id (om_xxx)")
    p.add_argument("--type", dest="event_type",
                   choices=["received", "replied", "poll_recovered",
                            "security_refusal", "claude_error"],
                   help="filter by event_type")
    p.add_argument("--since", help='e.g. "1 hour ago" or "2026-05-19T00:00"')
    p.add_argument("--grep", help="substring match on content (ILIKE)")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--json", action="store_true", help="emit one JSON object per line")
    p.add_argument("--full", action="store_true", help="don't truncate content")
    args = p.parse_args()

    url = os.environ.get("POSTGRES_URL", "")
    if not url:
        print("POSTGRES_URL is not set", file=sys.stderr)
        sys.exit(1)

    conn = psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        open_id: Optional[str] = None
        if args.user:
            open_id = _resolve_user(conn, args.user)

        sql = "SELECT * FROM audit_log WHERE TRUE"
        params: list = []
        if open_id:
            sql += " AND open_id = %s"; params.append(open_id)
        if args.chat_id:
            sql += " AND chat_id = %s"; params.append(args.chat_id)
        if args.message_id:
            sql += " AND message_id = %s"; params.append(args.message_id)
        if args.event_type:
            sql += " AND event_type = %s"; params.append(args.event_type)
        if args.since:
            sql += " AND ts >= %s"; params.append(_parse_since(args.since))
        if args.grep:
            sql += " AND content ILIKE %s"; params.append(f"%{args.grep}%")
        sql += " ORDER BY ts DESC LIMIT %s"
        params.append(args.limit)

        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

        if args.json:
            for r in rows:
                r = dict(r)
                r["ts"] = r["ts"].isoformat()
                if r.get("extra") is None:
                    r.pop("extra", None)
                print(json.dumps(r, ensure_ascii=False, default=str))
        else:
            width = 10_000 if args.full else 100
            for r in rows:
                print(_format_row(dict(r), content_width=width))
            print(f"\n— {len(rows)} row(s) —", file=sys.stderr)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
