# Task Scheduler MCP

**🌐 繁體中文** | [English](README.md)

一個可自行托管的 MCP 伺服器，以持久化 HTTP 服務方式運行 — **7 個工具 · 4 個資源 · 2 個提示詞** — 讓 LLM 客戶端能夠排程、串接並取消以 Postgres + SQS 為後端的週期性任務。

[![CI](https://github.com/PaynePew/task_scheduler_mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/PaynePew/task_scheduler_mcp/actions) [![Demo](https://img.shields.io/badge/demo-scheduler.paynepew.dev-blue)](https://scheduler.paynepew.dev) [![Status](https://img.shields.io/badge/status-status.paynepew.dev-green)](https://status.paynepew.dev)

---

<!-- HERO-GIF placeholder — filled in W4-S17b -->

---

## §1 適用對象

為有需要的開發者而打造：運行自己的 webhooks/API，並希望透過自然語言 LLM 對話來排程任務，且需要在對話結束後仍能持久保存可稽核的執行記錄。

**支援的 MCP 客戶端：** Claude Desktop · Cursor · Claude in Chrome · MCP Inspector
**不支援：** ChatGPT（Custom GPT Actions ≠ MCP 協議）

---

## §2 系統架構

```mermaid
flowchart LR
    User([User]) --> LLM[LLM Client<br/>Claude / Cursor]
    LLM -->|MCP tool call| MCP[MCP Server<br/>HTTP · stdio]
    MCP --> DB[(Postgres)]
    DB --> W[Watcher<br/>SKIP LOCKED]
    W -->|enqueue| Q[(SQS / ElasticMQ)]
    Q --> Worker[Worker]
    Worker -->|dispatches| AH[ActionHandler]
    AH -->|outbound API call| Ext[External Service<br/>Slack · GitHub · SMTP · R2 · ICS]
    Worker --> DB
    DB --> CW[ChainWatcher]
    DB --> RW[RecurringJobWatcher]
```

MCP 伺服器持久化 `Job` 與 `JobRun` 資料列。**Watcher** 透過 `FOR UPDATE SKIP LOCKED` 認領到期的執行，並將其推送至佇列。**Worker** 分派至型別化的 **ActionHandler**。**ChainWatcher** 和 **RecurringJobWatcher** 消費僅能附加的 `run_events` outbox — 兩者從不輪詢可變狀態。

---

## §3 使用方式

三條部署路徑。它們使用**不同的 MCP transport**，OAuth 也走**不同的範疇** — 混用是最常見的踩雷點。選一條走到底，不要中途交叉。

|                          | **A. Hosted（線上）**                              | **B. 自行托管（HTTP）**                          | **C. 自行托管（stdio）**                                  |
|--------------------------|----------------------------------------------------|--------------------------------------------------|-----------------------------------------------------------|
| MCP transport            | streamable-http over TLS                           | streamable-http                                  | stdio 子行程                                              |
| MCP 端點                 | `https://scheduler.paynepew.dev/mcp`               | `http://localhost:8000/mcp`                      | spawn：`uv run python -m app.entrypoints.mcp_stdio`       |
| `user_id` 來源           | WorkOS Bearer JWT 的 `sub` 欄位（ADR-053）         | `X-User-Id` header（trust-only，ADR-015）        | `MCP_USER_ID` 環境變數（trust-only，ADR-015）             |
| OAuth 儀表板             | `https://scheduler.paynepew.dev/connections`       | `http://localhost:8000/connections`              | `http://localhost:8000/connections`（同一個 web tier）    |
| 本機要先啟動什麼         | 不用                                               | `docker compose --profile full up -d`            | `docker compose --profile full up -d`（為了 `/connections`）+ MCP 客戶端按需 spawn stdio |
| `CONNECTIONS_BASE_URL`   | （Operator 管理）                                  | `http://localhost:8000`                          | `http://localhost:8000`                                   |

> **為什麼這張表很重要。** 當 action（`github_digest` / `slack_post` / `email_send`）找不到對應的上游 OAuth token，server 會回 `MISSING_CONNECTION` 加上 `connect_url`，這個 URL 是用**它自己的 `CONNECTIONS_BASE_URL`** 組出來的。如果你 MCP 客戶端指向 Path A、但你跑去 Path B/C 做 OAuth（或反過來），兩邊的 `user_id` 不一樣、connect_url 指向錯誤的 host，每次 action 呼叫都會默默失敗。

### A. Hosted — 不用安裝，兩分鐘上手

```jsonc
// Claude Desktop / Claude Code / Cursor 的 MCP 設定
{ "mcpServers": { "task-scheduler": {
  "url": "https://scheduler.paynepew.dev/mcp",
  "transport": "streamable-http"
}}}
```

1. 重啟 MCP 客戶端；首次呼叫工具會觸發 WorkOS OAuth 授權流程（依 [RFC 9728 Protected Resource Metadata](https://www.rfc-editor.org/rfc/rfc9728)）。
2. 登入完成後開啟 `https://scheduler.paynepew.dev/connections`，依需要點各個 Connect（GitHub / Slack / Google）。
3. 健康檢查：`curl https://scheduler.paynepew.dev/healthz` → `{"ok":true,"db":"connected"}`。

### B. 自行托管（HTTP）— 上線部署推薦這條

```bash
git clone https://github.com/PaynePew/task_scheduler_mcp
cd task_scheduler_mcp
cp .env.docker.example .env.docker     # ← compose 讀的是這份，不是 .env
cp .env.example .env                   # 僅供 host-side `uv run`（測試、alembic）
docker compose --profile full up -d
```

```jsonc
// MCP 客戶端設定（Claude Desktop / Code / Cursor）
{ "mcpServers": { "task-scheduler": {
  "url": "http://localhost:8000/mcp",
  "transport": "streamable-http",
  "headers": { "X-User-Id": "me" }
}}}
```

OAuth 儀表板：開啟 `http://localhost:8000/connections` → 確認頁面顯示 `Signed in as me`（要跟你 `X-User-Id` header 一致）→ 點各個 Connect。要對外開放時，編輯 `.env.docker`：

- 設 `CONNECTIONS_BASE_URL=https://yourdomain.tld`，OAuth callback 跟 PRM resource URL 才會用公開 host。
- 把 §7 的 WorkOS Bearer auth 填好 — **絕對不要把 trust-only `X-User-Id` 模式暴露在公開網路上**（任何人猜到 header 就能讀你的任務）。
- 全新 Ubuntu 24.04 機器：`bin/setup-vps.sh` 一鍵裝 Docker + Caddy + ufw + 每夜 Postgres 備份 + systemd 重開機自動重啟。

**透過 `http_call` 自帶 LLM：** `action: "http_call"`，在 headers/body 引用 `${ANTHROPIC_API_KEY}`，執行時會從環境變數展開（[ADR-032](docs/adr/ADR-032-secrets-aware-action-handlers-and-env-var-substitution.md)）；變數名稱要加進 `ALLOWED_TEMPLATE_VARS`。速率限制：**1 000 次建立/24h · 每分鐘 10 次爆衝** — 可透過 env 調整（[ADR-042](docs/adr/ADR-042-postgres-backed-rate-limiting.md)）。

### C. 自行托管（stdio）— MCP Inspector / 開發便利路徑

Stdio MCP 是子行程，對話結束就消失（為什麼通常不推薦：[§4](#§4-為什麼用-http而非-stdio)）。只在 MCP Inspector 除錯、短期 dev 用。**OAuth 儀表板還是在 HTTP web tier 上**，所以整個 stack 仍要起來：

```bash
cp .env.docker.example .env.docker
cp .env.example .env
docker compose --profile full up -d    # web tier（給 /connections 用）+ Postgres + queue
```

```jsonc
// MCP 客戶端設定 — 客戶端按需 spawn stdio 進程
{ "mcpServers": { "task-scheduler": {
  "type": "stdio",
  "command": "uv",
  "args": ["run", "python", "-m", "app.entrypoints.mcp_stdio"],
  "env": { "MCP_USER_ID": "me", "MCP_USER_TZ": "UTC" }
}}}
```

OAuth：開啟 `http://localhost:8000/connections` → 連接 provider。**確認 `Signed in as me` 跟你傳給 stdio 進程的 `MCP_USER_ID` 是同一個字串。**

> **stdio 的隱形坑。** 兩個進程各自讀自己的 `MCP_USER_ID`：stdio 進程讀你 MCP 客戶端設定 `env` 區塊；web tier 讀 `.env.docker`。**兩邊必須解析成同一個字串**。如果不一樣，你會用一個 user OAuth、stdio 進程查另一個 user — 看起來像連線沒做，其實做了但對不上，error envelope 仍然吐 `connect_url=http://localhost:8000/connections`。

Inspector 快速驗證：

```bash
MCP_USER_ID=local-dev MCP_USER_TZ=UTC \
  npx @modelcontextprotocol/inspector uv run python -m app.entrypoints.mcp_stdio
```

延伸閱讀：[ADRs](docs/adr/) · [PRDs](docs/PRD/) · 設計決策（下方 §7）

---

## §4 為什麼用 HTTP，而非 stdio

Stdio MCP 是子行程 — 對話關閉時就會消亡。排程器必須在不依賴任何客戶端開啟的情況下，依照掛鐘時間觸發任務。請見 [ADR-006](docs/adr/ADR-006-mcp-transport-dual-stdio-http.md)。

---

## §5 部署架構

<!-- DIAGRAM-D2 infrastructure diagram placeholder — filled in W4-S17a -->

| | VPS（運行時 — 已上線） | Fargate（設計文物） |
|---|---|---|
| **平台** | AWS Lightsail 東京 | ECS Fargate / RDS / ALB / SQS |
| **每月費用** | ~$5 | ~$117–145 閒置 |
| **TLS** | Caddy 自動 ACME | ACM + ALB |
| **資料** | 容器內 Postgres + R2 備份 | RDS Multi-AZ 就緒 |
| **驗證方式** | 每次 CI/CD 推送 | `validate-fargate.yml`（W4） |

---

## §6 開發路線

### W3 驗收層

| 層次 | 描述 | 狀態 |
|---|---|---|
| L1 | 程式碼綠燈 — CI + `terraform plan` 通過 | ✅ W3 |
| L2 | 透過 `bin/setup-vps.sh` 完成全新 VPS 配置 | ✅ W3 |
| L3 | 上線 URL — `scheduler.paynepew.dev/healthz` 回傳 200 | ✅ W3 |
| L4a | Echo 週期任務在 5 分鐘內觸發 ≥ 2 次 | ✅ W3 |
| L4b | 串接 A→B 完成；`chain_watcher` 確認存活 | ✅ W3 |
| L5 | Better Stack ≥ 24 小時綠燈；R2 備份 + 還原演練 | ✅ W3 |
| L6 | Fargate `validate-fargate.yml` 通過；費用 < $5 | ⬜ W4 |
| L7 | 示範影片 / 替代文物 | ⬜ W4 |

### W4 驗收關卡

| 關卡 | 描述 | 狀態 |
|---|---|---|
| G1 | CI 綠燈；測試覆蓋率達標 | ⬜ |
| G2 | 5 個新處理器已加入登錄表；`task.list_actions.v1` 回傳 7 | ⬜ |
| G3 | Digest 工作流上線 — 生產 VPS 連續 ≥ 5 個工作日發送 Slack 訊息 | ⬜ |
| G4 | `tasks://recent-results` 可查詢；回傳真實 24 小時資料 | ⬜ |
| G5 | 落地頁上線 — `curl https://scheduler.paynepew.dev/` 回傳 200 + HTML | ⬜ |
| G6 | 速率限制 — 整合測試：第 1001 次建立請求被拒絕 | ⬜ |
| G7 | Fargate 證據 — 乾跑 + 錄製跑皆通過；費用 < $5 | ⬜ |
| G8 | 視覺文物 — README 中含 Hero GIF + 4 張截圖 + 3 張圖表 | ⬜ |
| G9 | README 精修 + i18n — EN + zh-TW，約 150 行 | ⬜ |
| G10 | ADR 群組 — 13 個新 W4 ADR 已合併 | ⬜ |

---

## §7 設計決策（ADR）

W1 範疇、語言、資料存儲、佇列、schema、模組佈局（[ADR-001–023](docs/adr/)）：

| ADR | 決策 |
|---|---|
| [ADR-001](docs/adr/ADR-001-project-scope.md) | 專案範疇 |
| [ADR-002](docs/adr/ADR-002-implementation-language-python.md) | Python |
| [ADR-003](docs/adr/ADR-003-primary-data-store-postgres.md) | Postgres 為主要存儲 |
| [ADR-006](docs/adr/ADR-006-mcp-transport-dual-stdio-http.md) | 雙模 stdio + HTTP MCP 傳輸 |
| [ADR-007](docs/adr/ADR-007-watcher-ha-skip-locked.md) | Watcher HA 透過 `SKIP LOCKED` |
| [ADR-008](docs/adr/ADR-008-message-queue-sqs.md) | SQS / ElasticMQ 佇列 |
| [ADR-009](docs/adr/ADR-009-database-schema-outbox.md) | 三表 schema + 交易式 outbox |
| [ADR-013](docs/adr/ADR-013-action-catalog-typed-registry.md) | 型別化 action 登錄表 |
| [ADR-014](docs/adr/ADR-014-mcp-tool-surface-v1.md) | MCP 工具介面 v1 |
| [ADR-018](docs/adr/ADR-018-no-server-side-llm-in-w2.md) | W2 不使用伺服器端 LLM |
| [ADR-018-amended](docs/adr/ADR-018-amended-w4-reconsidered-stays-llm-agnostic.md) | W4 重新審視 — 維持 LLM 中立 |

W3 部署群組（[ADR-024–031](docs/adr/)）：

| ADR | 決策 |
|---|---|
| [ADR-024](docs/adr/ADR-024-tier-scoping-and-w3-cut-scope.md) | W3 層次範疇 |
| [ADR-025](docs/adr/ADR-025-network-topology-w3-public-ecs-private-rds.md) | 網路拓撲 |
| [ADR-026](docs/adr/ADR-026-ecs-service-topology-and-replica-count.md) | ECS 服務拓撲 |
| [ADR-027](docs/adr/ADR-027-deployment-target-pivot-vps-first-aws-as-design-artifact.md) | VPS 優先運行；Fargate 作為設計文物 |
| [ADR-028](docs/adr/ADR-028-caddy-over-nginx-for-vps-reverse-proxy.md) | 採用 Caddy 取代 nginx |
| [ADR-029](docs/adr/ADR-029-vps-deployment-mechanics-ghcr-push-ssh-pull-containerized-data.md) | VPS 部署機制 |
| [ADR-030](docs/adr/ADR-030-vps-operational-concerns-backup-monitoring-fargate-validation.md) | 運維考量 |
| [ADR-031](docs/adr/ADR-031-monitoring-better-stack-over-uptimerobot.md) | 採用 Better Stack 監控 |

W4 行動衝刺群組：

| ADR | 決策 |
|---|---|
| [ADR-032](docs/adr/ADR-032-secrets-aware-action-handlers-and-env-var-substitution.md) | 透過環境變數替換管理密鑰 |
| [ADR-033](docs/adr/ADR-033-inter-handler-data-flow-via-job-run-result.md) | 透過 `JobRun.result` 實現跨處理器資料傳遞 |
| [ADR-037](docs/adr/ADR-037-tasks-recent-results-mcp-resource-as-briefing-surface.md) | `tasks://recent-results` 簡報介面 |
| [ADR-038](docs/adr/ADR-038-mcp-call-as-future-direction.md) | Worker 作為 MCP 客戶端 *（延後 — v2）* |
| [ADR-039](docs/adr/ADR-039-plan-abstraction-as-future-direction.md) | Plan 抽象層 *（延後 — v2）* |
| [ADR-040](docs/adr/ADR-040-predicate-based-chain-as-future-direction.md) | 條件式串接 *（延後 — v2）* |
| [ADR-041](docs/adr/ADR-041-static-landing-page-and-caddy-path-routing.md) | 靜態落地頁 + Caddy 路徑路由 |
| [ADR-042](docs/adr/ADR-042-postgres-backed-rate-limiting.md) | Postgres 速率限制 |
| [ADR-044](docs/adr/ADR-044-project-rename-to-task-scheduler-mcp.md) | 專案重新命名為 `task_scheduler_mcp` |
| [ADR-048](docs/adr/ADR-048-calendar-digest-ics-action-design.md) | 透過簽署 ICS URL 實現日曆摘要 |

---

## §8 MCP 介面

<!-- HANDLER-DETAIL placeholder — descriptions filled in W4-S15b after handlers ship -->

**工具（7）：** `task.create.v1` · `task.list.v1` · `task.status.v1` · `task.cancel.v1` · `task.list_actions.v1` · *（W4 工具待 S15b）*

**資源（4）：** `tasks://list` · `tasks://actions` · `tasks://job/{job_id}` · `tasks://recent-results`

**提示詞（2）：** `daily_review` · `setup_summary`

**功能特色：** 週期性（cron）任務 · 任務串接（`trigger_on_job_id`）· 取消語意 · 透過 `JobRun.result` 實現跨處理器資料傳遞

**支援的 MCP 客戶端：** Claude Desktop · Cursor · Claude in Chrome · MCP Inspector
**不支援：** ChatGPT（Custom GPT Actions 使用不同協議 — 非 MCP）

---

## §9 本地開發

Host-side 測試迴圈（不啟動 full stack）：

```bash
uv sync                                    # 安裝依賴
cp .env.example .env
docker compose up -d postgres elasticmq    # 只起 Postgres + queue
uv run alembic upgrade head
uv run pytest -m "not integration"         # 單元測試
uv run pytest -m integration               # 需要運行中的服務
uv run ruff check . && uv run ruff format --check .
```

MCP Inspector 對 stdio 入口的用法，請見 [§3 Path C](#§3-使用方式)。預期介面：**5 個工具 · 4 個資源 · 2 個提示詞**（W4 完成）。完整人工驗證流程：[docs/PRODUCTION-VERIFICATION.md](docs/PRODUCTION-VERIFICATION.md)。
