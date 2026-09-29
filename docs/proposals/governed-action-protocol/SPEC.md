# Governed Action Protocol v0.1.0-draft

Evan Alexander Prawda · 2026-09-25 · Experimental proposal

## 1. Status, scope, and terminology

This is a proposed implementer contract, not an IETF RFC, adopted standard, verified implementation, or guarantee of safe AI. MUST, MUST NOT, SHOULD, and MAY express requirements on a future implementation claiming this draft profile. Schema validation is necessary but insufficient. On a conflict between prose and schema, implementations MUST stop claiming conformance until the inconsistency is resolved.

The profile identifier is `gap/0.1`; its message identifiers and HTTP routes are private experimental conventions, not registered identifiers. A deployment MUST pin this exact profile and reject unsupported versions. Silent fallback to an ungoverned endpoint is prohibited.

The core authorizes a single exact action. It does not define general natural-language policy interpretation, model alignment, a universal risk score, cross-organization trust, bearer access tokens, chained delegation, or transaction rollback. Authorization permits an attempt; it does not guarantee a beneficial outcome.

**Principal:** person or organization on whose behalf an action is requested. **Agent:** authenticated requesting workload. **Authority:** trusted service that evaluates policy, issues permits, and serializes admission and revocation. **Executor:** trusted service that validates authorization and mediates all access to the protected operation. **Reviewer:** authenticated human entitled to approve a specific proposal. **Resource adapter:** implementation translating a typed action into a downstream operation.

## 2. Threat model and assumptions

Assume an attacker can control agent prompts, tool arguments, proposed action bodies, and network retries. They may copy a permit, substitute arguments, replay messages, race concurrent requests, switch tenant identifiers, or induce service crashes and partitions. TLS endpoints, certificate authorities, authority signing keys, executor software, and the durable admission database are trusted components, not assumed compromised.

Required deployment assumptions:

1. The executor is the only path to the protected operation. Agent workloads lack downstream credentials, privileged local execution, or alternate network paths that bypass it.
2. Authority policy and identity mappings are administered outside the agent's authority. An agent cannot make itself an approver or edit its own permissions.
3. All authority replicas use a linearizable admission/revocation store. An eventually consistent cache alone cannot satisfy this profile.
4. The resource adapter either commits the side effect transactionally with its operation record, or the downstream system enforces durable idempotency and supports outcome reconciliation. Otherwise this profile cannot safely authorize the action.
5. Services have trusted clocks with a configured uncertainty bound of at most five seconds. A service unable to establish that bound fails closed for new admissions.

The protocol does not prevent harm permitted by a bad policy, collusion among trusted operators, a malicious downstream system, erroneous semantic classification, or model misinformation. Encrypted transport and signed receipts do not prove an action was ethical, legally compliant, or factually correct.

## 3. Trust configuration and transport

All network hops MUST use authenticated HTTPS, TLS 1.2 or later; TLS 1.3 is preferred. In this draft, agent-to-authority and agent-to-executor requests MUST additionally use mutual TLS. The receiving service maps the validated certificate to the agent, principal, and allowed tenants using trusted configuration. JSON identity fields are assertions to check, not identity evidence.

The permit binds the certificate by `cnf.x5t#S256`, the unpadded base64url SHA-256 digest of its DER-encoded leaf certificate. The executor MUST match this against the actual authenticated client certificate. A stolen permit is insufficient without the corresponding private key. Certificate rotation requires a new permit. Trusted TLS termination MAY be used only with an authenticated, integrity-protected binding between proxy and service; client-supplied certificate headers MUST be stripped and cannot establish identity.

Authority-to-executor calls and executor-to-authority admission/status calls MUST use mutually authenticated service identities. Service-role permissions and tenant boundaries are enforced independently from agent permissions.

Issuer identifiers, exact executor audiences, policy owners, signing keys, permitted algorithms, and authority endpoint locations MUST be configured through a trusted administrative channel. Message fields MUST NOT trigger arbitrary URL/key retrieval. A `kid` identifies a key within an already trusted issuer's key set; it does not establish trust.

## 4. Canonical action and hashing

The action object includes `profile`, `operation_id`, `tenant_id`, `principal_id`, `agent_id`, `audience`, `action_type`, `resource_id`, `parameters`, and `context_digest`. Every field is REQUIRED. No additional top-level fields are allowed in this version. All identifiers are exact, case-sensitive strings; applications MUST NOT reinterpret the same identifier across tenants or silently normalize it after authorization.

`operation_id` is a UUID minted by the client for this logical operation. The authority MUST enforce uniqueness within the tenant and bind it permanently to the authenticated identity and action digest for the deployment's operation-retention period. Changing an action requires a new operation ID. A new ID is not permission to repeat a business operation: domain policy MUST enforce cumulative limits and duplicate-business-operation rules separately.

