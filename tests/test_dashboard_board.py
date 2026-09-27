import os
import unittest

from codex_monitor.dashboard import _key, _cell_width
from codex_monitor.dashboard_board import build, navigate, retain_selection, wrap_cells


class DashboardBoardTest(unittest.TestCase):
    def test_refresh_retains_identity_and_detects_removed_routes(self):
        previous = self.snapshot()
        current = dict(previous, connections=list(reversed(previous["connections"])))
        self.assertEqual(retain_selection(previous, current, 3, 0), (10, 0, True))
        self.assertFalse(retain_selection(previous, {"connections": []}, 3, 0)[2])
        current = dict(previous, connections=[dict(c, bindings=[]) for c in previous["connections"]])
        self.assertFalse(retain_selection(previous, current, 3, 0)[2])

    def test_korean_errors_are_wrapped_without_losing_characters(self):
        message = "\ub85c\uadf8\uc544\uc6c3\ud558\uac70\ub098 \ub2e4\ub978 \uacc4\uc815\uc73c\ub85c \ub85c\uadf8\uc778\ud55c \uc774\ud6c4 \uc561\uc138\uc2a4 \ud1a0\ud070\uc744 \uac31\uc2e0\ud560 \uc218 \uc5c6\uc2b5\ub2c8\ub2e4. \ub2e4\uc2dc \ub85c\uadf8\uc778\ud558\uc138\uc694."
        rows = wrap_cells(message, 28)
        self.assertEqual("".join(rows), message)
        self.assertTrue(all(sum(_cell_width(c) for c in row) <= 28 for row in rows))

    def test_scroll_clamps_and_selected_project_title_remains_visible(self):
        board = build(self.snapshot(), width=80, height=24, selected=8)
        self.assertIn("Large", board.render())
        details = build(self.snapshot(), width=60, height=16, selected=3, detail=True, detail_scroll=10000)
        self.assertEqual(details.detail_offset, details.detail_limit)
        up = build(self.snapshot(), width=60, height=16, selected=3, detail=True,
                   detail_scroll=details.detail_offset - 1)
        self.assertEqual(up.detail_offset, max(0, details.detail_offset - 1))

    def snapshot(self):
        return {"ok": True, "receiver": {"ready": True}, "connections": [
            {"thread": str(i), "project": "Large" if i < 9 else f"Project {i}",
             "display_name": f"Conversation {i}", "bindings": [{
                 "name": f"route-{i}", "enabled": True,
                 "owner_health": {"status": "execution-error" if i == 3 else "ready-to-receive"},
                 "events": {"counts": {"pending": 5 if i == 3 else 0}},
             }]} for i in range(14)]}

    def test_summary_does_not_confuse_receiver_readiness_with_execution(self):
        text = build(self.snapshot(), width=120, height=40).render()
        for expected in ("Online", "1 failed", "5 pending", "NEEDS ATTENTION", "Check the failed run"):
            self.assertIn(expected, text)

    def test_selected_conversation_remains_visible_at_all_terminal_sizes(self):
        snapshot = self.snapshot()
        for width, height in ((24, 8), (40, 16), (80, 24), (100, 32), (160, 50)):
            for index in range(14):
                with self.subTest(width=width, height=height, selected=index):
                    board = build(snapshot, width=width, height=height, selected=index)
                    self.assertIn(index, board.positions)
                    self.assertTrue(any(hit == ("conversation", index) for hit in board.hits.values()))
                    self.assertEqual(len(board.render().splitlines()), height)
                    self.assertTrue(all(sum(_cell_width(c) for c in row) <= width for row in board.render().splitlines()))

    def test_spatial_navigation_reaches_every_conversation(self):
        connections = self.snapshot()["connections"]
        for width, height in ((40, 16), (100, 32), (160, 50)):
            visited, pending = set(), [0]
            while pending:
                selected = pending.pop()
                if selected in visited:
                    continue
                visited.add(selected)
                pending.extend(navigate(connections, width, selected, direction, height)
                               for direction in ("up", "down", "left", "right"))
            self.assertEqual(visited, set(range(14)))

    def test_keyboard_and_mouse_escape_sequences(self):
        for sequence, expected in ((b"\x1b", "escape"), (b"\x1b[C", "right"),
                                   (b"\x1b[D", "left"), (b"\x1b[<0;11;8M", "mouse:10:7"),
                                   (b"\x1b[<0;11;8m", None), (b"\x1b[<65;1;1M", "down")):
            read, write = os.pipe()
            try:
                os.write(write, sequence)
                with os.fdopen(read) as stream:
                    self.assertEqual(_key(stream), expected)
            finally:
                os.close(write)

    def test_inventory_failure_is_visible_and_overlay_has_only_action_targets(self):
        failed = build({"ok": False, "error": "database unavailable"}, width=80, height=24)
        self.assertIn("database unavailable", failed.render())
        board = build(self.snapshot(), width=100, height=32, selected=3, detail=True)
        self.assertEqual(set(board.hits.values()), {("action", i) for i in range(6)})
        self.assertIn("Large / Conversation 3", board.render())
        self.assertIn("Esc close", board.render())

    def test_execution_error_and_auth_action_are_visible_in_details(self):
        snapshot = self.snapshot()
        binding = snapshot["connections"][3]["bindings"][0]
        binding["events"]["latest"] = {"error": "Older delivery failure"}
        binding["owner_health"]["execution_error"] = {
            "status": "observed", "kind": "authentication",
            "message": "Your access token could not be refreshed. Please sign in again.",
        }
        board = build(snapshot, width=100, height=32, selected=3, detail=True)
        self.assertIn("Your access token could not be refreshed", board.render())
        self.assertIn("Reconnect", board.render())
        self.assertIn(("action", 3), board.hits.values())
        for width in (40, 60, 80):
            narrow = build(snapshot, width=width, height=24, selected=3, detail=True, action=3)
            self.assertIn("Reconnect", narrow.render())
            self.assertIn(("action", 3), narrow.hits.values())

    def test_retry_notice_is_wrapped_in_scrollable_content(self):
        message = "Account access verified. Open in Codex to retry the failed work; no work was replayed."
        board = build(self.snapshot(), width=60, height=24, selected=3, detail=True,
                      action=3, notice=message, notice_kind="AUTH")
        text = board.render()
        self.assertIn("Account access verified", text)
        self.assertIn("no work was replayed", text)


