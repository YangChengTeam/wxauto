# AGENTS.md

**The single authority for how to work in this repo**, for Claude Code / Codex and for humans.
`CLAUDE.md` is a **symlink** to this file — change the rules here only, never split it back into two files.
(Merged 2026-09-02: `AGENTS.md` used to hold code rules and `CLAUDE.md` the collaboration contract; they
referenced each other and drifted apart — `CLAUDE.md` was stuck at 08-18 while `AGENTS.md` had reached 09-01.
One document cannot contradict itself.)

**Language**: this file is English. **Everything else stays in Chinese** — replies, `docs/`, skill prompts,
commit subjects, UI strings, log messages. Never translate those as a side effect.

> 🟢 **Starting a session**: read §2 to learn what must not be touched, §3 to learn where code goes.
> Before changing anything that drives WeChat, read §5 (red lines) and §6 (pacing).
>
> **This file never copies code, and never copies `docs/`.** Contract details live in the versioned
> interface documents under `docs/`; implementation details live in source comments. Here you get
> **constraints and the reasons behind them**.

---

## 1. What this repository is

A **Windows WeChat automation gateway** serving the "WeChat automation recruiting" capability of the
Laoxiaozhi recruiting product. It is the **only side-effecting end of the whole chain** — it is the thing
that actually drives the WeChat window.

> **Translator + gatekeeper**: translate an inbound WeChat message into one AI dialog turn, translate the
> AI's reply back into WeChat messages and send them; meanwhile hold the line on rate, human takeover and
> real-action confirmation using deterministic rules.

**What it does not do** (red lines, §5): no dialog understanding, no reply generation, no LLM. All of that
lives in the Laoxiaozhi AI service (`lxz_agent_backend`). This repo only **transports, orchestrates, executes**.

There is exactly one external entry point: the outbound WS gateway (`wxclient/gateway/`), plus a desktop shell
(`wxclient/ui/`: tray + live log + settings). No standalone calling client, no LLM reply module.

### Three layers of responsibility (across repos)

```
WeChat/RPA ──event──▶ [this repo · Windows gateway] ──WS client──▶ [AI service · Linux]
                ◀──sends via wechat_api──┤                          │ produces reply_segments + actions
                                         └──actions──▶ [business backend · writes DB]
```

- **This repo**: receive messages, dedupe/debounce, serial queue, connect to the AI service, send, gatekeep
- **AI service**: understand intent, judge funnel stage, look up open jobs, generate wording, produce cards
- **Business backend**: sign tickets, write `resume_lead`, blacklist, schedule interviews

---

## 2. Current shape and the frozen surface

**Frozen, reused as-is, do not modify.** The whole redesign was an "inversion"; these three modules did not
change by a single line:

| Module | Responsibility |
|---|---|
| `wxclient/queue/` | SQLite tasks + single-consumer serial worker + idempotency + lease + resume |
| `wxclient/wechat_api/` | pyweixin wrapper (send message / add friend / moments / read messages) |
| `wxclient/uia_probe/` | read-only UIA state probing |
| `wxclient/runtime.py` | task queue + executor + real-action authorization; one per process, shared by every entry |

**What is actively changing is `wxclient/gateway/` and `wxclient/ui/`.** Both are proven on a real machine
and ship to users as an installer (§13).

**`mcp_server/` was removed entirely on 2026-08-12** — do not resurrect it. It was passive, waiting for an
agent to call it; `gateway` is active, it collects messages itself and connects to the AI service itself.
Automation recruiting needs "an event calls the AI", and MCP cannot wake up an inbound message. Only the
irreplaceable runtime (queue + action authorization) was lifted out into `wxclient/runtime.py`.

⚠️ **This section deliberately does not list "features still missing."** Such a list expires every week, and
it expires silently — the previous one was frozen at 08-18 and still described 067/081/085/090 as not started
long after they shipped. To learn what is done, read `docs/gateway-plan.md` and the AI service's
`.agents/specs/`. Never read a hand-maintained status table.

---

## 3. Where code goes

```text
wxclient/
  gateway/      # the only long-running entry: inbound polling, debounce, session mapping,
                # ticket exchange, WS client, outbound sending, gatekeeping
  ui/           # desktop shell: login, tray, live log, settings center
  runtime.py    # shared runtime: task queue + executor + real-action authorization (one per process)
  queue/        # [FROZEN] SQLite tasks + single-consumer worker
  wechat_api/   # [FROZEN] pyweixin wrapper
  uia_probe/    # [FROZEN] read-only UIA state probing
  config/       # WeChat runtime parameters
third_party/pywechat/pyweixin/   # vendored; the package that actually drives WeChat
docs/gateway-plan.md             # gateway implementation checklist
```

**Four iron laws:**

- **Every WeChat UI action must go through `gateway → runtime → queue → wechat_api`.**
  `gateway` may only `submit()` a task. It **must never touch the WeChat window directly, and never
  bypass the queue to call pyweixin**.
