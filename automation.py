"""
Hybrid Automation System for CORTEX Agents

Supports both:
1. Time-based scheduling (cron-like: daily, weekly, specific times)
2. Event-based triggers (webhooks from external systems)
"""

import json
import os
from datetime import datetime, timedelta

AUTOMATION_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "automation.json")

DEFAULT_AUTOMATION = {
    "no-show": {
        "enabled": False,
        "schedule": "daily:06:00",  # daily at 6am
        "event_triggers": ["appointment_scheduled"],
        "last_run": None,
        "next_run": None
    },
    "appointment-reminder": {
        "enabled": False,
        "schedule": "24h_before",  # 24 hours before each appointment
        "event_triggers": ["appointment_created", "appointment_updated"],
        "last_run": None,
        "next_run": None
    },
    "lab-results": {
        "enabled": False,
        "schedule": "every_2h",  # Check every 2 hours
        "event_triggers": ["lab_result_available"],
        "last_run": None,
        "next_run": None
    },
    "prior-auth": {
        "enabled": False,
        "schedule": "daily:09:00",  # Daily at 9am
        "event_triggers": ["auth_request_received"],
        "last_run": None,
        "next_run": None
    },
    "integration-assistant": {
        "enabled": False,
        "schedule": None,
        "event_triggers": [],
        "last_run": None,
        "next_run": None
    },
    "editorial-verification": {
        "enabled": False,
        "schedule": None,
        "event_triggers": [],
        "last_run": None,
        "next_run": None
    }
}

def load_automation():
    """Load automation config, creating it if it doesn't exist."""
    try:
        with open(AUTOMATION_PATH) as f:
            config = json.load(f)
        # Ensure all agents have defaults
        for agent_id, defaults in DEFAULT_AUTOMATION.items():
            if agent_id not in config:
                config[agent_id] = defaults
        return config
    except FileNotFoundError:
        return DEFAULT_AUTOMATION.copy()

def save_automation(config):
    """Save automation config with restricted permissions."""
    with open(AUTOMATION_PATH, "w") as f:
        json.dump(config, f, indent=2)
    try:
        os.chmod(AUTOMATION_PATH, 0o600)
    except Exception:
        pass

def get_agent_automation(agent_id):
    """Get automation config for a specific agent."""
    config = load_automation()
    return config.get(agent_id, DEFAULT_AUTOMATION.get(agent_id, {}))

def update_agent_automation(agent_id, automation_config):
    """Update automation config for a specific agent."""
    config = load_automation()
    config[agent_id] = automation_config
    save_automation(config)
    return config[agent_id]

def parse_schedule(schedule_str):
    """Parse schedule string into next run time."""
    if not schedule_str:
        return None
    
    now = datetime.now()
    
    if schedule_str == "daily":
        return now + timedelta(days=1)
    elif schedule_str.startswith("daily:"):
        # daily:06:00
        time_str = schedule_str.split(":")[1:]
        hour, minute = int(time_str[0]), int(time_str[1]) if len(time_str) > 1 else 0
        next_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if next_run <= now:
            next_run += timedelta(days=1)
        return next_run
    elif schedule_str == "every_2h":
        return now + timedelta(hours=2)
    elif schedule_str == "every_6h":
        return now + timedelta(hours=6)
    elif schedule_str == "24h_before":
        return now + timedelta(hours=24)
    
    return None

def should_run(agent_id):
    """Check if an agent should run based on its schedule."""
    automation = get_agent_automation(agent_id)
    
    if not automation.get("enabled"):
        return False
    
    next_run = automation.get("next_run")
    if not next_run:
        return True  # First time
    
    try:
        next_run_dt = datetime.fromisoformat(next_run)
        return datetime.now() >= next_run_dt
    except (ValueError, TypeError):
        return False

def record_run(agent_id):
    """Record that an agent ran and compute next run time."""
    config = load_automation()
    automation = config.get(agent_id, {})
    
    automation["last_run"] = datetime.now().isoformat()
    automation["next_run"] = parse_schedule(automation.get("schedule")).isoformat() if automation.get("schedule") else None
    
    config[agent_id] = automation
    save_automation(config)

def check_event_trigger(agent_id, event_type):
    """Check if an agent should trigger on a specific event."""
    automation = get_agent_automation(agent_id)
    
    if not automation.get("enabled"):
        return False
    
    return event_type in automation.get("event_triggers", [])

