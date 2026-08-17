"""调试会话：在这台机器上直接跟 AI 端说话，看它下发什么指令。

**它不是一条旁路。** 说出去的话走的是网关常驻的那条 WS，AI 端产出的指令再从同一条连接
推回来、由同一个执行器执行（``gateway/runner.py`` 的 ``debug_turn`` 有整条路径的图）。
所以这一页测出来的行为就是线上的行为——装完机想确认「这台电脑到底能不能发朋友圈」，
在这儿说一句就知道，不用先去找个候选人发消息来触发。

线程规矩（本仓踩过的坑，别改）：网关那头只往 ``queue`` 里放东西，界面只在自己的心跳里
取——**两边谁都不去动对方的东西**。跨线程直接 ``after``/改控件正是 2026-08-13 那次卡死
的形状（见 ``ui/service.py`` 的 ``_set_state``）。
"""

from __future__ import annotations

import queue
import time
import tkinter as tk
from concurrent.futures import Future
from typing import Any

from wxclient.gateway.debugchat import format_turn

from . import theme, widgets
from .service import GatewayService, ServiceNotRunning

#: 心跳：多久取一次流式增量、看一眼这一轮完了没。和日志页一个量级（120ms），够跟手。
PUMP_MS = 120

#: 状态行里最多铺多长的流式尾巴。它只是「还在动」的证据，不是内容。
_TAIL_BUDGET = 48

PLACEHOLDER = "比如：给张三发条消息问他明天能不能来面试"

#: 空会话时摆的几句示例。**照着能力清单写**，别举一个网关做不到的例子。
_EXAMPLES = (
    "给张三发条消息，问他明天上午十点能不能来面试",
    "发一条朋友圈，招 3 个电子厂普工，日结 180",
    "看看有没有新的好友申请",
)


