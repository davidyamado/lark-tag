#!/usr/bin/env python3
# src/bash_guard.py
"""
Claude Code PreToolUse hook for the Bash tool.

Reads a JSON hook event from stdin, inspects the command, and:
  - exits 0 → allow the Bash tool call
  - exits 2 → block (stderr message is fed back to Claude as a tool error,
              so the model can apologise to the user instead of looping)

Designed to be narrow: false positives here directly hurt the user experience,
so only commands with no legitimate use inside this bot's workflow are blocked.
The egress proxy (egress_proxy.py) is the primary network filter — this hook
catches command-level patterns the proxy can't see (e.g. raw-socket / proc
snooping / kube tooling) and provides defense in depth at the language level.

Categories blocked:
  1. Kubernetes / container-runtime tooling (kubectl, crictl, ctr, nerdctl)
  2. Reads of K8s service-account token or process environ snooping
  3. Outbound network commands targeting RFC1918 / link-local / K8s / metadata
  4. Reverse-shell patterns (/dev/tcp/, mkfifo+nc, python -c 'socket.connect',
     interactive bash with output redirect)
"""
from __future__ import annotations

import json
import os
import re
import sys

_DANGER_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # 0. Bulk environment-variable dumps (would leak ANTHROPIC_API_KEY,
    #    INTERNAL_API_TOKEN, FEISHU_OPEN_ID etc.). Allow `printenv VAR` /
    #    `env -i cmd` / `env VAR=val cmd` (those have a non-empty arg);
    #    block bare `env` / `printenv` / `env | grep ...` / `env > file`.
    (re.compile(r"\b(?:env|printenv)\s*(?:[|;&<>]|$)"),
     "Bulk listing of environment variables is not allowed"),

    # 1. Container / cluster tooling
    (re.compile(r"\b(?:kubectl|crictl|ctr|nerdctl|kubeadm)\b"),
     "Kubernetes/container tooling is not allowed"),

    # 2. Secrets and process snooping
    (re.compile(r"/var/run/secrets(?:/|\b)"),
     "Reading service-account secrets is not allowed"),
    (re.compile(r"/proc/\d+/(?:environ|cmdline|fd/|status)"),
     "Snooping other processes is not allowed"),
    (re.compile(r"/proc/self/(?:environ|cmdline)"),
     "Reading own process credentials is not allowed"),

    # 3. Reverse-shell patterns
    (re.compile(r"/dev/tcp/"),
     "Reverse-shell pattern (/dev/tcp/) is not allowed"),
    (re.compile(r"\bmkfifo\b.*\b(?:nc|netcat|ncat)\b", re.DOTALL),
     "Reverse-shell pattern (mkfifo+nc) is not allowed"),
    # Match `python -c '...'` whose body imports/uses socket — covers
    # `s.connect((...))`, `socket.create_connection`, etc., not just `socket.connect`.
    (re.compile(r"python\d*\s+-c\b.+(?:import\s+socket|socket\.socket\b|"
                r"\.create_connection\b|socket\.create_connection\b|"
                r"\.connect\s*\(\s*\()",
                re.DOTALL),
     "Reverse-shell pattern (python+socket) is not allowed"),
    (re.compile(r"\bbash\b\s+-i\b[^|;&]*>"),
     "Interactive bash with output redirect is not allowed"),

    # 4. Private / cluster-internal network destinations
    (re.compile(
        r"(?:^|[\s\'\"/@:=])"                              # boundary considering URL chars
        r"(?:"
        r"10\.\d{1,3}\.\d{1,3}\.\d{1,3}"                  # 10.0.0.0/8
        r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"   # 172.16/12
        r"|192\.168\.\d{1,3}\.\d{1,3}"                    # 192.168/16
        r"|169\.254\.\d{1,3}\.\d{1,3}"                    # link-local / metadata
        r")"
        r"(?:[:/\s]|$)"
    ), "Internal/private IP destinations are blocked"),

    (re.compile(r"\.svc\.cluster\.local\b"),
     "Kubernetes service domains are blocked"),
    (re.compile(r"\bmetadata\.google\.internal\b"),
     "Cloud metadata endpoint is blocked"),
    (re.compile(r"\bkubernetes\.default(?:\.svc)?\b"),
     "Kubernetes API hostname is blocked"),

    # 5. Direct lark-cli auth login / logout — must go through the bot's
    #    /lark-reauth internal API so main.py can manage the device-code
    #    lifecycle and auto-resume poller. Direct calls generate orphan
    #    device codes the bot can't track, causing duplicate auth links
    #    sent to the user and "click does nothing" loops.
    (re.compile(r"\blark-cli\b\s+auth\s+(?:login|logout)\b"),
     "lark-cli auth login/logout must go through /lark-reauth API "
     "(curl -X POST http://127.0.0.1:$INTERNAL_API_PORT/lark-reauth ...)"),
]


