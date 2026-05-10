# pyweixin + WeChat UIA 解锁集成说明

本文档用于把本次 PoC 迁移到后续正式项目，避免重新摸索“解锁 UIA、发消息、收消息、好友、朋友圈”等流程。

## 结论

当前可用路径是分层解锁，不是业务 API 的一部分：

```text
检查当前微信 UIA 状态
  -> 已解锁：直接返回，不重启微信
  -> 未解锁：先启动自研无障碍客户端进程
      -> 成功：关闭自研客户端，进入 pyweixin
      -> 失败：再启动 Narrator
          -> 成功：关闭 Narrator，进入 pyweixin
```

关键观察：

- 微信必须在 Narrator 已启动后再启动，才会暴露可用 UIA 树。
- 微信 UIA 暴露后，关闭 Narrator 仍然可继续读 UIA。
- 如果微信已处于 UIA 暴露状态，不要重启微信，直接进入 pyweixin 功能测试。
- 解锁能力必须做成独立模块，业务层只调用 `ensure_wechat_uia()`，不要把解锁逻辑散落到发消息、收消息、好友、朋友圈 API 里。
- 解锁优先尝试自研无障碍客户端，也就是一个独立的“屏幕阅读器/盲人客户端”风格进程：创建 UIAutomation Client、订阅 UIA 事件、安装 WinEvent hook，可选临时设置 Windows screen reader 标志，但不伪装 Narrator、不注入微信。
- 自研客户端失败后，再 fallback 到系统 Narrator。
- 本 PoC 不做 DLL 注入、Hook、伪装 Narrator 进程。正式项目建议继续走系统无障碍启动路径，风险最低。

## 当前已验证能力

| 能力 | 状态 | 说明 |
| --- | --- | --- |
| UIA 解锁 | 已验证 | Narrator 先启动，再启动微信，可读 `mmui::MainWindow` |
| 发送消息 | 已验证 | `Messages.send_messages_to_friend()` |
| 接受消息/接收消息/读取当前聊天消息 | 已验证 | UIA 读取 `mmui::ChatTextItemView` 等节点 |
| 监听聊天消息 | 已验证 | `Monitor.listen_on_chat()` 和自定义轮询脚本均可 |
| 添加好友 | 已确认接口 | `FriendSettings.add_new_friend()` |
| 验证好友/查看/通过好友申请 | 已验证查看，已确认通过接口 | `Contacts.check_new_friends()` |
| 发朋友圈文字 | 已验证真实发表 | `Moments.post_moments(text=...)` |
| 发朋友圈图片 | 已验证真实发表 | `Moments.post_moments(text=..., medias=[...])` |

## API 总表

| 业务能力 | pyweixin API | 本地验证/辅助脚本 |
| --- | --- | --- |
| 解锁 UIA | `ensure_wechat_uia()`，由正式项目封装 | `wxgui.scripts.narrator_wechat_bootstrap` |
| 发送消息 | `Messages.send_messages_to_friend()` | `wxgui.scripts.pyweixin_a11y_test` |
| 接受消息/接收消息 | `Monitor.listen_on_chat()` 或 UIA 轮询 | `wxgui.scripts.listen_chat_print` |
| 添加好友 | `FriendSettings.add_new_friend()` | 建议正式项目封装 `add_friend()` |
| 验证好友/通过申请 | `Contacts.check_new_friends(verify=True)` | 建议先用 `verify=False` dry-run |
| 发朋友圈 | `Moments.post_moments()` | `wxgui.scripts.pyweixin_moments_dry_run` |

## 目录与关键文件

```text
third_party/pywechat/pyweixin/
  Config.py                 # pyweixin 配置与微信版本探测
  WeChatTools.py            # 窗口定位、导航、朋友圈入口补丁
  WeChatAuto.py             # Messages / Contacts / FriendSettings / Moments / Monitor

src/wxgui/scripts/
  a11y_wechat_bootstrap.py       # 自研无障碍客户端解锁 PoC，优先尝试
  narrator_wechat_bootstrap.py   # 核心 UIA 解锁流程
  pyweixin_a11y_test.py          # 解锁后发送消息测试
  listen_chat_print.py           # 打开会话并监听/打印新增消息
  pyweixin_moments_dry_run.py    # 朋友圈发布面板 dry-run，不真实发表
  uia_diagnose.py                # UIA 诊断与导出
```

