"""A plain-text brief preserves the job and makes no claim to have connected data."""
from unittest import TestCase

from agent_builder import build_brief, list_starters


class AgentBriefTests(TestCase):
    def test_preserves_custom_instructions(self):
        text = "Read these records.\nCite sources; ask me when evidence is missing."
        brief = build_brief("  " + text + "  ", "My checker")
        self.assertEqual(brief["instructions"], text)
        self.assertEqual(brief["name"], "My checker")
        self.assertEqual(brief["status"], "stopped")
        self.assertIsNone(brief["starter_id"])

    def test_all_starters_have_examples_and_required_data(self):
        for starter in list_starters():
            brief = build_brief(starter["prompt"], starter_id=starter["id"])
            self.assertEqual(brief["name"], starter["name"])
            self.assertEqual(brief["required_data"], starter["needs"])
            self.assertTrue(starter["sample"])
            self.assertTrue(starter["expected"])

    def test_returned_catalog_cannot_modify_future_briefs(self):
        starters = list_starters()
        starters[0]["needs"].clear()
        self.assertTrue(list_starters()[0]["needs"])

    def test_refuses_empty_instructions_and_unknown_starters(self):
        for text, starter in [(" " * 20, None), ("Review the provided records.", "missing")]:
            with self.assertRaises(ValueError):
                build_brief(text, starter_id=starter)
