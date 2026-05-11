from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from wxclient.agentbridge import AgentBridge
from wxclient.agentbridge.reply_flow import (
    _display_sender,
    _log_chat_messages,
    _message_fingerprint,
    reply_to_chat_messages,
    reply_to_new_chat_messages,
    reply_to_unread_messages,
    watch_chats_messages,
)
from wxclient.agents import AgentConfigError
from wxclient.agents.factory import build_chat_agent
from wxclient.agents.openai_agents import normalize_openai_base_url
from wxclient.agents.openai_agents import _build_http_client
from wxclient.app.cli import app
from wxclient.config import Settings
from wxclient.queue import OperationWorker, TaskStatus, TaskStore, TaskType
from wxclient.trigger.http_server import create_app
from wxclient.ui.config import load_gui_settings, save_gui_settings
from wxclient.ui.task_actions import enqueue_reply_unread, enqueue_watch_chats
from wxclient.uia_unlock.service import _is_wechat_window_candidate
from wxclient.wechat_api import WeChatApi
from wxclient.wechat_api.service import (
    WeChatApiError,
    _control_matches_criteria,
    _infer_message_is_self,
    _infer_message_is_self_from_row_image,
    _resolve_control_by_criteria,
    _split_selected_group_message_text,
    _tail_messages,
)


class FakeAgent:
    def __init__(self, reply_text: str) -> None:
        self.reply_text = reply_text

    def reply(self, message: str, context: list[str] | None = None) -> str:
        return self.reply_text


class EchoAgent:
    def reply(self, message: str, context: list[str] | None = None) -> str:
        return f"reply:{message}"


class ContextRecordingAgent:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str] | None]] = []

    def reply(self, message: str, context: list[str] | None = None) -> str:
        self.calls.append((message, context))
        return "contextual-reply"


class FakeChatApi:
    def __init__(
        self,
        messages: list[dict[str, str]],
        unread_messages: dict[str, list[dict[str, str]]] | None = None,
        append_sent_to_messages: bool = False,
        current_chat: str | None = None,
        current_messages: list[dict[str, str]] | None = None,
        visible_messages: list[dict[str, str]] | None = None,
        session_summaries: list[dict[str, object]] | None = None,
    ) -> None:
        self.messages = messages
        self.visible_messages = visible_messages if visible_messages is not None else messages
        self.unread_messages = unread_messages or {}
        self.append_sent_to_messages = append_sent_to_messages
        self.current_chat = current_chat
        self.current_messages = current_messages if current_messages is not None else messages
        self.session_summaries = session_summaries or []
        self.sent: list[dict[str, object]] = []
        self.current_sent: list[dict[str, object]] = []
        self.scanned: list[str] = []
        self.visible_reads: list[str] = []
        self.scan_separate_windows: list[bool] = []
        self.scan_select_senders: list[bool] = []
        self.read_separate_windows: list[bool] = []

    def check_unread_messages(self, dry_run: bool | None = None) -> dict[str, object]:
        return {
            "dry_run": dry_run,
            "action": "check_unread_messages",
            "messages_by_chat": self.unread_messages,
        }

    def read_chat_sessions(self, dry_run: bool | None = None) -> dict[str, object]:
        return {
            "dry_run": dry_run,
            "action": "read_chat_sessions",
            "sessions": self.session_summaries,
            "selected_session": self.session_summaries[0] if self.session_summaries else None,
        }

    def scan_messages(
        self,
        friend: str,
        duration: str = "30s",
        select_sender: bool = False,
        save_file: bool = False,
        save_media: bool = False,
        separate_window: bool = True,
        dry_run: bool | None = None,
    ) -> dict[str, object]:
        self.scanned.append(friend)
        self.scan_separate_windows.append(separate_window)
        self.scan_select_senders.append(select_sender)
        return {
            "dry_run": dry_run,
            "action": "scan_messages",
            "friend": friend,
            "duration": duration,
            "separate_window": separate_window,
            "window_mode": "separate" if separate_window else "main",
            "result": {"messages": self.messages},
        }

    def send_message(
        self,
        friend: str,
        text: str,
        dry_run: bool | None = None,
    ) -> dict[str, object]:
        result = {"friend": friend, "text": text, "dry_run": dry_run}
        self.sent.append(result)
        if self.append_sent_to_messages:
            self.messages.append({"sender": "self", "text": text})
        return result

    def send_current_chat_message(
        self,
        text: str,
        dry_run: bool | None = None,
    ) -> dict[str, object]:
        result = {
            "chat": self.current_chat,
            "text": text,
            "dry_run": dry_run,
            "action": "send_current_chat_message",
        }
        self.current_sent.append(result)
        if self.append_sent_to_messages:
            self.current_messages.append({"sender": "self", "text": text, "is_self": True})
        return result

    def read_visible_messages(
        self,
        friend: str,
        separate_window: bool = True,
        dry_run: bool | None = None,
    ) -> dict[str, object]:
        self.visible_reads.append(friend)
        self.read_separate_windows.append(separate_window)
        return {
            "dry_run": dry_run,
            "action": "read_visible_messages",
            "friend": friend,
            "separate_window": separate_window,
            "window_mode": "separate" if separate_window else "main",
            "messages": self.visible_messages,
        }

    def read_current_chat(self, dry_run: bool | None = None) -> dict[str, object]:
        latest = self.current_messages[-1] if self.current_messages else None
        return {
            "dry_run": dry_run,
            "action": "read_current_chat",
            "chat": self.current_chat,
            "messages": self.current_messages if self.current_chat else [],
            "latest_message": latest if self.current_chat else None,
            "latest_is_self": latest.get("is_self") if latest and self.current_chat else None,
        }


class InMemoryCursorStore:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str], dict[str, object]] = {}

    def get_message_cursor(
        self,
        account_id: str,
        chat_name: str,
        source: str = "visible",
    ) -> dict[str, object] | None:
        return self.rows.get((account_id, chat_name, source))

    def set_message_cursor(
        self,
        account_id: str,
        chat_name: str,
        cursor: dict[str, object],
        source: str = "visible",
    ) -> None:
        self.rows[(account_id, chat_name, source)] = cursor


class FakePoint:
    def __init__(self, x: int, y: int) -> None:
        self.x = x
        self.y = y


