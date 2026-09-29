# GAP v0.1 conformance and evaluation plan

**Status: required future tests, not executed implementation results.** The example validator checks structure and a digest only. No GAP server is included in this proposal.

Each future behavioral test must record the profile/version, fixture digest, implementation commit, execution trace, admission record, observed downstream effects, and signed receipts. Tests must assert downstream state, not merely HTTP responses. Use synthetic accounts and deterministic fault injection.

| ID | Scenario | Required observable result |
|---|---|---|
| C01 | Valid exact action, authorized agent, no review required | One durable admission and one confirmed effect |
| C02 | Policy denies action | No permit and zero effects |
| C03 | Review required but absent | REVIEW, no executable permit, zero effects |
| C04 | Valid authorized reviewer approves exact action | Fresh PERMIT; approval record bound to action/policy/context |
| C05 | Change amount, order, tenant, audience, context, or identity after signing | Reject before admission; zero effects |
| C06 | Bad signature, unknown issuer/key, wrong algorithm, wrong token type | Reject; no remote key lookup driven by token |
| C07 | Copied permit used with a different certificate | Reject even if both workloads are otherwise valid |
| C08 | Permit used before nbf or at/after exp, including clock uncertainty | Reject; test exact boundaries |
| C09 | 100 simultaneous exact retries | At most one business effect; all responses refer to the same operation |
| C10 | Same operation ID with different action | Conflict; original operation unchanged |
| C11 | Multiple permits issued for the same operation | At most one business effect across all permits |
| C12 | Revocation commits before admission | Reject admission; zero effects |
| C13 | Admission commits before revocation | ALREADY_ADMITTED; no false cancellation promise |
| C14 | Partition authority/revocation store before admission | Fail closed; zero effects |
| C15 | Crash after durable admission before dispatch | Recover same operation/key; at most one effect |
| C16 | Crash after downstream commit before local success persistence | UNKNOWN/reconcile, then SUCCEEDED; no second effect |
| C17 | Lost admission response | Query original operation; no new admission identity |
| C18 | Downstream timeout whose effect is unknown | UNKNOWN, never assume FAILED or use a fresh key |
| C19 | Policy/context changes after approval or permit issuance | No new admission under stale policy/context |
| C20 | Resource revision changes between admission and effect | Conditional effect rejects; no stale-state mutation |
| C21 | Agent fabricates approval or uses reviewer without rights | Reject approval; zero unauthorized effects |
| C22 | Agent delegates/forwards permit to another agent | Reject; core forbids delegation |
| C23 | Agent attempts direct downstream call or privileged local bypass | Deployment blocks access; otherwise no conformance claim |
| C24 | Cross-tenant decision/status/revocation lookup | No data or resource-existence disclosure |
| C25 | Duplicate keys, invalid Unicode, noncanonical signed payload, unknown fields, huge/deep JSON | Reject deterministically within resource limits |
| C26 | Receipt altered, reordered, or linked to wrong operation | Signature/link/semantic check fails |
| C27 | Terminal operation retried after permit expiry | Authorized status retrieval; no new effect |
| C28 | Key rotated/revoked before admission | Reject revoked/untrusted signing key; preserve historical receipt verification context |
| C29 | Repeated fresh operation IDs target same business refund | Adapter enforces remaining balance and business duplication limits |
| C30 | Service resumes after retry/retention window | Explicit expired-operation handling; never treat old retry as new |
| C31 | Receipt store fails after effect | Recover original result/receipt; no fresh execution |
| C32 | One client changes JSON key order only | Same action digest; no semantic change |

## Proposed experiment

Implement one synthetic refund ledger with transactional idempotency and two independently written clients/adapters. Compare: (A) resource-scoped OAuth-style authorization without exact-action permits; (B) exact-action authorization with correct idempotency but without GAP's linked receipt/review contract; (C) this GAP profile. Do not deliberately misconfigure baselines.

Run each applicable case across clients, executor replicas, and injected restart/partition schedules. Report invalid-effect count, valid-action success rate, duplicate-effect count, p50/p95/p99 added latency, review invalidation correctness, reconciliation time, and behavior under unavailable dependencies. Publish hardware, workload, repetitions, seeds, and uncertainty intervals. A finite suite with zero failures is not proof against all attacks.

## Release evidence checklist

- Independently review schema, signature handling, mTLS identity, trust configuration, and action canonicalization.
- Pin adapter semantics and publish immutable fixtures, including actual signed vectors with test-only keys.
- Validate every C01–C32 case against observed durable side effects.
- Test at least two implementations before claiming interoperability.
- Document which security properties rely on infrastructure assumptions.
- Publish failures and unsupported cases alongside successful results.
