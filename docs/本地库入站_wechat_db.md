# 本地库入站（`wechat_db/`）——读微信数据库判新消息，不再开窗轮询

> 状态：入站已实现并随 **0.1.121** 落地（`feat/db-watch`），真机验证待做。
> 加好友的 DB 化是**规划**，尚未动工（见 §7）。
> 红线与提交规则见 [`../AGENTS.md`](../AGENTS.md) §4.3 / §5.3 / §14；入站总体仍在 [`gateway-plan.md`](gateway-plan.md) 的框架里。
> 参照实现：劳小智仓的 `lxz_yingshen`（映身桌面端）的只读读取后端。

---

## 0. 这篇讲什么

把「怎么**发现**新消息」从 UIA 开聊天窗口轮询，换成**读本地微信库**。这篇说清楚：为什么换、
两条链路怎么分工、只读解密怎么做、入站一轮的完整流程、以及加好友三条链路各自能不能吃到 DB 红利。

**不讲**：AI 端对话协议、出站发送、去抖/频控/工作时间窗这些**下游**——它们一行没动，仍见
`gateway-plan.md` 与 AGENTS §4。

---

## 1. 背景：为什么从 UIA 轮询改成读本地库

老链路（`wechat_io.QueueMessageSource`）每一轮为了看「有没有新消息」，要把监听名单里的会话
**挨个开聊天窗口**读一遍（真机一个窗约 9 秒）。名单一长：

- **慢**：对方问一句，最坏要等一整轮才被看见。
- **招风控**：每隔固定节拍把几十个窗口挨个点一遍，正是自动退登事故（2026-08-12）最像的形状。
- **游标脆**：UIA 没有稳定消息 id，游标只能拿「最后一条消息的文本签名」凑，这条消息下一轮不再
  是最后一条、字段补得不一样，就算出别的哈希，游标对不回来 → 自动回复一次都触发不了（真机踩过）。

微信 4.x 把消息存在本机的加密 SQLite 库里。**读它**就能精确、便宜地知道「谁来了新消息、是什么」，
`local_id` 还是稳定自增的游标。这正是 `lxz_yingshen` 已经在做的事，移植过来即可。

---

## 2. 总架构：两条链路 + 失败回退

```
微信消息进来
   │
   ├─【DB 路线】读本地库判新消息         ← 0.1.121 默认走这条
   │   wxclient/wechat_db/  +  gateway/db_source.py
   │
   └─【UIA 路线】开窗轮询读              ← 回退 + 媒体/陌生人/加好友执行专用
       gateway/wechat_io.py (QueueMessageSource)

   两条都产出同一种 InboundMessage
        │
        ▼
   InboundLoop（去抖 / 聚合 / 串行派发）  gateway/inbound.py   ← 节拍全在这，没动
        │
        ▼
   GatewayRunner.handle_turn → AI 端(WS) → 卡片 → 出站发送
```

**选哪条**在 `gateway/factory.py`：
`wechat_db.build_reader()` 扫到密钥、认出当前登录的那个账号目录（换过号留下的旧目录按活跃度排后、密钥对不上自动跳过）→ DB 路线；否则 `build_reader()` 返回 `None`，
**自动回退** UIA 路线，行为与 0.1.120 完全一致。**任何一步失败都安全回退，不拦启动。**

---

## 3. 只读机制（`wechat_db/crypto.py`）

移植自 `lxz_yingshen/scripts/runtime/wechat_local_probe.py`。做两件事，**都是只读**：

1. **扫密钥** `scan_keys(pages)`：对 `Weixin.exe` 用 `ReadProcessMemory` 读内存，找出能通过
   SQLCipher 页校验（`verify_page`，PBKDF2-HMAC-SHA512 + 页 HMAC）的 32 字节密钥。
   布局/匹配基于 `328336690/wechat-decrypt`，运行时密钥定位改编自 `TANGandXUE/wcdb-key-tool`（MIT）。
2. **解密** `decrypt_database_bytes(path, key)`：按页 HMAC 校验 + AES-CBC 解成**内存里**的 SQLite
   镜像；WAL 只取已提交的帧（`committed_wal_pages`）。镜像用 `connection.deserialize` 开，
   `PRAGMA query_only=ON`，不落盘。

> ★ 密钥、任何解密后的字节**绝不写盘、绝不进日志**，日志只有计数（进程数、内存段数、验证通过数）。

---

## 4. 入站流程（`gateway/db_source.py` · `DbMessageSource`）

接口与 `QueueMessageSource` 一致（都实现 `MessageSource`），`InboundLoop` 每拍调一次 `fetch()`。

### 4.1 一轮 `fetch(account)`