- **WeChat UI automation must be executed serially by a single-consumer worker.** A `PENDING` task of the
  same type is never enqueued twice.
- **`gateway` implements no wording logic.** What to reply and how to say it comes entirely from the AI
  service; the gateway only makes deterministic orchestration decisions — whether to send, to whom, and when
  (dedupe, debounce, serialization, rate, human takeover).
- **Any future entry point shares the same `WeChatRuntime`.** No second copy of the queue or the API wrapper,
  and absolutely no side path around `_authorize_payload()`. Wiring (ticket source, account, real-action
  switch) lives in `gateway/factory.py` alone; entry points only parse arguments.

---

## 4. Contract with the AI service

### 4.1 Auth: the gateway holds one key and no employee credential whatsoever

```
[gateway] ──① Bearer client token──▶ [business backend]  exchange for that recruiter's 60s short ticket
[gateway] ──② session_ticket───────▶ [AI service]        open the WS connection
```

Ticket exchange is `POST {business}/recruitment-agent/client/session-ticket` with the client token, and
**the request body is empty** — whose ticket it is is derived server-side from the session; an identity sent
by the client does not count. The precondition is that this machine is currently `online`, otherwise
`409 CLIENT_NOT_ONLINE`. Details in
[`docs/招聘分身_客户端认证与状态接口_v1.1.md`](docs/招聘分身_客户端认证与状态接口_v1.1.md) §5.3.
The business API default lives in `settings_store.DEFAULT_BUSINESS_API_URL` and the AI service WS address in
`wxclient/endpoints.py: DEFAULT_AI_WS_URL` — **go read the values there, never copy them into a document**.

**Four iron laws:**

1. **Never cache, persist or log a ticket.** TTL 60 seconds: fetch it, use it, drop it.
2. **Every connect and reconnect fetches a fresh ticket.**
3. **A failed ticket exchange is always fail-closed** — stop every automated action for that recruiter.
   No degradation, no retry-around.
4. **One recruiter = one WS connection.** A single connection can carry every candidate session under that
   recruiter, but switching recruiter means switching connection.

### 4.2 Dialog: the WS frame sequence

```
{op:"auth",           data:{session_ticket}}        → {ev:"status", state:"authenticated"}
{op:"session.start",  data:{task_type:"..."}}       → {ev:"session.created",  session_id}
{op:"session.resume", data:{session_id}}            → {ev:"session.resumed"}
{op:"message.send",   data:{session_id, text}}      → {ev:"status", state:"running"}
                                                    → {ev:"text.delta", delta} × N
                                                    → {ev:"card", card:{...}}      ★ take this one
                                                    → {ev:"status", state:"done"}
```

- Only `card` counts as output. `text.delta` may only drive a "typing…" hint.
- Frames carry an `id` and responses echo `ack`, so replies can be matched on a shared connection.
- **The session map `(recruiter WeChat ID × peer wxid) → session_id` must be persisted** (local SQLite) so a
  restart can reattach to the same context via `session.resume`.

### 4.3 Inbound is polled, not pushed

`wechat_api`'s `watch_chats` / `get_unread_messages` are **bounded-duration tasks**: they run and return.
There is no "callback on new message". So `gateway` must run its own long-lived polling loop, and it must:

- **Dedupe by `msg_id`** to avoid double triggering
- **Debounce and aggregate**: when the peer sends several short messages in a row, merge them into one turn
  before calling the AI service, otherwise it replies several times
- **Strictly serialize one session**: never open a second turn before the first ends

This long-lived polling loop is **settled architecture**. It is not the "scheduled job" that is forbidden.

**Since 0.1.121 the loop finds new messages from the local WeChat database instead of opening chat
windows to poll** (`wxclient/wechat_db/` + `gateway/db_source.py`, user ruling 2026-11-03). The loop,
its cadence and every gate are unchanged — only *how a round discovers new messages* changed:

- **Text** is read straight from the decrypted DB, cursored by `local_id` (monotonic, so none of the
  text-signature drift the UIA path fights). Self vs. peer comes from `Name2Id`, so the "gateway's own
  reply read back as a new inbound" problem does not exist on this path.
- **Voice / image / file / video** carry no transcript or attachment bytes in the DB, so the whole peer
  for that round is handed back to `QueueMessageSource.read_peer` (UIA) — voice transcription, attachment
  save and not-found→add-friend all stay on the one tested path. A peer is DB-owned or UIA-owned, never
  both in the same round; the `local_id` cursor baselines forward across the handoff.
- **First contact / name mismatch / unlisted red-dot strangers** stay on UIA (first-seen tail, add-friend,
  the §10 stranger window).
- **The DB access is read-only** (see §5.1/§5.3): `ReadProcessMemory` on `Weixin.exe` for the key, page-HMAC
  verify + AES decrypt into an **in-memory** SQLite image. No key or chat text is ever written to disk or a log.
