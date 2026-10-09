"""候选人级接管/托管同步（091 / 业务端 v1.5 §4；`docs/19` v2 §1）。

回路是：招聘老师在名单里按下某个人后面那个小按钮 → **先落本机、界面立刻变** →
`action=request` 告诉业务后端 → 应用到本机 → `action=report` 回报真的应用了。

这个顺序不是为了快，是权威的划分：

- **即时执行权在这台电脑上。** 接管的意思是「现在立刻别再发给他了」。它必须在断网、
  业务端 500、接口还没上线的时候照样成立——那道闸在 ``guard.py``，纯本地、按会话名判。
- **定序权威在业务端。** 谁先谁后由业务端的落库顺序说了算，本机不自己编版本号。
  ★ 0.1.113 起 ``runner`` **不再带** ``expectedVersion``：同步名单不带版本，本机记的那个
  必然过期，带上去只会撞出假的版本冲突（见 ``runner._sync_candidate_mode``）。

所以这里只有一条铁律：**同步失败绝不回滚本机状态**，唯独 ``409`` 例外——
那是业务端说「你手上那份过期了」，才以它为准重来。

★ 为什么把这两种失败分开（本模块最容易做错的一条）：
  - 网络失败也回滚 = 「我明明点了接管，分身还在发消息」；
  - ``409`` 不回滚 = 两边长期各说各话，企业端看到的和真机上跑的不是一回事。

★ ``requestedMode`` 与 ``appliedMode`` **不许合并**（v1.5 §3.2）：``request`` 只是
  「业务端收下了这个请求」，只有 ``report`` 且 ``result=applied`` 才代表这台电脑真的照做了。
  少了那一步，业务端那份 ``syncStatus`` 永远停在 ``pending``，自动动作也不会被释放。

★ ``agentState=null`` **不是失败**（v1.5 §4.3 兜底那段）：历史数据里候选人任务还没初始化时
  它就是空的，账本照样写进去了。以 ``aiRecruitment.status/version`` 为准，不许回滚。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import structlog

from .backend_client import BackendEndpoint
from .sessions import AI_MANAGED, CANDIDATE_MODES, HUMAN_TAKEOVER

_log = structlog.get_logger(__name__)

#: v1.5 §4：路径里带 candidateId，不带 planId——归属由服务端按 Client Token 自己解析。
CANDIDATE_MODE_PATH = "/recruitment-agent/client/candidates/{candidate_id}/conversation-mode"

#: §4.1：接管必须写原因，1～500 字。
MAX_REASON = 500


@dataclass(frozen=True)
class ModeOutcome:
    """一次同步的结局。三态，**不要压成布尔**——中间那一态才是重点。

    ``accepted`` 假、``conflict_version`` 空 → 「还没送到」，本机不动，欠着下次再报。
    """

    accepted: bool
    """业务端收下了（`request`）或记下了（`report`）。"""
    version: int = 0
    """服务端 `aiRecruitment.version`，下一次 PUT 当 `expectedVersion`。"""
    mode_version: int = 0
    """服务端 `modeVersion`，`action=report` 要原样回传。"""
    trigger_id: str = ""
    """服务端 `triggerId`，同上。客户端**不解析它**（v1.5 §4.3）。"""
    mode: str = ""
    """服务端认定的当前 `aiRecruitment.status`。"""
    conflict_version: int = 0
    """★ 只有 409 版本冲突才有值：业务端手上那份的版本，本机据此重新取数再来一次。"""
    local_reject: bool = False
    """这一条**根本没出过门**（参数不对、这个会话还没对上候选人）。重试多少次都一样。"""
    reason: str = ""

    @property
    def should_retry(self) -> bool:
        """没成、不是版本冲突、也不是本机自己拦下的 → 「还没送到」，欠着，下次接着报。

        ★ 三态要分得开。把本机拦下的那一档也算成欠账，就会拿一条必然被 400 打回的请求
          按退避表一直撞墙，而且欠账表越攒越长——那不是「网络还没好」，是写错了。
        """
        return not self.accepted and self.conflict_version == 0 and not self.local_reject


class CandidateModeClient(BackendEndpoint):
    """候选人级接管/托管同步。**永不往上抛网络异常**。"""

    def __init__(self, *args: Any, clock: Callable[[], datetime] | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # ───────────────────────── action=request ─────────────────────────

    async def request(
        self,
        candidate_id: str,
        mode: str,
        *,
        reason: str = "",
        expected_version: int = 0,
        occurred_at: str | None = None,
        event_id: str | None = None,
    ) -> ModeOutcome:
        """设置某个候选人的档位。**拿到 `accepted=False` 时本机什么都别改。**"""
        if mode not in CANDIDATE_MODES:
            # 上游写错了，重试多少次都一样。不占欠账——欠账是留给「还没送到」的。
            return ModeOutcome(False, local_reject=True, reason=f"不认识的接管状态：{mode or '(空)'}")
        if mode == HUMAN_TAKEOVER and not reason.strip():
            # §4.1：`human_takeover` 的 `reason` 必填。少了会被 400 打回，本机先说清楚。
            return ModeOutcome(False, local_reject=True, reason="接管必须写一句原因")
        if not candidate_id.strip():
            # 没对上号的会话（同步名单里还没有他）。本机闸照样成立，但这条同步无从谈起。
            return ModeOutcome(False, local_reject=True, reason="这个会话还没对上劳小智里的候选人")

        body: dict[str, Any] = {
            "action": "request",
            "eventId": event_id or str(uuid.uuid4()),
            "mode": mode,
            "occurredAt": occurred_at or self._clock().isoformat(),
        }
        if reason.strip():
            body["reason"] = reason.strip()[:MAX_REASON]
        if expected_version > 0:
            body["expectedVersion"] = expected_version
        return await self._put(candidate_id, body, what="接管请求")

    # ────────────────────────── action=report ──────────────────────────

    async def report(
        self,
        candidate_id: str,
        mode: str,
        *,
        trigger_id: str,
        mode_version: int,
        applied: bool = True,
        error_code: str = "",
        error_message: str = "",
        occurred_at: str | None = None,
        event_id: str | None = None,
    ) -> ModeOutcome:
        """回报**本机真的应用了没有**（v1.5 §4.3）。

        ★ **当前不调用。** 业务端 2026-08-26 把 `action=request` 改成收到就直接
          `appliedMode = requestedMode` / `syncStatus = 'synced'`，并明确「不用处理 report」。
          接口那一侧还在，契约也还在，所以这个方法留着；真要再打开这一腿，
          调用点在 `runner._sync_candidate_mode`。

        ★ `triggerId` / `modeVersion` 原样回传，不解析、不重算——服务端据此找到本次请求
          影响的那几个计划分配。三样对不上会被 409 `..._REPORT_CONFLICT` 打回。
        """
        if mode not in CANDIDATE_MODES:
            return ModeOutcome(False, local_reject=True, reason=f"不认识的接管状态：{mode or '(空)'}")
        if not candidate_id.strip() or not trigger_id.strip():
            return ModeOutcome(False, local_reject=True, reason="缺 candidateId 或 triggerId，这条回报报不出去")

        body: dict[str, Any] = {
            "action": "report",
            "eventId": event_id or str(uuid.uuid4()),
            "triggerId": trigger_id,
            "modeVersion": int(mode_version),
            "mode": mode,
            "result": "applied" if applied else "failed",
            "occurredAt": occurred_at or self._clock().isoformat(),
        }
        if not applied:
            # §4.3：失败必须给错误码；说明要脱敏——**不许**把聊天原文、控件树、票据塞进来。
            body["errorCode"] = (error_code or "CLIENT_APPLY_FAILED")[:80]
            if error_message.strip():
                body["errorMessage"] = error_message.strip()[:500]
        return await self._put(candidate_id, body, what="接管回报")

    # ────────────────────────────── 内部 ──────────────────────────────

    async def _put(self, candidate_id: str, body: dict[str, Any], *, what: str) -> ModeOutcome:
        if not self.configured:
            return ModeOutcome(False, local_reject=True, reason="本机没配业务接口地址")
        path = CANDIDATE_MODE_PATH.format(candidate_id=quote(candidate_id.strip(), safe=""))
        response = await self._send("PUT", f"{self._base_url}{path}", json=body)
        if response is None:
            # 网络炸 / 没登录 / 没配地址。**本机状态一动不动**，欠着下次再报。
            return ModeOutcome(False, reason="业务接口没回应")

        if response.status_code == 409:
            return self._conflict(response, what=what)
        if response.status_code == 404:
            # §6 `CANDIDATE_NOT_ASSIGNED`：这个人不在该分身的有效招聘计划里（或计划已关闭）。
            # 文档要求**停止同步该事件**——重试多少次结果都一样，所以不占欠账。
            # 但**本机接管照样成立**：招聘老师按了就是按了，不能因为对不上计划就把闸放开。
            code = self._error_code(response)
            _log.warning("候选人不在有效招聘计划里", what=what, code=code or None)
            return ModeOutcome(
                False,
                local_reject=True,
                reason="这个候选人不在当前招聘计划里（或计划已关闭），业务端不收；这台电脑先自己生效了",
            )
        if response.status_code >= 400:
            # 400/401/403/410/429 全归这一档：同步不上去，但**本机说了算**。
            # 429 尤其不能回滚——那只是连点。
            _log.warning("候选人接管同步被拒", what=what, status=response.status_code)
            return ModeOutcome(False, reason=f"业务接口拒绝（HTTP {response.status_code}）")

        return self._accepted(response, what=what)

    @staticmethod
    def _error_code(response: Any) -> str:
        """从统一错误信封里取 `error.code`；取不到就空串（形状不对不影响判断）。"""
        try:
            payload = response.json() or {}
        except Exception:  # noqa: BLE001
            return ""
        error = payload.get("error")
        return str(error.get("code") or "") if isinstance(error, dict) else ""

    def _accepted(self, response: Any, *, what: str) -> ModeOutcome:
        """成功。响应形状解析不出来也**算成功**——它已经落到业务端账本上了。"""
        try:
            payload = response.json() or {}
        except Exception:  # noqa: BLE001
            payload = {}
        ai = payload.get("aiRecruitment") if isinstance(payload.get("aiRecruitment"), dict) else {}
        status = str((ai or {}).get("status") or "")
        _log.info("候选人接管已同步", what=what, mode=status or None)
        return ModeOutcome(
            True,
            version=_int(ai.get("version") if ai else None),
            mode_version=_int(payload.get("modeVersion") or (ai or {}).get("modeVersion")),
            trigger_id=str(payload.get("triggerId") or (ai or {}).get("triggerId") or ""),
            mode=status if status in CANDIDATE_MODES else "",
        )

    def _conflict(self, response: Any, *, what: str) -> ModeOutcome:
        """★ 唯一该「以业务端为准」的一档：企业端刚改过，本机要重新取数再来一次。

        ★ 和分身级那套的差别：这里**不直接回滚成某个值**。v1.5 §6 明写版本冲突时
          「查询最新候选人状态，由用户基于最新版本重新操作」——响应里那个
          `details.currentVersion` 只是版本号，不是「现在是接管还是托管」。
          凭它猜一个档位写回本机，就是拿一个自己编的值覆盖招聘老师刚按下的那一下。
        """
        try:
            details = (response.json() or {}).get("error", {}).get("details", {})
        except Exception:  # noqa: BLE001
            details = {}
        code = self._error_code(response)
        if code == "PLAN_TYPE_NOT_SUPPORTED":
            # 不是「状态变了」，是这个人所在的计划根本不收接管/托管。重试多少次都一样。
            _log.warning("候选人所在计划不支持接管", what=what, code=code)
            return ModeOutcome(
                False,
                local_reject=True,
                reason="只有招聘任务支持接管/托管，业务端不收；这台电脑先自己生效了",
            )
        version = _int((details or {}).get("currentVersion"))
        _log.warning("候选人接管状态已被改过", what=what, code=code or None,
                     current_version=version or None)
        return ModeOutcome(
            False,
            # 冲突版本至少是 1：0 会被 `should_retry` 当成「还没送到」，欠账越攒越多。
            conflict_version=version or 1,
            # ★ 别说「被企业端改过」：业务端别的流程也会推进版本，现场没人改过却这么说，是误导。
            reason="没同步上：业务端的状态刚变过，等名单同步后再点一次",
        )


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


__all__ = ["AI_MANAGED", "CANDIDATE_MODE_PATH", "HUMAN_TAKEOVER", "CandidateModeClient", "ModeOutcome"]
