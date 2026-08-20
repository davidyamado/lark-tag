import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from src.card_forms import (
    DIAGNOSTIC_MINIMAL_ACTION,
    SINGLE_CHOICE_TOO_MANY_OPTIONS_MESSAGE,
    normalize_diagnostic_response_mode,
    normalize_answer,
    render_diagnostic_received_card,
    render_completed_card,
    render_question_card,
    selected_from_checkers,
)
from src.form_store import stable_event_id

logger = logging.getLogger(__name__)


IGNORED_EVENT_REGISTRARS = (
    "register_p2_im_message_message_read_v1",
    "register_p2_im_chat_access_event_bot_p2p_chat_entered_v1",
    "register_p2_im_message_reaction_created_v1",
)


@dataclass(frozen=True)
class CardActionEvent:
    session_id: str
    action: str
    question_index: int
    operator_open_id: str
    form_value: dict[str, Any]
    event_id: str = ""
    message_id: str = ""
    callback_token: str = ""
    option_index: int | None = None
    value_type: str = "dict"
    form_value_type: str = "dict"
    response_mode: str = ""
    nonce: str = ""


def _read_attr(obj: Any, *names: str) -> Any:
    cur = obj
    for name in names:
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(name)
        else:
            cur = getattr(cur, name, None)
    return cur


def parse_card_action_event(raw: Any) -> CardActionEvent:
    """Normalize SDK/card-action payloads into a small internal event."""
    event = _read_attr(raw, "event") or raw
    action_obj = _read_attr(event, "action") or {}
    raw_value = _read_attr(action_obj, "value") or {}
    raw_form_value = _read_attr(action_obj, "form_value") or _read_attr(action_obj, "formValue") or {}
    value_type = type(raw_value).__name__
    form_value_type = type(raw_form_value).__name__
    value = raw_value if isinstance(raw_value, dict) else {}
    form_value = raw_form_value if isinstance(raw_form_value, dict) else {}
    operator_open_id = (
        _read_attr(event, "operator", "open_id")
        or _read_attr(event, "operator", "openId")
        or _read_attr(event, "operator_open_id")
        or ""
    )
    message_id = (
        _read_attr(event, "context", "open_message_id")
        or _read_attr(event, "context", "openMessageId")
        or _read_attr(event, "message_id")
        or ""
    )
    callback_token = _read_attr(event, "token") or _read_attr(event, "callback_token") or ""
    event_id = (
        _read_attr(raw, "header", "event_id")
        or _read_attr(raw, "header", "eventId")
        or _read_attr(event, "event_id")
        or ""
    )
    return CardActionEvent(
        session_id=str(value.get("session_id") or ""),
        action=str(value.get("action") or ""),
        question_index=int(value.get("question_index") or 0),
        operator_open_id=str(operator_open_id or ""),
        form_value=dict(form_value or {}),
        event_id=str(event_id or ""),
        message_id=str(message_id or ""),
        callback_token=str(callback_token or ""),
        option_index=int(value["option_index"]) if "option_index" in value else None,
        value_type=value_type,
        form_value_type=form_value_type,
        response_mode=str(value.get("response_mode") or ""),
        nonce=str(value.get("nonce") or ""),
    )


def register_ignored_sdk_events(builder: Any) -> Any:
    """Register no-op callbacks for events handled by the existing lark-cli listener."""
    def _ignore(req, registrar_name: str = ""):
        event_type, event_id, message_id, chat_id, open_id = _summarize_ignored_sdk_event(req)
        logger.debug(
            "Ignored SDK event: registrar=%s event_type=%s event_id=%s message_id=%s chat_id=%s open_id=%s",
            registrar_name,
            event_type,
            event_id,
            message_id,
            chat_id,
            open_id,
        )
        return None

    for name in IGNORED_EVENT_REGISTRARS:
        register = getattr(builder, name, None)
        if callable(register):
            builder = register(lambda req, registrar_name=name: _ignore(req, registrar_name))
    return builder


