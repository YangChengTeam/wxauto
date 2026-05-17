from __future__ import annotations

import re
import sys
import time
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import structlog

from wxclient.uia_unlock import ensure_wechat_uia

logger = structlog.get_logger(__name__)


class WeChatApiError(RuntimeError):
    pass


class WeChatApiUnavailableError(WeChatApiError):
    pass


class WeChatActionUnsupported(WeChatApiError):
    pass


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
        if self._is_dry_run(dry_run):
            return {"dry_run": True, "action": "add_friend", "number": number}
        self._ensure_ready()
        logger.info("friend.add.start", number=number, greetings=greetings[:20])
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
        logger.info("friend.add.done", number=number)
        return {"dry_run": False, "action": "add_friend", "number": number}

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
        return {"dry_run": False, "action": "send_message", "friend": friend}

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
        def _log_uia_unlock(message: str) -> None:
            logger.info("uia.operation_unlock", message=message)

        if not ensure_wechat_uia(
            restart_if_needed=False,
            wechat_path=self.wechat_path,
            log=_log_uia_unlock,
        ):
            raise WeChatApiUnavailableError("WeChat UIA is not available")

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


class _SelfUiaDriver:
    driver_name = "uia"

    def __init__(self, api: UiaWeChatDriver) -> None:
        self._api = api

    def __getattr__(self, name: str) -> Any:
        attr = getattr(UiaWeChatDriver, name, None)
        if attr is None or name.startswith("_"):
            raise AttributeError(name)
        if not callable(attr):
            return attr

        def _call(*args: Any, **kwargs: Any) -> Any:
            return attr(self._api, *args, **kwargs)

        return _call


