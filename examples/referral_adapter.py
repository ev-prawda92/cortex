"""Replace next_action with your existing local agent; no expected labels supplied.

Run: python -m rehearsal --adapter examples.referral_adapter:factory --report data/my-agent.json
The adapter is trusted Python code, not OS-sandboxed. Keep it inside your controlled
environment and connect only synthetic observation/tool response data during rehearsal.
"""
from rehearsal.agents import ScriptedAgent


def factory():
    # Return a new independent adapter for every scenario.
    # Interface: next_action(observation: dict, history: list) -> {tool, args} | None
    # Tools are executed by the harness, never by this adapter.
    return ScriptedAgent(careful=True)
