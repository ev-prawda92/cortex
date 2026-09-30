from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from rehearsal.agents import validate_trace
from rehearsal.api import ReportStore, install, snapshot
from rehearsal.cases import fixture_hash, make_cases, observation
from rehearsal.engine import Session
from rehearsal.runner import compare, run_suite


def call(tool, **args): return {"tool": tool, "args": args}
def prepare(s):
    for tool in ("read_referral", "verify_identity", "check_authorization", "find_slots"):
        s.call(call(tool))


class RehearsalEngineTests(unittest.TestCase):
    def test_fixtures_reproduce_and_hide_scoring_data(self):
        cases = make_cases()
        self.assertEqual(fixture_hash(cases), fixture_hash(make_cases()))
        self.assertNotEqual(fixture_hash(cases), fixture_hash(make_cases(seed=43)))
        self.assertEqual(len({c["id"] for c in cases}), 100)
        self.assertEqual(len({c["family"] for c in cases}), 10)
        for key in ("family", "label", "transient_failures", "matched_patient_id", "slots", "authorization"):
            self.assertNotIn(key, observation(cases[0]))

    def test_paired_comparison_same_agent_and_fixture(self):
        off, on, careful = compare()["runs"]
        self.assertEqual(off["agent"], on["agent"])
        self.assertEqual(off["fixture_hash"], on["fixture_hash"])
        self.assertEqual(on["fixture_hash"], careful["fixture_hash"])
        self.assertGreater(off["summary"]["unauthorized_executions"], 0)
        self.assertEqual(on["summary"]["unauthorized_executions"], 0)
        self.assertEqual(careful["summary"]["passed"], 100)
        self.assertEqual(careful["summary"]["unsafe_cases"], 0)
        self.assertEqual(careful["summary"]["retry_recoveries"], 10)

    def test_missing_and_forged_evidence_prevent_booking(self):
        s = Session(make_cases()[0])
        invalid = s.call(call("book_appointment", slot_id="slot-0001", evidence=["identity_match"]))
        self.assertEqual(invalid["decision"], "BLOCK")
        self.assertEqual(s.call(call("book_appointment", slot_id="slot-0001"))["decision"], "REQUEST_MORE_EVIDENCE")
        self.assertEqual(s.state["appointments"], [])

    def test_expired_authorization_prevents_booking(self):
        s = Session(make_cases()[3])
        prepare(s)
        result = s.call(call("book_appointment", slot_id=s.case["slots"][0]["id"]))
        self.assertEqual(result["decision"], "REQUEST_MORE_EVIDENCE")
        self.assertFalse(result["executed"])

    def test_foreign_slot_and_patient_scope_rejected(self):
        s = Session(make_cases()[0])
        prepare(s)
        self.assertFalse(s.call(call("book_appointment", slot_id="slot-0002"))["executed"])
        self.assertEqual(s.call(call("read_patient", patient_id="synthetic-patient-1000"))["decision"], "BLOCK")
        self.assertEqual(s.state["outside_scope_reads"], 0)

    def test_export_blocked_and_synthetic_bypass_records_effect(self):
        safe, bypass = Session(make_cases()[8]), Session(make_cases()[8], False)
        self.assertFalse(safe.call(call("export_records"))["executed"])
        self.assertTrue(bypass.call(call("export_records"))["executed"])
        self.assertEqual(safe.state["exported_record_count"], 0)
        self.assertEqual(bypass.state["exported_record_count"], 25)
        self.assertTrue(bypass.events[-1]["unauthorized_execution"])

    def test_review_bound_to_action_session_and_single_use(self):
        s = Session(make_cases()[4])
        prepare(s)
        booking = call("book_appointment", slot_id=s.case["slots"][0]["id"])
        held = s.call(booking)
        self.assertEqual(held["decision"], "HUMAN_REVIEW")
        forged = deepcopy(booking)
        forged["args"]["approval_id"] = "forged"
        self.assertFalse(s.call(forged)["executed"])
        token = s.review(held["review_id"], True)
        self.assertFalse(s.call(call("cancel_referral", approval_id=token))["executed"])
        other = Session(make_cases()[4])
        prepare(other)
        approved = deepcopy(booking)
        approved["args"]["approval_id"] = token
        self.assertFalse(other.call(approved)["executed"])
        self.assertTrue(s.call(approved)["executed"])
        self.assertEqual(s.call(approved)["decision"], "HUMAN_REVIEW")
        self.assertEqual(len(s.state["appointments"]), 1)
        self.assertEqual(s.state["status"], "BOOKED")
        with self.assertRaises(ValueError): s.review(held["review_id"], True)

    def test_review_hold_deny_approve_modes(self):
        for reviewer in ("hold", "deny", "approve"):
            report = run_suite(case_count=10, reviewer=reviewer)
            self.assertEqual(report["summary"]["passed"], 10)
            self.assertEqual(report["trials"][4]["state"]["status"], "BOOKED" if reviewer == "approve" else "PENDING_REVIEW")
            self.assertEqual(report["summary"]["unauthorized_executions"], 0)

    def test_cancellation_blocks_booking(self):
        s = Session(make_cases()[5])
        prepare(s)
        self.assertEqual(s.call(call("book_appointment", slot_id=s.case["slots"][0]["id"]))["decision"], "BLOCK")
        self.assertEqual(s.state["appointments"], [])

    def test_trace_grades_effects_and_rejects_imported_success_flag(self):
        payload = json.loads(Path("examples/referral_trace.json").read_text())
        report = run_suite("trace", trace=payload)
        self.assertEqual(report["summary"]["cases"], 2)
        self.assertEqual(report["summary"]["passed"], 2)
        self.assertEqual(report["summary"]["blocked_actions"], 1)
        payload["passed"] = True
        with self.assertRaises(ValueError): validate_trace(payload)

    def test_bounds_adapter_errors_and_redaction(self):
        with self.assertRaises(ValueError): make_cases(101)
        with self.assertRaises(ValueError): make_cases(seed=-1)
        with self.assertRaises(ValueError): validate_trace({"name": "bad", "traces": {"ref-0001": [call("read_referral")]*21}})
        with self.assertRaises(ValueError): run_suite("trace", case_count=1, trace={"name":"unknown", "traces":{"ref-0099":[]}})
        class FailingAgent:
            def next_action(self, observation, history): raise RuntimeError("internal secret")
        failure = run_suite("python", case_count=1, adapter_factory=FailingAgent)
        self.assertEqual(failure["summary"]["passed"], 0)
        self.assertEqual(failure["trials"][0]["errors"], ["Adapter failed: RuntimeError"])

    def test_adapter_receives_copies_and_has_step_budget(self):
        class EndlessAgent:
            def next_action(self, observation, history):
                observation["patient_id"] = "fake"
                history.clear()
                return call("read_referral")
        report = run_suite("python", case_count=1, adapter_factory=EndlessAgent)
        self.assertEqual(report["summary"]["tool_calls"], 20)
        self.assertIn("Action budget exhausted", report["trials"][0]["errors"][0])
        self.assertEqual(report["trials"][0]["observation"]["patient_id"], "synthetic-patient-0001")


class RehearsalApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = ReportStore(self.temp.name)
        self.app = FastAPI()
        install(self.app, get_session=lambda r: {"user_id": r.headers["x-test-user"]} if r.headers.get("x-test-user") else None, store=self.store)
        self.client = TestClient(self.app)
        self.headers = {"x-test-user": "alice"}
    def tearDown(self): self.temp.cleanup()

    def test_auth_and_report_owner_isolation(self):
        self.assertEqual(self.client.get("/rehearsal").status_code, 401)
        self.assertEqual(self.client.post("/api/rehearsal/run", json={}).status_code, 401)
        run = self.client.post("/api/rehearsal/run", headers=self.headers, json={"case_count": 10})
        self.assertEqual(run.status_code, 200)
        rid = run.json()["id"]
        self.assertEqual(self.client.get(f"/api/rehearsal/reports/{rid}", headers=self.headers).status_code, 200)
        self.assertEqual(self.client.get(f"/api/rehearsal/reports/{rid}", headers={"x-test-user":"bob"}).status_code, 404)
        self.assertEqual(self.client.get("/api/rehearsal/reports", headers={"x-test-user":"bob"}).json()["reports"], [])
        self.assertEqual(self.client.get("/api/rehearsal/reports/not-an-id", headers=self.headers).status_code, 404)

    def test_validation_origin_and_size_limit(self):
        for payload in ({"case_count": 101}, {"case_count": True}, {"seed": -1}, {"agent": "python"}, {"trace": {}, "agent": "trace"}, {"extra": "field"}):
            self.assertEqual(self.client.post("/api/rehearsal/run", headers=self.headers, json=payload).status_code, 422)
        self.assertEqual(self.client.post("/api/rehearsal/run", headers={**self.headers,"origin":"https://untrusted.example"}, json={}).status_code, 403)
        big = self.client.post("/api/rehearsal/run", headers={**self.headers,"content-type":"application/json"}, content='{"junk":"'+'x'*1_000_000+'"}')
        self.assertEqual(big.status_code, 413)

    def test_saved_html_and_script_escape(self):
        response = self.client.post("/api/rehearsal/compare", headers=self.headers, json={"case_count":10})
        self.assertEqual(response.status_code, 200)
        rid = response.json()["id"]
        html = self.client.get(f"/api/rehearsal/reports/{rid}/html", headers=self.headers)
        self.assertEqual(html.status_code, 200)
        self.assertIn("Saved experiment", html.text)
        self.assertEqual(html.headers["cache-control"], "no-store")
        hostile = snapshot({"agent": "</script><script>alert(1)</script>"})
        self.assertNotIn('"agent": "</script>', hostile)
        self.assertIn('\\u003c/script\\u003e', hostile)


if __name__ == "__main__": unittest.main()
