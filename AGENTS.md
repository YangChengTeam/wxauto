# AGENTS.md

本仓库目标是做一个 Windows 微信自动化客户端，基于 `third_party/pywechat/pyweixin`
和 UIA 解锁工程项目，优先实现加好友、自动同意好友申请、聊天与大模型回复。

## 文档索引

- UIA 解锁、pyweixin API、已验证能力：`docs/pyweixin-uia-integration.md`
- Windows 客户端模块、触发方式、稳定性、多微信方案：`docs/windows-client-architecture.md`
- 首版使用说明：`docs/first-version-usage.md`
- 第三方库源码：`third_party/pywechat/pyweixin/`
- pywechat 官方说明：`third_party/pywechat/README.md`
- 微信 4.1+ UI 自动化说明：`third_party/pywechat/Weixin4.0.md`

## 外部文档规则

- 涉及库、框架、SDK、API、CLI 或云服务时，必须先用 ctx7 查当前文档。
- 先执行 `npx ctx7@latest library <name> "<完整问题>"`，再执行 `npx ctx7@latest docs <libraryId> "<完整问题>"`。
- ctx7 请求要在 Codex 默认沙箱外运行；遇到 DNS、ENOTFOUND、fetch failed 等错误时，用同一命令在沙箱外重试。
- 不要把密钥、密码、token 放进 ctx7 查询。
- ctx7 不用于业务逻辑、重构、代码审查或纯概念问题。

## 架构原则

- 微信 UI 自动化动作必须串行执行。
- 定时器、UI、Agent 只能投递任务，不能直接操作微信窗口。
- 所有 pyweixin 调用必须经由 `wxclient/wechat_api/` 封装层。
- UIA 解锁能力独立成模块，业务层只调用 `ensure_wechat_uia()`。
- 真实加好友、同意好友、发消息、发朋友圈必须支持 `dry_run` 或确认机制。
- 默认不做 DLL 注入、微信进程伪装或绕过微信限制的实现。
- UIA 解锁允许使用独立辅助功能客户端、Windows 屏幕阅读器标志、原生 COM UIAutomation 客户端和 WinEvent hook。
- 消息监听优先复用主窗口/已有窗口，不频繁打开或关闭独立聊天窗口。
- 消息监听只负责采集和摘要，不要把 GUI 当前会话额外塞进监听集合。
- 聊天回复应基于完整上下文生成，不要把回复范围限制为“本轮新消息”。
- 未读回复或聊天回复完成后，优先返回“文件传输助手”，避免停留在会话页导致未读消息被吃掉。
- 每轮消息监听都要记录已读消息摘要，便于回溯。
- 同一类型任务如果已经存在 `PENDING` 未执行任务，就不要重复入队，避免消息堆积。
- GUI 打开后不自动执行微信动作，worker 仍应通过“启动服务”或对应入口显式开启。
- 点击“停止服务”必须等待 worker 线程真实退出后再更新为已停止状态。

## 推荐模块

```text
wxclient/
  app/           # 客户端入口、生命周期、托盘/窗口
  config/        # 账号、触发器、白名单、限频、LLM 配置
  uia_unlock/    # UIA 解锁编排、诊断、常驻辅助功能会话
  wechat_api/    # pyweixin 二次封装
  trigger/       # 定时任务、本地事件
  queue/         # 单消费者任务队列、去重、重试、状态机
  agents/        # 大模型或后续智能体
  agentbridge/   # WeChat 事件 <-> Agent 请求/响应协议
  storage/       # SQLite：任务、消息游标、好友申请、审计日志
  ui/            # 配置、状态、人工确认、运行日志
  diagnostics/   # UIA 诊断、控件树、健康检查
```

## UIA 与 pyweixin 调用规则

