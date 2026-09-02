# AGENTS.md

**本仓唯一的协作约定**，给 Claude Code / Codex 等 AI 助手看，也给人看。
`CLAUDE.md` 是指向本文的**软链**——改规约只改这里，别把软链拆成两份文件。
（2026-09-02 合并：原先 `AGENTS.md` 讲代码规则、`CLAUDE.md` 讲协作与契约，两份互相引用又各自漂，
`CLAUDE.md` 停在 8-18 而 `AGENTS.md` 已到 9-01。一份文档不会和自己对不上。）

> 🟢 **新会话起手**：读「二、现状与冻结面」知道哪些不能动，读「三、代码该写哪」知道往哪写，
> 动微信行为前必读「五、红线」和「六、节拍与守门」。
>
> **本文不复制代码，也不复制 `docs/`。** 契约细节在 `docs/` 那几份带版本号的接口文档里，
> 实现细节在源码注释里；这里只讲**约束和理由**。

---

## 一、这个仓库是什么

**Windows 微信自动化网关**，服务于劳小智招聘业务的「微信自动化招聘」能力。
它是整条链路里**唯一有副作用的一端**——真正操作微信窗口的就是它。

> **翻译官 + 守门人**：把入站微信消息翻译成一次 AI 对话轮，把 AI 产出的回复翻译回微信消息发出去；
> 同时用确定性规则守住频次、黑名单、真实动作确认。

**它不做什么**（红线，见第五章）：不做对话理解，不生成回复内容，不接大模型。
这些全在劳小智 AI 端（`lxz_agent_backend`）。本仓只负责**传输、编排、执行**。

对外只有一个入口：主动的 WS 网关（`wxclient/gateway/`），外加一层桌面壳
（`wxclient/ui/`：托盘 + 实时日志 + 配置中心）。不维护独立调用客户端，不维护大模型回复模块。

### 三层职责（跨仓）

```
微信/RPA ──事件──▶ [本仓 · Windows 网关] ──WS 客户端──▶ [AI 端 · Linux]
              ◀──调 wechat_api 发送──┤                      │ 产 reply_segments + actions
                                     └──actions──▶ [业务后端 · 写库]
```

- **本仓**：收消息、去重去抖、串行队列、连 AI 端、发消息、守门
- **AI 端**：理解意图、判漏斗阶段、查在招岗位、生成话术、产结构化卡
- **业务后端**：签票据、写 `resume_lead`、拉黑、约面试

---

## 二、现状与冻结面

**冻结、直接复用、不改**（改造的本质是「外翻」，底层三个模块一行不动）：

| 模块 | 职责 |
|---|---|
| `wxclient/queue/` | SQLite 任务 + 单消费者串行 + 幂等 + 租约 + 续跑 |
| `wxclient/wechat_api/` | pyweixin 二次封装（发消息 / 加好友 / 朋友圈 / 读消息） |
| `wxclient/uia_probe/` | 只读 UIA 状态探测 |
| `wxclient/runtime.py` | 任务队列 + 执行器 + 真实动作授权，全进程唯一，各入口共用 |

**在动的是 `wxclient/gateway/` 和 `wxclient/ui/`**。两者都已真机跑通并发过版
（客户端走安装包分发，见「十三、部署与发版」）。

**`mcp_server/` 已于 2026-08-12 整体移除**，别复活。原来它是被动的、等 agent 来调；
`gateway` 是主动的、自己收消息自己连 AI 端。自动化招聘要的是「事件来调 AI」，
MCP 唤不醒入站消息。只把不可替代的运行时（队列 + 动作授权）提到了 `wxclient/runtime.py`。

⚠️ **这里不列「还差哪些功能」。** 那种清单每周都会过期，而且过期时一声不吭
（原表停在 8-18，把早就上线的 067/081/085/090 还写成「未起」）。
要看做到哪了，读 `docs/gateway-plan.md` 和 AI 端的 `.agents/specs/`，不要读一份手抄的状态表。

---

## 三、代码该写哪

