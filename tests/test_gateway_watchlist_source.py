"""监听名单换来源（spec 085）：业务后端接管 + 并存期 + fail-closed 不退化。

★ 这组用例守的是整个迁移里最危险的一步。名单是 **fail-closed 白名单**——
「没拿到名单」和「名单是空的」在行为上天差地别：前者该沿用现有那份，后者是一个会话都不回。
把两者混起来，现场表现就是网关突然全哑，而候选人那边已经在等了。
"""

from __future__ import annotations

import unittest

import httpx

from wxclient.gateway.watchlist import WatchScope, Watchlist
from wxclient.gateway.watchlist_sync import WatchlistDirectory


def _directory(handler, *, base_url: str = "https://b.test", token: str = "T") -> WatchlistDirectory:
    return WatchlistDirectory(
        base_url, lambda: token,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


async def _pull(handler, **kwargs):
    return await _directory(handler, **kwargs).pull()


class WatchlistPullTests(unittest.IsolatedAsyncioTestCase):
    async def test_it_pulls_a_list_and_normalises_it(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("authorization", "")
            return httpx.Response(200, json={"only": ["张三·普工", " 李四 ", "张三·普工", ""],
                                             "never": ["王五"]})

        got = await _pull(handler)
        self.assertEqual(got, {"only": ["张三·普工", "李四"], "never": ["王五"]})
        self.assertTrue(seen["url"].endswith("/recruitment-agent/client/watchlist"))
        self.assertEqual(seen["auth"], "Bearer T")

    async def test_群名和人名一样收(self) -> None:
        """群没有手机号、进不了 contacts，只能走这条路——这正是它是独立接口的理由。"""
        got = await _pull(lambda r: httpx.Response(200, json={"only": ["普工招聘交流群", "张三"]}))
        self.assertEqual(got, {"only": ["普工招聘交流群", "张三"]})

    async def test_没登录不发请求(self) -> None:
        called = []

        def handler(request: httpx.Request) -> httpx.Response:
            called.append(1)
            return httpx.Response(200, json={"only": []})

        self.assertIsNone(await _pull(handler, token=""))
        self.assertIsNone(await _pull(handler, base_url=""))
        self.assertEqual(called, [])

    async def test_x_拉不到一律返回_None_而不是空名单(self) -> None:
        """None = 这轮没拿到（沿用现有名单）；空名单 = 一个会话都不回。绝不能混。"""
        for name, handler in [
            ("被拒", lambda r: httpx.Response(403, json={})),
            ("服务器炸", lambda r: httpx.Response(500, text="boom")),
            ("不是 JSON", lambda r: httpx.Response(200, text="<html>")),
            ("不是对象", lambda r: httpx.Response(200, json=["张三"])),
            ("没有 only/never", lambda r: httpx.Response(200, json={"updated_at": "x"})),
        ]:
            with self.subTest(name):
                self.assertIsNone(await _pull(handler))

    async def test_网络异常也是_None_不上抛(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down")

        self.assertIsNone(await _pull(handler))

    async def test_后端真的给空名单是收的(self) -> None:
        """★ 与上一条区分：后端**明确说**「一个都不管」是合法的，那是业务判断。"""
        self.assertEqual(await _pull(lambda r: httpx.Response(200, json={"only": []})), {"only": []})


class CoexistenceTests(unittest.TestCase):
    """并存期（design §6）：后端优先；后端还没说话之前 AI 推送照旧生效。"""

    def test_后端没说话之前_AI_推送照旧生效(self) -> None:
        wl = Watchlist()
        self.assertTrue(wl.apply_push({"only": ["张三"]}))
        self.assertEqual(wl.watched(), ["张三"])

    def test_x_后端说过话之后_AI_推送不再作数(self) -> None:
        wl = Watchlist()
        wl.apply_push({"only": ["张三"]})
        self.assertTrue(wl.apply_backend({"only": ["李四"]}))

        self.assertFalse(wl.apply_push({"only": ["张三", "被顶掉的人"]}))
        self.assertEqual(wl.watched(), ["李四"], "后端接管后名单只听后端的")

    def test_后端给了一份和现状一样的名单_也算接管(self) -> None:
        """接管与否看「后端说过话没有」，不看「说的话有没有让名单变化」。"""
        wl = Watchlist()
        wl.apply_push({"only": ["张三"]})
        self.assertFalse(wl.apply_backend({"only": ["张三"]}), "内容没变，不算「换了」")
        self.assertFalse(wl.apply_push({"only": ["别人"]}), "但已经接管了")
        self.assertEqual(wl.watched(), ["张三"])

    def test_后端给的形状不对_不算接管也不动名单(self) -> None:
        wl = Watchlist()
        wl.apply_push({"only": ["张三"]})
        self.assertFalse(wl.apply_backend(None))
        self.assertEqual(wl.watched(), ["张三"])
        self.assertTrue(wl.apply_push({"only": ["张三", "李四"]}), "没接管，AI 推送仍生效")


class FailClosedTests(unittest.TestCase):
    """换来源不许动默认值（`watchlist.py` 顶部两条硬规矩 · PRD §13.1）。"""

    def test_一份名单都没有时_一个会话都不碰(self) -> None:
        wl = Watchlist()
        self.assertFalse(wl.ready)
        self.assertIsNotNone(wl.reason("随便谁"))

    def test_后端明确给空名单_仍然一个都不回(self) -> None:
        wl = Watchlist()
        wl.apply_backend({"only": [], "never": []})
        self.assertEqual(wl.watched(), [])
        self.assertIsNotNone(wl.reason("张三"))

    def test_never_压过_only(self) -> None:
        wl = Watchlist()
        wl.apply_backend({"only": ["张三", "李四"], "never": ["李四"]})
        self.assertEqual(wl.watched(), ["张三"])
        self.assertIsNotNone(wl.reason("李四"))

    def test_落盘恢复不退化(self) -> None:
        """网关重启后从本机那份恢复，不必等后端——否则中间那段是全哑的。"""
        saved: list[WatchScope] = []
        wl = Watchlist(persist=saved.append)
        wl.apply_backend({"only": ["张三"]})
        self.assertTrue(saved, "后端同步来的名单要落盘")

        restored = Watchlist()
        restored.apply_scope(saved[-1])
        self.assertEqual(restored.watched(), ["张三"])
        self.assertTrue(restored.ready)


if __name__ == "__main__":
    unittest.main()
