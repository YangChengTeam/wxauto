# wxauto-gateway

Windows 微信自动化网关，服务于劳小智招聘业务：把入站微信消息翻译成一次 AI 对话轮，
再把 AI 产出的回复翻译回微信消息发出去。所有微信动作通过单消费者队列串行执行。

## 能力

- 入站轮询取消息、按 `msg_id` 去重、连发去抖聚合、同会话严格串行
- 连劳小智 AI 端（WS 客户端：换票 → `session.start`/`resume` → `message.send` → 取卡）
- 确定性守门：拒绝词表、频次上限、工作时间窗、黑名单
- 发送消息、加好友、监听会话、读取可见消息、发布朋友圈
- 只读探测微信 UIA 可用性
- 桌面端：登录 → 拉参数 → 托盘常驻 + 实时日志 + 配置中心 + 计划中心
- SQLite 任务记录、同类待执行任务去重、单 worker 串行操作微信
- 真实动作三重确认（`--allow-real-actions` + `dry_run=false` + 确认短语）

本项目**不做对话理解、不生成回复内容、不接大模型**——那些全在劳小智 AI 端
（`lxz_agent_backend`）。本仓只负责传输、编排、执行，也不做定时调度。

## 安装与启动

要求 Windows 10/11、Python 3.10+、已登录的微信 PC 端，以及未锁屏的交互式桌面。

```powershell
python -m pip install -e .
python -m wxclient.ui                                                          # 桌面端：开机即跑，缩到托盘
python -m wxclient.gateway --account wxid_zhangsan_work                        # 命令行：dry-run，不真发
python -m wxclient.gateway --account wxid_zhangsan_work --allow-real-actions   # 命令行：真发
```

## 桌面端（托盘）

`python -m wxclient.ui` 打开即启动服务并**直接缩到右下角托盘**，不弹窗。

- 双击托盘图标 → 日志窗口（和日志文件里同一份内容，可暂停滚动、清空）
- 托盘菜单 → 启动 / 停止服务、参数设置、退出
- 三页：**实时日志**（默认）· **计划中心**（后端下发的计划，现在还是空的）·
  **配置中心**（参数 + 只读的监听名单）
- 托盘图标颜色即服务状态：绿=运行中，灰=已停止，红=启动失败（悬浮看原因）
- 关闭日志窗口 = 缩回托盘，**不会停掉服务**；真要退出走托盘菜单「退出」

### 登录与参数下发

打开界面要**先登录**：登录之前看不到日志、配置和计划。登录之后客户端向后端拉一份参数，
省掉「一台台机器手抄微信号、地址、internal key」这件必然出错的事。

- 配了 `WXCLIENT_UI_LOGIN_API_URL` → 走后端（契约见 `wxclient/ui/auth.py` 的
  `REMOTE_CONTRACT`：`POST /desktop/login` + `GET /desktop/config`）。**后端这一头还没实现**，
  客户端这一侧已经按契约写好了。
- 没配 → 本机模式：第一次打开当场设一个本机账号（scrypt 派生存 `<data_dir>/console-account.json`，
  不存明文），参数就用本机这份。**没有「跳过登录」这条路**。
- 「记住账号」只记账号名，**永不记密码**。
- 退出登录只关门，**不停服务**——网关是常驻的，跟谁在看没关系。

参数有两份，合并规则只有一条 **本机盖过后端**：后端下发的落 `<data_dir>/gateway.remote.env`
（每次拉取整份覆盖），配置中心里改的落 `<data_dir>/gateway.env`。两边不一样的行会标出
「本机已改（后端：xxx）」。和后端一模一样的值**不会**在本机留一份，否则保存一次就等于
把后端从此架空。真实环境变量仍优先于这两个文件，除非在界面上点了「保存并重启服务」。
内部 key 在界面上只显示掩码，没动过就不会被写回。

服务本身不等人登录：上次拉下来的参数还在就照常自启（开机自启不能因为没人登录就停摆）；
全新的机器一个参数都没有，那才必须先登录，这时窗口会强制露面而不是缩在托盘里装死。

**默认 dry-run**：整条回路都跑，但不真往微信发消息。要真发必须显式加
`--allow-real-actions`。接口契约与红线见 [`CLAUDE.md`](CLAUDE.md)，实施清单见
[`docs/gateway-plan.md`](docs/gateway-plan.md)。

## 打包

```bat
pip install -e .[build]
pyinstaller --noconfirm packaging\lxz-wechat-gateway.spec
:: 产物：dist\劳小智微信助手\劳小智微信助手.exe
```

**onedir 不是 onefile。** onefile 每次双击都要把整包解压到 `%TEMP%` 再跑，冷启动白等
好几秒（杀软扫这个解压过程尤其慢）。要「一个文件发给客户」就打安装包或压缩包，别让
用户天天付解压的钱。

包大小 **61.8 MB / 1702 个文件**，双击到窗口出来 **4.5 秒**（2026-08-14 真机实测；
2026-08-13 那版是 125.8 MB / 1835 个文件 / 12.3 秒）。砍的都是「一处都没 import」的整块：
OpenCV 138MB（`pyautogui → pyscreeze` 拖进来，只有模糊图像匹配 `confidence=` 才用得上，
本仓找元素走 UIA 控件树）、numpy+openblas 28MB、cryptography 9.2MB、PIL 的 AVIF 编解码器
7.8MB、Pythonwin 6.7MB、aioquic 全家 4.6MB，逐条理由写在 spec 的 `excludes` 里。
**要做图像匹配就得先把 `cv2` 那行删掉；要做语音消息就得先把 `numpy/soundfile/sounddevice`
那行删掉**（对应地，`third_party/pywechat` 里那两处音频 import 已改成用到时再 import）。

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
  ui/           # 桌面端：托盘 + 日志窗口 + 参数设置（不含业务逻辑，只是网关的一层皮）
  gateway/      # WS 网关：入站轮询、去抖、会话映射、换票、出站发送、守门
  runtime.py    # 共享运行时：任务队列 + 执行器 + 动作授权（各入口共用一个）
  queue/        # SQLite 任务与单消费者 worker
  wechat_api/   # pyweixin 二次封装
  uia_probe/    # 只读 UIA 状态探测
  config/       # Windows 微信运行参数
```

所有微信 UI 动作必须通过 `gateway -> queue -> wechat_api`，不能直接操作窗口或绕过
队列调 pyweixin。UIA 探测不会启动、关闭、重启或解锁微信。

> 历史：本仓早期是一个被动的 MCP 工具服务（`wxclient/mcp_server/`，`wxclient-mcp.exe`）。
> 自动化招聘要的是「事件来调 AI」而不是「人来调 AI」，MCP 唤不醒入站消息，已于本次整体移除；
> 它唯一不可替代的部分——任务队列与动作授权——保留在 `wxclient/runtime.py`。
