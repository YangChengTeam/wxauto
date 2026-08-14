# WS 网关实施计划（gateway/）

> 接口契约见 [`../CLAUDE.md`](../CLAUDE.md) §4，**本文不复制**。
> 对端（劳小智 AI 服务）的实施清单在 `lxz_agent_backend/.claude/specs/064-wechat-gateway/`。

## 0. 单入口（2026-08-12 更新）

本仓只有**一个入口**：

| 模式 | 入口 | 用途 |
|---|---|---|
| **网关模式** | `python -m wxclient.gateway` | 生产链路：入站轮询 → AI 端 → 出站发送 |

> 历史：原先并存一个被动的 MCP 工具服务（`python -m wxclient.mcp_server`），用于本地调试与人工
> 干预。自动化招聘要的是「事件来调 AI」，MCP 唤不醒入站消息，已整体移除；它唯一不可替代的部分
> ——任务队列与真实动作授权——提到包级的 `wxclient/runtime.py`，由网关继续使用。
> 单动作验证改用网关的 dry-run 模式加临时脚本，不再保留常驻的第二个入口。

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

**依赖方向**：`gateway → runtime → queue → wechat_api`，单向不回头。

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
      `--allow-real-actions`（授权判定在 `wxclient/runtime.py` 一处）
- [x] AB5. 端到端测试 17 条：链路上只有微信那一端是假的

  **Checkpoint AB** ✅：候选人消息进 → 换票建连 → 建会话 → 一轮 → 卡片 → 逐段发出，整条打通。
  网关累计 75 条测试全绿，`ruff` 0 错误。

### Phase C · 真机（Windows 172.16.20.86，2026-08-12 首次实跑）

> ⚠️ `wechat_io.py` 的字段映射来自对 `uia.py` 的静态阅读，**入站那半边仍未真机验证**。
> 还欠这三条：① 连发两条一模一样的消息两条都要处理；② 网关首次上线不回复历史；
> ③ 专员本人手打的消息 `sender == "我"` 要被标成 `from_self`。

- [x] C1. 真机装依赖、`uia_probe` 确认微信 UIA 可用
      ✅ 2026-08-12 16:10：`logged_in=true` `uia_exposed=true`，`Weixin.exe` 4.x（`mmui::MainWindow`）。
      **必须在交互式桌面会话里跑**：SSH 进来是 Session 0，UIA 看不见 Session 1 的窗口，
      要用「登录后的计划任务」（`schtasks /RU <user> /IT`）把进程放进 Session 1。
- [x] C2. 用 `WeChatRuntime` 手动验证底座：读消息、发一条消息
      ✅ 2026-08-12 16:11：授权闸两条负例被拒（未开 `--allow-real-actions` / 确认短语不对），
      dry-run 走通队列，真读拿到 10 个会话；`send_message` / `send_files` 真发到「文件传输助手」成功。
- [x] C3. 网关模式接 Fake AI 端跑通（真微信 + 假 AI）
      ✅ 2026-08-12 17:26，脚本 `tests/e2e_windows_ws.py`：换票 → WS 建连 →
      帧序列 `auth / session.start / message.send` → 卡片 2 段 → 两条消息真发到微信
      （task `3a606859` / `18010203`，均 `succeeded`，全程约 10s），
      随后 `read_chat_history` 读回 2 条自测消息取证。
      ⚠️ 入站仍是脚本注入的，**真入站轮询没验**（见上面三条）。
- [x] C4. 网关模式接**真 AI 端**端到端（远程线上，不再是 Fake）
      ✅ 2026-08-12 18:28：AI 端 = `wss://worker.laoxiaozhi.cn/agent-ws/v1/ws`（线上 nginx TLS 反代
      到 `127.0.0.1:8137`，**不新开公网面**）。真票（`scope=wechat`，只准 `wechat_reply`）→ 建连 →
      `session.start` → 一轮 → 卡片 1 段 → 真发到「文件传输助手」（task `a6b90603` `succeeded`，
      `dry_run=false`，连上到发出约 30s），`read_chat_history` 读回原文取证：
      「在招的呀，看你之前做过普工，是想继续找普工类的岗位吗？……」。
      ⚠️ 入站仍是脚本注入的（`lxz_c4_windows.py`，一次性脚本不入库），**真入站轮询没验**。
