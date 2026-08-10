# WS 网关实施计划（gateway/）

> 接口契约见 [`../CLAUDE.md`](../CLAUDE.md) §4，**本文不复制**。
> 对端（劳小智 AI 服务）的实施清单在 `lxz_agent_backend/.claude/specs/064-wechat-gateway/`。

## 0. 双模式并存（明确保留）

本仓保留**两个入口**，共用同一套 `queue → wechat_api` 底座：

| 模式 | 入口 | 用途 |
|---|---|---|
| **MCP 模式** | `python -m wxclient.mcp_server` | 本地调试、人工干预、单动作验证。**保留，不删** |
| **网关模式** | `python -m wxclient.gateway` | 生产链路：入站轮询 → AI 端 → 出站发送 |

两者可同时起（各自监听不同端口），但**同一时刻只有一个 worker 操作微信**——这由既有的 SQLite 租约保证，不需要额外机制。调试时用 MCP 手动发一条消息，不会和网关的自动回复打架，只会排队。

## 1. 模块划分

```text
wxclient/gateway/
  __main__.py     # 入口：解析参数、装配、起循环
  config.py       # GatewaySettings（env 前缀 WXCLIENT_GATEWAY_）
  ticket.py       # 换票：TicketProvider 协议 + 业务端实现 + 本地假实现
  wsclient.py     # WS 协议客户端：auth / session.start / resume / message.send / 事件解析
  sessions.py     # 会话映射表 (专员微信号 × 对方 wxid) → session_id，SQLite 持久化
  inbound.py      # 入站轮询循环 + msg_id 去重 + 去抖聚合
  guard.py        # 确定性守门：黑名单关键词、频次上限、工作时间
  outbound.py     # card.reply_segments → send_message 任务
  runner.py       # 编排：一专员一连接、重连、生命周期、优雅退出
```

**依赖方向**：`gateway → queue → wechat_api`。`gateway` 不 import `mcp_server`，反之亦然。

## 2. 阶段与任务

### Phase A · 协议层（Linux 全可测）✅ 完成 2026-08-10

- [x] A1. `config.py`：`GatewaySettings`（AI 端 WS 地址、业务端地址、internal key、轮询间隔、去抖窗口、频次上限）+ `validate_for_start()`（非回环禁明文、票据源二选一）
- [x] A2. `ticket.py`：`TicketProvider` 协议 + `BusinessTicketProvider`（HTTP 换票，3s 超时，**fail-closed**）+ `LocalFileTicketProvider`（读本地未入库 json，仅供联调）
- [x] A3. `wsclient.py`：帧编解码、`auth`/`session.start`/`session.resume`/`message.send`、轮内事件按 `session_id` 归位（不依赖 ack 到齐）、`card`/`status`/`error` 解析、同 session 串行护栏
- [x] A4. **Fake AI 端 WS 服务** `tests/fake_agent_ws.py`：按契约回放 `status → text.delta × N → card → done`，支持按输入分支与错误/延迟脚本
- [x] A5. 协议层单测 20 条：完整帧序列、鉴权失败、resume 续接、越权 resume、错误带 `fallback_card`、超时、同 session 并发拒绝、多会话不串台、断线在途轮立即失败、发帧契约、票据 provider、配置校验

  **Checkpoint A** ✅：Linux 上 `unittest` 跑通「换票 → 建连 → 发一轮 → 收到 card」，不碰微信。
  全量 52 测试（新增 20 全绿；2 条既有 `pyautogui` 用例因无 X11 显示无法在 Linux 运行），`ruff` 0 错误。

### Phase B · 业务层（Linux 全可测）

- [ ] B1. `sessions.py`：映射表建表 + 读写 + 进程重启后恢复
- [ ] B2. `inbound.py`：轮询循环骨架 + `msg_id` 去重（持久化游标）
- [ ] B3. `inbound.py`：去抖聚合（窗口内多条合并成一段，带 `[对方 · 时间]` 前缀）
- [ ] B4. `inbound.py`：同会话严格串行（一轮未完不开第二轮）
- [ ] B5. `guard.py`：拒绝关键词表（**确定性第一道闸**）+ 频次计数器 + 工作时间窗
- [ ] B6. `outbound.py`：`reply_segments` 逐段投递 `SEND_MESSAGE` 任务；空数组则不发
- [ ] B7. 业务层单测：去抖合并、重复 `msg_id` 只触发一次、拒绝词命中即停、频次超限不发、空回复不发

  **Checkpoint B**：Linux 上用 Fake 微信驱动 + Fake AI 端跑通整条回路，**仍不碰真微信**。

### Phase C · 真机（需 Windows 通路）

- [ ] C1. 真机装依赖、`uia_probe` 确认微信 UIA 可用
- [ ] C2. MCP 模式手动验证底座：读消息、发一条消息
- [ ] C3. 网关模式接 Fake AI 端跑通（真微信 + 假 AI），验证入站轮询与出站发送
- [ ] C4. 网关模式接真 AI 端端到端
- [ ] C5. 拒绝→停发、频次超限、专员插话三个场景的真机验证

### Phase D · 接线（需业务端）

- [ ] D1. `LocalFileTicketProvider` 换成 `BusinessTicketProvider`
- [ ] D2. 微信号 → 专员绑定走业务端真实数据

## 3. 测试策略

**两个 Fake 撑起 Linux 全测**：

| Fake | 替代 | 让什么变得可测 |
|---|---|---|
| Fake AI 端 WS 服务 | Linux 上的 AI 服务 | 协议层、重连、card 提取 |
| Fake 微信驱动 | `wechat_api` | 入站轮询、去抖、去重、守门、出站投递 |

**红线**：Linux 跑绿 ≠ 能用。Phase C 没做完，不许声称网关可用。

## 4. 配置项（全部走环境变量，不入库）

```
WXCLIENT_GATEWAY_AI_WS_URL          wss://<ai-host>/v1/ws
WXCLIENT_GATEWAY_BUSINESS_API_URL   https://<business-host>
WXCLIENT_GATEWAY_INTERNAL_KEY       <网关专用 internal key>   ← 网关唯一持有的秘密
WXCLIENT_GATEWAY_POLL_INTERVAL      入站轮询间隔（秒）
WXCLIENT_GATEWAY_DEBOUNCE_SECONDS   去抖窗口（秒）
WXCLIENT_GATEWAY_MAX_SENDS_PER_HOUR 每号每小时发送上限
WXCLIENT_GATEWAY_ACTIVE_HOURS       工作时间窗，如 09:00-21:00
```

## 5. 当前阻塞

| 阻塞 | 影响 |
|---|---|
| Windows 主机 SSH 不通 | 只挡 Phase C |
| 业务端签票接口未建 | 只挡 Phase D；Phase A/B 用 `LocalFileTicketProvider` |

**Phase A 与 B 不依赖任何外部条件，可立即开工。**
