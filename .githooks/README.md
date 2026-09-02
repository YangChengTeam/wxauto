# .githooks

提交前闸。**每一条都对应 AGENTS.md 里已经写下的规矩或一次真实事故**，不在这里发明新规则。

## 启用（每个克隆各做一次）

```bash
git config core.hooksPath .githooks
```

`core.hooksPath` 是**本地配置、不随仓走**——克隆下来不会自动生效，新机器上要手动配一次。
（这是 git 的设计，不是我们漏了什么：仓库里的脚本能在你机器上自动执行，那是个安全问题。）

## 查什么

| # | 闸 | 依据 |
|---|---|---|
| ⓪ | `CLAUDE.md` / `.claude` 必须还是软链（mode 120000），`.agents` 不能变 submodule | 拆了两份规约又开始各自漂 |
| ① | 永不入库清单：`.env`、`tickets.json`、`console-account.json`、`gateway.env`、`client-session.json`、`dist/`、`build/`、`*.log`、`__pycache__/`、`.wxclient/` | AGENTS.md §14 |
| ② | 暂存内容里出现生产机 IP | AGENTS.md §5.1 红线 |
| ③ | 别的会话留下的临时产物（`win*.txt` / `probe_*.py`） | AGENTS.md §14 禁 `git add .` 的那类夹带 |
| ④ | 改了 `wxclient/` 的代码就必须升 `__version__` | 用户 2026-08-30 定的规矩；踩过一次，用户装的还是上一版 |
| ⑤ | 暂存的 Python 过 ruff | AGENTS.md §14 |

①②③ 多数已被 `.gitignore` 挡住。这道闸管的是**绕过去的那条路**：`git add -f`、换个文件名、
或者哪天有人手滑删了 `.gitignore` 那行。

## 放行

```bash
git commit --no-verify                     # 整条闸
LXZ_SKIP_VERSION_CHECK=1 git commit ...    # 只放行版本号那条（纯注释/文档改动）
```

## 改了钩子就重跑自检

```bash
bash .githooks/selftest.sh
```

它在临时克隆里对每条闸造一次「该被拦」的改动、再造几条「不该被拦」的，逐条断言。

★ **别靠读代码判断闸还灵不灵。** 第一版的 ⓪ 就是假绿：软链变普通文件在 git 里是
**T（类型变更）**，而当时的 `--diff-filter=ACM` 不含 T，于是整个钩子在第一行就空手退出、
一声不吭地放行。看代码看不出来，造一次真改动才抓到。

## 设计取向

**宁可漏报也别误报。** 一条动不动就拦错的闸，三天之内就会被 `--no-verify` 常态化，
那时它一条都拦不住了。所以这里只放「拦下来一定是对的」那几项——
**测试不在其中**（Linux 上一跑两分钟，放进 pre-commit 只会让人整条绕过去）。
