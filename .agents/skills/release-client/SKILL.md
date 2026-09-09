---
name: release-client
description: 发布劳小智微信助手客户端——升版本号、打包（GitHub Actions 或真机）、改产品站四页、publish.sh 上架到 https://worker.laoxiaozhi.cn/wxauto/、线上独立复核。当用户说「发版 / 发布客户端 / 上架新版本 / 把这个版本发出去 / 打个包发给用户」时使用。跨 ~/wxauto 与 ~/laoxiaozhi/lxz_agent_backend 两个仓。★ 只在用户明确要求发版时才跑：这是对外发布，不是本地构建。
---

# 发布客户端（劳小智微信助手）

**这条链有七步，每一步都能悄悄半途而废**，而失败的表现几乎总是「用户下到的还是上一版」——
不报错、不告警、没人发现。这个 skill 存在的唯一理由就是把那些静默失败点一次列全。

> **前置**：用户**明确**要求发版才动。这是对外发布，包会真的进到用户手里。
> 只是想验证「换个干净环境还打不打得出来」的话，去 Actions 手动跑构建，别走这条。

## 涉及的两个仓

| 仓 | 干什么 |
|---|---|
| `~/wxauto` | 客户端代码、版本号、打包脚本、CI |
| `~/laoxiaozhi/lxz_agent_backend` | 产品站四页 + `publish.sh`（**上架脚本在这边**） |

产品站目录：`packages/product-site/`（`index.html` / `changelog.html` / `environment.html` / `uia.html`）。

---

## 步骤

### ① 升版本号（在 `~/wxauto`）

**唯一来源是 `wxclient/__init__.py` 的 `__version__`**。`pyproject.toml` 是 dynamic 的、
`packaging/build.py` 也从这里读——**别在任何别处再写一份**（历史上 `pyproject.toml` 里那份写死的
`0.1.0` 跟着 25 次发版没人改）。

改完先确认一遍：

```bash
grep -n "__version__" ~/wxauto/wxclient/__init__.py
```

### ② 测试

```bash
cd ~/wxauto && .venv/bin/python -m ruff check wxclient tests packaging
cd ~/wxauto && .venv/bin/python -m unittest discover -s tests    # ★ 别加 -t .
```

**Linux 基线 = 139 skip + 16 error**（全来自 pywinauto / tkinter），真机约 950 条只 skip 1。
**跟基线比，不是跟全绿比。** 涉及微信行为的改动，Linux 绿不算数，必须真机验。

### ③ 出包——两条路，各有各的边界

**路 A · GitHub Actions**（`.github/workflows/build-exe.yml`）：推 `refactor/drop-mcp` 或打 `v<版本>` 标签。
干净环境，天然满足「venv 正好是 pyproject 闭包」和「dist 是空的」两条要求。
从 artifact 里取包，或从日志 grep 三件套：

```bash
# 取 run 的日志（TOK 从 ~/.git-credentials 解析，别打印出来）
grep -E "^(package|bytes|sha256)=" <日志>
```

**路 B · 真机打包**：代码走 `git bundle` 送上真机（真机 remote 是 https 且没存凭据，
`git fetch origin` 会卡在 `could not read Username`，别指望在那台机器上 pull），
**打包前先 `rmdir /s /q dist build`**，再跑 `packaging\build.py`。

★ **两条路出的包不是同一个字节产物**（同版本量到过差 9%），连 CI 自己前后两跑都不字节一致。
所以：**页面上贴的 SHA-256 必须取自你真正要上传的那一个包**，重打一次复现不出来。

★ **CI 绿 ≠ 验过。** runner 上没装微信，自检那条只证到「pyweixin 在包里」，
证不到真能驱动微信。**装到真机跑一次不可省。**

### ④ 真机装一次并验「装上去的就是刚打的」

**别再拿字符串 grep 安装目录**——业务代码进了 exe 内嵌的 PYZ（zlib 压缩），
生字节里搜不到，连版本号都搜不到。「搜不到」不是「没装上」，照着它判会得出反结论。

