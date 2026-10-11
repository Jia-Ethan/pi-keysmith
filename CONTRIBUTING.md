# Contributing

Contributions are welcome. Read this document before opening a pull request.

## Before you start

- Formal release status is established only when a source commit is bound to
  an immutable tag, a GitHub Release, and assets matching `SHA256SUMS`. The
  floating `main` branch is development state.
- Do not paste credentials, complete configurations, or private paths into
  issues, PRs, or commit messages.
- Keep the zero-runtime-dependency, single-file CLI invariant for
  `pi-keysmith.py`. The release builder may use the standard library only.

## Development workflow

1. Fork and branch from `main`.
2. Run the local checks below before pushing:

   ```bash
   python3 -m py_compile pi-keysmith.py scripts/build_release.py
   python3 -m pytest -p no:cacheprovider -q tests
   python3 -m ruff check pi-keysmith.py tests scripts
   ```

3. Add tests for any behavioral change. The bundled prompt in the script must
   remain byte-identical to `examples/system-prompt.md`; the parity test
   enforces this.
4. Keep the bilingual (`zh-CN` / `en`) CLI strings in sync when you change
   user-facing output.
5. Open a PR with a clear description of the change, the risk surface, and the
   verification evidence.

## Commit and PR conventions

- Write commit messages that describe what changed and why.
- PRs that change transaction, manifest, or recovery behavior must add or
  update regression tests and document the state-machine impact in the PR
  description.
- Release PRs that change `VERSION` must also update `CHANGELOG.md` and keep
  the embedded prompt byte-identical to `examples/system-prompt.md`.
