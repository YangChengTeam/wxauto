# wxauto-gateway

Windows 微信自动化网关，服务于劳小智招聘业务：把入站微信消息翻译成一次 AI 对话轮，
再把 AI 产出的回复翻译回微信消息发出去。所有微信动作通过单消费者队列串行执行。

## 能力

- 入站轮询取消息、按 `msg_id` 去重、连发去抖聚合、同会话严格串行
- 连劳小智 AI 端（WS 客户端：换票 → `session.start`/`resume` → `message.send` → 取卡）
- 确定性守门：拒绝词表、频次上限、工作时间窗、黑名单
- 发送消息、加好友、监听会话、读取可见消息、发布朋友圈
- 只读探测微信 UIA 可用性
- 桌面端：托盘常驻 + 日志窗口 + 参数设置
- SQLite 任务记录、同类待执行任务去重、单 worker 串行操作微信
- 真实动作三重确认（`--allow-real-actions` + `dry_run=false` + 确认短语）

本项目**不做对话理解、不生成回复内容、不接大模型**——那些全在劳小智 AI 端
（`lxz_agent_backend`）。本仓只负责传输、编排、执行，也不做定时调度。

## 安装与启动

要求 Windows 10/11、Python 3.10+、已登录的微信 PC 端，以及未锁屏的交互式桌面。

```powershell
python -m pip install -e .
python -m wxclient.ui                                                          # 桌面端：开机即跑，缩到托盘
python -m wxclient.gateway --account wxid_zhangsan_work                        # 命令行：dry-run，不真发
python -m wxclient.gateway --account wxid_zhangsan_work --allow-real-actions   # 命令行：真发
```

## 桌面端（托盘）

`python -m wxclient.ui` 打开即启动服务并**直接缩到右下角托盘**，不弹窗。

- 双击托盘图标 → 日志窗口（和日志文件里同一份内容，可暂停滚动、清空）
- 托盘菜单 → 启动 / 停止服务、参数设置、退出
- 托盘图标颜色即服务状态：绿=运行中，灰=已停止，红=启动失败（悬浮看原因）
- 关闭日志窗口 = 缩回托盘，**不会停掉服务**；真要退出走托盘菜单「退出」

参数设置界面写的是 `<data_dir>/gateway.env`（本机运行数据，永不入库）。真实环境变量
优先于该文件，除非在界面上点了「保存并重启服务」。内部 key 在界面上只显示掩码，
没动过就不会被写回。

**默认 dry-run**：整条回路都跑，但不真往微信发消息。要真发必须显式加
`--allow-real-actions`。接口契约与红线见 [`CLAUDE.md`](CLAUDE.md)，实施清单见
[`docs/gateway-plan.md`](docs/gateway-plan.md)。

## 打包

网关的单体 exe **尚未做**（`[build]` extra 里的 PyInstaller 是为它预留的）。
配置已备好：`packaging/lxz-wechat-gateway.spec`（**未验证**，出包时要补依赖收集并在真机跑）。
原先的 `scripts/build_mcp_onefile.py` 只打 MCP 入口，已随 MCP 一并删除。

图标是**一份管到底**：`wxclient/ui/assets/app.ico`（劳小智正式图标套件，和网站后台同一个）
供 exe / 桌面快捷方式 / 任务栏 / 窗口标题栏用；右下角托盘用 `app-256.png` 加一个状态色角标
（绿=运行中、灰=已停止、红=启动失败）——角标不能省，它是唯一能一眼看出服务在不在跑的地方。
换图标只改 `wxclient/ui/assets/`，别在代码里再画一个。
做网关 exe 时注意两件 onefile 特有的事：数据目录（SQLite 队列 / 网关状态库 / 日志）
必须落到 exe 外的固定路径，不能跟着临时解包目录走；凭证与 `--account` 走环境变量和
命令行，不编进二进制。

## 架构边界

```text
wxclient/
  ui/           # 桌面端：托盘 + 日志窗口 + 参数设置（不含业务逻辑，只是网关的一层皮）
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
