# tests/test_scheduler.py
"""Tests for SchedulerThread job dispatch logic."""
import json
import os
import time
import uuid
from unittest.mock import MagicMock, patch
import pytest

from src.scheduler import SchedulerThread


def _make_mock_job_store():
    """In-memory mock of JobStore for testing scheduler dispatch."""
    store = MagicMock()
    _jobs = {}

    def create_job(open_id, chat_id, job_type, content, schedule_type,
                   schedule_spec, next_run_at, mention_open_id=None):
        jid = str(uuid.uuid4())
        _jobs[jid] = {
            "id": jid, "open_id": open_id, "chat_id": chat_id,
            "job_type": job_type, "content": content,
            "schedule_type": schedule_type,
            "schedule_spec": schedule_spec if isinstance(schedule_spec, dict) else json.loads(schedule_spec),
            "next_run_at": next_run_at, "status": "active",
            "last_run_at": None, "run_count": 0,
            "created_at": int(time.time() * 1000),
            "mention_open_id": mention_open_id,
        }
        return jid

    def get_job(jid):
        return _jobs.get(jid)

    def get_due_jobs(now_ms):
        return [j for j in _jobs.values()
                if j["status"] == "active" and j["next_run_at"] <= now_ms]

    def _claim_due_jobs(now_ms, limit=50, lease_seconds=300):
        due = get_due_jobs(now_ms)[:limit]
        for job in due:
            job["status"] = "running"
        return [dict(j) for j in due]

    def mark_completed(jid):
        if jid in _jobs:
            _jobs[jid]["status"] = "completed"
            _jobs[jid]["run_count"] += 1
            _jobs[jid]["last_run_at"] = int(time.time() * 1000)

    def update_next_run(jid, next_run_at):
        if jid in _jobs:
            _jobs[jid]["next_run_at"] = next_run_at
            _jobs[jid]["status"] = "active"
            _jobs[jid]["run_count"] += 1
            _jobs[jid]["last_run_at"] = int(time.time() * 1000)

    def cancel_job(jid, open_id):
        if jid in _jobs and _jobs[jid]["open_id"] == open_id:
            _jobs[jid]["status"] = "cancelled"
            return True
        return False

    store.create_job = create_job
    store.get_job = get_job
    store.get_due_jobs = get_due_jobs
    store.claim_due_jobs = MagicMock(side_effect=_claim_due_jobs)
    store.mark_completed = mark_completed
    store.update_next_run = update_next_run
    store.cancel_job = cancel_job
    store.close = MagicMock()
    return store


@pytest.fixture
def job_store():
    return _make_mock_job_store()


def _make_scheduler(job_store, stream_fn=None):
    mock_feishu = MagicMock()
    mock_feishu.get_tenant_access_token.return_value = "tok"
    mock_executor = MagicMock()
    mock_executor.submit.side_effect = lambda fn, *args, **kwargs: fn(*args, **kwargs)

    st = SchedulerThread(
        job_store=job_store,
        executor=mock_executor,
        feishu_api=mock_feishu,
        app_id="app_id",
        app_secret="app_secret",
        stream_claude_fn=stream_fn or MagicMock(),
    )
    return st, mock_feishu


def _past_ms():
    return int(time.time() * 1000) - 1000


def _future_ms():
    return int(time.time() * 1000) + 60_000


def test_tick_fires_due_reminder_group(job_store):
    """Group chat reminder → send_text_card_to_chat with @mention prefix."""
    job_store.create_job(
        open_id="ou_1", chat_id="oc_1", job_type="reminder",
        content="记得开会", schedule_type="once",
        schedule_spec={"type": "once", "run_at": "2020-01-01T09:00:00+08:00"},
        next_run_at=_past_ms(),
        mention_open_id="ou_1",
    )

    st, mock_feishu = _make_scheduler(job_store)
    st._tick()

    mock_feishu.send_text_card_to_chat.assert_called_once_with(
        "oc_1", '<at id="ou_1"></at> 记得开会', "tok"
    )
    mock_feishu.send_text_card.assert_not_called()


def test_tick_fires_due_reminder_group_fallback_mention(job_store):
    """Group reminder without mention_open_id falls back to job's open_id."""
    job_store.create_job(
        open_id="ou_1", chat_id="oc_1", job_type="reminder",
        content="记得喝水", schedule_type="once",
        schedule_spec={"type": "once", "run_at": "2020-01-01T09:00:00+08:00"},
        next_run_at=_past_ms(),
    )

    st, mock_feishu = _make_scheduler(job_store)
    st._tick()

    mock_feishu.send_text_card_to_chat.assert_called_once_with(
        "oc_1", '<at id="ou_1"></at> 记得喝水', "tok"
    )


