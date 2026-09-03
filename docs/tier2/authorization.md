# Consequence-Aware Authorization

Cortex places a decision boundary between an agent's proposed action and a
real enterprise integration. Enterprise owners delegate authority through an
agent authority profile; Cortex compiles and enforces that profile.

## Decision flow

1. Create a draft authority profile with `PUT /api/agents/{id}/authority`.
2. An admin activates it after the relevant business, data, security, and risk
   owners approve the delegation.
3. An agent calls `POST /api/agents/{id}/authorize`, or submits an integration
   action through `POST /api/integrations/execute`.
4. Cortex returns `ALLOW`, `ALLOW_WITH_LIMITS`, `REQUEST_MORE_EVIDENCE`,
   `HUMAN_REVIEW`, or `BLOCK`.
5. The integration gateway executes only an allowed action and writes a
   hash-chained attestation.

Agents without an active profile remain in legacy compatibility mode. Once a
profile is active, its default should normally be `BLOCK`; authority is then
granted explicitly per action.

## Authority profile

See `examples/prior_auth_authority.json`. A profile includes credentials and
action privileges. Each privilege can constrain environments, data scopes,
target systems, required evidence, financial impact, throughput, and human
review.

Activating or revising a profile increments its version. Every decision stores
that version and a SHA-256 hash of the proposed-action payload.

## Human approval

When a policy requires review, Cortex creates an existing `ApprovalRequest`.
After an authorized user approves it, the caller resubmits the same action with
the returned `approval_id`. Cortex verifies that the approval belongs to the
same agent and action before allowing execution.

## Enforcement rule

Calling the decision endpoint alone is advisory. Strong enforcement requires
the target tool or integration to be reachable only through the Cortex
integration gateway, so the agent cannot bypass a BLOCK or HUMAN_REVIEW result.
