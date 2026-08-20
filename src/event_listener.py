import glob
import json
import logging
import os
import re
import subprocess
import sys
import threading
from typing import Callable, Optional

logger = logging.getLogger(__name__)

if sys.platform == "win32":
    _LARK_CLI_BIN = os.path.join(
        os.environ.get("APPDATA", ""),
        "npm", "node_modules", "@larksuite", "cli", "bin", "lark-cli.exe",
    )
    _LARK_CLI_CMD = [_LARK_CLI_BIN]
else:
    _LARK_CLI_CMD = ["lark-cli"]

_CONSUME_EVENT_KEYS = (
    "im.message.receive_v1",
    "im.chat.member.bot.added_v1",
)
RECONNECT_DELAY = 3    # seconds — fast reconnect to minimise message loss


def _event_consume_cmd(event_key: str) -> list[str]:
    return [*_LARK_CLI_CMD, "event", "consume", event_key, "--as", "bot"]


def _value_at(obj, *names: str):
    current = obj
    for name in names:
        if current is None:
            return ""
        if isinstance(current, dict):
            current = current.get(name)
        else:
            current = getattr(current, name, None)
    return current or ""


def _mention_open_id(mention) -> str:
    raw_id = _value_at(mention, "id")
    if isinstance(raw_id, dict):
        return str(raw_id.get("open_id") or raw_id.get("openId") or "")
    if raw_id and not isinstance(raw_id, (dict, list, tuple)):
        return str(raw_id)
    return str(_value_at(mention, "open_id") or _value_at(mention, "openId") or "")


def _mention_is_bot(mention) -> bool:
    value = _value_at(mention, "is_bot") or _value_at(mention, "isBot")
    return value is True or str(value).strip().lower() == "true"


def _is_bot_mentioned_legacy(data: dict, bot_open_id: str) -> bool:
    """
    Return True if the bot is @mentioned in this event.

    Checks the `mentions` field (array of objects with id/open_id) first.
    Falls back to checking whether content starts with '@' when mentions is absent.
    If bot_open_id is empty, any non-empty mentions list is treated as a match.
    """
    mentions = data.get("mentions")
    if mentions is not None:
        if not mentions:
            return False  # empty mentions list — no one is mentioned
        if not bot_open_id:
            return True  # mentions present but can't verify — assume bot is mentioned
        for m in mentions:
            if isinstance(m, dict):
                if _mention_is_bot(m):
                    return True
                mid = _mention_open_id(m)
                if mid == bot_open_id:
                    return True
            elif isinstance(m, str) and m == bot_open_id:
                return True
        return False
    # No mentions field — cannot determine who is mentioned; treat as not mentioned
    return False


def _is_bot_mentioned(data: dict, bot_open_id: str) -> bool:
    """Return True only when a mention resolves to the configured bot open_id."""
    if not bot_open_id:
        return False
    mentions = data.get("mentions")
    if not mentions:
        return False
    for m in mentions:
        if isinstance(m, dict):
            if _mention_open_id(m) == bot_open_id:
                return True
        elif isinstance(m, str) and m == bot_open_id:
            return True
    return False


def _strip_at_mention(text: str) -> str:
    """Remove leading @mention token (e.g. '@BotName ') from message text."""
    return re.sub(r"^@\S+\s*", "", text.strip()).strip()


def parse_bot_added_line(line: str) -> Optional[dict]:
    """
    Parse an im.chat.member.bot.added_v1 compact event line.

    Returns {"event_type": "bot_added", "operator_id": str, "chat_id": str,
             "chat_name": str} or None if not applicable.

    operator_id may be a plain open_id string or a nested object.
    """
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return None
    return parse_bot_added_data(data)


def parse_bot_added_data(data: dict) -> Optional[dict]:
    """
    Parse an im.chat.member.bot.added_v1 compact event dictionary.

    Returns {"event_type": "bot_added", "operator_id": str, "chat_id": str,
             "chat_name": str} or None if not applicable.
    """
    if data.get("type") != "im.chat.member.bot.added_v1":
        return None

    chat_id = data.get("chat_id", "")
    raw_op = data.get("operator_id", "")
    if isinstance(raw_op, dict):
        operator_id = raw_op.get("open_id") or raw_op.get("user_id") or ""
    else:
        operator_id = str(raw_op)

    if not chat_id or not operator_id:
        return None

    return {
        "event_type": "bot_added",
        "operator_id": operator_id,
        "chat_id": chat_id,
        "chat_name": data.get("name", "") or data.get("chat_name", ""),
    }


