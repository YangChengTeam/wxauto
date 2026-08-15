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

## 桌面端登录与参数下发（2026-08-15）

- 界面**先登录后使用**：登录之前看不到日志/配置/计划。不许加「跳过登录」的开关。
- 两个 provider，接口一样（`wxclient/ui/auth.py`）：配了 `WXCLIENT_UI_LOGIN_API_URL`
  走后端（契约 `REMOTE_CONTRACT`：`POST /desktop/login` + `GET /desktop/config`，**后端待实现**），
  没配走本机账号（scrypt 存哈希，首次运行当场设）。
- 后端下发的参数落 `gateway.remote.env`（整份覆盖），本机改的落 `gateway.env`，
  **同名键本机盖过后端**；和后端相同的值不在本机留副本，否则保存一次就把后端架空了。
- token 只在内存里，不落盘；「记住账号」只记账号名。
- 服务不等人登录：有上次的参数就照常自启；登录管的是「谁能看、谁能改」。
- 界面三页：实时日志 / 计划中心（后端下发，现为空）/ 配置中心（参数 + 只读监听名单）。
  名单权威仍在 AI 端（WS 推），桌面端不给编辑。

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

- 修改后运行 `python -m ruff check wxclient tests`。
- 运行 `python -m unittest discover -s tests -v`。
- 网关单体 exe 尚未做；`[build]` extra 里的 PyInstaller 为它预留（见 `README.md`「打包」）。
  真做时打包必须包含 `third_party/pywechat/pyweixin` 和其所需的 `logger/logging.conf`。
- 完成并验证后提交 Git；不得回退用户无关的工作区修改。
