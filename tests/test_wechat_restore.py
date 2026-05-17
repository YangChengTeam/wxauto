"""诊断微信窗口状态：检测托盘/任务栏最小化时的窗口行为。"""
import ctypes
import ctypes.wintypes as wintypes
import time

import win32gui
import win32con
import pyautogui

user32 = ctypes.windll.user32


def get_window_state(handle):
    """获取窗口的详细状态信息。"""
    style = user32.GetWindowLongW(handle, win32con.GWL_STYLE)
    placement = wintypes.WINDOWPLACEMENT()
    placement.length = ctypes.sizeof(wintypes.WINDOWPLACEMENT)
    user32.GetWindowPlacement(handle, ctypes.byref(placement))

    is_visible = bool(style & win32con.WS_VISIBLE)
    is_minimized = bool(style & win32con.WS_MINIMIZE)
    is_iconic = bool(user32.IsIconic(handle))
    show_cmd = placement.showCmd

    show_cmd_names = {
        1: "SW_SHOWNORMAL",
        2: "SW_SHOWMINIMIZED",
        3: "SW_SHOWMAXIMIZED",
        4: "SW_SHOWNOACTIVATE",
        5: "SW_SHOW",
        6: "SW_MINIMIZE",
        7: "SW_SHOWMINNOACTIVE",
        8: "SW_SHOWNA",
        9: "SW_RESTORE",
        11: "SW_SHOWDEFAULT",
    }

    rect = win32gui.GetWindowRect(handle)
    class_name = win32gui.GetClassName(handle)
    title = win32gui.GetWindowText(handle)

    return {
        "handle": handle,
        "class_name": class_name,
        "title": title,
        "rect": rect,
        "is_visible": is_visible,
        "is_minimized": is_minimized,
        "is_iconic": is_iconic,
        "show_cmd": show_cmd,
        "show_cmd_name": show_cmd_names.get(show_cmd, f"UNKNOWN({show_cmd})"),
        "placement_pt": (placement.ptMinPosition.x, placement.ptMinPosition.y),
        "placement_rc": (
            placement.rcNormalPosition.left,
            placement.rcNormalPosition.top,
            placement.rcNormalPosition.right,
            placement.rcNormalPosition.bottom,
        ),
    }


def find_wechat_windows():
    """查找所有可能的微信窗口。"""
    results = []
    qt_pattern = __import__("re").compile(r"Qt\d+QWindowIcon")

    def enum_callback(hwnd, _):
        classname = win32gui.GetClassName(hwnd)
        if qt_pattern.match(classname):
            results.append(hwnd)

    win32gui.EnumDesktopWindows(0, enum_callback, None)
    return results


def take_screenshot(label):
    """截图并保存。"""
    img = pyautogui.screenshot()
    path = f"diag_{label}.png"
    img.save(path)
    print(f"  截图已保存: {path}")
    return path


def try_restore_method(handle, method_name, method_fn):
    """尝试一种恢复方法并截图。"""
    print(f"\n=== 尝试恢复方法: {method_name} ===")
    state_before = get_window_state(handle)
    print(f"  恢复前: visible={state_before['is_visible']}, "
          f"iconic={state_before['is_iconic']}, "
          f"show_cmd={state_before['show_cmd_name']}")

    method_fn(handle)

    time.sleep(0.5)
    state_after = get_window_state(handle)
    print(f"  恢复后: visible={state_after['is_visible']}, "
          f"iconic={state_after['is_iconic']}, "
          f"show_cmd={state_after['show_cmd_name']}, "
          f"rect={state_after['rect']}")

    return take_screenshot(method_name)


def main():
    print("=" * 60)
    print("微信窗口状态诊断")
    print("=" * 60)

    # 1. 查找微信窗口
    handles = find_wechat_windows()
    print(f"\n找到 {len(handles)} 个 Qt 窗口")

    from pywinauto import Desktop
    desktop = Desktop(backend="uia")

    wechat_handle = None
    for h in handles:
        try:
            cn = desktop.window(handle=h).class_name()
            if "mmui::MainWindow" in cn:
                wechat_handle = h
                print(f"  微信主窗口: handle={h}, class={cn}")
        except Exception:
            pass

    if not wechat_handle:
        print("未找到微信主窗口！请确认微信正在运行且已登录。")
        return

    # 2. 当前窗口状态
    state = get_window_state(wechat_handle)
    print("\n当前窗口状态:")
    for k, v in state.items():
        print(f"  {k}: {v}")

    # 3. 如果窗口不在前台，先最小化/隐藏它来模拟托盘状态
    print("\n--- 模拟微信在托盘的状态 ---")
    print("请手动将微信最小化到托盘，然后按回车继续...")
    # input()

    # 先截图当前状态
    take_screenshot("0_before")

    state = get_window_state(wechat_handle)
    print("\n托盘后窗口状态:")
    for k, v in state.items():
        print(f"  {k}: {v}")

    # 4. 测试不同的恢复方法
    methods = {
        "1_sw_show": lambda h: win32gui.ShowWindow(h, win32con.SW_SHOW),
        "2_sw_restore": lambda h: win32gui.ShowWindow(h, win32con.SW_RESTORE),
        "3_sw_shownormal": lambda h: win32gui.ShowWindow(h, win32con.SW_SHOWNORMAL),
        "4_show_then_restore": lambda h: (
            win32gui.ShowWindow(h, win32con.SW_SHOW),
            time.sleep(0.1),
            win32gui.ShowWindow(h, win32con.SW_RESTORE),
        ),
        "5_restore_then_setforeground": lambda h: (
            win32gui.ShowWindow(h, win32con.SW_RESTORE),
            time.sleep(0.1),
            win32gui.SetForegroundWindow(h),
        ),
    }

    # 先最小化回托盘
    print("\n--- 开始测试各种恢复方法 ---")
    print("每次测试后会重新最小化到托盘")

    screenshots = {}
    for name, method in methods.items():
        try:
            path = try_restore_method(wechat_handle, name, method)
            screenshots[name] = path
        except Exception as e:
            print(f"  方法 {name} 失败: {e}")
            screenshots[name] = None

        # 重新最小化
        print("  请重新将微信最小化到托盘，然后按回车...")
        # input()
        # 用代码模拟最小化到托盘（发送关闭消息，微信会最小化到托盘）
        win32gui.ShowWindow(wechat_handle, win32con.SW_HIDE)
        time.sleep(0.5)

    print("\n" + "=" * 60)
    print("诊断完成！请检查截图判断哪种方法有效。")
    print("=" * 60)


if __name__ == "__main__":
    main()
