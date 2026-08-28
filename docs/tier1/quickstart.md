# Quickstart: Get Cortex Running in 15 Minutes

This guide gets you from zero to running your first agent in Cortex. By the end, you'll have:
- Cortex running locally
- A sample agent deployed
- Real-time monitoring visible in the UI
- A foundation to integrate your own agents

**Time:** ~15 minutes  
**Prerequisites:** Docker, Docker Compose

## Step 1: Clone & Start Cortex (3 min)

```bash
git clone https://github.com/your-org/cortex.git
cd cortex
docker compose up --build
```

Wait for the output to show `Uvicorn running on http://0.0.0.0:8000`.

Open your browser: **http://localhost:8000**

You should see the Cortex dashboard with empty tabs (Agents, Monitor, Runs, etc.).

## Step 2: Register a Sample Agent (2 min)

In a new terminal:

```bash
curl -X POST http://localhost:8000/api/agents \
  -H "Content-Type: application/json" \
  -d '{
    "name": "hello-agent",
    "description": "A simple test agent",
    "config": {
      "model": {
        "provider": "anthropic",
        "model_name": "claude-3-5-sonnet-20241022"
      },
      "standing_instruction": "Say hello and the current time."
    }
  }'
```

You'll get back a response with an `id`. Note it down (or just look at the dashboard—it should appear in the Agents tab).

## Step 3: Start the Agent (1 min)

```bash
curl -X POST http://localhost:8000/api/agents/{agent_id}/start
```

Replace `{agent_id}` with the ID from step 2.

The agent is now running. It will execute every 60 seconds (configurable).

## Step 4: Watch It in the Monitor Tab (5 min)

1. Go to http://localhost:8000 in your browser
2. Click the **Monitor** tab
3. You should see:
   - Agent name: "hello-agent"
   - Health score (will populate after first run)
   - Error count (starts at 0)
   - Recent runs with timestamps and outcomes

Wait 60 seconds and refresh. You'll see a new run appear.

## Step 5: View a Run's Details (2 min)

Click on any run in the Monitor tab. You'll see:
- **Claim:** What the agent was asked to do
- **Outcome:** COMPLETED, ESCALATED, or ERROR
- **Duration:** How long it took
- **Tokens:** Input and output token usage
- **Trace:** Step-by-step execution log

This is what you'll use to debug agents in production.

## You're Done

You now have:
- ✅ Cortex running locally
- ✅ An agent deployed and executing
- ✅ Real-time monitoring and run history
- ✅ A foundation to integrate your own agents

## Next Steps

- **Want to integrate your own agent?** → Read [Agent Integration Guide](./agent-integration.md)
- **Ready to set up monitoring alerts?** → Read [Monitoring & Alerting](../tier2/monitoring.md)
- **Deploy to production?** → Read [Deployment Guide](./deployment.md)

## Troubleshooting

**Dashboard shows no agents:**
- Check that the curl request succeeded (look for error in response)
- Ensure Cortex is running (`docker compose logs` to see server)

**Agent not executing:**
- Check that you called `/api/agents/{id}/start`
- Verify your API key for the model provider is set (`DATABASE_URL=... docker compose up`)

**Don't see Monitor tab:**
- Refresh the page (browser cache)
- Check browser console for errors (F12)
