# Production Verification — 完整人工測試範本

> 上線前最後一關的人工測試清單（manual verification runbook）。涵蓋全部 **5 tools · 8 actions · 4 resources · 2 prompts**、三種 transport、串接（chaining）、資料流（from_run_id）、錯誤與防護（guardrails），以及 **Codex** 串接。
>
> 這份是 `docs/W2-VERIFICATION.md` / `docs/W3-VERIFICATION.md` 的超集合（superset），上線前以這份為準。
>
> 操作方式：照 Part 0 → Part 9 順序做，每個步驟把實際結果填進 **Result** 欄，最後對照 Part 9 的 Go / No-Go 表。

---

## ⚠️ 開始前必讀的 4 個真相（很多人卡在這）

1. **實際是 8 個 actions，不是 README §8 寫的 7 個。** 真相以 `app/actions/registry.py` 為準：
   `echo` · `http_call` · `calendar_digest_ics` · `slack_post` · `github_digest` · `email_send` · `llm_summarize` · `llm_polish`。
   README 提到的 `r2_upload` **已在 issue #132 / ADR-051 移除**，改用 `bin/r2_backup.sh`。

2. **`http_call` 與 `calendar_digest_ics` 是 operator-only**（`requires_operator=True`）。
   你本地 `.env.docker` 的 `OPERATOR_USER_ID` **預設是註解掉的** → 沒有任何人是 operator → 這兩個 action 對所有人都會被拒絕（回 **`INVALID_STATE`**，不是 `USER_INPUT`；見 `app/mcp/errors.py` 的 `OperatorOnlyActionError` 對應）。要測它們，先做 Part 0 的「啟用 operator」步驟。

3. **MCP server 只負責把 job 寫進 Postgres，不負責執行。** 真正讓 job 跑起來的是 5 個背景 daemon：
   `watcher` · `worker` · `recurring-watcher` · `chain-watcher` · `reconciler`。
   → **不管你用 HTTP 還是 stdio 當 transport，這 5 個 daemon 都必須在跑**，否則 job 永遠停在 `scheduled`。`docker compose --profile full up -d` 會一次把它們全部帶起來。

4. **同一個 `user_id` 必須三邊一致**：`.env.docker` 的 `MCP_USER_ID`、你 MCP client 送的身分（HTTP 的 `X-User-Id` header / stdio 的 `MCP_USER_ID` env）、以及瀏覽器看到的 `/connections` 「Signed in as …」。
   三邊不一致 = 你在 A 身分授權 OAuth、卻用 B 身分查詢 → action 永遠回 `MISSING_CONNECTION`，連線明明已存在卻查不到。本範本一律用 `me`。

---

## Part 0 — 環境前置（Setup）

### 0.1 啟動全棧

```powershell
# 在專案根目錄
docker compose --profile full up -d
docker compose ps          # 期望：postgres / elasticmq / mcp-server / watcher / worker /
                           #       recurring-watcher / chain-watcher / reconciler 全部 Up（migrate 為 Exited 0 正常）
```

> 若只想跑 host-side 單元測試而非人工測試，見 README §9；本範本要的是「全棧都在跑」。

### 0.2 統一 user_id 為 `me`

編輯 `.env.docker`（注意：範本附帶的 `.env.docker` **預設就已是 `MCP_USER_ID=me`**，通常不用改；確認一下即可）：

```dotenv
MCP_USER_ID=me
```

### 0.3（選用，但要測 http_call / calendar_digest_ics 就必做）啟用 operator

`.env.docker` 取消註解並設成與上面相同的 `me`：

```dotenv
OPERATOR_USER_ID=me
```

要測 `http_call` 的 `${VAR}` 代換，再加上白名單（範例放一個示意變數）：

```dotenv
ALLOWED_TEMPLATE_VARS=DEMO_TOKEN
DEMO_TOKEN=hello-secret
```

### 0.4 改完環境變數一定要重新 up（不是 restart）

```powershell
docker compose --profile full up -d   # 會重新讀取 env_file；restart 不會重讀
```

> 為什麼：`restart` 不會重新載入 `env_file`。改完 `.env.docker` 必須 `up -d`。

### 0.5 健康檢查（health check）

