from __future__ import annotations

WECHAT_WINDOW_TITLE = "微信"
WECHAT_PROCESS_NAMES = {"wechat.exe", "weixin.exe"}
WECHAT_MAIN_CLASSES = {"WeChatMainWndForPC", "WeChatLoginWndForPC"}
UIA_MAIN_CLASSES = {"WeChatMainWndForPC", "mmui::MainWindow"}
UIA_LOGIN_CLASSES = {"WeChatLoginWndForPC", "mmui::LoginWindow"}
MIN_UIA_EXPOSED_DESCENDANTS = 30
DEFAULT_UIA_PROBE_MAX_DEPTH = 6
DEFAULT_UIA_PROBE_MAX_NODES = 500

WECHAT_UTILITY_CLASSES = {
    "Base_PowerMessageWindow",
    "Chrome_SystemMessageWindow",
    "DisplayICC_SystemMessageWindow",
    "IME",
    "MSCTFIME UI",
    "Qt51514WxTrayIconMessageWindowClass",
    "Sogou_TSF_UI",
    "SoWB_Status",
    "SoWB_UI",
    "libusb-1.0-windows-hotplug",
}
