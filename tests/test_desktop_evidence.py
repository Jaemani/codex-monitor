import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("desktop_canary", Path(__file__).parents[1] / "scripts/desktop-live-canary.py")
canary = importlib.util.module_from_spec(spec)
spec.loader.exec_module(canary)


class DesktopEvidenceTest(unittest.TestCase):
    def test_marker_before_event_is_not_a_successful_event_response(self):
        turns = [{"items": [
            {"type": "agentMessage", "text": "marker"},
            {"type": "userMessage", "clientId": "codex-monitor:one"},
        ]}]
        result = canary.assess_history(turns, "codex-monitor:one", "marker")
        self.assertEqual(result["event_history_count"], 1)
        self.assertFalse(result["event_response_recorded"])

    def test_newest_first_turns_preserve_event_response_and_human_continuation(self):
        turns = [{"items": [
            {"type": "userMessage", "clientId": "codex-monitor:one"},
            {"type": "agentMessage", "text": "marker received"},
            {"type": "userMessage", "clientId": "human-after"},
        ]}, {"items": [{"type": "userMessage", "clientId": "human-before"}]}]
        self.assertEqual(canary.assess_history(turns, "codex-monitor:one", "marker"), {
            "event_history_count": 1, "event_response_recorded": True,
            "user_input_before_event": True, "user_input_after_event": True,
        })

    def test_duplicate_event_invalidates_response_claim(self):
        turns = [{"items": [
            {"type": "userMessage", "clientId": "codex-monitor:one"},
            {"type": "userMessage", "clientId": "codex-monitor:one"},
            {"type": "agentMessage", "text": "marker"},
        ]}]
        result = canary.assess_history(turns, "codex-monitor:one", "marker")
        self.assertEqual(result["event_history_count"], 2)
        self.assertFalse(result["event_response_recorded"])
