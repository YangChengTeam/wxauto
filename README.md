# wxauto-gateway

Windows 微信自动化网关，服务于劳小智招聘业务：把入站微信消息翻译成一次 AI 对话轮，
再把 AI 产出的回复翻译回微信消息发出去。所有微信动作通过单消费者队列串行执行。

## 能力

- 入站轮询取消息、按 `msg_id` 去重、连发去抖聚合、同会话严格串行
- 连劳小智 AI 端（WS 客户端：换票 → `session.start`/`resume` → `message.send` → 取卡）
- 确定性守门：频次上限、工作时间窗、候选人级人工接管（AI 端判对方拒绝 / 不再找工作时提案接管；本机拉黑 2026-10-09 已拆）
- 发送消息、加好友、监听会话、读取可见消息、发布朋友圈
- 只读探测微信 UIA 可用性
- 桌面端：登录 → 托盘常驻 + 实时日志 + 配置中心（参数都在本机配置中心里，后端不下发）
- SQLite 任务记录、同类待执行任务去重、单 worker 串行操作微信
- 真实动作三重确认（`--allow-real-actions` + `dry_run=false` + 确认短语）

本项目**不做对话理解、不生成回复内容、不接大模型**——那些全在劳小智 AI 端
（`lxz_agent_backend`）。本仓只负责传输、编排、执行，也不做定时调度。

## 给用户看的产品页

<https://worker.laoxiaozhi.cn/wxauto/> —— 产品介绍 / 运行环境 / **解锁微信 UIA** / 更新日志，
四页静态、无需登录，可以直接把链接发给招聘专员。装机卡在「未找到可连接的微信主窗口」
就看 UIA 那页。源文件在 `lxz_agent_backend/packages/product-site/`（改动与部署方式见那边的 README）。

## 安装与启动

要求 **64 位 Windows 10/11**、Python 3.10+（打包机上是 3.12）、已登录的微信 PC 端，
以及**未锁屏的交互式桌面**。装安装包的话不用装 Python，其余四条一样，细节见「打包 → 系统要求」。

```powershell
python -m pip install -e .
python -m wxclient.ui                                                          # 桌面端：开机即跑，缩到托盘
python -m wxclient.gateway --account wxid_zhangsan_work                        # 命令行：dry-run，不真发
python -m wxclient.gateway --account wxid_zhangsan_work --allow-real-actions   # 命令行：真发
```

## 桌面端（托盘）

`python -m wxclient.ui` 打开即启动服务并**直接缩到右下角托盘**，不弹窗。

- 双击托盘图标 → 日志窗口（和日志文件里同一份内容，可暂停滚动、清空）
- 日志**一天一个文件**（`<数据目录>/logs/gateway-YYYY-MM-DD.log`，留 30 天），
  日志页右上角「打开目录」开的是这个目录——要看的常常是前几天那份，替人挑今天这份没用
- 托盘菜单 → 实时日志 / 配置中心 / 退出登录 / 退出（名字和左侧导航逐字一致）（没有「启动服务」——服务跟着登录走）
- 三页：**实时日志**（默认）· **调试会话** · **配置中心**（参数 + 只读的监听名单）。
  **没有「计划中心」**——几点做什么、多久跟进一次归 AI 端排，指令经 WS 推给网关执行
- **调试会话**（`ui/debug_page.py`）：直接跟 AI 端说话，看它给这台电脑下什么指令。
  走的是网关常驻那条 WS（`task_type=wechat_ops`），指令由 AI 端推回同一条连接、由同一个
  执行器执行——**不是旁路**，所以在这儿测出来的行为就是线上的行为。它自己**一条微信都不发**
  （不复用 `handle_turn`，那条路末尾会真发到某个联系人那儿去）
- 配置中心按「连接配置 / 调度配置 / 启动设置」分三段 + 只读的「监听名单」一节
  （版式照 `docs/setting.png` / `docs/setting2.png`）：
  「测试连接」只测 TCP/TLS 可达，**不测鉴权**，文案里也这么写；底部动作条固定不跟着滚
- 监听名单那一行标题上有「下次读消息 mm:ss」倒计时，读的是网关**真实排的那一觉**
  （含抖动、含热/闲快慢档）；不在工作时间窗就直说「暂不读」，正在读就说「正在读」
