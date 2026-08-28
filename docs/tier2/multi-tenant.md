# Multi-Tenant Setup

If you're operating agents for multiple clients (or teams), this guide explains how to isolate them, manage access, and track costs per client in Cortex.

## What is Multi-Tenancy?

Multi-tenancy means one Cortex instance serves multiple isolated teams or clients.

**Example:** You're a consultancy with 3 agents deployed for 50 clients.

- **Monolithic approach:** 150 agents in one Cortex workspace. Hard to manage, no isolation.
- **Multi-tenant approach:** 3 agents in each of 50 workspaces, fully isolated.

In Cortex, **workspaces** are the isolation boundary. Each workspace has:
- Its own agents
- Its own team members
- Its own access controls
- Its own audit logs
- Separate monitoring and cost tracking

Clients (or internal teams) don't see each other's data.

## Creating Workspaces

### Via Dashboard

1. Go to **More > Teams**
2. Click **Create Workspace**
3. Enter workspace name: "Client A - Acme Corp"
4. Description (optional): "Invoice processing for Acme"
5. Save

The workspace is created. You're now the owner.

### Via API

```bash
curl -X POST http://localhost:8000/api/workspaces \
  -H "Content-Type: application/json" \
  -d '{
    "name": "client-a",
    "description": "Acme Corp workspace"
  }'

# Response:
{
  "workspace_id": "ws_abc123",
  "name": "client-a",
  "created_at": "2026-08-27T16:00:00Z"
}
```

## Inviting Team Members

Once you have a workspace, invite team members to manage it.

### Via Dashboard

