# Troubleshooting & FAQ

Common issues and how to fix them.

## Agent Issues

### Agent Won't Start

**Symptoms:** You click "Start" but the agent never runs.

**Checklist:**
1. Is the agent actually created? List agents: `curl http://localhost:8000/api/agents`
2. Check status: `curl http://localhost:8000/api/agents/{id}`
3. Check daemon is running: `curl http://localhost:8000/api/health`

**Fixes:**
- Restart the daemon: Stop Cortex, restart with `docker compose up`
- Check logs: `docker logs cortex-api`
- Verify database is connected

### Agent Runs But Never Completes

**Symptoms:** Run status is stuck (not COMPLETED or ERROR).

**Root causes:**
- Agent is waiting for a tool that hangs
- LLM API is timing out
- Database is slow

**Debug:**
1. Go to **Runs** tab, click the stuck run
2. Look at the trace—where is it stuck?
3. Example: Tool call to external API, no response back
4. Fix: Increase timeout, or contact the external API provider

**To force-stop a run:**
```bash
curl -X POST http://localhost:8000/api/agents/{id}/runs/{run_id}/stop
```

### Agent Produces Wrong Output

**Symptoms:** Agent is running but giving bad answers or wrong behavior.

**Debug:**
1. Go to **Runs** tab, find a bad run
2. Click it, read the trace
3. Look at the "LLM Response" section
4. Is the model hallucinating? Being too creative? Misunderstanding?

**Fixes:**
1. **Improve the standing instruction**
   - Be more specific about what to do
   - Add constraints ("Be concise, under 50 words")
   - Add examples of good vs bad outputs

2. **Switch model**
   - If Claude 3.5 Sonnet is making mistakes, try Claude 3 Opus (more capable)
   - Cost will increase, but accuracy might improve

3. **Reduce scope**
   - If agent is supposed to do 3 things, maybe it's too complex
   - Split into 2 agents

**Example fix:**
```bash
# Old instruction (too vague)
"Process the data"

# New instruction (more specific)
"Process transaction data. Extract: vendor name, date, amount. Validate that amounts are positive. Format output as JSON."

# Even better
"Process transaction data. Rules:
1. Extract vendor (max 50 chars), date (YYYY-MM-DD), amount (positive number)
2. If vendor is unknown, flag as 'UNKNOWN_VENDOR'
3. If amount is negative, flag as 'INVALID_AMOUNT'
4. Return JSON: [{vendor, date, amount, flags}]"
```

## Performance Issues

### Agent Runs Are Slow

**Symptoms:** Response time is >10 seconds consistently.

**Root causes:**
- Model is taking a long time
- Tool calls to external APIs are slow
- Database queries are slow

**Debug:**
1. Go to **Analytics** tab, look at latency trend
2. Is it consistent or does it spike at certain times?
3. Click a run, read the trace—where is the time spent?

**Fixes:**
1. **Optimize the prompt**
   - Shorter standing instruction = faster
   - Remove unnecessary context
   - Use few-shot examples if needed (adds tokens but sometimes faster)

2. **Switch to a faster model**
   - Sonnet → Haiku (faster, less capable)
   - Cost goes down too
   - But quality might drop

3. **Cache responses**
   - If the agent is answering the same questions repeatedly, cache responses
   - Check "did we see this before?" before calling the LLM

4. **Parallelize tools**
   - If the agent calls 3 tools sequentially, make them parallel
   - Cortex's Agent Mesh can do this

### Costs Are Too High

**Symptoms:** Monthly bill is higher than expected.

**Debug:**
1. Go to **Usage** tab
2. See costs per agent
3. Which agent is most expensive?
4. Is it using a lot of tokens?

**Fixes:**
1. **Switch to a cheaper model**
   - Sonnet → Haiku (1/3 the cost)
   - But might lose capability

2. **Reduce token usage**
   - Shorten standing instruction
   - Remove unnecessary context
   - Reduce output length ("be concise")

3. **Cache common queries**
   - If you ask the same question 100 times, cache the answer

