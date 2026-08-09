from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import structlog

from .errors import (
    WeChatActionUnsupported,
    WeChatApiError,
    WeChatApiUnavailableError,
)

logger = structlog.get_logger(__name__)


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
