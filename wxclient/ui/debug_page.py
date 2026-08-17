"""调试会话：在这台机器上直接跟 AI 说话，看它给这台电脑下什么指令、做没做成。

版式照 ``docs/chat.png``：左侧头像 + 气泡的对话流，指令单独一张结果卡，
底部多行输入（Enter 发送 · Shift + Enter 换行）。

**它不是一条旁路。** 说出去的话走网关常驻的那条 WS，AI 端产出的指令再从同一条连接
推回来、由同一个执行器执行（``gateway/runner.py`` 的 ``debug_turn`` 有整条路径的图）。

★ 这一页最要紧的一条：**说真话**。
  卡里写着 `wechat_commands` 只代表「AI 打算做这几件事」，不代表它下发成功了——
  2026-08-17 真机上就出过：界面写「已下发指令 send_message」，而 AI 端那头记的是
  `no wechat gateway online for this employee`，一条指令都没出去，人在这边等了半天。
  所以现在指令卡起手是**待下发**，只有等到真回执（`wechat.command_result`）才会
  变成「执行成功」或「没有下发 + 原因」。认不出的状态一律留在中间态，不涂绿勾。

线程规矩（本仓踩过的坑，别改）：网关那头只往 ``queue`` 里放东西，界面只在自己的心跳里
取——**两边谁都不去动对方的东西**。跨线程直接 ``after``/改控件正是 2026-08-13 那次卡死
的形状（见 ``ui/service.py`` 的 ``_set_state``）。
"""

from __future__ import annotations

import queue
import time
import tkinter as tk
from collections.abc import Callable
from concurrent.futures import Future
from contextlib import suppress
from typing import Any

from wxclient.gateway.debugchat import format_receipt, format_turn

from . import icons, theme, widgets
from .service import GatewayService, ServiceNotRunning, ServiceState

#: 心跳：多久取一次流式增量 / 刷一次「已用时」。和日志页一个量级，够跟手。
PUMP_MS = 120

#: 对面是谁。**不是「微信助手」**——那是这台电脑上跑的这个程序自己的名字。
#: 2026-08-17 用户反馈：两边同名，界面上就变成「微信助手在跟微信助手说话」，
#: 分不清哪句是本机说的、哪句是 AI 端说的。这一页对面坐的是劳小智的 AI 端，叫小智。
AI_NAME = "小智"
PLACEHOLDER = f"给{AI_NAME}发送消息"

#: 头像直径（设计稿 docs/chat.png 量出来的）。文字列从 `_INDENT` 起，和头像右边留一口气。
_AVATAR = 34
_INDENT = 46

#: 气泡最宽占内容区多少。太宽了长段落一行到底，读起来累；照设计稿取 ~62%。
_BUBBLE_RATIO = 0.62
_BUBBLE_MIN = 260

#: 用户气泡的浅绿（设计稿 docs/chat.png 取色）。
BUBBLE_ME_BG = "#e7f6ec"
#: 指令结果卡的边框与底色。
CARD_LINE = "#e6e8eb"

#: 转圈那三个点的帧。跟着心跳走，一秒转两圈左右。
_DOT_FRAMES = ((1, 0, 0), (0, 1, 0), (0, 0, 1), (0, 1, 0))

#: 空会话时的开场白。**只此一句**——底下那行「试试：给张三…」被用户砍了
#: （2026-08-17）：举例子是教学，而这一页是给会用的人排障用的。
_GREETING = "你好，我是小智。你可以直接描述要完成的任务。"


