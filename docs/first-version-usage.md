# 第一版使用说明

第一版已经具备工程骨架和真实微信动作入口。默认仍是 dry-run，只有显式加
`--real` 才会尝试操作微信。

## 能力

- SQLAlchemy Core + SQLite 任务表。
- 单消费者微信操作 worker。
- APScheduler 定时好友申请检查任务。
- Typer CLI。
- Pydantic Settings 配置。
- pywinauto UIA 探测：识别 `mmui::MainWindow`、`mmui::LoginWindow` 和节点暴露状态。
- 只读 UIA 状态探测；不提供 UIA 解锁或微信重启入口。
- pyweixin 真实动作：发送消息、添加好友、检查/同意好友申请。

## 数据目录

默认数据目录：

```text
.wxclient/wxclient.db
```

## UIA 检查

只读探测：

```powershell
python -m wxclient.app.cli probe-uia
```

该命令只读取当前窗口和 UIA 树。UIA 不可用时，程序不会尝试解锁、启动、关闭或重启微信，需要用户手动处理微信状态后再次探测。

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

## 配置

```powershell
python -m wxclient.app.cli worker --config config.local.json
```

示例：

```json
{
  "data_dir": ".wxclient",
  "default_account_id": "default",
  "wechat_path": "C:/Program Files/Tencent/Weixin/Weixin.exe",
  "dry_run": true,
  "friend_check_interval_seconds": 60,
  "message_scan_interval_seconds": 15
}
```
