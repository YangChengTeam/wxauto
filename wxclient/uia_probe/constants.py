from __future__ import annotations

WECHAT_WINDOW_TITLE = "微信"
WECHAT_PROCESS_NAMES = {"wechat.exe", "weixin.exe"}
WECHAT_MAIN_CLASSES = {"WeChatMainWndForPC", "WeChatLoginWndForPC"}
UIA_MAIN_CLASSES = {"WeChatMainWndForPC", "mmui::MainWindow"}
UIA_LOGIN_CLASSES = {"WeChatLoginWndForPC", "mmui::LoginWindow"}
MIN_UIA_EXPOSED_DESCENDANTS = 30
DEFAULT_UIA_PROBE_MAX_DEPTH = 6
DEFAULT_UIA_PROBE_MAX_NODES = 500
#: 界面 15 秒一拍、保活 30 秒一拍的**周期探测**最多数多少个节点。判「暴露没有」只要 ≥30 个，
#: 原来默认数到 5000——每个节点都是一次跨进程 COM 调用，要微信界面线程来答；用户一拖动微信窗口
#: （界面线程在忙），微信和客户端一起卡（用户 2026-11-04：「移动微信窗口就卡死」）。数到 4 倍阈值
#: 就够分主窗口和登录窗口。自检 / 诊断照旧数全量。
PERIODIC_PROBE_MAX_NODES = 120

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
