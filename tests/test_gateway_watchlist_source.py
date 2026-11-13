"""监听名单换来源（spec 085）：从会话名映射派生 + 并存期 + fail-closed 不退化。

★ 用户 2026-08-20 裁决：**就用 `GET /client/contacts` 派生名单，不新增接口。**
所以名单 = 本机 `gateway_contacts` 里 `accepted` 且会话名非空的那些行；
「这一轮没拉到」天然等于「沿用上一份」——拉失败根本不写那张表。

★ 这组用例守的是整个迁移里最危险的一步。名单是 **fail-closed 白名单**——
「没拿到名单」和「名单是空的」在行为上天差地别：前者该沿用现有那份，后者是一个会话都不回。
把两者混起来，现场表现就是网关突然全哑，而候选人那边已经在等了。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from structlog.testing import capture_logs

from wxclient.gateway import GatewayRunner, GatewaySettings, LocalFileTicketProvider
from wxclient.gateway.sessions import GatewayStore
from wxclient.gateway.watchlist import Watchlist, WatchScope

ACCOUNT = "acct-1"


def _row(number: str, session_name: str, status: str = "accepted", worker_id: str | None = "w-1") -> dict:
    """一行 `GET /client/contacts` 的响应（字段名逐字照 api2 §4.2）。"""
    return {
        "number": number,
        "session_name": session_name,
        "profile_name": "",
        "contact_status": status,
        "worker_id": worker_id,
        "updated_at": "2026-08-20T09:00:00.000Z",
    }


class DerivedFromContactsTests(unittest.TestCase):
    """名单从会话名映射里派生：哪些行算数、哪些不算。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = GatewayStore(Path(self._tmp.name) / "gateway.db")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_只取已通过的_去重排序(self) -> None:
        self.store.save_contacts(ACCOUNT, [
            _row("138", "张三·普工"),
            _row("139", "李四·叉车"),
            _row("140", "张三·普工"),      # 同名不同号：名单里只该有一个
        ])
        self.assertEqual(self.store.watched_session_names(ACCOUNT), ["张三·普工", "李四·叉车"])

    def test_x_申请还没通过的不进名单(self) -> None:
        """好友没通过 = 微信里根本没有那个会话窗口，进名单只会每轮白开一次、白吃一个读名额。

        这类行还有第二个来源：业务后端推计划时自己写一条 `requested`，
        那是「计划点名要加谁」，更不是「可以自动回复」。
        """
        self.store.save_contacts(ACCOUNT, [_row("138", "张三", status="requested")])
        self.assertEqual(self.store.watched_session_names(ACCOUNT), [])

    def test_x_会话名是空的不进名单(self) -> None:
        """名单按微信里显示的名字**逐字**寻址，空名字查无此人。"""
        self.store.save_contacts(ACCOUNT, [_row("138", ""), _row("139", "   ")])
        self.assertEqual(self.store.watched_session_names(ACCOUNT), [])

    def test_归位不了候选人的照样算(self) -> None:
        """`worker_id` 为 null 只是「后端还没把这条上报归位到候选人」（api2 §4.2 明说映射不丢）。

        人是真加上了、名字也是真的。拿它过滤就等于「刚通过好友、开口说话，分身不理」。
        """
        self.store.save_contacts(ACCOUNT, [_row("138", "张三", worker_id=None)])
        self.assertEqual(self.store.watched_session_names(ACCOUNT), ["张三"])

    def test_别的账号的不算(self) -> None:
        self.store.save_contacts(ACCOUNT, [_row("138", "我的人")])
        self.store.save_contacts("acct-2", [_row("139", "别人的人")])
        self.assertEqual(self.store.watched_session_names(ACCOUNT), ["我的人"])


