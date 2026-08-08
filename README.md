# wxauto-client

Windows 微信 MCP 远程控制与部署说明见 [docs/mcp-server.md](docs/mcp-server.md)。

Windows 微信自动化客户端，基于 `third_party/pywechat/pyweixin` 和只读 UIA 探测构建。当前版本以任务队列为核心，默认 `dry_run`，只有显式开启真实动作才会操作微信。

## 现在能做什么

- 添加好友
- 检查并同意好友申请
- 发送消息
- 监听聊天消息并交给大模型回复
- 读取可见消息 / 未读消息
- 发送朋友圈
- 提供 CLI 和 GUI 两种入口

## 运行要求

- Windows 10 / 11
- Python 3.9+
- 微信 PC 端
- 推荐先阅读 [`docs/first-version-usage.md`](docs/first-version-usage.md)

## 安装

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -e .
```

## 快速开始

启动 GUI：

```powershell
wxclient gui
```

GUI 打开后不会自动执行微信动作；任务真正执行仍然要在首页点击“启动服务”。如果只是看日志或改配置，不启动 worker 也没关系。

GUI 启动时只读探测微信 UIA 树是否暴露，不会修改屏幕阅读器标志、创建辅助功能常驻会话，也不会启动、关闭、重启或切换微信窗口。点击“停止服务”会等待 worker 线程真正退出，退出后才恢复为“启动服务”。

## 常用命令

UIA 探测：

```powershell
wxclient probe-uia
```

任务入队（默认 dry-run）：

```powershell
wxclient send-message 文件传输助手 "测试"
wxclient add-friend 13800138000 --greetings "你好"
wxclient check-new-friends
wxclient reply-chat 文件传输助手 --latest-visible
wxclient listen-chats 文件传输助手 测试群 --auto-reply
```

真实执行：

```powershell
wxclient send-message 文件传输助手 "真实发送测试" --real
wxclient add-friend 13800138000 --greetings "你好" --real
```

## 配置

默认配置文件：

- GUI：`.wxclient/settings.json`
- 数据库：`.wxclient/wxclient.db`
- 日志：`.wxclient/logs/`

支持 `WXCLIENT_*` 环境变量，也支持 JSON 配置文件。常用项包括：

- `data_dir`
- `database_path`
- `wechat_path`
- `dry_run`
- `llm_api_key`

## GUI

GUI 包含这些页面：

- 日志
- 好友检测
- 未读回复
- 消息监听
- 添加好友
- 朋友圈
- 设置

GUI 里常用的自动化入口都在左侧，先在首页点“启动服务”，再去好友检测、未读回复、消息监听、添加好友、朋友圈这些页面触发任务。

## UIA 探测

程序只通过 pywinauto 读取当前微信窗口和 UIA 树：

- 识别 `mmui::MainWindow` / `mmui::LoginWindow` 和 UIA 节点数。
- 不修改 Windows 辅助功能状态，不创建 native COM/WinEvent 常驻会话。
- 不自动启动、关闭、重启微信，也不把微信切到前台。
- UIA 不可用时阻止 worker 或当前业务动作，并提示用户手动处理。

GUI 日志会输出只读 UIA 诊断摘要，包括当前用户、管理员状态、前台窗口、微信候选窗口和 probe 结果。

## 打包成单体 EXE

先安装打包依赖：

```powershell
pip install -e ".[build]"
```

然后执行：

```powershell
python scripts/build_gui_onefile.py
```

产物会输出到 `dist/wxclient-gui.exe`，并带有程序图标和启动图。打包脚本会把 `third_party/pywechat/pyweixin`、`third_party/pywechat/pywechat` 和 `logger/logging.conf` 一并带入 onefile 包里。如果只想先看命令，不真正构建，可以加 `--dry-run`。

注意：

- onefile 模式会先解压到临时目录，首次启动比 `onedir` 慢一些
- 如果旧版 exe 报 `pyweixin is unavailable` 或 `logger/logging.conf doesn't exist`，请重新用当前脚本打包
- 打包后的 GUI 仍然默认 `dry_run`，需要在设置或命令里显式开启真实执行

## 架构约束

- 所有真实微信动作都必须先进入队列，再由单消费者 worker 串行执行
- 定时器、UI 只负责投递任务，不能直接碰微信窗口
- 业务层只通过 `wxclient/wechat_api/` 封装调用 `pyweixin`
- UIA 状态检查只走 `wxclient.uia_probe.probe_wechat_uia()`，且必须保持只读
- UIA 不可用时直接阻止动作，不在探测层启动、关闭、重启或切换微信窗口
- 真实加好友、同意好友、发消息、发朋友圈都应保留 `dry_run` 或确认机制

## 文档

- [`docs/windows-client-architecture.md`](docs/windows-client-architecture.md)
- [`docs/pyweixin-uia-integration.md`](docs/pyweixin-uia-integration.md)
- [`docs/first-version-usage.md`](docs/first-version-usage.md)
