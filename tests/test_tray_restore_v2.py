"""测试从托盘状态恢复微信窗口的各种方法。

模拟真实托盘行为（仅 SW_HIDE，不先最小化），然后尝试不同的恢复策略。
"""
import ctypes
import ctypes.wintypes as wintypes
import time

import win32gui
import win32con
import pyautogui

user32 = ctypes.windll.user32


def find_wechat_handle():
    """查找微信主窗口句柄。"""
    import re
    qt_pattern = re.compile(r"Qt\d+QWindowIcon")
    results = []

    def enum_callback(hwnd, _):
        classname = win32gui.GetClassName(hwnd)
        if qt_pattern.match(classname):
            results.append(hwnd)

    win32gui.EnumDesktopWindows(0, enum_callback, None)

    from pywinauto import Desktop
    desktop = Desktop(backend="uia")
    for h in results:
        try:
            cn = desktop.window(handle=h).class_name()
            if "mmui::MainWindow" in cn:
                return h
        except Exception:
            pass
    return None


def get_state(handle):
    """获取窗口状态摘要。"""
    visible = bool(user32.IsWindowVisible(handle))
    iconic = bool(user32.IsIconic(handle))

    class WINDOWPLACEMENT(ctypes.Structure):
        _fields_ = [
            ("length", ctypes.c_uint),
            ("flags", ctypes.c_uint),
            ("showCmd", ctypes.c_uint),
            ("ptMinPosition", wintypes.POINT),
            ("ptMaxPosition", wintypes.POINT),
            ("rcNormalPosition", wintypes.RECT),
        ]

    placement = WINDOWPLACEMENT()
    placement.length = ctypes.sizeof(WINDOWPLACEMENT)
    user32.GetWindowPlacement(handle, ctypes.byref(placement))
    return {
        "visible": visible,
        "iconic": iconic,
        "showCmd": placement.showCmd,
    }


def screenshot(name):
    """截图并保存。"""
    img = pyautogui.screenshot()
    path = f"diag_v2_{name}.png"
    img.save(path)
    print(f"  截图: {path}")
    return path


def test_method(handle, name, fn):
    """模拟托盘后测试一种恢复方法。"""
    print(f"\n{'='*50}")
    print(f"测试方法: {name}")
    print(f"{'='*50}")

    # 模拟真实托盘：直接隐藏（不先最小化）
    print("  模拟托盘: SW_HIDE ...")
    win32gui.ShowWindow(handle, win32con.SW_HIDE)
    time.sleep(0.5)
    state = get_state(handle)
    print(f"  隐藏后: {state}")

    # 执行恢复方法
    print(f"  执行: {name}")
    fn(handle)
    time.sleep(1.0)  # 等待渲染

    state = get_state(handle)
    print(f"  恢复后: {state}")
    return screenshot(name)


def main():
    handle = find_wechat_handle()
    if not handle:
        print("未找到微信主窗口！")
        return

    print(f"微信主窗口句柄: {handle}")
    state = get_state(handle)
    print(f"当前状态: {state}")

    # 先确保窗口可见（用于初始截图）
    win32gui.ShowWindow(handle, win32con.SW_SHOW)
    time.sleep(0.5)
    screenshot("0_normal")

    # 定义恢复方法
    RDW_INVALIDATE = 0x0001
    RDW_ERASE = 0x0004
    RDW_FRAME = 0x0400
    RDW_ALLCHILDREN = 0x0080
    RDW_UPDATENOW = 0x0100
    RDW_ERASENOW = 0x0200

    methods = {
        # 方法1: SW_RESTORE only
        "1_sw_restore": lambda h: win32gui.ShowWindow(h, win32con.SW_RESTORE),

        # 方法2: SW_SHOWNORMAL only
        "2_sw_shownormal": lambda h: win32gui.ShowWindow(h, win32con.SW_SHOWNORMAL),

        # 方法3: SW_RESTORE + RedrawWindow 强制重绘
        "3_restore_redraw": lambda h: (
            win32gui.ShowWindow(h, win32con.SW_RESTORE),
            time.sleep(0.2),
            user32.RedrawWindow(h, None, None,
                RDW_FRAME | RDW_INVALIDATE | RDW_ALLCHILDREN | RDW_ERASE | RDW_UPDATENOW | RDW_ERASENOW),
        ),

        # 方法4: SW_HIDE -> SW_RESTORE (先隐藏再恢复，强制Qt重新走show路径)
        "4_hide_then_restore": lambda h: (
            win32gui.ShowWindow(h, win32con.SW_HIDE),
            time.sleep(0.1),
            win32gui.ShowWindow(h, win32con.SW_RESTORE),
        ),

        # 方法5: SW_RESTORE + SetForegroundWindow
        "5_restore_foreground": lambda h: (
            win32gui.ShowWindow(h, win32con.SW_RESTORE),
            time.sleep(0.1),
            win32gui.SetForegroundWindow(h),
        ),

        # 方法6: SW_RESTORE + SetForegroundWindow + RedrawWindow
        "6_restore_fg_redraw": lambda h: (
            win32gui.ShowWindow(h, win32con.SW_RESTORE),
            time.sleep(0.1),
            win32gui.SetForegroundWindow(h),
            time.sleep(0.1),
            user32.RedrawWindow(h, None, None,
                RDW_FRAME | RDW_INVALIDATE | RDW_ALLCHILDREN | RDW_ERASE | RDW_UPDATENOW | RDW_ERASENOW),
        ),

        # 方法7: WM_SYSCOMMAND SC_RESTORE
        "7_wm_syscommand_restore": lambda h: (
            win32gui.SendMessage(h, win32con.WM_SYSCOMMAND, win32con.SC_RESTORE, 0),
            time.sleep(0.1),
            win32gui.ShowWindow(h, win32con.SW_RESTORE),
        ),

        # 方法8: SW_SHOWMINIMIZED -> SW_RESTORE (先最小化再恢复)
        "8_minimize_then_restore": lambda h: (
            win32gui.ShowWindow(h, win32con.SW_SHOWMINIMIZED),
            time.sleep(0.2),
            win32gui.ShowWindow(h, win32con.SW_RESTORE),
        ),
    }

    screenshots = {}
    for name, method in methods.items():
        try:
            path = test_method(handle, name, method)
            screenshots[name] = path
        except Exception as e:
            print(f"  方法 {name} 失败: {e}")
            screenshots[name] = None

    # 恢复窗口到正常状态
    win32gui.ShowWindow(handle, win32con.SW_RESTORE)
    win32gui.SetForegroundWindow(handle)

    print(f"\n{'='*50}")
    print("测试完成！截图文件：")
    for name, path in screenshots.items():
        print(f"  {name}: {path}")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()
