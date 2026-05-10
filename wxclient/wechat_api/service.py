from __future__ import annotations

import re
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

from wxclient.uia_unlock import ensure_wechat_uia


class WeChatApiError(RuntimeError):
    pass


class WeChatApi:
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
        FriendSettings = self._pyweixin_class("FriendSettings")
        FriendSettings.add_new_friend(
            number=number,
            greetings=greetings,
            remark=remark,
            chat_only=False,
            is_maximize=False,
            close_weixin=False,
        )
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
            raise WeChatApiError(f"好友检测失败：{exc}") from exc
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

    def scan_messages(
        self,
        friend: str,
        duration: str = "30s",
        select_sender: bool = False,
        save_file: bool = False,
        save_media: bool = False,
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        if self._is_dry_run(dry_run):
            return {
                "dry_run": True,
                "action": "scan_messages",
                "friend": friend,
                "duration": duration,
                "select_sender": select_sender,
                "events": [],
            }
        self._ensure_ready()
        Navigator = self._pyweixin_class("Navigator")
        dialog_window = Navigator.open_seperate_dialog_window(
            friend=friend,
            select=select_sender,
            window_minimize=True,
            close_weixin=False,
        )
        result = self._poll_visible_messages(
            dialog_window=dialog_window,
            duration=duration,
        )
        try:
            dialog_window.close()
        except Exception:
            pass
        return {
            "dry_run": False,
            "action": "scan_messages",
            "friend": friend,
            "duration": duration,
            "save_file": save_file,
            "save_media": save_media,
            "result": result,
        }

    def read_visible_messages(
        self,
        friend: str,
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        if self._is_dry_run(dry_run):
            return {
                "dry_run": True,
                "action": "read_visible_messages",
                "friend": friend,
                "messages": [],
            }
        self._ensure_ready()
        Navigator = self._pyweixin_class("Navigator")
        dialog_window = Navigator.open_seperate_dialog_window(
            friend=friend,
            select=False,
            window_minimize=False,
            close_weixin=False,
        )
        messages = self._collect_visible_message_nodes(dialog_window)
        try:
            dialog_window.close()
        except Exception:
            pass
        return {
            "dry_run": False,
            "action": "read_visible_messages",
            "friend": friend,
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
        if not ensure_wechat_uia(
            restart_if_needed=False,
            wechat_path=self.wechat_path,
        ):
            raise WeChatApiError("WeChat UIA is not available")

    def _pyweixin_class(self, name: str) -> Any:
        vendor = self.project_root / "third_party" / "pywechat"
        if str(vendor) not in sys.path:
            sys.path.insert(0, str(vendor))
        try:
            module = __import__("pyweixin", fromlist=[name, "GlobalConfig"])
        except ImportError as exc:
            raise WeChatApiError(f"pyweixin is unavailable: {exc}") from exc

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
            raise WeChatApiError(f"pyweixin.{name} is unavailable: {exc}") from exc

    def _current_main_window(self) -> Any:
        try:
            Desktop = __import__("pywinauto", fromlist=["Desktop"]).Desktop
            desktop = Desktop(backend="uia")
            candidates = []
            for window in desktop.windows():
                if _safe_call(window.class_name) == "mmui::MainWindow":
                    candidates.append(window)
            if not candidates:
                raise WeChatApiError("没有找到微信主窗口")
            scored = sorted(
                candidates,
                key=lambda item: self._current_window_score(item),
                reverse=True,
            )
            return scored[0]
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
            return None
        return None

    def _selected_session_name(self, window: Any) -> str | None:
        return self._selected_session_info(window).get("name")

    def _selected_session_info(self, window: Any) -> dict[str, Any]:
        try:
            uielements = self._pyweixin_submodule("Uielements")
        except WeChatApiError:
            return {}
        main_window = getattr(uielements, "Main_window", None)
        criteria = getattr(main_window, "SessionList", None)
        items = []
        if criteria:
            try:
                session_list = _resolve_control_by_criteria(window, criteria)
                if session_list is not None:
                    items = session_list.children(control_type="ListItem")
            except Exception:
                items = []
        if not items:
            try:
                items = [
                    item
                    for item in window.descendants()
                    if _safe_call(item.class_name) == "mmui::ChatSessionCell"
                ]
            except Exception:
                items = []

        selected = []
        for item in items:
            if _truthy_safe_call(getattr(item, "is_selected", None)):
                selected.append(item)
                continue
            if _truthy_safe_call(getattr(item, "has_keyboard_focus", None)):
                selected.append(item)
        for item in selected or items[:1]:
            text = _safe_call(item.window_text) or ""
            name = _session_name_from_text(text)
            if name:
                return {
                    "name": name,
                    "text": text,
                    "preview": _session_preview_from_text(text),
                    "is_selected": bool(selected),
                }
        return {}

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
                chat_window = Navigator.open_dialog_window(
                    friend=chat,
                    select=False,
                    is_maximize=False,
                    search_pages=5,
                )
                visible_messages = self._collect_visible_message_nodes(chat_window)
            except Exception as exc:
                errors_by_chat[chat] = str(exc)
                continue
            messages_by_chat[chat] = _tail_messages(visible_messages, count)
        return messages_by_chat, errors_by_chat

    def _poll_visible_messages(self, dialog_window: Any, duration: str) -> dict[str, Any]:
        seconds = _parse_duration(duration)
        baseline = self._collect_visible_message_nodes(dialog_window)
        seen = {_message_key(item) for item in baseline}
        events: list[dict[str, Any]] = []
        end_time = time.monotonic() + seconds

        while time.monotonic() < end_time:
            current = self._collect_visible_message_nodes(dialog_window)
            for item in current:
                key = _message_key(item)
                if key in seen:
                    continue
                seen.add(key)
                events.append(item)
            time.sleep(0.5)

        return {
            "new_message_count": len(events),
            "messages": events,
            "initial_visible_count": len(baseline),
        }

    def _collect_visible_message_nodes(self, dialog_window: Any) -> list[dict[str, Any]]:
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
                    "runtime_id": runtime_id,
                    "is_self": _infer_message_is_self(rect, container_rect),
                    "is_timestamp": _is_timestamp_row(class_name, text),
                    "order": order,
                    **metrics,
                }
            )
        return _attach_message_timestamps(_sort_message_rows(rows))

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


def _safe_call(func: Any) -> Any:
    if func is None:
        return None
    try:
        return func()
    except Exception:
        return None


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
            actual = _safe_call(getattr(node, "friendly_class_name", None)) or _element_info_value(node, "control_type")
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


def _session_preview_from_text(text: str) -> str | None:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if len(lines) < 2:
        return None
    return lines[1] or None


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


def _infer_message_is_self(rect: Any, container_rect: Any) -> bool | None:
    if rect is None or container_rect is None:
        return None
    try:
        width = float(rect.width())
        container_width = float(container_rect.width())
        if container_width <= 0 or width >= container_width * 0.9:
            return None
        return rect.mid_point().x > container_rect.mid_point().x
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