4. **Batch your work**
   - Instead of 100 on-demand runs, batch process once

Example: Invoice processor running 1000 invoices/day.
- Cost per run: 0.001 (50 tokens input, 30 tokens output)
- Daily cost: $1.00
- Monthly: $30

After optimization:
- Shorter prompt, switch to Haiku: 0.0003 per run
- Daily cost: $0.30
- Monthly: $9
- Savings: 70%

## Error Issues

### Agent Erroring on Specific Inputs

**Symptoms:** Most runs are fine, but some specific inputs cause errors.

**Debug:**
1. Go to **Runs** tab, filter by ERROR
2. Find the failing run
3. Read the trace and error message
4. Pattern: What do all failing runs have in common?

Example: Invoice processor errors on invoices >$100k
- All $100k+ invoices fail
- Smaller invoices succeed
- Root cause: Prompt assumes invoices under $100k

**Fix:**
```bash
# Update standing instruction to handle large amounts
# Add: "Handle any amount, even if >$100k. These are valid."

curl -X PATCH http://localhost:8000/api/agents/invoice-processor \
  -d '{
    "config": {
      "standing_instruction": "Process invoices... Handle any amount. Even invoices >$100k are valid. Flag if amount seems unusual but don'\''t reject it."
    }
  }'
```

### High Error Rate Overnight

**Symptoms:** Error rate jumps from 2% to 50% overnight.

**Root causes:**
- Third-party API went down (payment processor, Slack, etc)
- Database connectivity issue
- Deployment happened at midnight (new version broke things)

**Debug:**
1. Check timestamp of the spike
2. Check if you deployed something around that time
3. Check third-party status pages
4. Read error messages in runs

**Fixes:**
1. **If it's a deployment:** Rollback
   ```bash
   curl -X POST http://localhost:8000/api/agents/agent-id/rollback \
     -d '{"target_version": "previous-version"}'
   ```

2. **If it's a third-party issue:** Wait for them to fix it, or use a fallback
   ```bash
   # Update standing instruction to handle the down service
   "If Slack API is down, log locally instead of posting"
   ```

3. **If it's a database issue:** Check RDS/database health
   - CloudWatch metrics
   - Connection pool exhausted?
   - Disk full?

## Deployment Issues

### Can't Connect to Database

**Error:** `Error: could not connect to database`

**Checklist:**
1. Is database running? `docker ps | grep postgres`
2. Is DATABASE_URL correct? `echo $DATABASE_URL`
3. Can you ping the database host? `ping db-host`
4. Security group allows inbound? (for cloud databases)

**Fixes:**
- Local dev: `docker compose down && docker compose up --build`
- Cloud RDS: Check security group, add your IP
- Verify password is correct

### Database Schema Mismatch

**Error:** `column agents.is_deleted does not exist`

**Root cause:** Database schema is old, but code expects new columns.

**Fix:** Run migrations
```bash
python migrate.py
```

Or (if local): Drop and recreate
```bash
docker compose down -v  # Remove volume
docker compose up --build  # Recreate fresh
```

### Docker Image Won't Build

**Error:** `failed to build docker image`

**Debug:**
```bash
docker build -t cortex . -v  # Verbose output
```

Look at the error. Common issues:
- Missing dependencies (pip install failed)
- Syntax errors in Python
- Port already in use

**Fixes:**
- Fix the Python error
- Rebuild: `docker build -t cortex . --no-cache`

## Monitoring Issues

### Alert Never Fires

**Symptoms:** Condition should have triggered but no alert.

**Debug:**
1. Go to **Agents > Alerts**, click the alert
2. Check condition: `error_rate_24h > 0.1`
3. Are you actually hitting that condition?

**Fixes:**
1. **Check the metric**
   - Go to **Analytics** tab
   - Is your actual error rate above the threshold?
   - Maybe it's not triggering because you're under the threshold (good news!)

2. **Check the channels**
   - Email: Check spam folder
   - Slack: Did you grant Cortex permission to post?
   - Webhook: Is your endpoint reachable?

