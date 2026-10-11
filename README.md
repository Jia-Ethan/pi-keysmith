# pi-keysmith

See CHANGELOG.md and docs.

## Machine-readable output

`--json` prints a single `pi-keysmith.envelope.v1` object for status, deploy,
uninstall, and recover. Confirmed writes need `--yes` plus the preview's
`--expected-preview-token`. The full contract (argv, states, codes, token,
exit codes) is in [docs/json-contract.md](docs/json-contract.md).