def _decide(cmd: str) -> tuple[bool, str]:
    """Returns (allowed, reason). reason is non-empty only when allowed=False."""
    for pattern, reason in _DANGER_PATTERNS:
        if pattern.search(cmd):
            return False, reason

    # Cross-user path access: refuse commands that touch another user's home.
    cross_user_block = _check_cross_user_paths(cmd)
    if cross_user_block:
        return False, cross_user_block

    # Second pass: if the command invokes an interpreter on a script file, peek
    # inside the script. This catches attacks where the dangerous behaviour is
    # in the script body rather than the command line — e.g. when a user-asked
    # Claude to write message_monitor.py that calls lark-cli with --as bot.
    # When bubblewrap is unavailable on the node, this is the main remaining
    # defence against bot-credential abuse.
    return _decide_script_content(cmd)


# Matches `/var/lark-bot/users/ou_<hex>/...` paths regardless of how the command
# wraps them (cd, cat, ls, grep, etc.). The user's own open_id is taken from
# FEISHU_OPEN_ID, which agent.py sets when spawning the Claude subprocess.
_USER_PATH_PATTERN = re.compile(
    r"/var/lark-bot/users/(ou_[a-zA-Z0-9_]+)\b"
)


def _check_cross_user_paths(cmd: str) -> str:
    """Return a reason string if the command references another user's home;
    empty string when no issue. Caller's identity comes from $FEISHU_OPEN_ID."""
    my_uid = os.environ.get("FEISHU_OPEN_ID", "").strip()
    if not my_uid:
        return ""  # can't enforce without identity; let it pass
    for match in _USER_PATH_PATTERN.finditer(cmd):
        target = match.group(1)
        if target != my_uid:
            return f"cross-user data access (caller={my_uid}, target={target})"
    return ""


# Same high-precision rules as src/file_watcher.py, kept inline so this hook
# stays a self-contained subprocess (it runs OUTSIDE the bot's Python process
# and can't import the watcher module reliably from /opt/bot-guard).
_SCRIPT_SIGNATURES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"--as\s+bot\b|--as=bot\b"),
     "script body uses --as bot (bot-credential escalation)"),
    (re.compile(r"['\"]--as['\"]\s*,\s*['\"]bot['\"]"),
     "script body uses --as bot (list syntax)"),
    (re.compile(r"\btenant_access_token\b"),
     "script body references tenant_access_token"),
    (re.compile(r"/open-apis/auth/v3/(?:tenant|app)_access_token"),
     "script body calls Feishu app-level auth endpoint"),
    (re.compile(r"\bapp_secret\b\s*[:=]"),
     "script body assigns app_secret"),
]

# Match "python script.py", "python3 -u foo.py", "bash run.sh", "node x.js" etc.
# Captures the file path as group 2. Stops at shell separators (|;&) and
# argument-looking tokens. The path char class accepts Linux (/) and Windows
# (\, :) separators so local dev tests work too.
_INTERPRETER_PATTERN = re.compile(
    r"\b(python\d*|bash|sh|zsh|node|deno|ruby|perl)\b[^|;&]*?"
    r"(?<=\s)([\w./\-:\\]+\.(?:py|sh|bash|js|mjs|ts|rb|pl))\b"
)


def _decide_script_content(cmd: str) -> tuple[bool, str]:
    """
    If the command runs an interpreter on a script file we can read, scan the
    file for high-confidence abuse signatures. Returns the same (allowed, reason)
    tuple as _decide.
    """
    for match in _INTERPRETER_PATTERN.finditer(cmd):
        path = match.group(2)
        # Only check paths we can actually open. Skip relative paths the script
        # might resolve later — false-negatives are acceptable here; the file
        # watcher and the post-stream marker provide additional coverage.
        if not os.path.isabs(path):
            # Best-effort: try relative to bot's typical user_home pattern
            for prefix in ("/var/lark-bot/users/", "/home/botuser/"):
                # Don't probe; only check if cmd happens to mention an absolute path
                pass
            continue
        try:
            if not os.path.isfile(path):
                continue
            with open(path, "rb") as f:
                raw = f.read(16 * 1024)
            text = raw.decode("utf-8", errors="replace")
        except OSError:
            continue
        for pattern, reason in _SCRIPT_SIGNATURES:
            if pattern.search(text):
                return False, f"{reason} (path={path})"
    return True, ""


def main() -> None:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        # Malformed input — be permissive so a misconfigured hook doesn't
        # take the bot down for every Bash call.
        sys.exit(0)

    if event.get("tool_name") != "Bash":
        sys.exit(0)

    cmd = (event.get("tool_input") or {}).get("command")
    if not isinstance(cmd, str) or not cmd:
        sys.exit(0)

    allowed, reason = _decide(cmd)
    if allowed:
        sys.exit(0)

    truncated = cmd if len(cmd) <= 200 else cmd[:200] + "…"
    print(
        f"Blocked by bot security policy: {reason}\n"
        f"Command: {truncated}\n"
        f"If this is a legitimate request, explain to the user that this "
        f"action is restricted by the bot's security policy.",
        file=sys.stderr,
    )
    sys.exit(2)


if __name__ == "__main__":
    main()
