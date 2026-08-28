# Versioning & Rollout

As you improve your agents, you'll want to deploy new versions without breaking everything. This guide covers how Cortex handles versioning, rollout strategies, and rollback.

## How Versioning Works

Every time you change an agent's config, Cortex creates a new **version snapshot**:

1. **Change something** — Edit standing instruction, model, tools, anything in config
2. **Save** — New version created automatically with a hash of the config
3. **Immutable** — That version can never change; you can only create new versions
4. **Rollback capable** — If v2 breaks, go back to v1 instantly

Each version includes:
- Config (standing instruction, model, tools, all settings)
- Timestamp (when created)
- Changed by (which team member)
- Change summary (what changed)
- Field-level diffs (exactly which fields changed)
- Hash chain (each version links to the previous one)

### Example: Changing an Agent

**Current version (v5):**
```json
{
  "standing_instruction": "Process invoices and categorize them by vendor"
}
```

You edit it:
```json
{
  "standing_instruction": "Process invoices. Categorize by vendor. Flag duplicates."
}
```

When you save, Cortex creates **v6**:
- Stores the new config
- Logs the diff: `standing_instruction: "Process invoices..." → "Process invoices. Categorize by vendor. Flag duplicates."`
- Records: "Changed by: you, Time: 2026-08-27 16:00:00"
- v5 stays accessible for rollback

## Viewing Version History

### Via Dashboard

1. Go to **Agents** tab
2. Click an agent
3. Click **Version History**
4. See all versions with timestamps, who changed it, and diffs

### Via API

```bash
# List versions for an agent
curl http://localhost:8000/api/agents/{agent_id}/versions

# Response:
[
  {
    "version": 6,
    "created_at": "2026-08-27T16:00:00Z",
    "created_by": "you@company.com",
    "config": { ... },
    "change_summary": "Updated standing instruction to flag duplicates",
    "previous_version": 5,
    "diff": {
      "standing_instruction": {
        "old": "Process invoices and categorize them by vendor",
        "new": "Process invoices. Categorize by vendor. Flag duplicates."
      }
    }
  },
  {
    "version": 5,
    "created_at": "2026-08-26T10:30:00Z",
    ...
  }
]

# Get a specific version
curl http://localhost:8000/api/agents/{agent_id}/versions/5
```

## Rollout Strategies

When you deploy a new version, you have choices about how fast to roll it out.

### Strategy 1: All-at-Once (Risky, Fast)

Deploy v6 to 100% of instances immediately. Use for:
- Non-critical agents
- Confident in the change
- Urgent fix

**How:**
```bash
curl -X POST http://localhost:8000/api/agents/{agent_id}/rollout \
  -H "Content-Type: application/json" \
  -d '{
    "target_version": 6,
    "strategy": "all-at-once"
  }'
```

Cortex immediately updates all running instances to v6.

**Risk:** If v6 is broken, all your agents are broken until you rollback.

### Strategy 2: Canary (Safe, Slower)

Deploy v6 to 10% of instances first. Monitor for errors. If all good, roll to 100%.

**How:**
```bash
curl -X POST http://localhost:8000/api/agents/{agent_id}/rollout \
  -H "Content-Type: application/json" \
  -d '{
    "target_version": 6,
    "strategy": "canary",
    "canary_percentage": 10,
    "canary_duration_minutes": 30
  }'
```

Cortex deploys v6 to 10% of instances. Monitors for 30 minutes:
- If error rate stays normal → automatically promote to 100%
- If error rate spikes → automatically rollback canary to v5, alert you

**Best for:** Production agents, uncertain changes.

### Strategy 3: Blue-Green (Zero Downtime)

Run both v5 and v6 simultaneously, switch traffic to v6 only when ready.

**How:**
```bash
curl -X POST http://localhost:8000/api/agents/{agent_id}/rollout \
  -H "Content-Type: application/json" \
  -d '{
    "target_version": 6,
    "strategy": "blue-green",
    "validation_duration_minutes": 10
  }'
```

For 10 minutes:
- v5 handles 100% of traffic
- v6 runs in parallel, handling 0% traffic
- Cortex compares outputs (if possible)
- If v6 outputs look good → switch traffic to v6
- Else rollback to v5

**Use for:** Critical agents where both output and latency matter.

### Strategy 4: Gradual Rollout (Slow, Safest)

Roll out v6 gradually: 10% → 30% → 70% → 100%, waiting between steps.

**How:**
```bash
curl -X POST http://localhost:8000/api/agents/{agent_id}/rollout \
  -H "Content-Type: application/json" \
  -d '{
    "target_version": 6,
    "strategy": "gradual",
    "steps": [
      {"percentage": 10, "wait_minutes": 15},
      {"percentage": 30, "wait_minutes": 15},
      {"percentage": 70, "wait_minutes": 30},
      {"percentage": 100, "wait_minutes": 0}
    ]
  }'
```

After each step, Cortex checks error rate. If it spikes, stops and alerts you.

**Best for:** Large deployments (100+ instances), high-stakes agents.

## Comparing Versions

Before rolling out, compare v5 and v6 side-by-side.

### Via Dashboard

1. Go to **Agents > Version History**
2. Select v5 and v6
3. Click **Compare**
4. See all field differences highlighted

### Via API

