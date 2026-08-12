from __future__ import annotations

import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from contextlib import closing
from pathlib import Path
from sqlite3 import connect
from typing import Any

from wxclient.queue import OperationWorker, Task, TaskStatus, TaskStore, TaskType
from wxclient.wechat_api import WeChatApiError


class _FakeApi:
    def __init__(self) -> None:
        self.scanned: list[str] = []

    def search_contacts(self, query: str, limit: int) -> dict[str, Any]:
        return {
            "action": "search_contacts",
            "query": query,
            "matches": [{"name": query, "type": "contact", "exact": True}],
            "match_count": 1,
            "limit": limit,
        }

    def get_contact_profile(self, friend: str) -> dict[str, Any]:
        return {"action": "get_contact_profile", "friend": friend, "profile": {"昵称": friend}}

    def list_group_members(self, group: str) -> dict[str, Any]:
        return {"action": "list_group_members", "group": group, "members": ["甲", "乙"]}

    def send_files(
        self,
        friend: str,
        files: list[str],
        text: str,
        dry_run: bool | None,
    ) -> dict[str, Any]:
        return {
            "action": "send_files",
            "friend": friend,
            "files": files,
            "text": text,
            "dry_run": bool(dry_run),
        }

    def get_unread_messages(self) -> dict[str, Any]:
        return {"action": "get_unread_messages", "total_unread": 2}

    def read_chat_history(self, friend: str, limit: int) -> dict[str, Any]:
        return {"action": "read_chat_history", "friend": friend, "limit": limit}

    def scan_messages(
        self,
        friend: str,
        duration: str,
        select_sender: bool,
        save_file: bool,
        save_media: bool,
        dry_run: bool | None,
    ) -> dict[str, Any]:
        self.scanned.append(friend)
        return {
            "action": "scan_messages",
            "friend": friend,
            "duration": duration,
            "dry_run": bool(dry_run),
            "messages": [{"content": f"message from {friend}"}],
            "message_count": 1,
        }


class _FailingApi(_FakeApi):
    def search_contacts(self, query: str, limit: int) -> dict[str, Any]:
        raise WeChatApiError("transient read failure")

    def send_message(
        self,
        friend: str,
        text: str,
        dry_run: bool | None,
    ) -> dict[str, Any]:
        raise WeChatApiError("result unknown")


class _BlockingApi(_FakeApi):
    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.call_count = 0

    def search_contacts(self, query: str, limit: int) -> dict[str, Any]:
        self.call_count += 1
        self.entered.set()
        self.release.wait(timeout=5)
        return super().search_contacts(query, limit)