`action_type` identifies an immutable adapter contract. This draft defines `refund.original-payment/v1`, with parameters `amount_minor` (integer 1 through 9007199254740991) and `currency` (exactly `USD`). The resource identifies an order within the tenant; destination is resolved to that order's original payment instrument. Other currencies and action types require a new documented profile/schema. Floating-point amounts, arbitrary destinations, unknown parameters, and implicit defaults are prohibited.

`context_digest` identifies a server-maintained immutable snapshot of relevant state, such as remaining refundable balance and the original payment instrument reference. The executor/authority MUST obtain that snapshot from trusted storage, verify its digest, and verify current state against its preconditions. An agent cannot supply authoritative context merely by hashing it. Snapshots MUST have an expiry and a resource revision, stored by the service; expired or mismatched snapshots require a new proposal.

All JSON MUST be UTF-8 conforming to I-JSON constraints, with duplicate object keys rejected before schema validation. Hashes are `sha256:` followed by 64 lowercase hexadecimal characters. Compute:

`action_digest = "sha256:" + hex(SHA256(UTF8(JCS(action))))`

JCS is RFC 8785. Validating parsers MUST reject invalid Unicode, non-finite numbers, and integers outside the safe range. The complete action, including identity, audience, context, and operation ID, is hashed. No unsignaled fields or out-of-band parameters may influence execution. Transport headers may authenticate and route requests but MUST NOT override action semantics.

## 5. Authorization and human review

`POST /gap/v0.1/authorize` receives an action object from an authenticated agent. The authority MUST validate structure, resolve the snapshot, verify identity and resource ownership, evaluate its current policy, and persist the proposal before returning a decision. Policies MUST specify the actions and resources they cover; unrecognized or uncovered actions are denied.

The decision body contains `profile`, `decision_id`, `operation_id`, `action_digest`, `outcome`, `reason_code`, `policy_digest`, `evaluated_at`, and `permit`. Outcomes are `PERMIT`, `DENY`, and `REVIEW`. Only `PERMIT` includes a compact JWS permit; the other outcomes require `permit: null` and cannot be executed. Reasons are stable machine codes such as `POLICY_DENIED`, `APPROVAL_REQUIRED`, and `AUTHORIZED`; detailed explanations are access-controlled and do not reveal other tenants' resources.

An initial decision is immutable. Review creates a new decision linked to the prior proposal in the authority's record. `GET /gap/v0.1/decisions/{decision_id}` returns that exact record only to an entitled caller. Clients reauthorize the same action after review to obtain a fresh decision and permit. Reauthorization MUST NOT reset an operation's execution/admission state.

Human approval MUST occur through an authenticated reviewer interface outside the agent's control. The reviewer sees the actual action, material consequences, relevant context, and policy version. Approval records bind `action_digest`, `policy_digest`, `context_digest`, reviewer identity, decision, and expiry. The authority MUST validate the reviewer's current approval rights and any separation-of-duties requirement. Approval strings in prompts, agent-written logs, and an agent-supplied `approved: true` flag are not approvals.

Before issuing a permit, the authority rechecks policy and current context. Any change to action, context, or policy invalidates earlier approval. The core never edits a proposed action into a supposedly safer one: a different amount or destination is a new proposal requiring fresh evaluation.

## 6. Signed permit

Permits use compact JWS (RFC 7515) with a fully protected header containing only `alg: ES256`, `kid`, and `typ: gap-permit+jws`. ES256 uses P-256 and SHA-256 with the JOSE 64-byte R||S signature encoding. Reject `none`, algorithm substitution, embedded keys, arbitrary key URLs, detached/unencoded payloads, unknown header fields, and unsupported critical extensions. Implementations MUST use established JOSE libraries and trusted key material.

The signed payload contains:

| Field | Meaning |
|---|---|
| `profile`, `permit_id` | Exact profile and unique UUID for this permit |
| `iss`, `aud` | Configured authority and exact executor identifier |
| `tenant_id`, `principal_id`, `agent_id` | Authorized identities |
| `operation_id`, `action_digest` | Exact logical operation and action |
| `policy_digest`, `context_digest` | Policy and context used in authorization |
| `iat`, `nbf`, `exp` | Integer Unix seconds |
| `cnf` | Actual client-certificate binding |
| `approval_id` | Authority-side approval UUID, or null when no review is required |
| `max_uses`, `delegation` | Exactly `1` and `false` |

The authority MUST set `iat = nbf` and `1 <= exp - iat <= 300`. Payload JSON uses JCS before JWS encoding. For a local time `t` and clock uncertainty `u`, admission is allowed only if `t-u >= nbf` and `t+u < exp`; no post-expiry grace is added. The signature validates the original encoded bytes; recipients also require canonical payload encoding.

