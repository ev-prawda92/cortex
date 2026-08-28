# Use Case Walkthroughs

Real-world scenarios and how to implement them in Cortex.

## Use Case 1: Customer Support Chatbot

**Scenario:** You have a Claude-built chatbot that handles tier-1 support. It answers FAQs, escalates complex issues to humans, logs tickets. You want to deploy it and monitor it in production.

### Step 1: Register the Agent

```bash
curl -X POST http://localhost:8000/api/agents \
  -H "Content-Type: application/json" \
  -d '{
    "name": "support-chatbot",
    "description": "Handles tier-1 support, escalates complex issues",
    "config": {
      "model": {
        "provider": "anthropic",
        "model_name": "claude-3-5-sonnet-20241022"
      },
      "standing_instruction": "You are a customer support chatbot. Answer FAQ questions from our knowledge base. If the customer reports a bug, escalate to engineering. If they request a feature, log it. Be friendly and concise.",
      "run_interval_seconds": 0,
      "task_type": "chat",
      "tools": [
        {
          "name": "fetch_faq",
          "description": "Look up an answer in the FAQ",
          "parameters": ["question"]
        },
        {
          "name": "escalate_to_human",
          "description": "Route to a human agent",
          "parameters": ["reason", "customer_id"]
        },
        {
          "name": "log_ticket",
          "description": "Create a support ticket",
          "parameters": ["customer_id", "issue_type", "description"]
        }
      ]
    }
  }'
```

Note: `run_interval_seconds: 0` because this is on-demand (triggered by incoming customer messages, not a schedule).

### Step 2: Integrate with Your Chat Platform

You have a webhook endpoint that receives customer messages. Forward them to Cortex:

```python
# Your chat platform receives a message from customer
@app.post("/messages")
def handle_message(message: dict):
    # Get the customer message
    text = message["text"]
    customer_id = message["customer_id"]
    
    # Trigger the chatbot in Cortex
    response = requests.post(
        "http://cortex.company.com/api/agents/support-chatbot/run",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={"context": f"Customer ID: {customer_id}\nMessage: {text}"}
    )
    
    run_id = response.json()["run_id"]
    
    # Cortex is processing. Poll for the result
    time.sleep(0.5)
    result = requests.get(
        f"http://cortex.company.com/api/agents/support-chatbot/runs/{run_id}",
        headers={"Authorization": f"Bearer {API_KEY}"}
    )
    
    run_data = result.json()
    
    # Extract the response
    chatbot_response = run_data["detail"]["summary"]
    
    # Send back to customer
    send_message(customer_id, chatbot_response)
```

### Step 3: Monitor & Alert

1. Go to **Monitor** tab in Cortex
2. See the chatbot's health score
3. Set up alerts:
   - Error rate > 5%: Something is wrong with the chatbot
   - Escalation rate > 50%: Too many human escalations, review FAQs
   - Response time > 5 seconds: Chatbot is slow, check model latency

```bash
# Alert if too many escalations
curl -X POST http://localhost:8000/api/agents/support-chatbot/alerts \
  -H "Content-Type: application/json" \
  -d '{
    "name": "High Escalation Rate",
    "condition": "escalation_rate_24h > 0.5",
    "severity": "warning",
    "channels": ["email"]
  }'
```

### Step 4: Improve Over Time

1. Go to **Runs** tab
2. Filter by status: ESCALATED
3. Read the escalations—are there patterns?
4. Example: "Billing questions always escalate"
5. Update the standing instruction to include more billing FAQs
6. Save → v2 created
7. Deploy v2 canary (10%, 30 minutes)
8. If escalation rate drops, promote to 100%

## Use Case 2: Batch Data Processing

**Scenario:** Every night, you need to process yesterday's data (transactions, logs, events). An agent fetches the data, processes it, and uploads results. You want Cortex to manage this.

### Step 1: Register the Agent

```bash
curl -X POST http://localhost:8000/api/agents \
  -H "Content-Type: application/json" \
  -d '{
    "name": "daily-data-processor",
    "description": "Processes daily transactions and logs results",
    "config": {
      "model": {
        "provider": "anthropic",
        "model_name": "claude-3-5-sonnet-20241022"
      },
      "standing_instruction": "Fetch transactions from the last 24 hours. Group by merchant. Flag any suspicious activity (>$10k in one transaction, or >$50k in a day). Summarize findings.",
      "run_interval_seconds": 0,
      "task_type": "data-processing",
      "tools": [
        {
          "name": "fetch_transactions",
          "description": "Get transactions from yesterday",
          "parameters": ["start_time", "end_time"]
        },
        {
          "name": "upload_report",
          "description": "Upload results to storage",
          "parameters": ["report_data", "filename"]
        }
      ]
    }
  }'
```

