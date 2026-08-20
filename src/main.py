# src/main.py
import datetime
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor

from src.agent import Agent, IntermediateText, StreamResult, ToolProgress
from src.audit_store import AuditStore
from src.auth import AuthManager
from src.card_action_listener import InteractiveFormHandler, start_card_action_listener
from src.card_forms import build_followup_prompt
from src.config import Config
from src.event_listener import EventListener, _is_bot_mentioned
from src import feishu_api
from src.form_store import FormStore
from src.interactive_form_service import InteractiveFormService
from src.job_store import JobStore
from src.lark_runner import TokenExpiredError
from src.oa_api import get_user_personal_key
from src.scheduler import SchedulerThread
from src.user_store import UserStore

logger = logging.getLogger(__name__)

_startup_ms = int(time.time() * 1000)   # bot start time in Feishu epoch (ms)
_known_chat_ids: set[str] = set()       # P2P-only chat IDs tracked for poll recovery
_last_event_epoch: str = ""             # epoch seconds of last received event (for recovery)

# Minimum new characters before pushing a streaming update to Feishu
_STREAM_UPDATE_THRESHOLD = 20

# Hardcoded admin open_ids — can view all users' usage
_ADMIN_OPEN_IDS: set[str] = {
    "ou_0fcb9d39f3565a5e5c7a57c302731a35",  # davidyamado
}

# Auth polling config
_AUTH_POLL_INTERVAL = 1    # seconds between auth status checks
_AUTH_POLL_TIMEOUT = 300   # seconds before giving up (5 minutes)

# Backpressure: max concurrent Claude sessions
_CLAUDE_MAX_CONCURRENT = 10
_claude_semaphore = threading.Semaphore(_CLAUDE_MAX_CONCURRENT)

# Internal API server (set by main(), used by _stream_claude to create per-session tokens)
_api_port: int = 0
_api_registry = None  # TokenRegistry instance
_egress_proxy_port: int = 0  # local egress proxy for Claude subprocesses (0 = disabled)
_audit_store: "AuditStore | None" = None  # shared audit log; set in main()

# Pod-start token validation: verify lark-cli token once per user per pod lifetime.
# Users authorized before this pod started may have stale/missing tokens (e.g. after
# a PVC change). We check is_authenticated() on their first message and reset_auth()
# if the token is gone — this triggers a clean re-auth rather than silently failing
# inside Claude with a broken lark_reauth_cli.py.
_pod_start_time = datetime.datetime.now(datetime.timezone.utc)
_pod_verified_users: set[str] = set()
# context_ids that have any active auth poller on this pod (all types).
# Used to prevent duplicate recovery pollers when a follow-up message
# arrives while the original poller is still running on the same pod.
_active_auth_pollers: set[str] = set()
_unmatched_group_log_at: dict[str, float] = {}


def _extract_file_text(path: str, file_name: str) -> str:
    """Extract plain text from a PDF, Word, or Excel file."""
    ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
    if ext == "pdf":
        import pypdf
        reader = pypdf.PdfReader(path)
        parts = []
        for page in reader.pages:
            t = page.extract_text()
            if t:
                parts.append(t)
        return "\n\n".join(parts)
    if ext in ("doc", "docx"):
        import docx
        doc = docx.Document(path)
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    if ext in ("xls", "xlsx"):
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        parts = []
        for sheet in wb.worksheets:
            parts.append(f"[Sheet: {sheet.title}]")
            for row in sheet.iter_rows(values_only=True):
                cells = [str(c) if c is not None else "" for c in row]
                if any(cells):
                    parts.append("\t".join(cells))
        return "\n".join(parts)
    if ext in ("txt", "md"):
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    raise ValueError(f"Unsupported file type: {ext}")


def _display_name(open_id: str, store: "UserStore | None" = None) -> str:
    """Return cached display name for open_id, falling back to the open_id itself."""
    if store:
        try:
            name = store.get_display_name(open_id)
            if isinstance(name, str) and name:
                return name
        except Exception:
            pass
    return open_id


def _strip_leading_at_mention(text: str) -> str:
    """Remove a visible leading @mention token when Feishu omits mention metadata."""
    stripped = (text or "").strip()
    if not stripped.startswith("@"):
        return stripped
    parts = stripped.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


def _log_unmatched_group_message(chat_id: str, msg_id: str, open_id: str, text: str) -> None:
    now = time.monotonic()
    key = chat_id or "unknown"
    last = _unmatched_group_log_at.get(key, 0.0)
    if now - last < 60:
        return
    _unmatched_group_log_at[key] = now
    logger.info(
        "Ignored unmatched group message: chat_id=%s message_id=%s open_id=%s "
        "text_len=%d starts_at=%s",
        chat_id,
        msg_id,
        open_id,
        len(text or ""),
        str((text or "").strip().startswith("@")).lower(),
    )


def send_feishu_message(open_id: str, text: str,
                        app_id: str | None = None,
                        app_secret: str | None = None) -> str | None:
    """Send a text message via bot. Returns message_id or None on failure."""
    try:
        _app_id = app_id or os.environ.get("FEISHU_APP_ID", "")
        _app_secret = app_secret or os.environ.get("FEISHU_APP_SECRET", "")
        token = feishu_api.get_tenant_access_token(_app_id, _app_secret)
        return feishu_api.send_text_message(open_id, text, token)
    except Exception as e:
        logger.error(f"Failed to send message to {open_id}: {e}")
    return None


def _format_tool_status(tool: ToolProgress) -> str:
    """Format a ToolProgress event into a user-friendly Chinese status line."""
    tool_labels = {
        "Bash": "执行命令",
        "Read": "读取文件",
        "Write": "写入文件",
        "Edit": "编辑文件",
        "Glob": "搜索文件",
        "Grep": "搜索内容",
        "WebSearch": "搜索网络",
        "WebFetch": "获取网页",
        "Agent": "调用子任务",
        "TodoWrite": "更新任务列表",
        "NotebookEdit": "编辑笔记本",
    }
    label = tool_labels.get(tool.tool_name, f"使用 {tool.tool_name}")
    desc = tool.tool_input or ""

    # Per-tool formatting: avoid dumping raw English commands/paths
    name = tool.tool_name
    if name == "Bash":
        hint = desc[:60] + ("…" if len(desc) > 60 else "") if desc else ""
        return f"{label}: {hint}" if hint else f"{label}…"
    if name in ("Read", "Write", "Edit", "NotebookEdit"):
        filename = desc.split("/")[-1].split("\\")[-1]
        # Strip internal _file_{8-hex-char}_ prefix injected for attachment handling
        import re as _re2
        m2 = _re2.match(r"_file_[0-9a-f]{8}_(.+)", filename)
        if m2:
            filename = m2.group(1).removesuffix(".md")
        hint = filename[:40] if filename else ""
        return f"{label}: {hint}…" if hint else f"{label}…"
    if name in ("Glob", "Grep"):
        hint = desc[:30] + ("…" if len(desc) > 30 else "") if desc else ""
        return f"{label}: {hint}" if hint else f"{label}…"
    if name in ("WebSearch",):
        hint = desc[:40] + ("…" if len(desc) > 40 else "") if desc else ""
        return f"{label}: {hint}" if hint else f"{label}…"
    if name in ("WebFetch",):
        import re as _re
        m = _re.match(r"https?://([^/]+)", desc)
        hint = m.group(1) if m else desc[:40]
        return f"{label}: {hint}…" if hint else f"{label}…"

    return f"{label}…"


# Rotating status hints shown during long tool execution waits
_PROGRESS_HINTS = [
    "⏳ 正在处理，请稍候…",
    "⏳ 收集信息中…",
    "⏳ 分析数据中…",
    "⏳ 整理结果中…",
    "⏳ 快好了，再等一下…",
    "⏳ 正在汇总…",
    "⏳ 梳理细节中…",
    "⏳ 咀嚼中…",
    "⏳ 让我酝酿一下…",
    "⏳ 正在吟唱…",
    "⏳ 施法中…",
    "⏳ 别打断我…",
    "⏳ 正在蓄力…",
    "⏳ 快想出来了…",
    "⏳ 摸会儿鱼吧…",
    "⏳ 进入下一关…",
    "⏳ 再来一回合…",
    "⏳ 就差一下了…",
    "⏳ 开会儿小差吧...",
    "⏳ 着色器编译中…",
    "⏳ 消化中…",
]
_PROGRESS_INTERVAL = 4  # seconds between hint rotations
_MAX_TOOL_STEPS = 1     # max tool steps shown in card (sliding window)


def _build_thread_context(root_id: str, token: str) -> str:
    """
    Fetch a group thread's message history and return a formatted context prefix.
    Returns empty string if the thread has no history or the fetch fails.
    """
    try:
        root_msg = feishu_api.get_message(root_id, token)
        thread_id = root_msg.get("thread_id", "")
        if not thread_id:
            return ""
        msgs = feishu_api.list_thread_messages(thread_id, token)
        if not msgs:
            return ""
        lines = ["以下是当前飞书群话题的历史消息，请先了解上下文再回复："]
        for msg in msgs:
            sender_type = msg.get("sender", {}).get("sender_type", "")
            sender_id = msg.get("sender", {}).get("id", "")
            msg_type = msg.get("msg_type", "")
            role = "助手" if sender_type == "app" else f"用户({sender_id})"
            text = ""
            try:
                body = json.loads(msg.get("body", {}).get("content", "{}"))
                if msg_type == "text":
                    text = body.get("text", "").strip()
                elif msg_type == "interactive":
                    for el in body.get("elements", []):
                        if el.get("tag") == "div":
                            text = el.get("text", {}).get("content", "")
                            break
            except (json.JSONDecodeError, AttributeError, KeyError):
                pass
            if text:
                lines.append(f"[{role}]: {text}")
        if len(lines) <= 1:
            return ""
        lines.append("---")
        return "\n".join(lines) + "\n\n"
    except Exception as e:
        logger.warning(f"_build_thread_context failed for root_id={root_id}: {e}")
        return ""


def _message_text_for_context(msg: dict) -> str:
    msg_type = msg.get("msg_type", "")
    try:
        body = json.loads(msg.get("body", {}).get("content", "{}"))
    except (json.JSONDecodeError, AttributeError, TypeError):
        body = {}

    if msg_type == "text":
        return (body.get("text") or "").strip()
    if msg_type == "post":
        parts: list[str] = []
        content = body.get("content", [])
        if isinstance(content, list):
            for para in content:
                if isinstance(para, list):
                    for el in para:
                        if isinstance(el, dict) and el.get("tag") == "text":
                            parts.append(el.get("text", ""))
        return "".join(parts).strip()
    if msg_type == "image":
        return "[图片]"
    if msg_type == "file":
        return f"[文件: {body.get('file_name', '') or body.get('name', '')}]"
    if msg_type == "interactive":
        for el in body.get("elements", []):
            if isinstance(el, dict) and el.get("tag") == "div":
                return el.get("text", {}).get("content", "").strip()
        return "[卡片消息]"
    return f"[{msg_type} 类型消息]"


def _build_recent_group_context(chat_id: str, current_message_id: str, token: str) -> str:
    """
    Fetch recent group-chat messages and return a formatted context prefix.
    Best-effort only; callers should continue without context if it returns empty.
    """
    if not chat_id:
        return ""
    try:
        msgs = feishu_api.list_chat_messages_around(chat_id, token, page_size=20)
        if not msgs:
            return ""

        lines = ["以下是当前飞书群聊最近的上下文，请结合这些消息理解“上面/上一条/刚才”等指代："]
        for msg in msgs:
            text = _message_text_for_context(msg)
            if not text:
                continue
            sender_type = msg.get("sender", {}).get("sender_type", "")
            sender_id = msg.get("sender", {}).get("id", "")
            role = "助手" if sender_type == "app" else f"用户({sender_id})"
            marker = " 当前触发消息" if msg.get("message_id") == current_message_id else ""
            lines.append(f"[{role}{marker}]: {text}")
        if len(lines) <= 1:
            return ""
        lines.append("[群聊上文结束，用户的新消息如下]")
        return "\n".join(lines) + "\n"
    except Exception as e:
        logger.warning(f"_build_recent_group_context failed for chat_id={chat_id}: {e}")
        return ""


def _conversation_lock(store: UserStore, key: str):
    if key and hasattr(store, "conversation_lock"):
        return store.conversation_lock(key)
    return nullcontext()


def _stream_claude(*args, **kwargs) -> str | None:
    open_id = kwargs.get("open_id") if "open_id" in kwargs else args[0]
    store = kwargs.get("store") if "store" in kwargs else args[3]
    context_id = kwargs.get("context_id")
    if context_id is None and len(args) > 7:
        context_id = args[7]
    thread_session_key = kwargs.get("thread_session_key")
    if thread_session_key is None and len(args) > 9:
        thread_session_key = args[9]
    lock_key = thread_session_key or context_id or open_id
    with _conversation_lock(store, lock_key):
        return _stream_claude_inner(*args, **kwargs)


