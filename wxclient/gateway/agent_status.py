"""分身级接管状态上报（089 / docs/19 §2.1）——**上行**那一条。

回路是：招聘老师在界面上按下开关 → **先落本机、界面立刻变** → 再把这件事报给业务后端。
这个顺序不是为了快，是权威的划分（design §1.1）：

- **即时执行权在本机。** 接管的意思是「现在立刻别再发了」。它必须在断网、业务端 500、
  接口还没上线的时候照样成立——那道闸在 ``guard.py``，纯本地。
- **定序权威在业务端。** 谁先谁后由业务端的 ``version`` 说了算，本机不自己编版本号。

所以这里只有一条铁律：**上报失败绝不回滚本机状态**，唯独 ``409`` 例外——
那是业务端说「你手上那份过期了，当前真值是这个」，才以它为准。

★ 为什么把这两种失败分开（这是本模块最容易做错的一条）：
  - 网络失败也回滚 = 「我明明点了接管，分身还在发消息」；
  - ``409`` 不回滚 = 两边长期各说各话，企业端看到的和真机上跑的不是一回事。

★ 无心跳契约：只在变化时报一次。所以报不上去的那一次要**记着欠账**
  （``gateway_context_mode.reported_at`` 为空），重启后补报——服务端不会自己来问。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import structlog

from .backend_client import BackendEndpoint

_log = structlog.get_logger(__name__)

AGENT_STATUS_PATH = "/recruitment-agent/client/agent-status"

#: 本机的词 → 契约 §2.1 的 `status` 枚举。两套词表分属两端，映射写在这里这一处。
_STATUS_OF: dict[str, str] = {"take_over": "human_takeover", "delegated": "ai_managed"}
_MODE_OF: dict[str, str] = {v: k for k, v in _STATUS_OF.items()}


@dataclass(frozen=True)
class ReportOutcome:
    """一次上报的结局。三态，**不要压成布尔**——中间那一态才是重点。"""

    reported: bool
    """报成功了（本机欠账可以销掉）。"""
    rollback_to: str | None = None
    """★ 只有 409 才有值：业务端给出的当前真值，本机据此回滚。其余一律 None。"""
    rollback_version: int = 0
    reason: str = ""

    @property
    def should_retry(self) -> bool:
        """没报成、也没让回滚 → 是「还没送到」，欠着，下次接着报。"""
        return not self.reported and self.rollback_to is None


class AgentStatusReporter(BackendEndpoint):
    """把「这台分身现在归谁用」报给业务后端。**永不往上抛网络异常**。"""

    def __init__(self, *args: Any, clock: Callable[[], datetime] | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def report(
        self,
        mode: str,
        *,
        reason: str = "",
        expected_version: int = 0,
        occurred_at: str | None = None,
        event_id: str | None = None,
    ) -> ReportOutcome:
        """报一次。**调用方拿到 `reported=False` 时什么都别改**（除非给了 `rollback_to`）。"""
        status = _STATUS_OF.get(mode)
        if status is None:
            # 上游写错了，重试多少次都一样。不占欠账——欠账是留给「还没送到」的。
            return ReportOutcome(False, reason=f"不认识的接管状态：{mode or '(空)'}")
        if status == "human_takeover" and not reason.strip():
            # §2.1：`human_takeover` 必须带原因。少了会被 400 打回，本机先说清楚。
            return ReportOutcome(False, reason="接管必须写一句原因")
        if not self.configured:
            return ReportOutcome(False, reason="本机没配业务接口地址")

        body: dict[str, Any] = {
            "eventId": event_id or str(uuid.uuid4()),
            "status": status,
            "occurredAt": occurred_at or self._clock().isoformat(),
        }
        if reason.strip():
            body["reason"] = reason.strip()[:200]
        if expected_version > 0:
            body["expectedVersion"] = expected_version

        response = await self._send("PUT", f"{self._base_url}{AGENT_STATUS_PATH}", json=body)
        if response is None:
            # 网络炸 / 没登录 / 没配地址。**本机状态一动不动**，欠着下次再报。
            return ReportOutcome(False, reason="业务接口没回应")

        if response.status_code == 409:
            return self._conflict(response)
        if response.status_code >= 400:
            # 400/401/403/429 全归这一档：报不上去，但**本机说了算**（§4.3 明写
            # 401 会话撤销时「本机那道闸不动」）。429 尤其不能回滚——那只是连点。
            _log.warning("分身状态上报被拒", status=response.status_code)
            return ReportOutcome(False, reason=f"业务接口拒绝（HTTP {response.status_code}）")

        _log.info("分身级接管状态已上报", context_mode=mode)
        return ReportOutcome(True)

    def _conflict(self, response: Any) -> ReportOutcome:
        """★ 唯一该回滚的一档：企业端刚改过，响应体里带着当前真值。

        真值取不出来（响应体不是约定的形状）就**不回滚**——宁可两边先各说各话，
        也不能凭一个解析失败把招聘老师刚按下的接管撤掉。
        """
        try:
            details = (response.json() or {}).get("error", {}).get("details", {})
        except Exception:  # noqa: BLE001
            details = {}
        current = _MODE_OF.get(str((details or {}).get("agentStatus") or ""))
        if current is None:
            _log.warning("版本冲突但响应里没有当前真值，不回滚")
            return ReportOutcome(False, reason="业务端说状态已被改过，但没给出当前值")
        raw_version = (details or {}).get("version")
        version = int(raw_version) if isinstance(raw_version, int) and raw_version > 0 else 0
        _log.warning("分身状态被企业端改过，以业务端为准回滚", context_mode=current)
        return ReportOutcome(
            False,
            rollback_to=current,
            rollback_version=version,
            reason="这台分身的状态已被企业端改过，已按企业端的最新状态显示",
        )