- 托盘图标颜色即服务状态：绿=运行中，灰=已停止，红=启动失败（悬浮看原因）
- 关闭日志窗口 = 缩回托盘，**不会停掉服务**；真要退出走托盘菜单「退出」
- **自动更新**（`ui/updater.py`）：启动 30 秒后、之后每 6 小时取一次产品站的 `latest.json`
  （由 `publish.sh` 上架时和包一起写出）；有新版本就在左侧导航下面亮一行「有新版本 vX，点击安装」，
  平时不占位也不弹窗。点了才下载（到数据目录 `updates/`），按清单里的 SHA-256 与字节数校验过
  才交给安装程序，校验不过的当场删。**安装程序只在客户端自己退干净之后才起**（offline 已上报、
  网关线程已收口——Inno 的 CloseApplications 关不掉一个「点 × 只是缩托盘」的窗口），
  `/SILENT /AUTOUPDATE=1` 静默装完由 `installer.iss` 的 `[Run]` 把新版拉起来。
  配置中心「启动设置」里可关掉自动查，「关于」一段仍能手动「检查更新」。从源码跑的那份不能原地装，
  点了只会打开产品站。

### 登录与状态上报

打开界面要**先登录**：用劳小智的**邮箱 + 密码**登录业务后端的招聘分身客户端接口，
不走邮箱验证码。契约见 [`docs/招聘分身_客户端认证与状态接口_v1.1.md`](docs/招聘分身_客户端认证与状态接口_v1.1.md)，
客户端实现在 `wxclient/ui/client_api.py`。

```text
POST /recruitment-agent/client/auth/login          登录（不触发 OTP）
POST /recruitment-agent/client/auth/logout         退出并撤销这台设备的 Token
PUT  /recruitment-agent/client/status              在线 / 离线 / 异常
PUT  /recruitment-agent/client/wechat-uia/status   本机微信 UIA 连接状态
POST /recruitment-agent/client/session-ticket      换 AI 端短票（网关用）
```

- **只有这一条登录路**。原来那个「本机账号」模式已删：服务端管不到的登录方式，
  撤销会话、停用账号、换设备对它一律无效。
- **Token 不过期、但随时可撤销**（契约 §2.2）。它加密存在 `<data_dir>/client-session.json`
  （Windows DPAPI，按当前用户加密）；**非 Windows 一个字节都不落盘**，那时每次打开都要重登。
- 重开客户端会直接接上上次的会话；这张票还算不算数，由第一次状态上报去问服务端——
  收到 `401 CLIENT_SESSION_REVOKED` 就地清 Token、回登录页。
- 「这个分身已在另一台电脑登录」不自动顶掉：弹一句问清是哪台机器，人点了确认才带
  `replaceExistingSession=true` 重来。
- 「记住登录状态」**连密码一起记**（用户 2026-08-16）：下次打开登录页直接把密码回填进框。
  密码和 Token 同在那份 DPAPI 密文里，明文文件一个字节都不沾。自己点过「退出登录」的话
  密码照样回填，但**不会自动登进去**——「不用再打密码」和「别替我做登录这个决定」是两件事；
  被服务端**撤销**则连密码一起清掉，必须有人坐在这台电脑前重输。
- 业务端地址**默认就是正式地址** `https://api.laoxiaozhi.cn`，装机不用抄；要连别的环境
  在配置中心改（填过的值不会被默认值盖掉）。万一被清空，登录页上会多一格「服务器地址」
  ——填一次就落进 `gateway.env`（和配置中心里那格是**同一个键**）。

**没有心跳**（契约 §7）：在线状态全靠这台机器在**变化时**说一声，服务端不会按时间把
online 改成 offline。所以正常退出前会先报一次 offline（最多等 3 秒），进程崩溃/断电时
企业端会保留最后一次在线状态——这是契约明写的边界，不是 bug。

微信连接状态取自只读 UIA 探测（15 秒一拍）：微信没开、开了没登录、开了但控件树没暴露，
是**三种不同的失败**，各给各的恢复引导；连过又断了报 `disconnected` 而不是 `failed`。
相同状态不重复上报。连上时一并报 `capabilityStatus`（这台机器**到底能干哪几件事**，
复用网关那份能力表）：只有 `passed` 会让企业端进入「运行中」，dry-run 机器如实报 `limited`。

### 参数

参数只有一份：配置中心里改的，落 `<data_dir>/gateway.env`（真实环境变量仍优先，除非点了
「保存并重启服务」）。**界面上一个凭证都没有**——换票用的是登录签发的客户端 Token
（`POST /recruitment-agent/client/session-ticket`，请求体是空的——换谁的票由服务端从会话派生），
微信号也不再手抄，用登录返回的分身 ID 记账。

