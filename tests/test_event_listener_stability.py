"""
Test: Event listener stability -- no unnecessary subscriber kills.

The event listener subscribes to Feishu events via lark-cli WebSocket.
Each time the subscriber is killed, a server-side zombie WebSocket is created.
Events are randomly split across all connections (zombies + live).
N zombie connections -> only ~1/(N+1) messages reach the live connection.

This test verifies:
  The subscriber process stays alive during quiet periods (no events).
  If the subscriber is killed/restarted, the test FAILS.
"""
import logging
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(message)s")

import src.event_listener as el
from src.event_listener import EventListener


def test_subscriber_stays_alive_during_quiet_period():
    """
    Start the subscriber and observe for 60 seconds.
    The subscriber MUST NOT be killed during this quiet period.
    Reconnect count > 0 means the subscriber died -> FAIL.
    """
    observation_seconds = 60
    reconnect_count = 0

    def on_message(event):
        pass

    original_run_loop = EventListener._run_loop

    def counting_run_loop(self):
        nonlocal reconnect_count
        self._cleanup_stale_subscribers()
        while not self._stop_event.is_set():
            try:
                self._listen_once()
            except Exception:
                pass
            if not self._stop_event.is_set():
                reconnect_count += 1
                self._stop_event.wait(timeout=el.RECONNECT_DELAY)

    EventListener._run_loop = counting_run_loop

    bot_home = os.environ.get("LARK_BOT_HOME", os.path.expanduser("~"))
    listener = EventListener(bot_home=bot_home, on_message=on_message)

    try:
        print(f"Starting subscriber...")
        listener.start()
        print(f"Observing for {observation_seconds}s with no messages...")

        time.sleep(observation_seconds)

        print(f"\nResults after {observation_seconds}s:")
        print(f"  Reconnect count: {reconnect_count}")

        if reconnect_count > 0:
            print(f"\nFAIL: Subscriber was killed {reconnect_count} time(s) during quiet period.")
            print(f"  Each kill creates a server-side zombie WebSocket connection.")
            print(f"  With {reconnect_count} zombies, only ~{100//(reconnect_count+1)}% of events")
            print(f"  reach the live connection. This is the root cause of lost messages.")
            return False
        else:
            print(f"\nPASS: Subscriber stayed alive for {observation_seconds}s. No zombie connections created.")
            return True
    finally:
        listener.stop()
        EventListener._run_loop = original_run_loop


if __name__ == "__main__":
    ok = test_subscriber_stays_alive_during_quiet_period()
    sys.exit(0 if ok else 1)
