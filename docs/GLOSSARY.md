# Glossary

**Agent:** An autonomous system that takes actions on a schedule or via triggers. Built in Claude, OpenAI, or custom code. Registered in Cortex for operations.

**Health Score:** A 0-100 composite metric for an agent based on success rate, latency, error trends, uptime, throughput, and SLO compliance. Indicates overall agent health at a glance.

**Run:** A single execution of an agent. Logged in Cortex with outcome (COMPLETED, ESCALATED, ERROR), duration, tokens used, and a trace of what happened.

**Standing Instruction:** The core directive given to an agent each cycle. Example: "Process new invoices in Slack and post results to #processed."

**Cycle:** One iteration of an agent's execution loop. Cortex executes agents on a schedule (default 60 seconds per cycle).

**Escalation:** When an agent decides it can't handle a task and hands it off to a human (or another agent). Logged separately from errors.

**Workspace:** An isolated environment where agents live. Teams can share a workspace and collaborate. Data is not shared across workspaces.

**Version Snapshot:** An immutable record of an agent's configuration at a point in time. Enables rollback to any previous config.

**Soft Delete:** An agent is marked as deleted but not removed. Lives in the Recycle Bin for 30 days, fully recoverable.

**SLO (Service Level Objective):** A target for agent performance (e.g., "99% of runs complete in <30 seconds"). Cortex tracks burn rate and alerts when SLOs are at risk.

**Observability:** Real-time visibility into agent health via metrics (error rate, latency, tokens), traces (step-by-step execution), and logs (structured events).

**Integration:** Connecting an agent to an external service (Slack, GitHub, database, email). Agents call these tools during execution.

**Plugin:** A reusable hook into the agent execution pipeline. Cortex provides 6 built-in plugins: logger, alerts, cost-guard, data-redact, retry-smart, eval.

**Recycle Bin:** Where deleted agents go. They're recoverable for 30 days before permanent purge.

**Cost Attribution:** Tracking spending per agent, per model, per provider. Enables chargeback and budget management.

**Provider:** The LLM service used (Anthropic, OpenAI, custom). Cortex supports multiple providers and switches between them based on agent config.

**Token:** A unit of LLM input/output (roughly 4 characters). Cortex tracks tokens per run for cost calculation.

**Trace:** A detailed log of what happened during a run, including tool calls, decisions, errors, and final output.

**Webhook:** An HTTP endpoint that Cortex calls (or that calls Cortex) to trigger an agent or receive event notifications.

**RBAC (Role-Based Access Control):** Permission system for workspaces. Roles: owner, admin, operator, viewer. Controls who can modify agents, view runs, etc.

**Multi-Tenant:** Supporting multiple isolated teams/clients in one Cortex instance. Each workspace is separate.
