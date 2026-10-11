# pi-keysmith JSON contract (`pi-keysmith.envelope.v1`)

This is the machine-readable contract for callers such as Keysmith Switch. It
ships with pi-keysmith **0.2.0**; callers should require adapter version
`>= 0.2.0`. Human-readable output (without `--json`) is unchanged and is not
part of this contract.

## Scope

- Only the global pi agent directory (`~/.pi/agent`). The caller names it with
  an absolute `--pi-dir`. There is no project-level `SYSTEM.md` and no
  second-directory discovery.
- A confirmed deploy writes only `<pi-dir>/SYSTEM.md` and
  `.pi-keysmith-manifest.json`, plus its own backups, journals, and lock file.
- `APPEND_SYSTEM.md`, `AGENTS.md`, `CLAUDE.md`, `settings.json`, and
  `models.json` are never modified.

## argv

Pass every argument as its own argv element.

| Operation | argv |
| --- | --- |
| version | `--version` (argparse text, not an envelope) |
| status / doctor | `--status --json --pi-dir <abs> --lang en` |
| plan-activate | `--file <abs.md> --name <safe> --pi-dir <abs> --dry-run --json --lang en` (omit `--file` for the bundled prompt) |
| activate | `--file <abs.md> --name <safe> --pi-dir <abs> --expected-preview-token <64 hex> --yes --json --lang en` |
| plan-deactivate | `--uninstall --pi-dir <abs> --json --lang en` |
| deactivate | `--uninstall --pi-dir <abs> --expected-preview-token <64 hex> --yes --json --lang en` |
| plan-recover | `--recover --pi-dir <abs> --json --lang en` |
| recover | `--recover --pi-dir <abs> --expected-preview-token <64 hex> --yes --json --lang en` |

Rules:

- With `--json`, `--pi-dir` is required and must be absolute. It may not
  exist: status then reports `not-installed` and writes are blocked by
  `pi-dir-missing`. The CLI never creates `pi_dir`.
- `pi_dir` may be a symlink to a directory. `target.pi_dir` echoes the given
  path; `target.pi_dir_resolved` is the real path. Tokens and journals use the
  resolved path.
- Previews take neither `--yes` nor a token. `--dry-run` is always a preview.
- With `--json`, a confirmed write needs both `--yes` and the token from the
  immediately preceding preview's `plan.confirmation_token`.
- `--expected-preview-token` is only valid on confirmed writes (with or
  without `--json`). Status and previews reject it.
- `--status` rejects `--yes`, `--file`, `--name`. `--uninstall` and
  `--recover` reject `--file` and `--name`.
- `--dry-run`, `--status`, `--uninstall`, `--recover` are mutually exclusive.
  The default operation is deploy; without `--yes` it is a preview.
- `--lang` only affects the sentence after the code in `diagnostics`. Keys and
  codes are always English.

## Envelope

With `--json`, stdout is exactly one JSON object, including on usage errors.
stderr stays empty except for uncaught exceptions.

```json
{
  "schema": "pi-keysmith.envelope.v1",
  "tool": "pi-keysmith",
  "version": "0.2.0",
  "operation": "status",
  "preview": true,
  "apply": false,
  "ok": true,
  "exit_code": 0,
  "target": {"pi_dir": "/abs/.pi/agent", "pi_dir_resolved": "/abs/.pi/agent", "scope": "user"},
  "status": {},
  "plan": null,
  "result": null,
  "diagnostics": []
}
```

| Field | Meaning |
| --- | --- |
| `operation` | `status`, `deploy`, `uninstall`, `recover`; `null` if argv could not be parsed |
| `preview` | `true` when nothing was written |
| `apply` | `true` when `--yes` reached the write path |
| `ok` | whether the caller may treat the call as success; see exit codes |
| `exit_code` | equals the process exit code |
| `target.scope` | always `user`; anything else is a contract violation. `target` is `{}` when `--pi-dir` was not given |
| `status` | read-only snapshot on every envelope except usage errors. Status and previews show the state before; confirmed writes (success or failure) show the state after |
| `plan` | non-null for deploy/uninstall/recover previews and confirms; `null` for status |
| `result` | non-null only after a successful confirmed write |
| `diagnostics` | strings of the form `"<code>: <sentence>"`. Match only the part before the first colon. Never contains secrets or prompt text |

## Status

