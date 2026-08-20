import json
import logging
import pytest
from unittest.mock import patch, MagicMock
from src.event_listener import (parse_event_line, _is_bot_mentioned, _strip_at_mention,
                                EventListener, _parse_image_content, _parse_post_content)

# Compact NDJSON format produced by lark-cli event consume
# {"type":"im.message.receive_v1","message_id":"om_xxx","chat_id":"oc_xxx",
#  "chat_type":"p2p","message_type":"text","content":"Hello",
#  "sender_id":"ou_xxx","create_time":"...","timestamp":"..."}

def _compact(overrides: dict = {}) -> str:
    base = {
        "type": "im.message.receive_v1",
        "message_id": "om_abc",
        "id": "om_abc",
        "chat_id": "oc_123",
        "chat_type": "p2p",
        "message_type": "text",
        "content": "你好",
        "sender_id": "ou_xyz",
        "create_time": "1773491924409",
        "timestamp": "1773491924409",
    }
    return json.dumps({**base, **overrides})


def test_parse_valid_message_event():
    result = parse_event_line(_compact())
    assert result is not None
    assert result["open_id"] == "ou_xyz"
    assert result["text"] == "你好"
    assert result["message_id"] == "om_abc"
    assert result["chat_id"] == "oc_123"


def test_parse_non_message_event_returns_none():
    line = json.dumps({"type": "im.chat.member.user.added_v1", "chat_id": "oc_123"})
    assert parse_event_line(line) is None


def test_parse_invalid_json_returns_none():
    assert parse_event_line("not json") is None
    assert parse_event_line("") is None


def test_parse_image_message_without_key_returns_none():
    """image 消息但 content 中没有 image_key 时应返回 None"""
    assert parse_event_line(_compact({"message_type": "image", "content": "{}"})) is None


def test_parse_image_message_with_key():
    """image 消息有 image_key 时应正常解析"""
    img_content = json.dumps({"image_key": "img_abc123"})
    result = parse_event_line(_compact({"message_type": "image", "content": img_content}))
    assert result is not None
    assert result["image_key"] == "img_abc123"
    assert result["text"] == "[图片]"


def test_parse_image_message_key_as_plain_string():
    """image_key 直接作为 content 字符串时也应能解析"""
    result = parse_event_line(_compact({"message_type": "image", "content": "img_xyz456"}))
    assert result is not None
    assert result["image_key"] == "img_xyz456"


def test_parse_post_message_text_only():
    """post 消息纯文字部分应被提取"""
    post = json.dumps({
        "zh_cn": {"title": "", "content": [[{"tag": "text", "text": "你好世界"}]]}
    })
    result = parse_event_line(_compact({"message_type": "post", "content": post}))
    assert result is not None
    assert result["text"] == "你好世界"
    assert result["image_key"] == ""


def test_parse_post_message_image_only():
    """post 消息只有图片时应提取 image_key，text 为 [图片]"""
    post = json.dumps({
        "zh_cn": {"title": "", "content": [[{"tag": "img", "image_key": "img_post_1"}]]}
    })
    result = parse_event_line(_compact({"message_type": "post", "content": post}))
    assert result is not None
    assert result["image_key"] == "img_post_1"
    assert result["text"] == "[图片]"


def test_parse_post_message_text_and_image():
    """post 消息图文混合时，文字和 image_key 都应被提取"""
    post = json.dumps({
        "zh_cn": {
            "title": "",
            "content": [
                [{"tag": "text", "text": "帮我看看这个截图"}],
                [{"tag": "img", "image_key": "img_post_2", "width": 100, "height": 80}],
            ],
        }
    })
    result = parse_event_line(_compact({"message_type": "post", "content": post}))
    assert result is not None
    assert result["text"] == "帮我看看这个截图"
    assert result["image_key"] == "img_post_2"


def test_parse_post_message_no_lang_wrapper():
    """post content 没有语言 key 时也应能解析"""
    post = json.dumps({
        "title": "",
        "content": [[{"tag": "text", "text": "直接格式"}]],
    })
    result = parse_event_line(_compact({"message_type": "post", "content": post}))
    assert result is not None
    assert result["text"] == "直接格式"


def test_parse_unsupported_message_type_returns_none():
    assert parse_event_line(_compact({"message_type": "file"})) is None
    assert parse_event_line(_compact({"message_type": "sticker"})) is None