```text
wxclient/
  gateway/      # 本仓唯一常驻入口：入站轮询、去抖、会话映射、换票、WS 客户端、出站发送、守门
  ui/           # 桌面壳：登录、托盘、实时日志、配置中心
  runtime.py    # 共享运行时：任务队列 + 执行器 + 真实动作授权（全进程唯一）
  queue/        # 【冻结】SQLite 任务 + 单消费者 worker
  wechat_api/   # 【冻结】pyweixin 二次封装
  uia_probe/    # 【冻结】只读 UIA 状态探测
  config/       # 微信运行参数
third_party/pywechat/pyweixin/   # vendored，真正驱动微信的那个包
docs/gateway-plan.md             # 网关实施清单
```

**四条铁律**：

- **所有微信 UI 动作必须走 `gateway → runtime → queue → wechat_api`。**
  `gateway` 只能 `submit()` 任务，**绝不直接操作微信窗口、绝不绕过队列调 pyweixin**。
- **微信 UI 自动化必须由单消费者 worker 串行执行。** 同类型 `PENDING` 任务不重复入队。
- **`gateway` 不实现任何话术逻辑。** 判断回什么、怎么说，全部由 AI 端返回；
  网关只做「发不发、发给谁、什么时候发」这类确定性编排（去重、去抖、串行、频次、黑名单）。
- **将来若再加入口，共用同一个 `WeChatRuntime`**，不允许各自复制一份队列或 API 封装，
  更不允许绕开 `_authorize_payload()` 自建授权旁路。装配（票据来源、账号、真实动作开关）
  只在 `gateway/factory.py` 一处，入口只解析参数。

---

## 四、与 AI 端的接口契约

### 4.1 鉴权：网关只持有一把 key，不持有任何员工凭证

```
[网关] ──① Bearer 客户端 Token──▶ [业务后端]  换该专员的 60s 短票
[网关] ──② session_ticket──────▶ [AI 端]     建 WS 连接
```

换票走 `POST {业务端}/recruitment-agent/client/session-ticket`，
带客户端 Token，**请求体是空的**——换谁的票由服务端从会话派生，客户端传身份不作数。
前置条件是这台机器当前 `online`，否则 `409 CLIENT_NOT_ONLINE`。
细节见 [`docs/招聘分身_客户端认证与状态接口_v1.1.md`](docs/招聘分身_客户端认证与状态接口_v1.1.md) §5.3；
业务端默认地址在 `settings_store.DEFAULT_BUSINESS_API_URL`，
AI 端 WS 地址在 `wxclient/endpoints.py: DEFAULT_AI_WS_URL`——**要值去那两处读，别在文档里抄一份**。

**四条铁律**：

1. **票不缓存、不落盘、不打日志。** TTL 60 秒，取票即用，用完即弃。
2. **每次建连/重连都现换一张新票。**
3. **换票失败一律 fail-closed** —— 停止该专员的一切自动化动作。不降级、不重试绕过。
4. **一个专员 = 一条 WS 连接。** 一条连接可承载该专员名下所有候选人会话，换专员必须换连接。

### 4.2 对话：WS 帧序列

```
{op:"auth",           data:{session_ticket}}        → {ev:"status", state:"authenticated"}
{op:"session.start",  data:{task_type:"..."}}       → {ev:"session.created",  session_id}
{op:"session.resume", data:{session_id}}            → {ev:"session.resumed"}
{op:"message.send",   data:{session_id, text}}      → {ev:"status", state:"running"}
                                                    → {ev:"text.delta", delta} × N
                                                    → {ev:"card", card:{...}}      ★ 取这个
                                                    → {ev:"status", state:"done"}
```

- 只认 `card` 作为产出，`text.delta` 仅可用于「正在输入」提示。
- 发帧带 `id`，响应回 `ack`，用于在同一连接上对号入座。
- **会话映射表 `(专员微信号 × 对方 wxid) → session_id` 必须持久化**（本地 SQLite），
  进程重启后靠 `session.resume` 接回同一上下文。

### 4.3 入站是轮询式，不是回调

`wechat_api` 的 `watch_chats` / `get_unread_messages` 都是**有界时长任务**，跑完就结束返回。
没有「来消息就回调」这回事。所以 `gateway` 必须自己跑常驻轮询循环，并且：

- **按 `msg_id` 去重**，防重复触发
- **去抖聚合**：对方连发多条短消息，合并成一轮再送 AI 端，否则会回多次
- **同一会话严格串行**：一轮没结束，绝不开第二轮

这条常驻轮询循环是**既定架构**，不属于被禁的「定时调度」。