```powershell
Invoke-RestMethod http://localhost:8000/healthz
# 期望：ok=True, db=connected, version=<git sha>
#       （本地 build 的 version 常是 "unknown"，git sha 只在 VPS/prod build 注入；只要 ok=true、db=connected 就算過）

Invoke-RestMethod http://localhost:8000/healthz/shed
# 期望：ok=True, shed=False（沒有在 load shedding）
```

### 0.6 連上 MCP client

本範本支援三種方式，**擇一**或全部跑過。先用「**A. 自架 HTTP**」當主線（你之前都是測 HTTP）。

**A. 自架 HTTP（主線）** — Claude Code / Cursor 的 mcp config（`~/.claude.json` 內 `projects[<本專案>].mcpServers`，或專案根的 `.mcp.json`）：
```jsonc
{ "mcpServers": { "owl-scheduler": {
  "type": "http",                        // ← 欄位叫 "type"，不是 "transport": "streamable-http"
  "url": "http://localhost:8000/mcp",
  "headers": { "X-User-Id": "me" }
}}}
```
> ⚠️ Claude Code 的 streamable-HTTP transport 欄位是 **`"type": "http"`**。寫成 `"transport": "streamable-http"` 會被忽略、根本連不上 server——這步錯了，Part 1 之後全部都測不了。改完設定要重開 session 讓 MCP 重連。

**B. 自架 stdio** — client 自己 spawn 子行程：
```jsonc
{ "mcpServers": { "owl-scheduler": {
  "type": "stdio",
  "command": "uv",
  "args": ["run", "python", "-m", "app.entrypoints.mcp_stdio"],
  "env": { "MCP_USER_ID": "me", "MCP_USER_TZ": "UTC" }
}}}
```
> stdio 仍需 Part 0.1 的全棧在跑（執行靠背景 daemon、OAuth 靠 web tier 的 `/connections`）。

**C. MCP Inspector（最快的人工點擊驗證）**：
```powershell
$env:MCP_USER_ID="me"; $env:MCP_USER_TZ="UTC"
npx @modelcontextprotocol/inspector uv run python -m app.entrypoints.mcp_stdio
```

> 下方每個步驟我用「**tool / args**」表示。在 Inspector 就是選 tool 貼 args；在 Claude/Cursor/Codex 就用自然語言請它呼叫對應 tool（附 args）。

---

## Part 1 — 連線與探索（Connect & Discover）

### 1.1 工具數量

列出 tools。**期望恰好 5 個**：
`task.create.v1` · `task.list.v1` · `task.status.v1` · `task.cancel.v1` · `task.list_actions.v1`

> 命名小提醒：server 端註冊的就是上面這 5 個**帶點**的名字（MCP Inspector 看到的即為此形）。但 **Claude Code / Cursor / Codex 會把點換成底線並加上 server 前綴**，所以你在那邊看到的會是 `mcp__owl-scheduler__task_create_v1` 這種形式——名稱不同、數量一樣是 5 個，別誤判成「對不上」。

### 1.2 探索 actions 與授權狀態

**tool:** `task.list_actions.v1` ／ **args:** `{}`

**期望：** `ok:true`，`data.actions` 內 **8 個** action。逐一檢查每個的 `auth_status`：

| action | auth_required | 一開始的 auth_status |
|---|---|---|
| `echo` | false | `n/a` |
| `http_call` | false | `n/a`（但 operator-only，見 Part 4） |
| `calendar_digest_ics` | false | `n/a`（operator-only） |
| `llm_summarize` | false | `n/a` |
| `llm_polish` | false | `n/a` |
| `github_digest` | true | `not_connected` → 連線後 `connected` |
| `slack_post` | true | `not_connected` → 連線後 `connected` |
| `email_send` | true | `not_connected` → 連線後 `connected` |

OAuth 類 action 若 `not_connected`，回應應附 `connect_url`（指向 `http://localhost:8000/connections`）。

---

## Part 2 — 核心排程語義（用 echo 驗證）

### 2.1 立即任務（immediate）

**tool:** `task.create.v1`
```json
{
  "action": "echo",
  "action_params": {"message": "hello immediate"},
  "schedule_type": "immediate"
}
```
**期望：** `{"ok":true,"data":{"job_id":<A>,"status":"scheduled"}}`

等 ~10 秒（watcher→queue→worker），再查：

**tool:** `task.status.v1` ／ **args:** `{"job_id":<A>, "include_runs": true}`
**期望：** `status:"completed"`；`runs[0].status` 內部為 `SUCCEEDED`，有 `start_at` / `finish_at`。