def test_tick_fires_due_reminder_p2p(job_store):
    """P2P chat reminder (chat_id does not start with oc_) → send_text_card."""
    job_store.create_job(
        open_id="ou_1", chat_id="p2p_1", job_type="reminder",
        content="记得开会", schedule_type="once",
        schedule_spec={"type": "once", "run_at": "2020-01-01T09:00:00+08:00"},
        next_run_at=_past_ms(),
    )

    st, mock_feishu = _make_scheduler(job_store)
    st._tick()

    mock_feishu.send_text_card.assert_called_once_with("ou_1", "记得开会", "tok")
    mock_feishu.send_text_card_to_chat.assert_not_called()


def test_tick_skips_future_job(job_store):
    job_store.create_job(
        open_id="ou_1", chat_id="oc_1", job_type="reminder",
        content="未来的提醒", schedule_type="once",
        schedule_spec={"type": "once", "run_at": "2099-01-01T09:00:00+08:00"},
        next_run_at=_future_ms(),
    )

    st, mock_feishu = _make_scheduler(job_store)
    st._tick()

    mock_feishu.send_text_card.assert_not_called()


def test_once_job_marked_completed_after_fire(job_store):
    jid = job_store.create_job(
        open_id="ou_1", chat_id="oc_1", job_type="reminder",
        content="test", schedule_type="once",
        schedule_spec={"type": "once", "run_at": "2020-01-01T09:00:00+08:00"},
        next_run_at=_past_ms(),
    )

    st, _ = _make_scheduler(job_store)
    st._tick()

    job = job_store.get_job(jid)
    assert job["status"] == "completed"


def test_recurring_job_gets_rescheduled(job_store):
    jid = job_store.create_job(
        open_id="ou_1", chat_id="oc_1", job_type="reminder",
        content="每日提醒", schedule_type="recurring",
        schedule_spec={"type": "daily", "time": "09:00", "timezone": "Asia/Shanghai"},
        next_run_at=_past_ms(),
    )
    original_next = job_store.get_job(jid)["next_run_at"]

    st, _ = _make_scheduler(job_store)
    st._tick()

    job = job_store.get_job(jid)
    assert job["status"] == "active"
    assert job["next_run_at"] > original_next
    assert job["run_count"] == 1


def test_ai_task_calls_stream_fn(job_store):
    stream_fn = MagicMock()
    job_store.create_job(
        open_id="ou_1", chat_id="oc_1", job_type="ai_task",
        content="总结消息", schedule_type="once",
        schedule_spec={"type": "once", "run_at": "2020-01-01T09:00:00+08:00"},
        next_run_at=_past_ms(),
    )

    st, mock_feishu = _make_scheduler(job_store, stream_fn=stream_fn)
    st._tick()

    stream_fn.assert_called_once_with(open_id="ou_1", prompt="总结消息", chat_id="oc_1")
    mock_feishu.send_text_card.assert_not_called()


def test_tick_does_not_fire_cancelled_job(job_store):
    jid = job_store.create_job(
        open_id="ou_1", chat_id="oc_1", job_type="reminder",
        content="已取消", schedule_type="once",
        schedule_spec={"type": "once", "run_at": "2020-01-01T09:00:00+08:00"},
        next_run_at=_past_ms(),
    )
    job_store.cancel_job(jid, "ou_1")

    st, mock_feishu = _make_scheduler(job_store)
    st._tick()

    mock_feishu.send_text_card.assert_not_called()


def test_tick_claims_due_jobs_before_dispatch(job_store):
    """Scheduler must atomically claim due jobs so multiple pods do not dispatch the same row."""
    job_store.create_job(
        open_id="ou_1", chat_id="oc_1", job_type="reminder",
        content="only once", schedule_type="once",
        schedule_spec={"type": "once", "run_at": "2020-01-01T09:00:00+08:00"},
        next_run_at=_past_ms(),
    )

    st, mock_feishu = _make_scheduler(job_store)
    st._tick()
    st._tick()

    assert job_store.claim_due_jobs.call_count == 2
    mock_feishu.send_text_card_to_chat.assert_called_once()