- [ ] C5. 拒绝→停发、频次超限、专员插话三个场景的真机验证

**AI 端侧同批修掉的一个真缺口**：`wechat_reply` 的 persona 没写「卡片由 skill 的 `emit` 产出」，
codex 把 JSON 叙述在正文里 → `card_count=0`，网关一个字也发不出去。已在 AI 端补两条命令
（`run.mjs read` → 写 JSON → `run.mjs emit`），见 `lxz_agent_backend` 提交 `06f55ad6`。

**真机踩到并已修的三处**（都不是本次改造引入的）：
- 微信最小化时 `Application.connect(title="微信")` 直接抛 ElementNotFoundError，所有 UI 动作全废，
  而 `uia_probe` 自己枚举窗口仍报「UIA 可用」——两边不一致极易误判成「微信没登录」。
  已改为动作前按 hwnd 恢复窗口（`_ensure_ready`）。
- 队列库里一条已删除的任务类型（本机遗留的 `reply_chat`）让 `TaskStore` 构造直接抛异常，
  服务整个起不来。已改为迁移时作废这类行。
- **最小化时 UIA 子树塌到阈值以下 → 探测报「未暴露」→ `_ensure_ready` 直接拒**，而恢复窗口的代码
  排在这个判定之后：网关常驻轮询在这里连挂十几分钟，手工恢复窗口后同一轮立刻通。已改为
  「未暴露且还找得到句柄 → 强制恢复一次 → 复探 → 仍不行才算真不可用」（提交 `2f062a1`）。
  ⚠️ 由此带出一个**产品问题**：窗口最小化时每轮轮询都会把微信弹回前台，机器没法用。
  建议按「有活才恢复」改（先只读判断有没有未读，有才恢复窗口），未定。

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
WXCLIENT_GATEWAY_POLL_INTERVAL_SECONDS 读消息间隔，默认 60，下限 30
WXCLIENT_GATEWAY_FRIEND_CHECK_INTERVAL_SECONDS  查新好友间隔，默认 900，下限 300，0=不查
WXCLIENT_GATEWAY_DEBOUNCE_SECONDS      去抖窗口
WXCLIENT_GATEWAY_TURN_TIMEOUT_SECONDS  一轮上限，默认 180
WXCLIENT_GATEWAY_MAX_SENDS_PER_HOUR    所有写动作合计的总闸，默认 30
WXCLIENT_GATEWAY_ACTIVE_HOURS          工作时间窗，如 09:00-21:00（窗外一概不动微信）
WXCLIENT_GATEWAY_PEER_ALLOWLIST        应急开关：只处理这几个会话
WXCLIENT_GATEWAY_PEER_DENYLIST         应急开关：永不处理这几个会话
```

分动作配额（072，需求文档 §13.3。总闸与分档**两道都要过**）：

```
WXCLIENT_GATEWAY_MAX_MESSAGES_PER_HOUR        私聊，每小时，默认 20
WXCLIENT_GATEWAY_MAX_GROUP_MESSAGES_PER_HOUR  群消息，每小时，默认 6（比私聊严：误伤面大）
WXCLIENT_GATEWAY_MAX_FRIEND_ACTIONS_PER_HOUR  加好友+通过申请，每小时，默认 5（最招风控）
WXCLIENT_GATEWAY_MAX_MOMENTS_PER_DAY          朋友圈，每**天**，默认 3
```

只有一个总闸的时候，「今天一条消息没发」就能换来「连着加 30 个好友」——而加好友
恰恰是风控最敏感的动作。配成 `0` = 本机不开放该类动作（不是「无限」）。

桌面端「参数设置」只摆必须由人定的那些；应急开关和有正确默认值的项
（task_type / 去抖 / 单轮上限 / 各档配额）只留环境变量，手工写进 `gateway.env` 依然生效。

### 4.1 节拍与风控（2026-08-12 事故后定的）

真机上跑了一天之后**微信自己退了登录**，判定触发风控。UI 自动化最像机器的两点是
「节拍精确到秒」和「半夜也在动」，所以节拍层（`gateway/pacing.py`）定死三条：

| 规则 | 值 | 为什么 |
|---|---|---|
| 读消息间隔下限 | 30s（默认 60s） | 原来 5s/15s 一趟，一天几千次翻聊天列表 |
| 查新好友间隔下限 | 300s（默认 900s） | 加好友是风控最敏感的一类动作，节拍要慢一个量级 |
| 每次间隔带随机抖动 | ±30% | 整点动作是「这不是人在用」最明显的指纹 |

外加：**工作时间窗之外一个微信动作都不做**（原来只拦发送，轮询是 24 小时不停的），
且时间窗比的是**本机墙上时间**——原来拿 UTC 比，在东八区等于「白天不发、半夜猛发」。

### 4.2 监听列表由 AI 端推（069）

该盯哪些候选人是业务侧才知道的事，不该在桌面端手抄。AI 端在网关登记那一刻推一次、
名单变了立刻重推：`ev: "wechat.watchlist"`，`data: {only:[…], never:[…]}`，
网关整体替换即时生效（`gateway/watchlist.py`）并落盘（重启不至于全哑）。
缺字段沿用现值、坏数据不换。

两条硬规矩（用户裁决 2026-08-14）：

- **群和单聊一视同仁**，都只看名单。要不要管一个群是业务判断，不是类型判断。
  原来写死「群聊一概不管」，「把某个招聘群纳入自动化」在网关侧根本做不到。
- **白名单语义，没在名单上就不管**。空名单 = 一个都不管，**不是**「不限制」。
  原来「留空 = 所有单聊都管」意味着装上就自动回复任何人，包括专员的家人朋友
  （违反需求文档 §13.1「对象必须在 HR 数据权限范围内」）。名单为空时会 warn 一次。

名单在 AI 端怎么来（`lxz_agent_backend` 仓 `src/ws/wechatWatchlist.ts`）：
① 专员在对话里点名（`watch_add` / `watch_remove`）；
② **主动联系过谁就自动收录**——分身给谁发过消息、加过谁好友，对方自动进名单，
   不然「加了好友却不会自动回复他」会变成每次都要手工补一遍的苦役。


启动：

```powershell
python -m wxclient.gateway --account wxid_zhangsan_work            # dry-run，不真发
python -m wxclient.gateway --account wxid_zhangsan_work --allow-real-actions
```

## 5. 当前阻塞

| 阻塞 | 影响 |
|---|---|
| ~~Windows 主机 SSH 不通~~ | 已解除：2026-08-12 打通 172.16.20.86，C1–C3 已实跑 |
| ~~AI 端不可达~~ | 已解除：走线上 `wss://worker.laoxiaozhi.cn/agent-ws/v1/ws`（nginx TLS 反代），C4 已实跑 |
| 业务端签票接口未建 | 只挡 Phase D；联调期用 `LocalFileTicketProvider`，票由 AI 端密钥手工签、有 TTL |
| 入站只有「新消息」一类 | 加好友申请（`check_new_friends` / `accept_new_friends` 底座已有）还没接进轮询，
AI 端也只有 `wechat_reply` 一个 task_type；「按时间/对象主动发起」同样没有承载的 action |

**下一步优先级**：入站轮询真机验证（那三条）→ 入站事件类型扩容（加好友 + 定时主动触达）→ C5。

> ⚠️ 真机操作纪律：SSH 进去是 **Session 0**，UIA 够不着桌面，`taskkill` 也杀不到桌面里的进程。
> 凡是要碰微信的，一律 `schtasks /create ... /RU <user> /IT` + `/run` 放进 Session 1 跑，把输出重定向到
> 文件再读。另外网关跑起来后每次轮询都会把微信窗口拉到前台——**别在别人正在用的机器上长时间开着**。