def _stream_claude_inner(open_id: str, user_text: str, agent: Agent, store: UserStore,
                         app_id: str, app_secret: str,
                         existing_msg_id: str | None = None,
                         context_id: str | None = None,
                         reply_msg_id: str | None = None,
                         thread_session_key: str | None = None,
                         root_id: str = "",
                         chat_id: str = "", chat_type: str = "p2p",
                         image_key: str = "", image_message_id: str = "",
                         file_key: str = "", file_name: str = "",
                         parent_id: str = "",
                         oa_api_key: str = "",
                         is_scheduled_task: bool = False) -> str | None:
    """
    Send an interactive card placeholder, then stream Claude Code output
    into it via live card updates — including tool execution progress
    and periodic animated status during long waits.

    Args:
        existing_msg_id: reuse this card instead of sending a new one (e.g. after auth)
        context_id: auth/session isolation key (open_id for P2P, g_{chat_id}_{open_id} for group)
        reply_msg_id: for group chats — the @mention message to create a thread reply on
        thread_session_key: for group chats — key for thread-scoped session lookup/storage
        image_key: Feishu image_key if user sent an image
        image_message_id: message_id of the image message (needed to download via API)
        is_scheduled_task: if True, run with a fresh isolated session — do NOT load or
            persist the user's main conversation session. Prevents scheduler tasks and
            user messages from corrupting each other's session state when they happen
            to run concurrently for the same open_id.
    """
    _ctx = context_id or open_id
    token = feishu_api.get_tenant_access_token(app_id, app_secret)
    _dname = _display_name(open_id, store)

    if existing_msg_id:
        msg_id = existing_msg_id
        logger.debug(f"Reusing existing card {msg_id}")
        try:
            feishu_api.update_card_text(msg_id, "⏳ 思考中…", token)
        except Exception as e:
            logger.warning(f"Failed to reset existing card: {e}")
    elif reply_msg_id:
        msg_id = feishu_api.reply_card_in_thread(reply_msg_id, "⏳ 思考中…", token)
        logger.info(f"Thread reply card sent: {msg_id} ({_display_name(open_id, store)})")
    else:
        msg_id = feishu_api.send_text_card(open_id, "⏳ 思考中…", token)
        logger.info(f"Card sent: {msg_id} ({_display_name(open_id, store)})")

    # Resolve per-user OpenRouter key after creating the placeholder card. Feishu
    # user lookups and OA key lookup can be slow; they should not delay first UI
    # feedback to the user.
    personal_key: str | None = None
    _key_error = False
    if oa_api_key:
        try:
            _email = feishu_api.get_user_email(open_id, token)
            logger.debug(f"[key] {_dname} | oa_api_key=set email={_email!r}")
            if _email:
                personal_key = get_user_personal_key(_email, oa_api_key) or None
                logger.debug(f"[key] {_dname} | personal_key={'set' if personal_key else 'empty'}")
        except Exception as e:
            logger.warning(f"Personal key lookup failed for {_dname}: {e}")
            _key_error = True
    else:
        logger.debug(f"[key] {_dname} | oa_api_key=empty, skipping personal key lookup")

    _shared_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if personal_key:
        logger.debug(f"[key] {_dname} | personal")
    elif _shared_key:
        logger.debug(f"[key] {_dname} | shared")
    else:
        logger.debug(f"[key] {_dname} | none")

    if not personal_key and not _shared_key:
        if _key_error:
            _msg = "key获取异常，请重试或联系开发者。"
        else:
            _msg = ("未获取到您的AI使用key，请前往 "
                    "https://aq.yostar.net/openrouter/my-keys 申请或填写您的key。"
                    "完成后，重新和我对话即可。")
        try:
            feishu_api.update_card_text(msg_id, _msg, token)
        except Exception as e:
            logger.warning(f"No-key card update failed: {e}")
        logger.info(f"[回复] {_dname} | {'key_error' if _key_error else 'no_key'}: {_msg}")
        return msg_id

    # Session: group threads use thread_session_key; P2P uses context_id.
    # Scheduler tasks always start with a fresh session — their state must not
    # collide with the user's main conversation if they run concurrently.
    if is_scheduled_task:
        session_id = None
        logger.info(f"Session: scheduled-task (fresh) ({_display_name(open_id, store)})")
    elif thread_session_key:
        session_id = store.get_thread_session(thread_session_key)
        logger.info(f"Session: {session_id} ({_display_name(open_id, store)}, thread)")
    else:
        session_id = store.get_session_id(_ctx)
        logger.info(f"Session: {session_id} ({_display_name(open_id, store)})")

    # New session in a group thread — inject thread history so Claude has full context
    effective_text = user_text
    thread_ctx_injected = False
    if thread_session_key and session_id is None and root_id:
        logger.info(f"New thread session, fetching history for root_id={root_id}")
        thread_ctx = _build_thread_context(root_id, token)
        if thread_ctx:
            effective_text = thread_ctx + user_text
            thread_ctx_injected = True
            logger.info(f"Thread context injected ({len(thread_ctx)} chars)")

    if chat_type == "group":
        recent_ctx = _build_recent_group_context(chat_id, image_message_id, token)
        if recent_ctx:
            effective_text = recent_ctx + effective_text
            logger.info(f"Recent group context injected ({len(recent_ctx)} chars)")

    # Inject quoted message body when user quote-replied to something.
    # Skip when thread_ctx already injected AND parent_id == root_id (the
    # thread history already includes that message).
    quoted_image_paths: list[str] = []
    if parent_id and not (thread_ctx_injected and parent_id == root_id):
        try:
            quoted = feishu_api.get_message(parent_id, token)
            quoted_body = ""
            quoted_msg_type = quoted.get("msg_type", "")
            quoted_sender = quoted.get("sender", {}).get("id", "")
            try:
                content_obj = json.loads(quoted.get("body", {}).get("content", "{}"))
            except (json.JSONDecodeError, TypeError):
                content_obj = {}
            if quoted_msg_type == "text":
                quoted_body = content_obj.get("text", "")
            elif quoted_msg_type == "post":
                # Flatten post paragraphs; collect any embedded image_keys too
                _parts = []
                _q_imgs = []
                for para in content_obj.get("content", []):
                    if isinstance(para, list):
                        for el in para:
                            if isinstance(el, dict):
                                if el.get("tag") == "text":
                                    _parts.append(el.get("text", ""))
                                elif el.get("tag") in ("img", "image"):
                                    _ikey = el.get("image_key", "")
                                    if _ikey:
                                        _q_imgs.append(_ikey)
                quoted_body = "".join(_parts).strip()
                if _q_imgs:
                    quoted_body += f" [包含 {len(_q_imgs)} 张图片，已下载]"
                # Stash for download below
                content_obj.setdefault("_image_keys", _q_imgs)
            elif quoted_msg_type == "image":
                _ikey = content_obj.get("image_key", "")
                if _ikey:
                    content_obj["_image_keys"] = [_ikey]
                quoted_body = "[图片]"
            else:
                quoted_body = f"[{quoted_msg_type} 类型消息]"

            # Download any image_keys from the quoted message
            _img_keys = content_obj.get("_image_keys", []) if isinstance(content_obj, dict) else []
            if _img_keys:
                import os as _os
                _u_home = agent._ensure_user_home(_ctx)
                for _ikey in _img_keys:
                    _qpath = _os.path.join(_u_home, f"_quoted_img_{_ikey}.jpg")
                    try:
                        feishu_api.download_image(parent_id, _ikey, token, _qpath)
                        quoted_image_paths.append(_qpath)
                        logger.info(f"Downloaded quoted image {_ikey} -> {_qpath}")
                    except Exception as e:
                        logger.warning(f"Could not download quoted image {_ikey}: {e}")

            if quoted_body or quoted_image_paths:
                _hdr = (
                    f"[用户引用了消息 {parent_id}，发送者 open_id={quoted_sender}，"
                    f"类型={quoted_msg_type}，内容如下]\n"
                )
                if quoted_image_paths:
                    _hdr += "（引用消息包含图片，下面会单独附加）\n"
                effective_text = (
                    _hdr + (quoted_body or "[无文本]") + "\n"
                    f"[引用结束，用户的新消息如下]\n{effective_text}"
                )
                logger.info(
                    f"Quoted message injected (parent_id={parent_id}, type={quoted_msg_type}, "
                    f"text={len(quoted_body)} chars, images={len(quoted_image_paths)})"
                )
        except Exception as e:
            logger.warning(f"Could not fetch quoted message {parent_id}: {e}")

    buffer: list[str] = []
    tool_steps: list[str] = []
    chars_at_last_update = 0
    result: StreamResult | None = None

    # Download image/file attachments to user home so Claude Code can read them
    image_paths: list[str] = list(quoted_image_paths)  # carry over quoted images
    _tmp_image: str | None = None
    _tmp_file: str | None = None
    import os as _os
    user_home = agent._ensure_user_home(_ctx)
    if image_key and image_message_id:
        _tmp_image = _os.path.join(user_home, f"_img_{image_key}.jpg")
        try:
            feishu_api.download_image(image_message_id, image_key, token, _tmp_image)
            image_paths.append(_tmp_image)
            logger.info(f"Downloaded image {image_key} to {_tmp_image}")
        except Exception as e:
            logger.warning(f"Could not download image {image_key}: {e}")
            _tmp_image = None
    if file_key and file_name and image_message_id:
        import hashlib as _hashlib
        safe_name = _os.path.basename(file_name)
        short_id = _hashlib.md5(file_key.encode()).hexdigest()[:8]
        _tmp_file = _os.path.join(user_home, f"_file_{short_id}_{safe_name}")
        try:
            feishu_api.download_file(image_message_id, file_key, token, _tmp_file)
            logger.info(f"Downloaded file {file_name} to {_tmp_file}")
        except Exception as e:
            logger.warning(f"Could not download file {file_key}: {e}")
            _tmp_file = None
    if _tmp_file:
        _txt_file = _tmp_file + ".md"
        try:
            _extracted = _extract_file_text(_tmp_file, file_name)
            with open(_txt_file, "w", encoding="utf-8") as _fh:
                _fh.write(_extracted)
            effective_text += f"\n\n[附件文件 {file_name}，已提取文本内容，请使用 Read 工具读取此路径: {_txt_file}]"
            logger.info(f"Extracted {len(_extracted)} chars from {file_name} to {_txt_file}")
        except Exception as e:
            logger.warning(f"Could not extract text from {file_name}: {e}")
            _txt_file = None

    # --- Animated progress ticker ---
    # Start only after key access is confirmed; otherwise background updates can
    # overwrite the no-key application prompt.
    _stop_ticker = threading.Event()
    _ticker_lock = threading.Lock()
    _current_hint: list[str] = [_PROGRESS_HINTS[0]]  # mutable box
    _committed_text: list[str] = [""]  # accumulated intermediate text (non-streaming turns)

    def _progress_ticker():
        """Periodically update card with random hints during long waits."""
        import random
        _shuffled = _PROGRESS_HINTS[:]
        random.shuffle(_shuffled)
        idx = 0
        while not _stop_ticker.wait(timeout=_PROGRESS_INTERVAL):
            with _ticker_lock:
                if idx >= len(_shuffled):
                    random.shuffle(_shuffled)
                    idx = 0
                hint = _shuffled[idx]
                _current_hint[0] = hint
                parts = []
                if _committed_text[0]:
                    parts.append(_committed_text[0])
                parts.extend(tool_steps[-_MAX_TOOL_STEPS:])
                parts.append(hint)
                card_content = "\n".join(parts)
            try:
                feishu_api.update_card_text(msg_id, card_content, token)
            except Exception as e:
                logger.debug(f"Ticker card update failed: {e}")
            idx += 1

    ticker_thread = threading.Thread(target=_progress_ticker, daemon=True)
    ticker_thread.start()

    logger.debug(f"Starting stream_chat for {open_id} (ctx={_ctx[:40]})")
    _form_metadata = {
        "operator_open_id": open_id,
        "chat_id": chat_id,
        "chat_type": chat_type,
        "reply_msg_id": reply_msg_id or "",
        "thread_session_key": thread_session_key or "",
        "root_id": root_id or "",
        "message_id": image_message_id or "",
        "original_text": user_text,
    }
    _session_token = _api_registry.create(_ctx, metadata=_form_metadata) if _api_registry else ""
    try:
        for chunk in agent.stream_chat(open_id, effective_text, session_id=session_id,
                                       home_key=_ctx, chat_id=chat_id, chat_type=chat_type,
                                       image_paths=image_paths or None,
                                       api_key=personal_key,
                                       display_name=_dname,
                                       api_port=_api_port, api_token=_session_token,
                                       egress_proxy_port=_egress_proxy_port,
                                       is_scheduled_task=is_scheduled_task):
            if isinstance(chunk, StreamResult):
                result = chunk
                logger.debug(f"StreamResult: session={result.session_id[:12]}... error={result.is_error} cost={result.cost_usd}")
                break

            if isinstance(chunk, ToolProgress):
                status = _format_tool_status(chunk)
                with _ticker_lock:
                    tool_steps.append(status)
                    parts = []
                    if _committed_text[0]:
                        parts.append(_committed_text[0])
                    parts.extend(tool_steps[-_MAX_TOOL_STEPS:])
                    parts.append(_current_hint[0])
                    card_content = "\n".join(parts)
                logger.debug(f"Tool progress: {status}")
                try:
                    feishu_api.update_card_text(msg_id, card_content, token)
                except Exception as e:
                    logger.warning(f"Tool progress card update failed: {e}")
                continue

            if isinstance(chunk, IntermediateText):
                with _ticker_lock:
                    if _committed_text[0]:
                        _committed_text[0] += "\n" + chunk.text
                    else:
                        _committed_text[0] = chunk.text
                    parts = [_committed_text[0]]
                    parts.extend(tool_steps[-_MAX_TOOL_STEPS:])
                    parts.append(_current_hint[0])
                    card_content = "\n".join(parts)
                logger.debug(f"IntermediateText received ({len(chunk.text)} chars)")
                try:
                    feishu_api.update_card_text(msg_id, card_content, token)
                except Exception as e:
                    logger.warning(f"IntermediateText card update failed: {e}")
                continue

            # Text chunk — stop the ticker, we have real content now
            _stop_ticker.set()
            buffer.append(chunk)
            total = sum(len(c) for c in buffer)
            logger.debug(f"Chunk received ({len(chunk)} chars), total={total}")
            if total - chars_at_last_update >= _STREAM_UPDATE_THRESHOLD:
                try:
                    feishu_api.update_card_text(msg_id, "".join(buffer), token)
                except Exception as e:
                    logger.warning(f"Card update failed: {e}")
                chars_at_last_update = total
    finally:
        _stop_ticker.set()  # ensure ticker stops
        ticker_thread.join(timeout=2)  # wait for any in-flight ticker API call to finish
        if _session_token and _api_registry:
            _api_registry.revoke(_session_token)

    # Final card update with complete text.
    # Fall back to committed intermediate text if full_text is empty (e.g. when the
    # entire response was delivered via IntermediateText chunks and full_text wasn't
    # populated), so we can still strip the progress hint from the card.
    final_text = result.full_text if result else "".join(buffer)
    if not final_text and _committed_text[0]:
        final_text = _committed_text[0]
    logger.debug(f"Stream done. final_text length={len(final_text)}")
    conv = f"群聊 {chat_id}" if chat_type == "group" else "私聊"
    _cost = f"cost=${result.cost_usd:.2f} | " if result and result.cost_usd else ""
    _dn_reply = _display_name(open_id, store)
    logger.info(f"[回复] {_dn_reply} | {conv} | {_cost}{final_text[:500]}"
                + (" …(截断)" if len(final_text) > 500 else ""))
    if _audit_store is not None and final_text:
        _audit_store.log_replied(
            open_id=open_id, display_name=_dn_reply,
            chat_type=chat_type, chat_id=chat_id, message_id=msg_id,
            content=final_text,
            cost_usd=result.cost_usd if result and result.cost_usd else None,
            input_tokens=result.input_tokens if result else None,
            output_tokens=result.output_tokens if result else None,
        )
    if final_text:
        try:
            feishu_api.update_card_text(msg_id, final_text, token)
        except Exception as e:
            logger.warning(f"Final card update failed: {e}")

    _temp_files_cleaned = False
    _usage_recorded = False

    def _cleanup_temp_attachments() -> None:
        nonlocal _temp_files_cleaned
        if _temp_files_cleaned:
            return
        _temp_files_cleaned = True
        if _tmp_image:
            try:
                os.remove(_tmp_image)
            except OSError:
                pass
        if _tmp_file:
            try:
                os.remove(_tmp_file)
            except OSError:
                pass
        if _tmp_file and os.path.exists(_tmp_file + ".md"):
            try:
                os.remove(_tmp_file + ".md")
            except OSError:
                pass

    def _record_usage() -> None:
        nonlocal _usage_recorded
        if _usage_recorded or not result:
            return
        _usage_recorded = True
        try:
            from datetime import datetime, timezone
            _year_month = datetime.now(timezone.utc).strftime("%Y-%m")
            _uname = store.get_display_name(open_id) or ""
            if not _uname:
                _usage_token = feishu_api.get_tenant_access_token(app_id, app_secret)
                _uname = feishu_api.get_user_display_name(open_id, _usage_token)
                if _uname:
                    store.set_display_name(open_id, _uname)
            store.add_usage(
                open_id=open_id,
                year_month=_year_month,
                display_name=_uname,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                cache_read_tokens=result.cache_read_tokens,
                cost_usd=result.cost_usd,
                using_personal_key=bool(personal_key),
            )
        except Exception as _e:
            logger.warning(f"Failed to record usage for {open_id}: {_e}")

    # Post-stream security-refusal alert. The system prompt instructs Claude to
    # output the marker phrase below verbatim when it refuses on security
    # grounds; the bot detects it here and notifies the security alert chat.
    # No extra LLM call — detection is purely string matching on the final text.
    if final_text and "本轮对话可能触发安全红线，相关信息已存档" in final_text:
        _alert_chat = os.environ.get("BOT_SECURITY_ALERT_CHAT_ID", "").strip()
        _dname_alert = _display_name(open_id, store)
        # Fall back to a live Feishu lookup if the cache hasn't been populated
        # yet (e.g. first message from a new user — eager-fetch happens later in
        # handle_message, but a refusal can fire on that same first message).
        if _dname_alert == open_id:
            try:
                _live_name = feishu_api.get_user_display_name(open_id, token)
                if _live_name:
                    store.set_display_name(open_id, _live_name)
                    _dname_alert = _live_name
            except Exception:
                pass
        _user_line = _dname_alert if _dname_alert and _dname_alert != open_id else "(未知，飞书侧未取到名字)"
        logger.warning(
            f"[security-refusal] {_dname_alert} | {conv} | "
            f"user_text={user_text[:200]!r}"
        )
        if _alert_chat:
            _alert_card = (
                "**🚨 [BOT 安全告警] AI 拒绝执行请求**\n\n"
                f"**用户**：{_user_line}\n"
                f"**open_id**：`{open_id}`\n"
                f"**聊天**：{conv}\n"
                f"**用户原文**：\n```\n{user_text[:800]}\n```\n"
                f"**AI 判定与回复**：\n```\n{final_text[:600]}\n```"
            )
            try:
                feishu_api.send_text_card_to_chat(_alert_chat, _alert_card, token)
            except Exception as _e:
                logger.warning(f"[security-refusal] alert send failed: {_e}")
        if _audit_store is not None:
            _audit_store.log_security_refusal(
                open_id=open_id, display_name=_dname_alert,
                chat_type=chat_type, chat_id=chat_id, message_id=msg_id,
                user_text=user_text, refusal_text=final_text,
            )

    # Turn limit reached — keep session alive, prompt user to continue
    if result and result.turn_limit_reached:
        cost_str = f"${result.cost_usd:.2f}" if result.cost_usd else ""
        cost_part = f"，本次对话预估已消耗 {cost_str}" if cost_str else ""
        limit_msg = f"抱歉，AI 已达到工具调用上限{cost_part}，回复「继续」可继续对话。"
        logger.info(f"Turn limit reached for {_display_name(open_id, store)}, cost={result.cost_usd:.4f}")
        try:
            feishu_api.update_card_text(msg_id, limit_msg, token)
        except Exception as e:
            logger.warning(f"Turn limit card update failed: {e}")
        # Save session so "继续" can resume it — but never for scheduled tasks,
        # which must remain stateless across runs.
        if result.session_id and not is_scheduled_task:
            if thread_session_key:
                store.set_thread_session(thread_session_key, result.session_id)
                if chat_id and root_id:
                    store.set_thread_session(f"{chat_id}:{root_id}", result.session_id)
            else:
                store.set_session_id(_ctx, result.session_id)
        _cleanup_temp_attachments()
        _record_usage()
        return

    # Persist session_id for conversation continuity.
    # Scheduler tasks skip all session persistence — their session_id is throwaway
    # and writing it back would clobber the user's main conversation session.
    if is_scheduled_task:
        pass
    elif result and result.invalid_session:
        # Stale session ID — clear it so the next message starts a fresh session
        if thread_session_key:
            store.clear_thread_session(thread_session_key)
            if chat_id and root_id:
                store.clear_thread_session(f"{chat_id}:{root_id}")
        else:
            store.set_session_id(_ctx, None)
        logger.warning(f"Cleared invalid session for {_ctx}")
    elif result and result.session_id and not result.is_error:
        # If Claude called session_reset_cli.py during this run, discard the session
        # instead of saving it — the next message will start a completely fresh conversation.
        _user_now = store.get_user(_ctx) or {}
        if _user_now.get("pending_session_reset"):
            store.upsert_user(_ctx, pending_session_reset=0)
            if thread_session_key:
                store.clear_thread_session(thread_session_key)
                if chat_id and root_id:
                    store.clear_thread_session(f"{chat_id}:{root_id}")
            else:
                store.set_session_id(_ctx, None)
            logger.info(f"Session reset applied for {_display_name(open_id, store)} (requested by Claude during run)")
        else:
            if thread_session_key:
                store.set_thread_session(thread_session_key, result.session_id)
                # Also write a shared thread-activity marker so other users in the same
                # thread can participate without needing to @mention the bot.
                if chat_id and root_id:
                    store.set_thread_session(f"{chat_id}:{root_id}", result.session_id)
            else:
                store.set_session_id(_ctx, result.session_id)

    if result and result.is_error:
        logger.error(f"Claude Code error for {_display_name(open_id, store)}: {final_text[:200]}")
        if _audit_store is not None:
            _audit_store.log_claude_error(
                open_id=open_id, display_name=_display_name(open_id, store),
                chat_type=chat_type, chat_id=chat_id, message_id=msg_id,
                error_text=final_text,
            )

    # Clean up temp image/file attachments
    _cleanup_temp_attachments()

    # Record token usage — non-fatal, never affects main flow
    _record_usage()

    return msg_id