class DebugPage(tk.Frame):
    """一页聊天。**只连 AI 端，不碰微信**——真去动微信的是收到指令之后的执行器。"""

    def __init__(
        self,
        parent: tk.Misc,
        service: GatewayService,
        *,
        on_open_log: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(parent, bg=theme.CONTENT_BG)
        self.service = service
        self._on_open_log = on_open_log
        self._events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._future: Future[Any] | None = None
        self._stream = ""
        self._started_at = 0.0
        self._frame = 0
        self._bubble_width = _BUBBLE_MIN
        self._live: tk.Label | None = None      # 正在流式写入的那个气泡
        self._pending: tk.Frame | None = None   # 「正在…」那一行
        self._stage_text = ""
        self._open_cards: list[tuple[str, Any]] = []  # (op, 卡片) 等回执认领
        self._my_mark: tk.Label | None = None    # 这一轮自己那句话右边的下场标记
        self._build()
        self._say_ai(_GREETING)
        self._pump()

    # ── 版式 ──────────────────────────────────────────────────────

    def build_header_actions(self, parent: tk.Misc) -> tk.Frame:
        """标题栏右边那一组（● AI 已连接 · 新会话）。由 window 摆进公共标题行。"""
        box = tk.Frame(parent, bg=theme.CONTENT_BG)
        self._conn_dot = tk.Label(box, text="●", font=(theme.UI, 8),
                                  bg=theme.CONTENT_BG, fg=theme.GRAY)
        self._conn_dot.pack(side=tk.LEFT, padx=(0, 5))
        self._conn_text = tk.Label(box, text="未连接", font=theme.FONT_SMALL,
                                   bg=theme.CONTENT_BG, fg=theme.TEXT_MUTED)
        self._conn_text.pack(side=tk.LEFT, padx=(0, 14))
        widgets.ghost_button(box, "新会话", self.reset).pack(side=tk.LEFT)
        return box

    def _build(self) -> None:
        # ── 底部：输入区（**先** pack：它是固定高度那块，先摆才不会被对话流挤出去）──
        composer = tk.Frame(self, bg=theme.CONTENT_BG)
        composer.pack(side=tk.BOTTOM, fill=tk.X, padx=24, pady=(0, 14))

        row = tk.Frame(composer, bg=theme.CONTENT_BG)
        row.pack(fill=tk.X)
        box = tk.Frame(row, bg=theme.FIELD_BG, highlightthickness=1,
                       highlightbackground=theme.FIELD_LINE, highlightcolor=theme.FIELD_LINE, bd=0)
        box.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._input = tk.Text(box, height=2, font=theme.FONT_INPUT, bg=theme.FIELD_BG,
                              fg=theme.TEXT, relief=tk.FLAT, bd=0, highlightthickness=0,
                              wrap="word", padx=12, pady=10, insertbackground=theme.TEXT)
        self._input.pack(fill=tk.X)
        self._input.bind("<Return>", self._on_return)
        self._input.bind("<Shift-Return>", lambda _e: None)  # 换行走默认行为
        self._input.bind("<FocusIn>", lambda _e: (self._clear_placeholder(),
                                                  box.configure(highlightbackground=theme.GREEN,
                                                                highlightcolor=theme.GREEN)))
        self._input.bind("<FocusOut>", lambda _e: (self._restore_placeholder(),
                                                   box.configure(highlightbackground=theme.FIELD_LINE,
                                                                 highlightcolor=theme.FIELD_LINE)))
        self._placeholder_on = False
        self._restore_placeholder()

        self._send_btn = _RoundButton(row, self.send)
        self._send_btn.pack(side=tk.LEFT, padx=(12, 0))

        tk.Label(composer, text="Enter 发送 · Shift + Enter 换行", font=theme.FONT_SMALL,
                 bg=theme.CONTENT_BG, fg=theme.TEXT_MUTED, anchor="w").pack(fill=tk.X, pady=(6, 0))

        # ── 上面：对话流（可滚动）──
        body = tk.Frame(self, bg=theme.CONTENT_BG)
        canvas = tk.Canvas(body, bg=theme.CONTENT_BG, highlightthickness=0, bd=0)
        scroll = _SlimScroll(body, canvas.yview_moveto)
        self._list = tk.Frame(canvas, bg=theme.CONTENT_BG)
        window_id = canvas.create_window((0, 0), window=self._list, anchor="nw")
        self._list.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: self._on_resize(canvas, window_id, e.width))
        canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side=tk.RIGHT, fill=tk.Y, padx=(8, 0))
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._canvas = canvas
        # 滚轮：进出这一页时装卸（和配置中心同一个路数，别把别的页的滚动劫走）
        body.bind("<Enter>", lambda _e: body.bind_all("<MouseWheel>", self._on_wheel))
        body.bind("<Leave>", lambda _e: body.unbind_all("<MouseWheel>"))
        body.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=24, pady=(0, 8))

    def _on_resize(self, canvas: tk.Canvas, window_id: int, width: int) -> None:
        canvas.itemconfigure(window_id, width=width)
        self._bubble_width = max(_BUBBLE_MIN, int(width * _BUBBLE_RATIO))
        for label in self._list.winfo_children():
            for child in _walk(label):
                if isinstance(child, tk.Label) and getattr(child, "_wraps", False):
                    child.configure(wraplength=self._bubble_width)

    def _on_wheel(self, event: Any) -> None:
        self._canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")

    # ── 发送 ──────────────────────────────────────────────────────

    def _on_return(self, _event: Any) -> str:
        self.send()
        return "break"  # 不要再插一个换行

    def send(self) -> None:
        if self._future is not None:
            return  # 一轮还没完。会话是串行的，排队只会让人以为发出去了
        text = self._current_text()
        if not text:
            return
        try:
            future = self.service.debug_turn(text, on_delta=self._on_delta, on_stage=self._on_stage)
        except ServiceNotRunning as exc:
            # 不清输入框：他打的那句话还在，起了服务点一下就能发出去
            self._say_error(str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - 发不出去要给个说法，不能静默
            self._say_error(f"发不出去：{exc}")
            return
        self._input.delete("1.0", tk.END)
        self._my_mark = self._say_me(text)
        self._future = future
        self._stream = ""
        self._stage_text = "正在发给 AI 端…"
        self._started_at = time.monotonic()
        self._live = None
        self._show_pending()
        self._send_btn.set_busy(True)

    def reset(self) -> None:
        """开一轮新的。**不清屏**——上一轮说了什么正是排障时要回看的。"""
        self.service.reset_debug_session()
        self._open_cards.clear()
        self._say_hint("已开始新会话，上下文从这里断开。")

    def stop(self) -> None:
        """停止这一轮。只是让 AI 别再往下算，**已经下发的指令不会被撤回**。"""
        self.service.interrupt_debug()
        self._stage_text = "正在停止…"

    # 这两个回调**跑在网关线程上**：只准往队列里放，绝不碰控件。
    def _on_delta(self, delta: str) -> None:
        self._events.put(("delta", delta))

    def _on_stage(self, stage: dict) -> None:
        self._events.put(("stage", stage))

    def on_command_result(self, body: dict) -> None:
        """指令回执（网关线程）。同样只入队。"""
        self._events.put(("receipt", body))

    # ── 心跳 ──────────────────────────────────────────────────────

    def _pump(self) -> None:
        try:
            self._drain()
            self._render_connection()
            self._render_pending()
        except tk.TclError:
            return  # 页面正在销毁
        finally:
            # 页面已经销毁时 after 会抛——那就是「不用再排下一拍了」
            with suppress(tk.TclError):
                self.after(PUMP_MS, self._pump)

    def _drain(self) -> None:
        while True:
            try:
                kind, payload = self._events.get_nowait()
            except queue.Empty:
                break
            if kind == "delta":
                self._stream += payload
                self._render_stream()
            elif kind == "stage":
                text = str(payload.get("text") or "").strip()
                if text:
                    self._stage_text = text
            elif kind == "receipt":
                self._render_receipt(payload)

        future = self._future
        if future is None or not future.done():
            return
        self._future = None
        self._hide_pending()
        self._send_btn.set_busy(False)
        try:
            result = future.result()
        except Exception as exc:  # noqa: BLE001 - 超时/断线/票据失效都照原样说出来
            self._mark_turn("×", theme.RED)
            self._say_error(f"{type(exc).__name__}：{exc}")
            return
        self._render_reply(result)

    def _mark_turn(self, glyph: str, color: str) -> None:
        """给自己那句话右边落个下场标记。**只在这一轮真收口之后调**。"""
        mark, self._my_mark = self._my_mark, None
        if mark is None:
            return
        with suppress(tk.TclError):
            mark.configure(text=glyph, fg=color)

    def _render_stream(self) -> None:
        """实时把 AI 正在说的话写进气泡——**边说边显示**，不等这一轮收口。"""
        text = self._stream.strip()
        if not text:
            return
        if self._live is None:
            self._live = self._say_ai(text)
        else:
            self._live.configure(text=text)
        self._scroll_to_end()

    def _render_reply(self, result: Any) -> None:
        reply = format_turn(result)
        if reply.error:
            self._mark_turn("×", theme.RED)
            self._say_error(f"AI 端报错：{reply.error}")
            return
        self._mark_turn("✓", theme.GREEN)
        if reply.text:
            if self._live is not None:
                self._live.configure(text=reply.text)  # 用收口后的正文覆盖流式那份
            else:
                self._say_ai(reply.text)
        elif self._live is None and not reply.commands and not reply.raw:
            self._say_hint("这一轮没有任何产出。")
        for line in reply.commands:
            self._add_command_card(line)
        for line in reply.skipped:
            self._say_hint(f"没下发：{line}")
        if reply.raw:
            self._say_hint(f"这一轮没给出能认的产出，原样贴在这儿：\n{reply.raw}")
        self._live = None
        self._scroll_to_end()

    def _render_receipt(self, body: dict) -> None:
        receipt = format_receipt(body)
        if receipt is None:
            return
        for index, (op, card) in enumerate(self._open_cards):
            if op == receipt.op:
                card.apply(receipt)
                if receipt.settled:
                    self._open_cards.pop(index)
                return
        # 没有对应的卡（比如上一轮的指令、或排期到点才执行的）——单独补一张
        card = self._add_command_card(receipt.op)
        card.apply(receipt)
        if not receipt.settled:
            self._open_cards.append((receipt.op, card))

    def _render_connection(self) -> None:
        """标题栏右边那句「AI 已连接」。**据实报**——连不上时说连不上。"""
        if not hasattr(self, "_conn_dot"):
            return
        status = self.service.status()
        if self.service.state is not ServiceState.RUNNING:
            text, color = "服务没在运行", theme.GRAY
        elif status.connect_error:
            text, color = "AI 连不上", theme.RED
        elif status.connected > 0:
            text, color = "AI 已连接", theme.GREEN_BRIGHT
        else:
            text, color = "正在连接 AI", theme.ORANGE
        self._conn_dot.configure(fg=color)
        self._conn_text.configure(text=text, fg=theme.TEXT_MUTED)

    # ── 「正在…」那一行 ───────────────────────────────────────────

    def _show_pending(self) -> None:
        row = tk.Frame(self._list, bg=theme.CONTENT_BG)
        row.pack(fill=tk.X, pady=(4, 10))
        left = tk.Frame(row, bg=theme.CONTENT_BG)
        left.pack(side=tk.LEFT, padx=(_INDENT, 0))
        self._dots = [tk.Label(left, text="●", font=(theme.UI, 8), bg=theme.CONTENT_BG,
                               fg=theme.GRAY) for _ in range(3)]
        for dot in self._dots:
            dot.pack(side=tk.LEFT, padx=2)
        self._pending_text = tk.Label(left, text="", font=theme.FONT_BODY,
                                      bg=theme.CONTENT_BG, fg=theme.TEXT_MUTED)
        self._pending_text.pack(side=tk.LEFT, padx=(8, 0))
        self._elapsed = tk.Label(row, text="", font=theme.FONT_SMALL,
                                 bg=theme.CONTENT_BG, fg=theme.TEXT_MUTED)
        self._elapsed.pack(side=tk.LEFT, padx=(14, 0))
        stop = tk.Label(row, text="停止", font=theme.FONT_SMALL, bg=theme.CONTENT_BG,
                        fg=theme.TEXT_MUTED, cursor="hand2", padx=10, pady=3,
                        highlightthickness=1, highlightbackground=CARD_LINE,
                        highlightcolor=CARD_LINE, bd=0)
        stop.pack(side=tk.RIGHT)
        stop.bind("<Button-1>", lambda _e: self.stop())
        self._pending = row
        self._scroll_to_end()

    def _render_pending(self) -> None:
        if self._pending is None:
            return
        self._frame = (self._frame + 1) % len(_DOT_FRAMES)
        for dot, on in zip(self._dots, _DOT_FRAMES[self._frame]):
            dot.configure(fg=theme.GREEN if on else "#cfd4d9")
        self._pending_text.configure(text=self._stage_text or "正在处理…")
        self._elapsed.configure(text=f"已用时 {int(time.monotonic() - self._started_at)} 秒")

    def _hide_pending(self) -> None:
        if self._pending is not None:
            self._pending.destroy()
            self._pending = None

    # ── 往对话流里加东西 ──────────────────────────────────────────

    def _say_ai(self, text: str) -> tk.Label:
        row = tk.Frame(self._list, bg=theme.CONTENT_BG)
        row.pack(fill=tk.X, pady=(14, 2), anchor="w")
        # ★ 头像那一格**宽高都要给**（2026-08-17 用户：「图片显示不全，一条线」）。
        # `pack_propagate(False)` 的意思是「别按内容算尺寸」，只给了 width 的话高度就退回
        # 默认的 1px —— 图被裁成一条横线，正是屏幕上看到的那样。
        avatar = tk.Frame(row, bg=theme.CONTENT_BG, width=_INDENT, height=_AVATAR)
        avatar.pack(side=tk.LEFT, anchor="n")
        avatar.pack_propagate(False)
        photo = icons.logo_photo(self, _AVATAR)
        if photo is not None:
            label = tk.Label(avatar, image=photo, bg=theme.CONTENT_BG)
            label._photo = photo  # 自己留引用，被 GC 掉就成了空白
            label.pack(anchor="nw")
        else:
            tk.Label(avatar, text="智", font=(theme.UI, 10, "bold"),
                     bg=theme.CONTENT_BG, fg=theme.GREEN).pack(anchor="nw")
        col = tk.Frame(row, bg=theme.CONTENT_BG)
        col.pack(side=tk.LEFT, fill=tk.X, expand=True)
        head = tk.Frame(col, bg=theme.CONTENT_BG)
        head.pack(anchor="w")
        tk.Label(head, text=AI_NAME, font=(theme.UI, 10, "bold"),
                 bg=theme.CONTENT_BG, fg=theme.TEXT).pack(side=tk.LEFT)
        tk.Label(head, text=time.strftime("%H:%M:%S"), font=theme.FONT_SMALL,
                 bg=theme.CONTENT_BG, fg=theme.TEXT_MUTED).pack(side=tk.LEFT, padx=(8, 0))
        body = tk.Label(col, text=text, font=theme.FONT_BODY, bg=theme.CONTENT_BG,
                        fg=theme.TEXT, wraplength=self._bubble_width, justify=tk.LEFT, anchor="w")
        body._wraps = True
        body.pack(anchor="w", pady=(4, 0))
        self._scroll_to_end()
        return body

    def _say_me(self, text: str) -> tk.Label:
        """自己说的话：右侧绿气泡 + 时间 + 一个「这一轮的下场」标记。

        标记是**空着起手**的：发出去了不代表 AI 端接住了。这一轮真收口才画勾，
        报错就画叉——和指令卡同一条规矩（见本模块开头那段）。
        """
        row = tk.Frame(self._list, bg=theme.CONTENT_BG)
        row.pack(fill=tk.X, pady=(14, 2))
        mark = tk.Label(row, text=" ", font=(theme.UI, 10, "bold"), width=2,
                        bg=theme.CONTENT_BG, fg=theme.GREEN)
        mark.pack(side=tk.RIGHT)
        tk.Label(row, text=time.strftime("%H:%M:%S"), font=theme.FONT_SMALL,
                 bg=theme.CONTENT_BG, fg=theme.TEXT_MUTED).pack(side=tk.RIGHT, padx=(10, 2))
        bubble = tk.Label(row, text=text, font=theme.FONT_BODY, bg=BUBBLE_ME_BG, fg=theme.TEXT,
                          wraplength=self._bubble_width, justify=tk.LEFT,
                          padx=14, pady=10, anchor="w")
        bubble._wraps = True
        bubble.pack(side=tk.RIGHT)
        self._scroll_to_end()
        return mark

    def _say_hint(self, text: str) -> None:
        label = tk.Label(self._list, text=text, font=theme.FONT_SMALL, bg=theme.CONTENT_BG,
                         fg=theme.TEXT_MUTED, wraplength=self._bubble_width * 2,
                         justify=tk.LEFT, anchor="w")
        label._wraps = False
        label.pack(fill=tk.X, padx=(_INDENT, 0), pady=(2, 2))
        self._scroll_to_end()

    def _say_error(self, text: str) -> None:
        row = tk.Frame(self._list, bg=theme.CONTENT_BG)
        row.pack(fill=tk.X, padx=(_INDENT, 0), pady=(6, 2))
        tk.Label(row, text="×", font=(theme.UI, 11, "bold"), bg=theme.CONTENT_BG,
                 fg=theme.RED).pack(side=tk.LEFT, anchor="n")
        body = tk.Label(row, text=text, font=theme.FONT_BODY, bg=theme.CONTENT_BG, fg=theme.RED,
                        wraplength=self._bubble_width, justify=tk.LEFT, anchor="w")
        body._wraps = True
        body.pack(side=tk.LEFT, padx=(8, 0))
        self._scroll_to_end()

    def _add_command_card(self, summary: str) -> _CommandCard:
        op = summary.split("（", 1)[0].strip()
        detail = summary[len(op):].strip("（）") if "（" in summary else ""
        card = _CommandCard(self._list, op=op, detail=detail, on_open_log=self._on_open_log,
                            bubble_width=self._bubble_width)
        card.pack(fill=tk.X, padx=(_INDENT, 0), pady=(8, 4))
        self._open_cards.append((op, card))
        self._scroll_to_end()
        return card

    # ── 杂 ────────────────────────────────────────────────────────

    def _scroll_to_end(self) -> None:
        """只在人本来就贴着底时才跟着滚：他往上翻着看历史时别把他拽回来。"""
        try:
            at_bottom = self._canvas.yview()[1] >= 0.995
            self._canvas.update_idletasks()
            self._canvas.configure(scrollregion=self._canvas.bbox("all"))
            if at_bottom:
                self._canvas.yview_moveto(1.0)
        except tk.TclError:
            return  # 正在销毁

    def _current_text(self) -> str:
        if self._placeholder_on:
            return ""
        return self._input.get("1.0", tk.END).strip()

    def _clear_placeholder(self) -> None:
        if self._placeholder_on:
            self._input.delete("1.0", tk.END)
            self._input.configure(fg=theme.TEXT)
            self._placeholder_on = False

    def _restore_placeholder(self) -> None:
        if not self._input.get("1.0", tk.END).strip():
            self._input.delete("1.0", tk.END)
            self._input.insert("1.0", PLACEHOLDER)
            self._input.configure(fg=theme.TEXT_MUTED)
            self._placeholder_on = True


class _CommandCard(tk.Frame):
    """一条指令在界面上的那张卡：起手「待下发」，等真回执来了才改口。"""

    def __init__(self, parent: tk.Misc, *, op: str, detail: str,
                 on_open_log: Callable[[], None] | None, bubble_width: int) -> None:
        super().__init__(parent, bg=theme.CONTENT_BG, highlightthickness=1,
                         highlightbackground=CARD_LINE, highlightcolor=CARD_LINE, bd=0)
        inner = tk.Frame(self, bg=theme.CONTENT_BG)
        inner.pack(fill=tk.X, padx=14, pady=11)
        self._icon = _StatusGlyph(inner)
        self._icon.pack(side=tk.LEFT, anchor="n", padx=(0, 11))
        col = tk.Frame(inner, bg=theme.CONTENT_BG)
        col.pack(side=tk.LEFT, fill=tk.X, expand=True)
        head = tk.Frame(col, bg=theme.CONTENT_BG)
        head.pack(anchor="w", fill=tk.X)
        self._op = tk.Label(head, text=op, font=(theme.MONO, 9, "bold"), bg=theme.CONTENT_BG,
                            fg=theme.TEXT)
        self._op.pack(side=tk.LEFT)
        self._status = tk.Label(head, text="· 待下发", font=(theme.UI, 9, "bold"),
                                bg=theme.CONTENT_BG, fg=theme.TEXT_MUTED)
        self._status.pack(side=tk.LEFT, padx=(6, 0))
        if detail:
            body = tk.Label(col, text=detail, font=theme.FONT_SMALL, bg=theme.CONTENT_BG,
                            fg=theme.TEXT_MUTED, wraplength=bubble_width, justify=tk.LEFT,
                            anchor="w")
            body._wraps = True
            body.pack(anchor="w", pady=(3, 0))
        self._reason = tk.Label(col, text="", font=theme.FONT_SMALL, bg=theme.CONTENT_BG,
                                fg=theme.ORANGE, wraplength=bubble_width, justify=tk.LEFT,
                                anchor="w")
        self._reason._wraps = True
        if on_open_log is not None:
            link = tk.Label(inner, text="查看实时日志 ›", font=theme.FONT_SMALL,
                            bg=theme.CONTENT_BG, fg=theme.GREEN, cursor="hand2")
            link.pack(side=tk.RIGHT, anchor="n")
            link.bind("<Button-1>", lambda _e: on_open_log())

    def apply(self, receipt: Any) -> None:
        """回执来了：改状态、改颜色；没做成的把原因摆出来。"""
        color = theme.TEXT_MUTED if receipt.ok is None else (
            theme.GREEN if receipt.ok else theme.RED
        )
        self._icon.paint(receipt.ok)
        self._status.configure(text=f"· {receipt.label}", fg=color)
        self._op.configure(fg=theme.TEXT if receipt.ok is None else color)
        if receipt.reason:
            self._reason.configure(text=receipt.reason,
                                   fg=theme.ORANGE if receipt.ok is None else color)
            if not self._reason.winfo_manager():
                self._reason.pack(anchor="w", pady=(3, 0))


class _SlimScroll(tk.Canvas):
    """右边那根细滚动条（设计稿 docs/chat.png）。

    系统那根 `tk.Scrollbar` 在 Windows 上是带上下箭头的经典灰槽，摆在这一页里像另一个
    年代的东西（2026-08-17 真机截图）。这根只有一条圆头细线，内容没超出时干脆不画。
    """

    WIDTH = 8
    #: 拖块再短也得抓得住。
    MIN_THUMB = 28

    def __init__(self, parent: tk.Misc, on_move: Callable[[float], None]) -> None:
        super().__init__(parent, width=self.WIDTH, bg=theme.CONTENT_BG,
                         highlightthickness=0, bd=0)
        self._first, self._last = 0.0, 1.0
        self._on_move = on_move
        self.bind("<Configure>", lambda _e: self._render())
        self.bind("<Button-1>", self._jump)
        self.bind("<B1-Motion>", self._jump)

    def set(self, first: Any, last: Any) -> None:
        """给 `yscrollcommand` 用，签名要和 `tk.Scrollbar.set` 一样。"""
        self._first, self._last = float(first), float(last)
        self._render()

    def _jump(self, event: Any) -> None:
        height = max(1, self.winfo_height())
        span = max(0.02, self._last - self._first)
        self._on_move(min(1.0, max(0.0, event.y / height - span / 2)))

    def _render(self) -> None:
        try:
            self.delete("all")
        except tk.TclError:
            return
        if self._last - self._first >= 0.999:
            return  # 内容没超出，一根条都不画
        height = max(1, self.winfo_height())
        top = self._first * height
        bottom = max(top + self.MIN_THUMB, self._last * height)
        r = (self.WIDTH - 2) / 2
        color = "#d3d8de"
        self.create_oval(1, top, self.WIDTH - 1, top + 2 * r, fill=color, outline="")
        self.create_oval(1, bottom - 2 * r, self.WIDTH - 1, bottom, fill=color, outline="")
        self.create_rectangle(1, top + r, self.WIDTH - 1, bottom - r, fill=color, outline="")


class _StatusGlyph(tk.Canvas):
    """指令卡左边那颗圆图标：待下发是一圈空心灰，成了画绿勾，没成画红叉。

    画而不是打字：`✓` 这类字符在不同字体下大小位置都不一样，设计稿要的是一颗**对齐的**
    圆圈（docs/chat.png）。
    """

    SIZE = 20

    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent, width=self.SIZE, height=self.SIZE, bg=theme.CONTENT_BG,
                         highlightthickness=0, bd=0)
        self.paint(None)

    def paint(self, ok: bool | None) -> None:
        self._ok = ok   # 当前画的是哪种下场（测试拿它断言，别删）
        try:
            self.delete("all")
        except tk.TclError:
            return
        color = theme.GRAY if ok is None else (theme.GREEN if ok else theme.RED)
        s = self.SIZE
        self.create_oval(2, 2, s - 2, s - 2, outline=color, width=2)
        if ok is None:
            return
        if ok:
            self.create_line(6, s / 2, s / 2 - 1, s - 7, s - 6, 6,
                             fill=color, width=2, capstyle=tk.ROUND, joinstyle=tk.ROUND)
        else:
            self.create_line(7, 7, s - 7, s - 7, fill=color, width=2, capstyle=tk.ROUND)
            self.create_line(s - 7, 7, 7, s - 7, fill=color, width=2, capstyle=tk.ROUND)


