; 劳小智微信助手 · Windows 安装包（Inno Setup 6）
;
;   由 packaging/build.py 调用，别手工敲 ISCC——版本号、产物路径都是那边算好传进来的：
;   iscc /DAppVersion=0.1.0 /DSourceDir=...\dist\劳小智微信助手 packaging/installer.iss
;
; 四条定下来的事，改之前先读完理由：
;
; 1. **按用户装，不要管理员**（PrivilegesRequired=lowest，装到 %LOCALAPPDATA%）。
;    不是图省事：招聘专员那台电脑多半没有管理员密码，弹一个 UAC 就装不下去了。
;    更要紧的是 UIA——用它驱动微信要求两个程序**权限级别相同**（客户端契约 §8 的
;    WECHAT_UIA_PERMISSION_DENIED）。微信是普通权限跑的，我们提权装、提权跑，
;    结果就是连不上微信，而报错只说「无法访问微信窗口」，没人查得到是装的时候埋的。
;
; 2. **不在安装包里做开机自启**。程序自己的「启动设置」里有这一项（WXCLIENT_UI_AUTOSTART）。
;    装的时候勾一次、程序里再关一次，「到底开没开」就有两个答案了。
;
; 3. **卸载不默认删数据**。数据目录里有登录状态、参数和日志（%LOCALAPPDATA%\劳小智微信助手）。
;    卸载往往只是为了装新版；默默删掉等于让人重新登录、重新配一遍。要删得人明确点头。
;
; 4. **升级先删上一版的 `_internal`**（[InstallDelete]）。理由写在那一节上面。

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #error 必须传 /DSourceDir=<PyInstaller 的 onedir 产物目录>
#endif
#ifndef OutputDir
  #define OutputDir "..\dist"
#endif

#define AppName "劳小智微信助手"
#define AppPublisher "劳小智"
#define AppExe "劳小智微信助手.exe"
#define AppUrl "https://www.laoxiaozhi.cn"

[Setup]
; AppId 是这个产品的身份，**升级安装靠它认出「装过的是同一个东西」**。
; 一旦发出去就永远别改——改了的后果是新版装成第二份，两个都在开机自启、
; 两个进程抢同一个微信。
AppId={{8F3C2A61-7D94-4E0B-9C55-6A1E2B7F0D3A}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppUrl}
AppSupportURL={#AppUrl}
VersionInfoVersion={#AppVersion}
VersionInfoCompany={#AppPublisher}
VersionInfoDescription={#AppName} 安装程序

DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; 装到哪其实没人关心，但**得让人能改**——公司统一装 D 盘这种要求很常见。
DisableDirPage=no
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; ★ 明确挡住装不了的系统。不写的话 Inno 默认放行到 Win7 SP1，而这个包里的
; Python 是 3.12（3.9 起就不支持 Win7 了），装完的表现是双击没反应——一个连
; 「为什么打不开」都说不出来的失败。写死 10.0：这也是我们唯一真机验过的档。
; x64compatible 顺带允许 ARM64 的 Win11 走 x64 模拟。
MinVersion=10.0

OutputDir={#OutputDir}
OutputBaseFilename={#AppName}_v{#AppVersion}_安装包
SetupIconFile=..\wxclient\ui\assets\app.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}

; 压缩到底：onedir 装开约 75 MB，装的人多半在用公司网络下载（压出来的安装包约 25 MB）。
Compression=lzma2/max
SolidCompression=yes
LZMANumBlockThreads=4

WizardStyle=modern
WizardImageFile=assets\wizard-large.bmp,assets\wizard-large@1.5x.bmp,assets\wizard-large@2x.bmp
WizardSmallImageFile=assets\wizard-small.bmp,assets\wizard-small@1.5x.bmp,assets\wizard-small@2x.bmp
WizardImageStretch=yes

; 装之前先把还开着的旧版关掉（走 Restart Manager，会明确问过人）。
; 不这么做的话，覆盖装到一半会撞上「文件被占用」，然后要求重启电脑才能收尾。
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "chs"; MessagesFile: "languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式:"

[InstallDelete]
; ★ 升级时先把上一版的 `_internal` 整个删掉，再装新的。
;
; Inno 只做「覆盖 + 新增」，**不删**新版里已经没有的文件。PyInstaller 的 onedir 每版
; 收集到的东西都不一样（换个依赖版本就少几十个文件），于是原地升级出来的目录是
; **两版的并集**。2026-08-19 实测 0.1.20 → 0.1.21：装完 1746 个文件，而包里只有 1067 个，
; 680 个是上一版的残留。
;
; 最难看的一个：pywin32 新版把 `Pythonwin\` 改成了 `pythonwin\`，而 Windows 文件名不分
; 大小写——两版的 `win32ui.pyd` 落进同一个目录，谁盖谁看装的顺序。`win32ui` 正是
; 2026-08-16 那次「微信明明开着，界面说找不到微信」的主角，拿一个版本不对的 .pyd 去
; 顶它，症状会一模一样、而且只在**升级过的机器**上出现——打包机和全新安装都是好的。
;
; 只删 `_internal`：exe 本身会被覆盖，而 `{app}` 下别的东西（如果哪天放了什么）不该连坐。
; 数据目录在 `%LOCALAPPDATA%\劳小智微信助手`，不在这儿，升级不碰。
Type: filesandordirs; Name: "{app}\_internal"

[Files]
; 整个 onedir 目录一起装。**别逐个列文件**——PyInstaller 每次升级都会换几个 DLL，
; 漏掉一个的表现是装完打不开，而这个包在打包机上是好的。
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; ★ 快捷方式必须直接指向 exe。指向 .bat 或 python.exe 的后果是每次打开先闪一个黑窗
;   ——2026-08 真机上排查过一轮，当时怀疑是打包的问题，真凶就是快捷方式指错了东西。
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; IconFilename: "{app}\{#AppExe}"
Name: "{group}\卸载 {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; IconFilename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "立即运行 {#AppName}"; Flags: nowait postinstall skipifsilent
; ★ 客户端自动更新（`wxclient/ui/updater.py`）用 `/SILENT ... /AUTOUPDATE=1` 起本安装程序：
;   旧版已经自己退干净了，装完要把新版**拉起来**，不然用户看到的是「点了更新，程序没了」。
;   只认这个参数，别改成 WizardSilent：管理员批量静默部署时不该在每台机器上弹出程序。
Filename: "{app}\{#AppExe}"; Flags: nowait; Check: LaunchedByUpdater

[Code]
function LaunchedByUpdater: Boolean;
begin
  Result := ExpandConstant('{param:AUTOUPDATE|0}') = '1';
end;

{ 卸载时问一句要不要连数据一起删。默认「否」——卸载多半只是为了装新版。 }
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{localappdata}\{#AppName}');
    if DirExists(DataDir) then
    begin
      if MsgBox('是否同时删除本机数据？' + #13#10 + #13#10 +
                '包括登录状态、参数设置和运行日志。' + #13#10 +
                '如果只是要重新安装或升级，请选「否」，这些会保留下来。',
                mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(DataDir, True, True, True);
    end;
  end;
end;
