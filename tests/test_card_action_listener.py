from unittest.mock import MagicMock

import asyncio
import json
import logging

from src.card_action_listener import (
    CardActionEvent,
    InteractiveFormHandler,
    instrument_ws_client_writes,
    parse_card_action_event,
    register_ignored_sdk_events,
)


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


def _session(current_index=0, status="active"):
    return {
        "id": "form_1",
        "context_id": "ou_1",
        "operator_open_id": "ou_1",
        "card_id": "om_card",
        "message_id": "om_user",
        "status": status,
        "current_index": current_index,
        "schema": _schema(),
        "answers": {},
        "original_text": "创建需求",
    }


def test_parse_card_action_event_extracts_action_and_form_value():
    raw = {
        "header": {"event_id": "evt_1"},
        "event": {
            "operator": {"open_id": "ou_1"},
            "token": "c_update_token",
            "context": {"open_message_id": "om_card"},
            "action": {
                "value": {"session_id": "form_1", "action": "submit", "question_index": 0},
                "form_value": {"q_priority_choice": "P0"},
            },
        },
    }

    event = parse_card_action_event(raw)

    assert event == CardActionEvent(
        session_id="form_1",
        action="submit",
        question_index=0,
        operator_open_id="ou_1",
        form_value={"q_priority_choice": "P0"},
        event_id="evt_1",
        message_id="om_card",
        callback_token="c_update_token",
    )


def test_parse_card_action_event_tolerates_string_callback_value():
    raw = {
        "header": {"event_id": "evt_1"},
        "event": {
            "operator": {"open_id": "ou_1"},
            "context": {"open_message_id": "om_card"},
            "action": {
                "value": "bad-string-callback-value",
                "form_value": {"q_priority_choice": "P0"},
            },
        },
    }

    event = parse_card_action_event(raw)

    assert event.session_id == ""
    assert event.action == ""
    assert event.question_index == 0
    assert event.form_value == {"q_priority_choice": "P0"}
    assert event.value_type == "str"
    assert event.form_value_type == "dict"


def test_parse_card_action_event_extracts_diagnostic_fields():
    raw = {
        "header": {"event_id": "evt_diag"},
        "event": {
            "operator": {"open_id": "ou_1"},
            "context": {"open_message_id": "om_diag"},
            "action": {
                "value": {
                    "action": "diagnostic_minimal",
                    "response_mode": "toast",
                    "nonce": "diag_1",
                },
            },
        },
    }

    event = parse_card_action_event(raw)

    assert event.action == "diagnostic_minimal"
    assert event.response_mode == "toast"
    assert event.nonce == "diag_1"
    assert event.session_id == ""


def test_diagnostic_minimal_ack_does_not_touch_form_store():
    store = MagicMock()
    api = MagicMock()
    handler = InteractiveFormHandler(store, api, "app", "secret")

    result = handler.handle_action(CardActionEvent(
        session_id="",
        action="diagnostic_minimal",
        question_index=0,
        operator_open_id="ou_1",
        form_value={},
        event_id="evt_diag",
        message_id="om_diag",
        response_mode="ack",
        nonce="diag_1",
    ))

    assert result == {"ok": True, "diagnostic": True, "response_mode": "ack", "nonce": "diag_1"}
    store.get_session.assert_not_called()
    store.record_event.assert_not_called()
    api.update_interactive_card.assert_not_called()


def test_diagnostic_minimal_toast_returns_visible_feedback():
    handler = InteractiveFormHandler(MagicMock(), MagicMock(), "app", "secret")

    result = handler.handle_action(CardActionEvent(
        session_id="",
        action="diagnostic_minimal",
        question_index=0,
        operator_open_id="ou_1",
        form_value={},
        response_mode="toast",
        nonce="diag_2",
    ))

    assert result["diagnostic"] is True
    assert result["toast"]["type"] == "success"
    assert "diag_2" in result["toast"]["content"]