---

## 五、红线（违反即不合格）

### 5.1 安全

- **凭证绝不入库**：token / 口令 / internal key 一律走环境变量，仓库里只允许 `.env.example` 和占位符。
  **部署机的真实 IP、账号、口令不写进任何入库文件。**
- **真实动作三重确认**：服务端 `--allow-real-actions` + `dry_run=false` + 确认短语
  （`wxclient.runtime.REAL_ACTION_CONFIRMATION`），三者缺一不可。加好友、同意好友、发消息、
  发朋友圈默认 `dry_run=true`。判定只在 `WeChatRuntime._authorize_payload()` 一处，
  **授权开关不进 `--config` 能加载的配置文件**（否则「改个配置文件就能真发消息」）。
- **网关不开监听面**：它是纯 WS 客户端，只出不入，没有端口可打。将来若新增任何监听端口，
  非回环必须带 token 且拒绝无 token 启动，跨公网必须 VPN 或 TLS 反代 + 来源 IP 限制，
  **不允许明文 HTTP 裸奔**。

### 5.2 业务

- **拒绝即停，双保险**：对方表达「别联系我 / 别加我 / 举报」类意思时必须停止一切自动化动作。
  **网关侧先跑确定性关键词闸**，模型判断只作补充——这条不能只押在 AI 端。
- **拒绝分两档，别混为一谈**：硬拒绝（「别联系我」）= 网关闸拦下 + 拉黑 + 一条都不发；
  软收口（「已经找到工作了」「暂时没兴趣」）**不进网关闸**，照常派发给 AI 端，
  由它礼貌收个尾、把漏斗标 `closed`、**不提案拉黑**——这种人以后还要触达，永久拉黑是过度处置。
- **频次上限在网关侧强制**：加好友、群发、私聊的频次由网关的确定性计数器守，不依赖 AI 端自觉。
- **允许不回**：AI 端返回「无需回复」时就不发。不强制每条必答。
- **人工接管优先**：专员自己在微信里回过话，必须把这条也灌进会话上下文（标为我方已说），
  否则 AI 下一轮会重复发问。专员插话后 AI 需静默一段时间。
- **失败任务要保留原因、状态和重试次数**，供调用方查询。

### 5.3 平台

- **只在已登录、未锁屏的交互式 Windows 桌面运行**。不放进 Session 0 的 Windows Service，
  用登录后的计划任务拉起。
- **MVP 只支持单 Windows 会话中的单个微信账号。**
- **不做 DLL 注入、进程伪装、屏幕阅读器标志修改**，或任何其他绕过微信限制的方案。

### 5.4 UIA

- `probe_wechat_uia()` **只能读取状态**，不启动、关闭、重启、切换或解锁微信。
- 业务动作前必须确认微信已登录且 UIA 可用；看到 `mmui::LoginWindow` 时阻止任务并提示登录。

---

## 六、节拍与守门（防微信风控）

这一节的每一条都是真机撞出来的，改之前先读理由。

- **加好友这类动作两道闸都要过：配额（次/小时）+ 最小间隔（≥300s）。**
  两者不可互相替代：配额管总量，间隔管节奏。只有配额的话，5 次/小时允许 5 次全挤进一分钟——
  2026-08-27 真机就挤成了 81 秒两次、两次都「没搜到这个号」，随后弹出安全验证。
  闸按**指令名**登记（`guard.WRITE_PACING`），不按 action：通过别人发来的申请不用搜号，
  不该被这道闸卡住。两个数都在调度配置里
  （`max_friend_actions_per_hour` / `friend_interval_seconds`）。
  ★ 曾经还有一档「连续失败翻倍退避」，2026-08-28 按用户要求删了（等太久）。
  **别加回来**：想更慢就把间隔调大——一个看得见、改得动的数，好过一个自己会长的数。
- **两条会互相挡的节拍闸，让路的必须是「能重来」的那一方。**
  加好友（`friend_write`）与复查循环（`read_friends`）都在搜人，确实不该挨着做，
  但**不能合账**：复查只要还有人待查就每 300 秒摸一次账，合账等于 AI 端下发的加好友
  永远排在它后面——而被拒的指令是直接丢掉、不排队的。做法是复查躲加好友
  （`friendcheck` 用 `seconds_since_read` **只看不占**），反过来不成立。
