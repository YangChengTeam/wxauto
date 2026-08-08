# Windows 微信客户端架构方案

## 目标能力

基于 `third_party/pywechat/pyweixin` 和只读 UIA 探测，先做一个本机 Windows 常驻客户端，提供三类能力：

1. 通过手机号、微信号或可搜索账号主动添加微信好友。
2. 检查新的好友申请，并按策略自动同意。
3. 监听或发起微信聊天，把新消息交给大模型生成回复；后续可替换为正式智能体。

所有能力都必须经由一个统一的 UIA/pyweixin 操作队列执行。不要让定时器、UI 按钮、智能体线程直接操作微信窗口。

## 推荐模块

```text
wxclient/
  app/                         # Windows 客户端入口、生命周期、托盘/窗口
  config/                      # 账号、触发器、白名单、限频、LLM 配置
  uia_probe/                   # 只读 probe_wechat_uia
  wechat_api/                  # pyweixin 二次封装
  trigger/                     # 定时任务、本地事件
  queue/                       # 单消费者任务队列、去重、重试、状态机
  agents/                      # 大模型或后续智能体
  agentbridge/                 # WeChat 事件 <-> Agent 请求/响应协议
  storage/                     # SQLite：任务、消息游标、好友申请记录、审计日志
  ui/                          # 配置、状态、人工确认、运行日志
  diagnostics/                 # UIA 诊断、截图/控件树、健康检查
```

已有 `third_party/pywechat` 保持 vendor 化二次开发；业务侧只通过 `wechat_api` 调用，不直接 import pyweixin。

## 调用边界

`uia_probe` 只负责只读 UIA 状态探测：

```python
probe_wechat_uia() -> dict
```

`wechat_api` 只暴露业务动作：

```python
add_friend(account_id, number, greetings, remark=None, dry_run=True)
check_new_friends(account_id, verify=False, limit=8, dry_run=True)
send_message(account_id, friend, text, dry_run=False)
read_visible_messages(account_id, friend)
listen_chat(account_id, friend, duration)
```

每个真实操作前都执行：

```python
probe_wechat_uia()
```

如果 UIA 不可用，任务进入 `blocked` 状态，由 UI 提示用户手动处理。探测层不得启动、关闭、重启或切换微信窗口，也不得修改系统辅助功能状态。

## 触发方式

推荐支持这些触发方式，但都只进入任务队列。

1. 定时轮询

用于微信侧没有稳定事件源的场景：

- 每 10 到 30 秒检查新消息红点或指定会话。
- 每 30 到 120 秒检查新的好友申请。
- 每 5 到 15 分钟跑一次 UIA 健康检查。

好友申请自动同意适合定时检查，因为 pyweixin 的 `Contacts.check_new_friends(verify=True)` 本质是 UI 自动化动作。

2. 本地 UI 操作

用于人工确认和调试：

- 手动添加好友。
- 手动检查/通过好友申请。
- 手动发送测试消息。
- 重新执行 UIA 探测诊断。

## 核心执行模型

必须是单消费者 UI 操作队列：

```text
Scheduler / UI
        |
        v
  Task Store + Queue
        |
        v
  WeChatOperationWorker(single consumer)
        |
        v
  probe_wechat_uia -> pyweixin -> result/audit log
```

原因：pywechat 文档明确提示不要多线程同时操作同一个微信界面，否则容易死锁或卡 UI。可以多线程接收事件、调用大模型、写数据库，但微信 UI 自动化动作必须串行。

## 三个业务流程

### 主动加好友

```text
UI/CLI 提交 add_friend
  -> 校验手机号/微信号、白名单、频率限制
  -> 入队
  -> probe_wechat_uia
  -> FriendSettings.add_new_friend(...)
  -> 记录结果
```

默认 `dry_run=True`，正式启用后再允许真实提交。建议加每日上限、同手机号去重、失败冷却。

### 自动同意好友申请