def test_diagnostic_minimal_sync_card_returns_static_card():
    handler = InteractiveFormHandler(MagicMock(), MagicMock(), "app", "secret")

    result = handler.handle_action(CardActionEvent(
        session_id="",
        action="diagnostic_minimal",
        question_index=0,
        operator_open_id="ou_1",
        form_value={},
        response_mode="sync_card",
        nonce="diag_3",
    ))

    assert result["diagnostic"] is True
    assert result["card"]["schema"] == "2.0"
    assert "diagnostic_minimal" not in json.dumps(result["card"], ensure_ascii=False)


def test_non_owner_click_is_ignored_without_recording_event():
    store = MagicMock()
    store.get_session.return_value = _session()
    handler = InteractiveFormHandler(store, MagicMock(), "app", "secret")

    result = handler.handle_action(CardActionEvent(
        session_id="form_1",
        action="submit",
        question_index=0,
        operator_open_id="ou_other",
        form_value={"q_priority_choice": "P0"},
        event_id="evt_1",
        message_id="om_card",
    ))

    assert result["ignored"] is True
    store.record_event.assert_not_called()


def test_duplicate_event_acks_without_updating_card():
    store = MagicMock()
    store.get_session.return_value = _session()
    store.record_event.return_value = False
    api = MagicMock()
    handler = InteractiveFormHandler(store, api, "app", "secret")

    result = handler.handle_action(CardActionEvent(
        session_id="form_1",
        action="submit",
        question_index=0,
        operator_open_id="ou_1",
        form_value={"q_priority_choice": "P0"},
        event_id="evt_1",
        message_id="om_card",
    ))

    assert result["duplicate"] is True
    api.update_interactive_card.assert_not_called()


def test_select_option_acknowledges_without_state_change_or_card_update():
    store = MagicMock()
    api = MagicMock()
    handler = InteractiveFormHandler(store, api, "app", "secret")

    result = handler.handle_action(CardActionEvent(
        session_id="form_1",
        action="select_option",
        question_index=0,
        operator_open_id="ou_1",
        form_value={"q_priority_opt_0": True},
        event_id="evt_select",
        message_id="om_card",
        option_index=0,
    ))

    assert result == {"ok": True, "ignored": True}
    store.get_session.assert_not_called()
    store.record_event.assert_not_called()
    api.update_interactive_card.assert_not_called()


def test_submit_saves_answer_and_schedules_next_question_card_without_callback_card():
    store = MagicMock()
    store.get_session.return_value = _session(current_index=0)
    store.record_event.return_value = True
    updated = _session(current_index=1)
    updated["answers"] = {
        "priority": {
            "question_id": "priority",
            "type": "single",
            "values": ["P0"],
            "selected_options": ["P0"],
            "custom_value": "",
        }
    }
    store.apply_submit.return_value = (updated, False)
    api = MagicMock()
    on_card_update = MagicMock()
    handler = InteractiveFormHandler(store, api, "app", "secret", on_card_update=on_card_update)

    result = handler.handle_action(CardActionEvent(
        session_id="form_1",
        action="submit",
        question_index=0,
        operator_open_id="ou_1",
        form_value={"q_priority_choice": "P0"},
        event_id="evt_1",
        message_id="om_card",
    ))

    assert result["completed"] is False
    assert result["deferred_card_update"] is True
    assert "card" not in result
    answer = store.apply_submit.call_args.args[2]
    assert answer["values"] == ["P0"]
    api.update_interactive_card.assert_not_called()
    api.get_tenant_access_token.assert_not_called()
    store.next_card_sequence.assert_not_called()
    on_card_update.assert_called_once()
    assert on_card_update.call_args.args[0] is updated
    assert on_card_update.call_args.args[1]["body"]["elements"][1]["content"] == "**问题 2 / 2**"
    assert on_card_update.call_args.args[2] == ""