- **动微信的每一步之间要有随机停顿**（`wechat_api/uia.py` 的 `_PAUSES` / `_type_like_human`）。
  整串 `set_text` + 零延迟点击 = 一趟 6 秒、方差为零，这本身就是特征。
  停顿一律取随机区间——固定 2.0 秒和固定 0.0 秒一样是机器，只是慢一点。
- **人工接管中的候选人连读都不读**（用户裁决 2026-08-28）。摘在
  `wechat_io.QueueMessageSource._drop_handed_off`；写侧那道闸（`guard._takeover_denial`）
  保留作第二条路。原口径是「读要继续，否则后端『未回复跟进』取消不掉」——那条代价现在是
  明知故犯地接受了，别照旧注释改回去。**交回分身时必须抹游标**
  （`GatewayStore._forget_cursor`，两条路都要：本机按钮 + 同步名单），
  否则分身一睁眼就把招聘老师自己打的那一串当新消息。

---

## 七、加好友回什么名字（★ 后续回消息就靠它）

- `add_friend` 返回 `{number, name, remark, session_name}`：`name` 是对方昵称（读名片得来，
  只给人看，会改、会重名、可能带 emoji），`remark` 是我们设的备注，
  **`session_name` = 备注优先、没备注才退回昵称，那才是后面 `send_message(to=...)`、
  监听名单、关系记忆该用的键**——对方通过后会话列表显示的就是备注。
- 所以要能稳稳回消息，加好友时**就得带 `remark`**（例：`张三-仓库工`），别指望昵称。
- 读不到昵称不算失败（`name` 给 `null`，走 `friend.add.fallback`）；但**号码搜不到会直接报错**，
  不再静默返回成功——否则 AI 端会一直等一个根本没发出去的申请。
- `add_friend` 成功只代表**申请已发出**，不代表能发消息了。对方哪天点通过是之后的事，
  有**两条**路认出来（2026-09-01 用户改口径）：
  ① `gateway/friendcheck.py` 隔一阵去搜一次（判据 = `search_contacts` 精确命中说明他已经
     在通讯录里了）。间隔和期限都在**配置中心**里（`friend_recheck_interval_seconds` /
     `friend_recheck_deadline_hours`），期限从**申请那一刻**算起，取代了原来按次数的
     `MAX_CHECKS`。搜的名称 = 我们报给业务端的那个（备注优先、没备注才退回昵称）。
  ② 业务端下发的**发消息指令发成功了** → 他早就通过了（`_note_sent_to_pending_friend`）。
     白捡的一条，不额外动微信。
  两条都汇到 `_report_accepted_now`：**当场**报 `accepted`，不再压半小时
  （原 `gateway_pending_accepted` 延迟队列 2026-09-01 已删）。
- ★ **不为「等通过」开轮询**（用户 2026-09-01：「业务端会发消息过来 不用轮询」）。
  `requested` 的人**始终不进监听名单**——名单只收 `accepted`（085 fail-closed）。
  放开它的代价是每人每轮白开一次不存在的会话窗口，而且 `requested` 有**两个来源**，
  另一个是后端推计划写的「计划点名要加谁」，那些人我们根本还没加，放进名单等于
  可能给一个从没打过招呼的人自动回复。
- ★ 「我们真发出过申请」的权威是**本机台账** `gateway_friend_checks`（只由本机写），
  不是 `gateway_contacts.status='requested'`——后者被同步名单无条件覆盖
  （`save_contacts`，后端没回 `contact_status` 就写成空串），一冲掉这个人就既不被复查、
  也认不出通过，而且一声不吭。
  ⚠️ `check_new_friends` 不是这个信号——那是**别人申请加我们**的列表，
  和「我们申请的对方通过了」是两件事。

---

## 八、桌面端登录与状态上报（2026-08-15 起按正式契约）

契约：[`docs/招聘分身_客户端认证与状态接口_v1.1.md`](docs/招聘分身_客户端认证与状态接口_v1.1.md)。
实现分三块：`ui/client_api.py`（四个接口）、`ui/token_store.py`（DPAPI 存 Token）、
`ui/status_reporter.py`（状态变化时上报）。**改任何一处都要和那份文档对齐，别就地发明字段。**

