# Cortex Experience — product vision (2026-11-19)

**Cortex is the intelligence layer for every agent.** Build agents that remember, learn, and improve without moving your application into a proprietary runtime.

This vision extends the existing **Cortex Agent Ops Hub**; it does not discard the operational control plane, policy engine, provenance, human approvals, deployments, or existing model integrations.

## User promise

A developer adds Cortex in minutes, sends existing execution history or saves a correction, checks what an agent learned, verifies the knowledge, compares the resulting outcomes, and exports approved examples for separate training. The model provider and application remain theirs.

Cortex deliberately distinguishes:
- **Experience:** events, prompts, tool calls, results, corrections, and observed outcomes.
- **Memory:** scoped and retrievable knowledge, preferences, and reusable patterns.
- **Verified skill:** a versioned, portable procedure with source evidence and held-out evaluation results (future milestone).
- **Training data:** permissioned, curated examples that may be exported for pre-/post-training. The v1 export is post-training oriented and is not a model training service.
- **Learning:** evidence that a version improves on relevant future work. Merely retaining text is not learning.

## Developer-first principles

1. **API first.** Every dashboard action maps to a documented API; the SDK is a convenience, not a required runtime.
2. **No lock-in.** Connect Claude, OpenAI, Gemini, open-weight models, custom agents and other frameworks as adapters mature. Do not claim integrations before tested.
3. **Interoperable by design.** JSON REST v1; Python and TypeScript clients; OpenTelemetry for traces, MCP for tools, and A2A for inter-agent workflows where appropriate. These standards address distinct problems.
4. **Clean, warm experience.** Inspired by Stripe's practical discoverability and Claude's visual calm; emphasize readable examples, strong defaults, fast errors, and honest empty states.
5. **Privacy-first.** Owner isolation, opt-in capture for sensitive data, manual verification, explicit per-memory training export consent, delete/export controls, and no default training on customer content.
6. **Measure honestly.** Completed operations are not necessarily correct. Compare outcomes with suitable baselines; require evidence before labeling a learned skill better.
7. **Fair usage.** Surface units, idempotency, estimated LLM and infrastructure spend, and budget controls. Diagnostic meters must not be presented as billable invoices.
8. **Composable.** Self-hosted and managed offerings should share API semantics; customers retain their agent runtime.

## Market thesis

Model providers get better at generating text and using tools, but applications still need portable expertise, evidence provenance, task-based evaluations, training eligibility, and a vendor-neutral learning record. This can become a durable developer platform if verified skills actually improve application outcomes.

## The initial wedge

Start with developer agents, where correctness can be tested via unit tests, build outcomes, and reproducible workflows. Focus the proof on capture -> human feedback -> candidate skill -> held-out evaluation -> safe promotion. Healthcare/claims is a plausible later vertical, not the default v1 workload.

## v1: what is implemented in this branch

- A responsive Experience console at /experience that uses the authenticated existing Cortex session.
- Owner-scoped persistent knowledge, preferences, corrections, and skill drafts.
- Lexical memory search, manual verification, explicit training-export opt-in, and deletion.
- Idempotency keys for memory creation and an informational usage counter.
- Feedback for existing runs, owned-agent run-version comparisons, and JSONL export of approved memories.
- Tests for authentication, tenancy, scopes, version comparison and export eligibility.

**Not yet implemented:** automated skill extraction, embeddings/semantic memory, actual model fine-tuning, published SDK packages, Stripe billing, trustworthy invoice-grade metering, managed cloud onboarding, semantic multilingual search, agent-to-agent learning, or production authorization for external enterprise data.

## Out of scope / safety

Cortex must never silently use user content or proprietary traces to train a model. Do not ingest secrets, patient data, customer data, or personal information without a documented retention policy, provider agreements, and relevant safeguards. Do not automatically mark agent outputs as verified.

## Success measures for a YC application

Evidence, not speculative valuation:
- 10–15 engineering teams recruited for a concrete pilot.
- At least 3 pay or sign a credible paid pilot after trying the product.
- Time to first successful memory API call under 5 minutes for a new developer.
- Measured improvement versus same-model baseline on held-out tasks; no cherry-picked wins.
- Repeat weekly usage and at least one team adopting Cortex in an actual development workflow.

See [v1 developer quickstart](./QUICKSTART.md) and [launch gates](./LAUNCH_GATES.md).
