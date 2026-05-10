# AGENTS.md

本仓库目标是做一个 Windows 微信自动化客户端，基于 `third_party/pywechat/pyweixin`
和 UIA 解锁工程项目，先实现加好友、自动同意好友申请、聊天与大模型回复。

## 文档索引

- UIA 解锁、pyweixin API、已验证能力：
  `docs/pyweixin-uia-integration.md`
- Windows 客户端模块、触发方式、稳定性、多微信方案：
  `docs/windows-client-architecture.md`
- 第三方库源码：
  `third_party/pywechat/pyweixin/`
- pywechat 官方说明：
  `third_party/pywechat/README.md`
- 微信 4.1+ UI 自动化说明：
  `third_party/pywechat/Weixin4.0.md`

## 外部文档规则

当问题涉及库、框架、SDK、API、CLI 或云服务时，必须先用 ctx7 查询当前文档：

1. `npx ctx7@latest library <name> "<完整问题>"`
2. 选择最佳 `/org/project` ID。
3. `npx ctx7@latest docs <libraryId> "<完整问题>"`

ctx7 请求要在 Codex 默认沙箱外运行。若遇到 DNS、ENOTFOUND、fetch failed 等网络错误，
按同一命令申请沙箱外重试。不要把密钥、密码、token 放进查询。

## 架构原则

- 微信 UI 自动化动作必须串行执行。
- Webhook、定时器、UI、Agent 只能投递任务，不能直接操作微信窗口。
- 所有 pyweixin 调用必须经由项目封装层，不要在业务代码中直接散落调用。
- UIA 解锁能力独立成模块，业务层只调用 `ensure_wechat_uia()`。
- 真实加好友、同意好友、发消息、发朋友圈必须支持 `dry_run` 或确认机制。
- 默认不做 DLL 注入、Hook、进程伪装、绕过微信限制的实现。

## 推荐模块

```text
wxclient/
  app/           # 客户端入口、生命周期、托盘/窗口
  config/        # 账号、触发器、白名单、限频、LLM 配置
  uia_unlock/    # ensure_wechat_uia/probe_wechat_uia
  wechat_api/    # pyweixin 二次封装
  trigger/       # 定时任务、Webhook、本地事件
  queue/         # 单消费者任务队列、去重、重试、状态机
  agents/        # 大模型或后续智能体
  agentbridge/   # WeChat 事件 <-> Agent 请求/响应协议
  storage/       # SQLite：任务、消息游标、好友申请、审计日志
  ui/            # 配置、状态、人工确认、运行日志
  diagnostics/   # UIA 诊断、控件树、健康检查
```

## UIA 与 pyweixin 调用规则

- 每个业务动作前先调用 `ensure_wechat_uia(restart_if_needed=False)`。
- `probe_wechat_uia()` 只能检测状态，不启动、关闭或重启微信。
- 已解锁时不要重启微信，避免破坏登录状态和当前聊天窗口。
- 若看到 `mmui::LoginWindow`，暂停业务任务，提示用户登录。
- 若 `mmui::MainWindow` 的 raw/control/content 接近 `1/1/1`，视为 UIA 未解锁。
- pyweixin 配置默认保持 `close_weixin=False`、`is_maximize=False`。

## 触发规则

- 好友申请：定时检查为主，Webhook 可触发立即检查。
- 外部发送消息/加好友：Webhook 入队，返回 `task_id`。
- 聊天回复：消息扫描生成事件，Agent 产出回复，再入队发送。
- 定时检查、Webhook、UI 按钮都不得绕过队列。

## 稳定性边界

- 不承诺跨微信版本 100% 稳定。
- 优先支持 Windows 10/11、Python 3.9+、微信 4.1.9.x。
- UI 改版、微信重启、登录失效、窗口焦点异常都要进入可恢复失败状态。
- 任务失败要记录原因、截图/诊断信息、重试次数和下一次处理时间。

## 多微信规则

- MVP 只支持单 Windows 用户会话中的单个微信账号。
- 多账号优先采用一账号一 Windows 会话、虚拟机或客户端实例。
- 不要在同一物理桌面并发操作多个微信窗口。
- 后续多账号必须绑定 `account_id -> process_id/window_handle`，且每账号串行执行。

## 开发顺序

1. 封装 `uia_unlock`。
2. 封装 `wechat_api.send_message()` 并打通文件传输助手。
3. 实现 SQLite 任务表和单消费者队列。
4. 实现 Webhook：发送消息、添加好友。
5. 实现好友申请 dry-run 检查和 UI 展示。
6. 实现白名单策略下的自动同意。
7. 实现消息扫描、`agentbridge`、大模型回复。
