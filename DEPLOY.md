# CORTEX — Deployment Guide

## Quick Start (Docker)

```bash
# 1. Clone and configure
git clone https://github.com/YOUR_USER/CortexUpdated.git
cd CortexUpdated
cp .env.example .env
# Edit .env with your values (especially SECRET_KEY and DB_PASSWORD)

# 2. Start everything
docker compose up -d

# 3. Open CORTEX
open http://localhost:3000
```

That's it. Docker Compose starts PostgreSQL and the CORTEX app together. The database is created automatically on first boot.

---

## Configuration

### Required

| Variable | Description |
|---|---|
| `SECRET_KEY` | Session signing key. Generate with `python3 -c "import secrets; print(secrets.token_hex(32))"` |
| `DB_PASSWORD` | PostgreSQL password |

### OAuth (Optional)

**Google:**
1. Go to [Google Cloud Console](https://console.cloud.google.com/apis/credentials)
2. Create OAuth 2.0 Client ID → Web application
3. Add redirect URI: `https://your-domain.com/api/auth/callback/google`
4. Set `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` in `.env`

**GitHub:**
1. Go to [GitHub Developer Settings](https://github.com/settings/developers)
2. New OAuth App → set callback: `https://your-domain.com/api/auth/callback/github`
3. Set `GITHUB_CLIENT_ID` and `GITHUB_CLIENT_SECRET` in `.env`

### LLM Provider Keys

Set any of these in `.env` or through the Settings UI:

| Variable | Provider |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic (Claude) |
| `OPENAI_API_KEY` | OpenAI (GPT) |
| `GEMINI_API_KEY` | Google (Gemini) |
| `XAI_API_KEY` | xAI (Grok) |
| `PERPLEXITY_API_KEY` | Perplexity (Sonar) |
| `MISTRAL_API_KEY` | Mistral AI |
| `COHERE_API_KEY` | Cohere (Command) |
| `TOGETHER_API_KEY` | Meta/Llama (via Together) |

---

## Cloud Deployment

### Railway

1. Push to GitHub
2. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub
3. Add a PostgreSQL plugin
4. Set environment variables (Railway auto-sets `DATABASE_URL`)
5. Done — Railway builds from the Dockerfile automatically

### Render

1. Push to GitHub
2. [render.com](https://render.com) → New Web Service → Connect repo
3. Add a PostgreSQL database
4. Set environment variables
5. Deploy

### Fly.io

```bash
flyctl launch                    # auto-detects Dockerfile
flyctl postgres create           # managed Postgres
flyctl secrets set SECRET_KEY=... DB_PASSWORD=... ANTHROPIC_API_KEY=...
flyctl deploy
```

### DigitalOcean / AWS / Any VPS

```bash
# On your server
git clone https://github.com/YOUR_USER/CortexUpdated.git
cd CortexUpdated
cp .env.example .env
# Edit .env
docker compose up -d

# Add HTTPS with Caddy (automatic SSL):
# apt install caddy
# caddy reverse-proxy --from your-domain.com --to localhost:3000
```

---

## Local Development (without Docker)

```bash
# Install PostgreSQL locally, then:
pip install -r requirements.txt
export DATABASE_URL=postgresql://user:pass@localhost:5432/cortex
export SECRET_KEY=dev-secret
uvicorn cortex:app --reload --port 3000
```

---

## Architecture

```
cortex.py          — FastAPI app + embedded dashboard UI
providers.py       — Multi-provider LLM engine (8 providers)
db.py              — SQLAlchemy models (PostgreSQL)
auth.py            — Authentication (email/password + OAuth)
cortex_mcp_server.py — MCP server for IDE integration
Dockerfile         — Container image
docker-compose.yml — Full stack (app + Postgres)
```

## Updating

```bash
git pull
docker compose build
docker compose up -d
```

Database migrations are automatic — new tables are created on startup.
