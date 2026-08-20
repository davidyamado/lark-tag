# tests/test_job_store.py
import os
import time
import pytest

_PG_URL = os.environ.get("POSTGRES_TEST_URL", "")
pytestmark = pytest.mark.skipif(not _PG_URL, reason="POSTGRES_TEST_URL not set — skipping integration tests")

from src.job_store import JobStore


@pytest.fixture
def store():
    s = JobStore(_PG_URL)
    conn = s._conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM scheduled_jobs")
        conn.commit()
    finally:
        s._put(conn)
    yield s
    s.close()


_SPEC_ONCE = {"type": "once", "run_at": "2099-01-01T09:00:00+08:00"}
_SPEC_DAILY = {"type": "daily", "time": "09:00", "timezone": "Asia/Shanghai"}


def _create(store, open_id="u1", chat_id="c1", job_type="reminder",
            content="test", schedule_type="once", spec=None, next_run_at=None):
    if spec is None:
        spec = _SPEC_ONCE
    if next_run_at is None:
        next_run_at = int(time.time() * 1000) + 60_000
    return store.create_job(open_id, chat_id, job_type, content,
                            schedule_type, spec, next_run_at)


def test_create_and_get(store):
    job_id = _create(store)
    job = store.get_job(job_id)
    assert job is not None
    assert job["open_id"] == "u1"
    assert job["status"] == "active"
    assert isinstance(job["schedule_spec"], dict)
    assert job["run_count"] == 0


def test_list_jobs_only_active(store):
    _create(store, content="a")
    jid = _create(store, content="b")
    store.cancel_job(jid, "u1")
    jobs = store.list_jobs("u1")
    assert len(jobs) == 1
    assert jobs[0]["content"] == "a"


def test_list_jobs_only_own_user(store):
    _create(store, open_id="u1")
    _create(store, open_id="u2")
    assert len(store.list_jobs("u1")) == 1
    assert len(store.list_jobs("u2")) == 1


def test_get_due_jobs(store):
    past_ms = int(time.time() * 1000) - 1000
    future_ms = int(time.time() * 1000) + 60_000
    _create(store, content="past", next_run_at=past_ms)
    _create(store, content="future", next_run_at=future_ms)

    now_ms = int(time.time() * 1000)
    due = store.get_due_jobs(now_ms)
    assert len(due) == 1
    assert due[0]["content"] == "past"


def test_mark_completed(store):
    jid = _create(store)
    store.mark_completed(jid)
    job = store.get_job(jid)
    assert job["status"] == "completed"
    assert job["run_count"] == 1
    assert job["last_run_at"] is not None


def test_update_next_run(store):
    jid = _create(store, schedule_type="recurring", spec=_SPEC_DAILY)
    new_next = int(time.time() * 1000) + 86_400_000
    store.update_next_run(jid, new_next)
    job = store.get_job(jid)
    assert job["next_run_at"] == new_next
    assert job["run_count"] == 1
    assert job["status"] == "active"


def test_cancel_job_own(store):
    jid = _create(store)
    result = store.cancel_job(jid, "u1")
    assert result is True
    assert store.get_job(jid)["status"] == "cancelled"


def test_cancel_job_wrong_user(store):
    jid = _create(store, open_id="u1")
    result = store.cancel_job(jid, "u2")
    assert result is False
    assert store.get_job(jid)["status"] == "active"


def test_due_jobs_excludes_cancelled(store):
    past_ms = int(time.time() * 1000) - 1000
    jid = _create(store, next_run_at=past_ms)
    store.cancel_job(jid, "u1")
    assert store.get_due_jobs(int(time.time() * 1000)) == []
