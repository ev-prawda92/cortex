from copy import deepcopy
from datetime import datetime, timezone
import time
import uuid

from .agents import ScriptedAgent, TraceAgent, validate_trace
from .cases import FIXTURE_VERSION, SIMULATED_NOW, fixture_hash, make_cases, observation
from .engine import Session, grade


def run_suite(agent="careful", enforce=True, case_count=100, seed=42, reviewer="hold", trace=None, adapter_factory=None):
    if agent not in ("baseline", "careful", "trace", "python"): raise ValueError("Unknown agent")
    if reviewer not in ("hold", "approve", "deny"): raise ValueError("Unknown reviewer mode")
    if type(enforce) is not bool: raise ValueError("enforce must be a boolean")
    cases = make_cases(case_count, seed)
    if agent == "trace":
        trace = validate_trace(trace)
        unknown = set(trace["traces"]) - {c["id"] for c in cases}
        if unknown: raise ValueError("Trace includes case IDs outside the selected fixture")
        cases = [c for c in cases if c["id"] in trace["traces"]]
    if agent == "python" and adapter_factory is None: raise ValueError("Python adapter factory is required")
    started = time.perf_counter()
    trials = []
    for case in cases:
        session = Session(case, enforce)
        if agent == "trace": adapter = TraceAgent(trace["traces"][case["id"]])
        elif agent == "python": adapter = adapter_factory()
        else: adapter = ScriptedAgent(careful=agent == "careful")
        obs, history, errors = observation(case), [], []
        for step in range(20):
            try: proposal = adapter.next_action(deepcopy(obs), deepcopy(history))
            except Exception as exc:
                errors.append(f"Adapter failed: {type(exc).__name__}")
                break
            if proposal is None: break
            response = session.call(proposal)
            history.append({"action": deepcopy(proposal), "result": response})
            if response.get("review_id") and reviewer != "hold":
                token = session.review(response["review_id"], reviewer == "approve")
                if token:
                    reviewed = deepcopy(proposal)
                    reviewed.setdefault("args", {})["approval_id"] = token
                    response = session.call(reviewed)
                    history.append({"action": reviewed, "result": response})
        else:
            errors.append("Action budget exhausted (20 proposals)")
        grading = grade(case, session, reviewer)
        if errors: grading["passed"] = False
        trials.append({"case_id": case["id"], "family": case["family"], "label": case["label"],
                       "patient_name": case["patient_name"], "specialty": case["specialty"],
                       "observation": obs, "grade": grading, "state": session.state,
                       "events": session.events, "errors": errors})
    events = [e for trial in trials for e in trial["events"] if e["actor"] == "agent"]
    summary = {
        "cases": len(trials), "passed": sum(t["grade"]["passed"] for t in trials),
        "workflow_correct": sum(t["grade"]["workflow_correct"] for t in trials),
        "unsafe_cases": sum(bool(t["grade"]["violations"]) for t in trials),
        "unauthorized_executions": sum(t["grade"]["unauthorized_executions"] for t in trials),
        "blocked_actions": sum(not e["executed"] and e["decision"] == "BLOCK" for e in events),
        "evidence_holds": sum(e["decision"] == "REQUEST_MORE_EVIDENCE" for e in events),
        "approval_holds": sum(e["decision"] == "HUMAN_REVIEW" for e in events),
        "tool_calls": len(events), "retry_recoveries": sum(
            t["grade"]["passed"] and any(e["result"].get("retryable") for e in t["events"]) for t in trials),
        "duration_ms": round((time.perf_counter()-started)*1000, 2),
        "model_cost_usd": None,
        "cost_note": "No model invoked" if agent in ("baseline", "careful", "trace") else "Adapter cost not reported",
    }
    return {"schema_version": "1.0", "id": str(uuid.uuid4()), "created_at": datetime.now(timezone.utc).isoformat(),
            "agent": trace["name"] if agent == "trace" else agent, "agent_type": agent,
            "enforcement": enforce, "reviewer": reviewer, "seed": seed,
            "fixture_version": FIXTURE_VERSION, "fixture_hash": fixture_hash(cases),
            "simulated_now": SIMULATED_NOW.isoformat(), "synthetic_only": True,
            "summary": summary, "trials": trials}


def compare(case_count=100, seed=42, reviewer="hold"):
    return {"id": str(uuid.uuid4()), "kind": "comparison", "synthetic_only": True,
            "runs": [run_suite("baseline", False, case_count, seed, reviewer),
                     run_suite("baseline", True, case_count, seed, reviewer),
                     run_suite("careful", True, case_count, seed, reviewer)],
            "comparison_note": "First two runs use the identical scripted agent and fixtures. The third also changes the agent strategy."}
