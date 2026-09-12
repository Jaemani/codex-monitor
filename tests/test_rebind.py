import tempfile
import unittest
from pathlib import Path
from codex_monitor.monitor import Monitor
from codex_monitor.errors import IngressError


class RebindTest(unittest.TestCase):
    def test_identity_receipts_and_inflight_guards(self):
        with tempfile.TemporaryDirectory() as root:
            m = Monitor(Path(root), lambda _: self.fail("rebind invoked a native session"))
            m.bind("route", "thread", "shared-local", ["source"])
            receipt = m.ingest("route", {"id": "one", "source": "source", "type": "test", "data": {}})
            before = m.event(receipt["delivery_id"])
            m.enable("route", False)
            with self.assertRaises(IngressError):
                m.rebind("route", "another-thread", "shared-local", "ws://127.0.0.1:8767")
            m.rebind("route", "thread", "shared-local", "ws://127.0.0.1:8767")
            self.assertEqual(m.event(receipt["delivery_id"]), before)
            self.assertFalse(m.bindings()[0]["enabled"])
            with self.assertRaises(IngressError):
                m.rebind("route", "thread", "shared-local", "local")
            with m.connect() as db:
                db.execute("UPDATE events SET state='uncertain'")
            with self.assertRaises(IngressError):
                m.rebind("route", "thread", "ws://127.0.0.1:8767", "local")
            self.assertEqual(m.bindings()[0]["endpoint"], "ws://127.0.0.1:8767")
