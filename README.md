# wxauto-client

Windows 微信自动化客户端，基于 `third_party/pywechat/pyweixin` 和 UIA 解锁能力构建。当前版本以任务队列为核心，默认 `dry_run`，只有显式开启真实动作才会操作微信。

## 现在能做什么

- 添加好友
- 检查并同意好友申请
- 发送消息
- 监听聊天消息并交给大模型回复
- 读取可见消息 / 未读消息
- 发送朋友圈
- 提供本地 HTTP API、CLI、GUI 三种入口

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

启动本地服务：

```powershell
wxclient serve
```

或：

```powershell
python -m wxclient.app.cli serve
```

启动 GUI：

```powershell
wxclient gui
```

GUI 打开后会自动拉起本地 HTTP 服务 `http://127.0.0.1:8765`，但任务真正执行仍然要在首页点击“启动服务”。如果只是看日志或改配置，不启动 worker 也没关系。

## 常用命令

UIA 探测与解锁：

```powershell
wxclient probe-uia
wxclient unlock-uia
wxclient unlock-uia --restart
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

## 本地 HTTP API

默认监听 `http://127.0.0.1:8765`，常用接口：

- `GET /health`
- `GET /tasks`
- `POST /tasks/add-friend`
- `POST /tasks/send-message`
- `POST /tasks/check-new-friends`
- `POST /tasks/accept-new-friends`
- `POST /tasks/listen-chat`
- `POST /tasks/listen-chats`

如果启用了 `webhook_token`（历史命名，当前作为 HTTP API 的鉴权 token），请求头需要：

```text
Authorization: Bearer <token>
```

## 配置

默认配置文件：

- GUI：`.wxclient/settings.json`
- 数据库：`.wxclient/wxclient.db`
- 日志：`.wxclient/logs/`

支持 `WXCLIENT_*` 环境变量，也支持 JSON 配置文件。常用项包括：

- `data_dir`
- `database_path`
- `host`
- `port`
- `wechat_path`
- `dry_run`
- `webhook_token`（HTTP 鉴权 token）
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
- HTTP API、定时器、UI 只负责投递任务，不能直接碰微信窗口
- 业务层只通过 `wxclient/wechat_api/` 封装调用 `pyweixin`
- UIA 解锁只走 `ensure_wechat_uia()`
- 真实加好友、同意好友、发消息、发朋友圈都应保留 `dry_run` 或确认机制

## 文档

- [`docs/windows-client-architecture.md`](docs/windows-client-architecture.md)
- [`docs/pyweixin-uia-integration.md`](docs/pyweixin-uia-integration.md)
- [`docs/first-version-usage.md`](docs/first-version-usage.md)
