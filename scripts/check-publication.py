#!/usr/bin/env python3
"""Check Git publication candidates for known documentation and privacy regressions."""
from pathlib import Path
import argparse
import re
import subprocess
import sys


def findings(path, content):
    errors = []
    if re.search(r"[\u1100-\u11ff\u3130-\u318f\uac00-\ud7af]", content):
        errors.append("non-English text: review Korean text in this public artifact")
    if path.endswith((".md", ".yml", ".yaml", ".json")) and re.search(r"\brtk\b", content, re.I):
        errors.append("internal execution wrapper in a public artifact")
    if re.search(r"/(?:Users|home)/[A-Za-z0-9_.-]+/|/private/" + r"var/folders/", content):
        errors.append("personal absolute path")
    if re.search(r"gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{20,}|-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----", content):
        errors.append("credential-shaped content; inspect locally without printing it")
    if path.startswith("docs/evidence/") and path not in {
        "docs/evidence/README.md", "docs/evidence/PUBLIC-SUMMARY.json"
    }:
        errors.append("raw evidence included in the publication candidate")
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staged", action="store_true", help="inspect the exact Git index that will be committed")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    paths = subprocess.check_output(
        ["git", "ls-files", "--cached", *([] if args.staged else ["--others", "--exclude-standard"]), "-z"], cwd=root
    ).decode().split("\0")
    failed = False
    checked = 0
    for name in sorted(set(filter(None, paths))):
        path = root / name
        if not args.staged and not path.is_file():
            continue
        try:
            content = (subprocess.check_output(["git", "show", ":" + name], cwd=root).decode()
                       if args.staged else path.read_text())
        except UnicodeDecodeError:
            print(f"{name}: binary publication candidate requires explicit review", file=sys.stderr)
            failed = True
            continue
        checked += 1
        for error in findings(name, content):
            print(f"{name}: {error}", file=sys.stderr)
            failed = True
    if not failed:
        print(f"Publication checks passed for {checked} files.")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
