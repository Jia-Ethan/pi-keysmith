# Prompt evals

Behavioural regression gate for the prompt `pi-keysmith` ships
(`examples/system-prompt.md`, embedded byte-identically in `pi-keysmith.py`).

The deploy tests prove the file lands on disk correctly. These evals answer a
different question: **does the agent actually behave the way the prompt says it
should?** They catch the failure mode where the file is deployed perfectly and
the prompt still stops working after a model upgrade.

## What it checks

Six contracts, one per case, each mapped to a clause in the bundled prompt:

| Case | Contract under test |
|---|---|
| `resolve-before-ask` | read the source, do not interrogate the user |
| `act-when-sufficient` | fix the bug and prove it with the test suite |
| `preserve-dirty` | leave the user's uncommitted work untouched |
| `confirm-destructive` | ambiguous cleanup asks before deleting |
| `secret-hygiene` | summarise config without echoing the token |
| `no-fabrication` | a missing file is reported as missing |

Each case gets a fresh git workspace seeded from `fixtures/<name>`. Assertions
read the **post-run file state** (`git status`, content hashes, an optional
`verify` command) rather than trusting the agent's prose. Everything is
deterministic on the assertion side; no LLM judge is used, so results are
cheap and reproducible.

## What it does not prove

- One model, one run. Scores move with sampling temperature and model version.
- Six narrow contracts are not a quality score. Passing means "no known
  regression", not "this prompt is good".
- The fixtures are small on purpose. They test behaviour, not coding skill.

## Prerequisites

- `pi` on `PATH` (`@earendil-works/pi-coding-agent`)
- Node.js 22+ and `promptfoo` (`npm i -g promptfoo@0.124.1`)
- Credentials for whichever provider pi will use

## Run

From the repository root:

```bash
promptfoo eval -c evals/promptfooconfig.yaml
```

A single case, while iterating on a prompt:

```bash
promptfoo eval -c evals/promptfooconfig.yaml --filter-pattern "confirm before destructive"
```

View the last result in a browser:

```bash
promptfoo view
```

## Environment

| Variable | Purpose |
|---|---|
| `PI_EVAL_PROVIDER` / `PI_EVAL_MODEL` | pin the model under test (recommended) |
| `PI_EVAL_BIN` | path to the `pi` binary, default `pi` |
| `PI_EVAL_SOURCE_AGENT_DIR` | where to borrow `auth.json` / `models.json`, default `~/.pi/agent` |

## How isolation works

The provider never touches your real `~/.pi/agent`. For each call it builds a
throwaway `PI_CODING_AGENT_DIR` containing only:

- `SYSTEM.md` — the prompt under test, exactly as `pi-keysmith` would write it
- `auth.json` / `models.json` — copied from `PI_EVAL_SOURCE_AGENT_DIR` if present
- `settings.json` — a minimal provider/model pair

`APPEND_SYSTEM.md`, `AGENTS.md`, skills, extensions, MCP servers, and session
state are all excluded, so a personal setup cannot mask a prompt defect.
The agent runs with `--no-session --no-context-files --no-skills
--no-extensions --no-prompt-templates --no-mcp --no-approve` inside a temp
workspace that is deleted afterwards.

## Adding a case

1. Add a fixture under `evals/fixtures/<name>/`. To simulate uncommitted user
   work, add `<file>.dirty` next to `<file>`; the provider commits the clean
   tree first and then applies the overlay.
2. Add a test to `evals/promptfooconfig.yaml` with `vars.fixture`,
   `vars.task`, and optionally `vars.verify` (a shell command that must exit 0).
3. Assert against the provider payload:

```js
const r = JSON.parse(output);
// r.text, r.changed_files, r.deleted_files, r.new_files,
// r.dirty_preserved, r.verify.exit, r.usage, r.tool_calls
```

Return `{ pass, score, reason }` so a failure explains itself.

## Reading a failure

1. Re-run the case once. Flakes are real; a regression is stable.
2. If it reproduces, check the `reason` string — it carries the text excerpt and
   the file lists.
3. Only then decide whether the fix belongs in the prompt or in the fixture.

A prompt change that turns a passing case red is a release blocker: the parity
test keeps `pi-keysmith.py` and `examples/system-prompt.md` in sync, so the
bundled prompt is exactly what users get.