1. 解一个很小的 `session.db` → `reader.sessions()` 得到 `{username: (最后一条 local_id, 最后一条类型)}`。
   这一步就知道「谁有新动静」，**不用开窗、不用解各个消息分片**。
2. 名单里**人工接管中**的会话这轮不读，顺手把它们的 `local_id` 游标推到最新（交回分身时不回放老师打的那串）。
3. 对每个要读的会话，按 §4.2 判定归属、产出 `InboundMessage`。
4. 名单外挂红点的陌生人仍交 UIA 的窄路扫（`QueueMessageSource.read_unread_strangers`）。但**只在 session.db 显示名单外单聊挂着红点（`unread_count > 0`）且是新来的（新会话或 local_id 涨了）时才扫**，首轮只建基线不扫（启动时就挂着的多是服务号、企业微信的陈年红点）；群、公众号、文件传输助手不算。每扫一次就会把微信拉到前台，每轮都扫等于没省（0.1.125）。

### 4.2 一个会话这一轮归谁（核心：同一时刻只归一条链路，不重不漏）

比 `session.db` 的 `last_local_id` 和本机游标（`gateway_db_cursors`，由 `GatewayStore.db_cursor` 存取）：

| 情况 | 处理 |
|---|---|
| 游标 == `-1`（UI 持有中）| 委回 UIA；若最后一条又变回文本且 UIA 这轮没东西（追平）→ 游标基线到最新，交回 DB |
| 第一次见（游标 None）| 本地库直接切「尾部连续、今天、对方发的」（同 `first_seen_fresh`，最多 5 条），游标基线到最新；这几条里有媒体才委回 UIA（0.1.126）|
| 名字对不上 / 不在会话表 | 委回 UIA（它走查无此人 → 加好友） |
| `session_max <= 游标` | 没新消息，返回空 |
| 新消息**全是文本** | **直接从库里出**，游标推到这批最后一条 `local_id` |
| 新消息**含语音/图片/文件/视频** | 整段委回 UIA（转写/附件/占位），游标标 `-1`（UI 持有） |
| 结尾连着的语音 | 先压住（转写可能还没落库），不推游标，下轮再看 |

**为什么媒体要「整段委回」而不是混读**：库里媒体消息只有类型、没有转写文字和附件原文。一旦这批
有媒体，就把整个会话这一轮交给 UIA（`QueueMessageSource.read_peer`），语音转写、附件另存、
查无此人计数全走那条**唯一一套、已验证**的逻辑。`local_id` 游标基线前移，两条链路不会同时对
同一条消息出手。

> 已知边界（待真机确认）：文本→媒体切换时 UIA 首见会重读今天尾部。常见情况下网关上一条回复会
> 打断这段 `first_seen_fresh` 的连续段、不产生重复，但这正是只有真机能确认的东西。

### 4.3 游标：为什么 DB 路线更稳

- **UIA 路线**：游标 = 最后一条文本签名 + 前两条上下文签名；字段补得不一样就对不回来（脆）。
- **DB 路线**：游标 = `local_id`，**稳定自增**不漂；自己/对方靠 `Name2Id` 权威判定，
  所以 UIA 那边「网关自己发的回复被读回当成对方新消息、自问自答」在这条链路上**根本不存在**。

### 4.4 真机 schema 要点（来自 `dbprobe.py` 的 `probe1.json`）

- `session/session.db` · `SessionTable`：`username` / `is_hidden` / `last_msg_locald_id`（微信自己拼错的列名，容错两种）/ `last_msg_type`。
- `message/message_<N>.db` · 表 `Msg_<md5(username)>`：`local_id` / `local_type` / `sort_seq` / `real_sender_id` / `create_time` / `message_content` / `compress_content`；外加 `Name2Id(rowid→user_name)`。
- `local_type` 低 32 位才是真类型（真机见过 `(6<<32)|49` 这种复合值）：1 文本、3 图片、34 语音、43 视频、47 表情、49 附件（文件/链接/引用）、10000 系统提示（跳过）。
- 正文：`compress_content` 优先、`message_content` 兜底；zstd（魔数 `28 b5 2f fd`）要解压；群消息正文带 `wxid:\n` 发送者前缀，切掉。

---

## 5. 下游完全不变（这是硬约束）

DB 只换「怎么**发现**新消息」。产出同一种 `InboundMessage`，后面全不变：去重（`msg_id`，DB 路线
= `会话#local_id`，天然唯一）、去抖聚合、工作时间窗、发送频次上限、抖动、串行、AI 端决定回不回。
**风控时序画像没变**，只是不再「为了看有没有新消息而开一堆窗口」。

---

## 6. 配置与回退