def _make_scheduled_task_runner(agent: Agent, store: UserStore,
                                app_id: str, app_secret: str,
                                oa_api_key: str = ""):
    """
    Return a callable suitable for SchedulerThread.stream_claude_fn.
    Signature: fn(open_id, prompt, chat_id) -> None
    Sends the result as a new card in the user's P2P chat (chat_id).
    """
    def _run(open_id: str, prompt: str, chat_id: str) -> None:
        _stream_claude(
            open_id=open_id,
            user_text=prompt,
            agent=agent,
            store=store,
            app_id=app_id,
            app_secret=app_secret,
            chat_id=chat_id,
            chat_type="p2p",
            oa_api_key=oa_api_key,
            is_scheduled_task=True,
        )
    return _run


def _make_form_completion_runner(agent: Agent, store: UserStore,
                                 app_id: str, app_secret: str,
                                 executor: ThreadPoolExecutor | None = None,
                                 oa_api_key: str = "",
                                 form_store=None,
                                 auth: AuthManager | None = None,
                                 auth_executor: ThreadPoolExecutor | None = None):
    """Return a callback for completed interactive form sessions."""
    def _run(session: dict) -> None:
        try:
            prompt = build_followup_prompt(
                original_text=session.get("original_text", ""),
                schema=session["schema"],
                answers=session.get("answers") or {},
            )
            context_id = session.get("context_id") or session["operator_open_id"]
            before = store.get_user(context_id) or {}
            lark_pending_at_before = before.get("pending_at", "")
            meegle_pending_at_before = before.get("meegle_pending_at", "")
            card_id = _stream_claude(
                open_id=session["operator_open_id"],
                user_text=prompt,
                agent=agent,
                store=store,
                app_id=app_id,
                app_secret=app_secret,
                context_id=context_id,
                reply_msg_id=session.get("reply_msg_id") or None,
                thread_session_key=session.get("thread_session_key") or None,
                root_id=session.get("root_id", ""),
                chat_id=session.get("chat_id", ""),
                chat_type=session.get("chat_type", "p2p"),
                oa_api_key=oa_api_key,
            )
            after = store.get_user(context_id) or {}
            lark_code = after.get("pending_code", "")
            meegle_code = after.get("meegle_pending_code", "")
            meegle_client = after.get("meegle_pending_client_id", "")
            if auth is not None and auth_executor is not None:
                need_lark_poller = (
                    after.get("auth_status") == "pending"
                    and after.get("pending_at", "") != lark_pending_at_before
                    and lark_code
                )
                need_meegle_poller = (
                    after.get("meegle_auth_status") == "pending"
                    and after.get("meegle_pending_at", "") != meegle_pending_at_before
                    and meegle_code and meegle_client
                )
                if need_lark_poller and need_meegle_poller:
                    _create_auth_resume_job(
                        store,
                        context_id=context_id,
                        provider="lark",
                        device_code=lark_code,
                        resume_text=prompt,
                        reply_id=session.get("reply_msg_id") or "",
                        thread_key=session.get("thread_session_key") or "",
                        root_id=session.get("root_id", ""),
                        chat_id=session.get("chat_id", ""),
                        chat_type=session.get("chat_type", "p2p"),
                        existing_msg_id=card_id or "",
                    )
                    _create_auth_resume_job(
                        store,
                        context_id=context_id,
                        provider="meegle",
                        device_code=meegle_code,
                        client_id=meegle_client,
                        resume_text=prompt,
                        reply_id=session.get("reply_msg_id") or "",
                        thread_key=session.get("thread_session_key") or "",
                        root_id=session.get("root_id", ""),
                        chat_id=session.get("chat_id", ""),
                        chat_type=session.get("chat_type", "p2p"),
                        existing_msg_id=card_id or "",
                    )
                    auth_executor.submit(
                        _start_combined_auth_and_poll,
                        session["operator_open_id"], prompt,
                        lark_code, after.get("pending_url", ""),
                        meegle_client, meegle_code, after.get("meegle_pending_url", ""),
                        auth, store, agent, app_id, app_secret,
                        context_id, session.get("reply_msg_id") or None,
                        session.get("thread_session_key") or None,
                        session.get("root_id", ""),
                        session.get("chat_id", ""),
                        session.get("chat_type", "p2p"),
                    )
                elif need_lark_poller:
                    _create_auth_resume_job(
                        store,
                        context_id=context_id,
                        provider="lark",
                        device_code=lark_code,
                        resume_text=prompt,
                        reply_id=session.get("reply_msg_id") or "",
                        thread_key=session.get("thread_session_key") or "",
                        root_id=session.get("root_id", ""),
                        chat_id=session.get("chat_id", ""),
                        chat_type=session.get("chat_type", "p2p"),
                        existing_msg_id=card_id or "",
                    )
                    auth_executor.submit(
                        _poll_lark_reauth_and_resume,
                        session["operator_open_id"], prompt, lark_code,
                        auth, store, agent, app_id, app_secret,
                        context_id, session.get("reply_msg_id") or None,
                        session.get("thread_session_key") or None,
                        session.get("root_id", ""),
                        session.get("chat_id", ""),
                        session.get("chat_type", "p2p"),
                        card_id,
                    )
                elif need_meegle_poller:
                    _create_auth_resume_job(
                        store,
                        context_id=context_id,
                        provider="meegle",
                        device_code=meegle_code,
                        client_id=meegle_client,
                        resume_text=prompt,
                        reply_id=session.get("reply_msg_id") or "",
                        thread_key=session.get("thread_session_key") or "",
                        root_id=session.get("root_id", ""),
                        chat_id=session.get("chat_id", ""),
                        chat_type=session.get("chat_type", "p2p"),
                        existing_msg_id=card_id or "",
                    )
                    auth_executor.submit(
                        _poll_meegle_and_resume,
                        session["operator_open_id"], prompt, meegle_client, meegle_code,
                        auth, store, agent, app_id, app_secret,
                        context_id, session.get("reply_msg_id") or None,
                        session.get("thread_session_key") or None,
                        session.get("root_id", ""),
                        session.get("chat_id", ""),
                        session.get("chat_type", "p2p"),
                        card_id,
                    )
            if form_store is not None:
                form_store.mark_completed(session["id"])
        except Exception:
            logger.exception(f"Interactive form completion failed: {session.get('id', '')}")
            if form_store is not None:
                try:
                    form_store.mark_failed(session["id"])
                except Exception:
                    logger.warning("Could not mark form session failed", exc_info=True)

    def _callback(session: dict) -> None:
        if executor is not None:
            executor.submit(_run, session)
        else:
            _run(session)

    return _callback


def _form_card_update_delay_seconds() -> float:
    raw = os.environ.get("BOT_FORM_CARD_UPDATE_DELAY_SECONDS", "0").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        logger.warning("Invalid BOT_FORM_CARD_UPDATE_DELAY_SECONDS=%r; using 0", raw)
        return 0.0


def _form_callback_token_update_delay_seconds() -> float:
    raw = os.environ.get("BOT_FORM_CALLBACK_TOKEN_UPDATE_DELAY_SECONDS", "0.3").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        logger.warning("Invalid BOT_FORM_CALLBACK_TOKEN_UPDATE_DELAY_SECONDS=%r; using 0.3", raw)
        return 0.3


def _form_defer_card_updates() -> bool:
    raw = os.environ.get("BOT_FORM_CARD_UPDATE_MODE", "deferred").strip().lower()
    if raw in ("sync", "synchronous", "callback"):
        return False
    if raw not in ("deferred", "async", "asynchronous", ""):
        logger.warning("Invalid BOT_FORM_CARD_UPDATE_MODE=%r; using deferred", raw)
    return True


def _make_form_card_update_runner(form_store, feishu_api_module, app_id: str, app_secret: str,
                                  executor: ThreadPoolExecutor | None = None,
                                  delay_seconds: float | None = None):
    """Return a callback that updates form cards outside the SDK action callback."""
    def _run(session: dict, card: dict, callback_token: str = "") -> None:
        card_id = session.get("card_id") or session.get("message_id") or ""
        if not card_id and not callback_token:
            logger.warning("Interactive form card update skipped: missing card_id for session=%s", session.get("id"))
            return
        try:
            effective_delay = _form_card_update_delay_seconds() if delay_seconds is None else delay_seconds
            if callback_token and delay_seconds is None:
                effective_delay = max(effective_delay, _form_callback_token_update_delay_seconds())
            if effective_delay > 0:
                time.sleep(effective_delay)
            token = feishu_api_module.get_tenant_access_token(app_id, app_secret)
            sequence = form_store.next_card_sequence(session["id"])
            logger.info(
                "Updating form card asynchronously: session=%s card_id=%s current_question=%s sequence=%s update_method=%s",
                session.get("id"),
                card_id,
                session.get("current_index"),
                sequence,
                "callback_token" if callback_token else "message_patch",
            )
            if callback_token:
                feishu_api_module.update_interactive_card_by_token(callback_token, card, token, sequence=sequence)
            else:
                feishu_api_module.update_interactive_card(card_id, card, token, sequence=sequence)
        except Exception:
            logger.exception("Interactive form card update failed: session=%s", session.get("id"))

    def _callback(session: dict, card: dict, callback_token: str = "") -> None:
        if executor is not None:
            executor.submit(_run, session, card, callback_token)
        else:
            _run(session, card, callback_token)

    return _callback


def _auth_resume_owner() -> str:
    return f"{os.environ.get('HOSTNAME', '')}:{os.getpid()}"


def _create_auth_resume_job(
        store: UserStore,
        *,
        context_id: str,
        provider: str,
        device_code: str,
        client_id: str = "",
        resume_text: str,
        reply_id: str | None = None,
        thread_key: str | None = None,
        root_id: str = "",
        chat_id: str = "",
        chat_type: str = "p2p",
        existing_msg_id: str = "") -> str:
    job_id = store.create_auth_resume_job(
        context_id=context_id,
        provider=provider,
        device_code=device_code,
        client_id=client_id,
        resume_text=resume_text,
        reply_id=reply_id or "",
        thread_key=thread_key or "",
        root_id=root_id or "",
        chat_id=chat_id or "",
        chat_type=chat_type or "p2p",
        existing_msg_id=existing_msg_id or "",
    )
    return job_id


def _set_auth_resume_card_id(
        store: UserStore,
        *,
        context_id: str,
        provider: str,
        device_code: str,
        card_id: str | None) -> None:
    if not card_id:
        return
    try:
        store.set_auth_resume_existing_msg_id(context_id, provider, device_code, card_id)
    except Exception as e:
        logger.warning("[auth-resume] could not update card id provider=%s ctx=%s: %s",
                       provider, context_id, e)


def _notify_auth_resume_missing(open_id: str, card_id: str | None,
                                app_id: str, app_secret: str) -> None:
    msg = "授权已完成，但这次请求的恢复状态已过期。请重新发送刚才的请求。"
    try:
        token = feishu_api.get_tenant_access_token(app_id, app_secret)
        if card_id:
            feishu_api.update_card_text(card_id, msg, token)
        else:
            feishu_api.send_text_card(open_id, msg, token)
    except Exception as e:
        logger.warning("[auth-resume] could not notify missing resume job: %s", e)


def _group_label(chat_id: str, token: str) -> str:
    if not chat_id:
        return "当前群聊"
    try:
        info = feishu_api.get_chat_info(chat_id, token)
        name = (info.get("name") or info.get("chat_name") or "").strip()
        if name:
            return name
    except Exception as e:
        logger.warning("Could not resolve group label for %s: %s", chat_id, e)
    return "当前群聊"


def _group_lark_auth_waiting_text(open_id: str, store: UserStore | None) -> str:
    name = _display_name(open_id, store)
    return (
        f"正在等待获取 {name} 的飞书授权。\n\n"
        f"请 {name} 查看机器人私聊并完成授权。授权链接已通过私聊单独发送，不会在群内展示。"
    )


def _private_group_lark_auth_text(verify_url: str, group_label: str) -> str:
    return (
        f"你刚刚在{group_label}中 @ 了机器人。\n\n"
        f"机器人在群中回复你的消息时，可能会涉及你的相关数据。"
        f"需要允许机器人在{group_label}回复消息吗？需要的话请点击链接进行单独授权：\n\n"
        f"[点击授权]({verify_url})\n\n"
        "授权完成后，机器人会自动回到群话题继续处理。"
    )


def _private_group_lark_auth_done_text(group_label: str) -> str:
    return f"飞书授权已完成，请回到{group_label}查看机器人回复。"


def _send_lark_auth_prompt(
        open_id: str,
        verify_url: str,
        token: str,
        *,
        reply_msg_id: str | None,
        chat_id: str,
        chat_type: str,
        store: UserStore | None = None) -> tuple[str, str]:
    if chat_type == "group" and reply_msg_id:
        group_card_id = feishu_api.reply_card_in_thread(
            reply_msg_id,
            _group_lark_auth_waiting_text(open_id, store),
            token,
        )
        group_label = _group_label(chat_id, token)
        private_card_id = ""
        try:
            private_card_id = feishu_api.send_text_card(
                open_id,
                _private_group_lark_auth_text(verify_url, group_label),
                token,
            )
        except Exception as e:
            logger.warning("Could not send private group auth card to %s: %s", open_id, e)
        return group_card_id, private_card_id

    auth_msg = (
        f"请完成飞书授权，授权后将自动继续处理您的请求：\n\n[点击授权]({verify_url})\n\n"
        "如授权后未自动刷新，请对我说「重新授权」。"
    )
    return feishu_api.send_text_card(open_id, auth_msg, token), ""


