import os
import pytest

from src.form_store import FormStore, stable_event_id


def test_stable_event_id_is_deterministic():
    payload = {
        "session_id": "form_1",
        "message_id": "om_1",
        "operator_open_id": "ou_1",
        "action": "submit",
        "question_index": 0,
        "form_value": {"q_priority_choice": "P0"},
    }

    assert stable_event_id(payload) == stable_event_id(dict(reversed(payload.items())))
    assert stable_event_id(payload).startswith("form_evt_")


_PG_URL = os.environ.get("POSTGRES_TEST_URL", "")
pytestmark_integration = pytest.mark.skipif(not _PG_URL, reason="POSTGRES_TEST_URL not set")


@pytest.fixture
def store():
    s = FormStore(_PG_URL)
    conn = s._conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM form_action_events")
            cur.execute("DELETE FROM form_sessions")
        conn.commit()
    finally:
        s._put(conn)
    yield s
    s.close()


def _schema():
    return {
        "title": "补充信息",
        "questions": [
            {
                "id": "priority",
                "title": "优先级？",
                "type": "single",
                "options": [{"label": "P0"}, {"label": "P1"}],
            },
            {
                "id": "module",
                "title": "模块？",
                "type": "multi",
                "options": [{"label": "前端"}, {"label": "后端"}],
            },
        ],
    }


@pytestmark_integration
def test_create_session_and_get_session(store):
    session = store.create_session(
        context_id="ou_1",
        operator_open_id="ou_1",
        chat_id="oc_1",
        chat_type="p2p",
        reply_msg_id="",
        root_id="",
        thread_session_key="",
        message_id="om_1",
        card_id="card_1",
        original_text="创建需求",
        schema=_schema(),
    )

    loaded = store.get_session(session["id"])
    assert loaded["context_id"] == "ou_1"
    assert loaded["status"] == "active"
    assert loaded["current_index"] == 0
    assert loaded["schema"]["title"] == "补充信息"


@pytestmark_integration
def test_record_event_is_idempotent(store):
    session = store.create_session(
        context_id="ou_1",
        operator_open_id="ou_1",
        chat_id="oc_1",
        chat_type="p2p",
        reply_msg_id="",
        root_id="",
        thread_session_key="",
        message_id="om_1",
        card_id="card_1",
        original_text="创建需求",
        schema=_schema(),
    )

    assert store.record_event("evt_1", session["id"], "submit") is True
    assert store.record_event("evt_1", session["id"], "submit") is False


@pytestmark_integration
def test_submit_previous_and_completion_flow(store):
    session = store.create_session(
        context_id="ou_1",
        operator_open_id="ou_1",
        chat_id="oc_1",
        chat_type="p2p",
        reply_msg_id="",
        root_id="",
        thread_session_key="",
        message_id="om_1",
        card_id="card_1",
        original_text="创建需求",
        schema=_schema(),
    )

    updated, completed = store.apply_submit(
        session["id"],
        0,
        {
            "question_id": "priority",
            "type": "single",
            "values": ["P0"],
            "selected_options": ["P0"],
            "custom_value": "",
        },
    )
    assert completed is False
    assert updated["current_index"] == 1
    assert updated["answers"]["priority"]["values"] == ["P0"]

    previous = store.apply_previous(session["id"])
    assert previous["current_index"] == 0
    assert previous["answers"]["priority"]["values"] == ["P0"]

    updated, completed = store.apply_submit(
        session["id"],
        1,
        {
            "question_id": "module",
            "type": "multi",
            "values": ["前端"],
            "selected_options": ["前端"],
            "custom_value": "",
        },
    )
    assert completed is True
    assert updated["status"] == "returning"


@pytestmark_integration
def test_next_card_sequence_increments(store):
    session = store.create_session(
        context_id="ou_1",
        operator_open_id="ou_1",
        chat_id="oc_1",
        chat_type="p2p",
        reply_msg_id="",
        root_id="",
        thread_session_key="",
        message_id="om_1",
        card_id="card_1",
        original_text="创建需求",
        schema=_schema(),
    )

    assert store.next_card_sequence(session["id"]) == 1
    assert store.next_card_sequence(session["id"]) == 2