def test_submit_returns_next_question_card_when_deferred_updates_disabled():
    store = MagicMock()
    store.get_session.return_value = _session(current_index=0)
    store.record_event.return_value = True
    updated = _session(current_index=1)
    store.apply_submit.return_value = (updated, False)
    api = MagicMock()
    on_card_update = MagicMock()
    handler = InteractiveFormHandler(
        store,
        api,
        "app",
        "secret",
        on_card_update=on_card_update,
        defer_card_updates=False,
    )

    result = handler.handle_action(CardActionEvent(
        session_id="form_1",
        action="submit",
        question_index=0,
        operator_open_id="ou_1",
        form_value={"q_priority_choice": "P0"},
        event_id="evt_1",
        message_id="om_card",
    ))

    assert result["completed"] is False
    assert result["card"]["body"]["elements"][1]["content"] == "**问题 2 / 2**"
    assert "deferred_card_update" not in result
    on_card_update.assert_not_called()


def test_submit_passes_callback_token_to_deferred_card_update():
    store = MagicMock()
    store.get_session.return_value = _session(current_index=0)
    store.record_event.return_value = True
    updated = _session(current_index=1)
    store.apply_submit.return_value = (updated, False)
    api = MagicMock()
    on_card_update = MagicMock()
    handler = InteractiveFormHandler(store, api, "app", "secret", on_card_update=on_card_update)

    handler.handle_action(CardActionEvent(
        session_id="form_1",
        action="submit",
        question_index=0,
        operator_open_id="ou_1",
        form_value={"q_priority_choice": "P0"},
        event_id="evt_1",
        message_id="om_card",
        callback_token="c_update_token",
    ))

    assert on_card_update.call_args.args[2] == "c_update_token"


def test_single_submit_with_multiple_checked_options_returns_toast_without_saving():
    store = MagicMock()
    store.get_session.return_value = _session(current_index=0)
    api = MagicMock()
    handler = InteractiveFormHandler(store, api, "app", "secret")

    result = handler.handle_action(CardActionEvent(
        session_id="form_1",
        action="submit",
        question_index=0,
        operator_open_id="ou_1",
        form_value={"q_priority_opt_0": True, "q_priority_opt_1": True},
        event_id="evt_submit",
        message_id="om_card",
    ))

    assert result["ok"] is False
    assert result["toast"] == {
        "type": "warning",
        "content": "当前问题为单选，请选择一个合适的答案",
    }
    store.record_event.assert_not_called()
    store.apply_submit.assert_not_called()
    api.update_interactive_card.assert_not_called()


def test_stale_submit_for_previous_question_is_ignored_without_updating_card():
    store = MagicMock()
    store.get_session.return_value = _session(current_index=1)
    api = MagicMock()
    handler = InteractiveFormHandler(store, api, "app", "secret")

    result = handler.handle_action(CardActionEvent(
        session_id="form_1",
        action="submit",
        question_index=0,
        operator_open_id="ou_1",
        form_value={"q_priority_choice": "P0"},
        event_id="evt_late",
        message_id="om_card",
    ))

    assert result["ok"] is True
    assert result["stale"] is True
    store.record_event.assert_not_called()
    store.apply_submit.assert_not_called()
    api.update_interactive_card.assert_not_called()


def test_stale_submit_schedules_current_question_card_without_callback_card():
    store = MagicMock()
    current = _session(current_index=1)
    current["answers"] = {
        "priority": {
            "question_id": "priority",
            "type": "single",
            "values": ["P0"],
            "selected_options": ["P0"],
            "custom_value": "",
        }
    }
    store.get_session.return_value = current
    api = MagicMock()
    on_card_update = MagicMock()
    handler = InteractiveFormHandler(store, api, "app", "secret", on_card_update=on_card_update)

    result = handler.handle_action(CardActionEvent(
        session_id="form_1",
        action="submit",
        question_index=0,
        operator_open_id="ou_1",
        form_value={"q_priority_choice": "P0"},
        event_id="evt_late",
        message_id="om_card",
    ))

    assert result["stale"] is True
    assert result["deferred_card_update"] is True
    assert "card" not in result
    on_card_update.assert_called_once()
    assert on_card_update.call_args.args[1]["body"]["elements"][1]["content"] == "**问题 2 / 2**"
    api.update_interactive_card.assert_not_called()