class PermissionButtonsTest(unittest.TestCase):
    def test_each_permission_action_is_clickable_when_selected(self):
        snapshot = DashboardBoardTest().snapshot()
        for width in (40, 80, 120):
            for index, label in ((0, "Full"), (1, "Read-only"), (2, "Project Access")):
                board = build(snapshot, width=width, height=32, selected=3, detail=True, action=index, permission_menu=True)
                self.assertIn(label, board.render())
                self.assertIn(("action", index), board.hits.values())

    def test_change_permission_is_separate_from_reconnect(self):
        snapshot = DashboardBoardTest().snapshot()
        board = build(snapshot, width=100, height=32, selected=3, detail=True, action=4)
        self.assertIn("Change Permission", board.render())
        self.assertNotIn("[ Full Access ]", board.render())
        menu = build(snapshot, width=100, height=32, selected=3, detail=True,
                     permission_menu=True, notice="Current saved resident policy: read-only.")
        self.assertIn("CHANGE PERMISSION", menu.render())
        self.assertIn("Current saved resident policy", menu.render())

    def test_permission_label_is_visible_in_overview_and_details(self):
        snapshot = DashboardBoardTest().snapshot()
        snapshot["connections"][3]["permission_label"] = "Full Access"
        for detail in (False, True):
            board = build(snapshot, width=100, height=40, selected=3, detail=detail)
            self.assertIn("Full Access", board.render())

    def test_all_detail_actions_are_visible_without_paging(self):
        for width in (40, 60, 80, 100, 120):
            board = build(DashboardBoardTest().snapshot(), width=width, height=32,
                          selected=3, detail=True, action=0)
            self.assertEqual(set(board.hits.values()), {("action", i) for i in range(6)})
            self.assertIn("Change Permission", board.render())
            self.assertIn("Close", board.render())

    def test_wide_details_keep_all_actions_on_one_row(self):
        for width in (100, 120, 160):
            board = build(DashboardBoardTest().snapshot(), width=width, height=32,
                          selected=3, detail=True)
            rows = {y for (x, y), action in board.hits.items() if action[0] == "action"}
            self.assertEqual(len(rows), 1)

    def test_overview_omits_configuration_jargon(self):
        snapshot = DashboardBoardTest().snapshot()
        snapshot["connections"][3]["permission_label"] = "Mixed"
        rendered = build(snapshot, width=100, height=40, selected=3).render()
        self.assertNotIn("(configured)", rendered)
        self.assertNotIn("Mixed", rendered)
        self.assertIn("Check permissions", rendered)
