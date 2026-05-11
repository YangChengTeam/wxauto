# 第一版使用说明

第一版已经具备工程骨架和真实微信动作入口。默认仍是 dry-run，只有显式加
`--real` 或请求体传 `dry_run:false` 才会尝试操作微信。

## 能力

- SQLAlchemy Core + SQLite 任务表。
- 单消费者微信操作 worker。
- FastAPI + uvicorn 本地 HTTP API。
- APScheduler 定时好友申请检查任务。
- Typer CLI。
- Pydantic Settings 配置。
- pywinauto UIA 探测：识别 `mmui::MainWindow`、`mmui::LoginWindow` 和节点暴露状态。
- Narrator 解锁入口：可选重启微信。
- pyweixin 真实动作：发送消息、添加好友、检查/同意好友申请。

## 启动服务

```powershell
python -m wxclient.app.cli serve
```

默认监听：

```text
http://127.0.0.1:8765
```

默认数据目录：

```text
.wxclient/wxclient.db
```

## UIA 检查与解锁

只读探测：

```powershell
python -m wxclient.app.cli probe-uia
```

不重启微信，只启动 Narrator 尝试解锁：

```powershell
python -m wxclient.app.cli unlock-uia
```

确认可以关闭/重启微信后，再使用：

```powershell
python -m wxclient.app.cli unlock-uia --restart
```

## CLI dry-run

```powershell
python -m wxclient.app.cli add-friend 13800138000 --greetings "你好"
python -m wxclient.app.cli send-message 文件传输助手 "测试"
python -m wxclient.app.cli check-new-friends
python -m wxclient.app.cli list-tasks
python -m wxclient.app.cli worker
```

## CLI 真实动作

真实发送消息：

```powershell
python -m wxclient.app.cli send-message 文件传输助手 "真实发送测试" --real
python -m wxclient.app.cli worker
```

真实添加好友：

```powershell
python -m wxclient.app.cli add-friend 13800138000 --greetings "你好" --real
python -m wxclient.app.cli worker
```

真实检查好友申请：

```powershell
python -m wxclient.app.cli check-new-friends --real
python -m wxclient.app.cli worker
```

真实同意好友申请：

```powershell
python -m wxclient.app.cli accept-new-friends --limit 3 --real
python -m wxclient.app.cli worker
```

注意：`--real` 只是入队真实任务，实际执行仍由 worker 串行完成。

## HTTP API

```http
POST /tasks/add-friend
POST /tasks/send-message
POST /tasks/check-new-friends
POST /tasks/accept-new-friends
POST /tasks/listen-chat
POST /tasks/listen-chats
GET  /tasks
GET  /health
```

详细说明见：

```text
docs/http-task-api.md
```

示例：

```powershell
Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:8765/tasks/send-message `
  -ContentType "application/json" `
  -Body '{"friend":"文件传输助手","text":"测试","dry_run":true}'
```

## 配置

```powershell
python -m wxclient.app.cli serve --config config.local.json
```

示例：

```json
{
  "data_dir": ".wxclient",
  "host": "127.0.0.1",
  "port": 8765,
  "webhook_token": "change-me",
  "default_account_id": "default",
  "wechat_path": "C:/Program Files/Tencent/Weixin/Weixin.exe",
  "dry_run": true,
  "friend_check_interval_seconds": 60,
  "message_scan_interval_seconds": 15
}
```

设置 `webhook_token`（兼容保留的配置名）后，POST 请求需要：

```text
Authorization: Bearer change-me
```
