#!/usr/bin/env python3
"""Package a host-native Rust runtime and skill with checksums; never publish."""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import tarfile
import tempfile
import tomllib


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("dist"))
    parser.add_argument("--binary", type=Path, help="Existing host-native release executable")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    version = tomllib.loads((root / "rust/Cargo.toml").read_text())["package"]["version"]
    binary = args.binary.resolve() if args.binary else root / "rust/target/release/codex-monitor-rs"
    if not args.binary:
        subprocess.run(["cargo", "build", "--locked", "--release", "--manifest-path", str(root / "rust/Cargo.toml")], check=True)
    actual = subprocess.check_output([str(binary), "--version"], text=True).strip()
    if actual != "codex-monitor " + version:
        parser.error("executable version does not match canonical Rust source")
    name = f"codex-monitor-{version}-{platform.system().lower()}-{platform.machine().lower()}"
    args.out.mkdir(parents=True, exist_ok=True)
    target = args.out.resolve() / (name + ".tar.gz")
    if target.exists():
        parser.error("release archive exists; choose a new output directory")
    with tempfile.TemporaryDirectory(prefix="cm-native-release-") as tmp:
        staging = Path(tmp)
        files = [(binary, Path("bin/codex-monitor")),
                 (root / "docs/INSTALLATION.md", Path("docs/INSTALLATION.md"))]
        for path in (root / "plugins/codex-monitor").rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts:
                files.append((path, path.relative_to(root)))
        instructions = staging / "INSTALL.txt"
        instructions.write_text(
            "Local native Rust release; not published. No Python runtime dependency.\n"
            "Verify the archive SHA-256 before extracting. From this directory:\n\n"
            "./bin/codex-monitor install --with-skill\n\n"
            "For existing Python state, follow docs/INSTALLATION.md before adoption.\n"
            "Installation does not start a receiver, change permissions or create conversations.\n")
        files.append((instructions, Path("INSTALL.txt")))
        manifest = {str(dest): hashlib.sha256(source.read_bytes()).hexdigest() for source, dest in files}
        manifest_path = staging / "SHA256SUMS.json"
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        files.append((manifest_path, Path("SHA256SUMS.json")))
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
    print(json.dumps({"archive": str(target), "sha256": digest, "runtime": "rust", "published": False}, indent=2))


if __name__ == "__main__":
    main()
