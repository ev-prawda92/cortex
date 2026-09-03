from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from authorization import Decision, evaluate, validate_profile


PROFILE = {
    "status": "active",
    "default_decision": "BLOCK",
    "credentials": [{"name": "pa-eval", "status": "valid", "expires_at": "2027-01-01T00:00:00Z"}],
    "privileges": [{
        "action": "deny_prior_authorization",
        "effect": "allow",
        "environments": ["production"],
        "data_scopes": ["assigned_members"],
        "target_systems": ["payer"],
        "required_credentials": ["pa-eval"],
        "min_evidence_items": 2,
        "required_evidence_types": ["benefit_policy", "clinical_documentation"],
        "requires_human_review": True,
    }],
}


def request(**updates):
    value = {
        "action": "deny_prior_authorization", "environment": "production",
        "data_scope": "assigned_members", "target_system": "payer",
        "evidence": [
            {"type": "benefit_policy", "current": True},
            {"type": "clinical_documentation", "current": True},
        ],
    }
    value.update(updates)
    return value


class AuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 3, tzinfo=timezone.utc)

    def test_valid_profile(self):
        self.assertEqual(validate_profile(PROFILE), [])

    def test_unknown_action_blocks(self):
        result = evaluate(PROFILE, request(action="delete_record"), self.now)
        self.assertEqual(result["decision"], Decision.BLOCK.value)

    def test_scope_violation_blocks(self):
        result = evaluate(PROFILE, request(data_scope="all_members"), self.now)
        self.assertEqual(result["decision"], Decision.BLOCK.value)

    def test_missing_evidence_requests_evidence(self):
        result = evaluate(PROFILE, request(evidence=[]), self.now)
        self.assertEqual(result["decision"], Decision.REQUEST_MORE_EVIDENCE.value)

    def test_human_review_is_required(self):
        result = evaluate(PROFILE, request(), self.now)
        self.assertEqual(result["decision"], Decision.HUMAN_REVIEW.value)

    def test_approved_action_is_allowed(self):
        result = evaluate(PROFILE, request(approval={"status": "approved"}), self.now)
        self.assertEqual(result["decision"], Decision.ALLOW.value)


if __name__ == "__main__":
    unittest.main()
