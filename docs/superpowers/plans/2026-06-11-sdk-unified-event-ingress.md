# SDK Unified Event Ingress Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the current split `lark-cli event +subscribe` plus SDK card-action topology with one SDK long-connection ingress that handles normal messages, bot-added events, card actions, and no-op ACKs for subscribed but unsupported events.

**Architecture:** The SDK ingress must produce **byte-for-byte the same internal event dictionary** that `src.event_listener.parse_event_line()` already produces, because `handle_message()` and `handle_bot_added()` depend on a rich contract (`image_key`, `file_key`, `file_name`, `parent_id`, `root_id`, `mentioned`, `is_group`, plus pass-through of un-@-mentioned group messages). To guarantee parity and avoid maintaining two divergent parsers, we **do not write a second parser**. Instead we:

1. Refactor `event_listener.py` to split the pure dict-parsing core out of the line/JSON wrapper: `parse_event_data(data: dict, bot_open_id)` and `parse_bot_added_data(data: dict)`.
2. Add a thin SDK→compact **adapter** that flattens the nested SDK callback object into the same flat shape lark-cli `--compact` emits, then feeds it through the shared `parse_event_data` / `parse_bot_added_data`.

`src.main` selects either the legacy `lark-cli` listener or the new SDK listener through `BOT_EVENT_INGRESS=lark_cli|sdk`, while card actions remain on the same SDK connection in SDK mode. Multi-pod safety relies on every active pod being a full consumer for all subscribed event types and on existing PostgreSQL idempotency/locking.

**Why a shared parser (not a new one):** the original draft of this plan wrote a text-only `parse_sdk_message_event` that silently dropped image/file/post messages, P2P quote-reply `parent_id`, and group thread continuation (un-@ messages), and emitted `is_at_bot` instead of the `mentioned` key that `handle_message` reads. Those regressions passed the happy-path tests. Routing through `parse_event_data` eliminates that entire class of bug.

**Bonus fix:** in the current split topology, normal messages randomly routed to the SDK connection currently fail with `processor not found` → `code=500` and are only recovered by the 30s `on_poll` sweep. Unifying onto the SDK (with all handlers registered) removes this silent message-loss-and-delay path.

**Tech Stack:** Python 3, `lark-oapi` long connection SDK, PostgreSQL-backed stores, `ThreadPoolExecutor`, pytest.

---

## Current Evidence

The diagnostic experiment on `diagnostic/interactive-form-200671-minimal-card` showed:

- Minimal callback-only cards with `ack`, `toast`, and `sync_card` all produced `200671` under the split topology.
- The same cards stopped producing `200671` when only the SDK card-action listener was running.
- During the split topology, SDK logs showed `processor not found, type: im.message.receive_v1` followed by SDK websocket writes with `code=500`.
- During SDK-only diagnostics, repeated card clicks logged `code=200` and no `processor not found`.

This plan treats split long connections as the root architectural problem. It does not continue changing card JSON.

## File Structure

- Modify `src/event_listener.py`
  - Extract the pure dict-parsing core out of `parse_event_line()` into `parse_event_data(data: dict, bot_open_id="")`; `parse_event_line()` becomes `parse_event_data(json.loads(line), bot_open_id)`.
  - Extract `parse_bot_added_data(data: dict)` out of `parse_bot_added_line()` the same way.
  - Legacy `EventListener` behavior is otherwise unchanged (it just calls the wrappers).
- Create `src/sdk_event_listener.py`
  - SDK→compact **adapters** (`_sdk_message_to_compact`, `_sdk_bot_added_to_compact`) plus public `parse_sdk_message_event()` / `parse_sdk_bot_added_event()` that delegate to the shared `event_listener` parsers.
  - Event dispatcher registration, no-op ACK handlers, lifecycle management, and websocket write instrumentation reuse.
  - Exposes `SdkEventListener(app_id, app_secret, on_message, on_poll, bot_open_id, on_bot_added, card_action_handler)`.
- Modify `src/card_action_listener.py`
  - Keep `CardActionEvent`, `InteractiveFormHandler`, `parse_card_action_event()`, and callback response builder.
  - Remove SDK client lifecycle responsibility only after the new listener is stable; during this plan, share callback-building helpers and avoid duplicate clients in SDK mode.
- Modify `src/main.py`
  - Add `BOT_EVENT_INGRESS` selection.
  - In `sdk` mode, start one `SdkEventListener` and do not call `start_card_action_listener()`.
  - In `lark_cli` mode, preserve current behavior for rollback.
- Modify `src/config.py`
  - Add `BOT_EVENT_INGRESS` with default `lark_cli`.
- Modify tests:
  - `tests/test_sdk_event_listener.py`
  - `tests/test_main.py`
  - `tests/test_config.py`
  - Existing `tests/test_card_action_listener.py` as needed.

## Multi-Pod Rules

- Every pod that opens a Feishu long connection must register handlers for all subscribed event types.
- If all pods run SDK mode, all pods must be capable of processing message events, bot-added events, card actions, and read/access events as no-op ACK.
- Shared PostgreSQL remains the source of truth for:
  - message deduplication through existing `seen_messages`
  - form sessions through `FormStore`
  - card action events through `form_action_events`
  - Claude session IDs through `UserStore`
  - scheduled jobs through `JobStore`
- Do not store required routing state in process memory. Process-local active poller sets are acceptable only when backed by DB state checks before user-visible effects.
- Keep pod count below Feishu's long-connection client limit. If pod count becomes high, split a dedicated SDK Event Gateway later; that is not part of this implementation plan.

## Known Divergences & Risks

These are the concrete differences between the lark-cli `--compact` stream and the raw SDK callback that the adapter MUST absorb. Each has a test in Task 1.

