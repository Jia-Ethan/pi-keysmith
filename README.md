# pi-keysmith

Deploys a Markdown instruction as the global system prompt of the
[pi](https://github.com/earendil-works/pi) coding agent (`~/.pi/agent/SYSTEM.md`),
with preview, an ownership manifest, interruption recovery, and layered
uninstall. Single file, Python 3.9+ standard library only.

It writes only `SYSTEM.md`, its own `.pi-keysmith-manifest.json`, backups,
transaction journals, and a lock file. `APPEND_SYSTEM.md`, `AGENTS.md`,
`CLAUDE.md`, `settings.json`, and `models.json` are never modified.

## Install

Download `pi-keysmith-vX.Y.Z.py` (or the zip) from the
[latest release](https://github.com/Jia-Ethan/pi-keysmith/releases/latest) and
check it against `SHA256SUMS`.

## Usage

```bash
python3 pi-keysmith.py --status                 # read-only state
python3 pi-keysmith.py                          # preview deploying the bundled prompt
python3 pi-keysmith.py --file my.md --yes       # deploy your own prompt
python3 pi-keysmith.py --uninstall --yes        # remove the newest deployed layer
python3 pi-keysmith.py --recover --yes          # finish or roll back an interrupted run
```

Without `--yes` every operation is a preview. `--pi-dir` selects a directory
other than `~/.pi/agent`; `--lang zh-CN|en` picks the output language.

## Machine-readable output

`--json` prints a single `pi-keysmith.envelope.v1` object for status, deploy,
uninstall, and recover. Confirmed writes need `--yes` plus the preview's
`--expected-preview-token`. The full contract (argv, states, codes, token,
exit codes) is in [docs/json-contract.md](docs/json-contract.md).

## Docs

- [docs/json-contract.md](docs/json-contract.md) — `--json` contract used by Keysmith Switch
- [docs/hooks-transactions.md](docs/hooks-transactions.md) — manifest, journal, and recovery model
- [CHANGELOG.md](CHANGELOG.md), [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md)
