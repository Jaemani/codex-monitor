"""macOS launchd integration for the durable monitor receiver.

The service deliberately owns one derived LaunchAgent plist per monitor state
directory.  It never edits monitor configuration or removes a plist whose
label does not belong to that state directory.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Mapping


PACKAGE_NAME = "codex-monitor"
PACKAGE_MODULE = "codex_monitor"
SERVICE_PREFIX = "com.codex.monitor"


class ServiceError(RuntimeError):
    """A service operation could not be completed safely."""


def service_label(state_dir: Path | str) -> str:
    """Return the stable, user-scoped launchd label for *state_dir*."""

    resolved = Path(state_dir).expanduser().resolve()
    digest = hashlib.sha256(os.fsencode(str(resolved))).hexdigest()[:16]
    return f"{SERVICE_PREFIX}.{digest}"


def _absolute_path(value: Path | str, name: str, *, resolve_symlinks: bool = True) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ServiceError(f"{name} must be an absolute path")
    # Keep executable symlinks such as .venv/bin/python lexical.  Resolving
    # them can silently switch launchd to the base interpreter, whose package
    # set may differ from the installed monitor.
    return path.resolve() if resolve_symlinks else Path(os.path.abspath(os.fspath(path)))


def _under(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _configured_codex_home(state_dir: Path, config: Mapping[str, Any]) -> Path:
    """Resolve the Codex storage home used by the owning CLI/Desktop client.

    CODEX_HOME is authoritative when explicitly set.  A separately configured
    sqlite_home is intentionally ignored: it is not the Codex home and must
    not silently switch the client's storage root.
    """

    candidates: list[Any] = [os.environ.get("CODEX_HOME"), config.get("codex_home")]
    for candidate in candidates:
        if candidate is not None and str(candidate).strip():
            return _absolute_path(str(candidate), "CODEX_HOME")
    return _absolute_path(Path.home() / ".codex", "CODEX_HOME")


def _read_state_config(state_dir: Path) -> dict[str, Any]:
    path = state_dir / "config.json"
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ServiceError(f"cannot read monitor configuration: {path}") from exc
    if not isinstance(value, dict):
        raise ServiceError(f"monitor configuration must be an object: {path}")
    return value


def _validate_installed_package() -> None:
    """Reject source-tree and editable installs for a durable launchd job."""

    try:
        distribution = importlib.metadata.distribution(PACKAGE_NAME)
    except importlib.metadata.PackageNotFoundError as exc:
        raise ServiceError(
            "codex-monitor must be installed as a wheel before installing a launchd service"
        ) from exc

    direct_url = distribution.read_text("direct_url.json")
    if direct_url:
        try:
            metadata = json.loads(direct_url)
        except ValueError as exc:
            raise ServiceError("installed codex-monitor metadata is invalid") from exc
        if metadata.get("dir_info", {}).get("editable"):
            raise ServiceError(
                "editable/source installs cannot back a durable launchd service; install a wheel first"
            )

    spec = importlib.util.find_spec(PACKAGE_MODULE)
    origin = Path(spec.origin).resolve() if spec and spec.origin else None
    if origin is None or not origin.is_file():
        raise ServiceError("cannot locate the installed codex-monitor package")

    distribution_root = Path(distribution.locate_file(".")).resolve()
    if not _under(origin, distribution_root):
        raise ServiceError(
            "the active codex-monitor package is outside its installed distribution; "
            "install a wheel and run its Python executable"
        )

    # A checkout can shadow a valid wheel when the command is run from the
    # checkout.  Refuse that ambiguous import rather than persisting it.
    source_root = Path(__file__).resolve().parents[1]
    source_checkout = (
        (source_root / "pyproject.toml").is_file()
        and (source_root / PACKAGE_MODULE).is_dir()
    )
    if source_checkout and _under(origin, source_root):
        raise ServiceError(
            "the active codex-monitor package is from a source checkout; install a wheel first"
        )


@dataclass(frozen=True)
class ServiceConfig:
    """Resolved launchd configuration and paths for one monitor state root."""

    state_dir: Path
    codex_home: Path
    python_executable: Path
    codex_executable: Path | None
    launch_agents_dir: Path
    label: str

    @classmethod
    def from_state(
        cls,
        state_dir: Path | str,
        *,
        codex_home: Path | str | None = None,
        python_executable: Path | str | None = None,
        codex_executable: Path | str | None = None,
        launch_agents_dir: Path | str | None = None,
        validate_install: bool = False,
        config: Mapping[str, Any] | None = None,
    ) -> "ServiceConfig":
        state = _absolute_path(state_dir, "state directory")
        values = dict(config) if config is not None else _read_state_config(state)
        if validate_install:
            _validate_installed_package()

        python = _absolute_path(
            python_executable or sys.executable,
            "Python executable",
            resolve_symlinks=False,
        )
        if not python.is_file() or not os.access(python, os.X_OK):
            raise ServiceError(f"Python executable is not runnable: {python}")

        if codex_home is None:
            home = _configured_codex_home(state, values)
        else:
            home = _absolute_path(codex_home, "CODEX_HOME")

        if codex_executable is None:
            executable = shutil.which("codex")
            codex = (
                _absolute_path(executable, "codex executable", resolve_symlinks=False)
                if executable
                else None
            )
            if codex is not None and (not codex.is_file() or not os.access(codex, os.X_OK)):
                raise ServiceError(f"codex executable is not runnable: {codex}")
        else:
            codex = _absolute_path(codex_executable, "codex executable", resolve_symlinks=False)
            if not codex.is_file() or not os.access(codex, os.X_OK):
                raise ServiceError(f"codex executable is not runnable: {codex}")

        agents = _absolute_path(
            launch_agents_dir or (Path.home() / "Library" / "LaunchAgents"),
            "LaunchAgents directory",
        )
        return cls(state, home, python, codex, agents, service_label(state))

    @property
    def plist_path(self) -> Path:
        return self.launch_agents_dir / f"{self.label}.plist"

    @property
    def service_dir(self) -> Path:
        return self.state_dir / "service"

    @property
    def stdout_path(self) -> Path:
        return self.service_dir / "stdout.log"

    @property
    def stderr_path(self) -> Path:
        return self.service_dir / "stderr.log"

    @property
    def domain(self) -> str:
        try:
            uid = os.getuid()
        except AttributeError as exc:  # pragma: no cover - only non-POSIX hosts
            raise ServiceError("macOS launchd requires a user account") from exc
        return f"gui/{uid}"

    @property
    def service_target(self) -> str:
        return f"{self.domain}/{self.label}"

    def launchd_path(self) -> str:
        """Build the PATH launchd should provide to the stdio Codex client."""

        if self.codex_executable is None:
            raise ServiceError("cannot generate a service without a codex executable")
        entries = [str(self.codex_executable.parent)]
        for entry in os.environ.get("PATH", "").split(os.pathsep):
            if not entry:
                continue
            path = Path(entry).expanduser()
            if path.is_absolute() and str(path) not in entries:
                entries.append(str(path))
        return os.pathsep.join(entries)

    def plist(self) -> dict[str, Any]:
        """Return the launchd plist payload without writing it to disk."""

        if self.codex_executable is None:
            raise ServiceError("cannot generate a service without a codex executable")
        environment = {
            "CODEX_HOME": str(self.codex_home),
            "CODEX_MONITOR_HOME": str(self.state_dir),
            "PATH": self.launchd_path(),
            "PYTHONUNBUFFERED": "1",
        }
        # This is a documented, non-secret Codex storage override.  Preserve
        # it only when the caller explicitly supplied it; never copy the
        # process environment wholesale into a durable plist.
        sqlite_home = os.environ.get("CODEX_SQLITE_HOME")
        if sqlite_home and sqlite_home.strip():
            environment["CODEX_SQLITE_HOME"] = str(
                _absolute_path(sqlite_home, "CODEX_SQLITE_HOME")
            )
        token_file = os.environ.get("CODEX_MONITOR_SERVER_TOKEN_FILE")
        if token_file and token_file.strip():
            # Keep only the private file's absolute path.  The transport
            # helper validates ownership, mode and token contents at runtime.
            environment["CODEX_MONITOR_SERVER_TOKEN_FILE"] = str(
                _absolute_path(token_file, "CODEX_MONITOR_SERVER_TOKEN_FILE")
            )

        return {
            "Label": self.label,
            "ProgramArguments": [
                str(self.python_executable),
                "-m",
                PACKAGE_MODULE,
                "--state",
                str(self.state_dir),
                "serve",
            ],
            "EnvironmentVariables": environment,
            "WorkingDirectory": str(self.state_dir),
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 5,
            "ProcessType": "Background",
            "StandardOutPath": str(self.stdout_path),
            "StandardErrorPath": str(self.stderr_path),
        }


class ServiceManager:
    """Manage the state directory's user launchd job on macOS."""

    def __init__(self, config: ServiceConfig, *, require_installed: bool = False):
        self.config = config
        self.require_installed = require_installed

    @classmethod
    def from_state(cls, state_dir: Path | str, **kwargs: Any) -> "ServiceManager":
        require_installed = bool(kwargs.pop("require_installed", False))
        config = ServiceConfig.from_state(state_dir, validate_install=require_installed, **kwargs)
        return cls(config, require_installed=require_installed)

    def _require_supported(self) -> None:
        if sys.platform != "darwin":
            raise ServiceError("macOS launchd services are only supported on macOS")

    def _require_runtime(self) -> None:
        if self.require_installed:
            return
        _validate_installed_package()

    def _run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                ["launchctl", *args],
                text=True,
                capture_output=True,
                check=False,
            )
        except OSError as exc:
            raise ServiceError(f"cannot execute launchctl: {exc}") from exc
        if check and result.returncode != 0:
            detail = (result.stderr or result.stdout or "launchctl failed").strip()
            raise ServiceError(f"launchctl {' '.join(args)} failed: {detail}")
        return result

    def _ownership(self) -> tuple[bool, bool]:
        path = self.config.plist_path
        if not path.exists():
            return False, False
        try:
            with path.open("rb") as stream:
                payload = plistlib.load(stream)
        except (OSError, plistlib.InvalidFileException, ValueError) as exc:
            raise ServiceError(f"cannot inspect service plist: {path}") from exc
        expected_arguments = [
            str(self.config.python_executable),
            "-m",
            PACKAGE_MODULE,
            "--state",
            str(self.config.state_dir),
            "serve",
        ]
        owned = (
            payload.get("Label") == self.config.label
            and payload.get("ProgramArguments") == expected_arguments
        )
        return True, owned

    def _require_owned_plist(self) -> None:
        exists, owned = self._ownership()
        if not exists:
            raise ServiceError(f"service is not installed: {self.config.plist_path}")
        if not owned:
            raise ServiceError(
                f"refusing to modify foreign launchd plist: {self.config.plist_path}"
            )

    def _loaded(self) -> bool:
        return self._run("print", self.config.service_target, check=False).returncode == 0

    def _wait_unloaded(self, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while self._loaded() and time.monotonic() < deadline:
            time.sleep(0.1)
        if self._loaded():
            raise ServiceError(f"launchd did not finish stopping {self.config.label}")

    def _bootstrap(self) -> None:
        """Load a plist, tolerating launchd's asynchronous bootout teardown."""

        deadline = time.monotonic() + 10.0
        last = "launchctl bootstrap failed"
        while True:
            result = self._run(
                "bootstrap", self.config.domain, str(self.config.plist_path), check=False
            )
            if result.returncode == 0:
                return
            last = (result.stderr or result.stdout or last).strip()
            if time.monotonic() >= deadline:
                raise ServiceError(f"launchctl bootstrap failed: {last}")
            time.sleep(0.2)

    def _prepare_private_paths(self) -> None:
        self.config.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.config.state_dir, 0o700)
        self.config.service_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.config.service_dir, 0o700)
        for log_path in (self.config.stdout_path, self.config.stderr_path):
            descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            os.close(descriptor)
            os.chmod(log_path, 0o600)
        self.config.launch_agents_dir.mkdir(parents=True, exist_ok=True)

    def _write_plist(self) -> None:
        self._prepare_private_paths()
        payload = self.config.plist()
        fd, temporary = tempfile.mkstemp(
            prefix=f".{self.config.label}.", suffix=".tmp", dir=self.config.launch_agents_dir
        )
        try:
            with os.fdopen(fd, "wb") as stream:
                plistlib.dump(payload, stream, fmt=plistlib.FMT_XML, sort_keys=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.config.plist_path)
            os.chmod(self.config.plist_path, 0o600)
        except Exception:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    def install(self) -> dict[str, Any]:
        self._require_supported()
        self._require_runtime()
        exists, owned = self._ownership()
        if exists and not owned:
            raise ServiceError(
                f"refusing to overwrite foreign launchd plist: {self.config.plist_path}"
            )
        self._write_plist()
        if self._loaded():
            self._run("bootout", self.config.service_target)
            self._wait_unloaded()
        self._bootstrap()
        return self.status()

    def start(self) -> dict[str, Any]:
        self._require_supported()
        self._require_runtime()
        self._require_owned_plist()
        if not self._loaded():
            self._bootstrap()
        return self.status()

    def stop(self) -> dict[str, Any]:
        self._require_supported()
        self._require_owned_plist()
        if self._loaded():
            self._run("bootout", self.config.service_target)
            self._wait_unloaded()
        return self.status()

    def restart(self) -> dict[str, Any]:
        self._require_supported()
        self._require_runtime()
        self._require_owned_plist()
        if self._loaded():
            self._run("kickstart", "-k", self.config.service_target)
        else:
            self._bootstrap()
        return self.status()

    def uninstall(self) -> dict[str, Any]:
        self._require_supported()
        self._require_owned_plist()
        if self._loaded():
            self._run("bootout", self.config.service_target)
            self._wait_unloaded()
        self.config.plist_path.unlink()
        return self.status()

    def status(self) -> dict[str, Any]:
        self._require_supported()
        exists, owned = self._ownership()
        result = self._run("print", self.config.service_target, check=False)
        return {
            "label": self.config.label,
            "domain": self.config.domain,
            "plist": str(self.config.plist_path),
            "installed": exists and owned,
            "foreign_plist": exists and not owned,
            "loaded": result.returncode == 0,
            "service_dir": str(self.config.service_dir),
        }