Every field is required and unknown fields are rejected. `iss`, tenant, principal, agent, audience, operation, digest, and context MUST match the trusted records and supplied action. A valid signature does not replace a current policy or revocation check. Core permits cannot be delegated. Forwarding one to another workload MUST fail certificate/identity checks. A different agent must obtain its own authorization from the authority; there is no autonomous chain extension.

## 7. Execution and the admission point

`POST /gap/v0.1/execute` receives `{ "action": <action>, "permit": <compact JWS> }` at the designated executor. Before any external side effect, the executor MUST validate TLS identity, message schema, signature, issuer, certificate binding, audience, times, digest, tenant/resource ownership, adapter version, and trusted context.

The executor then invokes the authority's internal `POST /gap/v0.1/admit`, passing the verified compact permit and action. The authenticated service identity MUST map to the permit audience. The authority independently verifies the permit and atomically checks current key/permit status, current policy, approval validity, context preconditions, and operation state. It MUST compare `policy_digest` to the currently active immutable policy digest. A policy change makes unadmitted permits stale.

The internal admit response contains `profile`, `admission_id`, `tenant_id`, `operation_id`, `action_digest`, `permit_id`, `executor_id`, `admitted_at` (integer Unix seconds), and `state: ADMITTED`. It is carried over the mutually authenticated channel and retained durably. Duplicate admission requests return the original admission record; they do not authorize another dispatch. The authority trusts the authenticated executor to have verified the live agent certificate; forwarding JSON certificate claims alone is insufficient.

Admission linearizes when a durable transaction records `(tenant_id, operation_id, action_digest, permit_id, executor_id)` as ADMITTED and consumes the permit. This transaction MUST be serialized with permit revocation and competing admissions. The authority returns a durable admission identifier; a lost response is resolved by querying the same operation, not by creating a new operation.

Resource state that may change between admission and execution MUST also be checked in the side-effect transaction, or protected by a version-conditioned downstream operation. The effect MUST NOT occur if that precondition fails. Snapshot checking only at issuance is insufficient.

Executor records and dispatch MUST be crash-recoverable. A durable worker claim/lease plus a stable downstream idempotency key is required when dispatch is not part of one transaction. The downstream idempotency key is deterministically derived from the tenant and operation ID with unambiguous encoding. All retries use the same key. A downstream service that forgets keys before recovery completes is unsupported.

The executor MUST NOT return success before downstream confirmation. A crash or timeout with an uncertain outcome produces `UNKNOWN`, retains the original operation/key, and starts reconciliation. It MUST NOT issue a replacement operation or assume the effect failed. At-most-one committed business effect is conditional on the adapter and downstream guarantees; this draft does not claim universal exactly-once delivery.

## 8. State, retries, expiry, and revocation

Execution state transitions are:

| From | To | Condition |
|---|---|---|
| No admission | `ADMITTED` | Successful serialized admission |
| `ADMITTED` | `SUCCEEDED` | Confirmed committed effect |
| `ADMITTED` | `FAILED` | Confirmed failure with no effect |
| `ADMITTED` | `UNKNOWN` | Effect cannot yet be determined |
| `UNKNOWN` | `SUCCEEDED` / `FAILED` | Authoritative reconciliation |

`SUCCEEDED` and `FAILED` are terminal for that operation. `UNKNOWN` is not failure. Internal retries after a confirmed transient no-effect condition retain the same operation and idempotency key; a terminal FAILED operation is not reopened.

An exact duplicate execution request returns the entitled caller's existing operation state, without a fresh dispatch. Reuse of an operation ID with a different action is a conflict. A used permit cannot authorize a second operation. Issuing another permit for the same action does not create another execution allowance. Authenticated operation-status retrieval remains available after permit expiry; it requires caller authorization, not possession of an old token.

`POST /gap/v0.1/permits/{permit_id}/revoke` is restricted to the authority's entitled administrators/principals. Revocation and admission share the same serialization order. Revocation that commits first MUST prevent admission. If admission commits first, revocation returns `ALREADY_ADMITTED`; it does not promise cancellation or reversal. Repeat revocation is idempotent. Unknown or unauthorized IDs use indistinguishable not-found responses.

Expiry, policy updates, reviewer-rights changes, and key revocation stop new admissions when observed in the admission transaction. They do not undo an already admitted operation. Pending dispatch cancellation, bounded admission-to-execution delay, and compensating actions require an additional profile and are open issues. Services MUST communicate this boundary accurately.

If the authority, revocation store, policy store, approval store, or required context is unavailable, new admission fails closed. An already durably admitted operation may resume with its original idempotency key. This is a documented consistency/availability tradeoff.