def _complete_auth_and_resume(
        open_id: str,
        context_id: str,
        provider: str,
        device_code: str,
        auth: AuthManager,
        store: UserStore,
        agent: Agent,
        app_id: str,
        app_secret: str,
        owner: str = "",
        resume_prefix: str = "") -> bool:
    """Claim and resume the original request after auth completes.

    Returns True if this pod claimed and resumed the pending request.
    Returns False if another pod already claimed it or no durable resume job exists.
    """
    _ = auth
    _ctx = context_id or open_id
    _owner = owner or _auth_resume_owner()
    job = store.claim_auth_resume_job(_ctx, provider, device_code, _owner)
    if not job:
        logger.info("[auth-resume] skipped provider=%s ctx=%s code=%s reason=no_claim",
                    provider, _ctx, (device_code or "")[:8])
        return False

    try:
        if not job.get("resume_text"):
            logger.info("[auth-resume] claimed empty resume_text provider=%s ctx=%s job=%s",
                        provider, _ctx, job["id"])
            store.consume_auth_resume_job(job["id"])
            return True

        _stream_claude(
            open_id,
            (resume_prefix or "") + job["resume_text"],
            agent,
            store,
            app_id,
            app_secret,
            existing_msg_id=job.get("existing_msg_id") or None,
            context_id=_ctx,
            reply_msg_id=(job.get("reply_id") or None) if not job.get("existing_msg_id") else None,
            thread_session_key=job.get("thread_key") or None,
            root_id=job.get("root_id") or "",
            chat_id=job.get("chat_id") or "",
            chat_type=job.get("chat_type") or "p2p",
            oa_api_key=os.environ.get("OA_API_KEY", ""),
        )
        store.consume_auth_resume_job(job["id"])
        return True
    except Exception as e:
        store.fail_auth_resume_job(job["id"], str(e))
        raise


def _poll_meegle_and_resume(open_id: str, original_text: str,
                            client_id: str, device_code: str,
                            auth: AuthManager, store: UserStore,
                            agent: Agent, app_id: str, app_secret: str,
                            context_id: str,
                            reply_msg_id: str | None = None,
                            thread_session_key: str | None = None,
                            root_id: str = "",
                            chat_id: str = "", chat_type: str = "p2p",
                            auth_card_id: str | None = None) -> None:
    """
    Poll for meegle auth completion after Claude has already shown the user the URL.
    On success, update the existing auth card and reuse it for Claude's response
    (mirrors lark-cli auth: same card gets updated in-place, no new card sent).
    On timeout, update the card with an error message.

    auth_card_id: the msg_id of the card Claude used to show the auth link.
    """
    _ctx = context_id
    _active_auth_pollers.add(_ctx)
    deadline = time.time() + _AUTH_POLL_TIMEOUT
    while time.time() < deadline:
        time.sleep(_AUTH_POLL_INTERVAL)
        try:
            authorized = auth.poll_meegle_once(_ctx, client_id, device_code)
        except Exception as e:
            logger.warning(f"Meegle auth poll error for {_ctx}: {e}")
            continue
        if authorized:
            logger.info(f"Meegle auth completed for {_ctx}, auto-resuming original request")
            if auth_card_id:
                try:
                    token = feishu_api.get_tenant_access_token(app_id, app_secret)
                    feishu_api.update_card_text(
                        auth_card_id, "✅ Meegle 授权完成！正在处理您的请求…", token)
                except Exception as e:
                    logger.warning(f"Could not update meegle auth card: {e}")
            resumed = _complete_auth_and_resume(
                open_id=open_id,
                context_id=context_id,
                provider="meegle",
                device_code=device_code,
                auth=auth,
                store=store,
                agent=agent,
                app_id=app_id,
                app_secret=app_secret,
            )
            if not resumed:
                _notify_auth_resume_missing(open_id, auth_card_id, app_id, app_secret)
            _active_auth_pollers.discard(_ctx)
            return

    # Timed out — only reset if our device_code is still the active one
    _active_auth_pollers.discard(_ctx)
    logger.info(f"Meegle auth polling timed out for {_ctx}")
    try:
        current_user = store.get_user(_ctx)
        if current_user and current_user.get("meegle_pending_code") == device_code:
            store.upsert_user(_ctx, meegle_auth_status="none",
                              meegle_pending_code=None, meegle_pending_client_id=None,
                              meegle_pending_url=None, meegle_pending_at=None)
        else:
            logger.info(f"Skipping meegle reset for {_ctx}: device_code superseded by newer flow")
    except Exception as e:
        logger.warning(f"Could not reset meegle pending state for {_ctx}: {e}")
    timeout_msg = "⏰ Meegle 授权超时（5 分钟），请重新发送消息重试。"
    if auth_card_id:
        try:
            token = feishu_api.get_tenant_access_token(app_id, app_secret)
            feishu_api.update_card_text(auth_card_id, timeout_msg, token)
        except Exception as e:
            logger.warning(f"Could not update meegle timeout card: {e}")
            send_feishu_message(open_id, timeout_msg, app_id, app_secret)
    else:
        send_feishu_message(open_id, timeout_msg, app_id, app_secret)


def _poll_lark_reauth_and_resume(open_id: str, original_text: str,
                                  device_code: str,
                                  auth: AuthManager, store: UserStore,
                                  agent: Agent, app_id: str, app_secret: str,
                                  context_id: str,
                                  reply_msg_id: str | None = None,
                                  thread_session_key: str | None = None,
                                  root_id: str = "",
                                  chat_id: str = "", chat_type: str = "p2p",
                                  auth_card_id: str | None = None,
                                  reauth_count: int = 1) -> None:
    """
    Poll for lark reauth completion after Claude has called lark_reauth_cli.py and
    shown the user the auth URL.  On success, update the existing card and re-run Claude
    with the original message.  On timeout, notify the user.
    """
    _ctx = context_id
    _active_auth_pollers.add(_ctx)
    deadline = time.time() + _AUTH_POLL_TIMEOUT
    while time.time() < deadline:
        time.sleep(_AUTH_POLL_INTERVAL)
        # Staleness check: bail out if a newer reauth flow has superseded ours
        # (user did /reauth, another scheduled task started a new flow, etc.).
        # Without this, the stale poller's fallback disk check will succeed on
        # the NEW flow's token and trigger a duplicate resume.
        try:
            _cur_user = store.get_user(_ctx) or {}
            _cur_code = _cur_user.get("pending_code") or ""
            _cur_status = _cur_user.get("auth_status") or ""
            if _cur_status != "pending" or _cur_code != device_code:
                logger.info(
                    f"Lark reauth poller for {_ctx} exiting: pending_code changed "
                    f"(was={device_code[:8]}..., now={_cur_code[:8] if _cur_code else 'cleared'}..., "
                    f"status={_cur_status})"
                )
                _active_auth_pollers.discard(_ctx)
                return
        except Exception as e:
            logger.warning(f"Lark reauth poller staleness check failed for {_ctx}: {e}")
        try:
            authorized = auth.poll_once(_ctx, device_code)
        except Exception as e:
            logger.warning(f"Lark reauth poll error for {_ctx}: {e}")
            continue
        if authorized:
            logger.info(f"Lark reauth completed for {_ctx}, auto-resuming original request")
            # Keep the existing Claude session when resuming after a scope/auth
            # refresh. The resumed prompt is often a short follow-up like
            # "export it as PDF", so clearing here strips the context it needs.
            if auth_card_id:
                try:
                    token = feishu_api.get_tenant_access_token(app_id, app_secret)
                    feishu_api.update_card_text(
                        auth_card_id, "✅ 飞书授权完成！正在处理您的请求…", token)
                except Exception as e:
                    logger.warning(f"Could not update lark reauth card: {e}")
            _pending_at_before_resume = (store.get_user(_ctx) or {}).get("pending_at", "")
            # Warn Claude about the reauth history so it doesn't loop forever on not_found.
            _resume_prefix = ""
            if reauth_count >= 1:
                _resume_prefix = (
                    f"[系统提示：飞书 token 刚刚完成了第 {reauth_count} 次重新授权。"
                    "如果 wiki/docx 命令仍然返回 not_found 或 403/no_permission/forbidden，"
                    "这不是 scope 问题，请换用其他方法或告知用户该资源无法访问，"
                    "不要再次调用 lark_reauth_cli.py。] "
                )
            resumed = _complete_auth_and_resume(
                open_id=open_id,
                context_id=context_id,
                provider="lark",
                device_code=device_code,
                auth=auth,
                store=store,
                agent=agent,
                app_id=app_id,
                app_secret=app_secret,
                resume_prefix=_resume_prefix,
            )
            resumed_card = auth_card_id
            if not resumed:
                _notify_auth_resume_missing(open_id, auth_card_id, app_id, app_secret)
                _active_auth_pollers.discard(_ctx)
                return
            # If the resumed stream itself triggered another reauth, chain a new poller.
            # Cap at 2 cycles to prevent infinite loops caused by misidentified not_found errors.
            _user_after_resume = store.get_user(_ctx) or {}
            _new_pending_at = _user_after_resume.get("pending_at", "")
            _new_code = _user_after_resume.get("pending_code", "")
            if (_user_after_resume.get("auth_status") == "pending"
                    and _new_pending_at != _pending_at_before_resume
                    and _new_code):
                if reauth_count < 2:
                    logger.info(f"Resumed stream triggered another lark reauth for {_ctx}, "
                                f"chaining poller (reauth_count={reauth_count + 1})")
                    _create_auth_resume_job(
                        store,
                        context_id=_ctx,
                        provider="lark",
                        device_code=_new_code,
                        resume_text=original_text,
                        thread_key=thread_session_key,
                        root_id=root_id,
                        chat_id=chat_id,
                        chat_type=chat_type,
                        existing_msg_id=resumed_card or "",
                    )
                    _poll_lark_reauth_and_resume(
                        open_id, original_text, _new_code,
                        auth, store, agent, app_id, app_secret,
                        context_id, None, thread_session_key, root_id,
                        chat_id, chat_type,
                        resumed_card,
                        reauth_count=reauth_count + 1,
                    )
                else:
                    logger.warning(f"Lark reauth loop detected for {_ctx} (reauth_count={reauth_count}), "
                                   "stopping chain to prevent infinite loop")
                    store.reset_auth(_ctx)
                    msg = ("⚠️ 已完成多次飞书重新授权，但仍无法访问该文档。"
                           "可能是文档不存在、权限未配置，或命令不支持该格式。"
                           "请确认文档链接正确后重新发送，或联系管理员检查应用权限。")
                    if resumed_card:
                        try:
                            token = feishu_api.get_tenant_access_token(app_id, app_secret)
                            feishu_api.update_card_text(resumed_card, msg, token)
                        except Exception:
                            send_feishu_message(open_id, msg, app_id, app_secret)
                    else:
                        send_feishu_message(open_id, msg, app_id, app_secret)
            _active_auth_pollers.discard(_ctx)
            return

    # Timed out — only reset if our device_code is still the active one
    _active_auth_pollers.discard(_ctx)
    logger.info(f"Lark reauth polling timed out for {_ctx}")
    try:
        current_user = store.get_user(_ctx)
        if current_user and current_user.get("pending_code") == device_code:
            store.reset_auth(_ctx)
        else:
            logger.info(f"Skipping reset_auth for {_ctx}: device_code superseded by newer flow")
    except Exception as e:
        logger.warning(f"Could not reset lark auth state for {_ctx}: {e}")
    timeout_msg = "⏰ 飞书授权超时（5 分钟），请重新发送消息重试。"
    if auth_card_id:
        try:
            token = feishu_api.get_tenant_access_token(app_id, app_secret)
            feishu_api.update_card_text(auth_card_id, timeout_msg, token)
        except Exception as e:
            logger.warning(f"Could not update lark reauth timeout card: {e}")
            send_feishu_message(open_id, timeout_msg, app_id, app_secret)
    else:
        send_feishu_message(open_id, timeout_msg, app_id, app_secret)


def _build_combined_auth_card(lark_done: bool, lark_url: str,
                              meegle_done: bool, meegle_url: str) -> str:
    """Build the text for a combined Lark + Meegle auth card."""
    lark_line = "1. 飞书授权 ✅" if lark_done else f"1. 飞书授权 ⏳ → [点击授权]({lark_url})"
    meegle_line = "2. Meegle 授权 ✅" if meegle_done else f"2. Meegle（飞书项目）授权 ⏳ → [点击授权]({meegle_url})"
    return (
        "请完成以下授权，全部完成后将自动处理您的请求：\n\n"
        f"{lark_line}\n{meegle_line}"
    )


