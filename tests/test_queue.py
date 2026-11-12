from __future__ import annotations

import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime
from pathlib import Path
from sqlite3 import connect
from typing import Any
from unittest import mock

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

    def save_message_attachment(self, friend: str, target_dir: str, **kwargs: Any) -> dict[str, Any]:
        return {"action": "save_message_attachment", "friend": friend, "target_dir": target_dir, **kwargs}

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

    def test_an_unexpected_error_does_not_kill_the_only_worker_thread(self) -> None:
        """任务库被锁之类的意外：记下来、缓一缓接着跑，不能让执行线程无声消失。"""
        from unittest import mock

        from wxclient.queue import worker as worker_module

        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            api = _FakeApi()
            worker = OperationWorker(store, api, poll_interval=0.01)  # type: ignore[arg-type]
            real_claim = store.claim_next
            boom = {"left": 2}

            def flaky_claim(*args, **kwargs):
                if boom["left"]:
                    boom["left"] -= 1
                    raise RuntimeError("database is locked")
                return real_claim(*args, **kwargs)

            task = store.create_task(
                TaskType.SCAN_MESSAGES, {"friend": "chat-a", "duration": "1s", "dry_run": True}, "default",
            )
            with mock.patch.object(store, "claim_next", flaky_claim), \
                    mock.patch.object(worker_module, "LOOP_ERROR_BASE_SECONDS", 0.01):
                worker.start()
                try:
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        current = store.get(task.id)
                        if current is not None and current.status == TaskStatus.SUCCEEDED:
                            break
                        time.sleep(0.02)
                    self.assertTrue(worker.running, "出过两次意外之后线程还活着")
                    self.assertEqual(store.get(task.id).status, TaskStatus.SUCCEEDED)  # type: ignore[union-attr]
                    self.assertEqual(boom["left"], 0)
                finally:
                    worker.stop()

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


class _HeartbeatWatchingStore(TaskStore):
    """记下每次写终态时租约心跳线程是否还活着。"""

    def __init__(self, database_path: Path) -> None:
        super().__init__(database_path)
        self.heartbeat_alive_at_settle: list[bool] = []

    def _note(self) -> None:
        alive = any(t.name == "wx-lease-heartbeat" and t.is_alive() for t in threading.enumerate())
        self.heartbeat_alive_at_settle.append(alive)

    def complete(self, task_id: str, result: dict[str, Any] | None = None, owner_id: str | None = None) -> bool:
        self._note()
        return super().complete(task_id, result, owner_id)

    def fail(self, task_id: str, error: str, retry: bool = False, owner_id: str | None = None,
             result: dict[str, Any] | None = None, retry_delay_seconds: float = 0.0) -> bool:
        self._note()
        return super().fail(task_id, error, retry, owner_id, result, retry_delay_seconds)


