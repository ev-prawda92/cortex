# API Reference

Complete reference for Cortex's REST API. All endpoints require authentication via API key.

## Authentication

Include your API key in the `Authorization` header:

```bash
curl http://localhost:8000/api/agents \
  -H "Authorization: Bearer YOUR_API_KEY"
```

Get your API key from **Runtime > API Keys** in the dashboard, or:

```bash
curl -X POST http://localhost:8000/api/keys \
  -H "Content-Type: application/json" \
  -d '{"name": "my-integration"}'
```

## Base URL

```
http://localhost:8000  (local development)
https://cortex.company.com  (production)
```

## Agents API

### List Agents

```
GET /api/agents
```

**Query params:**
- `workspace_id` (optional): Filter by workspace
- `status` (optional): "running", "stopped", "paused"
- `limit` (optional, default 25): Number of results
- `offset` (optional, default 0): Pagination offset

**Response:**
```json
{
  "agents": [
    {
      "id": "agent_abc123",
      "name": "invoice-processor",
      "description": "Processes invoices from Slack",
      "status": "running",
      "health_score": 85,
      "error_count_24h": 2,
      "workspace_id": "ws_xyz",
      "version": 3,
      "created_at": "2026-08-20T10:00:00Z",
      "last_run_at": "2026-08-27T15:30:00Z"
    }
  ],
  "total": 15,
  "limit": 25,
  "offset": 0
}
```

### Get Agent

```
GET /api/agents/{agent_id}
```

**Response:**
```json
{
  "id": "agent_abc123",
  "name": "invoice-processor",
  "description": "Processes invoices from Slack",
  "status": "running",
  "health_score": 85,
  "error_count_24h": 2,
  "workspace_id": "ws_xyz",
  "version": 3,
  "config": {
    "model": {
      "provider": "anthropic",
      "model_name": "claude-3-5-sonnet-20241022"
    },
    "standing_instruction": "Process invoices from Slack...",
    "run_interval_seconds": 300,
    "task_type": "data-processing",
    "tools": [
      {
        "name": "fetch_slack_messages",
        "description": "Get messages from a Slack channel",
        "parameters": ["channel_id", "limit"]
      }
    ]
  },
  "created_at": "2026-08-20T10:00:00Z",
  "created_by": "you@company.com",
  "last_modified_at": "2026-08-27T10:00:00Z",
  "last_modified_by": "you@company.com"
}
```

### Create Agent

```
POST /api/agents
```

**Request body:**
```json
{
  "name": "new-agent",
  "description": "What it does",
  "config": {
    "model": {
      "provider": "anthropic",
      "model_name": "claude-3-5-sonnet-20241022"
    },
    "standing_instruction": "Your agent's directive",
    "run_interval_seconds": 60,
    "task_type": "general",
    "tools": []
  }
}
```

**Response:**
```json
{
  "id": "agent_abc123",
  "name": "new-agent",
  ...
}
```

### Update Agent

```
PATCH /api/agents/{agent_id}
```

**Request body:** (same as create, but only include fields to change)
```json
{
  "standing_instruction": "Updated directive"
}
```

Creates a new version automatically. Returns the agent with updated config and new version number.

### Delete Agent

```
DELETE /api/agents/{agent_id}
```

Soft-deletes the agent (moves to Recycle Bin). Returns 204 No Content.

### Start Agent

```
POST /api/agents/{agent_id}/start
```

Starts continuous execution. Agent will run on its configured interval. Returns 200 OK.

### Stop Agent

```
POST /api/agents/{agent_id}/stop
```

Stops continuous execution. No new cycles will start. Returns 200 OK.

### Pause Agent

```
POST /api/agents/{agent_id}/pause
```

Pauses execution without stopping. Cycles pause, but agent is still "running" in the system. Returns 200 OK.

### Resume Agent

```
POST /api/agents/{agent_id}/resume
```

Resumes from pause. Next cycle starts immediately. Returns 200 OK.

### Trigger Run

```
POST /api/agents/{agent_id}/run
```

Manually trigger one execution cycle, regardless of schedule.

**Request body (optional):**
```json
{
  "context": "any additional context for this run"
}
```

**Response:**
```json
{
  "run_id": "run_xyz789",
  "agent_id": "agent_abc123",
  "started_at": "2026-08-27T16:00:00Z"
}
```

## Runs API

### List Runs

```
GET /api/agents/{agent_id}/runs
```

**Query params:**
- `limit` (optional, default 25): Number of results
- `offset` (optional, default 0): Pagination offset
- `status` (optional): "COMPLETED", "ERROR", "ESCALATED"
- `start_time` (optional): ISO timestamp, include runs after this
- `end_time` (optional): ISO timestamp, include runs before this

