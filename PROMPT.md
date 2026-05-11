# ChatGPT Task Scheduler Prototype

## System Requirements

Build a job scheduler with an MCP (Model Context Protocol) interface:

- Users schedule tasks for future execution via MCP tool calls
- A background watcher scans for due jobs and pushes them to a queue
- Workers pull jobs from the queue and execute them
- Support task creation, listing, status checking, and cancellation
- Tool naming follows namespace + action verb pattern (e.g., `task.create`)

### Architecture

```
User → MCP Tool Call → Job Scheduler API → DB
                                            ↓
                              Watcher (scans DB) → Queue → Worker (executes)
```

## Design Questions

Answer these before you start coding:

1. **Watcher vs Cron:** Why separate the watcher from the worker? What problems does a single cron job that both scans and executes have?
   - Watcher vs. Cron:
     - Why Separate Querying from Execution?The primary reason for separating the Watcher (Querying) from the Worker (Execution) is to decouple responsibilities and ensure system robustness as the number of users and jobs scales. Combining them into a single process (like a traditional Cron job) creates a fragile system due to the following reasons:
       1. Preventing Execution Blocking (Ensuring Timeliness)
       - The Problem: If the executor is responsible for both querying and running jobs, a long-running task can block the entire process. For example, if the executor is set to query the DB every 5 minutes, but a specific job takes 6 minutes to complete, the executor will miss the next scheduled query window.
       - The Solution: By separating them, the Watcher remains "lightweight." It only focuses on picking up due jobs and moving them to a queue, ensuring that scanning happens on time, every time, regardless of how long the actual tasks take to execute.
       2. Handling Traffic Spikes (Absorption of Bursts)
       - The Problem: Systems often face "execution peaks" (e.g., Black Friday reminders or top-of-the-hour notifications). A single executor has a fixed capacity within a time window; if 10,000 jobs trigger at once, a combined system will likely crash or suffer massive delays.
       - The Solution: Separating the two allows the Queue to act as a buffer. The Watcher can quickly push thousands of jobs into the queue in seconds, while a pool of Workers can consume them at their own pace without crashing the "scheduler" logic.
       3. Independent Scalability
       - The Problem: When querying and execution are tied together, you cannot easily scale the system. Running multiple instances of the same Cron job often leads to "double-execution" bugs unless complex distributed locking is implemented.
       - The Solution: You can keep a single (or highly available) Watcher to maintain order, while horizontally scaling the Workers. If the queue gets too long, you simply spin up more Workers to increase throughput without affecting the DB querying logic.
       4. Enhanced Fault ToleranceThe
       - Problem: In a combined system, if a job causes a "segmentation fault" or a fatal error that crashes the process, the entire scheduler stops running, and future jobs won't be picked up.
       - The Solution: If a Worker crashes while executing a job, the Watcher is unaffected and continues to schedule other tasks. Meanwhile, modern queue systems can detect the Worker's failure and re-queue the task for another Worker to try again.

2. **Queue Layer:** Why put a queue between the watcher and worker instead of having the watcher call the worker directly? What are the benefits?
   - Queue Layer: Benefits of Asynchronous Decoupling:
     - Introducing a Queue between the Watcher and Worker serves as a critical buffer, transforming the system from a tightly coupled synchronous model into a resilient asynchronous architecture.The core objective is to decouple database querying from job execution, providing the following key benefits:
       1. Absorbing Traffic Spikes (Load Leveling)
       - Buffer for Bursts: The queue acts as a "shock absorber" during high-traffic events (e.g., flash sales or bulk notifications).
       - Traffic Smoothing: Instead of overwhelming workers with a sudden flood of requests, the Watcher can en-queue thousands of tasks instantly. Workers then consume these tasks at a steady, controlled rate, preventing system-wide overload.
       2. Fault Isolation (Failure Decoupling)
       - Isolating Execution Failures: Since the Watcher only needs to successfully hand off a task to the queue, the querying process remains unaffected if a worker crashes or a specific job fails.
       - Independent Stability: The "finding work" logic (Watcher) is completely separated from the "doing work" logic (Worker). A bug in the execution code won't stop the system from continuing to scan for other due jobs.
       3. Independent Horizontal Scalability
       - Elastic Workers: Job execution workers can be scaled horizontally and independently of the Watcher.
       - On-Demand Scaling: If the queue backlog grows, you can simply spin up more worker instances. Because they are decoupled, this does not increase the load on the Watcher or require complex coordination.
       4. Message Durability and Reliability
       - Guaranteed Processing: Message queues provide durability; tasks are not "lost" if a worker goes down mid-process.
       - Atomic Deletion: Using services like Amazon SQS, a message is only removed from the queue after successful execution. If a worker fails, the message becomes visible again after a timeout, allowing for automatic retries and ensuring "at-least-once" delivery.

