# YC-ready versus production-ready — objective gates

**Product status:** Feature-branch developer preview. Not a claim of enterprise production readiness. Applying to YC requires an honest demonstration, user evidence, and a clear ambition; an audited production platform is a separate milestone.

## Gates for a shareable developer demo

- [ ] GitHub PR review and existing unittest + Python compile + Docker CI green
- [ ] Manual local smoke test: signup/login -> /experience -> create -> verify -> export -> delete
- [ ] Exercise both JWT-cookie access and scoped API keys
- [ ] Test tenant isolation with two users and a second user's agent
- [ ] Test PostgreSQL-backed startup and recovery; don't rely on SQLite-only tests
- [ ] Remove inaccurate / unimplemented claims from README and public product pages
- [ ] Ensure no secrets, sample personal data, or customer content in logs and screenshots
- [ ] Record five-minute first API call and UX usability feedback from developers

## Gates before external production data

- [ ] Durable database migration and rollback for all new tables
- [ ] Org/workspace tenancy with authorization at query and database layers
- [ ] Per-tenant retention/deletion policy, data export, backup restore and user data inventory
- [ ] Strong API key scope creation validation, key rotation and active-user enforcement
- [ ] CSRF analysis for cookie-authenticated write endpoints; token-origins and SameSite policies
- [ ] Rate limits, body-size caps, concurrency tests, and DB query pagination/aggregation
- [ ] Security testing, abuse/SSRF review, SAST, dependency audit and secrets scanning
- [ ] TLS, observability, redaction, trace sampling, alerting and SLOs
- [ ] Encrypted storage, audit trails and documented subprocessors; no PHI without HIPAA controls
- [ ] Idempotent revenue-grade usage ledger, pricing versioning, invoice reconciliation, budgets and billing integration
- [ ] Schema versioning and backwards-compatible API deprecation policy
- [ ] Data ownership, model-provider terms and rights-of-use validation for any training corpus
- [ ] Human and independent automated evals prior to promoting skills or running fine-tunes

## Experiments that validate the thesis

1. **Activation:** new developer saves and searches a memory in <5 minutes using the API.
2. **Retention:** three design partners adopt it in a real workflow for four weeks.
3. **Quality:** compare tasks with/without candidate knowledge using the same model and a held-out test set. Report both positive and negative results.
4. **Governance:** unverified memories never reach a training export; owner B cannot read owner A's data.
5. **Economics:** measure actual storage, read/search load, and evaluation costs before proposing rates.

## YC application evidence packet

Product demo recording, honest shipping status, GitHub architecture, technical differentiation, named design partners with outcomes, pilot revenue (if any), competitive comparison, and a focused future roadmap. Do not claim a billion-dollar valuation is probable or guaranteed.

See also the existing security baseline and roadmap at ../production-readiness.md.
