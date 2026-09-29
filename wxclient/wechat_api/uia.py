from __future__ import annotations

import random
import re
import sys
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import date
from pathlib import Path
from typing import Any, ClassVar

import structlog

from wxclient.heavy_import import import_wechat_driver
from wxclient.uia_probe import probe_wechat_uia
from wxclient.uia_probe.constants import (
    DEFAULT_UIA_PROBE_MAX_DEPTH,
    DEFAULT_UIA_PROBE_MAX_NODES,
)

from .errors import MomentsDockedLayoutError, WeChatApiError, WeChatApiUnavailableError

logger = structlog.get_logger(__name__)

#: `_disable_esc_close_fallback` 只需要生效一次，模块级记一下别反复包。
_ESC_CLOSE_FALLBACK_DISABLED = False

#: 输入框「还没 enabled」时，最多等它几秒再重来一次。
#: 3 秒是按真机现象给的：切完聊天窗口到输入框可用通常在半秒内，卡住的那次也只是差一拍。
#: 给太长会把一次本该快速失败的调用拖成十几秒，给太短等于没等。
EDIT_ENABLE_WAIT_SECONDS = 3.0
EDIT_ENABLE_POLL_SECONDS = 0.2


def _is_element_not_enabled(exc: BaseException) -> bool:
    """这个异常是不是 pywinauto 的 ``ElementNotEnabled``。

    **按类名认，不用 isinstance**：Linux 上单测环境根本没有 pywinauto，import 它会炸，
    而「该不该重试」这段逻辑恰恰是必须在无 GUI 的机器上测的（真机测试只在发版时跑一遍，
    这种判据错了要等到线上才发现）。
    """
    return type(exc).__name__ == "ElementNotEnabled"


def _is_not_friend_error(exc: BaseException) -> bool:
    """这个异常是不是 pyweixin 的 ``NotFriendError``（「非正常好友,无法发送消息」）。

    名字听着像「对方不是好友」，实际判据只是：点开会话后 **0.1 秒内没找到输入框**
    （`WeChatAuto.send_messages_to_friend` / `send_files_to_friend` 的 `edit_area.exists(timeout=0.1)`）。
    2026-09-17 真机：好友是真的，前一条、后一条都发成了，唯独刚读完他的消息立刻回的那一条
    撞上这句——微信那一刻的界面状态而已。同样按类名认，理由见 `_is_element_not_enabled`。
    """
    return type(exc).__name__ == "NotFriendError"



def _disable_esc_close_fallback() -> None:
    """让 pywinauto 的 ``close()`` 关不掉时**什么都不做**，而不是回退去按 ESC。

    pywinauto 的 ``UIAWrapper.close()`` 只有支持 Window 模式的控件才真关得掉；
    关不掉它就 ``type_keys("{ESC}")`` 兜底（它源码里的理由是「menu shadows, tooltips」）。
    可微信 4.x 里 **ESC = 关窗口** —— 一下就把整个微信收进右下角托盘，UIA 子树跟着被收走，
    之后每条指令、每次读消息全失败，而且 ``_restore_if_minimized`` 救不回来（那只救最小化），
    只能人去点托盘图标。同一个坑我们自己在 ``_leave_search`` 踩过一次，那次是自己按的 ESC；
    这次是 **vendor 里那些 ``close()``** 替我们按 —— 我们改不到每一处调用，就把兜底本身摘掉。

    关不掉的面板顶多留在屏幕上，下一个动作会把它带走；把微信关进托盘是不可逆的。
    两害相权取其轻：**关不掉就不关。**

    pywinauto 装不上（Linux 上跑单测）时静默跳过——这条只在真机上才有意义。
    """
    global _ESC_CLOSE_FALLBACK_DISABLED
    if _ESC_CLOSE_FALLBACK_DISABLED:
        return
    try:
        from pywinauto import uia_defines
        from pywinauto.controls.uiawrapper import UIAWrapper
    except Exception:      # noqa: BLE001 - 没有 pywinauto 就没有这个兜底，无事可做
        return

    def close_without_esc(self: Any) -> None:
        try:
            self.iface_window.Close()
        except uia_defines.NoPatternInterfaceError:
            # 原实现在这儿按 ESC。留个日志，别留个「微信没了」。
            logger.info("uia.close.no_window_pattern")

    UIAWrapper.close = close_without_esc
    _ESC_CLOSE_FALLBACK_DISABLED = True


def parse_wechat_version(text: Any) -> tuple[int, ...]:
    """``"4.1.12.26"`` → ``(4, 1, 13, 65)``；读不出来就空元组（**不猜**）。"""
    if not isinstance(text, str):
        return ()
    parts: list[int] = []
    for piece in text.strip().split("."):
        if not piece.isdigit():
            break
        parts.append(int(piece))
    return tuple(parts)


#: 朋友圈版式按微信版本分叉：(最低版本, 版式)，从高往低第一个够得着的算数。
#:
#: · ``docked``（4.1.13 起）——朋友圈**停靠进主窗口**（`mmui::SNSContentView`），
#:   没有独立的 `mmui::SNSWindow`，也没有工具栏上的「发表」按钮；发表入口是相册
#:   「今天」那一行里的**相机格**（`mmui::AlbumContentCell`「发表」），点一下直接弹
#:   系统「选择文件」框，选完图才出编辑面板（`SnsPublishPanel`）。**没有纯文字入口**
#:   （2026-09-15 真机 4.1.13.12：右键、长按、取消选文件三条路都试过，都没有）。
#: · ``window``（更早）——独立朋友圈窗口，右键「发表」按钮选图片/纯文字模式。
MOMENTS_LAYOUTS: tuple[tuple[tuple[int, ...], str], ...] = (
    ((4, 1, 13), "docked"),
    ((), "window"),
)


def moments_layout(version: Any) -> str:
    """按微信进程版本挑朋友圈版式。版本读不到 → 按老版式走，跑到再靠现场认（见 `_open_moments_timeline`）。"""
    parsed = parse_wechat_version(version)
    if not parsed:
        return "window"
    for floor, layout in MOMENTS_LAYOUTS:
        if parsed >= floor:
            return layout
    return "window"


