"""
End-to-end message delivery test.

Sends 3 messages to the bot via lark-cli and verifies each one gets a reply.
Uses the real Feishu API -- bot must be running.

Usage:
    python tests/test_message_delivery.py
"""
import json
import os
import subprocess
import sys
import time

CHAT_ID = "oc_4cf333e123170f2f84c70dd1d433af20"
SEND_INTERVAL = 10   # seconds between messages
POLL_INTERVAL = 5    # seconds between reply checks
MAX_WAIT = 90        # max seconds to wait for a reply per message
_LARK_CLI = "lark-cli.cmd" if sys.platform == "win32" else "lark-cli"

TEST_MESSAGES = [
    "test-ping-1: 你好",
    "test-ping-2: 1+1等于几",
    "test-ping-3: 今天星期几",
]


def run_cli(args: list[str], timeout: int = 30) -> str:
    """Run a lark-cli command and return stdout."""
    result = subprocess.run(
        [_LARK_CLI] + args,
        capture_output=True, text=True, encoding="utf-8", timeout=timeout,
    )
    return result.stdout


def send_message(text: str) -> bool:
    """Send a message as user to the bot chat. Returns True if sent."""
    out = run_cli([
        "im", "+messages-send",
        "--as", "user",
        "--chat-id", CHAT_ID,
        "--text", text,
    ])
    try:
        data = json.loads(out)
        return data.get("ok", False)
    except (json.JSONDecodeError, TypeError):
        return "ok" in out and "true" in out


def get_latest_bot_reply_id() -> str:
    """Get the message_id of the most recent bot reply in the chat."""
    out = run_cli([
        "im", "+chat-messages-list",
        "--as", "user",
        "--chat-id", CHAT_ID,
        "--sort", "desc",
        "--page-size", "5",
    ])
    try:
        data = json.loads(out)
        msgs = []
        if isinstance(data, dict):
            inner = data.get("data", data)
            msgs = inner.get("messages", inner.get("items", []))
        elif isinstance(data, list):
            msgs = data

        for msg in msgs:
            sender = msg.get("sender", {})
            if sender.get("sender_type") == "app":
                return msg.get("message_id", "")
    except (json.JSONDecodeError, TypeError):
        pass
    return ""


def wait_for_new_bot_reply(old_reply_id: str, timeout: int = MAX_WAIT) -> bool:
    """Wait until a new bot reply appears (different message_id from old_reply_id)."""
    start = time.time()
    while time.time() - start < timeout:
        time.sleep(POLL_INTERVAL)
        new_id = get_latest_bot_reply_id()
        if new_id and new_id != old_reply_id:
            return True
        elapsed = int(time.time() - start)
        print(f"    waiting... ({elapsed}s / {timeout}s)")
    return False


def main():
    print("=" * 60)
    print("End-to-end message delivery test")
    print("=" * 60)

    # Check bot is running
    result = subprocess.run(
        ["bash", "bot.sh", "status"],
        capture_output=True, text=True, cwd="c:/code/feishu",
    )
    if "running" not in result.stdout.lower():
        print("FAIL: Bot is not running. Start it with: bash bot.sh start")
        sys.exit(1)
    print(f"Bot status: {result.stdout.strip()}")

    results = []

    for i, text in enumerate(TEST_MESSAGES):
        print(f"\n--- Message {i+1}/{len(TEST_MESSAGES)}: \"{text}\" ---")

        # Get latest bot reply BEFORE sending
        before_id = get_latest_bot_reply_id()
        print(f"  Latest bot reply before: {before_id[:20]}...")

        # Send
        print(f"  Sending...")
        ok = send_message(text)
        print(f"  Sent: {'OK' if ok else 'FAILED'}")

        if not ok:
            results.append((text, False))
            continue

        # Wait for a NEW bot reply
        print(f"  Waiting for bot reply (max {MAX_WAIT}s)...")
        got_reply = wait_for_new_bot_reply(before_id, MAX_WAIT)

        if got_reply:
            new_id = get_latest_bot_reply_id()
            print(f"  -> Reply received! (id={new_id[:20]}...)")
            results.append((text, True))
        else:
            print(f"  -> NO REPLY within {MAX_WAIT}s")
            results.append((text, False))

        # Wait between messages
        if i < len(TEST_MESSAGES) - 1:
            print(f"  Waiting {SEND_INTERVAL}s before next message...")
            time.sleep(SEND_INTERVAL)

    # Summary
    print("\n" + "=" * 60)
    print("RESULTS:")
    total = len(results)
    replied = sum(1 for _, r in results if r)
    for text, reply in results:
        status = "OK" if reply else "MISSING REPLY"
        print(f"  [{status}] {text}")

    print(f"\n{replied}/{total} messages got a reply.")

    # Check bot log for watchdog kills
    try:
        with open("/tmp/bot.log", "r") as f:
            log = f.read()
        watchdog_kills = log.count("Watchdog")
        reconnects = log.count("Reconnecting")
        if watchdog_kills > 0 or reconnects > 0:
            print(f"\nWARNING: Bot log shows {watchdog_kills} watchdog kills, {reconnects} reconnects")
        else:
            print(f"\nBot log: 0 watchdog kills, 0 reconnects (stable connection)")
    except Exception:
        pass

    if replied == total:
        print("\nPASS: All messages received replies.")
        sys.exit(0)
    else:
        print(f"\nFAIL: {total - replied} message(s) got no reply.")
        sys.exit(1)


if __name__ == "__main__":
    main()
