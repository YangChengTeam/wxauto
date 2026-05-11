# Webhook / HTTP 服务器说明

## 结论

已实现。

当前仓库里的 Webhook 不是占位设计，而是已经落地的本地 HTTP 服务，技术栈是：

- `FastAPI`
- `uvicorn`
- `SQLite + SQLAlchemy Core` 任务存储
- 单消费者 `OperationWorker` 串行执行微信动作

对应代码位置：

- `wxclient/app/cli.py`
- `wxclient/trigger/http_server.py`
- `wxclient/queue/store.py`
- `wxclient/queue/worker.py`

## 它的职责

HTTP 服务器只负责两件事：

1. 接收外部 HTTP 请求
2. 把请求转换为任务写入本地任务库

它**不会**在请求线程里直接操作微信窗口，也不会绕过队列直接调用 `pyweixin`。

这符合当前项目的核心约束：

- 微信 UI 自动化动作必须串行执行
- Webhook、定时器、UI、Agent 只能投递任务，不能直接操作微信窗口

## 启动方式

使用：

```powershell
python -m wxclient.app.cli serve
```

或：

```powershell
wxclient serve
```

`serve` 启动时会按下面顺序工作：

1. 读取配置
2. 初始化 `TaskStore`
3. 初始化 `WeChatApi`
4. 先做一次 `bootstrap_wechat_uia(...)`
5. 启动串行 `OperationWorker`
6. 启动 `Scheduler`
7. 启动 `uvicorn` HTTP 服务

也就是说，`serve` 不是单独开一个 HTTP 端口这么简单，它还会一起拉起：

- 微信 UIA 启动检查/解锁
- 后台任务 worker
- 定时好友申请检查任务

如果 UIA 启动检查失败，服务会在绑定端口前直接退出，不会进入“HTTP 正常但任务根本跑不起来”的假启动状态。

## 默认监听地址

默认配置来自 `Settings`：

- `host = "127.0.0.1"`
- `port = 8765`

因此默认地址是：

```text
http://127.0.0.1:8765
```

如果你修改了配置文件或 `WXCLIENT_*` 环境变量，监听地址会跟着变化。

## 整体执行链路

```text
外部系统 / 脚本 / curl / PowerShell
                |
                v
        FastAPI 路由
                |
                v
       TaskStore.create_task(...)
                |
                v
    SQLite tasks 表（status=pending）
                |
                v
 OperationWorker.run_once()/run_forever()
                |
                v
 WeChatApi / AgentBridge / reply_flow
                |
                v
 ensure_wechat_uia() -> pyweixin -> 微信客户端
```

这个设计的直接效果是：

- HTTP 请求返回很快，因为只负责入队
- 真正执行是否成功，要看后续任务状态
- 多个 Webhook 并发到达时，微信动作仍然会被 worker 串行处理

## 当前已实现的 HTTP 接口

### 健康检查

```http
GET /health
```

返回：

```json
{
  "ok": true
}
```

用途：

- 检查进程是否存活
- 检查端口是否已监听

注意：

- 这里只代表 HTTP 服务在线
- 不代表微信一定可登录、UIA 一定可用、队列一定没有阻塞任务

### 查询任务列表

```http
GET /tasks
```

返回：

```json
{
  "tasks": [
    {
      "id": "uuid",
      "type": "send_message",
      "status": "pending",
      "account_id": "default",
      "attempts": 0,
      "created_at": "2026-05-11T05:00:00+00:00",
      "updated_at": "2026-05-11T05:00:00+00:00",
      "last_error": null,
      "result": null
    }
  ]
}
```

用途：

- 查看最近任务
- 轮询某个 `task_id` 的执行结果
- 排查 `blocked` / `failed` 任务

注意：

- 当前代码里 `GET /tasks` **没有**加 Bearer token 校验
- 如果你希望它也受保护，需要在该路由上补 `Depends(require_token)`

### 添加好友

```http
POST /tasks/add-friend
```

请求体：

```json
{
  "number": "13800138000",
  "greetings": "你好",
  "remark": "客户来源A",
  "account_id": "default",
  "dry_run": true
}
```

入队任务类型：

```text
add_friend
```

### 发送消息

```http
POST /tasks/send-message
```

请求体：

```json
{
  "friend": "文件传输助手",
  "text": "测试",
  "account_id": "default",
  "dry_run": true
}
```

