# Local installation and upgrades

`codex-monitor` is not assumed to exist on a public package registry. Install it from a trusted checkout, a local wheel, or the project release archive. The installer requires Python 3.11 or newer and currently supports macOS and Linux. Its atomic release switch uses POSIX symlinks; this installer has not been validated on Windows and refuses to run there.

The runtime and the optional `$codex-monitor` skill are separate. Installing either one does not initialize monitor state, start the receiver, attach a conversation, or create an event producer.

## Install from a checkout

From the repository root, run:

```bash
python3 scripts/install.py install
```

This builds a wheel from the current checkout, creates a fresh versioned virtual environment, installs dependencies, runs `pip check`, validates `codex-monitor --help`, and atomically switches the stable launcher.

To install the bundled skill at the same time:

```bash
python3 scripts/install.py --with-skill install
```

The default runtime prefix is `~/.local/share/codex-monitor`. The default skill root is `$CODEX_HOME/skills` when `CODEX_HOME` is set, or `~/.codex/skills` otherwise. Start a new Codex session after installing the skill so it can be discovered.

## Install from a release archive or local wheel

A release archive contains the installer, one project wheel under `wheels/`, the skill/plugin files, and `SHA256SUMS.json`. It is a local artifact, not evidence of publication to a registry. Verify the archive checksum supplied alongside the archive and the file hashes inside it before installation.

From the extracted release directory, use the relative wheel path printed in `INSTALL.txt`:

```bash
python3 scripts/install.py \
  --wheel wheels/codex_monitor-VERSION-py3-none-any.whl \
  --with-skill install
```

`--wheel` accepts an existing relative or absolute `.whl` path and normalizes it to an absolute path before staging. The installer validates that the wheel metadata names `codex-monitor`; it never substitutes a package with the same name from an index. `pip` may still use the configured Python package index to resolve declared third-party dependencies such as `websockets`.

To build an unpublished release archive from a checkout:

```bash
python3 scripts/build-release.py --out /absolute/output/directory
```

## Installed paths

Every successful install or upgrade prints JSON containing the exact absolute executable path. With the default prefix it is:

```text
~/.local/share/codex-monitor/bin/codex-monitor
```

The installer does not edit shell startup files and does not claim that this directory is on `PATH`. Invoke the printed absolute path, or set `CODEX_MONITOR_BIN` to it for the bundled skill helper.

The prefix layout is:

```text
PREFIX/
  .codex-monitor-installer.json
  bin/codex-monitor        -> ../current/bin/codex-monitor
  current                  -> releases/RELEASE_ID
  releases/RELEASE_ID/     # complete virtual environment
```

Each upgrade installs into a new release directory. It never runs `pip install --upgrade` against the active environment. The `current` symlink changes only after the candidate passes dependency and CLI validation. Earlier releases remain available for inspection until uninstall.

The atomic guarantee applies to the runtime `current` switch. With `--with-skill`, installer-owned skill files are updated individually before the runtime switch; the skill directory and runtime do not form one filesystem transaction. A process or machine failure during that short phase can require review with `--with-skill status`, while the previously selected runtime remains active.

Monitor state is separate. Its default remains `~/.local/state/codex-monitor`, or the path in `CODEX_MONITOR_HOME`/`--state`. The installer does not read, rewrite, migrate, or delete state configuration, source tokens, receipts, or reply data.

## Custom locations

Use an absolute runtime prefix when the default is unsuitable:

```bash
python3 scripts/install.py \
  --prefix /absolute/user-owned/codex-monitor \
  install
```

Use `--with-skill` and an absolute `--skill-root` to manage the skill elsewhere:

```bash
python3 scripts/install.py \
  --prefix /absolute/user-owned/codex-monitor \
  --with-skill \
  --skill-root /absolute/codex-home/skills \
  install
```

The installer places only `codex-monitor` below the selected skill root. It does not change other skills, plugins, marketplace configuration, or shell files.

## Status

Status is read-only:

```bash
python3 scripts/install.py status
python3 scripts/install.py --with-skill status
```

