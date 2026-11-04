"""微信本地数据库的只读读取（入站监听用，见 ``reader.WeChatDbReader``）。"""

from .reader import WeChatDbReader, build_reader, locate_storages

__all__ = ["WeChatDbReader", "build_reader", "locate_storages"]
