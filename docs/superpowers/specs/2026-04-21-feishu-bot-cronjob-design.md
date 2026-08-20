# Feishu Bot — Cron Job Feature Design

**Date:** 2026-04-21
**Status:** Implemented

## Context

The Feishu AI bot previously handled only real-time messages. This feature adds scheduled job capability so users can ask the bot (in P2P chat) to create one-shot reminders or recurring AI tasks via natural language. The scheduler fires in the background and proactively sends results to the user.

## User Decisions

| Decision | Choice |
|---|---|
| Task types | Both text reminders AND Claude-executed AI tasks |
| Time input | Natural language, parsed by Claude |
| Confirmation | Smart — Claude creates directly when time is unambiguous, asks follow-up when vague |
| Implementation | Pure SQLite + background polling thread (zero new dependencies, +1 `tzdata` for Windows timezone support) |

## Architecture

```
User message → Claude → python src/job_cli.py create/list/cancel → scheduled_jobs table
                                                                          ↑
SchedulerThread (every 30s) → get_due_jobs() → reminder: feishu_api.send_text_card()
                                              → ai_task: _stream_claude() via _make_scheduled_task_runner()
```

## Database Schema

New `scheduled_jobs` table in the existing SQLite DB (`SQLITE_PATH`):

```sql
CREATE TABLE IF NOT EXISTS scheduled_jobs (
    id            TEXT    PRIMARY KEY,
    open_id       TEXT    NOT NULL,
    chat_id       TEXT    NOT NULL,
    job_type      TEXT    NOT NULL,   -- 'reminder' | 'ai_task'
    content       TEXT    NOT NULL,   -- reminder text OR Claude prompt
    schedule_type TEXT    NOT NULL,   -- 'once' | 'recurring'
    schedule_spec TEXT    NOT NULL,   -- JSON schedule descriptor (see below)
    next_run_at   INTEGER NOT NULL,   -- epoch ms, indexed for fast polling
    status        TEXT    NOT NULL DEFAULT 'active',
    last_run_at   INTEGER,
    run_count     INTEGER NOT NULL DEFAULT 0,
    created_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_due ON scheduled_jobs(next_run_at, status);
```

`schedule_spec` JSON formats:

```json
{"type": "once",    "run_at": "2026-04-22T15:00:00+08:00"}
{"type": "daily",   "time": "09:00", "timezone": "Asia/Shanghai"}
{"type": "weekly",  "day_of_week": 1, "time": "09:00", "timezone": "Asia/Shanghai"}
{"type": "monthly", "day_of_month": 1, "time": "09:00", "timezone": "Asia/Shanghai"}
```

## New Files

| File | Purpose |
|---|---|
| `src/job_store.py` | `JobStore` class — CRUD for `scheduled_jobs` table |
| `src/schedule_utils.py` | `compute_next_run()` and `fmt_next_run()` — pure stdlib datetime/zoneinfo |
| `src/job_cli.py` | CLI tool Claude calls via Bash to create/list/cancel jobs |
| `src/scheduler.py` | `SchedulerThread` — polls every 30s, fires due jobs |
| `tests/test_job_store.py` | 9 tests for JobStore |
| `tests/test_schedule_utils.py` | 14 tests for compute_next_run edge cases |
| `tests/test_job_cli.py` | 8 tests for CLI subcommands |
| `tests/test_scheduler.py` | 6 tests for scheduler dispatch logic |

## Modified Files

| File | Change |
|---|---|
| `src/agent.py` | Injects current time (CST), open_id, and job_cli.py tool docs into system prompt for P2P chats |
| `src/main.py` | Instantiates `JobStore` and `SchedulerThread`; adds `_make_scheduled_task_runner()` helper |
| `requirements.txt` | Added `tzdata>=2024.1` (IANA timezone data for `zoneinfo` on Windows) |

## Execution Flow

### Reminder task
```
SchedulerThread._tick() → get_due_jobs() → _execute_job()
  → feishu_api.send_text_card(open_id, content, token)
  → mark_completed() (once) or update_next_run() (recurring)
```

### AI task
```
SchedulerThread._tick() → get_due_jobs() → _execute_job()
  → stream_claude_fn(open_id, prompt, chat_id)
      → _stream_claude() in main.py → agent.stream_chat() → live card update
  → mark_completed() or update_next_run()
```

## User Interaction Examples

| User says | Claude action |
|---|---|
| "明天下午3点提醒我开会" | `job_cli.py create --job-type reminder` → "✓ 已创建提醒：明天 15:00 开会" |
| "每天早上9点总结飞书消息" | `job_cli.py create --job-type ai_task --schedule '{"type":"daily",...}'` |
| "下周找个时间提醒我" | Claude asks for clarification before calling create |
| "我有哪些定时任务？" | `job_cli.py list` → numbered list |
| "取消任务1" | `job_cli.py cancel --id <uuid>` |
