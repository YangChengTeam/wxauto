from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from wxclient.config import Settings, load_settings
from wxclient.queue import OperationWorker, Task, TaskStatus, TaskStore, TaskType
from wxclient.wechat_api import WeChatApi

from .config import McpServerSettings
from .media import MediaStore


_TERMINAL_STATUSES = {
    TaskStatus.SUCCEEDED,
    TaskStatus.FAILED,
    TaskStatus.BLOCKED,
    TaskStatus.CANCELLED,
}


class WxMcpRuntime:
    """Owns the single WeChat worker used by all MCP tool providers."""

    def __init__(
        self,
        wechat_settings: Settings,
        mcp_settings: McpServerSettings,
        store: TaskStore | None = None,
        api: WeChatApi | None = None,
        worker: OperationWorker | None = None,
    ) -> None:
        self.wechat_settings = wechat_settings
        self.mcp_settings = mcp_settings
        self.store = store or TaskStore(wechat_settings.resolved_database_path)
        self.api = api or WeChatApi(
            dry_run=wechat_settings.dry_run,
            wechat_path=wechat_settings.wechat_path,
            wechat_driver_order=wechat_settings.wechat_driver_order,
            omni_visual_rpa_config=wechat_settings.omni_visual_rpa_config,
        )
        self.worker = worker or OperationWorker(
            self.store,
            self.api,
            settings=wechat_settings,
            poll_interval=0.1,
        )
        upload_dir = mcp_settings.upload_dir or wechat_settings.data_dir / "mcp_uploads"
        self.media_store = MediaStore(upload_dir, mcp_settings.upload_max_bytes)

    @classmethod
    def from_config(
        cls,
        config_path: str | Path | None,
        mcp_settings: McpServerSettings,
    ) -> "WxMcpRuntime":
        return cls(load_settings(config_path), mcp_settings)

    def start(self) -> None:
        self.worker.start()

    def stop(self) -> None:
        self.worker.stop(wait=True, timeout=10.0)

    async def submit(
        self,
        task_type: TaskType,
        payload: dict[str, Any],
        *,
        wait: bool = True,
        timeout_seconds: float | None = None,
        confirmation: str = "",
    ) -> dict[str, Any]:
        self._authorize_payload(payload, confirmation)
        # Imported ASGI hosts may not use our CLI lifecycle. Starting here is
        # idempotent and ensures wait=False tasks still have a live consumer.
        self.start()
        task = self.store.create_task(
            task_type,
            payload,
            self.wechat_settings.default_account_id,
        )
        if not wait:
            return self.task_to_dict(task)
        timeout = timeout_seconds or self.mcp_settings.task_timeout_seconds
        return await asyncio.to_thread(self.wait_for_task, task.id, timeout)

    def wait_for_task(self, task_id: str, timeout_seconds: float) -> dict[str, Any]:
        deadline = time.monotonic() + max(0.1, timeout_seconds)
        while time.monotonic() < deadline:
            task = self.store.get(task_id)
            if task is None:
                raise ValueError(f"Unknown task: {task_id}")
            if task.status in _TERMINAL_STATUSES:
                return self.task_to_dict(task)
            time.sleep(0.1)
        task = self.store.get(task_id)
        result = self.task_to_dict(task) if task else {"task_id": task_id}
        result["timed_out"] = True
        return result

    def get_task(self, task_id: str) -> dict[str, Any]:
        task = self.store.get(task_id)
        if task is None:
            raise ValueError(f"Unknown task: {task_id}")
        return self.task_to_dict(task)

    def list_tasks(self, limit: int = 20) -> list[dict[str, Any]]:
        safe_limit = min(max(int(limit), 1), 100)
        return [self.task_to_dict(task) for task in self.store.list_tasks(limit=safe_limit)]

    def _authorize_payload(self, payload: dict[str, Any], confirmation: str) -> None:
        if bool(payload.get("dry_run", True)):
            return
        if not self.mcp_settings.allow_real_actions:
            raise PermissionError(
                "Real WeChat actions are disabled; start the MCP server with --allow-real-actions"
            )
        if confirmation != self.mcp_settings.confirmation_phrase:
            raise PermissionError(
                f"Real WeChat actions require confirmation={self.mcp_settings.confirmation_phrase!r}"
            )

    @staticmethod
    def task_to_dict(task: Task) -> dict[str, Any]:
        return {
            "task_id": task.id,
            "type": task.type.value,
            "status": task.status.value,
            "account_id": task.account_id,
            "attempts": task.attempts,
            "created_at": task.created_at,
            "updated_at": task.updated_at,
            "last_error": task.last_error,
            "result": task.result,
        }
