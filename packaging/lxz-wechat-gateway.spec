# PyInstaller 打包配置。
#
#   pyinstaller --noconfirm packaging/lxz-wechat-gateway.spec
#   产物：dist/劳小智微信助手/劳小智微信助手.exe
#
# **为什么是 onedir 不是 onefile。** onefile 每次双击都要把整包（上百 MB）解压到
# %TEMP% 再跑，冷启动白等好几秒——正是「打开很慢」的一大来源，而且杀毒软件扫这个
# 解压过程尤其慢。onedir 直接从目录跑，省掉每次解压。要「一个文件发给客户」的话，
# 打个安装包或压缩包，别让用户天天付解压的钱。
#
# 图标一份管到底：exe、桌面快捷方式、任务栏、右下角托盘都用 wxclient/ui/assets/app.ico。

from PyInstaller.utils.hooks import collect_submodules

hiddenimports = collect_submodules("pystray") + collect_submodules("comtypes")

# 用不上的大件别装箱。这些要么是别的库顺手拖进来的可选依赖，要么是开发期才用的东西：
# 装进包里只会让体积变大、扫毒变慢，运行时一行都不会执行。
# ⚠️ 加名字前先确认真没人 import——third_party/pywechat 的运行时依赖
# （emoji/pillow/pycaw/sounddevice/soundfile 及其 numpy）本仓代码不直接 import，
# 但**跑起来要用**，绝不能因为「没人引用」就塞进这张表（见 pyproject 同款警告）。
excludes = [
    # 开发/打包工具链
    "pip", "setuptools", "wheel", "pkg_resources", "PyInstaller",
    "pytest", "unittest", "doctest", "pdb", "pydoc", "pydoc_data",
    # 别的 GUI 框架（我们只用 tkinter + pystray）
    "PyQt5", "PyQt6", "PySide2", "PySide6", "wx",
    # 科学计算/绘图全家桶：没人 import，但一旦被误收就是几十上百 MB
    "matplotlib", "pandas", "scipy", "IPython", "jupyter", "notebook",
    # OpenCV：**整包最大的一块，138 MB**（cv2.pyd 86MB + 两个 ffmpeg dll 52MB）。
    # 谁把它拖进来的：pyautogui → pyscreeze，而 pyscreeze 只有在 `locateOnScreen(...,
    # confidence=)` 这种模糊图像匹配时才需要 cv2。本仓 + third_party/pywechat 全文没有
    # 一处 `import cv2`、也没有一处用 confidence=（找元素走的是 UIA 控件树，不是找图），
    # 所以整块砍掉。**哪天真要做图像匹配，得先把这行删了再打包。**
    "cv2",
    # MySQL 驱动：队列库是 SQLite，SQLAlchemy 的 mysql 方言没人用，
    # 但打包机上装着驱动就会被连 libmysql.dll(7MB) 一起收进来。
    "pymysql", "MySQLdb", "mysql", "mysqlclient",
    # tkinter 自带的演示与测试
    "tkinter.test", "test",
    # ── 以下 2026-08-14 一批：真机 dist 逐目录量过体积，确认没有一处 import ──
    # numpy + openblas 共 28 MB，整包第二大。原来必须留着是因为 third_party/pywechat
    # 的 utils.py / WinSettings.py 在**模块顶上** import soundfile/sounddevice；这两个库
    # 只服务语音消息（本仓一处都不调），已改成用到时再 import，所以这三块一起砍。
    # ⚠️ 哪天要做语音消息，先把 utils.py 那两行改回顶层 import，再删这三个名字。
    "numpy", "soundfile", "sounddevice",
    # cryptography（_rust.pyd 9.2MB）：TLS 走标准库 ssl，全仓无一处 import。
    "cryptography",
    # HTTP/3 全家桶：httpx 用的是 h11/httpcore 那条路，aioquic 没人 import。
    "aioquic", "pylsqpack",
    # Pythonwin（win32ui.pyd + mfc140u.dll 共 6.7MB）：pywin32 的 GUI 部分，我们只用
    # win32api/win32gui/win32clipboard 那几个模块。
    "Pythonwin", "win32ui", "win32uiole",
    # 打包机 env 里装着、被顺手收进来的：类型检查器、另一套 HTTP 栈、.NET 桥、
    # httpx 的可选压缩后端（缺了它只是不宣告 br/zstd，功能不受影响）。
    "mypy", "aiohttp", "pythonnet", "clr", "zstandard", "brotli", "bcrypt",
]

# 模块级 excludes 管不到的：包本身要留，只是里面某个大插件从来用不上。
# PIL 要留（托盘图标 + pywechat 用），但 AVIF 编解码器一个人就 7.8 MB，我们只画 PNG/ICO。
DEAD_BINARIES = ("PIL\\_avif", "PIL/_avif")
# 没砍、也砍不得的几块：pydantic_core(5.4MB，配置模型要用)、PIL 主体(托盘图标)、
# tcl/tk(界面本体)、libcrypto/libssl(标准库 ssl 靠它连 wss)、pywin32_system32(COM)。

# 入口是 launcher.py 不是 wxclient/ui/__main__.py——理由见 launcher.py 顶上的注释。
a = Analysis(
    ["launcher.py"],
    pathex=["."],
    # ★ 整个 assets 目录一起打，**不要逐个文件列**：漏掉一个的表现是「打出来的包少一张图」，
    # 而代码对缺图都是优雅降级（背景变纯色、图标退回圆点），没人会看见报错——正是最难发现的
    # 那类回归。（`tests/test_ui.py::PackagingTests` 会盯着这条。）
    datas=[("../wxclient/ui/assets", "wxclient/ui/assets")],
    hiddenimports=hiddenimports,
    excludes=excludes,
    noarchive=False,
)
a.binaries = [item for item in a.binaries if not any(dead in item[0] for dead in DEAD_BINARIES)]

pyz = PYZ(a.pure)

# 冻结包的第一屏。**双击到 Python 起来那 1~2 秒 tkinter 还不存在**，只有 PyInstaller
# 自带的这个 Splash 顶得上；Python 一起来，`wxclient/ui/splash.py` 那个会动的启动窗
# 立刻接手并把它关掉（`pyi_splash.close()`）。两张图长得一样，看上去是同一个东西在动。
# 图由 `packaging/make_splash.py` 按 theme.py 生成，别手改 PNG。
splash = Splash(
    "../wxclient/ui/assets/splash.png",
    binaries=a.binaries,
    datas=a.datas,
    # 左对齐（PyInstaller 不给居中），所以 x 要自己往左挪半个文案的宽度才对得上中线
    text_pos=(155, 176),
    text_size=9,
    text_color="#8b8b8b",
    text_default="正在启动…",
    always_on_top=True,
)

exe = EXE(
    pyz, a.scripts, splash, splash.binaries, [],
    exclude_binaries=True,          # onedir：二进制留在目录里，不塞进 exe
    name="劳小智微信助手",
    icon="../wxclient/ui/assets/app.ico",
    console=False,                  # 桌面程序，不弹黑窗
    # UPX 不开：压出来的 exe 是杀软误报重灾区，为了几十 MB 让客户机报毒不划算。
    upx=False,
)

COLLECT(
    exe, a.binaries, a.datas,
    name="劳小智微信助手",
    upx=False,
)