## 9. Execution receipts and status

`GET /gap/v0.1/operations/{operation_id}` returns an authorized tenant-scoped status and its latest signed receipt. The executor signs receipt bodies using compact JWS and protected `typ: gap-receipt+jws`, with the same ES256 restrictions. Receipt keys are separately identified and trusted for executor receipts, not permit issuance.

A receipt contains `profile`, `receipt_id`, `iss` (executor), `tenant_id`, `operation_id`, `action_digest`, `permit_id`, `policy_digest`, `admission_id`, `state`, `recorded_at`, `result_digest`, and `previous_receipt_digest`. Every field is required. Digests may be null only where the schema allows. `result_digest` is null before a result is known; terminal outcomes require a digest of a retained result/error record. `previous_receipt_digest` is null for the first receipt and otherwise hashes the exact prior compact JWS ASCII bytes with SHA-256.

Each state change appends a new receipt to durable storage. Receipt persistence failure after a side effect MUST trigger recovery of that operation, never replay under a fresh key. Retained receipts and admission records MUST be linked by operation ID. No raw credentials, model chain of thought, or unnecessary personal data may appear in public receipts. Detailed evidence remains access-controlled, with retention and deletion rules set by the operator.

A receipt proves that a trusted key signed a statement. It does not independently prove physical execution, source truth, complete logging, or absence of other actions. Hash chaining detects modification relative to a trusted checkpoint but does not itself prevent deletion, truncation, or equivocation. External witnesses/checkpoints are out of scope.

## 10. HTTP behavior and operational bounds

Use `application/json` for JSON and prohibit side effects on GET. HTTP 200 carries completed authorization decisions (including DENY/REVIEW) and known terminal/status responses; 202 indicates an admitted/pending operation. Invalid structure returns 400, missing/invalid authentication 401, failed authorization 403, hidden/missing resources 404, operation conflicts/stale preconditions 409, oversized bodies 413, rate limits 429, and unavailable required dependencies 503. Responses MUST NOT leak cross-tenant existence. A 5xx or lost response after dispatch is not evidence of no effect; clients query status with the original operation ID.

Deployments MUST enforce request-size limits (maximum 64 KiB for core JSON bodies), bounded JSON depth (16), bounded identifiers (256 characters), rate limits per authenticated tenant/workload, and a published retention/retry window. Admission tombstones and idempotency records MUST survive the longest supported recovery/retry period. Within that window, identifiers cannot be recycled. After retention expiry, callers MUST receive an explicit unsupported/expired-operation response rather than silently replaying a historical operation. A deployment MUST retain compact non-reusable tombstones (or an equivalent durable identifier registry) for as long as that tenant namespace accepts requests, even when detailed evidence is deleted; deleting all evidence of an ID while still accepting it as new violates this requirement. An adapter MUST explain how downstream retention supports these requirements.

Key rotation MUST distinguish active signing keys, verification-only historical keys, and revoked keys. Old trusted public keys may verify historical receipts, while new admission still checks current revocation. Do not log private keys or raw permits. An issuer must not use a receipt-verification key as authority to issue permits.

## 11. Mapping to existing protocols

MCP tool calls and A2A tasks MAY carry a GAP operation reference through a documented adapter, but their existing authentication and authorization requirements still apply. An adapter MUST reconstruct and validate the exact action before execution, preserve tenant identity, and propagate UNKNOWN and conflict states without silently retrying with fresh operation IDs. Native metadata transport is not specified in v0.1, so cross-framework interoperability is an evaluation target rather than an achieved property.

OAuth rich authorization requests can convey fine-grained intent; certificate-bound credentials and JWS already provide relevant security mechanisms. GAP's experimental additions are the selected composition, exact-action binding, online admission semantics, approval invalidation, and receipt/recovery contract. These may overlap with other work. See [RELATED-WORK.md](RELATED-WORK.md).

## 12. Conformance, limitations, and open questions

Conformance requires all normative rules plus the behavioral cases in [CONFORMANCE.md](CONFORMANCE.md), not merely valid JSON. A claimed implementation MUST identify its adapter, trust store, storage model, clock bound, retention policy, and bypass isolation. Independent review and fault-injection testing remain necessary.

Open design questions include certificate lifecycle usability, controlled federation, typed attenuation/delegation, resource-specific budget reservations, asynchronous cancellation, evidence confidentiality, external audit witnesses, a DPoP profile for environments without mTLS, and formal verification of the state machine. These are not implicit v0.1 features.

No registry allocation, patent assurance, legal certification, safety certification, or standards-body endorsement is claimed. Normative references and related proposals are linked in RELATED-WORK.md.
