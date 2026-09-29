# Exact-Action Authorization and Recoverable Execution for AI Agents

**Evan Alexander Prawda**  
Working research note · 2026-09-25 · Companion to GAP v0.1.0-draft  
**Not submitted to arXiv. No experimental results.**

## Abstract

AI agents can translate untrusted instructions into actions against external systems. Authentication and permission to access an API do not, by themselves, specify the complete transaction a workload may execute or explain how authorization survives retries, policy changes, and uncertain execution outcomes. We propose an experimental application-layer profile connecting a canonical action description, a short-lived certificate-bound permit, serialized admission and revocation, and signed execution receipts. The profile deliberately limits its scope to one exact action per permit and excludes delegated authority chains. It defines a recovery contract in which uncertain downstream outcomes retain their original operation identity rather than triggering a new action. We describe a threat model, conditional security properties, and an adversarial evaluation plan. No implementation performance, interoperability, or safety result is reported. The proposed contribution remains subject to comparison with existing action-admission and behavioral-governance protocols.

## 1. Problem statement

Consider an assistant permitted to access a merchant's refund API. An organization may additionally require that a particular invocation refund a particular order, use the original payment instrument, remain within a reviewed amount, and execute at most once despite retries. An authorization decision may be valid when issued but stale when presented. An execution response may be lost after the refund succeeds. These problems combine authorization, distributed state, and evidence management.

GAP attempts to make the interfaces between those components explicit. The research question is whether a limited shared profile can make independent implementations agree on admission and recovery behavior. It is not whether cryptography can establish benevolent intent or eliminate all AI risks.

## 2. Design

The requesting workload submits a typed action A. Its digest d(A) commits to workload and principal identities, tenant, resource, executor audience, operation ID, typed parameters, and a trusted context snapshot. The authority evaluates current policy P and any required human review. If permitted, it signs a permit binding d(A), d(P), the workload certificate, and a bounded validity interval.

The executor validates the proposed action and permit, then consults a strongly consistent admission service. That service serializes revocation against admission and consumes the permit for the logical operation. Downstream effects require either a common transaction or durable idempotency and reconciliation. Receipts link the action and admission to the executor's reported outcome.

This construction uses existing signing, transport, canonicalization, and identity mechanisms. The new document's role is to choose a constrained composition and specify the boundary conditions. It does not supply new cryptography.

## 3. Conditional properties

The following are design objectives, not formal proofs:

**Action integrity.** If the signature is unforgeable, the hash is collision resistant, the executor is trusted, and canonicalization/adapter semantics agree, a permit for A cannot authorize a semantically different action B. This depends on including every material execution input, including trusted context, in the validation boundary.

**Possession binding.** A copied permit alone cannot authenticate a different workload when the executor verifies the actual mTLS certificate and the private key remains secret. A compromised authorized workload can still exercise its legitimate permissions.

**Revocation ordering.** For a single permit, let R be the durable revocation transaction and D the admission transaction. If R precedes D in the shared serialization order, D must fail. If D precedes R, revocation cannot promise cancellation. This property does not extend to an arbitrary delayed physical side effect after admission.

**Duplicate containment.** For one tenant/operation ID, a correct transactional or idempotent adapter should commit no more than one business effect. The guarantee fails if an alternate endpoint bypasses admission, the downstream idempotency window expires during recovery, or fresh operation IDs evade business policy. Those conditions must be tested and documented.

**Evidence attribution.** A valid receipt signature attributes a statement to a trusted executor key. It does not prove that a dishonest executor described reality correctly or disclosed all events. Cryptographic attribution and independent observation are different claims.

## 4. Prior work and positioning

OAuth rich authorization requests already support detailed transaction intent [1]. Certificate-bound credentials and JWS supply established binding and signature mechanisms [2–3]. MCP and A2A provide relevant integration and access-control foundations [4–5]. Agent Control Protocol directly proposes admission control for agents [6], while AgentBound describes behavioral governance and receipts [7]. MI9 discusses a broader runtime-governance architecture [8].

Consequently, this note does not claim to introduce governance protocols, action-specific permission, admission control, or signed receipts. A potential contribution would be a minimal interoperable profile with measured behavior under policy changes, partitions, and uncertain effects. That contribution is currently a hypothesis. A full prior-work review may instead support extending an existing protocol.

## 5. Evaluation design

Use a synthetic refund ledger and at least two independently implemented clients/executors. The planned conformance cases are listed in CONFORMANCE.md. Compare implementations with well-configured resource-scoped authorization and exact-action authorization baselines. Report both prevented invalid effects and incorrectly blocked valid actions.

Inject crashes before admission, after admission, after downstream commit, and before receipt persistence. Race revocation against admission with controlled schedules. Measure duplicate effects, policy/review invalidation, tenant isolation, added latency distributions, recovery time, and dependency-outage behavior. Publish fixtures, seeds, software commits, and the downstream state used as ground truth.

No sample sizes, timings, percentages, or success rates are reported because no protocol implementation has been evaluated. Validating example JSON is not a substitute for these experiments.

## 6. Limitations and research decisions

Mandatory online admission sacrifices availability during control-plane outages. Mandatory mTLS adds certificate lifecycle and proxy complexity. Exact-action permits may increase authorization traffic. Resource-specific adapters must make semantic preconditions explicit; the protocol cannot infer them reliably from natural-language descriptions. Excluding delegation reduces the initial attack surface but limits workflow expressiveness.

Approval can still be mistaken, policy can still permit harm, and an attacker controlling trusted services can falsify records. A finite adversarial suite does not establish comprehensive security. Formal modeling and independent security review would strengthen the work but are not completed here.

Before submission, decide whether the document is best positioned as a systems design proposal, an empirical interoperability study, or a profile contributed to another standard. Confirm author identity, affiliation if any, rights, citations, and any generative-AI disclosure required by the selected venue. No university affiliation or institutional endorsement is asserted.

## 7. References

1. [RFC 9396, OAuth 2.0 Rich Authorization Requests](https://www.rfc-editor.org/info/rfc9396/).
2. [RFC 8705, OAuth mTLS and Certificate-Bound Access Tokens](https://www.rfc-editor.org/info/rfc8705/).
3. [RFC 7515, JSON Web Signature](https://www.rfc-editor.org/info/rfc7515/).
4. [MCP authorization](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2026-07-28/basic/authorization/index.mdx).
5. [A2A specification](https://a2a-protocol.org/latest/specification/).
6. Fernandez. [Agent Control Protocol: Admission Control for Agent Actions](https://arxiv.org/abs/2603.18829), 2026.
7. Kaul, Lan, and Gupta. [AgentBound: Verifiable Behavioral Governance for Autonomous AI Agents](https://arxiv.org/abs/2606.30970), 2026. Full text not reviewed for this draft.
8. Wang, Singhal, Kelkar, and Tuo. [MI9: An Integrated Runtime Governance Framework for Agentic AI](https://arxiv.org/abs/2508.03858), 2025. Abstract-level comparison only.

## Drafting disclosure

AI tools assisted in developing the specification text, literature discovery, and structural consistency checks. This working note requires the named author's technical review and verification of references before scholarly submission. No AI system is listed as an author. No experimental results were generated or inferred from illustrative fixtures.
