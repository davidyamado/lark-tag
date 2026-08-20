from typing import Any

from src.card_forms import (
    normalize_diagnostic_response_mode,
    render_diagnostic_minimal_card,
    render_question_card,
    validate_form_schema,
)


class InteractiveFormService:
    def __init__(self, store, feishu_api_module, app_id: str, app_secret: str):
        self.store = store
        self.feishu_api = feishu_api_module
        self.app_id = app_id
        self.app_secret = app_secret

    def create_form(
        self,
        *,
        context_id: str,
        operator_open_id: str,
        chat_id: str,
        chat_type: str,
        reply_msg_id: str,
        root_id: str,
        thread_session_key: str,
        message_id: str,
        original_text: str,
        schema: dict[str, Any],
    ) -> dict[str, str]:
        schema = validate_form_schema(schema)
        session = self.store.create_session(
            context_id=context_id,
            operator_open_id=operator_open_id,
            chat_id=chat_id,
            chat_type=chat_type,
            reply_msg_id=reply_msg_id,
            root_id=root_id,
            thread_session_key=thread_session_key,
            message_id=message_id,
            card_id="",
            original_text=original_text,
            schema=schema,
        )
        card = render_question_card(
            schema,
            session_id=session["id"],
            current_index=session["current_index"],
            answers=session.get("answers") or {},
        )
        token = self.feishu_api.get_tenant_access_token(self.app_id, self.app_secret)
        if reply_msg_id:
            message_id = self.feishu_api.reply_interactive_card_in_thread(reply_msg_id, card, token)
        elif chat_type == "group" and chat_id:
            message_id = self.feishu_api.send_interactive_card_to_chat(chat_id, card, token)
        else:
            message_id = self.feishu_api.send_interactive_card(operator_open_id, card, token)
        self.store.set_card_id(session["id"], message_id)
        return {"session_id": session["id"], "message_id": message_id}

    def send_diagnostic_minimal_card(
        self,
        *,
        operator_open_id: str,
        chat_id: str,
        chat_type: str,
        reply_msg_id: str,
        response_mode: str = "ack",
        nonce: str = "",
    ) -> dict[str, str]:
        response_mode = normalize_diagnostic_response_mode(response_mode)
        nonce = str(nonce or "")
        card = render_diagnostic_minimal_card(response_mode=response_mode, nonce=nonce)
        token = self.feishu_api.get_tenant_access_token(self.app_id, self.app_secret)
        if reply_msg_id:
            message_id = self.feishu_api.reply_interactive_card_in_thread(reply_msg_id, card, token)
        elif chat_type == "group" and chat_id:
            message_id = self.feishu_api.send_interactive_card_to_chat(chat_id, card, token)
        else:
            message_id = self.feishu_api.send_interactive_card(operator_open_id, card, token)
        return {"message_id": message_id, "response_mode": response_mode, "nonce": nonce}
