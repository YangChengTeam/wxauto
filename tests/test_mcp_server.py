from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path
from typing import Any

from wxclient.config import Settings
from wxclient.mcp_server.media import MediaStore
from wxclient.queue import TaskStore, TaskType

try:
    from mcp.shared.memory import create_connected_server_and_client_session

    from wxclient.mcp_server import McpServerSettings, WxMcpRuntime, create_mcp_server

    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False


class _FakeWeChatApi:
    def send_message(
        self,
        friend: str,
        text: str,
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        return {
            "action": "send_message",
            "friend": friend,
            "text": text,
            "dry_run": bool(dry_run),
        }


class _FakeMcpRuntime:
    def __init__(self, upload_dir: Path) -> None:
        self.media_store = MediaStore(upload_dir, 1024 * 1024)
        self.started = False
        self.calls: list[tuple[TaskType, dict[str, Any]]] = []

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    async def submit(
        self,
        task_type: TaskType,
        payload: dict[str, Any],
        **_: Any,
    ) -> dict[str, Any]:
        self.calls.append((task_type, payload))
        return {"task_id": "fake-task", "type": task_type.value, "status": "succeeded"}

    def get_task(self, task_id: str) -> dict[str, Any]:
        return {"task_id": task_id, "status": "succeeded"}

    def list_tasks(self, limit: int = 20) -> list[dict[str, Any]]:
        return [{"task_id": "fake-task", "status": "succeeded"}][:limit]


@unittest.skipUnless(MCP_AVAILABLE, "MCP optional dependency is not installed")
class McpRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_executes_dry_run_through_single_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root, database_path=root / "tasks.db", dry_run=True)
            mcp_settings = McpServerSettings(upload_dir=root / "uploads")
            runtime = WxMcpRuntime(
                settings,
                mcp_settings,
                store=TaskStore(root / "tasks.db"),
                api=_FakeWeChatApi(),  # type: ignore[arg-type]
            )
            runtime.start()
            try:
                result = await runtime.submit(
                    TaskType.SEND_MESSAGE,
                    {"friend": "filehelper", "text": "hello", "dry_run": True},
                )
            finally:
                runtime.stop()

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["result"]["friend"], "filehelper")

    async def test_runtime_rejects_real_action_without_server_and_call_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root, database_path=root / "tasks.db")
            runtime = WxMcpRuntime(
                settings,
                McpServerSettings(allow_real_actions=False, upload_dir=root / "uploads"),
                store=TaskStore(root / "tasks.db"),
                api=_FakeWeChatApi(),  # type: ignore[arg-type]
            )
            with self.assertRaises(PermissionError):
                await runtime.submit(
                    TaskType.SEND_MESSAGE,
                    {"friend": "filehelper", "text": "hello", "dry_run": False},
                    confirmation="EXECUTE_REAL_ACTION",
                )


