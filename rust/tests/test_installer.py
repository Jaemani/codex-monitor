#!/usr/bin/env python3
"""Black-box checks for the isolated Rust executable installer.

Run directly with ``python3 rust/tests/test_installer.py``.  This harness is
intentionally outside Cargo: it exercises the public installer CLI against
small disposable executable fixtures and never installs the candidate into a
user's real runtime prefix.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
INSTALLER = ROOT / "rust" / "installer.py"


class InstallerHarness(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="codex-monitor-rust-installer-")
        self.root = Path(self.temp.name)
        self.prefix = self.root / "prefix"
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def fixture(self, version: str, marker: Path | None = None) -> Path:
        path = self.root / f"codex-monitor-rs-{version}"
        marker_code = ""
        if marker is not None:
            marker_code = f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n"
        script = (
            "#!/usr/bin/env python3\n"
            "import sys\n"
            + marker_code
            + f"VERSION = {version!r}\n"
            + "if '--version' in sys.argv:\n"
            + "    print('codex-monitor-rs ' + VERSION)\n"
            + "elif '--help' in sys.argv:\n"
            + "    print('usage: codex-monitor-rs')\n"
            + "else:\n"
            + "    print('fixture ' + VERSION)\n"
        )
        path.write_text(script, encoding="utf-8")
        path.chmod(0o700)
        return path

    def invoke(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [sys.executable, str(INSTALLER), "--prefix", str(self.prefix), "--state", str(self.state), *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if check:
            self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def result(self, *args: str) -> dict:
        return json.loads(self.invoke(*args).stdout)

    def test_install_upgrade_explicit_rollback_and_uninstall_preserve_state(self) -> None:
        first = self.fixture("1.2.3")
        digest = hashlib.sha256(first.read_bytes()).hexdigest()
        installed = self.result("--binary", str(first), "--sha256", digest, "install")
        self.assertEqual(installed["action"], "installed")
        first_release = installed["release"]
        self.assertEqual(installed["sha256"], digest)
        self.assertEqual(os.readlink(self.prefix / "current"), f"releases/{first_release}")
        self.assertEqual(os.readlink(self.prefix / "bin" / "codex-monitor-rs"), "../current/bin/codex-monitor-rs")
        self.assertEqual((self.prefix / "current" / "bin" / "codex-monitor-rs").exists(), True)
        provenance = json.loads(
            (self.prefix / "releases" / first_release / "provenance.json").read_text(encoding="utf-8")
        )
        self.assertEqual(provenance["source_sha256"], digest)
        self.assertEqual(provenance["installed_sha256"], digest)

        second = self.fixture("2.0.0")
        upgraded = self.result("--binary", str(second), "upgrade")
        second_release = upgraded["release"]
        self.assertNotEqual(first_release, second_release)
        self.assertTrue((self.prefix / "releases" / first_release).is_dir())
        self.assertEqual(upgraded["version"], "2.0.0")

        (self.state / "rust.sqlite3").write_text("state", encoding="utf-8")
        refused_populated = self.invoke("rollback", "--release", first_release, check=False)
        self.assertNotEqual(refused_populated.returncode, 0)
        self.assertIn("state is populated", refused_populated.stderr)
        self.assertEqual(self.result("status")["current_release"], second_release)
        (self.state / "rust.sqlite3").unlink()
        rolled_back = self.result("rollback", "--release", first_release)
        self.assertEqual(rolled_back["release"], first_release)
        version = subprocess.run(
            [str(self.prefix / "bin" / "codex-monitor-rs"), "--version"],
            text=True,
            stdout=subprocess.PIPE,
            check=True,
        ).stdout.strip()
        self.assertEqual(version, "codex-monitor-rs 1.2.3")

        (self.state / "sentinel.txt").write_text("preserve me\n", encoding="utf-8")
        stray = self.prefix / "stray.txt"
        stray.write_text("leave this", encoding="utf-8")
        refused = self.invoke("uninstall", check=False)
        self.assertNotEqual(refused.returncode, 0)
        self.assertTrue(stray.exists())
        stray.unlink()
        removed = self.result("uninstall")
        self.assertTrue(removed["preserved_state"])
        self.assertFalse(self.prefix.exists())
        self.assertEqual((self.state / "sentinel.txt").read_text(encoding="utf-8"), "preserve me\n")

    def test_foreign_prefix_and_nested_state_are_refused(self) -> None:
        foreign = self.root / "foreign"
        foreign.mkdir()
        (foreign / "user-data").write_text("keep", encoding="utf-8")
        result = subprocess.run(
            [
                sys.executable,
                str(INSTALLER),
                "--prefix",
                str(foreign),
                "--state",
                str(self.state),
                "--binary",
                str(self.fixture("1.0.0")),
                "install",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((foreign / "user-data").exists())

        nested_state = self.prefix / "state"
        bad = subprocess.run(
            [
                sys.executable,
                str(INSTALLER),
                "--prefix",
                str(self.prefix),
                "--state",
                str(nested_state),
                "--binary",
                str(self.fixture("1.0.0")),
                "install",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("outside", bad.stderr)
        self.assertFalse(self.prefix.exists())

        resolved_state = self.root / "resolved-state-link"
        resolved_state.symlink_to(self.prefix / "state", target_is_directory=False)
        resolved_bad = subprocess.run(
            [
                sys.executable,
                str(INSTALLER),
                "--prefix",
                str(self.prefix),
                "--state",
                str(resolved_state),
                "--binary",
                str(self.fixture("1.0.0")),
                "install",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertNotEqual(resolved_bad.returncode, 0)
        self.assertIn("outside", resolved_bad.stderr)

    def test_checksum_version_and_release_tamper_guards(self) -> None:
        execution_marker = self.root / "source-executed"
        first = self.fixture("1.0.0", execution_marker)
        mismatch = self.invoke("--binary", str(first), "--sha256", "0" * 64, "install", check=False)
        self.assertNotEqual(mismatch.returncode, 0)
        self.assertIn("SHA256", mismatch.stderr)
        self.assertFalse(execution_marker.exists())
        self.assertFalse((self.prefix / "current").exists())

        self.result("--binary", str(first), "install")
        current = self.result("status")["current_release"]
        binary = self.prefix / "releases" / current / "bin" / "codex-monitor-rs"
        binary.write_text(binary.read_text(encoding="utf-8") + "# changed\n", encoding="utf-8")
        refused = self.invoke("status", check=False)
        self.assertNotEqual(refused.returncode, 0)
        rollback = self.invoke("rollback", "--release", current, check=False)
        self.assertNotEqual(rollback.returncode, 0)


if __name__ == "__main__":
    unittest.main()
