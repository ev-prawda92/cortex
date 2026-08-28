# Architecture & Concepts

How Cortex works under the hood. This is reference material—understand these concepts to troubleshoot issues and design better agents.

## Core Components

### API Server
- FastAPI application
- Handles all HTTP requests
- Validates input, enforces auth
- Returns JSON responses

### Daemon
- Background process that runs continuously
- Wakes up every 10 seconds
- Checks which agents are due to run
- Executes agents, saves results
- Adjusts next run time based on errors

### Database
- PostgreSQL (production) or SQLite (dev)
- Stores agents, runs, versions, team data
- All data is immutable (versions, runs)
- Soft-delete (agents marked as deleted, recoverable for 30 days)

### LLM Providers
- Anthropic, OpenAI, custom
- API clients that call the LLM
- Cortex manages API keys and routing
- Supports swapping models at runtime

### Observability Engine
- Tracks metrics (error rate, latency, tokens)
- Generates health score
- Stores traces and logs
- Powers alerting

## Agent Lifecycle

### 1. Creation

User creates agent:
```bash
curl -X POST /api/agents -d '{...config...}'
```

Flow:
1. API validates config
2. Database inserts Agent row (status: "stopped")
3. Version 1 snapshot created
4. Returns agent_id

### 2. Starting

User clicks "Start":
```bash
curl -X POST /api/agents/{id}/start
```

Flow:
1. API updates agent status to "running"
2. Daemon picks it up on next tick
3. Daemon initializes AgentRunState (tracks next_run_at, cycle_count, etc)
4. Daemon executes first cycle immediately (next_run_at = 0)

### 3. Execution (Daemon Loop)

Daemon tick (every 10 seconds):
1. Query database: get all agents with status="running"
2. For each agent:
   a. Get its AgentRunState
   b. Check: is now >= next_run_at?
   c. If yes, execute cycle

Execute cycle:
1. Build standing instruction + cycle context
2. Call LLM with instruction + tools
3. Process tool calls (call tool implementations)
4. Get final response
5. Save run record to database
6. Update health score
7. Calculate next_run_at (with exponential backoff if errors)

### 4. Monitoring

Run record is saved immediately:
- Observability engine updates metrics
- Health score is recalculated
- Alerts are checked
- WebSocket event broadcast to connected dashboards

Users see the run in **Monitor** tab within 1 second.

### 5. Versioning

User edits agent config:
```bash
curl -X PATCH /api/agents/{id} -d '{...new_config...}'
```

Flow:
1. API compares new config vs current
2. If different, creates new Version row
3. Version is hash-chained (includes hash of previous version)
4. Diff is calculated and stored
5. Current agent points to new version
6. Rollback is possible (any old version can be restored)

### 6. Deletion

User clicks "Delete":
```bash
curl -X DELETE /api/agents/{id}
```

Flow:
1. Agent is soft-deleted (is_deleted = true, deleted_at = now())
2. Agent disappears from dashboard (filtered out)
3. Agent moves to **Recycle Bin**
4. For 30 days: can be restored
5. After 30 days: permanently purged (task runs nightly)

## Health Score Calculation

Health score is 0-100, computed after every run.

Formula:
```
score = 0.30 * success_rate 
      + 0.20 * latency_score
      + 0.15 * error_trend_score
      + 0.15 * uptime_score
      + 0.10 * throughput_score
      + 0.10 * slo_compliance_score
```

Each component is 0-100:

### Success Rate (30%)
```
success_rate = (successful_runs / total_runs) * 100
```
- Recent 100 runs
- COMPLETED = success
- ERROR or ESCALATED = failure

### Latency Score (20%)
```
p95_latency = 95th percentile of run durations (recent 100 runs)
latency_score = max(0, 100 - (p95_latency / 10) * 10)
```
- If p95 is 1s: 100
- If p95 is 5s: 50
- If p95 is >10s: 0

### Error Trend Score (15%)
```
error_trend = (errors_last_1h - errors_last_24h) / errors_last_24h
error_trend_score = max(0, 100 * (1 - error_trend))
```
- If errors are steady: 100
- If errors increased 50%: 50
- If errors doubled: 0

### Uptime Score (15%)
```
uptime_score = (successful_executions / total_executions) * 100
```
- Similar to success rate, but counts execution attempts
- Accounts for agent being paused/stopped

### Throughput Score (10%)
```
runs_per_hour = (recent 100 runs) / (time span in hours)
expected_throughput = (3600 / run_interval_seconds)
throughput_score = min(100, (runs_per_hour / expected_throughput) * 100)
```
- If agent is running as expected: 100
- If it's slower: <100

