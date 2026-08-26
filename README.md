# CORTEX — Agent Control Plane

A real-time dashboard for monitoring, configuring, and managing AI agents in production. Built for healthcare and enterprise use cases where agents need human oversight, version control, and audit trails.

## Features

- **Monitor** — Live dashboards showing agent metrics (containment, resolution, escalation rates)
- **Control** — Edit agent configs in plain English; propose diffs; version every change
- **History** — Full version history and rollback for all agent configurations
- **Automation** — Time-based scheduling and event-triggered execution
- **Runs** — Execution logs and output from each agent run
- **Settings** — Multi-provider LLM key management (Anthropic, OpenAI, Gemini)
- **Glossary** — Configuration guide and explanation of all agent settings
- **Integrations** — Deploy agents to external systems (webhooks, APIs)
- **Diagnostics** — Real-world case studies showing agent reasoning and escalation

## Quick Start

```bash
cd CortexUpdated
python3 cortex.py
```

Then open **http://localhost:3000** in your browser.

## Architecture

**One-file backend** (FastAPI):
- Single `cortex.py` file contains the entire server
- Serves embedded HTML/CSS/JS dashboard
- No build step required

**Multi-provider support**:
- `providers.py` — Abstraction for Anthropic, OpenAI, Gemini
- `automation.py` — Time-based and event-triggered scheduling
- `settings.json` — Persisted API keys and model configuration

**Agent configs**:
- Structured YAML-like configs with posture (replace/augment/support)
- Journey settings (channels, timing, retry logic)
- Escalation thresholds and routing
- Prompt templates

## File Structure

```
cortex.py              # Main FastAPI server + embedded dashboard
providers.py           # Multi-provider LLM abstraction
automation.py          # Scheduling and event triggers
settings.json          # API keys and model choices (created at runtime)
automation.json        # Automation schedules per agent (created at runtime)
```

## Configuration

### API Keys

Keys are stored in `settings.json` (auto-created). You can set them:
1. In the **Settings** tab of the dashboard
2. Via environment variables: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`
3. Or directly in `settings.json`

### Models

Default models are set in `providers.py`. Override in **Settings** tab.

## Development

To add a new agent:
1. Add it to the `AGENTS` dict in `cortex.py`
2. Set its config (posture, channels, escalation rules)
3. Add default automation settings in `automation.py`
4. Reload the dashboard

## API Endpoints

- `GET /api/agents` — List all agents
- `GET /api/agents/{id}` — Get agent details
- `POST /api/agents/{id}/run` — Run agent with input
- `GET /api/agents/{id}/automation` — Get automation config
- `POST /api/agents/{id}/automation` — Update automation config
- `POST /api/agents/{id}/propose` — Propose config change
- `POST /api/agents/{id}/apply` — Apply approved change
- `POST /api/agents/{id}/history` — Get version history

## Security Notes

- `settings.json` and `automation.json` are created with `0o600` (read/write for owner only)
- API keys are masked in the UI (first 7 + last 4 chars)
- Add these files to `.gitignore` before committing
- Use environment variables for sensitive data in production

## Status

**v0.2.0** — Early stages. Core features working:
- ✅ Dashboard rendering
- ✅ Agent monitoring
- ✅ Config management with diffs
- ✅ Version control and rollback
- ✅ Automation scheduling
- ✅ Multi-provider LLM support
- 🔧 Integration deployment (framework ready)
- 🔧 Real-time agent execution output

## License

MIT