- **It fails safe**: no key / unsupported WeChat version / more than one logged-in account → `build_reader`
  returns `None` and the gateway runs the UIA loop exactly as before (`GatewaySettings.use_local_db` also
  turns it off). Debounce, work-hours window, send-rate limits and jitter are untouched.

The UIA `watch_chats` / `get_unread_messages` path remains the fallback and is still the only way media and
strangers are read; do not delete it.

---

## 5. Red lines (a violation is a defect)

### 5.1 Security

- **Credentials never enter the repository**: tokens, passwords and internal keys go through environment
  variables; only `.env.example` and placeholders may be committed. **The deployment host's real IP, account
  and password go into no committed file.**
- **Real actions need triple confirmation**: server-side `--allow-real-actions` + `dry_run=false` + the
  confirmation phrase (`wxclient.runtime.REAL_ACTION_CONFIRMATION`), all three. Adding a friend, accepting a
  friend, sending a message and posting to Moments all default to `dry_run=true`. The decision lives in
  `WeChatRuntime._authorize_payload()` alone, and **the authorization switch must never be loadable from a
  `--config` file** (otherwise "editing a config file is enough to really send messages").
- **The gateway opens no listening surface**: it is a pure WS client, outbound only, with no port to knock on.
  Should any listening port ever be added, a non-loopback one must require a token and refuse to start without
  it, and crossing the public internet requires VPN or a TLS reverse proxy plus source-IP restriction.
  **Plaintext HTTP in the open is not allowed.**

### 5.2 Business