**Response:**
```json
{
  "runs": [
    {
      "id": "run_xyz789",
      "agent_id": "agent_abc123",
      "status": "COMPLETED",
      "duration_seconds": 3.2,
      "input_tokens": 240,
      "output_tokens": 85,
      "total_tokens": 325,
      "started_at": "2026-08-27T16:00:00Z",
      "finished_at": "2026-08-27T16:00:03Z",
      "claim": "[continuous cycle #15] Process invoices from Slack",
      "detail": {
        "summary": "Processed 3 invoices, found 1 duplicate",
        "reason": null,
        "route_to": null,
        "citations": []
      }
    }
  ],
  "total": 342,
  "limit": 25,
  "offset": 0
}
```

### Get Run

```
GET /api/agents/{agent_id}/runs/{run_id}
```

**Response:**
```json
{
  "id": "run_xyz789",
  "agent_id": "agent_abc123",
  "status": "COMPLETED",
  "duration_seconds": 3.2,
  "input_tokens": 240,
  "output_tokens": 85,
  "total_tokens": 325,
  "started_at": "2026-08-27T16:00:00Z",
  "finished_at": "2026-08-27T16:00:03Z",
  "claim": "[continuous cycle #15] Process invoices from Slack",
  "provider": "anthropic",
  "model": "claude-3-5-sonnet-20241022",
  "task_type": "data-processing",
  "config_version": 3,
  "trace": [
    {
      "type": "instruction",
      "text": "Process invoices from Slack..."
    },
    {
      "type": "tool_call",
      "name": "fetch_slack_messages",
      "input": {"channel_id": "C123ABC", "limit": 10},
      "result": "[3 new messages with PDF attachments]"
    },
    {
      "type": "completion",
      "text": "Processed 3 invoices. Found 1 duplicate (duplicate of run_xyz788). 2 new invoices created."
    }
  ],
  "detail": {
    "summary": "Processed 3 invoices, found 1 duplicate",
    "reason": null,
    "route_to": null,
    "citations": []
  },
  "error": null
}
```

## Versions API

### List Versions

```
GET /api/agents/{agent_id}/versions
```

**Response:**
```json
{
  "versions": [
    {
      "version": 3,
      "created_at": "2026-08-27T10:00:00Z",
      "created_by": "you@company.com",
      "change_summary": "Updated standing instruction",
      "config": { ... },
      "diff": {
        "standing_instruction": {
          "old": "Old instruction",
          "new": "New instruction"
        }
      }
    },
    {
      "version": 2,
      ...
    }
  ]
}
```

### Get Version

```
GET /api/agents/{agent_id}/versions/{version}
```

Returns the config for that specific version.

### Compare Versions

```
GET /api/agents/{agent_id}/versions/compare?v1=2&v2=3
```

**Response:**
```json
{
  "v1": 2,
  "v2": 3,
  "differences": {
    "standing_instruction": {
      "changed": true,
      "old": "Old instruction",
      "new": "New instruction"
    },
    "model": {
      "changed": false,
      "value": "claude-3-5-sonnet-20241022"
    }
  }
}
```

### Rollback

```
POST /api/agents/{agent_id}/rollback
```

**Request body:**
```json
{
  "target_version": 2
}
```

Creates a new version (N+1) that is identical to version 2, and deploys it to all instances. Returns the new version.

### Rollout

```
POST /api/agents/{agent_id}/rollout
```

**Request body:**
```json
{
  "target_version": 3,
  "strategy": "canary",
  "canary_percentage": 10,
  "canary_duration_minutes": 30
}
```

**Strategy options:**
- `all-at-once`: Deploy to 100% immediately
- `canary`: Deploy to N%, monitor, auto-promote if healthy
- `gradual`: Deploy in steps (10% → 30% → 70% → 100%)
- `blue-green`: Run both versions, compare, switch

**Response:**
```json
{
  "rollout_id": "rollout_abc123",
  "agent_id": "agent_abc123",
  "target_version": 3,
  "strategy": "canary",
  "status": "in_progress",
  "started_at": "2026-08-27T16:00:00Z",
  "progress": {
    "current_percentage": 10,
    "healthy": true,
    "error_rate": 0.002
  }
}
```

## Workspace API

### List Workspaces

```
GET /api/workspaces
```

**Response:**
```json
{
  "workspaces": [
    {
      "id": "ws_abc123",
      "name": "client-a",
      "description": "Acme Corp",
      "created_at": "2026-08-20T10:00:00Z",
      "member_count": 3,
      "agent_count": 3
    }
  ],
  "total": 50
}
```

### Create Workspace

```
POST /api/workspaces
```

**Request body:**
```json
{
  "name": "client-new",
  "description": "New client workspace"
}
```

**Response:**
```json
{
  "id": "ws_xyz789",
  "name": "client-new",
  "description": "New client workspace",
  "created_at": "2026-08-27T16:00:00Z"
}
```

### Invite Member

```
POST /api/workspaces/{workspace_id}/invitations
```

**Request body:**
```json
{
  "email": "john@company.com",
  "role": "operator"
}
```

**Roles:** owner, admin, operator, viewer

**Response:**
```json
{
  "invitation_id": "inv_abc123",
  "email": "john@company.com",
  "role": "operator",
  "created_at": "2026-08-27T16:00:00Z",
  "expires_at": "2026-09-03T16:00:00Z"
}
```

### List Members

```
GET /api/workspaces/{workspace_id}/members
```