### 2.2 一次性未來任務（one-shot · 未來）

```json
{
  "action": "echo",
  "action_params": {"message": "far future"},
  "schedule_type": "one-shot",
  "scheduled_at": "2099-12-31T00:00:00+00:00"
}
```
**期望：** `ok:true`、`status:"scheduled"`，且不會執行（時間還沒到）。記下 `job_id=<B>`。

### 2.3 一次性過去任務 → 應被拒（negative）

```json
{
  "action": "echo",
  "action_params": {"message": "in the past"},
  "schedule_type": "one-shot",
  "scheduled_at": "2000-01-01T00:00:00+00:00"
}
```
**期望：** `ok:false`，`error.code:"USER_INPUT"`（scheduled_at 必須在未來）。

### 2.4 時區（timezone）

```json
{
  "action": "echo",
  "action_params": {"message": "tz test"},
  "schedule_type": "one-shot",
  "scheduled_at": "2099-06-01T08:00:00",
  "timezone": "Asia/Taipei"
}
```
**期望：** `ok:true`。（naive 時間用 IANA `Asia/Taipei` 解讀；若給 Windows 時區字串如 `Taipei Standard Time` 會被拒 → 也可順手測這個 negative。）

### 2.5 週期任務（recurring · cron 與捷徑）

捷徑：
```json
{
  "action": "echo",
  "action_params": {"message": "recurring hourly"},
  "schedule_type": "recurring",
  "cron_expr": "@hourly",
  "timezone": "UTC"
}
```
5 欄 POSIX：
```json
{
  "action": "echo",
  "action_params": {"message": "every minute"},
  "schedule_type": "recurring",
  "cron_expr": "* * * * *",
  "timezone": "UTC"
}
```
**期望：** 兩者皆 `ok:true`。第 2 個（每分鐘）等 ~70 秒後 `task.status.v1 {include_runs:true}` 應看到**多次** run（第一次完成後自動排下一次）。記下每分鐘那個的 `job_id=<R>`，下一步要取消。

> 注意（已知行為）：recurring「上一個 run 終結後才會排下一個」（sequential），不會並發堆疊。

### 2.6 取消週期任務（cancel recurring）

**tool:** `task.cancel.v1` ／ **args:** `{"job_id":<R>}`
**期望：** `ok:true`、`status:"cancelled"`。之後 recurring-watcher **不再**產生新 run（再等 2 分鐘確認 run 數不再增加）。

### 2.7 取消未來一次性任務

**tool:** `task.cancel.v1` ／ **args:** `{"job_id":<B>}`（Part 2.2 那個）
**期望：** `ok:true`、`status:"cancelled"`。
再 `cancel` 一次同 job → **idempotent**，仍 `ok:true`。

### 2.8 列表、分頁、狀態過濾（list）

```json
{ "page": 1, "pageSize": 20 }
```
**期望：** `data.jobs` 新到舊、含 `total` / `page` / `pageSize`，前面建立的 job 都在。
再測過濾：`{"status":"completed"}` 只回已完成；`{"status":"cancelled"}` 只回已取消。
分頁：`{"page":1,"pageSize":2}` 只回 2 筆且 `total` 不變。

### 2.9 冪等鍵（idempotency_key）

連送兩次**完全相同**的：
```json
{
  "action": "echo",
  "action_params": {"message": "idem"},
  "schedule_type": "immediate",
  "idempotency_key": "verify-2099-key-1"
}
```
**期望：** 兩次回**同一個 `job_id`**（第二次不是新建、也不是錯誤，而是原樣回傳既有 job）。

---

## Part 3 — 串接與資料流（Chaining & Data Flow）

### 3.1 成功觸發（A SUCCEEDED → B）

建 A：
```json
{ "action": "echo", "action_params": {"message": "chain A"}, "schedule_type": "immediate" }
```
→ 記 `A`。建 B：
```json
{
  "action": "echo",
  "action_params": {"message": "chain B"},
  "schedule_type": "immediate",
  "trigger_on_job_id": <A>,
  "trigger_on_status": "SUCCEEDED"
}
```
**期望：** 剛建完 B 時 `task.status.v1(B)` 為 `scheduled`（內部 run 在 `WAITING`）。A 完成 ~10 秒後、chain-watcher 再 ~5 秒，B → `completed`。