def test_parse_missing_sender_returns_none():
    assert parse_event_line(_compact({"sender_id": ""})) is None


def test_parse_missing_content_returns_none():
    assert parse_event_line(_compact({"content": ""})) is None


def _group(overrides: dict = {}) -> str:
    base = {
        "type": "im.message.receive_v1",
        "message_id": "om_grp",
        "id": "om_grp",
        "chat_id": "oc_group_123",
        "chat_type": "group",
        "message_type": "text",
        "content": "@BotName 帮我查日程",
        "sender_id": "ou_xyz",
        "create_time": "1773491924409",
        "timestamp": "1773491924409",
        "mentions": [{"id": "ou_50fc6d8e545d2e3379240e619a85b3aa", "name": "BotName"}],
        "root_id": "",
    }
    return json.dumps({**base, **overrides})


BOT_OID = "ou_50fc6d8e545d2e3379240e619a85b3aa"


# --- Group message parsing ---

def test_parse_group_message_with_mention():
    result = parse_event_line(_group(), bot_open_id=BOT_OID)
    assert result is not None
    assert result["open_id"] == "ou_xyz"
    assert result["chat_type"] == "group"
    assert result["is_group"] is True
    assert result["text"] == "帮我查日程"  # @mention stripped


def test_parse_group_message_strips_at_mention():
    result = parse_event_line(_group({"content": "@BotName  hello world"}), bot_open_id=BOT_OID)
    assert result is not None
    assert result["text"] == "hello world"


def test_parse_group_message_wrong_mention_passes_through_not_mentioned():
    # Another user is @mentioned (not the bot) — passes through with mentioned=False
    # so main.py can check the thread session and decide whether to respond.
    line = _group({"mentions": [{"id": "ou_someone_else", "name": "Other"}]})
    result = parse_event_line(line, bot_open_id=BOT_OID)
    assert result is not None
    assert result["mentioned"] is False


def test_parse_group_message_no_mention_field_passes_through_not_mentioned():
    # No mentions field — cannot determine who is mentioned.
    # Passes through with mentioned=False so main.py can check thread session.
    base = {
        "type": "im.message.receive_v1",
        "message_id": "om_grp2",
        "chat_id": "oc_group_456",
        "chat_type": "group",
        "message_type": "text",
        "content": "@熊斯奇 会议日程能不能发一下～",
        "sender_id": "ou_xyz",
        "create_time": "1773491924409",
    }
    result = parse_event_line(json.dumps(base), bot_open_id=BOT_OID)
    assert result is not None
    assert result["mentioned"] is False


def test_parse_group_message_no_mention_no_at_passes_through_not_mentioned():
    base = {
        "type": "im.message.receive_v1",
        "message_id": "om_grp3",
        "chat_id": "oc_group_456",
        "chat_type": "group",
        "message_type": "text",
        "content": "普通群消息",
        "sender_id": "ou_xyz",
        "create_time": "1773491924409",
    }
    result = parse_event_line(json.dumps(base), bot_open_id=BOT_OID)
    assert result is not None
    assert result["mentioned"] is False


def test_parse_group_message_includes_root_id():
    result = parse_event_line(_group({"root_id": "om_root_111"}), bot_open_id=BOT_OID)
    assert result is not None
    assert result["root_id"] == "om_root_111"


def test_parse_group_message_empty_text_after_strip_skipped():
    result = parse_event_line(_group({"content": "@BotName  "}), bot_open_id=BOT_OID)
    assert result is None


# --- Helper function tests ---

def test_is_bot_mentioned_true_when_in_list():
    data = {"mentions": [{"id": BOT_OID}], "content": "@Bot hi"}
    assert _is_bot_mentioned(data, BOT_OID) is True


def test_is_bot_mentioned_true_for_nested_open_id():
    data = {"mentions": [{"id": {"open_id": BOT_OID}}], "content": "hi"}
    assert _is_bot_mentioned(data, BOT_OID) is True


def test_is_bot_mentioned_false_for_bot_marker_without_matching_open_id():
    data = {"mentions": [{"key": "@_user_1", "is_bot": True}], "content": "hi"}
    assert _is_bot_mentioned(data, BOT_OID) is False


def test_is_bot_mentioned_false_when_bot_open_id_missing():
    data = {"mentions": [{"id": BOT_OID}], "content": "@Bot hi"}
    assert _is_bot_mentioned(data, "") is False


