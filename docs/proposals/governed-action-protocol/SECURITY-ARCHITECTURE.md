# Security architecture for the proposed international framework

Concept draft 0.1 · 2026-09-25 · Companion to [FRAMEWORK.md](FRAMEWORK.md)

**Status:** architecture and extension sketch. Only [SPEC.md](SPEC.md) defines the existing experimental GAP v0.1 profile. This document does not silently add federation, crypto transactions, delegation, or new wire messages to that profile. No runtime or security audit is claimed.

## 1. Protect separate kinds of authority

The system separates institutional authority to publish requirements, organizational authority to configure policy, a person's authority to delegate an action, and an executor's ability to perform it. Compromising one role must not automatically grant the others.

| Boundary | Protected asset | Proposed control |
|---|---|---|
| Public submission to policy process | Legitimate deliberation | Provenance, moderation appeals, conflict disclosures, protected incident reporting |
| Adopted requirement to release | Integrity of a policy version | Multiple authorized approvals, signed manifest, public history, rollback protection |
| Release to local deployment | Correct interpretation and applicability | Human-reviewed mapping, pinned versions, tests, explicit effective dates |
| Agent to authority | Identity and scope of proposed action | Authentication, tenant binding, typed action, input limits |
| Authority to executor | Permission for one exact operation | Short-lived signed permit bound to identity, action, audience, and context |
| Executor to resource | Prevention of bypass and duplicate effects | Credential isolation, resource checks, durable admission and idempotency |
| Evidence to reviewer | Integrity, confidentiality, and fair interpretation | Signed records, access controls, minimized disclosure, independent corroboration |

## 2. Proposed requirement-release protocol

A future release manifest would carry: requirement ID and version; adopted-text digest; scope and domain; adoption decision reference; test-suite digest; publication, activation, review, and expiry dates; predecessor digest; implementation compatibility; and signatures from the mandated release roles.

Illustrative fields, **not a wire schema or registered format**:

```json
{
  "kind": "proposed-requirement-release",
  "requirement_id": "example-human-approval",
  "version": "0.1-draft",
  "adoption_reference": "NOT-ADOPTED",
  "scope": ["example-consequential-transfer"],
  "text_digest": "PLACEHOLDER",
  "test_suite_digest": "PLACEHOLDER",
  "effective_at": null,
  "predecessor_digest": null,
  "release_signatures": []
}
```

This example is intentionally invalid for deployment. Final encoding, signature algorithms, quorum, identity roots, expiry, key recovery, and verification rules need a separate specification and review. Reuse established signature and secure-update mechanisms; do not invent cryptography.

A signature authenticates a release issuer and content; it does not demonstrate legitimate adoption. Verifiers must also confirm the governance decision, signer roles, and release process. Releases require independent review and staged rollout. A compromised publisher must not silently roll deployments back to a weaker version. Recovery needs previously configured independent roles and an auditable procedure; ordinary agent credentials cannot perform it.

The UN-level release key would authorize a requirement release only. It would not sign individual payment permits or supply a master decryption or shutdown key. Policy adoption and transaction authorization are distinct namespaces, keys, and verification paths.

## 3. Policy composition at the deployment boundary

An operator determines which adopted requirements apply to a declared system, domain, jurisdiction, and activity. It maps those requirements to local controls alongside user grants, organizational limits, and resource-owner constraints. The mapping, policy bundle, adapter semantics, and test version are pinned and reviewable.

Permission to act requires all applicable mandatory checks and required approvals to pass. Unknown applicability, missing evidence, or a genuine conflict produces a scoped hold or review, not a fabricated assertion of compliance. Broader rights or legal conflicts are escalated to the responsible human institution. Local configuration cannot label itself compliant while silently disabling a required control.

This does not imply the entire AI service stops for every uncertain issue. The affected action is constrained; ordinary unprivileged assistance can continue when safe and within policy. Emergency exceptions, if adopted, need specific authority, narrow scope, expiry, logging, and review; the model cannot declare its own emergency.

## 4. Runtime: the existing GAP profile

GAP v0.1 already specifies a proposed narrow sequence:

1. Authenticate the agent using mTLS and map it to trusted tenant/principal records.
2. Receive a typed exact action with operation ID, audience, resource, parameters, and trusted context digest.
3. Check permissions, resource ownership, current policy, context, and any human approval.
4. Issue PERMIT, DENY, or REVIEW. Only PERMIT carries an executable signed permit.
5. Bind that permit to the complete action digest, client certificate, issuer, executor audience, policy/context, and at most five minutes of validity. Delegation is disabled.
6. At the protected executor, independently verify the request and redeem it through a durable, serialized authority admission transaction.
7. Dispatch with a stable idempotency key and resource precondition checks; record the actual outcome.
8. Issue signed receipts. If execution is uncertain, retain UNKNOWN and reconcile using the original operation ID.

It uses HTTPS, canonical JSON and SHA-256 action digests, compact JWS with ES256, trusted issuer keys, and a durable admission/revocation store. Detailed requirements, limits, and message schemas are in SPEC.md. These are specified behaviors, not evidence of an implemented system.

Revocation that commits before admission blocks the action. Revocation after admission cannot promise cancellation. That limitation is particularly important for delayed actions and irreversible effects; a future execution-time authorization or cancellation profile would need to define its own consistency boundary.

## 5. Prevent bypass in real deployments

Agents must not have independent access to production service credentials, unrestricted wallet keys, privileged shells, or alternative write endpoints for protected resources. Executors hold narrowly scoped credentials and validate every relevant mutation. Egress policy, service identity, sandboxing, and downstream authorization enforce the architecture beyond the model's prompt.

The deployment inventories all mutation paths, including batch tools, browser automation, background jobs, plugins, recovery endpoints, and administrator impersonation. An integration that protects one API but leaves an equivalent path open cannot claim complete enforcement. Access to read sensitive data also needs controls; blocking writes alone does not prevent disclosure.

Human reviewers must authenticate outside the agent and see the material action. Review interfaces must avoid hiding changed recipients, amounts, dependencies, or data disclosures. Changing the action, relevant context, or policy invalidates approval. High-consequence profiles may require independent approvers and stronger authentication, but those requirements need explicit domain specifications.

## 6. Cross-organization federation: proposed extension

For a future buyer-agent/merchant interaction, the merchant would accept only explicitly trusted issuers and scopes. The merchant remains responsible for local resource policy and transaction acceptance. A buyer's authority cannot grant rights to the merchant's internal systems.

Federation requires identity mapping, audience restrictions, issuer onboarding, key discovery through trusted configuration, revocation, responsibility allocation, and refusal semantics. A national delegation or international registry entry must not automatically be trusted to authorize every transaction. Avoid transitive global trust: acceptance is scoped by issuer, action class, tenant relationship, and verification profile.

Future delegated authority would require mechanically checkable narrowing, bounded depth, parent revocation behavior, and protection against confused-deputy attacks. GAP v0.1 rejects delegated permits; these are design requirements for a later version, not current capabilities.

## 7. Economic and blockchain profiles: proposed extension

For commerce, distinguish permission to negotiate, acceptance of contract terms, payment authorization, settlement, delivery, and cancellation. Each is a separate state and may involve different parties. Signing a receipt does not settle a payment or establish a universally enforceable contract. Existing rails and dispute mechanisms remain necessary.

For an eventual blockchain profile, bind chain ID, wallet, token/contract address, exact call data, recipient, integer amount, nonce, deadline, and transaction-cost limits. A swap additionally needs minimum output and destination constraints. The wallet contract or restricted signer must enforce the authorization; an agent with an unrestricted key can bypass it.

Define submitted, included, reverted, finalized, dropped/replaced, and uncertain states with chain-specific finality/reorganization handling. Never treat a broadcast response as final settlement. Permission expiry off-chain does not automatically invalidate a transaction already signed or broadcast; deadline and revocation behavior must be enforced in the execution mechanism. Cross-chain actions introduce additional trust and recovery assumptions.

Ethereum account abstraction provides existing custom validation mechanisms; typed signatures require explicit replay defenses. Draft ERC-8273 addresses transaction-scoped agent attestations and is relevant prior work, not an adopted GAP dependency. [T1–T3] Any initial experiment should use synthetic assets on a local chain or testnet, not real funds.