def _parse_image_content(content_raw: str) -> str:
    """Extract image_key from an image message content field.

    lark-cli compact delivers image content as a JSON string:
      '{"image_key": "img_xxx"}'
    but may also deliver the key directly as a plain string.
    """
    content_raw = (content_raw or "").strip()
    try:
        obj = json.loads(content_raw)
        if isinstance(obj, dict):
            return obj.get("image_key", "")
    except (json.JSONDecodeError, TypeError):
        pass
    # Fallback: treat the raw value as the key itself if it looks like one
    if content_raw.startswith("img_"):
        return content_raw
    # Handle "[Image: img_xxx]" format
    m = re.search(r"(img_\S+)", content_raw)
    if m:
        return m.group(1).rstrip("]")
    return ""


def _parse_post_content(content_raw: str) -> tuple[str, str]:
    """Extract (text, image_key) from a post (rich-text) message.

    Feishu post content structure (JSON string):
      {"zh_cn": {"title": "...", "content": [[{tag, text/image_key, ...}], ...]}}
    The outer language key (zh_cn / en_us) may be absent in compact format.
    Returns ("", "") on any parse error.
    """
    try:
        obj = json.loads(content_raw)
    except (json.JSONDecodeError, TypeError):
        # lark-cli compact may deliver post content as plain text instead of JSON.
        # Extract image_key if present (e.g. "[Image: img_xxx]"), strip it from text.
        raw = content_raw.strip()
        if not raw:
            return "", ""
        img_match = re.search(r"\[Image:\s*(img_\S+?)\]", raw)
        image_key = img_match.group(1) if img_match else ""
        text = re.sub(r"\[Image:\s*img_\S+?\]", "", raw).strip()
        return (text or ("[图片]" if image_key else ""), image_key)
    # Unwrap optional language key
    if isinstance(obj, dict):
        for lang in ("zh_cn", "en_us"):
            if lang in obj and isinstance(obj[lang], dict):
                obj = obj[lang]
                break
    paragraphs = obj.get("content", []) if isinstance(obj, dict) else []
    text_parts: list[str] = []
    image_key = ""
    for para in paragraphs:
        if not isinstance(para, list):
            continue
        for elem in para:
            if not isinstance(elem, dict):
                continue
            tag = elem.get("tag", "")
            if tag == "text":
                t = elem.get("text", "")
                if t:
                    text_parts.append(t)
            elif tag in ("img", "image") and not image_key:
                image_key = elem.get("image_key", "")
    return "".join(text_parts).strip(), image_key


def parse_event_line(line: str, bot_open_id: str = "") -> Optional[dict]:
    """
    Parse one NDJSON line from lark-cli event consume.

    Handles both P2P and group messages. Group messages are only processed
    when the bot is @mentioned.

    Compact format:
      {"type":"im.message.receive_v1","message_id":"om_xxx","chat_id":"oc_xxx",
       "chat_type":"p2p"|"group","message_type":"text","content":"Hello",
       "sender_id":"ou_xxx","create_time":"...","mentions":[...],"root_id":"..."}

    Returns event dict or None.
    """
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return None
    return parse_event_data(data, bot_open_id)