**登录之前一概不起服务**（用户 2026-08-15）：网关只在登录成功之后才起来。
所以启动时窗口一定会露面——否则就是一个缩在托盘里、什么都不干、也不说为什么的图标。
反过来，**「退出登录」就是「退出服务」**（只留这一个说法）：停掉网关 + 撤销服务端会话 +
回到登录页；要关掉整个程序走托盘菜单「退出」。

**默认 dry-run**：整条回路都跑，但不真往微信发消息。要真发必须显式加
`--allow-real-actions`。接口契约与红线见 [`CLAUDE.md`](CLAUDE.md)，实施清单见
[`docs/gateway-plan.md`](docs/gateway-plan.md)。

## 打包

**发给用户的是安装包，不是一个目录。** 一条命令打完：

```bat
pip install -e .[build]
python packaging\build.py
:: 产物：dist\劳小智微信助手_v<版本>_安装包.exe
```

它按顺序做四件事：生成两张图（启动首屏 + 安装向导竖幅，都按 `theme.py` 画）→ PyInstaller
打 onedir → **拿打出来的 exe 跑一次 `--self-check`**（真 import 一遍重活、真探一次微信，
不过就不出包——2026-08-16 一天撞两次「装到别人机器上才炸」，这一步是为此加的）→
Inno Setup 把 onedir 包成安装程序。改 `installer.iss` 时用 `--skip-exe` 跳过最慢的那步。
只要 onedir 不要安装包的话：

```bat
pyinstaller --noconfirm packaging\lxz-wechat-gateway.spec
:: 产物：dist\劳小智微信助手\劳小智微信助手.exe
```

### 安装包（`packaging/installer.iss`，Inno Setup 6）

