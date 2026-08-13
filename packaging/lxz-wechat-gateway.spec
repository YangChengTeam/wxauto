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
    # tkinter 自带的演示与测试
    "tkinter.test", "test",
]

a = Analysis(
    ["../wxclient/ui/__main__.py"],
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
