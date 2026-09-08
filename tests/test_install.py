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