def test_previous_schedules_previous_question_card_without_callback_card():
    store = MagicMock()
    store.get_session.return_value = _session(current_index=1)
    updated = _session(current_index=0)
    store.record_event.return_value = True
    store.apply_previous.return_value = updated
    api = MagicMock()
    on_card_update = MagicMock()
    handler = InteractiveFormHandler(store, api, "app", "secret", on_card_update=on_card_update)

    result = handler.handle_action(CardActionEvent(
        session_id="form_1",
        action="previous",
        question_index=1,
        operator_open_id="ou_1",
        form_value={},
        event_id="evt_prev",
        message_id="om_card",
    ))

    assert result["completed"] is False
    assert result["deferred_card_update"] is True
    assert "card" not in result
    assert on_card_update.call_args.args[1]["body"]["elements"][1]["content"] == "**问题 1 / 2**"
    store.apply_previous.assert_called_once_with("form_1")
    api.update_interactive_card.assert_not_called()
    api.get_tenant_access_token.assert_not_called()
    store.next_card_sequence.assert_not_called()


def test_last_submit_schedules_completed_card_and_dispatches_followup():
    store = MagicMock()
    store.get_session.return_value = _session(current_index=1)
    store.record_event.return_value = True
    updated = _session(current_index=1, status="returning")
    updated["answers"] = {
        "priority": {
            "question_id": "priority",
            "type": "single",
            "values": ["P0"],
            "selected_options": ["P0"],
            "custom_value": "",
        },
        "module": {
            "question_id": "module",
            "type": "multi",
            "values": ["前端"],
            "selected_options": ["前端"],
            "custom_value": "",
        },
    }
    store.apply_submit.return_value = (updated, True)
    api = MagicMock()
    on_completed = MagicMock()
    on_card_update = MagicMock()
    handler = InteractiveFormHandler(
        store, api, "app", "secret", on_completed=on_completed, on_card_update=on_card_update,
    )

    result = handler.handle_action(CardActionEvent(
        session_id="form_1",
        action="submit",
        question_index=1,
        operator_open_id="ou_1",
        form_value={"q_module_choices": ["前端"]},
        event_id="evt_2",
        message_id="om_card",
    ))

    assert result["completed"] is True
    assert result["deferred_card_update"] is True
    assert "card" not in result
    assert on_card_update.call_args.args[1]["config"]["summary"]["content"] == "信息已提交"
    api.update_interactive_card.assert_not_called()
    api.get_tenant_access_token.assert_not_called()
    store.next_card_sequence.assert_not_called()
    on_completed.assert_called_once_with(updated)


def test_card_update_scheduler_failure_is_acknowledged_without_callback_error():
    store = MagicMock()
    store.get_session.return_value = _session(current_index=1)
    store.record_event.return_value = True
    updated = _session(current_index=0)
    store.apply_previous.return_value = updated
    api = MagicMock()
    on_card_update = MagicMock(side_effect=RuntimeError("queue full"))
    handler = InteractiveFormHandler(store, api, "app", "secret", on_card_update=on_card_update)

    result = handler.handle_action(CardActionEvent(
        session_id="form_1",
        action="previous",
        question_index=1,
        operator_open_id="ou_1",
        form_value={},
        event_id="evt_prev",
        message_id="om_card",
    ))

    assert result == {"ok": True, "completed": False, "deferred_card_update": False}
    api.update_interactive_card.assert_not_called()


def test_callback_response_wraps_card_as_raw_callback_card():
    from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse

    from src.card_action_listener import build_card_action_callback_response

    card = {"schema": "2.0", "body": {"elements": []}}

    response = build_card_action_callback_response(P2CardActionTriggerResponse, {"ok": True, "card": card})

    assert response.card.type == "raw"
    assert response.card.data == card


def test_callback_response_wraps_toast_feedback():
    from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse

    from src.card_action_listener import build_card_action_callback_response

    response = build_card_action_callback_response(
        P2CardActionTriggerResponse,
        {"ok": False, "toast": {"type": "warning", "content": "当前问题为单选，请选择一个合适的答案"}},
    )

    assert response.toast.type == "warning"
    assert response.toast.content == "当前问题为单选，请选择一个合适的答案"
    assert response.card is None


