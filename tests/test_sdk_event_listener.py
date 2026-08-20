import json
import logging
from unittest.mock import MagicMock, patch

from src.sdk_event_listener import (
    SdkEventListener,
    parse_sdk_bot_added_event,
    parse_sdk_message_event,
    sdk_noop_ack,
)
from src.event_listener import parse_event_line


def _sdk_text(text, *, chat_type="p2p", mentions=None):
    msg = {
        "message_id": "om_1",
        "chat_id": "oc_1",
        "chat_type": chat_type,
        "create_time": "1781157390915",
        "message_type": "text",
        "content": json.dumps({"text": text}),
    }
    if mentions is not None:
        msg["mentions"] = mentions
    return {
        "header": {"event_id": "evt_1", "event_type": "im.message.receive_v1"},
        "event": {"sender": {"sender_id": {"open_id": "ou_1"}}, "message": msg},
    }


def _sdk_msg(message_type, content_obj):
    return {
        "header": {"event_id": "evt_x", "event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_1"}},
            "message": {
                "message_id": "om_x",
                "chat_id": "oc_1",
                "chat_type": "p2p",
                "create_time": "1781157390915",
                "message_type": message_type,
                "content": json.dumps(content_obj),
            },
        },
    }


def test_sdk_text_message_has_full_contract():
    event = parse_sdk_message_event(_sdk_text("hello"), bot_open_id="")

    assert event["open_id"] == "ou_1"
    assert event["text"] == "hello"
    assert event["message_id"] == "om_1"
    assert event["chat_type"] == "p2p"
    assert event["is_group"] is False
    assert event["image_key"] == ""
    assert event["file_key"] == ""
    assert event["file_name"] == ""


def test_sdk_image_message_extracts_image_key():
    event = parse_sdk_message_event(_sdk_msg("image", {"image_key": "img_abc"}), bot_open_id="")
    compact = {
        "type": "im.message.receive_v1",
        "message_id": "om_x",
        "chat_id": "oc_1",
        "chat_type": "p2p",
        "create_time": "1781157390915",
        "message_type": "image",
        "sender_id": "ou_1",
        "content": json.dumps({"image_key": "img_abc"}),
    }
    expected = parse_event_line(json.dumps(compact), bot_open_id="")

    assert event["image_key"] == "img_abc"
    assert event["text"] == expected["text"]


def test_sdk_file_message_extracts_file_key_and_name():
    event = parse_sdk_message_event(
        _sdk_msg("file", {"file_key": "file_x", "file_name": "report.pdf"}), bot_open_id=""
    )

    assert event["file_key"] == "file_x"
    assert event["file_name"] == "report.pdf"


def test_sdk_post_message_extracts_text():
    content = {"zh_cn": {"title": "t", "content": [[{"tag": "text", "text": "body text"}]]}}
    event = parse_sdk_message_event(_sdk_msg("post", content), bot_open_id="")

    assert "body text" in event["text"]


def test_sdk_group_message_without_at_is_forwarded_with_mentioned_false():
    event = parse_sdk_message_event(
        _sdk_text("group continuation", chat_type="group", mentions=[]), bot_open_id="ou_bot"
    )

    assert event is not None
    assert event["is_group"] is True
    assert event["mentioned"] is False


def test_sdk_group_message_with_bot_mention_sets_mentioned():
    raw = _sdk_text(
        "@Bot help",
        chat_type="group",
        mentions=[{"key": "@_user_1", "id": {"open_id": "ou_bot"}}],
    )

    event = parse_sdk_message_event(raw, bot_open_id="ou_bot")

    assert event["mentioned"] is True
    assert event["is_group"] is True
    assert event["text"] == "help"


def test_sdk_group_message_with_bot_marker_without_matching_open_id_is_not_mentioned():
    raw = _sdk_text(
        "help",
        chat_type="group",
        mentions=[{"key": "@_user_1", "is_bot": True}],
    )

    event = parse_sdk_message_event(raw, bot_open_id="ou_bot")

    assert event["mentioned"] is False


def test_sdk_group_message_with_string_false_bot_marker_is_not_mentioned():
    raw = _sdk_text(
        "help",
        chat_type="group",
        mentions=[{"key": "@_user_1", "is_bot": "false"}],
    )

    event = parse_sdk_message_event(raw, bot_open_id="ou_bot")

    assert event["mentioned"] is False


def test_sdk_message_callback_logs_group_ingress(caplog):
    listener = SdkEventListener(
        app_id="app",
        app_secret="secret",
        on_message=MagicMock(),
        on_poll=None,
        bot_open_id="ou_bot",
    )

    caplog.set_level(logging.INFO, logger="src.sdk_event_listener")
    listener._message_callback(_sdk_text("hello", chat_type="group", mentions=[]))

    assert "SDK message ingress" in caplog.text
    assert "chat_id=oc_1" in caplog.text
    assert "message_id=om_1" in caplog.text
    assert "chat_type=group" in caplog.text


def test_sdk_message_callback_logs_every_group_ingress(caplog):
    listener = SdkEventListener(
        app_id="app",
        app_secret="secret",
        on_message=MagicMock(),
        on_poll=None,
        bot_open_id="ou_bot",
    )
    first = _sdk_text("one", chat_type="group", mentions=[])
    second = _sdk_text("two", chat_type="group", mentions=[])
    second["event"]["message"]["message_id"] = "om_2"

    caplog.set_level(logging.INFO, logger="src.sdk_event_listener")
    listener._message_callback(first)
    listener._message_callback(second)

    assert "message_id=om_1" in caplog.text
    assert "message_id=om_2" in caplog.text