### 3.2 失敗觸發（A FAILED → B）

用一個會「永久失敗（non-retryable）」的上游當 A。最穩的方式：operator 已啟用時用 `http_call` 打一個會回 4xx 的 URL（4xx = non-retryable → `FAILED`）：
```json
{ "action": "http_call",
  "action_params": {"method":"GET","url":"https://httpbingo.org/status/404"},
  "schedule_type": "immediate" }
```
→ 記 `A2`。建 B2：
```json
{ "action": "echo", "action_params": {"message":"runs only on upstream failure"},
  "schedule_type": "immediate",
  "trigger_on_job_id": <A2>, "trigger_on_status": "FAILED" }
```
**期望：** A2 最終 `failed`，B2 隨後 `completed`（因為它等的是 FAILED）。
> 沒啟用 operator 就跳過本步（http_call 會被擋），改用 `trigger_on_status:"ANY"` 接在任何上游後驗證「ANY 一律觸發」。

### 3.3 上游資料流入下游（from_run_id + digest_v1）

> 最寫實的版本要 OAuth（github_digest → slack_post/email_send），放在 Part 4 做。這裡先用不需 OAuth 的 LLM action 驗證 `from_run_id` 機制本身：

建上游 U（產生文字結果）：
```json
{ "action": "llm_summarize",
  "action_params": {"text":"MCP is a protocol that lets LLM clients call tools over a transport. It standardizes tool discovery and invocation.", "length":"short", "style":"bullet", "language":"zh-TW"},
  "schedule_type": "immediate" }
```
→ 等 U `completed`，記 `U`。建下游 D 消費 U 的結果：
```json
{ "action": "llm_polish",
  "action_params": {"from_run_id": <U 的 run_id>, "tone":"professional", "language":"zh-TW"},
  "schedule_type": "immediate",
  "trigger_on_job_id": <U>, "trigger_on_status": "SUCCEEDED" }
```
**期望：** D `completed`，其 `result.polished` 是「潤飾過的 U 摘要」。
> `from_run_id` 要填上游那次 **run 的 id**（在 `task.status.v1(U, include_runs:true)` 的 `runs[0].run_id`），不是 job_id。

### 3.4 串接驗證的 negative cases

| 測試 | args 重點 | 期望 |
|---|---|---|
| V1 上游不存在 | `trigger_on_job_id: 999999` | `ok:false` **`NOT_FOUND`**（找不到上游，`field=trigger_on_job_id`） |
| V6 cron 與 trigger 互斥 | 同時給 `cron_expr` + `trigger_on_job_id` | `ok:false` `USER_INPUT`（recurring 不能同時被觸發） |
| 跨使用者觸發 | trigger 指向別的 user 的 job | `ok:false` **`NOT_FOUND`**（V2，跨 user 一律視同 not found，不洩漏存在性） |

> V3（上游已終結就不准等）、V4（不可成環）、V5（鏈深 ≤ 10）屬深度驗證，若要全測，照 `app/domain/chain_validation.py` 的 V1–V5 各構造一例。

---

## Part 4 — 逐一驗證 8 個 actions

> echo（Part 2）、llm_summarize / llm_polish（Part 3.3）已涵蓋。以下補其餘 5 個。

### 4.1 llm_summarize（單獨直跑，無 OAuth）

```json
{ "action": "llm_summarize",
  "action_params": {"text":"<貼一段 300+ 字英文或中文>", "length":"medium", "style":"paragraph", "language":"zh-TW", "focus":["風險","結論"]},
  "schedule_type": "immediate" }
```
**期望：** `completed`，`result.summary` 有內容、`result.tokens.{input,output,total}` 有數字。
**預設模型** `gpt-4o-mini`、**輸入截斷** 16000 字元、**輸出上限** 1024 tokens、**每人每日** 10000 tokens 預算（`.env.docker`）。

### 4.2 llm_polish（無 OAuth）

```json
{ "action": "llm_polish",
  "action_params": {"text":"this sentence have some grammar issue and need fix", "tone":"concise", "language":"en"},
  "schedule_type": "immediate" }
```
**期望：** `completed`，`result.polished` 為修順後的句子。

### 4.3 http_call（**operator-only**）

