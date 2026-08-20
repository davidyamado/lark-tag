"""Direct Feishu HTTP API — used for streaming message updates (not available in lark-cli)."""
import json
import logging
import threading
import time
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

FEISHU_BASE = "https://open.feishu.cn/open-apis"

_cache: dict = {"token": None, "expire": 0.0, "app_id": "", "app_secret": ""}
_lock = threading.Lock()


def _refresh_token_locked() -> str:
    """Re-fetch token using last-known credentials. _lock must be held."""
    body = json.dumps(
        {"app_id": _cache["app_id"], "app_secret": _cache["app_secret"]}
    ).encode()
    req = urllib.request.Request(
        f"{FEISHU_BASE}/auth/v3/tenant_access_token/internal",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())
    if data.get("code") != 0:
        raise RuntimeError(f"tenant_access_token error: {data}")
    _cache["token"] = data["tenant_access_token"]
    _cache["expire"] = time.monotonic() + data.get("expire", 7200) - 300
    return _cache["token"]


def get_tenant_access_token(app_id: str, app_secret: str) -> str:
    """Fetch and cache the bot tenant_access_token."""
    with _lock:
        # Remember creds so we can refresh on 99991663 later
        _cache["app_id"] = app_id
        _cache["app_secret"] = app_secret
        if _cache["token"] and time.monotonic() < _cache["expire"]:
            return _cache["token"]
        return _refresh_token_locked()


def invalidate_token_cache():
    """Force the next get_tenant_access_token call to refresh."""
    with _lock:
        _cache["token"] = None
        _cache["expire"] = 0.0


def _text_card(text: str) -> dict:
    return {
        "config": {"wide_screen_mode": True},
        "elements": [{"tag": "div", "text": {"content": text, "tag": "lark_md"}}],
    }


def _api(req: urllib.request.Request) -> dict:
    """Send a Feishu API request. On 99991663 (token expired/invalid),
    invalidate token cache, rebuild Authorization header from a fresh
    token, and retry once."""
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            if data.get("code") == 0:
                return data
            if data.get("code") == 99991663 and attempt == 1 and _cache.get("app_id"):
                with _lock:
                    fresh = _refresh_token_locked()
                req.add_header("Authorization", f"Bearer {fresh}")
                logger.warning("Feishu token expired mid-request; refreshed and retrying")
                continue
            raise RuntimeError(f"Feishu API error: {data}")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            if "99991663" in body and attempt == 1 and _cache.get("app_id"):
                with _lock:
                    fresh = _refresh_token_locked()
                req.add_header("Authorization", f"Bearer {fresh}")
                logger.warning("Feishu token expired mid-request (HTTP 400); refreshed and retrying")
                continue
            raise RuntimeError(f"Feishu HTTP {e.code}: {body}") from e


def send_text_card(open_id: str, text: str, token: str) -> str:
    """Send an interactive card with plain text. Returns message_id."""
    return send_interactive_card(open_id, _text_card(text), token)


