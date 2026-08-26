#!/usr/bin/env python3
"""
Integration Assistant Agent Runner
Helps developers integrate CORTEX agents into their applications.

Usage:
  export ANTHROPIC_API_KEY="your-api-key-here"
  python3 integration_assistant_agent.py
"""

import os
import sys
from cortex_agents_framework import IntegrationAssistantAgent

api_key = os.getenv("ANTHROPIC_API_KEY")
if not api_key:
    print("ERROR: ANTHROPIC_API_KEY not set")
    sys.exit(1)

agent = IntegrationAssistantAgent(api_key)

user_message = """
I need help integrating the No-Show Outreach agent into our patient app.
We're using React and want to embed it as a widget. What's the first step?
"""

print(f"User Request:\n{user_message}\n")
print("Integration Assistant Response:\n")
result = agent.run(user_message)
print(result)