## 8. Threats, controls, and residual risks

| Threat | Control to specify and test | Remaining limit |
|---|---|---|
| Prompt injection causes an unauthorized action | Treat model proposals as untrusted; external authorization and credential isolation | Harm within legitimately granted scope remains possible |
| Stolen or modified permit | Possession binding, signature checks, complete action digest, issuer/audience checks | Compromised authorized keys/services require separate response |
| Replay or retry storm | Atomic admission, operation identity, stable downstream idempotency | Fresh IDs still require business-level cumulative controls |
| Stale human approval | Bind approval to action, policy, context, reviewer rights, expiry | A duly authorized reviewer can still make a mistake |
| Cross-tenant or cross-issuer confusion | Explicit namespaces, trusted mappings, scoped queries, no arbitrary key fetch | Federation mapping bugs remain an implementation risk |
| Revocation/admission race | Defined linearization point and strong consistency | Already-admitted effects are not automatically reversed |
| Forged or selectively omitted audit | Signed receipts, independent checkpoints where adopted, downstream reconciliation | Signatures alone cannot prove completeness or truth |
| Policy capture or censorship | Diverse representation, conflicts/recusals, transparency, independent appeal | Political legitimacy is not secured by cryptography |
| Requirement-release key compromise | Multiple release roles, separate keys, rollback protection, recovery plan | Recovery and trust redistribution must be tested |
| Supply-chain compromise | Dependency controls, pinned artifacts, isolated build/release roles, review | Signed malicious code can still be malicious |
| Central service outage or denial of service | Quotas, bounded inputs, replicated consistent state, scoped failure | New admissions can lose availability; do not fail open |
| Excessive data collection | Minimized receipts, scoped access, retention policy, confidential reporting | Metadata can remain identifying |

## 9. Incident response and assurance

Separate detection, containment, investigation, recovery, and public accountability. Define who can suspend a workload, revoke an issuer key, stop new admissions for a resource, or withdraw an assurance claim. Emergency permissions must be scoped and logged. They do not include a global override of every participant's systems.

Preserve necessary evidence securely; notify affected parties and competent bodies through agreed channels and applicable obligations. Reports should distinguish confirmed effects from suspected or uncertain ones. Publish appropriate aggregate findings while protecting victims and active vulnerability details. Restore access after documented remediation and review rather than indefinite unexplained exclusion.

Assurance statements must identify the tested implementation, configuration, protocol/profile version, adapter, environment, date, assessor, and exceptions. They must expire or be reassessed when material components change. “Passed this conformance suite” is not “safe for every purpose.” Independent assessment requires independence from commercial implementation incentives and a means to challenge findings.

## 10. Implementation and evaluation package

First build a synthetic ledger, authority, executor, certificate/key fixtures, signed permits/receipts, and failure-injection harness. Run C01–C32 in CONFORMANCE.md against observed downstream state. Then add tests for release tampering/rollback, authorization role separation, context-to-policy mapping, assurance scope, and accessible appeals.

Publish valid-operation success, unauthorized effects, duplicate effects, false denials, review burden, tail latency, outage behavior, and reconciliation time. Include baseline configurations, failed tests, environments, and repeatable seeds. No such runtime experiment has been conducted for this architecture.

The present schema validator checks illustrative GAP payloads and their linked digest only. The new manifest above is not a schema-validated artifact, and neither framework document is executable policy.

## 11. Technical references

Existing security standards and related governance proposals: [RELATED-WORK.md](RELATED-WORK.md).

T1. [ERC-4337: Account Abstraction](https://eips.ethereum.org/EIPS/eip-4337).

T2. [EIP-712: Typed structured data hashing and signing](https://eips.ethereum.org/EIPS/eip-712). Replay prevention must be supplied by the application.

T3. [Draft ERC-8273: Attestation-Gated Agentic Actions](https://eips.ethereum.org/EIPS/eip-8273). Its transaction-scoped design must not be conflated with GAP's short-lived off-chain permits.

These references motivate design options, not a completed compatibility review. Institutional source references are in FRAMEWORK.md. AI-assisted concept drafting; human author and specialist review are pending.