def test_callback_response_returns_none_for_lightweight_ack():
    from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse

    from src.card_action_listener import build_card_action_callback_response

    response = build_card_action_callback_response(
        P2CardActionTriggerResponse,
        {"ok": True, "deferred_card_update": True},
    )

    assert response is None


def test_register_ignored_sdk_events_does_not_subscribe_message_receive():
    calls = []

    class Builder:
        def register_p2_im_message_receive_v1(self, callback):
            calls.append(("receive", callback))
            return self

        def register_p2_im_message_message_read_v1(self, callback):
            calls.append(("read", callback))
            return self

        def register_p2_im_chat_access_event_bot_p2p_chat_entered_v1(self, callback):
            calls.append(("entered", callback))
            return self

        def register_p2_im_message_reaction_created_v1(self, callback):
            calls.append(("reaction_created", callback))
            return self

    builder = Builder()

    assert register_ignored_sdk_events(builder) is builder
    assert [name for name, _ in calls] == ["read", "entered", "reaction_created"]
    for _, callback in calls:
        assert callback(object()) is None


def test_ignored_sdk_read_event_logs_event_and_message_ids_only_at_debug(caplog):
    calls = []

    class Builder:
        def register_p2_im_message_message_read_v1(self, callback):
            calls.append(callback)
            return self

    raw = {
        "header": {"event_id": "evt_read_1", "event_type": "im.message.message_read_v1"},
        "event": {
            "message": {"message_id": "om_msg_1", "chat_id": "oc_1"},
            "sender": {"sender_id": {"open_id": "ou_1"}},
        },
    }

    with caplog.at_level(logging.INFO, logger="src.card_action_listener"):
        register_ignored_sdk_events(Builder())
        assert calls[0](raw) is None

    assert "Ignored SDK event" not in caplog.text

    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="src.card_action_listener"):
        assert calls[0](raw) is None

    text = caplog.text
    assert "Ignored SDK event" in text
    assert "im.message.message_read_v1" in text
    assert "evt_read_1" in text
    assert "om_msg_1" in text
    assert "oc_1" in text


def test_instrument_ws_client_writes_preserves_original_write():
    from lark_oapi.ws.pb.pbbp2_pb2 import Frame

    writes = []

    class Client:
        async def _write_message(self, data):
            writes.append(data)

    frame = Frame()
    frame.SeqID = 1
    frame.LogID = 1
    frame.service = 1
    frame.method = 1
    frame.payload = json.dumps({"code": 200}).encode("utf-8")
    data = frame.SerializeToString()
    client = Client()

    instrument_ws_client_writes(client)
    asyncio.run(client._write_message(data))

    assert writes == [data]


def _sdk_ws_frame(payload):
    from lark_oapi.ws.pb.pbbp2_pb2 import Frame

    frame = Frame()
    frame.SeqID = 1
    frame.LogID = 1
    frame.service = 1
    frame.method = 1
    frame.payload = json.dumps(payload).encode("utf-8") if payload is not None else b""
    return frame.SerializeToString()


def test_instrument_ws_client_writes_logs_successful_writes_only_at_debug(caplog):
    writes = []

    class Client:
        async def _write_message(self, data):
            writes.append(data)

    client = Client()
    instrument_ws_client_writes(client)

    with caplog.at_level(logging.INFO, logger="src.card_action_listener"):
        asyncio.run(client._write_message(_sdk_ws_frame({"code": 200})))

    assert writes
    assert "Lark SDK websocket write completed" not in caplog.text

    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="src.card_action_listener"):
        asyncio.run(client._write_message(_sdk_ws_frame(None)))

    assert "Lark SDK websocket write completed" in caplog.text
    assert "code=None" in caplog.text


def test_instrument_ws_client_writes_warns_on_non_success_code(caplog):
    class Client:
        async def _write_message(self, data):
            return None

    client = Client()
    instrument_ws_client_writes(client)

    with caplog.at_level(logging.WARNING, logger="src.card_action_listener"):
        asyncio.run(client._write_message(_sdk_ws_frame({"code": 500})))

    assert "Lark SDK websocket write completed with non-success code" in caplog.text
    assert "code=500" in caplog.text

