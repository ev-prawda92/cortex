# Governed Action Protocol (GAP)

**Version:** 0.1.0-draft · **Date:** 2026-09-25  
**Author:** Evan Alexander Prawda  
**Status:** Experimental design proposal for review. No implementation or security certification is claimed.

GAP describes how an AI agent can request permission for one precisely specified action, how a service checks that permission before execution, and how it records the result. It is an application-layer profile over HTTPS, using existing identity and signature mechanisms. It is not a replacement for TLS, OAuth, MCP, or A2A.

The proposed boundary is deliberately small: **one tenant, one authority, one executor, one exact action per permit**. Broad capabilities, autonomous delegation, and cross-organization federation are outside v0.1.

## Read the proposal

- [Specification](SPEC.md): normative message semantics, trust boundaries, authorization, approval, revocation, execution, and receipts.
- [Message schema](schemas/gap-v0.1.schema.json): JSON Schema for the draft payloads. Structural validity does not establish authorization or security.
- [Examples](examples/): unsigned, synthetic message bodies. They are not usable credentials.
- [Conformance plan](CONFORMANCE.md): adversarial and fault-injection requirements for future implementations.
- [Related work](RELATED-WORK.md): overlap with existing standards and agent governance proposals.
- [Research note](RESEARCH-NOTE.md): initial scholarly framing and evaluation design; no experimental results.
- [Publication plan](PUBLICATION.md): GitHub review and a possible later arXiv submission.

## Concrete example

An agent proposes a USD 40.00 refund for a particular order, to its original payment instrument. An authorization service checks policy and any required human approval. It issues a short-lived signed permit bound to the complete proposed action and the agent's TLS client certificate. The executor validates the permit and atomically redeems it with the authority before dispatching the refund. A retry retrieves the same operation; it does not initiate another refund.

This depends on effective credential isolation and downstream idempotency. A signed permit alone cannot prevent an agent from using an unprotected alternative API.

## Relationship to Cortex and Arbiter

Cortex is a candidate authorization/enforcement host. Arbiter is a candidate evidence/decision-record integration. These are prospective integration points, not claims that either product implements GAP. The proposal is designed to be implementable independently of both products.

## Validation and maturity

Run `python validate_examples.py` in an environment with `jsonschema` installed to check illustrative payloads against the schema and check the included action digest. This is document/fixture validation, not a protocol implementation or a security conformance suite. See `CONFORMANCE.md` for the unimplemented behavioral tests.

No performance measurements, attack-resistance results, deployment claims, or novelty claims are made. Similar protocols already exist; the contribution to investigate is whether this limited profile improves interoperable enforcement and explicit recovery semantics.

## Publication and rights

This proposal is initially published in a review branch of Cortex. The repository currently contains both `LICENSE` and `License` with different terms. This proposal does not amend either file, grant new implementation or patent rights, or describe the repository as open source. A separately licensed specification and contribution policy require an explicit author decision before a standards/community release.

AI tools assisted with drafting and consistency checks. The named human author must review the technical content and accepts authorship only through their own approval; this draft does not assert that such review has occurred.