class FakeRect:
    def __init__(self, left: int, top: int, right: int, bottom: int) -> None:
        self.left = left
        self.top = top
        self.right = right
        self.bottom = bottom

    def width(self) -> int:
        return self.right - self.left

    def mid_point(self) -> FakePoint:
        return FakePoint((self.left + self.right) // 2, (self.top + self.bottom) // 2)


class FakeElementInfo:
    def __init__(
        self,
        automation_id: str = "",
        framework_id: str = "Qt",
        control_type: str = "",
    ) -> None:
        self.automation_id = automation_id
        self.framework_id = framework_id
        self.control_type = control_type


class FakeUiaNode:
    def __init__(
        self,
        class_name: str,
        control_type: str,
        text: str,
        rect: FakeRect | None = None,
        automation_id: str = "",
    ) -> None:
        self._class_name = class_name
        self._control_type = control_type
        self._text = text
        self._rect = rect
        self.element_info = FakeElementInfo(automation_id=automation_id, control_type=control_type)

    def class_name(self) -> str:
        return self._class_name

    def friendly_class_name(self) -> str:
        return self._control_type

    def window_text(self) -> str:
        return self._text

    def rectangle(self) -> FakeRect:
        if self._rect is None:
            raise RuntimeError("no rectangle")
        return self._rect

    def is_visible(self) -> bool:
        return True

    def is_enabled(self) -> bool:
        return True


class FakeUiaWindow:
    def __init__(self, nodes: list[FakeUiaNode]) -> None:
        self.nodes = nodes

    def descendants(self) -> list[FakeUiaNode]:
        return self.nodes


class FakeDialogWindow(FakeUiaWindow):
    def __init__(self, title: str, nodes: list[FakeUiaNode] | None = None) -> None:
        super().__init__(nodes or [])
        self.title = title
        self.closed = False

    def class_name(self) -> str:
        return "mmui::ChatSingleWindow"

    def window_text(self) -> str:
        return self.title

    def close(self) -> None:
        self.closed = True


class TestableWeChatApi(WeChatApi):
    def _resolve_chat_list(self, window):  # type: ignore[no-untyped-def]
        return None


class CurrentChatFallbackWeChatApi(TestableWeChatApi):
    def __init__(self, window: FakeUiaWindow) -> None:
        super().__init__(dry_run=False)
        self.window = window

    def _ensure_ready(self) -> None:
        return None

    def _current_main_window(self):  # type: ignore[no-untyped-def]
        return self.window

    def _current_chat_name(self, window):  # type: ignore[no-untyped-def]
        return None

    def _selected_session_name(self, window):  # type: ignore[no-untyped-def]
        return None

    def _latest_message_self_by_context_menu(self, window):  # type: ignore[no-untyped-def]
        return False


class FakeUnreadNavigator:
    def __init__(self) -> None:
        self.opened_with_select: list[bool] = []

    def open_weixin(self, is_maximize: bool = False):  # type: ignore[no-untyped-def]
        return object()

    def open_dialog_window(self, friend: str, select: bool, **kwargs):  # type: ignore[no-untyped-def]
        self.opened_with_select.append(select)
        return friend

    def open_seperate_dialog_window(self, friend: str, select: bool, **kwargs):  # type: ignore[no-untyped-def]
        self.opened_with_select.append(select)
        return friend


class FakeUnreadUtils:
    def scan_for_new_messages(self, **kwargs):  # type: ignore[no-untyped-def]
        return {"Mzxx": 2}


class FakeUnreadWeChatApi(WeChatApi):
    def __init__(self) -> None:
        super().__init__(dry_run=False)
        self.navigator = FakeUnreadNavigator()
        self.utils = FakeUnreadUtils()

    def _ensure_ready(self) -> None:
        return None

    def _pyweixin_class(self, name: str):  # type: ignore[no-untyped-def]
        if name == "Navigator":
            return self.navigator
        return super()._pyweixin_class(name)

    def _pyweixin_submodule(self, name: str):  # type: ignore[no-untyped-def]
        if name == "utils":
            return self.utils
        return super()._pyweixin_submodule(name)

    def _collect_visible_message_nodes(self, dialog_window, *args, **kwargs):  # type: ignore[no-untyped-def]
        return [
            {"text": "old", "class_name": "mmui::ChatTextItemView"},
            {"text": "first unread", "class_name": "mmui::ChatTextItemView"},
            {"text": "second unread", "class_name": "mmui::ChatTextItemView"},
        ]


class FakeNavigator:
    def __init__(self, dialog: FakeDialogWindow) -> None:
        self.dialog = dialog
        self.open_count = 0

    def open_seperate_dialog_window(self, **kwargs):  # type: ignore[no-untyped-def]
        self.open_count += 1
        return self.dialog


class ReuseDialogWeChatApi(WeChatApi):
    def __init__(self, existing_dialog: FakeDialogWindow | None) -> None:
        super().__init__(dry_run=False)
        self.existing_dialog = existing_dialog
        self.opened_dialog = FakeDialogWindow("Mzxx")
        self.navigator = FakeNavigator(self.opened_dialog)

    def _ensure_ready(self) -> None:
        return None

    def _find_separate_dialog_window(self, friend: str):  # type: ignore[no-untyped-def]
        return self.existing_dialog

    def _try_minimize_window(self, dialog_window):  # type: ignore[no-untyped-def]
        return None

    def _pyweixin_class(self, name: str):  # type: ignore[no-untyped-def]
        if name == "Navigator":
            return self.navigator
        return super()._pyweixin_class(name)

    def _poll_visible_messages(self, dialog_window, duration: str, *args, **kwargs):  # type: ignore[no-untyped-def]
        return {
            "new_message_count": 1,
            "messages": [{"sender": "Mzxx", "text": "hello", "is_self": False}],
            "initial_visible_count": 0,
        }


class CoreTests(unittest.TestCase):
    def test_store_creates_and_lists_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "test.db")
            task = store.create_task(
                TaskType.SEND_MESSAGE,
                {"friend": "filehelper", "text": "hello", "dry_run": True},
                "default",
            )

            loaded = store.get(task.id)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.type, TaskType.SEND_MESSAGE)
            self.assertEqual(store.list_tasks()[0].id, task.id)

    def test_store_filters_and_clears_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "test.db")
            pending = store.create_task(
                TaskType.SEND_MESSAGE,
                {"friend": "filehelper", "text": "hello", "dry_run": True},
                "default",
            )
            blocked = store.create_task(
                TaskType.ADD_FRIEND,
                {"number": "13800138000", "greetings": "你好", "dry_run": True},
                "default",
            )
            store.complete(pending.id, {"action": "send_message"})
            store.block(blocked.id, "UIA not ready")

            visible = store.list_tasks(statuses={TaskStatus.PENDING, TaskStatus.BLOCKED})
            self.assertEqual([task.id for task in visible], [blocked.id])

            removed = store.delete_tasks(statuses={TaskStatus.BLOCKED})
            self.assertEqual(removed, 1)
            self.assertIsNone(store.get(blocked.id))

    def test_worker_executes_dry_run_send_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "test.db")
            task = store.create_task(
                TaskType.SEND_MESSAGE,
                {"friend": "filehelper", "text": "hello", "dry_run": True},
                "default",
            )
            worker = OperationWorker(store, WeChatApi(dry_run=True))

            self.assertTrue(worker.run_once())
            loaded = store.get(task.id)
            self.assertEqual(loaded.status, TaskStatus.SUCCEEDED)
            self.assertEqual(loaded.result["action"], "send_message")
            self.assertTrue(loaded.result["dry_run"])

    def test_scan_messages_reuses_existing_separate_window(self) -> None:
        existing = FakeDialogWindow("Mzxx")
        api = ReuseDialogWeChatApi(existing)

        result = api.scan_messages(friend="Mzxx", duration="1s", dry_run=False)

        self.assertTrue(result["window_reused"])
        self.assertEqual(api.navigator.open_count, 0)
        self.assertFalse(existing.closed)
        self.assertEqual(result["result"]["new_message_count"], 1)

    def test_worker_can_restart_after_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "test.db")
            worker = OperationWorker(store, WeChatApi(dry_run=True), poll_interval=0.01)

            worker.start()
            worker.stop()
            worker.start()

            self.assertFalse(worker._stop.is_set())
            worker.stop()

    def test_agentbridge_enqueues_reply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "test.db")
            reply = AgentBridge(store, agent=FakeAgent("pong")).handle_message(
                "default",
                "filehelper",
                "ping",
            )

            self.assertEqual(reply, "pong")
            tasks = store.list_tasks()
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0].type, TaskType.SEND_MESSAGE)
            self.assertEqual(tasks[0].payload["text"], "pong")
            self.assertTrue(tasks[0].payload["dry_run"])

    def test_agentbridge_skips_empty_reply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "test.db")
            reply = AgentBridge(store, agent=FakeAgent("  ")).handle_message(
                "default",
                "filehelper",
                "ping",
            )

            self.assertEqual(reply, "")
            self.assertEqual(store.list_tasks(), [])

    def test_reply_chat_replies_to_last_new_message(self) -> None:
        api = FakeChatApi(
            [
                {"text": "first"},
                {"text": "last"},
            ]
        )

        result = reply_to_new_chat_messages(
            api=api,
            agent=EchoAgent(),
            friend="filehelper",
            duration="1s",
            real=True,
        )

        self.assertEqual(result["new_message_count"], 2)
        self.assertEqual(result["reply_count"], 1)
        self.assertEqual(api.sent[0]["text"], "reply:last")
        self.assertFalse(api.sent[0]["dry_run"])

    def test_reply_chat_reply_all(self) -> None:
        api = FakeChatApi(
            [
                {"text": "first"},
                {"text": "last"},
            ]
        )

        result = reply_to_new_chat_messages(
            api=api,
            agent=EchoAgent(),
            friend="filehelper",
            duration="1s",
            real=True,
            reply_all=True,
        )

        self.assertEqual(result["reply_count"], 2)
        self.assertEqual([item["text"] for item in api.sent], ["reply:first", "reply:last"])

    def test_reply_chat_no_new_messages_does_not_send(self) -> None:
        api = FakeChatApi([])

        result = reply_to_new_chat_messages(
            api=api,
            agent=EchoAgent(),
            friend="filehelper",
            duration="1s",
            real=True,
        )

        self.assertEqual(result["new_message_count"], 0)
        self.assertEqual(result["reply_count"], 0)
        self.assertEqual(api.sent, [])

    def test_reply_chat_latest_visible_replies_to_last_visible_message(self) -> None:
        api = FakeChatApi(
            [
                {"text": "old"},
                {"text": "visible-last"},
            ]
        )

        result = reply_to_chat_messages(
            api=api,
            agent=EchoAgent(),
            friend="filehelper",
            duration="1s",
            real=True,
            source="visible",
        )

        self.assertEqual(result["message_source"], "visible")
        self.assertEqual(result["visible_message_count"], 2)
        self.assertEqual(result["reply_count"], 1)
        self.assertEqual(api.sent[0]["text"], "reply:visible-last")

    def test_reply_chat_passes_recent_visible_messages_as_context(self) -> None:
        api = FakeChatApi(
            [
                {"sender": "Mzxx", "text": "old-1"},
                {"sender": "self", "text": "old-2"},
                {"sender": "Mzxx", "text": "latest"},
            ]
        )
        agent = ContextRecordingAgent()

        result = reply_to_chat_messages(
            api=api,
            agent=agent,
            friend="Mzxx",
            duration="1s",
            real=True,
            source="visible",
            context_message_limit=2,
        )

        self.assertEqual(result["reply_count"], 1)
        self.assertEqual(agent.calls[0][0], "latest")
        self.assertEqual(agent.calls[0][1], ["对方: old-1", "我: old-2"])
        self.assertEqual(result["replies"][0]["context_count"], 2)

    def test_watch_chats_auto_replies_to_multiple_targets(self) -> None:
        api = FakeChatApi([{"sender": "Mzxx", "text": "hello", "is_self": False}])

        result = watch_chats_messages(
            api=api,
            agent=EchoAgent(),
            friends=["Mzxx", "测试群"],
            duration="1s",
            real=True,
            auto_reply=True,
        )

        self.assertEqual(result["action"], "watch_chats")
        self.assertEqual(result["chat_count"], 2)
        self.assertEqual(result["reply_count"], 2)
        self.assertEqual(api.scanned, ["Mzxx", "测试群"])
        self.assertEqual(api.visible_reads, ["Mzxx", "测试群"])
        self.assertEqual(api.scan_separate_windows, [True, True])
        self.assertEqual(api.read_separate_windows, [True, True])
        self.assertEqual([item["friend"] for item in api.sent], ["Mzxx", "测试群"])

    def test_watch_chats_can_listen_without_reply(self) -> None:
        api = FakeChatApi([{"sender": "Mzxx", "text": "hello", "is_self": False}])

        result = watch_chats_messages(
            api=api,
            agent=EchoAgent(),
            friends=["Mzxx"],
            duration="1s",
            real=True,
            auto_reply=False,
        )

        self.assertEqual(result["message_count"], 1)
        self.assertEqual(result["visible_message_count"], 1)
        self.assertEqual(result["reply_count"], 0)
        self.assertEqual(api.scanned, ["Mzxx"])
        self.assertEqual(api.visible_reads, ["Mzxx"])
        self.assertEqual(api.scan_separate_windows, [True])
        self.assertEqual(api.read_separate_windows, [True])
        self.assertEqual(api.sent, [])

    def test_watch_chats_skips_separate_window_when_session_preview_marks_self(self) -> None:
        api = FakeChatApi(
            [{"sender": "Other", "text": "hello", "is_self": False}],
            session_summaries=[
                {
                    "name": "Mzxx",
                    "preview": "Me: already answered",
                    "timestamp_text": "10:32",
                    "timestamp_is_today": True,
                    "is_selected": True,
                },
            ],
        )

        result = watch_chats_messages(
            api=api,
            agent=EchoAgent(),
            friends=["Mzxx"],
            duration="1s",
            real=True,
            auto_reply=True,
        )

        self.assertEqual(result["reply_count"], 0)
        self.assertEqual(api.scanned, [])
        self.assertEqual(api.visible_reads, [])
        self.assertEqual(result["results"][0]["fallback_window_opened"], False)
        self.assertEqual(result["results"][0]["latest_message_source"], "session_preview")
        self.assertEqual(result["results"][0]["decision_reason"], "latest_is_self_preview")
        self.assertTrue(result["results"][0]["latest_is_today"])
        self.assertEqual(result["results"][0]["timestamp_text"], "10:32")

    def test_watch_chats_skips_separate_window_when_session_summary_is_not_today(self) -> None:
        api = FakeChatApi(
            [{"sender": "Other", "text": "hello", "is_self": False}],
            session_summaries=[
                {
                    "name": "Mzxx",
                    "preview": "Other: old message",
                    "timestamp_text": "昨天 10:32",
                    "timestamp_is_today": False,
                    "is_selected": True,
                },
            ],
        )

        result = watch_chats_messages(
            api=api,
            agent=EchoAgent(),
            friends=["Mzxx"],
            duration="1s",
            real=True,
            auto_reply=True,
        )

        self.assertEqual(result["reply_count"], 0)
        self.assertEqual(api.scanned, [])
        self.assertEqual(api.visible_reads, [])
        self.assertFalse(result["results"][0]["fallback_window_opened"])
        self.assertEqual(result["results"][0]["decision_reason"], "not_today")
        self.assertFalse(result["results"][0]["latest_is_today"])
        self.assertEqual(result["results"][0]["timestamp_text"], "昨天 10:32")

    def test_watch_chats_uses_scan_result_instead_of_seeding_visible_history(self) -> None:
        api = FakeChatApi([{"sender": "Mzxx", "text": "hello", "is_self": False}])
        cursors = InMemoryCursorStore()

        result = watch_chats_messages(
            api=api,
            agent=EchoAgent(),
            friends=["Mzxx"],
            duration="1s",
            real=True,
            auto_reply=True,
            cursor_store=cursors,
        )

        self.assertEqual(result["visible_message_count"], 1)
        self.assertEqual(result["message_count"], 1)
        self.assertEqual(result["cursor_seed_count"], 0)
        self.assertEqual(result["reply_count"], 1)
        self.assertIn("hello", str(api.sent[0]["text"]))

    def test_watch_chats_replies_to_latest_visible_when_scan_has_no_new_nodes(self) -> None:
        api = FakeChatApi(
            [],
            visible_messages=[
                {"sender": "Mzxx", "text": "old", "is_self": False},
                {"sender": "Mzxx", "text": "visible-last", "is_self": False},
            ],
        )
        cursors = InMemoryCursorStore()

        result = watch_chats_messages(
            api=api,
            agent=EchoAgent(),
            friends=["Mzxx"],
            duration="1s",
            real=True,
            auto_reply=True,
            cursor_store=cursors,
        )

        self.assertEqual(result["message_count"], 0)
        self.assertEqual(result["reply_count"], 1)
        self.assertIn("visible-last", str(api.sent[0]["text"]))

    def test_watch_chats_replies_to_latest_visible_when_cursor_already_has_it(self) -> None:
        latest = {"sender": "Mzxx", "text": "visible-last", "is_self": False}
        api = FakeChatApi([], visible_messages=[latest])
        cursors = InMemoryCursorStore()
        cursors.set_message_cursor(
            "default",
            "Mzxx",
            {"tail": [_message_fingerprint(latest)]},
            "watch",
        )

        result = watch_chats_messages(
            api=api,
            agent=EchoAgent(),
            friends=["Mzxx"],
            duration="1s",
            real=True,
            auto_reply=True,
            cursor_store=cursors,
        )

        self.assertEqual(result["message_count"], 1)
        self.assertEqual(result["new_message_count"], 1)
        self.assertEqual(result["reply_count"], 1)
        self.assertEqual(api.visible_reads, ["Mzxx"])
        self.assertIn("visible-last", str(api.sent[0]["text"]))

    def test_watch_chats_skips_new_self_messages(self) -> None:
        api = FakeChatApi([{"sender": "Mzxx", "text": "hello", "is_self": False}])
        cursors = InMemoryCursorStore()

        watch_chats_messages(
            api=api,
            agent=EchoAgent(),
            friends=["Mzxx"],
            duration="1s",
            real=True,
            auto_reply=True,
            cursor_store=cursors,
        )
        api.messages = [{"sender": "me", "text": "mine", "is_self": True}]
        api.visible_messages = [{"sender": "me", "text": "mine", "is_self": True}]
        api.sent.clear()

        result = watch_chats_messages(
            api=api,
            agent=EchoAgent(),
            friends=["Mzxx"],
            duration="1s",
            real=True,
            auto_reply=True,
            cursor_store=cursors,
        )

        self.assertEqual(result["message_count"], 1)
        self.assertEqual(result["reply_count"], 0)
        self.assertEqual(api.sent, [])

    def test_reply_chat_skips_when_latest_message_is_self_even_with_prior_inbound(self) -> None:
        api = FakeChatApi(
            [
                {"sender": "Mzxx", "text": "question", "is_self": False},
                {"sender": "我", "text": "already answered", "is_self": True},
            ]
        )

        result = reply_to_chat_messages(
            api=api,
            agent=EchoAgent(),
            friend="Mzxx",
            duration="1s",
            real=True,
        )

        self.assertEqual(result["new_message_count"], 2)
        self.assertEqual(result["reply_count"], 0)
        self.assertEqual(api.sent, [])

    def test_reply_chat_skips_self_messages(self) -> None:
        api = FakeChatApi([{"sender": "我", "text": "mine", "is_self": True}])

        result = reply_to_chat_messages(
            api=api,
            agent=EchoAgent(),
            friend="Mzxx",
            duration="1s",
            real=True,
        )

        self.assertEqual(result["new_message_count"], 1)
        self.assertEqual(result["reply_count"], 0)
        self.assertEqual(api.sent, [])

    def test_reply_unread_replies_to_last_message_per_chat(self) -> None:
        api = FakeChatApi(
            [],
            unread_messages={
                "Mzxx": [
                    {
                        "\u6d88\u606f\u5185\u5bb9": "first",
                        "\u6d88\u606f\u53d1\u9001\u4eba": "Mzxx",
                    },
                    {
                        "\u6d88\u606f\u5185\u5bb9": "last",
                        "\u6d88\u606f\u53d1\u9001\u4eba": "Mzxx",
                    },
                ]
            },
        )

        result = reply_to_unread_messages(api=api, agent=EchoAgent(), real=True)

        self.assertEqual(result["chat_count"], 1)
        self.assertEqual(result["message_count"], 2)
        self.assertEqual(result["reply_count"], 1)
        self.assertEqual(api.sent[0]["friend"], "Mzxx")
        self.assertIn("last", str(api.sent[0]["text"]))
        self.assertFalse(api.sent[0]["dry_run"])

    def test_reply_unread_passes_unread_history_as_context(self) -> None:
        api = FakeChatApi(
            [],
            unread_messages={
                "Mzxx": [
                    {"sender": "Mzxx", "text": "old"},
                    {"sender": "Mzxx", "text": "latest"},
                ]
            },
        )
        agent = ContextRecordingAgent()

        result = reply_to_unread_messages(
            api=api,
            agent=agent,
            real=True,
            context_message_limit=5,
        )

        self.assertEqual(result["reply_count"], 1)
        self.assertIn("latest", agent.calls[0][0])
        self.assertEqual(agent.calls[0][1], ["对方: old"])
        self.assertEqual(result["replies"][0]["context_count"], 1)

    def test_reply_unread_replies_current_chat_before_unread_scan(self) -> None:
        api = FakeChatApi(
            [],
            unread_messages={"Other": [{"sender": "Other", "text": "unread"}]},
            current_chat="Mzxx",
            current_messages=[
                {"sender": "self", "text": "old", "is_self": True},
                {
                    "sender": "Mzxx",
                    "text": "current-new",
                    "is_self": False,
                    "timestamp_is_today": True,
                },
            ],
        )

        result = reply_to_unread_messages(api=api, agent=EchoAgent(), real=True)

        self.assertTrue(result["current_chat_checked"])
        self.assertTrue(result["current_chat_replied"])
        self.assertEqual(api.current_sent[0]["chat"], "Mzxx")
        self.assertIn("current-new", str(api.current_sent[0]["text"]))
        self.assertEqual(api.sent[0]["friend"], "Other")

    def test_reply_unread_skips_current_chat_when_latest_is_not_today(self) -> None:
        api = FakeChatApi(
            [],
            unread_messages={},
            current_chat="Mzxx",
            current_messages=[
                {
                    "sender": "Mzxx",
                    "text": "old-message",
                    "is_self": False,
                    "timestamp_is_today": False,
                },
            ],
        )

        result = reply_to_unread_messages(api=api, agent=EchoAgent(), real=True)

        self.assertTrue(result["current_chat_checked"])
        self.assertFalse(result["current_chat_replied"])
        self.assertFalse(result["current_chat_latest_is_today"])
        self.assertEqual(result["reply_count"], 0)
        self.assertEqual(api.current_sent, [])

    def test_reply_unread_replies_current_chat_without_timestamp_when_not_self(self) -> None:
        api = FakeChatApi(
            [],
            unread_messages={},
            current_chat="Mzxx",
            current_messages=[
                {"sender": "Mzxx", "text": "unknown-time", "is_self": False},
            ],
        )

        result = reply_to_unread_messages(api=api, agent=EchoAgent(), real=True)

        self.assertTrue(result["current_chat_checked"])
        self.assertTrue(result["current_chat_replied"])
        self.assertIsNone(result["current_chat_latest_is_today"])
        self.assertEqual(result["reply_count"], 1)
        self.assertIn("unknown-time", str(api.current_sent[0]["text"]))

    def test_reply_unread_current_chat_cursor_does_not_block_latest_not_self(self) -> None:
        api = FakeChatApi(
            [],
            unread_messages={},
            current_chat="Mzxx",
            current_messages=[
                {
                    "sender": "Mzxx",
                    "text": "current-new",
                    "is_self": False,
                    "timestamp_is_today": True,
                },
            ],
        )
        cursors = InMemoryCursorStore()
        cursors.set_message_cursor(
            "default",
            "Mzxx",
            {"tail": [], "count": 0},
            "visible",
        )

        result = reply_to_unread_messages(
            api=api,
            agent=EchoAgent(),
            real=True,
            cursor_store=cursors,
            account_id="default",
        )

        self.assertTrue(result["current_chat_replied"])
        self.assertIn("current-new", str(api.current_sent[0]["text"]))

    def test_reply_unread_skips_current_chat_when_latest_is_self(self) -> None:
        api = FakeChatApi(
            [],
            unread_messages={},
            current_chat="Mzxx",
            current_messages=[
                {"sender": "Mzxx", "text": "old", "is_self": False},
                {
                    "sender": "self",
                    "text": "already-replied",
                    "is_self": True,
                    "timestamp_is_today": True,
                },
            ],
        )

        result = reply_to_unread_messages(api=api, agent=EchoAgent(), real=True)

        self.assertTrue(result["current_chat_checked"])
        self.assertFalse(result["current_chat_replied"])
        self.assertEqual(result["reply_count"], 0)
        self.assertEqual(api.current_sent, [])

    def test_reply_unread_skips_current_chat_when_latest_matches_recent_outbound(self) -> None:
        api = FakeChatApi(
            [],
            unread_messages={},
            current_chat="Mzxx",
            current_messages=[
                {"sender": "Mzxx", "text": "old", "is_self": False},
                {"text": "already-sent-by-agent"},
            ],
        )
        cursors = InMemoryCursorStore()
        cursors.set_message_cursor(
            "default",
            "Mzxx",
            {"tail": [], "count": 2, "outbound_texts": ["already-sent-by-agent"]},
            "visible",
        )

        result = reply_to_unread_messages(
            api=api,
            agent=EchoAgent(),
            real=True,
            cursor_store=cursors,
            account_id="default",
        )

        self.assertTrue(result["current_chat_checked"])
        self.assertFalse(result["current_chat_replied"])
        self.assertEqual(result["reply_count"], 0)
        self.assertEqual(api.current_sent, [])

    def test_reply_unread_marks_self_messages_in_current_context(self) -> None:
        api = FakeChatApi(
            [],
            unread_messages={},
            current_chat="Mzxx",
            current_messages=[
                {"text": "my prior answer", "is_self": True},
                {"text": "new question", "is_self": False},
            ],
        )
        agent = ContextRecordingAgent()

        result = reply_to_unread_messages(api=api, agent=agent, real=True)

        self.assertTrue(result["current_chat_replied"])
        self.assertEqual(agent.calls[0][1], ["我: my prior answer"])

    def test_reply_unread_current_chat_respects_friend_filter(self) -> None:
        api = FakeChatApi(
            [],
            unread_messages={},
            current_chat="Other",
            current_messages=[
                {"sender": "Other", "text": "should-not-reply", "is_self": False},
            ],
        )

        result = reply_to_unread_messages(
            api=api,
            agent=EchoAgent(),
            real=True,
            friend="Mzxx",
        )

        self.assertTrue(result["current_chat_checked"])
        self.assertFalse(result["current_chat_replied"])
        self.assertEqual(result["reply_count"], 0)
        self.assertEqual(api.current_sent, [])

    def test_reply_unread_respects_friend_filter(self) -> None:
        api = FakeChatApi(
            [],
            unread_messages={
                "Mzxx": [{"text": "hello"}],
                "Other": [{"text": "skip"}],
            },
        )

        result = reply_to_unread_messages(
            api=api,
            agent=EchoAgent(),
            real=True,
            friend="Mzxx",
        )

        self.assertEqual(result["chat_count"], 1)
        self.assertEqual(result["reply_count"], 1)
        self.assertEqual(api.sent[0]["friend"], "Mzxx")

    def test_reply_unread_friend_filter_allows_substring_match(self) -> None:
        api = FakeChatApi(
            [],
            unread_messages={
                "陈卓、Hope Of The Whole Village、周彪": [{"text": "hello"}],
            },
        )

        result = reply_to_unread_messages(
            api=api,
            agent=EchoAgent(),
            real=True,
            friend="Hope Of The Whole Village",
        )

        self.assertEqual(result["chat_count"], 1)
        self.assertEqual(result["reply_count"], 1)
        self.assertEqual(api.sent[0]["friend"], "陈卓、Hope Of The Whole Village、周彪")

    def test_reply_unread_polls_visible_chat_after_unread_cursor(self) -> None:
        api = FakeChatApi(
            [{"sender": "Mzxx", "text": "first"}],
            unread_messages={"Mzxx": [{"sender": "Mzxx", "text": "first"}]},
            append_sent_to_messages=True,
        )
        cursors = InMemoryCursorStore()

        first = reply_to_unread_messages(
            api=api,
            agent=EchoAgent(),
            real=True,
            cursor_store=cursors,
            account_id="default",
            visible_chats=[],
        )

        self.assertEqual(first["reply_count"], 1)
        api.unread_messages = {}
        api.messages.append({"sender": "Mzxx", "text": "second"})

        second = reply_to_unread_messages(
            api=api,
            agent=EchoAgent(),
            real=True,
            cursor_store=cursors,
            account_id="default",
            visible_chats=["Mzxx"],
        )

        self.assertEqual(second["chat_count"], 0)
        self.assertEqual(second["visible_chat_count"], 1)
        self.assertEqual(second["reply_count"], 1)
        self.assertIn("second", str(api.sent[-1]["text"]))

    def test_wechat_api_dry_run_check_unread_messages(self) -> None:
        result = WeChatApi(dry_run=True).check_unread_messages()

        self.assertEqual(result["action"], "check_unread_messages")
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["messages_by_chat"], {})

    def test_moments_file_dialog_value_uses_raw_single_path(self) -> None:
        path = r"D:\tmp\file with space.png"

        self.assertEqual(WeChatApi._build_native_file_dialog_value([path]), path)

    def test_moments_file_dialog_value_quotes_multiple_paths_without_trailing_space(self) -> None:
        value = WeChatApi._build_native_file_dialog_value(
            [r"D:\tmp\a one.png", r"D:\tmp\b two.png"]
        )

        self.assertEqual(value, r'"D:\tmp\a one.png" "D:\tmp\b two.png"')

    def test_moments_media_paths_are_normalized_from_quoted_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / "a file.png"
            media.write_bytes(b"png")
            api = WeChatApi(project_root=Path(tmp), dry_run=True)

            self.assertEqual(
                api._normalize_moments_media_paths([f'"{media}"']),
                [str(media.resolve())],
            )

    def test_moments_media_paths_reject_missing_files(self) -> None:
        with self.assertRaises(WeChatApiError):
            WeChatApi(dry_run=True)._normalize_moments_media_paths([r"D:\tmp\missing.png"])

    def test_moments_file_dialog_fallback_picks_bottom_edit(self) -> None:
        search_edit = FakeUiaNode("Edit", "Edit", "", rect=FakeRect(20, 40, 220, 64))
        file_name_edit = FakeUiaNode("Edit", "Edit", "", rect=FakeRect(20, 360, 420, 384))
        dialog = FakeUiaWindow([search_edit, file_name_edit])

        self.assertIs(
            WeChatApi(dry_run=True)._find_native_file_name_edit(dialog),
            file_name_edit,
        )

    def test_moments_file_dialog_open_button_ignores_dropdown(self) -> None:
        dropdown = FakeUiaNode("Button", "Button", "打开", automation_id="DropDown")
        open_button = FakeUiaNode("Button", "Button", "打开(O)", automation_id="1")
        dialog = FakeUiaWindow([dropdown, open_button])

        self.assertIs(WeChatApi(dry_run=True)._find_native_open_button(dialog), open_button)

    def test_uia_unlock_rejects_explorer_weixin_window(self) -> None:
        self.assertFalse(
            _is_wechat_window_candidate("Weixin", "CabinetWClass", "explorer.exe")
        )

    def test_uia_unlock_rejects_real_weixin_non_chinese_title(self) -> None:
        self.assertFalse(
            _is_wechat_window_candidate("Weixin", "Qt51514QWindowIcon", "Weixin.exe")
        )

    def test_uia_unlock_accepts_real_weixin_qt_window(self) -> None:
        self.assertTrue(
            _is_wechat_window_candidate("微信", "Qt51514QWindowIcon", "Weixin.exe")
        )

    def test_uia_unlock_rejects_wechat_tray_window(self) -> None:
        self.assertFalse(
            _is_wechat_window_candidate(
                "WxTrayIconMessageWindow",
                "Qt51514WxTrayIconMessageWindowClass",
                "Weixin.exe",
            )
        )

    def test_wechat_api_check_unread_messages_does_not_use_multiselect(self) -> None:
        api = FakeUnreadWeChatApi()

        result = api.check_unread_messages(dry_run=False)

        self.assertEqual(
            [item["text"] for item in result["messages_by_chat"]["Mzxx"]],
            ["first unread", "second unread"],
        )
        self.assertEqual(api.navigator.opened_with_select, [False])

    def test_visible_message_reader_ignores_session_list_items(self) -> None:
        api = TestableWeChatApi(dry_run=True)
        window = FakeUiaWindow(
            [
                FakeUiaNode("mmui::ChatSessionCell", "ListItem", "Mzxx\n[1]"),
                FakeUiaNode("mmui::ChatTextItemView", "Text", "hello"),
                FakeUiaNode("mmui::ChatBubbleItemView", "ListItem", "world"),
            ]
        )

        rows = api._collect_visible_message_nodes(window)

        self.assertEqual([row["text"] for row in rows], ["hello", "world"])

    def test_visible_message_reader_keeps_collection_diagnostics_out_of_info_logs(self) -> None:
        api = TestableWeChatApi(dry_run=True)
        window = FakeUiaWindow([FakeUiaNode("mmui::ChatTextItemView", "Text", "hello")])

        with patch("wxclient.wechat_api.service.logger") as fake_logger:
            rows = api._collect_visible_message_nodes(window)

        self.assertEqual([row["text"] for row in rows], ["hello"])
        info_events = [call.args[0] for call in fake_logger.info.call_args_list]
        debug_events = [call.args[0] for call in fake_logger.debug.call_args_list]
        self.assertNotIn("collect_visible_messages.collected", info_events)
        self.assertNotIn("collect_visible_messages.group_detect", info_events)
        self.assertIn("collect_visible_messages.collected", debug_events)
        self.assertIn("collect_visible_messages.group_detect", debug_events)

    def test_control_type_matching_accepts_uia_control_type_alias(self) -> None:
        node = FakeUiaNode("mmui::RecyclerListView", "ListBox", "消息")
        node.element_info.control_type = "List"

        self.assertTrue(_control_matches_criteria(node, {"control_type": "List"}))

    def test_visible_message_reader_marks_group_from_count_label(self) -> None:
        api = TestableWeChatApi(dry_run=True)
        count_label = FakeUiaNode("mmui::XTextView", "Text", "(3)")
        count_label.element_info.automation_id = (
            "content_view.top_content_view.title_h_view.left_v_view.left_content_v_view."
            "left_ui_.big_title_line_h_view.current_chat_count_label"
        )
        window = FakeUiaWindow(
            [
                count_label,
                FakeUiaNode("mmui::ChatTextItemView", "Text", "hello"),
            ]
        )

        rows = api._collect_visible_message_nodes(window, chat_name="Mzxx")

        self.assertTrue(rows[0]["is_group"])
        self.assertEqual(_display_sender(rows[0], "Mzxx"), "未知")

    def test_selected_group_message_text_extracts_sender_and_content(self) -> None:
        sender, content = _split_selected_group_message_text("Kk 凯哥\n@面面xx 早")

        self.assertEqual(sender, "Kk 凯哥")
        self.assertEqual(content, "@面面xx 早")

    def test_group_message_without_sender_displays_other_person_when_not_self(self) -> None:
        sender = _display_sender({"is_group": True, "is_self": False}, "Mzxx")

        self.assertEqual(sender, "其他人")

    def test_chat_message_logging_skips_timestamp_rows(self) -> None:
        messages = [
            {"text": "08:56", "is_timestamp": True, "timestamp_text": "08:56"},
            {"text": "hello", "is_group": True, "is_self": False},
            {"text": "mine", "is_group": True, "is_self": True},
        ]

        with patch("wxclient.agentbridge.reply_flow.logger") as fake_logger:
            _log_chat_messages("watch.chats.message", "Mzxx", messages)

        self.assertEqual(fake_logger.info.call_count, 2)
        first = fake_logger.info.call_args_list[0].kwargs
        second = fake_logger.info.call_args_list[1].kwargs
        self.assertEqual(first["index"], 1)
        self.assertEqual(first["total"], 2)
        self.assertEqual(first["sender"], "其他人")
        self.assertEqual(second["sender"], "我")

    def test_visible_message_reader_marks_today_timestamp_for_following_message(self) -> None:
        api = TestableWeChatApi(dry_run=True)
        window = FakeUiaWindow(
            [
                FakeUiaNode("mmui::ChatItemView", "ListItem", "12:30"),
                FakeUiaNode("mmui::ChatTextItemView", "Text", "today message"),
            ]
        )

        rows = api._collect_visible_message_nodes(window)

        self.assertTrue(rows[0]["is_timestamp"])
        self.assertTrue(rows[1]["timestamp_is_today"])

    def test_visible_message_reader_marks_yesterday_timestamp_for_following_message(self) -> None:
        api = TestableWeChatApi(dry_run=True)
        window = FakeUiaWindow(
            [
                FakeUiaNode("mmui::ChatItemView", "ListItem", "昨天 12:30"),
                FakeUiaNode("mmui::ChatTextItemView", "Text", "old message"),
            ]
        )

        rows = api._collect_visible_message_nodes(window)

        self.assertTrue(rows[0]["is_timestamp"])
        self.assertFalse(rows[1]["timestamp_is_today"])

    def test_visible_message_reader_orders_messages_by_screen_bottom(self) -> None:
        api = TestableWeChatApi(dry_run=True)
        window = FakeUiaWindow(
            [
                FakeUiaNode(
                    "mmui::ChatTextItemView",
                    "Text",
                    "bottom",
                    FakeRect(100, 300, 200, 330),
                ),
                FakeUiaNode(
                    "mmui::ChatTextItemView",
                    "Text",
                    "top",
                    FakeRect(100, 100, 200, 130),
                ),
            ]
        )

        rows = api._collect_visible_message_nodes(window)

        self.assertEqual([row["text"] for row in rows], ["top", "bottom"])

    def test_message_side_detection_marks_left_and_right_bubbles(self) -> None:
        container = FakeRect(300, 0, 1000, 900)

        self.assertFalse(_infer_message_is_self(FakeRect(360, 100, 560, 150), container))
        self.assertTrue(_infer_message_is_self(FakeRect(740, 200, 940, 250), container))

    def test_message_side_detection_handles_wide_bubbles_by_gap(self) -> None:
        container = FakeRect(300, 0, 1000, 900)

        self.assertFalse(_infer_message_is_self(FakeRect(330, 100, 870, 150), container))
        self.assertTrue(_infer_message_is_self(FakeRect(430, 200, 970, 250), container))
        self.assertIsNone(_infer_message_is_self(FakeRect(300, 300, 1000, 350), container))

    def test_message_screenshot_side_detection_marks_left_and_right_rows(self) -> None:
        left_row = Image.new("RGB", (120, 36), (245, 245, 245))
        right_row = Image.new("RGB", (120, 36), (245, 245, 245))
        ImageDraw.Draw(left_row).rectangle((8, 8, 42, 28), fill=(255, 255, 255))
        ImageDraw.Draw(right_row).rectangle((78, 8, 112, 28), fill=(149, 236, 105))

        self.assertFalse(_infer_message_is_self_from_row_image(left_row))
        self.assertTrue(_infer_message_is_self_from_row_image(right_row))

    def test_message_screenshot_side_detection_ignores_centered_rows(self) -> None:
        row = Image.new("RGB", (120, 36), (245, 245, 245))
        ImageDraw.Draw(row).rectangle((46, 8, 74, 28), fill=(220, 220, 220))

        self.assertIsNone(_infer_message_is_self_from_row_image(row))

    def test_read_current_chat_uses_fallback_name_when_title_missing(self) -> None:
        window = FakeUiaWindow(
            [
                FakeUiaNode("mmui::ChatItemView", "ListItem", "22:27"),
                FakeUiaNode("mmui::ChatTextItemView", "Text", "很高兴认识你"),
            ]
        )
        api = CurrentChatFallbackWeChatApi(window)

        result = api.read_current_chat(dry_run=False)

        self.assertEqual(result["chat"], "当前会话")
        self.assertEqual(result["latest_message"]["text"], "很高兴认识你")
        self.assertFalse(result["latest_is_self"])

    def test_read_current_chat_marks_self_recall_activity(self) -> None:
        window = FakeUiaWindow(
            [
                FakeUiaNode("mmui::ChatTextItemView", "Text", "\u6211\u662fkcc"),
                FakeUiaNode("mmui::ChatItemView", "ListItem", "23:05"),
                FakeUiaNode(
                    "mmui::ChatItemView",
                    "ListItem",
                    "\u4f60\u64a4\u56de\u4e86\u4e00\u6761\u6d88\u606f  \u91cd\u65b0\u7f16\u8f91",
                ),
            ]
        )
        api = CurrentChatFallbackWeChatApi(window)

        result = api.read_current_chat(dry_run=False)

        self.assertTrue(result["latest_is_self"])
        self.assertEqual(result["latest_message"]["sender"], "\u6211")
        self.assertIn("\u64a4\u56de", result["latest_message"]["text"])

    def test_resolve_control_by_criteria_falls_back_to_descendants(self) -> None:
        input_node = FakeUiaNode(
            "mmui::ChatInputField",
            "Edit",
            "",
            automation_id="chat_input_field",
        )
        window = FakeUiaWindow([input_node])

        result = _resolve_control_by_criteria(
            window,
            {"control_type": "Edit", "auto_id": "chat_input_field"},
        )

        self.assertIs(result, input_node)

    def test_tail_messages_keeps_only_unread_count(self) -> None:
        rows = [{"text": "old"}, {"text": "first"}, {"text": "second"}]

        self.assertEqual(_tail_messages(rows, 2), [{"text": "first"}, {"text": "second"}])

    def test_worker_executes_dry_run_check_unread_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "test.db")
            task = store.create_task(
                TaskType.CHECK_UNREAD_MESSAGES,
                {"dry_run": True},
                "default",
            )
            worker = OperationWorker(store, WeChatApi(dry_run=True))

            self.assertTrue(worker.run_once())
            loaded = store.get(task.id)
            self.assertEqual(loaded.status, TaskStatus.SUCCEEDED)
            self.assertEqual(loaded.result["action"], "check_unread_messages")
            self.assertTrue(loaded.result["dry_run"])

    def test_worker_executes_dry_run_reply_chat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "test.db")
            task = store.create_task(
                TaskType.REPLY_CHAT,
                {
                    "friend": "filehelper",
                    "duration": "1s",
                    "source": "new",
                    "reply_all": False,
                    "dry_run": True,
                },
                "default",
            )
            worker = OperationWorker(store, WeChatApi(dry_run=True), agent=FakeAgent("pong"))

            self.assertTrue(worker.run_once())
            loaded = store.get(task.id)
            self.assertEqual(loaded.status, TaskStatus.SUCCEEDED)
            self.assertEqual(loaded.result["action"], "reply_chat")
            self.assertEqual(loaded.result["reply_count"], 0)
            self.assertTrue(loaded.result["dry_run"])

    def test_worker_executes_dry_run_reply_unread(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "test.db")
            task = store.create_task(
                TaskType.REPLY_UNREAD,
                {
                    "friend": "filehelper",
                    "reply_all": False,
                    "dry_run": True,
                },
                "default",
            )
            worker = OperationWorker(store, WeChatApi(dry_run=True), agent=FakeAgent("pong"))

            self.assertTrue(worker.run_once())
            loaded = store.get(task.id)
            self.assertEqual(loaded.status, TaskStatus.SUCCEEDED)
            self.assertEqual(loaded.result["action"], "reply_unread")
            self.assertEqual(loaded.result["reply_count"], 0)
            self.assertTrue(loaded.result["dry_run"])

    def test_worker_executes_dry_run_watch_chats(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "test.db")
            task = store.create_task(
                TaskType.WATCH_CHATS,
                {
                    "friends": ["filehelper", "测试群"],
                    "duration": "1s",
                    "auto_reply": True,
                    "reply_all": False,
                    "dry_run": True,
                },
                "default",
            )
            worker = OperationWorker(store, WeChatApi(dry_run=True), agent=FakeAgent("pong"))

            self.assertTrue(worker.run_once())
            loaded = store.get(task.id)
            self.assertEqual(loaded.status, TaskStatus.SUCCEEDED)
            self.assertEqual(loaded.result["action"], "watch_chats")
            self.assertEqual(loaded.result["chat_count"], 2)
            self.assertTrue(loaded.result["dry_run"])

    def test_cli_check_unread_messages_enqueues_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "test.db"
            config_path = _write_config(Path(tmp), store_path)
            result = _invoke_cli(["check-unread-messages", "--config", str(config_path)])
            store = TaskStore(store_path)

            self.assertEqual(result.exit_code, 0)
            tasks = store.list_tasks()
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0].type, TaskType.CHECK_UNREAD_MESSAGES)
            self.assertTrue(tasks[0].payload["dry_run"])

    def test_cli_reply_chat_enqueues_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "test.db"
            config_path = _write_config(Path(tmp), store_path)
            result = _invoke_cli(
                [
                    "reply-chat",
                    "filehelper",
                    "--latest-visible",
                    "--config",
                    str(config_path),
                ],
            )
            store = TaskStore(store_path)

            self.assertEqual(result.exit_code, 0)
            tasks = store.list_tasks()
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0].type, TaskType.REPLY_CHAT)
            self.assertEqual(tasks[0].payload["friend"], "filehelper")
            self.assertEqual(tasks[0].payload["source"], "visible")
            self.assertTrue(tasks[0].payload["dry_run"])

    def test_cli_listen_chats_enqueues_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "test.db"
            config_path = _write_config(Path(tmp), store_path)
            result = _invoke_cli(
                [
                    "listen-chats",
                    "filehelper",
                    "测试群",
                    "--duration",
                    "1s",
                    "--config",
                    str(config_path),
                ],
            )
            store = TaskStore(store_path)

            self.assertEqual(result.exit_code, 0)
            tasks = store.list_tasks()
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0].type, TaskType.WATCH_CHATS)
            self.assertEqual(tasks[0].payload["friends"], ["filehelper", "测试群"])
            self.assertTrue(tasks[0].payload["auto_reply"])
            self.assertTrue(tasks[0].payload["dry_run"])

    def test_cli_reply_unread_enqueues_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "test.db"
            config_path = _write_config(Path(tmp), store_path)
            result = _invoke_cli(
                [
                    "reply-unread",
                    "--friend",
                    "filehelper",
                    "--config",
                    str(config_path),
                ],
            )
            store = TaskStore(store_path)

            self.assertEqual(result.exit_code, 0)
            tasks = store.list_tasks()
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0].type, TaskType.REPLY_UNREAD)
            self.assertEqual(tasks[0].payload["friend"], "filehelper")
            self.assertTrue(tasks[0].payload["dry_run"])

    def test_gui_settings_round_trip_agent_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "settings.json"
            settings = Settings(
                data_dir=Path(tmp) / "data",
                llm_base_url="http://172.16.6.23:3000",
                llm_model="glm-5.1",
                agent_timeout_seconds=45,
                dry_run=False,
            )

            save_gui_settings(config_path, settings, api_key="secret-value")
            loaded, loaded_path = load_gui_settings(config_path)

            self.assertEqual(loaded_path, config_path)
            self.assertEqual(loaded.llm_base_url, "http://172.16.6.23:3000")
            self.assertEqual(loaded.llm_model, "glm-5.1")
            self.assertEqual(loaded.llm_api_key.get_secret_value(), "secret-value")
            self.assertEqual(loaded.agent_timeout_seconds, 45)
            self.assertFalse(loaded.dry_run)

    def test_ui_task_action_enqueue_reply_unread(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "test.db")

            task = enqueue_reply_unread(
                store,
                account_id="default",
                friend="Mzxx",
                reply_all=True,
                real=True,
            )

            self.assertEqual(task.type, TaskType.REPLY_UNREAD)
            self.assertEqual(task.payload["friend"], "Mzxx")
            self.assertTrue(task.payload["reply_all"])
            self.assertFalse(task.payload["dry_run"])

    def test_ui_task_action_enqueue_watch_chats(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "test.db")

            task = enqueue_watch_chats(
                store,
                account_id="default",
                friends=["Mzxx", "测试群"],
                duration="30s",
                auto_reply=True,
                reply_all=False,
                select_sender=True,
                real=True,
            )

            self.assertEqual(task.type, TaskType.WATCH_CHATS)
            self.assertEqual(task.payload["friends"], ["Mzxx", "测试群"])
            self.assertTrue(task.payload["auto_reply"])
            self.assertTrue(task.payload["select_sender"])
            self.assertFalse(task.payload["dry_run"])

    def test_openai_agent_config_requires_api_key(self) -> None:
        with patch.dict(os.environ, {"WXCLIENT_LLM_API_KEY": ""}, clear=False):
            with self.assertRaises(AgentConfigError):
                build_chat_agent(Settings())

    def test_openai_agent_defaults_to_internal_model_endpoint(self) -> None:
        settings = Settings()

        self.assertEqual(settings.llm_base_url, "http://172.16.6.23:3000")
        self.assertEqual(settings.llm_model, "glm-5.1")

    def test_openai_base_url_is_normalized(self) -> None:
        self.assertEqual(
            normalize_openai_base_url("http://172.16.6.23:3000/"),
            "http://172.16.6.23:3000/v1",
        )
        self.assertEqual(
            normalize_openai_base_url("http://172.16.6.23:3000/v1"),
            "http://172.16.6.23:3000/v1",
        )

    def test_openai_http_client_ignores_environment_proxy(self) -> None:
        client = _build_http_client()
        self.assertFalse(client._trust_env)
        asyncio.run(client.aclose())

    def test_fastapi_send_message_endpoint_enqueues_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "test.db")
            settings = Settings(data_dir=Path(tmp), dry_run=True)
            client = TestClient(create_app(settings, store))

            response = client.post(
                "/tasks/send-message",
                json={"friend": "filehelper", "text": "hello"},
            )

            self.assertEqual(response.status_code, 202)
            tasks = store.list_tasks()
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0].type, TaskType.SEND_MESSAGE)

    def test_fastapi_listen_chats_endpoint_enqueues_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "test.db")
            settings = Settings(data_dir=Path(tmp), dry_run=True)
            client = TestClient(create_app(settings, store))

            response = client.post(
                "/tasks/listen-chats",
                json={"friends": ["filehelper", "测试群"], "duration": "1s"},
            )

            self.assertEqual(response.status_code, 202)
            tasks = store.list_tasks()
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0].type, TaskType.WATCH_CHATS)
            self.assertEqual(tasks[0].payload["friends"], ["filehelper", "测试群"])

    def test_worker_executes_dry_run_scan_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "test.db")
            task = store.create_task(
                TaskType.SCAN_MESSAGES,
                {
                    "friend": "filehelper",
                    "duration": "30s",
                    "select_sender": False,
                    "dry_run": True,
                },
                "default",
            )
            worker = OperationWorker(store, WeChatApi(dry_run=True))

            self.assertTrue(worker.run_once())
            loaded = store.get(task.id)
            self.assertEqual(loaded.status, TaskStatus.SUCCEEDED)
            self.assertEqual(loaded.result["action"], "scan_messages")
            self.assertTrue(loaded.result["dry_run"])

    def test_watch_chats_auto_replies_to_multiple_targets(self) -> None:
        api = FakeChatApi([{"sender": "Mzxx", "text": "hello", "is_self": False}])

        result = watch_chats_messages(
            api=api,
            agent=EchoAgent(),
            friends=["Mzxx", "测试群"],
            duration="1s",
            real=True,
            auto_reply=True,
        )

        self.assertEqual(result["action"], "watch_chats")
        self.assertEqual(result["chat_count"], 2)
        self.assertEqual(result["reply_count"], 2)
        self.assertEqual(api.scanned, [])
        self.assertEqual(api.visible_reads, ["Mzxx", "测试群"])
        self.assertEqual(api.scan_separate_windows, [])
        self.assertEqual(api.read_separate_windows, [False, False])
        self.assertEqual([item["friend"] for item in api.sent], ["Mzxx", "测试群"])

    def test_watch_chats_can_listen_without_reply(self) -> None:
        api = FakeChatApi([{"sender": "Mzxx", "text": "hello", "is_self": False}])

        result = watch_chats_messages(
            api=api,
            agent=EchoAgent(),
            friends=["Mzxx"],
            duration="1s",
            real=True,
            auto_reply=False,
        )

        self.assertEqual(result["message_count"], 1)
        self.assertEqual(result["visible_message_count"], 1)
        self.assertEqual(result["reply_count"], 0)
        self.assertEqual(api.scanned, [])
        self.assertEqual(api.visible_reads, ["Mzxx"])
        self.assertEqual(api.scan_separate_windows, [])
        self.assertEqual(api.read_separate_windows, [False])
        self.assertEqual(api.sent, [])

    def test_watch_chats_skips_separate_window_when_session_preview_marks_self(self) -> None:
        api = FakeChatApi(
            [{"sender": "Other", "text": "hello", "is_self": False, "timestamp_is_today": True}],
            session_summaries=[
                {
                    "name": "Mzxx",
                    "preview": "Me: already answered",
                    "timestamp_text": "10:32",
                    "timestamp_is_today": True,
                    "is_selected": True,
                },
            ],
        )

        result = watch_chats_messages(
            api=api,
            agent=EchoAgent(),
            friends=["Mzxx"],
            duration="1s",
            real=True,
            auto_reply=True,
        )

        self.assertEqual(result["reply_count"], 1)
        self.assertEqual(api.scanned, [])
        self.assertEqual(api.visible_reads, ["Mzxx"])
        self.assertEqual(api.read_separate_windows, [False])
        self.assertFalse(result["results"][0]["fallback_window_opened"])
        self.assertEqual(result["results"][0]["latest_message_source"], "visible_messages")
        self.assertEqual(result["results"][0]["decision_reason"], "replied")
        self.assertTrue(result["results"][0]["latest_is_today"])

    def test_watch_chats_skips_separate_window_when_session_summary_is_not_today(self) -> None:
        api = FakeChatApi(
            [{"sender": "Other", "text": "hello", "is_self": False, "timestamp_is_today": True}],
            session_summaries=[
                {
                    "name": "Mzxx",
                    "preview": "Other: old message",
                    "timestamp_text": "昨天 10:32",
                    "timestamp_is_today": False,
                    "is_selected": True,
                },
            ],
        )

        result = watch_chats_messages(
            api=api,
            agent=EchoAgent(),
            friends=["Mzxx"],
            duration="1s",
            real=True,
            auto_reply=True,
        )

        self.assertEqual(result["reply_count"], 1)
        self.assertEqual(api.scanned, [])
        self.assertEqual(api.visible_reads, ["Mzxx"])
        self.assertEqual(api.read_separate_windows, [False])
        self.assertFalse(result["results"][0]["fallback_window_opened"])
        self.assertEqual(result["results"][0]["decision_reason"], "replied")
        self.assertTrue(result["results"][0]["latest_is_today"])

    def test_watch_chats_uses_scan_result_instead_of_seeding_visible_history(self) -> None:
        api = FakeChatApi([{"sender": "Mzxx", "text": "hello", "is_self": False}])
        cursors = InMemoryCursorStore()

        result = watch_chats_messages(
            api=api,
            agent=EchoAgent(),
            friends=["Mzxx"],
            duration="1s",
            real=True,
            auto_reply=True,
            cursor_store=cursors,
        )

        self.assertEqual(result["visible_message_count"], 1)
        self.assertEqual(result["message_count"], 1)
        self.assertEqual(result["new_message_count"], 1)
        self.assertEqual(result["cursor_seed_count"], 0)
        self.assertEqual(result["reply_count"], 1)
        self.assertEqual(api.scanned, [])
        self.assertEqual(api.visible_reads, ["Mzxx"])
        self.assertEqual(api.read_separate_windows, [False])
        self.assertIn("hello", str(api.sent[0]["text"]))

    def test_watch_chats_replies_to_latest_visible_when_scan_has_no_new_nodes(self) -> None:
        api = FakeChatApi(
            [],
            visible_messages=[
                {"sender": "Mzxx", "text": "old", "is_self": False},
                {"sender": "Mzxx", "text": "visible-last", "is_self": False},
            ],
        )
        cursors = InMemoryCursorStore()

        result = watch_chats_messages(
            api=api,
            agent=EchoAgent(),
            friends=["Mzxx"],
            duration="1s",
            real=True,
            auto_reply=True,
            cursor_store=cursors,
        )

        self.assertEqual(result["message_count"], 1)
        self.assertEqual(result["new_message_count"], 1)
        self.assertEqual(result["reply_count"], 1)
        self.assertEqual(api.scanned, [])
        self.assertEqual(api.visible_reads, ["Mzxx"])
        self.assertEqual(api.read_separate_windows, [False])
        self.assertIn("visible-last", str(api.sent[0]["text"]))

    def test_watch_chats_skips_new_self_messages(self) -> None:
        api = FakeChatApi([{"sender": "Mzxx", "text": "hello", "is_self": False}])
        cursors = InMemoryCursorStore()

        watch_chats_messages(
            api=api,
            agent=EchoAgent(),
            friends=["Mzxx"],
            duration="1s",
            real=True,
            auto_reply=True,
            cursor_store=cursors,
        )
        api.messages = [{"sender": "me", "text": "mine", "is_self": True}]
        api.visible_messages = [{"sender": "me", "text": "mine", "is_self": True}]
        api.sent.clear()

        result = watch_chats_messages(
            api=api,
            agent=EchoAgent(),
            friends=["Mzxx"],
            duration="1s",
            real=True,
            auto_reply=True,
            cursor_store=cursors,
        )

        self.assertEqual(result["message_count"], 0)
        self.assertEqual(result["new_message_count"], 0)
        self.assertEqual(result["reply_count"], 0)
        self.assertEqual(api.sent, [])

    def test_watch_chats_marks_empty_agent_reply_when_latest_visible_is_not_self(self) -> None:
        api = FakeChatApi([{"sender": "Mzxx", "text": "图片", "is_self": False}])

        result = watch_chats_messages(
            api=api,
            agent=FakeAgent("  "),
            friends=["Mzxx"],
            duration="1s",
            real=True,
            auto_reply=True,
        )

        self.assertEqual(result["message_count"], 1)
        self.assertEqual(result["new_message_count"], 1)
        self.assertEqual(result["reply_count"], 0)
        self.assertEqual(result["results"][0]["decision_reason"], "empty_agent_reply")
        self.assertEqual(api.sent, [])

    def test_watch_chats_replies_to_latest_image_message_even_if_cursor_seen(self) -> None:
        latest = {"sender": "Mzxx", "text": "图片", "is_self": False}
        api = FakeChatApi([], visible_messages=[latest])
        cursors = InMemoryCursorStore()
        cursors.set_message_cursor(
            "default",
            "Mzxx",
            {"tail": [_message_fingerprint(latest)]},
            "watch",
        )

        result = watch_chats_messages(
            api=api,
            agent=EchoAgent(),
            friends=["Mzxx"],
            duration="1s",
            real=True,
            auto_reply=True,
            cursor_store=cursors,
        )

        self.assertEqual(result["message_count"], 1)
        self.assertEqual(result["new_message_count"], 1)
        self.assertEqual(result["reply_count"], 1)
        self.assertEqual(api.visible_reads, ["Mzxx"])
        self.assertIn("图片", str(api.sent[0]["text"]))


def _write_config(tmp: Path, store_path: Path) -> Path:
    config_path = tmp / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "data_dir": str(tmp),
                "database_path": str(store_path),
            }
        ),
        encoding="utf-8",
    )
    return config_path


def _invoke_cli(args: list[str]):
    result = CliRunner().invoke(app, args)
    logging.getLogger().handlers.clear()
    return result


if __name__ == "__main__":
    unittest.main()
