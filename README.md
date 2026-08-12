# wxauto-gateway

Windows 微信自动化网关，服务于劳小智招聘业务：把入站微信消息翻译成一次 AI 对话轮，
再把 AI 产出的回复翻译回微信消息发出去。所有微信动作通过单消费者队列串行执行。

## 能力

- 入站轮询取消息、按 `msg_id` 去重、连发去抖聚合、同会话严格串行
- 连劳小智 AI 端（WS 客户端：换票 → `session.start`/`resume` → `message.send` → 取卡）
- 确定性守门：拒绝词表、频次上限、工作时间窗、黑名单
- 发送消息、加好友、监听会话、读取可见消息、发布朋友圈
- 只读探测微信 UIA 可用性
- SQLite 任务记录、同类待执行任务去重、单 worker 串行操作微信
- 真实动作三重确认（`--allow-real-actions` + `dry_run=false` + 确认短语）

本项目**不做对话理解、不生成回复内容、不接大模型**——那些全在劳小智 AI 端
（`lxz_agent_backend`）。本仓只负责传输、编排、执行。也不包含 GUI、托盘、定时调度。

## 安装与启动

要求 Windows 10/11、Python 3.10+、已登录的微信 PC 端，以及未锁屏的交互式桌面。

```powershell
python -m pip install -e .
python -m wxclient.gateway --account wxid_zhangsan_work                        # dry-run，不真发
python -m wxclient.gateway --account wxid_zhangsan_work --allow-real-actions   # 真发
```

**默认 dry-run**：整条回路都跑，但不真往微信发消息。要真发必须显式加
`--allow-real-actions`。接口契约与红线见 [`CLAUDE.md`](CLAUDE.md)，实施清单见
[`docs/gateway-plan.md`](docs/gateway-plan.md)。

## 打包

网关的单体 exe **尚未做**（`[build]` extra 里的 PyInstaller 是为它预留的）。
原先的 `scripts/build_mcp_onefile.py` 只打 MCP 入口，已随 MCP 一并删除。
做网关 exe 时注意两件 onefile 特有的事：数据目录（SQLite 队列 / 网关状态库 / 日志）
必须落到 exe 外的固定路径，不能跟着临时解包目录走；凭证与 `--account` 走环境变量和
命令行，不编进二进制。

## 架构边界

```text
wxclient/
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