> 前置：Part 0.3 已設 `OPERATOR_USER_ID=me`。未設則此步會回 `INVALID_STATE`（operator-only），那就是 Part 6.5 的 negative 驗證。

基本（無密鑰）：
```json
{ "action": "http_call",
  "action_params": {"method":"GET","url":"https://httpbingo.org/json"},
  "schedule_type": "immediate" }
```
**期望：** `completed`，`result.status_code:200`，`result.body` 為回應內容。

`${VAR}` 代換（前置：Part 0.3 設了 `ALLOWED_TEMPLATE_VARS=DEMO_TOKEN`、`DEMO_TOKEN=...`）：
```json
{ "action": "http_call",
  "action_params": {"method":"POST","url":"https://httpbingo.org/anything",
                    "headers":{"Authorization":"Bearer ${DEMO_TOKEN}"},
                    "body":{"hello":"world"}},
  "schedule_type": "immediate" }
```
**期望：** `completed`；回應裡看到 `Authorization: Bearer hello-secret`（已被 server 端代換，client 從未看到明文）。

### 4.4 calendar_digest_ics（**operator-only**）

```json
{ "action": "calendar_digest_ics",
  "action_params": {"ics_url":"<一個可公開存取的 .ics URL>", "date_range_days": 7},
  "schedule_type": "immediate" }
```
**期望：** `completed`，`result.events[]`（每筆有 start/end/summary）、`result.count`、`result.date_range`。可加 `title_contains` 過濾。

### 4.5 github_digest（**需 GitHub OAuth**）

先**不連線**直接建立：
```json
{ "action": "github_digest",
  "action_params": {"repo":"PaynePew/task_scheduler_mcp","labels":["needs-triage"],"pr_stale_days":3},
  "schedule_type": "immediate" }
```
**期望（未連線）：** `ok:false`，`error.code:"MISSING_CONNECTION"`，附 `connect_url`。

→ 開 `http://localhost:8000/connections`，確認頁面顯示 **Signed in as me**，點 **Connect GitHub** 完成授權。
→ 重送上面同一個建立請求。**期望：** `completed`，`result` 含 `repo` / `queried_at` / `labels{...}` / `prs{open, stuck[]}`。

### 4.6 slack_post（**需 Slack OAuth**）

連線後（`/connections` → Connect Slack）：
```json
{ "action": "slack_post",
  "action_params": {"channel":"#<你的測試頻道>", "message":"hello from owl-scheduler verify", "template":"raw"},
  "schedule_type": "immediate" }
```
**期望：** `completed`，`result.{channel, ts}`，Slack 頻道實際出現訊息。

### 4.7 email_send（**需 Google OAuth**）

連線後（`/connections` → Connect Google）：
```json
{ "action": "email_send",
  "action_params": {"to":["<你的信箱>"], "subject":"owl-scheduler verify", "body":"hello", "template":"raw"},
  "schedule_type": "immediate" }
```
**期望：** `completed`，`result.{recipients, subject, provider:"gmail"}`，信箱實際收到信。

### 4.8 真實串接資料流（github_digest → slack_post / email_send · digest_v1）

建上游 G（github_digest，如 4.5），等 `completed` 記其 `run_id`。建下游：
```json
{ "action": "slack_post",
  "action_params": {"channel":"#<頻道>", "from_run_id": <G 的 run_id>, "template":"digest_v1"},
  "schedule_type": "immediate",
  "trigger_on_job_id": <G>, "trigger_on_status": "SUCCEEDED" }
```
**期望：** Slack 收到由 `digest_v1` 把 github_digest 結構化結果排版成的條列摘要。
（email 版同理，`email_send` + `from_run_id` + `template:"digest_v1"`。）

> 這一步就是專案的招牌工作流（README G3：production VPS 連續 ≥5 則 Slack digest）。能跑通＝核心價值驗證完成。

---

## Part 5 — Resources（4）與 Prompts（2）

### 5.1 Resources（期望 4 個）

| URI | 點 Read 後期望 |
|---|---|
| `tasks://list` | `{snapshot_at, total, items[]}`，**只含 user=me 的 job** |
| `tasks://actions` | action registry 陣列，含各 action 的 `params_schema` |
| `tasks://job/{job_id}` | 填一個你的 job_id → 單一 job ＋ 最近 5 次 run |
| `tasks://recent-results` | 最近 24h 的 terminal JobRun（給 LLM 當每日簡報） |

