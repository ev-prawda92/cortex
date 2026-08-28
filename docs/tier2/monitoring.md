# Monitoring & Alerting

Now that you have agents running in Cortex, you need visibility into their health and quick alerts when things break. This guide covers setting up observability, SLOs, and alerting.

## The Observability Stack

Cortex automatically tracks:

- **Metrics:** Error rate, latency (p50/p90/p95/p99), token usage, success rate
- **Traces:** Step-by-step execution log for every run (what the agent did, tool calls, errors)
- **Logs:** Structured events (agent started, error occurred, escalated)
- **Health Score:** Composite 0-100 score based on recent performance

All of this flows into the Monitor and Analytics tabs. No setup required—just deploy an agent and it starts collecting.

## The Health Score Explained

The health score combines:

| Factor | Weight | What it measures |
|--------|--------|------------------|
| **Success Rate** | 30% | % of runs that completed without error |
| **Latency (p95)** | 20% | 95th percentile response time |
| **Error Trend** | 15% | Is error rate going up or down? |
| **Uptime** | 15% | How often is the agent available? |
| **Throughput** | 10% | How many runs per hour? Stability matters. |
| **SLO Compliance** | 10% | Are you meeting your SLO targets? |

**Interpretation:**
- **90-100:** Healthy. No action needed.
- **70-89:** Degraded. Monitor closely. Consider rollback if trending down.
- **<70:** Unhealthy. Investigate immediately.

The score updates after every run, so it's real-time.

## Setting Up Alerts

Cortex supports alerting on thresholds and anomalies.

### Alert 1: High Error Rate

Alert when error rate exceeds a threshold.

**In the dashboard:**
1. Go to **More > Observability**
2. Click **Alerts**
3. **New Alert**
4. Select agent
5. Condition: `error_rate_24h > 0.1` (10% error rate)
6. Severity: **Critical**
7. Notify: **Email** (or Slack if integrated)
8. Save

Now, if your agent errors more than 10% of runs in the last 24 hours, you'll get an alert.

**Via API:**
```bash
curl -X POST http://localhost:8000/api/alerts \
  -H "Content-Type: application/json" \
  -d '{
    "agent_id": "invoice-processor",
    "name": "High Error Rate",
    "condition": "error_rate_24h > 0.1",
    "severity": "critical",
    "channels": ["email"]
  }'
```

### Alert 2: High Latency

Alert when runs are slow.

```bash
curl -X POST http://localhost:8000/api/alerts \
  -H "Content-Type: application/json" \
  -d '{
    "agent_id": "customer-chat",
    "name": "Slow Response",
    "condition": "latency_p95 > 30",
    "severity": "warning",
    "channels": ["email", "slack"]
  }'
```

Triggers if 95th percentile latency exceeds 30 seconds.

### Alert 3: Health Score Degrading

Alert when health drops below a threshold.

```bash
curl -X POST http://localhost:8000/api/alerts \
  -H "Content-Type: application/json" \
  -d '{
    "agent_id": "email-categorizer",
    "name": "Agent Unhealthy",
    "condition": "health_score < 70",
    "severity": "critical",
    "channels": ["email"]
  }'
```

### Alert 4: Anomaly Detection

Alert when metrics deviate from baseline (no fixed threshold).

```bash
curl -X POST http://localhost:8000/api/alerts \
  -H "Content-Type: application/json" \
  -d '{
    "agent_id": "data-processor",
    "name": "Unusual Error Spike",
    "condition": "error_rate_1h > baseline * 2",
    "severity": "warning",
    "channels": ["slack"]
  }'
```

Triggers if error rate in the last hour doubles vs. the baseline.

### Notification Channels

**Email:**
```bash
# Already set up in Cortex. Alerts go to your email address.
```

**Slack:**
```bash
# First, integrate Slack
# 1. Go to More > Integrations
# 2. Click Slack
# 3. Follow OAuth flow
# 4. Grant permission to post to a channel

# Then, set alert channel
curl -X POST http://localhost:8000/api/alerts \
  -d '{
    ...
    "channels": ["slack"],
    "slack_channel": "#alerts"
  }'
```

**Webhook (custom):**
```bash
curl -X POST http://localhost:8000/api/alerts \
  -d '{
    ...
    "channels": ["webhook"],
    "webhook_url": "https://your-service.example.com/alerts"
  }'
```

Cortex will POST to your webhook when an alert fires:
```json
{
  "alert_name": "High Error Rate",
  "agent_id": "invoice-processor",
  "severity": "critical",
  "message": "Error rate exceeded 10% threshold (current: 15%)",
  "triggered_at": "2026-08-27T16:00:00Z"
}
```

## Setting Up SLOs

An SLO (Service Level Objective) is a target for agent performance. Cortex tracks whether you're meeting it.

### Example SLO: "99% of runs complete in <30 seconds"

```bash
curl -X POST http://localhost:8000/api/agents/{agent_id}/slos \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Response Time SLO",
    "target": 0.99,
    "window_minutes": 1440,
    "metric": "latency_p95",
    "threshold": 30
  }'
```

