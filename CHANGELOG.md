# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Formal release status is established only when a source commit is bound to an
immutable tag, a GitHub Release, and assets matching `SHA256SUMS`. A floating
branch or embedded version string is not sufficient.

## [0.1.0] — Unreleased

### Added

- First public candidate of `pi-keysmith`, a zero-runtime-dependency single-file
  Python CLI that deploys the bundled or a custom Markdown instruction as the
  pi global `SYSTEM.md` (`~/.pi/agent/SYSTEM.md`), which pi auto-discovers and
  uses to replace its default system prompt. No pi configuration file is
  mutated and pi has no hooks to isolate; `APPEND_SYSTEM.md` and `AGENTS.md`
  are left untouched and continue to stack on top as pi's own loader dictates.
- Bundled general engineering-assistant system prompt, embedded byte-for-byte
  in the script and mirrored as `examples/system-prompt.md`. Its SHA-256 is
  recomputed and displayed on deploy/status/dry-run.
- Manifest-based layered uninstall, durable transaction journal with immutable
  intent and before-state snapshots, explicit `--recover` for interrupted
  deploy/uninstall, read-only `--status`, preview-only `--dry-run`, and the
  `--yes` confirmation gate.
- Bilingual CLI output via `--lang auto|zh-CN|en` with `sys.argv` pre-scan.
- POSIX atomic no-replace rename, file/directory `fsync`, no-follow regular
  file enforcement, `0600`/`0700` private journals. Windows fresh deployment
  open under `EXPLICIT_BETA` with a prominent CLI warning; status, recover,
  uninstall and restore do not emit the deployment warning.
- `scripts/build_release.py` for deterministic candidate/formal release assets
  with tag/commit binding and `SHA256SUMS`.
- Test suite covering prompt parity, CLI, discovery, deploy/manifest/uninstall,
  durable journal/recovery, and reproducible release artifacts.

### Known limitations

- Single-file CLI; no `pip install` or automatic updater.
- Windows fresh deployment is `EXPLICIT_BETA` and is not a formal support claim.
- Python 3.9 is compatibility-test only; Python 3.10–3.14 is recommended.
- `main` / `Unreleased` is development state, not a formal Release.
- Backups and uninstall archives are not automatically cleaned.

[0.1.0]: https://github.com/Jia-Ethan/pi-keysmith/releases/tag/v0.1.0