### Step 2: Schedule the Agent

Cortex agents run on an interval. But for batch jobs, you want to trigger at a specific time (e.g., 2 AM daily).

Use a cron job or scheduler to trigger Cortex:

```bash
# In your cron job (runs at 2 AM)
0 2 * * * curl -X POST https://cortex.company.com/api/agents/daily-data-processor/run \
  -H "Authorization: Bearer YOUR_API_KEY"
```

### Step 3: Monitor & Alert

Set up SLO: "Process data within 5 minutes."

```bash
curl -X POST http://localhost:8000/api/agents/daily-data-processor/slos \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Daily Processing SLO",
    "target": 1.0,
    "window_minutes": 1440,
    "metric": "latency_p95",
    "threshold": 300
  }'
```

Alert if the job fails:
```bash
curl -X POST http://localhost:8000/api/agents/daily-data-processor/alerts \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Daily Processing Failed",
    "condition": "error_rate_24h > 0.0",
    "severity": "critical",
    "channels": ["email", "slack"]
  }'
```

If the job fails, you get paged immediately.

### Step 4: Observe Results

Go to **Analytics** tab:
- See if processing time is trending up (more data, slower processing)
- See cost per day (tokens used)
- If cost is increasing, maybe the agent is generating too much analysis—simplify the instruction

## Use Case 3: Multi-Agent Workflow

**Scenario:** You have 2 agents that work together:
1. **Analyzer:** Reads raw data, extracts insights
2. **Reporter:** Takes insights, formats a report

You want to run them in sequence: Analyzer → Reporter.

### Step 1: Create Both Agents

```bash
# Agent 1: Analyzer
curl -X POST http://localhost:8000/api/agents \
  -d '{
    "name": "analyzer",
    "config": {
      "standing_instruction": "Analyze the data and extract key metrics...",
      ...
    }
  }'

# Agent 2: Reporter
curl -X POST http://localhost:8000/api/agents \
  -d '{
    "name": "reporter",
    "config": {
      "standing_instruction": "Take the insights and format as a report...",
      ...
    }
  }'
```

### Step 2: Create a Workflow

Use **More > Agent Mesh** to connect them:

1. Click **New Workflow**
2. Add Analyzer as first step
3. Add Reporter as second step
4. Set dependency: Reporter waits for Analyzer
5. Save

Or via API:

```bash
curl -X POST http://localhost:8000/api/workflows \
  -H "Content-Type: application/json" \
  -d '{
    "name": "analysis-to-report",
    "steps": [
      {
        "id": "step1",
        "agent_id": "analyzer",
        "depends_on": []
      },
      {
        "id": "step2",
        "agent_id": "reporter",
        "depends_on": ["step1"],
        "input_from": "step1.output"
      }
    ]
  }'
```

### Step 3: Trigger the Workflow

```bash
curl -X POST http://localhost:8000/api/workflows/analysis-to-report/run
```

Cortex runs Analyzer, waits for it to complete, then runs Reporter with the output.

## Use Case 4: A/B Testing Agent Versions

**Scenario:** You want to test two versions of an agent against real traffic and measure which is better.

### Step 1: Create Two Versions

You have agent v1 (current). You want to test v2 (new model, new prompt).

Create v2:
```bash
curl -X PATCH http://localhost:8000/api/agents/my-agent \
  -H "Content-Type: application/json" \
  -d '{
    "config": {
      "model": {
        "provider": "anthropic",
        "model_name": "claude-3-opus-20250219"
      },
      "standing_instruction": "New, improved instruction"
    }
  }'

# v2 is created
```

### Step 2: Split Traffic

Route 50% of incoming requests to v1, 50% to v2:

```python
# Your application
def handle_request(request):
    import random
    agent_version = random.choice(["v1-agent", "v2-agent"])
    
    result = cortex_client.run_agent(agent_version, request)
    return result
```

Or use Cortex's built-in A/B testing:

```bash
curl -X POST http://localhost:8000/api/agents/my-agent/ab-test \
  -H "Content-Type: application/json" \
  -d '{
    "version_a": 1,
    "version_b": 2,
    "split": 0.5,
    "duration_minutes": 1440
  }'
```

### Step 3: Compare Metrics

After 24 hours, go to **Analytics** tab:
- Compare error rate: v1 vs v2
- Compare latency: v1 vs v2
- Compare user satisfaction (if you have feedback)
- Compare cost: v1 vs v2 (Opus is more expensive)

Example data:
- v1: 95% success, 2.3s latency, $0.15/run
- v2: 98% success, 1.9s latency, $0.32/run