## 正式项目建议模块设计

建议把能力拆成两层：

```text
wechat_uia_unlock/
  ensure_wechat_uia()       # 只负责 UIA 解锁和状态检查
  probe_wechat_uia()        # 只检测，不改变微信状态
  unlock_with_a11y_client() # 先尝试自研无障碍客户端
  unlock_with_narrator()    # 兜底使用系统 Narrator

wechat_api/
  send_message()
  listen_chat()
  read_visible_messages()
  add_friend()
  check_new_friends()
  accept_new_friends()
  post_moments()
```

核心原则：

- `wechat_uia_unlock` 不依赖具体业务，只返回 UIA 是否可用。
- `wechat_api` 在调用前统一执行 `ensure_wechat_uia(restart_if_needed=False)`。
- 已经解锁时不要重启微信，否则会破坏当前登录状态、聊天窗口状态和测试上下文。
- 所有真实发送、通过好友、发朋友圈接口都应支持 `dry_run=True`。

## UIA 解锁模块 API

建议正式项目封装为：

```python
from typing import Literal

UnlockStrategy = Literal["auto", "a11y_client", "narrator"]

def ensure_wechat_uia(
    wechat_path: str | None = None,
    strategy: UnlockStrategy = "auto",
    restart_if_needed: bool = True,
    skip_restart_if_unlocked: bool = True,
    prefer_a11y_client: bool = True,
    fallback_to_narrator: bool = True,
    stop_narrator_after_wechat: bool = True,
    stop_a11y_client_after_wechat: bool = True,
    startup_wait: float = 35.0,
    max_depth: int = 10,
    max_nodes: int = 5000,
) -> bool:
    """确保微信暴露可用 UIA 树。"""
```

建议再单独暴露只读检测 API：

```python
def probe_wechat_uia(
    max_depth: int = 10,
    max_nodes: int = 5000,
) -> dict:
    """只检测当前微信窗口是否已登录、是否已暴露 UIA，不启动/关闭/重启任何进程。"""
```

实现流程：

1. 先检测当前微信窗口状态，只检测，不改变进程。
2. 如果存在 `mmui::MainWindow`，说明当前微信主窗口已登录或至少已进入主界面。
3. 检查该窗口 UIA 节点数。如果 raw/control/content 明显大于 10，判定已经解锁，直接返回 `True`，绝不重启微信。
4. 如果当前微信已经登录但 UIA 未解锁，才进入解锁流程。
5. `strategy="auto"` 时，先启动自研无障碍客户端进程。
6. 自研客户端启动后，如果需要触发微信重新初始化 UIA Provider，按 `restart_if_needed` 决定是否关闭/重启微信。
7. 等待 `mmui::MainWindow` 出现，并检查 raw/control/content 节点数。
8. 如果自研客户端成功，关闭自研客户端，再次确认 UIA 仍可读。
9. 如果自研客户端失败，并且 `fallback_to_narrator=True`，再启动 Narrator，重启微信，重复验证。
10. Narrator 成功后关闭 Narrator，再次确认 UIA 仍可读。

状态判断建议：

| 状态 | 判断 | 动作 |
| --- | --- | --- |
| 微信未启动 | 找不到 Weixin/WeChat 进程或窗口 | 可直接进入解锁启动流程 |
| 微信登录页 | 只看到 `mmui::LoginWindow` | 不做业务 API，等待用户登录或启动微信后再检测 |
| 微信已登录但 UIA 未解锁 | 有 `mmui::MainWindow`，但 raw/control/content 接近 `1/1/1` | 先自研无障碍客户端，失败再 Narrator |
| 微信已登录且 UIA 已解锁 | 有 `mmui::MainWindow`，raw/control/content 大于 10 | 直接返回，不重启 |

现成实现参考：

自研无障碍客户端优先：

```python
from wxgui.scripts.a11y_wechat_bootstrap import start_keepalive_child
```

Narrator 兜底：

```python
from wxgui.scripts.narrator_wechat_bootstrap import (
    wait_for_wechat_uia,
    start_narrator,
    stop_narrator,
    close_wechat_windows,
    force_kill_wechat,
    start_wechat,
    guess_wechat_path,
)
```