class TaskStoreTests(unittest.TestCase):
    def test_only_identical_pending_tasks_are_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            first = store.create_task(
                TaskType.SEND_MESSAGE,
                {"friend": "a", "text": "one"},
                "default",
            )
            duplicate = store.create_task(
                TaskType.SEND_MESSAGE,
                {"text": "one", "friend": "a"},
                "default",
            )
            different = store.create_task(
                TaskType.SEND_MESSAGE,
                {"friend": "b", "text": "two"},
                "default",
            )
        self.assertEqual(first.id, duplicate.id)
        self.assertNotEqual(first.id, different.id)

    def test_legacy_pending_task_is_backfilled_and_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database_path = Path(tmp) / "tasks.db"
            with closing(connect(database_path)) as connection:
                connection.execute(
                    """
                    CREATE TABLE tasks (
                        id VARCHAR PRIMARY KEY,
                        type VARCHAR NOT NULL,
                        payload TEXT NOT NULL,
                        status VARCHAR NOT NULL,
                        account_id VARCHAR NOT NULL,
                        attempts INTEGER NOT NULL,
                        max_attempts INTEGER NOT NULL,
                        created_at VARCHAR NOT NULL,
                        updated_at VARCHAR NOT NULL,
                        available_at VARCHAR NOT NULL,
                        last_error TEXT,
                        result TEXT
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO tasks VALUES (
                        'legacy-task', 'send_message',
                        '{"friend":"a","text":"one"}', 'pending', 'default',
                        0, 3, '2026-01-01T00:00:00+00:00',
                        '2026-01-01T00:00:00+00:00',
                        '2026-01-01T00:00:00+00:00', NULL, NULL
                    )
                    """
                )
                connection.commit()

            store = TaskStore(database_path)
            duplicate = store.create_task(
                TaskType.SEND_MESSAGE,
                {"text": "one", "friend": "a"},
                "default",
            )

        self.assertEqual(duplicate.id, "legacy-task")

    def test_unknown_legacy_task_type_does_not_block_startup(self) -> None:
        """2026-08-12 真机：库里一条 'reply_chat' 让 TaskStore 构造直接抛异常，服务起不来。"""
        with tempfile.TemporaryDirectory() as tmp:
            database_path = Path(tmp) / "tasks.db"
            with closing(connect(database_path)) as connection:
                connection.execute(
                    """
                    CREATE TABLE tasks (
                        id VARCHAR PRIMARY KEY,
                        type VARCHAR NOT NULL,
                        payload TEXT NOT NULL,
                        status VARCHAR NOT NULL,
                        account_id VARCHAR NOT NULL,
                        attempts INTEGER NOT NULL,
                        max_attempts INTEGER NOT NULL,
                        created_at VARCHAR NOT NULL,
                        updated_at VARCHAR NOT NULL,
                        available_at VARCHAR NOT NULL,
                        last_error TEXT,
                        result TEXT
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO tasks VALUES (
                        'stale-task', 'reply_chat',
                        '{"friend":"a"}', 'pending', 'default',
                        0, 3, '2026-01-01T00:00:00+00:00',
                        '2026-01-01T00:00:00+00:00',
                        '2026-01-01T00:00:00+00:00', NULL, NULL
                    )
                    """
                )
                connection.commit()

            store = TaskStore(database_path)  # 不抛异常就是本用例的核心断言

            with closing(connect(database_path)) as connection:
                row = connection.execute(
                    "SELECT status, last_error FROM tasks WHERE id = 'stale-task'"
                ).fetchone()

            self.assertEqual(row[0], TaskStatus.CANCELLED.value)
            self.assertIn("reply_chat", row[1])
            # 脏数据不该拖累正常任务
            fresh = store.create_task(TaskType.SEND_MESSAGE, {"friend": "b", "text": "hi"}, "default")
            self.assertEqual(fresh.status, TaskStatus.PENDING)

    def test_legacy_running_tasks_are_recovered_safely_on_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database_path = Path(tmp) / "tasks.db"
            with closing(connect(database_path)) as connection:
                connection.execute(
                    """
                    CREATE TABLE tasks (
                        id VARCHAR PRIMARY KEY,
                        type VARCHAR NOT NULL,
                        payload TEXT NOT NULL,
                        status VARCHAR NOT NULL,
                        account_id VARCHAR NOT NULL,
                        attempts INTEGER NOT NULL,
                        max_attempts INTEGER NOT NULL,
                        created_at VARCHAR NOT NULL,
                        updated_at VARCHAR NOT NULL,
                        available_at VARCHAR NOT NULL,
                        last_error TEXT,
                        result TEXT
                    )
                    """
                )
                connection.executemany(
                    "INSERT INTO tasks VALUES (?, ?, ?, ?, 'default', 1, 3, ?, ?, ?, NULL, NULL)",
                    [
                        (
                            "legacy-read",
                            "scan_messages",
                            '{"friend":"a","duration":"1s"}',
                            "running",
                            "2026-01-01T00:00:00+00:00",
                            "2026-01-01T00:00:00+00:00",
                            "2026-01-01T00:00:00+00:00",
                        ),
                        (
                            "legacy-write-pending",
                            "send_message",
                            '{"friend":"a","text":"one","dry_run":false}',
                            "pending",
                            "2026-01-01T00:00:00+00:00",
                            "2026-01-01T00:00:00+00:00",
                            "2026-01-01T00:00:00+00:00",
                        ),
                        (
                            "legacy-write-running",
                            "send_message",
                            '{"friend":"a","text":"one","dry_run":false}',
                            "running",
                            "2026-01-02T00:00:00+00:00",
                            "2026-01-02T00:00:00+00:00",
                            "2026-01-02T00:00:00+00:00",
                        ),
                    ],
                )
                connection.commit()

            store = TaskStore(database_path)
            recovered_read = store.get("legacy-read")
            cancelled_duplicate = store.get("legacy-write-pending")
            unknown_write = store.get("legacy-write-running")

        self.assertEqual(recovered_read.status, TaskStatus.PENDING)  # type: ignore[union-attr]
        self.assertIsNone(recovered_read.lease_expires_at)  # type: ignore[union-attr]
        self.assertEqual(cancelled_duplicate.status, TaskStatus.CANCELLED)  # type: ignore[union-attr]
        self.assertEqual(unknown_write.status, TaskStatus.FAILED)  # type: ignore[union-attr]
        self.assertEqual(unknown_write.result["outcome"], "unknown")  # type: ignore[union-attr,index]
        self.assertTrue(unknown_write.result["retry_suppressed"])  # type: ignore[union-attr,index]

    def test_atomic_claim_has_only_one_winner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database_path = Path(tmp) / "tasks.db"
            TaskStore(database_path).create_task(
                TaskType.SCAN_MESSAGES,
                {"friend": "chat-a", "duration": "1s"},
                "default",
            )

            def claim(owner: str) -> Task | None:
                return TaskStore(database_path).claim_next(owner, lease_seconds=30)

            with ThreadPoolExecutor(max_workers=2) as executor:
                claimed = list(executor.map(claim, ["worker-a", "worker-b"]))

        winners = [task for task in claimed if task is not None]
        self.assertEqual(len(winners), 1)

    def test_expired_running_task_is_recovered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.create_task(
                TaskType.SCAN_MESSAGES,
                {"friend": "chat-a", "duration": "1s"},
                "default",
            )
            claimed = store.claim_next("worker-a", lease_seconds=0)
            self.assertEqual(claimed.id, task.id)  # type: ignore[union-attr]

            recovered = store.recover_expired()
            reclaimed = store.claim_next("worker-b", lease_seconds=30)

        self.assertEqual(recovered, 1)
        self.assertEqual(reclaimed.id, task.id)  # type: ignore[union-attr]
        self.assertEqual(reclaimed.lease_owner, "worker-b")  # type: ignore[union-attr]

    def test_ui_worker_lease_allows_only_one_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            self.assertTrue(store.acquire_worker("worker-a", lease_seconds=30))
            self.assertFalse(store.acquire_worker("worker-b", lease_seconds=30))
            store.release_worker("worker-a")
            self.assertTrue(store.acquire_worker("worker-b", lease_seconds=30))

    def test_expired_real_write_is_not_retried(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.create_task(
                TaskType.SEND_MESSAGE,
                {"friend": "chat-a", "text": "hello", "dry_run": False},
                "default",
            )
            store.claim_next("worker-a", lease_seconds=0)

            recovered = store.recover_expired()
            current = store.get(task.id)

        self.assertEqual(recovered, 1)
        self.assertEqual(current.status, TaskStatus.FAILED)  # type: ignore[union-attr]
        self.assertEqual(current.result["outcome"], "unknown")  # type: ignore[union-attr,index]

    def test_yielded_task_does_not_jump_a_new_ready_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            watch = store.create_task(
                TaskType.WATCH_CHATS,
                {"friends": ["chat-a"], "duration": "2s", "dry_run": True},
                "default",
            )
            store.claim_next("worker-a", lease_seconds=30)
            self.assertTrue(
                store.yield_task(
                    watch.id,
                    "worker-a",
                    watch.payload,
                    delay_seconds=0,
                )
            )
            search = store.create_task(
                TaskType.SEARCH_CONTACTS,
                {"query": "彭杏", "limit": 5},
                "default",
            )
            with store.engine.begin() as connection:
                connection.exec_driver_sql(
                    "UPDATE tasks SET available_at = '2026-01-02T00:00:00+00:00' "
                    "WHERE id = ?",
                    (watch.id,),
                )
                connection.exec_driver_sql(
                    "UPDATE tasks SET available_at = '2026-01-01T00:00:00+00:00' "
                    "WHERE id = ?",
                    (search.id,),
                )

            claimed = store.claim_next("worker-b", lease_seconds=30)

        self.assertEqual(claimed.id, search.id)  # type: ignore[union-attr]

    def test_worker_completes_a_serialized_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            api = _FakeApi()
            worker = OperationWorker(store, api)  # type: ignore[arg-type]
            task = store.create_task(
                TaskType.SCAN_MESSAGES,
                {"friend": "chat-a", "duration": "1s", "dry_run": True},
                "default",
            )
            self.assertTrue(worker.run_once())
            completed = store.get(task.id)
        self.assertIsNotNone(completed)
        self.assertEqual(completed.status, TaskStatus.SUCCEEDED)  # type: ignore[union-attr]
        self.assertEqual(completed.result["message_count"], 1)  # type: ignore[union-attr,index]


class WorkerDispatchTests(unittest.TestCase):
    def test_one_worker_instance_cannot_dispatch_concurrently(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            api = _BlockingApi()
            worker = OperationWorker(store, api)  # type: ignore[arg-type]
            first = store.create_task(
                TaskType.SEARCH_CONTACTS,
                {"query": "first", "limit": 5},
                "default",
            )
            second = store.create_task(
                TaskType.SEARCH_CONTACTS,
                {"query": "second", "limit": 5},
                "default",
            )

            with ThreadPoolExecutor(max_workers=2) as executor:
                running = executor.submit(worker.run_once)
                self.assertTrue(api.entered.wait(timeout=2))
                concurrent_result = executor.submit(worker.run_once).result(timeout=2)
                api.release.set()
                self.assertTrue(running.result(timeout=2))

            self.assertFalse(concurrent_result)
            self.assertEqual(api.call_count, 1)
            self.assertEqual(store.get(first.id).status, TaskStatus.SUCCEEDED)  # type: ignore[union-attr]
            self.assertEqual(store.get(second.id).status, TaskStatus.PENDING)  # type: ignore[union-attr]

    def test_read_failure_is_requeued_but_real_write_failure_is_not(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            worker = OperationWorker(
                store,
                _FailingApi(),  # type: ignore[arg-type]
                retry_base_seconds=5,
            )
            read_task = store.create_task(
                TaskType.SEARCH_CONTACTS,
                {"query": "彭杏", "limit": 5},
                "default",
            )
            self.assertTrue(worker.run_once())
            pending_read = store.get(read_task.id)
            self.assertEqual(pending_read.status, TaskStatus.PENDING)  # type: ignore[union-attr]
            retry_delay = datetime.fromisoformat(  # type: ignore[union-attr]
                pending_read.available_at
            ) - datetime.fromisoformat(pending_read.updated_at)  # type: ignore[union-attr]
            self.assertGreaterEqual(retry_delay.total_seconds(), 4.9)
            store.fail(read_task.id, "end test retry", retry=False)

            write_task = store.create_task(
                TaskType.SEND_MESSAGE,
                {"friend": "大月", "text": "你好", "dry_run": False},
                "default",
            )
            self.assertTrue(worker.run_once())
            current = store.get(write_task.id)

        self.assertEqual(current.status, TaskStatus.FAILED)  # type: ignore[union-attr]
        self.assertEqual(current.result["outcome"], "unknown")  # type: ignore[union-attr,index]
        self.assertTrue(current.result["retry_suppressed"])  # type: ignore[union-attr,index]

    def test_search_contacts_is_dispatched_through_worker(self) -> None:
        api = _FakeApi()
        with tempfile.TemporaryDirectory() as tmp:
            worker = OperationWorker(
                TaskStore(Path(tmp) / "tasks.db"),
                api,  # type: ignore[arg-type]
            )
            result = worker.dispatch(
                Task.new(TaskType.SEARCH_CONTACTS, {"query": "彭杏", "limit": 5})
            )

        self.assertEqual(result["action"], "search_contacts")
        self.assertEqual(result["matches"][0]["name"], "彭杏")

    def test_watch_chats_yields_between_short_slices(self) -> None:
        api = _FakeApi()
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            worker = OperationWorker(
                store,
                api,  # type: ignore[arg-type]
                watch_slice_seconds=1,
            )
            watch = store.create_task(
                TaskType.WATCH_CHATS,
                {
                    "friends": ["chat-a", "chat-b", "chat-a"],
                    "duration": "2s",
                    "dry_run": True,
                },
                "default",
            )
            search = store.create_task(
                TaskType.SEARCH_CONTACTS,
                {"query": "彭杏", "limit": 5},
                "default",
            )

            self.assertTrue(worker.run_once())
            self.assertEqual(api.scanned, ["chat-a"])
            self.assertEqual(store.get(watch.id).status, TaskStatus.PENDING)  # type: ignore[union-attr]

            self.assertTrue(worker.run_once())
            self.assertEqual(store.get(search.id).status, TaskStatus.SUCCEEDED)  # type: ignore[union-attr]

            while store.get(watch.id).status != TaskStatus.SUCCEEDED:  # type: ignore[union-attr]
                time.sleep(0.11)
                worker.run_once()
            result = store.get(watch.id).result  # type: ignore[union-attr]

        self.assertEqual(api.scanned, ["chat-a", "chat-a", "chat-b", "chat-b"])
        self.assertEqual(result["chat_count"], 2)
        self.assertEqual(result["message_count"], 4)
        self.assertNotIn("reply_count", result)

    def test_new_core_capabilities_are_dispatched_through_worker(self) -> None:
        api = _FakeApi()
        with tempfile.TemporaryDirectory() as tmp:
            worker = OperationWorker(TaskStore(Path(tmp) / "tasks.db"), api)  # type: ignore[arg-type]
            profile = worker.dispatch(Task.new(TaskType.GET_CONTACT_PROFILE, {"friend": "大月"}))
            members = worker.dispatch(Task.new(TaskType.LIST_GROUP_MEMBERS, {"group": "测试群"}))
            unread = worker.dispatch(Task.new(TaskType.GET_UNREAD_MESSAGES, {}))
            history = worker.dispatch(
                Task.new(TaskType.READ_CHAT_HISTORY, {"friend": "大月", "limit": 20})
            )
            files = worker.dispatch(
                Task.new(
                    TaskType.SEND_FILES,
                    {"friend": "文件传输助手", "files": ["a.txt"], "dry_run": True},
                )
            )

        self.assertEqual(profile["profile"]["昵称"], "大月")
        self.assertEqual(members["members"], ["甲", "乙"])
        self.assertEqual(unread["total_unread"], 2)
        self.assertEqual(history["limit"], 20)
        self.assertTrue(files["dry_run"])


if __name__ == "__main__":
    unittest.main()
