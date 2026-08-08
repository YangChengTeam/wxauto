from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from wxclient.queue import OperationWorker, Task, TaskStatus, TaskStore, TaskType


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


class TaskStoreTests(unittest.TestCase):
    def test_pending_tasks_of_same_type_are_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            first = store.create_task(
                TaskType.SEND_MESSAGE,
                {"friend": "a", "text": "one"},
                "default",
            )
            second = store.create_task(
                TaskType.SEND_MESSAGE,
                {"friend": "b", "text": "two"},
                "default",
            )
        self.assertEqual(first.id, second.id)

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

    def test_watch_chats_is_pure_monitoring(self) -> None:
        api = _FakeApi()
        with tempfile.TemporaryDirectory() as tmp:
            worker = OperationWorker(
                TaskStore(Path(tmp) / "tasks.db"),
                api,  # type: ignore[arg-type]
            )
            task = Task.new(
                TaskType.WATCH_CHATS,
                {
                    "friends": ["chat-a", "chat-b", "chat-a"],
                    "duration": "1s",
                    "dry_run": True,
                },
            )
            result = worker.dispatch(task)

        self.assertEqual(api.scanned, ["chat-a", "chat-b"])
        self.assertEqual(result["chat_count"], 2)
        self.assertEqual(result["message_count"], 2)
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
