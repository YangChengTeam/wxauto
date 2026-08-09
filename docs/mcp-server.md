# Windows 微信 MCP 服务

`wxclient.mcp_server` 是本项目唯一的产品入口。它把微信动作放入 SQLite 任务队列，
由一个 worker 串行执行。服务只提供 MCP 工具，不包含大模型或自动回复逻辑。

## 能力

- `wx_probe_uia`：只读探测微信登录与 UIA 状态。
- `wx_upload_media`：以 Base64 上传受支持的媒体或文档，返回 `media_id`。
- `wx_search_contacts`：按名称搜索现有联系人、群聊、服务号和公众号，不发送消息。
- `wx_get_contact_profile`：读取联系人资料。
- `wx_list_group_members`：读取群成员名称；同时返回群人数、已读取人数和 `complete`，避免把
  微信未暴露完整列表时的部分结果误判为完整结果。
- `wx_add_friend`：发送好友申请。
- `wx_check_new_friends`：检查好友申请，可选择同意。
- `wx_send_message`：发送文字消息。
- `wx_send_files`：把已上传文件发送给好友或群聊，可附带文字。
- `wx_listen_messages`：监听一个会话，不自动回复。
- `wx_watch_chats`：依次监听多个会话，不生成或发送回复。
- `wx_list_chat_sessions`：列出主窗口当前会话列表中的准确名称。
- `wx_get_unread_messages`：汇总未读会话、数量和预览，不打开会话、不标记已读。
- `wx_read_chat_history`：读取指定好友或群聊的最近聊天记录。
- `wx_read_visible_messages`：读取当前可见聊天记录。
- `wx_post_moments`：用 `media_id` 发布朋友圈。
- `wx_read_recent_moments`：读取当天最近朋友圈，用于发布后验证。
- `wx_get_task` / `wx_list_tasks`：查询异步任务和失败原因。

所有微信 UI 动作仍经过 `wxclient/wechat_api/` 和单消费者任务队列。新增工具组时实现
`McpToolProvider.register()`，再与 `WeChatToolProvider` 一起传给 `create_mcp_server()`。

## 队列可靠性

- 任务使用账号、类型和规范化 payload 生成幂等键。内容相同且仍处于 `PENDING` 或
  `RUNNING` 的任务会复用现有任务；同类型但参数不同的任务不会再被错误合并。
- SQLite 通过原子更新领取任务，并为任务和微信 UI worker 保存可续期租约。因此即使误启
  多个 MCP 进程，同一时刻也只有一个 worker 可以操作微信界面。
- 服务启动和领取任务时会恢复租约过期的任务。只读任务可以在重试上限内按指数退避重试；
  真实写操作如果执行结果未知，会直接标记失败并设置 `retry_suppressed=true`，避免重复发送。
- `wx_watch_chats` 按短时间片执行，每个时间片结束后把续作放回队列，让发送消息等新任务
  获得执行机会，同时仍保持所有 UI 动作由单消费者串行完成。

`wxclient.wechat_api.service` 只保留兼容导出；统一门面和驱动编排位于 `api.py`，UIA 实现
位于 `uia.py`，可选视觉驱动位于 `omni.py`，异常类型位于 `errors.py`。

## 本机安装与启动

需要 Python 3.10+。

```powershell
python -m pip install -e .
python -m wxclient.mcp_server `
  --config .wxclient\settings.json `
  --allow-real-actions
```

默认地址为 `http://127.0.0.1:8765/mcp`。也可以生成独立控制台 EXE：

```powershell
python scripts\build_mcp_onefile.py --dry-run
python scripts\build_mcp_onefile.py
```

输出为 `dist\wxclient-mcp.exe`。

## 日志

服务日志使用中文，并包含服务启动/停止、UIA 探测、任务类型、任务短 ID、执行结果和失败
原因。控制台重定向时统一写入 UTF-8，可直接保存：

```powershell
.\dist\wxclient-mcp.exe --allow-real-actions `
  *> .wxclient\mcp.log
```

MCP SDK 和 Uvicorn 的常规英文启动提示已隐藏；第三方库抛出的原始异常文本可能仍保留，
便于排查具体错误。

## Windows 远程部署

UI 自动化必须运行在已登录、未锁屏的交互式 Windows 桌面中。不要把进程放到 Windows
Service 的 Session 0；可以用登录后的计划任务启动。

在 Windows 上设置一个随机长令牌。不要把令牌写进仓库或命令行历史：

```powershell
$env:WXCLIENT_MCP_AUTH_TOKEN = "<random-long-token>"
$env:WXCLIENT_MCP_PUBLIC_URL = "http://192.168.1.20:8765"
python -m wxclient.mcp_server `
  --config C:\wxauto\.wxclient\settings.json `
  --host 0.0.0.0 `
  --port 8765 `
  --allowed-host "192.168.1.20:*" `
  --allow-real-actions
```

其中 `192.168.1.20` 应替换为 Windows 主机的实际地址或域名。非回环监听如果没有
`WXCLIENT_MCP_AUTH_TOKEN`，服务会拒绝启动。DNS 重绑定保护也要求显式配置允许的 Host。

调用方使用 Streamable HTTP MCP，通用连接配置如下：

```json
{
  "mcpServers": {
    "windows-wechat": {
      "type": "http",
      "url": "http://192.168.1.20:8765/mcp",
      "headers": {
        "Authorization": "Bearer ${WXCLIENT_MCP_AUTH_TOKEN}"
      }
    }
  }
}
```

不要把未加密 HTTP 直接暴露到公网。跨公网时应使用 VPN，或在 Windows MCP 前放置
TLS 反向代理，并限制来源 IP。

## 真实动作确认

涉及真实 UI/写入的工具默认 `dry_run=true`。真实动作需要同时满足：

1. 服务使用 `--allow-real-actions` 启动；
2. 工具参数设置 `dry_run=false`；
3. 工具参数设置 `confirmation="EXECUTE_REAL_ACTION"`。

`wx_probe_uia`、`wx_search_contacts`、`wx_get_contact_profile`、`wx_list_group_members`、
`wx_get_unread_messages`、`wx_read_chat_history`、`wx_get_task` 和 `wx_list_tasks` 是只读工具，
不要求真实动作确认；除 UIA 探测和任务查询外，微信读取操作仍通过串行 worker 执行，
避免与其他微信动作冲突。

朋友圈或文件发送的远程调用先执行 `wx_upload_media`，再把返回的 `media_id` 传给
`wx_post_moments` 或 `wx_send_files`。服务只解析自己的上传目录，不接受调用方指定任意
Windows 文件路径。可上传图片、视频，以及 PDF、Office 文档、文本、CSV、JSON、ZIP、7z；
不接受可执行文件和脚本。

## 扩展工具组

```python
from mcp.server.fastmcp import FastMCP

from wxclient.mcp_server import McpServerSettings, create_mcp_server
from wxclient.mcp_server.providers import WeChatToolProvider


class DiagnosticsProvider:
    def register(self, mcp: FastMCP, runtime) -> None:
        @mcp.tool(name="wx_runtime_status", structured_output=True)
        async def wx_runtime_status() -> dict:
            return {"worker_running": runtime.worker.running}


mcp = create_mcp_server(
    settings=McpServerSettings(),
    providers=[WeChatToolProvider(), DiagnosticsProvider()],
)
```

扩展模块只负责注册工具；微信窗口动作必须继续投递到 `WxMcpRuntime.submit()`，不能在
工具函数里绕过队列直接调用 pyweixin。
