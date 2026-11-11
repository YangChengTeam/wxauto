#!/usr/bin/env bash
# 钩子自检：对每一条闸造一次「该被拦」的改动，再造几条「不该被拦」的，逐条断言。
#
#     bash .githooks/selftest.sh
#
# ★ 为什么非有这个不可：第一版钩子里 ⓪ 那条是**假绿**——软链变普通文件在 git 里是
#   T（类型变更），而 `--diff-filter=ACM` 不含 T，于是整个钩子在第一行就空手退出、
#   一声不吭地放行。看代码看不出来，是造了一次真改动才抓到的。
#   **改了钩子就重跑这个**，别靠读代码判断闸还灵不灵。
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
git clone -q "$REPO" "$WORK/c" || { echo "克隆失败"; exit 1; }
cd "$WORK/c"
git config user.email t@t; git config user.name t
BASE="$(git rev-parse HEAD)"
install(){ mkdir -p .githooks; cp "$REPO/.githooks/pre-commit" .githooks/pre-commit; chmod +x .githooks/pre-commit; }
reset(){ git reset -q --hard "$BASE"; git clean -qfdx >/dev/null 2>&1; install; }
install
pass=0; failn=0
expect_block(){ local name="$1"; shift; reset; eval "$@" >/dev/null 2>&1
  if .githooks/pre-commit >/dev/null 2>&1; then printf '  ❌ %s：没拦住（假绿）\n' "$name"; failn=$((failn+1))
  else printf '  ✅ %s\n' "$name"; pass=$((pass+1)); fi; }
expect_pass(){ local name="$1"; shift; reset; eval "$@" >/dev/null 2>&1
  if .githooks/pre-commit >/dev/null 2>&1; then printf '  ✅ %s\n' "$name"; pass=$((pass+1))
  else printf '  ❌ %s：误拦\n' "$name"; failn=$((failn+1)); fi; }

echo "该被拦的："
expect_block "CLAUDE.md 软链被拆"   'rm CLAUDE.md; cp AGENTS.md CLAUDE.md; git add CLAUDE.md'
expect_block ".claude 软链被拆"     'rm .claude; mkdir .claude; echo x > .claude/f; git add -f .claude'
expect_block ".env 入库"            'echo "S=x" > .env; git add -f .env'
expect_block "dist/ 产物入库"       'mkdir -p dist; echo x > dist/a.exe; git add -f dist/a.exe'
expect_block "tickets.json 入库"    'echo "{}" > tickets.json; git add -f tickets.json'
expect_block "生产机 IP 进内容"     'echo "H=121.196.149.175" >> docs/gateway-plan.md; git add docs/gateway-plan.md'
expect_block "临时产物 win*.txt"    'echo x > winfoo30.txt; git add -f winfoo30.txt'
expect_block "改代码没升版本号"     'printf "\nX_PROBE = 1\n" >> wxclient/runtime.py; git add wxclient/runtime.py'

echo "不该被拦的："
expect_pass "纯文档改动"            'echo "# x" >> docs/gateway-plan.md; git add docs/gateway-plan.md'
expect_pass "改代码且升了版本"      'printf "\nX_PROBE = 1\n" >> wxclient/runtime.py; v=$(grep -oE "[0-9]+\.[0-9]+\.[0-9]+" wxclient/__init__.py|head -1); sed -i.bak "s/$v/9.9.9/" wxclient/__init__.py; rm -f wxclient/__init__.py.bak; git add wxclient/runtime.py wxclient/__init__.py'
expect_pass "wxclient 纯注释改动"   'printf "\n# 一条注释\n" >> wxclient/runtime.py; git add wxclient/runtime.py'
expect_pass ".env.example"          'echo "K=" > .env.example; git add .env.example'

printf '\n通过 %d / 失败 %d\n' "$pass" "$failn"
[ "$failn" = 0 ]
