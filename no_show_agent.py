#!/usr/bin/env python3
"""
No-Show Outreach Agent — real agent loop
Identifies high-risk patients and sends personalized outreach.

Run standalone:
    export ANTHROPIC_API_KEY=sk-...
    python3 no_show_agent.py
"""
import json
import os
import sys
from datetime import datetime
from cortex_agents_framework import NoShowOutreachAgent

API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
if not API_KEY:
    print("ERROR: ANTHROPIC_API_KEY not set")
    sys.exit(1)

agent = NoShowOutreachAgent(API_KEY)

# Default request for testing
request = """
Identify patients at high risk of no-showing their appointments this week 
and send them personalized outreach. Consider their history and communication 
preferences. For each patient, compose an appropriate message and send it 
through their preferred channel.
"""

print(f"[{datetime.now().isoformat()}] No-Show Outreach Agent starting...")
print(f"Request: {request.strip()}\n")

result = agent.run(request)
print(f"\nAgent Response:\n{result}")

# Report metrics (mock for now, would update CORTEX API)
metrics = {
    "agent": "no-show",
    "status": "completed",
    "containment": 85,
    "resolution": 90,
    "escalation": 10,
    "clinical_flags": 0,
    "timestamp": datetime.now().isoformat()
}
print(f"\nMetrics: {json.dumps(metrics, indent=2)}")
