"""真机 WS 端到端：换票 → 建连 → 建会话 → 发一轮 → 取卡 → 逐段真发到微信。

**不是单测**，故意不叫 ``test_*.py``：它要 Windows + 已登录的微信，会真的往微信发消息，
不能进 CI。手动跑（在交互式桌面会话里，不能走 SSH 的 Session 0）：

    python tests\\e2e_windows_ws.py                 # 真发到「文件传输助手」
    python tests\\e2e_windows_ws.py --peer 某联系人  # 换个对象（慎用，会打扰真人）

链路上只有两处是假的：AI 端换成 ``fake_agent_ws``（按 ``CLAUDE.md`` §4.2 契约回放），
入站那条消息由脚本注入——「真人给微信发消息」这一步没法在自动化里造。**其余全是生产
代码**：真 GatewayRunner、真 ConnectionPool、真换票、真 WS 帧、真队列、真 pyweixin 发送。

对应 ``docs/gateway-plan.md`` 的 Phase C3。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent))

MARK = "劳小智网关WS自测"
ACCOUNT = "wxid_selftest"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="真机 WS 端到端验证")
    parser.add_argument("--peer", default="文件传输助手", help="回复发给谁（默认发给自己）")
    parser.add_argument("--dry-run", action="store_true", help="只走链路，不真的发消息")
    return parser.parse_args(argv)


async def run(args: argparse.Namespace) -> int:
    from fake_agent_ws import FakeAgentServer, TurnScript

    from wxclient.config import load_settings
    from wxclient.gateway.config import GatewaySettings
    from wxclient.gateway.inbound import InboundMessage
    from wxclient.gateway.runner import GatewayRunner
    from wxclient.gateway.sessions import GatewayStore
    from wxclient.gateway.ticket import LocalFileTicketProvider
    from wxclient.gateway.wechat_io import QueueMessageSink
    from wxclient.logging_config import configure_logging
    from wxclient.runtime import REAL_ACTION_CONFIRMATION, RuntimeSettings, WeChatRuntime

    configure_logging()
    wechat_settings = load_settings(None)
    data_dir = wechat_settings.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)

    # 本地联调票：格式是 {"<微信号>": "<session_ticket>"}，落在运行数据目录（永不入库）
    ticket_path = data_dir / "e2e_ticket.json"
    ticket_path.write_text(json.dumps({ACCOUNT: "e2e-local-ticket"}), encoding="utf-8")

    os.environ["WXCLIENT_GATEWAY_LOCAL_TICKET_FILE"] = str(ticket_path)
    os.environ["WXCLIENT_GATEWAY_BUSINESS_API_URL"] = ""
    os.environ["WXCLIENT_GATEWAY_DEBOUNCE_SECONDS"] = "0.5"
    os.environ["WXCLIENT_GATEWAY_ACTIVE_HOURS"] = "00:00-23:59"

    class ScriptedSource:
        """只在第一次 fetch 时给一条入站消息，之后一直是空。"""

        def __init__(self, message: InboundMessage) -> None:
            self._pending = [message]

        async def fetch(self, account_id: str):
            pending, self._pending = self._pending, []
            return pending

    script = TurnScript(
        deltas=["正在", "回复"],
        cards=[{
            "type": "info",
            "body": {
                "kind": "wechat_reply",
                "reply_segments": [f"{MARK}：第一段，收到你的消息", f"{MARK}：第二段，链路已打通"],
            },
        }],
    )

    ok = True
    async with FakeAgentServer(valid_tickets={"e2e-local-ticket"}, turn_script=script) as fake:
        os.environ["WXCLIENT_GATEWAY_AI_WS_URL"] = fake.url
        settings = GatewaySettings()
        settings.validate_for_start()
        print(f"[cfg] AI 端={fake.url} 回复对象={args.peer} 真发={not args.dry_run}")

        runtime = WeChatRuntime(wechat_settings, RuntimeSettings(allow_real_actions=not args.dry_run))
        runtime.start()
        store = GatewayStore(data_dir / "gateway_e2e.db")

        message = InboundMessage(
            account_id=ACCOUNT,
            peer_id=args.peer,
            # 每次唯一：msg_id 去重游标会挡住重复的消息（这是设计行为，别用固定值）
            msg_id=f"e2e-{uuid4()}",
            text="你好，我想问下还招人吗",
            at=datetime.now(timezone.utc),
            peer_name=args.peer,
        )
        runner = GatewayRunner(
            settings=settings,
            ticket_provider=LocalFileTicketProvider(ticket_path),
            store=store,
            source=ScriptedSource(message),
            sink=QueueMessageSink(
                runtime=runtime,
                allow_real_actions=not args.dry_run,
                confirmation=REAL_ACTION_CONFIRMATION,
            ),
            accounts=[ACCOUNT],
        )

        await runner.tick()            # 取到入站，进去抖窗口
        await asyncio.sleep(1.0)
        await runner.tick()            # 去抖到期，发起一轮
        await runner.drain()           # 等在途轮收口
        await runner.stop()
        runtime.stop()

        ops = [frame.get("op") for frame in fake.received]
        print(f"[ws] AI 端收到的帧序列：{ops}")
        for expected in ("auth", "session.start", "message.send"):
            hit = expected in ops
            ok = ok and hit
            print(f"[ws] {'OK ' if hit else 'FAIL'} {expected}")

        sent = next((f for f in fake.received if f.get("op") == "message.send"), None)
        if sent is not None:
            text = (sent.get("data") or {}).get("text") or ""
            has_context = "还招人吗" in text
            ok = ok and has_context
            print(f"[ws] {'OK ' if has_context else 'FAIL'} 入站上下文进了 message.send（{len(text)} 字）")

    print("\n" + ("[PASS] WS 端到端通过" if ok else "[FAIL] WS 端到端未通过"))
    print(f"提示：去微信「{args.peer}」里应能看到两段以「{MARK}」开头的消息。")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run(parse_args())))