def test_is_bot_mentioned_false_when_not_in_list():
    data = {"mentions": [{"id": "ou_other"}], "content": "@Other hi"}
    assert _is_bot_mentioned(data, BOT_OID) is False


def test_is_bot_mentioned_empty_mentions_false():
    assert _is_bot_mentioned({"mentions": []}, BOT_OID) is False


def test_is_bot_mentioned_no_mentions_field_returns_false():
    # No mentions field — treat as not mentioned regardless of content
    assert _is_bot_mentioned({"content": "@Bot hi"}, BOT_OID) is False
    assert _is_bot_mentioned({"content": "no at"}, BOT_OID) is False


def test_strip_at_mention():
    assert _strip_at_mention("@BotName hello") == "hello"
    assert _strip_at_mention("  @Bot  hi there  ") == "hi there"
    assert _strip_at_mention("no mention") == "no mention"


# --- P2P backward compat ---

def test_parse_p2p_message_has_new_fields():
    result = parse_event_line(_compact())
    assert result is not None
    assert result["chat_type"] == "p2p"
    assert result["is_group"] is False
    assert result["root_id"] == ""


class _FakePipe:
    def __init__(self, lines):
        self._lines = list(lines)

    def readline(self):
        if self._lines:
            return self._lines.pop(0)
        return ""


class _FakeProc:
    def __init__(self, pid, stdout_lines, poll_values=None):
        self.pid = pid
        self.returncode = 0
        self.stdin = _FakePipe([])
        self.stdout = _FakePipe(stdout_lines)
        self.stderr = _FakePipe([""])
        self._poll_values = list(poll_values or [None, 0])
        self.poll_calls = 0

    def poll(self):
        self.poll_calls += 1
        if self._poll_values:
            return self._poll_values.pop(0)
        return 0

    def wait(self):
        self.returncode = 0
        return 0


def test_event_listener_calls_callback_on_message():
    messages = []
    valid_line = _compact() + "\n"
    proc = _FakeProc(12345, [valid_line, ""])

    with patch("src.event_listener._CONSUME_EVENT_KEYS", ("im.message.receive_v1",)), \
         patch("src.event_listener.subprocess.Popen", return_value=proc) as popen, \
         patch("src.event_listener.EventListener._kill_proc", lambda self, proc: None):
        listener = EventListener(bot_home="/fake/bot-home", on_message=messages.append)
        listener._listen_once()

    assert len(messages) == 1
    assert messages[0]["open_id"] == "ou_xyz"
    event_commands = [
        call.args[0]
        for call in popen.call_args_list
        if len(call.args[0]) >= 4 and call.args[0][1:3] == ["event", "consume"]
    ]
    cmd = event_commands[0]
    assert cmd[1:4] == ["event", "consume", "im.message.receive_v1"]
    assert "--as" in cmd
    assert "bot" in cmd
    assert messages[0]["text"] == "你好"


def test_event_listener_logs_unhandled_card_action_like_events(caplog):
    messages = []
    card_action_line = json.dumps({
        "type": "card.action.trigger",
        "event_id": "evt_card_1",
        "message_id": "om_card_1",
        "open_message_id": "om_open_card_1",
        "chat_id": "oc_card_1",
        "action": {
            "value": {
                "action": "submit",
                "session_id": "form_1",
            }
        },
    }) + "\n"

    proc = _FakeProc(12345, [card_action_line, ""])

    caplog.set_level(logging.INFO, logger="src.event_listener")
    with patch("src.event_listener._CONSUME_EVENT_KEYS", ("im.message.receive_v1",)), \
         patch("src.event_listener.subprocess.Popen", return_value=proc), \
         patch("src.event_listener.EventListener._kill_proc", lambda self, proc: None):
        listener = EventListener(bot_home="/fake/bot-home", on_message=messages.append)
        listener._listen_once()

    assert messages == []
    log_text = caplog.text
    assert "Unhandled lark-cli event" in log_text
    assert "type=card.action.trigger" in log_text
    assert "event_id=evt_card_1" in log_text
    assert "message_id=om_card_1" in log_text
    assert "open_message_id=om_open_card_1" in log_text
    assert "chat_id=oc_card_1" in log_text
    assert "action=submit" in log_text
    assert "session_id=form_1" in log_text


