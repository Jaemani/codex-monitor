#!/usr/bin/env python3
"""Install codex-monitor from this checkout or an explicit local wheel.

The installer owns one prefix, creates immutable versioned virtual
environments, and switches a stable launcher through an atomic symlink.  It
never installs from a package index by package name and never mutates an
active release in place.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile


PRODUCT = "codex-monitor"
MODULE = "codex_monitor"
MARKER = ".codex-monitor-installer.json"
MARKER_OWNER = "codex-monitor-local-installer"
SKILL_NAME = "codex-monitor"
SKILL_MARKER = ".codex-monitor-skill-install.json"
SKILL_OWNER = "codex-monitor-skill-installer"
SCHEMA = 1
REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_SOURCE = REPO_ROOT / "plugins" / "codex-monitor" / "skills" / SKILL_NAME
DEFAULT_PREFIX = Path.home() / ".local" / "share" / PRODUCT
DEFAULT_BIN_DIR = Path.home() / ".local" / "bin"


class InstallError(RuntimeError):
    pass


def _absolute(value: str | Path, label: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise InstallError(f"{label} must be an absolute path: {value}")
    return Path(os.path.abspath(os.fspath(path)))


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("x") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_json(path: Path, label: str) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise InstallError(f"cannot read {label}: {path}") from exc
    if not isinstance(value, dict):
        raise InstallError(f"invalid {label}: {path}")
    return value


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _run(argv: list[str], label: str, *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:
        raise InstallError(f"cannot run {label}: {exc}") from exc
    if result.returncode != 0:
        raise InstallError(f"{label} failed with exit code {result.returncode}")
    return result


def _wheel_identity(path: Path) -> tuple[str, str]:
    try:
        with zipfile.ZipFile(path) as archive:
            names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
            if len(names) != 1:
                raise InstallError(f"wheel must contain exactly one METADATA file: {path}")
            metadata = archive.read(names[0]).decode("utf-8")
    except (OSError, UnicodeError, zipfile.BadZipFile, KeyError) as exc:
        raise InstallError(f"invalid wheel: {path}") from exc
    fields = {}
    for line in metadata.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            fields.setdefault(key.strip(), value.strip())
    name = re.sub(r"[-_.]+", "-", fields.get("Name", "")).lower()
    version = fields.get("Version", "")
    if name != PRODUCT or not version or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+!-]*", version):
        raise InstallError(f"wheel is not a valid {PRODUCT} distribution: {path}")
    return name, version


def _skill_files(source: Path) -> dict[str, Path]:
    if not source.is_dir() or not (source / "SKILL.md").is_file():
        raise InstallError(f"codex-monitor skill source is missing: {source}")
    files: dict[str, Path] = {}
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise InstallError(f"skill source symlinks are not supported: {path}")
        if path.is_file():
            relative = path.relative_to(source).as_posix()
            if relative == SKILL_MARKER:
                raise InstallError(f"skill source uses reserved file name: {SKILL_MARKER}")
            files[relative] = path
    return files


def _valid_manifest_files(value: object, label: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise InstallError(f"invalid {label} file list")
    result: dict[str, str] = {}
    for relative, digest in value.items():
        if not isinstance(relative, str):
            raise InstallError(f"invalid path or hash in {label}: {relative!r}")
        path = Path(relative)
        if (
            not relative
            or path.is_absolute()
            or ".." in path.parts
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise InstallError(f"invalid path or hash in {label}: {relative!r}")
        result[relative] = digest
    return result


def _release_inventory(root: Path) -> dict[str, str]:
    files = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            files[relative] = "symlink:" + os.readlink(path)
        elif path.is_file():
            files[relative] = "sha256:" + _digest(path)
    return files


def _valid_release_inventory(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        raise InstallError("invalid release inventory")
    result = {}
    for relative, fingerprint in value.items():
        if not isinstance(relative, str) or not isinstance(fingerprint, str):
            raise InstallError("invalid release inventory entry")
        path = Path(relative)
        if not relative or path.is_absolute() or ".." in path.parts:
            raise InstallError(f"invalid release inventory path: {relative!r}")
        if not (
            re.fullmatch(r"sha256:[0-9a-f]{64}", fingerprint)
            or (fingerprint.startswith("symlink:") and "\x00" not in fingerprint)
        ):
            raise InstallError(f"invalid release inventory fingerprint: {relative!r}")
        result[relative] = fingerprint
    return result


class Installer:
    def __init__(
        self,
        prefix: Path | str,
        *,
        wheel: Path | str | None = None,
        with_skill: bool = False,
        skill_root: Path | str | None = None,
        launch_agents_dir: Path | str | None = None,
        bin_dir: Path | str | None = None,
        no_command: bool = False,
    ):
        self.prefix = _absolute(prefix, "prefix")
        if self.prefix in (Path("/"), Path.home()) or self.prefix.is_symlink():
            raise InstallError(f"refusing unsafe or symlinked prefix: {self.prefix}")
        self.wheel_arg = Path(wheel).expanduser().resolve() if wheel is not None else None
        if self.wheel_arg is not None and (not self.wheel_arg.is_file() or self.wheel_arg.suffix != ".whl"):
            raise InstallError(f"wheel must be an existing absolute .whl file: {self.wheel_arg}")
        self.with_skill = with_skill
        default_skill_root = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "skills"
        self.skill_root = _absolute(skill_root or default_skill_root, "skill root")
        self.launch_agents_dir = _absolute(
            launch_agents_dir or (Path.home() / "Library" / "LaunchAgents"),
            "LaunchAgents directory",
        )
        if no_command and bin_dir is not None:
            raise InstallError("--no-command cannot be combined with --bin-dir")
        self.bin_dir = _absolute(bin_dir, "bin directory") if bin_dir is not None else None
        self.no_command = no_command

    @property
    def marker_path(self) -> Path:
        return self.prefix / MARKER

    @property
    def releases_dir(self) -> Path:
        return self.prefix / "releases"

    @property
    def current_path(self) -> Path:
        return self.prefix / "current"

    @property
    def executable(self) -> Path:
        return self.prefix / "bin" / PRODUCT

    @property
    def default_command_path(self) -> Path | None:
        if self.bin_dir is not None:
            return self.bin_dir / PRODUCT
        if self.no_command:
            return None
        if self.prefix == _absolute(DEFAULT_PREFIX, "default prefix"):
            return _absolute(DEFAULT_BIN_DIR, "default bin directory") / PRODUCT
        return None

    @property
    def skill_path(self) -> Path:
        return self.skill_root / SKILL_NAME

    @property
    def lock_path(self) -> Path:
        digest = hashlib.sha256(os.fsencode(str(self.prefix))).hexdigest()[:16]
        return self.prefix.parent / f".{PRODUCT}-install-{digest}.lock"

    @contextmanager
    def _mutation_lock(self):
        """Fail fast when another installer is changing this exact prefix."""

        if os.name == "nt":
            raise InstallError("this installer is not supported on Windows because atomic symlink switching is unverified")
        import fcntl

        self.prefix.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        created = False
        try:
            descriptor = os.open(self.lock_path, flags | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
        except FileExistsError:
            try:
                descriptor = os.open(self.lock_path, flags)
            except OSError as exc:
                raise InstallError(f"cannot safely open installer lock: {self.lock_path}") from exc
        try:
            details = os.fstat(descriptor)
            if (
                not stat.S_ISREG(details.st_mode)
                or details.st_uid != os.getuid()
                or stat.S_IMODE(details.st_mode) & 0o077
            ):
                raise InstallError(f"refusing foreign or non-regular installer lock: {self.lock_path}")
            expected = json.dumps(
                {"owner": MARKER_OWNER, "prefix": str(self.prefix)}, sort_keys=True
            ).encode() + b"\n"
            if created:
                os.write(descriptor, expected)
                os.fsync(descriptor)
            else:
                os.lseek(descriptor, 0, os.SEEK_SET)
                if os.read(descriptor, len(expected) + 1) != expected:
                    raise InstallError(f"refusing installer lock with unknown ownership: {self.lock_path}")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise InstallError(f"another installer process is changing this prefix: {self.prefix}") from exc
            yield
        finally:
            os.close(descriptor)

    def _validate_runtime_paths(self, marker: dict) -> None:
        for path in (self.releases_dir, self.executable.parent):
            if path.is_symlink():
                raise InstallError(f"refusing symlinked managed directory: {path}")
            if path.exists() and not path.is_dir():
                raise InstallError(f"managed path is not a directory: {path}")
        for release in marker.get("releases", []):
            path = self.releases_dir / release["id"]
            if path.is_symlink():
                raise InstallError(f"refusing symlinked managed release: {path}")

    def _claim_or_read(self) -> dict:
        if self.prefix.exists() and not self.prefix.is_dir():
            raise InstallError(f"prefix is not a directory: {self.prefix}")
        if not self.prefix.exists():
            self.prefix.mkdir(parents=True, mode=0o700)
        if not self.marker_path.exists():
            if any(self.prefix.iterdir()):
                raise InstallError(f"refusing to claim non-empty unowned prefix: {self.prefix}")
            marker = {"schema": SCHEMA, "owner": MARKER_OWNER, "product": PRODUCT, "releases": []}
            _atomic_json(self.marker_path, marker)
            return marker
        marker = _read_json(self.marker_path, "installer ownership marker")
        if marker.get("schema") != SCHEMA or marker.get("owner") != MARKER_OWNER or marker.get("product") != PRODUCT:
            raise InstallError(f"refusing prefix with an unknown ownership marker: {self.prefix}")
        releases = marker.get("releases")
        if not isinstance(releases, list) or any(
            not isinstance(item, dict)
            or not isinstance(item.get("id"), str)
            or not isinstance(item.get("version"), str)
            or not isinstance(item.get("files"), dict)
            for item in releases
        ):
            raise InstallError(f"invalid installer ownership marker: {self.marker_path}")
        for release in releases:
            _valid_release_inventory(release["files"])
        self._marker_command(marker)
        return marker

    def _read_owned(self) -> dict:
        if not self.marker_path.is_file():
            raise InstallError(f"codex-monitor is not installed at owned prefix: {self.prefix}")
        return self._claim_or_read()

    def _current_release(self, marker: dict) -> str | None:
        if not self.current_path.exists() and not self.current_path.is_symlink():
            return None
        if not self.current_path.is_symlink():
            raise InstallError(f"managed current pointer is not a symlink: {self.current_path}")
        target = Path(os.readlink(self.current_path))
        if target.is_absolute() or len(target.parts) != 2 or target.parts[0] != "releases":
            raise InstallError(f"managed current pointer has an unsafe target: {self.current_path}")
        release_id = target.parts[1]
        known = {item["id"] for item in marker["releases"]}
        if release_id not in known or not (self.releases_dir / release_id).is_dir():
            raise InstallError(f"managed current pointer does not name an owned release: {self.current_path}")
        return release_id

    def _check_shim(self) -> None:
        if self.executable.parent.is_symlink():
            raise InstallError(f"refusing symlinked managed bin directory: {self.executable.parent}")
        if not self.executable.exists() and not self.executable.is_symlink():
            return
        expected = Path("..") / "current" / "bin" / PRODUCT
        if not self.executable.is_symlink() or Path(os.readlink(self.executable)) != expected:
            raise InstallError(f"refusing to replace modified launcher: {self.executable}")

    def _ensure_shim(self) -> None:
        self._check_shim()
        self.executable.parent.mkdir(parents=True, exist_ok=True)
        if not self.executable.is_symlink():
            self.executable.symlink_to(Path("..") / "current" / "bin" / PRODUCT)

    def _marker_command(self, marker: dict) -> dict | None:
        value = marker.get("command")
        if value is None:
            return None
        if not isinstance(value, dict):
            raise InstallError(f"invalid command ownership marker: {self.marker_path}")
        path = value.get("path")
        target = value.get("target")
        if (
            not isinstance(path, str)
            or not isinstance(target, str)
            or not path
            or not target
            or not Path(path).is_absolute()
            or "\x00" in path
            or "\x00" in target
            or Path(path).name != PRODUCT
            or target != str(self.executable)
        ):
            raise InstallError(f"invalid command ownership marker: {self.marker_path}")
        return {"path": path, "target": target}

    def _command_path(self, marker: dict | None) -> Path | None:
        recorded = self._marker_command(marker) if marker is not None else None
        if recorded is not None:
            recorded_path = Path(recorded["path"])
            configured = self.bin_dir / PRODUCT if self.bin_dir is not None else None
            if configured is not None and recorded_path != configured:
                raise InstallError(
                    "configured bin directory differs from the owned command path: "
                    f"{configured.parent} != {recorded_path.parent}"
                )
            return recorded_path
        return self.default_command_path

    def _command_record(self, path: Path) -> dict[str, str]:
        if path == self.executable:
            raise InstallError(f"refusing command link that replaces the managed launcher: {path}")
        try:
            path.relative_to(self.prefix)
        except ValueError:
            pass
        else:
            raise InstallError(f"refusing command link inside the managed runtime prefix: {path}")
        return {"path": str(path), "target": str(self.executable)}

    def _preflight_command(self, action: str, marker: dict | None) -> Path | None:
        if action == "uninstall" and marker is not None and self._marker_command(marker) is None:
            # Legacy markers predate the PATH command.  An unrelated command
            # at the default location must remain entirely outside uninstall.
            return None
        path = self._command_path(marker)
        if path is None:
            if action == "link":
                raise InstallError(
                    "link requires --bin-dir when installing a non-default prefix"
                )
            return None
        record = self._command_record(path)
        parent = path.parent
        if parent.is_symlink():
            raise InstallError(f"refusing symlinked command directory: {parent}")
        if parent.exists() and not parent.is_dir():
            raise InstallError(f"command directory is not a directory: {parent}")
        exists = path.exists() or path.is_symlink()
        if not exists:
            if marker is not None and marker.get("command") is not None:
                # A missing link owned by this installer is safe to recreate.
                return path
            return path
        if marker is None or self._marker_command(marker) is None:
            raise InstallError(f"refusing foreign existing command path: {path}")
        if not path.is_symlink() or os.readlink(path) != record["target"]:
            raise InstallError(f"refusing modified owned command link: {path}")
        return path

    def _ensure_command(self, marker: dict) -> dict | None:
        path = self._preflight_command("install", marker)
        if path is None:
            return None
        record = self._command_record(path)
        created = False
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists() and not path.is_symlink():
                path.symlink_to(record["target"])
                created = True
            if marker.get("command") != record:
                marker["command"] = record
                _atomic_json(self.marker_path, marker)
        except Exception:
            if created:
                try:
                    if path.is_symlink() and os.readlink(path) == record["target"]:
                        path.unlink()
                except FileNotFoundError:
                    pass
            raise
        return {
            "path": str(path),
            "target": record["target"],
            "created": created,
        }

    def _remove_command(self, marker: dict) -> dict | None:
        if self._marker_command(marker) is None:
            return None
        path = self._command_path(marker)
        if path is None:
            return None
        record = self._command_record(path)
        if not path.exists() and not path.is_symlink():
            return {"path": str(path), "removed": False}
        if not path.is_symlink() or os.readlink(path) != record["target"]:
            raise InstallError(f"refusing to remove modified owned command link: {path}")
        path.unlink()
        return {"path": str(path), "removed": True}

    def _command_status(self, marker: dict | None) -> dict:
        path = self._command_path(marker)
        command = path or self.executable
        recorded = self._marker_command(marker) if marker is not None else None
        if path is None:
            link_status = "unmanaged"
        elif not path.exists() and not path.is_symlink():
            link_status = "missing"
        elif recorded is None:
            link_status = "foreign"
        elif path.is_symlink() and os.readlink(path) == recorded["target"]:
            link_status = "owned"
        else:
            link_status = "modified"
        path_on_path = any(
            Path(os.path.abspath(entry or os.curdir)) == command.parent
            for entry in os.environ.get("PATH", "").split(os.pathsep)
        )
        effective = shutil.which(PRODUCT)
        effective_path = os.path.abspath(effective) if effective else None
        setup_hint = None
        if not path_on_path:
            setup_hint = f'Add {command.parent} to PATH, for example: export PATH="{command.parent}:$PATH"'
        return {
            "command": str(command),
            "link_status": link_status,
            "path_on_path": path_on_path,
            "effective_command": effective_path,
            "command_shadowed": bool(path_on_path and effective_path and effective_path != str(command)),
            "setup_hint": setup_hint,
        }

    def _build_wheel(self, directory: Path) -> Path:
        _run(
            [sys.executable, "-m", "pip", "wheel", "--no-deps", "--wheel-dir", str(directory), str(REPO_ROOT)],
            "local wheel build",
            cwd=REPO_ROOT,
        )
        wheels = list(directory.glob("*.whl"))
        matches = []
        for wheel in wheels:
            try:
                name, _ = _wheel_identity(wheel)
            except InstallError:
                continue
            if name == PRODUCT:
                matches.append(wheel)
        if len(matches) != 1:
            raise InstallError("local build did not produce exactly one codex-monitor wheel")
        return matches[0]

    def _prepare_release(self, wheel: Path) -> tuple[Path, dict]:
        _, version = _wheel_identity(wheel)
        self.releases_dir.mkdir(parents=True, exist_ok=True)
        release_id = f"{version}-{time.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
        release = self.releases_dir / release_id
        try:
            _run([sys.executable, "-m", "venv", str(release)], "virtual environment creation")
            python = release / "bin" / "python"
            cli = release / "bin" / PRODUCT
            _run([str(python), "-m", "pip", "install", str(wheel)], "wheel installation", cwd=release)
            _run([str(python), "-m", "pip", "check"], "installed dependency check", cwd=release)
            _run([str(cli), "--help"], "installed CLI validation", cwd=release)
            installed_version = _run(
                [str(python), "-c", "import importlib.metadata; print(importlib.metadata.version('codex-monitor'))"],
                "installed version validation",
                cwd=release,
            ).stdout.strip()
            if installed_version != version:
                raise InstallError(
                    f"installed version {installed_version!r} does not match wheel version {version!r}"
                )
        except Exception:
            shutil.rmtree(release, ignore_errors=True)
            raise
        return release, {
            "id": release_id,
            "version": version,
            "installed_at": int(time.time()),
            "files": _release_inventory(release),
        }

    def _switch_current(self, release_id: str) -> None:
        temporary = self.prefix / f".current-{uuid.uuid4().hex}"
        try:
            temporary.symlink_to(Path("releases") / release_id)
            os.replace(temporary, self.current_path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _service_records(self) -> list[dict]:
        if not self.launch_agents_dir.is_dir():
            return []
        records = []
        for path in sorted(self.launch_agents_dir.glob("com.codex.monitor.*.plist")):
            try:
                with path.open("rb") as stream:
                    payload = plistlib.load(stream)
            except (OSError, ValueError, plistlib.InvalidFileException):
                continue
            arguments = payload.get("ProgramArguments")
            if not isinstance(arguments, list) or len(arguments) < 6 or not all(isinstance(x, str) for x in arguments):
                continue
            python = Path(os.path.abspath(arguments[0]))
            try:
                relative = python.relative_to(self.prefix)
            except ValueError:
                continue
            if not relative.parts or relative.parts[0] not in ("releases", "current"):
                continue
            if arguments[1:3] != ["-m", MODULE] or arguments[-1] != "serve" or "--state" not in arguments:
                continue
            state_index = arguments.index("--state") + 1
            if state_index >= len(arguments):
                continue
            label = payload.get("Label", path.stem)
            loaded = False
            if sys.platform == "darwin" and isinstance(label, str):
                target = f"gui/{os.getuid()}/{label}"
                try:
                    loaded = subprocess.run(
                        ["launchctl", "print", target],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                    ).returncode == 0
                except OSError:
                    loaded = False
            records.append(
                {
                    "plist": str(path),
                    "label": label,
                    "loaded": loaded,
                    "python": str(python),
                    "cli": str(python.parent / PRODUCT),
                    "state": arguments[state_index],
                }
            )
        return records

    def _refuse_managed_services(self, action: str) -> None:
        records = self._service_records()
        if not records:
            return
        commands: list[str] = []
        for record in records:
            cli = record["cli"]
            state = record["state"]
            commands.append(shlex.join([cli, "--state", state, "service", "stop"]))
            commands.append(shlex.join([cli, "--state", state, "service", "uninstall"]))
        if action == "upgrade":
            installer = [sys.executable, str(Path(__file__).resolve()), "--prefix", str(self.prefix)]
            if self.wheel_arg:
                installer += ["--wheel", str(self.wheel_arg)]
            if self.with_skill:
                installer += ["--with-skill", "--skill-root", str(self.skill_root)]
            installer.append("upgrade")
            commands.append(shlex.join(installer))
            for record in records:
                commands.append(
                    shlex.join([str(self.executable), "--state", record["state"], "service", "install"])
                )
        else:
            installer = [sys.executable, str(Path(__file__).resolve()), "--prefix", str(self.prefix)]
            if self.with_skill:
                installer += ["--with-skill", "--skill-root", str(self.skill_root)]
            installer.append("uninstall")
            commands.append(shlex.join(installer))
        detail = "\n  ".join(commands)
        raise InstallError(
            "a launchd service still references this managed runtime; refusing to change its interpreter in place. "
            f"Run these explicit steps:\n  {detail}"
        )

    def _read_skill_marker(self) -> tuple[dict, dict[str, str]]:
        marker_path = self.skill_path / SKILL_MARKER
        if not marker_path.is_file():
            raise InstallError(f"refusing unowned skill directory: {self.skill_path}")
        marker = _read_json(marker_path, "skill ownership marker")
        if marker.get("schema") != SCHEMA or marker.get("owner") != SKILL_OWNER or marker.get("skill") != SKILL_NAME:
            raise InstallError(f"refusing skill with an unknown ownership marker: {self.skill_path}")
        return marker, _valid_manifest_files(marker.get("files"), "skill ownership marker")

    def _skill_destination(self, relative: str) -> Path:
        destination = self.skill_path / relative
        current = self.skill_path
        if current.is_symlink():
            raise InstallError(f"refusing symlinked skill directory: {current}")
        for part in Path(relative).parts[:-1]:
            current = current / part
            if current.is_symlink():
                raise InstallError(f"refusing symlinked skill path: {current}")
            if current.exists() and not current.is_dir():
                raise InstallError(f"skill path ancestor is not a directory: {current}")
        return destination

    def _preflight_skill(self, action: str) -> None:
        if not self.with_skill:
            return
        if action in ("install", "upgrade"):
            _skill_files(SKILL_SOURCE)
            if self.skill_path.is_symlink():
                raise InstallError(f"refusing symlinked skill directory: {self.skill_path}")
            if self.skill_path.exists():
                if not self.skill_path.is_dir():
                    raise InstallError(f"skill destination is not a directory: {self.skill_path}")
                if any(self.skill_path.iterdir()):
                    self._read_skill_marker()
        elif action == "uninstall" and self.skill_path.exists():
            self._verify_skill_unchanged()

    def _copy_skill_file(self, source: Path, destination: Path, root: Path) -> None:
        try:
            relative = destination.relative_to(root)
        except ValueError as exc:
            raise InstallError(f"skill destination escapes its managed root: {destination}") from exc
        current = root
        if current.is_symlink():
            raise InstallError(f"refusing symlinked skill directory: {current}")
        for part in relative.parts[:-1]:
            current = current / part
            if current.is_symlink():
                raise InstallError(f"refusing symlinked skill path: {current}")
            if current.exists() and not current.is_dir():
                raise InstallError(f"skill path ancestor is not a directory: {current}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.tmp"
        try:
            with source.open("rb") as incoming, temporary.open("xb") as outgoing:
                shutil.copyfileobj(incoming, outgoing)
                outgoing.flush()
                os.fsync(outgoing.fileno())
            os.chmod(temporary, source.stat().st_mode & 0o777)
            os.replace(temporary, destination)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _apply_skill(self, action: str) -> dict | None:
        if not self.with_skill:
            return None
        source_files = _skill_files(SKILL_SOURCE)
        self.skill_root.mkdir(parents=True, exist_ok=True)
        if not self.skill_path.exists() or not any(self.skill_path.iterdir()):
            if self.skill_path.exists():
                self.skill_path.rmdir()
            stage = self.skill_root / f".{SKILL_NAME}.{uuid.uuid4().hex}.tmp"
            try:
                stage.mkdir(mode=0o700)
                hashes = {}
                for relative, source in source_files.items():
                    destination = stage / relative
                    self._copy_skill_file(source, destination, stage)
                    hashes[relative] = _digest(destination)
                _atomic_json(
                    stage / SKILL_MARKER,
                    {"schema": SCHEMA, "owner": SKILL_OWNER, "skill": SKILL_NAME, "files": hashes},
                )
                os.replace(stage, self.skill_path)
            except Exception:
                shutil.rmtree(stage, ignore_errors=True)
                raise
            return {"path": str(self.skill_path), "installed": True, "preserved_modified": []}

        _, old_hashes = self._read_skill_marker()
        new_hashes: dict[str, str] = {}
        preserved: set[str] = set()
        for relative, source in source_files.items():
            destination = self._skill_destination(relative)
            old_digest = old_hashes.get(relative)
            unchanged = (
                old_digest is not None
                and destination.is_file()
                and not destination.is_symlink()
                and _digest(destination) == old_digest
            )
            if old_digest is not None and not unchanged:
                preserved.add(relative)
                continue
            if old_digest is None and (destination.exists() or destination.is_symlink()):
                preserved.add(relative)
                continue
            self._copy_skill_file(source, destination, self.skill_path)
            new_hashes[relative] = _digest(destination)
        for relative, old_digest in old_hashes.items():
            if relative in source_files:
                continue
            destination = self._skill_destination(relative)
            if destination.is_file() and not destination.is_symlink() and _digest(destination) == old_digest:
                destination.unlink()
            elif destination.exists() or destination.is_symlink():
                preserved.add(relative)
        _atomic_json(
            self.skill_path / SKILL_MARKER,
            {"schema": SCHEMA, "owner": SKILL_OWNER, "skill": SKILL_NAME, "files": new_hashes},
        )
        return {
            "path": str(self.skill_path),
            "installed": True,
            "preserved_modified": sorted(preserved),
        }

    def _verify_skill_unchanged(self) -> None:
        _, hashes = self._read_skill_marker()
        actual = set()
        for path in self.skill_path.rglob("*"):
            if path.is_file() or path.is_symlink():
                actual.add(path.relative_to(self.skill_path).as_posix())
        expected = set(hashes) | {SKILL_MARKER}
        if actual != expected:
            raise InstallError(f"refusing to remove modified or untracked skill data: {self.skill_path}")
        for relative, digest in hashes.items():
            path = self._skill_destination(relative)
            if path.is_symlink() or not path.is_file() or _digest(path) != digest:
                raise InstallError(f"refusing to remove modified skill file: {path}")

    def _remove_skill(self) -> dict | None:
        if not self.with_skill:
            return None
        if not self.skill_path.exists():
            return {"path": str(self.skill_path), "removed": False}
        self._verify_skill_unchanged()
        shutil.rmtree(self.skill_path)
        return {"path": str(self.skill_path), "removed": True}

    def install_or_upgrade(self, action: str) -> dict:
        with self._mutation_lock():
            return self._install_or_upgrade(action)

    def _install_or_upgrade(self, action: str) -> dict:
        if sys.version_info < (3, 11):
            raise InstallError("Python 3.11 or newer is required")
        # Check the externally visible command before claiming a new prefix or
        # preparing a release.  A foreign path must never cause partial work.
        preflight_marker = self._claim_or_read() if self.marker_path.exists() else None
        self._preflight_command(action, preflight_marker)
        marker = self._claim_or_read()
        self._validate_runtime_paths(marker)
        current = self._current_release(marker)
        if action == "install" and current is not None:
            raise InstallError(f"codex-monitor is already installed; use upgrade: {self.prefix}")
        if action == "upgrade" and current is None:
            raise InstallError(f"codex-monitor is not installed; use install: {self.prefix}")
        if action == "upgrade":
            self._refuse_managed_services("upgrade")
        self._check_shim()
        self._preflight_command(action, marker)
        self._preflight_skill(action)
        with tempfile.TemporaryDirectory(prefix="codex-monitor-wheel-") as temporary:
            wheel = self.wheel_arg or self._build_wheel(Path(temporary))
            release, release_record = self._prepare_release(wheel)
        registered = False
        try:
            skill = self._apply_skill(action)
            marker["releases"].append(release_record)
            _atomic_json(self.marker_path, marker)
            registered = True
            self._ensure_shim()
            command = self._ensure_command(marker)
            self._switch_current(release_record["id"])
        finally:
            if not registered:
                shutil.rmtree(release, ignore_errors=True)
        command_status = self._command_status(marker)
        return {
            "action": "installed" if action == "install" else "upgraded",
            "prefix": str(self.prefix),
            "version": release_record["version"],
            "release": str(release),
            "executable": str(self.executable),
            "link": command,
            **command_status,
            "skill": skill,
            "state": "preserved outside the managed runtime prefix",
        }

    def link(self) -> dict:
        with self._mutation_lock():
            return self._link()

    def _link(self) -> dict:
        marker = self._read_owned()
        self._validate_runtime_paths(marker)
        if self._current_release(marker) is None:
            raise InstallError(f"codex-monitor is not installed; use install: {self.prefix}")
        self._check_shim()
        self._preflight_command("link", marker)
        command = self._ensure_command(marker)
        command_status = self._command_status(marker)
        return {
            "action": "linked",
            "prefix": str(self.prefix),
            "executable": str(self.executable),
            "link": command,
            **command_status,
            "state": "preserved outside the managed runtime prefix",
        }

    def _verify_runtime_tree(self, marker: dict) -> None:
        allowed = {MARKER, "releases", "current", "bin"}
        extras = {path.name for path in self.prefix.iterdir()} - allowed
        if extras:
            raise InstallError(f"refusing to remove untracked prefix entries: {', '.join(sorted(extras))}")
        self._current_release(marker)
        self._check_shim()
        if self.executable.parent.exists():
            bin_entries = {path.name for path in self.executable.parent.iterdir()}
            if bin_entries - {PRODUCT}:
                raise InstallError("refusing to remove untracked files from managed bin directory")
        known = {item["id"] for item in marker["releases"]}
        actual = {path.name for path in self.releases_dir.iterdir()} if self.releases_dir.is_dir() else set()
        if actual != known or any(not (self.releases_dir / name).is_dir() for name in actual):
            raise InstallError("refusing to remove runtime with untracked or missing release directories")
        for release in marker["releases"]:
            root = self.releases_dir / release["id"]
            expected = _valid_release_inventory(release["files"])
            observed = _release_inventory(root)
            if observed != expected:
                raise InstallError(f"refusing to remove changed or untracked release data: {root}")

    def uninstall(self) -> dict:
        with self._mutation_lock():
            return self._uninstall()

    def _uninstall(self) -> dict:
        marker = self._read_owned()
        self._validate_runtime_paths(marker)
        self._refuse_managed_services("uninstall")
        self._preflight_command("uninstall", marker)
        self._preflight_skill("uninstall")
        state = Path(os.environ.get("CODEX_MONITOR_HOME", Path.home() / ".local/state/codex-monitor")).expanduser()
        try:
            state.absolute().relative_to(self.prefix)
        except ValueError:
            pass
        else:
            raise InstallError(
                f"refusing to remove runtime prefix because CODEX_MONITOR_HOME is inside it: {state.absolute()}"
            )
        self._verify_runtime_tree(marker)
        skill = self._remove_skill()
        command = self._remove_command(marker)
        shutil.rmtree(self.prefix)
        return {
            "action": "uninstalled",
            "prefix": str(self.prefix),
            "link": command,
            "skill": skill,
            "preserved_state": str(state.absolute()),
        }

    def _skill_status(self) -> dict:
        if not self.skill_path.exists():
            return {"path": str(self.skill_path), "installed": False}
        try:
            _, hashes = self._read_skill_marker()
        except InstallError as exc:
            return {"path": str(self.skill_path), "installed": True, "owned": False, "error": str(exc)}
        modified = []
        for relative, digest in hashes.items():
            path = self.skill_path / relative
            if path.is_symlink() or not path.is_file() or _digest(path) != digest:
                modified.append(relative)
        actual = {
            path.relative_to(self.skill_path).as_posix()
            for path in self.skill_path.rglob("*")
            if path.is_file() or path.is_symlink()
        }
        untracked = sorted(actual - set(hashes) - {SKILL_MARKER})
        return {
            "path": str(self.skill_path),
            "installed": True,
            "owned": True,
            "modified": sorted(modified),
            "untracked": untracked,
        }

    def status(self) -> dict:
        if not self.prefix.exists():
            result = {
                "prefix": str(self.prefix),
                "installed": False,
                "executable": str(self.executable),
                **self._command_status(None),
            }
        else:
            marker = self._read_owned()
            current = self._current_release(marker)
            releases = marker["releases"]
            active = next((item for item in releases if item["id"] == current), None)
            result = {
                "prefix": str(self.prefix),
                "installed": current is not None,
                "version": active["version"] if active else None,
                "current_release": current,
                "releases": [
                    {key: value for key, value in release.items() if key != "files"}
                    for release in releases
                ],
                "executable": str(self.executable),
                "services": self._service_records(),
                **self._command_status(marker),
            }
        if self.with_skill:
            result["skill"] = self._skill_status()
        return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Install codex-monitor from this checkout or a local wheel.")
    parser.add_argument(
        "--prefix",
        default=str(DEFAULT_PREFIX),
        help="absolute user-owned runtime prefix (default: ~/.local/share/codex-monitor)",
    )
    command_options = parser.add_mutually_exclusive_group()
    command_options.add_argument(
        "--bin-dir",
        help="absolute directory for the PATH command link (default only for the standard prefix)",
    )
    command_options.add_argument(
        "--no-command",
        action="store_true",
        help="do not create a new PATH command link; use the absolute launcher path",
    )
    parser.add_argument("--wheel", help="relative or absolute local codex-monitor wheel; otherwise build this checkout")
    parser.add_argument("--with-skill", action="store_true", help="also manage the bundled $codex-monitor skill")
    parser.add_argument(
        "--skill-root",
        help="absolute skills root (default: $CODEX_HOME/skills or ~/.codex/skills; requires --with-skill)",
    )
    parser.add_argument("action", choices=("install", "upgrade", "link", "status", "uninstall"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.skill_root and not args.with_skill:
        print(json.dumps({"error": "--skill-root requires --with-skill"}), file=sys.stderr)
        return 2
    try:
        installer = Installer(
            args.prefix,
            wheel=args.wheel,
            with_skill=args.with_skill,
            skill_root=args.skill_root,
            bin_dir=args.bin_dir,
            no_command=args.no_command,
        )
        if args.action in ("install", "upgrade"):
            result = installer.install_or_upgrade(args.action)
        else:
            result = getattr(installer, args.action)()
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except InstallError as exc:
        print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
