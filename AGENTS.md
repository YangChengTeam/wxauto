# AGENTS.md

本仓库维护 Windows 微信自动化服务，负责微信 UI 自动化和串行任务执行，对外提供一个
入口：主动的 WS 网关（`gateway/`），外加一层桌面壳（`ui/`：托盘 + 日志 + 参数设置）。
仓库不维护独立调用客户端或大模型回复模块。

> 协作约定、跨仓接口契约与红线见 [`CLAUDE.md`](CLAUDE.md)，本文只讲本仓代码规则。

## 文档与源码

- 协作约定与跨仓契约：`CLAUDE.md`
- 网关实施清单：`docs/gateway-plan.md`
- WS 网关：`wxclient/gateway/`
- 桌面端（托盘/日志/参数）：`wxclient/ui/`
- 共享运行时（任务队列 + 执行器 + 动作授权）：`wxclient/runtime.py`
- 微信动作封装：`wxclient/wechat_api/`
- 只读 UIA 探测：`wxclient/uia_probe/`
- 串行任务队列：`wxclient/queue/`
- 第三方 pyweixin：`third_party/pywechat/pyweixin/`

## 外部文档规则

涉及库、框架、SDK、API、CLI 或云服务时，先使用 ctx7 获取当前文档：

1. `npx ctx7@latest library <name> "<完整问题>"`
2. 选择官方且匹配的 `/org/project` ID。
3. `npx ctx7@latest docs <libraryId> "<完整问题>"`

每个问题最多执行 3 条 ctx7 命令，不在查询中放入密钥。ctx7 不用于业务逻辑重构、
纯代码审查或从零编写普通脚本。配额不足时明确提示登录或配置 `CONTEXT7_API_KEY`。

## 架构约束

- 所有微信 UI 动作必须由 `gateway -> runtime -> queue -> wechat_api` 调用；新增入口
  必须复用同一个 `WeChatRuntime`，不得各自复制一份队列与封装。
- 微信 UI 自动化必须由单消费者 worker 串行执行。
- 网关只负责校验参数和投递任务，不得直接操作微信窗口或绕过队列调 pyweixin。
- 装配（票据来源、账号、真实动作开关）只在 `gateway/factory.py` 一处，入口只解析参数。
- 网关不实现对话理解与回复生成——这些在劳小智 AI 端。`gateway` 只做确定性编排
  （去重、去抖、串行、频次、黑名单），话术内容一律来自 AI 端返回。
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
- 真实动作必须同时由服务端开启 `--allow-real-actions`，并由单次调用提供确认短语
  （`wxclient.runtime.REAL_ACTION_CONFIRMATION`）。
- 授权判定只在 `WeChatRuntime._authorize_payload()` 一处，新增入口不得自建旁路。
- 失败任务要保留原因、状态和重试次数，供调用方查询。

## 加好友回什么名字（★ 后续回消息就靠它）

- `add_friend` 返回 `{number, name, remark, session_name}`：`name` 是对方昵称（读名片得来，
  只给人看，会改、会重名、可能带 emoji），`remark` 是我们设的备注，
  **`session_name` = 备注优先、没备注才退回昵称，那才是后面 `send_message(to=...)`、
  监听名单、关系记忆该用的键**——对方通过后会话列表显示的就是备注。
- 所以要能稳稳回消息，加好友时**就得带 `remark`**（例：`张三-仓库工`），别指望昵称。
- 读不到昵称不算失败（`name` 给 `null`，走 `friend.add.fallback`）；但**号码搜不到会直接报错**，
  不再静默返回成功——否则 AI 端会一直等一个根本没发出去的申请。
- `add_friend` 成功只代表**申请已发出**，不代表能发消息了。「现在能回了」的信号是
  `check_new_friends` 的 `items`（对方点了同意）。

## 桌面端登录与状态上报（2026-08-15 起按正式契约）

契约：[`docs/招聘分身_客户端认证与状态接口_v1.1.md`](docs/招聘分身_客户端认证与状态接口_v1.1.md)。
客户端实现分三块：`ui/client_api.py`（四个接口）、`ui/token_store.py`（DPAPI 存 Token）、
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
- 换票 `POST /recruitment-agent/client/session-ticket`（契约 §5.3，后端已实现），
  用客户端 Token，internal key 已删。**请求体是空的**：换谁的票由服务端从会话派生，
  客户端传身份不作数。前置条件是这台机器**当前是 online**（否则 `409 CLIENT_NOT_ONLINE`）
  ——状态上报不是装饰，它是这条链路的开关。
- 参数只有一份 `gateway.env`（客户端接口不下发配置，原 `gateway.remote.env` 那层已删）。
  界面上**一个凭证都没有**；微信号也不再手抄，用登录返回的 `agent.id` 记账。