1. Go to **Settings** tab
2. Click **Invite Team Member**
3. Enter email: john@company.com
4. Select role:
   - **Owner:** Full control (create/delete agents, invite members, delete workspace)
   - **Admin:** Manage agents and team (can't delete workspace)
   - **Operator:** Run/stop agents, view monitoring (can't edit config)
   - **Viewer:** Read-only access to monitoring
5. Send

John gets an email. He clicks the link and joins the workspace.

### Via API

```bash
curl -X POST http://localhost:8000/api/workspaces/{workspace_id}/invitations \
  -H "Content-Type: application/json" \
  -d '{
    "email": "john@company.com",
    "role": "operator"
  }'
```

### Role Permissions

| Action | Owner | Admin | Operator | Viewer |
|--------|-------|-------|----------|--------|
| Create agent | ✓ | ✓ | ✗ | ✗ |
| Edit agent config | ✓ | ✓ | ✗ | ✗ |
| Start/stop agent | ✓ | ✓ | ✓ | ✗ |
| View runs & monitoring | ✓ | ✓ | ✓ | ✓ |
| Invite team member | ✓ | ✓ | ✗ | ✗ |
| Delete agent | ✓ | ✓ | ✗ | ✗ |
| Delete workspace | ✓ | ✗ | ✗ | ✗ |

**Typical setup:**
- **Owner:** Your VP of Delivery (overall accountability)
- **Admin:** Project lead for each client (manages agents)
- **Operator:** Day-to-day ops engineer (monitors, stops/starts)
- **Viewer:** Client liaison (sees status, can't touch anything)

## Multi-Tenant Workflow Example

**Scenario:** You have 3 agents (invoice processor, email categorizer, data aggregator) deployed for 50 clients.

### Setup Phase

1. Create 50 workspaces (one per client)
   ```bash
   for i in {1..50}; do
     curl -X POST http://localhost:8000/api/workspaces \
       -d "{\"name\":\"client-$i\"}"
   done
   ```

2. For each workspace, create the 3 agents
   ```bash
   # In each workspace:
   # - Create invoice-processor agent
   # - Create email-categorizer agent
   # - Create data-aggregator agent
   ```

3. Invite client's ops team to their workspace
   ```bash
   # Client A team gets access to client-a workspace
   # Client B team gets access to client-b workspace
   ```

### Deployment Phase

1. You deploy a new version of invoice-processor (v2)
2. Test in a staging workspace
3. Roll out gradual: 10% of clients first
   ```bash
   curl -X POST http://localhost:8000/api/agents/{agent_id}/rollout \
     -H "Content-Type: application/json" \
     -d '{
       "target_version": 2,
       "strategy": "gradual",
       "client_subset": "10%"
     }'
   ```
4. If good, promote to 100% of clients

### Monitoring Phase

Each workspace sees only their agents:

- **Client A's ops team:** Logs into their workspace, sees only their 3 agents
- **Client B's ops team:** Logs into their workspace, sees only their 3 agents
- **You (the vendor):** Log into your vendor workspace, see all 50 clients' instances + health

### Cost Tracking Phase

Track costs per client:

1. Go to **More > Usage** in your vendor workspace
2. See cost breakdown by workspace (client)
3. Chargeback to clients
   ```bash
   curl http://localhost:8000/api/usage/by-workspace
   
   # Response:
   [
     {"workspace": "client-a", "cost": 523.45, "agents": 3, "tokens": 1234567},
     {"workspace": "client-b", "cost": 287.12, "agents": 3, "tokens": 891234},
     ...
   ]
   ```

## Isolating Agents

Agents in one workspace can't see agents in another.

**How it works:**

- When an agent runs, it logs data to its workspace
- The workspace's audit log only shows events in that workspace
- Team members in workspace A can't list agents in workspace B
- API calls from workspace A are filtered to only return workspace A's data

**Result:** Perfect isolation. Client A's ops team sees only their agents, their runs, their costs.

## Workspace-Level Settings

Each workspace can have custom settings.

### Budget & Alerts

Set a monthly budget per workspace:

```bash
curl -X POST http://localhost:8000/api/workspaces/{workspace_id}/budget \
  -H "Content-Type: application/json" \
  -d '{
    "monthly_budget_usd": 500,
    "alert_threshold_percent": 80
  }'
```

When Client A's usage hits 80% of their $500 budget, you and their ops team get alerted.

### Audit Retention

Set how long to keep audit logs:

```bash
curl -X POST http://localhost:8000/api/workspaces/{workspace_id}/settings \
  -H "Content-Type: application/json" \
  -d '{
    "audit_retention_days": 90,
    "compliance_level": "sox"
  }'
```

For SOX compliance, keep logs for 90 days.

### Custom Integrations

Each workspace can integrate with different Slack teams, notification channels, etc.

```bash
# In workspace A:
curl -X POST http://localhost:8000/api/workspaces/{workspace_a_id}/integrations/slack \
  -d '{
    "oauth_token": "xoxb-client-a-token",
    "alert_channel": "#client-a-alerts"
  }'

# In workspace B (different Slack workspace):
curl -X POST http://localhost:8000/api/workspaces/{workspace_b_id}/integrations/slack \
  -d '{
    "oauth_token": "xoxb-client-b-token",
    "alert_channel": "#client-b-alerts"
  }'
```

When an alert fires in workspace A, it posts to Client A's Slack. When one fires in workspace B, it posts to Client B's Slack.

## Cross-Workspace Visibility

As the vendor, you need to see all clients in one place.

### Vendor Dashboard

1. Create a **vendor workspace** (for your internal team)
2. In that workspace, you get a **Fleet View**
3. Fleet View shows:
   - All agents across all client workspaces
   - Health scores per client
   - Cost breakdown by client
   - Incident dashboard (agents erroring across clients)

```bash
# Get fleet-wide health
curl http://localhost:8000/api/fleet/health

# Response:
{
  "total_workspaces": 50,
  "total_agents": 150,
  "agents_healthy": 142,
  "agents_degraded": 7,
  "agents_unhealthy": 1,
  "error_rate_24h": 0.003,
  "cost_24h": 1243.56
}
```

### Unified Monitoring

Monitor issues across all clients:

```bash
# Get all errors across all workspaces (last 1 hour)
curl http://localhost:8000/api/fleet/errors?minutes=60

# Response:
[
  {"agent": "invoice-processor-client-a", "error": "Database timeout", "count": 5},
  {"agent": "invoice-processor-client-b", "error": "API rate limit", "count": 12},
  {"agent": "invoice-processor-client-c", "error": "Null reference", "count": 2}
]
```

If invoice-processor is erroring for multiple clients, you see it immediately.

## Common Multi-Tenant Scenarios

### Scenario 1: Onboarding a New Client

1. Create workspace
2. Create the 3 standard agents in that workspace
3. Configure per-client settings (budget, integrations)
4. Deploy production versions
5. Invite client's ops team
6. Done

Entire process: 30 minutes.

### Scenario 2: Rolling Out a New Agent Version

You have 50 clients, each running agent v2. You want to test v3 with 5 clients before rolling to all 50.

```bash
# Deploy v3 canary to 5 random workspaces
curl -X POST http://localhost:8000/api/agents/{agent_id}/rollout \
  -H "Content-Type: application/json" \
  -d '{
    "target_version": 3,
    "strategy": "canary",
    "canary_workspace_count": 5,
    "canary_duration_minutes": 120
  }'
```

Cortex picks 5 random workspaces, deploys v3 to them, monitors for 2 hours. If error rate stays normal, promotes to all 50. If it spikes in any of the 5, rolls back.

### Scenario 3: Incident Response Across Clients

Invoice-processor suddenly errors for all 50 clients.

1. You see the alert: "invoice-processor health score critical"
2. Go to **Fleet View**
3. Click incident
4. See all 50 workspaces are affected
5. Go to one workspace, read the error trace
6. Root cause: Third-party vendor's API changed
7. Fix standing instruction or tool definition
8. Save → v3 created
9. Deploy v3 canary to 2 workspaces
10. Looks good
11. Deploy v3 to all 50 workspaces (gradual, 20% at a time)
12. All clients back to normal

Total time to fix: <15 minutes.

### Scenario 4: A Client Wants to Customize Their Agent

Client A wants their invoice processor to flag certain vendors as "VIP". Others don't need this.

**Solution:** Fork the agent

1. In Client A's workspace, create a new agent: invoice-processor-v2-client-a
2. Update standing instruction: "Flag invoices from: [list of VIP vendors]"
3. Deploy it to Client A only
4. Other clients still run the standard invoice-processor

Now Client A has a custom version, but managing 51 agent instances instead of 150.

## Best Practices for Multi-Tenancy

1. **One workspace per client (or team)**
   - Simplifies access control
   - Clear cost tracking
   - Easy to say "this client's data"

2. **Use operator role for client ops teams**
   - They can monitor and restart agents
   - They can't delete or reconfigure
   - Gives them autonomy without risk

3. **Track costs per workspace**
   - Chargeback to clients
   - Spot inefficient agents (re-train, re-prompt)
   - Budget alerts prevent surprises

4. **Use a vendor workspace for your internal team**
   - Fleet view to spot issues across clients
   - Cost reporting by client
   - Incident dashboard

5. **Test new versions with canary on a subset**
   - 5-10 random clients first
   - 100% rollout only if all good
   - Avoids breaking all clients at once

6. **Document your SLOs per agent**
   - "Invoice processor: 99% success, <30s response"
   - Each client knows what to expect
   - Cortex alerts you if you breach them

## Next Steps

- **Set up monitoring per workspace:** [Monitoring & Alerting](./monitoring.md)
- **Deploy new versions safely:** [Versioning & Rollout](./versioning.md)
- **Learn the API:** [API Reference](../tier3/api-reference.md)
