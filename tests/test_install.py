import csv
import io
import json
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from scripts import install as installer


def make_wheel(directory: Path, version: str, *, help_exit: int = 0) -> Path:
    """Build a tiny valid wheel so tests exercise real venv and pip installs."""

    distribution = "codex_monitor"
    dist_info = f"{distribution}-{version}.dist-info"
    files = {
        f"{distribution}/__init__.py": f'__version__ = "{version}"\n',
        f"{distribution}/cli.py": (
            "import sys\n"
            f"VERSION = {version!r}\n"
            "def main():\n"
            "    if '--help' in sys.argv:\n"
            "        print('codex-monitor test help')\n"
            f"        return {help_exit}\n"
            "    if '--version' in sys.argv:\n"
            "        print(VERSION)\n"
            "        return 0\n"
            "    return 0\n"
        ),
        f"{dist_info}/METADATA": (
            "Metadata-Version: 2.1\n"
            "Name: codex-monitor\n"
            f"Version: {version}\n"
            "Requires-Python: >=3.11\n\n"
        ),
        f"{dist_info}/WHEEL": (
            "Wheel-Version: 1.0\n"
            "Generator: codex-monitor-tests\n"
            "Root-Is-Purelib: true\n"
            "Tag: py3-none-any\n"
        ),
        f"{dist_info}/entry_points.txt": "[console_scripts]\ncodex-monitor = codex_monitor.cli:main\n",
    }
    record = io.StringIO()
    writer = csv.writer(record, lineterminator="\n")
    for name in files:
        writer.writerow((name, "", ""))
    writer.writerow((f"{dist_info}/RECORD", "", ""))
    files[f"{dist_info}/RECORD"] = record.getvalue()
    wheel = directory / f"{distribution}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, body in files.items():
            archive.writestr(name, body)
    return wheel


