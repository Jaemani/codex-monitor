#!/usr/bin/env python3
"""Build a local, unpublished runtime+skill distribution with checksums."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import tomllib


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("dist"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    args.out.mkdir(parents=True, exist_ok=True)
    name = "codex-monitor-" + version
    target = args.out.resolve() / (name + ".tar.gz")
    if target.exists():
        parser.error("release archive already exists; choose a new output directory")
    with tempfile.TemporaryDirectory(prefix="cm-release-") as tmp:
        staging = Path(tmp)
        wheels = staging / "wheels"
        subprocess.run([sys.executable, "-m", "pip", "wheel", str(root), "--no-deps", "--wheel-dir", str(wheels)], check=True)
        files = [(p, Path("wheels") / p.name) for p in wheels.glob("*.whl")]
        files += [(root / "scripts/install.py", Path("scripts/install.py")),
                  (root / "docs/INSTALLATION.md", Path("docs/INSTALLATION.md"))]
        for path in (root / "plugins/codex-monitor").rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts:
                files.append((path, path.relative_to(root)))
        manifest = {str(dest): hashlib.sha256(source.read_bytes()).hexdigest() for source, dest in files}
        manifest_path = staging / "SHA256SUMS.json"
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        wheel_name = next(wheels.glob("*.whl")).name
        instructions = staging / "INSTALL.txt"
        instructions.write_text(
            "Local release; not published to a package registry. Requires Python 3.11+.\n"
            "From this extracted directory:\n\n"
            f"python3 scripts/install.py --wheel wheels/{wheel_name} --with-skill install\n\n"
            "See docs/INSTALLATION.md for status, upgrade and uninstall.\n"
            "Start a new Codex session after skill installation and invoke $codex-monitor.\n"
            "Installation does not start a receiver or invent an event producer.\n")
        files += [(manifest_path, Path("SHA256SUMS.json")), (instructions, Path("INSTALL.txt"))]
        temporary = target.with_suffix(target.suffix + ".tmp")
        try:
            with tarfile.open(temporary, "w:gz") as archive:
                for source, dest in files:
                    archive.add(source, arcname=str(Path(name) / dest), recursive=False)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_suffix(target.suffix + ".sha256").write_text(digest + "  " + target.name + "\n")
    print(json.dumps({"archive": str(target), "sha256": digest, "published": False}, indent=2))


if __name__ == "__main__":
    main()