- 每个业务动作前先调用 `ensure_wechat_uia(restart_if_needed=False)`。
- `probe_wechat_uia()` 只能检测状态，不启动、关闭或重启微信。
- `ensure_wechat_uia()` 必须先启动辅助功能常驻会话，再检测 UIA 是否暴露。
- 辅助功能常驻会话包括：临时开启屏幕阅读器标志、原生 COM UIAutomation 客户端、WinEvent hook 线程。
- GUI 运行期间常驻会话不应提前退出；GUI 关闭或进程退出时再停止线程并恢复屏幕阅读器标志。
- 已解锁时不要重启微信，避免破坏登录状态和当前聊天窗口。
- 如果看到 `mmui::LoginWindow`，暂停业务任务并提示用户登录。
- 如果 `mmui::MainWindow` 的 raw/control/content 接近 `1/1/1`，视为 UIA 未解锁。
- GUI 日志中必须输出 UIA 诊断摘要，包括前台窗口、微信候选窗口、pywinauto probe、native COM probe、屏幕阅读器标志和常驻会话状态。
- `pyweixin` 配置默认保持 `close_weixin=False`、`is_maximize=False`。
- 打包版如果出现 `pyweixin is unavailable`，优先检查 `third_party/pywechat` 是否被正确带入。
- 打包版如果出现 `logger/logging.conf doesn't exist`，优先检查打包数据是否遗漏。

## 触发规则

- 好友申请：定时检查为主，UI/CLI 可触发立即检查。
- 外部发送消息、加好友：现阶段通过 UI/CLI 入队。
- 聊天回复：消息扫描生成事件，Agent 基于上下文产出回复，再入队发送。
- 定时检查、UI 按钮都不得绕过队列。
- 好友检测、未读回复、消息监听、添加好友、朋友圈等自动化入口都应走同一队列与同一状态机。

## UI 约定

- 自动化页的主要功能按钮统一放左侧，尺寸和间距保持一致。
- 自动化页的任务面板默认隐藏，通过勾选项开启。
- 自动化页的日志默认隐藏或折叠，按时间倒序展示。
- 每个功能页的右侧标题和主体布局要保持一致，日志折叠后不要把页面高度挤歪。
- 设置页按职责拆成 tab，至少区分“基础设备”和“Agent 设置”。

## GUI 与打包

- GUI 入口脚本是 `scripts/wxclient_gui.py`。
- 单体 EXE 构建脚本是 `scripts/build_gui_onefile.py`。
- onefile 打包时要同时带上 `third_party/pywechat/pyweixin`、`third_party/pywechat/pywechat` 和 `logger/logging.conf`。
- 打包后需要验证 `dist/wxclient-gui.exe` 能启动并正常显示 GUI。
- GUI 相关改动后要确认启动图会主动关闭，避免卡住不消失。
- 配置 JSON 读取要兼容 UTF-8 与 UTF-8-SIG，避免 BOM 报错。
- 修改 GUI、启动路径、打包入口或 vendor 目录时，优先跑 `python scripts/build_gui_onefile.py --dry-run`，必要时再完整构建验证。
- 打包或启动异常时，优先排查 `pyweixin`、`logger/logging.conf`、相对路径和 `sys._MEIPASS` 兼容。

## 稳定性边界

- 不承诺跨微信版本 100% 稳定。
- 优先支持 Windows 10/11、Python 3.9+、微信 4.1.9.x。
- UI 改版、微信重启、登录失效、窗口焦点异常都要进入可恢复失败状态。
- 任务失败要记录原因、截图/诊断信息、重试次数和下一次处理时间。
- 若需要修改会影响真实动作，请先保证有 `dry_run`、确认机制或清晰回退路径。

## 多微信规则

- MVP 只支持单 Windows 用户会话中的单个微信账号。
- 多账号优先采用一账号一 Windows 会话、虚拟机或独立客户端实例。
- 不要在同一物理桌面并发操作多个微信窗口。
- 后续多账号必须绑定 `account_id -> process_id/window_handle`，且每账号串行执行。

## 开发顺序

1. 先打通 `uia_unlock`。
2. 再封装 `wechat_api.send_message()`，并打通文件传输助手。
3. 再实现 SQLite 任务表和单消费者队列。
4. 再实现好友申请 `dry_run` 检查和 UI 展示。
5. 再实现白名单策略下的自动同意。
6. 最后实现消息扫描、`agentbridge`、大模型回复。

## 变更要求

- 做完代码改动后，先运行相关检查，再提交 Git。
- 如果工作区已有与当前任务无关的修改，不要回退它们。
- 编辑文件时优先使用 `apply_patch`。
- 涉及打包、启动路径、编码或第三方 vendor 的改动，至少要做一次实际验证。