**Decision:** v2 is more accurate and faster, but costs 2x more. Is the improvement worth it?

### Step 4: Promote Winner

If v2 wins:
```bash
curl -X POST http://localhost:8000/api/agents/my-agent/rollout \
  -H "Content-Type: application/json" \
  -d '{
    "target_version": 2,
    "strategy": "gradual",
    "steps": [
      {"percentage": 50, "wait_minutes": 30},
      {"percentage": 100, "wait_minutes": 0}
    ]
  }'
```

Gradually roll out v2 to 100%.

## Use Case 5: Multi-Client Agent Management

**Scenario:** You're a consultancy. You built an invoice processor. You're deploying it to 50 clients. Each client wants slight customizations (their own invoice format, their own vendor list).

### Step 1: Create Base Agent

```bash
curl -X POST http://localhost:8000/api/agents \
  -d '{
    "name": "invoice-processor-base",
    "description": "Standard invoice processor",
    "config": {
      "standing_instruction": "Process invoices: extract vendor, date, amount, categorize expense",
      ...
    }
  }'
```

### Step 2: Create Client Workspaces

```bash
# Create 50 workspaces, one per client
for i in {1..50}; do
  curl -X POST http://localhost:8000/api/workspaces \
    -d "{\"name\":\"client-$i\"}"
done
```

### Step 3: Deploy Agent to Each Workspace

For each client, create their version of the agent:

```python
# For Client A: Standard agent
# For Client B: Agent with custom vendors
# For Client C: Agent with custom format

for client_id in range(1, 51):
    if client_id == 2:
        # Client B has custom vendors
        instruction = "Process invoices... Known vendors: Acme, BigCorp, TechInc..."
    elif client_id == 3:
        # Client C has custom format
        instruction = "Process invoices... Format: CSV with 4 columns..."
    else:
        # Standard instruction
        instruction = "Process invoices..."
    
    create_agent(
        workspace=f"client-{client_id}",
        name="invoice-processor",
        instruction=instruction
    )
```

### Step 4: Roll Out Updates

New version of the base agent (v2). You want to test with 5 clients before rolling to all 50.

```bash
curl -X POST http://localhost:8000/api/agents/invoice-processor/rollout \
  -H "Content-Type: application/json" \
  -d '{
    "target_version": 2,
    "strategy": "canary",
    "canary_workspace_count": 5,
    "canary_duration_minutes": 120
  }'
```

Cortex picks 5 random clients, deploys v2 to them, monitors for 2 hours. If error rate stays normal, promotes to all 50.

### Step 5: Track Costs Per Client

```bash
curl http://localhost:8000/api/usage/by-workspace

# Response:
[
  {"workspace": "client-1", "cost": 523.45},
  {"workspace": "client-2", "cost": 287.12},
  {"workspace": "client-3", "cost": 445.67},
  ...
]
```

Chargeback to each client based on their usage.

## Use Case 6: Incident Response

**Scenario:** An agent suddenly starts erroring for all your clients. You need to debug and fix it quickly.

### Step 1: Alert Fires

You get alerted: "invoice-processor health score critical"

### Step 2: Investigate

Go to **Monitor** tab in Cortex:
- Click the agent
- See recent runs all failing
- Click a failing run
- Read the trace

Example trace:
```
[Tool Call] fetch_invoices(source="s3")
[Tool Error] Access Denied: s3://invoices bucket
[Error] Cannot proceed, no invoices to process
[Status] ERROR
```

Root cause: S3 access is broken.

### Step 3: Fix

Option A (quick): Rollback to v1 (which worked)
```bash
curl -X POST http://localhost:8000/api/agents/invoice-processor/rollback \
  -d '{
    "target_version": 1
  }'
```

All instances immediately switch back. Service restored in <1 minute.

Option B (proper): Fix the issue and deploy v3
```bash
# Update the S3 credentials or endpoint
curl -X PATCH http://localhost:8000/api/agents/invoice-processor \
  -d '{
    "config": {
      "tools": [
        {
          "name": "fetch_invoices",
          "implementation": "use-new-s3-endpoint"
        }
      ]
    }
  }'

# v3 created. Deploy canary.
curl -X POST http://localhost:8000/api/agents/invoice-processor/rollout \
  -d '{
    "target_version": 3,
    "strategy": "canary",
    "canary_percentage": 20,
    "canary_duration_minutes": 60
  }'
```

Monitor the canary. If good, promote to 100%.

### Step 4: Post-Mortem

Go to **More > Recovery** (version history). See:
- v1: Worked fine
- v2: Broke S3 access (see the diff)
- v3: Fixed it

Learn what changed in v2 that broke things.