- 「记住邮箱」只记邮箱，永不记密码。
- **登录之前一概不起服务**：服务只在 `__main__.start_service()` 一处起。
- **「退出登录」= 「退出服务」，只留这一个**（停网关 + 撤销服务端会话 + 回登录页）。
  窗口侧栏和托盘菜单必须一致；托盘菜单 = 实时日志 / 配置中心 / 退出登录 / 退出。
- 界面两页：实时日志 / 配置中心（参数 + 只读监听名单）。名单权威在 AI 端（WS 推），桌面端不给编辑。
  **计划不进桌面端**（用户 2026-08-15 裁决）：几点做什么、多久跟进一次由 AI 端排，
  指令经 WS 推下来执行；客户端连 `plans` 字段都不解析，别再往回加一页。
- 配置中心版式照 `docs/setting.png`：参数分段（`settings_store.SECTIONS`）+ 每段一句说明；
  加一个参数 = 往 `FIELDS` 里加一条（带 `section`/`unit`/`kind`/`placeholder`），**页面不用改**。
  共用小控件（灰字占位、聚焦绿边、胶囊开关、描边按钮）在 `ui/widgets.py`，别各页各写一份。
  「测试连接」只测到网络可达，**不许把话说成「连接正常」**——鉴权对不对只有真连上才知道。
- 监听名单是配置中心里一节只读卡片（`docs/setting2.png`）：一人一行，谁在名单里就是谁会被
  自动回复（069 fail-closed）。「同步名单」是**预留位**，后端没接上之前点了要说清没接，
  别假装同步过。名单权威始终在 AI 端，本机不给编辑。
- 名单标题上那个「下次读消息」倒计时取自 `InboundLoop.seconds_to_next_poll()`——**记的是
  循环真睡的那一觉**（`_next_poll_at`，含抖动与快慢档），不是照配置算的；照配置算会一直和
  现实差几十秒，看着像坏了。没排期/窗外就留白或直说，别给个走到 00:00 却什么都不发生的数。
- 日志按天分文件（`logging_config.DailyFileHandler`，`logs/gateway-YYYY-MM-DD.log`，留 30 天）。
  不用 `TimedRotatingFileHandler`：它把**今天**那份留成没有日期的 `gateway.log`，
  一目录里两种命名，按日期找的人第一眼就找不到今天。

## 微信消息事件上报（未回复跟进的事实来源）

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

## 陌生人主动发起（090 / 接管托管同步接口 v1 §9.5）

契约：[`docs/招聘分身_客户端接管托管同步接口_v1.md`](docs/招聘分身_客户端接管托管同步接口_v1.md) §9。
实现：`gateway/strangers.py` + `gateway_strangers` 表 + `runner._apply_derived_watchlist` 的并集。

- **为什么要有这一层**：入站只读监听名单里的会话，而名单由 `GET /client/contacts` 派生
  （spec 085）＝「后端已经知道的候选人」。主动加好友、主动发第一句话的人**不在里面**，
  他的消息网关根本读不到——AI 端那条「主动收集资料、主动建档」从第一步就走不下去。
- **默认关**（`WXCLIENT_GATEWAY_STRANGER_INTAKE`）。名单是 fail-closed 白名单，
  它的存在理由就是「装上不会自动回复专员的家人朋友」（069 / 需求文档 §13.1）。
  打开它是专员在配置中心的一次知情选择，不该是缺省行为。
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
  （见 `third_party/pywechat/.../WeChatAuto.py`），且日频上限极严。这条路仍然只看不通过。

## 扩展方式

- 新微信能力先加入 `wechat_api` 封装，再增加任务类型和 worker 分发，最后暴露给网关。
- 桌面端只做「登录 + 启动/停止服务 + 看日志 + 看/改参数」，**不得**在 `ui/` 里写任何业务
  判断、话术或微信动作；它只调 `gateway`/`service` 的公开接口。可测逻辑放 `ui/logbuffer.py`、
  `ui/service.py`、`ui/settings_store.py`、`ui/auth.py`，`ui/window.py`、`ui/login_page.py`
  与 `ui/tray.py` 只留 GUI 装配。
- 不恢复独立调用客户端或大模型模块；若产品方向变化，应单独讨论架构。
  （2026-08-12：产品方向确认要桌面端，原「不做 GUI/托盘」的禁令按此收窄为上面一条。）
- `gateway` 的常驻轮询循环是既定架构（入站只能轮询，见 `CLAUDE.md` §4.3），不属于被禁的
  「定时调度」。

## 测试与打包

- 修改后运行 `python -m ruff check wxclient tests packaging`。
- 运行 `python -m unittest discover -s tests -v`。
- 网关单体 exe 尚未做；`[build]` extra 里的 PyInstaller 为它预留（见 `README.md`「打包」）。
  真做时打包必须包含 `third_party/pywechat/pyweixin` 和其所需的 `logger/logging.conf`。
- 完成并验证后提交 Git；不得回退用户无关的工作区修改。
