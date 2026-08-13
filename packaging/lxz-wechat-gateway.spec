# PyInstaller 打包配置：单体 exe。
#
# ⚠️ **未验证**——exe 本身还没做（README「打包」）。这份 spec 先把**图标**这条钉死：
# exe、桌面快捷方式、任务栏、右下角托盘统一用 `wxclient/ui/assets/app.ico`
# （劳小智正式图标套件，和网站后台同一个）。真要出包时再补依赖收集并在真机验。
#
#   pyinstaller packaging/lxz-wechat-gateway.spec
from PyInstaller.utils.hooks import collect_submodules

hiddenimports = collect_submodules("pystray") + collect_submodules("comtypes")

a = Analysis(
    ["../wxclient/ui/__main__.py"],
    pathex=["."],
    datas=[("../wxclient/ui/assets/app.ico", "wxclient/ui/assets"),
           ("../wxclient/ui/assets/app-256.png", "wxclient/ui/assets")],
    hiddenimports=hiddenimports,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="劳小智微信助手",
    icon="../wxclient/ui/assets/app.ico",
    console=False,          # 桌面程序，不弹黑窗
    upx=False,
)
