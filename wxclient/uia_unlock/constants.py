from __future__ import annotations

WECHAT_WINDOW_TITLE = "微信"
WECHAT_PROCESS_NAMES = {"wechat.exe", "weixin.exe"}
WECHAT_MAIN_CLASSES = {"WeChatMainWndForPC", "WeChatLoginWndForPC"}

NATIVE_UIA_CLIENT_CLSID = "{ff48dba4-60ef-4201-aa87-54103eef594e}"
NATIVE_UIA_MAIN_CLASSES = {"WeChatMainWndForPC", "mmui::MainWindow"}
NATIVE_UIA_LOGIN_CLASSES = {"WeChatLoginWndForPC", "mmui::LoginWindow"}
NATIVE_UIA_WINDOW_CLASSES = tuple(
    sorted(NATIVE_UIA_MAIN_CLASSES | NATIVE_UIA_LOGIN_CLASSES)
)
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

SW_SHOW = 5
SW_RESTORE = 9
HWND_TOP = 0
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_SHOWWINDOW = 0x0040
SWP_NOOWNERZORDER = 0x0200
