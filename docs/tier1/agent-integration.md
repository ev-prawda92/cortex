# Agent Integration Guide

This guide explains how to take an existing agent (from Claude, OpenAI, custom code, or anywhere) and register it in Cortex for operations.

## What Integration Means

Integrating an agent into Cortex means:
1. **Register** the agent with Cortex (give it a name, description, config)
2. **Connect** Cortex to your agent's runtime (API endpoint or execution logic)
3. **Monitor** its health, costs, and runs from Cortex
4. **Operate** it (version, rollback, alert, scale) from Cortex

Cortex doesn't *build* your agent—it *operates* it.

## The Agent Registration Schema

When you register an agent, you provide:

```json
{
  "name": "string (required, unique)",
  "description": "string (optional, what it does)",
  "config": {
    "model": {
      "provider": "anthropic | openai | custom",
      "model_name": "claude-3-5-sonnet-20241022 | gpt-4o | etc"
    },
    "standing_instruction": "string (required, what to do each cycle)",
    "run_interval_seconds": 60,
    "task_type": "general | chat | data-processing | etc",
    "behavior": {
      "confidence_threshold": 0.75
    },
    "tools": [
      {
        "name": "tool-name",
        "description": "what it does",
        "parameters": ["param1", "param2"]
      }
    ]
  },
  "endpoint": {
    "url": "optional, only if agent runs at a custom endpoint",
    "auth_method": "api_key | bearer | basic"
  }
}
```

### Fields Explained

| Field | Required | Example | Notes |
|-------|----------|---------|-------|
| `name` | Yes | `customer-support-bot` | Unique per workspace |
| `description` | No | `Handles tier-1 support tickets` | For humans |
| `standing_instruction` | Yes | `Respond to support tickets in Slack` | What the agent does each cycle |
| `run_interval_seconds` | No | `60` | Default 60s. Set to 0 for on-demand only |
| `model.provider` | Yes | `anthropic`, `openai` | Where the model lives |
| `model.model_name` | Yes | `claude-3-5-sonnet-20241022` | Which model to use |
| `task_type` | No | `chat`, `data-processing` | For categorization and cost tracking |
| `tools` | No | See below | Custom tools the agent can use |
| `endpoint.url` | No | `https://my-agent.example.com/run` | Only if agent is hosted externally |

## Integration Patterns

### Pattern 1: Claude-Built Agent (Simplest)

You have an agent in Claude or a notebook. You want Cortex to run it on a loop.

**What you do:**
1. Export your agent's system prompt (the "standing instruction")
2. Register in Cortex with that instruction
3. Cortex calls Claude API with your instruction each cycle

**Registration:**

```bash
curl -X POST http://localhost:8000/api/agents \
  -H "Content-Type: application/json" \
  -d '{
    "name": "invoice-processor",
    "description": "Processes invoices from Slack",
    "config": {
      "model": {
        "provider": "anthropic",
        "model_name": "claude-3-5-sonnet-20241022"
      },
      "standing_instruction": "Check the #invoices Slack channel for new PDFs. Extract invoice data (vendor, date, amount), validate it, and post results to #processed-invoices. Be concise.",
      "run_interval_seconds": 300,
      "task_type": "data-processing"
    }
  }'
```

### Pattern 2: Existing HTTP Endpoint

You have an agent running at `https://my-agent.example.com/run`. Cortex should call it and monitor it.

**What you do:**
1. Ensure your endpoint accepts POST requests
2. Register in Cortex with the endpoint URL
3. Cortex will call your endpoint, track the response, and monitor health

**Registration:**

```bash
curl -X POST http://localhost:8000/api/agents \
  -H "Content-Type: application/json" \
  -d '{
    "name": "custom-data-agent",
    "description": "Custom Python agent running on our servers",
    "config": {
      "standing_instruction": "Fetch daily metrics from warehouse and aggregate them"
    },
    "endpoint": {
      "url": "https://my-agent.example.com/run",
      "auth_method": "api_key"
    }
  }'
```

Cortex will POST to your endpoint with:
```json
{
  "agent_id": "...",
  "cycle": 1,
  "instruction": "Fetch daily metrics...",
  "timestamp": "2026-08-27T16:00:00Z"
}
```

Your endpoint should respond with:
```json
{
  "ok": true,
  "outcome": "COMPLETED",
  "summary": "Processed 1,245 rows, found 3 anomalies",
  "duration_seconds": 12
}
```

