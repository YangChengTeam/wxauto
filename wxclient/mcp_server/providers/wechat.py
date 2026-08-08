from __future__ import annotations

import asyncio
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from wxclient.queue import TaskType
from wxclient.uia_probe import probe_wechat_uia

from ..runtime import WxMcpRuntime


READ_ONLY_TOOL = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
WRITE_TOOL = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)


class WeChatToolProvider:
    """Registers the built-in WeChat MCP tools."""

    def register(self, mcp: FastMCP, runtime: WxMcpRuntime) -> None:
        @mcp.tool(name="wx_probe_uia", annotations=READ_ONLY_TOOL, structured_output=True)
        async def wx_probe_uia() -> dict[str, Any]:
            """Probe WeChat login and UIA availability without changing WeChat state."""
            return await asyncio.to_thread(probe_wechat_uia)

        @mcp.tool(name="wx_upload_media", annotations=WRITE_TOOL, structured_output=True)
        async def wx_upload_media(filename: str, content_base64: str) -> dict[str, Any]:
            """Upload an image/video to Windows and return a media_id for Moments."""
            return await asyncio.to_thread(
                runtime.media_store.save_base64,
                filename,
                content_base64,
            )

        @mcp.tool(name="wx_add_friend", annotations=WRITE_TOOL, structured_output=True)
        async def wx_add_friend(
            number: str,
            greetings: str = "你好",
            remark: str | None = None,
            dry_run: bool = True,
            confirmation: str = "",
            wait: bool = True,
        ) -> dict[str, Any]:
            """Add a friend by WeChat ID/phone. Real execution requires explicit confirmation."""
            return await runtime.submit(
                TaskType.ADD_FRIEND,
                {
                    "number": number,
                    "greetings": greetings,
                    "remark": remark,
                    "dry_run": dry_run,
                },
                wait=wait,
                confirmation=confirmation,
            )

        @mcp.tool(name="wx_check_new_friends", annotations=WRITE_TOOL, structured_output=True)
        async def wx_check_new_friends(
            accept: bool = False,
            limit: int = 8,
            dry_run: bool = True,
            confirmation: str = "",
            wait: bool = True,
        ) -> dict[str, Any]:
            """Check friend requests and optionally accept them."""
            task_type = TaskType.ACCEPT_NEW_FRIENDS if accept else TaskType.CHECK_NEW_FRIENDS
            return await runtime.submit(
                task_type,
                {"verify": accept, "limit": limit, "dry_run": dry_run},
                wait=wait,
                confirmation=confirmation,
            )

        @mcp.tool(name="wx_send_message", annotations=WRITE_TOOL, structured_output=True)
        async def wx_send_message(
            friend: str,
            text: str,
            dry_run: bool = True,
            confirmation: str = "",
            wait: bool = True,
        ) -> dict[str, Any]:
            """Send a text message to a friend or group through the serialized queue."""
            return await runtime.submit(
                TaskType.SEND_MESSAGE,
                {"friend": friend, "text": text, "dry_run": dry_run},
                wait=wait,
                confirmation=confirmation,
            )

        @mcp.tool(name="wx_listen_messages", annotations=READ_ONLY_TOOL, structured_output=True)
        async def wx_listen_messages(
            friend: str,
            duration: str = "30s",
            select_sender: bool = False,
            save_file: bool = False,
            save_media: bool = False,
            dry_run: bool = True,
            confirmation: str = "",
            wait: bool = True,
        ) -> dict[str, Any]:
            """Listen for new messages in one chat without generating replies."""
            return await runtime.submit(
                TaskType.SCAN_MESSAGES,
                {
                    "friend": friend,
                    "duration": duration,
                    "select_sender": select_sender,
                    "save_file": save_file,
                    "save_media": save_media,
                    "dry_run": dry_run,
                },
                wait=wait,
                confirmation=confirmation,
            )

        @mcp.tool(name="wx_watch_chats", annotations=READ_ONLY_TOOL, structured_output=True)
        async def wx_watch_chats(
            friends: list[str],
            duration: str = "30s",
            dry_run: bool = True,
            confirmation: str = "",
            wait: bool = True,
        ) -> dict[str, Any]:
            """Listen to multiple chats without generating or sending replies."""
            return await runtime.submit(
                TaskType.WATCH_CHATS,
                {
                    "friends": friends,
                    "duration": duration,
                    "select_sender": False,
                    "dry_run": dry_run,
                },
                wait=wait,
                confirmation=confirmation,
            )

        @mcp.tool(name="wx_list_chat_sessions", annotations=READ_ONLY_TOOL, structured_output=True)
        async def wx_list_chat_sessions(
            dry_run: bool = True,
            confirmation: str = "",
            wait: bool = True,
        ) -> dict[str, Any]:
            """List chat session names visible in the main WeChat conversation list."""
            return await runtime.submit(
                TaskType.READ_CHAT_SESSIONS,
                {"dry_run": dry_run},
                wait=wait,
                confirmation=confirmation,
            )

        @mcp.tool(name="wx_read_visible_messages", annotations=READ_ONLY_TOOL, structured_output=True)
        async def wx_read_visible_messages(
            friend: str,
            dry_run: bool = True,
            confirmation: str = "",
            wait: bool = True,
        ) -> dict[str, Any]:
            """Read messages currently visible in a chat."""
            return await runtime.submit(
                TaskType.READ_VISIBLE_MESSAGES,
                {"friend": friend, "dry_run": dry_run},
                wait=wait,
                confirmation=confirmation,
            )

        @mcp.tool(name="wx_post_moments", annotations=WRITE_TOOL, structured_output=True)
        async def wx_post_moments(
            text: str,
            media_ids: list[str] | None = None,
            dry_run: bool = True,
            confirmation: str = "",
            wait: bool = True,
        ) -> dict[str, Any]:
            """Publish a Moments post using media_ids returned by wx_upload_media."""
            ids = media_ids or []
            if not text.strip() and not ids:
                raise ValueError("Moments text or at least one media_id is required")
            medias = await asyncio.to_thread(runtime.media_store.resolve_many, ids)
            return await runtime.submit(
                TaskType.POST_MOMENTS,
                {"text": text, "medias": medias, "dry_run": dry_run},
                wait=wait,
                confirmation=confirmation,
            )

        @mcp.tool(name="wx_get_task", annotations=READ_ONLY_TOOL, structured_output=True)
        async def wx_get_task(task_id: str) -> dict[str, Any]:
            """Get a queued WeChat task and its terminal result or error."""
            return await asyncio.to_thread(runtime.get_task, task_id)

        @mcp.tool(name="wx_read_recent_moments", annotations=READ_ONLY_TOOL, structured_output=True)
        async def wx_read_recent_moments(
            limit: int = 5,
            dry_run: bool = True,
            confirmation: str = "",
            wait: bool = True,
        ) -> dict[str, Any]:
            """Read recent visible Moments posts for post-delivery verification."""
            return await runtime.submit(
                TaskType.READ_RECENT_MOMENTS,
                {"limit": limit, "dry_run": dry_run},
                wait=wait,
                confirmation=confirmation,
            )

        @mcp.tool(name="wx_list_tasks", annotations=READ_ONLY_TOOL, structured_output=True)
        async def wx_list_tasks(limit: int = 20) -> dict[str, Any]:
            """List recent serialized WeChat tasks."""
            tasks = await asyncio.to_thread(runtime.list_tasks, limit)
            return {"tasks": tasks, "count": len(tasks)}