- 界面**先登录后使用**：邮箱 + 密码 → `POST /recruitment-agent/client/auth/login`，
  **不触发邮箱 OTP**。不许加「跳过登录」的开关，也不许再加第二条登录路——
  服务端管不到的登录方式，撤销会话/停用账号对它一律无效（原「本机账号」模式已删）。
- **Token 不过期但可撤销**：只加密落盘（`client-session.json`，Windows DPAPI），
  非 Windows **一个字节都不写**。任何调用回 `401 CLIENT_SESSION_REVOKED` → 清 Token +
  回登录页（`ClientAuth.handle_revoked`），这是不过期 Token 唯一的刹车。
- **没有心跳**（契约 §7）：只在状态变化时 PUT，服务端不按时间推断离线。所以
  **退出前必须先报 offline**（`StatusReporter.shutdown()`，最多等 3 秒），漏了这一步，
  企业端会一直显示这台早就关机的机器在线。
- **相同状态不重复上报**；上报失败要把去重记录**回滚**——否则一次网络抖动会让这个状态
  永远报不出去（去重表说报过，服务端压根没收到）。
- 微信连接状态由只读 UIA 探测映射（`ui/uia_status.py`，纯函数）：没开 / 没登录 / 控件树没暴露
  是三种不同失败，各给各的恢复引导；连过又断报 `disconnected`。**拿不到的别编**——
  非 Windows 直接不上报；`accountRef`（防一个微信被两个分身占用）需要读微信自身账号，
  客户端现在没这个能力，**宁可不给也不编一个假的稳定标识**。
- **能力档**（契约 §8 `capabilityStatus`）跟 `connected` 一起报，直接复用网关那份能力表
  （`gateway/capabilities.py: capability_status`）——**不另起一套判据**，两套迟早给出两个
  答案。只有 `passed` 会让企业端显示「运行中」，所以 dry-run 机器必须如实报 `limited`。
- 参数只有一份 `gateway.env`（客户端接口不下发配置，原 `gateway.remote.env` 那层已删）。
  界面上**一个凭证都没有**；微信号也不再手抄，用登录返回的 `agent.id` 记账。
- 「记住邮箱」只记邮箱，永不记密码。
- **登录之前一概不起服务**：服务只在 `__main__.start_service()` 一处起。
- **「退出登录」= 「退出服务」，只留这一个**（停网关 + 撤销服务端会话 + 回登录页）。
  窗口侧栏和托盘菜单必须一致；托盘菜单 = 实时日志 / 配置中心 / 退出登录 / 退出。
- 界面两页：实时日志 / 配置中心（参数 + 只读监听名单），整体版式基准是
  [`docs/wxui.png`](docs/wxui.png)。
  **计划不进桌面端**（用户 2026-08-15 裁决）：几点做什么、多久跟进一次由 AI 端排，
  指令经 WS 推下来执行；客户端连 `plans` 字段都不解析，别再往回加一页。
- 配置中心版式照 `docs/setting.png`：参数分段（`settings_store.SECTIONS`）+ 每段一句说明；
  加一个参数 = 往 `FIELDS` 里加一条（带 `section`/`unit`/`kind`/`placeholder`），**页面不用改**。
  共用小控件（灰字占位、聚焦绿边、胶囊开关、描边按钮）在 `ui/widgets.py`，别各页各写一份。
  「测试连接」只测到网络可达，**不许把话说成「连接正常」**——鉴权对不对只有真连上才知道。
- 监听名单是配置中心里一节只读卡片（`docs/setting2.png`）：一人一行，谁在名单里就是谁会被
  自动回复（069 fail-closed）。**名单权威始终在 AI 端，本机不给编辑。**
  「同步名单」按钮走 WS 的 `wechat.watchlist.sync`（`wsclient.py`），只走推送落地、应答只回数；
  它**不是**接口文档 §4.2 的会话名同步，两件事别混。
- 名单标题上那个「下次读消息」倒计时取自 `InboundLoop.seconds_to_next_poll()`——**记的是
  循环真睡的那一觉**（`_next_poll_at`，含抖动与快慢档），不是照配置算的；照配置算会一直和
  现实差几十秒，看着像坏了。没排期/窗外就留白或直说，别给个走到 00:00 却什么都不发生的数。