def test_sdk_parent_id_preserved_for_quote_reply():
    raw = _sdk_text("quote reply")
    raw["event"]["message"]["parent_id"] = "om_parent"

    event = parse_sdk_message_event(raw, bot_open_id="")

    assert event["parent_id"] == "om_parent"


def test_parse_sdk_bot_added_event_matches_handle_bot_added_contract():
    raw = {
        "header": {"event_id": "evt_add", "event_type": "im.chat.member.bot.added_v1"},
        "event": {
            "chat_id": "oc_1",
            "operator_id": {"open_id": "ou_inviter"},
            "name": "Project group",
            "external": False,
        },
    }

    event = parse_sdk_bot_added_event(raw)

    assert event == {
        "event_type": "bot_added",
        "operator_id": "ou_inviter",
        "chat_id": "oc_1",
        "chat_name": "Project group",
    }


def test_parse_sdk_bot_added_event_returns_none_when_chat_or_operator_missing():
    raw = {
        "header": {"event_id": "evt_add", "event_type": "im.chat.member.bot.added_v1"},
        "event": {"chat_id": "", "operator_id": {"open_id": ""}},
    }

    assert parse_sdk_bot_added_event(raw) is None


def test_sdk_noop_ack_returns_none_for_successful_empty_sdk_ack():
    assert sdk_noop_ack({}) is None


def test_sdk_event_listener_registers_required_callbacks_and_starts_threads():
    with patch("src.sdk_event_listener.threading.Thread") as thread_cls:
        listener = SdkEventListener(
            app_id="app",
            app_secret="secret",
            on_message=MagicMock(),
            on_poll=MagicMock(),
            bot_open_id="ou_bot",
            on_bot_added=MagicMock(),
            card_action_handler=MagicMock(),
        )
        with patch.dict("sys.modules", _fake_lark_modules()):
            listener.start()

    registered = FakeEventDispatcherHandler.last_builder.registered
    assert set(registered) == {"message", "bot_added", "card", "read", "p2p_entered", "reaction_created"}
    thread_names = [call.kwargs.get("name") for call in thread_cls.call_args_list]
    assert "sdk-event-listener" in thread_names
    assert "sdk-poll-recovery" in thread_names


def test_sdk_event_listener_dispatches_message_and_card_action():
    on_message = MagicMock()
    on_bot_added = MagicMock()
    card_handler = MagicMock()
    card_handler.handle_action.return_value = {"ok": True}

    listener = SdkEventListener(
        app_id="app",
        app_secret="secret",
        on_message=on_message,
        on_poll=None,
        bot_open_id="ou_bot",
        on_bot_added=on_bot_added,
        card_action_handler=card_handler,
    )
    with patch.dict("sys.modules", _fake_lark_modules()):
        listener.start()

    registered = FakeEventDispatcherHandler.last_builder.registered
    registered["message"](_sdk_text("hello"))
    registered["bot_added"]({
        "header": {"event_type": "im.chat.member.bot.added_v1"},
        "event": {"chat_id": "oc_1", "operator_id": {"open_id": "ou_inviter"}},
    })
    registered["card"]({
        "header": {"event_id": "evt_card"},
        "event": {
            "operator": {"open_id": "ou_1"},
            "context": {"open_message_id": "om_card"},
            "action": {"value": {"action": "diagnostic_minimal"}},
        },
    })

    on_message.assert_called_once()
    on_bot_added.assert_called_once()
    card_handler.handle_action.assert_called_once()


class FakeResponse:
    def __init__(self, payload=None):
        self.payload = payload or {}


class FakeDispatcherBuilder:
    def __init__(self):
        self.registered = {}

    def register_p2_im_message_receive_v1(self, callback):
        self.registered["message"] = callback
        return self

    def register_p2_im_chat_member_bot_added_v1(self, callback):
        self.registered["bot_added"] = callback
        return self

    def register_p2_card_action_trigger(self, callback):
        self.registered["card"] = callback
        return self

    def register_p2_im_message_message_read_v1(self, callback):
        self.registered["read"] = callback
        return self

    def register_p2_im_chat_access_event_bot_p2p_chat_entered_v1(self, callback):
        self.registered["p2p_entered"] = callback
        return self

    def register_p2_im_message_reaction_created_v1(self, callback):
        self.registered["reaction_created"] = callback
        return self

    def build(self):
        return self


class FakeEventDispatcherHandler:
    last_builder = None

    @classmethod
    def builder(cls, *_args):
        cls.last_builder = FakeDispatcherBuilder()
        return cls.last_builder


class FakeWsClient:
    last_client = None

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        FakeWsClient.last_client = self

    def start(self):
        return None

    def stop(self):
        return None


class FakeLark:
    EventDispatcherHandler = FakeEventDispatcherHandler
    LogLevel = type("LogLevel", (), {"INFO": "INFO"})
    ws = type("ws", (), {"Client": FakeWsClient})


def _fake_lark_modules():
    import types

    response_module = types.ModuleType("lark_oapi.event.callback.model.p2_card_action_trigger")
    response_module.P2CardActionTriggerResponse = FakeResponse
    return {
        "lark_oapi": FakeLark,
        "lark_oapi.event": types.ModuleType("lark_oapi.event"),
        "lark_oapi.event.callback": types.ModuleType("lark_oapi.event.callback"),
        "lark_oapi.event.callback.model": types.ModuleType("lark_oapi.event.callback.model"),
        "lark_oapi.event.callback.model.p2_card_action_trigger": response_module,
    }