It reports ownership, the active version and release, the stable executable, retained releases, launchd services that reference the managed runtime, and optional skill modifications. A missing or unknown ownership marker is reported rather than adopted.

After runtime installation, monitor lifecycle checks use the installed executable, for example:

```bash
/absolute/prefix/bin/codex-monitor --version
/absolute/prefix/bin/codex-monitor init
/absolute/prefix/bin/codex-monitor sessions
```

## Upgrade

Upgrade from the checkout:

```bash
python3 scripts/install.py upgrade
```

Upgrade from an extracted archive or explicit wheel:

```bash
python3 scripts/install.py \
  --wheel wheels/codex_monitor-VERSION-py3-none-any.whl \
  upgrade
```

Add `--with-skill` to upgrade the bundled skill too. The skill ownership manifest records installed file hashes. Files that still match the previous manifest are updated; user-modified, deleted, or conflicting files are preserved and listed in the result. The installer refuses to replace a pre-existing unowned `codex-monitor` skill directory.

A failed build, wheel install, dependency check, or CLI validation removes the candidate and leaves `current` on the prior working release.

### Upgrade with a macOS launchd receiver

The launchd plist records the virtual environment's lexical Python path. Changing `current` alone would leave the service running the old release. Therefore upgrade refuses whenever a `com.codex.monitor.*` LaunchAgent still references any release under the managed prefix, even if that job is stopped.

The error prints commands using the exact old executable, state path, installer path, wheel, prefix, and optional skill root. Follow them in order:

```bash
/absolute/old-release/bin/codex-monitor --state /absolute/state service stop
/absolute/old-release/bin/codex-monitor --state /absolute/state service uninstall
python3 /absolute/install.py --prefix /absolute/prefix --wheel /absolute/wheel upgrade
/absolute/prefix/bin/codex-monitor --state /absolute/state service install
```

`service install` writes a new plist using the upgraded release's Python path and loads it. The installer never edits or restarts the service silently.

## Uninstall

Remove the managed runtime:

```bash
python3 scripts/install.py uninstall
```

This removes only a prefix with the installer's valid ownership marker and expected managed entries. It refuses a non-empty unowned prefix, a changed launcher, untracked prefix entries, untracked release directories, or a runtime referenced by a launchd service. Stop and uninstall the service with the exact commands in the error before retrying.

Every installed release has a file-and-symlink inventory. Uninstall refuses when a release contains changed or untracked data, including state accidentally written below a virtual environment, rather than deleting it recursively without review.

The skill remains unless removal is explicitly requested:

```bash
python3 scripts/install.py --with-skill uninstall
```

Skill removal requires the installer's ownership marker and byte-for-byte matches for all owned files, with no untracked files. If a skill was customized, uninstall refuses and preserves both the skill and runtime so the user can review them first. Running uninstall without `--with-skill` removes the runtime and leaves the skill untouched.

Monitor state belongs outside the managed runtime. If `CODEX_MONITOR_HOME` points inside the prefix, uninstall refuses rather than deleting it. A normal uninstall reports the preserved state path.

## Ownership and recovery rules

- The runtime prefix and skill use separate ownership markers. One marker never grants permission to alter the other location.
- Mutating commands serialize through a persistent, user-owned sibling lock file named from a hash of the prefix. The file is intentionally retained to avoid lock-inode replacement races; it contains only its owner tag and absolute prefix, not credentials.
- An existing non-empty directory without the expected marker is never claimed, overwritten, or removed.
- Installer failures do not modify shell configuration, Codex settings, plugins, marketplaces, other skills, or monitor state.
- A partially prepared release is not selected. `status` identifies the active `current` release and all releases recorded by the installer.
- Release archives and wheels are local inputs. If a trusted artifact is unavailable, obtain it from the project owner; do not install an unrelated registry package as a substitute.

The service guard inspects the current user's standard macOS `~/Library/LaunchAgents/com.codex.monitor.*.plist` files. It does not discover services installed by another account or service manager. Stop those explicitly before changing or removing their runtime.