def _summarize_ignored_sdk_event(raw: Any) -> tuple[str, str, str, str, str]:
    event = _read_attr(raw, "event") or raw
    header = _read_attr(raw, "header") or {}
    message = _read_attr(event, "message") or {}
    sender = _read_attr(event, "sender") or {}
    sender_id = _read_attr(sender, "sender_id") or _read_attr(sender, "senderId") or sender
    event_type = (
        _read_attr(header, "event_type")
        or _read_attr(header, "eventType")
        or _read_attr(raw, "type")
        or _read_attr(event, "type")
        or ""
    )
    event_id = (
        _read_attr(header, "event_id")
        or _read_attr(header, "eventId")
        or _read_attr(event, "event_id")
        or ""
    )
    message_id = (
        _read_attr(message, "message_id")
        or _read_attr(message, "messageId")
        or _read_attr(event, "message_id")
        or _read_attr(event, "messageId")
        or _read_attr(event, "context", "open_message_id")
        or ""
    )
    chat_id = _read_attr(message, "chat_id") or _read_attr(message, "chatId") or _read_attr(event, "chat_id") or ""
    open_id = (
        _read_attr(sender_id, "open_id")
        or _read_attr(sender_id, "openId")
        or _read_attr(sender, "open_id")
        or _read_attr(sender, "openId")
        or ""
    )
    return str(event_type or ""), str(event_id or ""), str(message_id or ""), str(chat_id or ""), str(open_id or "")


@dataclass(frozen=True)
class _WsWriteSummary:
    text: str
    code: Any = None


def _summarize_ws_write_payload(data: bytes) -> _WsWriteSummary:
    try:
        import json

        from lark_oapi.ws.pb.pbbp2_pb2 import Frame

        frame = Frame()
        frame.ParseFromString(data)
        payload = frame.payload.decode("utf-8", errors="replace") if frame.payload else ""
        parsed = json.loads(payload) if payload else {}
        code = parsed.get("code")
        return _WsWriteSummary(
            f"code={parsed.get('code')} data_present={bool(parsed.get('data'))} "
            f"payload_bytes={len(frame.payload or b'')}",
            code=code,
        )
    except Exception as e:
        return _WsWriteSummary(f"unparsed_payload error={type(e).__name__} bytes={len(data)}")


def _is_successful_ws_write_code(code: Any) -> bool:
    return code in (None, 0, 200, "0", "200")


def instrument_ws_client_writes(client: Any) -> None:
    original = getattr(client, "_write_message", None)
    if not callable(original) or getattr(client, "_form_write_instrumented", False):
        return

    async def _logged_write(data: bytes):
        start = time.monotonic()
        summary = _summarize_ws_write_payload(data)
        try:
            await original(data)
            elapsed_ms = (time.monotonic() - start) * 1000
            if _is_successful_ws_write_code(summary.code):
                logger.debug("Lark SDK websocket write completed: %s elapsed_ms=%.1f", summary.text, elapsed_ms)
            else:
                logger.warning(
                    "Lark SDK websocket write completed with non-success code: %s elapsed_ms=%.1f",
                    summary.text,
                    elapsed_ms,
                )
        except Exception:
            elapsed_ms = (time.monotonic() - start) * 1000
            logger.exception("Lark SDK websocket write failed: %s elapsed_ms=%.1f", summary.text, elapsed_ms)
            raise

    setattr(client, "_write_message", _logged_write)
    setattr(client, "_form_write_instrumented", True)