class _RoundButton(tk.Canvas):
    """设计稿右下角那颗绿色圆形发送键（tk 没有圆按钮，画一个）。"""

    SIZE = 44

    def __init__(self, parent: tk.Misc, command: Callable[[], None]) -> None:
        super().__init__(parent, width=self.SIZE, height=self.SIZE, bg=theme.CONTENT_BG,
                         highlightthickness=0, bd=0, cursor="hand2")
        self._command = command
        self._busy = False
        self.bind("<Button-1>", lambda _e: None if self._busy else command())
        self._render()

    def set_busy(self, busy: bool) -> None:
        if busy == self._busy:
            return
        self._busy = busy
        self.configure(cursor="watch" if busy else "hand2")
        self._render()

    def _render(self) -> None:
        try:
            self.delete("all")
        except tk.TclError:
            return
        color = "#cfd4d9" if self._busy else theme.GREEN
        self.create_oval(2, 2, self.SIZE - 2, self.SIZE - 2, fill=color, outline=color)
        self.create_text(self.SIZE // 2, self.SIZE // 2 - 1, text="↑",
                         font=(theme.UI, 15, "bold"), fill="#ffffff")


def _walk(widget: tk.Misc):
    """深度遍历（改 wraplength 用）。"""
    yield widget
    for child in widget.winfo_children():
        yield from _walk(child)
