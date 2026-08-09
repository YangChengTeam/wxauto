"""Compatibility facade for the responsibility-specific WeChat API modules."""

from .api import CompositeWeChatDriver, WeChatApi
from .errors import (
    WeChatActionUnsupported,
    WeChatApiError,
    WeChatApiUnavailableError,
)
from .omni import OmniVisualDriver
from .uia import UiaWeChatDriver, _collect_contact_search_matches

__all__ = [
    "CompositeWeChatDriver",
    "OmniVisualDriver",
    "UiaWeChatDriver",
    "WeChatActionUnsupported",
    "WeChatApi",
    "WeChatApiError",
    "WeChatApiUnavailableError",
    "_collect_contact_search_matches",
]
