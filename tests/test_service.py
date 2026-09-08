import json
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from codex_monitor.service import ServiceConfig, ServiceError, ServiceManager, service_label


class ServiceTest(unittest.TestCase):
    def _config(self, root: Path) -> ServiceConfig:
        python = root / "venv" / "bin" / "python"
        codex = root / "venv" / "bin" / "codex"
        python.parent.mkdir(parents=True)
        for executable in (python, codex):
            executable.write_text("#!/bin/sh\n")
            executable.chmod(0o755)
        return ServiceConfig.from_state(
            root / "state",
            codex_home=root / "codex-home",
            python_executable=python,
            codex_executable=codex,
            launch_agents_dir=root / "LaunchAgents",
        )

    def test_generated_plist_binds_absolute_runtime_paths_without_secrets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._config(root)
            payload = config.plist()

            self.assertEqual(
                payload["ProgramArguments"],
                [str(config.python_executable), "-m", "codex_monitor", "--state", str(config.state_dir), "serve"],
            )
            environment = payload["EnvironmentVariables"]
            self.assertEqual(environment["CODEX_HOME"], str((root / "codex-home").resolve()))
            self.assertEqual(environment["CODEX_MONITOR_HOME"], str(config.state_dir))
            self.assertEqual(environment["PATH"].split(os.pathsep)[0], str(root / "venv" / "bin"))
            self.assertNotIn("token", json.dumps(payload).lower())
            self.assertTrue(config.label.startswith("com.codex.monitor."))
            self.assertNotEqual(config.label, service_label(root / "other-state"))

    def test_python_symlink_stays_on_the_installed_venv_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            real = root / "venv" / "bin" / "python-real"
            link = root / "venv" / "bin" / "python"
            real.parent.mkdir(parents=True)
            real.write_text("#!/bin/sh\n")
            real.chmod(0o755)
            link.symlink_to(real.name)
            codex = root / "venv" / "bin" / "codex"
            codex.write_text("#!/bin/sh\n")
            codex.chmod(0o755)
            config = ServiceConfig.from_state(
                root / "state",
                codex_home=root / "codex-home",
                python_executable=link,
                codex_executable=codex,
                launch_agents_dir=root / "LaunchAgents",
            )
            self.assertEqual(config.python_executable, Path(os.path.abspath(link)))
            self.assertNotEqual(config.python_executable, config.python_executable.resolve())

    def test_sqlite_home_does_not_override_codex_home(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            state.mkdir()
            (state / "config.json").write_text(json.dumps({"sqlite_home": str(root / "wrong")}))
            python = root / "python"
            codex = root / "codex"
            for executable in (python, codex):
                executable.write_text("#!/bin/sh\n")
                executable.chmod(0o755)
            with patch.dict(os.environ, {"CODEX_HOME": ""}):
                config = ServiceConfig.from_state(
                    state,
                    python_executable=python,
                    codex_executable=codex,
                    launch_agents_dir=root / "LaunchAgents",
                )
                self.assertEqual(config.codex_home, (Path.home() / ".codex").resolve())

    def test_explicit_sqlite_environment_override_is_preserved_separately(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._config(root)
            sqlite_home = root / "sqlite-home"
            with patch.dict(os.environ, {"CODEX_SQLITE_HOME": str(sqlite_home)}):
                environment = config.plist()["EnvironmentVariables"]
            self.assertEqual(environment["CODEX_HOME"], str((root / "codex-home").resolve()))
            self.assertEqual(environment["CODEX_SQLITE_HOME"], str(sqlite_home.resolve()))

    def test_token_file_path_is_preserved_without_token_contents(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._config(root)
            token_file = root / "token"
            token_file.write_text("secret-value")
            with patch.dict(os.environ, {"CODEX_MONITOR_SERVER_TOKEN_FILE": str(token_file)}):
                payload = config.plist()
            environment = payload["EnvironmentVariables"]
            self.assertEqual(environment["CODEX_MONITOR_SERVER_TOKEN_FILE"], str(token_file.resolve()))
            self.assertNotIn("secret-value", json.dumps(payload))

    def test_install_start_stop_restart_uninstall_preserve_state_and_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._config(root)
            config.state_dir.mkdir(parents=True)
            original = {"version": 1, "port": 8766, "sources": {"build": {"token_file": "secret.path"}}}
            (config.state_dir / "config.json").write_text(json.dumps(original, sort_keys=True))
            before = (config.state_dir / "config.json").read_bytes()
            loaded = False
            calls = []

            def launchctl(argv, **kwargs):
                nonlocal loaded
                calls.append(argv)
                operation = argv[1]
                if operation == "print":
                    return subprocess.CompletedProcess(argv, 0 if loaded else 1, "", "not loaded")
                if operation == "bootstrap":
                    loaded = True
                elif operation == "bootout":
                    loaded = False
                return subprocess.CompletedProcess(argv, 0, "", "")

            manager = ServiceManager(config, require_installed=True)
            with patch("codex_monitor.service.sys.platform", "darwin"), patch(
                "codex_monitor.service.subprocess.run", side_effect=launchctl
            ):
                installed = manager.install()
                self.assertTrue(installed["installed"])
                self.assertTrue(installed["loaded"])
                manager.start()
                manager.stop()
                self.assertFalse(manager.status()["loaded"])
                manager.start()
                manager.restart()
                self.assertTrue(manager.status()["loaded"])
                manager.uninstall()

            self.assertFalse(config.plist_path.exists())
            self.assertEqual((config.state_dir / "config.json").read_bytes(), before)
            self.assertTrue((config.service_dir.stat().st_mode & 0o777) == 0o700)
            self.assertTrue(any(args[1] == "bootstrap" for args in calls))
            self.assertTrue(any(args[1] == "bootout" for args in calls))
            self.assertEqual(sum(args[1] == "kickstart" for args in calls), 1)
            self.assertFalse(any(args[1] == "rm" for args in calls))

    def test_install_refuses_foreign_plist_without_calling_launchctl(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._config(root)
            config.launch_agents_dir.mkdir(parents=True)
            config.plist_path.write_bytes(
                plistlib.dumps({"Label": "com.someone.elses.service"}, fmt=plistlib.FMT_XML)
            )
            original = config.plist_path.read_bytes()
            manager = ServiceManager(config, require_installed=True)
            with patch("codex_monitor.service.sys.platform", "darwin"), patch(
                "codex_monitor.service.subprocess.run"
            ) as launchctl:
                with self.assertRaisesRegex(ServiceError, "foreign"):
                    manager.install()
                launchctl.assert_not_called()
            self.assertEqual(config.plist_path.read_bytes(), original)

    def test_same_label_with_wrong_state_argv_is_foreign(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._config(root)
            config.launch_agents_dir.mkdir(parents=True)
            config.plist_path.write_bytes(
                plistlib.dumps(
                    {"Label": config.label, "ProgramArguments": [str(config.python_executable), "-m", "codex_monitor", "--state", str(root / "other"), "serve"]},
                    fmt=plistlib.FMT_XML,
                )
            )
            manager = ServiceManager(config, require_installed=True)
            with patch("codex_monitor.service.sys.platform", "darwin"), patch(
                "codex_monitor.service.subprocess.run"
            ) as launchctl:
                with self.assertRaisesRegex(ServiceError, "foreign"):
                    manager.uninstall()
                launchctl.assert_not_called()

    def test_editable_or_source_validation_blocks_install_before_writing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._config(root)
            manager = ServiceManager(config)
            with patch("codex_monitor.service.sys.platform", "darwin"), patch(
                "codex_monitor.service._validate_installed_package",
                side_effect=ServiceError("editable/source installs cannot back a durable launchd service"),
            ), patch("codex_monitor.service.subprocess.run") as launchctl:
                with self.assertRaisesRegex(ServiceError, "editable/source"):
                    manager.install()
                launchctl.assert_not_called()
            self.assertFalse(config.plist_path.exists())

    def test_non_macos_reports_unsupported_without_os_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = self._config(Path(temporary))
            manager = ServiceManager(config)
            with patch("codex_monitor.service.sys.platform", "linux"), patch(
                "codex_monitor.service.subprocess.run"
            ) as launchctl:
                with self.assertRaisesRegex(ServiceError, "only supported on macOS"):
                    manager.status()
                launchctl.assert_not_called()


if __name__ == "__main__":
    unittest.main()