1. **Content shape.** lark-cli compact delivers a text message's `content` as a plain string (`"你好"`); the SDK delivers the raw Feishu content JSON (`{"text":"你好"}`). The adapter unwraps `text` for `message_type == "text"`. For image/post/file, the SDK's JSON-string content is passed through unchanged, because the existing `_parse_image_content` / `_parse_post_content` / file branch in `parse_event_data` already `json.loads` it.
2. **Mentions shape.** compact mentions look like `[{"id": "ou_xxx"}]` (id is a string open_id); SDK mentions look like `[{"id": {"open_id": "ou_xxx"}, "key": "@_user_1"}]`. The adapter normalizes each mention to `{"id": "<open_id>"}` so the shared `_is_bot_mentioned()` keeps working. (Group @ is also re-verified via API in `handle_message`, so this is defense-in-depth.)
3. **Group pass-through.** `parse_event_data` returns un-@ group messages with `mentioned=False` (NOT `None`), so group thread continuation keeps working. The adapter must not pre-filter them out.
4. **Nested vs flat keys.** SDK uses `event.message.*` and `event.sender.sender_id.open_id`; compact is flat (`sender_id`, `message_id`, …). The adapter maps nested → flat.
5. **Reconnection.** Legacy `EventListener` has explicit reconnect + orphan cleanup + lock files + a 30s poll. `SdkEventListener` relies on `lark_oapi` `ws.Client`'s internal auto-reconnect. **Verify** (Task 10) that the SDK client reconnects after a forced network drop and resumes receiving; the retained `on_poll` sweep is the safety net if it does not.
6. **bot_added contract.** `handle_bot_added` reads `operator_id` / `chat_id` / `chat_name`. Route SDK bot-added through `parse_bot_added_data` so these exact keys are produced (do NOT invent `operator_open_id`).

---

### Task 1: Reuse the existing parser via an SDK→compact adapter

**Files:**
- Modify: `src/event_listener.py`
- Create: `src/sdk_event_listener.py`
- Test: `tests/test_sdk_event_listener.py`

- [ ] **Step 1: Refactor `event_listener.py` to expose dict-level parsers (no behavior change)**

Split the JSON-decode wrapper from the parsing core so the SDK adapter can reuse it. In `src/event_listener.py`:

```python
def parse_event_line(line: str, bot_open_id: str = "") -> Optional[dict]:
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return None
    return parse_event_data(data, bot_open_id)


def parse_event_data(data: dict, bot_open_id: str = "") -> Optional[dict]:
    # Body is the current parse_event_line logic AFTER the json.loads,
    # unchanged: type check, message_type routing (text/image/post/file),
    # group @mention handling and pass-through, and the returned event dict.
    ...
```

Do the same split for bot-added:

```python
def parse_bot_added_line(line: str) -> Optional[dict]:
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return None
    return parse_bot_added_data(data)


def parse_bot_added_data(data: dict) -> Optional[dict]:
    # Current parse_bot_added_line logic after the json.loads, unchanged.
    ...
```

The existing `tests/test_event_listener.py` is the regression guard for this refactor — it must still pass unchanged.

- [ ] **Step 2: Write failing parity tests for the SDK adapter**

The contract: `parse_sdk_message_event()` must produce the **same rich event dict** the lark-cli path produces — including `image_key`, `file_key`, `file_name`, `parent_id`, `root_id`, `is_group`, and the `mentioned` key (NOT `is_at_bot`), and it must pass un-@ group messages through rather than dropping them.

Add `tests/test_sdk_event_listener.py`:

```python
import json

from src.sdk_event_listener import parse_sdk_message_event


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
    event = parse_sdk_message_event(_sdk_text("你好"), bot_open_id="")
    assert event["open_id"] == "ou_1"
    assert event["text"] == "你好"
    assert event["message_id"] == "om_1"
    assert event["chat_type"] == "p2p"
    assert event["is_group"] is False
    # handle_message does event.get("image_key")/("file_key")/("file_name");
    # the keys must exist so the contract holds for every message type.
    assert event["image_key"] == ""
    assert event["file_key"] == ""
    assert event["file_name"] == ""


def test_sdk_image_message_extracts_image_key():
    event = parse_sdk_message_event(_sdk_msg("image", {"image_key": "img_abc"}), bot_open_id="")
    assert event["image_key"] == "img_abc"
    assert event["text"] == "[图片]"


def test_sdk_file_message_extracts_file_key_and_name():
    event = parse_sdk_message_event(
        _sdk_msg("file", {"file_key": "file_x", "file_name": "report.pdf"}), bot_open_id=""
    )
    assert event["file_key"] == "file_x"
    assert event["file_name"] == "report.pdf"


def test_sdk_post_message_extracts_text():
    content = {"zh_cn": {"title": "t", "content": [[{"tag": "text", "text": "正文内容"}]]}}
    event = parse_sdk_message_event(_sdk_msg("post", content), bot_open_id="")
    assert "正文内容" in event["text"]


def test_sdk_group_message_without_at_is_passed_through_not_dropped():
    event = parse_sdk_message_event(
        _sdk_text("大家好", chat_type="group", mentions=[]), bot_open_id="ou_bot"
    )
    assert event is not None
    assert event["is_group"] is True
    assert event["mentioned"] is False


def test_sdk_group_message_with_bot_mention_sets_mentioned():
    raw = _sdk_text("@机器人 帮我查日程", chat_type="group",
                    mentions=[{"key": "@_user_1", "id": {"open_id": "ou_bot"}}])
    event = parse_sdk_message_event(raw, bot_open_id="ou_bot")
    assert event["mentioned"] is True
    assert event["is_group"] is True


def test_sdk_parent_id_preserved_for_quote_reply():
    raw = _sdk_text("引用回复")
    raw["event"]["message"]["parent_id"] = "om_parent"
    event = parse_sdk_message_event(raw, bot_open_id="")
    assert event["parent_id"] == "om_parent"
```

- [ ] **Step 3: Run adapter tests and verify RED**

Run:

```bash
python -m pytest tests/test_sdk_event_listener.py -q
```

Expected:

```text
ModuleNotFoundError: No module named 'src.sdk_event_listener'
```

- [ ] **Step 4: Implement the SDK→compact adapter and delegate to the shared parser**

Create `src/sdk_event_listener.py`:

