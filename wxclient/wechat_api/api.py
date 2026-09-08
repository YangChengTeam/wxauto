from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog

from .errors import WeChatActionUnsupported, WeChatApiUnavailableError
from .uia import UiaWeChatDriver

logger = structlog.get_logger(__name__)


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


class WeChatApi:
    def __init__(
        self,
        project_root: Path | None = None,
        dry_run: bool = True,
        wechat_path: str | Path | None = None,
        wechat_driver_order: list[str] | None = None,
    ) -> None:
        self._uia_driver = UiaWeChatDriver(
            project_root=project_root,
            dry_run=dry_run,
            wechat_path=wechat_path,
        )
        self.project_root = self._uia_driver.project_root
        self.dry_run = self._uia_driver.dry_run
        self.wechat_driver_order = _normalize_driver_order(wechat_driver_order)
        self._composite_driver = CompositeWeChatDriver(self._build_drivers())

    def __getattr__(self, name: str) -> Any:
        """Preserve legacy UIA helper access while keeping the facade compositional."""
        uia_driver = self.__dict__.get("_uia_driver")
        if uia_driver is None:
            raise AttributeError(name)
        return getattr(uia_driver, name)

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

    def like_recent_moments(
        self,
        limit: int = 20,
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        """给朋友圈时间线里**今天**的动态点赞，最多 ``limit`` 条。只刷自己的时间线，不逐个好友翻。"""
        return self._run_driver(
            "like_recent_moments",
            limit=limit,
            dry_run=dry_run,
        )

    def scan_recent_moments(
        self,
        limit: int = 10,
        like_limit: int = 0,
        until_key: str = "",
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        """本机巡看：从时间线顶端往下翻，只收**今天**且**上次没看过**的动态（翻到 ``until_key`` 就停），
        顺手给新动态点赞（最多 ``like_limit`` 个，0 = 不点）。"""
        return self._run_driver(
            "scan_recent_moments",
            limit=limit,
            like_limit=like_limit,
            until_key=until_key,
            dry_run=dry_run,
        )

    def comment_recent_moments(
        self,
        comments: list[dict[str, Any]],
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        """给时间线里**今天**的、指定的那几条动态各写一条评论。

        每条 ``{"content": 动态正文, "text": 评论}``——正文就是 ``read_recent_moments`` 读回来的那段，
        按它在时间线上认动态；认不到的一条都不评。
        """
        return self._run_driver(
            "comment_recent_moments",
            comments=comments,
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
                drivers.append(self._uia_driver)
            else:
                logger.warning("wechat.driver.unknown", driver=driver_name)
        if not drivers:
            drivers.append(self._uia_driver)
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
