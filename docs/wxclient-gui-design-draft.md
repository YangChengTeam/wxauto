# wxclient GUI 设计稿

> 说明：当前会话没有连接 Figma MCP，也缺少 `figma-use` 技能，因此这版先产出可落图到 Figma 的设计规格与本地 SVG 视觉稿。Figma 连接好后，可按本文的 frame、组件和 token 直接重建。

## 设计目标

- 面向长期值守的 Windows 微信自动化客户端，不做营销页，首屏就是工作台。
- 优先让用户看清三件事：微信/UIA 是否可用、worker 是否在跑、哪些任务正在排队或阻塞。
- 所有真实动作都保留确认感：启动服务、发送朋友圈、添加好友、自动回复等操作要有明确按钮、状态和任务记录。
- 页面保持低噪声、适合反复使用：信息密度高，但不要把日志、任务表、配置项互相挤压。

## 画板

| Frame | 尺寸 | 用途 |
| --- | --- | --- |
| `Desktop / Run Console` | 1280 x 780 | 默认首页，运行日志与服务状态 |
| `Desktop / Automation Detail` | 1280 x 780 | 好友检测、未读回复、消息监听、添加好友、朋友圈共用结构 |
| `Desktop / Settings` | 1280 x 780 | 基础设备与 Agent 设置 |
| `Desktop / Blocked State` | 1280 x 780 | UIA 不可用、微信未登录、任务阻塞时的状态 |

视觉预览：[`docs/wxclient-gui-design-draft.svg`](wxclient-gui-design-draft.svg)

## 布局

- App shell：横向布局，左侧导航固定 `248px`，右侧内容自适应。
- Sidebar：内边距 `14px`，分区间距 `12px`。
- Content：内边距 `28px 24px 28px 28px`，垂直间距 `16px`。
- 页面标题区：左侧标题，右侧主操作按钮。首页主操作为“启动服务 / 停止服务”。
- 自动化页：上方表单面板，下方可折叠任务面板。任务面板默认隐藏，由“设置 / 基础设备 / 显示自动化任务列表”控制。
- 日志区：首页大块日志，自动化页只展示任务摘要，避免页面被日志挤高。

## 设计 Token

| Token | 值 | 用途 |
| --- | --- | --- |
| `color.bg.app` | `#F7F7F4` | 应用背景 |
| `color.bg.sidebar` | `#F1F0EC` | 左侧导航 |
| `color.surface` | `#FFFFFF` | 面板、输入框 |
| `color.border` | `#DEDBD3` | 默认描边 |
| `color.text.primary` | `#171717` | 主文本 |
| `color.text.secondary` | `#5F5F5F` | 辅助文本 |
| `color.text.muted` | `#77736B` | 分区标签 |
| `color.action.primary` | `#111111` | 主按钮、选中导航 |
| `color.success` | `#1F9D55` | UIA 正常、服务空闲 |
| `color.warning` | `#D99000` | 执行中、需要注意 |
| `color.danger` | `#D64545` | 阻塞、失败 |
| `radius.panel` | `8px` | 面板、品牌卡 |
| `radius.control` | `7px` | 按钮、输入框、导航项 |
| `space.page.x` | `28px` | 内容左右留白 |
| `space.panel` | `20px 22px` | 表单面板内边距 |

字体优先级与当前代码一致：`Microsoft YaHei UI`, `Microsoft YaHei`, `SimHei`, `Segoe UI`。

## 组件

### Sidebar

- `BrandPanel`
  - 46 x 46 logo 容器，使用 `wxclient/ui/assets/app-logo-64.png`。
  - 标题：`AI微信自动化`。
  - 内嵌 `UiaStatusPill`，显示“UIA 已就绪 / 正在检测 / 需要登录 / 未解锁”。
- `OperationStatus`
  - stopped：`服务未启动`，说明“启动服务后开始处理任务”。
  - idle：`服务运行中`，说明“队列空闲 / 等待任务”。
  - busy：`正在处理任务`，说明当前任务名称。
- `NavItem`
  - 日志、好友检测、未读回复、消息监听、添加好友、朋友圈、设置。
  - 选中态为黑底白字；hover 为浅灰底。

