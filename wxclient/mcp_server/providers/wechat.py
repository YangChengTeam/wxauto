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
            """Upload a supported media/document file and return a media_id."""
            return await asyncio.to_thread(
                runtime.media_store.save_base64,
                filename,
                content_base64,
            )

        @mcp.tool(name="wx_search_contacts", annotations=READ_ONLY_TOOL, structured_output=True)
        async def wx_search_contacts(
            query: str,
            limit: int = 10,
            wait: bool = True,
        ) -> dict[str, Any]:
            """Search existing WeChat contacts, groups, and accounts without sending or adding."""
            normalized_query = query.strip()
            if not normalized_query:
                raise ValueError("Search query cannot be empty")
            return await runtime.submit(
                TaskType.SEARCH_CONTACTS,
                {"query": normalized_query, "limit": min(max(int(limit), 1), 50)},
                wait=wait,
            )

        @mcp.tool(name="wx_get_contact_profile", annotations=READ_ONLY_TOOL, structured_output=True)
        async def wx_get_contact_profile(
            friend: str,
            wait: bool = True,
        ) -> dict[str, Any]:
            """Get the profile of an existing WeChat contact without sending anything."""
            normalized_friend = friend.strip()
            if not normalized_friend:
                raise ValueError("Contact name cannot be empty")
            return await runtime.submit(
                TaskType.GET_CONTACT_PROFILE,
                {"friend": normalized_friend},
                wait=wait,
            )

        @mcp.tool(name="wx_list_group_members", annotations=READ_ONLY_TOOL, structured_output=True)
        async def wx_list_group_members(
            group: str,
            wait: bool = True,
        ) -> dict[str, Any]:
            """List member names in an existing WeChat group."""
            normalized_group = group.strip()
            if not normalized_group:
                raise ValueError("Group name cannot be empty")
            return await runtime.submit(
                TaskType.LIST_GROUP_MEMBERS,
                {"group": normalized_group},
                wait=wait,
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

        @mcp.tool(name="wx_send_files", annotations=WRITE_TOOL, structured_output=True)
        async def wx_send_files(
            friend: str,
            media_ids: list[str],
            text: str = "",
            dry_run: bool = True,
            confirmation: str = "",
            wait: bool = True,
        ) -> dict[str, Any]:
            """Send uploaded files to a friend or group through the serialized queue."""
            if not friend.strip():
                raise ValueError("Recipient cannot be empty")
            if not media_ids:
                raise ValueError("At least one media_id is required")
            files = await asyncio.to_thread(runtime.media_store.resolve_many, media_ids)
            return await runtime.submit(
                TaskType.SEND_FILES,
                {
                    "friend": friend.strip(),
                    "files": files,
                    "text": text,
                    "dry_run": dry_run,
                },
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

        @mcp.tool(name="wx_get_unread_messages", annotations=READ_ONLY_TOOL, structured_output=True)
        async def wx_get_unread_messages(wait: bool = True) -> dict[str, Any]:
            """Summarize unread sessions without opening chats or marking them read."""
            return await runtime.submit(TaskType.GET_UNREAD_MESSAGES, {}, wait=wait)

        @mcp.tool(name="wx_read_chat_history", annotations=READ_ONLY_TOOL, structured_output=True)
        async def wx_read_chat_history(
            friend: str,
            limit: int = 50,
            wait: bool = True,
        ) -> dict[str, Any]:
            """Read recent chat history for one friend or group."""
            normalized_friend = friend.strip()
            if not normalized_friend:
                raise ValueError("Chat name cannot be empty")
            return await runtime.submit(
                TaskType.READ_CHAT_HISTORY,
                {"friend": normalized_friend, "limit": min(max(int(limit), 1), 200)},
                wait=wait,
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
            medias = await asyncio.to_thread(runtime.media_store.resolve_many_for_moments, ids)
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
