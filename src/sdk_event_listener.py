import json
import logging
import os
import threading
from typing import Any, Callable, Optional

from src.card_action_listener import (
    build_card_action_callback_response,
    instrument_ws_client_writes,
    parse_card_action_event,
)
from src.event_listener import parse_bot_added_data, parse_event_data

logger = logging.getLogger(__name__)


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


def _read_bool(value: Any) -> bool:
    if value is True:
        return True
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return False


def _log_message_ingress(raw: Any) -> None:
    event = _read_attr(raw, "event") or raw
    header = _read_attr(raw, "header") or {}
    message = _read_attr(event, "message") or {}
    event_type = str(_read_attr(header, "event_type") or _read_attr(header, "eventType") or "")
    if event_type != "im.message.receive_v1":
        return
    chat_type = str(_read_attr(message, "chat_type") or _read_attr(message, "chatType") or "")
    if chat_type != "group":
        return
    chat_id = str(_read_attr(message, "chat_id") or _read_attr(message, "chatId") or "")
    mentions = _read_attr(message, "mentions")
    mention_count = len(mentions) if isinstance(mentions, list) else -1
    content = _read_attr(message, "content") or ""
    logger.info(
        "SDK message ingress: chat_id=%s message_id=%s chat_type=%s "
        "message_type=%s mentions=%d content_len=%d",
        chat_id,
        _read_attr(message, "message_id") or _read_attr(message, "messageId") or "",
        chat_type,
        _read_attr(message, "message_type") or _read_attr(message, "messageType") or "",
        mention_count,
        len(str(content)),
    )


def _normalize_mentions(mentions: Any) -> list[dict[str, Any]]:
    if not isinstance(mentions, list):
        return []
    normalized = []
    for mention in mentions:
        open_id = (
            _read_attr(mention, "id", "open_id")
            or _read_attr(mention, "id", "openId")
            or _read_attr(mention, "open_id")
            or _read_attr(mention, "openId")
            or (mention if isinstance(mention, str) else "")
        )
        normalized.append({
            "id": str(open_id or ""),
            "key": str(_read_attr(mention, "key") or ""),
            "name": str(_read_attr(mention, "name") or ""),
            "is_bot": _read_bool(_read_attr(mention, "is_bot") or _read_attr(mention, "isBot")),
            "user_id": str(
                _read_attr(mention, "id", "user_id")
                or _read_attr(mention, "id", "userId")
                or _read_attr(mention, "user_id")
                or _read_attr(mention, "userId")
                or ""
            ),
            "union_id": str(
                _read_attr(mention, "id", "union_id")
                or _read_attr(mention, "id", "unionId")
                or _read_attr(mention, "union_id")
                or _read_attr(mention, "unionId")
                or ""
            ),
        })
    return normalized


def _sdk_message_to_compact(raw: Any) -> dict[str, Any]:
    event = _read_attr(raw, "event") or raw
    header = _read_attr(raw, "header") or {}
    message = _read_attr(event, "message") or {}
    sender_id = _read_attr(event, "sender", "sender_id") or _read_attr(event, "sender", "senderId") or {}

    message_type = str(_read_attr(message, "message_type") or _read_attr(message, "messageType") or "")
    raw_content = _read_attr(message, "content")
    if message_type == "text":
        try:
            parsed = json.loads(raw_content) if isinstance(raw_content, str) else (raw_content or {})
            content = parsed.get("text", "") if isinstance(parsed, dict) else (raw_content or "")
        except (json.JSONDecodeError, TypeError):
            content = raw_content or ""
    elif isinstance(raw_content, dict):
        content = json.dumps(raw_content)
    else:
        content = raw_content if raw_content is not None else ""

    return {
        "type": str(_read_attr(header, "event_type") or _read_attr(header, "eventType") or ""),
        "message_id": str(_read_attr(message, "message_id") or _read_attr(message, "messageId") or ""),
        "chat_id": str(_read_attr(message, "chat_id") or _read_attr(message, "chatId") or ""),
        "chat_type": str(_read_attr(message, "chat_type") or _read_attr(message, "chatType") or "p2p"),
        "message_type": message_type,
        "content": content,
        "sender_id": str(_read_attr(sender_id, "open_id") or _read_attr(sender_id, "openId") or ""),
        "create_time": str(_read_attr(message, "create_time") or _read_attr(message, "createTime") or ""),
        "mentions": _normalize_mentions(_read_attr(message, "mentions")),
        "root_id": str(_read_attr(message, "root_id") or _read_attr(message, "rootId") or ""),
        "parent_id": str(_read_attr(message, "parent_id") or _read_attr(message, "parentId") or ""),
    }


def parse_sdk_message_event(raw: Any, bot_open_id: str = "") -> Optional[dict[str, Any]]:
    return parse_event_data(_sdk_message_to_compact(raw), bot_open_id)