class WorkerLeaseHeartbeatTests(unittest.TestCase):
    """★ 2026-09-21 假警报：终态先写、心跳后停，心跳撞进窗口就记「任务租约丢」。"""

    def test_heartbeat_is_stopped_before_the_terminal_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = _HeartbeatWatchingStore(Path(tmp) / "tasks.db")
            worker = OperationWorker(store, _FakeApi())  # type: ignore[arg-type]
            task = store.create_task(TaskType.SEARCH_CONTACTS, {"query": "a"}, "default")

            self.assertTrue(worker.run_once())

            self.assertEqual(store.heartbeat_alive_at_settle, [False])
            self.assertEqual(store.get(task.id).status, TaskStatus.SUCCEEDED)  # type: ignore[union-attr]

    def test_heartbeat_is_stopped_before_a_failure_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = _HeartbeatWatchingStore(Path(tmp) / "tasks.db")
            worker = OperationWorker(store, _FailingApi())  # type: ignore[arg-type]
            task = store.create_task(TaskType.SEARCH_CONTACTS, {"query": "a"}, "default")

            self.assertTrue(worker.run_once())

            self.assertEqual(store.heartbeat_alive_at_settle, [False])
            self.assertEqual(store.get(task.id).status, TaskStatus.PENDING)  # type: ignore[union-attr]

    def test_a_tick_that_lands_after_stop_does_not_cry_lease_lost(self) -> None:
        """join 超时的兜底：续租失败但 stop 已置位（终态可能刚写下去）就不记 lease_lost。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            worker = OperationWorker(store, _FakeApi(), lease_seconds=1.0)  # type: ignore[arg-type]
            stop = threading.Event()

            def renew_task(task_id: str, owner_id: str, lease_seconds: float) -> bool:
                stop.set()  # 续租进行中，主线程写完终态并叫停了心跳
                return False

            with mock.patch.object(store, "renew_task", side_effect=renew_task), \
                    mock.patch("wxclient.queue.worker.logger") as fake_logger:
                worker._renew_leases("task-1", stop)

            fake_logger.error.assert_not_called()

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

    def test_contact_not_found_is_a_definite_not_sent_failure(self) -> None:
        """「查无此人」是搜联系人那一步就失败：确定没发出去，不是「结果未知」，也不自动重试（2026-09-24）。"""

        class NoSuchFriendError(Exception):  # 同名即认：vendor 那个类不在这里 import
            pass

        class _NoSuchFriendApi(_FakeApi):
            def send_message(self, friend: str, text: str, dry_run: bool | None) -> dict[str, Any]:
                raise NoSuchFriendError("好友或群聊备注有误！查无此人！")

            def send_files(self, friend: str, files: list[str], text: str = "", dry_run: bool | None = None) -> dict[str, Any]:
                try:
                    raise NoSuchFriendError("好友或群聊备注有误！查无此人！")
                except NoSuchFriendError as exc:
                    raise WeChatApiError(f"发送文件失败：{exc}") from exc

        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            worker = OperationWorker(store, _NoSuchFriendApi())  # type: ignore[arg-type]
            message = store.create_task(
                TaskType.SEND_MESSAGE, {"friend": "张三", "text": "你好", "dry_run": False}, "default",
            )
            self.assertTrue(worker.run_once())
            files = store.create_task(
                TaskType.SEND_FILES,
                {"friend": "李四", "files": [str(Path(tmp) / "a.png")], "dry_run": False},
                "default",
            )
            self.assertTrue(worker.run_once())
            sent, filed = store.get(message.id), store.get(files.id)

        for task, name in ((sent, "张三"), (filed, "李四")):
            self.assertEqual(task.status, TaskStatus.FAILED)  # type: ignore[union-attr]
            self.assertEqual(task.attempts, 1)  # type: ignore[union-attr]
            self.assertEqual(task.result["outcome"], "not_sent")  # type: ignore[union-attr,index]
            self.assertEqual(task.result["reason_code"], "CONTACT_NOT_FOUND")  # type: ignore[union-attr,index]
            self.assertIn(f"「{name}」", task.last_error)  # type: ignore[union-attr,operator]
            self.assertNotIn("结果未知", task.last_error)  # type: ignore[union-attr,operator]

    def test_contact_not_found_on_a_read_is_not_retried_and_carries_the_code(self) -> None:
        """读消息也「查无此人」：不重试（名字对不上），带 reason_code 给网关去数连击。"""

        class NoSuchFriendError(Exception):
            pass

        class _Api(_FakeApi):
            def search_contacts(self, query: str, limit: int) -> dict[str, Any]:
                raise NoSuchFriendError("好友或群聊备注有误！查无此人！")

        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            worker = OperationWorker(store, _Api())  # type: ignore[arg-type]
            task = store.create_task(TaskType.SEARCH_CONTACTS, {"query": "张三", "limit": 5}, "default")
            self.assertTrue(worker.run_once())
            current = store.get(task.id)

        self.assertEqual(current.status, TaskStatus.FAILED)  # type: ignore[union-attr]
        self.assertEqual(current.result["reason_code"], "CONTACT_NOT_FOUND")  # type: ignore[union-attr,index]
        self.assertEqual(current.result["outcome"], "not_found")  # type: ignore[union-attr,index]
        self.assertIn("没读到消息", current.last_error)  # type: ignore[union-attr,operator]

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
            saved = worker.dispatch(
                Task.new(TaskType.SAVE_MESSAGE_ATTACHMENT,
                         {"friend": "大月", "target_dir": tmp, "runtime_id": "r1", "order": 2, "text": "图片"})
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
        self.assertEqual((saved["runtime_id"], saved["order"], saved["text"]), ("r1", 2, "图片"))
        self.assertTrue(files["dry_run"])


if __name__ == "__main__":
    unittest.main()