def _start_combined_auth_and_poll(open_id: str, original_text: str,
                                  lark_device_code: str, lark_url: str,
                                  meegle_client_id: str, meegle_device_code: str, meegle_url: str,
                                  auth: AuthManager, store: UserStore,
                                  agent: Agent, app_id: str, app_secret: str,
                                  context_id: str | None = None,
                                  reply_msg_id: str | None = None,
                                  thread_session_key: str | None = None,
                                  root_id: str = "",
                                  chat_id: str = "", chat_type: str = "p2p") -> None:
    """
    Send a single card containing both Lark and Meegle auth links, then poll
    both in parallel.  The card is updated immediately whenever either auth
    completes so the user gets instant feedback.  Once both are done, Claude
    is invoked with the original message.

    If only one auth is needed (the other already done), the corresponding
    url/device_code args should be empty strings and need_lark/need_meegle
    is inferred from them.
    """
    _ctx = context_id or open_id
    _active_auth_pollers.add(_ctx)
    need_lark = bool(lark_device_code)
    need_meegle = bool(meegle_device_code)

    token = feishu_api.get_tenant_access_token(app_id, app_secret)

    # Build initial card: both pending
    card_text = _build_combined_auth_card(
        lark_done=not need_lark, lark_url=lark_url,
        meegle_done=not need_meegle, meegle_url=meegle_url,
    )
    if reply_msg_id:
        card_id = feishu_api.reply_card_in_thread(reply_msg_id, card_text, token)
    else:
        card_id = feishu_api.send_text_card(open_id, card_text, token)
    logger.info(f"Combined auth card sent to {open_id} (ctx={_ctx[:40]}): {card_id}")
    if need_lark:
        _set_auth_resume_card_id(
            store, context_id=_ctx, provider="lark", device_code=lark_device_code, card_id=card_id,
        )
    if need_meegle:
        _set_auth_resume_card_id(
            store, context_id=_ctx, provider="meegle", device_code=meegle_device_code, card_id=card_id,
        )

    # Shared mutable state protected by a lock
    _lock = threading.Lock()
    _lark_done = [not need_lark]   # True if already satisfied before this call
    _meegle_done = [not need_meegle]
    _finished = [False]            # True once both done and Claude has been invoked

    def _try_finish():
        """Called (under _lock) when either auth completes. Invokes Claude if both done."""
        if _finished[0]:
            return
        if not (_lark_done[0] and _meegle_done[0]):
            return
        _finished[0] = True
        # Update card to "processing"
        try:
            feishu_api.update_card_text(card_id, "✅ 授权完成！正在处理您的请求…", token)
        except Exception as e:
            logger.warning(f"Could not update combined auth card to processing: {e}")
        _pending_at_before = (store.get_user(_ctx) or {}).get("pending_at", "")
        _meegle_pending_before = (store.get_user(_ctx) or {}).get("meegle_pending_at", "")
        resume_provider = "lark" if need_lark else "meegle"
        resume_code = lark_device_code if need_lark else meegle_device_code
        resumed = _complete_auth_and_resume(
            open_id=open_id,
            context_id=_ctx,
            provider=resume_provider,
            device_code=resume_code,
            auth=auth,
            store=store,
            agent=agent,
            app_id=app_id,
            app_secret=app_secret,
        )
        if not resumed:
            _notify_auth_resume_missing(open_id, card_id, app_id, app_secret)
            return
        # If Claude triggered a reauth during this run, start a new poller in a
        # background thread (_try_finish may hold _lock, so we must not block here).
        _user_after = store.get_user(_ctx) or {}
        _new_lark_code = _user_after.get("pending_code", "")
        _new_meegle_code = _user_after.get("meegle_pending_code", "")
        _new_meegle_client = _user_after.get("meegle_pending_client_id", "")
        if (_user_after.get("auth_status") == "pending"
                and _user_after.get("pending_at", "") != _pending_at_before
                and _new_lark_code):
            logger.info(f"Combined auth stream triggered lark reauth for {_ctx}, starting poller thread")
            _create_auth_resume_job(
                store,
                context_id=_ctx,
                provider="lark",
                device_code=_new_lark_code,
                resume_text=original_text,
                thread_key=thread_session_key,
                root_id=root_id,
                chat_id=chat_id,
                chat_type=chat_type,
                existing_msg_id=card_id,
            )
            threading.Thread(
                target=_poll_lark_reauth_and_resume,
                args=(open_id, original_text, _new_lark_code,
                      auth, store, agent, app_id, app_secret,
                      context_id, None, thread_session_key, root_id,
                      chat_id, chat_type, card_id),
                daemon=True,
            ).start()
        elif (_user_after.get("meegle_auth_status") == "pending"
                and _user_after.get("meegle_pending_at", "") != _meegle_pending_before
                and _new_meegle_code and _new_meegle_client):
            logger.info(f"Combined auth stream triggered meegle reauth for {_ctx}, starting poller thread")
            _create_auth_resume_job(
                store,
                context_id=_ctx,
                provider="meegle",
                device_code=_new_meegle_code,
                client_id=_new_meegle_client,
                resume_text=original_text,
                thread_key=thread_session_key,
                root_id=root_id,
                chat_id=chat_id,
                chat_type=chat_type,
                existing_msg_id=card_id,
            )
            threading.Thread(
                target=_poll_meegle_and_resume,
                args=(open_id, original_text, _new_meegle_client, _new_meegle_code,
                      auth, store, agent, app_id, app_secret,
                      context_id, None, thread_session_key, root_id,
                      chat_id, chat_type, card_id),
                daemon=True,
            ).start()

    def _poll_lark():
        deadline = time.time() + _AUTH_POLL_TIMEOUT
        while time.time() < deadline:
            time.sleep(_AUTH_POLL_INTERVAL)
            with _lock:
                if _finished[0] or _lark_done[0]:
                    return
            try:
                authorized = auth.poll_once(_ctx, lark_device_code)
            except Exception as e:
                logger.warning(f"Combined auth lark poll error for {_ctx}: {e}")
                continue
            if authorized:
                logger.info(f"Combined auth: Lark done for {_ctx}")
                with _lock:
                    _lark_done[0] = True
                    # Update card: Lark ✅, Meegle status as-is
                    try:
                        feishu_api.update_card_text(
                            card_id,
                            _build_combined_auth_card(
                                lark_done=True, lark_url=lark_url,
                                meegle_done=_meegle_done[0], meegle_url=meegle_url,
                            ),
                            token,
                        )
                    except Exception as e:
                        logger.warning(f"Could not update combined auth card (lark done): {e}")
                    _try_finish()
                return
        # Lark timed out — reset both auth states so the next message starts clean
        with _lock:
            if _finished[0]:
                return
            _finished[0] = True
        logger.info(f"Combined auth: Lark polling timed out for {_ctx}")
        try:
            store.reset_auth(_ctx)
            store.reset_meegle_auth(_ctx)
        except Exception:
            pass
        try:
            feishu_api.update_card_text(
                card_id, "⏰ 飞书授权超时（5 分钟），请重新发送消息重试。", token)
        except Exception as e:
            logger.warning(f"Could not update combined auth card (lark timeout): {e}")

    def _poll_meegle():
        deadline = time.time() + _AUTH_POLL_TIMEOUT
        while time.time() < deadline:
            time.sleep(_AUTH_POLL_INTERVAL)
            with _lock:
                if _finished[0] or _meegle_done[0]:
                    return
            try:
                authorized = auth.poll_meegle_once(_ctx, meegle_client_id, meegle_device_code)
            except Exception as e:
                logger.warning(f"Combined auth meegle poll error for {_ctx}: {e}")
                continue
            if authorized:
                logger.info(f"Combined auth: Meegle done for {_ctx}")
                with _lock:
                    _meegle_done[0] = True
                    try:
                        feishu_api.update_card_text(
                            card_id,
                            _build_combined_auth_card(
                                lark_done=_lark_done[0], lark_url=lark_url,
                                meegle_done=True, meegle_url=meegle_url,
                            ),
                            token,
                        )
                    except Exception as e:
                        logger.warning(f"Could not update combined auth card (meegle done): {e}")
                    _try_finish()
                return
        # Meegle timed out — reset both auth states so the next message starts clean
        with _lock:
            if _finished[0]:
                return
            _finished[0] = True
        logger.info(f"Combined auth: Meegle polling timed out for {_ctx}")
        try:
            store.reset_meegle_auth(_ctx)
            store.reset_auth(_ctx)
        except Exception:
            pass
        try:
            feishu_api.update_card_text(
                card_id, "⏰ Meegle 授权超时（5 分钟），请重新发送消息重试。", token)
        except Exception as e:
            logger.warning(f"Could not update combined auth card (meegle timeout): {e}")

    # Start poller threads — each runs until done or timed out
    threads: list[threading.Thread] = []
    if need_lark:
        t = threading.Thread(target=_poll_lark, daemon=True)
        t.start()
        threads.append(t)
    if need_meegle:
        t = threading.Thread(target=_poll_meegle, daemon=True)
        t.start()
        threads.append(t)

    # Wait for both pollers to finish (they will either succeed or time out)
    for t in threads:
        t.join()
    _active_auth_pollers.discard(_ctx)


def _start_meegle_auth_and_poll(open_id: str, original_text: str,
                                client_id: str, device_code: str, verify_url: str,
                                auth: AuthManager, store: UserStore,
                                agent: Agent, app_id: str, app_secret: str,
                                context_id: str | None = None,
                                reply_msg_id: str | None = None,
                                thread_session_key: str | None = None,
                                root_id: str = "",
                                chat_id: str = "", chat_type: str = "p2p") -> None:
    """
    Send a meegle auth-link card and poll until the user completes OAuth.
    On success, stream Claude with the original message. On timeout, notify user.
    """
    _ctx = context_id or open_id
    token = feishu_api.get_tenant_access_token(app_id, app_secret)
    auth_msg = (
        "请完成 Meegle（飞书项目）授权，授权后将自动继续处理您的请求：\n\n"
        f"[点击授权]({verify_url})"
    )
    if reply_msg_id:
        card_id = feishu_api.reply_card_in_thread(reply_msg_id, auth_msg, token)
    else:
        card_id = feishu_api.send_text_card(open_id, auth_msg, token)
    logger.info(f"Meegle auth card sent to {open_id} (ctx={_ctx[:40]}): {card_id}")
    _set_auth_resume_card_id(
        store, context_id=_ctx, provider="meegle", device_code=device_code, card_id=card_id,
    )

    deadline = time.time() + _AUTH_POLL_TIMEOUT
    while time.time() < deadline:
        time.sleep(_AUTH_POLL_INTERVAL)
        try:
            authorized = auth.poll_meegle_once(_ctx, client_id, device_code)
        except Exception as e:
            logger.warning(f"Meegle auth poll error for {_ctx}: {e}")
            continue
        if authorized:
            logger.info(f"Meegle auth completed for {_ctx}, streaming original message")
            try:
                feishu_api.update_card_text(card_id, "✅ Meegle 授权完成！正在处理您的请求…", token)
            except Exception as e:
                logger.warning(f"Could not update meegle auth card: {e}")
            resumed = _complete_auth_and_resume(
                open_id=open_id,
                context_id=_ctx,
                provider="meegle",
                device_code=device_code,
                auth=auth,
                store=store,
                agent=agent,
                app_id=app_id,
                app_secret=app_secret,
            )
            if not resumed:
                _notify_auth_resume_missing(open_id, card_id, app_id, app_secret)
            return

    logger.info(f"Meegle auth polling timed out for {_ctx}")
    # Only reset if our device_code is still the active one
    try:
        current_user = store.get_user(_ctx)
        if current_user and current_user.get("meegle_pending_code") == device_code:
            store.reset_meegle_auth(_ctx)
        else:
            logger.info(f"Skipping meegle reset for {_ctx}: device_code superseded")
    except Exception as e:
        logger.warning(f"Could not reset meegle pending state for {_ctx}: {e}")
    try:
        feishu_api.update_card_text(card_id, "⏰ Meegle 授权超时（5 分钟），请重新发送消息重试。", token)
    except Exception as e:
        logger.warning(f"Could not update meegle auth timeout card: {e}")


def _recover_pending_auth_and_resume(
        open_id: str, original_text: str, device_code: str,
        auth: AuthManager, store: UserStore, agent: Agent,
        app_id: str, app_secret: str,
        context_id: str, reply_msg_id: str | None,
        thread_session_key: str | None, root_id: str,
        chat_id: str, chat_type: str) -> None:
    """Poll an already-pending device code (no new card sent) and stream Claude on success.

    Used when a follow-up message arrives while the original poller (possibly on another
    pod or already timed out) is no longer watching for auth completion.
    """
    _ctx = context_id or open_id
    _n = store.get_display_name(open_id) or open_id
    logger.info(f"[auth-flow] {_n} recovery poller started for pending code {device_code[:8]}...")
    try:
        deadline = time.time() + _AUTH_POLL_TIMEOUT
        while time.time() < deadline:
            time.sleep(_AUTH_POLL_INTERVAL)
            # Staleness check: if the DB pending_code has changed (user did /reauth,
            # or a concurrent scheduled task started a new flow), abandon this poller.
            # Otherwise a stale poller's fallback disk check will succeed on the NEW
            # flow's token and trigger a duplicate resume of the original message.
            try:
                _cur_user = store.get_user(_ctx) or {}
                _cur_code = _cur_user.get("pending_code") or ""
                _cur_status = _cur_user.get("auth_status") or ""
                if _cur_status != "pending" or _cur_code != device_code:
                    logger.info(
                        f"[auth-flow] {_n} recovery poller exiting: pending_code changed "
                        f"(was={device_code[:8]}..., now={_cur_code[:8] if _cur_code else 'cleared'}..., "
                        f"status={_cur_status})"
                    )
                    return
            except Exception as e:
                logger.warning(f"[auth-flow] {_n} recovery poll staleness check failed: {e}")
                # Fall through and attempt poll — better than aborting on a transient DB blip.
            try:
                authorized = auth.poll_once(_ctx, device_code)
            except Exception as e:
                logger.warning(f"[auth-flow] {_n} recovery poll error: {e}")
                continue
            if authorized:
                logger.info(f"[auth-flow] {_n} recovery poller: auth completed, completing durable resume")
                resumed = _complete_auth_and_resume(
                    open_id=open_id,
                    context_id=context_id,
                    provider="lark",
                    device_code=device_code,
                    auth=auth,
                    store=store,
                    agent=agent,
                    app_id=app_id,
                    app_secret=app_secret,
                )
                if not resumed:
                    _notify_auth_resume_missing(open_id, None, app_id, app_secret)
                return
        logger.info(f"[auth-flow] {_n} recovery poller timed out")
    finally:
        _active_auth_pollers.discard(_ctx)


def _start_auth_and_poll(open_id: str, original_text: str, device_code: str,
                         verify_url: str, auth: AuthManager, store: UserStore,
                         agent: Agent, app_id: str, app_secret: str,
                         context_id: str | None = None,
                         reply_msg_id: str | None = None,
                         thread_session_key: str | None = None,
                         root_id: str = "",
                         chat_id: str = "", chat_type: str = "p2p") -> None:
    """
    Send an auth-link card and poll until the user completes OAuth.
    On success, update the card and stream Claude with the original message text.
    On timeout, update the card to ask the user to retry.

    Args:
        context_id: auth context key (open_id for P2P, g_{chat_id}_{open_id} for group)
        reply_msg_id: for group — the @mention message to send auth card as thread reply
        thread_session_key: forwarded to _stream_claude after auth completes
    """
    _ctx = context_id or open_id
    _active_auth_pollers.add(_ctx)
    token = feishu_api.get_tenant_access_token(app_id, app_secret)
    card_id, private_auth_card_id = _send_lark_auth_prompt(
        open_id,
        verify_url,
        token,
        reply_msg_id=reply_msg_id,
        chat_id=chat_id,
        chat_type=chat_type,
        store=store,
    )
    logger.info(f"Auth card sent to {open_id} (ctx={_ctx[:40]}): {card_id}")
    _set_auth_resume_card_id(
        store, context_id=_ctx, provider="lark", device_code=device_code, card_id=card_id,
    )

    deadline = time.time() + _AUTH_POLL_TIMEOUT
    while time.time() < deadline:
        time.sleep(_AUTH_POLL_INTERVAL)
        try:
            authorized = auth.poll_once(_ctx, device_code)
        except Exception as e:
            logger.warning(f"Auth poll error for {_ctx}: {e}")
            continue
        if authorized:
            logger.info(f"Auth completed for {_ctx}, streaming original message")
            if not original_text:
                # /reauth flow: no original message to replay — just confirm and let
                # the user resend their request.
                try:
                    feishu_api.update_card_text(
                        card_id, "✅ 飞书授权完成！请重新发送您的消息。", token)
                except Exception as e:
                    logger.warning(f"Could not update auth card: {e}")
                return
            try:
                feishu_api.update_card_text(card_id, "✅ 授权完成！正在处理您的请求…", token)
            except Exception as e:
                logger.warning(f"Could not update auth card: {e}")
            if private_auth_card_id:
                try:
                    group_label = _group_label(chat_id, token)
                    feishu_api.update_card_text(
                        private_auth_card_id,
                        _private_group_lark_auth_done_text(group_label),
                        token,
                    )
                except Exception as e:
                    logger.warning(f"Could not update private auth card: {e}")
            _pending_at_before = (store.get_user(_ctx) or {}).get("pending_at", "")
            _meegle_pending_before = (store.get_user(_ctx) or {}).get("meegle_pending_at", "")
            resumed = _complete_auth_and_resume(
                open_id=open_id,
                context_id=_ctx,
                provider="lark",
                device_code=device_code,
                auth=auth,
                store=store,
                agent=agent,
                app_id=app_id,
                app_secret=app_secret,
            )
            if not resumed:
                _notify_auth_resume_missing(open_id, card_id, app_id, app_secret)
                _active_auth_pollers.discard(_ctx)
                return
            # If Claude triggered a reauth during this run, start a new poller so
            # the user doesn't get permanently stuck at pending.
            _user_after = store.get_user(_ctx) or {}
            _new_lark_code = _user_after.get("pending_code", "")
            _new_meegle_code = _user_after.get("meegle_pending_code", "")
            _new_meegle_client = _user_after.get("meegle_pending_client_id", "")
            if (_user_after.get("auth_status") == "pending"
                    and _user_after.get("pending_at", "") != _pending_at_before
                    and _new_lark_code):
                logger.info(f"Initial auth stream triggered lark reauth for {_ctx}, starting poller")
                _create_auth_resume_job(
                    store,
                    context_id=_ctx,
                    provider="lark",
                    device_code=_new_lark_code,
                    resume_text=original_text,
                    thread_key=thread_session_key,
                    root_id=root_id,
                    chat_id=chat_id,
                    chat_type=chat_type,
                    existing_msg_id=card_id,
                )
                _poll_lark_reauth_and_resume(
                    open_id, original_text, _new_lark_code,
                    auth, store, agent, app_id, app_secret,
                    context_id, None, thread_session_key, root_id,
                    chat_id, chat_type, card_id,
                )
            elif (_user_after.get("meegle_auth_status") == "pending"
                    and _user_after.get("meegle_pending_at", "") != _meegle_pending_before
                    and _new_meegle_code and _new_meegle_client):
                logger.info(f"Initial auth stream triggered meegle reauth for {_ctx}, starting poller")
                _create_auth_resume_job(
                    store,
                    context_id=_ctx,
                    provider="meegle",
                    device_code=_new_meegle_code,
                    client_id=_new_meegle_client,
                    resume_text=original_text,
                    thread_key=thread_session_key,
                    root_id=root_id,
                    chat_id=chat_id,
                    chat_type=chat_type,
                    existing_msg_id=card_id,
                )
                _poll_meegle_and_resume(
                    open_id, original_text, _new_meegle_client, _new_meegle_code,
                    auth, store, agent, app_id, app_secret,
                    context_id, None, thread_session_key, root_id,
                    chat_id, chat_type, card_id,
                )
            _active_auth_pollers.discard(_ctx)
            return

    # Timed out — only reset if our device_code is still the active one
    _active_auth_pollers.discard(_ctx)
    logger.info(f"Auth polling timed out for {_ctx}")
    try:
        user_now = store.get_user(_ctx)
        if user_now and user_now.get("pending_code") == device_code:
            store.reset_auth(_ctx)
        else:
            logger.info(f"Skipping reset_auth for {_ctx}: device_code superseded")
    except Exception as e:
        logger.warning(f"Could not reset auth state for {_ctx}: {e}")
    try:
        feishu_api.update_card_text(card_id, "⏰ 授权超时（5 分钟），请重新发送消息重试。", token)
    except Exception as e:
        logger.warning(f"Could not update auth timeout card: {e}")


