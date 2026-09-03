# Production Readiness and Enterprise Acquisition Gates

Cortex has a hardened production baseline, but production readiness is a
continuous operating property rather than a release label. This document is
the acceptance checklist for customer pilots and enterprise diligence.

## Implemented baseline

- Production startup fails on weak secrets, SQLite, insecure public URLs,
  wildcard origins/hosts, sample data, open signup, or fail-open authorization.
- All private API routes authenticate at a central middleware boundary.
- Agent routes enforce ownership or explicit agent/global RBAC grants.
- Consequence-aware authorization defaults to block, records decisions, and
  supports human review with expiring, single-use approvals.
- Password hashes use PBKDF2-SHA256 with 600,000 iterations while retaining
  compatibility with existing hashes; disabled users lose access immediately.
- Stored integration credentials use Fernet encryption with a required
  production key.
- PostgreSQL migrations are idempotent and exercised in CI; liveness and
  dependency-aware readiness endpoints support orchestration.
- Container configuration uses explicit proxy trust and secure production
  defaults. API responses include baseline browser security headers.

## Required before an external production pilot

- Replace local email/OAuth identity with customer SSO (OIDC or SAML), enforced
  MFA, SCIM provisioning, and group-to-role mapping.
- Add a first-class organization/workspace foreign key to every tenant-owned
  record and database-level tenant enforcement. Owner-based isolation is a
  safety boundary, not the final multi-tenant architecture.
- Move secrets to a managed KMS/secrets manager, implement envelope encryption,
  key rotation, and credential revocation procedures.
- Add rate limiting, abuse controls, idempotency enforcement, request-size
  limits, and outbound network allowlists/SSRF defenses.
- Separate the web API, scheduler, and execution workers; use a durable queue,
  leases, retries, dead-letter handling, and horizontal worker concurrency.
- Replace in-process metrics/events with OpenTelemetry, centralized logs,
  immutable audit export, alerts, SLOs, and customer-visible incident status.
- Test PostgreSQL backup/restore, point-in-time recovery, regional recovery,
  schema rollback, and zero-downtime deployment procedures.
- Complete dependency, container, SAST, DAST, secret-scanning, SBOM, and
  penetration testing; remediate all critical/high findings.
- Define data classification, retention/deletion, residency, subprocessors,
  incident response, vulnerability disclosure, access reviews, and support.
- For healthcare workloads, execute the HIPAA risk analysis and validate every
  administrative, physical, and technical safeguard before processing PHI.

## Pilot exit criteria

1. One narrow workflow has a named customer owner, documented authority model,
   measurable baseline, and rollback path.
2. No critical/high security findings remain open; disaster recovery and
   approval-bypass exercises pass with retained evidence.
3. A 30-day shadow deployment meets agreed availability, latency, authorization
   correctness, and escalation targets without autonomous external writes.
4. A limited production phase uses capped scope, human review, kill switches,
   and weekly joint risk review.
5. General availability begins only after the customer accepts the control
   evidence and both parties approve the expanded authority profile.

## Acquisition-grade diligence package

- Clean IP chain of title, dependency/license inventory, contributor records,
  trademarks/domains, and assignment agreements.
- Architecture, threat model, data-flow diagrams, security controls, test
  evidence, incident history, and remediation register.
- Reproducible builds, signed releases, release notes, upgrade/rollback policy,
  and supported-version matrix.
- Design-partner agreements, referenceable deployment evidence, retention and
  expansion metrics, unit economics, and a defensible integration thesis.
- SOC 2 Type I readiness before broad pilots and a funded path to Type II;
  customer-specific requirements may also include ISO 27001, HIPAA, or GDPR.

The highest-value product position is an authorization and control plane that
sits between enterprise agents and consequential tools: identity and context
in, deterministic allow/review/block decisions out, with evidence for every
decision. The dashboard is useful, but the durable acquisition asset is the
policy engine, enforcement gateway, audit record, and integration surface.