入队任务类型：

```text
send_message
```

### 检查好友申请

```http
POST /tasks/check-new-friends
```

请求体：

```json
{
  "verify": false,
  "limit": 8,
  "account_id": "default",
  "dry_run": true
}
```

入队任务类型：

```text
check_new_friends
```

说明：

- `verify=false` 表示只检查，不自动同意
- `verify=true` 表示检查时直接尝试同意
- 在 `dry_run=true` 时，即使你传了 `verify=true`，真正执行层也会把它降成只读检查

### 同意好友申请

```http
POST /tasks/accept-new-friends
```

请求体：

```json
{
  "limit": 8,
  "account_id": "default",
  "dry_run": true
}
```

入队任务类型：

```text
accept_new_friends
```

说明：

- 该接口本质上是单独包装了一次“带 `verify=True` 的好友申请处理”

### 监听单个聊天

```http
POST /tasks/listen-chat
```

请求体：

```json
{
  "friend": "测试群",
  "duration": "30s",
  "select_sender": false,
  "save_file": false,
  "save_media": false,
  "account_id": "default",
  "dry_run": true
}
```

入队任务类型：

```text
scan_messages
```

说明：

- `duration` 支持类似 `30s`、`1min`、`1h`
- `select_sender` 控制是否尝试识别消息发送者
- `save_file` / `save_media` 用于文件、图片、视频类消息

### 监听多个聊天

```http
POST /tasks/listen-chats
```

请求体：

```json
{
  "friends": ["客户A", "客户B", "测试群"],
  "duration": "30s",
  "auto_reply": true,
  "reply_all": false,
  "account_id": "default",
  "dry_run": true
}
```

入队任务类型：

```text
watch_chats
```

说明：

- `friends` 不能为空，否则返回 `422`
- `auto_reply=true` 时会调用配置的 Agent 自动回复
- `reply_all=false` 时默认只回本轮最新消息
- 当前 HTTP 路由里 `select_sender` 没有开放，内部固定为 `False`

## 通用请求字段

所有 `POST /tasks/*` 请求都继承了两个通用字段：

- `account_id: str | null`
- `dry_run: bool | null`

行为如下：

### `account_id`

- 不传时，使用 `settings.default_account_id`
- 目前仓库整体仍以单账号视角为主，但字段已经预留

### `dry_run`

- 不传时，使用服务端配置 `settings.dry_run`
- 传 `true` 时，只入队模拟任务，不做真实微信动作
- 传 `false` 时，worker 才会尝试真实操作微信

这意味着：

- 如果服务端全局配置本来就是 `dry_run=true`
- 而你请求里又不传 `dry_run`

那么该任务仍然是模拟执行

## 返回格式

所有任务型 POST 接口当前都返回：

```json
{
  "task_id": "uuid",
  "status": "pending"
}
```

HTTP 状态码：

```text
202 Accepted
```

这里的含义是：

- 服务只保证“请求已收下并入队”
- 不保证“微信动作已经执行成功”

执行结果需要靠后续查询：

```http
GET /tasks
```

## 鉴权方式

当前实现使用可选的 Bearer Token。

配置：

```json
{
  "webhook_token": "change-me"
}
```

请求头：

```text
Authorization: Bearer change-me
```

行为：

- 如果 `webhook_token` 为空，则所有 POST 路由都不校验
- 如果 `webhook_token` 有值，而请求头不匹配，则返回：

```http
401 Unauthorized
```

响应体：

```json
{
  "detail": "unauthorized"
}
```

注意：

- 当前只有 `POST /tasks/*` 路由用了这个校验
- `GET /health` 和 `GET /tasks` 不做 token 校验

## 任务状态说明

任务表中的状态来自 `TaskStatus`：

- `pending`
- `running`
- `succeeded`
- `failed`
- `blocked`
- `cancelled`

当前 HTTP / worker 流程里最常见的是：

### `pending`

已入队，等待 worker 拾取

### `running`

worker 已开始处理

### `succeeded`

执行成功，`result` 会写入结果摘要

### `failed`

执行过程中发生普通异常，且最终重试失败

### `blocked`

执行过程中抛出 `WeChatApiError`，常见于：

- UIA 未解锁
- 微信窗口状态异常
- 微信界面元素未找到

