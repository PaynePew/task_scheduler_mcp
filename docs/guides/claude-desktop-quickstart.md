# Owl Task Scheduler MCP — Claude Desktop Quickstart

Schedule, chain, and cancel recurring tasks from a Claude chat. Setup takes about 2 minutes.

**You need:** Claude Desktop (latest) and a paid Claude plan (Pro or higher).

---

## 1. Add the connector

1. Claude Desktop → **Settings** → **Connectors**.
2. Click **Add custom connector**.
3. Enter:
   - **Name:** `owl-scheduler`
   - **URL:** `https://scheduler.paynepew.dev/mcp`
4. Leave the advanced fields empty and click **Add**.

## 2. Sign in

Click **Connect**. A browser window opens — sign in and approve. When it says **Connected**,
you're in, and the scheduling tools are now available in chat.

## 3. Connect your apps

To let it post to Slack, summarize GitHub, or send email, you grant access once:

1. Open **https://scheduler.paynepew.dev/connections**
2. Make sure the name shown at the top is the account you just signed in with.
3. Click **Connect** next to each app you want: GitHub, Slack, or Google.

> If you skip this, those actions will fail with a "connection needed" message and a link back to
> this page.

## 4. Try it

Type into Claude:

```
List all my scheduled tasks
```

If it answers, you're done.

---

## Things you can ask for

```
Every weekday at 9am Taipei time, post a summary of my GitHub notifications to Slack #standup
Email me@example.com a reminder titled "standup" in 30 minutes
Remind me to review PRs every Monday at 10am
What can you schedule?
Cancel that task
```

It handles repeating schedules and chaining one task's result into the next.

> **If Claude tries to use its own built-in scheduler instead** (e.g. it says it can't do
> something this connector clearly can), name the connector explicitly: start your request with
> **"use owl-scheduler to …"**. `owl-scheduler` is a collision-free handle that routes straight here.

---

## If something doesn't work

| What you see | What to do |
|---|---|
| "Connection needed" when running an action | Open the connections page and connect that app, using the same account you signed in with |
| Asked to sign in again and again | Disconnect the connector, then Connect again |
| No "Add custom connector" button | Your plan doesn't include connectors — upgrade to Pro or higher |
| The tools don't show up in chat | Make sure the connector says "Connected"; restart Claude Desktop |

Need help? File an issue: https://github.com/PaynePew/task_scheduler_mcp/issues