- 0.1.128 起**没有开关**：入站一律走本地库监听（`use_local_db` / `prefilter_by_session_list` 删了）。
- **节拍**：固定 `db_source.DB_POLL_INTERVAL_SECONDS`（3 秒，带抖动）看一次库，不走快慢档、不吃 AI 端下发的
  `wechat.pacing`。读库微信看不见，30/180 秒地板是给开窗轮询定的。配置中心「读消息间隔」、极速页「正聊着的
  时长 / 每轮读几个会话 / 正聊着每轮几个」随之删除，老文件里的键启动时清掉（`settings_store._RETIRED_KEYS`）。
- **委回 UIA 也按动静**：归 UIA 的会话（含媒体）只在库里 local_id 涨了才再开窗，停在一张图片上不会每 3 秒开一次。
- 回退触发：非 Windows / 扫不到密钥（微信没登录、版本不支持）/ 所有账号目录都对不上当前微信 →
  `build_reader()` 返回 `None` 并记一条 `info`，走 UIA，节拍用 `GatewaySettings` 里的代码默认值
  （空闲 180 秒 / 聊着 30 秒 / 热窗 600 秒，界面上不再能改）。

---

## 7. 加好友：三条独立链路与 DB 化方案

加好友在本仓是**三条独立链路**，正对应「主动 / 被动」之分。**三条现在都没坏**——DB 入站落地时
一行加好友逻辑都没删；①的触发现在绕道「委回 UIA → 开窗失败 → 老 unreachable 链」，②③是 runner
里各自独立的循环、与入站源无关。

> **一条红线先立**：发申请、点「通过」、改备注都是**写动作**，库只读 → **执行永远在 UIA/`wechat_api`**，
> 搬不走。DB 能动的只有「发现 / 判定 / 确认」这些**读**环节。

### ① 主动加好友——我们去加候选人（`autofriend.py`, 092）

名单里的人还不是好友 → 补发申请。**整条流程和现在完全一样，不改**（用户 2026-11-04）：

| 环节 | 现在 | 改不改 |
|---|---|---|
| 触发 | `on_unreachable`：UIA 开窗连续 N 轮读不到 | **不改**。DB 入站下仍生效：名字对不上 → `DbMessageSource` 委回 UIA → 开窗失败 → 老 unreachable 链 |
| 判定 | `_not_a_contact` → UIA `SEARCH_CONTACTS` | **不改** |
| 号从哪来 | 后端同步的 `contact_number_for` | 不改，号永远来自业务端 |
| 执行 | `add_friend` 指令 → UIA | 不改，写动作永远 UIA |

> DB 在主动加好友这侧**只做一件事：确认（见 ②）**。触发、判定、发申请一律沿用现有逻辑。

### ② 确认①通过了没（`friendcheck.py`, 093）

申请发出 ≠ 对方通过，中间隔个真人。**这一步改用 DB 的系统消息确认**（用户 2026-11-04）：

| 环节 | 现在 | DB 化 |
|---|---|---|
| 判定「对方通过了没」| 隔一阵 UIA `SEARCH_CONTACTS` 复查，搜到了 = 已是好友 | ✅ **改读 DB**：对方一通过，那个会话的消息库里就会出现一条 `local_type=10000`「你已添加了 XX，现在可以开始聊天了」系统消息——读到它 = 确认通过（yingshen 正是这么确认的）|
| 执行 | 只上报（`runner._note_friend_accepted` → `POST /client/contacts` → 进监听名单）| 无写动作，不变 |

**怎么定位那条消息**：主动加好友时设了备注 `remark=session_name`。对方通过后：

1. 他成为联系人 → `reader.resolve_username(session_name)` 能解析出 username（之前 pending 解析不到）；
2. 读他的消息表 `Msg_<md5(username)>`，找**申请时间之后**的 `local_type=10000`「已添加/现在可以开始聊天」系统消息；
3. 读到 → 当场报 accepted；读不到 → 这轮不动（**「读不到」≠「没通过」**，和现有「搜不到≠搜失败」同一条红线，既不报也不计进放弃次数）。

这样 `friendcheck.py` 那条每隔几分钟 `SEARCH_CONTACTS` 开窗复查的循环可以退休——确认改成读库，
库一变（`poll_changes`）就看一眼，不再盲扫。

### ③ 有人加你——候选人加我们，自动通过（`friends.py`）

独立 15 分钟一趟。

| 环节 | 现在 | DB 化 |
|---|---|---|
| 发现新申请 | UIA 盲扫「新的朋友」 | ✅（**待探表确认**）`contact.db` 很可能有待处理申请；库一变就触发检查，替掉盲扫 |
| 执行（点「通过」）| `accept_new_friends` 指令 → UIA | ❌ 写动作，永远 UIA |
| 确认真加上 | UIA 回读 | ✅ 用 10000 系统消息确认 |