- 日志按天分文件（`logging_config.DailyFileHandler`，`logs/gateway-YYYY-MM-DD.log`，留 30 天）。
  不用 `TimedRotatingFileHandler`：它把**今天**那份留成没有日期的 `gateway.log`，
  一目录里两种命名，按日期找的人第一眼就找不到今天。

---

## 九、微信消息事件上报（未回复跟进的事实来源）

契约：[`docs/招聘分身_客户端微信消息事件上报_v1.md`](docs/招聘分身_客户端微信消息事件上报_v1.md)。
实现：`gateway/message_events.py` + `gateway_message_events` 表；三个登记点（入站回调 /
出站投递 / 指令执行）见那份文档的表格。

- **客户端只上报事实，不判断该不该发第二条**。四小时该不该追、追什么，全在后端的计划里；
  本机再排一次 = 同一条跟进发两遍。**不许在这儿加时钟。**
- **先落发件箱再上报**：`record()` 同步、只写本机 SQLite，真正发请求的是后台那条循环。
  记账绝不能挡在微信动作前面，网络抖一下也不能把事件抖没。
- **`eventId` 由内容派生**（uuid5），断网重投永远是同一个值——后端据它幂等去重。
- **归不了位就不报**：候选人 ID 由本机按会话名反查，反查不到如实跳过，**不编 ID 顶上**。
- **dry-run 机器一条都不报**：`factory.py` 在 `allow_real_actions` 为假时不建上报器。
  同理，队列任务没回 `succeeded` 也不报——「没抛异常」不等于「话发出去了」。
- **不上传聊天正文**：只带 `contentDigest` 摘要，`messageId` 也哈希过（本机 msg_id 里带备注名）。

---

## 十、陌生人主动发起（090 / 接管托管同步接口 v1 §9.5）

契约：`lxz_agent_backend/docs/招聘分身_客户端接管托管同步接口_v1.md` §9
（**这份在 AI 端仓，本仓没有副本**，原文写成本仓相对路径是断链，别照抄）。
权威源是根仓的 `requirement/` 同名文件。
实现：`gateway/strangers.py` + `gateway_strangers` 表 + `runner._apply_derived_watchlist` 的并集。

- **为什么要有这一层**：入站只读监听名单里的会话，而名单由 `GET /client/contacts` 派生
  （spec 085）＝「后端已经知道的候选人」。主动加好友、主动发第一句话的人**不在里面**，
  他的消息网关根本读不到——AI 端那条「主动收集资料、主动建档」从第一步就走不下去。
- **默认开**（`WXCLIENT_GATEWAY_STRANGER_INTAKE`，用户裁决 2026-08-29，配置中心标题
  「来问陌生人」）。原来默认关，理由是「装上不会自动回复专员的家人朋友」
  （069 / 需求文档 §13.1）——但真实用法里主动来问的人正是要接的那一类，默认关等于
  每台机器装完都要有人记得来开一次，忘了就没人理。**真正兜住误伤的是后面那几道确定性闸**
  （判不出在找工作就一条都不发），不是这个开关的默认值。不想接的在配置中心关掉即可。
- **只读会话列表，不开聊天窗口**：走 `read_chat_sessions`（主窗口那个列表控件），
  节拍按「查好友」那一档（≥300s）+ 抖动 + 工作时间窗。**读类必须显式 `dry_run=False`**，
  否则底座回空壳（扫了等于没扫）。
- **只收今天有动静的**、有名额上限（50）、有保鲜期（7 天）。把整个会话列表倒进来
  等于把 fail-closed 名单变成 fail-open。
- **归位就撤销**：后端建好档、会话名同步回来之后这个人进正式名单，临时准入删掉——
  两边都挂着的话，临时准入到期会把一个正经候选人清掉。
- **第一轮带 `first_contact`**：AI 端据此收紧，判不出对方在找工作就一条都不发。
  ★ 这个标记**必须由本机给**：入站 `refs` 为空是常态（会话名还没归位到候选人，后端明说允许），
  让 AI 端拿它当判据，名单里的正常联系人会一起哑掉。
- **不自动通过好友申请**：`check_new_friends(verify=True)` 是**全量通过、无法按人挑**
  （见 `third_party/pywechat/pyweixin/WeChatAuto.py`），且日频上限极严。这条路仍然只看不通过。

---

## 十一、扩展方式