This says: "In any 24-hour window, 99% of runs should complete in ≤30 seconds."

### SLO Burn Rate Alerts

Once you set an SLO, Cortex calculates your "burn rate"—how fast you're using up your error budget.

If your SLO is 99% uptime, you have a 1% error budget. Over 30 days, that's ~7 hours of downtime budget.

Alert when burn rate is too fast:
```bash
curl -X POST http://localhost:8000/api/alerts \
  -d '{
    "agent_id": "critical-agent",
    "name": "SLO Burn Rate Critical",
    "condition": "slo_burn_rate_30m > 10",
    "severity": "critical",
    "channels": ["email", "slack"]
  }'
```

This triggers if you're burning through 10x your budget rate (will violate SLO in 3 hours if sustained).

## Viewing Observability Data

### Real-Time Health (Monitor Tab)

1. Go to **Monitor** tab
2. Look at health score for each agent
3. Click an agent to see recent runs
4. Click a run to see the full trace

### Trends (Analytics Tab)

1. Go to **Analytics** tab
2. Charts show:
   - Error rate over time
   - Latency percentiles
   - Success rate
   - Token usage
3. Filters let you slice by agent, time range, provider

### Debugging a Failed Run

1. Go to **Monitor** or **Runs** tab
2. Click the failed run
3. Read the **Trace**—it shows:
   - Agent's standing instruction
   - LLM prompt that was sent
   - Tool calls made
   - LLM response
   - Any errors or exceptions

Example trace:
```
[Agent: customer-chat]
[Instruction] "Respond to customer inquiries from the queue"
[Input] "Customer asked: Is your product available for international shipping?"
[Tool Call] fetch_product_info(region="international")
[Tool Result] "Shipping to 45 countries"
[LLM Response] "Yes, we ship to 45 countries including..."
[Status] COMPLETED (2.3s, 340 tokens)
```

If there was an error:
```
[Tool Call] fetch_customer_data(id=12345)
[Tool Error] Database timeout after 5 seconds
[LLM Response] "I'm having trouble looking up your account..."
[Status] ESCALATED (7.1s, 210 tokens)
```

Read the trace to understand what went wrong and why.

## Common Observability Scenarios

### Scenario 1: Agent Errors Spike (Alert Fires)

1. You get an alert: "High Error Rate"
2. Go to **Monitor**, click the agent
3. Look at recent runs—they're failing
4. Click a failed run, read the trace
5. Root cause: "Database connection timeout"
6. **Fix:** Increase timeout in config → new version created automatically
7. Deploy new version to 10% of clients (if multi-tenant) → watch error rate drop
8. Roll out to 100%

### Scenario 2: Latency Creeping Up

1. You notice in **Analytics** tab: p95 latency was 5s, now 15s
2. No error spike, but agent is slow
3. Likely cause: model is taking longer, or database queries are slower
4. Check **More > Usage** tab—token counts are higher?
5. If yes, simplify standing instruction (fewer words, clearer asks)
6. If no, database might be slow (check RDS metrics)

### Scenario 3: Health Score Dropped Overnight

1. **Monitor** tab shows health 92 → 65
2. Click agent, check recent runs
3. Find the turn where it broke
4. Read the trace
5. If a tool call started failing: fix the integration
6. If the agent is giving wrong answers: update standing instruction
7. If a model change: rollback to previous version (see [Versioning & Rollout](./versioning.md))

### Scenario 4: SLO Burning Too Fast

1. Alert: "SLO Burn Rate Critical"
2. Go to **More > Observability**, click the SLO
3. See current burn rate vs. safe burn rate
4. If you have 1 month left of error budget at current burn, act now
5. Options:
   - Increase SLO threshold (make it less strict)
   - Rollback to previous version
   - Scale up (more resources for faster runs)
   - Pause agent, fix the root cause, resume

## Best Practices

1. **Set SLOs based on your users' expectations**
   - Chat agents: <5 second response
   - Batch processing: <1 minute per batch
   - Scheduled data: <30 minutes to complete

2. **Alert early, before SLO breaks**
   - Set alert threshold at 2x your SLO target
   - For 99% SLO, alert at 1% error rate

3. **Use anomaly alerts for unexpected spikes**
   - Fixed thresholds miss gradual degradation
   - Anomaly detection catches sudden changes

4. **Check your alerts regularly**
   - If alert fires and you ignore it, disable it
   - Spam alerts are worse than no alerts

5. **Correlate with external events**
   - Agent latency spike? Check if database had maintenance
   - Error spike? Check if third-party API went down
   - Cost spike? Check if token counts increased (longer responses?)

## Next Steps

- **Deploy new versions safely:** [Versioning & Rollout](./versioning.md)
- **Operate multiple agents across clients:** [Multi-Tenant Setup](./multi-tenant.md)
- **Learn how health scores are calculated:** [Architecture & Concepts](../tier3/architecture.md)
