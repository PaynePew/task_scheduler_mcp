# Runbook: Mute External Monitor During Deploy

**When to use**: You have a deploy that produces a brief unhealthy window (container recreate, blue/green cutover, migration lock) and an external monitor (Better Stack, UptimeRobot, Pingdom, Statuscake, PagerDuty heartbeat) is firing false-positive alerts during deploys.

**The pattern**: call the monitor's API to pause it before the deploy step, resume it after — with `if: always()` on the resume so a failed deploy cannot leave the monitor silently muted.

This runbook is generic. Substitute your monitor's API endpoint, auth scheme, and pause/resume verbs. Worked example used in this repo is Better Stack on `deploy-vps.yml`.

---

## Prerequisites

- A deploy workflow on GitHub Actions (or equivalent CI) that performs the disruptive step.
- Admin access to the external monitor's account (to create API tokens).
- Admin access to the GitHub repo (to create secrets / variables).
- A way to verify monitor state (UI or status page).

---

## 1. Identify the correct API endpoint

Documentation lies, especially across major API versions. Probe before trusting any spec.

```bash
# Authoritative tells whether a path exists:
curl -sS -o /dev/null -w "%{http_code}\n" \
  -X PATCH "https://<vendor>/api/v3/monitors/0" \
  -H "Content-Type: application/json" -d '{}'

# Compare with the previous major version:
curl -sS -o /dev/null -w "%{http_code}\n" \
  -X PATCH "https://<vendor>/api/v2/monitors/0" \
  -H "Content-Type: application/json" -d '{}'
```

Interpretation:

| Status | Meaning |
|--------|---------|
| `404`  | Route doesn't exist — wrong path or wrong version |
| `401`  | Route exists, just needs auth — **this is what you want** |
| `403`  | Route exists, your auth lacks scope |
| `405`  | Route exists, wrong HTTP verb |

**Pitfall**: A `404` from the API server when you're sure the resource exists is almost always the route being wrong, not the resource. Probe both versions before debugging tokens.

---

## 2. Create a least-privilege API token at the vendor

Most monitor SaaS allow scoping a token to specific resources + actions. Use the minimum:

| Operation | Required scope |
|-----------|----------------|
| Pause monitor | `monitors:write` (or `monitors:edit`, depends on vendor) |
| Resume monitor | Same as Pause |
| List monitors (sanity check) | `monitors:read` |

**Scope to the single target monitor**, not the whole account. If the token leaks, blast radius is limited to "someone can mute one specific monitor", not "someone can delete every monitor + incident in the account".

**Save the token immediately on the page that creates it.** SaaS vendors hash tokens server-side and cannot reveal the plaintext after the create page closes. If you lose the value, you must delete and regenerate the token.

Name the token to indicate where it's used and why: `gh-actions-deploy-pause-<service>`. When you audit tokens 6 months later, you want to know what each one does.

---

## 3. Find the resource ID

Most SaaS UIs hide internal IDs but expose them in URLs:

```
https://uptime.betterstack.com/team/<team_id>/monitors/4421333
                                              ─────┬─────
                                                  resource ID
```

This pattern is universal — Linear, Jira, Notion, Stripe, GitHub itself all encode IDs in URLs. When a vendor's docs ask for `monitor_id` / `issue_id` / `resource_id`, check the URL first.

---

## 4. Add to GitHub repo secrets / variables

Decide for each value: secret or variable?

| Value type | Storage | Example |
|------------|---------|---------|
| Anything that gives access if leaked | **Secret** (encrypted, masked in logs) | API tokens, SSH keys, passwords |
| Anything safe to expose | **Variable** (plaintext, visible in UI and logs) | Monitor ID, region, image tag, env name |

Rule of thumb: **"What can an attacker do if they have this value?"** If the answer is "log in / call APIs as me", it's a secret. If the answer is "know what monitor ID I use", it's a variable.

UI path: repo → **Settings** → **Secrets and variables** → **Actions** → Secrets tab / Variables tab → **New repository secret** / **New repository variable**.

**Choosing Repository vs Environment scope**:

- **Repository**: every workflow in the repo can use it. Default. Simplest.
- **Environment** (e.g. `production`, `staging`): scoped to workflows that explicitly declare `environment: production`. Supports protection rules (manual approval, wait timer, restricted branches). Use this once you have a multi-stage deploy.

This runbook uses repository scope because the project deploys directly to one environment.

---

## 5. Wire pause/resume into the deploy workflow

Two steps, bracketing the disruptive part:

```yaml
- name: Pause external monitor
  env:
    BS_TOKEN: ${{ secrets.BETTERSTACK_API_TOKEN }}
    MONITOR_ID: ${{ vars.BETTERSTACK_MONITOR_ID }}
  run: |
    curl -fsS -X PATCH "https://uptime.betterstack.com/api/v2/monitors/$MONITOR_ID" \
      -H "Authorization: Bearer $BS_TOKEN" \
      -H "Content-Type: application/json" \
      -d '{"paused": true}' > /dev/null
    echo "Monitor $MONITOR_ID paused"

# … your existing deploy + smoke test …

- name: Resume external monitor
  if: always()
  env:
    BS_TOKEN: ${{ secrets.BETTERSTACK_API_TOKEN }}
    MONITOR_ID: ${{ vars.BETTERSTACK_MONITOR_ID }}
  run: |
    curl -fsS -X PATCH "https://uptime.betterstack.com/api/v2/monitors/$MONITOR_ID" \
      -H "Authorization: Bearer $BS_TOKEN" \
      -H "Content-Type: application/json" \
      -d '{"paused": false}' > /dev/null
    echo "Monitor $MONITOR_ID resumed"
```

