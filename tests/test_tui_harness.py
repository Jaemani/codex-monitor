"""No-model regressions for the owned PTY canary cleanup path."""
import importlib.util
from pathlib import Path
import signal
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "tui_canary_cleanup", Path(__file__).resolve().parents[1] / "scripts/tui-canary.py"
)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class TerminalCleanupTest(unittest.TestCase):
    def terminal(self):
        terminal = helper.Terminal.__new__(helper.Terminal)
        terminal.pid, terminal.fd = 12345, 123
        return terminal

    def test_exited_child_is_reaped_without_signaling_and_close_is_idempotent(self):
        terminal = self.terminal()
        with patch.object(helper.os, "waitpid", return_value=(12345, 0)), \
             patch.object(helper.os, "killpg") as group, \
             patch.object(helper.os, "kill") as child, \
             patch.object(helper.os, "close") as close:
            terminal.close()
            terminal.close()
            group.assert_not_called()
            child.assert_not_called()
            close.assert_called_once_with(123)

    def test_group_permission_failure_falls_back_to_owned_child(self):
        terminal = self.terminal()
        with patch.object(helper.os, "waitpid", side_effect=[(0, 0), (12345, 0)]), \
             patch.object(helper.os, "killpg", side_effect=PermissionError), \
             patch.object(helper.os, "kill") as child, \
             patch.object(helper.os, "close") as close:
            terminal.close()
            child.assert_called_once_with(12345, signal.SIGTERM)
            close.assert_called_once_with(123)

    def test_descriptor_closes_even_when_child_signal_fails(self):
        terminal = self.terminal()
        with patch.object(helper.os, "waitpid", return_value=(0, 0)), \
             patch.object(helper.os, "killpg", side_effect=PermissionError), \
             patch.object(helper.os, "kill", side_effect=PermissionError), \
             patch.object(helper.os, "close") as close:
            with self.assertRaises(PermissionError):
                terminal.close()
            close.assert_called_once_with(123)
