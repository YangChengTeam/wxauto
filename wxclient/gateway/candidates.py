"""候选人资料与 AI 状态回写（api2 §9）——闭环的最后一段。

回路是：业务后端投一条计划触发 → AI 端产出一条微信指令 → 这台电脑真去做 → 回执回到 AI 端 →
AI 端算出「这个人该往前走一格」→ 把那一格**投回来**（`op: update_candidate`）→ 这里落到后端。

**为什么是这台电脑发这个请求**：候选人资料/AI 状态是 `client` 面接口（api2 §3.2 已经把它
从 agent 面移走了），只有本机手上的 Client Token 调得动。AI 端既没有这把钥匙，也不该有。

**它不是微信动作**：不占任何微信频控配额、不受工作时间窗管——那些闸管的是「会不会打扰到
真人」，而这条请求只改一行业务数据。把它算进配额，等于让簿记去挤真正要发的消息。

`token` 是取值函数不是字符串，同 `contacts.py`：Token 运行中会变，每次现取。
"""

from __future__ import annotations

from typing import Any

import structlog

from .backend_client import BackendEndpoint

_log = structlog.get_logger(__name__)

CANDIDATES_PATH = "/recruitment-agent/client/candidates"

#: api2 §9.2 的六态。不认识的值一律不发——后端会 400，而我们这边还会以为写过了。
AI_STATUSES = frozenset(
    {"not_started", "ai_managed", "human_takeover", "paused", "blocked", "completed"}
)
#: §9.2：这两态必须带原因。
_REASON_REQUIRED = frozenset({"human_takeover", "blocked"})
#: §9.1：`profile` 里我们只写这三样。别的字段由人在网页上改，分身不碰。
_PROFILE_KEYS = frozenset({"wechatName", "wechatAdded", "remark"})


class CandidateUpdateError(Exception):
    """请求本身不成立（缺 ID、状态不认识、缺必填原因）。调用方据此回 `rejected`。"""


class CandidateDirectory(BackendEndpoint):
    """候选人回写。**永不往上抛网络异常**：这条链断了不该影响已经做完的微信动作。"""

    @property
    def configured(self) -> bool:
        return bool(self._base_url)

    async def update(
        self,
        candidate_id: str,
        *,
        profile: dict[str, Any] | None = None,
        ai_status: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """写一次。成功回 `{"updated": True, "version": …}`；失败抛或回 `{"updated": False, …}`。

        形状不对（缺 ID / 状态不认识 / 缺必填原因）抛 `CandidateUpdateError`——那是**上游写错了**，
        重试多少次都一样，要当场回 `rejected` 说清楚，不是默默吞掉。
        """
        candidate_id = (candidate_id or "").strip()
        if not candidate_id:
            raise CandidateUpdateError("没说要改哪个候选人")

        body: dict[str, Any] = {}
        clean_profile = {k: v for k, v in (profile or {}).items() if k in _PROFILE_KEYS}
        if clean_profile:
            body["profile"] = clean_profile
        if ai_status:
            status = str(ai_status.get("status") or "").strip()
            if status not in AI_STATUSES:
                raise CandidateUpdateError(f"不认识的 AI 状态：{status or '(空)'}")
            reason = str(ai_status.get("reason") or "").strip()
            if status in _REASON_REQUIRED and not reason:
                raise CandidateUpdateError(f"{status} 必须说明原因")
            entry: dict[str, Any] = {"status": status}
            if reason:
                entry["reason"] = reason[:200]
            # `expectedVersion` 暂不传：client 面还没有候选人读接口，本机拿不到当前 version
            # （api2 §9.2 的乐观锁在这条路上因此是空的）。后端 `expectedVersion` 是选填，
            # 不传就是无条件更新——这一点如实记在这里，别当成已经有并发保护。
            body["aiRecruitment"] = entry
        if not body:
            raise CandidateUpdateError("这次请求没有要改的字段")

        if not self.configured:
            return {"updated": False, "reason": "本机没配业务接口地址"}

        result = await self._request(candidate_id, body)
        if result is None:
            return {"updated": False, "reason": "业务接口没回应"}
        version = None
        ai = result.get("aiRecruitment")
        if isinstance(ai, dict):
            version = ai.get("version")
        _log.info(
            "候选人状态已回写",
            ai_status=(body.get("aiRecruitment") or {}).get("status"),
            wechat_added=clean_profile.get("wechatAdded"),
        )
        return {"updated": True, **({"version": version} if version is not None else {})}

    async def _request(self, candidate_id: str, body: dict[str, Any]) -> dict[str, Any] | None:
        # candidate_id 是后端给的 UUID，直接拼进路径前先挡掉分隔符——多一道，别让它跑出这条路径。
        safe_id = candidate_id.replace("/", "").replace("?", "").replace("#", "")
        response = await self._send("PATCH", f"{self._base_url}{CANDIDATES_PATH}/{safe_id}", json=body)
        if response is None:
            return None

        if response.status_code == 409:
            # §9.2 的乐观锁冲突。两端同时在改这个人——本机没有 version 可比，如实报，不重试。
            _log.warning("候选人回写撞上版本冲突")
            return None
        if response.status_code >= 400:
            _log.warning("候选人回写被拒", status=response.status_code)
            return None
        try:
            parsed = response.json()
        except Exception:  # noqa: BLE001
            return {}
        return parsed if isinstance(parsed, dict) else {}
