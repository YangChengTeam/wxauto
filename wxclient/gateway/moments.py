"""本机朋友圈巡看：和读消息轮询同形，**只碰上次看过之后新出的动态**（用户 2026-09-08 裁决）。

用户原话：「本地做一个，和轮询消息一样，只对最新的朋友圈进行」。
101（点赞）与 102（评论）都是**业务端计划触发**的形态：业务端一天发一次，AI 端或网关
把「今天的」动态整趟刷一遍。它们发现不了「刚发的」——两次触发之间的新动态要等下一次计划。
本循环补的就是这个洞：本机按自己的间隔开一趟朋友圈，翻到**上一趟看到的第一条**就停，
中间那些就是新出的。

**做什么、不做什么**
· 点赞：新动态当场点，配额与节拍走「朋友圈点赞」那两档（`moments_like` 20/天、两趟 ≥600s），
  和计划触发的 `like_moments` **共用同一本账**——两条路合起来也不会超过专员设的上限。
· 评论：本机写不出有分寸的评论，也不该由本机写（102 裁决：评论文字由 AI 端按素材组织）。
  所以本循环只把新动态**交给 runner 自触发 AI 端**（`on_new_posts`），AI 端照 102 那条路
  回一条 `comment_moments` 指令，网关照旧走能力闸、配额、节拍、同一条只评一次的台账——
  评论那一侧**一个字都不新写**。
· 游标：`gateway_moment_cursor` 记上一趟第一条的键（正文去空白；纯图动态用去掉时间戳的整段）。
  只在这趟**成功**时前移——失败时不动，下一趟把这批再看一遍，宁重不漏。
  第一趟没有游标：只收今天的、最多 `read_limit` 条，从此往后只看新的。

**为什么默认关**：每趟都要开一次朋友圈窗口，是风控看得见的动作；和「读消息轮询」不同，
朋友圈不是非看不可。开不开、多久一趟，是专员的知情选择（配置中心「朋友圈巡看间隔」）。

**风控红线不动**（[[wechat-rate-control]]）：本循环自己的间隔只是「多久看一眼」，
点赞节拍仍由 `pace_write("like_moments")` 守，且**先占额度再过节拍**；被节拍挡下就这趟只看不点，
额度当场还回去。
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timezone
from typing import Any

import structlog

from wxclient.queue import TaskType

from .guard import ACTION_MOMENTS_LIKE, OutboundGuard, Reservation, within_active_hours
from .pacing import MIN_MOMENTS_POLL_SECONDS, jittered
from .sessions import GatewayStore

_log = structlog.get_logger(__name__)

#: 一趟最多收几条新动态交给 AI 端（AI 端那边 `MOMENTS_READ_LIMIT` 也是 10，多了也不看）。
READ_LIMIT = 10
#: 一趟最多点几个赞。上限再由「朋友圈点赞」日配额减量。
LIKE_LIMIT = 20
#: 巡看间隔的地板（秒）。比它短就抬到它：朋友圈窗口开开关关比读消息显眼得多。
MIN_POLL_INTERVAL_SECONDS = MIN_MOMENTS_POLL_SECONDS


class MomentsWatcher:
    """按自己的间隔看一趟朋友圈，只碰新动态。``interval_seconds <= 0`` = 不巡看。"""

    def __init__(
        self,
        *,
        accounts: Sequence[str],
        runtime,  # WeChatRuntime，或任何提供 submit(TaskType, payload, confirmation=) 的对象
        store: GatewayStore,
        guard: OutboundGuard,
        interval_seconds: float,
        auto_like: bool = False,
        auto_comment: bool = False,
        comment_material: str = "",
        active_hours: str = "",
        allow_real_actions: bool = False,
        confirmation: str = "",
        on_new_posts: Callable[[str, list[dict[str, Any]]], Awaitable[object]] | None = None,
        read_limit: int = READ_LIMIT,
        like_limit: int = LIKE_LIMIT,
        clock: Callable[[], datetime] | None = None,
        rng: random.Random | None = None,
        min_interval_seconds: float = MIN_POLL_INTERVAL_SECONDS,
    ) -> None:
        self._accounts = list(accounts)
        self._runtime = runtime
        self._store = store
        self._guard = guard
        interval = float(interval_seconds)
        self._interval = max(interval, float(min_interval_seconds)) if interval > 0 else 0.0
        self._auto_like = bool(auto_like)
        self._auto_comment = bool(auto_comment)
        self._material = str(comment_material or "").strip()
        self._active_hours = active_hours
        self._allow_real = bool(allow_real_actions)
        self._confirmation = confirmation
        #: async (account_id, posts) -> None：新动态交给谁写评论。runner 拼好之后再装。
        self.on_new_posts = on_new_posts
        self._read_limit = max(1, min(int(read_limit), 20))
        self._like_limit = max(0, min(int(like_limit), 20))
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._rng = rng
        self._stopped = asyncio.Event()

    @property
    def interval_seconds(self) -> float:
        return self._interval

    @property
    def comments_enabled(self) -> bool:
        """有素材才评：没素材 AI 端写不出有分寸的话，与其乱评不如不评。"""
        return self._auto_comment and bool(self._material)

    @property
    def enabled(self) -> bool:
        """间隔为 0、没账号、或者点赞评论都没开 = 一趟都不开（开了窗口什么也不做没意义）。"""
        return (
            self._interval > 0
            and bool(self._accounts)
            and (self._auto_like or self.comments_enabled)
        )

    def _within_active_hours(self) -> bool:
        if not self._active_hours:
            return True
        return within_active_hours(self._clock(), self._active_hours)

    async def tick(self) -> list[dict[str, Any]]:
        """看一趟，返回这一趟**新出的**动态（所有账号合并）。"""
        if not self.enabled or not self._within_active_hours():
            return []
        found: list[dict[str, Any]] = []
        for account_id in self._accounts:
            found.extend(await self._scan_one(account_id))
        return found

    async def _scan_one(self, account_id: str) -> list[dict[str, Any]]:
        now = self._clock()
        like_limit = 0
        reservation = Reservation()
        if self._auto_like:
            reservation = self._guard.reserve(
                account_id, "", now=now, action=ACTION_MOMENTS_LIKE,
                count=self._like_limit, clamp=True,
            )
            if reservation.allowed:
                paced = self._guard.pace_write(account_id, "like_moments", now=now)
                if paced.allowed:
                    like_limit = reservation.granted
                else:
                    # 先占额度再过节拍；节拍挡下 = 这趟只看不点，额度当场还回去。
                    reservation.release()
                    _log.info("moments.poll.skip", account=account_id, reason=f"这趟不点赞：{paced.reason}")
            else:
                _log.info("moments.poll.skip", account=account_id, reason=f"这趟不点赞：{reservation.reason}")

        cursor = self._store.moment_cursor(account_id)
        payload = {
            "limit": self._read_limit,
            "like_limit": like_limit,
            "until_key": cursor,
            "dry_run": not self._allow_real,
        }
        try:
            raw = await self._runtime.submit(
                TaskType.SCAN_MOMENTS, payload, confirmation=self._confirmation,
            )
        except Exception as exc:  # noqa: BLE001 —— 巡看失败不能把循环带死，也不能前移游标
            reservation.release()
            _log.warning("moments.poll.failed", account=account_id, error=str(exc)[:160])
            return []
        status = str((raw or {}).get("status") or "succeeded")
        if status != "succeeded" or (raw or {}).get("timed_out"):
            # 超时：微信那边可能已经点了几下，按占住的算，宁多记不少记；其余全额还回。
            if (raw or {}).get("timed_out"):
                reservation.settle(reservation.granted)
            else:
                reservation.release()
            _log.warning(
                "moments.poll.failed", account=account_id,
                error=str((raw or {}).get("last_error") or status)[:160],
            )
            return []

        result = (raw or {}).get("result") or {}
        reservation.settle(int(result.get("liked") or 0))
        if result.get("dry_run"):
            # 本机没开真实动作：窗口没开、什么也没看到，游标别动。
            return []
        first_key = str(result.get("first_key") or "")
        if first_key:
            self._store.set_moment_cursor(account_id, first_key, now)
        posts = [
            {
                "content": str(post.get("content") or "").strip(),
                "posted_at": str(post.get("posted_at") or ""),
                "photos": int(post.get("photos") or 0),
                "videos": int(post.get("videos") or 0),
            }
            for post in (result.get("posts") or [])
            if isinstance(post, dict)
        ]
        _log.info(
            "moments.poll.done", account=account_id,
            new=len(posts), liked=int(result.get("liked") or 0),
            already_liked=int(result.get("already_liked") or 0),
            stopped=str(result.get("stopped") or ""),
        )
        # 交给 AI 端写评论：只交**有正文**的（纯图动态 AI 端也认不了，102 同一条规矩）。
        with_text = [post for post in posts if len(post["content"]) >= 2]
        if self.comments_enabled and with_text and self.on_new_posts is not None:
            try:
                await self.on_new_posts(account_id, with_text)
            except Exception as exc:  # noqa: BLE001 —— AI 端没接住不影响下一趟
                _log.warning("moments.poll.comment.failed", account=account_id, error=str(exc)[:160])
        return posts

    async def run(self) -> None:
        if not self.enabled:
            return
        self._stopped.clear()
        while not self._stopped.is_set():
            # 先睡后看：启动那一刻读消息已经在动微信了，别两件事挤在一起。
            try:
                await asyncio.wait_for(
                    self._stopped.wait(), timeout=jittered(self._interval, rng=self._rng)
                )
            except TimeoutError:
                pass
            if self._stopped.is_set():
                break
            try:
                await self.tick()
            except Exception as exc:  # noqa: BLE001 —— 一趟出错不能把循环带死
                _log.warning("moments.poll.failed", error=str(exc)[:160])

    def stop(self) -> None:
        self._stopped.set()