**Response:**
```json
{
  "members": [
    {
      "id": "user_abc123",
      "email": "you@company.com",
      "role": "owner",
      "joined_at": "2026-08-20T10:00:00Z"
    }
  ],
  "total": 3
}
```

## Alerts API

### List Alerts

```
GET /api/agents/{agent_id}/alerts
```

**Response:**
```json
{
  "alerts": [
    {
      "id": "alert_abc123",
      "agent_id": "agent_abc123",
      "name": "High Error Rate",
      "condition": "error_rate_24h > 0.1",
      "severity": "critical",
      "channels": ["email", "slack"],
      "enabled": true,
      "created_at": "2026-08-27T16:00:00Z"
    }
  ]
}
```

### Create Alert

```
POST /api/agents/{agent_id}/alerts
```

**Request body:**
```json
{
  "name": "High Error Rate",
  "condition": "error_rate_24h > 0.1",
  "severity": "critical",
  "channels": ["email", "slack"]
}
```

**Conditions:**
- `error_rate_24h`: Error rate in last 24 hours (0-1)
- `error_rate_1h`: Error rate in last 1 hour
- `latency_p95`: 95th percentile latency in seconds
- `health_score`: Current health score (0-100)
- `slo_burn_rate_30m`: Burn rate in last 30 minutes

### Update Alert

```
PATCH /api/agents/{agent_id}/alerts/{alert_id}
```

Update any field (name, condition, channels, enabled).

### Delete Alert

```
DELETE /api/agents/{agent_id}/alerts/{alert_id}
```

Removes the alert. Returns 204 No Content.

## Usage API

### Get Usage

```
GET /api/usage
```

**Query params:**
- `start_date` (optional): YYYY-MM-DD
- `end_date` (optional): YYYY-MM-DD
- `workspace_id` (optional): Filter by workspace

**Response:**
```json
{
  "period": {
    "start_date": "2026-08-27",
    "end_date": "2026-08-27"
  },
  "total_tokens": 1234567,
  "total_cost_usd": 12.34,
  "by_agent": [
    {
      "agent_id": "agent_abc123",
      "name": "invoice-processor",
      "tokens": 567890,
      "cost_usd": 5.68,
      "runs": 342
    }
  ],
  "by_provider": [
    {
      "provider": "anthropic",
      "tokens": 1000000,
      "cost_usd": 10.00
    },
    {
      "provider": "openai",
      "tokens": 234567,
      "cost_usd": 2.34
    }
  ]
}
```

### Get Usage by Workspace

```
GET /api/usage/by-workspace
```

Returns usage per workspace (useful for multi-tenant chargeback).

**Response:**
```json
{
  "by_workspace": [
    {
      "workspace_id": "ws_abc123",
      "name": "client-a",
      "tokens": 567890,
      "cost_usd": 5.68,
      "agents": 3
    }
  ]
}
```

## Health API

### Health Check

```
GET /api/health
```

**Response:**
```json
{
  "status": "ok",
  "database": "connected",
  "uptime_seconds": 3600,
  "agents_running": 42,
  "agents_total": 150
}
```

Returns 200 if healthy, 503 if database or critical services are down.

## Error Responses

All errors follow this format:

```json
{
  "error": "error-code",
  "message": "Human-readable error message",
  "status": 400
}
```

**Common error codes:**
- `agent-not-found`: Agent doesn't exist
- `unauthorized`: API key invalid or missing
- `forbidden`: Don't have permission for this workspace
- `invalid-request`: Request validation failed
- `conflict`: Resource already exists (e.g., agent name taken)
- `rate-limit`: Too many requests
- `internal-error`: Server error

## Rate Limiting

- **Development:** No limits
- **Production:** 100 requests per minute per API key
- **Headers returned:**
  - `X-RateLimit-Limit`: 100
  - `X-RateLimit-Remaining`: 87
  - `X-RateLimit-Reset`: Unix timestamp of reset time

If you exceed the limit, you get a 429 response.

## Pagination

List endpoints support pagination:

```bash
# Get first 25
curl http://localhost:8000/api/agents?limit=25&offset=0

# Get next 25
curl http://localhost:8000/api/agents?limit=25&offset=25
```

## Webhooks

Configure webhooks to receive events:

```
POST /api/webhooks
```

**Request body:**
```json
{
  "url": "https://your-service.example.com/cortex-events",
  "events": ["run.completed", "run.error", "alert.triggered"],
  "active": true
}
```

**Events:**
- `agent.created`
- `agent.updated`
- `agent.deleted`
- `run.started`
- `run.completed`
- `run.error`
- `run.escalated`
- `alert.triggered`
- `alert.resolved`

Cortex will POST events to your URL:
```json
{
  "event": "run.completed",
  "timestamp": "2026-08-27T16:00:00Z",
  "data": {
    "agent_id": "agent_abc123",
    "run_id": "run_xyz789",
    "status": "COMPLETED",
    "duration_seconds": 3.2
  }
}
```

Expect a 200 response within 5 seconds, or Cortex will retry up to 3 times.
