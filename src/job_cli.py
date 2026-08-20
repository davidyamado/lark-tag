#!/usr/bin/env python3
# src/job_cli.py
"""
CLI tool for Claude to manage scheduled jobs.
Output is always a single JSON line to stdout.

Usage:
  python src/job_cli.py create \\
    --open-id ou_xxx --chat-id oc_xxx \\
    --job-type reminder|ai_task \\
    --schedule '{"type":"once","run_at":"2026-04-22T15:00:00+08:00"}' \\
    --content "提醒文字或AI任务描述"

  python src/job_cli.py list --open-id ou_xxx

  python src/job_cli.py cancel --open-id ou_xxx --id <job_uuid>
"""
import argparse
import json
import os
import sys
import time

# Allow running as `python src/job_cli.py` from repo root
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from src.job_store import JobStore
from src.schedule_utils import compute_next_run, fmt_next_run


def _ok(**kwargs) -> None:
    print(json.dumps({"ok": True, **kwargs}, ensure_ascii=False), flush=True)


def _err(msg: str) -> None:
    print(json.dumps({"ok": False, "error": msg}, ensure_ascii=False), flush=True)
    sys.exit(1)


def _get_store() -> JobStore:
    url = os.environ.get("POSTGRES_URL", "")
    if not url:
        _err("POSTGRES_URL environment variable is not set")
    return JobStore(url)


# ------------------------------------------------------------------
# Sub-commands
# ------------------------------------------------------------------

def cmd_create(args) -> None:
    try:
        spec = json.loads(args.schedule)
    except json.JSONDecodeError as e:
        _err(f"--schedule is not valid JSON: {e}")
        return

    # Validate job_type
    if args.job_type not in ("reminder", "ai_task"):
        _err(f"--job-type must be 'reminder' or 'ai_task', got: {args.job_type!r}")
        return

    # Determine schedule_type from spec
    schedule_type = "once" if spec.get("type") == "once" else "recurring"

    # Compute next_run_at
    now_ms = int(time.time() * 1000)
    try:
        next_run_at = compute_next_run(spec, now_ms)
    except (ValueError, KeyError) as e:
        _err(f"Invalid schedule spec: {e}")
        return

    if schedule_type == "once" and next_run_at <= now_ms:
        _err("Scheduled time is in the past.")
        return

    store = _get_store()
    try:
        job_id = store.create_job(
            open_id=args.open_id,
            chat_id=args.chat_id,
            job_type=args.job_type,
            content=args.content,
            schedule_type=schedule_type,
            schedule_spec=spec,
            next_run_at=next_run_at,
            mention_open_id=args.mention_open_id or None,
        )
    finally:
        store.close()

    _ok(id=job_id, next_run_at=fmt_next_run(next_run_at))


def cmd_list(args) -> None:
    store = _get_store()
    try:
        jobs = store.list_jobs(args.open_id)
    finally:
        store.close()

    formatted = []
    for i, job in enumerate(jobs, 1):
        spec = job["schedule_spec"]
        spec_type = spec.get("type", "?") if isinstance(spec, dict) else "?"
        if spec_type == "once":
            schedule_label = f"一次性 {fmt_next_run(job['next_run_at'])}"
        elif spec_type == "daily":
            schedule_label = f"每天 {spec.get('time', '?')}"
        elif spec_type == "weekly":
            days = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
            dow = spec.get("day_of_week", 0)
            schedule_label = f"每{days[dow]} {spec.get('time', '?')}"
        elif spec_type == "monthly":
            schedule_label = f"每月{spec.get('day_of_month', '?')}号 {spec.get('time', '?')}"
        else:
            schedule_label = spec_type

        formatted.append({
            "index": i,
            "id": job["id"],
            "job_type": job["job_type"],
            "schedule": schedule_label,
            "content": job["content"],
            "run_count": job["run_count"],
            "next_run_at": fmt_next_run(job["next_run_at"]),
        })

    _ok(jobs=formatted, count=len(formatted))


def cmd_cancel(args) -> None:
    store = _get_store()
    try:
        cancelled = store.cancel_job(job_id=args.id, open_id=args.open_id)
    finally:
        store.close()

    if not cancelled:
        _err(f"Job {args.id!r} not found or does not belong to you.")
        return
    _ok(id=args.id)


# ------------------------------------------------------------------
# Argument parsing
# ------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Feishu bot job manager")
    sub = parser.add_subparsers(dest="command", required=True)

    # create
    p_create = sub.add_parser("create", help="Create a scheduled job")
    p_create.add_argument("--open-id", required=True)
    p_create.add_argument("--chat-id", required=True)
    p_create.add_argument("--job-type", required=True, choices=["reminder", "ai_task"])
    p_create.add_argument("--schedule", required=True,
                          help='JSON schedule spec, e.g. \'{"type":"once","run_at":"2026-04-22T15:00:00+08:00"}\'')
    p_create.add_argument("--content", required=True,
                          help="Reminder text or AI task prompt")
    p_create.add_argument("--mention-open-id", default="",
                          help="open_id to @mention in group reminder (optional)")

    # list
    p_list = sub.add_parser("list", help="List active jobs for a user")
    p_list.add_argument("--open-id", required=True)

    # cancel
    p_cancel = sub.add_parser("cancel", help="Cancel a job")
    p_cancel.add_argument("--open-id", required=True)
    p_cancel.add_argument("--id", required=True, help="Job UUID")

    args = parser.parse_args()

    if args.command == "create":
        cmd_create(args)
    elif args.command == "list":
        cmd_list(args)
    elif args.command == "cancel":
        cmd_cancel(args)


if __name__ == "__main__":
    main()