```json
{
  "state": "active-aligned",
  "recovery_required": false,
  "nodes": {
    "pi_dir": {"kind": "directory", "path": "/abs/.pi/agent"},
    "system": {"kind": "regular", "path": "/abs/.pi/agent/SYSTEM.md", "fingerprint": {"sha256": "<64 hex>", "size": 1}},
    "manifest": {"kind": "regular", "path": "/abs/.pi/agent/.pi-keysmith-manifest.json", "fingerprint": {"sha256": "<64 hex>", "size": 1}},
    "append_system": {"kind": "missing", "path": "/abs/.pi/agent/APPEND_SYSTEM.md", "fingerprint": null},
    "agents_md": {"kind": "missing", "path": "/abs/.pi/agent/AGENTS.md", "fingerprint": null},
    "claude_md": {"kind": "missing", "path": "/abs/.pi/agent/CLAUDE.md", "fingerprint": null}
  },
  "manifest": null,
  "drift": [],
  "conflicts": [],
  "residue": {"journals": [], "cleanup_markers": [], "temp_files": []},
  "preserved_changed": [],
  "blockers": {"deploy": [], "uninstall": []},
  "deployable": true,
  "uninstall_ready": true
}
```

- `nodes.*.kind`: `missing`, `regular`, `directory`, `symlink`, `special`.
  `fingerprint` is present only for `regular`. A `pi_dir` symlink to a
  directory reports `directory`.
- `manifest`: public fields of the valid schema-v1 manifest
  (`deployment_id`, `tool_version`, `system.before/after/backup`,
  `append_system.preserved`, `agents_md.preserved`, `previous_manifest`);
  `null` when absent or invalid. `system.after.sha256` is the deployed prompt.
- `residue` lists basenames: `journals` (`.pi-keysmith-transaction-*`),
  `cleanup_markers` (`.pi-keysmith-cleanup-*.intent.json`), `temp_files`
  (regular `.keysmith-*` files in `pi_dir`). The lock file
  `.pi-keysmith.lock` is not residue.

### State order

First match wins.

| `state` | Condition |
| --- | --- |
| `not-installed` | `pi_dir` does not exist (`deployable=false`, `blockers.deploy=["pi-dir-missing"]`, exit 0) |
| `conflict` | `pi_dir` is not a directory; `SYSTEM.md` or manifest is not a regular file; manifest invalid; a journal or cleanup marker cannot be parsed; more than one unfinished transaction; residue that is not a regular file; a recovery resource that is neither before nor a planned after |
| `recovery-required` | any residue (journal, cleanup marker, temp file), even with drift |
| `not-installed` | no valid manifest and no residue (missing or hand-written `SYSTEM.md`) |
| `drift` | valid manifest, `SYSTEM.md` does not match `system.after` |
| `active-aligned` | valid manifest, `SYSTEM.md` matches `system.after`, no residue |

Invariant: `state == "recovery-required"` exactly when the recover preview's
`plan.writes` is non-empty. Anything recover cannot handle is a `conflict`.

### Codes

- `drift`: `system-fingerprint-mismatch`
- `conflicts`: `pi-dir-not-directory`, `system-not-regular`,
  `manifest-not-regular`, `manifest-invalid`, `journal-invalid`,
  `journal-ambiguous`, `residue-not-regular`,
  `recover-resource-unexpected:<system|manifest>`
- additional blockers: `pi-dir-missing`, `residue-present`,
  `preserved-not-regular:<append_system|agents_md|claude_md>`,
  `not-installed`, `system-fingerprint-mismatch`, `system-backup-missing`,
  `system-backup-mismatch`, `manifest-backup-missing`,
  `manifest-backup-mismatch`
- other diagnostics: `preserved-changed:<node>`, `preview-token-mismatch`,
  `preview-token-required`, `preview-token-invalid`,
  `preview-token-unexpected`, `pi-dir-required`, `pi-dir-not-absolute`,
  `name-invalid`, `prompt-file-invalid`, `usage-error`, `lock-busy`,
  `lock-unavailable`, `write-failed`, `windows-beta`

### Blockers

The booleans come from the same preflight the write path re-runs under lock.

- `blockers.deploy` = `conflicts` + `pi-dir-missing` + `residue-present` +
  `preserved-not-regular:*`. Drift and a hand-written `SYSTEM.md` do not
  block deploy (it is backed up and replaced).
- `blockers.uninstall` = `conflicts` + `pi-dir-missing` + `not-installed`
  (no valid manifest) + `residue-present` + `system-fingerprint-mismatch` +
  backup checks. Both the `SYSTEM.md` backup and the previous-manifest backup
  must exist and match their recorded fingerprints.
- `preserved-not-regular:*` blocks deploy only; it is not a conflict.
- `preserved_changed` lists `append_system` / `agents_md` whose fingerprint
  differs from the manifest. It blocks nothing; the uninstall preview adds a
  `preserved-changed:<node>` diagnostic.

## Plan

```json
{
  "confirmation_token": "<64 lowercase hex>",
  "blockers": [],
  "writes": [{"path": "SYSTEM.md", "action": "replace", "backup": "SYSTEM.md.bak_<ts>"}],
  "preserves": ["APPEND_SYSTEM.md", "AGENTS.md", "CLAUDE.md", "settings.json", "models.json"],
  "prompt": {"name": "system-prompt", "source": "builtin", "sha256": "<64 hex>", "size": 1}
}
```

