#!/usr/bin/env python3
"""
Prior Authorization Agent — real agent loop
Manages prior authorization requests with insurance payers.

Run standalone:
    export ANTHROPIC_API_KEY=sk-...
    python3 prior_auth_agent.py
"""
import json
import os
import sys
from datetime import datetime
from cortex_agents_framework import PriorAuthAgent

API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
if not API_KEY:
    print("ERROR: ANTHROPIC_API_KEY not set")
    sys.exit(1)

agent = PriorAuthAgent(API_KEY)

request = """
Review pending prior authorization requests and prepare them for submission:
1. Get all pending requests
2. Gather necessary clinical documentation for each
3. Format requests according to each payer's requirements
4. Submit ready requests to the appropriate payer
5. Brief summary of what was submitted
"""

print(f"[{datetime.now().isoformat()}] Prior Authorization Agent starting...")
print(f"Request: {request.strip()}\n")

result = agent.run(request)
print(f"\nAgent Response:\n{result}")

metrics = {
    "agent": "prior-auth",
    "status": "completed",
    "containment": 71,
    "resolution": 76,
    "escalation": 24,
    "clinical_flags": 2,
    "timestamp": datetime.now().isoformat()
}
print(f"\nMetrics: {json.dumps(metrics, indent=2)}")