- **Every new candidate message is dispatched and answered** (user ruling 2026-09-19: "每次客户端轮询消息，
  有新消息是候选人发的我们就需要回复；闲聊也是可以的；去掉闸"). The gateway runs **no keyword gate of its
  own on inbound text** any more — the deterministic "don't contact me / I'll report you → blacklist locally,
  dispatch nothing" gate that used to sit in `InboundLoop._ingest` was removed that day. Whether the peer is
  refusing is judged by the AI service after it has read the actual words; small talk gets a reply too.
- **There is no local blacklist any more** (user ruling 2026-10-09: "不应该有拉黑的情况…明确不想找工作了，
  自动切成人工接管"). The old local blacklist was permanent, invisible and impossible to undo — one "不想找工作了"
  and the peer was never read again. When the AI service decides the peer refuses contact or is no longer
  looking for work, it answers with one closing line and proposes `set_ai_status` → `human_takeover`; the
  gateway applies it as a **candidate-level takeover** through the same path as the recruiter's button
  (`outbound._hand_over` → `GatewayRunner.set_candidate_mode`: local first, then conversation-mode sync),
  **after** that card's segments are sent, and never defers that turn. A legacy `blacklist` action is treated
  the same way. Legacy `gateway_blacklist` rows are migrated to takeovers at start-up and deleted. Never add a
  write path back to a permanent, unrecoverable block. `guard.looks_like_rejection` remains as a pure helper,
  no longer wired into the read loop.
- **Rate limits are enforced on the gateway side**: friend requests, broadcasts and direct messages are
  guarded by the gateway's deterministic counters, never by the AI service's good behaviour.
- **Silence is the AI service's call, not the gateway's**: when a card carries no `reply_segments`, send
  nothing and log the reason. The AI service itself only stays silent on a recruiter-set hold or an unrecognised
  stranger (spec 090); everything else gets at least one line.
- **Human takeover wins**: if the recruiter replied in WeChat themselves, that message must also be fed into
  the session context (marked as said by us), otherwise the AI repeats the question on its next turn. After a
  human interjects, the AI stays silent for a while.
- **A failed task keeps its reason, state and retry count** so the caller can query them.

### 5.3 Platform

- **Runs only on a logged-in, unlocked, interactive Windows desktop.** Never inside a Session 0 Windows
  Service; launch it from a scheduled task after login.
- **The MVP supports a single WeChat account in a single Windows session.**
- **No DLL injection, no process spoofing, no screen-reader flag tampering**, and no other scheme that works
  around WeChat's restrictions.
- **Reading the local database is read-only and allowed** (0.1.121, user ruling 2026-11-03): the key is read
  from `Weixin.exe` with `ReadProcessMemory`, the database is decrypted into memory and opened `query_only`
  with a deny-by-default authorizer. This is *reading state the user already owns*, the same thing the 映身
  desktop app ships. It is **not** an exception to the line above: no code is written into WeChat, no handle
  or window is spoofed, nothing is injected, and no accessibility flag is touched. The key and any decrypted
  bytes stay in memory and never reach disk or a log. Writing to, or tampering with, any WeChat database is
  still forbidden.

### 5.4 UIA

- `probe_wechat_uia()` **may only read state**. It never starts, closes, restarts, switches or unlocks WeChat.
- Confirm WeChat is logged in and UIA is available before any business action. On seeing `mmui::LoginWindow`,
  block the task and prompt for login.

---

## 6. Pacing and gatekeeping (WeChat anti-abuse)

Every line here was learned on a real machine. Read the reason before changing one.

- **An action like adding a friend must pass both gates: a quota (per hour) and a minimum interval (≥300s).**
  Neither substitutes for the other: the quota governs volume, the interval governs rhythm. With a quota
  alone, "5 per hour" permits all five inside one minute — on 2026-08-27 that is exactly what happened, two
  within 81 seconds, both reporting "number not found", and a security verification popped up right after.
  The gate is registered **by command name** (`guard.WRITE_PACING`), not by action: accepting a request
  somebody sent us needs no search and must not be blocked by this gate. Both numbers live in the scheduling
  config (`max_friend_actions_per_hour` / `friend_interval_seconds`).
  ★ **Speed mode (demo accounts only) lowers the friend floor from 300s to 10s** (user ruling 2026-10-15,
  reversing 09-30) — `GatewaySettings.friend_floor_seconds`; the presets also set the add-friend interval
  and the accept re-check interval to 10s and restore them when speed mode is turned off. Quotas are untouched.
  ★ There used to be a third tier, "double the backoff after consecutive failures". It was removed on
  2026-08-28 at the user's request (waits too long). **Do not add it back**: if you want it slower, raise the
  interval — a number you can see and change beats a number that grows on its own.
- **When two pacing gates block each other, the one that yields must be the one that can be retried.**
  Adding a friend (`friend_write`) and the re-check loop (`read_friends`) both search for people, and indeed
  should not run back to back, but **they must not share one account**: the re-check touches the account every
  300 seconds as long as anyone is pending, so sharing means an AI-dispatched friend request queues forever
  behind it — and a rejected command is dropped, not queued. The answer is that the re-check dodges the friend
  request (`friendcheck` uses `seconds_since_read`, **looking without claiming**), never the reverse.
- **Every step that drives WeChat needs a random pause in between**
  (`_PAUSES` / `_type_like_human` in `wechat_api/uia.py`). A full `set_text` plus a zero-delay click is a
  six-second run with zero variance, and that by itself is a signature. Pauses are always drawn from a random
  range — a fixed 2.0 seconds is as much a machine as a fixed 0.0 seconds, only slower.
- **A candidate under human takeover is not even read** (user ruling 2026-08-28). It is filtered in
  `wechat_io.QueueMessageSource._drop_handed_off`; the write-side gate (`guard._takeover_denial`) stays as a
  second path. The earlier position was "reading must continue, otherwise the backend cannot cancel its
  unanswered-follow-up" — that cost is now knowingly accepted, so do not revert it based on an old comment.
  **Handing a candidate back to the agent must clear the cursor** (`GatewayStore._forget_cursor`, both paths:
  the local button and the watchlist sync), otherwise the agent wakes up and treats everything the recruiter
  typed as new messages.

---

## 7. Which name `add_friend` returns (★ replying later depends on it)

- `add_friend` returns `{number, name, remark, session_name}`. `name` is the peer's nickname (read off their
  profile card, for human eyes only — it changes, it collides, it may contain emoji). `remark` is the remark
  we set. **`session_name` = remark first, falling back to nickname, and that is the key to use for
  `send_message(to=...)`, the watchlist and relationship memory** — once they accept, the remark is what the
  chat list shows.
- So to be able to reply reliably, **pass a `remark` when adding the friend** (e.g. `张三-仓库工`).
  Do not rely on the nickname.
- ★ When a `remark` (the candidate's label) is present, it is also what gets typed into the box under
  「发送添加朋友申请」, replacing `greetings` (user, 2026-09-24; `CommandExecutor` in `gateway/commands.py`).
  No remark → the greeting rules of 092 still apply.
- Failing to read the nickname is not a failure (`name` becomes `null`, logged as `friend.add.fallback`).
  But **a number that cannot be found raises an error**, no longer silently returning success — otherwise the
  AI service waits forever on a request that was never sent.
- A successful `add_friend` only means **the request was sent**, not that you can message them. When they
  accept is up to them, and there are **two** ways to find out (user changed the position on 2026-09-01):
  ① `gateway/friendcheck.py` searches again every so often (the criterion is an exact `search_contacts` hit,
     which means they are already in the contact list). Interval and deadline both live in the **settings
     center** (`friend_recheck_interval_seconds` / `friend_recheck_deadline_hours`); the deadline counts from
     **the moment of the request**, replacing the old count-based `MAX_CHECKS`. The name searched for is the
     one we reported to the business backend (remark first, nickname as fallback).
  ② A **send-message command from the business backend succeeded** → they accepted long ago
     (`_note_sent_to_pending_friend`). A free signal that costs no WeChat interaction.
  Both funnel into `_report_accepted_now`, which reports `accepted` **immediately**, no longer holding it for
  half an hour (the old `gateway_pending_accepted` delay queue was deleted on 2026-09-01).
- ★ **Do not open a poll just to wait for acceptance** (user, 2026-09-01: "the business backend will send a
  message, no polling needed"). People in `requested` **never enter the watchlist** — the list only accepts
  `accepted` (085, fail-closed). Loosening that costs one pointless open of a non-existent chat window per
  person per round, and `requested` has **two sources**: the other is "the plan says to add these people",
  pushed by the backend, whom we have not even contacted. Putting them in the list means possibly
  auto-replying to someone we never greeted.
- ★ The authority on "we really did send a request" is the **local ledger** `gateway_friend_checks`
  (written only by this machine), not `gateway_contacts.status='requested'` — the latter is overwritten
  unconditionally by the watchlist sync (`save_contacts` writes an empty string when the backend returns no
  `contact_status`), and once that is flushed the person is neither re-checked nor recognized as accepted,
  without a word.
  ⚠️ `check_new_friends` is **not** this signal: it is the list of **people requesting to add us**, which is a
  different thing from "the person we requested accepted".

---

## 8. Desktop login and status reporting (per the formal contract since 2026-08-15)

Contract: [`docs/招聘分身_客户端认证与状态接口_v1.1.md`](docs/招聘分身_客户端认证与状态接口_v1.1.md).
Implemented in three parts: `ui/client_api.py` (four endpoints), `ui/token_store.py` (token stored via DPAPI),
`ui/status_reporter.py` (reports on state change). **Any change to any of them must line up with that
document — do not invent fields locally.**

- The UI is **login first, use second**: email + password → `POST /recruitment-agent/client/auth/login`,
  which **does not trigger the email OTP**. No "skip login" switch, and no second login path — a login method
  the server does not control makes session revocation and account deactivation meaningless (the old "local
  account" mode is gone).
- **The token does not expire but can be revoked**: it is only stored encrypted (`client-session.json`,
  Windows DPAPI) and **not a single byte is written on non-Windows**. Any call returning
  `401 CLIENT_SESSION_REVOKED` clears the token and returns to the login page (`ClientAuth.handle_revoked`).
  That is the only brake a non-expiring token has.
- **There is no heartbeat** (contract §7): a PUT happens only on state change, and the server does not infer
  offline from time. So **the process must report offline before exiting** (`StatusReporter.shutdown()`, up to
  3 seconds). Skip that step and the enterprise console shows a machine that was powered off long ago as
  online.
- **Never report the same state twice**; and when a report fails, **roll the dedupe record back** — otherwise
  one network hiccup makes that state unreportable forever (the dedupe table says it was sent, the server
  never got it).
- WeChat connection state is mapped from the read-only UIA probe (`ui/uia_status.py`, pure functions): not
  running / not logged in / control tree not exposed are three different failures, each with its own recovery
  hint; connected-then-dropped reports `disconnected`. **Do not invent what you cannot obtain** — non-Windows
  reports nothing at all, and `accountRef` (which prevents one WeChat being claimed by two agents) requires
  reading WeChat's own account, which the client cannot do today. **Better to omit it than to fabricate a
  fake stable identifier.**
- **The capability tier** (contract §8 `capabilityStatus`) is reported together with `connected` and reuses
  the gateway's capability table (`gateway/capabilities.py: capability_status`) — **no second set of
  criteria**, because two sets will eventually give two answers. Only `passed` makes the enterprise console
  show "running", so a dry-run machine must honestly report `limited`.
- There is exactly one parameter file, `gateway.env` (the client endpoints push no configuration; the old
  `gateway.remote.env` layer is gone). **The UI contains not a single credential**, and the WeChat ID is no
  longer transcribed by hand — accounting uses the `agent.id` returned by login.
- "Remember my email" remembers the email only, never the password.
- **Nothing starts before login**: the service starts in `__main__.start_service()` and nowhere else.
- **"Log out" is "stop the service", and there is only this one** (stop the gateway + revoke the server
  session + return to the login page). The window sidebar and the tray menu must agree; the tray menu is
  live log / settings center / log out / quit.
- Two pages: live log and settings center (parameters + read-only watchlist), laid out against
  [`docs/wxui.png`](docs/wxui.png).
  **Plans do not belong in the desktop client** (user ruling 2026-08-15): what happens at what time and how
  often to follow up is scheduled by the AI service and pushed down as commands over WS. The client does not
  even parse a `plans` field — do not add that page back.
- The settings center follows `docs/setting.png`: parameters in sections (`settings_store.SECTIONS`) with one
  sentence of explanation each. Adding a parameter means adding one entry to `FIELDS` (with
  `section`/`unit`/`kind`/`placeholder`); **the page itself does not change**. Shared small widgets (grey
  placeholder, green focus ring, pill switch, outlined button) live in `ui/widgets.py` — do not rewrite them
  per page. "Test connection" only proves the network is reachable, so **it must never claim "connection is
  fine"**: whether the credentials are right is only known once it really connects.
- The watchlist is a read-only card inside the settings center (`docs/setting2.png`): one person per row, and
  whoever is on the list is who gets auto-replied (069, fail-closed). **Authority over the list always stays
  with the AI service; the local machine offers no editing.** The "sync list" button goes through the WS call
  `wechat.watchlist.sync` (`wsclient.py`); it only lands what is pushed and answers with a count. It is
  **not** §4.2's session-name sync — two different things, do not conflate them.
- The "next message poll" countdown above the list comes from `InboundLoop.seconds_to_next_poll()` — it
  records **the nap the loop is actually taking** (`_next_poll_at`, jitter and fast/slow tier included), not a
  number computed from the config. Computing it from the config leaves it tens of seconds off reality forever,
  which looks broken. With nothing scheduled, or outside the window, leave it blank or say so — never show a
  countdown that reaches 00:00 and does nothing.
- Logs are one file per day (`logging_config.DailyFileHandler`, `logs/gateway-YYYY-MM-DD.log`, kept 30 days).
  Not `TimedRotatingFileHandler`: it leaves **today's** file named `gateway.log` with no date, so one
  directory carries two naming schemes and anyone looking by date fails to find today at first glance.

---

## 9. WeChat message event reporting (the source of truth for unanswered follow-ups)

Contract: [`docs/招聘分身_客户端微信消息事件上报_v1.md`](docs/招聘分身_客户端微信消息事件上报_v1.md).
Implemented in `gateway/message_events.py` + the `gateway_message_events` table; the three registration points
(inbound callback / outbound delivery / command execution) are tabulated in that document.

- **The client reports facts and never decides whether a second message is due.** Whether to follow up in four
  hours, and with what, lives entirely in the backend's plan; scheduling it again locally means the same
  follow-up goes out twice. **Do not add a clock here.**
- **Write the outbox first, report second**: `record()` is synchronous and only writes local SQLite; the
  background loop is what actually sends the request. Bookkeeping must never sit in front of a WeChat action,
  and a network hiccup must not shake an event loose.
- **`eventId` is derived from content** (uuid5), so a re-send after a network outage always carries the same
  value and the backend dedupes on it.
- **If it cannot be attributed, it is not reported**: the candidate ID is looked up locally by session name,
  and a failed lookup is honestly skipped. **Never fabricate an ID to fill the slot.**
- **A dry-run machine reports nothing at all**: `factory.py` does not build the reporter when
  `allow_real_actions` is false. Likewise a queue task that did not return `succeeded` is not reported —
  "no exception was raised" is not "the message went out".
- **Chat bodies are never uploaded**: only a `contentDigest` summary, and `messageId` is hashed too (the local
  msg_id embeds the remark name).

---

## 10. The watchlist comes only from sync (090 stranger intake retired 2026-09-24)

**The watchlist is exactly the `accepted` session names synced from `GET /client/contacts` (spec 085).
Nothing on this machine adds to it.** User ruling 2026-09-24: 「监听逻辑是同步的，不应该自己添加」.

- **Retired**: `gateway/strangers.py` (scanning the session list and admitting unknown names), the
  `gateway_strangers` table, and "admit on auto-accepted friend request". Once 0.1.80 made the scan actually run,
  it admitted service accounts (「腾讯新闻」) and the recruiter's own groups, and the settings-center remove button
  could not take them out. `sessions._retire_strangers` drops the table on upgrade and strips the names it had
  added from the persisted watchlist. **Do not bring a local admission path back** — if someone new must be
  auto-replied to, the business backend writes them into contacts.
- **The session-list read that remains only prioritises within the list** (`wechat_io._skip_unchanged` /
  `_due_peers`): it decides which watched sessions to open this round, never who is watched.
- **「同步名单」 is a full resync** (`runner.sync_watched` → `_sync_contacts_once(full=True)`): pull every page,
  and only if every page arrived, delete local `accepted` rows the backend no longer returns. Incremental sync
  never brings a deleted row back, which is why a stopped plan's people used to linger. Local `requested`
  rows (the friend-request ledger) are never touched by it.
- Auto-accepting friend requests (110) still accepts and reports `accepted` to the backend; it does not add
  anyone to the watchlist.
- **Reading an unlisted chat that shows a red-dot count is allowed; admitting it is not** (user ruling
  2026-09-28, 0.1.90, `wechat_io._unread_strangers`, switch `read_unread_strangers`). On a full sweep round only,
  `get_unread_messages` (no window opened) lists sessions with a badge; unlisted one-to-one chats are opened —
  at most `MAX_UNREAD_STRANGERS_PER_POLL` — and at most that many trailing messages go to the AI service with
  `first_contact`, which stays silent unless the peer is job-seeking (spec 090). Excluded without opening:
  anything on the list or in local `never`, under human takeover, a known group,
  `SYSTEM_SESSION_NAMES` (service/official accounts, WeChat feature entries). A group found on opening is
  remembered and dropped. **Nobody enters the watchlist this way**, and an unlisted read failure never feeds
  the unreachable / not-found strike counters (those add friends and remove list entries). Known cost the user
  accepted: opening the chat clears the recruiter's red dot, including for peers the AI then ignores.

---

## 11. How to extend

- A new WeChat capability goes into the `wechat_api` wrapper first, then a task type and worker dispatch, and
  only then is it exposed to the gateway.
- The desktop client only does "log in + start/stop the service + watch the log + view/edit parameters".
  It **must not** contain any business judgement, wording, or WeChat action inside `ui/`; it only calls the
  public interfaces of `gateway`/`service`. Testable logic goes in `ui/logbuffer.py`, `ui/service.py`,
  `ui/settings_store.py`, `ui/auth.py`; `ui/window.py`, `ui/login_page.py` and `ui/tray.py` keep GUI wiring only.
- Do not restore the standalone calling client or an LLM module. If the product direction changes, the
  architecture gets a separate discussion.
  (2026-08-12: the product direction confirmed a desktop client, so the old "no GUI/tray" ban is narrowed to
  the rule above.)

---

## 12. Development and testing

```powershell
python -m ruff check wxclient tests packaging
python -m unittest discover -s tests -v              # from the repo root; do NOT add -t .
python -m wxclient.gateway --account <wxid>          # dry-run smoke test, sends nothing for real
```

★ **Do not add `-t .`.** `discover -s tests -t .` fails immediately with
`ImportError: Start directory is not importable` — `tests/` has no `__init__.py`, and once a top-level
directory is specified, discover insists on importing it as a package. Without `-t .` it runs fine, and
`cd tests` followed by `discover -s . -t .` also works. Of the three spellings only the middle one is broken,
on the real machine and on Linux alike.

**The Linux baseline** (measured 2026-09-02): 139 skipped + 16 errors, all from GUI dependencies such as
pywinauto and tkinter. **That is normal, not something you broke.** The real machine runs about 950 tests
with a single skip. Judge "did I break something" against this baseline, not against all-green.

### ⚠️ What Linux can and cannot test

Development mostly happens on Linux, but **WeChat automation can only really run on Windows.**

| Runs on Linux | Requires a real Windows machine |
|---|---|
| `ruff` static checks | any actual `uia_probe` / `wechat_api` call |
| queue logic, idempotency keys, state machine unit tests | sending messages, adding friends, posting Moments |
| pure-function unit tests for debounce / dedupe / gatekeeping | inbound message polling |
| the WS client protocol layer (against a fake AI service) | the end-to-end chain |

**The rule: green on Linux does not mean it works.** A change that touches WeChat behaviour must not be marked
done, and must not be described as "verified", until it has been verified on a real Windows machine.
Real-machine verification must leave reviewable evidence: action type, target session, task ID, result,
elapsed time. A verbal "I tested it" does not count.

---

## 13. Packaging, deployment and releases

**What users receive is an installer, not a directory.** Packaging details (why onedir rather than onefile,
cutting OpenCV, anchoring the data directory, deleting the previous `_internal` on upgrade) live in
[`README.md`](README.md) under 「打包」 and in the comments at the top of
`packaging/lxz-wechat-gateway.spec`.

- **Packaging only runs on Windows**: PyInstaller collects **this machine's DLLs**, and **the build machine's
  venv must be exactly the closure of `pyproject.toml`** — one extra package in the environment can be pulled
  in along an import chain. (Rebuilding in a clean venv took the installer from 33.7 MB down to 25.0 MB.)
- ★ **Packaging must include the vendored `third_party/pywechat/pyweixin` and the `logger/logging.conf` its
  dependency needs.** The former is located at runtime via `sys.path.insert` and is absent from PyInstaller's
  dependency graph; miss it and the first WeChat action reports `No module named 'pyweixin'`. The latter is a
  data file read **at import time** along the pywinauto import chain, and PyInstaller only collects `.py`;
  miss it and UIA probing never once succeeds in the frozen build. Both showed up as "WeChat is clearly
  running but the UI says it cannot find WeChat" (one real-machine incident each, 2026-08-16 and 08-17), and
  **running from source, running the tests, even double-clicking the exe inside onedir on the build machine
  can all look fine**. The spec already handles both and `--self-check` guards them — do not strip either out.
- **CI can build too**: [`.github/workflows/build-exe.yml`](.github/workflows/build-exe.yml) runs the same
  `packaging/build.py` on a pinned `windows-2022`, where the environment is clean by construction. It triggers
  on a push to `refactor/drop-mcp` or on a `v*` tag.
  ★ **Green CI is not verification**: the runner has no WeChat installed, so the self-check only proves
  "pyweixin is inside the package", not that it can really drive WeChat. And the CI package is **not the same
  bytes** as the real machine's (the same version differed by 9% once), nor are two consecutive CI runs
  byte-identical. **Installing on a real machine and running it once remains mandatory.**
- **The release chain** is documented as a skill: [`.agents/skills/release-client/SKILL.md`](.agents/skills/release-client/SKILL.md).
  In short: bump `__version__` → test → build → edit the product site → `publish.sh` → verify online.
  Read the skill before releasing; it carries the ordering constraints and the traps.
- **Publishing is pushing.** `publish.sh` writes `latest.json` next to the package, and every installed
  client (0.1.53+) polls it every 6 hours and offers a one-click silent install (`wxclient/ui/updater.py`,
  README 「桌面端」). A published package reaches users' machines without anyone re-downloading, so the
  real-machine install-and-run step before publishing is not optional. Only a higher version can undo a
  bad publish — the client never downgrades.
- **The only source of the version number is `__version__` in `wxclient/__init__.py`.** `pyproject.toml`
  reads it dynamically and `packaging/build.py` reads it too — never write a second copy anywhere.
- **Bump the version in the same batch as the client code change**, never "later, when we release": code in
  the repo is not code in users' hands, and the version number is the only visible evidence between the two.
  Documentation-only and comment-only changes do not count.
- The deployment host's address, account and password go **only into environment variables or local
  uncommitted config**, never into the repository.
- Before deploying, confirm: WeChat logged in, desktop unlocked, UIA probe passing, and the
  `--allow-real-actions` switch in the state you expect.
- Before shipping, strip: debug switches, fixed test accounts, temporary probes, verbose logging.

---

## 14. Git

- **The main line is `refactor/drop-mcp`** (confirmed by the user 2026-09-02), not `master`. GitHub's default
  branch is still `master`, and one concrete consequence is that the Actions "Run workflow" button only lists
  workflows from the default branch, so it currently cannot be clicked.
- **Forbidden**: `git add -A` / `git add .` / `git commit -a`. Only `git add <explicit path>`. The repo root
  permanently holds untracked scratch files left by other sessions (`win*.txt` and friends), so a blanket add
  will sweep them in.
- Before committing, always run `git status` and `git diff --cached` and eyeball what is staged.
- Commit message: `<type>(<scope>): <one-line Chinese summary>`
  - type ∈ `feat` / `fix` / `refactor` / `chore` / `docs` / `test`
  - scope ∈ `gateway` / `runtime` / `queue` / `wechat` / `uia` / `ui` / `ci` / `docs`
  - e.g. `feat(gateway): 入站轮询 + 去抖聚合`
- `ruff` and `unittest` must both be green before committing.
- **A pre-commit gate lives in [`.githooks/`](.githooks/README.md)**; enable it once per clone with
  `git config core.hooksPath .githooks` (`core.hooksPath` is local config and does not travel with the repo).
  It checks only deterministic things that already appear in this file: the two symlinks are still symlinks,
  nothing from the never-commit list is staged, the production host IP is not in the diff, other sessions'
  scratch files are not swept in, a `wxclient/` code change bumps `__version__`, and staged Python passes
  ruff. Escape hatches: `git commit --no-verify`, or `LXZ_SKIP_VERSION_CHECK=1` for the version check alone.
  **After changing a hook, re-run `bash .githooks/selftest.sh`** — the first version of the symlink check was
  a false green and only a real mutation caught it.
- **Push only when explicitly asked**; commit locally by default.
- Commit once the work is done and verified; **never revert workspace changes unrelated to the user's task**.

### Never commit

Keys / tokens / passwords / deployment host IPs, real candidate data and chat records, `.wxclient/` runtime
data and logs, `dist/` build artefacts, `__pycache__/`.

---

## 15. Closing status table (required at the end of every task)

Every finished task ends the reply with a three-column table (cells in Chinese):

| 测试 | 提交 | 部署 |
|---|---|---|
| ✅ 全绿（写明跑了什么）/ ⏭️ 本次不涉及 / ❌ 失败 | ✅ `<hash>` / 📝 未提交（说明原因）| ✅ 已部署真机验证 / ⏭️ 本次不涉及 |

Fill it honestly; never leave a cell blank and never drop the table.
**If the change touches WeChat behaviour and was not verified on a real machine, the deployment cell may only
say not verified — never "verified".**

---

## Appendix: external documentation

When a library, framework, SDK, API, CLI or cloud service is involved, fetch current documentation with ctx7
first:

1. `npx ctx7@latest library <name> "<full question>"`
2. Pick the official, matching `/org/project` ID.
3. `npx ctx7@latest docs <libraryId> "<full question>"`

At most 3 ctx7 commands per question, and never put a secret in a query. ctx7 is not for refactoring business
logic, plain code review, or writing an ordinary script from scratch. When the quota runs out, say so
explicitly and point at logging in or configuring `CONTEXT7_API_KEY`.
