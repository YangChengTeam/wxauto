from __future__ import annotations


class WeChatApiError(RuntimeError):
    pass


class WeChatApiUnavailableError(WeChatApiError):
    pass


class WeChatActionUnsupported(WeChatApiError):
    pass