跨使用者隔離：`tasks://list` 不應出現別的 user_id 的 job。

### 5.2 Prompts（期望 2 個）

- `daily_review`：無必填參數 → Get Prompt 應回一段「請讀 `tasks://list` 幫我 review」的訊息。
- `setup_summary`：必填 `topic`、`schedule`。填 `topic="AI news"`、`schedule="every morning at 8am"` → Get Prompt 結果應含這兩個字串並引用 `tasks://actions`。

---

## Part 6 — 錯誤與防護（Guardrails · Negative tests）

| # | 測試 | 怎麼觸發 | 期望 |
|---|---|---|---|
| 6.1 | 未知 action | `action:"does_not_exist"` | `ok:false` `UNKNOWN_ACTION` |
| 6.2 | 查不存在的 job | `task.status.v1 {"job_id":999999}` | `ok:false` `NOT_FOUND` |
| 6.3 | 取消不存在的 job | `task.cancel.v1 {"job_id":999999}` | `ok:false` `NOT_FOUND` |
| 6.4 | 明文密鑰被擋 | `http_call` 的 header 直接寫 `"Authorization":"Bearer sk-realsecret123"` | `ok:false` `USER_INPUT`，提示改用 `${VAR}` |
| 6.5 | 非 operator 叫 operator-only | 把 `OPERATOR_USER_ID` 註解掉重新 `up -d` 後叫 `http_call` | `ok:false` `INVALID_STATE`（operator-only） |
| 6.6 | 缺 OAuth 連線 | 未連 GitHub 就叫 `github_digest` | `ok:false` `MISSING_CONNECTION` ＋ `connect_url` |
| 6.7 | burst rate limit | **operator 須關閉**（見下注），一分鐘內連送 6 次 `immediate` create | 第 6 次 `ok:false` `USER_INPUT`（`RATE_LIMIT_BURST_PER_MINUTE=5`） |
| 6.8 | recurring 配額 | **operator 須關閉**，建立 recurring 直到 active 數達上限 5（⚠ Part 2.5 的 `@hourly` 若沒取消已佔 1 格，請先 `task.cancel.v1` 掉或把它算進去） | 使 active 達 6 的那一個 `ok:false` `USER_INPUT`（`QUOTA_ACTIVE_RECURRING_PER_USER=5`） |
| 6.9 | LLM 預算 | 連叫 `llm_summarize` 直到當日 token 超過 10000 | 預算在**執行時（worker）**才檢查，不是 create 當下：超過後 `task.create.v1` 仍回 `ok:true/scheduled`，但那個 **run 會 `FAILED`**（用 `task.status.v1 {include_runs:true}` 看，budget exhausted、non-retryable、隔日 UTC 午夜才恢復） |

> ⚠️ **operator 會 bypass rate-limit 與 quota**：server 端 `if not is_operator:` 才檢查（`app/mcp/server.py`）。因此 **6.5–6.8 這四個 negative 都要在 `OPERATOR_USER_ID` 註解掉（非 operator）的狀態下跑**，否則 6.7/6.8 永遠不會被擋，你會以為限流壞了而白白 debug。建議順序：先做需要 operator 的 4.3/4.4（operator 開），再把 operator 關掉一路做完 6.5–6.8。
> 全部 6.5–6.8 做完、要回頭驗 operator 動作時，再把 `OPERATOR_USER_ID=me` 設回去並 `up -d`。
> 6.7/6.8 跑完，被擋的限制會在隔日（`RATE_LIMIT_DAILY=100`，每日上限）/ 取消後恢復；不想等可調 `.env.docker` 數字後 `up -d`。

---

## Part 7 — Codex 串接（可以！）

**結論：可以串。** Codex CLI 本身就是一個 MCP **client**，stdio 與 streamable-HTTP 兩種 transport 都支援。
（README §1 寫「不支援 ChatGPT」指的是 **ChatGPT 網頁版的 Custom GPT Actions**——那是 OpenAPI Actions，不是 MCP；跟 Codex CLI 是兩回事，別混淆。）

設定檔：`~/.codex/config.toml`。

### 7.1 Codex + 自架 stdio