### SLO Compliance Score (10%)
```
if SLO exists:
    slo_score = (passing_runs / total_runs) * 100
else:
    slo_score = 100  # No SLO = can't fail
```

## Version Snapshots

Versions are immutable records of agent config at a point in time.

**Structure:**
```
Version {
  version: int (1, 2, 3, ...)
  agent_id: str
  config: JSON (standing_instruction, model, tools, etc)
  created_at: timestamp
  created_by: user email
  change_summary: str ("Updated prompt", etc)
  previous_version: int (for hash chain)
  config_hash: str (SHA256 of config)
  prev_config_hash: str (for chain validation)
}
```

**Why immutable?**
- Enables rollback (previous version is unchanged)
- Enables audit (see exactly what was running when)
- Enables diffs (compare v1 vs v2)
- Enables analysis (why did error rate change at v3?)

**Hash Chain:**
- v1: config_hash = hash(config_1)
- v2: prev_config_hash = hash(config_1), config_hash = hash(config_2)
- v3: prev_config_hash = hash(config_2), config_hash = hash(config_3)

If someone tries to tamper with v2, the hash chain breaks, and you'd know.

## Soft Delete & Recycle Bin

**Why soft delete?**
- Users sometimes delete agents by mistake
- 30-day recovery window prevents data loss
- Compliance: some regulations require audit trails

**How it works:**
```
Agent table:
- id: str
- name: str
- is_deleted: bool (default False)
- deleted_at: timestamp (null if not deleted)
- deleted_by: str (user who deleted it)
- purge_after: timestamp (30 days from deletion)
```

**Soft delete:**
```sql
UPDATE agents SET is_deleted = true, deleted_at = NOW(), 
  deleted_by = 'user@company.com', 
  purge_after = NOW() + INTERVAL '30 days'
WHERE id = 'agent_abc123'
```

**View in Recycle Bin:**
```sql
SELECT * FROM agents WHERE is_deleted = true
```

**Restore:**
```sql
UPDATE agents SET is_deleted = false, deleted_at = null, 
  deleted_by = null, purge_after = null
WHERE id = 'agent_abc123'
```

**Automatic purge (runs nightly):**
```sql
DELETE FROM agents WHERE is_deleted = true AND purge_after < NOW()
```

## Observability Stack

### Metrics
- DDSketch histograms for latency percentiles (p50, p90, p95, p99)
- Counters for errors, escalations, completions
- Gauges for health score, throughput

**Example:**
```
latency_histogram = DDSketch()
latency_histogram.add(2.3)  # 2.3 second run
latency_histogram.add(1.8)
latency_histogram.add(15.2)
latency_histogram.percentile(0.95)  # Returns ~15.2 (95th percentile)
```

### Traces
- Step-by-step execution log
- Stored as JSON array
- One entry per major step (instruction, tool call, response)

**Example:**
```json
[
  {"type": "instruction", "text": "Process invoices..."},
  {"type": "tool_call", "name": "fetch_invoices", "input": {...}},
  {"type": "tool_result", "name": "fetch_invoices", "result": "3 invoices"},
  {"type": "completion", "text": "Processed 3 invoices"}
]
```

### Logs
- Structured JSON logs
- One log per significant event
- Indexed by agent_id, timestamp, level (info, error, warn)

**Example:**
```json
{"timestamp": "2026-08-27T16:00:00Z", "agent_id": "agent_abc123", 
 "level": "error", "message": "Tool call failed", 
 "tool_name": "fetch_invoices", "error": "S3 timeout"}
```

### Health Score
- Computed after every run
- Cached in Agent table
- Updated from 6 dimensions (success, latency, trend, uptime, throughput, SLO)
- Used for alerting and dashboard

## Rollout Strategies

### Canary
1. Deploy to N% of instances
2. Monitor for M minutes
3. If healthy (error rate < threshold), promote to 100%
4. If degraded, rollback canary automatically

**Metrics watched:**
- Error rate
- Latency p95
- Health score

**Decision logic:**
```
if (error_rate_during_canary > baseline_error_rate + 0.02) {
  rollback_canary()
  alert("Canary health degraded")
} else if (duration_minutes > canary_duration) {
  promote_to_100_percent()
}
```

### Gradual
1. Deploy to 10%, wait 15 min
2. Deploy to 30%, wait 15 min
3. Deploy to 70%, wait 30 min
4. Deploy to 100%

Each step checks health. If any step fails, rollback.

**Advantage:** Smaller increments = easier to catch issues early

**Disadvantage:** Takes longer (1 hour instead of 30 minutes)