- 新微信能力先加入 `wechat_api` 封装，再增加任务类型和 worker 分发，最后暴露给网关。
- 桌面端只做「登录 + 启动/停止服务 + 看日志 + 看/改参数」，**不得**在 `ui/` 里写任何业务
  判断、话术或微信动作；它只调 `gateway`/`service` 的公开接口。可测逻辑放 `ui/logbuffer.py`、
  `ui/service.py`、`ui/settings_store.py`、`ui/auth.py`，`ui/window.py`、`ui/login_page.py`
  与 `ui/tray.py` 只留 GUI 装配。
- 不恢复独立调用客户端或大模型模块；若产品方向变化，应单独讨论架构。
  （2026-08-12：产品方向确认要桌面端，原「不做 GUI/托盘」的禁令按此收窄为上面一条。）

---

## 十二、开发与测试

```powershell
python -m ruff check wxclient tests packaging
python -m unittest discover -s tests -v              # 仓库根跑，别加 -t .
python -m wxclient.gateway --account <wxid>          # dry-run 冒烟，不真发
```

★ **别加 `-t .`。** `discover -s tests -t .` 会当场炸：
`ImportError: Start directory is not importable` —— `tests/` 没有 `__init__.py`，
指定顶层目录后 discover 就非要把它当包 import。不加 `-t .` 正常跑，
或者 `cd tests` 再 `discover -s . -t .` 也行。三种写法只有中间那种是坏的，
真机和 Linux 上一样。

**Linux 上的基线**（2026-09-02 实测）：139 skip + 16 error，全部来自
pywinauto / tkinter 这类 GUI 依赖，**属正常，不是你改坏了**；真机全量约 950 条只 skip 1。
判断「我有没有改坏」要跟这个基线比，不是跟「全绿」比。

### ⚠️ Linux 上能测什么、不能测什么

开发多在 Linux 上进行，但**微信自动化只能在 Windows 上真跑**。

| 能在 Linux 跑 | 不能，必须上 Windows 真机 |
|---|---|
| `ruff` 静态检查 | 任何 `uia_probe` / `wechat_api` 实际调用 |
| 队列逻辑、幂等键、状态机单测 | 发消息、加好友、发朋友圈 |
| 去抖 / 去重 / 守门规则的纯函数单测 | 入站消息轮询 |
| WS 客户端协议层（对着 Fake AI 端） | 端到端链路 |

**规则：Linux 上跑绿不等于能用。** 涉及微信行为的改动，未在 Windows 真机验证过，
不许标记完成、不许写「已验证」。真机验证必须留下可复核的证据：动作类型、目标会话、
任务 ID、执行结果、耗时。口头「测过了」不算。

---

## 十三、部署与发版

**发给用户的是安装包，不是一个目录。** exe 早已做完并持续发版，
打包细节（onedir 为什么不是 onefile、砍 OpenCV、数据目录锚定、升级先删 `_internal`）
见 [`README.md`](README.md)「打包」与 `packaging/lxz-wechat-gateway.spec` 顶上的注释。

- **打包只能在 Windows 上**：PyInstaller 收的是**本机的 DLL**，
  而且**打包机的 venv 必须正好是 `pyproject.toml` 的闭包**——环境里多一个包，
  它就可能顺着 import 链被打进去（换干净 venv 重打，包从 33.7 MB 掉到 25.0 MB）。
- ★ **打包必须带上 vendored 的 `third_party/pywechat/pyweixin` 及其依赖要的
  `logger/logging.conf`。** 前者运行时靠 `sys.path.insert` 现找，不在 PyInstaller 的依赖图里，
  漏了就是一动微信报 `No module named 'pyweixin'`；后者是 pywinauto 那条 import 链上
  被**导入时**就读的数据文件，PyInstaller 只收 `.py`，漏了整个 UIA 探测在打包版里一次都不成功。
  两次的表现都是「微信明明开着，界面说找不到微信」（2026-08-16 / 08-17 真机各撞一次），
  **而源码跑、单测跑、在打包机上双击 onedir 里的 exe 都可能不报错**。
  spec 里已经处理，`--self-check` 会守住——别把这两样从 spec 里摘掉。
