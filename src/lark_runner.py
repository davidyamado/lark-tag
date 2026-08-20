import os
import re
import shlex
import subprocess
import sys

_LARK_CLI = "lark-cli.cmd" if sys.platform == "win32" else "lark-cli"

# Signals that definitively indicate token expiry — matched only against stderr
# (lark-cli writes error JSON / messages to stderr, document content to stdout).
# "unauthorized" is intentionally NOT matched against stdout to avoid false positives
# when document content happens to contain that word.
_TOKEN_EXPIRED_SIGNALS_STDERR = (
    "token_expired",
    "token invalid",
    "unauthorized",
    "please login",
    "401 unauthorized",
)

# A small set of signals unambiguous enough to match in full output (stdout+stderr).
# These strings are lark-cli internal error codes, not prose that could appear in docs.
_TOKEN_EXPIRED_SIGNALS_ANY = (
    "token_expired",
    "please login",
)


class TokenExpiredError(Exception):
    """Raised when lark-cli output indicates the user token has expired."""


def run_lark_cli(command: str, open_id: str, users_dir: str, timeout: int = 30) -> str:
    if not command.strip().startswith("lark-cli "):
        return "错误：只允许执行 lark-cli 命令"

    # Strip any pre-existing --as flag to prevent duplication
    command_clean = re.sub(r'\s+--as\s+\S+', '', command.rstrip())
    final_command = command_clean + " --as user"
    user_home = os.path.join(users_dir, open_id)
    env = {
        **os.environ,
        "HOME": user_home,
        # Override pod-level LARKSUITE_CLI_CONFIG_DIR to keep each user's
        # credentials isolated in their own writable directory.
        "LARKSUITE_CLI_CONFIG_DIR": os.path.join(user_home, ".lark-cli"),
    }

    try:
        args = shlex.split(final_command)
        args[0] = _LARK_CLI
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            env=env,
        )
        output = result.stdout or result.stderr
        # Detect token expiry before returning to caller.
        # Match the precise signals against the full output, and the broader set
        # (which includes "unauthorized") only against stderr to avoid false positives
        # when document content happens to contain those words.
        stderr_lower = result.stderr.lower()
        stdout_lower = result.stdout.lower()
        expired = (
            any(sig in stdout_lower or sig in stderr_lower
                for sig in _TOKEN_EXPIRED_SIGNALS_ANY)
            or any(sig in stderr_lower for sig in _TOKEN_EXPIRED_SIGNALS_STDERR)
        )
        if expired:
            raise TokenExpiredError(f"Token expired for {open_id}: {output[:200]}")
        return output
    except TokenExpiredError:
        raise
    except subprocess.TimeoutExpired:
        return f"错误：命令执行超时（{timeout}s）"
    except Exception as e:
        return f"错误：{e}"
