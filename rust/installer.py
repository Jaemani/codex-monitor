#!/usr/bin/env python3
"""Install the Rust candidate in an isolated, user-owned prefix.

The installer accepts an already-built ``codex-monitor-rs`` executable. Normal
release actions do not build, migrate, start, stop, or register a service;
service lifecycle actions are explicit and delegated to ``service.py``. Each
accepted executable is copied to an immutable release directory and a stable
``current`` symlink is switched only after the copy passes ``--version`` and
``--help`` checks.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import time
import uuid


PRODUCT = "codex-monitor-rs"
OWNER = "codex-monitor-rust-installer"
SCHEMA = 1
MARKER = ".codex-monitor-rust-installer.json"
SERVICE_MARKER = ".codex-monitor-rs-service.json"
SERVICE_DIR = "service"
DEFAULT_PREFIX = Path.home() / ".local" / "share" / "codex-monitor-rust"
DEFAULT_STATE = Path.home() / ".local" / "state" / "codex-monitor-rust"
VERSION_RE = re.compile(r"(?<![0-9])([0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?)(?![0-9A-Za-z])")


class InstallError(RuntimeError):
    """A safe precondition for an install operation was not met."""


def _absolute(value: str | Path, label: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise InstallError(f"{label} must be an absolute path: {value}")
    return Path(os.path.abspath(os.fspath(path)))


def _reject_symlink(path: Path, label: str) -> None:
    if path.is_symlink():
        raise InstallError(f"refusing symlinked {label}: {path}")


def _atomic_json(path: Path, value: object) -> None:
    """Write a private JSON file and atomically replace the old value."""

    path.parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink(path, "managed file")
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_json(path: Path, label: str) -> dict:
    _reject_symlink(path, label)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise InstallError(f"cannot read {label}: {path}") from exc
    if not isinstance(value, dict):
        raise InstallError(f"invalid {label}: {path}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise InstallError(f"cannot hash file: {path}") from exc
    return digest.hexdigest()


def _run(argv: list[str], label: str, *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    # A validation command must not accidentally select the caller's state.
    environment.pop("CODEX_MONITOR_RUST_HOME", None)
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InstallError(f"{label} could not be completed") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        suffix = f": {detail[-1]}" if detail else ""
        raise InstallError(f"{label} failed with exit code {result.returncode}{suffix}")
    return result


def _version_from(binary: Path) -> str:
    result = _run([str(binary), "--version"], "binary version validation")
    match = VERSION_RE.search(f"{result.stdout}\n{result.stderr}")
    if match is None:
        raise InstallError(f"binary did not report a semantic version: {binary}")
    return match.group(1)


def _check_binary_file(binary: Path) -> None:
    if not binary.is_file() or binary.is_symlink():
        raise InstallError(f"binary must be a regular file: {binary}")
    try:
        mode = binary.stat().st_mode
    except OSError as exc:
        raise InstallError(f"cannot inspect binary: {binary}") from exc
    if not stat.S_ISREG(mode) or not mode & 0o111:
        raise InstallError(f"binary is not executable: {binary}")


def _smoke(binary: Path) -> str:
    _check_binary_file(binary)
    version = _version_from(binary)
    _run([str(binary), "--help"], "binary smoke validation")
    return version


def _safe_release_id(version: str, digest: str) -> str:
    version_part = re.sub(r"[^A-Za-z0-9.+-]", "_", version)
    return f"{version_part}-{time.strftime('%Y%m%d%H%M%S')}-{digest[:12]}-{uuid.uuid4().hex[:8]}"


def _inventory(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise InstallError(f"release contains a symlink: {path}")
        if path.is_file():
            result[relative] = _sha256(path)
        elif not path.is_dir():
            raise InstallError(f"release contains an unsupported entry: {path}")
    return result


def _valid_inventory(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        raise InstallError("invalid release file inventory")
    result: dict[str, str] = {}
    for relative, digest in value.items():
        if not isinstance(relative, str):
            raise InstallError(f"invalid release inventory entry: {relative!r}")
        path = Path(relative)
        if (
            not relative
            or path.is_absolute()
            or ".." in path.parts
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise InstallError(f"invalid release inventory entry: {relative!r}")
        result[relative] = digest
    return result


def _remove_tree(root: Path) -> None:
    """Remove a previously verified tree without a blanket recursive delete."""

    if root.is_symlink() or not root.is_dir():
        raise InstallError(f"managed release is not a regular directory: {root}")
    for path in sorted(root.iterdir(), key=lambda item: len(item.parts), reverse=True):
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            _remove_tree(path)
        else:
            raise InstallError(f"refusing to remove unsupported managed entry: {path}")
    root.rmdir()


class Installer:
    """State-free operations over one explicitly owned Rust prefix."""

    def __init__(
        self,
        prefix: str | Path = DEFAULT_PREFIX,
        *,
        state: str | Path = DEFAULT_STATE,
        binary: str | Path | None = None,
        expected_sha256: str | None = None,
    ):
        self.prefix = _absolute(prefix, "prefix")
        if self.prefix in (Path("/"), Path.home()) or self.prefix.is_symlink():
            raise InstallError(f"refusing unsafe or symlinked prefix: {self.prefix}")
        self.state = _absolute(state, "state")
        self.prefix_real = self.prefix.resolve(strict=False)
        self.state_real = self.state.resolve(strict=False)
        try:
            self.state_real.relative_to(self.prefix_real)
        except ValueError:
            pass
        else:
            raise InstallError("Rust state must be outside the Rust installation prefix")
        self.binary = _absolute(binary, "binary") if binary is not None else None
        if expected_sha256 is not None and not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256):
            raise InstallError("expected SHA256 must be 64 hexadecimal characters")
        self.expected_sha256 = expected_sha256.lower() if expected_sha256 else None

    @property
    def marker_path(self) -> Path:
        return self.prefix / MARKER

    @property
    def releases(self) -> Path:
        return self.prefix / "releases"

    @property
    def current(self) -> Path:
        return self.prefix / "current"

    @property
    def bin_dir(self) -> Path:
        return self.prefix / "bin"

    @property
    def launcher(self) -> Path:
        return self.bin_dir / PRODUCT

    @property
    def lock_path(self) -> Path:
        digest = hashlib.sha256(os.fsencode(str(self.prefix))).hexdigest()[:20]
        return self.prefix.parent / f".{PRODUCT}-{digest}.lock"

    @contextmanager
    def _lock(self):
        if os.name == "nt":
            raise InstallError("Rust installation switching is only verified on POSIX hosts")
        self.prefix.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.lock_path, flags, 0o600)
        except OSError as exc:
            raise InstallError(f"cannot safely open installer lock: {self.lock_path}") from exc
        try:
            details = os.fstat(descriptor)
            if details.st_uid != os.getuid() or stat.S_IMODE(details.st_mode) & 0o077:
                raise InstallError(f"refusing foreign installer lock: {self.lock_path}")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise InstallError(f"another installer is changing this prefix: {self.prefix}") from exc
            yield
        finally:
            os.close(descriptor)

    def _validate_marker(self, marker: dict) -> dict:
        if (
            marker.get("schema") != SCHEMA
            or marker.get("owner") != OWNER
            or marker.get("product") != PRODUCT
        ):
            raise InstallError(f"refusing prefix with an unknown ownership marker: {self.prefix}")
        recorded_state = marker.get("state")
        recorded_state_real = marker.get("state_real")
        if (
            not isinstance(recorded_state, str)
            or recorded_state != str(self.state)
            or not isinstance(recorded_state_real, str)
            or recorded_state_real != str(self.state_real)
        ):
            raise InstallError("configured state differs from the state recorded for this prefix")
        releases = marker.get("releases")
        if not isinstance(releases, list):
            raise InstallError(f"invalid installer ownership marker: {self.marker_path}")
        for release in releases:
            if not isinstance(release, dict):
                raise InstallError("invalid release record")
            if not isinstance(release.get("id"), str) or not re.fullmatch(r"[A-Za-z0-9.+_-]+", release["id"]):
                raise InstallError("invalid release id")
            if not isinstance(release.get("version"), str) or VERSION_RE.fullmatch(release["version"]) is None:
                raise InstallError("invalid release version")
            if not isinstance(release.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", release["sha256"]):
                raise InstallError("invalid release SHA256")
            _valid_inventory(release.get("files"))
        return marker

    def _claim_or_read(self) -> dict:
        if self.prefix.exists() and not self.prefix.is_dir():
            raise InstallError(f"prefix is not a directory: {self.prefix}")
        if self.prefix.is_symlink():
            raise InstallError(f"refusing symlinked prefix: {self.prefix}")
        if not self.prefix.exists():
            self.prefix.mkdir(parents=True, mode=0o700)
        if not self.marker_path.exists():
            if any(self.prefix.iterdir()):
                raise InstallError(f"refusing to claim non-empty unowned prefix: {self.prefix}")
            marker = {
                "schema": SCHEMA,
                "owner": OWNER,
                "product": PRODUCT,
                "state": str(self.state),
                "state_real": str(self.state_real),
                "releases": [],
            }
            _atomic_json(self.marker_path, marker)
            return marker
        marker = _read_json(self.marker_path, "installer ownership marker")
        return self._validate_marker(marker)

    def _read_owned(self) -> dict:
        if not self.marker_path.is_file():
            raise InstallError(f"Rust runtime is not installed at owned prefix: {self.prefix}")
        return self._validate_marker(_read_json(self.marker_path, "installer ownership marker"))

    def _release_record(self, marker: dict, release_id: str) -> dict:
        records = [item for item in marker["releases"] if item["id"] == release_id]
        if len(records) != 1:
            raise InstallError(f"unknown owned release: {release_id}")
        return records[0]

    def _current_id(self, marker: dict) -> str | None:
        if not self.current.exists() and not self.current.is_symlink():
            return None
        if self.current.exists() and not self.current.is_symlink():
            raise InstallError(f"managed current pointer is not a symlink: {self.current}")
        if not self.current.is_symlink():
            raise InstallError(f"managed current pointer is not a symlink: {self.current}")
        target = Path(os.readlink(self.current))
        if target.is_absolute() or len(target.parts) != 2 or target.parts[0] != "releases":
            raise InstallError(f"managed current pointer has an unsafe target: {self.current}")
        release_id = target.parts[1]
        self._release_record(marker, release_id)
        release = self.releases / release_id
        if release.is_symlink() or not release.is_dir():
            raise InstallError(f"managed current release is missing or symlinked: {release}")
        return release_id

    def _check_layout(self, marker: dict) -> None:
        for path in (self.releases, self.bin_dir, self.prefix / SERVICE_DIR):
            if path.exists() and (path.is_symlink() or not path.is_dir()):
                raise InstallError(f"managed path is not a regular directory: {path}")
        known = {item["id"] for item in marker["releases"]}
        if self.releases.is_dir():
            actual = {path.name for path in self.releases.iterdir()}
            if actual != known:
                missing = known - actual
                extra = actual - known
                detail = []
                if missing:
                    detail.append(f"missing: {', '.join(sorted(missing))}")
                if extra:
                    detail.append(f"untracked: {', '.join(sorted(extra))}")
                raise InstallError(f"release layout differs from ownership marker ({'; '.join(detail)})")
            for release_id in actual:
                release = self.releases / release_id
                if release.is_symlink() or not release.is_dir():
                    raise InstallError(f"managed release is not a regular directory: {release}")
        self._current_id(marker)

    def _refuse_service(self, action: str) -> None:
        marker = self.prefix / SERVICE_MARKER
        service_dir = self.prefix / SERVICE_DIR
        if marker.exists() or marker.is_symlink():
            raise InstallError(
                f"refusing to {action} while a Rust service is installed; "
                f"run service-stop then service-remove for {self.prefix}"
            )
        if service_dir.exists() or service_dir.is_symlink():
            if service_dir.is_symlink() or not service_dir.is_dir() or any(service_dir.iterdir()):
                raise InstallError(f"refusing to {action} with foreign or untracked service data: {service_dir}")
            raise InstallError(f"refusing to {action} with an unowned service directory: {service_dir}")

    def _check_launcher(self) -> None:
        if self.bin_dir.is_symlink():
            raise InstallError(f"refusing symlinked managed bin directory: {self.bin_dir}")
        if self.bin_dir.exists() and not self.bin_dir.is_dir():
            raise InstallError(f"managed bin path is not a directory: {self.bin_dir}")
        if not self.launcher.exists() and not self.launcher.is_symlink():
            return
        expected = Path("..") / "current" / "bin" / PRODUCT
        if not self.launcher.is_symlink() or Path(os.readlink(self.launcher)) != expected:
            raise InstallError(f"refusing foreign or modified launcher: {self.launcher}")

    def _ensure_launcher(self) -> None:
        self._check_launcher()
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        if not self.launcher.exists() and not self.launcher.is_symlink():
            self.launcher.symlink_to(Path("..") / "current" / "bin" / PRODUCT)
            _fsync_directory(self.bin_dir)

    def _verify_release(self, record: dict, *, smoke: bool = True) -> Path:
        release = self.releases / record["id"]
        if release.is_symlink() or not release.is_dir():
            raise InstallError(f"owned release is missing or symlinked: {release}")
        expected = _valid_inventory(record["files"])
        observed = _inventory(release)
        if observed != expected:
            raise InstallError(f"release was modified or has untracked files: {release}")
        provenance = _read_json(release / "provenance.json", "release provenance")
        if (
            provenance.get("schema") != SCHEMA
            or provenance.get("owner") != OWNER
            or provenance.get("product") != PRODUCT
            or provenance.get("version") != record["version"]
            or provenance.get("installed_sha256") != record["sha256"]
            or provenance.get("source_sha256") != record.get("source_sha256")
        ):
            raise InstallError(f"release provenance does not match its ownership record: {release}")
        binary = release / "bin" / PRODUCT
        if not binary.is_file() or binary.is_symlink():
            raise InstallError(f"release binary is missing or symlinked: {binary}")
        if _sha256(binary) != record["sha256"]:
            raise InstallError(f"release binary SHA256 does not match provenance: {binary}")
        if smoke and _smoke(binary) != record["version"]:
            raise InstallError(f"release version does not match provenance: {binary}")
        return release

    def _switch_current(self, release_id: str) -> None:
        temporary = self.prefix / f".current-{uuid.uuid4().hex}.tmp"
        try:
            temporary.symlink_to(Path("releases") / release_id)
            os.replace(temporary, self.current)
            _fsync_directory(self.prefix)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _prepare_release(self, source: Path) -> tuple[Path, dict]:
        _check_binary_file(source)
        source_digest = _sha256(source)
        if self.expected_sha256 and source_digest != self.expected_sha256:
            raise InstallError("binary SHA256 does not match --sha256")
        self.releases.mkdir(parents=True, exist_ok=True)
        stage = self.releases / f".stage-{uuid.uuid4().hex}"
        release: Path | None = None
        published = False
        try:
            (stage / "bin").mkdir(parents=True, mode=0o700)
            destination = stage / "bin" / PRODUCT
            with source.open("rb") as incoming, destination.open("xb") as outgoing:
                shutil.copyfileobj(incoming, outgoing)
                outgoing.flush()
                os.fsync(outgoing.fileno())
            os.chmod(destination, source.stat().st_mode & 0o777)
            copied_digest = _sha256(destination)
            if copied_digest != source_digest:
                raise InstallError("source binary changed while it was being copied")
            version = _smoke(destination)
            release_id = _safe_release_id(version, source_digest)
            release = self.releases / release_id
            provenance = {
                "schema": SCHEMA,
                "owner": OWNER,
                "product": PRODUCT,
                "version": version,
                "source": str(source),
                "source_sha256": source_digest,
                "installed_sha256": copied_digest,
                "validated_at": int(time.time()),
            }
            _atomic_json(stage / "provenance.json", provenance)
            if release.exists() or release.is_symlink():
                raise InstallError(f"release id already exists: {release_id}")
            os.replace(stage, release)
            published = True
            _fsync_directory(self.releases)
            record = {
                "id": release_id,
                "version": version,
                "sha256": copied_digest,
                "source_sha256": source_digest,
                "validated_at": provenance["validated_at"],
                "files": _inventory(release),
            }
            return release, record
        except Exception:
            if stage.exists():
                _remove_tree(stage)
            if published and release is not None and release.exists():
                _remove_tree(release)
            raise

    def _source_for_action(self) -> Path:
        if self.binary is None:
            raise InstallError("install and upgrade require --binary PATH")
        return self.binary

    def install_or_upgrade(self, action: str) -> dict:
        with self._lock():
            marker = self._claim_or_read()
            self._check_layout(marker)
            self._refuse_service(action)
            current = self._current_id(marker)
            if action == "install" and current is not None:
                raise InstallError("Rust runtime is already installed; use upgrade")
            if action == "install" and marker["releases"]:
                raise InstallError(
                    "prefix contains owned releases without a current pointer; refusing implicit adoption"
                )
            if action == "upgrade" and current is None:
                raise InstallError("Rust runtime is not installed; use install")
            if current is not None:
                self._verify_release(self._release_record(marker, current))
            self._check_launcher()
            release, record = self._prepare_release(self._source_for_action())
            marker["releases"].append(record)
            registered = False
            try:
                _atomic_json(self.marker_path, marker)
                registered = True
                self._ensure_launcher()
                self._switch_current(record["id"])
            except Exception:
                # Keep no unregistered release behind if the marker was not committed.
                if not registered:
                    _remove_tree(release)
                raise
            return {
                "action": "installed" if action == "install" else "upgraded",
                "prefix": str(self.prefix),
                "state": str(self.state),
                "version": record["version"],
                "release": record["id"],
                "sha256": record["sha256"],
                "executable": str(self.launcher),
            }

    def rollback(self, release_id: str) -> dict:
        if not release_id:
            raise InstallError("rollback requires an explicit --release ID")
        with self._lock():
            marker = self._read_owned()
            self._check_layout(marker)
            current = self._current_id(marker)
            if current is None:
                raise InstallError("Rust runtime is not installed")
            if release_id == current:
                raise InstallError("rollback target is already current")
            target = self._release_record(marker, release_id)
            self._verify_release(target)
            current_record = self._release_record(marker, current)
            self._verify_release(current_record)
            if self.state.exists() and (
                not self.state.is_dir() or any(self.state.iterdir())
            ) and target["sha256"] != current_record["sha256"]:
                raise InstallError(
                    "refusing rollback across different binary hashes while Rust state is populated; "
                    "state compatibility is not established"
                )
            self._check_launcher()
            self._switch_current(release_id)
            return {
                "action": "rolled_back",
                "prefix": str(self.prefix),
                "state": str(self.state),
                "version": target["version"],
                "release": release_id,
                "sha256": target["sha256"],
                "executable": str(self.launcher),
            }

    def _verify_uninstall_tree(self, marker: dict) -> str | None:
        self._refuse_service("uninstall")
        current = self._current_id(marker)
        if marker["releases"] and current is None:
            raise InstallError("refusing to uninstall an owned prefix without a current pointer")
        allowed = {MARKER, "releases", "current", "bin"}
        actual = {path.name for path in self.prefix.iterdir()}
        extras = actual - allowed
        if extras:
            raise InstallError(f"refusing to remove untracked prefix entries: {', '.join(sorted(extras))}")
        if self.releases.is_dir():
            known = {item["id"] for item in marker["releases"]}
            actual_releases = {path.name for path in self.releases.iterdir()}
            if actual_releases != known:
                raise InstallError("refusing to remove untracked or missing release directories")
            for record in marker["releases"]:
                self._verify_release(record, smoke=False)
        elif marker["releases"]:
            raise InstallError("refusing to remove missing release directory")
        self._check_launcher()
        if self.bin_dir.is_dir() and {path.name for path in self.bin_dir.iterdir()} - {PRODUCT}:
            raise InstallError("refusing to remove untracked files from managed bin directory")
        return current

    def uninstall(self) -> dict:
        with self._lock():
            marker = self._read_owned()
            self._verify_uninstall_tree(marker)
            # State is deliberately outside the prefix and remains untouched.
            if self.state_real == self.prefix_real or self.state_real.is_relative_to(self.prefix_real):
                raise InstallError("Rust state must be outside the Rust installation prefix")
            if self.current.is_symlink():
                self.current.unlink()
            if self.launcher.is_symlink():
                self.launcher.unlink()
            if self.bin_dir.is_dir():
                self.bin_dir.rmdir()
            if self.releases.is_dir():
                for record in marker["releases"]:
                    _remove_tree(self.releases / record["id"])
                self.releases.rmdir()
            self.marker_path.unlink()
            self.prefix.rmdir()
            _fsync_directory(self.prefix.parent)
            return {
                "action": "uninstalled",
                "prefix": str(self.prefix),
                "state": str(self.state),
                "preserved_state": True,
            }

    def status(self) -> dict:
        if not self.prefix.exists():
            return {"installed": False, "prefix": str(self.prefix), "state": str(self.state)}
        marker = self._read_owned()
        self._check_layout(marker)
        current = self._current_id(marker)
        active = self._release_record(marker, current) if current else None
        if active is not None:
            self._verify_release(active)
        return {
            "installed": current is not None,
            "prefix": str(self.prefix),
            "state": str(self.state),
            "current_release": current,
            "version": active["version"] if active else None,
            "sha256": active["sha256"] if active else None,
            "releases": [
                {key: item[key] for key in ("id", "version", "sha256", "source_sha256", "validated_at")}
                for item in marker["releases"]
            ],
            "executable": str(self.launcher),
        }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Install the isolated Rust codex-monitor executable.")
    parser.add_argument("--prefix", default=str(DEFAULT_PREFIX), help="absolute user-owned Rust prefix")
    parser.add_argument("--state", default=str(DEFAULT_STATE), help="absolute Rust state directory; never modified")
    parser.add_argument("--binary", help="absolute prebuilt codex-monitor-rs executable")
    parser.add_argument("--sha256", help="expected SHA256 for --binary")
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser("install", help="install the first validated release")
    subparsers.add_parser("upgrade", help="install a new validated release and switch current")
    rollback = subparsers.add_parser("rollback", help="switch to an explicitly selected validated release")
    rollback.add_argument("--release", required=True, help="owned release ID from status")
    subparsers.add_parser("status", help="inspect the owned prefix")
    subparsers.add_parser("uninstall", help="remove only the verified owned prefix")
    subparsers.add_parser("service-install", help="install and start an explicit user service")
    subparsers.add_parser("service-status", help="inspect the explicit user service")
    subparsers.add_parser("service-start", help="start the explicit user service")
    subparsers.add_parser("service-restart", help="restart the explicit user service")
    subparsers.add_parser("service-stop", help="stop the explicit user service")
    subparsers.add_parser("service-remove", help="stop and remove the explicit user service")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    service_error_type = InstallError
    service_manager_factory = None
    if args.action.startswith("service-"):
        from service import ServiceError, service_manager

        service_error_type = ServiceError
        service_manager_factory = service_manager
    try:
        installer = Installer(
            args.prefix,
            state=args.state,
            binary=args.binary,
            expected_sha256=args.sha256,
        )
        if args.action in ("install", "upgrade"):
            result = installer.install_or_upgrade(args.action)
        elif args.action == "rollback":
            result = installer.rollback(args.release)
        elif args.action.startswith("service-"):
            manager = service_manager_factory(installer)
            result = getattr(manager, args.action.replace("service-", ""))()
        else:
            result = getattr(installer, args.action)()
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (InstallError, service_error_type) as exc:
        print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
