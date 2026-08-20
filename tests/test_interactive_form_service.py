from unittest.mock import MagicMock

from src.interactive_form_service import InteractiveFormService


def _schema():
    return {
        "title": "补充信息",
        "questions": [
            {
                "id": "priority",
                "title": "优先级？",
                "type": "single",
                "options": [{"label": "P0"}],
            }
        ],
    }


def test_create_form_creates_session_and_sends_first_card_to_p2p():
    store = MagicMock()
    store.create_session.return_value = {
        "id": "form_1",
        "schema": _schema(),
        "current_index": 0,
        "answers": {},
    }
    store.set_card_id.return_value = {}
    api = MagicMock()
    api.get_tenant_access_token.return_value = "token"
    api.send_interactive_card.return_value = "om_card"
    service = InteractiveFormService(store, api, "app", "secret")

    result = service.create_form(
        context_id="ou_1",
        operator_open_id="ou_1",
        chat_id="oc_1",
        chat_type="p2p",
        reply_msg_id="",
        root_id="",
        thread_session_key="",
        message_id="om_user",
        original_text="创建需求",
        schema=_schema(),
    )

    assert result == {"session_id": "form_1", "message_id": "om_card"}
    api.send_interactive_card.assert_called_once()
    sent_card = api.send_interactive_card.call_args.args[1]
    form = next(element for element in sent_card["body"]["elements"] if element.get("tag") == "form")
    assert form["name"] == "interactive_question_form"
    store.set_card_id.assert_called_once_with("form_1", "om_card")


def test_create_form_replies_in_thread_when_reply_message_is_present():
    store = MagicMock()
    store.create_session.return_value = {
        "id": "form_1",
        "schema": _schema(),
        "current_index": 0,
        "answers": {},
    }
    api = MagicMock()
    api.get_tenant_access_token.return_value = "token"
    api.reply_interactive_card_in_thread.return_value = "om_reply_card"
    service = InteractiveFormService(store, api, "app", "secret")

    service.create_form(
        context_id="g_oc_1_ou_1",
        operator_open_id="ou_1",
        chat_id="oc_1",
        chat_type="group",
        reply_msg_id="om_parent",
        root_id="om_root",
        thread_session_key="thread",
        message_id="om_user",
        original_text="创建需求",
        schema=_schema(),
    )

    api.reply_interactive_card_in_thread.assert_called_once()
    api.send_interactive_card.assert_not_called()


def test_send_diagnostic_minimal_card_sends_p2p_card_without_session():
    store = MagicMock()
    api = MagicMock()
    api.get_tenant_access_token.return_value = "token"
    api.send_interactive_card.return_value = "om_diag"
    service = InteractiveFormService(store, api, "app", "secret")

    result = service.send_diagnostic_minimal_card(
        operator_open_id="ou_1",
        chat_id="",
        chat_type="p2p",
        reply_msg_id="",
        response_mode="toast",
        nonce="diag_1",
    )

    assert result == {"message_id": "om_diag", "response_mode": "toast", "nonce": "diag_1"}
    store.create_session.assert_not_called()
    api.send_interactive_card.assert_called_once()
    sent_card = api.send_interactive_card.call_args.args[1]
    assert "diagnostic_minimal" in str(sent_card)


def test_send_diagnostic_minimal_card_replies_in_thread_when_reply_message_is_present():
    api = MagicMock()
    api.get_tenant_access_token.return_value = "token"
    api.reply_interactive_card_in_thread.return_value = "om_diag_reply"
    service = InteractiveFormService(MagicMock(), api, "app", "secret")

    service.send_diagnostic_minimal_card(
        operator_open_id="ou_1",
        chat_id="oc_1",
        chat_type="group",
        reply_msg_id="om_parent",
        response_mode="ack",
        nonce="diag_2",
    )

    api.reply_interactive_card_in_thread.assert_called_once()
    api.send_interactive_card.assert_not_called()