def _sdk_bot_added_to_compact(raw: Any) -> dict[str, Any]:
    event = _read_attr(raw, "event") or raw
    header = _read_attr(raw, "header") or {}
    return {
        "type": str(_read_attr(header, "event_type") or _read_attr(header, "eventType") or ""),
        "chat_id": str(_read_attr(event, "chat_id") or _read_attr(event, "chatId") or ""),
        "operator_id": _read_attr(event, "operator_id") or _read_attr(event, "operatorId") or "",
        "name": str(_read_attr(event, "name") or _read_attr(event, "chat_name") or _read_attr(event, "chatName") or ""),
    }


def parse_sdk_bot_added_event(raw: Any) -> Optional[dict[str, Any]]:
    return parse_bot_added_data(_sdk_bot_added_to_compact(raw))


def sdk_noop_ack(_raw: Any) -> None:
    return None


class SdkEventListener:
    def __init__(
        self,
        app_id: str,
        app_secret: str,
        on_message: Callable[[dict], None],
        on_poll: Optional[Callable[[], None]] = None,
        bot_open_id: str = "",
        on_bot_added: Optional[Callable[[dict], None]] = None,
        card_action_handler: Any = None,
    ):
        self.app_id = app_id
        self.app_secret = app_secret
        self.on_message = on_message
        self.on_poll = on_poll
        self.bot_open_id = bot_open_id
        self.on_bot_added = on_bot_added
        self.card_action_handler = card_action_handler
        self._client = None
        self._thread: threading.Thread | None = None
        self._poll_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        if os.environ.get("BOT_SDK_EVENT_LISTENER", "1").strip().lower() in ("0", "false", "off", "no"):
            logger.info("SDK event listener disabled by BOT_SDK_EVENT_LISTENER")
            return
        try:
            import lark_oapi as lark
            from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse
        except Exception as e:
            logger.warning("lark-oapi unavailable; SDK event listener not started: %s", e)
            return

        builder = lark.EventDispatcherHandler.builder("", "")
        builder = self._register_if(builder, "register_p2_im_message_receive_v1", self._message_callback)
        builder = self._register_if(builder, "register_p2_im_chat_member_bot_added_v1", self._bot_added_callback)
        builder = self._register_if(
            builder,
            "register_p2_card_action_trigger",
            self._card_action_callback_factory(P2CardActionTriggerResponse),
        )
        builder = self._register_if(builder, "register_p2_im_message_message_read_v1", sdk_noop_ack)
        builder = self._register_if(
            builder,
            "register_p2_im_chat_access_event_bot_p2p_chat_entered_v1",
            sdk_noop_ack,
        )
        builder = self._register_if(builder, "register_p2_im_message_reaction_created_v1", sdk_noop_ack)
        dispatcher = builder.build()
        self._client = lark.ws.Client(
            self.app_id,
            self.app_secret,
            event_handler=dispatcher,
            log_level=lark.LogLevel.INFO,
        )
        instrument_ws_client_writes(self._client)
        self._thread = threading.Thread(target=self._client.start, daemon=True, name="sdk-event-listener")
        self._thread.start()
        if self.on_poll:
            self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True, name="sdk-poll-recovery")
            self._poll_thread.start()
        logger.info("SDK event listener started")

    def stop(self) -> None:
        self._stop_event.set()
        client = self._client
        if client and hasattr(client, "stop"):
            try:
                client.stop()
            except Exception as e:
                logger.warning("SDK event listener stop failed: %s", e)

    def _poll_loop(self) -> None:
        self._stop_event.wait(timeout=30)
        while not self._stop_event.is_set():
            try:
                if self.on_poll:
                    self.on_poll()
            except Exception as e:
                logger.error("SDK poll callback error: %s", e)
            self._stop_event.wait(timeout=30)

    def _message_callback(self, req: Any) -> None:
        try:
            _log_message_ingress(req)
            event = parse_sdk_message_event(req, bot_open_id=self.bot_open_id)
            if event:
                self.on_message(event)
        except Exception:
            logger.exception("SDK message callback failed")
        return None

    def _bot_added_callback(self, req: Any) -> None:
        try:
            event = parse_sdk_bot_added_event(req)
            if event and self.on_bot_added:
                self.on_bot_added(event)
        except Exception:
            logger.exception("SDK bot-added callback failed")
        return None

    def _card_action_callback_factory(self, response_cls: Any):
        def _card_action_callback(req: Any):
            try:
                if self.card_action_handler is None:
                    return None
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
                result = self.card_action_handler.handle_action(event)
                return build_card_action_callback_response(response_cls, result)
            except Exception:
                logger.exception("SDK card action callback failed")
                raise

        return _card_action_callback

    @staticmethod
    def _register_if(builder: Any, registrar_name: str, callback: Callable):
        registrar = getattr(builder, registrar_name, None)
        if callable(registrar):
            return registrar(callback)
        logger.warning("SDK event registrar unavailable: %s", registrar_name)
        return builder
