# src/internal_api.py
"""
Lightweight internal HTTP API on 127.0.0.1 that replaces direct CLI→PostgreSQL access.

Claude subprocesses call these endpoints via curl instead of running CLI scripts that
connect to the database directly.  This keeps POSTGRES_URL out of the subprocess env.

Every request must carry a Bearer token (injected into the subprocess env at launch).
Tokens are per-session and bound to a specific open_id — cross-user requests are rejected.
"""
import json
import logging
import secrets
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Token registry — maps active tokens to the open_id they were issued for
# ---------------------------------------------------------------------------

class TokenRegistry:
    def __init__(self):
        self._tokens: dict[str, str] = {}  # token -> open_id
        self._metadata: dict[str, dict] = {}
        self._lock = threading.Lock()

    def create(self, open_id: str, metadata: dict | None = None) -> str:
        token = secrets.token_hex(16)
        with self._lock:
            self._tokens[token] = open_id
            self._metadata[token] = dict(metadata or {})
        return token

    def validate(self, token: str, open_id: str) -> tuple[bool, str]:
        """Returns (ok, error_message)."""
        with self._lock:
            bound_id = self._tokens.get(token)
        if bound_id is None:
            return False, "invalid token"
        if bound_id != open_id:
            return False, "open_id mismatch"
        return True, ""

    def revoke(self, token: str) -> None:
        with self._lock:
            self._tokens.pop(token, None)
            self._metadata.pop(token, None)

    def metadata(self, token: str) -> dict:
        with self._lock:
            return dict(self._metadata.get(token) or {})


# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------