3. **Test the alert**
   ```bash
   curl -X POST http://localhost:8000/api/agents/{id}/alerts/{alert_id}/test
   ```
   Sends a test alert to all channels.

### Too Many Alerts (Alert Fatigue)

**Symptoms:** You're getting alerts constantly, can't keep up.

**Fixes:**
1. **Increase the threshold**
   ```bash
   # Instead of error_rate > 0.05 (5%)
   # Use error_rate > 0.10 (10%)
   # Fewer false positives
   ```

2. **Change condition to anomaly**
   ```bash
   # Instead of fixed threshold
   "error_rate_1h > baseline * 2"
   # Triggers only if error rate doubles
   # More signal, less noise
   ```

3. **Disable non-critical alerts**
   - Keep only critical alerts enabled
   - Archive others for reference

## Authorization Issues

### "Unauthorized" or "Forbidden" Errors

**Error:** `403 Forbidden` or `401 Unauthorized`

**Causes:**
1. API key missing or invalid
2. Don't have permission in this workspace
3. API key is for a different workspace

**Fixes:**
1. Check API key: `curl -H "Authorization: Bearer KEY" http://localhost:8000/api/health`
   - If 401, key is invalid
   - If 200, key is valid

2. Check workspace permissions: `curl http://localhost:8000/api/workspaces -H "Authorization: Bearer KEY"`
   - You should see your workspaces in the response

3. Generate a new API key: Go to **Runtime > API Keys** in dashboard

## Common Questions

### How do I test an agent before deploying?

Create a staging workspace:
1. Create workspace: "staging"
2. Create agent there with v1 config
3. Run it manually: `curl -X POST /api/agents/{id}/run`
4. Check the output
5. Refine, save (v2)
6. When confident, deploy to production

### How do I know if my SLO is realistic?

1. Deploy agent, run it for a week
2. Go to **Analytics** tab
3. Look at actual latency (p95)
4. If p95 is 5 seconds, set SLO target to 10 seconds (2x buffer)
5. If you're consistently beating the SLO, tighten it

### Can I have different agents for different clients?

Yes. Create workspaces:
- Workspace A: invoice-processor-v1 for Client A
- Workspace B: invoice-processor-v2 for Client B
- Workspace C: custom-agent for Client C

Each client only sees their agents.

### How do I avoid vendor lock-in?

Cortex stores config as JSON. You can export:
```bash
curl http://localhost:8000/api/agents/{id} > agent-export.json
```

Takes your agent config with you. To re-import to another system, you'd need to adapt the config format.

### How often should I upgrade agents?

Cadence depends on your risk tolerance:
- **Conservative:** New version only if there's a critical bug
- **Standard:** New version monthly, test canary first
- **Aggressive:** New version weekly, high confidence in canary testing

Most teams: monthly, canary always.

### What's the max number of agents in one workspace?

Cortex scales to 1000+ agents per workspace. But operationally:
- <50 agents: One pane of glass is fine
- 50-200 agents: Start grouping by function/team
- 200+: Use multiple workspaces, one per team/service

### How do I migrate from the old system to Cortex?

1. Create agents in Cortex (same logic as old system)
2. Deploy canary (10% of traffic)
3. Monitor for 1 week
4. If clean, promote to 100%
5. Keep old system as fallback for 30 days
6. Decommission old system

### What if my agent needs real-time interactions?

Cortex supports webhooks. For real-time:
1. External system POSTs data to `POST /api/agents/{id}/run`
2. Cortex executes immediately
3. Returns run_id
4. Caller polls for result or subscribes to webhook callback

Response time: typically <1 second to kick off, 1-5 seconds to complete.

## Still Stuck?

1. Check **Runtime > Logs** in Cortex dashboard (server logs)
2. Check the agent's run trace (click a run, read the trace)
3. Check [Architecture & Concepts](./architecture.md) to understand how things work
4. Reach out to support with:
   - Agent config
   - Recent run trace
   - Error message
   - What you've already tried