class InteractiveFormHandler:
    def __init__(
        self,
        store,
        feishu_api_module,
        app_id: str,
        app_secret: str,
        on_completed: Callable[[dict[str, Any]], None] | None = None,
        on_card_update: Callable[[dict[str, Any], dict[str, Any]], None] | None = None,
        defer_card_updates: bool = True,
    ):
        self.store = store
        self.feishu_api = feishu_api_module
        self.app_id = app_id
        self.app_secret = app_secret
        self.on_completed = on_completed
        self.on_card_update = on_card_update
        self.defer_card_updates = defer_card_updates

    def handle_action(self, event: CardActionEvent) -> dict[str, Any]:
        if event.action == DIAGNOSTIC_MINIMAL_ACTION:
            return self._handle_diagnostic_minimal_action(event)
        if not event.session_id or event.action not in ("submit", "previous"):
            return {"ok": True, "ignored": True}

        session = self.store.get_session(event.session_id)
        if not session:
            return {"ok": True, "ignored": True}
        if event.operator_open_id != session.get("operator_open_id"):
            logger.info("Ignoring form action from non-owner")
            return {"ok": True, "ignored": True}
        if event.question_index != int(session.get("current_index") or 0):
            logger.info(
                "Ignoring stale form action: action=%s session=%s event_question=%s current_question=%s",
                event.action,
                event.session_id,
                event.question_index,
                session.get("current_index"),
            )
            return self._card_update_result(
                {"ok": True, "stale": True},
                session,
                self._render_question_card(session),
                event.callback_token,
            )

        schema = session["schema"]
        question = schema["questions"][event.question_index]
        if event.action == "submit" and self._single_question_has_multiple_checked_options(question, event.form_value):
            return {
                "ok": False,
                "toast": {"type": "warning", "content": SINGLE_CHOICE_TOO_MANY_OPTIONS_MESSAGE},
            }

        event_id = event.event_id or stable_event_id({
            "session_id": event.session_id,
            "message_id": event.message_id,
            "operator_open_id": event.operator_open_id,
            "action": event.action,
            "question_index": event.question_index,
            "form_value": event.form_value,
        })
        if not self.store.record_event(event_id, event.session_id, event.action):
            return self._card_update_result(
                {"ok": True, "duplicate": True},
                session,
                self._render_session_card(session),
                event.callback_token,
            )

        if event.action == "previous":
            updated = self.store.apply_previous(event.session_id)
            card = self._render_question_card(updated)
            return self._card_update_result(
                {"ok": True, "completed": False},
                updated,
                card,
                event.callback_token,
            )

        answer = normalize_answer(question, event.form_value)
        updated, completed = self.store.apply_submit(event.session_id, event.question_index, answer)
        if completed:
            card = render_completed_card()
            if self.on_completed:
                self.on_completed(updated)
            return self._card_update_result(
                {"ok": True, "completed": True},
                updated,
                card,
                event.callback_token,
            )

        card = self._render_question_card(updated)
        return self._card_update_result(
            {"ok": True, "completed": False},
            updated,
            card,
            event.callback_token,
        )

    def _render_session_card(self, session: dict[str, Any]) -> dict[str, Any]:
        if session.get("status") in ("returning", "completed"):
            return render_completed_card()
        return self._render_question_card(session)

    def _render_question_card(self, session: dict[str, Any]) -> dict[str, Any]:
        return render_question_card(
            session["schema"],
            session_id=session["id"],
            current_index=session["current_index"],
            answers=session.get("answers") or {},
        )

    def _single_question_has_multiple_checked_options(
        self,
        question: dict[str, Any],
        form_value: dict[str, Any],
    ) -> bool:
        if question.get("type") != "single":
            return False
        selected_options = selected_from_checkers(question, form_value)
        return bool(selected_options and len(selected_options) > 1)

    def _schedule_card_update(
        self,
        session: dict[str, Any],
        card: dict[str, Any],
        callback_token: str = "",
    ) -> bool:
        if self.on_card_update is None:
            return False
        try:
            self.on_card_update(session, card, callback_token)
            return True
        except Exception:
            logger.exception("Could not queue form card update: session=%s", session.get("id"))
            return False

    def _card_update_result(
        self,
        result: dict[str, Any],
        session: dict[str, Any],
        card: dict[str, Any],
        callback_token: str = "",
    ) -> dict[str, Any]:
        if not self.defer_card_updates:
            return {**result, "card": card}
        card_update_queued = self._schedule_card_update(session, card, callback_token)
        return {**result, "deferred_card_update": card_update_queued}

    def _handle_diagnostic_minimal_action(self, event: CardActionEvent) -> dict[str, Any]:
        response_mode = normalize_diagnostic_response_mode(event.response_mode or "ack")
        result: dict[str, Any] = {
            "ok": True,
            "diagnostic": True,
            "response_mode": response_mode,
            "nonce": event.nonce,
        }
        logger.info(
            "Diagnostic card action received: mode=%s nonce=%s message=%s operator=%s value_type=%s form_value_type=%s",
            response_mode,
            event.nonce,
            event.message_id,
            event.operator_open_id,
            event.value_type,
            event.form_value_type,
        )
        if response_mode == "toast":
            return {
                **result,
                "toast": {
                    "type": "success",
                    "content": f"诊断回调已收到: {event.nonce or response_mode}",
                },
            }
        if response_mode == "sync_card":
            return {
                **result,
                "card": render_diagnostic_received_card(response_mode=response_mode, nonce=event.nonce),
            }
        return result


