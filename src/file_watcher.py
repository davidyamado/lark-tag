# src/file_watcher.py
"""
Background watcher that scans the per-user home directories for newly created
script files containing suspicious patterns (e.g. attempts to use the bot's
app-level credentials to read other users' data).

This is a *detective* control — it does NOT prevent malicious file creation
(Claude must be able to write to user_home for legitimate workflows), but it
fires an alert when high-confidence indicators of abuse appear.

Polling interval: every 30 s. Files with mtime newer than the previous scan
(plus a small slop window) are read up to 16 KB and pattern-matched.

Alert delivery:
  - Always: WARNING-level log line containing the file path, user, matched
            patterns, and a content snippet.
  - When BOT_SECURITY_ALERT_CHAT_ID env var is set, also pushes an interactive
    card to that chat via the bot's tenant_access_token.

Disable with BOT_FILE_WATCHER=0.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

# Extensions worth scanning. Other files (data, logs, configs) are skipped.
_WATCH_EXTENSIONS = {".py", ".sh", ".js", ".ts", ".mjs", ".rb", ".pl"}
_MAX_READ_BYTES = 16 * 1024
_POLL_INTERVAL_SECONDS = 30
_RESCAN_SLOP_SECONDS = 5  # re-check files modified slightly before last scan


# Patterns chosen for high precision: each one is a near-unambiguous signal of
# someone trying to use bot-level credentials or directly forge tenant tokens.
# Designed to NOT match legitimate user scripts (which use --as user via the
# bot's lark_runner.py and never touch tenant_access_token directly).
_SIGNATURES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"--as\s+bot\b|--as=bot\b"), "uses --as bot"),
    (re.compile(r"['\"]--as['\"]\s*,\s*['\"]bot['\"]"), "uses --as bot (list syntax)"),
    (re.compile(r"\btenant_access_token\b"), "references tenant_access_token"),
    (re.compile(r"/open-apis/auth/v3/(?:tenant|app)_access_token"),
     "calls Feishu app-level auth endpoint directly"),
    (re.compile(r"\bapp_secret\b\s*[:=]"), "hardcoded app_secret"),
]


def _scan_content(text: str) -> list[str]:
    hits: list[str] = []
    for pattern, label in _SIGNATURES:
        if pattern.search(text):
            hits.append(label)
    return hits


def _iter_recent_script_files(root: Path, mtime_after: float) -> Iterable[tuple[Path, str]]:
    """
    Yield (path, open_id) for script files under root/<open_id>/ modified after
    `mtime_after`. open_id is the immediate child of root.
    """
    try:
        children = list(root.iterdir())
    except (FileNotFoundError, PermissionError):
        return
    for user_dir in children:
        if not user_dir.is_dir():
            continue
        open_id = user_dir.name
        # Walk shallow-ish: we don't expect malicious scripts to live N levels deep
        # under the user home (they need to be Python-runnable, so usually top-level).
        try:
            for path in user_dir.rglob("*"):
                if not path.is_file():
                    continue
                if path.suffix.lower() not in _WATCH_EXTENSIONS:
                    continue
                try:
                    if path.stat().st_mtime <= mtime_after:
                        continue
                except FileNotFoundError:
                    continue
                yield path, open_id
        except (PermissionError, OSError) as e:
            logger.debug(f"file_watcher: cannot enumerate {user_dir}: {e}")


def _format_alert(file_path: Path, open_id: str, hits: list[str],
                  display_name: str, snippet: str) -> str:
    _user_line = display_name if display_name else "(未知，飞书侧未取到名字)"
    return (
        "**⚠️ [BOT 安全告警] 检测到可疑用户文件**\n\n"
        f"**用户**：{_user_line}\n"
        f"**open_id**：`{open_id}`\n"
        f"**文件路径**：`{file_path}`\n"
        f"**命中规则**：{', '.join(hits)}\n"
        f"**文件开头**：\n```\n{snippet}\n```\n\n"
        "请人工核查该用户是否在尝试用 bot 凭据越权访问他人数据。"
    )


class FileWatcher:
    def __init__(self, users_dir: str, store, feishu_api, app_id: str, app_secret: str,
                 alert_chat_id: str = ""):
        self.users_dir = Path(users_dir).resolve()
        self.store = store
        self.feishu_api = feishu_api
        self.app_id = app_id
        self.app_secret = app_secret
        self.alert_chat_id = alert_chat_id
        # Start scanning from "now" so existing files aren't re-alerted on every
        # restart. Operators wanting to re-scan can clear the bot.pid and
        # backdate by setting BOT_FILE_WATCHER_BACKDATE_SECONDS env var.
        backdate = float(os.environ.get("BOT_FILE_WATCHER_BACKDATE_SECONDS", "0") or 0)
        self._last_scan_mtime = time.time() - backdate
        self._alerted: set[str] = set()  # path strings we've already alerted on
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="file-watcher", daemon=True,
        )
        self._thread.start()
        logger.info(
            f"[file-watcher] started: root={self.users_dir} interval={_POLL_INTERVAL_SECONDS}s "
            f"alert_chat={'set' if self.alert_chat_id else 'unset (log-only)'}"
        )

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(timeout=_POLL_INTERVAL_SECONDS):
            try:
                self._scan_once()
            except Exception as e:
                logger.warning(f"[file-watcher] scan iteration failed: {e}")

    def _scan_once(self) -> None:
        now = time.time()
        cutoff = self._last_scan_mtime - _RESCAN_SLOP_SECONDS
        for path, open_id in _iter_recent_script_files(self.users_dir, cutoff):
            path_str = str(path)
            if path_str in self._alerted:
                continue
            try:
                with open(path, "rb") as f:
                    raw = f.read(_MAX_READ_BYTES)
            except OSError as e:
                logger.debug(f"[file-watcher] cannot read {path}: {e}")
                continue
            try:
                text = raw.decode("utf-8", errors="replace")
            except Exception:
                continue
            hits = _scan_content(text)
            if not hits:
                continue
            self._alerted.add(path_str)
            self._deliver_alert(path, open_id, hits, text[:600])
        self._last_scan_mtime = now

    def _deliver_alert(self, file_path: Path, open_id: str,
                       hits: list[str], snippet: str) -> None:
        try:
            display_name = self.store.get_display_name(open_id) or ""
        except Exception:
            display_name = ""

        # If the user's display name isn't cached yet, fetch it from Feishu and
        # cache it so this and future alerts (plus logs) show a human name.
        token = ""
        if not display_name:
            try:
                token = self.feishu_api.get_tenant_access_token(self.app_id, self.app_secret)
                display_name = self.feishu_api.get_user_display_name(open_id, token) or ""
                if display_name:
                    try:
                        self.store.set_display_name(open_id, display_name)
                    except Exception:
                        pass
            except Exception as e:
                logger.debug(f"[file-watcher] could not resolve display_name for {open_id}: {e}")

        logger.warning(
            f"[file-watcher] SUSPICIOUS FILE: user={display_name or open_id} "
            f"path={file_path} hits={hits}"
        )

        if not self.alert_chat_id:
            return
        try:
            if not token:
                token = self.feishu_api.get_tenant_access_token(self.app_id, self.app_secret)
            text = _format_alert(file_path, open_id, hits, display_name, snippet)
            self.feishu_api.send_text_card_to_chat(self.alert_chat_id, text, token)
        except Exception as e:
            logger.warning(f"[file-watcher] failed to send alert to {self.alert_chat_id}: {e}")
