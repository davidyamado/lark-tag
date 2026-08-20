"""Run only the Lark SDK card-action listener for callback diagnostics."""

import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.card_action_listener import InteractiveFormHandler, start_card_action_listener
from src.config import Config


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        handlers=[
            logging.FileHandler("sdk_diag.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )

    cfg = Config()
    handler = InteractiveFormHandler(None, None, cfg.feishu_app_id, cfg.feishu_app_secret)
    listener = start_card_action_listener(cfg.feishu_app_id, cfg.feishu_app_secret, handler)
    with open("sdk_diag.pid", "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    logging.info("SDK-only diagnostic listener started pid=%s", os.getpid())
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        listener.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
