"""python -m rehearsal [--serve | --report PATH] [--trace PATH | --adapter module:factory]"""
import argparse
import importlib
import json
from pathlib import Path

from .runner import compare, run_suite


def main():
    parser = argparse.ArgumentParser(description="Cortex synthetic referral rehearsal")
    parser.add_argument("--serve", action="store_true", help="Start local UI at http://127.0.0.1:3010/rehearsal")
    parser.add_argument("--port", type=int, default=3010)
    parser.add_argument("--report", type=Path, help="Write a JSON report")
    parser.add_argument("--html", type=Path, help="Write an interactive offline report")
    parser.add_argument("--case-count", type=int, default=100)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--reviewer", choices=("hold", "approve", "deny"), default="hold")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--trace", type=Path, help="Replay an existing agent's recorded action JSON")
    source.add_argument("--adapter", help="TRUSTED LOCAL CODE ONLY: module:factory providing next_action(observation, history)")
    args = parser.parse_args()
    if args.serve:
        if args.adapter or args.trace: parser.error("Use CLI report mode for adapters/traces; UI supports trace import")
        from fastapi import FastAPI
        from starlette.middleware.trustedhost import TrustedHostMiddleware
        import uvicorn
        from .api import install
        app = FastAPI(title="Cortex Rehearsal (local synthetic demo)")
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
        install(app)
        uvicorn.run(app, host="127.0.0.1", port=args.port)
        return
    factory, trace = None, None
    if args.adapter:
        module, separator, name = args.adapter.partition(":")
        if not separator: parser.error("Adapter must be module:factory")
        factory = getattr(importlib.import_module(module), name)
    if args.trace:
        if args.trace.stat().st_size > 1_000_000: parser.error("Trace exceeds the 1 MB limit")
        trace = json.loads(args.trace.read_text())
    try:
        if factory or trace:
            report = run_suite("python" if factory else "trace", True, args.case_count, args.seed, args.reviewer, trace, factory)
        else: report = compare(args.case_count, args.seed, args.reviewer)
    except ValueError as exc: parser.error(str(exc))
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2))
    if args.html:
        from .api import snapshot
        args.html.parent.mkdir(parents=True, exist_ok=True)
        args.html.write_text(snapshot(report))
    for run in report.get("runs", [report]):
        print(json.dumps({"agent": run["agent"], "enforcement": run["enforcement"], **run["summary"]}))


if __name__ == "__main__": main()