class DerivedTakeoverTests(unittest.IsolatedAsyncioTestCase):
    """派生结果怎么装进 `Watchlist`——**空的一律不接管**是这里的重点。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = GatewayStore(root / "gateway.db")
        self.tickets = root / "tickets.json"
        self.tickets.write_text(json.dumps({ACCOUNT: "tkt"}), encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _runner(self, watchlist: Watchlist) -> GatewayRunner:
        return GatewayRunner(
            settings=GatewaySettings(ai_ws_url="ws://127.0.0.1:1/ws", local_ticket_file=self.tickets),
            ticket_provider=LocalFileTicketProvider(self.tickets),
            store=self.store,
            source=_SilentSource(),
            sink=_SilentSink(),
            accounts=[ACCOUNT],
            watchlist=watchlist,
        )

    def test_派生出东西就装上(self) -> None:
        self.store.save_contacts(ACCOUNT, [_row("138", "张三·普工")])
        wl = Watchlist()
        self.assertEqual(self._runner(wl)._apply_derived_watchlist(), 1)
        self.assertEqual(wl.watched(), ["张三·普工"])

    def test_x_派生出来是空的_绝不接管(self) -> None:
        """★ 这一条是整次换源的安全阀。

        现网 `gateway_contacts` 眼下还是 0 行。空名单意味着一个会话都不碰，而
        `apply_backend` 一调就会把 AI 端那条推送永久挡掉——真拿空名单去接管，
        等来的是整台机器全哑，而且退不回去。
        """
        wl = Watchlist()
        wl.apply_push({"only": ["AI 端推来的张三"]})
        self.assertEqual(self._runner(wl)._apply_derived_watchlist(), 0)
        self.assertEqual(wl.watched(), ["AI 端推来的张三"], "沿用现有那份，不清空")
        self.assertTrue(wl.apply_push({"only": ["李四"]}), "更没接管，AI 推送仍然生效")


class _SilentSource:
    async def fetch(self, account_id: str) -> list:  # pragma: no cover - 这组用例不读微信
        return []


class _SilentSink:
    async def send(self, account_id: str, peer_id: str, text: str) -> str:  # pragma: no cover
        raise AssertionError("这条测试不该往微信发东西")


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

    def test_x_业务端配着_重启后AI推送也不作数(self) -> None:
        """★ 用户 2026-11-12：每次登录名单 36 个，同步后没了，下次登录又是 36。
        「接管了」只在内存里，重启清零，AI 端一登记就把它那份老名单推回来。"""
        saved: list[WatchScope] = []
        old = [f"老名单{i}" for i in range(36)]
        wl = Watchlist(persist=saved.append)
        wl.apply_push({"only": old})               # 登录时 AI 端推来的老名单
        wl.apply_backend({"only": []})            # 点「同步名单」：业务端明说没人

        restarted = Watchlist()                    # 重新登录 = 新进程装配
        restarted.apply_scope(saved[-1])
        restarted.adopt_backend_authority()        # factory：业务端同步配着
        self.assertFalse(restarted.apply_push({"only": old}))
        self.assertEqual(restarted.watched(), [], "★ 老名单不许在登录时装回来")

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


class DerivedEmptyIsLoudTests(unittest.TestCase):
    """★ 派生出来是空的，必须**喊出来**（2026-08-31 事故）。

    `Watchlist.warn_if_idle()` 只在名单**真空着**时喊。一旦有别的东西把名单填上了
    （迁移期 AI 端那条推送、或上次落盘的那份），`ready` 就是 True，于是
    「后端这条路其实早就不出货了」一声不响。现场就这样连着几天：
    `GET /client/contacts` 被 409 顶回、派生恒为空，机器跑在一份旧名单上，
    日志/界面/AI 端三头都看不出异常，最后靠登进 Windows 翻 sqlite 才查出来。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = GatewayStore(root / "gateway.db")
        self.tickets = root / "tickets.json"
        self.tickets.write_text(json.dumps({ACCOUNT: "tkt"}), encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _runner(self, watchlist: Watchlist) -> GatewayRunner:
        return GatewayRunner(
            settings=GatewaySettings(ai_ws_url="ws://127.0.0.1:1/ws", local_ticket_file=self.tickets),
            ticket_provider=LocalFileTicketProvider(self.tickets),
            store=self.store,
            source=_SilentSource(),
            sink=_SilentSink(),
            accounts=[ACCOUNT],
            watchlist=watchlist,
        )

    @staticmethod
    def _notes(logs: list[dict]) -> list[dict]:
        return [line for line in logs if "这一轮不接管" in str(line.get("event", ""))]

    def test_x_名单被别处填上时_派生为空不再静默(self) -> None:
        """机器没全哑（AI 端推送顶着），恰恰因此以前一个字都不说。"""
        wl = Watchlist()
        wl.apply_push({"only": ["旧名单里的人"]})
        runner = self._runner(wl)

        with capture_logs() as logs:
            self.assertEqual(runner._apply_derived_watchlist(), 0)

        notes = self._notes(logs)
        self.assertEqual(len(notes), 1, "派生为空必须说一次")
        self.assertEqual(notes[0]["现在在盯"], 1, "要说明此刻真正生效的是几个会话")
        self.assertEqual(notes[0]["本机会话名"], 0, "0 = 同步就没成功过，去看错误码")

    def test_有会话名但都没通过_数字分得开这两种情况(self) -> None:
        """`本机会话名>0` 而 `可自动回复=0` = 人全是 requested，这不是故障。"""
        self.store.save_contacts(ACCOUNT, [_row("138", "还没通过的人", status="requested")])
        runner = self._runner(Watchlist())

        with capture_logs() as logs:
            runner._apply_derived_watchlist()

        self.assertEqual(self._notes(logs)[0]["本机会话名"], 1)

    def test_情况没变就不再重复喊(self) -> None:
        """同步 120 秒一轮，照喊会把日志刷满，反把状态真变化那一刻埋掉。"""
        runner = self._runner(Watchlist())
        with capture_logs() as logs:
            for _ in range(3):
                runner._apply_derived_watchlist()
        self.assertEqual(len(self._notes(logs)), 1)

    def test_x_同一种故障复发_不能被去重吃掉(self) -> None:
        """去重态必须在派生成功时清掉，否则**第二次同样的故障永远静默**。

        ★ 这一条第一版是假绿：当时让两次故障处在不同状态（本机会话名 0→1），签名本来
        就不同，去重压根没机会生效，摘掉清零那行照样全绿。要真测到它，两次故障的三个数
        必须**一模一样**——现实里这正是最常见的形态：409 来了又走、走了又来，而本机
        contacts 的条数自始至终没变。
        """
        self.store.save_contacts(ACCOUNT, [_row("138", "张三")])
        runner = self._runner(Watchlist())
        runner._apply_derived_watchlist()                                        # 正常出货
        flip = lambda status: self.store.save_contacts(ACCOUNT, [_row("138", "张三", status=status)])

        flip("requested")
        with capture_logs() as first:
            runner._apply_derived_watchlist()                                    # 第一次故障 → 喊
        flip("accepted")
        runner._apply_derived_watchlist()                                        # 自愈
        flip("requested")
        with capture_logs() as again:
            runner._apply_derived_watchlist()                                    # 同样的故障复发

        self.assertEqual(len(self._notes(first)), 1)
        self.assertEqual(self._notes(first)[0], self._notes(again)[0] if self._notes(again) else None,
                         "两次故障的三个数必须一致，否则这条测不到去重")
        self.assertEqual(len(self._notes(again)), 1, "同一种故障复发不能被去重吃掉")

    def test_派生正常时一个字都不说(self) -> None:
        self.store.save_contacts(ACCOUNT, [_row("138", "张三")])
        runner = self._runner(Watchlist())
        with capture_logs() as logs:
            runner._apply_derived_watchlist()
        self.assertEqual(self._notes(logs), [])



if __name__ == "__main__":
    unittest.main()