def build_card_action_callback_response(response_cls: Any, result: dict[str, Any]) -> Any:
    """Build the SDK response for a card action callback."""
    toast = result.get("toast") if isinstance(result, dict) else None
    card = result.get("card") if isinstance(result, dict) else None
    if toast and card:
        return response_cls({"toast": toast, "card": {"type": "raw", "data": card}})
    if toast:
        return response_cls({"toast": toast})
    if not card:
        return None
    return response_cls({"card": {"type": "raw", "data": card}})


class CardActionListener:
    """Thin lifecycle wrapper around the Lark SDK long connection client."""

    def __init__(self, app_id: str, app_secret: str, handler: InteractiveFormHandler):
        self.app_id = app_id
        self.app_secret = app_secret
        self.handler = handler
        self._client = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if os.environ.get("BOT_CARD_ACTION_LISTENER", "1").strip().lower() in ("0", "false", "off", "no"):
            logger.info("Card action listener disabled by BOT_CARD_ACTION_LISTENER")
            return
        try:
            import lark_oapi as lark
            from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse
        except Exception as e:
            logger.warning(f"lark-oapi unavailable; card action listener not started: {e}")
            return

        def _card_action_callback(req):
            try:
                event = parse_card_action_event(req)
                logger.info(
                    "Card action received: action=%s session=%s question_index=%s message=%s value_type=%s form_value_type=%s form_keys=%s",
                    event.action,
                    event.session_id,
                    event.question_index,
                    event.message_id,
                    event.value_type,
                    event.form_value_type,
                    ",".join(sorted(event.form_value.keys()))[:300],
                )
                result = self.handler.handle_action(event)
                if isinstance(result, dict) and result.get("card"):
                    elements = result["card"].get("body", {}).get("elements") or []
                    question_label = elements[1].get("content", "") if len(elements) > 1 else ""
                    logger.info(
                        "Returning form card in callback response: session=%s action=%s question_index=%s",
                        event.session_id,
                        event.action,
                        question_label,
                    )
                elif isinstance(result, dict) and "deferred_card_update" in result:
                    logger.info(
                        "Acknowledging form action with deferred card update: session=%s action=%s queued=%s",
                        event.session_id,
                        event.action,
                        result.get("deferred_card_update"),
                    )
                response = build_card_action_callback_response(P2CardActionTriggerResponse, result)
                logger.info(
                    "Card action response built: action=%s session=%s response_type=%s has_toast=%s has_card=%s",
                    event.action,
                    event.session_id,
                    type(response).__name__,
                    bool(getattr(response, "toast", None)),
                    bool(getattr(response, "card", None)),
                )
                return response
            except Exception as e:
                logger.exception(f"card action callback failed: {e}")
                raise

        builder = lark.EventDispatcherHandler.builder("", "")
        builder = register_ignored_sdk_events(builder)
        dispatcher = builder.register_p2_card_action_trigger(_card_action_callback).build()
        self._client = lark.ws.Client(
            self.app_id,
            self.app_secret,
            event_handler=dispatcher,
            log_level=lark.LogLevel.INFO,
        )
        instrument_ws_client_writes(self._client)
        self._thread = threading.Thread(target=self._client.start, daemon=True, name="card-action-listener")
        self._thread.start()
        logger.info("Card action listener started")

    def stop(self) -> None:
        client = self._client
        if client and hasattr(client, "stop"):
            try:
                client.stop()
            except Exception as e:
                logger.warning(f"Card action listener stop failed: {e}")


def start_card_action_listener(app_id: str, app_secret: str, handler: InteractiveFormHandler) -> CardActionListener:
    listener = CardActionListener(app_id, app_secret, handler)
    listener.start()
    return listener
