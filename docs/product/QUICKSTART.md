# Cortex Experience v1 — developer quickstart

This is an **additive developer preview** of Cortex's memory and learning APIs. It is not yet an independently audited, production SaaS service.

## Start locally

Run from the root of ev-prawda92/cortex:

    git clone https://github.com/ev-prawda92/cortex.git
    cd cortex
    python -m pip install -r requirements.txt
    python cortex.py

For the feature branch preview use:

    git checkout feat/experience-v1

Open http://localhost:3000 and sign in using the existing Cortex UI. Then go to:

    http://localhost:3000/experience

A local setup can use SQLite; production requires PostgreSQL and the configuration described in the existing production-readiness document. Do not use sample credentials or production data in this preview.

## Interact with the console

1. Open **Memory** and create a draft such as "Use typed request models" with a short explanation.
2. Search for it; you are searching text, not embeddings.
3. Select **Verify** and supply a real note explaining why it is trusted. Training export is a separate opt-in.
4. Open **Datasets** to preview the count and explicitly export eligible JSONL.
5. Open **Learning**, choose one of your owned agents, and inspect its run history. Until runs from multiple config versions exist, the verdict should say insufficient_data.
6. Open **Usage** to see diagnostic operation counts and confirm no charges are generated.

## API quickstart

Create a scoped key with the existing Cortex /api/keys endpoint or UI. Recommended scopes are:
- experience:read — GET summary, memories, agents, performance, dataset preview, usage
- experience:write — create/review/delete memories and add run feedback
- experience:export — export explicitly eligible JSONL training examples

JWT sessions can also access these APIs. Keys must not be committed to Git.

Create a memory:

    curl -X POST http://localhost:3000/api/v1/experience/memories \
      -H "Authorization: Bearer ctx_YOUR_API_KEY" \
      -H "Content-Type: application/json" \
      -H "Idempotency-Key: first-memory-001" \
      -d '{"title":"Prefer typed FastAPI APIs","kind":"preference","content":"Use Pydantic request and response models.","tags":["python","api"]}'

Review:

    curl -X PATCH http://localhost:3000/api/v1/experience/memories/MEMORY_ID \
      -H "Authorization: Bearer ctx_YOUR_API_KEY" \
      -H "Content-Type: application/json" \
      -d '{"status":"verified","verification_note":"Confirmed with the project's API conventions and tests.","training_opt_in":false}'

Read:

    curl -H "Authorization: Bearer ctx_YOUR_API_KEY" \
      "http://localhost:3000/api/v1/experience/memories?q=FastAPI"

Performance:

    curl -H "Authorization: Bearer ctx_YOUR_API_KEY" \
      "http://localhost:3000/api/v1/experience/agents/AGENT_ID/performance"

The FastAPI-generated OpenAPI explorer is at http://localhost:3000/docs. The new routes use the /api/v1/experience prefix.

## API contract in this preview

| Method | Path | Scope |
| --- | --- | --- |
| GET | /api/v1/experience/summary | experience:read |
| GET | /api/v1/experience/agents | experience:read |
| POST | /api/v1/experience/memories | experience:write |
| GET | /api/v1/experience/memories | experience:read |
| PATCH | /api/v1/experience/memories/{id} | experience:write |
| DELETE | /api/v1/experience/memories/{id} | experience:write |
| POST | /api/v1/experience/feedback | experience:write |
| GET | /api/v1/experience/agents/{agent_id}/performance | experience:read |
| GET | /api/v1/experience/datasets/preview | experience:read |
| GET | /api/v1/experience/datasets/export | experience:export |
| GET | /api/v1/experience/usage | experience:read |

A memory stays in draft until explicitly verified. Only verified memories with training_opt_in=true are eligible for the JSONL export. The export does not initiate training or transmit data to a provider.

## Integration with existing Cortex

The Experience API uses the existing SQLAlchemy Base, Agent/Run tables, authentication middleware, ownership/RBAC checks and database sessions. The only change to the large cortex.py module is the Experience router registration. Three additive tables are created on startup; no existing agent or run columns are modified. The version-comparison behavior was adapted from ev-prawda92/cortexlearning.

## Operational limitations

- No semantic vector retrieval; exact text matching only.
- Current owner-scoped isolation is not organization/workspace tenancy.
- Cross-version completion thresholds are heuristic and do not establish statistical significance or real task correctness.
- The diagnostic meter does not price or bill operations.
- Schema changes, OpenAPI review, security audits, and load tests must pass before external production deployment.