def test_event_listener_logs_group_message_ingress(caplog):
    messages = []
    group_line = _group({"mentions": []}) + "\n"
    proc = _FakeProc(12345, [group_line, ""])

    caplog.set_level(logging.INFO, logger="src.event_listener")
    with patch("src.event_listener._CONSUME_EVENT_KEYS", ("im.message.receive_v1",)), \
         patch("src.event_listener.subprocess.Popen", return_value=proc), \
         patch("src.event_listener.EventListener._kill_proc", lambda self, proc: None):
        listener = EventListener(bot_home="/fake/bot-home", on_message=messages.append)
        listener._listen_once()

    assert len(messages) == 1
    assert "lark-cli message ingress" in caplog.text
    assert "chat_id=oc_group_123" in caplog.text
    assert "message_id=om_grp" in caplog.text
    assert "chat_type=group" in caplog.text


def test_event_listener_logs_every_group_message_ingress(caplog):
    messages = []
    first = _group({"message_id": "om_grp_1", "mentions": []}) + "\n"
    second = _group({"message_id": "om_grp_2", "mentions": []}) + "\n"
    proc = _FakeProc(12345, [first, second, ""])

    caplog.set_level(logging.INFO, logger="src.event_listener")
    with patch("src.event_listener._CONSUME_EVENT_KEYS", ("im.message.receive_v1",)), \
         patch("src.event_listener.subprocess.Popen", return_value=proc), \
         patch("src.event_listener.EventListener._kill_proc", lambda self, proc: None):
        listener = EventListener(bot_home="/fake/bot-home", on_message=messages.append)
        listener._listen_once()

    assert len(messages) == 2
    assert "message_id=om_grp_1" in caplog.text
    assert "message_id=om_grp_2" in caplog.text


def test_event_listener_starts_one_consumer_per_event_key():
    procs = [_FakeProc(pid, [""], poll_values=[0]) for pid in (1001, 1002)]

    with patch("src.event_listener.subprocess.Popen", side_effect=procs) as popen, \
         patch("src.event_listener.EventListener._kill_proc", lambda self, proc: None):
        listener = EventListener(bot_home="/fake/bot-home", on_message=lambda _event: None)
        listener._listen_once()

    commands = [
        call.args[0]
        for call in popen.call_args_list
        if len(call.args[0]) >= 4 and call.args[0][1:3] == ["event", "consume"]
    ]
    assert [cmd[1:4] for cmd in commands] == [
        ["event", "consume", "im.message.receive_v1"],
        ["event", "consume", "im.chat.member.bot.added_v1"],
    ]


def test_event_listener_reconnects_when_any_consumer_exits():
    exited_proc = _FakeProc(1001, [""], poll_values=[0])
    still_running_proc = _FakeProc(1002, [""], poll_values=[None] * 20 + [0])

    with patch("src.event_listener.subprocess.Popen", side_effect=[exited_proc, still_running_proc]), \
         patch("src.event_listener.EventListener._kill_proc", lambda self, proc: None):
        listener = EventListener(bot_home="/fake/bot-home", on_message=lambda _event: None)
        listener._listen_once()

    assert exited_proc.poll_calls == 1
    assert still_running_proc.poll_calls == 0


def test_event_listener_cleans_started_consumers_when_later_start_fails():
    started_proc = _FakeProc(1001, [""], poll_values=[None])
    killed = []

    def popen_side_effect(*args, **kwargs):
        if not killed and args[0][3] == "im.message.receive_v1":
            return started_proc
        raise OSError("boom")

    with patch("src.event_listener.subprocess.Popen", side_effect=popen_side_effect), \
         patch("src.event_listener.EventListener._kill_proc", lambda self, proc: killed.append(proc)):
        listener = EventListener(bot_home="/fake/bot-home", on_message=lambda _event: None)
        with pytest.raises(OSError):
            listener._listen_once()

    assert killed == [started_proc]


def test_run_loop_cleans_stale_subscribers_before_reconnect():
    listener = EventListener(bot_home="/fake/bot-home", on_message=lambda _event: None)
    calls = []

    def cleanup():
        calls.append("cleanup")

    def listen_once():
        calls.append("listen")

    def wait(timeout=None):
        listener._stop_event.set()
        return True

    listener._cleanup_stale_subscribers = cleanup
    listener._listen_once = listen_once
    listener._stop_event.wait = wait

    listener._run_loop()

    assert calls == ["cleanup", "listen", "cleanup"]
