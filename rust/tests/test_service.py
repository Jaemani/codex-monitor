#!/usr/bin/env python3
"""Mocked supervisor checks for the isolated Rust service integration."""

from __future__ import annotations

from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "rust"))

from installer import InstallError, Installer  # noqa: E402
from service import ServiceError, ServiceManager  # noqa: E402


class MockSupervisor:
    def __init__(self) -> None:
        self.loaded = False
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        if argv[0] == "launchctl":
            operation = argv[1]
            if operation == "print":
                return subprocess.CompletedProcess(argv, 0 if self.loaded else 1, "", "not loaded")
            if operation == "bootstrap":
                self.loaded = True
            elif operation == "bootout":
                self.loaded = False
            elif operation == "kickstart":
                self.loaded = True
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[:3] == ["systemctl", "--user", "is-active"]:
            return subprocess.CompletedProcess(argv, 0 if self.loaded else 3, "", "")
        if argv[:3] == ["systemctl", "--user", "enable"]:
            self.loaded = True
        elif argv[:3] == ["systemctl", "--user", "disable"]:
            self.loaded = False
        return subprocess.CompletedProcess(argv, 0, "", "")


class ServiceHarness(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="codex-monitor-rust-service-")
        self.root = Path(self.temp.name)
        self.prefix = self.root / "prefix"
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def binary(self, version: str) -> Path:
        path = self.root / f"codex-monitor-rs-{version}"
        path.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            f"VERSION = {version!r}\n"
            "if '--version' in sys.argv:\n"
            "    print('codex-monitor-rs ' + VERSION)\n"
            "elif '--help' in sys.argv:\n"
            "    print('usage')\n"
            "else:\n"
            "    print('serve')\n",
            encoding="utf-8",
        )
        path.chmod(0o700)
        return path

    def install(self, version: str = "1.0.0") -> Installer:
        installer = Installer(self.prefix, state=self.state, binary=self.binary(version))
        installer.install_or_upgrade("install")
        return installer

    def test_launchd_lifecycle_and_upgrade_guard(self) -> None:
        installer = self.install()
        supervisor = MockSupervisor()
        manager = ServiceManager(installer, platform="darwin", uid=501, runner=supervisor)

        installed = manager.install()
        self.assertTrue(installed["installed"])
        self.assertTrue(installed["loaded"])
        plist_path = self.prefix / "service" / f"{manager.label}.plist"
        payload = plistlib.loads(plist_path.read_bytes())
        self.assertEqual(
            payload["ProgramArguments"],
            [
                installed["command"][0],
                "--state",
                str(self.state),
                "serve",
            ],
        )
        self.assertIn("/releases/", payload["ProgramArguments"][0])
        self.assertTrue(any(call[1] == "bootstrap" for call in supervisor.calls))

        with self.assertRaisesRegex(InstallError, "service is installed"):
            Installer(self.prefix, state=self.state, binary=self.binary("2.0.0")).install_or_upgrade("upgrade")
        self.assertEqual(installer.status()["version"], "1.0.0")

        stopped = manager.stop()
        self.assertFalse(stopped["loaded"])
        self.assertTrue((self.prefix / ".codex-monitor-rs-service.json").exists())
        started = manager.start()
        self.assertTrue(started["loaded"])
        restarted = manager.restart()
        self.assertTrue(restarted["loaded"])
        self.assertTrue(any(call[1:3] == ["kickstart", "-k"] for call in supervisor.calls))
        removed = manager.remove()
        self.assertFalse(removed["installed"])
        self.assertFalse(plist_path.exists())
        self.assertFalse((self.prefix / ".codex-monitor-rs-service.json").exists())

        upgraded = Installer(self.prefix, state=self.state, binary=self.binary("2.0.0"))
        upgraded.install_or_upgrade("upgrade")
        self.assertEqual(upgraded.status()["version"], "2.0.0")

    def test_foreign_and_modified_service_data_are_refused_without_supervisor_calls(self) -> None:
        installer = self.install()
        service_dir = self.prefix / "service"
        service_dir.mkdir()
        foreign = service_dir / "foreign.service"
        foreign.write_text("owned by someone else", encoding="utf-8")
        supervisor = MockSupervisor()
        manager = ServiceManager(installer, platform="darwin", uid=501, runner=supervisor)
        with self.assertRaisesRegex(ServiceError, "untracked service data"):
            manager.install()
        self.assertEqual(supervisor.calls, [])
        foreign.unlink()
        service_dir.rmdir()

        manager.install()
        service_path = Path(manager.status()["service"])
        service_path.write_text(service_path.read_text(encoding="utf-8") + "# changed\n", encoding="utf-8")
        before = list(supervisor.calls)
        with self.assertRaisesRegex(ServiceError, "modified"):
            manager.status()
        self.assertEqual(supervisor.calls, before)

    def test_service_command_pins_an_immutable_release_binary(self) -> None:
        installer = self.install()
        supervisor = MockSupervisor()
        manager = ServiceManager(installer, platform="darwin", uid=501, runner=supervisor)
        manager.install()
        payload = plistlib.loads((self.prefix / "service" / f"{manager.label}.plist").read_bytes())
        binary = Path(payload["ProgramArguments"][0])
        self.assertEqual(
            binary,
            self.prefix / "releases" / installer.status()["current_release"] / "bin" / "codex-monitor-rs",
        )
        self.assertTrue(binary.is_file())
        manager.remove()
        self.assertFalse((self.prefix / "service").exists())


if __name__ == "__main__":
    unittest.main()
