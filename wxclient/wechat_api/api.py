from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog

from .errors import WeChatActionUnsupported, WeChatApiUnavailableError
from .omni import OmniVisualDriver
from .uia import UiaWeChatDriver

logger = structlog.get_logger(__name__)


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

    def search_contacts(self, query: str, limit: int = 10) -> dict[str, Any]:
        return self._run_driver("search_contacts", query=query, limit=limit)

    def get_contact_profile(self, friend: str) -> dict[str, Any]:
        return self._run_driver("get_contact_profile", friend=friend)

    def list_group_members(self, group: str) -> dict[str, Any]:
        return self._run_driver("list_group_members", group=group)

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

    def send_files(
        self,
        friend: str,
        files: list[str],
        text: str = "",
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        return self._run_driver(
            "send_files",
            friend=friend,
            files=files,
            text=text,
            dry_run=dry_run,
        )

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

    def read_recent_moments(
        self,
        limit: int = 5,
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        return self._run_driver(
            "read_recent_moments",
            limit=limit,
            dry_run=dry_run,
        )

    def send_current_chat_message(
        self,
        text: str,
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        return self._run_driver("send_current_chat_message", text=text, dry_run=dry_run)

    def check_unread_messages(self, dry_run: bool | None = None) -> dict[str, Any]:
        return self._run_driver("check_unread_messages", dry_run=dry_run)

    def get_unread_messages(self) -> dict[str, Any]:
        return self._run_driver("get_unread_messages")

    def read_chat_sessions(self, dry_run: bool | None = None) -> dict[str, Any]:
        return self._run_driver("read_chat_sessions", dry_run=dry_run)

    def read_chat_history(self, friend: str, limit: int = 50) -> dict[str, Any]:
        return self._run_driver("read_chat_history", friend=friend, limit=limit)

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
