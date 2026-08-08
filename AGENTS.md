# AGENTS.md

本仓库只维护一个 Windows 微信 MCP 服务，负责微信 UI 自动化和串行任务执行。仓库不
维护 GUI、托盘、独立调用客户端、定时调度或大模型回复模块。

## 文档与源码

- MCP 部署、鉴权、工具和扩展：`docs/mcp-server.md`
- MCP 服务：`wxclient/mcp_server/`
- 微信动作封装：`wxclient/wechat_api/`
- 只读 UIA 探测：`wxclient/uia_probe/`
- 串行任务队列：`wxclient/queue/`
- 第三方 pyweixin：`third_party/pywechat/pyweixin/`
- MCP 入口：`scripts/wxclient_mcp.py`
- 单体 EXE 构建：`scripts/build_mcp_onefile.py`

## 外部文档规则

涉及库、框架、SDK、API、CLI 或云服务时，先使用 ctx7 获取当前文档：

1. `npx ctx7@latest library <name> "<完整问题>"`
2. 选择官方且匹配的 `/org/project` ID。
3. `npx ctx7@latest docs <libraryId> "<完整问题>"`

每个问题最多执行 3 条 ctx7 命令，不在查询中放入密钥。ctx7 不用于业务逻辑重构、
纯代码审查或从零编写普通脚本。配额不足时明确提示登录或配置 `CONTEXT7_API_KEY`。

## 架构约束

- 所有微信 UI 动作必须由 `mcp_server -> queue -> wechat_api` 调用。
- 微信 UI 自动化必须由单消费者 worker 串行执行。
- MCP 工具只负责校验参数和投递任务，不得直接操作微信窗口。
- MCP 服务只提供工具，不实现对话理解、回复生成或工作流编排。
- 同类型 `PENDING` 任务不重复入队。
- MVP 只支持一个 Windows 交互式用户会话中的一个微信账号。
- 不在 Session 0 的 Windows Service 中运行 UI 自动化。

## UIA 规则

- `probe_wechat_uia()` 只能读取状态，不启动、关闭、重启、切换或解锁微信。
- 业务动作前必须确认微信已登录且 UIA 可用。
- 看到 `mmui::LoginWindow` 时阻止任务并提示登录。
- 不实现 DLL 注入、进程伪装、屏幕阅读器标志修改或其他绕过微信限制的方案。

## 真实动作安全

- 加好友、同意好友、发消息和发朋友圈默认 `dry_run=true`。
- 真实动作必须同时由服务端开启 `--allow-real-actions`，并由单次 MCP 调用提供确认短语。
- MCP 上传只接受 Base64 内容并返回 `media_id`；工具不接受调用方指定任意 Windows 路径。
- 失败任务要保留原因、状态和重试次数，供 MCP 工具查询。

## 扩展方式

- 新 MCP 工具组实现 `McpToolProvider.register()`，通过 `create_mcp_server()` 注册。
- 新微信能力先加入 `wechat_api` 封装，再增加任务类型和 worker 分发，最后暴露 MCP 工具。
- 不恢复 GUI、调用客户端、定时器或大模型模块；若产品方向变化，应单独讨论架构。

## 测试与打包

- 修改后运行 `python -m ruff check wxclient scripts tests`。
- 运行 `python -m unittest discover -s tests -v`。
- 修改入口、依赖、vendor 或打包脚本时，先运行
  `python scripts/build_mcp_onefile.py --dry-run`，再实际构建并验证 `dist/wxclient-mcp.exe`。
- 打包必须包含 `third_party/pywechat/pyweixin` 和其所需的 `logger/logging.conf`。
- 完成并验证后提交 Git；不得回退用户无关的工作区修改。