自研无障碍客户端命令行验证：

```powershell
.\.venv\Scripts\python.exe -m wxgui.scripts.a11y_wechat_bootstrap `
  --set-screenreader `
  --restart-wechat `
  --force-kill `
  --stop-client-after-wechat `
  --startup-wait 35 `
  --max-depth 10 `
  --max-nodes 5000
```

说明：

- `--set-screenreader` 是自研“屏幕阅读器/盲人客户端”路径的增强信号，会临时设置系统 screen reader 标志。
- 子进程退出时会恢复原 screen reader 标志。
- 如果不希望碰系统标志，可去掉 `--set-screenreader`，只保留 UIAutomation Client + WinEvent hook。

Narrator 兜底命令行验证：

```powershell
.\.venv\Scripts\python.exe -m wxgui.scripts.narrator_wechat_bootstrap `
  --restart-wechat `
  --force-kill `
  --stop-narrator-after-wechat `
  --force-stop-narrator `
  --startup-wait 35 `
  --max-depth 10 `
  --max-nodes 5000
```

成功标准：

```text
uia.window : class='mmui::MainWindow' raw>10 control>10 content>10
result     : UIA exposed
```

正式项目伪代码：

```python
def ensure_wechat_uia(...):
    probe = probe_wechat_uia()
    if probe["logged_in"] and probe["uia_exposed"]:
        return True

    if probe["login_window"]:
        return False

    if strategy in ("auto", "a11y_client") and prefer_a11y_client:
        if unlock_with_a11y_client(restart_if_needed=restart_if_needed):
            return True

    if strategy in ("auto", "narrator") and fallback_to_narrator:
        if unlock_with_narrator(restart_if_needed=restart_if_needed):
            return True

    return False
```

## pyweixin 初始化

正式项目里每次调用业务 API 前建议设置：

```python
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root / "third_party" / "pywechat"))

from pyweixin.Config import GlobalConfig

GlobalConfig.close_weixin = False
GlobalConfig.is_maximize = False
GlobalConfig.search_pages = 5
GlobalConfig.clear = True
GlobalConfig.send_delay = 0.2
```

注意：

- `close_weixin=False` 很重要，不要让库操作完就关闭微信。
- `search_pages` 控制搜索联系人时最多翻多少页。
- 当前微信版本验证过 `4.1.9.30`。

## 必须保留的 pyweixin 补丁点

### 1. 微信窗口选择

文件：

```text
third_party/pywechat/pyweixin/WeChatTools.py
```

`WxWindowManage.find_wx_window()` 必须优先选择：

```text
mmui::MainWindow
```

而不是误选：

```text
mmui::LoginWindow
```

否则多微信/登录窗口场景容易定位错。

### 2. 朋友圈入口

同文件：

```text
Navigator.open_moments()
```

当前微信左侧栏没有直接的“朋友圈”按钮时，已验证路径是：

```text
点击左侧栏顶部头像 -> 弹出个人资料卡 -> 点击“朋友圈” -> 进入个人朋友圈页 -> 点击“返回” -> 到朋友圈首页 -> 出现“发表”
```

补丁逻辑必须保留：

- 先尝试 `SideBar.Moments`。
- 找不到则点击左侧工具栏顶部头像。
- 在 `mmui::ProfileUniquePop` 内点击“朋友圈”。
- 如果打开后没有外层 `发表`，但有 `返回`，先点 `返回`。
- 返回后重新定位 `mmui::SNSWindow`。

### 3. 好友申请列表

文件：

```text
third_party/pywechat/pyweixin/WeChatAuto.py
```

`Contacts.check_new_friends()` 已加参数：

```python
return_to_chat: bool = True
```

用途：

- `True`：检查后回到聊天页。
- `False`：停留在“新的朋友”页面，方便继续人工/自动验证。

还修过一条好友申请时可能漏读的问题，迁移时要带上当前版本。

## 发送消息 API

pyweixin API：

```python
from pyweixin.WeChatAuto import Messages

Messages.send_messages_to_friend(
    friend="文件传输助手",
    messages=["张凯你好"],
    search_pages=5,
    clear=True,
    send_delay=0.2,
    is_maximize=False,
    close_weixin=False,
)
```