```bash
curl http://localhost:8000/api/agents/{agent_id}/versions/compare?v1=5&v2=6

# Response:
{
  "v1": 5,
  "v2": 6,
  "differences": {
    "standing_instruction": {
      "changed": true,
      "old": "Process invoices and categorize by vendor",
      "new": "Process invoices. Categorize by vendor. Flag duplicates."
    },
    "model": {
      "changed": false,
      "value": "claude-3-5-sonnet-20241022"
    },
    "run_interval_seconds": {
      "changed": false,
      "value": 300
    }
  }
}
```

## Rolling Back

If v6 is broken, roll back to v5 instantly.

### Via Dashboard

1. Go to **Agents > Version History**
2. Click v5
3. Click **Rollback to This Version**
4. Confirm

Cortex creates v7 (which is identical to v5) and deploys it to all instances.

### Via API

```bash
curl -X POST http://localhost:8000/api/agents/{agent_id}/rollback \
  -H "Content-Type: application/json" \
  -d '{
    "target_version": 5
  }'

# Creates v7 (copy of v5) and deploys
# All instances immediately switch to v7
```

### Rollback is Instant

No gradual rollout. If v6 is on fire, you get back to v5 in seconds.

**Downside:** All instances see the rollback immediately. Small moment of inconsistency.

**Upside:** Emergency fix is available now.

## Multi-Tenant Rollout

If you're operating the same agent for 50 clients, you can roll out per-client.

**Scenario:** You want to test v6 with Client A before rolling to the other 49.

```bash
curl -X POST http://localhost:8000/api/agents/{agent_id}/rollout \
  -H "Content-Type: application/json" \
  -d '{
    "target_version": 6,
    "strategy": "canary",
    "canary_percentage": 100,
    "canary_workspace_id": "client-a-workspace",
    "canary_duration_minutes": 60
  }'
```

v6 deploys to Client A's workspace only. Run it for an hour. Monitor for errors. If clean, roll out to Client B, then C, etc.

This way, if v6 breaks for Client A, Clients B-Z are still on v5.

## Testing Before Rollout

Before rolling out to production, test in a staging environment.

**Two approaches:**

### Approach 1: Staging Workspace
```bash
# Create a staging agent (same config as production)
# Update its config to v6
# Run it for a day
# If good, deploy production version to v6
```

### Approach 2: Webhook-Based A/B Test
```bash
# Create two agents: agent-a (v5) and agent-b (v6)
# Route 50% of incoming requests to each
# Compare outputs for 24 hours
# If agent-b is better, promote to production
```

## Version Retention & Cleanup

Cortex keeps all versions forever (they're immutable, tiny, and useful for audits).

If you want to archive old versions (disk space, GDPR):
```bash
curl -X DELETE http://localhost:8000/api/agents/{agent_id}/versions?older_than_days=365
```

This deletes versions older than 1 year. You can still see them in audit logs, but the config is gone.

## Common Rollout Scenarios

### Scenario 1: Fixing a Bug

Agent is erroring. You know the fix.

**Strategy:** Canary (10%, 30 min)

1. Edit standing instruction or fix config
2. Save → v6 created
3. Deploy v6 as canary
4. Monitor for 30 min
5. If error rate drops, auto-promoted to 100%
6. Done

### Scenario 2: Experimenting with a New Model

Want to try Claude 3 Opus instead of Sonnet (more capable, more expensive).

**Strategy:** Gradual + Compare

1. Create v6 with model: claude-3-opus
2. Compare v5 vs v6 in dashboard
3. Deploy v6 gradual: 10% → 30% → 70% → 100%
4. Track cost increase in Analytics tab
5. If too expensive, rollback to v5
6. If good quality, keep it

### Scenario 3: Rolling Out to Dozens of Clients

You manage 50 client instances of the same agent. You want to test before rolling to all.

**Strategy:** Canary + Multi-Tenant

1. Edit agent config
2. Save → v6 created
3. Deploy v6 as canary to Client A's workspace (100% for 1 hour)
4. Client A's ops team tests it
5. If good, deploy v6 gradual to Clients B-Z (10% each, then 30%, then 100%)
6. Done

### Scenario 4: Critical Production Issue

Agent is broken now. Every second matters.

**Strategy:** Immediate rollback

1. Go to Version History
2. Find the last known-good version (v3)
3. Click **Rollback**
4. All instances switch to v3 immediately
5. Issue is fixed
6. Later, investigate what went wrong in v4, v5, v6

## Versioning Best Practices

1. **Change one thing at a time**
   - If you change standing instruction AND model AND tools, you won't know which broke things
   - Edit one → save → test → edit another

2. **Use meaningful change summaries**
   - "Fixed grammar" ✓
   - "Ugh" ✗
   - Helps you understand your version history later

3. **Use canary rollout by default**
   - Even for small changes
   - 10% for 30 minutes costs nothing, saves headaches

4. **Test in staging first**
   - If you have a staging environment, test there before production
   - Or use webhook-based A/B testing

5. **Monitor the first hour after rollout**
   - Set up alerts to page you if error rate spikes
   - Usually issues surface within the first 100 runs

6. **Keep at least 3 versions back**
   - In case you need to rollback multiple times
   - Cortex keeps all versions by default

## Next Steps

- **Set up monitoring to catch issues:** [Monitoring & Alerting](./monitoring.md)
- **Learn multi-tenant rollout strategies:** [Multi-Tenant Setup](./multi-tenant.md)
- **Understand version snapshots under the hood:** [Architecture & Concepts](../tier3/architecture.md)