`blocked` 往往意味着不是简单重试就能解决，需要看微信当前状态或 UIA 诊断

### `cancelled`

模型里已定义，但当前仓库还没有取消任务的 HTTP 接口或 worker 逻辑

## 重试行为

当前 worker 的重试策略比较简单：

- 普通异常：会重试，最多 3 次
- `WeChatApiError`：不会自动重试，而是直接标记为 `blocked`

并且当前实现里：

- 没有指数退避
- 没有延迟重试时间
- 失败后若可重试，基本会在下一轮轮询马上再试

这对本地调试够用，但如果以后要接更正式的外部系统，建议后续补：

- 重试间隔
- 幂等键
- 请求来源字段
- 失败分类和报警

## Scheduler 和 Webhook 的关系

启动 `serve` 时，除了 HTTP 服务还会一起启动 `Scheduler`。

当前 scheduler 会：

- 立即入队一次 `check_new_friends`
- 然后按 `friend_check_interval_seconds` 周期继续入队

所以你在 `GET /tasks` 里看到的任务，不一定都是外部 Webhook 触发的，也可能是定时器生成的。

## 当前未暴露成 HTTP 的能力

虽然 worker 已支持更多任务类型，但 HTTP 服务器目前**没有**给它们开放路由，例如：

- `reply_chat`
- `reply_unread`
- `read_visible_messages`
- `check_unread_messages`
- `post_moments`

也就是说：

- CLI 和 GUI 能做的事
- 不一定都能直接通过当前 Webhook 调用

如果后续要补接口，最直接的地方就是：

- `wxclient/trigger/http_server.py`

按现有风格新增：

1. Pydantic 请求模型
2. FastAPI 路由
3. `store.create_task(...)`

## 目前这个 HTTP 服务器“不做什么”

当前实现还没有这些能力：

- 没有任务取消接口
- 没有单任务查询接口，例如 `GET /tasks/{task_id}`
- 没有幂等 key
- 没有请求来源签名校验
- 没有速率限制
- 没有 CORS 配置
- 没有 WebSocket / 推送通知
- 没有把 `/tasks` 也纳入鉴权

所以它现在更适合：

- 本机使用
- 内网单机调度
- 由你自己掌控的脚本或服务发起请求

而不是直接裸露到公网

## PowerShell 示例

### 发送消息

```powershell
Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:8765/tasks/send-message `
  -ContentType "application/json" `
  -Body '{"friend":"文件传输助手","text":"测试","dry_run":true}'
```

### 自动回复监听多个会话

```powershell
Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:8765/tasks/listen-chats `
  -ContentType "application/json" `
  -Body '{"friends":["客户A","测试群"],"duration":"30s","auto_reply":true,"reply_all":false,"dry_run":true}'
```

### 开启 Bearer Token 后发送消息

```powershell
Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:8765/tasks/send-message `
  -Headers @{ Authorization = "Bearer change-me" } `
  -ContentType "application/json" `
  -Body '{"friend":"文件传输助手","text":"测试","dry_run":true}'
```

### 查看任务

```powershell
Invoke-RestMethod -Method Get `
  -Uri http://127.0.0.1:8765/tasks
```

## 排查建议

### `GET /health` 正常，但任务一直不执行

先检查：

- `serve` 是否是完整启动，而不是中途因为 UIA bootstrap 失败退出
- `GET /tasks` 中任务是否一直停在 `pending`
- 日志目录 `.wxclient/logs/` 下是否有错误信息

### POST 返回了 `202`，但最终没发出去

这属于“已入队，但执行失败”，排查顺序建议是：

1. `GET /tasks` 看 `status`
2. 看 `last_error`
3. 看 `result`
4. 再看微信是否登录、UIA 是否可用

### 收到 `401 unauthorized`

说明：

- 服务端配置了 `webhook_token`
- 你的请求头没有带 `Authorization: Bearer <token>`
- 或 token 不匹配

### 收到 `422`

常见原因：

- JSON 字段缺失
- 字段类型不对
- `listen-chats` 的 `friends` 为空列表或空字符串

## 相关文件

- `wxclient/app/cli.py`
- `wxclient/trigger/http_server.py`
- `wxclient/trigger/scheduler.py`
- `wxclient/queue/store.py`
- `wxclient/queue/worker.py`
- `docs/first-version-usage.md`

