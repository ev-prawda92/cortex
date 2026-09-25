# Foundations and related work

References checked 2026-09-25. This is a starting literature review, not an exhaustive novelty search. Standards below supply established mechanisms; papers describe their authors' proposals and are not treated as independently validated systems.

## Standards and protocol foundations

| Source | Relationship to GAP |
|---|---|
| [RFC 9396 — OAuth 2.0 Rich Authorization Requests](https://www.rfc-editor.org/info/rfc9396/) | Already expresses fine-grained authorization details, including transaction information. GAP must not claim to invent action-specific authorization. |
| [RFC 8705 — OAuth mTLS and certificate-bound access tokens](https://www.rfc-editor.org/info/rfc8705/) | Foundation for certificate binding. GAP reuses the certificate-thumbprint concept; a GAP permit is not itself a complete OAuth flow. |
| [RFC 9449 — DPoP](https://www.rfc-editor.org/info/rfc9449/) | Existing application-level proof-of-possession approach. A future alternative to the draft's mandatory mTLS would need a separate explicit binding profile. |
| [RFC 7515 — JSON Web Signature](https://www.rfc-editor.org/info/rfc7515/) | Existing signed envelope mechanism. GAP defines restricted message types and verification requirements. |
| [RFC 7518 — JSON Web Algorithms](https://www.rfc-editor.org/info/rfc7518/) | Defines ES256 and its JOSE signature encoding. |
| [RFC 8785 — JSON Canonicalization Scheme](https://www.rfc-editor.org/info/rfc8785/) | Deterministic action encoding before hashing; implementations should use a conforming library. |
| [RFC 9700 — OAuth security best current practice](https://www.rfc-editor.org/info/rfc9700/) | Security guidance for any surrounding OAuth integration. |
| [MCP authorization specification](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2026-07-28/basic/authorization/index.mdx) | Existing authorization framework for MCP access. GAP must complement its requirements, not bypass them or imply MCP lacks authorization. This main-branch URL can change; an implementation must pin its actual supported MCP release. |
| [A2A specification](https://a2a-protocol.org/latest/specification/) | Existing agent communication and authorization requirements. Specific organizational policy is implementation-defined. Adapter mapping remains future work. |

## Closely related proposals

**Marcelo Fernandez, “Agent Control Protocol: Admission Control for Agent Actions,” arXiv:2603.18829 (2026).** [Abstract and versions](https://arxiv.org/abs/2603.18829). This is direct prior work on agent admission checks, identity, capability scope, delegation, policy, revocation, and audit. The initial version reports an implementation and conformance vectors; the current v10 abstract emphasizes temporal/stateful admission and model-checking. These are author-reported claims, not reproduced results. GAP cannot reasonably claim that placing cryptographic admission between intent and execution is new. A detailed comparison of the actual specifications and code is still required.

**Anuj Kaul, Qianlong Lan, and Pranay Gupta, “AgentBound: Verifiable Behavioral Governance for Autonomous AI Agents,” arXiv:2606.30970 (2026).** [Record](https://arxiv.org/abs/2606.30970). The retrieved abstract describes composition of delegated authority, owner policy, site contracts, and governance receipts. Those concepts overlap with GAP's policy-bound action records. Full-text and implementation comparison remain pending; no superiority claim is justified.

**Charles L. Wang, Trisha Singhal, Ameya Kelkar, and Jason Tuo, “MI9: An Integrated Runtime Governance Framework for Agentic AI,” arXiv:2508.03858 (2025).** [Record](https://arxiv.org/abs/2508.03858). Its abstract describes runtime telemetry, authorization monitoring, state-machine conformance, drift detection, and containment. GAP is narrower: a particular action admission and recovery contract. Narrower scope alone does not establish a research contribution.

## Candidate contribution to test

GAP proposes a deliberately small integration profile: one exact action, no delegated chains, certificate-bound permits, serializable online admission/revocation, approval invalidation when context changes, and explicit UNKNOWN/reconciliation semantics. These are design choices to evaluate, not established inventions or measured improvements.

Before a research submission, compare against the full ACP and AgentBound specifications, established capability systems and transaction authorization, OAuth extensions, and relevant agent security literature. Determine whether GAP should become an implementation profile or contribution to an existing project instead of a separate protocol. Publish the comparison even if it shows no material novelty.
