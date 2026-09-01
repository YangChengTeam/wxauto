"""候选人会话名同步（api2 §4）——本机微信里的**备注名**与业务后端之间的两条路。

「会话名」= 这位专员在自己微信里给候选人设的备注，也是本机逐字寻址那个人时用的名字。
它和候选人档案里的 `wechatName`（对方昵称）、`wechat`（微信号）是三样东西，别混。

- **上报**（`POST /client/contacts`）：申请发出去、对方通过、或备注名变了 → 告诉后端。
  后端拿 `number` 归位候选人；归不了位也照收不打回（`matched: false`），映射不丢。
- **同步**（`GET /client/contacts`）：重装 / 换机 / 启动增量 → 把这台机器该知道的映射拉回来，
  免得同一个人在新机器上被起了第二个备注名。

两条都**只同步映射与好友状态，不碰聊天内容**。凭 Client Token 走，后端只认它自己那个员工的
数据——`tenantId` / `userId` / `agentId` 一律不传（传了也扩不了范围，api2 §4.2）。

`token` 是**取值函数**不是字符串，与 `ticket.py` 同一理由：Token 会在运行中变（重登、被撤销），
每次现取才不会拿着一把作废的钥匙一直敲门。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import structlog

from .backend_client import BackendEndpoint, error_of, error_shape

_log = structlog.get_logger(__name__)

CONTACTS_PATH = "/recruitment-agent/client/contacts"

#: api2 §4.1 的 `status` 两档。与 `commands.py` 的 `friend_state` 逐字一致（那边产出、这边上报）。
STATUS_REQUESTED = "requested"
STATUS_ACCEPTED = "accepted"
_STATUSES = frozenset({STATUS_REQUESTED, STATUS_ACCEPTED})

#: §4.2 的分页上限。再大后端也只给 200。
MAX_LIMIT = 200
#: 拉全量时最多翻几页——防后端游标不收敛把启动卡死。
_MAX_PAGES = 50


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class ContactDirectory(BackendEndpoint):
    """会话名的上报与同步。**永不往上抛**：这条链断了不该影响已经做完的微信动作。"""

    # ── 上报 ─────────────────────────────────────────────────────

    async def report(
        self,
        *,
        number: str,
        remark: str,
        status: str,
        profile_name: str = "",
        command_id: str = "",
        occurred_at: str = "",
    ) -> dict[str, Any] | None:
        """报一条会话名。返回后端的 `{matched, worker_id}`；报不出去返回 None（只记账）。

        `number` 和 `remark` 都是必填（api2 §4.1）——缺了就别发：后端归不了位，
        而我们这边还会以为报过了。
        """
        number = (number or "").strip()
        remark = (remark or "").strip()
        if status not in _STATUSES:
            _log.warning("会话名上报被跳过：status 不认识", status=status)
            return None
        if not number or not remark:
            # 常见于 AI 端下的 add_friend 没给 remark：不编一个，如实跳过。
            _log.info("会话名上报被跳过：缺 number 或 remark", has_number=bool(number),
                      has_remark=bool(remark))
            return None

        payload: dict[str, Any] = {
            "number": number,
            "remark": remark[:120],          # §4.1：最长 120
            "status": status,
            "occurred_at": occurred_at or _now_iso(),
        }
        if profile_name:
            payload["profile_name"] = profile_name[:120]
        if command_id:
            payload["command_id"] = command_id

        body = await self._request("POST", CONTACTS_PATH, json=payload)
        if body is None:
            return None
        matched = bool(body.get("matched"))
        _log.info("会话名已上报", status=status, matched=matched)
        return {"matched": matched, "worker_id": body.get("worker_id")}

    # ── 同步 ─────────────────────────────────────────────────────

    async def pull(self, *, updated_since: str = "", limit: int = MAX_LIMIT) -> list[dict[str, Any]]:
        """拉回本员工的会话名映射（自动翻页）。拉不动返回已经拿到的那部分，不抛。

        `cursor` 是后端给的 opaque 串，**原样回传**——api2 §4.2 明确说客户端不许解析或自己拼。
        """
        out: list[dict[str, Any]] = []
        params: dict[str, Any] = {"limit": max(1, min(int(limit or MAX_LIMIT), MAX_LIMIT))}
        if updated_since:
            params["updated_since"] = updated_since
        for _ in range(_MAX_PAGES):
            body = await self._request("GET", CONTACTS_PATH, params=params)
            if body is None:
                break
            rows = body.get("contacts")
            if isinstance(rows, list):
                out.extend(row for row in rows if isinstance(row, dict))
            cursor = body.get("next_cursor")
            if not body.get("has_more") or not isinstance(cursor, str) or not cursor:
                break
            params["cursor"] = cursor
        _log.info("会话名同步完成", count=len(out))
        return out

    # ── 发请求 ───────────────────────────────────────────────────

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any] | None:
        response = await self._send(method, f"{self._base_url}{path}", **kwargs)
        if response is None:
            return None
        if response.status_code >= 400:
            # 原因码一起记：光有 409 看不出是「这台设备没绑微信账号」还是别的
            # （2026-08-31 那次冻住的会话名同步就是 WECHAT_ACCOUNT_NOT_BOUND）。
            code, message = error_of(response)
            _log.warning(
                "会话名接口被拒", path=path, status=response.status_code,
                错误码=code, 说明=message,
                # 码取不出来时把响应体的形状记一笔，否则这条日志等于只有一个 409。
                **({} if code else {"响应形状": error_shape(response)}),
            )
            return None
        try:
            body = response.json()
        except Exception:  # noqa: BLE001
            _log.warning("会话名接口响应不是合法 JSON", path=path)
            return None
        return body if isinstance(body, dict) else None