```text
定时器触发 check_new_friends
  -> probe_wechat_uia
  -> Contacts.check_new_friends(verify=False)
  -> 写入申请列表
  -> 策略判断
      -> 白名单/关键词命中：再入队 accept_new_friends
      -> 不确定：UI 待确认
  -> Contacts.check_new_friends(verify=True, limit=N)
```

不要一开始就无条件 `verify=True`。第一版建议：

- dry-run 查看申请。
- 仅对白名单手机号、备注关键词、外部系统预创建申请自动通过。
- 其他申请进入 UI 待确认。

### 聊天和大模型回复

```text
定时器扫描新消息或监听指定会话
  -> 标准化为 MessageEvent
  -> agentbridge 构造上下文
  -> agents 调用大模型
  -> 输出 ReplyAction
  -> 入队 send_message
  -> Messages.send_messages_to_friend(...)
```

大模型部分和微信 UI 操作解耦。模型可以并发跑，但最终回复动作仍通过单队列执行。

## 稳定性判断

不能承诺 100% 稳定。这个方案依赖 Windows UIA、微信 PC 版本控件结构、窗口焦点、登录状态和系统无障碍行为，天然比官方 API 脆弱。

可以做到工程上可用：

- 固定支持范围：Windows 10/11、Python 3.9+、微信 4.1.9.x 优先。
- 启动时只运行 `probe_wechat_uia()` 确认节点是否暴露。
- 每次任务前检查 UIA 状态，失败进入可恢复状态。
- 所有真实动作有 `dry_run`、限频、重试、审计日志。
- UI 操作串行化，避免死锁。
- pyweixin 补丁保留：优先选择 `mmui::MainWindow`，不要误选 `mmui::LoginWindow`。
- 做 watchdog：发现微信重启、UIA 退化到 `1/1/1`、登录窗口出现时暂停队列。

稳定性目标建议这样定：

- 单账号、固定微信版本、固定机器：可以作为半自动客服工具长期运行。
- 大规模无人值守、强 SLA、跨版本自动适配：不适合承诺。

## 多微信支持

短期不建议把“多个微信”当作同进程并发能力做。

可支持的分级方案：

1. MVP：单 Windows 用户会话 + 单微信账号。
2. 第二阶段：同一机器多个 Windows 用户会话/虚拟桌面/虚拟机，每个客户端实例只控制一个微信。
3. 第三阶段：为每个账号维护 `account_id -> window_handle/pid/profile`，但 UI 操作仍然按账号串行，并且同一物理桌面不要并发操作多个微信窗口。

pywinauto 文档建议通过窗口句柄连接更稳定，所以多账号必须在启动时绑定：

```text
account_id
wechat_path
process_id
main_window_handle
display_name
uia_status
```

如果微信官方客户端本身不允许稳定多开，就不要在本方案里绕过多开限制。更稳的产品形态是“一账号一 Windows 会话/一 VM/一客户端实例”，后续如需外部聚合再单独设计上层通信。

## UI 建议

第一版 UI 不需要复杂：

- 首页：微信状态、UIA 状态、当前账号、队列状态。
- 任务页：待执行、执行中、失败、已完成。
- 好友申请页：申请列表、自动通过规则、手动通过按钮。
- 聊天页：最近消息、模型回复草稿、发送状态。
- 设置页：微信路径、LLM 配置、限频策略。
- 诊断页：运行 `probe_wechat_uia`、导出控件树、查看最近错误。

## 最小落地顺序

1. 封装只读的 `uia_probe.probe_wechat_uia()`。
2. 封装 `wechat_api.send_message()`，先打通文件传输助手。
3. 加单消费者任务队列和 SQLite 任务表。
4. 加定时 `check_new_friends(verify=False)`，先只展示申请。
5. 加策略化自动通过，默认 dry-run。
6. 加消息轮询 -> agentbridge -> LLM -> send_message。
7. 最后再做多实例管理。
