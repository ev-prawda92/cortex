<p align="center">
  <img src="https://img.shields.io/badge/CORTEX-Agent_Ops_Hub-f97316?style=for-the-badge&labelColor=1a1208" alt="Cortex"/>
</p>

<h1 align="center">CORTEX</h1>
<p align="center"><strong>The control plane that safely connects AI agents to the real world.</strong></p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.12-blue?logo=python&logoColor=white" alt="Python 3.12"/>
  <img src="https://img.shields.io/badge/FastAPI-0.100+-green?logo=fastapi&logoColor=white" alt="FastAPI"/>
  <img src="https://img.shields.io/badge/PostgreSQL-16-336791?logo=postgresql&logoColor=white" alt="PostgreSQL"/>
  <img src="https://img.shields.io/badge/Docker-ready-2496ED?logo=docker&logoColor=white" alt="Docker"/>
  <img src="https://img.shields.io/badge/License-BSL_1.1-yellow" alt="Business Source License 1.1"/>
  <img src="https://img.shields.io/badge/PRs-welcome-brightgreen" alt="PRs Welcome"/>
</p>

<p align="center">
  <a href="#quickstart">Quickstart</a> •
  <a href="#features">Features</a> •
  <a href="#architecture">Architecture</a> •
  <a href="#api">API</a> •
  <a href="#deployment">Deployment</a> •
  <a href="#contributing">Contributing</a>
</p>

---

## What is Cortex?

Cortex is a **self-hosted agent operations platform** — register, configure, run, and monitor any AI agent from a single dashboard. It doesn't own your data or lock you into a provider. It controls access, enforces policy, and gives you a complete audit trail.

**Cortex sits at layer 3** of the modern AI stack — the orchestration layer between your agents and the outside world:

```
┌─────────────────────────────────────────┐
│  Layer 5 · Enterprise                   │  Attestation, RBAC, compliance
├─────────────────────────────────────────┤
│  Layer 4 · Integrations                 │  Webhooks, APIs, data sources
├═════════════════════════════════════════╡
│  Layer 3 · CORTEX  ◄── you are here    │  Orchestrate, monitor, gate, audit
├═════════════════════════════════════════╡
│  Layer 2 · Agents                       │  Custom, imported, templates
├─────────────────────────────────────────┤
│  Layer 1 · Models                       │  Claude, GPT, Gemini — swap freely
└─────────────────────────────────────────┘
```

## Quickstart

### Docker (recommended)

```bash
git clone https://github.com/cortex-ai/cortex.git
cd cortex
docker compose up -d
```

Open **http://localhost:3000** → create an account → start registering agents.

### Manual

```bash
pip install -r requirements.txt
export DATABASE_URL="postgresql://user:pass@localhost:5432/cortex"
python3 cortex.py
```

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `postgresql://cortex:cortex@localhost:5432/cortex` | PostgreSQL connection string |
| `SECRET_KEY` | generated in development | JWT signing key; required in production |
| `CORTEX_ENCRYPTION_KEY` | generated in development | Fernet key; required in production |
| `CORS_ORIGINS` | local development origins | Comma-separated trusted origins; required in production |
| `TRUSTED_HOSTS` | local development hosts | Comma-separated hostnames; required in production |
| `CORTEX_AUTHZ_FAIL_CLOSED` | environment-based | Must be `true` in production |
| `ALLOW_SIGNUP` | environment-based | Must be `false` in production; provision users through admins or SSO |
| `CORTEX_BOOTSTRAP_TOKEN` | — | 32+ character one-time secret for creating the first production administrator |
| `SEED_SAMPLE_AGENTS` | environment-based | Must be `false` in production |
| `ANTHROPIC_API_KEY` | — | Enables Anthropic Claude models |
| `OPENAI_API_KEY` | — | Enables OpenAI GPT models |
| `GOOGLE_API_KEY` | — | Enables Google Gemini models |
| `GOOGLE_CLIENT_ID` | — | Google OAuth SSO |
| `GITHUB_CLIENT_ID` | — | GitHub OAuth SSO |

## Features

### Core Platform
- **Provider-agnostic runtime** — swap between Anthropic, OpenAI, and Google with one setting
- **Natural language control** — describe changes in English, review diffs, approve explicitly
- **Agent templates** — save, share, and clone agent configurations
- **Multi-endpoint support** — embedded execution, REST proxy, webhook-triggered
- **Import agents** from OpenAI Assistants, LangChain, or custom JSON

### Enterprise
- **Tamper-evident attestation** — hash-chained provenance records (SHA-256, linked `prev_hash`)
- **RBAC** — viewer / operator / admin roles with per-agent scope overrides
- **Human approval workflows** — gate high-risk actions behind manual approval
- **Scoped API keys** — `agents:read`, `agents:run`, `agents:write` permissions
- **OAuth SSO** — Google and GitHub authentication

