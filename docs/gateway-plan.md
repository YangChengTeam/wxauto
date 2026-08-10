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

### Phase B · 业务层（Linux 全可测）✅ 完成 2026-08-10

- [x] B1. `sessions.py`：`GatewayStore`（会话映射 / 去重游标 / 黑名单 / 发送计数），与队列分库避免抢锁
- [x] B2. `inbound.py`：轮询循环 + `msg_id` 去重（持久化游标，RPA 重推不触发第二轮）
- [x] B3. `inbound.py`：`Debouncer` 去抖聚合 + `format_transcript` 生成 `[对方/专员本人 · 时间]` 上下文（不泄露 wxid）
- [x] B4. `inbound.py`：同联系人严格串行（在途轮未收口 → 新批次放回缓冲继续攒；不同联系人并发不受影响）
- [x] B5. `guard.py`：拒绝关键词表（**确定性第一道闸**，命中即拉黑且**一条都不发**）+ 频次计数器 + 工作时间窗（支持跨午夜）
- [x] B6. `outbound.py`：`extract_reply` 只认契约形状（畸形卡当不回复，绝不猜）+ 逐段投递 + 拉黑提案即使不回复也立即生效
- [x] B7. 业务层单测 38 条：去抖合并 / 重复 `msg_id` 只触发一次 / 拒绝命中即停 / 黑名单永不处理 / 严格串行 / 多联系人并发 / handler 与 source 异常不拖垮循环 / 专员插话进上下文 / 频次与时间窗拦截 / 空回复不发

  **Checkpoint B** ✅：Linux 上用 Fake 微信驱动 + Fake AI 端跑通整条回路，**仍不碰真微信**。
  全量 90 测试（网关新增 58 条全绿；2 条既有 `pyautogui` 用例因无 X11 无法在 Linux 运行），`ruff` 0 错误。

> **设计裁决（B5）**：命中拒绝词后**连告别语都不发**。需求文档 §7 要求「不再对其发起任何自动化动作」，
> 再回一句「好的打扰了」同样是一次联系。宁可显得冷淡，不可越过这条线。
>
> **补正（2026-08-10，AI 端 skill 落地时发现）**：这道闸原来还收了「已经找到工作了 / 没兴趣 / 不找工作」，
> 等于把这些人也永久拉黑——过度处置（人才库里他们以后还要触达）。已从词表移除：
> **闸只管硬拒绝**（别联系我/拉黑/举报），软收口照常派发给 AI 端，由 `wechat_reply` skill 的两档判定
> 礼貌收尾、漏斗标 `closed`、**不提案拉黑**。两侧词表分档口径保持一致。

### Phase AB · 合流：端到端（Linux 全可测）✅ 完成 2026-08-10

> 规划时漏排了这一环——A 层和 B 层各自可测，但没有把它们接起来的编排。补上：

- [x] AB1. `runner.py`：`ConnectionPool`（一专员一连接、断线现换新票重连）+ `GatewayRunner`
      （取连接 → 建/接会话 → 发一轮 → 收卡 → 交出站）
- [x] AB2. 会话生命周期：本连接首次用到某会话才 `resume`（顺带触发 AI 端重投影）；
      AI 端不认的旧会话 → 丢掉重建，不卡死该联系人；断线单次重试
- [x] AB3. `wechat_io.py`：队列版 source/sink + **游标切新消息**（首次见到某联系人只对齐游标，
      绝不照着历史逐条回复）
- [x] AB4. `__main__.py`：`python -m wxclient.gateway`，**默认 dry-run**，真发需显式
      `--allow-real-actions`（与 MCP 模式同一条纪律）
- [x] AB5. 端到端测试 17 条：链路上只有微信那一端是假的

  **Checkpoint AB** ✅：候选人消息进 → 换票建连 → 建会话 → 一轮 → 卡片 → 逐段发出，整条打通。
  网关累计 75 条测试全绿，`ruff` 0 错误。

### Phase C · 真机（需 Windows 通路）

> ⚠️ `wechat_io.py` 的字段映射来自对 `uia.py` 的静态阅读，**尚未在真机验证**。
> C1 必须专门验这三条：① 连发两条一模一样的消息两条都要处理；② 网关首次上线不回复历史；
> ③ 专员本人手打的消息 `sender == "我"` 要被标成 `from_self`。

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
WXCLIENT_GATEWAY_AI_WS_URL             wss://<ai-host>/v1/ws
WXCLIENT_GATEWAY_BUSINESS_API_URL      https://<business-host>
WXCLIENT_GATEWAY_INTERNAL_KEY          <网关专用 internal key>   ← 网关唯一持有的秘密
WXCLIENT_GATEWAY_LOCAL_TICKET_FILE     联调用本地票据文件（与上面两项互斥，生产留空）
WXCLIENT_GATEWAY_TASK_TYPE             默认 wechat_reply
WXCLIENT_GATEWAY_POLL_INTERVAL_SECONDS 入站轮询间隔
WXCLIENT_GATEWAY_DEBOUNCE_SECONDS      去抖窗口
WXCLIENT_GATEWAY_TURN_TIMEOUT_SECONDS  一轮上限，默认 180
WXCLIENT_GATEWAY_MAX_SENDS_PER_HOUR    每号每小时发送上限
WXCLIENT_GATEWAY_ACTIVE_HOURS          工作时间窗，如 09:00-21:00
```

启动：

```powershell
python -m wxclient.gateway --account wxid_zhangsan_work            # dry-run，不真发
python -m wxclient.gateway --account wxid_zhangsan_work --allow-real-actions
```

## 5. 当前阻塞

| 阻塞 | 影响 |
|---|---|
| Windows 主机 SSH 不通 | 只挡 Phase C |
| 业务端签票接口未建 | 只挡 Phase D；Phase A/B 用 `LocalFileTicketProvider` |

**Phase A 与 B 不依赖任何外部条件，可立即开工。**
