"""共享运行时：一个微信账号对应一个任务队列 + 一个执行器 + 一套动作授权。

原先叫 ``mcp_server.runtime.WxMcpRuntime``，但它从来不是 MCP 的东西——MCP 只是它
最早的调用方。网关（``wxclient.gateway``）同样依赖它。MCP 入口下线后把它提到包级，
名字也改回它真正的职责。

**动作授权是红线**（``CLAUDE.md`` §5.1 真实动作三重确认）：
``dry_run=false`` + ``allow_real_actions`` + 确认短语，三者缺一不可。
``RuntimeSettings`` 刻意**不**并进 ``wxclient.config.Settings``——后者可由 ``--config``
指定的 JSON 文件加载，把开关放进去等于允许「改个配置文件就能真发消息」。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict

from wxclient.config import Settings, load_settings
from wxclient.queue import OperationWorker, Task, TaskStatus, TaskStore, TaskType
from wxclient.wechat_api import WeChatApi

REAL_ACTION_CONFIRMATION = "EXECUTE_REAL_ACTION"


_TERMINAL_STATUSES = {
    TaskStatus.SUCCEEDED,
    TaskStatus.FAILED,
    TaskStatus.BLOCKED,
    TaskStatus.CANCELLED,
}


class RuntimeSettings(BaseSettings):
    """动作授权与任务等待参数。与「微信运行参数」(``Settings``) 分开，见模块说明。"""

    model_config = SettingsConfigDict(env_prefix="WXCLIENT_RUNTIME_", extra="ignore")

    allow_real_actions: bool = False
    confirmation_phrase: str = REAL_ACTION_CONFIRMATION
    task_timeout_seconds: float = 180.0

    def validate_for_start(self) -> None:
        if self.task_timeout_seconds <= 0:
            raise ValueError("任务等待超时必须为正数")
        if not self.confirmation_phrase:
            raise ValueError("确认短语不能为空")


class WeChatRuntime:
    """持有全进程唯一的微信任务队列与执行器；所有入口共用一个。"""

    def __init__(
        self,
        wechat_settings: Settings,
        runtime_settings: RuntimeSettings,
        store: TaskStore | None = None,
        api: WeChatApi | None = None,
        worker: OperationWorker | None = None,
    ) -> None:
        self.wechat_settings = wechat_settings
        self.runtime_settings = runtime_settings
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
            poll_interval=0.1,
        )

    @classmethod
    def from_config(
        cls,
        config_path: str | Path | None,
        runtime_settings: RuntimeSettings,
    ) -> WeChatRuntime:
        return cls(load_settings(config_path), runtime_settings)

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
        # 调用方未必走 CLI 生命周期；这里 start 是幂等的，保证 wait=False 的任务也有消费者。
        self.start()
        task = self.store.create_task(
            task_type,
            payload,
            self.wechat_settings.default_account_id,
        )
        if not wait:
            return self.task_to_dict(task)
        timeout = timeout_seconds or self.runtime_settings.task_timeout_seconds
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
        if not self.runtime_settings.allow_real_actions:
            raise PermissionError("真实微信操作未启用；启动时须显式加 --allow-real-actions")
        if confirmation != self.runtime_settings.confirmation_phrase:
            raise PermissionError(
                f"真实微信操作需要确认短语 confirmation={self.runtime_settings.confirmation_phrase!r}"
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
