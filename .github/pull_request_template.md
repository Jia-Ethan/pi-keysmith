## Summary

<!-- What does this PR change and why? -->

## Risk surface

<!-- Which subsystems are affected: deploy / manifest / journal / recovery / uninstall / prompt parity / release artifacts? -->

## Verification

<!-- Commands run and their results. At minimum: -->
- [ ] `python3 -m py_compile pi-keysmith.py scripts/build_release.py`
- [ ] `python3 -m pytest -p no:cacheprovider -q tests`
- [ ] `python3 -m ruff check pi-keysmith.py tests scripts`
- [ ] Bundled prompt in `pi-keysmith.py` is byte-identical to `examples/system-prompt.md`

## State-machine impact

<!-- If this changes transaction, manifest, or recovery behavior, describe the impact and link the new/updated tests. -->

## Checklist

- [ ] No credentials, complete configurations, or private paths in the diff.
- [ ] `VERSION` and `CHANGELOG.md` updated if version changes.
- [ ] Bilingual CLI strings kept in sync.