class DebugPage(tk.Frame):
    """一页聊天。**只连 AI 端，不碰微信**——真去动微信的是收到指令之后的执行器。"""

    def __init__(self, parent: tk.Misc, service: GatewayService) -> None:
        super().__init__(parent, bg=theme.CONTENT_BG)
        self.service = service
        self._events: queue.Queue[str] = queue.Queue()
        self._future: Future[Any] | None = None
        self._stream = ""
        self._started_at = 0.0
        self._build()
        self._say_meta("在这儿说的话直接发给 AI 端；它要动微信的话，指令会推回这台电脑执行。")
        for example in _EXAMPLES:
            self._say_meta(f"    · {example}")
        self._pump()

    # ── 版式 ──────────────────────────────────────────────────────

    def _build(self) -> None:
        body = tk.Frame(self, bg=theme.CONTENT_BG)

        # 用 Text 而不是自绘：调试时**要能选中、能复制**（指令参数、报错原文都要贴出去）。
        self._view = tk.Text(body, bg=theme.CONTENT_BG, fg=theme.TEXT, font=theme.FONT_BODY,
                             wrap="word", relief=tk.FLAT, bd=0, highlightthickness=0,
                             padx=4, pady=6, spacing1=2, spacing3=6, cursor="arrow")
        scroll = tk.Scrollbar(body, orient=tk.VERTICAL, command=self._view.yview, width=12)
        self._view.configure(yscrollcommand=scroll.set)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self._view.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._view.tag_configure("me", foreground=theme.GREEN_DEEP, font=(theme.UI, 10, "bold"))
        self._view.tag_configure("ai", foreground=theme.TEXT)
        self._view.tag_configure("meta", foreground=theme.TEXT_MUTED, font=theme.FONT_SMALL)
        self._view.tag_configure("cmd", foreground=theme.GREEN, font=theme.FONT_LOG)
        self._view.tag_configure("warn", foreground=theme.ORANGE, font=theme.FONT_SMALL)
        self._view.tag_configure("err", foreground=theme.RED)
        self._view.configure(state=tk.DISABLED)  # 只读，但仍可选中复制

        # 状态行摆在输入框上方，**不写进对话流**——它每 120ms 变一次，写进去的话
        # 人一往上翻就被几十行「正在处理…」淹了。
        self._status = tk.Label(self, text="", font=theme.FONT_SMALL, bg=theme.CONTENT_BG,
                                fg=theme.TEXT_MUTED, anchor="w")

        bar = tk.Frame(self, bg=theme.CONTENT_BG)
        bar.pack(side=tk.BOTTOM, fill=tk.X, padx=24, pady=(0, 16))

        box = tk.Frame(bar, bg=theme.FIELD_BG, highlightthickness=1,
                       highlightbackground=theme.FIELD_LINE, highlightcolor=theme.FIELD_LINE, bd=0)
        box.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=6)
        self._text = tk.StringVar()
        self._entry = tk.Entry(box, textvariable=self._text, font=theme.FONT_INPUT,
                               bg=theme.FIELD_BG, fg=theme.TEXT, relief=tk.FLAT,
                               highlightthickness=0, bd=0, insertbackground=theme.TEXT)
        self._entry.pack(fill=tk.X, padx=10)
        widgets.attach_placeholder(self._entry, self._text, PLACEHOLDER)
        widgets.focus_ring(box, self._entry)
        self._entry.bind("<Return>", lambda _e: self.send())

        self._send_button = widgets.ghost_button(bar, "发送", self.send)
        self._send_button.pack(side=tk.LEFT, padx=(10, 0))
        widgets.ghost_button(bar, "新会话", self.reset).pack(side=tk.LEFT, padx=(8, 0))

        # ★ 对话区**最后** pack：它是 expand 的那个，先 pack 的话会把底下的输入行挤出可视范围
        # （配置中心的动作条踩过同一个坑）。
        body.pack(fill=tk.BOTH, expand=True, padx=24, pady=(0, 12))

    # ── 发送 ──────────────────────────────────────────────────────

    def send(self) -> None:
        if self._future is not None:
            return  # 一轮还没完。会话是串行的，排队只会让人以为发出去了
        text = self._text.get().strip()
        if not text:
            return
        try:
            future = self.service.debug_turn(text, on_delta=self._events.put)
        except ServiceNotRunning as exc:
            # 不清输入框：他打的那句话还在，起了服务点一下就能发出去
            self._say("err", f"× {exc}")
            return
        except Exception as exc:  # noqa: BLE001 - 发不出去要给个说法，不能静默
            self._say("err", f"× 发不出去：{exc}")
            return
        self._text.set("")
        self._say("me", f"我：{text}")
        self._future = future
        self._stream = ""
        self._started_at = time.monotonic()
        self._render_status("正在发给 AI 端…")
        self._sync_send_button()

    def reset(self) -> None:
        """开一轮新的。**不清屏**——上一轮说了什么正是排障时要回看的。"""
        self.service.reset_debug_session()
        self._say_meta("已开始新会话，上下文从这里断开。")

    # ── 心跳：取增量、看这一轮完了没 ───────────────────────────────

    def _pump(self) -> None:
        try:
            self._drain()
        finally:
            try:
                self.after(PUMP_MS, self._pump)
            except tk.TclError:
                pass  # 页面已经销毁

    def _drain(self) -> None:
        changed = False
        while True:
            try:
                self._stream += self._events.get_nowait()
            except queue.Empty:
                break
            changed = True

        future = self._future
        if future is None:
            return
        if not future.done():
            if changed:
                self._render_status(f"AI 正在处理…  {self._tail()}")
            return

        self._future = None
        self._render_status("")
        try:
            result = future.result()
        except Exception as exc:  # noqa: BLE001 - 超时/断线/票据失效都要照原样说出来
            self._say("err", f"× {type(exc).__name__}：{exc}")
            self._sync_send_button()
            return
        self._render_reply(result)
        self._sync_send_button()

    def _tail(self) -> str:
        tail = self._stream.strip().replace("\n", " ")
        return tail[-_TAIL_BUDGET:] if len(tail) > _TAIL_BUDGET else tail

    def _render_reply(self, result: Any) -> None:
        reply = format_turn(result)
        cost = f"（{time.monotonic() - self._started_at:.1f}s）"
        if reply.error:
            self._say("err", f"× AI 端报错：{reply.error} {cost}")
            return
        if reply.text:
            self._say("ai", f"小智：{reply.text} {cost}")
        for line in reply.commands:
            self._say("cmd", f"    ⇩ 已下发指令 {line}")
        for line in reply.skipped:
            self._say("warn", f"    ! 没下发：{line}")
        if reply.commands:
            self._say_meta("    指令在这台电脑上串行执行，过程和结果看「实时日志」。")
        if reply.raw:
            self._say("meta", f"这一轮没给出能认的产出，原样贴在这儿：\n{reply.raw}")
        if not (reply.text or reply.commands or reply.skipped or reply.raw):
            self._say_meta(f"这一轮没有任何产出 {cost}")

    # ── 往下写 ────────────────────────────────────────────────────

    def _say(self, tag: str, text: str) -> None:
        at_bottom = self._at_bottom()
        self._view.configure(state=tk.NORMAL)
        self._view.insert(tk.END, f"{time.strftime('%H:%M:%S')}  ", "meta")
        self._view.insert(tk.END, f"{text}\n", tag)
        self._view.configure(state=tk.DISABLED)
        # 只在人本来就贴着底的时候才跟着滚：他往上翻着看历史时别把他拽回来
        if at_bottom:
            self._view.see(tk.END)

    def _say_meta(self, text: str) -> None:
        self._say("meta", text)

    def _at_bottom(self) -> bool:
        try:
            return self._view.yview()[1] >= 0.999
        except tk.TclError:
            return True

    def _render_status(self, text: str) -> None:
        if text:
            self._status.configure(text=text)
            if not self._status.winfo_manager():
                self._status.pack(side=tk.BOTTOM, fill=tk.X, padx=28, pady=(0, 4))
        else:
            self._status.pack_forget()

    def _sync_send_button(self) -> None:
        """一轮跑着的时候按钮变灰。点了没反应比灰着更让人怀疑是不是卡了。"""
        busy = self._future is not None
        self._send_button.configure(
            text="发送中…" if busy else "发送",
            fg=theme.TEXT_MUTED if busy else theme.GREEN,
            highlightbackground=theme.ROW_LINE if busy else theme.GREEN,
            highlightcolor=theme.ROW_LINE if busy else theme.GREEN,
            cursor="arrow" if busy else "hand2",
        )