### Page Header

- 标题使用 `22px / 700`。
- 主按钮宽度建议 `168px`，和自动化页主动作一致。
- 按钮文案按状态切换：`启动服务`、`停止服务`、`启动好友检测`、`停止好友检测`。

### Form Panel

- 白底 `8px` 圆角，描边 `#DEDBD3`。
- Label 左对齐，字段自适应。
- `QTextEdit` 在短文本场景固定高度 `34px`，和当前实现一致。
- 危险真实动作附近用确认弹窗，不在表单内堆长说明文案。

### Task Panel

- 默认折叠，标题行包含折叠箭头、任务类型、摘要。
- 摘要格式：
  - 空：`当前没有未完成任务`
  - 非空：`未完成任务：待执行 2，阻塞 1`
- 展开后显示清除按钮、空状态或表格。
- 表格列：`任务 220px`、`状态 96px`、`说明 420px`、`更新时间 150px`，末列可拉伸。

### Log Panel

- 日志面板占据首页主体。
- 顶部右侧 `清除日志` 次级按钮。
- 日志内容使用等宽字体，按时间倒序/追加显示均可，但 UI 上保持单列可扫读。

## 页面内容

### Desktop / Run Console

首屏承载“值守状态”：

- 左侧：品牌、UIA 状态、服务状态、导航。
- 右侧顶部：`运行日志` 标题 + `启动服务` 主按钮。
- 右侧主体：运行日志面板。
- 日志空态：`等待任务日志...`。
- 阻塞日志要高亮原因，如“看到 mmui::LoginWindow，请先登录微信”。

### Desktop / Automation Detail

用于五个自动化功能页，结构一致：

- 好友检测：间隔秒数、处理数量、启动好友检测。
- 未读回复：间隔秒数、目标会话、回复所有未读消息、启动自动任务。
- 消息监听：间隔秒数、监听对象、监听到新消息后自动调用 Agent 回复、启动监听。
- 添加好友：手机号、招呼语、备注、提交添加好友。
- 朋友圈：内容、图片/视频、选择图片/视频、发送朋友圈。

所有页面下方都接对应任务面板，保持“配置动作在上，执行反馈在下”。

### Desktop / Settings

- Tab 1：`基础设备`
  - 数据目录、数据库路径、默认账号、微信路径、显示自动化任务列表。
- Tab 2：`Agent 设置`
  - Provider、Base URL、Model、API Key、Timeout、上下文消息数、System Prompt。
- 底部右侧：`保存设置` 主按钮。

### Desktop / Blocked State

当 `probe_wechat_uia()` 发现登录窗、UIA 未暴露或节点数接近 `1/1/1`：

- Sidebar 的 UIA 状态切为红色。
- OperationStatus 保留 worker 状态，但详情显示“任务已暂停，等待 UIA 恢复”。
- 当前任务表的状态显示 `阻塞`。
- 日志写入一条人话摘要，避免只显示异常栈。

## Figma 图层建议

```text
Desktop / Run Console
  App Shell
    Sidebar
      BrandPanel
      OperationStatus
      Nav / Logs
      Nav / Automation
      Nav / Settings
    Content
      PageHeader
      LogPanel

Desktop / Automation Detail
  App Shell
    Sidebar
    Content
      PageHeader
      FormPanel
      TaskPanel / Collapsed
      TaskPanel / Expanded

Desktop / Settings
  App Shell
    Sidebar
    Content
      PageHeader
      Tabs
      FormPanel / Base
      FormPanel / Agent
      FooterActions
```

## 后续落图步骤

1. 连接 Figma MCP，并提供目标 Figma 文件 URL 或 file key。
2. 搜索或导入设计系统里的 Button、Input、Checkbox、Tabs、Table、Panel/Card、NavItem 组件。
3. 若文件没有现成设计系统，按本文 token 创建本地 styles/variables，再落四个 frame。
4. 逐页截图校验：文字不裁切、任务表不挤压、按钮宽度稳定、折叠任务面板不改变页面主体高度。
