# Installation

The skill/plugin and receiver runtime are separate. No public registry package or repository URL has
been established for this project; use a user-provided trusted checkout or release archive.

From the checkout: `python3 scripts/install.py --with-skill install`.
From an archive: `python3 scripts/install.py --wheel /absolute/release/wheels/codex_monitor-0.1.0-py3-none-any.whl --with-skill install`.

Default executable: `~/.local/share/codex-monitor/bin/codex-monitor`. For a custom prefix set
`CODEX_MONITOR_BIN` to the installed executable's absolute path. The installer prints actual paths.
Standalone skill installation uses `CODEX_HOME/skills` or `~/.codex/skills`. A new Codex session may be
needed for discovery. Installing a skill does not attach a conversation or start a producer.

Installer `status` reports ownership and paths. `upgrade` stages a validated new environment before
switching. Follow its exact service stop/update/reinstall instructions when it detects an active
service; never overwrite a live service interpreter in place. `uninstall` preserves monitor state,
tokens and receipts. `--with-skill` also manages only the installer's owned skill.

If trusted release files are unavailable, report the missing input rather than inventing a download URL
or installing an unrelated public package with the same name.