def handle_bot_added(event: dict, app_id: str, app_secret: str) -> None:
    """Bot が群に追加されたとき、追加した人に私信で通知カードを送る。"""
    operator_id = event.get("operator_id", "")
    chat_id = event.get("chat_id", "")
    if not operator_id:
        logger.warning(f"bot_added event missing operator_id: {event}")
        return
    try:
        token = feishu_api.get_tenant_access_token(app_id, app_secret)
        chat_name = event.get("chat_name", "")
        if not chat_name and chat_id:
            info = feishu_api.get_chat_info(chat_id, token)
            chat_name = info.get("name", "")
        chat_name = chat_name or "这个群"
        text = (
            f'⚠️  我刚刚被添加到了群聊「{chat_name}」，请注意安全隐私风险。\n'
            "我在群内回复你的消息时，可能会涉及你的相关数据。\n"
            "如果你只是想让我总结这个群的信息，可以直接单聊告诉我，无需进群即可完成。"
        )
        feishu_api.send_text_card(operator_id, text, token)
        logger.info(f"[入群通知] operator={operator_id} chat={chat_id} ({chat_name})")
    except Exception as e:
        logger.error(f"handle_bot_added failed: {e}")


def _handle_reauth_command(open_id: str, text: str, context_id: str,
                           auth: "AuthManager", store: UserStore,
                           app_id: str, app_secret: str,
                           auth_executor: "ThreadPoolExecutor | None",
                           reply_msg_id: str | None,
                           thread_session_key: str | None,
                           root_id: str, chat_id: str, chat_type: str,
                           agent: "Agent") -> bool:
    """Handle /reauth command — revoke old token and start a fresh OAuth flow.

    Returns True if handled (caller should return immediately), False otherwise.
    Useful when the user's existing token lacks required OAuth scopes
    (e.g. wiki read access was added to the app after the user first authorised).
    """
    lower = text.strip().lower()
    triggers = {"/reauth", "/重新授权", "重新授权", "/refresh_auth"}
    if not any(t in lower for t in triggers):
        return False

    logger.info(f"[reauth] Revoking token and restarting auth for {context_id}")
    try:
        auth.revoke_token(context_id)
    except Exception as e:
        logger.warning(f"[reauth] revoke_token failed (continuing): {e}")
    store.reset_auth(context_id)

    try:
        data = auth.start_auth(context_id)
        if auth_executor:
            auth_executor.submit(
                _start_auth_and_poll,
                open_id, "", data["device_code"], data["verification_url"],
                auth, store, agent, app_id, app_secret,
                context_id, reply_msg_id, thread_session_key, root_id,
                chat_id, chat_type,
            )
        else:
            _start_auth_and_poll(
                open_id, "", data["device_code"], data["verification_url"],
                auth, store, agent, app_id, app_secret,
                context_id, reply_msg_id, thread_session_key, root_id,
                chat_id, chat_type,
            )
    except Exception as e:
        logger.error(f"[reauth] start_auth failed for {context_id}: {e}")
        send_feishu_message(open_id, "重新授权初始化失败，请稍后重试。", app_id, app_secret)
    return True


def _handle_meegle_reauth_command(open_id: str, text: str, context_id: str,
                                   auth: "AuthManager", store: UserStore,
                                   app_id: str, app_secret: str) -> bool:
    """Handle /meegle-reauth command — revoke meegle token and reset DB state.

    Returns True if handled (caller should return immediately), False otherwise.
    After reset, Claude will detect 'authenticated=false' on the next message and
    re-trigger the meegle OAuth flow automatically via meegle_auth_cli.py.
    """
    lower = text.strip().lower()
    triggers = {"/meegle-reauth", "/meegle_reauth", "/重新授权meegle", "/meegle重新授权",
                "重新授权meegle", "meegle重新授权", "重新授权 meegle", "meegle 重新授权"}
    if not any(t in lower for t in triggers):
        return False

    logger.info(f"[meegle-reauth] Revoking meegle token and resetting auth for {context_id}")
    try:
        auth.revoke_meegle_token(context_id)
    except Exception as e:
        logger.warning(f"[meegle-reauth] revoke_meegle_token failed (continuing): {e}")
    store.reset_meegle_auth(context_id)
    send_feishu_message(
        open_id,
        "✅ Meegle 授权已重置。请重新发送您的消息，机器人会引导您完成重新授权。",
        app_id, app_secret,
    )
    return True


def _meegle_status_for_reconcile(context_id: str, auth: AuthManager) -> dict:
    if hasattr(auth, "meegle_auth_status"):
        return auth.meegle_auth_status(context_id)
    return {
        "authenticated": auth.is_meegle_authenticated(context_id),
        "retryable": False,
        "reason": "",
    }


def _is_meegle_related_text(text: str) -> bool:
    lower = (text or "").lower()
    return any(s in lower for s in (
        "meegle",
        "飞书项目",
        "需求",
        "工作项",
        "project",
    ))


def _reconcile_meegle_state_before_use(context_id: str, auth: AuthManager,
                                       store: UserStore, text: str,
                                       user: dict | None) -> dict | None:
    """Align DB Meegle state with real CLI credentials before a Meegle request."""
    if not _is_meegle_related_text(text):
        return user
    if not user:
        return user
    db_status = user.get("meegle_auth_status")
    try:
        status = _meegle_status_for_reconcile(context_id, auth)
    except Exception as e:
        logger.warning("[meegle-auth] credential reconciliation failed for %s: %s", context_id, e)
        return user

    if status.get("retryable"):
        logger.warning(
            "[meegle-auth] status probe retryable for %s; keeping DB state reason=%s",
            context_id, status.get("reason", ""),
        )
        return user
    if status.get("authenticated"):
        if db_status == "authorized":
            logger.info("[meegle-auth] DB and CLI authorized for %s", context_id)
            return user
        logger.info(
            "[meegle-auth] CLI authorized while DB status=%s for %s; marking DB authorized",
            db_status,
            context_id,
        )
        store.mark_meegle_authorized(context_id)
        return store.get_user(context_id) or user

    if db_status != "authorized":
        return user

    logger.warning(
        "[meegle-auth] DB authorized but CLI status false for %s; resetting DB state reason=%s",
        context_id, status.get("reason", ""),
    )
    store.reset_meegle_auth(context_id)
    return store.get_user(context_id) or user


def _handle_reset_command(open_id: str, text: str, context_id: str,
                          store: UserStore, app_id: str, app_secret: str,
                          thread_session_key: str | None,
                          chat_id: str, root_id: str) -> bool:
    """Handle /reset — clear Claude session so the next message starts a fresh conversation."""
    lower = text.strip().lower()
    if lower not in ("/reset", "/重置", "/新对话", "/clear"):
        return False

    if thread_session_key:
        store.clear_thread_session(thread_session_key)
        if chat_id and root_id:
            store.clear_thread_session(f"{chat_id}:{root_id}")
    else:
        store.set_session_id(context_id, None)
    logger.info(f"[reset] Session cleared for {context_id}")
    send_feishu_message(open_id, "✅ 对话已重置，下一条消息将开启全新对话。", app_id, app_secret)
    return True


def _handle_usage_command(open_id: str, text: str, app_id: str, app_secret: str,
                          store: UserStore) -> bool:
    """Handle /usage command. Returns True if handled, False to continue to Claude."""
    from datetime import datetime, timezone
    lower = text.strip().lower()
    triggers = {"/usage", "/用量", "用量查询", "本月用量"}
    if not any(t in lower for t in triggers):
        return False

    year_month = datetime.now(timezone.utc).strftime("%Y-%m")
    token = feishu_api.get_tenant_access_token(app_id, app_secret)

    def _split(row: dict) -> tuple[int, int, float, int, int, float]:
        """Return (personal_in+out, personal_cache, personal_cost,
                   public_in+out, public_cache, public_cost)."""
        p_io = (row.get("personal_input_tokens", 0) or 0) + (row.get("personal_output_tokens", 0) or 0)
        p_cache = row.get("personal_cache_read_tokens", 0) or 0
        p_cost = row.get("personal_cost_usd", 0.0) or 0.0
        t_io = (row["input_tokens"] or 0) + (row["output_tokens"] or 0)
        t_cache = row["cache_read_tokens"] or 0
        t_cost = row["cost_usd"] or 0.0
        return (p_io, p_cache, p_cost, t_io - p_io, t_cache - p_cache, t_cost - p_cost)

    if open_id in _ADMIN_OPEN_IDS:
        rows = store.get_all_usage(year_month)
        if not rows:
            msg = f"📊 {year_month} 暂无用量数据"
        else:
            lines = [f"📊 {year_month} 全员用量排行（个人 key / 公共 key）：\n"]
            for i, r in enumerate(rows, 1):
                name = r["display_name"] or r["open_id"]
                p_io, _p_cache, p_cost, pub_io, _pub_cache, pub_cost = _split(r)
                lines.append(
                    f"{i}. {name}｜总 {r['input_tokens']+r['output_tokens']:,} tok / ${r['cost_usd']:.4f}"
                    f"｜个人 {p_io:,} tok / ${p_cost:.4f}"
                    f"｜公共 {pub_io:,} tok / ${pub_cost:.4f}"
                    f"｜{r['request_count']} 次"
                )
            msg = "\n".join(lines)
    else:
        r = store.get_user_usage(open_id, year_month)
        if not r or r.get("request_count", 0) == 0:
            msg = f"📊 {year_month} 你还没有用量记录"
        else:
            name = r.get("display_name") or open_id
            p_io, p_cache, p_cost, pub_io, pub_cache, pub_cost = _split(r)
            p_req = r.get("personal_request_count", 0) or 0
            pub_req = (r["request_count"] or 0) - p_req
            msg = (
                f"📊 {name} 的 {year_month} 用量：\n"
                f"输入：{r['input_tokens']:,} tokens\n"
                f"输出：{r['output_tokens']:,} tokens\n"
                f"缓存读取：{r['cache_read_tokens']:,} tokens\n"
                f"费用：${r['cost_usd']:.4f}\n"
                f"对话次数：{r['request_count']}\n\n"
                f"——按 key 分类——\n"
                f"个人 key：{p_io:,} tok（缓存 {p_cache:,}）｜${p_cost:.4f}｜{p_req} 次\n"
                f"公共 key：{pub_io:,} tok（缓存 {pub_cache:,}）｜${pub_cost:.4f}｜{pub_req} 次"
            )

    try:
        feishu_api.send_text_card(open_id, msg, token)
    except Exception as e:
        logger.warning(f"Usage command reply failed: {e}")
    return True