```toml
[mcp_servers.owl-scheduler]
command = "uv"
args = ["run", "python", "-m", "app.entrypoints.mcp_stdio"]
cwd = "C:/Users/MaxL/work/projects/live_sessions/chatgpt_task"
env = { MCP_USER_ID = "me", MCP_USER_TZ = "UTC" }
```

### 7.2 Codex + 自架 HTTP（建議；對齊你既有 HTTP 測法）

```toml
[mcp_servers.owl-scheduler]
url = "http://localhost:8000/mcp"
```
或用指令加（已實測 `codex mcp add` 的 HTTP 形式支援 `--url`）：
```powershell
codex mcp add owl-scheduler --url http://localhost:8000/mcp
```
> ⚠️ **修正（依實測 `codex mcp add --help`）**：Codex 的 HTTP MCP **不支援自訂 header**——只有 `--url` 與 `--bearer-token-env-var`（`--env` 明確「Only valid with stdio servers」）。所以**不要**寫 `http_headers = {...}`，那不是有效設定鍵。
> 本地 **TrustOnly** 模式其實**不用帶 `X-User-Id`**：server 沒收到該 header 時會 fallback 到 mcp-server 容器自己的 `MCP_USER_ID`（本範本＝`me`），所以 Codex 走 HTTP 連進來就是以 `me` 身分操作。「每個 client 帶不同身分」才需要 header，本地這條路走不了——多租戶情境改用 7.3 的 hosted + WorkOS bearer。

### 7.3 Codex + Hosted（scheduler.paynepew.dev，WorkOS Bearer）

```toml
[mcp_servers.owl-scheduler]
url = "https://scheduler.paynepew.dev/mcp"
# 方式一（OAuth）：先 `codex mcp login owl-scheduler`，Codex 透過 RFC 9728 PRM 走 WorkOS OAuth
# 方式二（自備 JWT）：環境變數放一顆 WorkOS JWT，再用下面這行指定該 env var 名稱
# bearer_token_env_var = "TASK_SCHEDULER_JWT"   # 對應 CLI flag --bearer-token-env-var
```
> Codex 有 `codex mcp login / logout` 子指令；HTTP server 的身分驗證走 **bearer token**（`--bearer-token-env-var`），不是自訂 header。

### 7.4 Codex 串接驗證

> ✅ **本機已驗證（2026-06-14）**：docker 全棧 Up；`codex mcp` 子指令（list/get/add/remove/login）皆在；直接對 `localhost:8000/mcp` 跑 MCP handshake（initialize→tools/list）回得到**正好 5 個 tool**。注意 `codex.exe` 可能不在 PATH（實際在 `…\AppData\Local\OpenAI\Codex\bin\codex.exe`），但在 Codex app 的整合終端機裡直接打 `codex` 即可。

1. `codex mcp list` → 看到 `owl-scheduler`。
2. 在 Codex 對話輸入：「**list my scheduled tasks**」→ 它應呼叫 `task.list.v1` 並回你前面建立的 job。
3. 「**schedule an echo task that says hi, immediately**」→ 應呼叫 `task.create.v1`，`status:"scheduled"`，~10 秒後查 `completed`。

> 一樣受「user_id 三邊一致」規則約束：身分要和 `/connections` 授權時相同，否則 OAuth action 會 `MISSING_CONNECTION`。stdio 用 `MCP_USER_ID="me"`；HTTP 一般用 `X-User-Id="me"` header，但 **Codex 的 HTTP 不送自訂 header**，靠 server 端容器的 `MCP_USER_ID="me"` fallback 同樣解析成 `me`（見 7.2 修正）。

---

## Part 8 — stdio 在哪些環境能用？（你問的）

**一句話：stdio 就是為「能 spawn 子行程的本機 CLI／桌面 client」設計的；不能用在「遠端／網頁」client。**

| 環境 | stdio 可用？ | 說明 |
|---|---|---|
| **Codex CLI** | ✅ | `[mcp_servers.x]` 帶 `command/args/env`（Part 7.1） |
| **Claude Code（CLI）** | ✅ | `claude mcp add` 加 stdio server |
| **Claude Desktop / Cursor** | ✅ | client 在本機 spawn 子行程 |
| **MCP Inspector** | ✅ | 開發點擊驗證最常用 |
| **ChatGPT 網頁版 / claude.ai 網頁** | ❌ | 雲端，無法 spawn 你本機的行程 |
| **Hosted 遠端伺服器情境** | ❌ | client 與 server 不同機，stdio 無法跨機；要用 HTTP |

