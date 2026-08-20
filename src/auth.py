# src/auth.py
import json
import logging
import os
import shlex
import subprocess
import time
from contextlib import nullcontext

logger = logging.getLogger(__name__)
import sys
import datetime
from src.user_store import UserStore

_LARK_CLI = "lark-cli.cmd" if sys.platform == "win32" else "lark-cli"
_MEEGLE_CMD = "meegle.cmd" if sys.platform == "win32" else "meegle"
_MEEGLE_HOST = os.environ.get("MEEGLE_HOST", "project.feishu.cn")


class AuthManager:
    def __init__(self, store: UserStore, users_dir: str, bot_home: str,
                 app_id: str = "", app_secret: str = ""):
        self.store = store
        self.users_dir = users_dir
        self.bot_home = bot_home
        self.app_id = app_id
        self.app_secret = app_secret

    def _run(self, command, home: str, timeout: int = 30) -> subprocess.CompletedProcess:
        env = {
            **os.environ,
            "HOME": home,
            # Override any pod-level LARKSUITE_CLI_CONFIG_DIR so each user's
            # credentials are stored in their own directory (not the read-only
            # ConfigMap mount used for the bot's app credentials).
            "LARKSUITE_CLI_CONFIG_DIR": os.path.join(home, ".lark-cli"),
        }
        args = command if isinstance(command, list) else shlex.split(command)
        args[0] = _LARK_CLI
        return subprocess.run(
            args,
            capture_output=True, text=True, encoding="utf-8", timeout=timeout, env=env
        )

    def _ensure_user_app_config(self, user_home: str) -> None:
        """
        Write app credentials into the user's lark-cli config dir so that
        `lark-cli auth login` knows which Feishu app to authorize against.

        Writes unconditionally when the file is absent.  When the file already
        exists, rewrites it only if the stored appId or appSecret differs from
        the current values — this handles app-secret rotation without requiring
        manual intervention.
        """
        app_id = self.app_id or os.environ.get("FEISHU_APP_ID", "")
        app_secret = self.app_secret or os.environ.get("FEISHU_APP_SECRET", "")
        if not app_id or not app_secret:
            return
        config_dir = os.path.join(user_home, ".lark-cli")
        os.makedirs(config_dir, mode=0o700, exist_ok=True)
        config_file = os.path.join(config_dir, "config.json")

        # Check whether an existing file already has the correct credentials.
        if os.path.exists(config_file):
            try:
                with open(config_file) as f:
                    existing = json.load(f)
                apps = existing.get("apps", [])
                if apps and apps[0].get("appId") == app_id and apps[0].get("appSecret") == app_secret:
                    return  # credentials match — nothing to do
                logger.info(f"App credentials changed, rewriting lark-cli config for {user_home}")
            except (json.JSONDecodeError, OSError, KeyError):
                # Corrupt or unreadable file — overwrite it
                logger.warning(f"Could not read existing lark-cli config at {config_file}, rewriting")

        config = {"apps": [{"appId": app_id, "appSecret": app_secret,
                             "brand": "feishu", "lang": "zh", "users": []}]}
        with open(config_file, "w") as f:
            json.dump(config, f)

    def _name(self, open_id: str) -> str:
        try:
            return self.store.get_display_name(open_id) or open_id
        except Exception:
            return open_id

    def _auth_lock(self, open_id: str):
        if hasattr(self.store, "conversation_lock"):
            return self.store.conversation_lock(f"auth:{open_id}")
        return nullcontext()

    @staticmethod
    def _fresh_pending(user: dict | None, max_age_seconds: int = 270) -> bool:
        if not user:
            return False
        if user.get("auth_status") != "pending":
            return False
        if not user.get("pending_code") or not user.get("pending_url") or not user.get("pending_at"):
            return False
        try:
            pending_at = user["pending_at"]
            if isinstance(pending_at, str):
                dt = datetime.datetime.fromisoformat(pending_at)
            else:
                dt = pending_at
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            elapsed = (datetime.datetime.now(datetime.timezone.utc) - dt).total_seconds()
            return 0 <= elapsed < max_age_seconds
        except Exception:
            return False

    def start_auth(self, open_id: str) -> dict:
        """
        Start device-code OAuth flow for a user.
        Returns {"url": ..., "code": ...}
        """
        with self._auth_lock(open_id):
            existing = self.store.get_user(open_id)
            if self._fresh_pending(existing):
                logger.info(
                    f"[auth] start_auth: reusing pending code for {self._name(open_id)} "
                    f"device_code={(existing.get('pending_code') or '')[:8]}..."
                )
                return {
                    "device_code": existing["pending_code"],
                    "verification_url": existing["pending_url"],
                    "reused": True,
                }

            user_home = os.path.join(self.users_dir, open_id)
            os.makedirs(user_home, mode=0o700, exist_ok=True)
            self._ensure_user_app_config(user_home)

            logger.info(f"[auth] start_auth: running lark-cli auth login for {self._name(open_id)}")
            result = self._run(
                "lark-cli auth login --domain all --no-wait",
                home=user_home
            )
            raw = result.stdout.strip()
            if not raw:
                logger.error(f"[auth] start_auth: {self._name(open_id)} empty stdout, stderr={result.stderr[:200]!r}")
                raise RuntimeError(f"auth login failed: {result.stderr}")

            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"auth login returned non-JSON: {raw!r}") from exc
            device_code = data.get("device_code", "")
            verify_url = data.get("verification_url", "")
            if not device_code or not verify_url:
                raise RuntimeError(f"auth login response missing device_code/verification_url: {data!r}")
            now = datetime.datetime.now(datetime.UTC).isoformat()
            self.store.upsert_user(
                open_id,
                auth_status="pending",
                pending_code=device_code,
                pending_url=verify_url,
                pending_at=now,
            )
            logger.info(
                f"[auth] start_auth ok: {self._name(open_id)} device_code={device_code[:8]}... url={verify_url}"
            )
            return data

    def revoke_token(self, open_id: str) -> None:
        """Logout the user's lark-cli session so the next start_auth gets fresh scopes."""
        user_home = os.path.join(self.users_dir, open_id)
        try:
            self._run("lark-cli auth logout", home=user_home, timeout=10)
            logger.info(f"lark-cli auth logout for {open_id}")
        except Exception as e:
            logger.warning(f"lark-cli auth logout failed for {open_id} (non-fatal): {e}")

    @staticmethod
    def _lark_token_status(data: dict) -> str:
        status = data.get("tokenStatus")
        if status:
            return str(status)
        identities = data.get("identities")
        if isinstance(identities, dict):
            user_identity = identities.get("user")
            if isinstance(user_identity, dict):
                status = user_identity.get("tokenStatus")
                if status:
                    return str(status)
        return "unknown"

    def is_authenticated(self, open_id: str) -> bool:
        """Check if user already has valid lark-cli credentials."""
        user_home = os.path.join(self.users_dir, open_id)
        os.makedirs(user_home, mode=0o700, exist_ok=True)
        self._ensure_user_app_config(user_home)
        result = self._run("lark-cli auth status", home=user_home)
        try:
            data = json.loads(result.stdout)
            status = self._lark_token_status(data)
            ok = status in ("valid", "needs_refresh")
            logger.debug(
                f"[auth] is_authenticated: {self._name(open_id)} tokenStatus={status!r} -> {ok}"
            )
            return ok
        except (json.JSONDecodeError, TypeError):
            logger.warning(
                f"[auth] is_authenticated: {self._name(open_id)} could not parse lark-cli output "
                f"stdout={result.stdout[:100]!r} stderr={result.stderr[:100]!r}"
            )
            return False

    def poll_once(self, open_id: str, code: str) -> bool:
        """
        Poll for device-code completion.
        Returns True if authorized, False if still pending.
        """
        user_home = os.path.join(self.users_dir, open_id)
        # Ensure app config exists on this pod — necessary when this pod did not
        # issue the original device code (e.g. cross-pod auth after a restart).
        os.makedirs(user_home, mode=0o700, exist_ok=True)
        self._ensure_user_app_config(user_home)
        _code_prefix = code[:8] if code else "?"
        _n = self._name(open_id)
        try:
            result = self._run(
                ["lark-cli", "auth", "login", "--device-code", code],
                home=user_home, timeout=15
            )
        except subprocess.TimeoutExpired:
            # lark-cli blocks until the user authorises or the code expires.
            # A timeout just means we didn't wait long enough — the user may have
            # already completed auth in the browser, so fall through to the disk
            # check rather than returning False immediately.
            logger.debug(f"[auth] poll_once: {_n} code={_code_prefix}... lark-cli timeout, checking disk")
            if self.is_authenticated(open_id):
                self.store.mark_authorized(open_id)
                return True
            return False

        combined = (result.stdout + result.stderr).strip()
        if not combined:
            # Empty output can happen when lark-cli exits cleanly after writing
            # the token to disk without printing anything — check disk anyway.
            logger.debug(f"[auth] poll_once: {_n} code={_code_prefix}... empty output, checking disk")
            if self.is_authenticated(open_id):
                self.store.mark_authorized(open_id)
                return True
            return False

        # Still waiting — clear signal
        if "authorization_pending" in combined.lower():
            logger.debug(f"[auth] poll_once: {_n} code={_code_prefix}... still pending")
            return False

        # Try JSON parse for a definitive result.
        for raw in (result.stdout, result.stderr):
            raw = raw.strip()
            if not raw:
                continue
            try:
                data = json.loads(raw)
                if data.get("ok") is True:
                    logger.info(f"[auth] poll_once: {_n} code={_code_prefix}... ok=true, marking authorized")
                    self.store.mark_authorized(open_id)
                    return True
                # ok:false could mean device_code was consumed by a successful auth
                # (code becomes invalid after user authorizes it) — fall through to
                # is_authenticated() disk check instead of returning False immediately.
                logger.debug(
                    f"[auth] poll_once: {_n} code={_code_prefix}... ok=false "
                    f"({data.get('error', '?')}), falling through to disk check"
                )
            except (json.JSONDecodeError, TypeError):
                pass

        # Fallback: verify token was actually written to disk.
        # This handles the common case where device_code is "invalid" because the
        # user just consumed it by completing authorization in the browser.
        logger.debug(f"[auth] poll_once: {_n} code={_code_prefix}... fallback disk check")
        if self.is_authenticated(open_id):
            logger.info(f"[auth] poll_once: {_n} code={_code_prefix}... fallback disk check succeeded")
            self.store.mark_authorized(open_id)
            return True

        logger.debug(f"[auth] poll_once: {open_id} code={_code_prefix}... not authorized yet")
        return False

    # ------------------------------------------------------------------
    # Meegle auth (per-user device-code flow, mirrors lark-cli auth)
    # ------------------------------------------------------------------

    def _run_meegle(self, args: list[str], home: str, timeout: int = 30) -> subprocess.CompletedProcess:
        env = {**os.environ, "HOME": home}
        return subprocess.run(
            [_MEEGLE_CMD] + args,
            capture_output=True, text=True, encoding="utf-8", timeout=timeout, env=env,
        )

    def start_meegle_auth(self, open_id: str) -> dict:
        """
        Start meegle device-code OAuth flow for a user.
        Returns {"url": ..., "device_code": ..., "client_id": ...}
        """
        user_home = os.path.join(self.users_dir, open_id)
        os.makedirs(user_home, mode=0o700, exist_ok=True)

        host = os.environ.get("MEEGLE_HOST", _MEEGLE_HOST)
        result = self._run_meegle(
            ["auth", "login", "--device-code", "--phase", "init", "--host", host],
            home=user_home,
        )
        raw = result.stdout.strip()
        if not raw:
            raise RuntimeError(f"meegle auth init failed: {result.stderr}")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"meegle auth init returned non-JSON: {raw!r}") from exc

        device_code = data.get("device_code", "")
        client_id = data.get("client_id", "")
        if not device_code or not client_id:
            raise RuntimeError(f"meegle auth init response missing device_code/client_id: {data!r}")
        verify_url = data.get("verification_uri_complete", data.get("verification_uri", ""))
        now = datetime.datetime.now(datetime.UTC).isoformat()
        self.store.upsert_user(
            open_id,
            meegle_auth_status="pending",
            meegle_pending_code=device_code,
            meegle_pending_client_id=client_id,
            meegle_pending_url=verify_url,
            meegle_pending_at=now,
        )
        return {
            "url": verify_url,
            "device_code": device_code,
            "client_id": client_id,
        }

    def meegle_auth_status(self, open_id: str) -> dict:
        """Return structured Meegle credential status for this user."""
        user_home = os.path.join(self.users_dir, open_id)
        try:
            result = self._run_meegle(["auth", "status"], home=user_home)
        except (subprocess.TimeoutExpired, subprocess.SubprocessError) as exc:
            logger.warning("[meegle-auth] status ctx=%s retryable=true error=%s", open_id, exc)
            return {
                "authenticated": False,
                "host": "",
                "reason": "status_probe_failed",
                "returncode": 2,
                "retryable": True,
            }

        raw = (result.stdout or "").strip()
        returncode = getattr(result, "returncode", 0)
        try:
            data = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, TypeError):
            data = {}

        authenticated = bool(data.get("authenticated"))
        reason = str(data.get("reason") or "")
        host = str(data.get("host") or "")
        retryable = returncode == 2 or reason == "server_unreachable_or_error"
        status = {
            "authenticated": authenticated,
            "host": host,
            "reason": reason,
            "returncode": returncode,
            "retryable": retryable,
        }
        if "expires_in_minutes" in data:
            status["expires_in_minutes"] = data.get("expires_in_minutes")
        logger.info(
            "[meegle-auth] status ctx=%s authenticated=%s host=%s reason=%s returncode=%s retryable=%s expires_in_minutes=%s",
            open_id,
            authenticated,
            host,
            reason,
            returncode,
            retryable,
            status.get("expires_in_minutes", ""),
        )
        return status

    def is_meegle_authenticated(self, open_id: str) -> bool:
        """Check if user already has valid meegle credentials."""
        return bool(self.meegle_auth_status(open_id).get("authenticated"))

    def wait_for_meegle_authenticated(self, open_id: str, attempts: int = 3,
                                      delay_seconds: float = 0.5) -> bool:
        """Shortly retry Meegle status after device-code poll writes credentials."""
        for attempt in range(max(1, attempts)):
            status = self.meegle_auth_status(open_id)
            if status.get("authenticated"):
                return True
            if attempt < attempts - 1:
                time.sleep(delay_seconds)
        return False

    def revoke_meegle_token(self, open_id: str) -> None:
        """Logout the user's meegle session so the next start_meegle_auth gets fresh scopes."""
        user_home = os.path.join(self.users_dir, open_id)
        try:
            self._run_meegle(["auth", "logout"], home=user_home, timeout=10)
            logger.info(f"meegle auth logout for {open_id}")
        except Exception as e:
            logger.warning(f"meegle auth logout failed for {open_id} (non-fatal): {e}")

    def poll_meegle_once(self, open_id: str, client_id: str, device_code: str) -> bool:
        """
        Single non-blocking poll for meegle device-code completion.
        Returns True if authorized, False if still pending.
        """
        user_home = os.path.join(self.users_dir, open_id)
        try:
            result = self._run_meegle(
                ["auth", "login", "--device-code", "--phase", "poll",
                 "--client-id", client_id,
                 "--device-code-value", device_code,
                 "--once"],
                home=user_home, timeout=10,
            )
        except subprocess.TimeoutExpired:
            if self.wait_for_meegle_authenticated(open_id):
                self.store.mark_meegle_authorized(open_id)
                return True
            return False

        combined = (result.stdout + result.stderr).lower()

        # Still waiting — clear signal, skip the disk check to avoid a redundant
        # subprocess call on every poll cycle.
        if "authorization_pending" in combined or "pending" in combined:
            return False

        # Poll subprocess finished without a "pending" signal — verify token on disk.
        # This is the normal success path: the meegle CLI writes the token and exits 0,
        # or the device_code was already consumed (code becomes invalid after the user
        # authorizes), so we fall through to the status check as a definitive answer.
        if self.wait_for_meegle_authenticated(open_id):
            self.store.mark_meegle_authorized(open_id)
            return True

        return False
