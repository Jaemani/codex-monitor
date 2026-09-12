#!/usr/bin/env python3
"""Explicit user-service integration for the isolated Rust runtime.

Service definitions live below the owned Rust prefix. The macOS launchd
manager is invoked only after the user asks for a service action; importing
this module does not inspect or mutate the host supervisor. ``runner`` is
injectable so the contract can be tested without touching launchd.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import time
from typing import Callable

from installer import (
    InstallError,
    Installer,
    OWNER as INSTALLER_OWNER,
    PRODUCT,
    SCHEMA,
    _atomic_json,
    _fsync_directory,
    _read_json,
    _sha256,
)


SERVICE_MARKER = ".codex-monitor-rs-service.json"
SERVICE_DIR = "service"
SERVICE_OWNER = "codex-monitor-rust-service"
LABEL_RE = re.compile(r"^codex-monitor-rs-[0-9a-f]{16}$")


class ServiceError(RuntimeError):
    """A safe service precondition failed."""


Runner = Callable[..., subprocess.CompletedProcess[str]]


def _runner(argv: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        **kwargs,
    )


def _path_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return False
    return True


class ServiceManager:
    """Manage one service definition owned by one Rust installer prefix."""

    def __init__(
        self,
        installer: Installer,
        *,
        platform: str | None = None,
        uid: int | None = None,
        runner: Runner | None = None,
    ):
        self.installer = installer
        self.platform = platform or sys.platform
        self.uid = os.getuid() if uid is None and hasattr(os, "getuid") else uid
        self.runner = runner or _runner
        if self.uid is None:
            raise ServiceError("user service management requires a POSIX user id")
        if self.platform != "darwin":
            raise ServiceError("Rust user service integration is currently supported only on macOS launchd")
        self.label = self._label()

    @property
    def marker_path(self) -> Path:
        return self.installer.prefix / SERVICE_MARKER

    @property
    def service_dir(self) -> Path:
        return self.installer.prefix / SERVICE_DIR

    @property
    def service_path(self) -> Path:
        return self.service_dir / f"{self.label}.plist"

    @property
    def service_target(self) -> str:
        return f"gui/{self.uid}/{self.label}"

    def _label(self) -> str:
        digest = hashlib.sha256(os.fsencode(str(self.installer.prefix_real))).hexdigest()[:16]
        return f"codex-monitor-rs-{digest}"

    def _platform_name(self) -> str:
        return "launchd"

    def _check_installed(self) -> dict:
        try:
            status = self.installer.status()
        except InstallError as exc:
            raise ServiceError(str(exc)) from exc
        if not status["installed"]:
            raise ServiceError("install the Rust executable before installing a service")
        return status

    def _check_service_root(self, *, allow_empty: bool = True) -> None:
        if self.service_dir.is_symlink():
            raise ServiceError(f"refusing symlinked service directory: {self.service_dir}")
        if self.service_dir.exists() and not self.service_dir.is_dir():
            raise ServiceError(f"service path is not a directory: {self.service_dir}")
        if not _path_inside(self.service_dir, self.installer.prefix):
            raise ServiceError("service definition escaped the Rust prefix")
        if not allow_empty and self.service_dir.exists() and any(self.service_dir.iterdir()):
            raise ServiceError(f"refusing untracked service data: {self.service_dir}")

    def _read_owned(self, *, require_definition: bool = True) -> dict:
        if not self.marker_path.is_file() or self.marker_path.is_symlink():
            raise ServiceError(f"Rust service is not installed at {self.installer.prefix}")
        marker = _read_json(self.marker_path, "Rust service ownership marker")
        if (
            marker.get("schema") != SCHEMA
            or marker.get("owner") != SERVICE_OWNER
            or marker.get("product") != PRODUCT
            or marker.get("installer_owner") != INSTALLER_OWNER
            or marker.get("prefix") != str(self.installer.prefix)
            or marker.get("state") != str(self.installer.state)
            or marker.get("state_real") != str(self.installer.state_real)
            or marker.get("platform") != self._platform_name()
            or marker.get("label") != self.label
            or not isinstance(marker.get("service_path"), str)
            or marker.get("service_path") != str(self.service_path)
            or marker.get("command") != self._command()
        ):
            raise ServiceError(f"refusing foreign or modified service marker: {self.marker_path}")
        recorded_digest = marker.get("service_sha256")
        if not isinstance(recorded_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", recorded_digest):
            raise ServiceError(f"invalid service ownership marker: {self.marker_path}")
        if require_definition:
            self._check_service_definition(marker)
        return marker

    def _check_service_definition(self, marker: dict) -> None:
        if self.service_path.is_symlink() or not self.service_path.is_file():
            raise ServiceError(f"managed service definition is missing or symlinked: {self.service_path}")
        if _sha256(self.service_path) != marker["service_sha256"]:
            raise ServiceError(f"managed service definition was modified: {self.service_path}")

    def _foreign_service_guard(self) -> None:
        if self.marker_path.exists() or self.marker_path.is_symlink():
            raise ServiceError(f"refusing existing service ownership marker: {self.marker_path}")
        self._check_service_root()
        if self.service_dir.exists() and any(self.service_dir.iterdir()):
            raise ServiceError(f"refusing untracked service data: {self.service_dir}")

    def _command(self) -> list[str]:
        status = self.installer.status()
        release = status.get("current_release")
        if not isinstance(release, str):
            raise ServiceError("install a validated Rust release before creating a service")
        binary = self.installer.releases / release / "bin" / PRODUCT
        return [str(binary), "--state", str(self.installer.state), "serve"]

    def _plist(self) -> dict:
        return {
            "Label": self.label,
            "ProgramArguments": self._command(),
            "RunAtLoad": True,
            "KeepAlive": True,
            "ProcessType": "Background",
            "StandardOutPath": "/dev/null",
            "StandardErrorPath": "/dev/null",
        }

    def _write_definition(self) -> dict:
        self._check_service_root()
        self.service_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        temporary = self.service_path.parent / f".{self.service_path.name}.tmp"
        try:
            payload = plistlib.dumps(self._plist(), fmt=plistlib.FMT_XML, sort_keys=True)
            with temporary.open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            if self.service_path.exists() or self.service_path.is_symlink():
                raise ServiceError(f"refusing to replace service definition: {self.service_path}")
            os.replace(temporary, self.service_path)
            _fsync_directory(self.service_dir)
            return {
                "schema": SCHEMA,
                "owner": SERVICE_OWNER,
                "product": PRODUCT,
                "installer_owner": INSTALLER_OWNER,
                "prefix": str(self.installer.prefix),
                "state": str(self.installer.state),
                "state_real": str(self.installer.state_real),
                "platform": self._platform_name(),
                "label": self.label,
                "service_path": str(self.service_path),
                "service_sha256": _sha256(self.service_path),
                "command": self._command(),
            }
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _invoke(self, argv: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            result = self.runner(argv, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ServiceError(f"cannot execute service manager: {' '.join(argv)}") from exc
        if check and result.returncode != 0:
            detail = (result.stderr or result.stdout or "service manager failed").strip().splitlines()
            suffix = f": {detail[-1]}" if detail else ""
            raise ServiceError(f"service manager command failed ({' '.join(argv)}){suffix}")
        return result

    def _loaded(self) -> bool:
        result = self._invoke(["launchctl", "print", self.service_target], check=False)
        return result.returncode == 0

    def _start(self) -> None:
        if self._loaded():
            return
        self._invoke(["launchctl", "bootstrap", f"gui/{self.uid}", str(self.service_path)])

    def _wait_unloaded(self, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while self._loaded() and time.monotonic() < deadline:
            time.sleep(0.1)
        if self._loaded():
            raise ServiceError(f"launchd did not finish stopping {self.label}")

    def _stop(self) -> None:
        if not self._loaded():
            return
        self._invoke(["launchctl", "bootout", self.service_target])
        self._wait_unloaded()

    def install(self) -> dict:
        with self.installer._lock():
            self._check_installed()
            self._foreign_service_guard()
            marker = self._write_definition()
            try:
                _atomic_json(self.marker_path, marker)
                self._start()
            except Exception:
                # Keep the definition and ownership marker for a safe retry if
                # the supervisor is temporarily unavailable; no foreign file
                # is removed.
                raise
            return self.status()

    def start(self) -> dict:
        with self.installer._lock():
            self._check_installed()
            self._read_owned()
            self._start()
            return self.status()

    def restart(self) -> dict:
        with self.installer._lock():
            self._check_installed()
            self._read_owned()
            if self._loaded():
                self._invoke(["launchctl", "kickstart", "-k", self.service_target])
            else:
                self._start()
            return self.status()

    def status(self) -> dict:
        self._check_installed()
        marker = self._read_owned()
        return {
            "installed": True,
            "loaded": self._loaded(),
            "manager": marker["platform"],
            "label": marker["label"],
            "service": marker["service_path"],
            "command": marker["command"],
            "prefix": str(self.installer.prefix),
            "state": str(self.installer.state),
        }

    def stop(self) -> dict:
        with self.installer._lock():
            self._check_installed()
            self._read_owned()
            self._stop()
            return self.status()

    def remove(self) -> dict:
        with self.installer._lock():
            self._check_installed()
            marker = self._read_owned()
            self._stop()
            self._check_service_definition(marker)
            self.service_path.unlink()
            self.marker_path.unlink()
            if self.service_dir.is_dir():
                self.service_dir.rmdir()
            _fsync_directory(self.installer.prefix)
            return {
                "installed": False,
                "manager": marker["platform"],
                "label": marker["label"],
                "prefix": str(self.installer.prefix),
                "state": str(self.installer.state),
            }


def service_manager(installer: Installer, **kwargs) -> ServiceManager:
    try:
        return ServiceManager(installer, **kwargs)
    except InstallError as exc:
        raise ServiceError(str(exc)) from exc


__all__ = ["SERVICE_MARKER", "ServiceError", "ServiceManager", "service_manager"]