def handle_message(event: dict, store: UserStore, auth: AuthManager,
                   agent: Agent, app_id: str, app_secret: str,
                   executor: ThreadPoolExecutor | None = None,
                   auth_executor: ThreadPoolExecutor | None = None,
                   bot_open_id: str = "",
                   oa_api_key: str = "") -> None:
    global _last_event_epoch
    msg_claimed = False
    claimed_msg_id = ""
    fatal_error = False
    try:
        logger.debug(f"handle_message called: open_id={event.get('open_id')}")
        msg_id = event.get("message_id", "")
        create_time = int(event.get("create_time") or 0)

        # Normalize create_time to milliseconds.
        # WebSocket (lark-cli compact) delivers ms (13-digit); Feishu REST API delivers s (10-digit).
        create_time_ms = create_time
        if create_time and create_time < 10_000_000_000:  # looks like epoch seconds
            create_time_ms = create_time * 1000

        # Drop messages sent before this bot instance started.
        # Must happen BEFORE mark_message_seen — otherwise the message is marked as
        # handled but never processed, and no other pod can pick it up.
        if create_time_ms and create_time_ms < _startup_ms:
            logger.info(f"Skipping pre-startup message {msg_id} (msg_ms={create_time_ms} < startup_ms={_startup_ms})")
            return

        # Atomic dedup — INSERT OR IGNORE into seen_messages; works across pods via PostgreSQL.
        # Skip if poll already claimed this message (to avoid double-claim failure).
        if msg_id and not event.get("_poll_claimed"):
            owner = f"{os.environ.get('HOSTNAME', '')}:{os.getpid()}"
            if hasattr(store, "claim_message"):
                reclaim_after = int(os.environ.get("BOT_MESSAGE_RECLAIM_SECONDS", "1800"))
                claimed = store.claim_message(msg_id, reclaim_after_seconds=reclaim_after, owner=owner)
            else:
                claimed = store.mark_message_seen(msg_id)
            if not claimed:
                logger.info(f"Duplicate message {msg_id}, skipping")
                return
            msg_claimed = True
            claimed_msg_id = msg_id
            logger.debug(f"Claimed message {msg_id} from {event.get('open_id')}")
        elif msg_id and event.get("_poll_claimed"):
            msg_claimed = True
            claimed_msg_id = msg_id

        # Track P2P chat_id and last event time for missed-message recovery.
        # Only P2P chats are polled — group recovery requires @mention context
        # that the poll API doesn't reliably provide.
        chat_id = event.get("chat_id", "")
        if chat_id and event.get("chat_type", "p2p") == "p2p":
            _known_chat_ids.add(chat_id)

        if create_time:
            _last_event_epoch = str(create_time_ms // 1000 + 1)

        open_id = event["open_id"]
        text = event["text"]
        chat_type = event.get("chat_type", "p2p")
        is_group = chat_type == "group"
        chat_id = event.get("chat_id", "")
        # parent_id is set when the user quote-replies to a specific message.
        # Distinct from root_id (which scopes to a thread). When present, we
        # fetch the quoted message body via API so Claude has the full context.
        parent_id = event.get("parent_id", "")

        # Context ID for auth/session isolation:
        #   P2P:   context_id = open_id
        #   Group: context_id = "g_{chat_id}_{open_id}"
        context_id = f"g_{chat_id}_{open_id}" if is_group else open_id

        # Group replies go back as thread replies to the triggering @mention message
        reply_msg_id = event.get("message_id") if is_group else None

        # Group sessions are per-user-per-thread. Each user keeps their own Claude
        # conversation history, but when starting a new session the thread history
        # is injected as context so Claude understands the full picture.
        if is_group:
            event_root_id = event.get("root_id", "")
            # Always fetch full message for groups — compact event lacks parent_id
            # (quote-reply target) and often lacks root_id and mentions too.
            try:
                _token = feishu_api.get_tenant_access_token(app_id, app_secret)
                full_msg = feishu_api.get_message(msg_id, _token)
                if not event_root_id:
                    event_root_id = full_msg.get("root_id", "")
                # parent_id: only set when user quote-replied to a specific message
                if not parent_id:
                    parent_id = full_msg.get("parent_id", "") or full_msg.get("upper_message_id", "")
                # @mention verification (compact format may omit `mentions`)
                if not event.get("mentioned") and bot_open_id:
                    api_mentions = full_msg.get("mentions", [])
                    if _is_bot_mentioned({"mentions": api_mentions}, bot_open_id):
                        event["mentioned"] = True
                logger.debug(
                    f"Group msg API lookup: msg_id={msg_id} root_id={event_root_id!r} "
                    f"parent_id={parent_id!r} keys={list(full_msg.keys())[:15]}"
                )
            except Exception as e:
                logger.warning(f"Full-message API lookup failed for {msg_id}: {e}")
            # Fall back to message_id only for messages that truly start a thread
            root_id = event_root_id or event.get("message_id", "")
            thread_session_key = f"{context_id}:{root_id}"
        else:
            root_id = ""
            event_root_id = ""
            thread_session_key = None

        # Eagerly fetch and cache display_name so all logs show a human name
        if not store.get_display_name(open_id):
            try:
                _dn_token = feishu_api.get_tenant_access_token(app_id, app_secret)
                _dn = feishu_api.get_user_display_name(open_id, _dn_token)
                if _dn:
                    store.set_display_name(open_id, _dn)
            except Exception:
                pass

        conv = f"群聊 {chat_id}" if is_group else "私聊"
        _dn_now = _display_name(open_id, store)
        _is_at_bot = bool(event.get("mentioned"))
        # stdout: full content for private chats and group@-bot mentions.
        # Log every parsed message so group delivery issues are visible.
        logger.info(f"[收到] {_dn_now} | {conv} | {text}")
        if _audit_store is not None:
            _audit_store.log_received(
                open_id=open_id, display_name=_dn_now,
                chat_type=chat_type, chat_id=chat_id,
                message_id=msg_id, is_at_bot=_is_at_bot, content=text,
            )

        user = store.get_user(context_id)
        logger.debug(f"User state: {user}")

        # Clean up stale meegle pending state so it doesn't linger indefinitely.
        # _poll_meegle_and_resume resets on timeout, but if the bot restarted mid-poll
        # or Claude triggered meegle auth and the poller never started, the record stays.
        if user and store.is_meegle_pending_expired(context_id):
            logger.info(f"Clearing expired meegle pending state for {context_id}")
            store.reset_meegle_auth(context_id)
            user = store.get_user(context_id)  # refresh

        # Pod-start token validation: on a user's first message after pod restart, verify
        # their lark-cli token is still accessible. Tokens live on the PVC; a PVC change
        # (or the user's home directory being on a different mount) causes silent 401s
        # inside Claude that lark_reauth_cli.py would then fail to handle. Catching it
        # here gives a clean auth card instead of a confusing error from within Claude.
        # Runs once per user per pod instance (in-memory set, cleared on restart).
        if (user and user["auth_status"] == "authorized"
                and context_id not in _pod_verified_users):
            _pod_verified_users.add(context_id)
            if not auth.is_authenticated(context_id):
                logger.info(
                    f"[auth-flow] Pod-start token check failed for {_display_name(open_id, store)} — resetting auth"
                )
                store.reset_auth(context_id)
                user = store.get_user(context_id)

        # Group messages, including replies inside an existing topic, must
        # explicitly @mention the bot before they are considered relevant.
        if is_group and not event.get("mentioned", True):
            _log_unmatched_group_message(chat_id, msg_id, open_id, text)
            logger.debug(f"Ignoring group message without @mention: {msg_id}")
            return

        # Handle control commands before auth state routing. Otherwise an expired
        # pending user can have "/reauth" stored as the request to resume.
        if _handle_meegle_reauth_command(open_id, text, context_id,
                                         auth, store, app_id, app_secret):
            return

        if _handle_reauth_command(open_id, text, context_id,
                                  auth, store, app_id, app_secret, auth_executor,
                                  reply_msg_id, thread_session_key,
                                  root_id, chat_id, chat_type, agent):
            return

        if _handle_reset_command(open_id, text, context_id, store, app_id, app_secret,
                                  thread_session_key, chat_id, root_id):
            return

        # New user or expired pending — check existing auth, or start OAuth.
        # Also check Meegle here so that when both are needed we can issue a single
        # combined card instead of two sequential auth rounds.
        _auth_status_now = user["auth_status"] if user else "null"
        _pending_expired = (
            user is not None
            and user["auth_status"] == "pending"
            and store.is_pending_expired(context_id)
        )
        _dname = _display_name(open_id, store)
        if user is None or _pending_expired:
            logger.info(
                f"[auth-flow] {_dname} auth_status={_auth_status_now!r} "
                f"pending_expired={_pending_expired} user_none={user is None}"
            )
            lark_ok = auth.is_authenticated(context_id)
            if lark_ok:
                logger.info(f"[auth-flow] {_dname} token valid on disk, marking authorized")
                store.mark_authorized(context_id)
                user = store.get_user(context_id)
            else:
                logger.info(f"[auth-flow] {_dname} token missing/expired, starting new auth flow")
                # Lark auth required. Don't proactively start Meegle auth here —
                # Claude will request it on demand when meegle commands are needed.
                need_meegle_now = False
                lark_data: dict = {}
                meegle_data: dict = {}
                try:
                    lark_data = auth.start_auth(context_id)
                except Exception as e:
                    logger.error(f"Auth start failed for {context_id}: {e}")
                    send_feishu_message(open_id, "授权初始化失败，请稍后重试。", app_id, app_secret)
                    return
                if need_meegle_now:
                    try:
                        meegle_data = auth.start_meegle_auth(context_id)
                    except Exception as e:
                        logger.warning(f"Meegle auth start failed for {context_id} (non-fatal): {e}")
                        # Fall back to Lark-only card; Meegle will be handled later
                        meegle_data = {}

                if meegle_data:
                    _create_auth_resume_job(
                        store,
                        context_id=context_id,
                        provider="lark",
                        device_code=lark_data["device_code"],
                        resume_text=text,
                        reply_id=reply_msg_id,
                        thread_key=thread_session_key,
                        root_id=root_id,
                        chat_id=chat_id,
                        chat_type=chat_type,
                    )
                    _create_auth_resume_job(
                        store,
                        context_id=context_id,
                        provider="meegle",
                        device_code=meegle_data["device_code"],
                        client_id=meegle_data.get("client_id", ""),
                        resume_text=text,
                        reply_id=reply_msg_id,
                        thread_key=thread_session_key,
                        root_id=root_id,
                        chat_id=chat_id,
                        chat_type=chat_type,
                    )
                    auth_executor.submit(
                        _start_combined_auth_and_poll,
                        open_id, text,
                        lark_data["device_code"], lark_data["verification_url"],
                        meegle_data["client_id"], meegle_data["device_code"], meegle_data["url"],
                        auth, store, agent, app_id, app_secret,
                        context_id, reply_msg_id, thread_session_key, root_id,
                        chat_id, chat_type,
                    )
                else:
                    _create_auth_resume_job(
                        store,
                        context_id=context_id,
                        provider="lark",
                        device_code=lark_data["device_code"],
                        resume_text=text,
                        reply_id=reply_msg_id,
                        thread_key=thread_session_key,
                        root_id=root_id,
                        chat_id=chat_id,
                        chat_type=chat_type,
                    )
                    auth_executor.submit(
                        _start_auth_and_poll,
                        open_id, text, lark_data["device_code"], lark_data["verification_url"],
                        auth, store, agent, app_id, app_secret,
                        context_id, reply_msg_id, thread_session_key, root_id,
                        chat_id, chat_type,
                    )
                return

        # Handle control commands first — before any expensive subprocess calls.
        # These must run regardless of Meegle auth state, and even while auth is pending
        # so users can escape a stuck auth loop with /reset or /reauth.

        # /meegle-reauth — revoke meegle token and reset DB so next message re-triggers OAuth.
        # Must run BEFORE _handle_reauth_command: the lark handler does substring match on
        # "重新授权", which would otherwise swallow "重新授权 meegle" / "重新授权meegle".
        if _handle_meegle_reauth_command(open_id, text, context_id,
                                         auth, store, app_id, app_secret):
            return

        # /reauth — revoke old token and restart OAuth (handles scope-upgrade case)
        if _handle_reauth_command(open_id, text, context_id,
                                  auth, store, app_id, app_secret, auth_executor,
                                  reply_msg_id, thread_session_key,
                                  root_id, chat_id, chat_type, agent):
            return

        # /reset — clear Claude session so next message starts fresh
        if _handle_reset_command(open_id, text, context_id, store, app_id, app_secret,
                                  thread_session_key, chat_id, root_id):
            return

        # Pending but not yet expired — try to complete auth, or remind user
        if user and user["auth_status"] == "pending":
            code = user.get("pending_code", "")
            pending_at = user.get("pending_at", "")
            logger.info(
                f"[auth-flow] {_dname} status=pending "
                f"code={'set' if code else 'none'} pending_at={pending_at!r}"
            )
            if code and auth.poll_once(context_id, code):
                logger.info(f"[auth-flow] {_dname} poll_once succeeded inline, completing durable resume")
                resumed = _complete_auth_and_resume(
                    open_id=open_id,
                    context_id=context_id,
                    provider="lark",
                    device_code=code,
                    auth=auth,
                    store=store,
                    agent=agent,
                    app_id=app_id,
                    app_secret=app_secret,
                )
                if not resumed:
                    _notify_auth_resume_missing(open_id, None, app_id, app_secret)
                return
            elif auth.is_authenticated(context_id):
                logger.info(f"[auth-flow] {_dname} disk check succeeded inline, marking authorized")
                store.mark_authorized(context_id)
            else:
                # Still waiting — do NOT start a new auth (would overwrite the active
                # device code and break the existing poller). Just remind the user.
                logger.info(f"[auth-flow] {_dname} still waiting for user to authorize")
                pending_url = user.get("pending_url", "")
                if is_group:
                    try:
                        token = feishu_api.get_tenant_access_token(app_id, app_secret)
                        if pending_url:
                            _send_lark_auth_prompt(
                                open_id,
                                pending_url,
                                token,
                                reply_msg_id=reply_msg_id,
                                chat_id=chat_id,
                                chat_type=chat_type,
                                store=store,
                            )
                        else:
                            if reply_msg_id:
                                feishu_api.reply_card_in_thread(
                                    reply_msg_id,
                                    _group_lark_auth_waiting_text(open_id, store),
                                    token,
                                )
                            feishu_api.send_text_card(
                                open_id,
                                "正在等待飞书授权完成。授权完成后，机器人会自动回到群话题继续处理。",
                                token,
                            )
                    except Exception as e:
                        logger.warning(f"Could not send group pending auth reminder: {e}")
                    if code and context_id not in _active_auth_pollers and auth_executor:
                        _active_auth_pollers.add(context_id)
                        logger.info(f"[auth-flow] {_dname} starting recovery poller for existing pending auth")
                        auth_executor.submit(
                            _recover_pending_auth_and_resume,
                            open_id, text, code,
                            auth, store, agent, app_id, app_secret,
                            context_id, None, thread_session_key, root_id,
                            chat_id, chat_type,
                        )
                    return
                if pending_url:
                    send_feishu_message(
                        open_id,
                        f"请完成飞书授权，授权后将自动继续处理您的请求：\n\n[点击授权]({pending_url})\n\n"
                        "如授权后未自动刷新，请对我说「重新授权」",
                        app_id, app_secret,
                    )
                else:
                    send_feishu_message(
                        open_id,
                        "正在等待授权完成，请稍候。\n\n如授权后未自动刷新，请对我说「重新授权」",
                        app_id, app_secret,
                    )
                # Start a recovery poller on this pod in case the original poller
                # (on another pod, or already timed out) is no longer watching.
                if code and context_id not in _active_auth_pollers and auth_executor:
                    _active_auth_pollers.add(context_id)
                    logger.info(f"[auth-flow] {_dname} starting recovery poller for existing pending auth")
                    auth_executor.submit(
                        _recover_pending_auth_and_resume,
                        open_id, text, code,
                        auth, store, agent, app_id, app_secret,
                        context_id, None, thread_session_key, root_id,
                        chat_id, chat_type,
                    )
                return

        # Usage command — handle before Claude
        if _handle_usage_command(open_id, text, app_id, app_secret, store):
            return

        user = _reconcile_meegle_state_before_use(context_id, auth, store, text, user)

        # "继续" — user resuming after turn limit; expand to a clearer prompt for Claude
        if text.strip() == "继续":
            _sess = (store.get_thread_session(thread_session_key) if thread_session_key
                     else store.get_session_id(context_id))
            if _sess:
                text = "请继续之前未完成的任务。"

        # Authorized — stream Claude Code output into Feishu card
        _user_before = store.get_user(context_id) or {}
        lark_pending_at_before = _user_before.get("pending_at", "")
        meegle_pending_at_before = _user_before.get("meegle_pending_at", "")
        _claude_card_id: str | None = None
        _image_key = event.get("image_key", "")
        _file_key = event.get("file_key", "")
        _file_name = event.get("file_name", "")

        # Backpressure: limit concurrent Claude sessions
        if not _claude_semaphore.acquire(blocking=False):
            logger.info(f"Claude semaphore full, queuing {_dname}")
            try:
                token = feishu_api.get_tenant_access_token(app_id, app_secret)
                if reply_msg_id:
                    feishu_api.reply_card_in_thread(
                        reply_msg_id, "⏳ 当前请求较多，正在排队中，请稍候…", token)
                else:
                    feishu_api.send_text_card(
                        open_id, "⏳ 当前请求较多，正在排队中，请稍候…", token)
            except Exception as e:
                logger.warning(f"Could not send queue notification: {e}")
            _claude_semaphore.acquire()

        try:
            _claude_card_id = _stream_claude(open_id, text, agent, store, app_id, app_secret,
                                             context_id=context_id, reply_msg_id=reply_msg_id,
                                             thread_session_key=thread_session_key, root_id=root_id,
                                             chat_id=chat_id, chat_type=chat_type,
                                             image_key=_image_key, image_message_id=msg_id,
                                             file_key=_file_key, file_name=_file_name,
                                             parent_id=parent_id,
                                             oa_api_key=oa_api_key)
        except TokenExpiredError:
            store.reset_auth(context_id)
            try:
                data = auth.start_auth(context_id)
                _create_auth_resume_job(
                    store,
                    context_id=context_id,
                    provider="lark",
                    device_code=data["device_code"],
                    resume_text=text,
                    reply_id=reply_msg_id,
                    thread_key=thread_session_key,
                    root_id=root_id,
                    chat_id=chat_id,
                    chat_type=chat_type,
                    existing_msg_id=_claude_card_id or "",
                )
                auth_executor.submit(
                    _start_auth_and_poll,
                    open_id, text, data["device_code"], data["verification_url"],
                    auth, store, agent, app_id, app_secret,
                    context_id, reply_msg_id, thread_session_key, root_id,
                    chat_id, chat_type,
                )
            except Exception as e:
                logger.error(f"Re-auth failed for {context_id}: {e}")
                send_feishu_message(open_id, "授权已过期，重新授权时出现错误，请稍后重试。", app_id, app_secret)
        except Exception as e:
            logger.exception(f"Agent error for {_display_name(open_id, store)}: {e}")
            send_feishu_message(open_id, "处理您的请求时出现错误，请稍后重试。", app_id, app_secret)
        finally:
            _claude_semaphore.release()

        # If Claude triggered auth during this response, start auto-resume poller(s).
        # Compare pending_at timestamps so a new auth triggered while a previous one
        # is still pending also gets a fresh poller.
        user_after = store.get_user(context_id)
        lark_pending_at_after = (user_after or {}).get("pending_at", "")
        meegle_pending_at_after = (user_after or {}).get("meegle_pending_at", "")

        need_lark_poller = (
            (user_after or {}).get("auth_status") == "pending"
            and lark_pending_at_after != lark_pending_at_before
            and (user_after or {}).get("pending_code")
        )
        need_meegle_poller = (
            (user_after or {}).get("meegle_auth_status") == "pending"
            and meegle_pending_at_after != meegle_pending_at_before
            and (user_after or {}).get("meegle_pending_code")
            and (user_after or {}).get("meegle_pending_client_id")
        )

        if need_lark_poller and need_meegle_poller:
            l_code = user_after["pending_code"]
            m_code = user_after["meegle_pending_code"]
            m_client = user_after["meegle_pending_client_id"]
            logger.info(f"Both lark+meegle auth pending for {context_id}, starting combined poller")
            _create_auth_resume_job(
                store,
                context_id=context_id,
                provider="lark",
                device_code=l_code,
                resume_text=text,
                reply_id=reply_msg_id,
                thread_key=thread_session_key,
                root_id=root_id,
                chat_id=chat_id,
                chat_type=chat_type,
                existing_msg_id=_claude_card_id or "",
            )
            _create_auth_resume_job(
                store,
                context_id=context_id,
                provider="meegle",
                device_code=m_code,
                client_id=m_client,
                resume_text=text,
                reply_id=reply_msg_id,
                thread_key=thread_session_key,
                root_id=root_id,
                chat_id=chat_id,
                chat_type=chat_type,
                existing_msg_id=_claude_card_id or "",
            )
            auth_executor.submit(
                _start_combined_auth_and_poll,
                open_id, text,
                l_code, user_after.get("pending_url", ""),
                m_client, m_code, user_after.get("meegle_pending_url", ""),
                auth, store, agent, app_id, app_secret,
                context_id, None, thread_session_key, root_id,
                chat_id, chat_type,
            )
        elif need_lark_poller:
            l_code = user_after["pending_code"]
            logger.info(f"Lark reauth pending for {context_id}, starting auto-resume poller "
                        f"(card={_claude_card_id})")
            _create_auth_resume_job(
                store,
                context_id=context_id,
                provider="lark",
                device_code=l_code,
                resume_text=text,
                reply_id=reply_msg_id,
                thread_key=thread_session_key,
                root_id=root_id,
                chat_id=chat_id,
                chat_type=chat_type,
                existing_msg_id=_claude_card_id or "",
            )
            auth_executor.submit(
                _poll_lark_reauth_and_resume,
                open_id, text, l_code,
                auth, store, agent, app_id, app_secret,
                context_id, reply_msg_id, thread_session_key, root_id,
                chat_id, chat_type,
                _claude_card_id,
            )
        elif need_meegle_poller:
            m_code = user_after["meegle_pending_code"]
            m_client = user_after["meegle_pending_client_id"]
            logger.info(f"Meegle auth pending for {context_id}, starting auto-resume poller "
                        f"(card={_claude_card_id})")
            _create_auth_resume_job(
                store,
                context_id=context_id,
                provider="meegle",
                device_code=m_code,
                client_id=m_client,
                resume_text=text,
                reply_id=reply_msg_id,
                thread_key=thread_session_key,
                root_id=root_id,
                chat_id=chat_id,
                chat_type=chat_type,
                existing_msg_id=_claude_card_id or "",
            )
            auth_executor.submit(
                _poll_meegle_and_resume,
                open_id, text, m_client, m_code,
                auth, store, agent, app_id, app_secret,
                context_id, reply_msg_id, thread_session_key, root_id,
                chat_id, chat_type,
                _claude_card_id,
            )
    except Exception as e:
        fatal_error = True
        logger.exception(f"FATAL handle_message error: {e}")
    finally:
        if msg_claimed and claimed_msg_id and not fatal_error and hasattr(store, "complete_message"):
            try:
                store.complete_message(claimed_msg_id)
            except Exception as e:
                logger.warning(f"Could not mark message complete {claimed_msg_id}: {e}")


def _pid_alive(pid: int) -> bool:
    """Return True if a process with the given PID is still running."""
    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=5,
            )
            return str(pid) in result.stdout
        else:
            os.kill(pid, 0)
            return True
    except (OSError, subprocess.SubprocessError):
        return False