**為什麼這個專案仍以 HTTP 為主（ADR-006 / README §4）：**
stdio 子行程會**隨對話關閉而死**。但排程器的本質是「到了 wall-clock 時間就要觸發」，所以——

> 🔑 **stdio 的關鍵限制（針對本專案）**：stdio entrypoint **只處理 MCP 工具呼叫（建立／查詢／取消，寫進 Postgres）**，它**不執行** job。真正執行靠那 5 個常駐 daemon（watcher/worker/…）。
> 所以即使你用 stdio 當 transport，**全棧 daemon 仍要在背景跑**，job 才會真的執行。stdio 在這裡只適合「開發期手動操作／Inspector 驗證」，不適合當生產排程入口——生產請用 HTTP（Path A/B）。

---

## Part 9 — Go / No-Go 上線判定

全綠才上線：

| 區塊 | 檢查 | Pass 條件 | Result |
|---|---|---|---|
| 0 | 全棧 + health | 8 服務 Up；`/healthz` ok=true；`/healthz/shed` shed=false | |
| 1 | 探索 | 5 tools；8 actions；auth_status 正確 | |
| 2.1 | immediate | 建立後 ~10s `completed` | |
| 2.2–2.4 | one-shot / 過去拒絕 / tz | 未來 scheduled；過去 `USER_INPUT`；IANA tz ok | |
| 2.5–2.6 | recurring + cancel | 多次 run；cancel 後不再排 | |
| 2.7–2.9 | cancel / list / idempotency | cancel idempotent；list 過濾分頁正確；同 key 回同 job | |
| 3.1 | chain SUCCEEDED | B 在 A 成功後 `completed` | |
| 3.2 | chain FAILED | B2 在 A2 失敗後 `completed` | |
| 3.3/3.4 | from_run_id + negative | 資料流入下游；V1/V6 被拒 | |
| 4.1–4.2 | llm_summarize/polish | result 有內容 + tokens | |
| 4.3–4.4 | http_call/calendar（operator） | 200/events；`${VAR}` 代換成功 | |
| 4.5–4.7 | github/slack/email OAuth | 未連 `MISSING_CONNECTION`；連後實際送達 | |
| 4.8 | digest_v1 真串接 | Slack/Email 收到排版摘要 | |
| 5 | resources/prompts | 4 resources（user 隔離）；2 prompts（參數代換） | |
| 6 | guardrails | 6.1–6.9 全部回對應 error code | |
| 7 | Codex | list/create 成功經由 Codex 觸發 | |
| 8 | stdio 認知 | 確認背景 daemon 才是執行者 | |

---

### 附錄 A — 錯誤碼字典（envelope `error.code`）

`USER_INPUT`（你的參數/配額/限流） · `NOT_FOUND`（含 job 不存在、chain 上游不存在/跨 user） · `INVALID_STATE`（含 operator-only 被擋、chain 上游已終結、cancel 已自然終結、背壓擋件） · `UNKNOWN_ACTION` · `MISSING_CONNECTION`（附 `connect_url`） · `INTERNAL`。
> 另有保留碼 **`DUPLICATE`**：列在 7-code 詞彙裡（`app/mcp/errors.py`），但目前 `map_domain_error` **不會實際回傳**——idempotency_key 重送是回傳「原本那個 job」而非報錯（見 Part 2.9），所以你測不到這個碼。
成功一律 `{"ok":true,"data":{...}}`；失敗 `{"ok":false,"error":{code,message,field?,expected?,connect_url?}}`。

### 附錄 B — 內部 run 狀態 ↔ 對外 status

`PENDING/QUEUED/WAITING` → `scheduled`；`RUNNING/RETRYING` → `running`；`SUCCEEDED` → `completed`；`FAILED` → `failed`；`CANCELLED` → `cancelled`。

### 附錄 C — 常見卡關

- action 一直 `MISSING_CONNECTION`：user_id 三邊沒對齊（Part 0「真相 4」）。
- job 永遠 `scheduled`：背景 daemon 沒在跑（`docker compose --profile full ps`）。
- 改了 `.env.docker` 沒生效：用了 `restart` 而非 `up -d`。
- `http_call`/`calendar_digest_ics` 被擋：`OPERATOR_USER_ID` 沒設或沒對齊。