封装建议：

```python
def send_message(friend: str, text: str) -> None:
    ensure_wechat_uia(restart_if_needed=False)
    Messages.send_messages_to_friend(
        friend=friend,
        messages=[text],
        search_pages=5,
        clear=True,
        send_delay=0.2,
        is_maximize=False,
        close_weixin=False,
    )
```

已验证：

- 给 `文件传输助手` 发送成功。
- 给 `大月` 发送 `晚上我回来也要吃这个` 成功。

测试命令：

```powershell
.\.venv\Scripts\python.exe -m wxgui.scripts.pyweixin_a11y_test `
  --bootstrap none `
  --friend "文件传输助手" `
  --message "张凯你好"
```

## 接受消息/接收消息/读取当前聊天消息

pyweixin 对历史消息读取不稳定时，建议用 UIA 直接读取当前聊天可见节点。

已验证可读的类名：

```text
mmui::ChatTextItemView
mmui::ChatBubbleItemView
mmui::ChatBubbleReferItemView
mmui::ChatVoiceItemView
mmui::ChatItemView
```

现成脚本：

```text
src/wxgui/scripts/listen_chat_print.py
```

测试命令：

```powershell
.\.venv\Scripts\python.exe -m wxgui.scripts.listen_chat_print `
  --friend "大月" `
  --duration 30 `
  --print-initial
```

已验证读取到“大月”的可见消息，包括语音摘要。

## 监听消息 API

pyweixin 官方接口：

```python
from pyweixin import Monitor, Navigator

dialog_window = Navigator.open_seperate_dialog_window(
    friend="大月",
    select=True,
    window_minimize=True,
)

result = Monitor.listen_on_chat(
    dialog_window=dialog_window,
    duration="1min",
    save_file=True,
)

print(result)
```

参数说明：

| 参数 | 说明 |
| --- | --- |
| `friend` | 联系人或群名 |
| `select=True` | 尝试获取消息发送人 |
| `window_minimize=True` | 独立窗口最小化，不影响办公 |
| `duration="1min"` | 监听时长，支持 `s/min/h` |
| `save_file=True` | 保存文件类消息 |
| `save_media=True` | 保存图片/视频类消息 |

本地更稳的监听脚本：

```powershell
.\.venv\Scripts\python.exe -m wxgui.scripts.listen_chat_print `
  --friend "大月" `
  --duration 60
```

建议正式项目：

- 普通文字消息监听：优先用自定义 UIA 轮询，输出稳定可控。
- 文件/图片保存：复用 `Monitor.listen_on_chat()`。

## 添加好友 API

pyweixin API：

```python
from pyweixin import FriendSettings

FriendSettings.add_new_friend(
    number="wxid_or_phone_or_wechat_id",
    greetings="你好，我是张凯",
    remark="备注名",
    chat_only=False,
    is_maximize=False,
    close_weixin=False,
)
```

参数说明：

| 参数 | 说明 |
| --- | --- |
| `number` | 微信号、手机号或可搜索账号 |
| `greetings` | 验证申请文案 |
| `remark` | 添加时设置备注 |
| `chat_only` | 是否仅聊天 |
| `close_weixin` | 必须传 `False` |

正式项目封装建议：

```python
def add_friend(number: str, greetings: str, remark: str | None = None) -> None:
    ensure_wechat_uia(restart_if_needed=False)
    FriendSettings.add_new_friend(
        number=number,
        greetings=greetings,
        remark=remark,
        chat_only=False,
        is_maximize=False,
        close_weixin=False,
    )
```

## 验证好友/查看/通过好友申请 API

pyweixin API：

```python
from pyweixin import Contacts

items = Contacts.check_new_friends(
    verify=False,
    limit=8,
    clear=False,
    is_maximize=False,
    close_weixin=False,
    return_to_chat=True,
)

print(items)
```

只查看：

```python
Contacts.check_new_friends(
    verify=False,
    return_to_chat=False,
    close_weixin=False,
)
```

自动通过：

```python
Contacts.check_new_friends(
    verify=True,
    limit=8,
    clear=False,
    return_to_chat=True,
    close_weixin=False,
)
```

参数说明：