打包机上要先装 [Inno Setup 6](https://jrsoftware.org/isdl.php)（中文语言文件随仓库带，
官方安装包里没有）。
安装包本身：中文向导、自带品牌配图、**桌面快捷方式**、开始菜单项、
控制面板里的卸载入口。

★ **打包机的 venv 要正好是 `pyproject.toml` 的闭包，别多装东西。** PyInstaller 收的是
**本机装了什么**，不是仓库声明了什么——环境里多一个包，它就可能顺着 import 链被打进去。
2026-08-19 换了个干净 venv 重打，安装包从 33.7 MB 掉到 25.0 MB：少掉的 `logger`、`yaml`、
`simplejson`、`tzdata`、`markupsafe`、`werkzeug` 等等**全仓一处都没 import**，是上一台打包
环境里的杂物。体积对不上先查这个，别急着怀疑包打漏了（自检那一步会告诉你漏没漏）。

### 在 GitHub Actions 上打（[`.github/workflows/build-exe.yml`](.github/workflows/build-exe.yml)）

上一段那个「venv 必须正好是闭包」的要求，在 CI 里是**结构上**满足的：每次都是一台新的
Windows 机器、`pip install -e ".[build]"` 装出来的闭包、空的 `dist/`——环境杂物和
「上一版残留混进新包」这两类事故都不可能发生。往 `refactor/drop-mcp` 推就会打一次包；
打 `v<版本>` 标签会额外核对标签和 `__version__` 对不对、建 Release，
并把 SHA-256 写进任务摘要。

★ **这是私有仓，Actions 不免费。** 免费无限量只给公开仓；私有仓吃账号每月额度，
Windows runner 还按 **2 倍**分钟计费。所以触发面只开了这一个分支加 `v*` 标签，
别顺手加 `master` 或通配分支。想再省就把 workflow 里 `branches:` 那两行删掉，
剩下只在打标签时跑。

★ **它不替代真机验收。** CI 只证明「包打得出来、`--self-check` 没炸」；
「装到别人机器上真能驱动微信」只有真机装一次才看得见（那两回「微信明明开着却说找不到微信」
都是装完才暴露的）。上架仍走 `publish.sh`，顺序照旧是**页面先改、包后传**。

四条定下来的事，改之前先读 `installer.iss` 顶上的理由：

| 决定 | 为什么 |
|---|---|
| **按用户装，不提权**（装到 `%LOCALAPPDATA%\Programs\`） | 招聘专员那台电脑多半没有管理员密码；更要紧的是 UIA 要求和微信**权限级别相同**，提权装、提权跑的结果是连不上微信，而报错只说「无法访问微信窗口」 |
| **安装包里不做开机自启** | 程序自己的「启动设置」里有这一项。两处都能改 = 「到底开没开」有两个答案 |
| **卸载不默认删数据** | 数据目录里是登录状态、参数和日志。卸载多半只是为了装新版，默默删掉等于让人重登重配一遍 |
| **只装 64 位 Win10/11**（`MinVersion=10.0` + `x64compatible`） | 包里的 Python 是 3.12，3.9 起就不支持 Win7 了。不写 `MinVersion` 的话 Inno 默认放行到 Win7 SP1，装完的表现是**双击没反应**——一个连「为什么打不开」都说不出来的失败。当场拒绝，至少人知道该换什么 |
| **升级先删上一版的 `_internal`**（`[InstallDelete]`） | Inno 只做「覆盖 + 新增」，不删新版里已经没有的文件，而 PyInstaller 每版收集到的东西都不一样 → 原地升级装出来的是**两版的并集**。2026-08-19 实测 0.1.20 → 0.1.21：装完 1746 个文件，包里只有 1067 个。最难看的是 pywin32 把 `Pythonwin` 改名成 `pythonwin`、而 Windows 不分大小写，两版的 `win32ui.pyd` 落进同一个目录——症状和「微信明明开着却说找不到」一模一样，且**只在升级过的机器上出现** |

#### 系统要求（发给用户前先对一遍）

| 项 | 要求 | 不满足会怎样 |
|---|---|---|
| 系统 | 64 位 Windows 10 / 11（ARM64 的 Win11 走 x64 模拟也行） | 安装程序**当场拒绝**并说明 |
| 权限 | **普通用户，别用管理员运行**；装到 `%LOCALAPPDATA%\Programs\` | 提权跑会因权限级别和微信不一致而连不上微信，报错只说「无法访问微信窗口」 |
| 桌面 | 有人登着的、**没锁屏**的交互式桌面；RDP 断开会话也不行 | UIA 找不到窗口，服务停在「微信不可用」 |
| 微信 | PC 版微信已登录并保持在前台可见 | 同上 |
| Python | **不需要**（运行时已打进包里） | — |

★ **快捷方式必须直接指向 exe。** 指向 `.bat` / `wscript some.vbs` / `python.exe` 的表现是
每次打开先闪一个黑窗——2026-08 真机上为这个排查过一轮，当时怀疑是打包的问题，真凶是
手工建的快捷方式指错了东西。`tests/test_ui.py::InstallerTests` 盯着这条。

**还没有代码签名。** 所以首次运行会撞上 SmartScreen 的「已保护你的电脑」，要点「更多信息
→ 仍要运行」。要去掉这一屏得买代码签名证书（OV/EV），在 `installer.iss` 里配 `SignTool`。

**onedir 不是 onefile。** onefile 每次双击都要把整包解压到 `%TEMP%` 再跑，冷启动白等
好几秒（杀软扫这个解压过程尤其慢）。要「一个文件发给客户」就打安装包或压缩包，别让
用户天天付解压的钱。

包大小约 **68 MB**，双击到窗口出来 **4.5 秒**（2026-08-14 真机实测那版是 61.8 MB /
1702 文件；2026-08-13 那版是 125.8 MB / 12.3 秒）。砍的都是「一处都没 import」的整块：
OpenCV 138MB（`pyautogui → pyscreeze` 拖进来，只有模糊图像匹配 `confidence=` 才用得上，
本仓找元素走 UIA 控件树）、numpy+openblas 28MB、PIL 的 AVIF 编解码器
7.8MB、aioquic 全家 4.6MB，逐条理由写在 spec 的 `excludes` 里。
（cryptography 曾在这张表里，0.1.121 起本地库入站要用它解库，已放回包里——别再砍。）
**要做图像匹配就得先把 `cv2` 那行删掉；要做语音消息就得先把 `numpy/soundfile/sounddevice`
那行删掉**（对应地，`third_party/pywechat` 里那两处音频 import 已改成用到时再 import）。

⚠️ **`excludes` 的依据不能是「grep 不到有人 import」。** 那只看得见我们自己的 import，
看不见依赖自己的。2026-08-16 就为这个赔了一轮：`win32ui`（6.7MB）被按这个理由砍掉，
可 **pywinauto 要用它**，于是打包版里 `from pywinauto import Desktop` 直接
ModuleNotFoundError——**UIA 探测一次都没成功过**，而源码跑一切正常。同一天还撞到
`logger/logging.conf` 这个数据文件没被收进去（PyInstaller 只收 .py）。两次的表现一模一样：
微信明明开着，界面说找不到微信。**加名字前必须打包、安装、真跑一遍**——`build.py`
现在会自动跑打出来的 exe 的 `--self-check`（真 import 重活 + 真探一次微信），不过不出包。

启动动画分两段接力：PyInstaller 的 `Splash` 先顶一张静态首屏（双击到 Python 起来那 1~2 秒
tkinter 还不存在），Python 一起来由 `wxclient/ui/splash.py` 那个会动的接手并把它关掉。
静态图别手改，跑 `python packaging/make_splash.py` 按 `theme.py` 重新生成。会动的那个整窗
带 8% 透明（`splash_style.OPACITY`），静态首屏没有透明度可调——PyInstaller 的 `Splash`
不提供这个参数，所以冻结包最开始那一两秒仍是实心的。

**别让黑框冒出来。** spec 里 `console=False`，打出来的 exe 是 GUI 子系统，本身不带控制台。
黑框基本都来自**快捷方式**：`.bat` 必然拉起一个 cmd.exe 窗口，`python.exe` 又是控制台版的
（GUI 版叫 `pythonw.exe`）。所以快捷方式要么直接指 `劳小智微信助手.exe`，要么指一个
`WScript.Shell.Run "...bat", 0` 的 `.vbs` 由它隐藏着拉起 `pythonw.exe`——
**别把 `.bat` 或 `python.exe` 挂到 `.lnk` 上**（真机 2026-08-14 就是这么踩的）。

两个真机踩过的坑，都已在仓库里修好，别再踩回去：

1. **入口必须是 `packaging/launcher.py`**，不能直接把 `wxclient/ui/__main__.py` 喂给
   PyInstaller——那样它以顶层脚本身份跑，相对 import 全废；而 windowed 构建里这条
   报错弹在**看不见的对话框**上，现象是「进程在、窗口永远不出来、日志一个字不写」。
2. **数据目录不能跟着 cwd 走**。冻结态已锚到 `%LOCALAPPDATA%\劳小智微信助手`
   （`wxclient/config/settings.py` 的 `_default_data_dir`）。之前的相对 `.wxclient`
   在真机上把日志写去了映射的网络盘 `Z:\`，装到 `Program Files` 下更是直接不可写。

凭证与 `--account` 走环境变量和命令行，不编进二进制。
原先的 `scripts/build_mcp_onefile.py` 只打 MCP 入口，已随 MCP 一并删除。

图标是**一份管到底**：`wxclient/ui/assets/app.ico`（劳小智正式图标套件，和网站后台同一个）
供 exe / 桌面快捷方式 / 任务栏 / 窗口标题栏用；右下角托盘用 `app-256.png` 加一个状态色角标
（绿=运行中、灰=已停止、红=启动失败）——角标不能省，它是唯一能一眼看出服务在不在跑的地方。
换图标只改 `wxclient/ui/assets/`，别在代码里再画一个。

## 架构边界

```text
wxclient/
  ui/           # 桌面端：登录页 + 托盘 + 日志窗口 + 配置中心（不含业务逻辑，只是网关的一层皮）
  gateway/      # WS 网关：入站轮询、去抖、会话映射、换票、出站发送、守门
  runtime.py    # 共享运行时：任务队列 + 执行器 + 动作授权（各入口共用一个）
  queue/        # SQLite 任务与单消费者 worker
  wechat_api/   # pyweixin 二次封装
  uia_probe/    # 只读 UIA 状态探测
  wechat_db/    # 只读读取微信本地库（入站监听的主路径，在子进程里跑）
  config/       # Windows 微信运行参数
```

所有微信 UI 动作必须通过 `gateway -> queue -> wechat_api`，不能直接操作窗口或绕过
队列调 pyweixin。UIA 探测不会启动、关闭、重启或解锁微信。

> 历史：本仓早期是一个被动的 MCP 工具服务（`wxclient/mcp_server/`，`wxclient-mcp.exe`）。
> 自动化招聘要的是「事件来调 AI」而不是「人来调 AI」，MCP 唤不醒入站消息，已于本次整体移除；
> 它唯一不可替代的部分——任务队列与动作授权——保留在 `wxclient/runtime.py`。