- **CI 也能打**：[`.github/workflows/build-exe.yml`](.github/workflows/build-exe.yml)
  在钉死的 `windows-2022` 上跑同一条 `packaging/build.py`，环境天然干净。
  往 `refactor/drop-mcp` 推或打 `v*` 标签会触发。
  ★ **CI 绿 ≠ 验过**：runner 上没装微信，自检那条只证到「pyweixin 在包里」，
  证不到真能驱动微信；而且 CI 的包和真机的包**不是同一个字节产物**（同版本差过 9%），
  连 CI 自己前后两跑都不字节一致。**装到真机跑一次仍然不可省。**
- **发版链路**：升 `wxclient/__init__.py` 的 `__version__`（**唯一来源**，别在别处再写一份）
  → 真机全量测试 → 打包前先清 `dist` / `build` → `packaging/build.py`
  → 改产品站页面（版本串有四处，`publish.sh` 会逐条巡检）
  → `bash packages/product-site/scripts/publish.sh <安装包>`（在 `lxz_agent_backend/` 下）。
  ★ **顺序是「页面先改、包后传」**，反了下载按钮就指向 404。
  ★ 页面上贴的 SHA-256 必须取自**你真正上传的那个包**——重打一次复现不出来。
  ★ **旧包一个都不删**，有人拿着旧链接。
- **改了客户端代码就同步升版本号**，别留到「以后发版再说」：代码进仓 ≠ 到用户手里，
  版本号是这两件事之间唯一看得见的凭据。纯文档 / 纯注释改动不算。
- 部署机地址、账号、口令**只放环境变量或本地未入库的配置**，不写进仓库。
- 部署前确认：微信已登录、桌面未锁屏、UIA 探测通过、`--allow-real-actions` 的开关状态符合预期。
- 上线前排除：调试开关、固定测试账号、临时探针、高详细度日志。

---

## 十四、Git 提交

- **主线分支是 `refactor/drop-mcp`**（用户 2026-09-02 确认），不是 `master`。
  GitHub 上的默认分支仍是 `master`，两者不一致带来的一个具体后果：
  Actions 的「Run workflow」手动按钮只按默认分支上的 workflow 列，所以现在点不了。
- **禁止** `git add -A` / `git add .` / `git commit -a`。只 `git add <明确路径>`。
  仓库根常年躺着别人会话留下的未跟踪临时文件（`win*.txt` 之类），一把梭必然夹带。
- 提交前必做 `git status` + `git diff --cached`，肉眼核对暂存内容。
- commit message：`<type>(<scope>): <中文简述>`
  - type ∈ `feat` / `fix` / `refactor` / `chore` / `docs` / `test`
  - scope ∈ `gateway` / `runtime` / `queue` / `wechat` / `uia` / `ui` / `ci` / `docs`
  - 例：`feat(gateway): 入站轮询 + 去抖聚合`
- 提交前 `ruff` 和 `unittest` 必须全绿。
- **只有明确要求才推远端**，默认只在本地提交。
- 完成并验证后提交；**不得回退用户无关的工作区修改**。

### 永不入库

密钥 / token / 口令 / 部署机 IP、真实候选人与聊天记录、`.wxclient/` 运行数据与日志、
`dist/` 构建产物、`__pycache__/`。

---

## 十五、收口状态表（每个任务结束必输出）

每次任务做完，回复**结尾**给一张三列表：

| 测试 | 提交 | 部署 |
|---|---|---|
| ✅ 全绿（写明跑了什么）/ ⏭️ 本次不涉及 / ❌ 失败 | ✅ `<hash>` / 📝 未提交（说明原因）| ✅ 已部署真机验证 / ⏭️ 本次不涉及 |

据实填，不留空、不省略整张表。
**涉及微信行为却没上真机验证的，「部署」一列只能填未验证，不许填已验证。**

---

## 附：外部文档规则

涉及库、框架、SDK、API、CLI 或云服务时，先使用 ctx7 获取当前文档：

1. `npx ctx7@latest library <name> "<完整问题>"`
2. 选择官方且匹配的 `/org/project` ID。
3. `npx ctx7@latest docs <libraryId> "<完整问题>"`

每个问题最多执行 3 条 ctx7 命令，不在查询中放入密钥。ctx7 不用于业务逻辑重构、
纯代码审查或从零编写普通脚本。配额不足时明确提示登录或配置 `CONTEXT7_API_KEY`。
