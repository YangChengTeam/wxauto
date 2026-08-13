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
]
# 没往表里放但值得知道的：numpy（含 openblas 19MB）**必须留着**——
# third_party/pywechat 的 `utils.py` 顶上就 `import soundfile`，而 soundfile 依赖 numpy。
# cryptography（_rust.pyd 9MB）也先留着：TLS 走的是标准库 ssl 不是它，但没实测过砍掉的
# 影响，9MB 不值得拿线上连不上去换。

# 入口是 launcher.py 不是 wxclient/ui/__main__.py——理由见 launcher.py 顶上的注释。
a = Analysis(
    ["launcher.py"],
    pathex=["."],
    datas=[("../wxclient/ui/assets/app.ico", "wxclient/ui/assets"),
           ("../wxclient/ui/assets/app-256.png", "wxclient/ui/assets")],
    hiddenimports=hiddenimports,
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
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