def parse_event_data(data: dict, bot_open_id: str = "") -> Optional[dict]:
    """
    Parse one compact event dictionary from lark-cli event consume.

    Handles both P2P and group messages. Group messages pass through with
    mentioned=False when the bot is not @mentioned, so main.py can decide
    whether an active thread session should continue.
    """
    if data.get("type") != "im.message.receive_v1":
        return None

    msg_type = data.get("message_type", "")
    if msg_type not in ("text", "image", "post", "file"):
        logger.warning("unsupported message_type=%r, dropping", msg_type)
        return None

    open_id = data.get("sender_id", "").strip()
    chat_type = data.get("chat_type", "p2p")
    image_key = ""
    file_key = ""
    file_name = ""
    content_raw = data.get("content", "")

    if msg_type == "image":
        image_key = _parse_image_content(content_raw)
        if not image_key:
            logger.warning("image message missing image_key, content=%r", content_raw[:200])
            return None
        text = "[图片]"
    elif msg_type == "post":
        text, image_key = _parse_post_content(content_raw)
        if not text and not image_key:
            logger.warning("post message yielded no text/image, content=%r", content_raw[:200])
            return None
        if not text:
            text = "[图片]"
    elif msg_type == "file":
        try:
            obj = json.loads(content_raw)
        except (json.JSONDecodeError, TypeError):
            obj = {}
        file_key = obj.get("file_key", "")
        file_name = obj.get("file_name", "")
        # lark-cli compact delivers file content as XML: <file key="..." name="..."/>
        if not file_key:
            m_key = re.search(r'key="([^"]+)"', content_raw)
            m_name = re.search(r'name="([^"]+)"', content_raw)
            if m_key:
                file_key = m_key.group(1)
            if m_name:
                file_name = m_name.group(1)
        if not file_key:
            logger.warning("file message missing file_key, content=%r", content_raw[:200])
            return None
        ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
        if ext not in ("pdf", "doc", "docx", "xls", "xlsx", "txt", "md"):
            logger.warning("unsupported file type %r (%s), dropping", ext, file_name)
            return None
        text = f"[文件: {file_name}]"
    else:
        text = content_raw.strip()

    if not open_id or not text:
        return None

    mentioned = True
    if chat_type == "group":
        mentioned = _is_bot_mentioned(data, bot_open_id)
        if not mentioned:
            # Pass through; main.py will resolve root_id via API if needed
            # and decide whether to respond based on active thread session.
            pass
        else:
            text = _strip_at_mention(text)
            if not text and not image_key and not file_key:
                return None
            if not text:
                text = "[图片]" if image_key else f"[文件: {file_name}]"

    return {
        "open_id": open_id,
        "text": text,
        "message_id": data.get("message_id", ""),
        "create_time": data.get("create_time", ""),
        "chat_id": data.get("chat_id", ""),
        "chat_type": chat_type,
        "is_group": chat_type == "group",
        "root_id": data.get("root_id", ""),
        # parent_id present when user quote-replies to a specific message.
        # Distinct from root_id (which scopes to a thread/topic). When set,
        # we fetch the quoted message content via Feishu API and inject it
        # into the prompt for context.
        "parent_id": data.get("parent_id", ""),
        "mentioned": mentioned,
        "image_key": image_key,  # non-empty for image messages
        "file_key": file_key,    # non-empty for file messages (pdf/doc/xls)
        "file_name": file_name,  # original filename with extension
    }


def _safe_log_value(value) -> str:
    if value is None:
        return ""
    text = str(value).replace("\n", "\\n").replace("\r", "\\r")
    if len(text) > 120:
        return text[:117] + "..."
    return text


def _dict_at(data: dict, *keys: str) -> dict:
    current = data
    for key in keys:
        if not isinstance(current, dict):
            return {}
        current = current.get(key)
    return current if isinstance(current, dict) else {}


def _summarize_unhandled_event_line(line: str) -> Optional[dict]:
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None

    header = _dict_at(data, "header")
    event = _dict_at(data, "event")
    message = _dict_at(event, "message")
    action = data.get("action")
    if not isinstance(action, dict):
        action = _dict_at(event, "action")
    value = action.get("value") if isinstance(action, dict) else {}
    if not isinstance(value, dict):
        value = {}

    return {
        "type": _safe_log_value(
            data.get("type") or data.get("event_type") or header.get("event_type")
        ),
        "event_id": _safe_log_value(data.get("event_id") or header.get("event_id")),
        "message_id": _safe_log_value(data.get("message_id") or message.get("message_id")),
        "open_message_id": _safe_log_value(
            data.get("open_message_id")
            or event.get("open_message_id")
            or action.get("open_message_id", "")
            if isinstance(action, dict)
            else ""
        ),
        "chat_id": _safe_log_value(data.get("chat_id") or message.get("chat_id")),
        "action": _safe_log_value(value.get("action") or value.get("type") or action.get("type", "")),
        "session_id": _safe_log_value(value.get("session_id") or value.get("form_id")),
        "keys": ",".join(sorted(str(key) for key in data.keys())[:20]),
    }


def _log_unhandled_event_line(line: str) -> None:
    summary = _summarize_unhandled_event_line(line)
    if not summary:
        return
    logger.info(
        "Unhandled lark-cli event: type=%s event_id=%s message_id=%s "
        "open_message_id=%s chat_id=%s action=%s session_id=%s keys=%s",
        summary["type"],
        summary["event_id"],
        summary["message_id"],
        summary["open_message_id"],
        summary["chat_id"],
        summary["action"],
        summary["session_id"],
        summary["keys"],
    )


