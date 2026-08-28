# Dashboard Overview

The Cortex dashboard is your command center for operating agents. Here's what each tab does.

## Main Tabs

### Monitor Tab
**Purpose:** See agent health at a glance. Spot problems early.

**What you see:**
- List of all agents with their current health scores (0-100)
- Error count (total errors in last 24h)
- Recent runs (last 10)
- Status indicator (Running, Stopped, Paused)

**What you do here:**
- Click an agent to see detailed run history
- Click a run to debug what went wrong
- Spot trends (error rate climbing, latency increasing)
- Decide if you need to rollback or restart

**Key metric:** Health Score
- 90-100: Healthy, no action needed
- 70-89: Degraded, monitor closely
- <70: Unhealthy, investigate or rollback

### Agents Tab
**Purpose:** Manage your agent portfolio.

**What you see:**
- Full list of agents (running, stopped, paused)
- Configuration for each
- Workspace they belong to
- Last modified time

**What you do here:**
- Create new agent
- Start/stop agents
- Edit agent config (standing instruction, model, tools)
- Delete agents (goes to Recycle Bin)
- View version history

### Runs Tab
**Purpose:** Deep-dive into execution history.

**What you see:**
- Timeline of all agent runs
- Filter by agent, date range, outcome (COMPLETED, ERROR, ESCALATED)
- Details of each run (duration, tokens, trace)

**What you do here:**
- Search for errors by agent or time
- Understand why a run failed (read the trace)
- Calculate actual vs. budgeted costs (token count)
- Audit who triggered what when

### Analytics Tab
**Purpose:** Trends and insights over time.

**What you see:**
- Charts: error rate trend, latency (p50/p90/p95), tokens per run
- Success rate by agent
- Cost breakdown by agent/model/provider
- Comparison (agent A vs. agent B)

**What you do here:**
- Spot degradation over time
- Identify expensive agents
- Justify SLA targets
- Plan capacity (if running 50 agents, how many will still be under SLA?)

### Runtime Tab
**Purpose:** Configuration and system settings.

**What you see:**
- Database connection status
- API key management (providers)
- Workspace settings
- Database migration status

**What you do here:**
- Add API keys for new providers
- Configure notification channels (email, Slack)
- Set global defaults (retry policy, timeouts)
- View system health

### Settings Tab
**Purpose:** Workspace and team management.

**What you see:**
- Current workspace name
- Team members and their roles
- Pending invitations
- Audit log

**What you do here:**
- Invite new team members
- Change roles (owner, admin, operator, viewer)
- Set workspace name/description
- Review who changed what when

### More Menu
Additional features hidden by default.

**Observability:** Advanced metrics and SLO configuration
- Define custom health scoring rules
- Set up anomaly detection
- View traces and distributed tracing

**Usage:** Cost and budget tracking
- Per-agent costs
- Per-user spending
- Budget alerts
- Chargeback by client (if multi-tenant)

**Teams:** Multi-workspace and team management
- Create additional workspaces
- Invite teams to specific workspaces
- Cross-workspace reports

**Plugins:** Extend agent behavior
- Enable/disable built-in plugins (logger, alerts, cost-guard, data-redact, retry-smart, eval)
- Configure plugin behavior
- View plugin logs

**Agent Mesh:** Connect multiple agents
- Build DAG workflows (agent A → agent B → agent C)
- Define conditional routing
- Fan out to parallel agents

**Recovery:** Soft-delete and version management
- View Recycle Bin
- Restore deleted agents
- Rollback to previous versions
- View version diffs

## Quick Navigation Examples

**Scenario: "An agent is erroring"**
1. Go to **Monitor** tab
2. Find the agent with low health score
3. Click it to see recent runs
4. Click the failed run
5. Read the trace to understand why
6. Go to **Agents** tab → **Version History** → **Rollback** if needed

**Scenario: "Agent is too expensive"**
1. Go to **Analytics** tab
2. Look at "Cost by Agent" chart
3. Filter by the expensive agent
4. Go to **Runs** tab, filter by agent
5. See which runs used most tokens
6. Optimize the standing instruction

**Scenario: "Need to deploy a new agent version"**
1. Go to **Agents** tab
2. Find the agent
3. Edit config (standing instruction, model, etc)
4. Save → new version created automatically
5. Go to **More → Agent Mesh** (or use canary deployment feature if available)
6. Test on 10% of clients first
7. Roll out to the rest

**Scenario: "Need to give a team member access"**
1. Go to **Settings** tab
2. Click "Invite Team Member"
3. Enter email, select role (admin, operator, or viewer)
4. Send invite
5. They'll receive email, accept, and get access to the workspace

## Key Takeaways

| Tab | Go here when... |
|-----|-----------------|
| **Monitor** | You want to see overall health and spot problems |
| **Agents** | You want to create, edit, or manage agents |
| **Runs** | You want to debug a specific execution or audit history |
| **Analytics** | You want to understand trends and costs |
| **Runtime** | You need to configure providers or system settings |
| **Settings** | You want to manage your team or workspace |
| **More > Observability** | You want to set up custom alerts or SLOs |
| **More > Usage** | You want to track costs per agent or set budgets |
| **More > Recovery** | You want to restore a deleted agent or rollback |

See [Glossary](../GLOSSARY.md) for definitions of terms like "Health Score," "SLO," "Escalation," etc.
