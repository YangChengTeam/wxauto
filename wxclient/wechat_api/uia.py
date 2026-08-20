from __future__ import annotations

import re
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

import structlog

from wxclient.uia_probe import probe_wechat_uia
from wxclient.uia_probe.constants import (
    DEFAULT_UIA_PROBE_MAX_DEPTH,
    DEFAULT_UIA_PROBE_MAX_NODES,
)

from .errors import WeChatApiError, WeChatApiUnavailableError

logger = structlog.get_logger(__name__)


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

    def add_friend(
        self,
        number: str,
        greetings: str,
        remark: str | None = None,
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        """按手机号/微信号加好友，并把**后续拿什么名字回消息**一并带回来。

        返回里三个名字各有各的用处，别混：
          · ``name``         —— 申请时对方名片上的**昵称**。会变、会重名、可能带 emoji，
                                只适合给人看，**不要拿它当回复的锚**。
          · ``remark``       —— 我们自己设的备注。对方通过后，会话列表里显示的就是它。
          · ``session_name`` —— 后续 ``send_message(to=...)`` 该用的名字 = 备注优先，
                                没设备注才退回昵称。这是唯一该被记下来的那个。

        ⚠️ 加好友是**异步**的：这里返回成功只代表申请发出去了，对方没通过就没有会话、
        也发不了消息。真正"可以回了"的信号是 ``check_new_friends`` / 对方通过。
        """
        if self._is_dry_run(dry_run):
            return {
                "dry_run": True, "action": "add_friend", "number": number,
                "name": None, "remark": remark, "session_name": remark,
            }
        self._ensure_ready()
        logger.info("friend.add.start", number=number, greetings=greetings[:20])
        name: str | None = None
        try:
            name = self._add_friend_reading_profile(number, greetings, remark)
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
        session_name = remark or name
        logger.info("friend.add.done", number=number, name=name, session_name=session_name)
        return {
            "dry_run": False, "action": "add_friend", "number": number,
            "name": name, "remark": remark, "session_name": session_name,
        }

    def _add_friend_reading_profile(
        self, number: str, greetings: str, remark: str | None
    ) -> str | None:
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
            add_friend_pane, _main_window = Navigator.open_add_friend_panel(is_maximize=False)
        except Exception as exc:
            raise _AddFriendNotStarted(f"打不开添加朋友面板：{exc}") from exc

        try:
            try:
                search_edit = add_friend_pane.child_window(control_type="Edit")
                search_edit.set_text("")
                search_edit.type_keys(str(number), with_spaces=True)
                search_edit.type_keys("{ENTER}")
            except Exception as exc:
                raise _AddFriendNotStarted(f"搜索框用不了：{exc}") from exc
            time.sleep(1)

            profile = add_friend_pane.child_window(**Uielements.Groups.ContactProfileViewGroup)
            if not _truthy_safe_call(lambda: profile.exists(timeout=2)):
                # 原来这种情况是**静默返回成功**的：查无此号也报"加好友完成"，
                # AI 端只会以为对方没通过而一直等。说清楚是查不到。
                raise WeChatApiError(f"没搜到这个号：{number}（号码不对，或对方不允许被搜到）")

            name = _pick_profile_name(_profile_text_lines(profile), str(number))

            add_button = profile.child_window(**Uielements.Buttons.AddToContactsButton)
            if not _truthy_safe_call(lambda: add_button.exists(timeout=2)):
                raise WeChatApiError(f"这个号加不了：{number}（多半已经是好友了）")

            add_button.click_input()          # ← 从这里开始就动真格了，不再退回 vendor
            verify_window = Tools.move_window_to_center(Window=Uielements.Windows.VerifyFriendWindow)
            request_edit = verify_window.child_window(control_type="Edit", found_index=0)
            remark_edit = verify_window.child_window(control_type="Edit", found_index=1)
            if greetings:
                request_edit.set_text(greetings)
            if remark:
                remark_edit.set_text(remark)
            verify_window.child_window(**Uielements.Buttons.ConfirmButton).click_input()
            return name
        finally:
            _safe_call(getattr(add_friend_pane, "close", None))

    def _add_friend_via_vendor(self, number: str, greetings: str, remark: str | None) -> None:
        """原来那条路：vendor 全包，但什么名字都不给。只在退化时用。"""
        FriendSettings = self._pyweixin_class("FriendSettings")
        try:
            FriendSettings.add_new_friend(
                number=number,
                greetings=greetings,
                remark=remark,
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
        Contacts = self._pyweixin_class("Contacts")
        try:
            items = Contacts.check_new_friends(
                verify=verify,
                limit=limit,
                clear=False,
                is_maximize=False,
                close_weixin=False,
                return_to_chat=True,
            )
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
                samples=names,
                auto_accept=verify,
            )
        return {
            "dry_run": False,
            "action": "check_new_friends",
            "verify": verify,
            "items": items,
        }

    def send_message(
        self,
        friend: str,
        text: str,
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        if self._is_dry_run(dry_run):
            return {"dry_run": True, "action": "send_message", "friend": friend}
        self._ensure_ready()
        Messages = self._pyweixin_class("Messages")
        Messages.send_messages_to_friend(
            friend=friend,
            messages=[text],
            search_pages=5,
            clear=True,
            send_delay=0.2,
            is_maximize=False,
            close_weixin=False,
        )
        # ★ 发后核对（075，需求文档 §12.4）：文字没发走会**留在输入框里**，
        # 而底座照样返回成功——067 的文件路径上真机踩过一模一样的坑
        # （文件躺在输入框里、回执却报 done）。宁可报失败，也不谎报「已发送」：
        # AI 会照着回执告诉用户「已经发过去了」。
        #
        # 判据只看**文字**，不看子元素：空输入框天生就带子元素（067 的教训），
        # 但它的 window_text() 一定是空的——所以这里不需要跟基线较劲。
        leftover = self._chat_input_text_len(friend)
        verified = leftover >= 0
        if leftover > 0:
            self._nudge_send(friend)
            leftover = self._chat_input_text_len(friend)
            if leftover > 0:
                logger.warning("message.send.stuck_in_input", friend=friend, left=leftover)
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
            Files.send_files_to_friend(
                friend=normalized_friend,
                files=normalized_files,
                with_messages=bool(text),
                messages=[text] if text else [],
                messages_first=True,
                clear=True,
                send_delay=send_delay,
                is_maximize=False,
                close_weixin=False,
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

    def _chat_input_text_len(self, friend: str) -> int:
        """输入框里还剩多少**文字**。``-1`` = 看不出来（判不了，不当作失败）。

        只数文字是刻意的：空输入框本来就可能带子元素（067 真机教训），拿子元素个数
        判「发没发出去」要跟空态基线比才准；而文字不用——发走了就是空的。
        """
        try:
            edit_area = self._chat_input_area(friend)
            if edit_area is None:
                return -1
            left = len(str(edit_area.window_text() or "").strip())
            logger.info("message.send.input_state", text=left)
            return left
        except Exception as exc:  # noqa: BLE001 —— 探测失败只是没法判定，不改变发送结论
            logger.warning("message.send.input_probe_failed", error=str(exc))
            return -1

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

    def _post_moments_impl(self, text: str, medias: list[str]) -> None:
        """用 pyweixin 底层 UIA 操作发朋友圈。

        流程：
        1. 点击主窗口工具栏头像 → 弹出 profile 面板
        2. 在 profile 面板点「朋友圈」→ 打开朋友圈窗口
        3. 如果进入的不是主页面（看不到发表按钮），点返回
        4. 右键发表按钮 → 有图片选图片模式，没图片选纯文字
        5. 填入文字 → 点击发表
        """
        import pyautogui
        from pywinauto import Desktop, mouse

        Navigator = self._pyweixin_class("Navigator")
        Tools = self._pyweixin_class("Tools")
        Uielements = self._pyweixin_submodule("Uielements")

        Buttons = Uielements.Buttons
        Edits = Uielements.Edits
        Groups = Uielements.Groups
        Independent_window = Uielements.Independent_window
        Main_window = Uielements.Main_window
        Windows = Uielements.Windows

        desktop = Desktop(backend="uia")
        valid_paths = self._normalize_moments_media_paths(medias) if medias else []

        moments_window = self._find_existing_moments_window(desktop, Independent_window)
        if moments_window is None:
            # 1. 获取微信主窗口，点击工具栏头像
            main_window = Navigator.open_weixin(is_maximize=False)
            toolbar = main_window.child_window(**Main_window.Toolbar)

            rect = toolbar.rectangle()
            mouse.click(coords=(rect.mid_point().x, rect.top + 52))
            time.sleep(1)

            # 2. 在弹出的 profile 面板中点「朋友圈」
            profile_pane = self._wait_profile_pane(desktop, Windows)
            try:
                moments_btn = profile_pane.child_window(**Buttons.MomentsButton)
                if moments_btn.exists(timeout=2):
                    moments_btn.click_input()
                else:
                    moments_btn = profile_pane.child_window(
                        control_type="Button",
                        class_name="mmui::ProfileDetailValueSnsView",
                    )
                    moments_btn.click_input()
            except Exception:
                moments_btn = profile_pane.child_window(
                    control_type="Button",
                    class_name="mmui::ProfileDetailValueSnsView",
                )
                moments_btn.click_input()
            time.sleep(1.5)

            # 3. 定位朋友圈窗口
            moments_window = Tools.move_window_to_center(Independent_window.MomentsWindow)

        # 如果看不到发表按钮，先点返回
        post_button = moments_window.child_window(**Buttons.PostButton)
        if not post_button.exists(timeout=2):
            back_button = moments_window.child_window(**Buttons.BackButton)
            if back_button.exists(timeout=1):
                back_button.click_input()
                time.sleep(1)
                moments_window = Tools.move_window_to_center(Independent_window.MomentsWindow)
            post_button = moments_window.child_window(**Buttons.PostButton)

        if not post_button.exists(timeout=2):
            raise WeChatApiError("无法找到朋友圈发表按钮")

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

    def _find_existing_moments_window(self, desktop: Any, Independent_window: Any) -> Any | None:
        try:
            window = desktop.window(**Independent_window.MomentsWindow)
            if window.exists(timeout=0.5):
                return window
        except Exception:
            return None
        return None

    def _wait_profile_pane(self, desktop: Any, Windows: Any) -> Any:
        selectors = [
            Windows.PopUpProfileWindow,
            {
                "control_type": "Window",
                "class_name": "mmui::ProfileUniquePop",
                "top_level_only": False,
            },
        ]
        for selector in selectors:
            try:
                window = desktop.window(**selector)
                if window.exists(timeout=2):
                    return window
            except Exception:
                continue
        raise WeChatApiError("WeChat profile pane did not appear")

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

    def _choose_native_files(self, native_window: Any, paths: list[str], pyautogui: Any) -> None:
        dialog_value = self._build_native_file_dialog_value(paths)
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

    def _collect_history_pages(
        self, window: Any, chat_name: str, safe_limit: int
    ) -> list[dict[str, Any]]:
        """在给定窗口里往上翻页收集消息。主窗口和独立聊天窗口的消息列表控件是一样的。"""
        chat_list = self._resolve_chat_list(window)
        if chat_list is None:
            raise WeChatApiError("未找到聊天消息列表")
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
        self._restore_if_minimized(probe.get("selected_window"))

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
            app = Application(backend="uia").connect(title="微信")
            main_window = app.window(title="微信")
            main_window.wait("ready", timeout=5)
            return main_window
        except Exception as exc:
            raise WeChatApiError(f"无法定位当前微信主窗口：{exc}") from exc

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
            if not text:
                continue
            is_message_class = class_name in message_classes
            is_message_control = _looks_like_chat_message_control(class_name, control_type)
            if not is_message_class and not is_message_control:
                continue
            runtime_id = _safe_runtime_id(node)
            key = (str(runtime_id), text) if runtime_id else (str(class_name), text)
            if key in seen:
                continue
            seen.add(key)
            rect = _safe_call(getattr(node, "rectangle", None))
            metrics = _rect_metrics(rect)
            rows.append(
                {
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
            )
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
class _AddFriendNotStarted(Exception):
    """加好友流程还没点到任何按钮就走不下去了——可以安全退回 vendor 实现重来一次。"""


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
})


def _looks_like_number(text: str) -> bool:
    """一串数字/加号/横线 —— 手机号那行，不是名字。"""
    stripped = text.replace("+", "").replace("-", "").replace(" ", "")
    return bool(stripped) and stripped.isdigit()


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