```python
import json
import logging
import os
import sys
import threading
import time
from typing import Any, Callable

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


def _normalize_mentions(mentions: Any) -> list[dict[str, str]]:
    """SDK mention id is nested ({"id": {"open_id": ...}}); compact expects {"id": "<open_id>"}."""
    if not isinstance(mentions, list):
        return []
    normalized = []
    for m in mentions:
        open_id = (
            _read_attr(m, "id", "open_id")
            or _read_attr(m, "id", "openId")
            or _read_attr(m, "open_id")
            or _read_attr(m, "openId")
            or (m if isinstance(m, str) else "")
        )
        normalized.append({"id": str(open_id or "")})
    return normalized


def _sdk_message_to_compact(raw: Any) -> dict[str, Any]:
    """Flatten a nested SDK message callback into the flat lark-cli --compact shape."""
    event = _read_attr(raw, "event") or raw
    header = _read_attr(raw, "header") or {}
    message = _read_attr(event, "message") or {}
    sender_id = _read_attr(event, "sender", "sender_id") or _read_attr(event, "sender", "senderId") or {}
    message_type = str(_read_attr(message, "message_type") or _read_attr(message, "messageType") or "")
    raw_content = _read_attr(message, "content")

    # compact delivers a TEXT message's content as a plain string; the SDK delivers
    # {"text": "..."}. For image/post/file, parse_event_data json.loads the content
    # itself, so pass the SDK's JSON string through unchanged.
    if message_type == "text":
        try:
            parsed = json.loads(raw_content) if isinstance(raw_content, str) else (raw_content or {})
            content: Any = parsed.get("text", "") if isinstance(parsed, dict) else (raw_content or "")
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


def parse_sdk_message_event(raw: Any, bot_open_id: str = "") -> dict[str, Any] | None:
    """Adapt an SDK message callback, then reuse the shared parser for full contract parity."""
    return parse_event_data(_sdk_message_to_compact(raw), bot_open_id)
```

- [ ] **Step 5: Run adapter tests and verify GREEN**

Run:

```bash
python -m pytest tests/test_sdk_event_listener.py tests/test_event_listener.py -q
```

Expected:

```text
all tests pass
```

(`test_event_listener.py` confirms the Step 1 refactor preserved legacy behavior.)

- [ ] **Step 6: Commit parser refactor and adapter**

```bash
git add src/event_listener.py src/sdk_event_listener.py tests/test_sdk_event_listener.py
git commit -m "feat: 复用事件解析器并增加 SDK 适配层"
```

---

### Task 2: Add SDK Bot-Added Parser and Unsupported Event ACK Contract

**Files:**
- Modify: `src/sdk_event_listener.py`
- Test: `tests/test_sdk_event_listener.py`

- [ ] **Step 1: Write failing tests**