def find_gray_tile(
    pixels: Any, width: int, height: int, *, step: int = 2, min_hits: int = 200,
) -> tuple[int, int] | None:
    """在「发表」那一格的截图里找相机格（一块中灰色的方块），返回它的中心（相对格子左上角）。

    ★ 为什么截图找而不是写死偏移（2026-09-15 真机 4.1.13.12）：那一格 UIA 只暴露成一个
      `AlbumContentCell`，相机方块**不是子控件**，点格子中心没反应；方块离左边多远随窗口宽度、
      系统缩放走。而它的颜色很稳（(225,225,227) 常态、(213,213,215) 悬停），背景是 (250,250,250)
      的近白——按颜色认，中心取中位数，抗零星噪点。

    ``pixels`` 是 PIL 的 ``Image.load()``（按 ``pixels[x, y]`` 取色），任何同形状的都行。
    """
    xs: list[int] = []
    ys: list[int] = []
    for y in range(0, height, step):
        for x in range(0, width, step):
            r, g, b = pixels[x, y][:3]
            if 198 <= r <= 238 and abs(r - g) < 4 and -2 <= b - r <= 6:
                xs.append(x)
                ys.append(y)
    if len(xs) < min_hits:
        return None
    xs.sort()
    ys.sort()
    return xs[len(xs) // 2], ys[len(ys) // 2]


def find_bubble_span(
    pixels: Any, width: int, height: int, *, tolerance: int = 6, min_width: int = 30,
) -> tuple[int, int] | None:
    """在一条消息行的截图里找气泡本体，返回它的中心（相对行左上角）；认不出回 None。

    ★ 为什么截图找（2026-09-24 真机 4.1.13.12）：消息行的 ``ListItem`` 横跨整个聊天区，
      UIA 下面**没有子控件**，气泡在哪一侧、多宽都拿不到；点行中心落在空白上，右键菜单不弹。
      行中心那一点就是背景色（对方发的在左、自己发的在右，中间总是空的），拿它当背景，
      在几条横线上找「跟背景不一样」的连续段，**最长那段**就是气泡——头像只有 40px 上下。
    ★ 容差要小：文件气泡是纯白 (255,255,255)，聊天背景是 (250,250,250)，三通道差加起来才 15。
    ★ 背景取**行最左边**那一点，不取行中心（2026-09-28 真机）：文件卡片宽 300px 上下、从 x≈88 起，
      正好盖住 598px 宽那一行的中心——中心取到的是卡片本身的灰，于是「跟背景不一样」的最长段
      成了右边那片空白，右键点空，线上报「右键菜单里没有『另存为』」。行最左边在头像（x≈16 起）外侧，
      对方发的、自己发的都是空白。
    """
    background = pixels[2, height // 2][:3]
    best: tuple[int, int, int] | None = None
    for fraction in (0.5, 0.35, 0.65):
        y = min(height - 1, int(height * fraction))
        start: int | None = None
        for x in range(width + 1):
            differs = x < width and sum(
                abs(a - b) for a, b in zip(pixels[x, y][:3], background)
            ) > tolerance
            if differs and start is None:
                start = x
            elif not differs and start is not None:
                if best is None or x - start > best[1] - best[0]:
                    best = (start, x, y)
                start = None
    if best is None or best[1] - best[0] < min_width:
        return None
    return (best[0] + best[1]) // 2, best[2]


def find_save_dialog(windows: Any) -> Any | None:
    """从一组窗口里挑出「另存为」对话框：Win32 ``#32770`` 且标题像保存（跟系统语言走）。"""
    for candidate in windows:
        with suppress(Exception):
            if candidate.element_info.class_name != "#32770":
                continue
            title = str(candidate.element_info.name or "")
            if any(mark in title for mark in _SAVE_DIALOG_TITLE_MARKS):
                return candidate
    return None


_SAVE_DIALOG_TITLE_MARKS = ("另存为", "另存為", "Save As", "Save as", "保存")


def dialog_control(dialog: Any, control_type: str, automation_id: str) -> Any | None:
    """在保存对话框里按 (控件类型, AutomationId) 找控件：文件名框 1001、「保存」1、「取消」2（Win32 通用对话框的固定 ID）。

    ★ 对话框是从 ``window.children()`` 拿到的 **wrapper**，没有 ``child_window``（那是
      ``WindowSpecification`` 才有的）——2026-09-24 真机第一次端到端就栽在这：调用抛 AttributeError
      被吞，报「找不到文件名输入框」。所以遍历 ``descendants()`` 按 element_info 比。
    """
    for control in dialog.descendants():
        with suppress(Exception):
            info = control.element_info
            if info.control_type == control_type and str(info.automation_id or "") == automation_id:
                return control
    return None


def save_path_for(target: Path, default_name: str) -> Path:
    """保存对话框该填的完整路径：我们的目录 + 微信给的默认名（只取文件名那段，防默认名里带路径）。"""
    name = Path(str(default_name or "").replace("\\", "/")).name.strip()
    if not name:
        raise WeChatApiError("保存对话框里没有默认文件名")
    return target / name


def _edit_value(edit: Any) -> str:
    try:
        return str(edit.get_value() or "")
    except Exception:  # noqa: BLE001 —— 不支持 ValuePattern 的退回窗口文字
        return str(_safe_call(edit.window_text) or "")


def _cancel_save_dialog(dialog: Any) -> None:
    """点对话框的「取消」关掉它。只点对话框自己的按钮，绝不按 ESC（焦点一偏就关掉微信主窗口）。"""
    with suppress(Exception):
        button = dialog_control(dialog, "Button", "2")
        if button is not None:
            button.invoke()


def _await_saved_file(target: Path, before: set[str], timeout: float) -> Path:
    """等目录里多出一个文件且大小不再变化（微信是流式写盘的）。扩展名由微信补，所以不按文件名等。"""
    deadline = time.monotonic() + timeout
    last_size: dict[str, int] = {}
    while time.monotonic() < deadline:
        fresh = [entry for entry in target.iterdir() if entry.name not in before and entry.is_file()]
        fresh = [entry for entry in fresh if not entry.name.lower().endswith((".tmp", ".crdownload", ".part"))]
        if fresh:
            entry = max(fresh, key=lambda e: e.stat().st_mtime)
            size = entry.stat().st_size
            if size > 0 and last_size.get(entry.name) == size:
                return entry
            last_size[entry.name] = size
        time.sleep(0.5)
    raise WeChatApiError("另存为没有落下文件（等超时了）")


class UiaWeChatDriver:
    driver_name = "uia"

    def __init__(
        self,
        project_root: Path | None = None,
        dry_run: bool = True,
        wechat_path: str | Path | None = None,
    ) -> None:
        self.project_root = project_root or Path(__file__).resolve().parents[2]
        self.dry_run = dry_run
        self.wechat_path = wechat_path
        # 下面各动作里 `from pywinauto import ...` 全是惰性的：首次加载排进闸里做（heavy_import.py），
        # 别让网关线程的第一个动作去和探测线程撞加载器锁。
        import_wechat_driver()
        #: `_ensure_ready` 探测到的微信进程版本（如 ``4.1.12.26``），朋友圈按它挑版式。
        self._wechat_version = ""

    def add_friend(
        self,
        number: str,
        greetings: str,
        remark: str | None = None,
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        """按手机号/微信号加好友，并把**后续拿什么名字回消息**一并带回来。

        返回里几个名字各有各的用处，别混：
          · ``name``         —— 申请时对方名片上的**昵称**。会变、会重名、可能带 emoji，
                                只适合给人看，**不要拿它当回复的锚**。
          · ``remark``       —— 我们自己设的备注。对方通过后，会话列表里显示的就是它。
          · ``session_name`` —— 后续 ``send_message(to=...)`` 该用的名字 = 备注真填上了就是备注，
                                没填上才退回昵称。这是唯一该被记下来的那个。
          · ``profile_name`` —— 确定是**昵称**的那个值，只在拿得准时才有（见下）。
          · ``already_friend`` —— 这个号早就是好友了。

        ★ 已经是好友时不再当失败抛（2026-08-26 用户提的场景）：名片这一趟已经搜完了，
          名字也已经读到手，抛掉等于白搜一次陌生号（节拍红线：查好友 ≥300s 一次）而且
          这个人在计划里永远停在原地。改成如实回一条「他已经是好友了」，昵称跟着回来，
          由上层报回业务端。

        ★ 备注按**名字**找「修改备注」那一栏填（用户 2026-09-23：「备注要填，只是不要填到昵称上去」）。
          原来盲填申请窗里的「第 2 个输入框」（vendor 也这么写），那一格不是备注栏，
          候选人名字被填进了昵称那一格。找不到备注栏就**不填**，绝不退回按序号猜。

        ⚠️ 加好友是**异步**的：``already_friend`` 为假时，返回成功只代表申请发出去了，
        对方没通过就没有会话、也发不了消息。真正"可以回了"的信号是
        ``check_new_friends`` / 对方通过。
        """
        if self._is_dry_run(dry_run):
            return {
                "dry_run": True, "action": "add_friend", "number": number,
                "name": None, "remark": remark, "session_name": remark,
                "profile_name": None, "already_friend": False,
            }
        self._ensure_ready()
        logger.info("friend.add.start", number=number, greetings=greetings[:20])
        read: dict[str, Any] = {}
        try:
            read = self._add_friend_reading_profile(number, greetings, remark)
        except _AddFriendNotStarted as exc:
            # 只有在**一个按钮都还没点**的时候才会走到这儿，退回原来那条路，
            # 最坏也就是拿不到昵称——绝不会变成"申请发了两遍"。
            logger.info("friend.add.fallback", number=number, reason=str(exc)[:120])
            self._add_friend_via_vendor(number, greetings, remark)
        except WeChatApiError:
            raise
        except Exception as exc:
            logger.info("friend.add.failed", number=number, error=str(exc)[:100])
            raise WeChatApiError(f"添加好友失败：{exc}") from exc
        name = read.get("name")
        already_friend = bool(read.get("already_friend"))
        if already_friend:
            # 这一轮**没有**点添加、也就没设过备注：微信里他现在叫什么，名片顶上那行才是实情。
            # 拿我们提议的 remark 当会话名会给出一个微信里根本不存在的名字，
            # 逐字寻址随即找不到窗口（[[wechat-uia-window-state]] 同一类坑）。
            session_name = read.get("display_name") or remark
        elif read.get("remark_set"):
            session_name = remark
        else:
            # 备注没填上：会话名就是名片上的昵称；挑不出昵称时退到名片顶上那行，别退回 remark
            # ——那是个微信里不存在的名字。
            session_name = name or read.get("display_name")
        logger.info("friend.add.done", number=number, name=name,
                    session_name=session_name, already_friend=already_friend)
        return {
            "dry_run": False, "action": "add_friend", "number": number,
            "name": name, "remark": remark, "session_name": session_name,
            "profile_name": read.get("profile_name"), "already_friend": already_friend,
        }

    def _add_friend_reading_profile(
        self, number: str, greetings: str, remark: str | None
    ) -> dict[str, Any]:
        """自己走一遍加好友流程，好在中间把名片上的昵称读下来。

        为什么不"先查一次名字再让 vendor 加一次"：那是**两次陌生号网络查找**，
        正是最招风控的动作（节拍红线：查好友 ≥300s 一次）。自己走一遍 = 和原来一样
        只搜一次、只加一次，昵称是顺手读的，不多动微信一下。

        任何"还没点到按钮"的失败都抛 ``_AddFriendNotStarted``，由调用方退回 vendor 实现。
        """
        Navigator = self._pyweixin_class("Navigator")
        Tools = self._pyweixin_class("Tools")
        Uielements = self._pyweixin_submodule("Uielements")
        try:
            add_friend_pane = self._open_add_friend_panel(Navigator, Tools, Uielements)
        except Exception as exc:
            raise _AddFriendNotStarted(f"打不开添加朋友面板：{exc}") from exc

        try:
            try:
                search_edit = add_friend_pane.child_window(control_type="Edit")
                search_edit.set_text("")
                _type_like_human(search_edit, str(number))
                _human_pause("before_enter")
                search_edit.type_keys("{ENTER}")
            except Exception as exc:
                raise _AddFriendNotStarted(f"搜索框用不了：{exc}") from exc
            _human_pause("after_enter")

            profile, add_button = self._await_contact_profile(add_friend_pane, Uielements, number)

            lines = _profile_text_lines(profile)
            name = _pick_profile_name(lines, str(number))
            display_name = _pick_profile_display_name(lines, str(number))
            nickname = _pick_profile_nickname(lines)

            if add_button is None:
                # 名片上没有「添加到通讯录」、但有「发消息」＝ 这个人已经在通讯录里了。
                # 判据是**正向**的（vendor 自己也这么判：`WeChatTools.py` 里
                # 「有发送消息按钮说明不是新朋友不需要添加」）。少了这道正向判据，
                # 任何一次名片没渲染完都会被报成「已经是好友」，于是 `wechatAdded`
                # 被写成真——而那个人根本没加上。
                logger.info("friend.add.already", number=number)
                return {"name": name, "display_name": display_name,
                        "profile_name": nickname, "already_friend": True}

            _human_pause("read_profile")       # 名片出来了，先看一眼——真人不会零延迟点下去
            add_button.click_input()          # ← 从这里开始就动真格了，不再退回 vendor
            verify_window = Tools.move_window_to_center(Window=Uielements.Windows.VerifyFriendWindow)
            _human_pause("verify_open")
            request_edit = verify_window.child_window(control_type="Edit", found_index=0)
            if greetings:
                request_edit.set_text(greetings)
                _human_pause("between_fields")
            remark_set = False
            if remark:
                # 按名字找「修改备注」栏（vendor 表里的 `Edits.ChangeRemarkEdit`），不按序号——
                # 序号 1 那格不是备注栏（见 `add_friend` 文档）。
                remark_edit = verify_window.child_window(**Uielements.Edits.ChangeRemarkEdit)
                if _truthy_safe_call(lambda: remark_edit.exists(timeout=1)):
                    remark_edit.set_text(remark)
                    remark_set = True
                else:
                    logger.info("friend.add.remark_edit_missing", number=number)
            _human_pause("before_confirm")
            verify_window.child_window(**Uielements.Buttons.ConfirmButton).click_input()
            # 陌生人名片上没有备注可言，顶上那行就是昵称——这条路上 `profile_name` 拿得准。
            return {"name": name, "display_name": display_name,
                    "profile_name": nickname or display_name, "already_friend": False,
                    "remark_set": remark_set}
        finally:
            _safe_call(getattr(add_friend_pane, "close", None))

    def _open_add_friend_panel(self, Navigator: Any, Tools: Any, Uielements: Any) -> Any:
        """打开「添加朋友」面板并返回那个窗口。

        ★ **按名字点菜单项，不数第几项**（2026-09-22 线上事故）：vendor 的
          `WeChatTools.open_add_friend_panel` 是点开「快捷操作」后盲敲 ``{UP}``×2 + ``{ENTER}``
          ——它赌的是菜单项的**顺序**。微信 4.1.12.26 上那个菜单只有三项
          「发起群聊 / 添加朋友 / 写笔记」，两下 UP 落在别处，回车下去面板压根没开，
          随后找 `mmui::AddFriendWindow` 顶层窗自然找不到。现场表现就是用户说的
          **「添加框出来了，但没有输入手机号去查询」**——出来的是快捷操作菜单，不是添加面板；
          报上来的「原因」还是 pywinauto 那个 criteria 字典（异常的 str 就长这样），
          客户机上一天失败 6 次没一次能看出卡在哪。
          项的**名字**跟语言走（vendor 的表里已经有：简中「添加朋友」/繁中「新增朋友」/
          英文「Add Contacts」），顺序却跟版本走——按名字点是这里唯一稳的判据。

        点不开时抛原异常，由 `_add_friend_reading_profile` 统一转成 `_AddFriendNotStarted`。
        """
        main_window = Navigator.open_weixin(is_maximize=False)
        window_criteria = dict(Uielements.Windows.AddFriendWindow)
        # 面板窗口的标题和菜单项的文案是同一句（「添加朋友」/「新增朋友」/「Add Contacts」），
        # 所以语言分支只认这一处，不再另抄一张表。
        label = window_criteria.get("title")

        # 「快捷操作」只挂在会话页上，停在通讯录/收藏页时这个按钮根本不存在——
        # vendor 原来也是先点亮会话页再点它的，这一步省不得。
        main_window.child_window(**Uielements.SideBar.Weixin).click_input()
        main_window.child_window(**Uielements.Buttons.QuickActionsButton).click_input()
        _human_pause("menu_open")
        quick_actions = main_window.child_window(**Uielements.Lists.QuickActionsList)
        entry = quick_actions.child_window(title=label, control_type="ListItem")
        if not _truthy_safe_call(lambda: entry.exists(timeout=2)):
            # 菜单开着但没有这一项：别再盲敲键盘碰运气（那正是这次事故的由来），
            # 把看到的都记下来，让下一次「版式又变了」一眼看得出来。
            logger.info("friend.panel.entry_missing", label=label,
                        seen=_pane_inventory(quick_actions))
            raise WeChatApiError(f"快捷操作菜单里没有「{label}」这一项（微信版式可能变了）")
        entry.click_input()
        _human_pause("panel_open")
        return Tools.move_window_to_center(Window=window_criteria)

    def _await_contact_profile(
        self, pane: Any, Uielements: Any, number: str
    ) -> tuple[Any, Any | None]:
        """等名片渲染出来，返回 ``(名片根控件, 添加按钮)``；``添加按钮`` 为 ``None`` = 已经是好友。

        ★ **判「搜到了」用正向证据：名片上出现「添加到通讯录」或「发消息」**，
          不拿那个 Group 的 ``class_name`` 当判据。它是**跟微信版本走的**——vendor 的表里
          写死 ``mmui::ContactProfileView``，只有版本号里带 `4.1.9` 才换成 ``mmui::ProfileView``
          （`third_party/pywechat/pyweixin/Uielements.py`）。表里没覆盖到的版本会让整张名片
          「查无此控件」，现场表现就是**人明明就在屏幕上，我们却报「没搜到这个号」，
          然后 finally 把面板关掉**（2026-08-27 陈卓那次：弹框出来了、人也找到了，一下又关了）。

        ★ **等得起**：搜一个陌生号是一次网络查找，慢的时候名片好几秒才渲染出来。
          原来只等 1+2 秒，等不到就当查无此号——把「慢」误判成「没有」。
          等久一点的代价只是这条指令多花几秒；判错的代价是这条计划项被记成失败。
        """
        add_criteria = getattr(getattr(Uielements, "Buttons", None), "AddToContactsButton", None)
        send_criteria = getattr(getattr(Uielements, "Buttons", None), "SendMessageButton", None)
        deadline = time.monotonic() + _PROFILE_WAIT_SECONDS
        rendered = False
        while True:
            roots = self._contact_profile_roots(pane, Uielements)
            rendered = rendered or len(roots) > 1     # 兜底根永远在，多出来的才是真认出了名片
            for root in roots:
                if add_criteria:
                    button = _safe_call(lambda r=root: r.child_window(**add_criteria))
                    if button is not None and _truthy_safe_call(lambda b=button: b.exists(timeout=0.2)):
                        return root, button
                if send_criteria:
                    button = _safe_call(lambda r=root: r.child_window(**send_criteria))
                    if button is not None and _truthy_safe_call(lambda b=button: b.exists(timeout=0.2)):
                        return root, None
            if time.monotonic() >= deadline:
                break
            time.sleep(0.5)
        # 放弃之前把面板上到底有什么留一行：控件树各版本不一样，下次再遇到，
        # 这一行日志自己说得清是「版式又变了」还是「真的没搜到」。
        logger.info("friend.add.profile_missing", number=number,
                    rendered=rendered, seen=_pane_inventory(pane))
        if rendered:
            # 名片在，但两个按钮一个都没有 —— 「加不了」和「没搜到」是两回事，别混着报。
            raise WeChatApiError(
                f"这个号加不了：{number}（名片上既没有「添加到通讯录」也没有「发消息」）"
            )
        # 原来这种情况是**静默返回成功**的：查无此号也报"加好友完成"，
        # AI 端只会以为对方没通过而一直等。说清楚是查不到。
        raise WeChatApiError(f"没搜到这个号：{number}（号码不对，或对方不允许被搜到）")

    @staticmethod
    def _contact_profile_roots(pane: Any, Uielements: Any) -> list[Any]:
        """名片可能挂在哪几个控件下面，从最确切到最兜底。**最后一个永远是整个面板。**

        兜底为什么安全：名片一定在这个面板里，我们只是不认得包着它的那层 Group 叫什么；
        而挑名字的那几个函数（`_pick_profile_*`）已经把按钮文案、字段行、号码行和
        面板自身的标题都排掉了。
        """
        roots: list[Any] = []
        criteria_list: list[dict] = []
        declared = getattr(getattr(Uielements, "Groups", None), "ContactProfileViewGroup", None)
        if declared:
            criteria_list.append(dict(declared))
        for class_name in _PROFILE_GROUP_CLASSES:
            entry = {"title": "", "control_type": "Group", "class_name": class_name}
            if entry not in criteria_list:
                criteria_list.append(entry)
        for criteria in criteria_list:
            group = _safe_call(lambda c=criteria: pane.child_window(**c))
            if group is not None and _truthy_safe_call(lambda g=group: g.exists(timeout=0.2)):
                roots.append(group)
        roots.append(pane)
        return roots

    def _add_friend_via_vendor(self, number: str, greetings: str, remark: str | None) -> None:
        """原来那条路：vendor 全包，但什么名字都不给。只在退化时用。"""
        FriendSettings = self._pyweixin_class("FriendSettings")
        try:
            FriendSettings.add_new_friend(
                number=number,
                # 空串按 None 传：vendor 的签名就是 `greetings:str=None`，
                # None 才是「你别管这句话」，空串未必被它当成同一回事（092）。
                greetings=greetings or None,
                # vendor 按序号填第 2 个输入框，那格不是备注栏——退化路宁可不设备注。
                remark=None,
                chat_only=False,
                is_maximize=False,
                close_weixin=False,
            )
        except Exception as exc:
            logger.info("friend.add.failed", number=number, error=str(exc)[:100])
            raise WeChatApiError(f"添加好友失败：{exc}") from exc

    def search_contacts(self, query: str, limit: int = 10) -> dict[str, Any]:
        normalized_query = str(query or "").strip()
        if not normalized_query:
            raise ValueError("搜索内容不能为空")
        safe_limit = min(max(int(limit), 1), 50)
        self._ensure_ready()
        logger.info("contact.search.start", query=normalized_query, limit=safe_limit)

        Navigator = self._pyweixin_class("Navigator")
        Uielements = self._pyweixin_submodule("Uielements")
        main_window = Navigator.open_weixin(is_maximize=False)
        chat_button = main_window.child_window(**Uielements.SideBar.Weixin)
        search_edit = None
        try:
            chat_button.click_input()
            search_edits = main_window.descendants(**Uielements.Edits.SearchEdit)
            if not search_edits:
                raise WeChatApiError("未找到微信顶部搜索框")
            search_edit = search_edits[0]
            search_edit.click_input()
            search_edit.set_text(normalized_query)
            time.sleep(0.5)
            search_results = main_window.child_window(**Uielements.Lists.SearchResult)
            result_items: list[Any] = []
            if search_results.exists(timeout=1):
                result_items = search_results.descendants(control_type="ListItem")
            if not result_items:
                Desktop = __import__("pywinauto", fromlist=["Desktop"]).Desktop
                desktop = Desktop(backend="uia")
                for window in desktop.windows():
                    try:
                        result_lists = window.descendants(
                            control_type="List",
                            auto_id="search_list",
                        )
                    except Exception:
                        continue
                    for result_list in result_lists:
                        result_items = result_list.descendants(control_type="ListItem")
                        if result_items:
                            break
                    if result_items:
                        break
            matches = _collect_contact_search_matches(
                result_items,
                normalized_query,
                safe_limit,
            )
        except WeChatApiError:
            raise
        except Exception as exc:
            raise WeChatApiError(f"搜索联系人失败：{exc}") from exc
        finally:
            self._leave_search(chat_button, search_edit)

        exact_count = sum(1 for item in matches if item["exact"])
        logger.info(
            "contact.search.done",
            query=normalized_query,
            match_count=len(matches),
            exact_count=exact_count,
        )
        return {
            "action": "search_contacts",
            "query": normalized_query,
            "matches": matches,
            "match_count": len(matches),
            "exact_count": exact_count,
        }

    @staticmethod
    def _leave_search(chat_button: Any, search_edit: Any) -> None:
        """退出搜索态：清空搜索框 + 回会话列表。**绝不按 ESC。**

        ★ 微信 4.x 里，搜索框空着时 ESC = 关窗口 —— 整个微信直接收进右下角托盘
        （2026-08-17 用户实报：「用搜索去找的话，微信就直接关闭到右下角了」）。
        我们这一步只是想把界面收拾回原样，结果顺手把人家的微信关了：收进托盘之后
        UIA 子树会被微信收走（见 `_ensure_ready` 那段注释），下一条指令、下一次读消息
        都要先把窗口捞回来才做得成，中间那几秒任何操作都可能扑空。

        改成点一下左侧「微信」按钮回到会话列表：搜索面板自然收起，窗口留在原地。
        两步都是「收拾现场」，失败了也不该把这次搜索判成失败——所以各自吞掉异常。
        """
        try:
            if search_edit is not None:
                search_edit.set_text("")
        except Exception:
            pass
        try:
            chat_button.click_input()
        except Exception:
            pass

    def get_contact_profile(self, friend: str) -> dict[str, Any]:
        normalized_friend = str(friend or "").strip()
        if not normalized_friend:
            raise ValueError("联系人名称不能为空")
        self._ensure_ready()
        logger.info("contact.profile.start", friend=normalized_friend)
        Navigator = self._pyweixin_class("Navigator")
        main_window = None
        try:
            chatinfo_pane, main_window = Navigator.open_chatinfo(
                friend=normalized_friend,
                search_pages=5,
                is_maximize=False,
            )
            friend_button = chatinfo_pane.child_window(
                title=normalized_friend,
                control_type="Button",
            )
            friend_button.wait(wait_for="ready", timeout=3)
            friend_button.click_input()
            deadline = time.monotonic() + 3
            profile: dict[str, str] = {}
            while time.monotonic() < deadline and not profile:
                profile = self._read_open_contact_profile()
                if not profile:
                    time.sleep(0.1)
            if not profile:
                raise WeChatApiError("未找到联系人资料窗口")
        except Exception as exc:
            raise WeChatApiError(f"读取联系人资料失败：{exc}") from exc
        finally:
            if main_window is not None:
                try:
                    Uielements = self._pyweixin_submodule("Uielements")
                    main_window.child_window(**Uielements.Buttons.ChatInfoButton).click_input()
                except Exception:
                    pass
        normalized_profile = profile if isinstance(profile, dict) else {"raw": profile}
        logger.info("contact.profile.done", friend=normalized_friend)
        return {
            "action": "get_contact_profile",
            "friend": normalized_friend,
            "profile": normalized_profile,
        }

    def _read_open_contact_profile(self) -> dict[str, str]:
        """Read the top-level WeChat 4.x profile popup left open by pyweixin."""
        try:
            Desktop = __import__("pywinauto", fromlist=["Desktop"]).Desktop
            popup = next(
                (
                    window
                    for window in Desktop(backend="uia").windows()
                    if str(_safe_call(window.class_name) or "") == "mmui::ProfileUniquePop"
                ),
                None,
            )
            if popup is None:
                return {}
            texts = [
                str(_safe_call(control.window_text) or "").strip()
                for control in popup.descendants(control_type="Text")
            ]
            texts = [text for text in texts if text]
            if not texts:
                return {}
            labels = {
                "昵称": "昵称",
                "微信号": "微信号",
                "地区": "地区",
                "备注": "备注",
                "电话": "电话",
                "标签": "标签",
                "描述": "描述",
                "朋友权限": "朋友权限",
                "共同群聊": "共同群聊",
                "个性签名": "个性签名",
                "来源": "来源",
                "添加时间": "添加时间",
            }
            profile: dict[str, str] = {"昵称": texts[0]}
            for index, text in enumerate(texts[:-1]):
                normalized_label = text.rstrip("：:").strip()
                key = labels.get(normalized_label)
                if key:
                    profile[key] = texts[index + 1]
            try:
                popup.close()
            except Exception:
                pass
            return profile
        except Exception:
            return {}

    def list_group_members(self, group: str) -> dict[str, Any]:
        normalized_group = str(group or "").strip()
        if not normalized_group:
            raise ValueError("群聊名称不能为空")
        self._ensure_ready()
        logger.info("group.members.start", group=normalized_group)
        history_window = None
        popover = None
        try:
            Desktop = __import__("pywinauto", fromlist=["Desktop"]).Desktop
            desktop = Desktop(backend="uia")
            for window in desktop.windows():
                class_name = str(_safe_call(window.class_name) or "")
                if class_name in {"mmui::XPopover", "mmui::SearchMsgUniqueChatWindow"}:
                    self._try_close_window(window)
            main_window = self._open_main_dialog_window(friend=normalized_group, select=False)
            Uielements = self._pyweixin_submodule("Uielements")
            history_button = main_window.child_window(**Uielements.Buttons.ChatHistoryButton)
            history_button.click_input()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and history_window is None:
                for window in desktop.windows():
                    if str(_safe_call(window.class_name) or "") != "mmui::SearchMsgUniqueChatWindow":
                        continue
                    if normalized_group in str(_safe_call(window.window_text) or ""):
                        history_window = window
                        break
                if history_window is None:
                    time.sleep(0.1)
            if history_window is None:
                raise WeChatApiError("未找到群聊记录窗口")
            history_window.maximize()
            time.sleep(0.3)
            group_tab = next(
                (
                    tab
                    for tab in history_window.descendants(control_type="TabItem")
                    if str(_safe_call(tab.window_text) or "").strip() == "群成员"
                ),
                None,
            )
            if group_tab is None:
                raise WeChatApiError("聊天记录窗口没有群成员标签")
            group_tab.click_input()
            deadline = time.monotonic() + 3
            member_list = None
            while time.monotonic() < deadline and member_list is None:
                for window in desktop.windows():
                    if str(_safe_call(window.class_name) or "") != "mmui::XPopover":
                        continue
                    lists = [
                        control
                        for control in window.descendants(control_type="List")
                        if str(_element_info_value(control, "automation_id") or "")
                        == "chatroom_member_list"
                    ]
                    if lists:
                        popover = window
                        member_list = lists[0]
                        break
                if member_list is None:
                    time.sleep(0.1)
            if member_list is None:
                raise WeChatApiError("未找到群成员列表")
            title_match = re.search(r"\((\d+)\)\s*$", str(_safe_call(history_window.window_text) or ""))
            total_member_count = int(title_match.group(1)) if title_match else 0
            mouse = __import__("pywinauto", fromlist=["mouse"]).mouse
            rectangle = member_list.rectangle()
            coords = (rectangle.mid_point().x, rectangle.mid_point().y)
            members: list[str] = []
            previous_visible: tuple[str, ...] = ()
            stagnant_pages = 0
            max_pages = min(max(total_member_count + 30, 100), 600)
            for _ in range(max_pages):
                visible = tuple(
                    name
                    for item in member_list.children(control_type="ListItem")
                    if (name := str(_safe_call(item.window_text) or "").strip())
                )
                if visible and visible != previous_visible:
                    overlap = min(len(members), len(visible))
                    while overlap and members[-overlap:] != list(visible[:overlap]):
                        overlap -= 1
                    members.extend(visible[overlap:])
                    previous_visible = visible
                    stagnant_pages = 0
                else:
                    stagnant_pages += 1
                if total_member_count and len(members) >= total_member_count:
                    break
                if stagnant_pages >= 6:
                    break
                mouse.scroll(coords=coords, wheel_dist=-40)
                time.sleep(0.1)
        except Exception as exc:
            raise WeChatApiError(f"读取群成员失败：{exc}") from exc
        finally:
            if popover is not None:
                self._try_close_window(popover)
            if history_window is not None:
                self._try_close_window(history_window)
        logger.info("group.members.done", group=normalized_group, member_count=len(members))
        return {
            "action": "list_group_members",
            "group": normalized_group,
            "members": members,
            "member_count": len(members),
            "total_member_count": total_member_count or len(members),
            "complete": not total_member_count or len(members) >= total_member_count,
        }

    def check_new_friends(
        self,
        verify: bool = False,
        limit: int = 8,
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        effective_dry_run = self._is_dry_run(dry_run)
        if effective_dry_run and verify:
            verify = False
        if effective_dry_run:
            return {
                "dry_run": True,
                "action": "check_new_friends",
                "verify": verify,
                "items": [],
            }
        self._ensure_ready()
        logger.info(
            "friend.check.start",
            verify=verify,
            limit=limit,
        )
        # 「新的朋友」列表里老条目（早就通过的）也在 `items` 里；这一趟**真点了「通过」**的名字
        # 单独收在 `accepted`，上层据此判「通过了谁」，不拿整个列表当通过。
        #
        # ★ 不再交给 vendor 的 `Contacts.check_new_friends`（2026-09-17 真机 4.1.13.12 坐实）：它按
        #   `class_name == 'mmui::XTableCell'` 挑申请条目，而 4.1.13 的条目是 `mmui::ContactsCellNewFriendView`
        #   ——整趟拿到空列表、一个都不点、一个字不记，候选人加了好友就一直挂着。自己走一遍，
        #   判据全用正向证据（条目 class 表 + 按钮标题），和加好友那条路（`_await_contact_profile`）同思路。
        try:
            items, accepted = self._check_new_friends_reading_list(
                verify=verify, limit=max(1, min(int(limit), _NEW_FRIENDS_LIMIT_MAX)),
            )
        except WeChatApiError:
            raise
        except Exception as exc:
            logger.info("friend.check.failed", error=str(exc)[:100])
            raise WeChatApiError(f"好友检测失败：{exc}") from exc
        if not items:
            logger.info("friend.check.empty")
        else:
            names = [str(i)[:30] for i in items[:5]]
            logger.info(
                "friend.check.found",
                count=len(items),
                accepted=len(accepted),
                samples=names,
                auto_accept=verify,
            )
        return {
            "dry_run": False,
            "action": "check_new_friends",
            "verify": verify,
            "items": items,
            "accepted": [str(name) for name in accepted],
        }

    def _check_new_friends_reading_list(
        self, *, verify: bool, limit: int
    ) -> tuple[list[str], list[str]]:
        """通讯录 → 「新的朋友」→ 逐条看右侧详情，有「通过」按钮就点。返回 ``(整个列表的文字, 真通过的名字)``。

        ★ 条目怎么认：`_NEW_FRIEND_CELL_CLASSES`（4.1.13 是 `ContactsCellNewFriendView`，更早是 `XTableCell`）。
          vendor 只认后者，4.1.13 上整趟为空——这就是「候选人加了好友没人通过」的真因（2026-09-17 真机 dump）。
        ★ 「通过」怎么认：右侧详情面板（`FMessageDetailView`）上标题在 `_VERIFY_BUTTON_TITLES` 里的按钮。
          不拿包着它的面板 class 当判据（vendor 那张 `ContactDetailCustom` 表按版本分叉，4.1.13 落到 `found_index`）。
          我方发出去还没被通过的条目，同一个位置是「等待对方验证」——标题对不上就不点，别把它当「通过」。
        ★ 已处理过的条目（列表文字末尾带「已添加 / 已过期 / …」）不点进去：右侧没有按钮，白翻一次。
        ★ 通过者的名字从详情面板顶上的昵称（`TextViewCanCopy`）读，**在点「通过」之前读**；列表那一行是
          「昵称 + 验证语 + 状态」拼在一起的（`周彪我: 我是Kk已添加`），拿它当会话名去读消息会「查无此人」。
        ★ 收尾点回左侧「微信」，不按 ESC（[[wechat-uia-window-state]]：ESC = 关窗）。
        """
        Navigator = self._pyweixin_class("Navigator")
        Tools = self._pyweixin_class("Tools")
        Uielements = self._pyweixin_submodule("Uielements")
        contact_list, main_window = Navigator.open_contacts(is_maximize=False)
        items: list[str] = []
        accepted: list[str] = []
        try:
            _safe_call(lambda: Tools.collapse_contacts(main_window, contact_list))
            section = main_window.child_window(**Uielements.ListItems.NewFriendListItem)
            if not _truthy_safe_call(lambda: section.exists(timeout=1)):
                logger.info("friend.check.no_section", seen=_pane_inventory(contact_list))
                return items, accepted
            entries = self._expand_new_friend_entries(section, contact_list)
            items = [text for _cell, text in entries]
            if not verify:
                return items, accepted
            detail = self._new_friend_detail_root(main_window)
            for cell, text in entries:
                if len(accepted) >= limit:
                    break
                if text.endswith(_NEW_FRIEND_SETTLED_SUFFIXES):
                    continue
                cell.click_input()
                time.sleep(1.0)
                name = self._new_friend_display_name(detail) or _strip_new_friend_status(text)
                button = self._find_verify_button(detail)
                if button is None:
                    # 留证：这一条右侧到底有哪些按钮。下次再遇到「有申请却没通过」，日志自己说得清
                    # 是按钮换了标题、还是这条本来就不是别人加我们（比如「等待对方验证」）。
                    logger.info("friend.check.verify_button_missing", entry=text[:20],
                                buttons=_button_titles(detail))
                    continue
                button_title = str(_safe_call(getattr(button, "window_text", None)) or "").strip()[:20]
                windows_before = _window_handles(self._desktop_windows())
                button.click_input()
                try:
                    via = self._confirm_friend_verification(
                        main_window, detail, Uielements, button=button, windows_before=windows_before,
                    )
                except Exception as exc:
                    # 「通过」已经点下去、确认框却没等到：这一下是不是生效了说不准，别再往下点别人。
                    # 留证：点的是哪颗按钮、桌面上此刻有哪些顶层窗口、详情面板上还剩哪些按钮——
                    # 4.1.12.26 现场（2026-09-17）就是这一步停手，而日志里只有 vendor 那串判据，看不出窗口去哪了。
                    # ★ 证据要进异常文本本身：用户机上我们看不到本地日志，能到 AI 端 / 用户眼前的只有这句报错。
                    windows = _window_inventory(self._desktop_windows())
                    wechat_pid = _window_pid(main_window)
                    hidden = _window_inventory(
                        [w for w in self._desktop_windows(visible_only=False)
                         if str(_safe_call(getattr(w, "class_name", None)) or "").startswith("mmui::")
                         or (wechat_pid is not None and _window_pid(w) == wechat_pid)], limit=8,
                    )
                    buttons = _button_titles(detail)
                    enum_error = _take_enum_error()   # pywinauto 带缓存枚举吞掉的真实 COMError（若有）
                    logger.info(
                        "friend.check.confirm_missing", entry=text[:20], button=button_title,
                        error=str(exc)[:160], windows=windows, hidden=hidden, buttons=buttons,
                        enum_error=enum_error or None,
                    )
                    raise WeChatApiError(
                        f"点了「{button_title or '通过'}」但没等到确认框：{exc}"
                        f"｜此刻顶层窗口：{windows}｜含不可见的微信窗口：{hidden}｜详情面板按钮：{buttons}{enum_error}"
                    ) from exc
                time.sleep(1.0)
                accepted.append(name)
                logger.info("friend.check.accepted", name=name[:20], via=via)
        finally:
            _safe_call(lambda: contact_list.type_keys("{HOME}"))
            _safe_call(lambda: Tools.collapse_contacts(main_window, contact_list))
            _safe_call(lambda: main_window.child_window(**Uielements.SideBar.Weixin).click_input())
        return items, accepted

    @staticmethod
    def _expand_new_friend_entries(section: Any, contact_list: Any) -> list[tuple[Any, str]]:
        """点开「新的朋友」分区，把展开的申请条目 ``(控件, 文字)`` 收回来。

        分区是开关式的：`collapse_contacts` 把它收起后点一下是展开；万一它本来就是展开的，
        这一下反而收起——读不到条目就再点一次。
        """
        section.click_input()
        time.sleep(1.0)
        entries = _new_friend_entries(contact_list)
        if not entries:
            section.click_input()
            time.sleep(1.0)
            entries = _new_friend_entries(contact_list)
        return entries

    @staticmethod
    def _new_friend_detail_root(main_window: Any) -> Any:
        """右侧申请详情面板；认不出就拿整个主窗口兜底（按钮标题判据在整窗口里也不会撞车）。"""
        detail = _safe_call(lambda: main_window.child_window(**_NEW_FRIEND_DETAIL_ROOT))
        if detail is not None and _truthy_safe_call(lambda: detail.exists(timeout=0.5)):
            return detail
        return main_window

    @staticmethod
    def _find_verify_button(detail: Any) -> Any | None:
        for title in _VERIFY_BUTTON_TITLES:
            button = _find_child(detail, {"title": title, "control_type": "Button"})
            if button is not None:
                return button
        return None

    def _confirm_friend_verification(
        self, main_window: Any, detail: Any, Uielements: Any, *, button: Any = None, timeout: float = 8.0,
        windows_before: set[Any] | None = None,
    ) -> str:
        """点了「通过」之后把「通过朋友验证」那一步走完，返回是怎么确认的（中文，直接进日志）。

        vendor 只认一种形态：顶层窗口、标题「通过朋友验证」、class `mmui::VerifyFriendWindow`，5 秒内没出现就
        `ElementNotFoundError`——2026-09-17 微信 4.1.12.26 现场正是这样停手的（[[wechat-auto-accept-friend-110]]）。
        4.1.13 把朋友圈都停靠进主窗口了，这个确认框长什么样还没在真机上见过，所以这里按正向证据找三种形态：
        ① 顶层窗口：class 里带 `VerifyFriend` **或** 标题是「通过朋友验证」一类——两条判据任一命中，不要求同时；
        ② 停靠在主窗口里：主窗口里出现「通过朋友验证」这行字或 `VerifyFriend` 类控件；
        ③ 根本没有确认这一步：详情面板上「通过」按钮没了、换成了「发消息」，说明一点就通过了。
        找到 ①/② 就点里面的「确定」（各语言写法都认）。三种都没等到才抛，让上层留证停手。
        ★ 等到一半（``timeout`` 的一半）什么都没出现、「通过」按钮还原样在面板上，就**再点一次**（只补一次）：
        同一套判据 4.1.13.12 真机点得通，4.1.12.26 现场却没等到，「第一下没点上」是最便宜也最常见的解释
        （[[wxauto-win-realmachine]]：焦点不在微信上时 click_input 会落到别的窗口）。
        """
        started = time.monotonic()
        deadline = started + timeout
        reclick_at = started + timeout / 2
        reclicked = False
        seen_host: tuple[str, str] | None = None   # (形态, 里面的按钮标题) —— 找到过确认框却认不出按钮时的证据
        while True:
            host, via = self._find_verify_confirm_host(main_window, windows_before)
            if host is not None:
                _safe_call(getattr(host, "set_focus", None))
                time.sleep(0.5)
                confirm = self._find_confirm_button(host, Uielements, standalone=host is not main_window)
                if confirm is not None:
                    confirm.click_input()
                    return via
                seen_host = (via, _button_titles(host))
            elif self._find_verify_button(detail) is None and _has_button(detail, _ACCEPTED_BUTTON_TITLES):
                return "无需确认（一点即通过）"
            now = time.monotonic()
            if button is not None and not reclicked and now >= reclick_at and now < deadline and seen_host is None:
                still_there = self._find_verify_button(detail)
                if still_there is not None:
                    logger.info("friend.check.verify_reclicked", waited=round(now - started, 1))
                    # 第二下走 UIA 的 Invoke（不靠坐标、不靠焦点）；没有这个模式再退回坐标点击。
                    if _safe_call(getattr(still_there, "invoke", None)) is None:
                        still_there.click_input()
                    reclicked = True
                    continue
            if now >= deadline:
                if seen_host is not None:
                    raise WeChatApiError(
                        f"{timeout:.0f} 秒内见到了确认框（{seen_host[0]}）但认不出里面的确认按钮，它上面的按钮是：{seen_host[1]}"
                    )
                raise WeChatApiError(f"{timeout:.0f} 秒内没见到「通过朋友验证」确认框（顶层窗口、主窗口内都没有），详情面板也没变成已通过")
            time.sleep(0.5)

    def _find_verify_confirm_host(
        self, main_window: Any, windows_before: set[Any] | None = None,
    ) -> tuple[Any | None, str]:
        """「通过朋友验证」确认框现在在哪：``(宿主控件, 形态说明)``，没有就 ``(None, "")``。

        判据从具体到泛：① 顶层窗口 class 带 VerifyFriend / FriendVerify 或标题命中；② 点击之后**新冒出来的微信自家窗口**
        （同属微信进程、或 class `mmui::` 打头，且不在点击前的句柄集合里）——名字全没猜中也认得出「它刚弹出来」，
        是否点它由里面有没有确认按钮决定（Qt 构建 4.1.12.26 的确认框 class 不是 `mmui::`、标题还可能空，只能靠进程认）；
        ③ 停靠在主窗口里：标题文字（不限控件类型）或 Verify 类控件。
        """
        windows = self._desktop_windows()
        for window in windows:
            if _looks_like_verify_confirm_window(window):
                return window, "独立确认框"
        if windows_before is not None:
            wechat_pid = _window_pid(main_window)
            for window in windows:
                handle = _window_handle(window)
                if handle is None or handle in windows_before:
                    continue
                class_name = str(_safe_call(getattr(window, "class_name", None)) or "")
                if class_name in _NOT_A_CONFIRM_WINDOW:
                    continue
                is_mm = class_name.startswith("mmui::")
                same_proc = wechat_pid is not None and _window_pid(window) == wechat_pid
                if not (is_mm or same_proc):
                    continue
                return window, f"点击后新出现的窗口（{class_name or '无 class'}）"
        for title in _VERIFY_CONFIRM_TITLES:
            node = _safe_call(lambda t=title: main_window.child_window(title=t))
            if node is not None and _truthy_safe_call(lambda n=node: n.exists(timeout=0.2)):
                return main_window, "主窗口内确认框"
        node = _safe_call(lambda: main_window.child_window(class_name_re=".*(VerifyFriend|FriendVerify).*"))
        if node is not None and _truthy_safe_call(lambda n=node: n.exists(timeout=0.2)):
            return node, "主窗口内确认框"
        # 用户机 4.1.12.26（2026-09-18 截图）：确认框明明在屏幕上，「可见的顶层窗口」里却没有它——
        # 再按「不限可见性」枚举一遍，标题 / class 对得上就当它是。
        for window in self._desktop_windows(visible_only=False):
            if _looks_like_verify_confirm_window(window):
                return window, "独立确认框（UIA 标记为不可见）"
        return None, ""

    @staticmethod
    def _find_confirm_button(host: Any, Uielements: Any, *, standalone: bool = False) -> Any | None:
        """确认框里那颗「确定」。三层：① 标题 + Button；② 标题不限控件类型（文字可能挂在按钮的子 Text 上，点它的坐标
        同样落在按钮上）；③ 独立确认框里一颗标题都对不上时，点最后一颗**不是「取消」一类**的按钮，并把点了谁写进日志。
        ★ 宿主可能是 `Desktop().windows()` 给的 UIAWrapper——它没有 child_window，只能 descendants；找法统一走
        `_find_child`（2026-09-18 真机：0.1.63–0.1.66 对它 child_window 一直报 AttributeError 被吞掉，「确定」一次都没
        找到，兜底点了最后一颗按钮=「取消」，框关了、申请原样留着，还记成了 accepted）。
        """
        criteria: list[dict[str, Any]] = [dict(Uielements.Buttons.ConfirmButton)]
        criteria.extend({"title": title, "control_type": "Button"} for title in _CONFIRM_BUTTON_TITLES)
        criteria.extend({"title": title} for title in _CONFIRM_BUTTON_TITLES)
        for crit in criteria:
            button = _find_child(host, crit)
            if button is not None:
                return button
        if standalone:
            buttons = _safe_call(lambda: host.descendants(control_type="Button")) or []
            candidates = [
                b for b in buttons
                if (title := str(_safe_call(getattr(b, "window_text", None)) or "").strip()) and title not in _CANCEL_BUTTON_TITLES
            ]
            if candidates:
                logger.info("friend.check.confirm_fallback_button", buttons=_button_titles(host),
                            picked=str(_safe_call(getattr(candidates[-1], "window_text", None)) or "")[:20])
                return candidates[-1]
        return None

    @staticmethod
    def _desktop_windows(*, visible_only: bool = True) -> list[Any]:
        """桌面上此刻的顶层窗口；pywinauto 不在（Linux 跑测试）或枚举失败就空列表，测试里直接替换这个方法。

        ★ 2026-09-20 用户机现场坐实：**冻结（PyInstaller 打包）客户端**里 `Desktop(backend="uia").windows()`
          **一个顶层窗口都不返回**，连微信主窗口都枚举不到；同一台机、同一个桌面会话，标准 Python 里同一句
          却返回 10 个窗口。STA/MTA、带不带缓存都试过，都不是主因——根因是 comtypes 打进 exe 后跑 UIAutomation
          **根枚举**的行为问题；而 `child_window` 这类**元素级** UIA 在客户端里照常能用（点完「前往验证」能读到
          详情面板按钮）。所以拿不到窗口时，改走 Win32 `EnumWindows`（纯 ctypes、不经 comtypes，冻结与否一致）拿
          顶层句柄，再用元素级 UIA 逐个包装，得到同形状的窗口列表（[[wechat-auto-accept-friend-110]]）。
        """
        windows = _pywinauto_top_windows(visible_only=visible_only)
        if windows:
            return windows
        alt, err = _win32_top_windows(visible_only=visible_only)
        if err:
            _record_enum_error(err)
        return alt

    @staticmethod
    def _new_friend_display_name(detail: Any) -> str:
        text = _safe_call(lambda: detail.child_window(**_NEW_FRIEND_NAME_TEXT))
        if text is None or not _truthy_safe_call(lambda: text.exists(timeout=0.3)):
            return ""
        return str(_safe_call(getattr(text, "window_text", None)) or "").strip()

    def send_message(
        self,
        friend: str,
        text: str,
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        if self._is_dry_run(dry_run):
            return {"dry_run": True, "action": "send_message", "friend": friend}
        self._ensure_ready()
        self._send_text_once(friend, text)
        # ★ 发后核对（075，需求文档 §12.4）：文字没发走会**留在输入框里**，
        # 而底座照样返回成功——067 的文件路径上真机踩过一模一样的坑
        # （文件躺在输入框里、回执却报 done）。宁可报失败，也不谎报「已发送」：
        # AI 会照着回执告诉用户「已经发过去了」。
        #
        # 判据只看**文字**，不看子元素：空输入框天生就带子元素（067 的教训），
        # 但它的 window_text() 一定是空的——所以这里不需要跟基线较劲。
        #
        # ★ 补按回车之前先认一认框里是不是**我们这条**（2026-09-29 真机事故）：AI 回的是
        # 「早上好，今天有什么需要随时跟我说。」，对方收到的却是一个「s」。底座是
        # 「剪贴板 → Ctrl+V → Alt+S」：粘贴落空、Alt 又被吞掉时，框里只剩一个 s；
        # 旧逻辑见框里有字就补回车，把这个 s 发给了候选人。框里的东西和要发的不一致
        # = 粘贴或发送键没生效、**一个字都还没发出去**（发出去了框就是空的），所以清空后
        # 重来一次不会多发；重来还不对就清空报失败，绝不把不是我们这条的东西按出去。
        leftover = self._chat_input_text(friend)
        if leftover and not _same_chat_text(leftover, text):
            logger.warning("message.send.input_mismatch", friend=friend,
                           left=len(leftover), expected=len(text))
            self._clear_chat_input(friend)
            self._send_text_once(friend, text)
            leftover = self._chat_input_text(friend)
            if leftover and not _same_chat_text(leftover, text):
                logger.warning("message.send.input_mismatch_again", friend=friend, left=len(leftover))
                self._clear_chat_input(friend)
                raise WeChatApiError("输入框里的内容和要发的不一致（粘贴或发送键没生效），已清空，对方没有收到")
        verified = leftover is not None
        if leftover:
            self._nudge_send(friend)
            leftover = self._chat_input_text(friend)
            if leftover:
                logger.warning("message.send.stuck_in_input", friend=friend, left=len(leftover))
                raise WeChatApiError("消息打进输入框后没能发出去，已停在输入框里，对方没有收到")
        return {
            "dry_run": False,
            "action": "send_message",
            "friend": friend,
            # 探测不到输入框时据实说「没核对过」，别把「不知道」讲成「已确认发出」。
            "verified": verified,
        }

    def send_files(
        self,
        friend: str,
        files: list[str],
        text: str = "",
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        normalized_friend = str(friend or "").strip()
        normalized_files = [str(Path(path).resolve()) for path in files if str(path).strip()]
        if not normalized_friend:
            raise ValueError("接收人不能为空")
        if not normalized_files:
            raise ValueError("至少需要一个待发送文件")
        result = {
            "dry_run": self._is_dry_run(dry_run),
            "action": "send_files",
            "friend": normalized_friend,
            "file_count": len(normalized_files),
            "filenames": [Path(path).name for path in normalized_files],
        }
        if result["dry_run"]:
            return result
        self._ensure_ready()
        Files = self._pyweixin_class("Files")
        # 发文件 = 复制到剪贴板 → Ctrl+V 粘进输入框 → Alt+S 发送（见 pyweixin 的 send_files_to_friend）。
        # 底座默认只等 0.2s 就按 Alt+S：**两张图还没在输入框里渲染完，这一下就被吞掉**，
        # 结果是文件躺在输入框里、回执却报成功（2026-08-13 真机实况）。按份数放宽等待。
        send_delay = min(6.0, max(1.5, 1.0 + 0.8 * len(normalized_files)))
        # 先量一次「空输入框」是什么样：不同版本的输入框天生就带子元素，
        # 拿绝对数当「有残留」会误报。基线一减，判据就只剩「这次粘进去的东西还在不在」。
        baseline = self._chat_input_leftover(normalized_friend)
        logger.info(
            "files.send.start",
            friend=normalized_friend,
            file_count=len(normalized_files),
            send_delay=send_delay,
            baseline=baseline,
        )
        try:
            self._with_edit_retry(
                normalized_friend, "send_files", lambda: Files.send_files_to_friend(
                    friend=normalized_friend,
                    files=normalized_files,
                    with_messages=bool(text),
                    messages=[text] if text else [],
                    messages_first=True,
                    clear=True,
                    send_delay=send_delay,
                    is_maximize=False,
                    close_weixin=False,
                ),
            )
        except Exception as exc:
            raise WeChatApiError(f"发送文件失败：{exc}") from exc
        leftover = self._flush_chat_input(normalized_friend, baseline)
        if leftover:
            # 宁可报失败也不报「已发送」：躺在输入框里 = 对方根本没收到，
            # 谎报成功比失败更糟（AI 会照着回执告诉用户「已经发过去了」）。
            raise WeChatApiError("文件粘进输入框后没能发出去（Alt+S 未生效），已停在输入框里，没有发送")
        logger.info("files.send.done", friend=normalized_friend, file_count=len(normalized_files))
        result["text_sent"] = bool(text)
        return result

    def _with_edit_retry(self, friend: str, op: str, call: Any) -> Any:
        """跑一次 pyweixin 的发送调用；撞上「输入框还没 enabled」就等它一下、重来一次。

        **现象**：刚 `open_dialog_window` 切完聊天，微信的输入框已经进了 UIA 树、但还没
        变成可操作状态，于是 vendor 里那句 `edit_area.set_text('')` 抛
        ``pywinauto.base_wrapper.ElementNotEnabled``。它上面那道闸
        `edit_area.exists(timeout=0.1)` 只问「在不在」、不问「能不能用」，0.1s 也太短。
        表现是整条任务记 failed（日志里一整条堆栈），下一轮从待处理队列重投才成功——
        白白多等一个周期，候选人那边就是回得慢（2026-08-30 用户实报，第二次才成）。

        **为什么重来一次不会让对方多收到消息**（这是能不能重试的全部关键）：
        pyweixin 那两个函数的顺序是
        ``open_dialog_window`` → ``edit_area.exists()`` → ``if clear: edit_area.set_text('')``
        → 之后全靠 ``pyautogui.hotkey('ctrl+v' / 'alt+s')`` 发出去。走 pywinauto
        ``verify_actionable`` 的**只有** ``set_text('')`` 和单条消息的 ``click_input()``，
        也就是说 **ElementNotEnabled 只可能在「一个字都还没粘进输入框」之前抛出来**。
        而我们两处调用都只传一条消息（``messages=[text]``），不存在「前一条已发出、
        重试又发一遍」那种半截情况。

        ★ 哪天改成一次传多条消息，上面这个论证就不成立了——先把它重新想清楚再动，
          别顺手加进来。重复骚扰候选人比慢一轮严重得多。

        **第二种也重来：``NotFriendError``（「非正常好友,无法发送消息」）**（2026-09-17 用户实报）。
        它和上面那种是同一道闸的两副面孔：`edit_area.exists(timeout=0.1)` 说「不在」就抛这句，
        `set_text('')` 说「不能用」就抛那句——都在**一个字都还没粘进去之前**，所以重来同样不会多发。
        真机上好友是真的：通过后第一条发成了、半小时后的跟进也发成了，只有刚读完他的回复
        立刻回的那一条撞上它。多半是那一刻主窗口里没有这个会话的输入框——最常见的一种是
        这个会话被弹成了独立小窗口（读聊天记录退回弹窗那条路就会弹，读完关不掉就留着），
        微信 4.x 里被弹出去的会话在主窗口里没有输入框，而发消息只认主窗口。
        所以重来之前先把这个人的独立窗口关掉（有就关、没有就过），再等输入框回来。
        对方真不是好友时（删了 / 拉黑 / 还没通过）重来那次照样抛，只多等最长 3 秒。
        """
        try:
            return call()
        except Exception as exc:
            # 只挑这两种重来；别的错原样上抛——见上面「为什么重来一次不会多发」那段
            if _is_not_friend_error(exc):
                logger.warning("wechat.edit_missing", friend=friend, op=op)
                self._close_separate_dialog_window(friend)
            elif _is_element_not_enabled(exc):
                logger.warning("wechat.edit_not_enabled", friend=friend, op=op)
            else:
                raise
        ready = self._wait_chat_input_enabled(friend)
        logger.info("wechat.edit_retry", friend=friend, op=op, 等到可用=ready)
        return call()

    def _close_separate_dialog_window(self, friend: str) -> bool:
        """这个人的会话要是被弹成了独立窗口，关掉它，让会话回到主窗口里。返回关没关。

        关不掉也不抛：这只是重来前的收拾现场，真正的结论由重来那一次给。
        """
        try:
            window = self._find_separate_dialog_window(friend)
        except Exception:  # noqa: BLE001 —— 找窗口本身失败就当没有
            window = None
        if window is None:
            return False
        logger.info("wechat.separate_window_closed_before_retry", friend=friend)
        self._try_close_window(window)
        time.sleep(0.5)   # 关窗之后主窗口要重排一下右侧才有输入框
        return True

    def _wait_chat_input_enabled(self, friend: str) -> bool:
        """等输入框变成可操作。返回「等到了没有」——等不到也照样重来一次。

        等不到就不重试是错的：``is_enabled()`` 自己也可能查不到（控件树正好在重建），
        那时候直接放弃等于把一次本来会成功的发送判死。宁可再试一次让它自己抛。
        """
        deadline = time.monotonic() + EDIT_ENABLE_WAIT_SECONDS
        while time.monotonic() < deadline:
            with suppress(Exception):   # 探测本身失败就当没等到，接着轮询
                edit = self._chat_input_area(friend)
                if edit is not None and edit.is_enabled():
                    return True
            time.sleep(EDIT_ENABLE_POLL_SECONDS)
        return False

    def _chat_input_area(self, friend: str) -> Any | None:
        """拿到当前会话的输入框控件。拿不到返回 None（判不了，不当作失败）。"""
        Navigator = self._pyweixin_class("Navigator")
        Uielements = self._pyweixin_submodule("Uielements")
        main_window = Navigator.open_dialog_window(friend=friend, is_maximize=False)
        if isinstance(main_window, tuple):     # 不同版本返回 (window, ...) 的情况
            main_window = main_window[0]
        edit_area = main_window.child_window(**Uielements.Edits.CurrentChatEdit)
        return edit_area if edit_area.exists(timeout=1) else None

    def _chat_input_leftover(self, friend: str) -> int:
        """输入框里现在有多少东西（文本字数 + 子元素个数）。-1 = 看不出来。"""
        try:
            edit_area = self._chat_input_area(friend)
            if edit_area is None:
                return -1
            text_left = len(str(edit_area.window_text() or "").strip())
            children_left = len(edit_area.children())
            logger.info("files.send.input_state", text=text_left, children=children_left)
            return text_left + children_left
        except Exception as exc:  # noqa: BLE001 —— 探测失败只是没法判定，不改变发送结论
            logger.warning("files.send.input_probe_failed", error=str(exc))
            return -1

    def _send_text_once(self, friend: str, text: str) -> None:
        """底座发一条文字：清空输入框 → 粘贴 → Alt+S。发没发走由调用方看输入框核对。"""
        Messages = self._pyweixin_class("Messages")
        self._with_edit_retry(friend, "send_message", lambda: Messages.send_messages_to_friend(
            friend=friend,
            messages=[text],
            search_pages=5,
            clear=True,
            send_delay=0.2,
            is_maximize=False,
            close_weixin=False,
        ))

    def _chat_input_text(self, friend: str) -> str | None:
        """输入框里还剩的**文字**（去掉首尾空白）。``None`` = 看不出来（判不了，不当作失败）。

        只看文字是刻意的：空输入框本来就可能带子元素（067 真机教训），拿子元素个数
        判「发没发出去」要跟空态基线比才准；而文字不用——发走了就是空的。
        """
        try:
            edit_area = self._chat_input_area(friend)
            if edit_area is None:
                return None
            left = str(edit_area.window_text() or "").strip()
            logger.info("message.send.input_state", text=len(left))
            return left
        except Exception as exc:  # noqa: BLE001 —— 探测失败只是没法判定，不改变发送结论
            logger.warning("message.send.input_probe_failed", error=str(exc))
            return None

    def _clear_chat_input(self, friend: str) -> None:
        """把输入框清空。清不掉只记日志：结论由之后那次核对给。"""
        try:
            edit_area = self._chat_input_area(friend)
            if edit_area is not None:
                edit_area.set_text("")
        except Exception as exc:  # noqa: BLE001
            logger.warning("message.send.clear_failed", error=str(exc))

    def _nudge_send(self, friend: str) -> None:
        """补按一次回车把停在输入框里的内容发走。

        ★ 这**不是**重发：发走的是已经打进框里的那一条，不会产生第二条消息
        （需求文档 §12.4「结果未知不能盲目重发」——重复消息发给候选人是硬伤）。
        """
        try:
            edit_area = self._chat_input_area(friend)
            if edit_area is None:
                return
            edit_area.click_input()   # 焦点必须先回到输入框：不然按下的键会落到别处
            time.sleep(0.3)
            __import__("pyautogui").press("enter")
            time.sleep(1.0)
        except Exception as exc:  # noqa: BLE001 —— 补发失败就按「没发出去」处理
            logger.warning("message.send.retry_failed", error=str(exc))

    def _flush_chat_input(self, friend: str, baseline: int) -> bool:
        """确认粘进去的东西已经发走；没发走就补按发送键。返回 True = 仍有残留（= 没发出去）。

        判据是**跟基线比**：输入框空着时本来就可能有子元素，只有比空态多出来的才算残留。
        基线量不到（-1）时不做判定——宁可不报错，也不凭猜把成功说成失败。
        """
        time.sleep(1.2)
        if baseline < 0:
            return False
        for attempt, key in enumerate(("enter", "alt+s")):
            if self._chat_input_leftover(friend) <= baseline:
                return False
            logger.warning("files.send.retry_send", attempt=attempt, key=key)
            try:
                edit_area = self._chat_input_area(friend)
                if edit_area is None:
                    return False
                edit_area.click_input()   # 焦点必须先回到输入框：不然按下的键会落到别处
                time.sleep(0.3)
                pyautogui = __import__("pyautogui")
                if key == "enter":
                    pyautogui.press("enter")
                else:
                    pyautogui.hotkey("alt", "s", _pause=False)
            except Exception as exc:  # noqa: BLE001 —— 补发失败就按「没发出去」处理
                logger.warning("files.send.retry_failed", error=str(exc))
                return True
            time.sleep(1.5)
        return self._chat_input_leftover(friend) > baseline

    def open_chat(
        self,
        friend: str,
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        if self._is_dry_run(dry_run):
            return {"dry_run": True, "action": "open_chat", "friend": friend}
        self._ensure_ready()
        self._open_main_dialog_window(friend=friend, select=False)
        return {"dry_run": False, "action": "open_chat", "friend": friend}

    def post_moments(
        self,
        text: str = "",
        medias: list[str] | None = None,
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        if self._is_dry_run(dry_run):
            return {"dry_run": True, "action": "post_moments"}
        self._ensure_ready()
        _medias = medias or []
        logger.info("moments.post.start", text_length=len(text), media_count=len(_medias))
        try:
            self._post_moments_impl(text, _medias)
        except Exception as exc:
            logger.info("moments.post.failed", error=str(exc)[:200])
            raise
        logger.info("moments.post.done", media_count=len(_medias))
        return {"dry_run": False, "action": "post_moments", "media_count": len(_medias)}

    def read_recent_moments(
        self,
        limit: int = 5,
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        if self._is_dry_run(dry_run):
            return {"dry_run": True, "action": "read_recent_moments", "posts": []}
        self._ensure_ready()
        safe_limit = min(max(int(limit), 1), 20)
        Moments = self._pyweixin_class("Moments")
        try:
            posts = Moments.dump_recent_posts(
                recent="Today",
                number=safe_limit,
                is_maximize=False,
                close_weixin=False,
            )
        except Exception as exc:
            raise WeChatApiError(f"读取朋友圈失败：{exc}") from exc
        return {
            "dry_run": False,
            "action": "read_recent_moments",
            "posts": posts,
            "post_count": len(posts),
        }

    def like_recent_moments(
        self,
        limit: int = 20,
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        """刷一遍自己的朋友圈时间线，给**今天**发的动态点赞，最多 ``limit`` 条。

        刻意**不做**「逐个好友翻他的朋友圈」：那条路是搜人 → 开资料 → 进他的相册 → 翻，
        和加好友前搜号是同一条路、正是风控最盯的动作（2026-08-27 81 秒两次搜索就弹
        安全验证）。刷自己的时间线只开一个窗口、只按方向键翻，动作形状就是真人刷朋友圈。

        已经点过的不再点：点开省略号后弹的是「取消」而不是「赞」，认不到「赞」就跳过——
        再点一下等于**取消赞**，对方手机上会先弹「XX 赞了你」再消失，比不点还糟。
        """
        if self._is_dry_run(dry_run):
            return {"dry_run": True, "action": "like_moments", "liked": 0, "scanned": 0,
                    "already_liked": 0, "stopped": "dry_run"}
        self._ensure_ready()
        safe_limit = min(max(int(limit), 1), 20)
        logger.info("moments.like.start", limit=safe_limit)
        try:
            outcome = self._like_recent_moments_impl(safe_limit)
        except Exception as exc:
            logger.info("moments.like.failed", error=str(exc)[:200])
            raise
        logger.info("moments.like.done", **outcome)
        return {"dry_run": False, "action": "like_moments", **outcome}

    def scan_recent_moments(
        self,
        limit: int = 10,
        like_limit: int = 0,
        until_key: str = "",
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        """本机朋友圈巡看（和读消息轮询同形）：只收上次看过之后**新出**的今天的动态。

        游标是上一趟看到的第一条动态的键（正文去空白；纯图动态用去掉时间戳的整段文本），
        这一趟翻到它就停。时间戳不进键：「5分钟前」下一趟就变成「20分钟前」了。
        顺手给新动态点赞（``like_limit`` 个，0 = 不点），判据同 ``like_recent_moments``。
        """
        if self._is_dry_run(dry_run):
            return {"dry_run": True, "action": "scan_moments", "posts": [], "liked": 0, "already_liked": 0,
                    "scanned": 0, "first_key": "", "stopped": "dry_run"}
        self._ensure_ready()
        safe_limit = min(max(int(limit), 1), 20)
        safe_like = min(max(int(like_limit), 0), 20)
        logger.info("moments.scan.start", limit=safe_limit, like_limit=safe_like, has_cursor=bool(until_key))
        try:
            outcome = self._scan_recent_moments_impl(safe_limit, safe_like, str(until_key or ""))
        except Exception as exc:
            logger.info("moments.scan.failed", error=str(exc)[:200])
            raise
        logger.info(
            "moments.scan.done",
            new=len(outcome["posts"]), liked=outcome["liked"], already_liked=outcome["already_liked"],
            scanned=outcome["scanned"], stopped=outcome["stopped"],
        )
        return {"dry_run": False, "action": "scan_moments", **outcome}

    def _scan_recent_moments_impl(self, limit: int, like_limit: int, until_key: str) -> dict[str, Any]:
        import pyautogui
        from pywinauto import Desktop

        Navigator = self._pyweixin_class("Navigator")
        Tools = self._pyweixin_class("Tools")
        Uielements = self._pyweixin_submodule("Uielements")
        utils = self._pyweixin_submodule("utils")
        Buttons = Uielements.Buttons
        Lists = Uielements.Lists
        Main_window = Uielements.Main_window

        desktop = Desktop(backend="uia")
        moments_window, _post_button = self._open_moments_timeline(
            desktop, Navigator, Tools, Main_window, Buttons,
        )
        _human_pause("moments_open")
        moments_list = moments_window.child_window(**Lists.MomentsList)
        if not moments_list.exists(timeout=5):
            raise WeChatApiError("朋友圈窗口里找不到动态列表")
        like_button = moments_window.child_window(**Buttons.LikeButton)
        try:
            return self._scan_timeline(
                moments_list, like_button,
                timestamp=utils.Regex_Patterns.Sns_Timestamp_pattern,
                images_pattern=utils.Regex_Patterns.Contain_Images_pattern,
                older_marks=(utils.Special_Labels.Yesterday, utils.Special_Labels.DaysAgo),
                click_ellipsis=utils.ColorMatch.click_gray_ellipsis_button,
                limit=limit, like_limit=like_limit, until_key=until_key,
            )
        finally:
            try:
                pyautogui.press("esc")
                time.sleep(0.3)
                moments_window.close()
            except Exception:  # noqa: BLE001, S110 —— 收尾关窗，关不上不影响结果
                pass

    def _scan_timeline(
        self,
        moments_list: Any,
        like_button: Any,
        *,
        timestamp: Any,
        images_pattern: Any,
        older_marks: tuple[str, ...],
        click_ellipsis: Callable[[Any], Any],
        limit: int,
        like_limit: int,
        until_key: str,
    ) -> dict[str, Any]:
        """翻法同 `_like_timeline`；多了「翻到上次看过的那条就停」和「把新动态收回来」。纯 UIA 逻辑，可用假控件测。"""
        posts: list[dict[str, Any]] = []
        liked = already = scanned = 0
        first_key = ""
        stopped = "end"
        moments_list.type_keys("{HOME}")
        last_key: tuple[str, str] | None = None
        for _ in range(self.MOMENTS_MAX_STEPS):
            moments_list.type_keys("{DOWN}", pause=0.1)
            _human_pause("moments_scroll")
            focused = [item for item in moments_list.children(control_type="ListItem")
                       if item.has_keyboard_focus()]
            if not focused:
                continue
            item = focused[0]
            key = (str(item.class_name()), str(item.window_text()))
            if key == last_key:
                break
            last_key = key
            if key[0] in self.MOMENTS_NON_POST_CLASSES:
                continue
            found = timestamp.search(key[1])
            if found is None:
                continue
            when = found.group(0)
            if any(mark and mark in when for mark in older_marks):
                stopped = "older"
                break
            post = _parse_post_text(key[1], when, timestamp, images_pattern)
            if not first_key:
                first_key = post["key"]
            if until_key and post["key"] == until_key:
                stopped = "cursor"
                break
            scanned += 1
            posts.append(post)
            if like_limit and liked < like_limit:
                click_ellipsis(item.rectangle())
                _human_pause("before_like")
                if like_button.exists(timeout=1.5):
                    like_button.click_input()
                    liked += 1
                    _human_pause("after_like")
                else:
                    already += 1
            if len(posts) >= limit:
                stopped = "limit"
                break
        return {"posts": posts, "liked": liked, "already_liked": already, "scanned": scanned,
                "first_key": first_key, "stopped": stopped}

    #: 一条评论指令最多评几条。和网关那档「一天 5 条」默认同数——评论是**公开文字**，
    #: 挂在别人的动态下面，所有共同好友都看得见，量必须比点赞小一个量级。
    MOMENTS_COMMENT_MAX = 5

    def comment_recent_moments(
        self,
        comments: list[dict[str, Any]],
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        """给时间线里**今天**的、指定的那几条动态各写一条评论。

        每条 ``{"content": 动态正文, "text": 评论}``。**按正文认动态**：正文去掉空白后是
        列表项文本的子串就算认到（正文本来就是 ``read_recent_moments`` 从同一段文本里
        抠出来的）。认不到的一条都不评——评错一条动态比漏评十条难看得多，而且撤不回来。
        同一趟里同一段正文只评一次（两个好友转了同一条，也只评先看到的那条）。
        """
        wanted = _normalize_comment_requests(comments, self.MOMENTS_COMMENT_MAX)
        if self._is_dry_run(dry_run):
            return {
                "dry_run": True, "action": "comment_moments",
                "commented": 0, "scanned": 0, "matched": 0,
                "commented_contents": [], "missed": [c["content"][:30] for c in wanted],
                "stopped": "dry_run",
            }
        self._ensure_ready()
        logger.info("moments.comment.start", count=len(wanted))
        try:
            outcome = self._comment_recent_moments_impl(wanted)
        except Exception as exc:
            logger.info("moments.comment.failed", error=str(exc)[:200])
            raise
        logger.info(
            "moments.comment.done",
            commented=outcome["commented"], scanned=outcome["scanned"],
            matched=outcome["matched"], missed=len(outcome["missed"]), stopped=outcome["stopped"],
        )
        return {"dry_run": False, "action": "comment_moments", **outcome}

    #: 时间线列表里**不是一条动态**的项：评论区、「余下 x 条」、广告。跟 vendor `like_posts` 一致。
    MOMENTS_NON_POST_CLASSES = (
        "mmui::TimelineCommentCell", "mmui::TimelineCell", "mmui::TimelineAdGridImageCell",
    )

    #: 翻多少下还没到头就收手。20 条上限 × 每条动态在列表里最多占几项，再留余量；
    #: 这是防死循环的硬边，不是节拍——正常一趟远远到不了。
    MOMENTS_MAX_STEPS = 160

    def _like_recent_moments_impl(self, limit: int) -> dict[str, Any]:
        import pyautogui
        from pywinauto import Desktop

        Navigator = self._pyweixin_class("Navigator")
        Tools = self._pyweixin_class("Tools")
        Uielements = self._pyweixin_submodule("Uielements")
        utils = self._pyweixin_submodule("utils")
        Buttons = Uielements.Buttons
        Lists = Uielements.Lists
        Main_window = Uielements.Main_window

        desktop = Desktop(backend="uia")
        moments_window, _post_button = self._open_moments_timeline(
            desktop, Navigator, Tools, Main_window, Buttons,
        )
        _human_pause("moments_open")
        moments_list = moments_window.child_window(**Lists.MomentsList)
        if not moments_list.exists(timeout=5):
            raise WeChatApiError("朋友圈窗口里找不到动态列表")
        like_button = moments_window.child_window(**Buttons.LikeButton)
        try:
            return self._like_timeline(
                moments_list, like_button,
                timestamp=utils.Regex_Patterns.Sns_Timestamp_pattern,
                older_marks=(utils.Special_Labels.Yesterday, utils.Special_Labels.DaysAgo),
                click_ellipsis=utils.ColorMatch.click_gray_ellipsis_button,
                limit=limit,
            )
        finally:
            # 点完关窗口：留着的话下一趟 `_moments_window` 会直接复用，停在哪一页说不准。
            try:
                pyautogui.press("esc")
                time.sleep(0.3)
                moments_window.close()
            except Exception:
                pass

    def _like_timeline(
        self,
        moments_list: Any,
        like_button: Any,
        *,
        timestamp: Any,
        older_marks: tuple[str, ...],
        click_ellipsis: Callable[[Any], Any],
        limit: int,
    ) -> dict[str, Any]:
        """从列表顶端往下翻，逐条判「是不是今天的动态」再决定点不点。纯 UIA 逻辑，可用假控件测。

        - 只认时间戳里**没有**「昨天」「x 天前」的（`older_marks`）；一碰到就停，时间线是按时间排的。
        - 认不出时间戳的项（评论、广告、「余下 x 条」）跳过不动。
        - 省略号点开后**必须看到「赞」**才点；看到的是「取消」= 已经点过，跳过。
        - 翻到底（按方向键焦点不再动）就停；另有 `MOMENTS_MAX_STEPS` 硬边防死循环。
        """
        liked = scanned = already = 0
        stopped = "end"
        moments_list.type_keys("{HOME}")
        last_key: tuple[str, str] | None = None
        for _ in range(self.MOMENTS_MAX_STEPS):
            moments_list.type_keys("{DOWN}", pause=0.1)
            _human_pause("moments_scroll")
            focused = [item for item in moments_list.children(control_type="ListItem")
                       if item.has_keyboard_focus()]
            if not focused:
                continue
            item = focused[0]
            key = (str(item.class_name()), str(item.window_text()))
            if key == last_key:
                break                       # 焦点没动 = 到底了
            last_key = key
            if key[0] in self.MOMENTS_NON_POST_CLASSES:
                continue
            found = timestamp.search(key[1])
            if found is None:
                continue                    # 认不出时间就不碰它
            when = found.group(0)
            if any(mark and mark in when for mark in older_marks):
                stopped = "older"
                break
            scanned += 1
            click_ellipsis(item.rectangle())
            _human_pause("before_like")
            if like_button.exists(timeout=1.5):
                like_button.click_input()
                liked += 1
                _human_pause("after_like")
            else:
                already += 1
            if liked >= limit:
                stopped = "limit"
                break
        return {"liked": liked, "scanned": scanned, "already_liked": already, "stopped": stopped}

    def _comment_recent_moments_impl(self, wanted: list[dict[str, str]]) -> dict[str, Any]:
        import pyautogui
        from pywinauto import Desktop

        Navigator = self._pyweixin_class("Navigator")
        Tools = self._pyweixin_class("Tools")
        SystemSettings = self._pyweixin_class("SystemSettings")
        Uielements = self._pyweixin_submodule("Uielements")
        utils = self._pyweixin_submodule("utils")
        Buttons = Uielements.Buttons
        Lists = Uielements.Lists
        Main_window = Uielements.Main_window

        desktop = Desktop(backend="uia")
        moments_window, _post_button = self._open_moments_timeline(
            desktop, Navigator, Tools, Main_window, Buttons,
        )
        _human_pause("moments_open")
        moments_list = moments_window.child_window(**Lists.MomentsList)
        if not moments_list.exists(timeout=5):
            raise WeChatApiError("朋友圈窗口里找不到动态列表")
        comment_button = moments_window.child_window(**Buttons.CommentButton)

        def type_text(text: str) -> None:
            # vendor 的写法：清空输入框、剪贴板贴进去。中文用 pyautogui 逐字打不进去，
            # 只能贴；贴完按字数停一会儿，像是人打完在看一遍。
            pyautogui.hotkey("ctrl", "a")
            pyautogui.press("backspace")
            SystemSettings.copy_text_to_clipboard(text)
            pyautogui.hotkey("ctrl", "v")
            time.sleep(min(len(text) * random.uniform(0.06, 0.12), 8.0))

        def click_send(item: Any) -> None:
            # 评论输入框出现在这条动态**下面那一项**里，发送键按它的矩形找（vendor 同法）。
            following = Tools.get_next_item(moments_list, item)
            rect = (following or item).rectangle()
            utils.ColorMatch.click_green_send_button(rect, x_offset=70, y_offset=42)

        try:
            return self._comment_timeline(
                moments_list, comment_button,
                timestamp=utils.Regex_Patterns.Sns_Timestamp_pattern,
                older_marks=(utils.Special_Labels.Yesterday, utils.Special_Labels.DaysAgo),
                click_ellipsis=utils.ColorMatch.click_gray_ellipsis_button,
                type_text=type_text,
                click_send=click_send,
                comments=wanted,
            )
        finally:
            try:
                pyautogui.press("esc")
                time.sleep(0.3)
                moments_window.close()
            except Exception:  # noqa: BLE001, S110 —— 收尾关窗，关不上不影响结果
                pass

    def _comment_timeline(
        self,
        moments_list: Any,
        comment_button: Any,
        *,
        timestamp: Any,
        older_marks: tuple[str, ...],
        click_ellipsis: Callable[[Any], Any],
        type_text: Callable[[str], None],
        click_send: Callable[[Any], None],
        comments: list[dict[str, str]],
    ) -> dict[str, Any]:
        """从列表顶端往下翻，逐条看「是不是今天的、是不是要评的那几条之一」。纯 UIA 逻辑，可用假控件测。

        - 翻法、跳过、到底、「昨天」即停，全同 `_like_timeline`。
        - 认动态只认正文：去掉空白后是这一项文本的子串。认到了就点省略号 → 「评论」→ 贴文字 → 发送。
        - 省略号点开后没看到「评论」按钮（这条动态不让评/控件变了）→ 这条记 missed，不硬点。
        - 要评的都评完了就收手，不把时间线翻到底。
        """
        pending = list(comments)
        commented = scanned = matched = 0
        done_contents: list[str] = []
        unclickable: list[str] = []
        stopped = "end"
        moments_list.type_keys("{HOME}")
        last_key: tuple[str, str] | None = None
        for _ in range(self.MOMENTS_MAX_STEPS):
            moments_list.type_keys("{DOWN}", pause=0.1)
            _human_pause("moments_scroll")
            focused = [item for item in moments_list.children(control_type="ListItem")
                       if item.has_keyboard_focus()]
            if not focused:
                continue
            item = focused[0]
            key = (str(item.class_name()), str(item.window_text()))
            if key == last_key:
                break                       # 焦点没动 = 到底了
            last_key = key
            if key[0] in self.MOMENTS_NON_POST_CLASSES:
                continue
            found = timestamp.search(key[1])
            if found is None:
                continue
            when = found.group(0)
            if any(mark and mark in when for mark in older_marks):
                stopped = "older"
                break
            scanned += 1
            haystack = _squash(key[1])
            hit = next((c for c in pending if c["key"] in haystack), None)
            if hit is None:
                continue
            matched += 1
            pending.remove(hit)
            click_ellipsis(item.rectangle())
            _human_pause("before_comment")
            if not comment_button.exists(timeout=1.5):
                unclickable.append(hit["content"][:30])
                continue
            comment_button.click_input()
            _human_pause("before_typing")
            type_text(hit["text"])
            _human_pause("before_send")
            click_send(item)
            commented += 1
            done_contents.append(hit["content"])
            _human_pause("after_comment")
            if not pending:
                stopped = "all"
                break
        missed = unclickable + [c["content"][:30] for c in pending]
        return {
            "commented": commented, "scanned": scanned, "matched": matched,
            "commented_contents": done_contents, "missed": missed, "stopped": stopped,
        }

    def _open_moments_timeline(
        self, desktop: Any, Navigator: Any, Tools: Any, Main_window: Any, Buttons: Any,
    ) -> tuple[Any, Any]:
        """打开朋友圈窗口并停在时间线主页（看得到「发表」按钮）。发朋友圈和点赞共用。

        1. 桌面上已经有朋友圈窗口就直接用；没有就点主窗口工具栏头像 → 资料面板 → 「朋友圈」
        2. 进的不是主页（看不到发表按钮）就点返回
        """
        if self._moments_layout() == "docked":
            raise MomentsDockedLayoutError(self._docked_layout_message())

        from pywinauto import mouse

        moments_window = self._moments_window(desktop, Tools)
        if moments_window is None:
            main_window = Navigator.open_weixin(is_maximize=False)
            toolbar = main_window.child_window(**Main_window.Toolbar)
            profile_pane = self._open_profile_pane(desktop, toolbar, mouse)
            self._click_moments_entry(profile_pane, Buttons)
            moments_window = self._moments_window(desktop, Tools, timeout=12.0)
            if moments_window is None:
                # 版本没读到、或版本表没跟上：窗口没出来但主窗口里长出了停靠面板，就是新版式。
                if self._docked_moments_host(desktop) is not None:
                    raise MomentsDockedLayoutError(self._docked_layout_message())
                raise WeChatApiError("点了「朋友圈」但朋友圈窗口没出来")

        post_button = moments_window.child_window(**Buttons.PostButton)
        if not post_button.exists(timeout=2):
            back_button = moments_window.child_window(**Buttons.BackButton)
            if back_button.exists(timeout=1):
                back_button.click_input()
                time.sleep(1)
                moments_window = self._moments_window(desktop, Tools) or moments_window
            post_button = moments_window.child_window(**Buttons.PostButton)
        if not post_button.exists(timeout=2):
            raise WeChatApiError("无法找到朋友圈发表按钮")
        return moments_window, post_button

    def _moments_layout(self) -> str:
        return moments_layout(self._wechat_version)

    def _docked_layout_message(self) -> str:
        shown = self._wechat_version or "未知版本"
        return (
            f"微信 {shown} 的朋友圈已停靠进主窗口（4.1.13 起的版式），"
            "点赞/评论/巡看还没适配这个版式；要用请先退回微信 4.1.12"
        )

    def _post_moments_impl(self, text: str, medias: list[str]) -> dict[str, Any] | None:
        """用 pyweixin 底层 UIA 操作发朋友圈。**按微信版本挑版式**（`moments_layout`）。

        · 4.1.13 起 → `_post_moments_docked`（停靠面板 + 相机格）
        · 更早 → 下面这条老路：独立朋友圈窗口
          1–3. 打开朋友圈窗口并停在主页（`_open_moments_timeline`）
          4. 右键发表按钮 → 有图片选图片模式，没图片选纯文字
          5. 填入文字 → 点击发表
        · 版本没读到、老路开出来的却是停靠面板 → 就地改走新路（面板已经开着，直接接手）
        """
        if self._moments_layout() == "docked":
            return self._post_moments_docked(text, medias)

        import pyautogui
        from pywinauto import Desktop

        Navigator = self._pyweixin_class("Navigator")
        Tools = self._pyweixin_class("Tools")
        Uielements = self._pyweixin_submodule("Uielements")

        Buttons = Uielements.Buttons
        Edits = Uielements.Edits
        Groups = Uielements.Groups
        Main_window = Uielements.Main_window
        Windows = Uielements.Windows

        desktop = Desktop(backend="uia")
        valid_paths = self._normalize_moments_media_paths(medias) if medias else []

        try:
            moments_window, post_button = self._open_moments_timeline(
                desktop, Navigator, Tools, Main_window, Buttons,
            )
        except MomentsDockedLayoutError:
            logger.info("moments.post.layout_sniffed_docked", version=self._wechat_version or None)
            return self._post_moments_docked(text, medias, desktop=desktop)

        # 4. 右键发表按钮 → 选择模式
        post_button.right_click_input()
        time.sleep(0.4)
        pyautogui.press("up", presses=2, interval=0.05)

        if valid_paths:
            # 图片模式：选菜单第一项，打开文件选择对话框
            pyautogui.press("enter")
            time.sleep(0.8)

            native_window = self._wait_native_file_dialog(desktop, Windows)
            self._choose_native_files(native_window, valid_paths, pyautogui)
            time.sleep(1)
        else:
            # 纯文字模式
            pyautogui.press("down", presses=1, interval=0.05)
            pyautogui.press("enter")
            time.sleep(1.5)

        # 5. 在发布面板中填入文字
        publish_panel = moments_window.child_window(**Groups.SnsPublishGroup)
        if text:
            text_input = publish_panel.child_window(**Edits.SnsEdit)
            text_input.click_input()
            text_input.set_text(text)

        # 6. 点击发表
        final_button = publish_panel.child_window(**Buttons.PostButton)
        final_button.click_input()
        time.sleep(1)

        # 7. 关闭朋友圈窗口
        try:
            pyautogui.press("esc")
            time.sleep(0.3)
            moments_window.close()
        except Exception:
            pass
        return None

    # ---- 4.1.13 起：停靠进主窗口的朋友圈 ----------------------------------------

    #: 停靠式朋友圈的内容视图（挂在主窗口里，也可能被「恢复」按钮弹成独立窗口）。
    MOMENTS_DOCKED_VIEW_CLASS = "mmui::SNSContentView"
    #: 能装下停靠面板的顶层窗口 class：主窗口，或弹出去之后的 Chromium 宿主窗口。
    MOMENTS_DOCKED_HOST_CLASSES = ("mmui::MainWindow", "Chrome_WidgetWin_0")
    #: 相册列表 / 「发表」那一格 / 发表编辑面板。
    MOMENTS_ALBUM_LIST_CLASS = "mmui::AlbumListView"
    MOMENTS_POST_CELL_CLASS = "mmui::AlbumContentCell"
    MOMENTS_PUBLISH_PANEL_ID = "SnsPublishPanel"
    #: 相机格找不着时的退路：真机 4.1.13.12 上格子宽 358、相机中心在左起 128 ≈ 36%。
    MOMENTS_CAMERA_TILE_FALLBACK_RATIO = 0.36
    #: 点完「发表」等面板收起多久；图片要上传，给足。
    MOMENTS_PUBLISH_SETTLE_SECONDS = 20.0

    def _post_moments_docked(
        self, text: str, medias: list[str], *, desktop: Any = None,
    ) -> dict[str, Any]:
        """4.1.13 起的发朋友圈（2026-09-15 真机 4.1.13.12 逐步 dump 出来的路）：

        1. 主窗口里已有停靠面板就直接用；没有就点头像 → 资料面板 → 「朋友圈」，等面板长出来
        2. 相册「今天」行的「发表」格（`AlbumContentCell`）→ 截图认出相机方块 → 点它
        3. 系统「选择文件」框（Win32 `#32770`，UIA 顶层枚举看不见、win32 后端看得见）填路径 → 打开
        4. `SnsPublishPanel` 出来 → 填文字（`mmui::XValidatorTextEdit`）→ 点「发表」
        5. 等面板收起（图片要上传）→ 顺手把停靠面板关掉，桌面还给用户

        ★ 没有纯文字入口：相机格右键没菜单、长按还是选文件、选文件框取消什么都不出。
          所以没图就直接拒，别去点——点了只会给用户桌面上留一个选文件框。
        """
        if not medias:
            raise WeChatApiError(
                f"微信 {self._wechat_version or '4.1.13+'} 的朋友圈没有纯文字发表入口，"
                "请带图片发，或把微信退回 4.1.12"
            )
        import pyautogui
        from pywinauto import Desktop

        valid_paths = self._normalize_moments_media_paths(medias)
        Navigator = self._pyweixin_class("Navigator")
        Uielements = self._pyweixin_submodule("Uielements")
        Buttons = Uielements.Buttons
        Edits = Uielements.Edits
        Main_window = Uielements.Main_window
        Windows = Uielements.Windows
        desktop = desktop if desktop is not None else Desktop(backend="uia")

        host = self._open_docked_moments(desktop, Navigator, Main_window, Buttons)
        cell = self._docked_post_cell(host, Buttons)
        self._click_camera_tile(host, cell, pyautogui)

        native_window = self._wait_native_file_dialog_any(desktop, Windows)
        self._choose_native_files(native_window, valid_paths, pyautogui)

        panel = host.child_window(auto_id=self.MOMENTS_PUBLISH_PANEL_ID)
        if not panel.exists(timeout=15):
            raise WeChatApiError("选完图片但朋友圈发表面板没出来")
        if text:
            text_input = panel.child_window(**Edits.SnsEdit)
            text_input.click_input()
            text_input.set_text(text)
            time.sleep(0.3)
        final_button = panel.child_window(**Buttons.PostButton)
        if not final_button.exists(timeout=3):
            raise WeChatApiError("朋友圈发表面板里找不到「发表」按钮")
        final_button.click_input()
        closed = self._wait_publish_panel_gone(panel)
        logger.info("moments.post.docked.published", panel_closed=closed)
        self._close_docked_moments(host)
        return {"layout": "docked", "publish_panel_closed": closed}

    def _docked_moments_host(self, desktop: Any) -> Any | None:
        """此刻装着停靠式朋友圈的那个顶层窗口（spec），没有就 None。只枚举顶层，再往里看一层判据。"""
        try:
            windows = list(desktop.windows())
        except Exception:  # noqa: BLE001 —— 桌面枚举都失败就当没有
            return None
        # 弹出去的宿主窗口 class 是 Chromium 通用的，别的程序也叫这个名；只认微信自己进程里的。
        wechat_pids: set[Any] = set()
        for window in windows:
            with suppress(Exception):
                if window.element_info.class_name == self.MOMENTS_DOCKED_HOST_CLASSES[0]:
                    wechat_pids.add(window.element_info.process_id)
        for window in windows:
            try:
                info = window.element_info
                if info.class_name not in self.MOMENTS_DOCKED_HOST_CLASSES:
                    continue
                if info.class_name != self.MOMENTS_DOCKED_HOST_CLASSES[0] and info.process_id not in wechat_pids:
                    continue
                spec = desktop.window(handle=info.handle)
                if spec.child_window(class_name=self.MOMENTS_DOCKED_VIEW_CLASS).exists(timeout=0.5):
                    return spec
            except Exception:  # noqa: BLE001, S112 —— 单个窗口读不到就看下一个
                continue
        return None

    def _open_docked_moments(self, desktop: Any, Navigator: Any, Main_window: Any, Buttons: Any) -> Any:
        from pywinauto import mouse

        host = self._docked_moments_host(desktop)
        if host is not None:
            return host
        main_window = Navigator.open_weixin(is_maximize=False)
        toolbar = main_window.child_window(**Main_window.Toolbar)
        profile_pane = self._open_profile_pane(desktop, toolbar, mouse)
        self._click_moments_entry(profile_pane, Buttons)
        deadline = time.monotonic() + 12.0
        while True:
            host = self._docked_moments_host(desktop)
            if host is not None:
                return host
            if time.monotonic() >= deadline:
                raise WeChatApiError("点了「朋友圈」但停靠式朋友圈面板没出来")
            time.sleep(0.5)

    def _docked_post_cell(self, host: Any, Buttons: Any) -> Any:
        """相册里「发表」那一格。名字跟客户端语言走（和工具栏「发表」按钮同一个词）。不在视野里就先回顶。"""
        title = dict(Buttons.PostButton).get("title", "发表")
        cell = host.child_window(
            control_type="ListItem", class_name=self.MOMENTS_POST_CELL_CLASS, title=title,
        )
        if cell.exists(timeout=2):
            return cell
        album = host.child_window(class_name=self.MOMENTS_ALBUM_LIST_CLASS)
        if album.exists(timeout=2):
            with suppress(Exception):
                album.type_keys("{HOME}")
                time.sleep(0.6)
        if cell.exists(timeout=3):
            return cell
        raise WeChatApiError("停靠式朋友圈里找不到「发表」那一格")

    def _click_camera_tile(self, host: Any, cell: Any, pyautogui: Any) -> None:
        """点「发表」格里的相机方块。先把微信提到前台再截图，不然截到的是盖在上面的别的窗口。"""
        with suppress(Exception):
            host.set_focus()
            time.sleep(0.4)
        rect = cell.rectangle()
        width, height = rect.width(), rect.height()
        tile: tuple[int, int] | None = None
        try:
            shot = pyautogui.screenshot(region=(rect.left, rect.top, width, height)).convert("RGB")
            tile = find_gray_tile(shot.load(), *shot.size)
        except Exception as exc:  # noqa: BLE001 —— 截不到就按比例点，别在这儿断
            logger.info("moments.post.docked.screenshot_failed", error=str(exc)[:120])
        if tile is None:
            tile = (int(width * self.MOMENTS_CAMERA_TILE_FALLBACK_RATIO), height // 2)
            logger.info("moments.post.docked.tile_fallback", x=tile[0], y=tile[1])
        cell.click_input(coords=tile)
        time.sleep(1.5)

    #: 系统选文件框的标题（跟系统语言走）。
    NATIVE_FILE_DIALOG_TITLES = ("选择文件", "打开", "Open", "選擇檔案", "開啟")

    def _wait_native_file_dialog_any(self, desktop: Any, Windows: Any, *, timeout: float = 8.0) -> Any:
        """等系统「选择文件」框。**先 win32 后 UIA**（2026-09-15 真机：这个框 UIA 顶层枚举、
        `top_level_only=False` 的判据都找不到，win32 后端一枚举就有）。"""
        from pywinauto import Desktop

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                for window in Desktop(backend="win32").windows():
                    try:
                        if (window.is_visible() and window.class_name() == "#32770"
                                and window.window_text() in self.NATIVE_FILE_DIALOG_TITLES):
                            return window
                    except Exception:  # noqa: BLE001, S112 —— 单个窗口读不到就看下一个
                        continue
            except Exception:  # noqa: BLE001, S110 —— 这一轮枚举失败，下一轮再来
                pass
            time.sleep(0.5)
        return self._wait_native_file_dialog(desktop, Windows)

    def _wait_publish_panel_gone(self, panel: Any, *, timeout: float | None = None) -> bool:
        """点完「发表」等面板收起。收不起**不当失败报**——发表已经点下去了，报失败上层会重发，
        重复一条朋友圈比晚知道更糟；只如实带回 ``publish_panel_closed=False``。"""
        deadline = time.monotonic() + (self.MOMENTS_PUBLISH_SETTLE_SECONDS if timeout is None else timeout)
        while True:
            try:
                if not panel.exists(timeout=0.5):
                    return True
            except Exception:  # noqa: BLE001 —— 面板都查不到了，就是收起了
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.5)

    def _close_docked_moments(self, host: Any) -> None:
        """把停靠面板关掉（面板顶栏的「关闭」，在朋友圈那一列里）。纯善后，失败不影响结果。"""
        try:
            view = host.child_window(class_name=self.MOMENTS_DOCKED_VIEW_CLASS)
            if not view.exists(timeout=1):
                return
            left_edge = view.rectangle().left - 10
            for button in host.descendants(control_type="Button"):
                try:
                    if ((button.window_text() or "").strip() in ("关闭", "Close", "關閉")
                            and button.rectangle().left >= left_edge):
                        button.click_input()
                        return
                except Exception:  # noqa: BLE001, S112 —— 单个按钮读不到就看下一个
                    continue
        except Exception as exc:  # noqa: BLE001
            logger.info("moments.post.docked.close_failed", error=str(exc)[:120])

    #: 朋友圈窗口的 class。
    MOMENTS_WINDOW_CLASS = "mmui::SNSWindow"

    #: 资料面板里「朋友圈」那一项的名字（跟客户端语言走）。
    MOMENTS_LABELS = ("朋友圈", "Moments")

    def _moments_window(self, desktop: Any, Tools: Any, *, timeout: float = 2.0) -> Any | None:
        """找朋友圈窗口并摆到屏幕中间；没有就 None。

        ★ **只认 class，不认标题**（2026-08-29 真机实测）：vendor 的
          `Independent_window.MomentsWindow` 把 `title` 写死，还按版本分叉成
          `朋友圈` / `Moments`——而同一台机器上主窗口报 `'微信'`、资料面板报 `'Weixin'`，
          标题根本不是个稳的判据。线上那条
          `ElementNotFoundError: {'title': '朋友圈', …'mmui::SNSWindow'…}`
          就是标题这一项没对上。class 在 vendor 表的每个版本分支里都是同一个值。
        """
        deadline = time.monotonic() + timeout
        while True:
            try:
                for window in desktop.windows():
                    try:
                        if window.element_info.class_name == self.MOMENTS_WINDOW_CLASS:
                            return Tools.move_window_to_center(
                                Window_handle=window.element_info.handle)
                    except Exception:
                        continue
            except Exception:
                pass
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.3)

    def _click_moments_entry(self, pane: Any, Buttons: Any) -> None:
        """点资料面板里的「朋友圈」。

        ★ **点「朋友圈」标签右边那块，不点标签本身**（2026-08-29 真机 dump 4.1.12）。
          那一行 `mmui::ProfileDetailLineSnsView`（L626 T304 R858 B369）长这样：

              XVBoxView > XTextView「朋友圈」   L626–R683   ← 只是个标签，点了**没反应**
              QWidget                          L683–R691
              ProfileDetailValueSnsView(Button) L691–R858   ← 缩略图那块，点这里
                XView > AlbumSummary > XImage ×4

          实测：点标签，三轮朋友圈窗口一次都没出来；点右边那个 Button 2/2 成，
          点整行（中心 x=742，正好落在那个 Button 里）也是 2/2 成。
          首选那个 Button——它是个 Button，比「整行中心」少依赖一个「中心落在哪」的假设；
          整行留作退路，认名字再退一步。
        """
        for wanted in ("mmui::ProfileDetailValueSnsView", "SnsView"):
            for node in pane.descendants():
                try:
                    class_name = node.element_info.class_name or ""
                    if class_name == wanted or (wanted == "SnsView" and wanted in class_name):
                        node.click_input()
                        return
                except Exception:
                    continue
        for node in pane.descendants():
            try:
                if (node.element_info.name or "").strip() in self.MOMENTS_LABELS:
                    node.click_input()
                    return
            except Exception:
                continue
        for selector in (dict(Buttons.MomentsButton),
                         {"control_type": "Button",
                          "class_name": "mmui::ProfileDetailValueSnsView"}):
            try:
                button = pane.child_window(**selector)
                if button.exists(timeout=1):
                    button.click_input()
                    return
            except Exception:
                continue
        raise WeChatApiError("资料面板里找不到「朋友圈」入口")

    #: 头像相对左侧工具栏顶边的偏移，**只在量不出来的时候用**。
    #: 真机实测 4.1.12（缩放 100%）：工具栏 T160、第一个按钮「微信」T256，头像在这段空白里，
    #: 中心大约 T212 → 偏移 52。
    #:
    #: ★ 这个数只对 100% 缩放成立。见 `_avatar_offset_y`：正常路径是**现量**，不用这个常数。
    AVATAR_OFFSET_Y = 52

    #: 第一个工具栏按钮（「微信」）的判据。和 vendor 的 `Buttons.WeixinButton` 一致：
    #: **按位置认不按名字认**，所以不随客户端语言变。
    FIRST_TOOLBAR_BUTTON: ClassVar[dict[str, object]] = {"control_type": "Button", "found_index": 0}

    #: 头像最多点几次。真机上偶尔一下不生效（前台刚切过来、或者被合成双击吞掉）。
    AVATAR_CLICK_TRIES = 3

    #: 个人资料面板的 class。title 跟客户端语言走（4.1.12 上是 `Weixin`），所以**只认 class**。
    PROFILE_PANE_CLASS = "mmui::ProfileUniquePop"

    def _profile_pane_now(self, desktop: Any) -> Any | None:
        """此刻桌面上开着的资料面板，没有就 None。**只枚举顶层窗口**。

        ★ 拿到句柄后要**换成 `desktop.window(handle=…)` 这个 WindowSpecification 再返回**：
          `desktop.windows()` 给的是 `UIAWrapper`，**没有 `.child_window()`**，
          而调用方下一步就要在面板里找「朋友圈」。按句柄查是直接命中，不走全树。

        ★ 为什么不用 `desktop.window(**判据).exists(timeout=2)`（2026-08-29 真机实测）：
          vendor 那条判据带 `top_level_only=False`，于是 pywinauto 要走一遍整个桌面的
          控件树。这台机器上开着 Chrome / 钉钉 / 一堆资源管理器窗口，**一次查询实测 3.0～3.2 秒**，
          而调用方只给了 `timeout=2` —— 预算比一次尝试还短，等于只查一次、没有重试，
          面板晚出来一点就直接判死。而面板本身**就是个顶层窗口**（真机枚举确认），
          顶层枚举一瞬间就回来了。
        """
        try:
            for window in desktop.windows():
                try:
                    if window.element_info.class_name == self.PROFILE_PANE_CLASS:
                        return desktop.window(handle=window.element_info.handle)
                except Exception:
                    continue
        except Exception:
            return None
        return None

    def _wait_profile_pane(self, desktop: Any, Windows: Any, timeout: float = 8.0) -> Any:
        """等资料面板出来。轮询顶层窗口，别再用那条会走全树的判据。

        `Windows` 已经不用了，留着是为了不改调用方签名（vendor 的判据表见
        `third_party/pywechat/pyweixin/Uielements.py::PopUpProfileWindow`）。
        """
        deadline = time.monotonic() + timeout
        while True:
            pane = self._profile_pane_now(desktop)
            if pane is not None:
                return pane
            if time.monotonic() >= deadline:
                raise WeChatApiError("WeChat profile pane did not appear")
            time.sleep(0.3)

    def _open_profile_pane(self, desktop: Any, toolbar: Any, mouse: Any) -> Any:
        """点头像把资料面板叫出来。

        ★ **头像那一下是个开关**（2026-08-29 真机实测）：面板开着的时候再点一次是把它关掉。
          上一趟如果死在后面某一步、把面板留在了屏幕上，这一趟一点就关，
          现象就是「WeChat profile pane did not appear」，而且会自己反复下去。
          所以先看它在不在，在就直接用，不点。

        ★ 点完没出来要**再点一次**：三轮实测里有一轮第一下没生效（同一坐标的连续点击
          会被当成双击吞掉一次）。两次之间隔开，别再凑成一个双击。
        """
        pane = self._profile_pane_now(desktop)
        if pane is not None:
            logger.info("资料面板已经开着，这次不点头像（点了反而是关掉它）")
            return pane

        last = self.AVATAR_CLICK_TRIES
        for attempt in range(1, last + 1):
            self._click_avatar(toolbar, mouse)
            try:
                return self._wait_profile_pane(desktop, None, timeout=4.0)
            except WeChatApiError:
                if attempt == last:
                    raise
                logger.info("点了头像但资料面板没出来，再点一次", 第几次=attempt)
                # 隔开一点，免得两下被合成双击（双击会被吞掉一次）
                time.sleep(1.0)
        raise WeChatApiError("WeChat profile pane did not appear")

    def _avatar_offset_y(self, toolbar: Any, rect: Any) -> int:
        """头像中心离工具栏顶边多远，**现量，不写死像素**。

        ★ 为什么不能写死（2026-08-30 客户机实报「同样的微信版本，我这台能发，客户那台
          报 profile pane did not appear」）：`click_input(coords=…)` 收的是**真实像素**，
          而 52 这个数是在缩放 100% 的机器上量的。客户那台若开着 125%/150% 缩放，
          同样一版微信的头像中心就在 65 / 78 像素处，点 52 会落在头像上方的空白里——
          点了个寂寞，面板当然不出来。**同版本、不同机器**的差别几乎都是这一类。

        量法：头像待在**工具栏顶边**和**第一个按钮**之间那段空白里，取中点。
        两个矩形来自同一个坐标系，所以不管系统缩放多少、这两个数一起变，比值不变。
        真机 100% 上算出来是 (256-160)//2 = 48，实测中心 52，都落在头像圈里。

        量不到（拿不到按钮、或者数看着不合理）就退回常数，并**把量到的数写进日志**——
        下次再有机器报这个错，日志里直接就有几何数据，不必再去猜。
        """
        top = getattr(rect, "top", None)
        button_top = None
        try:
            button = toolbar.child_window(**self.FIRST_TOOLBAR_BUTTON)
            # ★ 先 `exists` 限一个短预算再取矩形：`rectangle()` 找不到会按 pywinauto 的默认
            #   超时（5s）干等，而这里一轮要点三次，白等 15 秒。工具栏的子控件只有那几个按钮，
            #   查的是**一小棵子树**（不是 `_profile_pane_now` 里那种走全桌面的判据），1.5s 绰绰有余。
            if not button.exists(timeout=1.5):
                raise LookupError("工具栏里没有这个按钮")
            button_top = button.rectangle().top
        except Exception as exc:  # noqa: BLE001 —— 量不到不是错，退回常数继续
            logger.info("量不到第一个工具栏按钮，头像偏移退回常数", 原因=str(exc))

        if isinstance(top, int) and isinstance(button_top, int):
            gap = button_top - top
            # 合理性闸：头像那段空白总得有点高度，也不可能比按钮还高一大截。
            # 不合理多半是认错了控件，这时候常数比一个错的测量值可靠。
            if 16 <= gap <= 400:
                offset = gap // 2
                logger.info(
                    "现量头像位置",
                    工具栏顶=top, 第一个按钮顶=button_top, 空白高=gap, 用的偏移=offset,
                    写死的偏移=self.AVATAR_OFFSET_Y,
                )
                return offset
            logger.info("量到的空白高看着不对，退回常数", 工具栏顶=top, 第一个按钮顶=button_top, 空白高=gap)

        logger.info("头像偏移用写死的常数（只对 100% 缩放准）", 偏移=self.AVATAR_OFFSET_Y)
        return self.AVATAR_OFFSET_Y

    def _click_avatar(self, toolbar: Any, mouse: Any) -> None:
        """点左侧工具栏顶上的头像。它**不是一个 UIA 控件**（工具栏的子控件只有
        微信/通讯录/收藏/视频号/游戏中心/小程序面板/手机/更多），所以只能按位置点。

        ★ 但**不能用屏幕绝对坐标盲点**（2026-08-29 真机实测，这就是线上那条
          「WeChat profile pane did not appear」的真因）：那台机器上 Chrome 最大化盖在
          微信上面，`mouse.click(coords=...)` 那一下**点进了 Chrome**，微信毫不知情。
          同一轮里点「通讯录」却是好的——因为 `click_input()` 会先 `set_focus()`
          把窗口提到前台，再按**控件自己的**矩形算坐标。截图为证：按微信窗口的矩形截出来
          是一个 Chrome 页面。

        所以这里走 `toolbar.click_input(coords=…)`（相对工具栏左上角），
        顺带把微信提到前台。老的绝对坐标只作兜底。
        """
        rect = toolbar.rectangle()
        offset_y = self._avatar_offset_y(toolbar, rect)
        try:
            # 先把窗口提到前台并让它稳一下再点：`click_input` 自己也会 set_focus，
            # 但刚被提上来的窗口马上接到点击时会漏掉一下（真机 5 轮里漏过 1 轮）。
            try:
                toolbar.set_focus()
                time.sleep(0.4)
            except Exception:  # noqa: BLE001 —— 提前台失败不影响点，继续
                pass
            toolbar.click_input(coords=(rect.width() // 2, offset_y))
            return
        except Exception as exc:  # noqa: BLE001 —— 兜底那条还能试，别在这儿断掉
            logger.info("按控件坐标点头像没成，退回屏幕坐标", 原因=str(exc))
        mouse.click(coords=(rect.mid_point().x, rect.top + offset_y))

    def _normalize_moments_media_paths(self, medias: list[str]) -> list[str]:
        valid_paths: list[str] = []
        missing_paths: list[str] = []
        for media in medias:
            raw = str(media).strip().strip('"').strip("'")
            if not raw:
                continue
            path = Path(raw).expanduser()
            if not path.is_absolute():
                path = self.project_root / path
            try:
                path = path.resolve()
            except OSError:
                missing_paths.append(media)
                continue
            if path.is_file():
                valid_paths.append(str(path))
            else:
                missing_paths.append(media)
        if missing_paths:
            logger.info("moments.post.media.missing", paths=missing_paths[:5])
        if not valid_paths:
            raise WeChatApiError(f"Moments media paths do not exist: {medias}")
        return valid_paths

    @staticmethod
    def _build_native_file_dialog_value(paths: list[str]) -> str:
        if len(paths) == 1:
            return paths[0]
        return " ".join(f'"{path}"' for path in paths)

    def _wait_native_file_dialog(self, desktop: Any, Windows: Any) -> Any:
        selectors = [
            {
                "class_name": "#32770",
                "control_type": "Window",
                "framework_id": "Win32",
                "top_level_only": False,
            },
            Windows.NativeChooseFileWindow,
        ]
        for selector in selectors:
            try:
                window = desktop.window(**selector)
                if window.exists(timeout=3):
                    window.wait("exists visible ready", timeout=5)
                    return window
            except Exception:
                continue
        raise WeChatApiError("Native file dialog did not appear")

    @staticmethod
    def _is_win32_wrapper(control: Any) -> bool:
        return getattr(getattr(control, "backend", None), "name", "") == "win32"

    def _choose_native_files_win32(self, dialog: Any, dialog_value: str, pyautogui: Any) -> None:
        """win32 后端拿到的「选择文件」框：底部文件名 `Edit` 直接填，再点「打开(&O)」。"""
        edits = [child for child in dialog.children() if _safe_call(child.class_name) == "Edit"]
        if not edits:
            raise WeChatApiError("Native file name edit was not found")
        edit = edits[0]
        with suppress(Exception):
            dialog.set_focus()
        with suppress(Exception):
            edit.set_focus()
        edit.set_edit_text(dialog_value)
        if _safe_call(edit.window_text) != dialog_value:
            with suppress(Exception):
                SystemSettings = self._pyweixin_class("SystemSettings")
                SystemSettings.copy_text_to_clipboard(dialog_value)
                pyautogui.hotkey("ctrl", "a", _pause=False)
                pyautogui.hotkey("ctrl", "v", _pause=False)
        time.sleep(0.3)
        for child in dialog.children():
            text = str(_safe_call(child.window_text) or "").replace("&", "").lower()
            if _safe_call(child.class_name) == "Button" and text in {"打开(o)", "打开", "open", "開啟(o)", "開啟"}:
                child.click()
                return
        pyautogui.hotkey("alt", "o", _pause=False)

    def _choose_native_files(self, native_window: Any, paths: list[str], pyautogui: Any) -> None:
        dialog_value = self._build_native_file_dialog_value(paths)
        if self._is_win32_wrapper(native_window):
            self._choose_native_files_win32(native_window, dialog_value, pyautogui)
            return
        edit = self._find_native_file_name_edit(native_window)
        try:
            native_window.set_focus()
        except Exception:
            pass
        edit.click_input()
        try:
            edit.set_edit_text(dialog_value)
        except Exception:
            edit.set_text(dialog_value)
        actual = _safe_call(edit.window_text)
        if actual != dialog_value:
            try:
                SystemSettings = self._pyweixin_class("SystemSettings")
                SystemSettings.copy_text_to_clipboard(dialog_value)
                pyautogui.hotkey("ctrl", "a", _pause=False)
                pyautogui.hotkey("ctrl", "v", _pause=False)
            except Exception:
                pass

        open_button = self._find_native_open_button(native_window)
        if open_button is not None:
            open_button.click_input()
        else:
            pyautogui.hotkey("alt", "o", _pause=False)

    def _find_native_file_name_edit(self, native_window: Any) -> Any:
        for selector in (
            {"auto_id": "1148", "control_type": "Edit"},
            {"automation_id": "1148", "control_type": "Edit"},
        ):
            try:
                edit = native_window.child_window(**selector)
                if edit.exists(timeout=0.2):
                    return edit
            except Exception:
                continue

        edits = self._native_dialog_descendants(native_window, "Edit")
        visible_edits = [edit for edit in edits if _control_available(edit)]
        if visible_edits:
            return sorted(visible_edits, key=lambda item: item.rectangle().top)[-1]
        raise WeChatApiError("Native file name edit was not found")

    def _find_native_open_button(self, native_window: Any) -> Any | None:
        for button in self._native_dialog_descendants(native_window, "Button"):
            auto_id = _safe_call(lambda: button.element_info.automation_id)
            if auto_id == "1":
                return button
        for button in self._native_dialog_descendants(native_window, "Button"):
            text = _native_control_text(button).lower()
            auto_id = _safe_call(lambda: button.element_info.automation_id)
            if auto_id == "DropDown":
                continue
            if text in {"open", "打开", "打开(o)", "打开(&o)", "開啟", "開啟(o)", "開啟(&o)"}:
                return button
        return None

    @staticmethod
    def _native_dialog_descendants(native_window: Any, control_type: str) -> list[Any]:
        try:
            return list(native_window.descendants(control_type=control_type))
        except TypeError:
            return [
                item
                for item in native_window.descendants()
                if _safe_call(lambda: item.element_info.control_type) == control_type
            ]

    def send_current_chat_message(
        self,
        text: str,
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        if self._is_dry_run(dry_run):
            return {"dry_run": True, "action": "send_current_chat_message"}
        if not text:
            raise ValueError("不能发送空白消息")
        self._ensure_ready()
        window = self._current_main_window()
        chat_name = self._current_chat_name(window)
        edit_area = self._resolve_chat_edit(window)
        if not _control_available(edit_area):
            raise WeChatApiError("当前窗口没有可用的聊天输入框")

        try:
            pyautogui = __import__("pyautogui")
            SystemSettings = self._pyweixin_class("SystemSettings")
            edit_area.click_input()
            edit_area.set_text("")
            SystemSettings.copy_text_to_clipboard(text)
            pyautogui.hotkey("ctrl", "v", _pause=False)
            time.sleep(0.2)
            pyautogui.hotkey("alt", "s", _pause=False)
        except Exception as exc:
            raise WeChatApiError(f"当前会话发送失败：{exc}") from exc

        return {
            "dry_run": False,
            "action": "send_current_chat_message",
            "chat": chat_name,
        }

    def check_unread_messages(self, dry_run: bool | None = None) -> dict[str, Any]:
        if self._is_dry_run(dry_run):
            return {
                "dry_run": True,
                "action": "check_unread_messages",
                "messages_by_chat": {},
            }
        self._ensure_ready()
        messages_by_chat, errors_by_chat = self._check_unread_messages_without_select()
        return {
            "dry_run": False,
            "action": "check_unread_messages",
            "messages_by_chat": messages_by_chat,
            "errors_by_chat": errors_by_chat,
        }

    def get_unread_messages(self) -> dict[str, Any]:
        """Return unread counts and session previews without opening any chat."""
        self._ensure_ready()
        utils = self._pyweixin_submodule("utils")
        Navigator = self._pyweixin_class("Navigator")
        try:
            main_window = Navigator.open_weixin(is_maximize=False)
            unread_counts = utils.scan_for_new_messages(
                main_window=main_window,
                is_maximize=False,
                close_weixin=False,
            )
            sessions = self._collect_chat_sessions(main_window)
        except Exception as exc:
            raise WeChatApiError(f"读取未读消息失败：{exc}") from exc

        session_by_name = {str(item.get("name")): item for item in sessions}
        unread_sessions: list[dict[str, Any]] = []
        total_unread = 0
        for raw_name, raw_count in (unread_counts or {}).items():
            name = str(raw_name).strip()
            count = _safe_int(raw_count)
            if not name or count <= 0:
                continue
            session = session_by_name.get(name, {})
            unread_sessions.append(
                {
                    "name": name,
                    "unread_count": count,
                    "preview": session.get("preview"),
                    "timestamp_text": session.get("timestamp_text"),
                }
            )
            total_unread += count
        logger.info(
            "messages.unread.read",
            session_count=len(unread_sessions),
            total_unread=total_unread,
        )
        return {
            "action": "get_unread_messages",
            "unread_sessions": unread_sessions,
            "unread_session_count": len(unread_sessions),
            "total_unread": total_unread,
            "marks_read": False,
        }

    def read_chat_sessions(self, dry_run: bool | None = None) -> dict[str, Any]:
        if self._is_dry_run(dry_run):
            return {
                "dry_run": True,
                "action": "read_chat_sessions",
                "sessions": [],
                "selected_session": None,
            }
        self._ensure_ready()
        window = self._current_main_window()
        sessions = self._collect_chat_sessions(window)
        selected = next((session for session in sessions if session.get("is_selected")), None)
        if selected is None and sessions:
            selected = sessions[0]
        logger.info(
            "chat.sessions.read",
            session_count=len(sessions),
            selected_session=selected.get("name") if isinstance(selected, dict) else None,
        )
        return {
            "dry_run": False,
            "action": "read_chat_sessions",
            "sessions": sessions,
            "selected_session": selected,
            "session_count": len(sessions),
        }

    def read_chat_history(self, friend: str, limit: int = 50) -> dict[str, Any]:
        normalized_friend = str(friend or "").strip()
        if not normalized_friend:
            raise ValueError("会话名称不能为空")
        safe_limit = min(max(int(limit), 1), 200)
        self._ensure_ready()
        logger.info("chat.history.start", friend=normalized_friend, limit=safe_limit)
        # ★ 先在**主窗口**里读，读不了才退回弹独立窗口（用户 2026-08-14：「查询记录时
        # 不要点击弹出的窗口不行吗」）。弹窗的代价是实打实的：每读一个会话就弹一个
        # mmui::ChatSingleWindow 抢焦点、读完再关掉；专员正在用电脑时窗口乱跳，
        # 而且真机上它本身就常失败（16:15「读取聊天记录失败：mmui::ChatSingleWindow」）。
        # 主窗口这条路读的是同一个消息列表控件（`read_current_chat` 一直这么读）。
        try:
            main_window = self._open_main_dialog_window(normalized_friend, select=False)
            messages = self._collect_history_pages(main_window, normalized_friend, safe_limit)
            reused_window = False
        except Exception as main_exc:
            logger.info(
                "chat.history.main_window_fallback",
                friend=normalized_friend,
                error=str(main_exc)[:200],
            )
            dialog_window = None
            try:
                dialog_window, reused_window = self._open_or_reuse_separate_dialog_window(
                    friend=normalized_friend,
                    select=False,
                    window_minimize=False,
                )
                messages = self._collect_history_pages(
                    dialog_window, normalized_friend, safe_limit
                )
            except Exception as exc:
                raise WeChatApiError(f"读取聊天记录失败：{exc}") from exc
            finally:
                if dialog_window is not None:
                    self._try_close_window(dialog_window)
        messages = messages[-safe_limit:]
        logger.info("chat.history.done", friend=normalized_friend, message_count=len(messages))
        return {
            "action": "read_chat_history",
            "friend": normalized_friend,
            "messages": messages,
            "message_count": len(messages),
            "limit": safe_limit,
            "window_reused": reused_window,
        }

    def save_message_attachment(
        self,
        friend: str,
        target_dir: str,
        runtime_id: str = "",
        order: int = -1,
        text: str = "",
        timeout_seconds: float = 20.0,
    ) -> dict[str, Any]:
        """把当前会话里的一条图片/文件气泡另存到 ``target_dir``（104）。

        走**右键 → 「另存为...」 → 系统保存对话框**，但**存到哪由我们定**（用户 2026-09-24：
        「需要我们保存，但是保存到我们的地址，我们控制选择什么」）：文件名框里读出微信给的默认名，
        换成 ``target_dir / 默认名`` 的**完整路径**写回去、读回核对，再点「保存(S)」。扩展名由微信按
        「保存类型」补（默认名不带扩展名，真机见过 ``微信图片_20260924171455_3963_4`` → ``.jpg``），
        所以落没落下按「目录里多出一个文件」判，不按文件名判。

        ★ 2026-09-24 真机 4.1.13.12 查出的两个老毛病：
          · 右键原来点在消息行中心——行横跨整个聊天区、UIA 下没有子控件，中心是空白，菜单根本不弹，
            线上一直报「右键菜单里没有『另存为』」。现在截行图按颜色找气泡本体（`find_bubble_span`）。
          · 保存对话框（``#32770``「另存为...」）在 UIA 里挂在**微信主窗口下面**，不是桌面顶层窗口；
            原来只在桌面顶层里等，永远等不到。现在先看主窗口的子节点，再看桌面顶层（老版本）。

        失败一律抛 `WeChatApiError` 让网关如实放弃，不重试、不猜路径；对话框点「取消」关掉，
        绝不对微信主窗口按 ESC。
        """
        normalized_friend = str(friend or "").strip()
        if not normalized_friend:
            raise ValueError("会话名称不能为空")
        if not str(target_dir or "").strip():
            raise ValueError("保存目录不能为空")
        target = Path(str(target_dir)).expanduser()
        target.mkdir(parents=True, exist_ok=True)
        self._ensure_ready()
        logger.info("chat.attachment.save.start", friend=normalized_friend, text=str(text or "")[:40])
        # 另存分六小步，每一步都打一行（像 AI 端阶段那样看得见走到哪），失败时说清停在哪一步。
        # 2026-09-29：客户机上只看到「没能保存下来」，说不出卡在菜单、对话框还是落盘。
        progress = _SaveProgress(normalized_friend)
        try:
            return self._save_message_attachment_steps(
                progress, normalized_friend, target, runtime_id, order, text, timeout_seconds
            )
        except Exception as exc:
            logger.warning(
                "chat.attachment.save.failed",
                friend=normalized_friend,
                step=progress.step,
                total=_SAVE_STEP_TOTAL,
                stage=progress.next_label,
                error=str(exc)[:160],
            )
            raise

    def _save_message_attachment_steps(
        self,
        progress: _SaveProgress,
        normalized_friend: str,
        target: Path,
        runtime_id: str,
        order: int,
        text: str,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        window = self._open_main_dialog_window(normalized_friend, select=False)
        chat_list = self._resolve_chat_list(window)
        if chat_list is None:
            raise WeChatApiError("未找到聊天消息列表")
        progress.done()   # 1 打开会话
        item = _matching_list_item_for_message(
            chat_list, {"runtime_id": runtime_id or "", "order": order, "text": text or ""}
        )
        if item is None:
            raise WeChatApiError("这条消息不在当前窗口的可见区里，没法另存")
        progress.done()   # 2 找到这条消息

        with suppress(Exception):
            window.set_focus()
            time.sleep(0.3)
        before = {entry.name for entry in target.iterdir()}
        mouse = __import__("pywinauto", fromlist=["mouse"]).mouse
        item = self._scroll_row_into_view(chat_list, item, text or "", mouse)
        click_at = self._bubble_click_point(item, _safe_call(chat_list.rectangle))
        mouse.right_click(coords=click_at)
        time.sleep(random.uniform(0.4, 0.9))

        Uielements = self._pyweixin_submodule("Uielements")
        save_item = window.child_window(**Uielements.MenuItems.SaveMenuItem)
        if not save_item.exists(timeout=2.0):
            # 菜单没弹 / 没有「另存为」= 这不是可保存的气泡（表情、已过期的图、系统消息）。
            # 把菜单点掉（vendor 同款：在原位再点一下），绝不按 ESC——那会关掉微信窗口。
            with suppress(Exception):
                mouse.click(coords=click_at)
            raise WeChatApiError("右键菜单里没有「另存为」，这条内容不可保存（可能已过期或不是图片/文件）")
        save_item.click_input()
        progress.done()   # 3 右键菜单点了「另存为」

        dialog = self._await_save_dialog(window, timeout=8.0)
        if dialog is None:
            raise WeChatApiError("点了「另存为」但保存对话框没出现")
        progress.done()   # 4 保存对话框弹出来了
        try:
            self._fill_save_dialog(dialog, target)
            progress.done()   # 5 路径填好、点了保存
            saved = _await_saved_file(target, before, timeout=timeout_seconds)
        except Exception:
            _cancel_save_dialog(dialog)
            raise
        size = saved.stat().st_size
        progress.done(f"{saved.name}，{size} 字节")   # 6 文件落到本机
        logger.info("chat.attachment.save.done", friend=normalized_friend, name=saved.name, bytes=size)
        return {
            "action": "save_message_attachment",
            "friend": normalized_friend,
            "path": str(saved),
            "file_name": saved.name,
            "bytes": size,
        }

    @staticmethod
    def _await_save_dialog(window: Any, timeout: float) -> Any | None:
        """等「另存为」对话框（Win32 ``#32770``）。4.1.13 挂在微信主窗口下面，老版本在桌面顶层。"""
        Desktop = __import__("pywinauto", fromlist=["Desktop"]).Desktop
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with suppress(Exception):  # 窗口正在出现/消失时枚举会抛，下一拍再看
                found = find_save_dialog(window.children())
                if found is None:
                    found = find_save_dialog(Desktop(backend="uia").windows())
                if found is not None:
                    return found
            time.sleep(0.25)
        return None

    @staticmethod
    def _fill_save_dialog(dialog: Any, target: Path) -> None:
        """文件名框：默认名 → ``target / 默认名`` 完整路径，读回核对，再点「保存(S)」。"""
        edit = dialog_control(dialog, "Edit", "1001")
        if edit is None:
            raise WeChatApiError("保存对话框里找不到文件名输入框")
        default_name = _edit_value(edit)
        full_path = save_path_for(target, default_name)
        # set_edit_text 走 UIA ValuePattern：不经键盘、不经剪贴板，中文路径也不会被输入法吃掉。
        edit.set_edit_text(str(full_path))
        time.sleep(0.3)
        if _edit_value(edit) != str(full_path):
            raise WeChatApiError("保存对话框的文件名没填进去（读回来对不上）")
        button = dialog_control(dialog, "Button", "1")
        if button is None:
            raise WeChatApiError("保存对话框里找不到「保存」按钮")
        try:
            button.invoke()
        except Exception:  # noqa: BLE001 —— invoke 不支持就老实点一下
            button.click_input()

    @staticmethod
    def _scroll_row_into_view(chat_list: Any, item: Any, text: str, mouse: Any) -> Any:
        """把这一行整条滚进消息列表的可见区，返回滚完之后的那一行（控件对象会换）。

        ★ 2026-09-28 真机：文件气泡一行 151px 高，常常半截露在列表外（行顶 171、列表顶 272）。
          截图截到的是窗口标题栏，认不出气泡就退回行中心——那一点也在标题栏上，右键不弹消息菜单。
        """
        for _ in range(8):
            rect = _safe_call(item.rectangle)
            view = _safe_call(chat_list.rectangle)
            if rect is None or view is None:
                return item
            if rect.top < view.top:
                wheel = 1
            elif rect.bottom > view.bottom:
                wheel = -1
            else:
                return item
            mouse.scroll(coords=(int(view.mid_point().x), int(view.mid_point().y)), wheel_dist=wheel)
            time.sleep(0.3)
            again = _matching_list_item_for_message(chat_list, {"text": text}) if text else None
            if again is not None:
                item = again
        return item

    @staticmethod
    def _bubble_click_point(item: Any, view: Any = None) -> tuple[int, int]:
        """这一行里气泡本体的中心（屏幕坐标）。截不到图 / 认不出气泡就退回行中心（原来的点法）。

        ``view`` 是消息列表的可见区：只截行在可见区里的那一段，行半截露在外面时也点不到列表外头去。
        """
        rect = item.rectangle()
        left, top, right, bottom = rect.left, rect.top, rect.right, rect.bottom
        if view is not None:
            top, bottom = max(top, view.top), min(bottom, view.bottom)
            if bottom - top < 10:
                top, bottom = rect.top, rect.bottom
        fallback = (int((left + right) // 2), int((top + bottom) // 2))
        try:
            ImageGrab = __import__("PIL.ImageGrab", fromlist=["ImageGrab"])
            shot = ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True).convert("RGB")
        except Exception as exc:  # noqa: BLE001 —— 截不到就按老办法点，别在这儿断
            logger.warning("chat.attachment.bubble.screenshot_failed", error=str(exc)[:120])
            return fallback
        span = find_bubble_span(shot.load(), *shot.size)
        if span is None:
            logger.warning(
                "chat.attachment.bubble.not_found",
                width=shot.size[0], height=shot.size[1], row=str(rect), view=str(view),
            )
            return fallback
        x, y = span
        logger.info("chat.attachment.bubble.click", x=left + x, y=top + y, row=str(rect), view=str(view))
        return (left + x, top + y)

    def _collect_history_pages(
        self, window: Any, chat_name: str, safe_limit: int
    ) -> list[dict[str, Any]]:
        """在给定窗口里往上翻页收集消息。主窗口和独立聊天窗口的消息列表控件是一样的。"""
        chat_list = self._resolve_chat_list(window)
        if chat_list is None:
            raise WeChatApiError("未找到聊天消息列表")
        # ★ 先回到底部再读（2026-09-23 线上）：这里往上翻了几页之后靠末尾那个 {END} 回底，
        #   它一旦没生效（或专员自己翻着历史看），下一趟读到的就是一窗**旧消息**——最新的
        #   几条根本不在里面，网关那头游标对不上，整段旧对话被当成新消息重跑一遍。
        #   读取窗口的语义是「最新的 N 条」，那就每次都从底部开始。
        try:
            chat_list.click_input()
            chat_list.type_keys("{END}")
            time.sleep(0.15)
        except Exception:  # noqa: BLE001, S110 —— 回不了底就按现状读，后面的翻页照旧
            pass
        messages: list[dict[str, Any]] = []
        seen: set[tuple[Any, ...]] = set()
        max_pages = min(max((safe_limit + 7) // 8, 2), 30)
        for _ in range(max_pages):
            current = self._collect_visible_message_nodes(
                window,
                chat_name=chat_name,
                log_collection=False,
            )
            older = [item for item in current if _message_key(item) not in seen]
            for item in current:
                seen.add(_message_key(item))
            if older:
                messages = older + messages
            elif messages:
                break
            if len(messages) >= safe_limit:
                break
            chat_list.click_input()
            chat_list.type_keys("{PGUP}")
            time.sleep(0.25)
            if not current:
                break
        try:
            chat_list.type_keys("{END}")
        except Exception:
            pass
        return messages

    def scan_messages(
        self,
        friend: str,
        duration: str = "30s",
        select_sender: bool = False,
        save_file: bool = False,
        save_media: bool = False,
        separate_window: bool = True,
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        if self._is_dry_run(dry_run):
            return {
                "dry_run": True,
                "action": "scan_messages",
                "friend": friend,
                "duration": duration,
                "select_sender": select_sender,
                "separate_window": separate_window,
                "events": [],
            }
        self._ensure_ready()
        sender_selection_error = None
        if separate_window:
            try:
                dialog_window, reused_window = self._open_or_reuse_separate_dialog_window(
                    friend=friend,
                    select=select_sender,
                    window_minimize=False,
                )
            except Exception as exc:
                if not select_sender:
                    raise
                sender_selection_error = str(exc)
                dialog_window, reused_window = self._open_or_reuse_separate_dialog_window(
                    friend=friend,
                    select=False,
                    window_minimize=False,
                )
            window_mode = "separate"
        else:
            dialog_window = self._open_main_dialog_window(friend=friend, select=False)
            reused_window = True
            window_mode = "main"
        result = self._poll_visible_messages(
            dialog_window=dialog_window,
            duration=duration,
            chat_name=friend,
            annotate_senders=True,
        )
        return {
            "dry_run": False,
            "action": "scan_messages",
            "friend": friend,
            "duration": duration,
            "window_reused": reused_window,
            "window_mode": window_mode,
            "separate_window": separate_window,
            "save_file": save_file,
            "save_media": save_media,
            "sender_selection_error": sender_selection_error,
            "result": result,
        }

    def read_visible_messages(
        self,
        friend: str,
        separate_window: bool = True,
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        if self._is_dry_run(dry_run):
            return {
                "dry_run": True,
                "action": "read_visible_messages",
                "friend": friend,
                "separate_window": separate_window,
                "messages": [],
            }
        self._ensure_ready()
        dialog_window = None
        window_mode = "main"
        reused_window = True
        messages: list[dict[str, Any]] = []
        try:
            if separate_window:
                dialog_window, reused_window = self._open_or_reuse_separate_dialog_window(
                    friend=friend,
                    select=False,
                    window_minimize=False,
                )
                window_mode = "separate"
            else:
                dialog_window = self._open_main_dialog_window(friend=friend, select=False)
                reused_window = True
                window_mode = "main"
            messages = self._collect_visible_message_nodes(dialog_window, chat_name=friend)
        finally:
            if separate_window and dialog_window is not None:
                self._try_close_window(dialog_window)
        return {
            "dry_run": False,
            "action": "read_visible_messages",
            "friend": friend,
            "window_reused": reused_window,
            "window_mode": window_mode,
            "separate_window": separate_window,
            "messages": messages,
        }

    def read_current_chat(self, dry_run: bool | None = None) -> dict[str, Any]:
        if self._is_dry_run(dry_run):
            return {
                "dry_run": True,
                "action": "read_current_chat",
                "chat": None,
                "messages": [],
                "latest_message": None,
                "latest_is_self": None,
            }
        self._ensure_ready()
        window = self._current_main_window()
        messages = self._collect_visible_message_nodes(window)
        selected_session = self._selected_session_info(window)
        chat_name = self._current_chat_name(window) or selected_session.get("name")
        if not chat_name and messages:
            chat_name = "当前会话"
        latest = _last_replyable_message(messages)
        latest_activity = _last_visible_activity(messages)
        latest_is_self = latest.get("is_self") if latest else None
        if latest_activity is not None and _is_self_activity_row(latest_activity):
            latest = latest_activity
            latest_is_self = True
            latest["is_self"] = True
            latest["sender"] = "\u6211"
        if (
            latest is not None
            and latest_is_self is None
            and _session_preview_is_self_message(selected_session.get("preview"), str(latest.get("text") or ""))
        ):
            latest_is_self = True
            latest["is_self"] = True
            latest["sender"] = "\u6211"
        if latest is not None and latest_is_self is None:
            latest_is_self = self._latest_message_self_by_context_menu(window)
            latest["is_self"] = latest_is_self
        return {
            "dry_run": False,
            "action": "read_current_chat",
            "chat": chat_name,
            "selected_session": selected_session,
            "messages": messages,
            "latest_message": latest,
            "latest_is_self": latest_is_self,
        }

    def _is_dry_run(self, override: bool | None) -> bool:
        return self.dry_run if override is None else override

    def _ensure_ready(self) -> None:
        probe = probe_wechat_uia(
            max_depth=DEFAULT_UIA_PROBE_MAX_DEPTH,
            max_nodes=DEFAULT_UIA_PROBE_MAX_NODES,
        )
        logger.info(
            "uia.operation_probe",
            uia_exposed=bool(probe.get("uia_exposed")),
            reason=probe.get("reason"),
        )
        # 死结：窗口最小化时微信把 UIA 子树收了（真机实测 descendant_count 掉到阈值以下），
        # 探测就报「未暴露」——可这恰恰是**要靠恢复窗口来解**的状态。所以不能直接拒：
        # 先按 hwnd 恢复一次再复探，复探还是不行才是真不可用（没登录 / 锁屏 / 微信没起）。
        # 2026-08-12 真机：网关常驻轮询在这里连挂十几分钟，恢复后同一轮立刻通。
        if not probe.get("uia_exposed") and self._restore_if_minimized(
            probe.get("selected_window"), force=True
        ):
            probe = probe_wechat_uia(
                max_depth=DEFAULT_UIA_PROBE_MAX_DEPTH,
                max_nodes=DEFAULT_UIA_PROBE_MAX_NODES,
            )
            logger.info(
                "uia.operation_probe.after_restore",
                uia_exposed=bool(probe.get("uia_exposed")),
                reason=probe.get("reason"),
            )
        if not probe.get("uia_exposed"):
            raise WeChatApiUnavailableError(
                f"微信 UIA 不可用：{probe.get('reason') or '未知原因'}"
            )
        selected = probe.get("selected_window")
        if isinstance(selected, dict):
            self._wechat_version = str(selected.get("process_version") or "")
        self._restore_if_minimized(selected)

    @classmethod
    def _restore_if_minimized(cls, window_info: Any, *, force: bool = False) -> bool:
        """微信最小化时先恢复窗口，否则后面所有 UI 动作都会失败。返回是否真的动了窗口。

        ``force=True`` 用于「探测说未暴露」那条路：此时 UIA 子树已经塌了，``is_iconic``
        不一定还看得见，但只要还找得到窗口句柄就值得恢复一次再复探——反正塌着也用不了。

        2026-08-12 真机实测：窗口最小化时 ``Application.connect(title="微信")`` 直接抛
        ElementNotFoundError（报的是查找条件那个字典），pyweixin 那条路同样跑不动，
        而 ``uia_probe`` 因为自己枚举窗口所以照样报「UIA 已暴露」——两边表现不一致，
        最容易被误读成「微信没登录」。所以恢复必须发生在 connect 之前，且只能用 hwnd。

        与 ``uia_probe`` 那条「只读、不启动/不解锁微信」的约束不冲突：那是探测的纪律，
        这里是执行动作的前提，窗口本来就在、也已登录。
        """
        hwnd = cls._minimized_handle(window_info, force=force)
        if hwnd is None:
            return False
        try:
            win32gui = __import__("win32gui")
            win32con = __import__("win32con")
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            time.sleep(0.5)  # 给窗口一点时间真正显示出来（UIA 子树也要重新长出来）
            logger.info("window.restored", hwnd=hwnd, forced=force)
        except Exception as exc:  # noqa: BLE001 - 恢复失败不该盖住后面真正的定位错误
            logger.warning("window.restore_failed", error=f"{type(exc).__name__}: {exc}")
            return False
        return True

    @staticmethod
    def _minimized_handle(window_info: Any, *, force: bool = False) -> int | None:
        """从探测结果里挑出「需要恢复」的窗口句柄；不需要恢复就返回 None。"""
        if not isinstance(window_info, dict):
            return None
        if not force and not window_info.get("is_iconic"):
            return None
        handle = window_info.get("handle")
        try:
            return int(handle) if handle else None
        except (TypeError, ValueError):
            return None

    def _pyweixin_class(self, name: str) -> Any:
        vendor = self.project_root / "third_party" / "pywechat"
        if str(vendor) not in sys.path:
            sys.path.insert(0, str(vendor))
        try:
            module = __import__("pyweixin", fromlist=[name, "GlobalConfig"])
        except ImportError as exc:
            raise WeChatApiUnavailableError(f"pyweixin is unavailable: {exc}") from exc

        _disable_esc_close_fallback()

        config = getattr(module, "GlobalConfig", None)
        if config is not None:
            config.close_weixin = False
            config.is_maximize = False
            config.search_pages = 5
            config.clear = True
            config.send_delay = 0.2
        return getattr(module, name)

    def _pyweixin_submodule(self, name: str) -> Any:
        vendor = self.project_root / "third_party" / "pywechat"
        if str(vendor) not in sys.path:
            sys.path.insert(0, str(vendor))
        try:
            self._pyweixin_class("Navigator")
            return __import__(f"pyweixin.{name}", fromlist=["*"])
        except ImportError as exc:
            raise WeChatApiUnavailableError(f"pyweixin.{name} is unavailable: {exc}") from exc

    def _current_main_window(self) -> Any:
        try:
            Application = __import__("pywinauto", fromlist=["Application"]).Application
            try:
                app = Application(backend="uia").connect(title="微信")
                main_window = app.window(title="微信")
                main_window.wait("ready", timeout=5)
                return main_window
            except Exception as exc:
                # 2026-09-28 用户机：「There are 2 elements that match the criteria」——同时有两个标题叫「微信」的
                # 顶层窗口（主窗口之外还开着别的同名窗口），按标题连不上就整条「读取会话列表」失败。
                # 同名时挑真正有会话列表/输入框的那个，按句柄连；只认歧义这一种错，别的照旧报。
                if type(exc).__name__ != "ElementAmbiguousError" and "elements that match" not in str(exc):
                    raise
                handle = self._pick_main_window_handle(self._titled_wechat_windows())
                if handle is None:
                    raise
                app = Application(backend="uia").connect(handle=handle)
                main_window = app.window(handle=handle)
                main_window.wait("ready", timeout=5)
                return main_window
        except Exception as exc:
            raise WeChatApiError(f"无法定位当前微信主窗口：{exc}") from exc

    @staticmethod
    def _titled_wechat_windows() -> list[Any]:
        windows = _pywinauto_top_windows() or _win32_top_windows()[0]
        return [w for w in windows if str(_safe_call(w.window_text) or "").strip() == "微信"]

    def _pick_main_window_handle(self, candidates: list[Any]) -> int | None:
        """几个同叫「微信」的顶层窗口里挑主窗口：有会话列表/输入框的分高；同分取先枚举到的。"""
        scored = [(self._current_window_score(w), index, w) for index, w in enumerate(candidates)]
        scored = [(score, index, w) for score, index, w in scored if _window_handle(w) is not None]
        if not scored:
            return None
        score, _, best = max(scored, key=lambda row: (row[0], -row[1]))
        logger.info("window.main.ambiguous_picked", candidates=len(candidates), score=score, hwnd=_window_handle(best))
        return _window_handle(best)

    def _current_window_score(self, window: Any) -> int:
        score = 0
        if self._resolve_chat_list(window) is not None:
            score += 10
        if self._resolve_chat_edit(window) is not None:
            score += 5
        try:
            if window.is_active():
                score += 2
        except Exception:
            pass
        return score

    def _current_chat_name(self, window: Any) -> str | None:
        try:
            uielements = self._pyweixin_submodule("Uielements")
        except WeChatApiError:
            return None
        texts = getattr(uielements, "Texts", None)
        criteria = getattr(texts, "CurrentChatNameText", None)
        if not criteria:
            return None
        try:
            label = window.child_window(**criteria)
            if label.exists(timeout=0.2):
                value = label.window_text().strip()
                return value or None
        except Exception:
            label = _resolve_control_by_criteria(window, criteria)
            if label is not None:
                value = str(_safe_call(getattr(label, "window_text", None)) or "").strip()
                return value or None
        return None

    def _selected_session_name(self, window: Any) -> str | None:
        return self._selected_session_info(window).get("name")

    def _selected_session_info(self, window: Any) -> dict[str, Any]:
        sessions = self._collect_chat_sessions(window)
        selected = next((session for session in sessions if session.get("is_selected")), None)
        if selected is not None:
            return selected
        return sessions[0] if sessions else {}

    def _collect_chat_sessions(self, window: Any) -> list[dict[str, Any]]:
        items = self._resolve_session_items(window)
        sessions: list[dict[str, Any]] = []
        for item in items:
            text = str(_safe_call(getattr(item, "window_text", None)) or "").strip()
            automation_id = str(_element_info_value(item, "automation_id") or "").strip()
            name = _session_name_from_automation_id(automation_id) or _session_name_from_text(text)
            if not name:
                continue
            sessions.append(
                {
                    "name": name,
                    "text": text,
                    "preview": _session_preview_from_text(text),
                    "timestamp_text": _session_timestamp_from_text(text),
                    "timestamp_is_today": _session_timestamp_is_today(_session_timestamp_from_text(text)),
                    "automation_id": automation_id or None,
                    "is_selected": bool(
                        _truthy_safe_call(getattr(item, "is_selected", None))
                        or _truthy_safe_call(getattr(item, "has_keyboard_focus", None))
                    ),
                }
            )
        return sessions

    def _resolve_session_items(self, window: Any) -> list[Any]:
        try:
            uielements = self._pyweixin_submodule("Uielements")
        except WeChatApiError:
            uielements = None

        if uielements is not None:
            main_window = getattr(uielements, "Main_window", None)
            criteria = getattr(main_window, "SessionList", None)
            if criteria:
                try:
                    session_list = _resolve_control_by_criteria(window, criteria)
                    if session_list is not None:
                        items = session_list.children(control_type="ListItem")
                        if items:
                            return list(items)
                except Exception:
                    pass

        try:
            return [
                item
                for item in window.descendants()
                if _safe_call(item.class_name) == "mmui::ChatSessionCell"
                or str(_element_info_value(item, "automation_id") or "").startswith("session_item_")
            ]
        except Exception:
            return []

    def _open_or_reuse_separate_dialog_window(
        self,
        friend: str,
        select: bool,
        window_minimize: bool,
    ) -> tuple[Any, bool]:
        dialog_window = self._find_separate_dialog_window(friend)
        if dialog_window is not None:
            self._try_focus_window(dialog_window)
            if select:
                self._try_select_chat_list(dialog_window)
            if window_minimize:
                self._try_minimize_window(dialog_window)
            return dialog_window, True

        Navigator = self._pyweixin_class("Navigator")
        dialog_window = Navigator.open_seperate_dialog_window(
            friend=friend,
            select=select,
            window_minimize=window_minimize,
            close_weixin=False,
        )
        self._try_focus_window(dialog_window)
        return dialog_window, False

    def _open_main_dialog_window(self, friend: str, select: bool) -> Any:
        Navigator = self._pyweixin_class("Navigator")
        try:
            return Navigator.open_dialog_window(
                friend=friend,
                select=select,
                is_maximize=False,
                search_pages=5,
            )
        except Exception as exc:
            raise WeChatApiError(f"打开主窗口会话「{friend}」失败：{exc}") from exc

    def _find_separate_dialog_window(self, friend: str) -> Any | None:
        try:
            Desktop = __import__("pywinauto", fromlist=["Desktop"]).Desktop
            desktop = Desktop(backend="uia")
            chat_windows = [
                window
                for window in desktop.windows()
                if _safe_call(window.class_name) == "mmui::ChatSingleWindow"
            ]
            exact_matches = []
            fuzzy_matches = []
            for window in chat_windows:
                title = str(_safe_call(window.window_text) or "").strip()
                if title == friend:
                    exact_matches.append(window)
                elif friend and title and not _is_generic_chat_window_title(title) and (friend in title or title in friend):
                    fuzzy_matches.append(window)
            matches = exact_matches or fuzzy_matches
            if not matches and len(chat_windows) == 1:
                title = str(_safe_call(chat_windows[0].window_text) or "").strip()
                if _is_generic_chat_window_title(title):
                    matches = chat_windows
            if not matches:
                exact_matches = []
                fuzzy_matches = []
                for window in chat_windows:
                    title = str(_safe_call(window.window_text) or "").strip()
                    current_chat = self._current_chat_name(window)
                    if current_chat == friend:
                        exact_matches.append(window)
                    elif friend and current_chat and (friend in current_chat or current_chat in friend):
                        fuzzy_matches.append(window)
                    elif friend and not title and not current_chat and len(chat_windows) == 1:
                        fuzzy_matches.append(window)
                matches = exact_matches or fuzzy_matches
            if not matches:
                return None
            return desktop.window(handle=matches[0].handle)
        except Exception:
            return None

    def _try_select_chat_list(self, dialog_window: Any) -> None:
        try:
            Tools = self._pyweixin_class("Tools")
            Tools.select_chatList(dialog_window)
        except Exception:
            pass

    def _try_focus_window(self, window: Any) -> None:
        try:
            window.restore()
        except Exception:
            pass
        try:
            window.set_focus()
            return
        except Exception:
            pass
        try:
            win32gui = __import__("win32gui")
            win32con = __import__("win32con")
            handle = getattr(window, "handle", None)
            if handle:
                win32gui.ShowWindow(handle, win32con.SW_RESTORE)
                win32gui.SetForegroundWindow(handle)
        except Exception:
            pass

    def _try_minimize_window(self, dialog_window: Any) -> None:
        try:
            win32gui = __import__("win32gui")
            win32con = __import__("win32con")
            handle = getattr(dialog_window, "handle", None)
            if handle:
                win32gui.SendMessage(handle, win32con.WM_SYSCOMMAND, win32con.SC_MINIMIZE, 0)
        except Exception:
            pass

    def _try_close_window(self, window: Any) -> None:
        try:
            window.close()
        except Exception:
            pass

    def _check_unread_messages_without_select(self) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str]]:
        utils = self._pyweixin_submodule("utils")
        Navigator = self._pyweixin_class("Navigator")

        try:
            main_window = Navigator.open_weixin(is_maximize=False)
            unread_counts = utils.scan_for_new_messages(
                main_window=main_window,
                is_maximize=False,
                close_weixin=False,
            )
        except Exception as exc:
            raise WeChatApiError(f"failed to scan unread sessions: {exc}") from exc

        messages_by_chat: dict[str, list[dict[str, Any]]] = {}
        errors_by_chat: dict[str, str] = {}
        for chat_name, unread_count in unread_counts.items():
            chat = str(chat_name)
            count = _safe_int(unread_count)
            if count <= 0:
                continue
            try:
                result = self.read_visible_messages(
                    friend=chat,
                    separate_window=True,
                    dry_run=False,
                )
                visible_messages = result.get("messages", [])
            except Exception as exc:
                errors_by_chat[chat] = str(exc)
                continue
            messages_by_chat[chat] = _tail_messages(visible_messages, count)
        return messages_by_chat, errors_by_chat

    def _poll_visible_messages(
        self,
        dialog_window: Any,
        duration: str,
        chat_name: str | None = None,
        annotate_senders: bool = False,
    ) -> dict[str, Any]:
        seconds = _parse_duration(duration)
        baseline = self._collect_visible_message_nodes(
            dialog_window,
            chat_name=chat_name,
            annotate_senders=False,
            log_collection=False,
        )
        seen = {_message_key(item) for item in baseline}
        events: list[dict[str, Any]] = []
        end_time = time.monotonic() + seconds

        if seconds <= 1.5:
            if seconds > 0:
                time.sleep(seconds)
        else:
            while time.monotonic() < end_time:
                current = self._collect_visible_message_nodes(
                    dialog_window,
                    chat_name=chat_name,
                    annotate_senders=False,
                    log_collection=False,
                )
                for item in current:
                    key = _message_key(item)
                    if key in seen:
                        continue
                    seen.add(key)
                    events.append(item)
                time.sleep(0.5)

        final_visible = self._collect_visible_message_nodes(
            dialog_window,
            chat_name=chat_name,
            annotate_senders=annotate_senders,
        )
        annotated_by_key = {_message_key(item): item for item in final_visible}
        if seconds <= 1.5:
            events = [
                item
                for item in final_visible
                if _message_key(item) not in seen
            ]
        else:
            events = [annotated_by_key.get(_message_key(item), item) for item in events]

        return {
            "new_message_count": len(events),
            "messages": events,
            "initial_visible_count": len(baseline),
            "baseline_messages": final_visible or baseline,
        }

    def _collect_visible_message_nodes(
        self,
        dialog_window: Any,
        chat_name: str | None = None,
        annotate_senders: bool = True,
        log_collection: bool = True,
    ) -> list[dict[str, Any]]:
        message_classes = {
            "mmui::ChatTextItemView",
            "mmui::ChatBubbleItemView",
            "mmui::ChatBubbleReferItemView",
            "mmui::ChatVoiceItemView",
            "mmui::ChatItemView",
            "mmui::ChatRecalledItemView",
        }
        rows: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        try:
            container = self._resolve_chat_list(dialog_window)
            container_rect = _safe_call(getattr(container, "rectangle", None)) if container is not None else None
            descendants = container.descendants() if container is not None else dialog_window.descendants()
        except Exception as exc:
            raise WeChatApiError(f"failed to read visible messages: {exc}") from exc

        for order, node in enumerate(descendants):
            class_name = _safe_call(node.class_name)
            control_type = _safe_call(node.friendly_class_name)
            text = (_safe_call(node.window_text) or "").strip()
            # 语音气泡没转文字时名称可能是空的，但这条消息本身不能丢——留成「转写未就绪」
            # 的行，由网关决定等还是不等（见 `wechat_io.hold_pending_voice`）。
            voice = _annotate_voice_row(class_name, text)
            if not text and voice is None:
                continue
            is_message_class = class_name in message_classes
            is_message_control = _looks_like_chat_message_control(class_name, control_type)
            if not is_message_class and not is_message_control:
                continue
            runtime_id = _safe_runtime_id(node)
            dedupe_text = text or str((voice or {}).get("voice_label") or "")
            key = (str(runtime_id), dedupe_text) if runtime_id else (str(class_name), dedupe_text)
            if key in seen:
                continue
            seen.add(key)
            rect = _safe_call(getattr(node, "rectangle", None))
            metrics = _rect_metrics(rect)
            row = {
                "class_name": class_name,
                "control_type": control_type,
                "text": text,
                "raw_text": text,
                "runtime_id": runtime_id,
                "is_self": _infer_message_is_self(rect, container_rect),
                "is_timestamp": _is_timestamp_row(class_name, text),
                "order": order,
                **metrics,
            }
            attachment = _annotate_attachment_row(class_name, text)
            if attachment is not None:
                row.update(attachment)
            if voice is not None:
                row.update(voice)
                # 真机上语音气泡的名称到底长什么样，只有这条日志能告诉我们（SSH 会话里
                # 看不到微信的 UI 树，见 memory `wxauto-win-realmachine`）。截断，别把整段转写刷进日志。
                logger.info(
                    "chat.voice.row",
                    raw=text[:60],
                    ready=not voice["transcript_pending"],
                )
            rows.append(row)
        messages = _attach_message_timestamps(_sort_message_rows(rows))
        if log_collection:
            logger.debug(
                "collect_visible_messages.collected",
                chat_name=chat_name,
                raw_node_count=len(rows),
                message_count=len(messages),
            )
        is_group = self._detect_group_chat(dialog_window)
        for message in messages:
            message["is_group"] = is_group
        if log_collection:
            logger.debug(
                "collect_visible_messages.group_detect",
                chat_name=chat_name,
                is_group=is_group,
            )
        if annotate_senders:
            self._annotate_message_senders(
                dialog_window,
                messages,
                chat_name=chat_name,
                is_group=is_group,
            )
        return messages

    def _detect_group_chat(self, window: Any) -> bool:
        try:
            Tools = self._pyweixin_class("Tools")
            if bool(Tools.is_group_chat(window)):
                return True
        except Exception:
            pass
        return _window_has_group_count_label(window)

    def _annotate_message_senders(
        self,
        window: Any,
        messages: list[dict[str, Any]],
        chat_name: str | None = None,
        is_group: bool = False,
    ) -> None:
        for message in messages:
            if _is_self_activity_row(message):
                message["is_self"] = True
                message["sender"] = "\u6211"

        try:
            Tools = self._pyweixin_class("Tools")
            chat_list = self._resolve_chat_list(window)
            edit_area = self._resolve_chat_edit(window)
            container_rect = _safe_call(getattr(chat_list, "rectangle", None))
        except Exception:
            Tools = None
            chat_list = None
            edit_area = None
            container_rect = None

        if is_group:
            _apply_group_sender_from_selected_text(messages)
        _annotate_message_side_from_screenshot(messages, container_rect)

        unknown_messages = [
            message
            for message in messages
            if _message_needs_sender_annotation(message)
        ]
        if not unknown_messages or Tools is None or chat_list is None or edit_area is None:
            return
        for message in unknown_messages:
            if not _message_inside_container(message, container_rect):
                continue
            item = _matching_list_item_for_message(chat_list, message)
            if item is None:
                continue
            try:
                is_self = bool(Tools.is_my_bubble(window, item, edit_area))
            except Exception:
                continue
            message["is_self"] = is_self
            if is_self:
                message["sender"] = "\u6211"
            elif not is_group:
                message["sender"] = chat_name or "\u5bf9\u65b9"

    def _resolve_chat_list(self, window: Any) -> Any | None:
        try:
            uielements = self._pyweixin_submodule("Uielements")
        except WeChatApiError:
            return None

        for holder_name in ("Main_window", "Lists"):
            holder = getattr(uielements, holder_name, None)
            criteria = getattr(holder, "FriendChatList", None)
            if not criteria:
                continue
            candidate = _resolve_control_by_criteria(window, criteria)
            if candidate is not None:
                return candidate
        return None

    def _resolve_chat_edit(self, window: Any) -> Any | None:
        try:
            uielements = self._pyweixin_submodule("Uielements")
        except WeChatApiError:
            return None
        candidates: list[dict[str, Any]] = []
        for holder_name, attr_name in (
            ("Edits", "CurrentChatEdit"),
            ("Edits", "InputEdit"),
            ("Main_window", "EditArea"),
        ):
            holder = getattr(uielements, holder_name, None)
            criteria = getattr(holder, attr_name, None)
            if criteria:
                candidates.append(criteria)
        for criteria in candidates:
            candidate = _resolve_control_by_criteria(window, criteria)
            if candidate is not None:
                return candidate
        return None

    def _latest_message_self_by_context_menu(self, window: Any) -> bool | None:
        try:
            Tools = self._pyweixin_class("Tools")
            chat_list = self._resolve_chat_list(window)
            edit_area = self._resolve_chat_edit(window)
            if chat_list is None or edit_area is None:
                return None
            for item in reversed(chat_list.children(control_type="ListItem")):
                text = (_safe_call(item.window_text) or "").strip()
                if not text:
                    continue
                class_name = str(_safe_call(item.class_name) or "")
                if not class_name.startswith("mmui::Chat"):
                    continue
                return bool(Tools.is_my_bubble(window, item, edit_area))
        except Exception:
            return None
        return None

_SEARCH_RESULT_SECTIONS = {
    "最近使用": ("recent", "unknown"),
    "联系人": ("contacts", "contact"),
    "群聊": ("groups", "group"),
    "服务号": ("service_accounts", "service_account"),
    "公众号": ("official_accounts", "official_account"),
    "最常使用": ("frequent", "unknown"),
    "功能": ("features", "feature"),
    "聊天记录": ("chat_history", "chat_history"),
    "Recent used": ("recent", "unknown"),
    "Contacts": ("contacts", "contact"),
    "Groups": ("groups", "group"),
    "Service Accounts": ("service_accounts", "service_account"),
    "Official Accounts": ("official_accounts", "official_account"),
    "Most used": ("frequent", "unknown"),
    "Features": ("features", "feature"),
    "Chat History": ("chat_history", "chat_history"),
    "聯絡人": ("contacts", "contact"),
    "群組": ("groups", "group"),
    "服務賬號": ("service_accounts", "service_account"),
    "官方賬號": ("official_accounts", "official_account"),
    "聊天記錄": ("chat_history", "chat_history"),
}
#: 加好友这趟交互里，每一步之间停多久（秒，闭区间，实际在区间内均匀取）。
#:
#: ★ 2026-08-27 真机弹了安全验证。节拍那层已经补了（`guard.WRITE_PACING_BUCKETS`：
#: 每 300 秒才碰一次好友），这里补的是**单趟交互的形状**：原来整趟 6~7 秒、方差为零——
#: 号码整串糊进搜索框、名片一出来立刻点添加、验证语和备注也是整串粘贴、点击之间零停顿。
#: 真人会打字、会看一眼名片、会想一下写什么，而且每次都不一样。
#:
#: **只加停顿，不改任何判据**：多等一会儿对名片渲染只有好处（`_await_contact_profile`
#: 本来就在轮询等它出来）。加完整趟大约 13~21 秒。
#:
#: 停顿一律取随机区间，不写死。固定 2.0 秒和固定 0.0 秒一样是机器特征，只是慢一点。
_PAUSES: dict[str, tuple[float, float]] = {
    "menu_open": (0.5, 1.4),       # 「快捷操作」点开了，扫一眼菜单再挑那一项
    "panel_open": (0.8, 2.0),      # 「添加朋友」点下去，等面板渲染出来
    "before_enter": (0.6, 1.6),    # 号码打完，按回车之前
    "after_enter": (1.5, 3.5),     # 回车之后等搜索结果（原来是写死的 sleep(1)）
    "read_profile": (1.2, 3.0),    # 名片出来了，看一眼再点「添加到通讯录」
    "verify_open": (0.8, 2.0),     # 验证窗弹出来，看清楚再动手
    "between_fields": (0.5, 1.5),  # 验证语写完到写备注
    "before_confirm": (0.8, 2.0),  # 都填好了，点确定之前
}

#: 朋友圈点赞的停顿档。**另开一张表**：`_PAUSES` 是加好友那一趟的每一步，测试拿它的长度
#: 断言「加好友每步都停了」（`test_add_friend_humanized`），混进来就把那条断言打歪。
#: 真人刷朋友圈是「翻一条、看一眼、偶尔点个赞」，每一步都不等长。
_MOMENTS_PAUSES: dict[str, tuple[float, float]] = {
    "moments_open": (2.0, 4.0),    # 朋友圈窗口出来了，等它刷新、扫一眼
    "moments_scroll": (0.8, 2.4),  # 翻到下一条，看看是谁发的
    "before_like": (0.6, 1.6),     # 省略号点开了，看清是「赞」还是「取消」再动
    "after_like": (1.5, 4.5),      # 点完赞，看一会儿再翻
    "before_comment": (0.6, 1.6),  # 省略号点开了，找「评论」
    "before_typing": (0.8, 2.0),   # 输入框出来了，想一下说什么
    "before_send": (1.0, 2.6),     # 打完了再看一眼
    "after_comment": (3.0, 8.0),   # 评完一条，多停会儿——评论比点赞重得多
}


def _squash(text: str) -> str:
    """认动态用的归一：去掉所有空白。列表项文本里换行/空格的位置和读回来的正文对不上。"""
    return re.sub(r"\s+", "", str(text or ""))


def _same_chat_text(left: str, text: str) -> bool:
    """输入框里读回来的字是不是要发的那条。换行在框里读回来可能变成 \r 或空格，只比去空白后的正文。"""
    return _squash(left) == _squash(text)


def _parse_post_text(text: str, when: str, timestamp: Any, images_pattern: Any) -> dict[str, Any]:
    """把时间线一项的文本拆成 ``{content, posted_at, photos, videos, key}``。

    正文 = 去掉「包含 N 张图片」「视频」和时间戳之后的部分（和 vendor ``dump_recent_posts`` 同一套抠法，
    所以 AI 端拿它去认动态对得上）。``key`` 是去空白的正文；纯图/纯视频动态没正文，
    键取去掉时间戳后的整段（时间戳会变，绝不能进键）。
    """
    photos = 0
    found = images_pattern.search(text)
    if found:
        try:
            photos = int(found.group(1))
        except (TypeError, ValueError):
            photos = 0
    videos = 1 if re.search(r"\s(视频|Video|影片)\s", text) else 0
    content = images_pattern.sub(" ", text)
    content = re.sub(r"\s(视频|Video|影片)\s", " ", content)
    content = timestamp.sub(" ", content)
    content = re.sub(r"\s+", " ", content).strip()
    key = _squash(content) or _squash(timestamp.sub("", text))
    return {"content": content, "posted_at": when, "photos": photos, "videos": videos, "key": key}


def _normalize_comment_requests(comments: Any, cap: int) -> list[dict[str, str]]:
    """把上游给的评论清单整理成 ``[{content, text, key}]``：空的丢、重复正文只留第一条、超过 ``cap`` 截断。

    正文太短（去空白后不足 2 个字）也丢：拿一个字去认动态几乎必然认错。
    """
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in comments if isinstance(comments, list) else []:
        if not isinstance(raw, dict):
            continue
        content = str(raw.get("content") or "").strip()
        text = str(raw.get("text") or "").strip()
        key = _squash(content)
        if len(key) < 2 or not text or key in seen:
            continue
        seen.add(key)
        out.append({"content": content, "text": text, "key": key})
        if len(out) >= cap:
            break
    return out

#: 逐字输入时每个键之间停多久（秒）。人打字大约每分钟 60~200 个字符。
_KEYSTROKE_PAUSE = (0.05, 0.18)


def _human_pause(what: str) -> None:
    """按 `_PAUSES` / `_MOMENTS_PAUSES` 里那一档随机停一会儿。表里没有的名字不停（不静默乱猜一个值）。"""
    span = _PAUSES.get(what) or _MOMENTS_PAUSES.get(what)
    if span is None:
        return
    time.sleep(random.uniform(*span))


def _type_like_human(edit: Any, text: str) -> None:
    """把 `text` 逐字敲进去，键与键之间随机停顿。

    只用于**号码/微信号**这类纯 ASCII 的短串：`type_keys` 会把 ``{}()+^%~`` 当按键语法，
    拿它敲用户写的验证语会把内容改掉。验证语和备注仍走 `set_text`（人也会粘贴），
    靠前后的停顿拉开节奏。

    老 pywinauto 的 `type_keys` 不一定认 `pause=`，认不出就退回整串输入——
    输不进去比慢一点严重得多，这一层不该有能让加好友失败的新失败点。
    """
    try:
        edit.type_keys(text, with_spaces=True, pause=random.uniform(*_KEYSTROKE_PAUSE))
    except TypeError:
        edit.type_keys(text, with_spaces=True)


class _AddFriendNotStarted(Exception):
    """加好友流程还没点到任何按钮就走不下去了——可以安全退回 vendor 实现重来一次。"""


#: 搜一个陌生号是**一次网络查找**，慢的时候名片好几秒才渲染出来。等不到就当查无此号，
#: 是把「慢」误判成「没有」——等久一点只是这条指令多花几秒，判错却让整条计划项记成失败。
_PROFILE_WAIT_SECONDS = 12.0

#: 名片那层 Group 在 vendor 的表里是**跟微信版本走的**（见 `_await_contact_profile` 的注释）。
#: 已知的两个都试一遍，别赌当前这台跑的是表里覆盖到的那个版本。
_PROFILE_GROUP_CLASSES = ("mmui::ContactProfileView", "mmui::ProfileView")

#: 添加朋友面板**自己**的标题/提示文案。退化到拿整个面板当名片根时，别把它们当成名字。
_ADD_FRIEND_PANE_CHROME = (
    "添加朋友", "新增朋友", "add friends", "add contacts",
    "搜索微信号/手机号", "搜尋微信id 或者手機號碼", "search by id/mobile",
    "我的微信号", "我的微信號", "my wechat id",
    "添加朋友面板",
)

#: 名片上这些是**字段行**不是昵称。前缀匹配，冒号写法（中英文冒号）都算。
_PROFILE_LABEL_PREFIXES = (
    "微信号", "微信號", "wechat id", "weixin id",
    "地区", "地區", "region",
    "来源", "來源", "source",
    "备注", "備註", "remark",
    "标签", "標籤", "tag",
    "个性签名", "個性簽名", "signature", "whats up",
    "朋友权限", "朋友權限",
    "手机号", "手機號", "phone",
    "昵称", "暱稱", "nickname",
)

#: 名片上的按钮/操作文案，别把它们当成名字。
_PROFILE_BUTTON_TEXTS = frozenset({
    "添加到通讯录", "新增到通訊錄", "add to contacts",
    "发消息", "發消息", "send message", "message",
    "搜索", "搜尋", "search", "取消", "cancel", "确定", "確定", "ok",
    "更多", "more", "朋友圈", "moments",
    # 退化到拿整个添加朋友面板当名片根时会多读到这些行（见 `_contact_profile_roots`）。
    *(text.casefold() for text in _ADD_FRIEND_PANE_CHROME),
})


def _looks_like_number(text: str) -> bool:
    """一串数字/加号/横线 —— 手机号那行，不是名字。"""
    stripped = text.replace("+", "").replace("-", "").replace(" ", "")
    return bool(stripped) and stripped.isdigit()


def _pick_profile_nickname(lines: list[str]) -> str | None:
    """只认**明写的**「昵称：X」。读不到就是读不到。

    为什么不拿卡片顶上那行顶替：已经是好友时那行显示的是**备注**（HR 自己起的名字，
    比如「张三-仓库工」）。把它当昵称报回业务端，候选人档案里的 `wechatName` 就成了
    我们自己写的那个名字——一个看起来完全正常、但永远对不上对方微信的值。
    """
    for raw in lines:
        line = str(raw or "").strip()
        folded = line.casefold()
        label = next((p for p in ("昵称", "暱稱", "nickname") if folded.startswith(p)), None)
        if label is None:
            continue
        value = line[len(label):].lstrip("：: ").strip()
        if value:
            return value
    return None


def _pick_profile_display_name(lines: list[str], number: str) -> str | None:
    """卡片顶上那行 —— 这台微信里这个人**显示成什么**（有备注就是备注，没有就是昵称）。

    这正是会话列表里的那个名字，也就是 ``send_message(to=...)`` 要逐字用的会话名。
    """
    for raw in lines:
        line = str(raw or "").strip()
        if not line or line == number or _looks_like_number(line):
            continue
        if line.casefold() in _PROFILE_BUTTON_TEXTS:
            continue
        folded = line.casefold()
        if any(folded.startswith(p) for p in _PROFILE_LABEL_PREFIXES):
            continue
        if number and number in line:
            continue
        return line
    return None


def _pick_profile_name(lines: list[str], number: str) -> str | None:
    """从加好友名片的文本行里挑出昵称。纯函数，好测——真机上这张卡片没法在 CI 里模拟。

    卡片长这样（顺序不保证，字段有无也不保证）::

        张三
        微信号：wxid_abc123
        地区：广东 深圳
        [添加到通讯录]

    规则：带字段名的行里，只有「昵称：X」直接取 X；其余字段行、按钮文案、
    搜的那个号码本身、纯数字行，全部排掉；剩下的第一行就是昵称。
    """
    fallback: str | None = None
    for raw in lines:
        line = str(raw or "").strip()
        if not line or line == number or _looks_like_number(line):
            continue
        if line.casefold() in _PROFILE_BUTTON_TEXTS:
            continue
        folded = line.casefold()
        label = next((p for p in _PROFILE_LABEL_PREFIXES if folded.startswith(p)), None)
        if label is not None:
            # 「昵称：张三」这种是明写的，最可信，直接用
            if label in {"昵称", "暱稱", "nickname"}:
                value = line[len(label):].lstrip("：: ").strip()
                if value:
                    return value
            continue
        if number and number in line:
            continue  # 「手机号 138…」这类没带字段名、但含号码的行
        if fallback is None:
            fallback = line
    return fallback


#: 「新的朋友」分区展开后每条申请的 ListItem class。4.1.13 起是 `ContactsCellNewFriendView`（2026-09-17 真机 dump），
#: 更早版本是 `XTableCell`——vendor 只认后者，4.1.13 上整趟拿到空列表。新版本再换名字就往这张表里加。
_NEW_FRIEND_CELL_CLASSES = ("mmui::ContactsCellNewFriendView", "mmui::XTableCell")
#: 申请详情面板上「通过」那颗按钮的标题。vendor 表里是「前往验证」（老版本）；4.1.13 待处理条目的标题
#: 还没在真机上见过（dump 那天没有待处理申请），几种可能的写法都认。**只认标题**：我方发出去的申请
#: 同一个位置是「等待对方验证」，靠 class 认会把它也点了。
_VERIFY_BUTTON_TITLES = ("前往验证", "通过验证", "接受", "同意", "Verify Now", "Accept", "前往驗證")
#: 已处理过的条目在列表文字末尾带的状态；这些条目右侧没有「通过」按钮，不点进去。
_NEW_FRIEND_SETTLED_SUFFIXES = ("已添加", "已过期", "已通过", "已拒绝", "已拉黑")
#: 右侧申请详情面板 / 面板顶上的昵称（4.1.13 dump：`mmui::DetailView` auto_id=FMessageDetailView，昵称是 `TextViewCanCopy`）。
_NEW_FRIEND_DETAIL_ROOT = {"control_type": "Group", "auto_id": "FMessageDetailView"}
_NEW_FRIEND_NAME_TEXT = {"control_type": "Text", "auto_id": "TextViewCanCopy"}
#: 点了「通过」之后弹出的「通过朋友验证」确认框的标题（vendor 三种语言 + 可能的改名）。class 带 `VerifyFriend` 也算，
#: 两条判据任一命中就是它——vendor 要求标题和 class 同时对上，4.1.12.26 上 5 秒没等到（2026-09-17 现场）。
_VERIFY_CONFIRM_TITLES = ("通过朋友验证", "通过好友验证", "通過朋友驗證", "Confirm Friend Request", "朋友验证")
#: 确认框里那颗按钮的标题（vendor 的 ConfirmButton 先试，再试这些）。
_CONFIRM_BUTTON_TITLES = ("确定", "完成", "通过", "确认", "添加", "发送", "確定", "確認", "通過", "OK", "Done", "Confirm")
#: 点击之后可能同时冒出来、但绝不是确认框的微信顶层窗口（气泡提示、主窗口本身）。
#: （XPopover 不排除：确认框是不是它没人说得准，反正只在里面找得到「确定」才会点。）
_NOT_A_CONFIRM_WINDOW = frozenset({"mmui::MainWindow", "mmui::XTooltip", "mmui::ToastWindow"})
#: 独立确认框里兜底选按钮时**绝不**点的标题——点「取消」会把框关掉、申请原样留着，却被记成 accepted（2026-09-18 真机）。
_CANCEL_BUTTON_TITLES = frozenset({"取消", "关闭", "關閉", "Cancel", "Close", "修改标签", "修改標籤", "Edit Tags"})
#: 详情面板上「已经是好友」的正向证据：「通过」按钮没了、换成这些，说明这一下一点就通过了、没有确认框。
_ACCEPTED_BUTTON_TITLES = ("发消息", "Send Message", "發消息", "發訊息")
#: 一趟最多通过几个（vendor 的口径：单次 ≤8）。
_NEW_FRIENDS_LIMIT_MAX = 8


def _new_friend_entries(contact_list: Any) -> list[tuple[Any, str]]:
    """通讯录列表里现在露出来的申请条目。列表是虚拟化的（`RecyclerListView`），只有露出来的才在树上。"""
    entries: list[tuple[Any, str]] = []
    for cell in _safe_call(lambda: contact_list.children()) or []:
        class_name = str(_safe_call(getattr(cell, "class_name", None)) or "")
        if class_name not in _NEW_FRIEND_CELL_CLASSES:
            continue
        text = str(_safe_call(getattr(cell, "window_text", None)) or "").strip()
        entries.append((cell, text))
    return entries


def _strip_new_friend_status(text: str) -> str:
    """列表那一行拼的是「昵称 + 验证语 + 状态」；读不到详情面板的昵称时，尽量剥出前面那段。"""
    cleaned = str(text or "").strip()
    for suffix in _NEW_FRIEND_SETTLED_SUFFIXES:
        cleaned = cleaned.removesuffix(suffix)
    head, sep, _tail = cleaned.partition("我: ")
    if sep:
        cleaned = head
    return cleaned.strip()[:30]


def _button_titles(pane: Any, limit: int = 12) -> str:
    """面板上有哪些按钮（标题）。「通过」按钮没认出来时留这一行，下次一眼看出是不是标题换了。"""
    titles: list[str] = []
    for control in (_safe_call(lambda: pane.descendants(control_type="Button")) or [])[:limit]:
        title = str(_safe_call(getattr(control, "window_text", None)) or "").strip()[:20]
        if title:
            titles.append(title)
    return "; ".join(titles) or "(没有带标题的按钮)"


def _has_button(pane: Any, titles: tuple[str, ...]) -> bool:
    return any(_find_child(pane, {"title": title, "control_type": "Button"}) is not None for title in titles)


def _find_child(pane: Any, criteria: dict[str, Any], timeout: float = 0.3) -> Any | None:
    """在 ``pane`` 里按判据找一个控件；``pane`` 是 WindowSpecification（有 ``child_window``）还是 UIAWrapper
    （``Desktop().windows()`` 给的那种，**只有** ``descendants``）都行。找不到给 ``None``，不抛。
    ★ 两种形状必须都认：wrapper 上调 ``child_window`` 是 AttributeError，被 ``_safe_call`` 吞掉后看起来就像「没找到」
    （2026-09-18 真机，[[wechat-auto-accept-friend-110]]）。
    """
    if callable(getattr(pane, "child_window", None)):
        spec = _safe_call(lambda: pane.child_window(**criteria))
        if spec is not None and _truthy_safe_call(lambda: spec.exists(timeout=timeout)):
            return spec
        return None
    found = _safe_call(lambda: pane.descendants(**criteria)) or []
    return found[0] if found else None


def _looks_like_verify_confirm_window(window: Any) -> bool:
    class_name = str(_safe_call(getattr(window, "class_name", None)) or "")
    title = str(_safe_call(getattr(window, "window_text", None)) or "").strip()
    lowered = class_name.lower()
    return "verifyfriend" in lowered or "friendverify" in lowered or title in _VERIFY_CONFIRM_TITLES


# 最近一次「非缓存兜底枚举」的真实异常（pywinauto 带缓存那条路把它吞了）。放弃时随失败信息一起记进日志。
_LAST_ENUM_ERROR: str | None = None


def _record_enum_error(err: str) -> None:
    global _LAST_ENUM_ERROR
    _LAST_ENUM_ERROR = err


def _take_enum_error() -> str:
    """取出并清掉最近一次枚举异常，只在真的记了错时返回一段文字。"""
    global _LAST_ENUM_ERROR
    err, _LAST_ENUM_ERROR = _LAST_ENUM_ERROR, None
    return f"｜枚举异常：{err}" if err else ""


def _win32_modules() -> tuple[Any, Any, Any] | None:
    """``(win32gui, UIAElementInfo 模块, UIAWrapper 模块)`` 或 ``None``（非 Windows / 没装 pywin32 或 pywinauto）。
    单独抽出来是为了测试能强制走「拿不到模块」的降级分支——真机上装了才走真路。"""
    try:
        import win32gui  # 惰性：Linux 测试没有 pywin32
        info_mod = __import__("pywinauto.uia_element_info", fromlist=["UIAElementInfo"])
        wrap_mod = __import__("pywinauto.controls.uiawrapper", fromlist=["UIAWrapper"])
    except Exception:  # noqa: BLE001 - 非 Windows / 没装 pywin32 或 pywinauto
        return None
    return win32gui, info_mod, wrap_mod


def _pywinauto_top_windows(visible_only: bool = True) -> list[Any]:
    """pywinauto 带缓存的整桌面根枚举 `Desktop().windows()`；没装 / 抛错返回 ``[]``。
    抽出来是为了测试能强制它返回空、逼出 Win32 兜底路（真机有桌面时它本来就返回真窗口）。"""
    with suppress(Exception):
        Desktop = __import__("pywinauto", fromlist=["Desktop"]).Desktop
        return list(Desktop(backend="uia").windows(visible_only=visible_only))
    return []


def _win32_top_windows(visible_only: bool = True) -> tuple[list[Any], str | None]:
    """用 Win32 ``EnumWindows`` 枚举顶层窗口句柄，再用**元素级** UIA（ElementFromHandle）逐个包成 pywinauto 包装器。

    返回 ``(窗口列表, 异常文字或 None)``。

    为什么这样：**打包后的冻结客户端**里，pywinauto 的整桌面根枚举 `Desktop().windows()` 会返回空
    （2026-09-20 实机坐实：同一台机、同一个桌面会话，标准 Python 里 `Desktop(uia).windows()` 返回 10 个顶层窗口，
    冻结客户端却返回 0；而 `child_window` 这种**元素级** UIA 在客户端里照常能用——所以客户端点完「前往验证」能读到详情
    面板按钮，却「一个顶层窗口都没枚举到」）。跟机器、线程、STA/MTA 套间都无关，是 comtypes 打进 exe 后跑 UIAutomation
    **根枚举**的行为问题。Win32 `EnumWindows` 是纯 ctypes、不经 comtypes/UIA typelib，冻结与否表现一致；拿到 HWND 再走
    元素级 UIA（`UIAElementInfo(hwnd)` → `UIAWrapper`，客户端里能用的那条路）就能得到和 `Desktop().windows()` 同形状的
    包装器。pywin32 / pywinauto 不在（Linux 测试）→ ``([], None)``。[[wechat-auto-accept-friend-110]]
    """
    mods = _win32_modules()
    if mods is None:  # 非 Windows / 没装 pywin32 或 pywinauto（Linux 测试）
        return [], None
    win32gui, info_mod, wrap_mod = mods
    handles: list[int] = []
    try:
        def _collect(hwnd: int, _: Any) -> bool:
            if not visible_only or win32gui.IsWindowVisible(hwnd):
                handles.append(hwnd)
            return True
        win32gui.EnumWindows(_collect, None)
    except Exception as exc:  # noqa: BLE001 - EnumWindows 本身几乎不会失败，真失败要留证
        return [], f"EnumWindows {type(exc).__name__}: {str(exc)[:160]}"
    windows: list[Any] = []
    for hwnd in handles:
        # 逐个 ElementFromHandle + 包装；单个失败（窗口刚消失 / COM 偶发）跳过，不拖垮整张列表。
        wrapped = _safe_call(lambda h=hwnd: wrap_mod.UIAWrapper(info_mod.UIAElementInfo(h)))
        if wrapped is not None:
            windows.append(wrapped)
    return windows, None


def _window_handle(window: Any) -> Any:
    """pywinauto 包装器上 ``handle`` 是属性不是方法，读它本身就可能抛（窗口已经没了）。"""
    handle = _safe_call(lambda: window.handle)
    return handle if isinstance(handle, int) else None


def _window_handles(windows: list[Any]) -> set[Any]:
    return {handle for handle in (_window_handle(window) for window in windows) if handle is not None}


def _window_pid(window: Any) -> int | None:
    """窗口所属进程 PID；拿不到（非 Windows / 窗口已消失 / 没装 pywin32）返回 ``None``。

    确认框判据里用它认「这是不是微信自己弹的窗口」——比认 class 前缀稳：4.1.13 的原生确认框 class 带 `mmui::`，
    4.1.12.26 是 Qt 构建、确认框 class 是 `Qt….QWindowIcon` 一类、窗口标题还可能是空的，只有「同属微信进程」这条
    判据两种构建都成立（[[wechat-auto-accept-friend-110]]）。
    """
    handle = _window_handle(window)
    if handle is None:
        return None
    try:
        win32process = __import__("win32process")
    except Exception:  # noqa: BLE001 - Linux 测试 / 没装 pywin32
        return None
    pid = _safe_call(lambda: win32process.GetWindowThreadProcessId(handle)[1])
    return pid if isinstance(pid, int) else None


def _window_inventory(windows: list[Any], limit: int = 12) -> str:
    """桌面上此刻有哪些顶层窗口（class|标题）。确认框没等到时留这一行，下次一眼看出它换了名还是根本没弹。"""
    parts: list[str] = []
    for window in windows[:limit]:
        class_name = str(_safe_call(getattr(window, "class_name", None)) or "")
        title = str(_safe_call(getattr(window, "window_text", None)) or "").strip()[:20]
        if class_name or title:
            parts.append(f"{class_name}|{title}")
    return "; ".join(parts) or "(一个顶层窗口都没枚举到)"


def _pane_inventory(pane: Any, limit: int = 25) -> str:
    """放弃时把面板上到底有什么留成一行。

    控件树各版本长得不一样，真机上又没法复现——出问题时这一行就是全部线索。
    只截前 ``limit`` 个、每个标题只留 20 字：这是本机日志，不是取证转储。
    """
    parts: list[str] = []
    for control in (_safe_call(lambda: pane.descendants()) or [])[:limit]:
        class_name = str(_safe_call(getattr(control, "class_name", None)) or "")
        title = str(_safe_call(getattr(control, "window_text", None)) or "").strip()[:20]
        if class_name or title:
            parts.append(f"{class_name}|{title}")
    return "; ".join(parts) or "(什么都没读到)"


def _profile_text_lines(pane: Any) -> list[str]:
    """把名片上所有文本抓成行。控件树长什么样各版本不一样，所以宁可抓全了再挑。"""
    lines: list[str] = []
    controls = [pane]
    controls.extend(_safe_call(lambda: pane.descendants(control_type="Text")) or [])
    for control in controls:
        text = _safe_call(getattr(control, "window_text", None))
        for line in str(text or "").splitlines():
            stripped = line.strip()
            if stripped and stripped not in lines:
                lines.append(stripped)
    return lines


def _collect_contact_search_matches(
    items: list[Any],
    query: str,
    limit: int,
) -> list[dict[str, Any]]:
    query_key = query.casefold()
    current_section = "unknown"
    current_type = "unknown"
    matches: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for item in items:
        item_class_name = str(_safe_call(getattr(item, "class_name", None)) or "")
        raw_text = str(_safe_call(getattr(item, "window_text", None)) or "").strip()
        texts = [raw_text] if raw_text else []
        try:
            descendants = item.descendants(control_type="Text")
        except Exception:
            descendants = []
        for descendant in descendants:
            value = str(
                _safe_call(getattr(descendant, "window_text", None)) or ""
            ).strip()
            if value and value not in texts:
                texts.append(value)
        if not texts:
            continue

        section = _SEARCH_RESULT_SECTIONS.get(raw_text) if len(texts) == 1 else None
        if section is not None:
            current_section, current_type = section
            continue
        if current_section == "chat_history":
            continue
        if any(
            text.startswith(("网络查找手机/QQ号", "Search mobile/QQ ID", "網路搜尋手機/QQ"))
            for text in texts
        ):
            continue
        if item_class_name != "mmui::SearchContentCellView" and not (
            item_class_name == "mmui::XTableCell" and current_type == "feature"
        ):
            continue
        lines = [
            line.strip()
            for text in texts
            for line in text.splitlines()
            if line.strip()
        ]
        matching_lines = [line for line in lines if query_key in line.casefold()]
        if not matching_lines:
            continue
        exact_lines = [line for line in matching_lines if line.casefold() == query_key]
        name = exact_lines[0] if exact_lines else matching_lines[0]
        dedupe_key = (name.casefold(), current_type)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        matches.append(
            {
                "name": name,
                "type": current_type,
                "section": current_section,
                "exact": bool(exact_lines),
            }
        )
        if len(matches) >= limit:
            break
    return matches


def _safe_call(func: Any) -> Any:
    if func is None:
        return None
    try:
        return func()
    except Exception:
        return None


def _native_control_text(control: Any) -> str:
    text = _safe_call(control.window_text)
    if text:
        return str(text).strip()
    return str(_safe_call(lambda: control.element_info.name) or "").strip()


def _truthy_safe_call(func: Any) -> bool:
    try:
        return bool(func())
    except Exception:
        return False


def _resolve_control_by_criteria(window: Any, criteria: dict[str, Any]) -> Any | None:
    child_window = getattr(window, "child_window", None)
    if child_window is not None:
        try:
            candidate = child_window(**criteria)
            if _control_available(candidate):
                return candidate
        except Exception:
            pass

    try:
        descendants = window.descendants()
    except Exception:
        return None

    matches = [node for node in descendants if _control_matches_criteria(node, criteria)]
    if not matches:
        return None
    found_index = _safe_int(criteria.get("found_index")) if "found_index" in criteria else 0
    if found_index < 0 or found_index >= len(matches):
        return None
    candidate = matches[found_index]
    return candidate if _control_available(candidate) else None


def _control_available(control: Any | None) -> bool:
    if control is None:
        return False
    exists = getattr(control, "exists", None)
    if exists is not None:
        try:
            if not exists(timeout=0.2):
                return False
        except TypeError:
            try:
                if not exists():
                    return False
            except Exception:
                return False
        except Exception:
            return False
    is_visible = getattr(control, "is_visible", None)
    if is_visible is not None and not _truthy_safe_call(is_visible):
        return False
    is_enabled = getattr(control, "is_enabled", None)
    if is_enabled is not None and not _truthy_safe_call(is_enabled):
        return False
    return True


def _control_matches_criteria(node: Any, criteria: dict[str, Any]) -> bool:
    for key, expected in criteria.items():
        if key in {"found_index", "top_level_only", "enabled_only", "visible_only"}:
            continue
        if key == "control_type":
            friendly = _safe_call(getattr(node, "friendly_class_name", None))
            element_type = _element_info_value(node, "control_type")
            actual_values = {str(value or "") for value in (friendly, element_type)}
            expected_type = str(expected or "")
            aliases = {
                "List": {"List", "ListBox"},
                "Text": {"Text", "Static"},
            }
            if expected_type not in actual_values and actual_values.isdisjoint(
                aliases.get(expected_type, {expected_type})
            ):
                return False
            continue
        elif key == "class_name":
            actual = _safe_call(getattr(node, "class_name", None))
        elif key in {"auto_id", "automation_id"}:
            actual = _element_info_value(node, "automation_id")
        elif key == "framework_id":
            actual = _element_info_value(node, "framework_id")
        elif key == "title":
            actual = _safe_call(getattr(node, "window_text", None))
        elif key == "title_re":
            actual = _safe_call(getattr(node, "window_text", None)) or ""
            try:
                if re.search(str(expected), str(actual)) is None:
                    return False
            except re.error:
                return False
            continue
        else:
            continue
        if str(actual or "") != str(expected or ""):
            return False
    return True


def _element_info_value(node: Any, name: str) -> Any:
    try:
        return getattr(node.element_info, name)
    except Exception:
        return None


def _session_name_from_text(text: str) -> str | None:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if not lines:
        return None
    first = lines[0]
    if first.startswith("[") and len(lines) > 1:
        return lines[1] or None
    return first or None


def _session_name_from_automation_id(automation_id: str) -> str | None:
    value = str(automation_id or "").strip()
    if not value.startswith("session_item_"):
        return None
    name = value.removeprefix("session_item_").strip()
    return name or None


def _session_preview_from_text(text: str) -> str | None:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if len(lines) < 2:
        return None
    return lines[1] or None


def _session_timestamp_from_text(text: str) -> str | None:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if len(lines) < 2:
        return None
    for line in reversed(lines):
        if _session_timestamp_is_today(line) is not None:
            return line
    return None


def _session_timestamp_is_today(text: str) -> bool | None:
    value = " ".join(str(text or "").strip().split())
    if not value:
        return None
    if re.fullmatch(r"\d{1,2}:\d{2}", value):
        return True
    lowered = value.lower()
    if "昨天" in value or "yesterday" in lowered:
        return False

    today = date.today()
    chinese_date = re.search(r"(?:(?P<year>\d{4})年)?(?P<month>\d{1,2})月(?P<day>\d{1,2})日", value)
    if chinese_date:
        try:
            year = int(chinese_date.group("year") or today.year)
            month = int(chinese_date.group("month"))
            day = int(chinese_date.group("day"))
            return date(year, month, day) == today
        except ValueError:
            return None

    slash_date = re.search(r"(?:(?P<year>\d{4})/)?(?P<month>\d{1,2})/(?P<day>\d{1,2})", value)
    if slash_date:
        try:
            year = int(slash_date.group("year") or today.year)
            month = int(slash_date.group("month"))
            day = int(slash_date.group("day"))
            return date(year, month, day) == today
        except ValueError:
            return None

    dash_date = re.search(r"(?P<year>\d{4})-(?P<month>\d{1,2})-(?P<day>\d{1,2})", value)
    if dash_date:
        try:
            return (
                date(
                    int(dash_date.group("year")),
                    int(dash_date.group("month")),
                    int(dash_date.group("day")),
                )
                == today
            )
        except ValueError:
            return None

    return None


def _session_preview_is_self_message(preview: Any, message_text: str) -> bool:
    preview_text = str(preview or "").strip()
    message = str(message_text or "").strip()
    if not preview_text or not message:
        return False
    self_prefixes = (
        "\u4f60:",  # 你:
        "\u4f60\uff1a",  # 你：
        "\u6211:",  # 我:
        "\u6211\uff1a",  # 我：
        "You:",
        "Me:",
    )
    for prefix in self_prefixes:
        if preview_text.startswith(prefix) and preview_text[len(prefix) :].strip() == message:
            return True
    return False


def _message_needs_sender_annotation(message: dict[str, Any]) -> bool:
    if message.get("is_self") is not None:
        return False
    if message.get("is_timestamp"):
        return False
    if not str(message.get("text") or "").strip():
        return False
    class_name = str(message.get("class_name") or "")
    return class_name.startswith("mmui::Chat")


def _window_has_group_count_label(window: Any) -> bool:
    title = str(_safe_call(getattr(window, "window_text", None)) or "").strip()
    if re.search(r"\(\d+\)$", title):
        return True
    try:
        descendants = window.descendants()
    except Exception:
        return False
    for node in descendants:
        auto_id = str(_element_info_value(node, "automation_id") or "")
        text = str(_safe_call(getattr(node, "window_text", None)) or "").strip()
        if auto_id.endswith("current_chat_count_label") and re.fullmatch(r"\(\d+\)", text):
            return True
        if re.fullmatch(r".+\(\d+\)", text) and "current_chat_name_label" not in auto_id:
            return True
    return False


def _is_generic_chat_window_title(title: str) -> bool:
    normalized = str(title or "").strip().lower()
    return normalized in {"微信", "wechat", "weixin"}


def _apply_group_sender_from_selected_text(messages: list[dict[str, Any]]) -> None:
    for message in messages:
        if message.get("sender"):
            continue
        if message.get("is_self") is True or message.get("is_timestamp"):
            continue
        raw_text = str(message.get("raw_text") or message.get("text") or "").strip()
        sender, content = _split_selected_group_message_text(raw_text)
        if not sender:
            continue
        message["sender"] = sender
        if content:
            message["text"] = content


def _split_selected_group_message_text(text: str) -> tuple[str | None, str | None]:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if len(lines) < 2:
        return None, None
    sender = lines[0]
    content = "\n".join(lines[1:]).strip()
    if not sender or not content:
        return None, None
    if _is_timestamp_row("", sender):
        return None, None
    return sender, content


def _annotate_message_side_from_screenshot(
    messages: list[dict[str, Any]],
    container_rect: Any,
) -> None:
    region = _screenshot_region_from_rect(container_rect)
    if region is None:
        return

    try:
        import pyautogui

        screenshot = pyautogui.screenshot(region=region)
    except Exception:
        return

    try:
        image_width, image_height = screenshot.size
        container_top = int(container_rect.top)
    except Exception:
        return

    if image_width <= 0 or image_height <= 0:
        return

    for message in messages:
        if message.get("is_self") is not None or message.get("is_timestamp"):
            continue
        top = _safe_int(message.get("top"))
        bottom = _safe_int(message.get("bottom"))
        if top < 0 or bottom < 0 or bottom <= top:
            continue

        row_top = max(0, top - container_top - 2)
        row_bottom = min(image_height, bottom - container_top + 2)
        if row_bottom - row_top < 4:
            continue

        try:
            row_image = screenshot.crop((0, row_top, image_width, row_bottom))
        except Exception:
            continue

        is_self = _infer_message_is_self_from_row_image(row_image)
        if is_self is None:
            continue
        message["is_self"] = is_self
        if is_self:
            message["sender"] = "\u6211"


def _screenshot_region_from_rect(rect: Any) -> tuple[int, int, int, int] | None:
    if rect is None:
        return None
    try:
        left = int(rect.left)
        top = int(rect.top)
        width = int(rect.width())
        height = int(rect.height())
    except Exception:
        return None
    if width <= 0 or height <= 0:
        return None
    return (left, top, width, height)


def _infer_message_is_self_from_row_image(row_image: Any) -> bool | None:
    try:
        image = row_image.convert("RGB")
        width, height = image.size
    except Exception:
        return None
    if width <= 20 or height <= 4:
        return None

    background = _dominant_row_background(image)
    if background is None:
        return None

    sample_step = 2
    active_columns = [0] * width
    changed_pixels = 0
    for y in range(0, height, sample_step):
        for x in range(2, max(2, width - 2), sample_step):
            if _rgb_distance(image.getpixel((x, y)), background) <= 45:
                continue
            active_columns[x] += 1
            changed_pixels += 1

    if changed_pixels < max(8, (width * height) // 500):
        return None

    min_column_hits = max(1, height // 28)
    xs = [x for x, count in enumerate(active_columns) if count >= min_column_hits]
    if not xs:
        xs = [x for x, count in enumerate(active_columns) if count]
    if not xs:
        return None

    left = min(xs)
    right = max(xs)
    if right - left < max(8, width * 0.025):
        return None

    center_ratio = ((left + right) / 2) / width
    if center_ratio >= 0.56:
        return True
    if center_ratio <= 0.44:
        return False
    return None


def _dominant_row_background(image: Any) -> tuple[int, int, int] | None:
    from collections import Counter

    try:
        width, height = image.size
    except Exception:
        return None
    if width <= 0 or height <= 0:
        return None

    counter: Counter[tuple[int, int, int]] = Counter()
    sample_step = 2
    for y in range(0, height, sample_step):
        for x in range(0, width, sample_step):
            r, g, b = image.getpixel((x, y))
            counter[(r // 8, g // 8, b // 8)] += 1
    if not counter:
        return None

    bucket = counter.most_common(1)[0][0]
    return tuple(channel * 8 + 4 for channel in bucket)


def _rgb_distance(first: tuple[int, int, int], second: tuple[int, int, int]) -> int:
    return sum(abs(first[index] - second[index]) for index in range(3))


def _message_inside_container(message: dict[str, Any], container_rect: Any) -> bool:
    if container_rect is None:
        return True
    top = message.get("top")
    bottom = message.get("bottom")
    if top is None or bottom is None:
        return True
    try:
        return int(top) >= int(container_rect.top) and int(bottom) <= int(container_rect.bottom)
    except Exception:
        return True


def _safe_runtime_id(node: Any) -> str | None:
    try:
        value = node.element_info.runtime_id
    except Exception:
        return None
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return ".".join(str(item) for item in value)
    return str(value)


def _matching_list_item_for_message(chat_list: Any, message: dict[str, Any]) -> Any | None:
    try:
        items = chat_list.children(control_type="ListItem")
    except Exception:
        return None
    if not items:
        return None

    runtime_id = str(message.get("runtime_id") or "").strip()
    if runtime_id:
        for item in items:
            if _safe_runtime_id(item) == runtime_id:
                return item

    text = str(message.get("text") or "").strip()
    order = _safe_int(message.get("order"))
    if 0 <= order < len(items):
        candidate = items[order]
        candidate_text = str(_safe_call(candidate.window_text) or "").strip()
        if not text or candidate_text == text:
            return candidate

    if text:
        matches = [
            item
            for item in items
            if str(_safe_call(item.window_text) or "").strip() == text
        ]
        if matches:
            return matches[-1]
    return None


def _infer_message_is_self(rect: Any, container_rect: Any) -> bool | None:
    if rect is None or container_rect is None:
        return None
    try:
        width = float(rect.width())
        container_width = float(container_rect.width())
        if container_width <= 0 or width >= container_width * 0.98:
            return None
        left_gap = float(rect.left - container_rect.left)
        right_gap = float(container_rect.right - rect.right)
        if left_gap < 0 or right_gap < 0:
            return None
        gap_delta = abs(left_gap - right_gap)
        if gap_delta >= max(container_width * 0.08, 24.0):
            return left_gap > right_gap

        center_ratio = float(rect.mid_point().x - container_rect.left) / container_width
        if center_ratio >= 0.58:
            return True
        if center_ratio <= 0.42:
            return False
        return None
    except Exception:
        return None


def _rect_metrics(rect: Any) -> dict[str, Any]:
    if rect is None:
        return {
            "left": None,
            "right": None,
            "top": None,
            "bottom": None,
            "mid_x": None,
            "mid_y": None,
        }
    try:
        midpoint = rect.mid_point()
        return {
            "left": int(rect.left),
            "right": int(rect.right),
            "top": int(rect.top),
            "bottom": int(rect.bottom),
            "mid_x": int(midpoint.x),
            "mid_y": int(midpoint.y),
        }
    except Exception:
        return {
            "left": None,
            "right": None,
            "top": None,
            "bottom": None,
            "mid_x": None,
            "mid_y": None,
        }


def _sort_message_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not any(row.get("bottom") is not None for row in rows):
        return rows
    return sorted(
        rows,
        key=lambda row: (
            row.get("bottom") is None,
            int(row.get("bottom") or 0),
            int(row.get("top") or 0),
            int(row.get("order") or 0),
        ),
    )


def _attach_message_timestamps(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    last_timestamp_text: str | None = None
    last_timestamp_is_today: bool | None = None
    for row in rows:
        if row.get("is_timestamp"):
            last_timestamp_text = str(row.get("text") or "")
            last_timestamp_is_today = _timestamp_is_today(last_timestamp_text)
            row["timestamp_text"] = last_timestamp_text
            row["timestamp_is_today"] = last_timestamp_is_today
            continue
        row["timestamp_text"] = last_timestamp_text
        row["timestamp_is_today"] = last_timestamp_is_today
    return rows


def _last_replyable_message(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    for row in reversed(rows):
        if row.get("is_timestamp"):
            continue
        if str(row.get("class_name") or "") == "mmui::ChatItemView":
            continue
        if str(row.get("text") or "").strip():
            return row
    return None


def _last_visible_activity(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    for row in reversed(rows):
        if row.get("is_timestamp"):
            continue
        if str(row.get("text") or "").strip():
            return row
    return None


def _is_self_activity_row(row: dict[str, Any]) -> bool:
    text = str(row.get("text") or "").strip()
    if not text:
        return False
    self_prefixes = (
        "\u4f60\u64a4\u56de\u4e86",  # 你撤回了
        "\u4f60\u62cd\u4e86\u62cd",  # 你拍了拍
    )
    if text.startswith(self_prefixes):
        return True
    return "\u91cd\u65b0\u7f16\u8f91" in text  # 重新编辑


VOICE_ITEM_CLASS = "mmui::ChatVoiceItemView"

#: 图片 / 文件 / 动画表情 / 视频气泡在 UIA 树里的名字（vendor `Special_Labels`：图片='图片'、文件='文件'、视频='视频'）。
#: 中英繁都认；方括号是微信在会话预览里的写法，气泡名字上通常没有，但认上不吃亏。
_IMAGE_LABELS = frozenset({"图片", "[图片]", "圖片", "[圖片]", "Image", "[Image]", "Photo", "[Photo]"})
_EMOJI_LABELS = frozenset({"动画表情", "[动画表情]", "動畫表情", "[動畫表情]", "Sticker", "[Sticker]", "表情", "[表情]"})
_VIDEO_LABELS = frozenset({"视频", "[视频]", "影片", "[影片]", "Video", "[Video]"})
_FILE_PREFIXES = ("文件", "[文件]", "檔案", "[檔案]", "File", "[File]")
#: 文件气泡名字里的文件名：`xxx.pdf` / `报名表.docx`（扩展名 1–8 位字母数字）。
#: 名字中间可以有空格（2026-09-29 真机：「李先洋-分拣员个人简历 (2).docx」原来只认出「(2).docx」）；
#: 按行找，所以大小那行（「7.2K」）不会粘进来；扩展名要以字母打头，免得把「7.2K」当成文件名。
_FILE_NAME_RE = re.compile(r"([^\[\]:：/\\\r\n]*?[^\s\[\]:：/\\]\.[A-Za-z][A-Za-z0-9]{0,7})(?![A-Za-z0-9.])")


def _file_name_in(body: str) -> str:
    """文件气泡的 UIA 名字里取文件名：先去掉打头的「文件」标签，再逐行找第一个像文件名的。"""
    rest = body
    for prefix in sorted(_FILE_PREFIXES, key=len, reverse=True):
        if rest.startswith(prefix):
            rest = rest[len(prefix):]
            break
    for line in rest.splitlines():
        match = _FILE_NAME_RE.search(line.strip())
        if match:
            return match.group(1).strip()
    return ""


#: 另存一条气泡的六小步（日志里「另存进度 k/6」）。
_SAVE_STEPS = ("打开会话", "找到这条消息", "右键点「另存为」", "保存对话框弹出", "填好保存路径", "文件落到本机")
_SAVE_STEP_TOTAL = len(_SAVE_STEPS)


class _SaveProgress:
    """另存走到第几步。每完成一步打一行；失败时 ``next_label`` 就是卡住的那一步。"""

    def __init__(self, friend: str) -> None:
        self.friend = friend
        self.step = 0

    @property
    def next_label(self) -> str:
        return _SAVE_STEPS[self.step] if self.step < _SAVE_STEP_TOTAL else "收尾"

    def done(self, detail: str = "") -> None:
        label = self.next_label
        self.step += 1
        logger.info("chat.attachment.save.step", friend=self.friend, step=self.step,
                    total=_SAVE_STEP_TOTAL, stage=label, detail=detail)


def _annotate_attachment_row(class_name: Any, text: str) -> dict[str, Any] | None:
    """图片 / 文件 / 动画表情 / 视频气泡 → 附加 ``message_type``（+ ``file_name``）；其余返回 None（104）。

    只按 UIA 名字和类名判，**不碰控件**。文字气泡恰好只写了「图片」两个字的情况认不出来，
    代价是把它当成一张图去另存一次然后失败——比把真图片当文字丢掉轻得多。
    """
    name = str(class_name or "")
    body = str(text or "").strip()
    if not body and not name:
        return None
    if "Emoticon" in name or "Sticker" in name or body in _EMOJI_LABELS:
        return {"message_type": "emoji"}
    if body in _VIDEO_LABELS or "Video" in name:
        return {"message_type": "video"}
    if body in _IMAGE_LABELS or "Image" in name:
        return {"message_type": "image"}
    if body.startswith(_FILE_PREFIXES) or "File" in name:
        return {"message_type": "file", "file_name": _file_name_in(body)}
    return None


#: 语音气泡的 UIA 名称：微信把「语音 + 时长」放前面，开了「聊天中的语音消息自动转成文字」后
#: 转写正文跟在后面。vendor `pyweixin/utils.py` 的 ``Audio_pattern``（`(?<=语音)\d+"秒(.*)$`）
#: 就是按这个形状写的；这里放宽了空格/引号/单位，中英繁都认。
_VOICE_TEXT_RE = re.compile(
    r'^\s*(?P<label>(?:语音|語音|Audio|Voice)\s*\d+\s*["″”\']?\s*(?:秒|sec|s)?)\s*[:：]?\s*(?P<body>.*)$',
    re.S,
)


def _annotate_voice_row(class_name: Any, text: str) -> dict[str, Any] | None:
    """语音气泡 → 附加字段；不是语音气泡返回 None。

    - ``voice_label``：``语音12"秒`` 这段——它在转写前后**都不变**，游标就签它
      （`wechat_io.message_signature`）。同一条消息的 ``text`` 会从空变成正文，签 text 就会
      「游标已滚出读取窗口」。
    - ``text``：转写正文；还没出来就是空串，并标 ``transcript_pending``。
    只按控件类名判定，不按文本猜——一条恰好以「语音」开头的文字消息不该被当成语音。
    """
    if str(class_name or "") != VOICE_ITEM_CLASS:
        return None
    match = _VOICE_TEXT_RE.match(text or "")
    if match:
        label = " ".join(match.group("label").split())
        body = match.group("body").strip()
    else:
        label = (text or "").strip() or "语音"
        body = ""
    return {
        "message_type": "voice",
        "voice_label": label,
        "text": body,
        "transcript_pending": not body,
    }


def _is_timestamp_row(class_name: Any, text: str) -> bool:
    if str(class_name or "") != "mmui::ChatItemView":
        return False
    return _timestamp_is_today(text) is not None


def _timestamp_is_today(text: str) -> bool | None:
    value = " ".join(str(text or "").strip().split())
    if not value:
        return None
    if re.fullmatch(r"\d{1,2}:\d{2}", value):
        return True
    lowered = value.lower()
    if "昨天" in value or "yesterday" in lowered:
        return False

    today = date.today()
    chinese_date = re.search(
        r"(?:(?P<year>\d{4})年)?(?P<month>\d{1,2})月(?P<day>\d{1,2})日",
        value,
    )
    if chinese_date:
        try:
            year = int(chinese_date.group("year") or today.year)
            month = int(chinese_date.group("month"))
            day = int(chinese_date.group("day"))
            return date(year, month, day) == today
        except ValueError:
            return None

    slash_date = re.search(
        r"(?:(?P<year>\d{4})/)?(?P<month>\d{1,2})/(?P<day>\d{1,2})",
        value,
    )
    if slash_date:
        try:
            year = int(slash_date.group("year") or today.year)
            month = int(slash_date.group("month"))
            day = int(slash_date.group("day"))
            return date(year, month, day) == today
        except ValueError:
            return None

    dash_date = re.search(
        r"(?P<year>\d{4})-(?P<month>\d{1,2})-(?P<day>\d{1,2})",
        value,
    )
    if dash_date:
        try:
            return (
                date(
                    int(dash_date.group("year")),
                    int(dash_date.group("month")),
                    int(dash_date.group("day")),
                )
                == today
            )
        except ValueError:
            return None

    weekday = _timestamp_weekday(value)
    if weekday is not None and re.search(r"\d{1,2}:\d{2}", value):
        return weekday == today.weekday()
    return None


def _timestamp_weekday(text: str) -> int | None:
    chinese = {
        "一": 0,
        "二": 1,
        "三": 2,
        "四": 3,
        "五": 4,
        "六": 5,
        "日": 6,
        "天": 6,
    }
    match = re.search(r"星期([一二三四五六日天])", text)
    if match:
        return chinese.get(match.group(1))
    english = {
        "monday": 0,
        "tuesday": 1,
        "wednesday": 2,
        "thursday": 3,
        "friday": 4,
        "saturday": 5,
        "sunday": 6,
    }
    lowered = text.lower()
    for label, index in english.items():
        if label in lowered:
            return index
    return None


def _message_key(item: dict[str, Any]) -> tuple[str, str]:
    return (str(item.get("class_name") or ""), str(item.get("text") or ""))


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _tail_messages(messages: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    if count <= 0:
        return []
    return messages[-count:]


def _looks_like_chat_message_control(class_name: Any, control_type: Any) -> bool:
    name = str(class_name or "")
    kind = str(control_type or "")
    if kind not in {"ListItem", "CheckBox"}:
        return False
    if not name.startswith("mmui::Chat"):
        return False
    if "Session" in name or "Contact" in name:
        return False
    return "ItemView" in name or "Bubble" in name


def _parse_duration(value: str) -> float:
    text = value.strip().lower()
    if text.endswith("min"):
        return float(text[:-3]) * 60
    if text.endswith("ms"):
        return float(text[:-2]) / 1000
    if text.endswith("s"):
        return float(text[:-1])
    if text.endswith("h"):
        return float(text[:-1]) * 3600
    return float(text)