def send_interactive_card(open_id: str, card: dict, token: str) -> str:
    """Send an arbitrary interactive card to a user. Returns message_id."""
    body = json.dumps({
        "receive_id": open_id,
        "msg_type": "interactive",
        "content": json.dumps(card, ensure_ascii=False),
    }).encode()
    req = urllib.request.Request(
        f"{FEISHU_BASE}/im/v1/messages?receive_id_type=open_id",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    data = _api(req)
    return data["data"]["message_id"]


def send_text_card_to_chat(chat_id: str, text: str, token: str) -> str:
    """Send an interactive card with plain text to a group chat. Returns message_id."""
    return send_interactive_card_to_chat(chat_id, _text_card(text), token)


def send_interactive_card_to_chat(chat_id: str, card: dict, token: str) -> str:
    """Send an arbitrary interactive card to a group chat. Returns message_id."""
    body = json.dumps({
        "receive_id": chat_id,
        "msg_type": "interactive",
        "content": json.dumps(card, ensure_ascii=False),
    }).encode()
    req = urllib.request.Request(
        f"{FEISHU_BASE}/im/v1/messages?receive_id_type=chat_id",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    data = _api(req)
    return data["data"]["message_id"]


def send_text_message(open_id: str, text: str, token: str) -> str:
    """Send a plain text message to a user. Returns message_id."""
    body = json.dumps({
        "receive_id": open_id,
        "msg_type": "text",
        "content": json.dumps({"text": text}),
    }).encode()
    req = urllib.request.Request(
        f"{FEISHU_BASE}/im/v1/messages?receive_id_type=open_id",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    data = _api(req)
    return data["data"]["message_id"]


def list_p2p_messages(chat_id: str, start_time: str, token: str,
                      page_size: int = 10) -> list[dict]:
    """List recent messages in a P2P chat since start_time (epoch seconds string).

    Returns a list of raw message dicts from the Feishu API.
    """
    params = (
        f"container_id_type=chat&container_id={chat_id}"
        f"&start_time={start_time}&sort_type=ByCreateTimeAsc&page_size={page_size}"
    )
    req = urllib.request.Request(
        f"{FEISHU_BASE}/im/v1/messages?{params}",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        data = _api(req)
        return data.get("data", {}).get("items", [])
    except Exception as e:
        logger.warning(f"list_p2p_messages failed: {e}")
        return []


def list_chat_messages_around(chat_id: str, token: str, page_size: int = 20) -> list[dict]:
    """List recent messages in a chat, oldest first.

    Used only as a best-effort group-context prefetch before handing an @mention
    to the agent. Returns an empty list on any API failure.
    """
    params = (
        f"container_id_type=chat&container_id={chat_id}"
        f"&sort_type=ByCreateTimeDesc&page_size={page_size}"
    )
    req = urllib.request.Request(
        f"{FEISHU_BASE}/im/v1/messages?{params}",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        data = _api(req)
        items = data.get("data", {}).get("items", [])
        return list(reversed(items))
    except Exception as e:
        logger.warning(f"list_chat_messages_around failed: {e}")
        return []


def reply_card_in_thread(parent_message_id: str, text: str, token: str) -> str:
    """以 thread reply 方式发送交互卡片（reply_in_thread=true）。返回新消息的 message_id。"""
    return reply_interactive_card_in_thread(parent_message_id, _text_card(text), token)


def reply_interactive_card_in_thread(parent_message_id: str, card: dict, token: str) -> str:
    """Reply with an arbitrary interactive card in the current thread."""
    body = json.dumps({
        "content": json.dumps(card, ensure_ascii=False),
        "msg_type": "interactive",
        "reply_in_thread": True,
    }).encode()
    req = urllib.request.Request(
        f"{FEISHU_BASE}/im/v1/messages/{parent_message_id}/reply",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    data = _api(req)
    return data["data"]["message_id"]


def get_message(message_id: str, token: str) -> dict:
    """Get a single message by ID. Returns the message dict or {} on failure."""
    req = urllib.request.Request(
        f"{FEISHU_BASE}/im/v1/messages/{message_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        data = _api(req)
        items = data.get("data", {}).get("items", [])
        return items[0] if items else {}
    except Exception as e:
        logger.warning(f"get_message {message_id} failed: {e}")
        return {}


def list_thread_messages(thread_id: str, token: str, page_size: int = 30) -> list[dict]:
    """List messages in a Feishu thread, oldest first."""
    params = (
        f"container_id_type=thread&container_id={thread_id}"
        f"&sort_type=ByCreateTimeAsc&page_size={page_size}"
    )
    req = urllib.request.Request(
        f"{FEISHU_BASE}/im/v1/messages?{params}",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        data = _api(req)
        return data.get("data", {}).get("items", [])
    except Exception as e:
        logger.warning(f"list_thread_messages {thread_id} failed: {e}")
        return []


def get_chat_info(chat_id: str, token: str) -> dict:
    """Get group chat info. Returns dict with at least 'name', or {} on failure."""
    req = urllib.request.Request(
        f"{FEISHU_BASE}/im/v1/chats/{chat_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        data = _api(req)
        return data.get("data", {})
    except Exception as e:
        logger.warning(f"get_chat_info {chat_id} failed: {e}")
        return {}


def get_user_email(open_id: str, token: str) -> str:
    """Return the work email for a Feishu user, or empty string on failure.

    Retries transient failures (timeout/network) with backoff. A swallowed
    transient error here otherwise propagates downstream as "user has no key",
    wrongly telling the user to go apply for one. A successful call that returns
    no email is NOT retried — empty is a valid answer there.
    """
    _attempts = 3
    for _attempt in range(1, _attempts + 1):
        try:
            req = urllib.request.Request(
                f"{FEISHU_BASE}/contact/v3/users/{open_id}?user_id_type=open_id",
                headers={"Authorization": f"Bearer {token}"},
            )
            data = _api(req)
            return data.get("data", {}).get("user", {}).get("enterprise_email", "") \
                or data.get("data", {}).get("user", {}).get("email", "")
        except Exception as e:
            if _attempt < _attempts:
                logger.warning("get_user_email failed (attempt %d/%d) for %s: %s; retrying",
                               _attempt, _attempts, open_id, e)
                time.sleep(0.5 * _attempt)
                continue
            logger.warning("get_user_email failed after %d attempts for %s: %s",
                           _attempts, open_id, e)
            raise


def get_user_display_name(open_id: str, token: str) -> str:
    """Return a human label for the user. Prefers Feishu's `name` field; if the
    bot app lacks contact:user.base:readonly scope (so `name` is absent),
    falls back to a title-cased email prefix (e.g. wei.dai → Wei Dai)."""
    try:
        req = urllib.request.Request(
            f"{FEISHU_BASE}/contact/v3/users/{open_id}?user_id_type=open_id",
            headers={"Authorization": f"Bearer {token}"},
        )
        data = _api(req)
        user = data.get("data", {}).get("user", {}) or {}
        name = user.get("name") or ""
        if name:
            return name
        email = user.get("enterprise_email") or user.get("email") or ""
        if "@" in email:
            local = email.split("@", 1)[0]
            # wei.dai → Wei Dai ; johnsmith → Johnsmith
            return " ".join(part.capitalize() for part in local.split(".") if part)
        return ""
    except Exception:
        return ""


def download_image(message_id: str, image_key: str, token: str, save_path: str) -> None:
    """Download an image resource from a Feishu message and save to disk."""
    req = urllib.request.Request(
        f"{FEISHU_BASE}/im/v1/messages/{message_id}/resources/{image_key}?type=image",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        with open(save_path, "wb") as f:
            f.write(resp.read())


def download_file(message_id: str, file_key: str, token: str, save_path: str) -> None:
    """Download a file resource (pdf/docx/xlsx/etc.) from a Feishu message and save to disk."""
    req = urllib.request.Request(
        f"{FEISHU_BASE}/im/v1/messages/{message_id}/resources/{file_key}?type=file",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        with open(save_path, "wb") as f:
            f.write(resp.read())


def update_card_text(message_id: str, text: str, token: str) -> None:
    """Update an existing interactive card's text content in place."""
    update_interactive_card(message_id, _text_card(text), token)
    logger.debug(f"Card updated: {message_id} ({len(text)} chars)")


def update_interactive_card(message_id: str, card: dict, token: str, sequence: int = 0) -> None:
    """Update an existing interactive card message in place."""
    body = json.dumps({"content": json.dumps(card, ensure_ascii=False)}).encode()
    req = urllib.request.Request(
        f"{FEISHU_BASE}/im/v1/messages/{message_id}",
        data=body,
        method="PATCH",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    _api(req)
    logger.debug(f"Interactive card updated: {message_id} (sequence={sequence})")


def update_interactive_card_by_token(callback_token: str, card: dict, token: str, sequence: int = 0) -> None:
    """Delay-update an interactive card after a card action callback."""
    body = json.dumps({"token": callback_token, "card": card}, ensure_ascii=False).encode()
    req = urllib.request.Request(
        f"{FEISHU_BASE}/interactive/v1/card/update",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    _api(req)
    logger.info("Interactive card updated by callback token (sequence=%s)", sequence)
