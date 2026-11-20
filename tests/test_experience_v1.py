"""Contract and isolation tests for the additive Cortex Experience API."""
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db import Base, User, Agent, Run, utcnow
import experience_v1 as exp


def test_experience_v1_end_to_end():
    with TemporaryDirectory() as folder:
        engine = create_engine("sqlite:///" + str(Path(folder) / "experience.db"))
        maker = sessionmaker(bind=engine, autoflush=False, autocommit=False)
        Base.metadata.create_all(engine)
        with maker() as db:
            db.add_all([
                User(id="u1", email="first@example.test", name="First", is_active=True),
                User(id="u2", email="second@example.test", name="Second", is_active=True),
                Agent(id="agent1", owner_id="u1", slug="demo1", name="My Demo", version=2, is_deleted=False),
                Agent(id="agent2", owner_id="u2", slug="demo2", name="Other User's Demo", is_deleted=False),
            ])
            db.commit()

        def session(request):
            user = request.headers.get("X-Test-User")
            return {"user_id": user} if user in {"u1", "u2"} else None

        def scope(sess, required):
            return required != "experience:export" or sess.get("user_id") == "u1"

        def agent_access(sess, agent_id, write=False):
            with maker() as db:
                row = db.query(Agent).filter(Agent.id == agent_id).first()
                return row is not None and row.owner_id == sess["user_id"]

        app = FastAPI()
        with patch.object(exp, "SessionLocal", maker):
            exp.install_experience(app, session, scope, agent_access)
            with TestClient(app) as client:
                b = "/api/v1/experience"
                u1 = {"X-Test-User": "u1"}
                u2 = {"X-Test-User": "u2"}

                assert client.get(b + "/summary").status_code == 401
                assert client.get(b + "/summary", headers=u1).json()["memory_count"] == 0
                assert client.get("/experience", follow_redirects=False).status_code == 303
                page = client.get("/experience", headers=u1)
                assert page.status_code == 200
                assert "Experience" in page.text

                payload = {
                    "agent_id": "agent1", "kind": "skill", "title": "API schema check",
                    "content": "Validate each request with typed input models.",
                    "tags": ["python", "fastapi"], "training_opt_in": True,
                }
                headers = {**u1, "Idempotency-Key": "reliable-idempotency-key"}
                first = client.post(b + "/memories", headers=headers, json=payload)
                assert first.status_code == 201, first.text
                mem = first.json()
                assert mem["status"] == "draft"
                assert mem["training_opt_in"] is True
                again = client.post(b + "/memories", headers=headers, json=payload)
                assert again.json()["id"] == mem["id"]
                changed = {**payload, "title": "A different memory"}
                assert client.post(b + "/memories", headers=headers, json=changed).status_code == 409
                assert client.get(b + "/usage", headers=u1).json()["units"] == [
                    {"operation": "memory.create", "count": 1}
                ]
                assert client.post(b + "/memories", headers=u1, json={
                    **payload, "agent_id": "agent2"
                }).status_code == 403

                result = client.get(b + "/memories?q=typed", headers=u1).json()
                assert result["total"] == 1 and result["search"] == "lexical"
                assert client.get(b + "/memories?q=unrelated", headers=u1).json()["total"] == 0
                assert client.get(b + "/memories", headers=u2).json()["total"] == 0
                assert client.patch(b + "/memories/" + mem["id"], headers=u2, json={
                    "status": "verified", "verification_note": "Trusted source tested"
                }).status_code == 404
                assert client.delete(b + "/memories/" + mem["id"], headers=u2).status_code == 404

                assert client.get(b + "/datasets/preview", headers=u1).json()["eligible"] == 0
                assert client.get(b + "/datasets/export", headers=u2).status_code == 403
                invalid = client.patch(b + "/memories/" + mem["id"], headers=u1, json={
                    "status": "verified", "verification_note": "too short"
                })
                assert invalid.status_code == 422
                verified = client.patch(b + "/memories/" + mem["id"], headers=u1, json={
                    "status": "verified",
                    "verification_note": "Reviewed after checking the API contract."
                })
                assert verified.status_code == 200, verified.text
                assert client.get(b + "/datasets/preview", headers=u1).json()["eligible"] == 1
                export = client.get(b + "/datasets/export", headers=u1)
                assert export.status_code == 200
                assert export.headers["content-type"].startswith("application/x-ndjson")
                assert "Validate each request" in export.text
                assert "second@example.test" not in export.text

                no_data = client.get(b + "/agents/agent1/performance", headers=u1).json()
                assert no_data["verdict"]["direction"] == "insufficient_data"
                assert client.get(b + "/agents/agent2/performance", headers=u1).status_code == 403
                with maker() as db:
                    for version, completed in [(1, 2), (2, 10)]:
                        for i in range(10):
                            start = utcnow() - timedelta(minutes=i + 1)
                            db.add(Run(
                                id="run-" + str(version) + "-" + str(i),
                                agent_id="agent1", config_version=version,
                                outcome="COMPLETED" if i < completed else "ESCALATED",
                                started_at=start, finished_at=start + timedelta(seconds=1),
                                total_tokens=100,
                            ))
                    db.commit()
                performance = client.get(b + "/agents/agent1/performance", headers=u1).json()
                assert performance["verdict"]["direction"] == "better"
                assert performance["versions"][0]["average_latency_ms"] == 1000.0
                assert client.post(b + "/feedback", headers=u1, json={
                    "run_id": "unknown", "rating": "helpful"
                }).status_code == 404
                assert client.post(b + "/feedback", headers=u2, json={
                    "run_id": "run-1-1", "rating": "helpful"
                }).status_code == 403
                feedback = client.post(b + "/feedback", headers=u1, json={
                    "run_id": "run-1-1", "rating": "unhelpful",
                    "note": "This escalation should be investigated."
                })
                assert feedback.status_code == 201
                assert client.get(b + "/summary", headers=u1).json()["feedback_count"] == 1

                deleted = client.delete(b + "/memories/" + mem["id"], headers=u1)
                assert deleted.status_code == 204
                assert client.get(b + "/datasets/preview", headers=u1).json()["eligible"] == 0
        engine.dispose()
