from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from codex_monitor.lock import ProcessLock


class ProcessTest(unittest.TestCase):
    def test_second_receiver_rejected_and_sigkill_releases_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "receiver.lock"
            code = "from codex_monitor.lock import ProcessLock;import sys,time\nwith ProcessLock(sys.argv[1]):\n print('ready',flush=True)\n time.sleep(60)\n"
            process = subprocess.Popen([sys.executable, "-c", code, str(path)], stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(process.stdout.readline().strip(), "ready")
                with self.assertRaises(RuntimeError):
                    with ProcessLock(path): pass
                process.kill(); process.wait(timeout=3)
                with ProcessLock(path): pass
            finally:
                if process.poll() is None: process.kill(); process.wait()
                process.stdout.close()