class InstallTest(unittest.TestCase):
    def test_global_command_link_lifecycle_migration_and_path_status(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prefix = root / "runtime"
            bin_dir = root / "bin"
            wheel1 = make_wheel(root, "1.0.0")
            wheel2 = make_wheel(root, "2.0.0")

            with patch.object(installer, "DEFAULT_PREFIX", prefix), patch.object(
                installer, "DEFAULT_BIN_DIR", bin_dir
            ):
                manager = installer.Installer(prefix, wheel=wheel1)
                first = manager.install_or_upgrade("install")
                command = bin_dir / "codex-monitor"
                executable = prefix / "bin" / "codex-monitor"
                self.assertTrue(command.is_symlink())
                self.assertEqual(os.readlink(command), str(executable))
                self.assertTrue(first["link"]["created"])
                self.assertFalse(first["path_on_path"])
                self.assertIn(str(bin_dir), first["setup_hint"])

                tracked = installer.Installer(prefix, wheel=wheel2, no_command=True).install_or_upgrade(
                    "upgrade"
                )
                self.assertEqual(tracked["link_status"], "owned")
                self.assertTrue(command.is_symlink())

                marker_path = prefix / installer.MARKER
                marker = json.loads(marker_path.read_text())
                marker.pop("command")
                marker_path.write_text(json.dumps(marker) + "\n")
                command.unlink()
                migrated = installer.Installer(prefix, wheel=wheel1).link()
                self.assertEqual(migrated["action"], "linked")
                self.assertTrue(command.is_symlink())
                self.assertTrue(json.loads(marker_path.read_text()).get("command"))

                with patch.dict(os.environ, {"PATH": str(bin_dir)}):
                    repeated = installer.Installer(prefix, wheel=wheel1).link()
                self.assertFalse(repeated["link"]["created"])
                self.assertTrue(repeated["path_on_path"])
                self.assertIsNone(repeated["setup_hint"])

                previous_target = os.readlink(prefix / "current")
                upgraded = installer.Installer(prefix, wheel=wheel2).install_or_upgrade("upgrade")
                self.assertEqual(upgraded["version"], "2.0.0")
                self.assertEqual(os.readlink(command), str(executable))
                self.assertNotEqual(os.readlink(prefix / "current"), previous_target)

                removed = installer.Installer(prefix).uninstall()
                self.assertTrue(removed["link"]["removed"])
                self.assertFalse(command.exists())

    def test_link_does_not_touch_services_and_foreign_paths_fail_preflight(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prefix = root / "runtime"
            bin_dir = root / "bin"
            wheel = make_wheel(root, "1.0.0")
            launch_agents = root / "LaunchAgents"
            manager = installer.Installer(
                prefix, wheel=wheel, bin_dir=bin_dir, launch_agents_dir=launch_agents
            )
            manager.install_or_upgrade("install")

            launch_agents.mkdir()
            plist_path = launch_agents / "com.codex.monitor.test.plist"
            plist_path.write_bytes(
                plistlib.dumps(
                    {
                        "Label": "com.codex.monitor.test",
                        "ProgramArguments": [
                            str(prefix / "current" / "bin" / "python"),
                            "-m",
                            "codex_monitor",
                            "--state",
                            str(root / "state"),
                            "serve",
                        ],
                    }
                )
            )
            before = plist_path.read_bytes()
            manager.link()
            self.assertEqual(plist_path.read_bytes(), before)

            command = bin_dir / "codex-monitor"
            command.unlink()
            command.symlink_to(root / "foreign-target")
            current = os.readlink(prefix / "current")
            with self.assertRaisesRegex(installer.InstallError, "modified owned command link"):
                installer.Installer(prefix, wheel=wheel, bin_dir=bin_dir).install_or_upgrade("upgrade")
            self.assertEqual(os.readlink(prefix / "current"), current)
            with self.assertRaisesRegex(installer.InstallError, "modified owned command link"):
                installer.Installer(prefix, bin_dir=bin_dir).uninstall()
            self.assertTrue(prefix.exists())

            foreign_prefix = root / "foreign-runtime"
            foreign_bin = root / "foreign-bin"
            foreign_bin.mkdir()
            (foreign_bin / "codex-monitor").write_text("user command\n")
            with self.assertRaisesRegex(installer.InstallError, "foreign existing command path"):
                installer.Installer(foreign_prefix, wheel=wheel, bin_dir=foreign_bin).install_or_upgrade(
                    "install"
                )
            self.assertFalse(foreign_prefix.exists())

    def test_no_command_allows_foreign_default_path_and_legacy_uninstall_preserves_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prefix = root / "runtime"
            bin_dir = root / "bin"
            bin_dir.mkdir()
            command = bin_dir / "codex-monitor"
            command.write_text("user command\n")
            wheel1 = make_wheel(root, "1.0.0")
            wheel2 = make_wheel(root, "2.0.0")

            with patch.object(installer, "DEFAULT_PREFIX", prefix), patch.object(
                installer, "DEFAULT_BIN_DIR", bin_dir
            ):
                first = installer.Installer(prefix, wheel=wheel1, no_command=True).install_or_upgrade(
                    "install"
                )
                self.assertEqual(first["link_status"], "unmanaged")
                self.assertEqual(command.read_text(), "user command\n")

                second = installer.Installer(prefix, wheel=wheel2, no_command=True).install_or_upgrade(
                    "upgrade"
                )
                self.assertEqual(second["version"], "2.0.0")
                self.assertEqual(command.read_text(), "user command\n")

                # This is the old marker shape: no command ownership record.
                command.unlink()
                command.symlink_to(prefix / "bin" / "codex-monitor")
                removed = installer.Installer(prefix).uninstall()
                self.assertIsNone(removed["link"])
                self.assertTrue(command.is_symlink(), "legacy uninstall must preserve unrelated command paths")

    def test_no_command_and_bin_dir_are_mutually_exclusive(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(installer.InstallError, "cannot be combined"):
                installer.Installer(root / "runtime", bin_dir=root / "bin", no_command=True)

    def test_actual_install_guarded_upgrade_failed_candidate_and_uninstall(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prefix = root / "runtime"
            wheel1 = make_wheel(root, "1.0.0")
            wheel2 = make_wheel(root, "2.0.0")
            bad_wheel = make_wheel(root, "3.0.0", help_exit=7)
            skill_root = root / "skills"
            launch_agents = root / "LaunchAgents"
            state = root / "state"
            state.mkdir()
            (state / "config.json").write_text('{"secret":"preserve"}\n')

            # A relative wheel is resolved at invocation time, matching an
            # extracted release's `wheels/FILENAME.whl` command.
            previous = Path.cwd()
            os.chdir(root)
            try:
                first = installer.Installer(
                    prefix,
                    wheel=wheel1.name,
                    with_skill=True,
                    skill_root=skill_root,
                    launch_agents_dir=launch_agents,
                ).install_or_upgrade("install")
            finally:
                os.chdir(previous)

            executable = Path(first["executable"])
            self.assertEqual(executable, prefix / "bin" / "codex-monitor")
            self.assertEqual(
                subprocess.run([executable, "--version"], text=True, capture_output=True, check=True).stdout.strip(),
                "1.0.0",
            )
            first_target = os.readlink(prefix / "current")
            first_release = prefix / first_target
            self.assertTrue(first_release.is_dir())
            skill_file = skill_root / "codex-monitor" / "SKILL.md"
            original_skill = skill_file.read_text()
            skill_file.write_text(original_skill + "\nUser customization.\n")

            launch_agents.mkdir()
            plist_path = launch_agents / "com.codex.monitor.test.plist"
            plist_path.write_bytes(
                plistlib.dumps(
                    {
                        "Label": "com.codex.monitor.test",
                        "ProgramArguments": [
                            str(first_release / "bin" / "python"),
                            "-m",
                            "codex_monitor",
                            "--state",
                            str(state),
                            "serve",
                        ],
                    }
                )
            )
            guarded = installer.Installer(
                prefix,
                wheel=wheel2,
                with_skill=True,
                skill_root=skill_root,
                launch_agents_dir=launch_agents,
            )
            with self.assertRaises(installer.InstallError) as caught:
                guarded.install_or_upgrade("upgrade")
            message = str(caught.exception)
            self.assertIn("service stop", message)
            self.assertIn("service uninstall", message)
            self.assertIn(" upgrade", message)
            self.assertIn("service install", message)
            self.assertEqual(os.readlink(prefix / "current"), first_target)
            self.assertEqual(len(list((prefix / "releases").iterdir())), 1)

            plist_path.unlink()
            second = guarded.install_or_upgrade("upgrade")
            self.assertEqual(second["version"], "2.0.0")
            self.assertEqual(second["skill"]["preserved_modified"], ["SKILL.md"])
            self.assertTrue(skill_file.read_text().endswith("User customization.\n"))
            second_target = os.readlink(prefix / "current")
            self.assertNotEqual(second_target, first_target)
            self.assertTrue(first_release.is_dir(), "upgrade must not mutate or remove the prior release")
            self.assertEqual(
                subprocess.run([executable, "--version"], text=True, capture_output=True, check=True).stdout.strip(),
                "2.0.0",
            )

            with self.assertRaisesRegex(installer.InstallError, "CLI validation failed"):
                installer.Installer(prefix, wheel=bad_wheel).install_or_upgrade("upgrade")
            self.assertEqual(os.readlink(prefix / "current"), second_target)
            self.assertEqual(len(list((prefix / "releases").iterdir())), 2)

            with self.assertRaisesRegex(installer.InstallError, "modified or untracked skill"):
                installer.Installer(
                    prefix, with_skill=True, skill_root=skill_root, launch_agents_dir=launch_agents
                ).uninstall()
            self.assertTrue(prefix.exists(), "a skill conflict must not partially uninstall the runtime")

            with patch.dict(os.environ, {"CODEX_MONITOR_HOME": str(state)}):
                removed = installer.Installer(
                    prefix, skill_root=skill_root, launch_agents_dir=launch_agents
                ).uninstall()
            self.assertFalse(prefix.exists())
            self.assertTrue(skill_file.exists(), "skill removal requires explicit --with-skill")
            self.assertEqual((state / "config.json").read_text(), '{"secret":"preserve"}\n')
            self.assertEqual(removed["preserved_state"], str(state))

    def test_unmodified_owned_skill_is_removed_only_when_requested(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prefix = root / "runtime"
            skill_root = root / "skills"
            wheel = make_wheel(root, "1.0.0")
            manager = installer.Installer(prefix, wheel=wheel, with_skill=True, skill_root=skill_root)
            manager.install_or_upgrade("install")
            skill_path = skill_root / "codex-monitor"
            self.assertTrue((skill_path / installer.SKILL_MARKER).is_file())

            outside = root / "outside"
            outside.mkdir()
            managed_bin = prefix / "bin"
            saved_bin = prefix / "bin.saved"
            managed_bin.rename(saved_bin)
            managed_bin.symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(installer.InstallError, "symlinked managed directory"):
                manager.install_or_upgrade("upgrade")
            managed_bin.unlink()
            saved_bin.rename(managed_bin)

            references = skill_path / "references"
            saved_references = skill_path / "references.saved"
            references.rename(saved_references)
            references.symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(installer.InstallError, "symlinked skill path"):
                manager._apply_skill("upgrade")
            references.unlink()
            saved_references.rename(references)

            release = prefix / os.readlink(prefix / "current")
            nested_state = release / "lib" / "user-state" / "admin.token"
            nested_state.parent.mkdir(parents=True)
            nested_state.write_text("keep")
            with self.assertRaisesRegex(installer.InstallError, "changed or untracked release data"):
                installer.Installer(prefix, with_skill=True, skill_root=skill_root).uninstall()
            self.assertEqual(nested_state.read_text(), "keep")
            nested_state.unlink()
            nested_state.parent.rmdir()

            result = installer.Installer(prefix, with_skill=True, skill_root=skill_root).uninstall()
            self.assertTrue(result["skill"]["removed"])
            self.assertFalse(prefix.exists())
            self.assertFalse(skill_path.exists())

    def test_refuses_unowned_runtime_prefix_and_skill_destination(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wheel = make_wheel(root, "1.0.0")
            prefix = root / "runtime"
            prefix.mkdir()
            unrelated = prefix / "keep.txt"
            unrelated.write_text("mine")
            with self.assertRaisesRegex(installer.InstallError, "non-empty unowned prefix"):
                installer.Installer(prefix, wheel=wheel).install_or_upgrade("install")
            self.assertEqual(unrelated.read_text(), "mine")

            second_prefix = root / "second-runtime"
            skill_root = root / "skills"
            skill = skill_root / "codex-monitor"
            skill.mkdir(parents=True)
            custom = skill / "SKILL.md"
            custom.write_text("user-owned skill")
            with self.assertRaisesRegex(installer.InstallError, "unowned skill"):
                installer.Installer(
                    second_prefix, wheel=wheel, with_skill=True, skill_root=skill_root
                ).install_or_upgrade("install")
            self.assertEqual(custom.read_text(), "user-owned skill")
            self.assertFalse((second_prefix / "current").exists())

    def test_concurrent_or_foreign_lock_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wheel = make_wheel(root, "1.0.0")
            manager = installer.Installer(root / "runtime", wheel=wheel)
            with manager._mutation_lock():
                with self.assertRaisesRegex(installer.InstallError, "another installer process"):
                    manager.install_or_upgrade("install")
            self.assertTrue(manager.lock_path.is_file(), "the stable sibling lock prevents inode races")

            manager.lock_path.write_text("foreign data\n")
            with self.assertRaisesRegex(installer.InstallError, "unknown ownership"):
                manager.install_or_upgrade("install")
            self.assertEqual(manager.lock_path.read_text(), "foreign data\n")

    def test_refuses_uninstall_when_state_is_inside_runtime_prefix(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prefix = root / "runtime"
            wheel = make_wheel(root, "1.0.0")
            installer.Installer(prefix, wheel=wheel).install_or_upgrade("install")
            state = prefix / "state"
            state.mkdir()
            (state / "config.json").write_text("keep")
            with patch.dict(os.environ, {"CODEX_MONITOR_HOME": str(state)}):
                with self.assertRaisesRegex(installer.InstallError, "CODEX_MONITOR_HOME is inside"):
                    installer.Installer(prefix).uninstall()
            self.assertEqual((state / "config.json").read_text(), "keep")
            self.assertTrue(prefix.exists())


if __name__ == "__main__":
    unittest.main()
