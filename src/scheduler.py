# src/scheduler.py
"""
Background scheduler thread.

Every POLL_INTERVAL seconds, scans scheduled_jobs for due jobs and fires them:
  - reminder: sends a text card directly via feishu_api
  - ai_task:  calls stream_claude_fn (extracted from main._stream_claude)
"""
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from src.job_store import JobStore
from src.schedule_utils import compute_next_run

logger = logging.getLogger(__name__)

POLL_INTERVAL = 30  # seconds


class SchedulerThread:
    def __init__(
        self,
        job_store: JobStore,
        executor: ThreadPoolExecutor,
        feishu_api,           # the feishu_api module
        app_id: str,
        app_secret: str,
        stream_claude_fn: Callable,
    ):
        """
        Args:
            stream_claude_fn: callable with signature
                stream_claude_fn(open_id, prompt, chat_id) -> None
              Sends the AI-task result to the user's P2P chat.
        """
        self.job_store = job_store
        self.executor = executor
        self.feishu_api = feishu_api
        self.app_id = app_id
        self.app_secret = app_secret
        self.stream_claude_fn = stream_claude_fn
        self._stop = threading.Event()

    def start(self) -> None:
        t = threading.Thread(target=self._loop, daemon=True, name="SchedulerThread")
        t.start()
        logger.info("SchedulerThread started (poll interval=%ds)", POLL_INTERVAL)

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.wait(timeout=POLL_INTERVAL):
            try:
                self._tick()
            except Exception:
                logger.exception("SchedulerThread._tick raised an unexpected error")

    def _tick(self) -> None:
        now_ms = int(time.time() * 1000)
        if hasattr(self.job_store, "claim_due_jobs"):
            due = self.job_store.claim_due_jobs(now_ms)
        else:
            due = self.job_store.get_due_jobs(now_ms)
        if due:
            logger.info("Scheduler: %d due job(s)", len(due))
        for job in due:
            self.executor.submit(self._execute_job, job)

    def _execute_job(self, job: dict) -> None:
        job_id = job["id"]
        job_type = job["job_type"]
        open_id = job["open_id"]
        chat_id = job["chat_id"]
        content = job["content"]
        schedule_type = job["schedule_type"]
        mention_open_id = job.get("mention_open_id")

        logger.info(
            "Executing job %s (type=%s schedule=%s open_id=%s)",
            job_id[:8], job_type, schedule_type, open_id,
        )

        # Advance state BEFORE execution so the next tick doesn't re-fire this job
        # while it's still running (ai_task can take longer than POLL_INTERVAL).
        if schedule_type == "once":
            self.job_store.mark_completed(job_id)
            logger.info("Job %s marked completed (once)", job_id[:8])
        else:
            now_ms = int(time.time() * 1000)
            spec = job["schedule_spec"]
            try:
                next_run_at = compute_next_run(spec, now_ms)
                self.job_store.update_next_run(job_id, next_run_at)
                logger.info("Job %s rescheduled, next=%d", job_id[:8], next_run_at)
            except Exception:
                logger.exception("Could not reschedule job %s — will still execute this run", job_id)

        try:
            if job_type == "reminder":
                self._send_reminder(open_id, chat_id, content, mention_open_id)
            elif job_type == "ai_task":
                self._run_ai_task(open_id, chat_id, content)
            else:
                logger.warning("Unknown job_type %r for job %s", job_type, job_id)
        except Exception:
            logger.exception("Job %s failed during execution", job_id)

    def _send_reminder(self, open_id: str, chat_id: str, text: str,
                       mention_open_id: str | None = None) -> None:
        token = self.feishu_api.get_tenant_access_token(self.app_id, self.app_secret)
        if chat_id.startswith("oc_"):
            # Group chat — prepend @mention then send to the group
            target = mention_open_id or open_id
            mention = f'<at id="{target}"></at> '
            self.feishu_api.send_text_card_to_chat(chat_id, mention + text, token)
            logger.info("Reminder sent to group chat_id=%s (mention=%s)", chat_id, target)
        else:
            # P2P — send to the user directly
            self.feishu_api.send_text_card(open_id, text, token)
            logger.info("Reminder sent to open_id=%s", open_id)

    def _run_ai_task(self, open_id: str, chat_id: str, prompt: str) -> None:
        self.stream_claude_fn(open_id=open_id, prompt=prompt, chat_id=chat_id)