class OmniVisualDriver:
    driver_name = "omni_visual"
    _SUPPORTED_ACTIONS = {"send_message", "open_chat"}

    def __init__(
        self,
        project_root: Path | None = None,
        dry_run: bool = True,
        rpa_config: dict[str, Any] | None = None,
    ) -> None:
        self.project_root = project_root or Path(__file__).resolve().parents[2]
        self.dry_run = dry_run
        self.rpa_config = rpa_config or {}
        self._controller: Any | None = None

    def send_message(
        self,
        friend: str,
        text: str,
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        if self._is_dry_run(dry_run):
            return {"dry_run": True, "action": "send_message", "friend": friend}
        self._ensure_supported("send_message")
        SendTextMessageAction = self._omni_action_class("SendTextMessageAction")
        action = SendTextMessageAction(
            content=text,
            target=friend,
            is_chatroom=False,
        )
        diagnostics = self._execute(action)
        result = {"dry_run": False, "action": "send_message", "friend": friend}
        if diagnostics:
            result["visual_diagnostics"] = diagnostics
        return result

    def open_chat(
        self,
        friend: str,
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        if self._is_dry_run(dry_run):
            return {"dry_run": True, "action": "open_chat", "friend": friend}
        self._ensure_supported("open_chat")
        SwitchConversationAction = self._omni_action_class("SwitchConversationAction")
        action = SwitchConversationAction(target=friend, is_chatroom=False)
        self._execute(action)
        return {"dry_run": False, "action": "open_chat", "friend": friend}

    def _is_dry_run(self, override: bool | None) -> bool:
        return self.dry_run if override is None else override

    def _ensure_supported(self, action_name: str) -> None:
        if action_name not in self._SUPPORTED_ACTIONS:
            raise WeChatActionUnsupported(
                f"Omni visual RPA does not support action: {action_name}"
            )

    def _execute(self, action: Any) -> dict[str, Any] | None:
        controller = self._controller_instance()
        handler = self._handler_for_action(action, controller)
        action_type = getattr(getattr(action, "action_type", None), "value", action)
        if not handler.execute(action):
            diagnostics = self._last_visual_diagnostics(controller)
            detail = f"; diagnostics={diagnostics}" if diagnostics else ""
            raise WeChatApiError(f"Omni visual RPA action failed: {action_type}{detail}")
        if action_type == "send_text_message":
            return self._last_visual_diagnostics(controller)
        return None

    def _last_visual_diagnostics(self, controller: Any) -> dict[str, Any] | None:
        message_sender = getattr(controller, "message_sender", None)
        diagnostics = getattr(message_sender, "last_send_diagnostics", None)
        if isinstance(diagnostics, dict) and diagnostics:
            return diagnostics
        return None

    def _close_visual_blocking_dialogs(self) -> None:
        if self.rpa_config.get("close_blocking_file_dialogs", True) is False:
            return
        try:
            import pyautogui
        except ImportError:
            return
        for title in ("选择文件", "打开", "Open"):
            try:
                windows = pyautogui.getWindowsWithTitle(title)
            except Exception:
                continue
            for window in windows:
                try:
                    window.activate()
                    time.sleep(0.1)
                    pyautogui.press("esc")
                    time.sleep(0.2)
                    if pyautogui.getWindowsWithTitle(title):
                        window.activate()
                        time.sleep(0.1)
                        pyautogui.hotkey("alt", "f4")
                        time.sleep(0.2)
                except Exception:
                    try:
                        window.close()
                    except Exception:
                        pass

    def _omni_action_class(self, name: str) -> Any:
        self._add_omni_vendor_to_path()
        self._check_omni_runtime()
        modules = {
            "SendTextMessageAction": "omni_bot_sdk.rpa.action_handlers.send_text_message_handler",
            "SwitchConversationAction": "omni_bot_sdk.rpa.action_handlers.switch_conversation_handler",
        }
        module_name = modules.get(name)
        if module_name is None:
            raise WeChatActionUnsupported(f"Omni visual RPA action is not wired: {name}")
        try:
            module = __import__(module_name, fromlist=[name])
        except ImportError as exc:
            raise WeChatApiUnavailableError(
                f"Omni visual RPA dependencies are unavailable: {exc}"
            ) from exc
        return getattr(module, name)

    def _controller_instance(self) -> Any:
        if self._controller is not None:
            return self._controller
        vendor_src = self._add_omni_vendor_to_path()
        self._check_omni_runtime()
        model_path = vendor_src / "omni_bot_sdk" / "yolo" / "models" / "msg_rec.pt"
        if not model_path.exists():
            raise WeChatApiUnavailableError(
                f"Omni visual RPA model is missing: {model_path}"
            )
        try:
            from omni_bot_sdk.rpa.image_processor import ImageProcessor
            from omni_bot_sdk.rpa.input_handler import InputHandler
            from omni_bot_sdk.rpa.message_sender import MessageSender
            from omni_bot_sdk.rpa.ocr_processor import OCRProcessor
            from omni_bot_sdk.rpa.ui_helper import UIInteractionHelper
            from omni_bot_sdk.rpa.window_manager import WindowManager
        except ImportError as exc:
            raise WeChatApiUnavailableError(
                f"Omni visual RPA dependencies are unavailable: {exc}"
            ) from exc

        try:
            image_processor = ImageProcessor()
            ocr_processor = OCRProcessor(ocr_config=self.rpa_config.get("ocr", {}))
            image_processor.setup()
            ocr_processor.setup()
            window_manager = WindowManager(
                image_processor=image_processor,
                ocr_processor=ocr_processor,
                rpa_config=self.rpa_config,
            )
            self._close_visual_blocking_dialogs()
            if not window_manager.init_chat_window():
                raise WeChatApiUnavailableError(
                    "Omni visual RPA could not initialize the WeChat chat window"
                )
            controller = SimpleNamespace()
            controller.logger = logger
            controller.db = None
            controller.window_manager = window_manager
            controller.ocr_processor = ocr_processor
            controller.image_processor = image_processor
            controller.input_handler = InputHandler()
            controller.message_sender = MessageSender(window_manager)
            controller.ui_helper = UIInteractionHelper(controller)
            self._controller = controller
        except WeChatApiUnavailableError:
            raise
        except Exception as exc:
            raise WeChatApiUnavailableError(
                f"Omni visual RPA initialization failed: {exc}"
            ) from exc
        return self._controller

    def _handler_for_action(self, action: Any, controller: Any) -> Any:
        action_type = str(getattr(getattr(action, "action_type", None), "value", ""))
        try:
            if action_type == "send_text_message":
                from omni_bot_sdk.rpa.action_handlers.send_text_message_handler import (
                    SendTextMessageHandler,
                )

                return SendTextMessageHandler(controller)
            if action_type == "switch_conversation":
                from omni_bot_sdk.rpa.action_handlers.switch_conversation_handler import (
                    SwitchConversationHandler,
                )

                return SwitchConversationHandler(controller)
        except ImportError as exc:
            raise WeChatApiUnavailableError(
                f"Omni visual RPA dependencies are unavailable: {exc}"
            ) from exc
        raise WeChatActionUnsupported(f"Omni visual RPA action is not wired: {action_type}")

    def _add_omni_vendor_to_path(self) -> Path:
        vendor_src = self._omni_vendor_src()
        if not vendor_src.exists():
            raise WeChatApiUnavailableError(
                f"Omni visual RPA vendor source is missing: {vendor_src}"
            )
        if str(vendor_src) not in sys.path:
            sys.path.insert(0, str(vendor_src))
        return vendor_src

    def _omni_vendor_src(self) -> Path:
        candidates = [
            self.project_root / "third_party" / "omni-bot-sdk-oss" / "src",
        ]
        bundle_root = getattr(sys, "_MEIPASS", None)
        if bundle_root:
            candidates.append(
                Path(bundle_root) / "third_party" / "omni-bot-sdk-oss" / "src"
            )
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return candidates[0]

    @staticmethod
    def _check_omni_runtime() -> None:
        if sys.platform != "win32":
            raise WeChatApiUnavailableError("Omni visual RPA requires Windows")
        if sys.version_info[:2] != (3, 12):
            version = f"{sys.version_info.major}.{sys.version_info.minor}"
            raise WeChatApiUnavailableError(
                f"Omni visual RPA requires Python 3.12; current Python is {version}"
            )


class CompositeWeChatDriver:
    def __init__(self, drivers: list[Any]) -> None:
        self.drivers = drivers

    def run(self, action_name: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        chain: list[dict[str, str]] = []
        fallback_reason: str | None = None
        errors: list[str] = []
        for driver in self.drivers:
            driver_name = str(getattr(driver, "driver_name", driver.__class__.__name__))
            method = getattr(driver, action_name, None)
            if not callable(method):
                reason = f"{driver_name} does not support {action_name}"
                errors.append(reason)
                fallback_reason = fallback_reason or reason
                chain.append({"driver": driver_name, "status": "unsupported", "reason": reason})
                continue
            try:
                result = method(*args, **kwargs)
            except WeChatActionUnsupported as exc:
                reason = str(exc)
                errors.append(f"{driver_name}: {reason}")
                fallback_reason = fallback_reason or reason
                chain.append({"driver": driver_name, "status": "unsupported", "reason": reason})
                continue
            except WeChatApiUnavailableError as exc:
                reason = str(exc)
                errors.append(f"{driver_name}: {reason}")
                fallback_reason = fallback_reason or reason
                chain.append({"driver": driver_name, "status": "unavailable", "reason": reason})
                continue
            if not isinstance(result, dict):
                result = {"result": result}
            chain.append({"driver": driver_name, "status": "succeeded"})
            result.setdefault("driver", driver_name)
            result.setdefault("fallback_chain", chain)
            if fallback_reason:
                result.setdefault("fallback_reason", fallback_reason)
            return result
        detail = "; ".join(errors) if errors else "no drivers configured"
        raise WeChatApiUnavailableError(f"No available WeChat driver for {action_name}: {detail}")


class WeChatApi(UiaWeChatDriver):
    def __init__(
        self,
        project_root: Path | None = None,
        dry_run: bool = True,
        wechat_path: str | Path | None = None,
        wechat_driver_order: list[str] | None = None,
        omni_visual_rpa_config: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            project_root=project_root,
            dry_run=dry_run,
            wechat_path=wechat_path,
        )
        self.wechat_driver_order = _normalize_driver_order(wechat_driver_order)
        self.omni_visual_rpa_config = omni_visual_rpa_config or {}
        self._composite_driver = CompositeWeChatDriver(self._build_drivers())

    def add_friend(
        self,
        number: str,
        greetings: str,
        remark: str | None = None,
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        return self._run_driver(
            "add_friend",
            number=number,
            greetings=greetings,
            remark=remark,
            dry_run=dry_run,
        )

    def check_new_friends(
        self,
        verify: bool = False,
        limit: int = 8,
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        return self._run_driver(
            "check_new_friends",
            verify=verify,
            limit=limit,
            dry_run=dry_run,
        )

    def send_message(
        self,
        friend: str,
        text: str,
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        return self._run_driver("send_message", friend=friend, text=text, dry_run=dry_run)

    def open_chat(
        self,
        friend: str,
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        return self._run_driver("open_chat", friend=friend, dry_run=dry_run)

    def post_moments(
        self,
        text: str = "",
        medias: list[str] | None = None,
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        return self._run_driver("post_moments", text=text, medias=medias, dry_run=dry_run)

    def send_current_chat_message(
        self,
        text: str,
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        return self._run_driver("send_current_chat_message", text=text, dry_run=dry_run)

    def check_unread_messages(self, dry_run: bool | None = None) -> dict[str, Any]:
        return self._run_driver("check_unread_messages", dry_run=dry_run)

    def read_chat_sessions(self, dry_run: bool | None = None) -> dict[str, Any]:
        return self._run_driver("read_chat_sessions", dry_run=dry_run)

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
        return self._run_driver(
            "scan_messages",
            friend=friend,
            duration=duration,
            select_sender=select_sender,
            save_file=save_file,
            save_media=save_media,
            separate_window=separate_window,
            dry_run=dry_run,
        )

    def read_visible_messages(
        self,
        friend: str,
        separate_window: bool = True,
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        return self._run_driver(
            "read_visible_messages",
            friend=friend,
            separate_window=separate_window,
            dry_run=dry_run,
        )

    def read_current_chat(self, dry_run: bool | None = None) -> dict[str, Any]:
        return self._run_driver("read_current_chat", dry_run=dry_run)

    def _run_driver(self, action_name: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._composite_driver.run(action_name, *args, **kwargs)

    def _build_drivers(self) -> list[Any]:
        drivers: list[Any] = []
        for driver_name in self.wechat_driver_order:
            if driver_name == "uia":
                drivers.append(_SelfUiaDriver(self))
            elif driver_name == "omni_visual":
                drivers.append(
                    OmniVisualDriver(
                        project_root=self.project_root,
                        dry_run=self.dry_run,
                        rpa_config=self.omni_visual_rpa_config,
                    )
                )
            else:
                logger.warning("wechat.driver.unknown", driver=driver_name)
        if not drivers:
            drivers.append(_SelfUiaDriver(self))
        return drivers


def _normalize_driver_order(value: list[str] | None) -> list[str]:
    if not value:
        return ["uia"]
    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        driver_name = str(item or "").strip()
        if not driver_name or driver_name in seen:
            continue
        seen.add(driver_name)
        normalized.append(driver_name)
    return normalized or ["uia"]


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


def _session_preview_self_reason(preview: Any, outbound_texts: list[str] | None = None) -> str | None:
    preview_text = str(preview or "").strip()
    if not preview_text:
        return None
    outbound = [str(text or "").strip() for text in outbound_texts or [] if str(text or "").strip()]
    self_prefixes = (
        "\u4f60:",
        "\u4f60\uff1a",
        "\u6211:",
        "\u6211\uff1a",
        "You:",
        "Me:",
    )
    for prefix in self_prefixes:
        if preview_text.startswith(prefix):
            tail = preview_text[len(prefix) :].strip()
            if not outbound or not tail or tail in outbound or preview_text in outbound:
                return f"preview_prefix:{prefix}"
    if outbound and preview_text in outbound:
        return "preview_matches_outbound"
    return None


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