### Observability
- **Usage analytics dashboard** — token usage, run volume, latency (avg + P95), success rates
- **Real-time monitoring** — agent status, health scores, activity feeds
- **HMAC-signed webhooks** — event delivery with auto-disable after repeated failures
- **In-app notifications** — real-time badge updates, dismiss/mark-read
- **Full run traces** — every step recorded with provider, model, and token counts

### Built-in Intelligence
- **Embedded AI assistant** — context-aware chat on the monitoring page, uses live agent data
- **Automated diagnostics** — config vs. platform issue detection with fix recommendations
- **Deterministic fallback** — works without an API key for common change types

## Architecture

```
cortex/
├── cortex.py          # FastAPI app + embedded dashboard (single-file)
├── db.py              # SQLAlchemy models + session management
├── auth.py            # JWT, password hashing, OAuth flows
├── providers.py       # Multi-provider LLM abstraction
├── automation.py      # Scheduling + event trigger engine
├── landing.html       # Marketing / product landing page
├── Dockerfile         # Production container
├── docker-compose.yml # One-command deploy
└── requirements.txt   # Python dependencies
```

### Database Schema

| Table | Purpose |
|---|---|
| `users` | Authentication (email/password + OAuth) |
| `agents` | Agent configs, status, metrics |
| `runs` | Execution history with full traces |
| `data_sources` | Agent data source configurations |
| `settings` | Per-instance provider settings |
| `api_keys` | Scoped API keys for external callers |
| `webhooks` | Webhook subscriptions with HMAC signing |
| `agent_templates` | Reusable agent config templates |
| `notifications` | In-app user notifications |
| `attestations` | Hash-chained provenance records |
| `user_roles` | RBAC role assignments |
| `approval_requests` | Human approval workflows |
| `audit_log` | Immutable event audit trail |
| `oauth_states` | CSRF protection for OAuth flows |

## API

All endpoints are under `/api/`. Authentication is via JWT cookie or `Authorization: Bearer ctx_...` API key.

### Agents
```
GET    /api/agents                    # List all agents
GET    /api/agents/:id                # Get agent details
POST   /api/agents/register           # Register a new agent
POST   /api/agents/import             # Import from external format
POST   /api/agents/:id/run            # Execute an agent
POST   /api/agents/:id/propose        # Propose a config change
POST   /api/agents/:id/apply          # Apply an approved change
POST   /api/agents/:id/control?action= # Start/stop an agent
DELETE /api/agents/:id                # Delete an agent
```

### Enterprise
```
GET    /api/attestations              # List provenance records
GET    /api/attestations/verify/:id   # Verify an agent's chain
GET    /api/roles                     # List role assignments
POST   /api/roles                     # Set a user's role
GET    /api/approvals                 # List approval requests
POST   /api/approvals/:id             # Approve or reject
```

### Operations
```
GET    /api/analytics                 # Usage analytics dashboard
GET    /api/metrics/portfolio         # Portfolio health metrics
GET    /api/events                    # Event log
GET    /api/notifications             # User notifications
POST   /api/webhooks                  # Create webhook subscription
GET    /api/templates                 # List agent templates
POST   /api/assistant/chat            # AI assistant query
```

### Auth & Admin
```
POST   /api/auth/signup               # Create account
POST   /api/auth/login                # Sign in
GET    /api/auth/me                   # Current user
GET    /api/admin/users               # List users (admin)
GET    /api/admin/stats               # System statistics
POST   /api/keys                      # Create API key
```

## Deployment

### Docker Compose (recommended)

The included `docker-compose.yml` starts Cortex + PostgreSQL 16:

```bash
docker compose up -d
```

### Production Checklist

- [ ] Set `SECRET_KEY` to a strong random value
- [ ] Configure at least one LLM provider API key
- [ ] Set up OAuth credentials for SSO (optional)
- [ ] Run behind a reverse proxy (nginx/Caddy) with TLS
- [ ] Set `DATABASE_URL` to a managed PostgreSQL instance
- [ ] Enable backups for the PostgreSQL database

### Kubernetes

Cortex is a single stateless container — deploy with a standard Deployment + Service + Ingress. Point `DATABASE_URL` at your managed Postgres.

## Design Principles

1. **Cortex does not own customer data** — it controls access only
2. **API keys are saved but owned by the user/company** — Cortex is a passthrough
3. **Lightweight and easy to adopt** — single binary, Docker-ready, no complex setup
4. **Human stays in the loop** — config changes require explicit approval
5. **Provider-agnostic** — no vendor lock-in at any layer
6. **Tamper-evident** — hash-chained attestation for every action

## Contributing

PRs are welcome. Please:

1. Fork the repo
2. Create a feature branch
3. Make your changes
4. Run the test suite
5. Submit a PR with a clear description

## License

Business Source License 1.1, converting to Apache License 2.0 on the Change
Date specified in [LICENSE](LICENSE).

---

<p align="center">
  <strong>CORTEX</strong> — The control plane that safely connects AI agents to the real world.
</p>