The bot-added contract MUST match what `handle_bot_added()` reads: `operator_id`, `chat_id`, `chat_name` (plus `event_type == "bot_added"` from the shared parser). It must NOT invent `operator_open_id`. To guarantee this, `parse_sdk_bot_added_event()` flattens the nested SDK callback into the compact shape and delegates to the shared `parse_bot_added_data()` — the same function the lark-cli path uses (see Risk #6).

Append to `tests/test_sdk_event_listener.py`:

```python
from src.sdk_event_listener import parse_sdk_bot_added_event, sdk_noop_ack


def test_parse_sdk_bot_added_event_matches_handle_bot_added_contract():
    raw = {
        "header": {"event_id": "evt_add", "event_type": "im.chat.member.bot.added_v1"},
        "event": {
            "chat_id": "oc_1",
            "operator_id": {"open_id": "ou_inviter"},
            "name": "项目群",
            "external": False,
        },
    }

    event = parse_sdk_bot_added_event(raw)

    # handle_bot_added reads operator_id / chat_id / chat_name — NOT operator_open_id.
    assert event == {
        "event_type": "bot_added",
        "operator_id": "ou_inviter",
        "chat_id": "oc_1",
        "chat_name": "项目群",
    }


def test_parse_sdk_bot_added_event_returns_none_when_chat_or_operator_missing():
    raw = {
        "header": {"event_id": "evt_add", "event_type": "im.chat.member.bot.added_v1"},
        "event": {"chat_id": "", "operator_id": {"open_id": ""}},
    }

    assert parse_sdk_bot_added_event(raw) is None


def test_sdk_noop_ack_returns_none_for_successful_empty_sdk_ack():
    assert sdk_noop_ack({}) is None
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
python -m pytest tests/test_sdk_event_listener.py::test_parse_sdk_bot_added_event_matches_handle_bot_added_contract tests/test_sdk_event_listener.py::test_sdk_noop_ack_returns_none_for_successful_empty_sdk_ack -q
```

Expected:

```text
ImportError: cannot import name 'parse_sdk_bot_added_event'
```

- [ ] **Step 3: Implement bot-added adapter (delegating to the shared parser) and no-op ACK helper**

`parse_bot_added_data` (split out of `parse_bot_added_line` in Task 1) expects the flat compact shape: top-level `type == "im.chat.member.bot.added_v1"`, plus `chat_id`, `operator_id` (string open_id or `{"open_id": ...}`), and `name`/`chat_name`. It already returns `{"event_type": "bot_added", "operator_id", "chat_id", "chat_name"}` and yields `None` when `chat_id` or `operator_id` is missing. So the SDK side only needs an adapter that flattens nested → compact and then delegates.

Add to `src/sdk_event_listener.py`:

`parse_bot_added_data` is already imported alongside `parse_event_data` at the top of the file from Task 1 — no new import is needed.

```python
def _sdk_bot_added_to_compact(raw: Any) -> dict[str, Any]:
    """Flatten a nested SDK bot-added callback into the flat lark-cli --compact shape."""
    event = _read_attr(raw, "event") or raw
    header = _read_attr(raw, "header") or {}
    operator = _read_attr(event, "operator_id") or _read_attr(event, "operatorId") or {}
    # operator_id may be nested ({"open_id": ...}) or already a string.
    if isinstance(operator, str):
        operator_id: Any = operator
    else:
        operator_id = _read_attr(operator, "open_id") or _read_attr(operator, "openId") or ""
    return {
        "type": str(_read_attr(header, "event_type") or _read_attr(header, "eventType") or ""),
        "chat_id": str(_read_attr(event, "chat_id") or _read_attr(event, "chatId") or ""),
        "operator_id": str(operator_id or ""),
        "name": str(_read_attr(event, "name") or _read_attr(event, "chat_name") or ""),
    }


def parse_sdk_bot_added_event(raw: Any) -> dict[str, Any] | None:
    """Adapt an SDK bot-added callback, then reuse the shared parser for contract parity."""
    return parse_bot_added_data(_sdk_bot_added_to_compact(raw))


def sdk_noop_ack(raw: Any) -> None:
    event_type = (
        _read_attr(raw, "header", "event_type")
        or _read_attr(raw, "header", "eventType")
        or _read_attr(raw, "event", "type")
        or ""
    )
    logger.info("SDK no-op ACK for event_type=%s", event_type)
    return None
```

- [ ] **Step 4: Run tests and verify GREEN**

Run:

```bash
python -m pytest tests/test_sdk_event_listener.py -q
```

Expected (7 parity tests from Task 1 + 3 from Task 2):

```text
10 passed
```

- [ ] **Step 5: Commit parser additions**

```bash
git add src/sdk_event_listener.py tests/test_sdk_event_listener.py
git commit -m "feat: 增加 SDK 群事件和空 ACK 解析"
```

---

### Task 3: Build `SdkEventListener` Lifecycle Without Starting It From `main`

**Files:**
- Modify: `src/sdk_event_listener.py`
- Test: `tests/test_sdk_event_listener.py`

- [ ] **Step 1: Write failing lifecycle tests with fake SDK builder**

Append to `tests/test_sdk_event_listener.py`:

```python
from unittest.mock import MagicMock, patch

from src.sdk_event_listener import SdkEventListener


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

    def build(self):
        return self


class FakeEventDispatcherHandler:
    last_builder = None

    @classmethod
    def builder(cls, *args):
        cls.last_builder = FakeDispatcherBuilder()
        return cls.last_builder


class FakeWsClient:
    instances = []

    def __init__(self, app_id, app_secret, event_handler, log_level):
        self.app_id = app_id
        self.app_secret = app_secret
        self.event_handler = event_handler
        self.log_level = log_level
        self.started = False
        FakeWsClient.instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.started = False


class FakeLark:
    EventDispatcherHandler = FakeEventDispatcherHandler
    LogLevel = type("LogLevel", (), {"INFO": "INFO"})
    ws = type("ws", (), {"Client": FakeWsClient})


def test_sdk_event_listener_registers_all_required_callbacks():
    on_message = MagicMock()
    on_bot_added = MagicMock()
    card_action_handler = MagicMock()

    with patch.dict("sys.modules", {"lark_oapi": FakeLark}):
        listener = SdkEventListener(
            app_id="app",
            app_secret="secret",
            on_message=on_message,
            on_poll=None,
            bot_open_id="ou_bot",
            on_bot_added=on_bot_added,
            card_action_handler=card_action_handler,
        )
        listener.start()

    registered = FakeEventDispatcherHandler.last_builder.registered
    assert set(registered) == {"message", "bot_added", "card", "read", "p2p_entered"}
    assert FakeWsClient.instances[-1].app_id == "app"
```

- [ ] **Step 2: Run lifecycle test and verify RED**

Run:

```bash
python -m pytest tests/test_sdk_event_listener.py::test_sdk_event_listener_registers_all_required_callbacks -q
```

Expected:

```text
ImportError: cannot import name 'SdkEventListener'
```

- [ ] **Step 3: Implement listener constructor and registration**

Add to `src/sdk_event_listener.py`:

```python
class SdkEventListener:
    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        on_message: Callable[[dict[str, Any]], None],
        on_poll: Callable[[], None] | None,
        bot_open_id: str,
        on_bot_added: Callable[[dict[str, Any]], None] | None,
        card_action_handler: Any,
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
        try:
            import lark_oapi as lark
        except Exception as e:
            logger.warning("lark-oapi unavailable; SDK event listener not started: %s", e)
            return

        from src.card_action_listener import (
            build_card_action_callback_response,
            instrument_ws_client_writes,
            parse_card_action_event,
        )

        def _message_callback(req):
            event = parse_sdk_message_event(req, bot_open_id=self.bot_open_id)
            if event:
                self.on_message(event)
            return None

        def _bot_added_callback(req):
            event = parse_sdk_bot_added_event(req)
            # Mirror the legacy `if bot_added:` guard — parse returns None when
            # chat_id/operator_id are missing, and handle_bot_added would crash on None.
            if event and self.on_bot_added:
                self.on_bot_added(event)
            return None

        def _card_action_callback(req):
            # Imported here, not in start(): the submodule path cannot be resolved
            # when sys.modules["lark_oapi"] is patched to a non-package fake in tests,
            # and it is only needed when a card action actually fires.
            from lark_oapi.event.callback.model.p2_card_action_trigger import (
                P2CardActionTriggerResponse,
            )

            event = parse_card_action_event(req)
            logger.info(
                "SDK card action received: action=%s session=%s message=%s",
                event.action,
                event.session_id,
                event.message_id,
            )
            result = self.card_action_handler.handle_action(event)
            response = build_card_action_callback_response(P2CardActionTriggerResponse, result)
            logger.info(
                "SDK card action response built: action=%s response_type=%s",
                event.action,
                type(response).__name__,
            )
            return response

        builder = lark.EventDispatcherHandler.builder("", "")
        builder = builder.register_p2_im_message_receive_v1(_message_callback)
        builder = builder.register_p2_im_chat_member_bot_added_v1(_bot_added_callback)
        builder = builder.register_p2_card_action_trigger(_card_action_callback)
        builder = builder.register_p2_im_message_message_read_v1(sdk_noop_ack)
        builder = builder.register_p2_im_chat_access_event_bot_p2p_chat_entered_v1(sdk_noop_ack)
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

    def _poll_loop(self) -> None:
        self._stop_event.wait(timeout=30)
        while not self._stop_event.is_set():
            try:
                if self.on_poll:
                    self.on_poll()
            except Exception as e:
                logger.error("SDK poll callback error: %s", e)
            self._stop_event.wait(timeout=30)

    def stop(self) -> None:
        self._stop_event.set()
        client = self._client
        if client and hasattr(client, "stop"):
            try:
                client.stop()
            except Exception as e:
                logger.warning("SDK event listener stop failed: %s", e)
```

- [ ] **Step 4: Run lifecycle tests and verify GREEN**

Run:

```bash
python -m pytest tests/test_sdk_event_listener.py -q
```

Expected (7 from Task 1 + 3 from Task 2 + 1 lifecycle test):

```text
11 passed
```

- [ ] **Step 5: Commit listener skeleton**

```bash
git add src/sdk_event_listener.py tests/test_sdk_event_listener.py
git commit -m "feat: 增加 SDK 统一事件监听器"
```

---

### Task 4: Ensure SDK Callback Only Enqueues Message Work

**Files:**
- Modify: `src/sdk_event_listener.py`
- Test: `tests/test_sdk_event_listener.py`

- [ ] **Step 1: Write failing tests for callback behavior**

Append to `tests/test_sdk_event_listener.py`:

```python
def test_sdk_message_callback_calls_on_message_and_returns_ack():
    received = []
    with patch.dict("sys.modules", {"lark_oapi": FakeLark}):
        listener = SdkEventListener(
            app_id="app",
            app_secret="secret",
            on_message=received.append,
            on_poll=None,
            bot_open_id="",
            on_bot_added=None,
            card_action_handler=MagicMock(),
        )
        listener.start()

    callback = FakeEventDispatcherHandler.last_builder.registered["message"]
    response = callback({
        "header": {"event_id": "evt_1", "event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_1"}},
            "message": {
                "message_id": "om_1",
                "chat_id": "oc_1",
                "chat_type": "p2p",
                "create_time": "1781157390915",
                "message_type": "text",
                "content": "{\"text\":\"ping\"}",
            },
        },
    })

    assert response is None
    assert received[0]["text"] == "ping"
    assert received[0]["message_id"] == "om_1"


def test_sdk_group_message_without_at_is_forwarded_with_mentioned_false():
    # Risk #3: un-@ group messages MUST be passed through (mentioned=False), not
    # dropped, so group thread continuation keeps working. handle_message decides
    # whether to respond based on the active thread session — the callback must not
    # pre-filter them.
    on_message = MagicMock()
    with patch.dict("sys.modules", {"lark_oapi": FakeLark}):
        listener = SdkEventListener(
            app_id="app",
            app_secret="secret",
            on_message=on_message,
            on_poll=None,
            bot_open_id="ou_bot",
            on_bot_added=None,
            card_action_handler=MagicMock(),
        )
        listener.start()

    callback = FakeEventDispatcherHandler.last_builder.registered["message"]
    response = callback({
        "header": {"event_id": "evt_2", "event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_1"}},
            "message": {
                "message_id": "om_2",
                "chat_id": "oc_group",
                "chat_type": "group",
                "create_time": "1781157390916",
                "message_type": "text",
                "content": "{\"text\":\"hello\"}",
                "mentions": [],
            },
        },
    })

    assert response is None
    on_message.assert_called_once()
    forwarded = on_message.call_args.args[0]
    assert forwarded["is_group"] is True
    assert forwarded["mentioned"] is False
```

- [ ] **Step 2: Run callback tests**

Run:

```bash
python -m pytest tests/test_sdk_event_listener.py::test_sdk_message_callback_calls_on_message_and_returns_ack tests/test_sdk_event_listener.py::test_sdk_group_message_without_at_is_forwarded_with_mentioned_false -q
```

Expected:

```text
2 passed
```

If this fails, fix `parse_sdk_message_event()` or `_message_callback()` until these pass. Do not change `main.py` in this task. Note: `_message_callback` must NOT pre-filter un-@ group messages — `parse_sdk_message_event` already returns them with `mentioned=False`, and `handle_message` decides whether to respond.

- [ ] **Step 3: Commit callback contract**

```bash
git add src/sdk_event_listener.py tests/test_sdk_event_listener.py
git commit -m "test: 固定 SDK 消息回调 ACK 契约"
```

---

### Task 5: Add Config Flag for Ingress Selection

**Files:**
- Modify: `src/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write failing config tests**

Append to `tests/test_config.py`:

```python
def test_config_defaults_event_ingress_to_lark_cli(monkeypatch):
    monkeypatch.setenv("FEISHU_APP_ID", "app")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")
    monkeypatch.setenv("POSTGRES_URL", "postgres://example")
    monkeypatch.delenv("BOT_EVENT_INGRESS", raising=False)

    cfg = Config()

    assert cfg.bot_event_ingress == "lark_cli"


def test_config_accepts_sdk_event_ingress(monkeypatch):
    monkeypatch.setenv("FEISHU_APP_ID", "app")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")
    monkeypatch.setenv("POSTGRES_URL", "postgres://example")
    monkeypatch.setenv("BOT_EVENT_INGRESS", "sdk")

    cfg = Config()

    assert cfg.bot_event_ingress == "sdk"
```

- [ ] **Step 2: Run config tests and verify RED**

Run:

```bash
python -m pytest tests/test_config.py::test_config_defaults_event_ingress_to_lark_cli tests/test_config.py::test_config_accepts_sdk_event_ingress -q
```

Expected:

```text
AttributeError: 'Config' object has no attribute 'bot_event_ingress'
```

- [ ] **Step 3: Implement config field with validation**

Modify `src/config.py`:

```python
optional = {
    "ANTHROPIC_AUTH_TOKEN": ("anthropic_auth_token", ""),
    "ANTHROPIC_BASE_URL": ("anthropic_base_url", ""),
    "CLAUDE_MODEL": ("claude_model", "anthropic/claude-sonnet-4.6"),
    "LARK_BOT_HOME": ("lark_bot_home", "/var/lark-bot/config"),
    "LARK_USERS_DIR": ("lark_users_dir", "/var/lark-bot/users"),
    "FEISHU_BOT_OPEN_ID": ("feishu_bot_open_id", ""),
    "CLAUDE_HOME": ("claude_home", ""),
    "OA_API_KEY": ("oa_api_key", ""),
    "BOT_EVENT_INGRESS": ("bot_event_ingress", "lark_cli"),
}
```

After the existing `for env_key, (attr, default) in optional.items():` loop, add:

```python
self.bot_event_ingress = str(self.bot_event_ingress or "lark_cli").strip().lower()
if self.bot_event_ingress not in ("lark_cli", "sdk"):
    raise ValueError("BOT_EVENT_INGRESS must be 'lark_cli' or 'sdk'")
```

- [ ] **Step 4: Add invalid value test**

Append to `tests/test_config.py`:

```python
def test_config_rejects_invalid_event_ingress(monkeypatch):
    monkeypatch.setenv("FEISHU_APP_ID", "app")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")
    monkeypatch.setenv("POSTGRES_URL", "postgres://example")
    monkeypatch.setenv("BOT_EVENT_INGRESS", "both")

    with pytest.raises(ValueError, match="BOT_EVENT_INGRESS"):
        Config()
```

- [ ] **Step 5: Run config tests and verify GREEN**

Run:

```bash
python -m pytest tests/test_config.py -q
```

Expected:

```text
all tests pass
```

- [ ] **Step 6: Commit config flag**

```bash
git add src/config.py tests/test_config.py
git commit -m "feat: 增加事件入口模式配置"
```

---

### Task 6: Wire SDK Ingress Into `main.py` Behind the Flag

**Files:**
- Modify: `src/main.py`
- Test: `tests/test_main.py`

- [ ] **Step 1: Extract listener startup into a small helper**

Before changing behavior, add a helper near the existing startup code in `src/main.py`:

```python
def _start_event_ingress(
    *,
    mode: str,
    cfg,
    on_message,
    on_poll,
    on_bot_added,
    card_action_handler,
):
    if mode == "sdk":
        from src.sdk_event_listener import SdkEventListener

        listener = SdkEventListener(
            app_id=cfg.feishu_app_id,
            app_secret=cfg.feishu_app_secret,
            on_message=on_message,
            on_poll=on_poll,
            bot_open_id=cfg.feishu_bot_open_id,
            on_bot_added=on_bot_added,
            card_action_handler=card_action_handler,
        )
        listener.start()
        return listener, None

    listener = EventListener(
        bot_home=cfg.lark_bot_home,
        on_message=on_message,
        on_poll=on_poll,
        bot_open_id=cfg.feishu_bot_open_id,
        on_bot_added=on_bot_added,
    )
    listener.start()
    return listener, "legacy_card_listener"
```

This helper initially returns a sentinel for the card listener in legacy mode. The next step will refine it with tests.

- [ ] **Step 2: Write failing tests for ingress selection**

Append to `tests/test_main.py`:

```python
def test_start_event_ingress_sdk_mode_starts_sdk_listener(mocker):
    from src import main

    cfg = MagicMock()
    cfg.feishu_app_id = "app"
    cfg.feishu_app_secret = "secret"
    cfg.feishu_bot_open_id = "ou_bot"
    sdk_cls = mocker.patch("src.sdk_event_listener.SdkEventListener")
    sdk_listener = sdk_cls.return_value

    listener, legacy_marker = main._start_event_ingress(
        mode="sdk",
        cfg=cfg,
        on_message=MagicMock(),
        on_poll=MagicMock(),
        on_bot_added=MagicMock(),
        card_action_handler=MagicMock(),
    )

    assert listener is sdk_listener
    assert legacy_marker is None
    sdk_listener.start.assert_called_once()


def test_start_event_ingress_lark_cli_mode_starts_legacy_listener(mocker):
    from src import main

    cfg = MagicMock()
    cfg.lark_bot_home = "bot-home"
    cfg.feishu_bot_open_id = "ou_bot"
    event_listener_cls = mocker.patch("src.main.EventListener")
    legacy_listener = event_listener_cls.return_value

    listener, legacy_marker = main._start_event_ingress(
        mode="lark_cli",
        cfg=cfg,
        on_message=MagicMock(),
        on_poll=MagicMock(),
        on_bot_added=MagicMock(),
        card_action_handler=MagicMock(),
    )

    assert listener is legacy_listener
    assert legacy_marker == "legacy_card_listener"
    legacy_listener.start.assert_called_once()
```

- [ ] **Step 3: Run helper tests and verify RED or GREEN**

Run:

```bash
python -m pytest tests/test_main.py::test_start_event_ingress_sdk_mode_starts_sdk_listener tests/test_main.py::test_start_event_ingress_lark_cli_mode_starts_legacy_listener -q
```

Expected before helper exists:

```text
AttributeError: module 'src.main' has no attribute '_start_event_ingress'
```

Expected after Step 1:

```text
2 passed
```

- [ ] **Step 4: Replace startup block in `main.py`**

Replace the existing block:

```python
card_action_listener = start_card_action_listener(
    cfg.feishu_app_id, cfg.feishu_app_secret, card_action_handler,
)
...
listener = EventListener(bot_home=cfg.lark_bot_home, on_message=on_message,
                         on_poll=on_poll, bot_open_id=cfg.feishu_bot_open_id,
                         on_bot_added=on_bot_added)
listener.start()
```

with:

```python
card_action_listener = None
legacy_card_action_listener_needed = cfg.bot_event_ingress == "lark_cli"
if legacy_card_action_listener_needed:
    card_action_listener = start_card_action_listener(
        cfg.feishu_app_id, cfg.feishu_app_secret, card_action_handler,
    )

listener, _legacy_marker = _start_event_ingress(
    mode=cfg.bot_event_ingress,
    cfg=cfg,
    on_message=on_message,
    on_poll=on_poll,
    on_bot_added=on_bot_added,
    card_action_handler=card_action_handler,
)
scheduler.start()
```

Keep the existing shutdown code:

```python
if card_action_listener:
    card_action_listener.stop()
if listener:
    listener.stop()
```

- [ ] **Step 5: Run main tests**

Run:

```bash
python -m pytest tests/test_main.py -q
```

Expected:

```text
all tests pass
```

- [ ] **Step 6: Commit main wiring**

```bash
git add src/main.py tests/test_main.py
git commit -m "feat: 支持 SDK 统一事件入口开关"
```

---

### Task 7: Remove SDK `processor not found` by Registering No-op ACKs for Observed Events

**Files:**
- Modify: `src/sdk_event_listener.py`
- Test: `tests/test_sdk_event_listener.py`

- [ ] **Step 1: Write test covering observed no-op event registrations**

Update the registration assertion in `test_sdk_event_listener_registers_all_required_callbacks()`:

```python
assert set(registered) == {
    "message",
    "bot_added",
    "card",
    "read",
    "p2p_entered",
}
```

Also add this test:

```python
def test_sdk_noop_registered_events_return_ack():
    with patch.dict("sys.modules", {"lark_oapi": FakeLark}):
        listener = SdkEventListener(
            app_id="app",
            app_secret="secret",
            on_message=MagicMock(),
            on_poll=None,
            bot_open_id="",
            on_bot_added=None,
            card_action_handler=MagicMock(),
        )
        listener.start()

    registered = FakeEventDispatcherHandler.last_builder.registered
    assert registered["read"]({"header": {"event_type": "im.message.message_read_v1"}}) is None
    assert registered["p2p_entered"]({
        "header": {"event_type": "im.chat.access_event.bot_p2p_chat_entered_v1"},
    }) is None
```

- [ ] **Step 2: Run no-op tests**

Run:

```bash
python -m pytest tests/test_sdk_event_listener.py::test_sdk_noop_registered_events_return_ack -q
```

Expected:

```text
1 passed
```

- [ ] **Step 3: Manual code audit against logs**

Run:

```bash
Select-String -Path bot.log -Pattern 'processor not found, type:' | Select-Object -Last 20
```

Expected observed event types from current logs:

```text
im.message.receive_v1
im.message.message_read_v1
im.chat.access_event.bot_p2p_chat_entered_v1
```

If another event type appears, add a no-op registration for it in `SdkEventListener.start()` and extend `FakeDispatcherBuilder` plus assertions in `tests/test_sdk_event_listener.py`.

- [ ] **Step 4: Commit no-op registrations**

```bash
git add src/sdk_event_listener.py tests/test_sdk_event_listener.py
git commit -m "fix: 为 SDK 入口注册已订阅事件 ACK"
```

---

### Task 8: Preserve Poll Recovery in SDK Mode

**Files:**
- Modify: `src/sdk_event_listener.py`
- Test: `tests/test_sdk_event_listener.py`

- [ ] **Step 1: Write poll recovery test**

Append to `tests/test_sdk_event_listener.py`:

```python
def test_sdk_event_listener_starts_poll_thread_when_on_poll_is_present():
    with patch.dict("sys.modules", {"lark_oapi": FakeLark}), \
         patch("src.sdk_event_listener.threading.Thread") as thread_cls:
        listener = SdkEventListener(
            app_id="app",
            app_secret="secret",
            on_message=MagicMock(),
            on_poll=MagicMock(),
            bot_open_id="",
            on_bot_added=None,
            card_action_handler=MagicMock(),
        )
        listener.start()

    thread_names = [call.kwargs.get("name") for call in thread_cls.call_args_list]
    assert "sdk-event-listener" in thread_names
    assert "sdk-poll-recovery" in thread_names
```

- [ ] **Step 2: Run poll test**

Run:

```bash
python -m pytest tests/test_sdk_event_listener.py::test_sdk_event_listener_starts_poll_thread_when_on_poll_is_present -q
```

Expected:

```text
1 passed
```

- [ ] **Step 3: Commit poll recovery coverage**

```bash
git add src/sdk_event_listener.py tests/test_sdk_event_listener.py
git commit -m "test: 覆盖 SDK 模式消息轮询兜底"
```

---

### Task 9: Add Local Diagnostic Runner for SDK Ingress Mode

**Files:**
- Create: `scripts/sdk_ingress_smoke.py`
- Test: no automated test; this is a bounded local diagnostic script.

- [ ] **Step 1: Create smoke runner**

Create `scripts/sdk_ingress_smoke.py`:

```python
"""Run SDK ingress only for local long-connection diagnostics."""

import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.card_action_listener import InteractiveFormHandler
from src.config import Config
from src.sdk_event_listener import SdkEventListener


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        handlers=[
            logging.FileHandler("sdk_ingress_smoke.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    cfg = Config()
    executor = ThreadPoolExecutor(max_workers=4)

    def on_message(event: dict):
        logging.info("SMOKE message event=%s", event)

    def on_bot_added(event: dict):
        logging.info("SMOKE bot_added event=%s", event)

    def on_poll():
        logging.info("SMOKE poll tick")

    handler = InteractiveFormHandler(None, None, cfg.feishu_app_id, cfg.feishu_app_secret)
    listener = SdkEventListener(
        app_id=cfg.feishu_app_id,
        app_secret=cfg.feishu_app_secret,
        on_message=lambda event: executor.submit(on_message, event),
        on_poll=on_poll,
        bot_open_id=cfg.feishu_bot_open_id,
        on_bot_added=lambda event: executor.submit(on_bot_added, event),
        card_action_handler=handler,
    )
    listener.start()
    with open("sdk_ingress_smoke.pid", "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    logging.info("SDK ingress smoke started pid=%s", os.getpid())
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        listener.stop()
        executor.shutdown(wait=False, cancel_futures=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Run smoke script with legacy bot stopped**

Run:

```bash
python scripts/sdk_ingress_smoke.py
```

Expected:

```text
SDK ingress smoke started
```

Send a normal message to the bot and click an existing diagnostic card. Expected in `sdk_ingress_smoke.log`:

```text
SMOKE message event=
Diagnostic card action received:
Lark SDK websocket write completed: code=200
```

Stop the script with Ctrl+C.

- [ ] **Step 3: Commit smoke runner**

```bash
git add scripts/sdk_ingress_smoke.py
git commit -m "chore: 增加 SDK 统一入口冒烟脚本"
```

---

### Task 10: Full Regression and SDK Mode Manual Verification

**Files:**
- No code changes unless verification reveals a specific defect.

- [ ] **Step 1: Run focused tests**

Run:

```bash
python -m pytest tests/test_sdk_event_listener.py tests/test_card_action_listener.py tests/test_main.py tests/test_config.py -q
```

Expected:

```text
all tests pass
```

- [ ] **Step 2: Run full tests**

Run:

```bash
python -m pytest -q
```

Expected:

```text
all tests pass
```

- [ ] **Step 3: Start bot in SDK mode locally**

Set environment:

```powershell
$env:BOT_EVENT_INGRESS='sdk'
python -m src.main
```

Expected logs:

```text
SDK event listener started
Feishu AI Bot started. Listening for messages...
```

There must be no line containing:

```text
lark-cli.exe event +subscribe
```

- [ ] **Step 4: Verify normal message path**

Send to the bot:

```text
/reset
```

Expected:

```text
[收到] ...
[reset] Session cleared ...
```

- [ ] **Step 5: Verify diagnostic card path**

Send:

```text
发送卡片回调诊断卡，模式 toast
```

Click the diagnostic card.

Expected:

```text
Diagnostic card action received: mode=toast
Lark SDK websocket write completed: code=200
```

Client must not show `200671`.

- [ ] **Step 6: Verify interactive form path**

Send:

```text
帮我创建一个任务，但优先级、内容、负责人和任务时间你用表单问我
```

Fill all form questions.

Expected:

```text
Card action received: action=submit
Lark SDK websocket write completed: code=200
```

Client must not show `200671`.

- [ ] **Step 7: Verify rollback path**

Set:

```powershell
$env:BOT_EVENT_INGRESS='lark_cli'
python -m src.main
```

Expected logs:

```text
Event listener started
Card action listener started
```

This confirms the old topology remains available during rollout.

---

### Task 11: Deployment Notes and Operational Guardrails

**Files:**
- Modify: `docs/superpowers/specs/2026-06-11-interactive-form-200671-investigation.md`
- Create: `docs/superpowers/specs/2026-06-11-sdk-unified-ingress-rollout.md`

- [ ] **Step 1: Add investigation conclusion**

Append to `docs/superpowers/specs/2026-06-11-interactive-form-200671-investigation.md`:

```markdown
## 追加结论：SDK-only 诊断

2026-06-11 诊断分支新增最小 callback 卡片。双长连接模式下，`ack`、`toast`、`sync_card` 三种最小卡点击均会在客户端出现 `200671`。切换为只运行 SDK card-action listener、停止 `lark-cli event +subscribe` 后，同一批卡片反复点击不再出现 `200671`。

服务端日志显示 SDK-only 下所有诊断动作均进入 SDK 并返回 `code=200`，且没有 `processor not found`。因此根因高度指向同一 Feishu app 下多长连接 client 的集群路由/订阅污染，而非卡片 JSON、form/checker/input 或同步/异步更新模式。
```

- [ ] **Step 2: Create rollout doc**

Create `docs/superpowers/specs/2026-06-11-sdk-unified-ingress-rollout.md`:

```markdown
# SDK 统一事件入口发布说明

## 环境变量

- `BOT_EVENT_INGRESS=lark_cli`：旧入口，保留回滚能力。
- `BOT_EVENT_INGRESS=sdk`：新入口，SDK 同时处理普通消息、bot 入群、卡片动作和 no-op ACK 事件。

## 多 pod 要求

1. 所有开启长连接的 pod 必须运行同一种入口模式。
2. `sdk` 模式下，每个 pod 都必须注册所有已订阅事件的 handler 或 no-op ACK。
3. 关键状态必须通过 PostgreSQL 共享：消息幂等、表单状态、卡片动作幂等、Claude session、任务状态。
4. 如果 pod 数量接近 Feishu 长连接 client 限制，应迁移到单独 SDK Event Gateway。

## 发布步骤

1. 单 pod 设置 `BOT_EVENT_INGRESS=sdk`。
2. 验证普通消息、诊断卡、完整交互表单。
3. 扩到多 pod，确认所有 pod 都使用 `sdk`。
4. 观察 24 小时内是否出现 `processor not found` 或 `200671`。

## 回滚

设置 `BOT_EVENT_INGRESS=lark_cli` 并重启服务。
```

- [ ] **Step 3: Commit docs**

```bash
git add docs/superpowers/specs/2026-06-11-interactive-form-200671-investigation.md docs/superpowers/specs/2026-06-11-sdk-unified-ingress-rollout.md
git commit -m "docs: 记录 SDK 统一入口发布策略"
```

---

## Rollback Plan

Runtime rollback:

```powershell
$env:BOT_EVENT_INGRESS='lark_cli'
python -m src.main
```

Code rollback:

```bash
git revert <commits from this plan>
```

Operational rollback checks:

- `Event listener started` appears.
- `Card action listener started` appears.
- Normal messages are handled.
- Existing form cards may still show `200671` in legacy mode; this is expected and is the reason to prefer SDK mode.

## Final Verification Checklist

- [ ] `python -m pytest -q` passes.
- [ ] SDK mode starts without launching `lark-cli event +subscribe`.
- [ ] SDK mode logs no `processor not found`.
- [ ] SDK mode handles `/reset`.
- [ ] SDK mode sends and handles diagnostic card without `200671`.
- [ ] SDK mode handles full interactive form without `200671`.
- [ ] Multi-pod deployment uses the same `BOT_EVENT_INGRESS` value across all pods.
- [ ] Rollback to `lark_cli` mode works.

## Self-Review

- Spec coverage: The plan covers the diagnostic conclusion, SDK unified ingress, normal messages, bot-added events, card actions, no-op ACK, poll recovery, multi-pod safety, rollout, and rollback.
- Placeholder scan: No placeholder work items remain; every task has concrete files, tests, commands, and expected outcomes.
- Type consistency: The plan consistently uses `SdkEventListener`, `parse_sdk_message_event`, `parse_sdk_bot_added_event`, `sdk_noop_ack`, and `BOT_EVENT_INGRESS`.
