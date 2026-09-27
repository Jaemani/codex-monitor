import unittest
from pathlib import Path
from unittest.mock import patch
from codex_monitor.owner_reconnect import Job
from codex_monitor.permission_status import configured_permissions

class PermissionStatusTest(unittest.TestCase):
    def test_saved_policy_labels_are_scoped_and_not_claimed_effective(self):
        for mode, extra, expected in [("read-only",[],"Read-only"),
                                      ("danger-full-access",[],"Full Access"),
                                      ("workspace-write",["--network-access"],"Project Access")]:
            job=Job(Path('/tmp/test.plist'),'digest','test',tuple(['codex-monitor','resident','--endpoint','ws://127.0.0.1:1','--thread','a','--sandbox',mode]+extra),{},None)
            connections=[{'thread':'a','bindings':[{'endpoint':'ws://127.0.0.1:1'}]},
                         {'thread':'b','bindings':[{'endpoint':'ws://127.0.0.1:1'}]}]
            with patch('codex_monitor.permission_status.OwnerReconnect._jobs',return_value=[job]):
                configured_permissions('/tmp/test',connections)
            self.assertEqual(connections[0]['permission_label'],expected)
            self.assertFalse(connections[0]['bindings'][0]['permission']['effective_verified'])
            self.assertEqual(connections[1]['permission_label'],'Unknown')

    def test_unavailable_configuration_is_unknown(self):
        connections=[{'thread':'a','bindings':[{'endpoint':'remote'}]}]
        with patch('codex_monitor.permission_status.OwnerReconnect._jobs',side_effect=OSError):
            configured_permissions('/tmp/test',connections)
        self.assertEqual(connections[0]['permission_label'],'Unknown')

    def test_disabled_legacy_route_does_not_pollute_active_permission(self):
        job = Job(Path('/tmp/test.plist'), 'digest', 'test',
                  ('codex-monitor', 'resident', '--endpoint', 'current', '--thread', 'a',
                   '--sandbox', 'workspace-write', '--network-access'), {}, None)
        conversation = {'thread': 'a', 'bindings': [
            {'endpoint': 'legacy', 'enabled': False},
            {'endpoint': 'current', 'enabled': True}]}
        with patch('codex_monitor.permission_status.OwnerReconnect._jobs', return_value=[job]):
            configured_permissions('/tmp/test', [conversation])
        self.assertEqual(conversation['permission_label'], 'Project Access')
        self.assertEqual(conversation['bindings'][0]['permission']['label'], 'Unknown')
        conversation['bindings'][0]['enabled'] = True
        with patch('codex_monitor.permission_status.OwnerReconnect._jobs', return_value=[job]):
            configured_permissions('/tmp/test', [conversation])
        self.assertEqual(conversation['permission_label'], 'Mixed')