### Pattern 3: Webhook-Triggered

Your agent should only run when an event happens (new message, scheduled time, external trigger).

**What you do:**
1. Set `run_interval_seconds` to 0 (disables loop)
2. Cortex exposes a webhook URL for your agent
3. When an event happens elsewhere, you call the webhook
4. Cortex executes the agent and monitors it

**Registration:**

```bash
curl -X POST http://localhost:8000/api/agents \
  -H "Content-Type: application/json" \
  -d '{
    "name": "webhook-responder",
    "config": {
      "model": {"provider": "anthropic", "model_name": "claude-3-5-sonnet-20241022"},
      "standing_instruction": "Respond to webhook events",
      "run_interval_seconds": 0
    }
  }'
```

Then trigger it from elsewhere:
```bash
curl -X POST http://localhost:8000/api/agents/{agent_id}/run \
  -H "Content-Type: application/json" \
  -d '{"context": "some event data"}'
```

## Adding Tools to Your Agent

If your agent needs to call external APIs (Slack, GitHub, database), define them:

```json
{
  "name": "slack-agent",
  "config": {
    "standing_instruction": "Monitor Slack and categorize messages",
    "tools": [
      {
        "name": "fetch_slack_messages",
        "description": "Get unread messages from a Slack channel",
        "parameters": ["channel_id", "limit"]
      },
      {
        "name": "post_slack_message",
        "description": "Send a message to Slack",
        "parameters": ["channel_id", "text"]
      }
    ]
  }
}
```

When the agent runs, it can request these tools. Cortex will:
1. Log the tool call
2. Execute it (if integrated)
3. Return the result to the agent

See [Integrations](../tier2/integrations.md) for how to wire up real Slack/GitHub/etc calls.

## Testing Your Integration

Once registered, your agent appears in the Agents tab. Test it:

```bash
# Manually trigger a run
curl -X POST http://localhost:8000/api/agents/{agent_id}/run

# Check the result in Monitor tab (refresh in browser)
# Or query the API:
curl http://localhost:8000/api/agents/{agent_id}/runs?limit=5
```

You should see:
- Run record in the database
- Outcome (COMPLETED, ERROR, etc)
- Duration
- Any errors logged

If something failed, check the trace:
```bash
curl http://localhost:8000/api/agents/{agent_id}/runs/{run_id}
```

Look at the `trace` field—it shows step-by-step execution and where it failed.

## Common Integration Scenarios

### Scenario 1: Email Processing Agent (Claude-built, scheduled)

```json
{
  "name": "email-categorizer",
  "description": "Reads new emails and categorizes them",
  "config": {
    "model": {"provider": "anthropic", "model_name": "claude-3-5-sonnet-20241022"},
    "standing_instruction": "Fetch new emails from support@company.com inbox. Categorize each as: urgent, feature-request, bug-report, or general. Post results to internal dashboard.",
    "run_interval_seconds": 300,
    "task_type": "data-processing",
    "tools": [{"name": "fetch_emails", "description": "Get new emails", "parameters": ["limit"]}]
  }
}
```

### Scenario 2: Real-Time Chat Agent (OpenAI, endpoint-based)

```json
{
  "name": "customer-chat",
  "description": "Handles real-time customer support chats",
  "config": {
    "standing_instruction": "Respond to customer inquiries in real-time"
  },
  "endpoint": {
    "url": "https://chat-api.company.com/agent/run",
    "auth_method": "api_key"
  }
}
```

### Scenario 3: Scheduled Report Generator (Webhook-triggered)

```json
{
  "name": "daily-report",
  "description": "Generates daily performance reports",
  "config": {
    "model": {"provider": "anthropic", "model_name": "claude-3-5-sonnet-20241022"},
    "standing_instruction": "Query metrics database, generate daily report, email to stakeholders",
    "run_interval_seconds": 0,
    "tools": [{"name": "query_metrics_db", "parameters": ["query"]}]
  }
}
```

Then trigger daily via cron:
```bash
# In your cron job or scheduler
curl -X POST http://localhost:8000/api/agents/{daily-report-id}/run
```

## Next Steps

- **Monitor this agent's health and costs** → Read [Monitoring & Alerting](../tier2/monitoring.md)
- **Deploy multiple versions and rollback** → Read [Versioning & Rollout](../tier2/versioning.md)
- **Set up team access** → Read [Multi-Tenant Setup](../tier2/multi-tenant.md)