### Why each line matters

| Element | Why |
|---------|-----|
| `secrets.X` vs `vars.X` | Two different namespaces — secrets and variables don't share syntax. |
| `env:` block (not inline `${{ }}` in `run:`) | GitHub Actions redacts secret values by literal string match. Inline expansion into shell can fail to redact under quoting / multiline / indent edge cases. `env:` injection is the documented-safe pattern. |
| `curl -fsS` | `-f` exits non-zero on HTTP 4xx/5xx; `-s` silent; `-S` show errors if `-s` is set. Standard "fail loudly, log quietly" combo. |
| `-X PATCH` (not `PUT`) | Partial update — only the `paused` field. PUT would require sending the full monitor config. |
| `Authorization: Bearer $TOKEN` | OAuth-style bearer auth, the de-facto standard for SaaS APIs. |
| `> /dev/null` on success | Response body is uninteresting on success. Keeps log clean. Remove it temporarily if debugging. |
| **`if: always()` on Resume** | **The single most important line.** See below. |

### Why `if: always()` is load-bearing

GitHub Actions default behaviour: if any step fails, subsequent steps are skipped. Without `if: always()`:

```
build → pause ✓ → ssh ✓ → smoke ✗ → resume SKIPPED → monitor stays Paused forever
                                                       ↑ silent failure
                                                       real downtime later won't alert
```

`if: always()` overrides the default and runs the step even when:

- A previous step failed
- The job was cancelled by the user
- A timeout fired

This is the safety net. Without it the whole pattern is **worse than no monitoring** — you'd think you're being watched, but actually you've been mute since the last failed deploy.

GitHub Actions step-condition cheat sheet:

```yaml
if: success()    # default — run if all previous steps succeeded
if: failure()    # run only if any previous step failed (e.g. "notify on failure")
if: cancelled()  # run only if the job was cancelled
if: always()    # run no matter what (success, failure, or cancel)
```

---

## 6. Verify with two deploys: happy path + fault injection

### 6.1 Happy path

Trigger any normal deploy. Watch:

1. Workflow logs: `Pause` step echoes paused, `Resume` step echoes resumed.
2. Monitor UI: timeline shows a `Paused` segment during the deploy window, returning to `Active` after.
3. Alert channel (Slack / email / PagerDuty): no `Down` alert during that window.

### 6.2 Fault injection (REQUIRED for sign-off)

You must prove `if: always()` actually fires. Don't trust it from inspection — run a real failing deploy.

The cleanest injection point is the **smoke test step** (between Pause and Resume): change the URL to a 404 path and merge to main:

```yaml
# Smoke test — fault-injected with non-existent URL
- name: Smoke test
  run: |
    curl -fsS https://your-service/this-does-not-exist || exit 1
```

Expected outcome:

| Step | Conclusion |
|------|------------|
| Pause monitor | ✅ success |
| Deploy | ✅ success (the actual service stays healthy) |
| Smoke test | ❌ **failure** (intentional) |
| Resume monitor | ✅ **success via `if: always()`** |
| Job | failure |

Then **immediately revert** the smoke URL with a follow-up PR. The actual service is unaffected during fault injection (the deploy itself succeeded; only the workflow reports failure), but you don't want a broken smoke step lingering on main.

If the Resume step is skipped instead of running: `if: always()` is missing or misspelled (`always` is a function call — `if: always` without `()` is a bug).

---

## Common pitfalls

| Pitfall | Symptom | Fix |
|---------|---------|-----|
| Wrong API version | `404` on every call | Probe v2 / v3 with `curl -o /dev/null -w "%{http_code}"` |
| Missing `if: always()` | One failed deploy leaves monitor paused indefinitely; subsequent real downtime doesn't alert | Add `if: always()` to the Resume step |
| `if: always` instead of `if: always()` | Step never runs (silently treated as a literal string condition) | Add the parens |
| Inline `${{ secrets.X }}` in `run:` | Token leaks into logs under quoting edge cases | Move to `env:` block |
| Token has `monitors:*` instead of just `monitors:write` on one ID | Compromised token can wipe entire monitoring account | Re-scope token; rotate the old one |
| Pause step also has `if: always()` | Harmless but useless — pause failing is fine, the safety net is Resume | Remove from Pause |
| Resume URL still has the trailing `paused=true` from copy-paste | Monitor toggles between Paused-Paused, never resumes | Diff the two snippets character-by-character |

---

## Cross-references

- The Better Stack–specific implementation: [.github/workflows/deploy-vps.yml](../../.github/workflows/deploy-vps.yml)
- Why we picked Better Stack over UptimeRobot: [ADR-031](../adr/ADR-031-monitoring-better-stack-over-uptimerobot.md)
- The driving issue + AC evidence: [#77](https://github.com/PaynePew/task_scheduler_mcp/issues/77)
- PRD reference: [docs/PRD/deploy-w3.md §D7-B](../PRD/deploy-w3.md)
