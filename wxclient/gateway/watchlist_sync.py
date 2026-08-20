"""监听名单同步（spec 085）：名单的权威在**业务后端**，这台机器按周期去取。

★ 归属变更（用户裁决 2026-08-20：「名单是 W 端给业务端，然后 W 端可以同步微信端」）。
在这之前名单存在 AI 端自己的库里、由 WS 推下来（`watchlist.PUSH_EVENT`）。那份必然是滞后副本：
该盯谁是业务侧的事实（谁在招聘漏斗里、谁归这位专员），而 AI 端只是推理层。滞后的代价很具体——
HR 在工作台圈定的候选人，在分身开口之前不在名单里，他要是**先开口**就没人接。

走的是 contacts 那条既有 W 面路子（`ContactDirectory`）：同一个 client token、同样失败只记账、
同样不挡网关启动。**但它和 contacts 不是一件事**：
- contacts = 手机号 ↔ 会话名的映射（这个人在微信里叫什么），只能有人；
- 名单 = 哪些会话轮得到自动回复，**群和人同表**（`watchlist.py` 顶部，用户裁决 2026-08-14），
  而群没有手机号、没有 worker_id，进不了 contacts 那张表。所以是两个接口，不是一个。

★ 拉不到 ≠ 名单是空的。名单是 fail-closed 白名单，空名单意味着**一个会话都不回**。
所以这里失败一律返回 None（「这轮没拿到」），由调用方沿用现有那份——绝不把 None 当成空名单。
"""

from __future__ import annotations

from typing import Any

import structlog

from .backend_client import BackendEndpoint

_log = structlog.get_logger(__name__)

WATCHLIST_PATH = "/recruitment-agent/client/watchlist"

#: 一份名单最多接受多少条。与 AI 端历史上的 `MAX_WATCHLIST_ENTRIES` 同值：
#: 白名单不该长到没边，真要群发请走人工分批。
MAX_ENTRIES = 500


def _clean_names(raw: Any) -> list[str] | None:
    """后端给的一列名字 → 去空去重保序。**不是 list 就返回 None**（缺字段沿用现值）。"""
    if not isinstance(raw, list):
        return None
    seen: set[str] = set()
    out: list[str] = []
    for item in raw:
        name = str(item or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
        if len(out) >= MAX_ENTRIES:
            _log.warning("监听名单超过上限，后面的丢弃", limit=MAX_ENTRIES)
            break
    return out


class WatchlistDirectory(BackendEndpoint):
    """从业务后端取监听名单。**永不往上抛**，拉不到就是拉不到。"""

    async def pull(self) -> dict[str, list[str]] | None:
        """取一份名单 → `{"only": [...], "never": [...]}`；拉不到返回 None。

        形状与 `Watchlist.apply_push` 的入参一致（少改一处解析）：缺哪个键就不动哪一边，
        与那边「AI 端只想改 never 时不必把 only 一起重发」的既有语义相同。
        """
        response = await self._send("GET", f"{self._base_url}{WATCHLIST_PATH}")
        if response is None:
            return None
        if response.status_code >= 400:
            _log.warning("监听名单接口被拒", status=response.status_code)
            return None
        try:
            body = response.json()
        except Exception:  # noqa: BLE001
            _log.warning("监听名单接口响应不是合法 JSON")
            return None
        if not isinstance(body, dict):
            _log.warning("监听名单接口响应形状不对")
            return None

        out: dict[str, list[str]] = {}
        for key in ("only", "never"):
            names = _clean_names(body.get(key))
            if names is not None:
                out[key] = names
        if not out:
            # 一个键都没有 = 这个响应什么也没说。当成没拿到，别拿它去覆盖现有名单。
            _log.warning("监听名单接口没给 only/never，按没拿到处理")
            return None
        _log.info("监听名单已同步", only=len(out.get("only", [])), never=len(out.get("never", [])))
        return out
