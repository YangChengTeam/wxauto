from __future__ import annotations


class WeChatApiError(RuntimeError):
    pass


class WeChatApiUnavailableError(WeChatApiError):
    pass


class WeChatActionUnsupported(WeChatApiError):
    pass


class MomentsDockedLayoutError(WeChatApiError):
    """朋友圈是 4.1.13 起的「停靠进主窗口」版式，而调用方走的是老的独立窗口那条路。

    发朋友圈接到它会改走停靠版式的流程；点赞/评论/巡看还没适配，直接把它当失败报出去。
    """


class WeChatContactNotFound(WeChatApiError):
    """微信里搜不到这个会话名——对应 pyweixin 的 ``NoSuchFriendError``（「好友或群聊备注有误！查无此人！」）。

    它是在**搜联系人那一步**抛的：搜索框里没有这个名字的结果，聊天窗口根本没打开，
    一个字都没粘进输入框。所以结论是确定的「没发出去」，不是「不知道发没发」。
    """


#: 回执里给上游（AI 端 → 业务端）认的稳定码。改名 = 业务端的归类立刻认不出来。
CONTACT_NOT_FOUND_CODE = "CONTACT_NOT_FOUND"


def is_contact_not_found(exc: BaseException) -> bool:
    """这个异常（或它 ``raise ... from`` 的那个原因）是不是「微信里搜不到这个人」。

    按类名认 vendor 的 ``NoSuchFriendError``，不 import 它：``pyweixin.Errors`` 一 import
    就连带执行整条 pywinauto 导入链（见 ``ui/__main__.py`` 的同款说明）。只顺着 ``__cause__``
    往下找——``__context__`` 是「处理 A 时又出了 B」，B 的结论不能拿 A 来定。
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, WeChatContactNotFound) or type(current).__name__ == "NoSuchFriendError":
            return True
        current = current.__cause__
    return False
