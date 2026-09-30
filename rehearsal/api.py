"""Standalone and Cortex-mounted routes share the same runner and report store."""
from pathlib import Path
from typing import Literal
import hashlib
import json
import os
import tempfile
import threading
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .cases import make_cases, observation
from .runner import compare, run_suite

PAGE = Path(__file__).with_name("index.html")
RUN_LOCK = threading.Lock()


class RunInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    agent: Literal["baseline", "careful", "trace"] = "careful"
    enforce: bool = True
    case_count: int = Field(default=100, ge=1, le=100)
    seed: int = Field(default=42, ge=0, le=2**31-1)
    reviewer: Literal["hold", "approve", "deny"] = "hold"
    trace: dict | None = None


class ReportStore:
    def __init__(self, root=None):
        self.root = Path(root or os.getenv("REHEARSAL_DATA_DIR", "data/rehearsals"))

    def folder(self, owner):
        return self.root / hashlib.sha256(owner.encode()).hexdigest()

    def save(self, owner, report):
        folder = self.folder(owner)
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = folder / (str(uuid.UUID(report["id"])) + ".json")
        fd, name = tempfile.mkstemp(dir=folder, prefix=".report-")
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(report, stream, sort_keys=True)
            os.replace(name, path)
        finally:
            if os.path.exists(name): os.unlink(name)

    def get(self, owner, report_id):
        try: normalized = str(uuid.UUID(report_id))
        except ValueError: raise HTTPException(404, "Report not found")
        try: return json.loads((self.folder(owner) / (normalized + ".json")).read_text())
        except FileNotFoundError: raise HTTPException(404, "Report not found")

    def list(self, owner):
        folder = self.folder(owner)
        if not folder.exists(): return []
        result = []
        for path in sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:20]:
            report = json.loads(path.read_text())
            runs = report.get("runs", [report])
            result.append({"id": report["id"], "kind": report.get("kind", "run"),
                           "created_at": runs[0]["created_at"], "cases": runs[0]["summary"]["cases"]})
        return result


def snapshot(report):
    # Escape HTML-significant characters in JSON to prevent script termination.
    data = json.dumps(report).replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    return PAGE.read_text().replace("/*REPORT_DATA*/null", data)


def install(app, get_session=None, store=None):
    store = store or ReportStore()
    router = APIRouter()

    def owner(request: Request):
        if get_session is None: return "standalone-local"
        session = get_session(request)
        if not session or not session.get("user_id"): raise HTTPException(401, "Sign in to Cortex")
        return str(session["user_id"])

    @app.middleware("http")
    async def rehearsal_request_guard(request, call_next):
        if request.url.path.startswith("/api/rehearsal") and request.method == "POST":
            length = request.headers.get("content-length", "")
            if not length.isdigit(): return JSONResponse(status_code=411, content={"detail": "Content-Length required"})
            if int(length) > 1_000_000: return JSONResponse(status_code=413, content={"detail": "Trace payload limit is 1 MB"})
            origin = request.headers.get("origin")
            if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
                return JSONResponse(status_code=403, content={"detail": "Cross-origin rehearsal requests are prohibited"})
        response = await call_next(request)
        if request.url.path.startswith(("/rehearsal", "/api/rehearsal")):
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "DENY"
        return response

    @router.get("/rehearsal", response_class=HTMLResponse)
    def page(_owner=Depends(owner)):
        return PAGE.read_text()

    @router.get("/api/rehearsal/scenarios")
    def scenarios(_owner=Depends(owner)):
        return {"synthetic_only": True, "cases": [observation(c) for c in make_cases()]}

    def execute(body, comparing):
        if not RUN_LOCK.acquire(blocking=False): raise HTTPException(429, "A rehearsal is already running; retry shortly")
        try:
            if comparing: return compare(body.case_count, body.seed, body.reviewer)
            return run_suite(body.agent, body.enforce, body.case_count, body.seed, body.reviewer, body.trace)
        except ValueError as exc: raise HTTPException(422, str(exc))
        finally: RUN_LOCK.release()

    @router.post("/api/rehearsal/run")
    def run(body: RunInput, user=Depends(owner)):
        report = execute(body, False)
        store.save(user, report)
        return report

    @router.post("/api/rehearsal/compare")
    def comparison(body: RunInput, user=Depends(owner)):
        if body.agent == "trace" or body.trace is not None:
            raise HTTPException(422, "Comparison uses the built-in scripted agents; use run for trace replay")
        report = execute(body, True)
        store.save(user, report)
        return report

    @router.get("/api/rehearsal/reports")
    def reports(user=Depends(owner)): return {"reports": store.list(user)}

    @router.get("/api/rehearsal/reports/{report_id}")
    def report(report_id: str, user=Depends(owner)): return store.get(user, report_id)

    @router.get("/api/rehearsal/reports/{report_id}/html", response_class=HTMLResponse)
    def report_html(report_id: str, user=Depends(owner)):
        return HTMLResponse(snapshot(store.get(user, report_id)), headers={
            "Content-Disposition": 'attachment; filename="Cortex-Rehearsal-Report.html"'})

    app.include_router(router)