### Blue-Green
1. Keep current version (v1) running 100%
2. Spin up new version (v2) running 0%
3. Both versions run in parallel
4. Compare outputs (if possible)
5. If v2 looks good, switch all traffic to v2
6. v1 is still available if you need to rollback

**Advantage:** Zero downtime, instant rollback

**Disadvantage:** 2x resource usage temporarily

## Multi-Tenancy

Cortex supports multiple workspaces. Each workspace:
- Has its own agents
- Has its own team members
- Has its own data (no cross-workspace queries)
- Has its own audit logs

**Isolation:**
```sql
-- Get agents for workspace A
SELECT * FROM agents WHERE workspace_id = 'ws_a'

-- Get agents for workspace B
SELECT * FROM agents WHERE workspace_id = 'ws_b'

-- No workspace filter = no results (safety default)
```

**Cost tracking per workspace:**
```sql
SELECT workspace_id, SUM(total_tokens) as tokens, 
       SUM(total_cost_usd) as cost
FROM runs
GROUP BY workspace_id
```

## Exponential Backoff

When an agent errors, Cortex automatically backs off (waits longer before retrying).

**Algorithm:**
```
consecutive_errors = 0
base_interval = 60 (seconds)

for each run:
  if run.status == ERROR:
    consecutive_errors += 1
  else:
    consecutive_errors = 0
  
  backoff = min(base_interval * (2 ^ min(consecutive_errors, 4)), 3600)
  # min(..., 4) caps exponential growth at 2^4 = 16
  # min(..., 3600) caps at 1 hour
  
  next_run_at = now + backoff
```

**Example:**
- Run 1 errors: wait 60s (2^0 * 60)
- Run 2 errors: wait 120s (2^1 * 60)
- Run 3 errors: wait 240s (2^2 * 60)
- Run 4 errors: wait 480s (2^3 * 60)
- Run 5 errors: wait 960s (2^4 * 60, capped at 4)
- Run 6 errors: wait 960s again
- Run 7 succeeds: reset, wait 60s next time

**Why?** If your agent is erroring, hammering it repeatedly won't help. Back off exponentially. Once it succeeds, resume normal cadence.

## Database Schema

**agents table:**
```
id, name, description, status (running/stopped/paused), 
workspace_id, version (current), config, 
is_deleted, deleted_at, deleted_by, purge_after,
created_at, created_by, last_modified_at, last_modified_by
```

**agent_versions table:**
```
id, agent_id, version (1, 2, 3...), config, created_at, created_by, 
change_summary, diff, config_hash, prev_config_hash
```

**runs table:**
```
id, agent_id, status (COMPLETED/ERROR/ESCALATED), 
started_at, finished_at, duration_seconds,
input_tokens, output_tokens, total_tokens,
provider (anthropic/openai), model, task_type,
claim (what the agent was asked),
detail (summary, reason, route_to), trace, error,
config_version
```

**workspaces table:**
```
id, name, description, created_at, created_by
```

**workspace_members table:**
```
id, workspace_id, user_id, role (owner/admin/operator/viewer), joined_at
```

## Performance Tuning

### Connection Pooling
```
pool_size = 20 (default)
max_overflow = 10
```
- 20 simultaneous database connections
- Can spike to 30 if needed
- If > 30 concurrent, connections wait

For 1000 agents, 20 is usually fine. If you hit limits, increase to 50.

### API Response Time
- Most endpoints: <100ms (database query)
- List agents (1000 agents): <500ms
- Create agent: <50ms

If slow, check database performance (CPU, disk I/O).

### Daemon Efficiency
- Daemon checks every 10 seconds
- If 1000 agents running: queries database once, checks all 1000
- Query time: <50ms (indexed on status and next_run_at)
- Each agent execution: varies (1-30s depending on agent)

## Scaling Cortex

**Single instance (local dev):**
- 1-10 agents
- Works fine

**Production (AWS RDS + 2 servers):**
- 100-500 agents
- 2 API servers (load balanced)
- PostgreSQL RDS multi-AZ
- Each agent: 1-5 runs/minute

**Large scale (1000+ agents):**
- 4-8 API servers
- PostgreSQL RDS multi-AZ, larger instance (db.r5.xlarge+)
- Consider read replicas if lots of dashboards querying
- Consider Elasticsearch for observability (if built-in isn't enough)
- Consider Kafka/message queue for event streaming (if many webhooks)

**Rough cost estimates:**
- 100 agents, 10k runs/day: ~$0.10/day (tokens) + $100/month (infrastructure)
- 1000 agents, 100k runs/day: ~$1.00/day (tokens) + $500/month (infrastructure)