def _log_message_ingress(data: dict) -> None:
    if data.get("type") != "im.message.receive_v1":
        return
    chat_type = str(data.get("chat_type") or "")
    if chat_type != "group":
        return
    chat_id = str(data.get("chat_id") or "")
    mentions = data.get("mentions")
    mention_count = len(mentions) if isinstance(mentions, list) else -1
    logger.info(
        "lark-cli message ingress: chat_id=%s message_id=%s chat_type=%s "
        "message_type=%s mentions=%d content_len=%d",
        chat_id,
        data.get("message_id", ""),
        chat_type,
        data.get("message_type", ""),
        mention_count,
        len(str(data.get("content") or "")),
    )


class EventListener:
    def __init__(self, bot_home: str, on_message: Callable[[dict], None],
                 on_poll: Optional[Callable[[], None]] = None,
                 bot_open_id: str = "",
                 on_bot_added: Optional[Callable[[dict], None]] = None):
        self.bot_home = bot_home
        self.on_message = on_message
        self.on_poll = on_poll  # periodic callback to poll for missed messages via API
        self.bot_open_id = bot_open_id
        self.on_bot_added = on_bot_added
        self._stop_event = threading.Event()
        self._procs: list[subprocess.Popen] = []
        self._proc_lock = threading.Lock()
        self._reconnect_count = 0

    def start(self):
        """Start listening in a background thread with auto-reconnect."""
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        # Periodic poll thread — recovers missed messages without killing the subscriber
        if self.on_poll:
            self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
            self._poll_thread.start()

    def stop(self):
        self._stop_event.set()
        with self._proc_lock:
            procs = list(self._procs)
        for proc in procs:
            self._kill_proc(proc)

    def _poll_loop(self):
        """Periodically poll for missed messages via Feishu API."""
        # Wait initial period before first poll (let WebSocket connect first)
        self._stop_event.wait(timeout=30)
        while not self._stop_event.is_set():
            try:
                self.on_poll()
            except Exception as e:
                logger.error(f"Poll callback error: {e}")
            self._stop_event.wait(timeout=30)

    def _locks_dir(self) -> str:
        """Return the lark-cli subscriber lock directory for this platform."""
        if sys.platform == "win32":
            return os.path.join(os.environ.get("USERPROFILE", ""), ".lark-cli", "locks")
        return os.path.join(self.bot_home, ".lark-cli", "locks")

    def _cleanup_stale_subscribers(self):
        """Kill orphaned lark-cli subscriber processes and remove stale lock files."""
        # On Windows, lark-cli.cmd is a wrapper; the real process is node.exe
        # running @larksuite/cli. Killing lark-cli.exe doesn't kill the node child.
        if sys.platform == "win32":
            try:
                result = subprocess.run(
                    ["powershell", "-Command",
                     "Get-CimInstance Win32_Process -Filter \"name='lark-cli.exe'\" "
                     "| Where-Object { "
                     "$_.CommandLine -like '*event*subscribe*' -or "
                     "$_.CommandLine -like '*event*consume*' "
                     "} "
                     "| ForEach-Object { $_.ProcessId }"],
                    capture_output=True, text=True, timeout=10,
                )
                for pid_str in result.stdout.strip().splitlines():
                    pid_str = pid_str.strip()
                    if pid_str.isdigit():
                        subprocess.run(["taskkill", "/F", "/PID", pid_str],
                                       capture_output=True, timeout=5)
                        logger.info(f"Killed orphaned lark-cli.exe subscriber (PID {pid_str})")
            except Exception as e:
                logger.debug(f"Orphan cleanup failed: {e}")
        else:
            # Linux: scan /proc for orphaned lark-cli subscriber processes
            try:
                for cmdline_path in glob.glob("/proc/*/cmdline"):
                    try:
                        with open(cmdline_path, "rb") as f:
                            cmdline = f.read().decode("utf-8", errors="replace").replace("\x00", " ")
                        if (
                            "lark-cli" in cmdline
                            and "event" in cmdline
                            and ("subscribe" in cmdline or "consume" in cmdline)
                        ):
                            pid = int(cmdline_path.split("/")[2])
                            if pid != os.getpid():
                                os.kill(pid, 9)
                                logger.info(f"Killed orphaned lark-cli subscriber (PID {pid})")
                    except (OSError, ValueError):
                        pass
            except Exception as e:
                logger.debug(f"Linux orphan cleanup failed: {e}")

        # Remove stale lock files
        for lock_file in glob.glob(os.path.join(self._locks_dir(), "subscribe_*.lock")):
            try:
                os.remove(lock_file)
                logger.info(f"Removed stale lock: {lock_file}")
            except OSError:
                logger.debug(f"Lock file in use: {lock_file}")

    def _run_loop(self):
        self._cleanup_stale_subscribers()
        while not self._stop_event.is_set():
            try:
                self._listen_once()
            except Exception as e:
                logger.error(f"Event listener error: {e}")
            if not self._stop_event.is_set():
                self._reconnect_count += 1
                self._cleanup_stale_subscribers()
                logger.info(f"Reconnecting in {RECONNECT_DELAY}s... (reconnect #{self._reconnect_count})")
                self._stop_event.wait(timeout=RECONNECT_DELAY)

    @staticmethod
    def _kill_proc(proc: subprocess.Popen):
        """Kill a subprocess tree (Windows-aware)."""
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               capture_output=True, timeout=5)
            else:
                proc.terminate()
        except Exception:
            pass

    def _handle_event_line(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        logger.debug(f"Raw line: {line[:500]}")
        try:
            raw_data = json.loads(line)
        except json.JSONDecodeError:
            raw_data = None
        if isinstance(raw_data, dict):
            _log_message_ingress(raw_data)
        bot_added = parse_bot_added_line(line)
        if bot_added:
            if self.on_bot_added:
                self.on_bot_added(bot_added)
            return
        event = parse_event_line(line, bot_open_id=self.bot_open_id)
        if event:
            self.on_message(event)
            return
        _log_unhandled_event_line(line)

    def _read_stdout(self, proc: subprocess.Popen) -> None:
        try:
            while not self._stop_event.is_set():
                line = proc.stdout.readline()
                if not line or not isinstance(line, str):
                    break
                try:
                    self._handle_event_line(line)
                except Exception as e:
                    logger.exception("Event callback failed for pid=%s: %s", proc.pid, e)
        except Exception as e:
            logger.debug("lark-cli stdout reader stopped for pid=%s: %s", proc.pid, e)

    def _read_stderr(self, proc: subprocess.Popen) -> None:
        try:
            while not self._stop_event.is_set():
                line = proc.stderr.readline()
                if not line or not isinstance(line, str):
                    break
                logger.debug("lark-cli stderr pid=%s: %s", proc.pid, line.strip())
        except Exception as e:
            logger.debug("lark-cli stderr reader stopped for pid=%s: %s", proc.pid, e)

    def _listen_once(self):
        env = {
            **os.environ,
            "HOME": self.bot_home,
            # Ensure lark-cli uses the writable bot home dir (not a read-only
            # ConfigMap mount) for both config reads and lock file creation.
            "LARKSUITE_CLI_CONFIG_DIR": os.path.join(self.bot_home, ".lark-cli"),
        }
        procs = []
        readers = []
        try:
            for event_key in _CONSUME_EVENT_KEYS:
                cmd = _event_consume_cmd(event_key)
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    env=env,
                )
                procs.append(proc)
                logger.info("Event consumer started (pid=%d, event_key=%s)", proc.pid, event_key)
                stdout_reader = threading.Thread(target=self._read_stdout, args=(proc,), daemon=True)
                stderr_reader = threading.Thread(target=self._read_stderr, args=(proc,), daemon=True)
                stdout_reader.start()
                stderr_reader.start()
                readers.extend([stdout_reader, stderr_reader])

            with self._proc_lock:
                self._procs = list(procs)

            while not self._stop_event.is_set():
                if any(proc.poll() is not None for proc in procs):
                    break
                self._stop_event.wait(timeout=0.2)
        finally:
            with self._proc_lock:
                self._procs = []
            for proc in procs:
                self._kill_proc(proc)
            for proc in procs:
                proc.wait()
                if proc.returncode not in (0, None, -15, 1):
                    logger.warning("lark-cli exited with code %d", proc.returncode)
            for reader in readers:
                reader.join(timeout=1)
            # Clean up lock file so next connection attempt isn't blocked
            for lf in glob.glob(os.path.join(self._locks_dir(), "subscribe_*.lock")):
                try:
                    os.remove(lf)
                except OSError:
                    pass
