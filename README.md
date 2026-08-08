# wxauto-mcp

Windows 微信 MCP 服务，提供 UIA 探测和微信自动化工具，并通过单消费者队列串行执行。

## 能力

- 只读探测微信 UIA 可用性
- 加好友、检查或同意好友申请
- 发送消息、监听单个或多个会话、读取可见消息
- 上传媒体、发布朋友圈、读取最近朋友圈验证结果
- SQLite 任务记录、同类待执行任务去重、单 worker 串行操作微信
- Streamable HTTP、Bearer Token、真实动作双重确认

本项目只提供 MCP 服务，不包含 GUI、托盘、独立调用客户端、定时调度或大模型模块。

## 安装与启动

要求 Windows 10/11、Python 3.10+、已登录的微信 PC 端，以及未锁屏的交互式桌面。

```powershell
python -m pip install -e .
python -m wxclient.mcp_server --allow-real-actions
```

默认端点是 `http://127.0.0.1:8765/mcp`。远程部署、安全配置、工具参数和扩展方式见
[MCP 服务文档](docs/mcp-server.md)。

## 打包

```powershell
python -m pip install -e ".[build]"
python scripts\build_mcp_onefile.py --dry-run
python scripts\build_mcp_onefile.py
```

产物为 `dist\wxclient-mcp.exe`。

## 保留的架构边界

```text
wxclient/
  mcp_server/   # HTTP MCP、鉴权、工具注册、媒体上传
  queue/        # SQLite 任务与单消费者 worker
  wechat_api/   # pyweixin 二次封装
  uia_probe/    # 只读 UIA 状态探测
  config/       # Windows 微信运行参数
```

所有微信 UI 动作必须通过 `mcp_server -> queue -> wechat_api`，不能在 MCP 工具函数中
直接操作窗口。UIA 探测不会启动、关闭、重启或解锁微信。