@unittest.skipUnless(MCP_AVAILABLE, "MCP optional dependency is not installed")
class McpProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_tools_are_callable_through_in_memory_mcp_transport(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = _FakeMcpRuntime(Path(tmp))
            mcp = create_mcp_server(
                settings=McpServerSettings(),
                runtime=runtime,  # type: ignore[arg-type]
            )
            async with create_connected_server_and_client_session(mcp) as client:
                tools = await client.list_tools()
                names = {tool.name for tool in tools.tools}
                self.assertIn("wx_add_friend", names)
                self.assertIn("wx_search_contacts", names)
                self.assertIn("wx_get_unread_messages", names)
                self.assertIn("wx_send_files", names)
                self.assertIn("wx_read_chat_history", names)
                self.assertIn("wx_get_contact_profile", names)
                self.assertIn("wx_list_group_members", names)
                self.assertIn("wx_listen_messages", names)
                self.assertIn("wx_post_moments", names)
                watch_tool = next(tool for tool in tools.tools if tool.name == "wx_watch_chats")
                watch_properties = watch_tool.inputSchema["properties"]
                self.assertNotIn("auto_reply", watch_properties)
                self.assertNotIn("reply_all", watch_properties)
                probe_tool = next(tool for tool in tools.tools if tool.name == "wx_probe_uia")
                search_tool = next(
                    tool for tool in tools.tools if tool.name == "wx_search_contacts"
                )
                send_tool = next(tool for tool in tools.tools if tool.name == "wx_send_message")
                unread_tool = next(
                    tool for tool in tools.tools if tool.name == "wx_get_unread_messages"
                )
                files_tool = next(tool for tool in tools.tools if tool.name == "wx_send_files")
                self.assertTrue(probe_tool.annotations.readOnlyHint)
                self.assertTrue(search_tool.annotations.readOnlyHint)
                self.assertTrue(unread_tool.annotations.readOnlyHint)
                self.assertFalse(send_tool.annotations.readOnlyHint)
                self.assertFalse(files_tool.annotations.readOnlyHint)

                searched = await client.call_tool(
                    "wx_search_contacts",
                    {"query": "彭杏", "limit": 5},
                )
                self.assertFalse(searched.isError)
                self.assertEqual(runtime.calls[0][0], TaskType.SEARCH_CONTACTS)

                result = await client.call_tool(
                    "wx_send_message",
                    {"friend": "filehelper", "text": "hello", "dry_run": True},
                )

            self.assertFalse(result.isError)
            self.assertEqual(result.structuredContent["status"], "succeeded")
            self.assertEqual(runtime.calls[1][0], TaskType.SEND_MESSAGE)

    async def test_media_upload_and_moments_reference_use_media_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = _FakeMcpRuntime(Path(tmp))
            mcp = create_mcp_server(
                settings=McpServerSettings(),
                runtime=runtime,  # type: ignore[arg-type]
            )
            async with create_connected_server_and_client_session(mcp) as client:
                uploaded = await client.call_tool(
                    "wx_upload_media",
                    {
                        "filename": "test.png",
                        "content_base64": base64.b64encode(b"fake-png").decode("ascii"),
                    },
                )
                media_id = uploaded.structuredContent["media_id"]
                posted = await client.call_tool(
                    "wx_post_moments",
                    {"text": "test", "media_ids": [media_id], "dry_run": True},
                )

            self.assertFalse(posted.isError)
            task_type, payload = runtime.calls[-1]
            self.assertEqual(task_type, TaskType.POST_MOMENTS)
            self.assertEqual(len(payload["medias"]), 1)
            self.assertTrue(Path(payload["medias"][0]).is_absolute())

    async def test_uploaded_document_can_be_sent_by_media_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = _FakeMcpRuntime(Path(tmp))
            mcp = create_mcp_server(
                settings=McpServerSettings(),
                runtime=runtime,  # type: ignore[arg-type]
            )
            async with create_connected_server_and_client_session(mcp) as client:
                uploaded = await client.call_tool(
                    "wx_upload_media",
                    {
                        "filename": "automation-test.txt",
                        "content_base64": base64.b64encode(b"mcp file test").decode("ascii"),
                    },
                )
                sent = await client.call_tool(
                    "wx_send_files",
                    {
                        "friend": "文件传输助手",
                        "media_ids": [uploaded.structuredContent["media_id"]],
                        "dry_run": True,
                    },
                )

            self.assertFalse(sent.isError)
            task_type, payload = runtime.calls[-1]
            self.assertEqual(task_type, TaskType.SEND_FILES)
            self.assertEqual(Path(payload["files"][0]).suffix, ".txt")
            self.assertTrue(Path(payload["files"][0]).is_absolute())

    async def test_document_upload_is_rejected_for_moments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = _FakeMcpRuntime(Path(tmp))
            mcp = create_mcp_server(
                settings=McpServerSettings(),
                runtime=runtime,  # type: ignore[arg-type]
            )
            async with create_connected_server_and_client_session(mcp) as client:
                uploaded = await client.call_tool(
                    "wx_upload_media",
                    {
                        "filename": "not-a-moment.txt",
                        "content_base64": base64.b64encode(b"text").decode("ascii"),
                    },
                )
                posted = await client.call_tool(
                    "wx_post_moments",
                    {
                        "text": "test",
                        "media_ids": [uploaded.structuredContent["media_id"]],
                        "dry_run": True,
                    },
                )

            self.assertTrue(posted.isError)


@unittest.skipUnless(MCP_AVAILABLE, "MCP optional dependency is not installed")
class McpSettingsTests(unittest.TestCase):
    def test_remote_listener_requires_bearer_token(self) -> None:
        settings = McpServerSettings(host="0.0.0.0", allowed_hosts=["10.0.0.2:*"])
        with self.assertRaisesRegex(ValueError, "AUTH_TOKEN"):
            settings.validate_for_start()


if __name__ == "__main__":
    unittest.main()