3. **Time Bucket Partitioning:** Instead of `SELECT * WHERE scheduled_at <= now()`, why partition jobs by time bucket (e.g., hour)? What happens to query performance at 1M+ jobs without partitioning?
   - As the job volume grows to 1M+, querying based solely on a timestamp (scheduled_at <= now()) leads to significant performance degradation. Here’s why partitioning by Time Bucket is necessary:
     1. Avoiding Full Index Scans
     - The Problem: Without partitioning, the database must scan a massive B-Tree index for every due job. As the table grows, the index size exceeds memory (RAM), forcing slow disk I/O.
     - The Solution: Partitioning limits the search space to a specific "bucket" (e.g., the current hour). This allows the database to ignore millions of irrelevant rows, keeping queries fast and predictable.
     2. Efficient Data Retention
     - The Problem: Deleting millions of old, completed rows using DELETE WHERE is extremely slow and causes index fragmentation.
     - The Solution: With time buckets, you can simply drop an entire partition (e.g., the bucket from 2 days ago). This is an O(1) operation that instantly frees up space without impacting system performance.
     3. Preventing Hotspots
     - The Problem: In distributed databases, searching for "latest" tasks often hits a single database node, creating a bottleneck.
     - The Solution: Time buckets allow you to distribute the load. You can parallelize the Watcher to scan multiple buckets across different nodes simultaneously.

4. **Tool Naming:** Why `task.create` instead of `createTask`? How does naming convention affect LLM tool selection accuracy?
   - The choice of task.create over createTask is a strategic design for LLM Tool Discovery.
     1. Why namespace.action?
     - Categorization: It groups related tools under a single entity (Task). This structure helps organized discovery, similar to how file systems or modern APIs (like Stripe) work.
     - Conflict Avoidance: It prevents ambiguity. In complex systems, user.delete and task.delete are far more distinct to an LLM than deleteUser and deleteTask, especially when the tool list is long.
     2. Impact on LLM Accuracy
     - Semantic Proximity: LLMs use semantic matching to select tools. A consistent pattern allows the model to first "filter" by the object (Task) and then "select" the action (Create). This two-step mental model significantly reduces tool selection errors.
     - Pattern Recognition: Consistent naming reduces "cognitive load" for the model. When tools follow a predictable schema, the LLM is less likely to hallucinate parameters or call non-existent functions.
     - Improved Tokenization: Clear delimiters (like dots or underscores) help the model's tokenizer represent each component of the name accurately, leading to better internal representation of the tool's intent.

5. **Registry vs If-Else:** Why use a dictionary registry to route tool calls instead of if-else chains? What happens when you need to add the 20th tool?
   - Choosing a Dictionary Registry over If-Else chains is essential for building a maintainable and professional-grade dispatcher.
     1. The Problem with If-Else Chains
     - Complexity Blowup: As you add your 20th tool, the if-else block becomes a "God Function" that is hard to read, hard to test, and prone to merge conflicts.
     - Violation of OCP: It violates the Open-Closed Principle (software entities should be open for extension, but closed for modification). You shouldn't have to touch your core routing logic every time you add a feature.
     2. Benefits of the Registry Pattern
     - Decoupling: The dispatcher doesn't need to know how a tool works; it only needs to look up the function associated with the tool name. This allows you to organize handlers into separate files/modules.
     - Constant Time Lookup: A dictionary lookup is O(1), ensuring the routing speed remains lightning-fast regardless of how many tools you support, whereas if-else is O(N).
     - Clean Code & Scalability: Adding the 20th or 100th tool is as simple as adding a single entry to a configuration map. It makes the codebase much cleaner and allows for advanced techniques like Dynamic Loading or Decorators to handle registration automatically.

## Verification

Your prototype is a real MCP server. Test it with the MCP inspector — no Claude needed.

### 1. Start the server (sanity check)

```bash
python -m app.mcp_server
```

The process should hang waiting on stdin (it's a stdio MCP server — that's correct). Ctrl+C to stop. If you see an `ImportError` or other crash, fix that first.

### 2. Run the MCP inspector

Requires Node.js (uses `npx`).

```bash
npx @modelcontextprotocol/inspector python -m app.mcp_server
```

This opens a browser GUI (usually `http://localhost:5173`).

Steps in the GUI:

1. Click **Connect** -> should show 4 tools: `task.create`, `task.list`, `task.status`, `task.cancel`
2. **task.create** -> fill `description="Summarize tech news"`, `scheduled_at="2025-01-01T00:00:00"` (past time so watcher picks it up immediately) -> **Run Tool** -> response should include `{"job_id": 1, "status": "pending", ...}`
3. Wait ~10 seconds, then **task.status** -> `job_id: 1` -> status should now be `"completed"`
4. **task.create** with future time `"2099-12-31T00:00:00"` -> get `job_id: 2`
5. **task.cancel** -> `job_id: 2` -> status `"cancelled"`
6. **task.list** -> see all your jobs

### 3. (Optional) Connect to Claude Desktop / Claude Code

Once the inspector tests pass, the server is ready. To talk to it through Claude:

**Claude Desktop**: edit `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) and add (use absolute paths):

```json
{
  "mcpServers": {
    "task-scheduler": {
      "command": "/absolute/path/to/scaffold/.venv/bin/python",
      "args": ["-m", "app.mcp_server"],
      "cwd": "/absolute/path/to/scaffold"
    }
  }
}
```

Restart Claude Desktop fully. The 🔨 icon in the chat input should show 4 tools.

**Claude Code**: edit `~/.claude.json` (top-level `mcpServers` for user scope) with the same block, or run `claude mcp add` from inside `scaffold/`.

Then chat:

> "Schedule a task to review PR #123 tomorrow at 9am."
> -> Claude calls `task.create` -> returns job_id
> "What's the status of that task?"
> -> Claude calls `task.status`

## Suggested Tech Stack

Python + the official `mcp` SDK is recommended (already in `requirements.txt` for the Guided Track). Challenge Track may use any language with an MCP SDK.