| 参数 | 说明 |
| --- | --- |
| `verify=False` | 只查看，不通过 |
| `verify=True` | 自动点击通过 |
| `limit` | 最多通过多少条 |
| `clear` | 是否清理申请记录 |
| `return_to_chat` | 检查后是否回聊天页 |

已验证查看结果示例：

```text
["范婷婷你好凯哥我是范婷婷等待验证"]
```

注意：

- `verify=True` 会真实通过好友，正式项目必须做二次确认或 `dry_run`。
- 读取列表后如果需要人工确认，使用 `return_to_chat=False`。

## 发朋友圈 API

pyweixin API：

```python
from pyweixin import Moments

Moments.post_moments(
    text="UIA 解锁验证：图片发布测试，由 AI 助手自动发布，请忽略。",
    medias=[r"Z:\path\to\image.png"],
    is_maximize=False,
    close_weixin=False,
)
```

纯文字：

```python
Moments.post_moments(
    text="UIA 解锁验证测试：当前微信 UIA 树已成功暴露，本条由 AI 助手自动发布，请忽略。",
    medias=[],
    close_weixin=False,
)
```

图片朋友圈：

```python
Moments.post_moments(
    text="UIA 解锁验证：图片发布测试，由 AI 助手自动发布，请忽略。",
    medias=[r"Z:\Users\ZK\Desktop\wxauto\output\imagegen\uia-unlock-moments-test-v2.png"],
    close_weixin=False,
)
```

已验证真实发表：

- 纯文字朋友圈发表成功。
- 带图片朋友圈发表成功。

dry-run 脚本：

```powershell
.\.venv\Scripts\python.exe -m wxgui.scripts.pyweixin_moments_dry_run
```

成功输出：

```text
outer_post     : True
publish_panel  : True
sns_edit       : True
panel_post     : True
closed         : true
```

正式项目必须加保护：

```python
def post_moments(text: str, medias: list[str] | None = None, dry_run: bool = True) -> None:
    ensure_wechat_uia(restart_if_needed=False)
    if dry_run:
        # 只打开发布面板，验证控件，不点最终发表
        return
    Moments.post_moments(
        text=text,
        medias=medias or [],
        is_maximize=False,
        close_weixin=False,
    )
```

## UIA 诊断

如果功能突然失败，先跑诊断：

```powershell
.\.venv\Scripts\python.exe -m wxgui.scripts.uia_diagnose `
  --label before-business-call `
  --max-depth 5 `
  --max-nodes 800
```

看点：

- 是否存在 `mmui::MainWindow`。
- raw/control/content 是否大于 10。
- 如果只有 `1/1/1`，说明 UIA 未解锁或读到的是空根节点。

## 新项目最小落地顺序

1. 引入 `third_party/pywechat` 当前补丁版本。
2. 引入 `narrator_wechat_bootstrap.py` 中的 UIA 解锁能力。
3. 封装 `ensure_wechat_uia()`。
4. 封装 `pyweixin` 初始化。
5. 先打通 `send_message("文件传输助手", "测试")`。
6. 再打通 `listen_chat()`。
7. 再接好友申请。
8. 最后接朋友圈，默认 `dry_run=True`。

## 风险与注意事项

- 发消息、通过好友、发朋友圈都是真实微信操作，正式项目必须有 `dry_run` 和确认机制。
- UIA 解锁依赖微信启动时机：Narrator 必须先于微信启动。
- 如果微信重启，UIA 解锁状态可能失效，需要重新执行解锁流程。
- 多微信场景必须优先选择 `mmui::MainWindow`，并在正式项目中增加账号/窗口选择策略。
- 微信 UI 改版可能改变控件名或类名，朋友圈和好友申请功能最容易受影响。
- 不建议正式项目走注入/Hook/进程伪装路线，维护和安全风险都高。

## 本次已验证的关键文案

纯文字朋友圈：

```text
UIA 解锁验证测试：当前微信 UIA 树已成功暴露，本条由 AI 助手自动发布，请忽略。
```

图片朋友圈：

```text
UIA 解锁验证：图片发布测试，由 AI 助手自动发布，请忽略。
```

测试图片：

```text
output/imagegen/uia-unlock-moments-test-v2.png
```
