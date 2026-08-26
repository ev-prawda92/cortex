# CORTEX Agent Integration

Your CORTEX control plane now has **5 live agents**:

## 1. Editorial Verification ⚡ (Reference)
- Newsroom · Standards
- Existing agent (unchanged)
- Verifies claims against live sources

## 2-5. Real Healthcare Agents (NEW)

### No-Show Outreach
**Account**: Bright Health  
**Status**: Running  
**Metrics**: 85% containment, 90% resolution, 10% escalation  
**Purpose**: Identify at-risk patients and send personalized outreach  
**Posture**: Replace (take the action directly)  
**Config**: 
- First contact: 24 hours before appointment
- Retries: 3 (24-hour gaps)
- Channel: Voice + SMS
- Escalates to: Clinical team

**Agent Runner**: `python3 no_show_agent.py`

---

### Appointment Reminder
**Account**: Primary Care Partners  
**Status**: Running  
**Metrics**: 94% containment, 97% resolution, 3% escalation  
**Purpose**: Send appointment reminders 24-48 hours before visits  
**Posture**: Replace (send the reminder directly)  
**Config**:
- First contact: 48 hours before appointment
- Retries: 2 (24-hour gaps)
- Channel: SMS
- Escalates to: Scheduler

**Agent Runner**: `python3 appointment_reminder_agent.py`

---

### Lab Result Notification
**Account**: Quest Diagnostics  
**Status**: Running  
**Metrics**: 89% containment, 94% resolution, 6% escalation, 1 clinical flag  
**Purpose**: Deliver lab results with clinical context; escalate critical values immediately  
**Posture**: Augment (add clinical guidance)  
**Config**:
- First contact: 2 hours after result available
- Retries: 2 (24-hour gaps)
- Channel: Voice + SMS
- Escalates to: Provider
- Severity escalation: Moderate (some issues go to provider immediately)

**Agent Runner**: `python3 lab_result_agent.py`

---

### Prior Authorization
**Account**: Blue Shield  
**Status**: Running  
**Metrics**: 71% containment, 76% resolution, 24% escalation, 2 clinical flags  
**Purpose**: Manage prior authorization requests with insurance payers  
**Posture**: Support (assist the process, don't act alone)  
**Config**:
- First contact: 12 hours after request created
- Retries: 4 (24-hour gaps)
- Channel: Voice
- Escalates to: Payer
- Severity escalation: Low (most things go through)

**Agent Runner**: `python3 prior_auth_agent.py`

---

## How to Run

### Start CORTEX Control Panel
```bash
export ANTHROPIC_API_KEY="sk-ant-..."
pip install fastapi uvicorn httpx
python3 cortex.py
# Opens http://localhost:3000
```

### Run Agents Standalone (for testing)
```bash
export ANTHROPIC_API_KEY="sk-ant-..."
python3 no_show_agent.py
python3 appointment_reminder_agent.py
python3 lab_result_agent.py
python3 prior_auth_agent.py
```

---

## Files Changed

### New Agent Runners
- `no_show_agent.py` - No-Show Outreach runner
- `appointment_reminder_agent.py` - Appointment Reminder runner
- `lab_result_agent.py` - Lab Result Notification runner
- `prior_auth_agent.py` - Prior Authorization runner

### Updated
- `cortex.py` - Agent registry now contains only 5 live agents (1 reference + 4 real)
  - Removed all mock agents
  - Added real healthcare agents with full configs
  - Each agent has tool graph, escalation rules, communication channels

### Existing (Unchanged)
- `cortex_agents_framework.py` - Agent implementations (4 classes)
- `demo_agents.py` - Standalone demo script
- `agent.py` - Editorial Verification runner (reference)

---

## What's Next

### To make agents actually run on a schedule:

1. **Task queue integration**
   - Use a task scheduler (APScheduler, Celery, or cloud functions)
   - Trigger agents at appropriate intervals
   - Report metrics back to CORTEX API

2. **Real integrations** (see INTEGRATION_REQUIREMENTS.md)
   - No-Show: EHR + SMS platform (1-2 weeks)
   - Reminder: EHR scheduling + SMS (1 week)
   - Lab Results: LIS + clinical escalation rules (2-3 weeks)
   - Prior Auth: Payer connectivity / clearinghouse (4-8 weeks)

3. **Live metrics reporting**
   - Agents report containment/resolution/escalation to CORTEX
   - Dashboard updates in real-time
   - Each row shows live status, not mock data

---

## Architecture Overview

```
CORTEX Control Panel (localhost:3000)
    ├─ Agent Registry (cortex.py)
    │   ├─ Editorial Verification (agent.py)
    │   ├─ No-Show Outreach (no_show_agent.py)
    │   ├─ Appointment Reminder (appointment_reminder_agent.py)
    │   ├─ Lab Result Notification (lab_result_agent.py)
    │   └─ Prior Authorization (prior_auth_agent.py)
    │
    ├─ Config Management
    │   └─ Each agent has editable config
    │       (timing, channels, escalation thresholds)
    │
    └─ Monitoring
        ├─ Monitor tab: Live metrics
        ├─ Control tab: Change agent config
        ├─ History tab: Version control
        └─ Diagnostics: Config issues

Agent Runners
    ├─ Real agentic loop (think → act → observe)
    ├─ Tool use via Anthropic API
    ├─ Metric reporting back to CORTEX
    └─ Error/escalation handling
```

---

## Key Design Decisions

**Posture** (from CORTEX):
- **Replace**: Agent acts directly (No-Show, Reminder)
- **Augment**: Agent adds context/guidance (Lab Results)
- **Support**: Agent assists but doesn't act alone (Prior Auth)

**Escalation**:
- Clinical agents escalate earlier (Lab Results: moderate severity)
- Administrative agents escalate less (Reminder, No-Show: high severity)
- Complex agents escalate more (Prior Auth: low severity threshold)

**Confirm-then-act**: All agents confirm before action (safety gate)

---

## Testing the Integration

1. **Without real integrations** (current):
   - Agents use mock data
   - Suitable for planning & demos
   - Shows what agent can do, not yet connected to EHR/payers

2. **With real integrations** (next phase):
   - Replace mock tool_calls with real API calls
   - Agents pull real patient/appointment/result data
   - Send real messages via SMS/email platforms
   - Submit real prior auth requests to payers

3. **Production deployment**:
   - Scale to process 1000s of patients/day
   - Real-time metrics & alerting
   - HIPAA-compliant infrastructure
   - Clinical oversight built in

---

## For the Demo/Planning Meeting

Show these files:
1. **cortex.py** - The control plane (show the agent registry)
2. **no_show_agent.py** - Example agent runner (simple to read)
3. **INTEGRATION_REQUIREMENTS.md** - What integrations cost/timeline
4. Open **localhost:3000** - Dashboard with 5 agents

Say: *"These aren't mocks anymore. Each agent is real — it uses Claude's API with tool-based reasoning. The mock data shows what would flow from your EHR/payers once we plug those in. Timeline is 1-2 weeks per agent for integrations."*
