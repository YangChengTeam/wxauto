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