def _make_handler(store, auth, job_store, registry: TokenRegistry, form_service=None):
    """Factory — returns a handler class closed over shared state."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            logger.debug(f"internal_api: {fmt % args}")

        # -- helpers --------------------------------------------------------

        def _read_body(self) -> dict | None:
            length = int(self.headers.get("Content-Length", 0))
            if not length:
                self._json_error(400, "empty body")
                return None
            try:
                return json.loads(self.rfile.read(length))
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                self._json_error(400, f"invalid JSON: {e}")
                return None

        def _check_auth(self, body: dict) -> bool:
            auth_header = self.headers.get("Authorization", "")
            if not auth_header.startswith("Bearer "):
                self._json_error(401, "missing Bearer token")
                return False
            token = auth_header[7:]
            open_id = body.get("open_id", "")
            if not open_id:
                self._json_error(400, "missing open_id")
                return False
            ok, err = registry.validate(token, open_id)
            if not ok:
                self._json_error(403, err)
                return False
            self._auth_token = token
            self._auth_metadata = registry.metadata(token)
            return True

        def _json_ok(self, data: dict | None = None) -> None:
            payload = {"ok": True, **(data or {})}
            self._send_json(200, payload)

        def _json_error(self, status: int, msg: str) -> None:
            self._send_json(status, {"ok": False, "error": msg})

        def _send_json(self, status: int, data: Any) -> None:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        # -- routes ---------------------------------------------------------

        def do_POST(self):
            body = self._read_body()
            if body is None:
                return
            if not self._check_auth(body):
                return

            path = self.path.rstrip("/")
            routes = {
                "/session-reset": self._handle_session_reset,
                "/lark-reauth": self._handle_lark_reauth,
                "/meegle-auth": self._handle_meegle_auth,
                "/job/create": self._handle_job_create,
                "/job/list": self._handle_job_list,
                "/job/cancel": self._handle_job_cancel,
                "/interactive-form/create": self._handle_interactive_form_create,
                "/interactive-form/diagnostic/minimal-card": self._handle_interactive_form_diagnostic_minimal_card,
            }
            handler_fn = routes.get(path)
            if not handler_fn:
                self._json_error(404, f"unknown path: {path}")
                return
            try:
                handler_fn(body)
            except Exception as e:
                logger.exception(f"internal_api {path} error: {e}")
                self._json_error(500, str(e))

        # -- endpoint implementations --------------------------------------

        def _handle_session_reset(self, body: dict):
            open_id = body["open_id"]
            store.upsert_user(open_id, pending_session_reset=1)
            self._json_ok()

        def _handle_lark_reauth(self, body: dict):
            open_id = body["open_id"]

            # Idempotency: if a fresh pending auth already exists for this user,
            # return its URL instead of generating a new device_code. Without this,
            # multiple concurrent scheduled tasks (or rapid retries) each call
            # start_auth, generate competing device_codes, and the DB pending_code
            # ends up being whichever lost the race — leaving the user with an
            # auth link nobody is polling for.
            try:
                existing = store.get_user(open_id) or {}
                if (existing.get("auth_status") == "pending"
                        and existing.get("pending_code")
                        and existing.get("pending_url")):
                    pending_at = existing.get("pending_at")
                    fresh = False
                    if pending_at is not None:
                        try:
                            import datetime as _dt
                            if isinstance(pending_at, str):
                                _pa = _dt.datetime.fromisoformat(pending_at)
                            else:
                                _pa = pending_at
                            if _pa.tzinfo is None:
                                _pa = _pa.replace(tzinfo=_dt.timezone.utc)
                            elapsed = (_dt.datetime.now(_dt.timezone.utc) - _pa).total_seconds()
                            fresh = 0 <= elapsed < 270  # device_code lives ~5min; reuse if <4.5min old
                        except Exception:
                            fresh = False
                    if fresh:
                        url = existing.get("pending_url", "")
                        logger.info(
                            f"[lark-reauth] reusing fresh pending auth for {open_id} "
                            f"(code={(existing.get('pending_code') or '')[:8]}..., elapsed={int(elapsed)}s)"
                        )
                        self._json_ok({
                            "url": url,
                            "device_code": existing.get("pending_code", ""),
                            "reused": True,
                            "message": (
                                f"飞书授权 token 权限不足，已发起重新授权。\n"
                                f"请点击以下链接完成授权，授权完成后将自动继续处理您的请求：\n{url}\n"
                                f"\n如果点击链接后机器人长时间没有反应，请直接回复「重新授权」生成新的授权链接。"
                            ),
                        })
                        return
            except Exception as e:
                # Idempotency check is best-effort; on error fall through to start_auth.
                logger.warning(f"[lark-reauth] idempotency check failed for {open_id}: {e}")

            try:
                auth.revoke_token(open_id)
            except Exception:
                pass
            try:
                data = auth.start_auth(open_id)
            except FileNotFoundError:
                self._json_error(500, "lark-cli not found")
                return
            except Exception as e:
                self._json_error(500, f"lark-cli auth login failed: {e}")
                return
            url = data.get("verification_url", "")
            self._json_ok({
                "url": url,
                "device_code": data.get("device_code", ""),
                "message": (
                    f"飞书授权 token 权限不足，已发起重新授权。\n"
                    f"请点击以下链接完成授权，授权完成后将自动继续处理您的请求：\n{url}\n"
                    f"\n如果点击链接后机器人长时间没有反应，请直接回复「重新授权」生成新的授权链接。"
                ),
            })

        def _handle_meegle_auth(self, body: dict):
            open_id = body["open_id"]
            try:
                data = auth.start_meegle_auth(open_id)
            except FileNotFoundError:
                self._json_error(500, "meegle CLI not found")
                return
            except Exception as e:
                self._json_error(500, f"meegle auth init failed: {e}")
                return
            self._json_ok({
                "url": data["url"],
                "device_code": data["device_code"],
                "client_id": data["client_id"],
                "message": (
                    f"请点击以下链接完成 Meegle（飞书项目）授权，"
                    f"授权完成后将自动继续处理你的请求：\n{data['url']}\n"
                    f"\n如果点击链接后机器人长时间没有反应，请直接回复「重新授权 meegle」生成新的授权链接。"
                ),
            })

        def _handle_job_create(self, body: dict):
            from src.schedule_utils import compute_next_run, fmt_next_run

            open_id = body["open_id"]
            chat_id = body.get("chat_id", "")
            job_type = body.get("job_type", "")
            schedule = body.get("schedule")
            content = body.get("content", "")
            mention_open_id = body.get("mention_open_id") or None

            if job_type not in ("reminder", "ai_task"):
                self._json_error(400, f"job_type must be 'reminder' or 'ai_task', got: {job_type!r}")
                return
            if not chat_id:
                self._json_error(400, "missing chat_id")
                return
            if not schedule or not isinstance(schedule, dict):
                self._json_error(400, "missing or invalid schedule")
                return
            if not content:
                self._json_error(400, "missing content")
                return

            schedule_type = "once" if schedule.get("type") == "once" else "recurring"
            now_ms = int(time.time() * 1000)
            try:
                next_run_at = compute_next_run(schedule, now_ms)
            except (ValueError, KeyError) as e:
                self._json_error(400, f"invalid schedule spec: {e}")
                return

            if schedule_type == "once" and next_run_at <= now_ms:
                self._json_error(400, "scheduled time is in the past")
                return

            job_id = job_store.create_job(
                open_id=open_id,
                chat_id=chat_id,
                job_type=job_type,
                content=content,
                schedule_type=schedule_type,
                schedule_spec=schedule,
                next_run_at=next_run_at,
                mention_open_id=mention_open_id,
            )
            self._json_ok({"id": job_id, "next_run_at": fmt_next_run(next_run_at)})

        def _handle_job_list(self, body: dict):
            from src.schedule_utils import fmt_next_run

            jobs = job_store.list_jobs(body["open_id"])
            formatted = []
            for i, job in enumerate(jobs, 1):
                spec = job["schedule_spec"]
                spec_type = spec.get("type", "?") if isinstance(spec, dict) else "?"
                if spec_type == "once":
                    schedule_label = f"一次性 {fmt_next_run(job['next_run_at'])}"
                elif spec_type == "daily":
                    schedule_label = f"每天 {spec.get('time', '?')}"
                elif spec_type == "weekly":
                    days = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
                    dow = spec.get("day_of_week", 0)
                    schedule_label = f"每{days[dow]} {spec.get('time', '?')}"
                elif spec_type == "monthly":
                    schedule_label = f"每月{spec.get('day_of_month', '?')}号 {spec.get('time', '?')}"
                else:
                    schedule_label = spec_type
                formatted.append({
                    "index": i,
                    "id": job["id"],
                    "job_type": job["job_type"],
                    "schedule": schedule_label,
                    "content": job["content"],
                    "run_count": job["run_count"],
                    "next_run_at": fmt_next_run(job["next_run_at"]),
                })
            self._json_ok({"jobs": formatted, "count": len(formatted)})

        def _handle_job_cancel(self, body: dict):
            job_id = body.get("id", "")
            if not job_id:
                self._json_error(400, "missing id")
                return
            cancelled = job_store.cancel_job(job_id=job_id, open_id=body["open_id"])
            if not cancelled:
                self._json_error(404, f"job {job_id!r} not found or does not belong to you")
                return
            self._json_ok({"id": job_id})

        def _handle_interactive_form_create(self, body: dict):
            if form_service is None:
                self._json_error(503, "interactive form service not configured")
                return
            try:
                schema = {
                    "title": body.get("title"),
                    "questions": body.get("questions"),
                }
                meta = getattr(self, "_auth_metadata", {}) or {}
                result = form_service.create_form(
                    context_id=body["open_id"],
                    operator_open_id=meta.get("operator_open_id") or body["open_id"],
                    chat_id=meta.get("chat_id", ""),
                    chat_type=meta.get("chat_type", "p2p"),
                    reply_msg_id=meta.get("reply_msg_id", ""),
                    root_id=meta.get("root_id", ""),
                    thread_session_key=meta.get("thread_session_key", ""),
                    message_id=meta.get("message_id", ""),
                    original_text=meta.get("original_text", ""),
                    schema=schema,
                )
            except KeyError as e:
                self._json_error(400, f"missing field: {e}")
                return
            except ValueError as e:
                self._json_error(400, str(e))
                return
            self._json_ok({
                "session_id": result.get("session_id", ""),
                "message_id": result.get("message_id", ""),
                "message": "已发送交互表单，请等待用户在飞书卡片中提交。本轮不要继续执行用户请求。",
            })

        def _handle_interactive_form_diagnostic_minimal_card(self, body: dict):
            if form_service is None:
                self._json_error(503, "interactive form service not configured")
                return
            try:
                from src.card_forms import normalize_diagnostic_response_mode

                response_mode = normalize_diagnostic_response_mode(body.get("response_mode", "ack"))
                meta = getattr(self, "_auth_metadata", {}) or {}
                result = form_service.send_diagnostic_minimal_card(
                    operator_open_id=meta.get("operator_open_id") or body["open_id"],
                    chat_id=meta.get("chat_id", ""),
                    chat_type=meta.get("chat_type", "p2p"),
                    reply_msg_id=meta.get("reply_msg_id", ""),
                    response_mode=response_mode,
                    nonce=body.get("nonce", ""),
                )
            except KeyError as e:
                self._json_error(400, f"missing field: {e}")
                return
            except ValueError as e:
                self._json_error(400, str(e))
                return
            self._json_ok({
                "message_id": result.get("message_id", ""),
                "response_mode": result.get("response_mode", ""),
                "nonce": result.get("nonce", ""),
                "message": "已发送最小回调诊断卡。请点击卡片按钮并观察客户端是否出现 200671。",
            })

    return Handler


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def start_internal_api(store, auth, job_store, form_service=None) -> tuple[int, TokenRegistry]:
    """Start the internal API server on a random localhost port.

    Returns (port, token_registry).
    """
    reg = TokenRegistry()
    handler_cls = _make_handler(store, auth, job_store, reg, form_service=form_service)
    server = HTTPServer(("127.0.0.1", 0), handler_cls)
    port = server.server_address[1]

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info(f"Internal API started on 127.0.0.1:{port}")

    return port, reg
