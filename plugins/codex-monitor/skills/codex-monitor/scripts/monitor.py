#!/usr/bin/env python3
"""Resolve an installed runtime; never auto-install or invoke a shell."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def locate():
    explicit = os.environ.get("CODEX_MONITOR_BIN")
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_absolute():
            raise ValueError("CODEX_MONITOR_BIN must be an absolute executable path")
        return str(path) if path.is_file() and os.access(path, os.X_OK) else None
    found = shutil.which("codex-monitor")
    if found:
        return found
    path = Path.home() / ".local/share/codex-monitor/bin/codex-monitor"
    return str(path) if path.is_file() and os.access(path, os.X_OK) else None


def main():
    binary = locate()
    if sys.argv[1:] == ["context"]:
        print(json.dumps({"executable": binary, "installed": binary is not None,
            "current_thread": os.environ.get("CODEX_THREAD_ID"),
            "state": os.environ.get("CODEX_MONITOR_HOME", str(Path.home() / ".local/state/codex-monitor"))}, indent=2))
        return 0
    if not binary:
        print("codex-monitor runtime not found; use the trusted project installer or set CODEX_MONITOR_BIN", file=sys.stderr)
        return 2
    return subprocess.run([binary, *sys.argv[1:]], check=False).returncode


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
