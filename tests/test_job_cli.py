# tests/test_job_cli.py
"""Tests for job_cli.py subcommands."""
import json
import os
import sys
import time
import uuid
import pytest
from unittest.mock import patch, MagicMock


def _make_mock_job_store():
    """In-memory mock of JobStore for CLI testing."""
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

    def list_jobs(open_id):
        return [j for j in _jobs.values()
                if j["open_id"] == open_id and j["status"] == "active"]

    def get_job(jid):
        return _jobs.get(jid)

    def cancel_job(job_id, open_id):
        j = _jobs.get(job_id)
        if j and j["open_id"] == open_id:
            j["status"] = "cancelled"
            return True
        return False

    store.create_job = create_job
    store.list_jobs = list_jobs
    store.get_job = get_job
    store.cancel_job = cancel_job
    store.close = MagicMock()
    return store


_mock_store_instance = None


def _patched_get_store():
    return _mock_store_instance


def _run_cli(args: list[str]) -> dict:
    """Run job_cli main() with given args, return parsed JSON output."""
    import io

    global _mock_store_instance
    _mock_store_instance = _make_mock_job_store()

    with patch.dict(os.environ, {"POSTGRES_URL": "postgresql://fake:fake@localhost/fake"}):
        with patch("sys.argv", ["job_cli.py"] + args):
            captured = io.StringIO()
            with patch("sys.stdout", captured):
                try:
                    from src import job_cli
                    import importlib
                    importlib.reload(job_cli)
                    with patch.object(job_cli, "_get_store", _patched_get_store):
                        job_cli.main()
                except SystemExit:
                    pass
            output = captured.getvalue().strip()

    for line in reversed(output.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise AssertionError(f"No JSON output found in: {output!r}")


# For tests that need to share state across multiple CLI calls
def _run_cli_with_store(args: list[str], store) -> dict:
    """Run job_cli main() with a shared store instance."""
    import io

    global _mock_store_instance
    _mock_store_instance = store

    with patch.dict(os.environ, {"POSTGRES_URL": "postgresql://fake:fake@localhost/fake"}):
        with patch("sys.argv", ["job_cli.py"] + args):
            captured = io.StringIO()
            with patch("sys.stdout", captured):
                try:
                    from src import job_cli
                    import importlib
                    importlib.reload(job_cli)
                    with patch.object(job_cli, "_get_store", _patched_get_store):
                        job_cli.main()
                except SystemExit:
                    pass
            output = captured.getvalue().strip()

    for line in reversed(output.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise AssertionError(f"No JSON output found in: {output!r}")


def test_create_once_reminder():
    result = _run_cli([
        "create",
        "--open-id", "ou_test",
        "--chat-id", "oc_test",
        "--job-type", "reminder",
        "--schedule", '{"type":"once","run_at":"2099-06-15T10:00:00+08:00"}',
        "--content", "记得开会",
    ])
    assert result["ok"] is True
    assert "id" in result
    assert "next_run_at" in result


def test_create_daily_ai_task():
    result = _run_cli([
        "create",
        "--open-id", "ou_test",
        "--chat-id", "oc_test",
        "--job-type", "ai_task",
        "--schedule", '{"type":"daily","time":"09:00","timezone":"Asia/Shanghai"}',
        "--content", "总结今天的飞书消息",
    ])
    assert result["ok"] is True


def test_create_past_time_fails():
    result = _run_cli([
        "create",
        "--open-id", "ou_test",
        "--chat-id", "oc_test",
        "--job-type", "reminder",
        "--schedule", '{"type":"once","run_at":"2020-01-01T10:00:00+08:00"}',
        "--content", "过去的提醒",
    ])
    assert result["ok"] is False
    assert "past" in result["error"].lower()


def test_create_invalid_json_schedule():
    result = _run_cli([
        "create",
        "--open-id", "ou_test",
        "--chat-id", "oc_test",
        "--job-type", "reminder",
        "--schedule", "not-json",
        "--content", "test",
    ])
    assert result["ok"] is False


def test_list_empty():
    result = _run_cli(["list", "--open-id", "ou_test"])
    assert result["ok"] is True
    assert result["jobs"] == []
    assert result["count"] == 0


def test_list_shows_created_job():
    shared_store = _make_mock_job_store()
    _run_cli_with_store([
        "create",
        "--open-id", "ou_test",
        "--chat-id", "oc_test",
        "--job-type", "reminder",
        "--schedule", '{"type":"once","run_at":"2099-06-15T10:00:00+08:00"}',
        "--content", "记得开会",
    ], shared_store)
    result = _run_cli_with_store(["list", "--open-id", "ou_test"], shared_store)
    assert result["ok"] is True
    assert result["count"] == 1
    assert result["jobs"][0]["content"] == "记得开会"
    assert result["jobs"][0]["index"] == 1


def test_cancel_job():
    shared_store = _make_mock_job_store()
    create_result = _run_cli_with_store([
        "create",
        "--open-id", "ou_test",
        "--chat-id", "oc_test",
        "--job-type", "reminder",
        "--schedule", '{"type":"once","run_at":"2099-06-15T10:00:00+08:00"}',
        "--content", "记得开会",
    ], shared_store)
    job_id = create_result["id"]
    cancel_result = _run_cli_with_store(["cancel", "--open-id", "ou_test", "--id", job_id], shared_store)
    assert cancel_result["ok"] is True

    list_result = _run_cli_with_store(["list", "--open-id", "ou_test"], shared_store)
    assert list_result["count"] == 0


def test_cancel_wrong_user_fails():
    shared_store = _make_mock_job_store()
    create_result = _run_cli_with_store([
        "create",
        "--open-id", "ou_owner",
        "--chat-id", "oc_test",
        "--job-type", "reminder",
        "--schedule", '{"type":"once","run_at":"2099-06-15T10:00:00+08:00"}',
        "--content", "只属于我",
    ], shared_store)
    job_id = create_result["id"]
    result = _run_cli_with_store(["cancel", "--open-id", "ou_other", "--id", job_id], shared_store)
    assert result["ok"] is False