**目标形态**：`DB 发现「谁申请了」 + DB 用 10000 消息确认「通过了」 + UIA 只负责点那一下`。
这比每 15 分钟盲扫干净得多——**只在库里真出现申请时才去点**。yingshen 正是「数据库变化触发检查、
UIA 执行、DB 确认」。

### 待探明的表（动工前必做）

`probe1.json` 只拿到了 `contact.db` 这几张表的**名字**，没拿到列和内容：

```
stranger · ticket_info · stranger_ticket_info · room_verify_application
```

看名字 `stranger_ticket_info` / `ticket_info` 最像「谁申请加我」。**动「有人加你」之前，必须先用
只读探测把这几张表的列、行数、以及有没有「待处理/已处理」状态列打出来**（只打结构和计数，
不打印任何昵称/号码），否则拿没核实的表结构写加好友逻辑就是风控事故的形状。探测加在现成的
`dbprobe.py`（只读）里，在 Windows 上跑。

### 建议分期

1. 先把 **DB 入站**真机验完（当前 PR），确认解密读取稳。
2. **主动加好友（①）保持不动**；只把 **② 的确认**从 `friendcheck.py` 的 UIA `SEARCH_CONTACTS` 复查
   改成**读 DB 的 10000 系统消息**。改动小、只读、不碰任何写动作。
3. 探表确认后再做 **「有人加你」侧（③）的发现与确认**，点「通过」仍 UIA。

---

## 8. 红线

- **只读**：扫钥 `ReadProcessMemory`，库解到内存 `query_only` + deny-by-default 授权器打开；
  密钥和明文不落盘、不进日志。**写/改任何微信库永远禁止。**
- **不是「绕过微信限制」的例外**（AGENTS §5.3）：不注入、不伪装进程/窗口、不改无障碍标志。
  这是「读用户自己已经拥有的数据」，和 `lxz_yingshen` 桌面端做的同一件事。
- **所有微信写动作仍走 `gateway → runtime → queue → wechat_api` 串行链路**，加好友/通过/发送概莫能外。

---

## 9. 文件地图

```
wxclient/wechat_db/              ← 新：本地库只读
  crypto.py    扫密钥(ReadProcessMemory) + 页HMAC校验 + AES解密到内存；counts-only 日志
  reader.py    会话变化探测 / local_id 取新消息 / 名字映射 / locate_storages+attach+build_reader(回退)
gateway/
  db_source.py 新：DbMessageSource（归属判定、委回、游标），实现 MessageSource
  wechat_io.py 老 UIA 源；新增 read_peer / read_unread_strangers 两个公开口给委回
  factory.py   选源：DB 优先，扫不到回退 UIA
  config.py    （0.1.128 删了 use_local_db 开关：没有不走本地库的选项）
  sessions.py  gateway_db_cursors 表 + db_cursor / set_db_cursor
  inbound.py   InboundLoop（节拍、去抖、派发）——没动
  autofriend.py / friendcheck.py / friends.py   加好友三条链路（§7，暂未 DB 化）
```

---

## 10. 待真机验证（Windows）

离线测试不等于真机可用（AGENTS §12）。真机上要确认：

- [x] `scan_keys` 在当前微信版本上扫得到密钥；`decrypt_database_bytes` 解出的库能通过完整性检查（真机 2026-11-04：22/22 库验证通过，263 个会话，文本与自己/对方认定正确；attach 约 40 秒）。
- [x] 换过号的机器（一旧一新两个账号目录）不再整条回退 UIA，挑密钥对得上的那个（0.1.123）。
- [ ] 文本消息：`local_id` 游标推进正确，连发/去抖表现与 UIA 路线一致。
- [ ] 媒体委回：语音转写、图片/文件另存走通；文本→媒体切换不重发今天尾部。
- [ ] 回退：微信退登 / 多账号时确实回退 UIA 且有日志。
- [ ] 风控：长时间运行的动作总量、时序画像不劣于 UIA 轮询。

真机验证要留可复核证据：动作类型、目标会话、任务 id、结果、耗时。口头「我测过了」不算（AGENTS §12/§15）。

---

## 11. 测试

- `tests/test_wechat_db_reader.py`：明文 SQLite fixture 冒充解密镜像，验布局解析 / `local_id` 游标 /
  自己对方认定 / 名字映射 / 变化探测 / 复合 `local_type` / 群前缀切除。
- `tests/test_gateway_db_source.py`：假 reader + 假 UIA 替身 + 真 `GatewayStore`，验「一个会话只归一条
  链路」：文本直读、首见/媒体委回、UI 持有后追平再交回、人工接管跳过且推游标。

```sh
# 在仓库根（Linux 可跑；需装 cryptography / zstandard）
python -m unittest tests.test_wechat_db_reader tests.test_gateway_db_source
python -m ruff check wxclient/wechat_db wxclient/gateway/db_source.py
```
