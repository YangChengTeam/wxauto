from __future__ import annotations

from apscheduler.schedulers.background import BackgroundScheduler

from wxclient.config import Settings
from wxclient.queue import TaskStore, TaskType


class Scheduler:
    def __init__(self, settings: Settings, store: TaskStore) -> None:
        self.settings = settings
        self.store = store
        self._scheduler = BackgroundScheduler(timezone="UTC")

    def start(self) -> None:
        if self._scheduler.running:
            return
        self._scheduler.add_job(
            self.enqueue_friend_check,
            "interval",
            seconds=self.settings.friend_check_interval_seconds,
            id="check_new_friends",
            replace_existing=True,
            next_run_time=None,
        )
        self.enqueue_friend_check()
        self._scheduler.start()

    def stop(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    def enqueue_friend_check(self) -> None:
        self.store.create_task(
            TaskType.CHECK_NEW_FRIENDS,
            {
                "verify": False,
                "limit": 8,
                "dry_run": self.settings.dry_run,
                "source": "scheduler",
            },
            self.settings.default_account_id,
        )
