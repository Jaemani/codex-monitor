# Repository instructions

This repository is published for external users. Before changing documentation, command examples, skills, releases or GitHub content, read [CONTRIBUTING.md](CONTRIBUTING.md).

Write public artifacts in English. Show commands using the actual executable and declared dependencies; host-specific execution wrappers belong only in the local tool invocation. Run `python3 scripts/check-publication.py` before committing or publishing.

Update docs/TODO.md and docs/STATUS.md with material changes. Keep real-client evidence separate from protocol checks and local transcripts outside Git.

The canonical runtime is Rust in `rust/`. Implement receiver, dashboard, resident,
permission/reconnect and lifecycle changes there. Python sources are frozen legacy
reference and migration-test fixtures; never ship a Python-only runtime fix.
Run the Rust tests and strict Clippy for runtime changes.