现在的证法是**字节比对**：安装目录 `%LOCALAPPDATA%\Programs\劳小智微信助手\` 下的
`劳小智微信助手.exe` 和 `_internal\base_library.zip`，跟打包产物里的同名文件逐个 `Get-FileHash` 对上
（安装目录会多 2 个文件：`unins000.exe` / `unins000.dat`）。

### ⑤ 改产品站四页（在 `~/laoxiaozhi/lxz_agent_backend/packages/product-site/`）

★ **顺序是「页面先改、包后传」。** `publish.sh` 会拿包名版本和页面版本对一遍，对不上当场停；
反了就是下载按钮指向一个 404，而服务器那边一个错都不报。

要改的地方（`publish.sh` **会**逐条巡检，漏一处发不出去）：

| 位置 | 写法 |
|---|---|
| 四页侧栏 | `当前版本 X.Y.Z` |
| 四页侧栏日期 | `<div>YYYY-MM-DD 发布</div>` |
| 四页页脚 | `劳小智微信助手 vX.Y.Z ·` |
| `index.html` 首屏按钮 | `下载安装包 vX.Y.Z` |
| `index.html` 下载区 | `<p class="dl__v">vX.Y.Z<span>YYYY-MM-DD 发布</span></p>` |
| `index.html` 下载链接 | `href="download/…_vX.Y.Z_安装包.exe"`（**百分号编码**） |
| `index.html` SHA-256 | 那一段里的校验值 |
| `environment.html` | 示例文件名 `劳小智微信助手_vX.Y.Z_安装包.exe` |
| `changelog.html` | 新增一节，最新那条 `rel__v` 必须等于本版 |

⚠️ **`dl__meta` 里的包体积是手写的，`publish.sh` 不校验它。** 从 0.1.41 起就一直写着 23.9MB
而实际早变了——**记得自己改**，没人会拦你。

⚠️ 下载链接那条尤其阴：文件名是百分号编码的，中文字面量的巡检模式匹配不到它，
而**服务器上旧包是留着不删的**，所以停在旧版不会 404，只会**安安静静把上一版发给所有人**——
比 404 难发现得多。`publish.sh` 为此单配了一条正向断言，别把它绕过去。

### ⑥ 上架

```bash
cd ~/laoxiaozhi/lxz_agent_backend
bash packages/product-site/scripts/publish.sh <安装包路径>
# 只改了文案/样式、不换包：
bash packages/product-site/scripts/publish.sh --site-only
```

脚本自己会做：版本串与日期巡检 → 包名版本对齐 → 页面 SHA 与包对齐 → 传包 → **传完立刻核远端 hash**
（传输截断不报错，只会让用户装到半个包）→ 同步四页 → **写 `latest.json`**（客户端自动更新的唯一来源：
版本 / URL / SHA-256 / 字节数 / 日期 / changelog 条目，全部从**本次上传的包**算）→ 兜底重建软链接
（`/var/www/lxz_business/wxauto` → `/var/www/lxz_wxauto`，业务端发版可能把它删掉）→ 线上验证
（含把 `latest.json` 读回来核版本与 SHA）。

★ `latest.json` 一写出去，**所有已装 0.1.53+ 的客户端 6 小时内都会提示更新**——发版就是推送。
所以 ③④ 那两步（真机装一次、跑一次）在这之前必须做完；发出去才发现有问题，只能再发一版更高的。

**禁止**用 `scp` / `ssh` / `rsync` 手工拼这几步代替脚本。这条是仓库根的硬规矩，
理由就是上面那一串「每步都能悄悄半途而废」。

### ⑦ 自己再独立复核一遍线上

脚本验过一遍，**你再验一遍**——「脚本说成功了」和「用户真能下到新版」之间还隔着缓存和软链接。

```bash
curl -fsS https://worker.laoxiaozhi.cn/wxauto/ | grep -o 'v[0-9.]*' | head -3   # 首页版本
curl -fsS "https://worker.laoxiaozhi.cn/wxauto/latest.json?t=$(date +%s)"       # 客户端会拿到的清单
# 页面上贴的 SHA、整包拉回来重算、旧版链接仍 200、latest.json 的 version/sha256 与包一致，四样都要看
```

**旧包一个都不删**——有人拿着旧链接。

---

## 收口

发完在回复结尾给 AGENTS.md §15 那张三列表，并把这几个数写清楚：
包名、字节数、SHA-256、线上首页版本。**「打过包」不等于「发布过」**——
发版前先看 `index.html` 的 `dl__v`，别拿 `__version__` 当「已发布版本」
（站上版本一度停在 v0.1.27 而仓库已到 0.1.30）。

## 绝不做

- 用户没明确要求就发版。
- 手工 `scp`/`ssh` 代替 `publish.sh`。
- 删服务器上的旧包。
- 把 CI 绿当成真机验过。
- 页面 SHA 从别处抄，而不是从**要上传的那个包**算。
- 涉及微信行为却没上真机，还在收口表里写「已验证」。