def _acquire_pid_lock() -> None:
    """Ensure only one bot instance runs at a time via a PID file."""
    import atexit
    if os.environ.get("BOT_DISABLE_PID_LOCK", "").strip().lower() in ("1", "true", "yes", "on"):
        logger.info("PID lock disabled by BOT_DISABLE_PID_LOCK")
        return
    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        logger.info("Skipping PID lock in Kubernetes; use DB leases for multi-pod coordination")
        return
    # /app is read-only for botuser (chmod 750, owned by root:botuser) so the
    # PID file must live somewhere writable. Order of preference:
    #   1. $BOT_PID_FILE   — explicit override
    #   2. /var/lark-bot/bot.pid — the persistent volume in the prod container
    #   3. ./bot.pid in CWD — local dev fallback
    pid_file = os.environ.get("BOT_PID_FILE", "").strip()
    if not pid_file:
        if os.path.isdir("/var/lark-bot") and os.access("/var/lark-bot", os.W_OK):
            pid_file = "/var/lark-bot/bot.pid"
        else:
            pid_file = os.path.join(os.path.dirname(__file__), "..", "bot.pid")
    pid_file = os.path.abspath(pid_file)
    if os.path.exists(pid_file):
        try:
            old_pid = int(open(pid_file).read().strip())
            if old_pid != os.getpid() and _pid_alive(old_pid):
                print(f"ERROR: Another bot instance is already running (PID {old_pid}). "
                      f"Stop it first or delete {pid_file}.", flush=True)
                sys.exit(1)
        except (ValueError, OSError):
            pass  # stale or unreadable PID file — proceed
    with open(pid_file, "w") as f:
        f.write(str(os.getpid()))
    atexit.register(lambda: os.path.exists(pid_file) and os.remove(pid_file))


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

    if mode != "lark_cli":
        raise ValueError("event ingress mode must be 'lark_cli' or 'sdk'")

    card_action_listener = start_card_action_listener(
        cfg.feishu_app_id, cfg.feishu_app_secret, card_action_handler,
    )
    listener = EventListener(
        bot_home=cfg.lark_bot_home,
        on_message=on_message,
        on_poll=on_poll,
        bot_open_id=cfg.feishu_bot_open_id,
        on_bot_added=on_bot_added,
    )
    listener.start()
    return listener, card_action_listener


def main():
    # Load .env / .env.local up-front so env-driven options like BOT_LOG_FILE
    # and BOT_DEBUG are honoured. Config() below also calls load_dotenv() to
    # populate its own attributes; calling it twice is idempotent.
    try:
        from dotenv import load_dotenv as _load_dotenv
        _load_dotenv(".env.local", override=True)
        _load_dotenv()
    except ImportError:
        pass

    _fmt = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
    _log_level = logging.DEBUG if os.environ.get("BOT_DEBUG") else logging.INFO
    logging.basicConfig(level=_log_level, format=_fmt)

    # Also write to a persistent log file so history survives container restarts.
    # The file lives under /var/lark-bot (a mounted volume in Docker).
    _log_path = os.environ.get("BOT_LOG_FILE", "/var/lark-bot/bot.log")
    try:
        from logging.handlers import RotatingFileHandler
        _fh = RotatingFileHandler(_log_path, maxBytes=50 * 1024 * 1024, backupCount=5,
                                  encoding="utf-8")
        _fh.setFormatter(logging.Formatter(_fmt))
        _fh.setLevel(logging.DEBUG)
        logging.getLogger().addHandler(_fh)
        logging.getLogger(__name__).info(f"Logging to file: {_log_path}")
    except OSError as e:
        logging.getLogger(__name__).warning(f"Could not open log file {_log_path}: {e}")
    _acquire_pid_lock()
    cfg = Config()
    store = UserStore(cfg.postgres_url)
    try:
        deleted = store.cleanup_seen_messages(max_age_hours=48)
        if deleted:
            logger.info(f"Cleaned up {deleted} stale seen_messages entries")
    except Exception as e:
        logger.warning(f"cleanup_seen_messages failed (non-fatal): {e}")
    job_store = JobStore(cfg.postgres_url)
    form_store = FormStore(cfg.postgres_url)
    auth = AuthManager(store, users_dir=cfg.lark_users_dir, bot_home=cfg.lark_bot_home,
                       app_id=cfg.feishu_app_id, app_secret=cfg.feishu_app_secret)
    form_service = InteractiveFormService(
        form_store, feishu_api, cfg.feishu_app_id, cfg.feishu_app_secret,
    )

    # Shared cross-pod audit log. INSERTs are fail-open and rate-limited so a
    # transient PG outage doesn't impact message processing.
    global _audit_store
    _audit_store = AuditStore(cfg.postgres_url)
    # One-shot retention prune at startup; the scheduler runs it daily after.
    try:
        _retention_days = int(os.environ.get("BOT_AUDIT_RETENTION_DAYS", "90"))
        _purged = _audit_store.cleanup_older_than(_retention_days)
        if _purged:
            logger.info(f"[audit] purged {_purged} rows older than {_retention_days} days at startup")
    except Exception as e:
        logger.warning(f"[audit] startup cleanup failed (non-fatal): {e}")

    from src.internal_api import start_internal_api
    global _api_port, _api_registry
    _api_port, _api_registry = start_internal_api(
        store, auth, job_store, form_service=form_service,
    )

    # Start the egress proxy used by sandboxed Claude subprocesses to reach the
    # internet. Blocks RFC1918 / K8s service domains / cloud-metadata IPs.
    # Set BOT_EGRESS_PROXY=0 to disable (e.g. local dev).
    global _egress_proxy_port
    _egress_proxy_port = 0
    if os.environ.get("BOT_EGRESS_PROXY", "1").strip().lower() not in ("0", "off", "false", "no"):
        try:
            from src.egress_proxy import start_egress_proxy
            _egress_proxy_port = start_egress_proxy(port=int(os.environ.get("BOT_EGRESS_PROXY_PORT", "7890")))
        except Exception as e:
            logger.warning(f"Could not start egress proxy (non-fatal): {e}")

    # Start the per-user file watcher: detects scripts in user_home that try to
    # abuse bot-level credentials. Alerts go to BOT_SECURITY_ALERT_CHAT_ID
    # (chat_id of an internal security/ops chat), or just to the log when unset.
    if os.environ.get("BOT_FILE_WATCHER", "1").strip().lower() not in ("0", "off", "false", "no"):
        try:
            from src.file_watcher import FileWatcher
            _watcher = FileWatcher(
                users_dir=cfg.lark_users_dir,
                store=store,
                feishu_api=feishu_api,
                app_id=cfg.feishu_app_id,
                app_secret=cfg.feishu_app_secret,
                alert_chat_id=os.environ.get("BOT_SECURITY_ALERT_CHAT_ID", "").strip(),
            )
            _watcher.start()
        except Exception as e:
            logger.warning(f"Could not start file watcher (non-fatal): {e}")

    # Set env vars for Claude Code CLI to route through OpenRouter.
    # 下周去掉公共 key：删除 env 里的 ANTHROPIC_AUTH_TOKEN 或移除下面这行即可，
    # 无个人 key 的用户会在 _stream_claude 里被阻断并收到申请提示。
    if cfg.anthropic_auth_token:
        os.environ["ANTHROPIC_API_KEY"] = cfg.anthropic_auth_token
    os.environ["ANTHROPIC_BASE_URL"] = cfg.anthropic_base_url

    agent = Agent(users_dir=cfg.lark_users_dir, bot_home=cfg.lark_bot_home, model=cfg.claude_model,
                  real_claude_dir=cfg.claude_home or None)

    executor = ThreadPoolExecutor(max_workers=20)
    _auth_executor = ThreadPoolExecutor(max_workers=5, thread_name_prefix="auth-poller")
    form_completion_runner = _make_form_completion_runner(
        agent, store, cfg.feishu_app_id, cfg.feishu_app_secret,
        executor=executor, oa_api_key=cfg.oa_api_key, form_store=form_store,
        auth=auth, auth_executor=_auth_executor,
    )
    form_card_update_runner = _make_form_card_update_runner(
        form_store, feishu_api, cfg.feishu_app_id, cfg.feishu_app_secret, executor=executor,
    )
    card_action_handler = InteractiveFormHandler(
        form_store, feishu_api, cfg.feishu_app_id, cfg.feishu_app_secret,
        on_completed=form_completion_runner,
        on_card_update=form_card_update_runner,
        defer_card_updates=_form_defer_card_updates(),
    )
    scheduler = SchedulerThread(
        job_store=job_store,
        executor=executor,
        feishu_api=feishu_api,
        app_id=cfg.feishu_app_id,
        app_secret=cfg.feishu_app_secret,
        stream_claude_fn=_make_scheduled_task_runner(
            agent, store, cfg.feishu_app_id, cfg.feishu_app_secret,
            oa_api_key=cfg.oa_api_key,
        ),
    )

    # Daily audit retention: a small daemon thread runs cleanup once every 24h.
    # The cleanup itself uses pg_try_advisory_lock so only one pod across the
    # fleet actually deletes; the rest skip silently.
    def _audit_cleanup_loop():
        retention_days = int(os.environ.get("BOT_AUDIT_RETENTION_DAYS", "90"))
        while True:
            time.sleep(24 * 60 * 60)
            if _audit_store is None:
                continue
            try:
                deleted = _audit_store.cleanup_older_than(retention_days)
                if deleted:
                    logger.info(f"[audit] daily purge removed {deleted} rows older than {retention_days} days")
            except Exception as e:
                logger.warning(f"[audit] daily purge failed: {e}")
    threading.Thread(target=_audit_cleanup_loop, daemon=True, name="audit-cleanup").start()

    def on_message(event: dict):
        logger.debug(f"on_message received event: {event.get('open_id')}")
        executor.submit(
            handle_message, event, store, auth, agent,
            cfg.feishu_app_id, cfg.feishu_app_secret, executor,
            _auth_executor,
            cfg.feishu_bot_open_id, cfg.oa_api_key,
        )

    def on_poll():
        """Periodically poll Feishu API for messages the WebSocket may have missed."""
        if not _known_chat_ids or not _last_event_epoch:
            return  # no chats known yet, nothing to poll
        try:
            token = feishu_api.get_tenant_access_token(cfg.feishu_app_id, cfg.feishu_app_secret)
            for chat_id in list(_known_chat_ids):
                msgs = feishu_api.list_p2p_messages(chat_id, _last_event_epoch, token)
                for msg in msgs:
                    mid = msg.get("message_id", "")
                    if msg.get("msg_type") != "text":
                        continue
                    sender = msg.get("sender", {})
                    if sender.get("sender_type") != "user":
                        continue
                    open_id = sender.get("id", "")
                    try:
                        content = json.loads(msg.get("body", {}).get("content", "{}"))
                        text = content.get("text", "").strip()
                    except (json.JSONDecodeError, AttributeError):
                        continue
                    if not open_id or not text:
                        continue
                    create_time = msg.get("create_time", "")
                    _dn = _display_name(open_id, store)
                    _chat_type = msg.get("chat_type", "p2p")
                    _mentions = msg.get("mentions") or []
                    _is_at_bot = bool(cfg.feishu_bot_open_id) and any(
                        (m.get("id") or m.get("key")) == cfg.feishu_bot_open_id
                        for m in _mentions
                    )
                    # Skip group messages where bot isn't @mentioned.
                    # _known_chat_ids should only contain P2P IDs, but guard here
                    # in case a group ID slipped in from a previous deployment.
                    if _chat_type == "group" and not _is_at_bot:
                        continue
                    if mid:
                        owner = f"{os.environ.get('HOSTNAME', '')}:{os.getpid()}:poll"
                        if hasattr(store, "claim_message"):
                            reclaim_after = int(os.environ.get("BOT_MESSAGE_RECLAIM_SECONDS", "1800"))
                            if not store.claim_message(mid, reclaim_after_seconds=reclaim_after, owner=owner):
                                continue
                        elif not store.mark_message_seen(mid):
                            continue
                    logger.info(f"Poll recovered message {mid} from {_dn}: {text}")
                    if _audit_store is not None:
                        _audit_store.log_poll_recovered(
                            open_id=open_id, display_name=_dn,
                            chat_type=_chat_type, chat_id=chat_id,
                            message_id=mid, is_at_bot=_is_at_bot, content=text,
                        )
                    on_message({
                        "open_id": open_id,
                        "text": text,
                        "message_id": mid,
                        "create_time": create_time,
                        "chat_id": chat_id,
                        "chat_type": _chat_type,
                        "_poll_claimed": True,  # poll already claimed the message lease
                    })
        except Exception as e:
            logger.debug(f"Poll failed: {e}")

    def on_bot_added(event: dict):
        executor.submit(handle_bot_added, event, cfg.feishu_app_id, cfg.feishu_app_secret)

    listener, card_action_listener = _start_event_ingress(
        mode=cfg.bot_event_ingress,
        cfg=cfg,
        on_message=on_message,
        on_poll=on_poll,
        on_bot_added=on_bot_added,
        card_action_handler=card_action_handler,
    )
    scheduler.start()

    # Convert SIGTERM (sent by docker stop / systemd) into KeyboardInterrupt
    # so the finally block below runs and cleans up gracefully.
    def _sigterm(signum, frame):
        raise KeyboardInterrupt
    try:
        signal.signal(signal.SIGTERM, _sigterm)
    except (AttributeError, OSError):
        pass  # not available on all platforms

    logger.info("Feishu AI Bot started. Listening for messages...")
    try:
        signal.pause()
    except AttributeError:
        # Windows: signal.pause() unavailable
        stop = threading.Event()
        try:
            stop.wait()
        except KeyboardInterrupt:
            pass
    except KeyboardInterrupt:
        pass
    finally:
        if card_action_listener:
            card_action_listener.stop()
        listener.stop()
        scheduler.stop()
        _auth_executor.shutdown(wait=False)
        executor.shutdown(wait=True)
        store.close()
        job_store.close()
        form_store.close()


if __name__ == "__main__":
    main()