- `blockers`: `status.blockers.deploy` (deploy), `status.blockers.uninstall`
  (uninstall), or `status.conflicts` (+ `pi-dir-missing`) for recover. When
  non-empty, the preview is `ok=false`, exit 1, `confirmation_token=null`,
  `writes=[]`.
- deploy `writes`: `SYSTEM.md` then manifest, each `create` or `replace`
  (`replace` carries `backup`).
- uninstall `writes`: `SYSTEM.md` `restore` (with `source`) or `remove`;
  manifest `archive` (with `archive`); manifest `restore` when a previous layer
  exists.
- recover `writes`:
  - unfinished journal: `restore` / `remove` of manifest then `SYSTEM.md`,
    then `cleanup` of the journal;
  - terminal journal (`committed` / `recovered`): `cleanup` only, never a
    rollback;
  - journal plus cleanup marker (interrupted cleanup): `cleanup` of both;
  - orphan cleanup marker: `cleanup`;
  - temp file: `remove`;
  - nothing to do: `writes=[]`, `ok=true`, exit 0, token still present.
- `prompt` (deploy only): `source` is `builtin` or `file`; only sha256 and
  size, never the text. `name` echoes `--name` and is not part of the token.

### Confirmation token

```text
payload = {
  "schema": "pi-keysmith.preview-token.v1",
  "operation": "deploy" | "uninstall" | "recover",
  "pi_dir": "<resolved absolute path>",
  "state": "<status.state at preview>",
  "system_sha256": "<SYSTEM.md sha256 or null>",
  "manifest_sha256": "<manifest sha256 or null>",
  "prompt_sha256": "<deploy only, else null>",
  "backups": {"system": "<sha256 or null>", "manifest": "<sha256 or null>"},   // uninstall only, else null
  "journals": [{"name": "<basename>", "phase": "<phase>", "sha256": "<journal.json sha256>"}],  // sorted by name
  "orphans": ["<sorted basenames of cleanup markers and temp files>"]
}
token = sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
```

Any change after the preview (an edited `SYSTEM.md`, a touched backup, a
journal that advanced a phase) makes the confirm fail with exit 3.

## Concurrency

Confirmed writes take `<pi_dir>/.pi-keysmith.lock` (POSIX
`fcntl.flock(LOCK_EX | LOCK_NB)`, Windows `msvcrt.locking` non-blocking),
then re-inspect and re-compute the token before writing. The token is checked
before blockers, so any post-preview change reports exit 3. Status and
previews do not lock. If the lock is held: no writes, exit 5, `lock-busy`.

## Result

```json
{
  "transaction_id": "<32 hex, or null for a no-op recover>",
  "applied": true,
  "system_sha256": "<64 hex or null>",
  "manifest_sha256": "<64 hex or null>",
  "cleaned": ["<basenames removed by recover>"]
}
```

For deploy, `transaction_id` equals the new manifest's `deployment_id`.

## Exit codes

| exit | When | `ok` | Writes |
| --- | --- | --- | --- |
| 0 | status not `conflict` / `recovery-required` (including missing `pi_dir`); preview without blockers; successful confirm; no-op recover | `true` | only on successful confirm |
| 1 | status `conflict` / `recovery-required`; preview or confirm refused by blockers | `false` | none |
| 2 | usage error (`status` is `null`) | `false` | none |
| 3 | `preview-token-mismatch` | `false` | none |
| 4 | failure after writes began (`write-failed`); `status.state` is usually `recovery-required` | `false` | possibly partial |
| 5 | `lock-busy` | `false` | none |

Exits 1, 2, 3, and 5 guarantee zero writes. Only exit 4 may require
`--recover`.

## Redaction

`diagnostics` and stderr never include prompt text or `SYSTEM.md` content.
Paths are limited to the caller's `pi_dir` and basenames under it. Manifest
and journal validation failures report only the code. On Windows the
deployment beta notice appears as the `windows-beta` diagnostic instead of a
banner.

## Differences from `grok-keysmith.envelope.v1`

Same shape, incompatible wire format. Switch needs its own `normalize_pi`.

| Aspect | Grok | pi |
| --- | --- | --- |
| State snapshot | `result.state` / `result.nodes` / `result.conflicts` | `status.*`; `result` only holds write results |
| `ok` on status | always `true` | `false` for `conflict` / `recovery-required` |
| drift exit | 1 | 0 |
| `diagnostics` | free text | `"<code>: <sentence>"` |
| States | includes `inactive` | no `inactive` |

Shared and reusable as-is: `plan.confirmation_token`, `plan.blockers`,
`preview`, `ok`, `target.scope`.
