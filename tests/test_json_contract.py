"""`--json` envelope contract tests (docs/json-contract.md)."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CLI_PATH = REPO_ROOT / "pi-keysmith.py"
SYSTEM_NAME = "SYSTEM.md"
MANIFEST_NAME = ".pi-keysmith-manifest.json"
JOURNAL_PREFIX = ".pi-keysmith-transaction-"
LOCK_NAME = ".pi-keysmith.lock"
TID = "abcdef0123456789abcdef0123456789"


def _invoke(args, *, extra_code: str = ""):
    """Run the CLI (optionally with injected test code) and return (code, envelope, stderr)."""
    env = dict(os.environ, PYTHONUTF8="1")
    if extra_code:
        script = (
            "import importlib.util, sys\n"
            f"spec = importlib.util.spec_from_file_location('pk', {str(CLI_PATH)!r})\n"
            "cli = importlib.util.module_from_spec(spec); spec.loader.exec_module(cli)\n"
            f"{extra_code}\n"
            "sys.exit(cli.main(sys.argv[1:]))\n"
        )
        cmd = [sys.executable, "-c", script, *args]
    else:
        cmd = [sys.executable, str(CLI_PATH), *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=str(REPO_ROOT))
    envelope = json.loads(proc.stdout) if "--json" in args and proc.stdout else None
    return proc.returncode, envelope, proc.stderr


def _ok_json(args, expect: int, **kw):
    code, env, err = _invoke(args, **kw)
    assert code == expect, json.dumps(env, indent=2)
    assert env["schema"] == "pi-keysmith.envelope.v1"
    assert env["exit_code"] == code
    assert env["ok"] is (code == 0)
    assert err == ""
    return env


def status(pi_dir: Path, expect: int = 0):
    return _ok_json(["--status", "--json", "--pi-dir", str(pi_dir), "--lang", "en"], expect)


def plan_deploy(pi_dir: Path, expect: int = 0, extra=()):
    return _ok_json(["--dry-run", "--json", "--pi-dir", str(pi_dir), "--lang", "en", *extra], expect)


def confirm_deploy(pi_dir: Path, token: str, expect: int = 0, extra=(), **kw):
    args = ["--yes", "--json", "--pi-dir", str(pi_dir), "--expected-preview-token", token, "--lang", "en", *extra]
    return _ok_json(args, expect, **kw)


def plan_op(op: str, pi_dir: Path, expect: int = 0):
    return _ok_json([f"--{op}", "--json", "--pi-dir", str(pi_dir), "--lang", "en"], expect)


def confirm_op(op: str, pi_dir: Path, token: str, expect: int = 0, **kw):
    args = [f"--{op}", "--yes", "--json", "--pi-dir", str(pi_dir), "--expected-preview-token", token, "--lang", "en"]
    return _ok_json(args, expect, **kw)


def deploy(pi_dir: Path, extra=()):
    token = plan_deploy(pi_dir, extra=extra)["plan"]["confirmation_token"]
    return confirm_deploy(pi_dir, token, extra=extra)


def snapshot(pi_dir: Path):
    """Bytes of every regular file under pi_dir, keyed by relative path."""
    if not pi_dir.is_dir():
        return None
    out = {}
    for path in sorted(pi_dir.rglob("*")):
        if path.name == LOCK_NAME:
            continue  # the lock file may persist and is not a write of managed state
        if path.is_file() and not path.is_symlink():
            out[str(path.relative_to(pi_dir))] = path.read_bytes()
        else:
            out[str(path.relative_to(pi_dir))] = path.is_symlink() or path.is_dir()
    return out


def codes(diagnostics):
    return [d.split(":", 1)[0] for d in diagnostics]


def simulate_journal(cli, pi_dir: Path, phase: str = "initializing") -> Path:
    resolved = str(pi_dir.resolve())
    journals = cli._create_deployment_journals(
        [pi_dir],
        TID,
        {resolved: cli._portable_fingerprint(pi_dir / SYSTEM_NAME)},
        {resolved: cli._portable_fingerprint(pi_dir / MANIFEST_NAME)},
        cli._prompt_sha256("NEW-CONTENT"),
    )
    if phase != "initializing":
        cli._update_journals(journals, phase)
    return pi_dir / f"{JOURNAL_PREFIX}{TID}"


# ─── State fixtures ──────────────────────────────────────────────────────────
def _missing(tmp_path, cli):
    return tmp_path / "nope" / "agent"


def _file(tmp_path, cli):
    path = tmp_path / "agent"
    path.write_text("x", encoding="utf-8")
    return path


def _empty(tmp_path, cli):
    path = tmp_path / "agent"
    path.mkdir()
    return path


def _handwritten(tmp_path, cli):
    path = _empty(tmp_path, cli)
    (path / SYSTEM_NAME).write_text("MINE\n", encoding="utf-8")
    return path


def _aligned(tmp_path, cli):
    path = _empty(tmp_path, cli)
    deploy(path)
    return path


def _drift(tmp_path, cli):
    path = _aligned(tmp_path, cli)
    (path / SYSTEM_NAME).write_text("EDITED\n", encoding="utf-8")
    return path


def _bad_manifest(tmp_path, cli):
    path = _empty(tmp_path, cli)
    (path / MANIFEST_NAME).write_text("{not json", encoding="utf-8")
    return path


def _system_symlink(tmp_path, cli):
    path = _empty(tmp_path, cli)
    target = tmp_path / "elsewhere.md"
    target.write_text("T\n", encoding="utf-8")
    (path / SYSTEM_NAME).symlink_to(target)
    return path


def _journal(tmp_path, cli):
    path = _empty(tmp_path, cli)
    simulate_journal(cli, path)
    return path


def _temp_only(tmp_path, cli):
    path = _empty(tmp_path, cli)
    (path / ".keysmith-abc123").write_text("partial", encoding="utf-8")
    return path


def _marker_only(tmp_path, cli):
    path = _empty(tmp_path, cli)
    cli._publish_cleanup_intent(path, TID, {}, "committed")
    return path


def _bad_journal(tmp_path, cli):
    path = _empty(tmp_path, cli)
    jdir = path / f"{JOURNAL_PREFIX}{TID}"
    jdir.mkdir()
    (jdir / "journal.json").write_text("{broken", encoding="utf-8")
    return path


STATES = {
    "missing": (_missing, "not-installed", 0),
    "not-a-directory": (_file, "conflict", 1),
    "empty": (_empty, "not-installed", 0),
    "handwritten-system": (_handwritten, "not-installed", 0),
    "aligned": (_aligned, "active-aligned", 0),
    "drift": (_drift, "drift", 0),
    "bad-manifest": (_bad_manifest, "conflict", 1),
    "system-symlink": (_system_symlink, "conflict", 1),
    "journal": (_journal, "recovery-required", 1),
    "temp-only": (_temp_only, "recovery-required", 1),
    "marker-only": (_marker_only, "recovery-required", 1),
    "bad-journal": (_bad_journal, "conflict", 1),
}


# ─── 1. status states ────────────────────────────────────────────────────────
@pytest.mark.parametrize("name", sorted(STATES))
def test_status_states(name, tmp_path, cli):
    build, state, exit_code = STATES[name]
    pi_dir = build(tmp_path, cli)
    env = status(pi_dir, expect=exit_code)
    st = env["status"]
    assert env["operation"] == "status"
    assert env["plan"] is None and env["result"] is None
    assert env["target"]["scope"] == "user"
    assert env["target"]["pi_dir"] == str(pi_dir)
    assert st["state"] == state
    assert st["recovery_required"] is (state == "recovery-required")
    assert st["deployable"] is (not st["blockers"]["deploy"])
    assert st["uninstall_ready"] is (not st["blockers"]["uninstall"])
    for node in st["nodes"].values():
        assert node["kind"] in ("missing", "regular", "directory", "symlink", "special")
    if name == "missing":
        assert st["deployable"] is False
        assert st["blockers"]["deploy"] == ["pi-dir-missing"]
        assert not pi_dir.exists()
    if name == "aligned":
        assert st["deployable"] and st["uninstall_ready"]
        assert st["manifest"]["system"]["after"]["sha256"] == st["nodes"]["system"]["fingerprint"]["sha256"]
    if name == "handwritten-system":
        assert st["deployable"] is True and st["manifest"] is None
    if name == "bad-manifest":
        assert "manifest-invalid" in st["conflicts"]
    if name == "bad-journal":
        assert "journal-invalid" in st["conflicts"]
    if name == "temp-only":
        assert st["residue"]["temp_files"] == [".keysmith-abc123"]
    if name == "marker-only":
        assert st["residue"]["cleanup_markers"] == [f".pi-keysmith-cleanup-{TID}.intent.json"]


def test_pi_dir_symlink_reports_directory(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    env = status(link)
    assert env["status"]["nodes"]["pi_dir"]["kind"] == "directory"
    assert env["target"]["pi_dir"] == str(link)
    assert env["target"]["pi_dir_resolved"] == os.path.realpath(str(real))


# ─── 2. preserved symlink ────────────────────────────────────────────────────
def test_agents_md_symlink_blocks_deploy_only(pi_agent, tmp_path):
    deploy(pi_agent)
    target = tmp_path / "dotfiles-agents.md"
    target.write_text("A\n", encoding="utf-8")
    (pi_agent / "AGENTS.md").symlink_to(target)
    st = status(pi_agent)["status"]
    assert st["state"] != "conflict"
    assert st["deployable"] is False
    assert "preserved-not-regular:agents_md" in st["blockers"]["deploy"]
    assert st["uninstall_ready"] is True
    env = plan_deploy(pi_agent, expect=1)
    assert env["plan"]["confirmation_token"] is None and env["plan"]["writes"] == []
    token = plan_op("uninstall", pi_agent)["plan"]["confirmation_token"]
    confirm_op("uninstall", pi_agent, token)
    assert (pi_agent / "AGENTS.md").is_symlink()


# ─── 3. drift ────────────────────────────────────────────────────────────────
def test_drift_blocks_uninstall_without_writes(tmp_path, cli):
    pi_dir = _drift(tmp_path, cli)
    st = status(pi_dir)["status"]
    assert st["uninstall_ready"] is False
    assert "system-fingerprint-mismatch" in st["blockers"]["uninstall"]
    before = snapshot(pi_dir)
    env = plan_op("uninstall", pi_dir, expect=1)
    assert "system-fingerprint-mismatch" in codes(env["diagnostics"])
    assert snapshot(pi_dir) == before


# ─── 4. deploy preview ───────────────────────────────────────────────────────
def test_deploy_preview_is_stable_and_writes_nothing(pi_agent, cli, tmp_path):
    before = snapshot(pi_agent)
    first = plan_deploy(pi_agent)
    assert snapshot(pi_agent) == before
    assert first["preview"] is True and first["apply"] is False and first["result"] is None
    plan = first["plan"]
    builtin_sha = hashlib.sha256(cli.BUILTIN_SYSTEM_PROMPT_MD.encode("utf-8")).hexdigest()
    assert plan["prompt"] == {"name": "system-prompt", "source": "builtin", "sha256": builtin_sha, "size": len(cli.BUILTIN_SYSTEM_PROMPT_MD.encode("utf-8"))}
    assert len(plan["confirmation_token"]) == 64 and int(plan["confirmation_token"], 16) >= 0
    assert plan["confirmation_token"] == plan["confirmation_token"].lower()
    assert plan["preserves"] == ["APPEND_SYSTEM.md", "AGENTS.md", "CLAUDE.md", "settings.json", "models.json"]
    assert [w["action"] for w in plan["writes"]] == ["create", "create"]
    assert plan_deploy(pi_agent)["plan"]["confirmation_token"] == plan["confirmation_token"]
    custom = tmp_path / "custom.md"
    custom.write_text("CUSTOM\n", encoding="utf-8")
    a = plan_deploy(pi_agent, extra=["--file", str(custom), "--name", "one"])["plan"]
    b = plan_deploy(pi_agent, extra=["--file", str(custom), "--name", "two"])["plan"]
    assert a["prompt"]["sha256"] == hashlib.sha256(b"CUSTOM\n").hexdigest()
    assert a["prompt"]["source"] == "file"
    assert a["confirmation_token"] == b["confirmation_token"] != plan["confirmation_token"]
    assert "CUSTOM" not in json.dumps(a)


# ─── 5. deploy confirm ───────────────────────────────────────────────────────
def test_deploy_confirm_writes_and_aligns(pi_agent):
    env = deploy(pi_agent)
    assert env["apply"] is True and env["preview"] is False
    result = env["result"]
    assert result["applied"] is True
    manifest = json.loads((pi_agent / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert result["transaction_id"] == manifest["deployment_id"]
    assert result["system_sha256"] == hashlib.sha256((pi_agent / SYSTEM_NAME).read_bytes()).hexdigest()
    assert result["manifest_sha256"] == hashlib.sha256((pi_agent / MANIFEST_NAME).read_bytes()).hexdigest()
    assert env["status"]["state"] == "active-aligned"
    assert status(pi_agent)["status"]["state"] == "active-aligned"


# ─── 6. token mismatch ───────────────────────────────────────────────────────
def test_token_mismatch_after_system_edit(pi_agent):
    (pi_agent / SYSTEM_NAME).write_text("ONE\n", encoding="utf-8")
    token = plan_deploy(pi_agent)["plan"]["confirmation_token"]
    (pi_agent / SYSTEM_NAME).write_text("TWO\n", encoding="utf-8")
    before = snapshot(pi_agent)
    env = confirm_deploy(pi_agent, token, expect=3)
    assert codes(env["diagnostics"]) == ["preview-token-mismatch"]
    assert env["result"] is None
    assert snapshot(pi_agent) == before


def test_token_mismatch_after_backup_tamper(pi_agent):
    (pi_agent / SYSTEM_NAME).write_text("ORIGINAL\n", encoding="utf-8")
    deploy(pi_agent)
    token = plan_op("uninstall", pi_agent)["plan"]["confirmation_token"]
    backup = next(p for p in pi_agent.iterdir() if p.name.startswith("SYSTEM.md.bak_"))
    backup.write_text("TAMPERED\n", encoding="utf-8")
    before = snapshot(pi_agent)
    confirm_op("uninstall", pi_agent, token, expect=3)
    assert snapshot(pi_agent) == before


def test_token_mismatch_after_journal_phase_advance(pi_agent, cli):
    simulate_journal(cli, pi_agent)
    token = plan_op("recover", pi_agent)["plan"]["confirmation_token"]
    jdir = pi_agent / f"{JOURNAL_PREFIX}{TID}"
    journal = cli._load_journal(jdir)
    cli._update_journals({str(pi_agent.resolve()): journal}, "prepared")
    before = snapshot(pi_agent)
    confirm_op("recover", pi_agent, token, expect=3)
    assert snapshot(pi_agent) == before


# ─── 7. usage errors ─────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "args, code",
    [
        (["--yes", "--json", "--pi-dir", "{abs}"], "preview-token-required"),
        (["--status", "--json", "--pi-dir", "relative/agent"], "pi-dir-not-absolute"),
        (["--status", "--yes", "--json", "--pi-dir", "{abs}"], "usage-error"),
        (["--status", "--json"], "pi-dir-required"),
        (["--dry-run", "--status", "--json", "--pi-dir", "{abs}"], "usage-error"),
        (["--status", "--json", "--pi-dir", "{abs}", "--expected-preview-token", "0" * 64], "preview-token-unexpected"),
        (["--dry-run", "--json", "--pi-dir", "{abs}", "--expected-preview-token", "0" * 64], "preview-token-unexpected"),
        (["--yes", "--json", "--pi-dir", "{abs}", "--expected-preview-token", "XYZ"], "preview-token-invalid"),
        (["--uninstall", "--json", "--pi-dir", "{abs}", "--name", "x"], "usage-error"),
    ],
)
def test_usage_errors_emit_envelope(args, code, pi_agent):
    args = [a.replace("{abs}", str(pi_agent)) for a in args]
    before = snapshot(pi_agent)
    env = _ok_json(args, 2)
    assert env["status"] is None
    assert codes(env["diagnostics"]) == [code]
    assert snapshot(pi_agent) == before


# ─── 8. uninstall ────────────────────────────────────────────────────────────
def test_uninstall_restores_and_archives(pi_agent):
    (pi_agent / SYSTEM_NAME).write_text("ORIGINAL\n", encoding="utf-8")
    (pi_agent / "APPEND_SYSTEM.md").write_text("PERSONA\n", encoding="utf-8")
    deploy(pi_agent)
    plan = plan_op("uninstall", pi_agent)["plan"]
    assert [w["action"] for w in plan["writes"]] == ["restore", "archive"]
    env = confirm_op("uninstall", pi_agent, plan["confirmation_token"])
    assert env["result"]["applied"] is True
    assert (pi_agent / SYSTEM_NAME).read_text(encoding="utf-8") == "ORIGINAL\n"
    assert not (pi_agent / MANIFEST_NAME).exists()
    assert any(p.name.startswith(f"{MANIFEST_NAME}.uninstalled_") for p in pi_agent.iterdir())
    assert (pi_agent / "APPEND_SYSTEM.md").read_bytes() == b"PERSONA\n"
    assert env["status"]["state"] == "not-installed"


def test_uninstall_removes_created_system(pi_agent):
    deploy(pi_agent)
    plan = plan_op("uninstall", pi_agent)["plan"]
    assert plan["writes"][0] == {"path": SYSTEM_NAME, "action": "remove"}
    confirm_op("uninstall", pi_agent, plan["confirmation_token"])
    assert not (pi_agent / SYSTEM_NAME).exists()


def test_uninstall_blocked_by_previous_manifest_backup_mismatch(pi_agent):
    deploy(pi_agent)
    deploy(pi_agent)
    backup = next(p for p in pi_agent.iterdir() if p.name.startswith(f"{MANIFEST_NAME}.bak_"))
    backup.write_text(backup.read_text(encoding="utf-8") + " ", encoding="utf-8")
    st = status(pi_agent)["status"]
    assert "manifest-backup-mismatch" in st["blockers"]["uninstall"]
    before = snapshot(pi_agent)
    env = plan_op("uninstall", pi_agent, expect=1)
    assert env["plan"]["confirmation_token"] is None
    assert snapshot(pi_agent) == before


def test_uninstall_preview_warns_on_preserved_change(pi_agent):
    (pi_agent / "AGENTS.md").write_text("A\n", encoding="utf-8")
    deploy(pi_agent)
    (pi_agent / "AGENTS.md").write_text("B\n", encoding="utf-8")
    env = plan_op("uninstall", pi_agent)
    assert env["status"]["preserved_changed"] == ["agents_md"]
    assert "preserved-changed" in codes(env["diagnostics"])


# ─── 9. recover ──────────────────────────────────────────────────────────────
def _kill_before_phase(phase: str) -> str:
    return (
        "import os\n"
        "_orig = cli._update_journals\n"
        "def _patched(journals, p, manifest_sha256=None):\n"
        f"    if p == {phase!r}:\n"
        "        os._exit(9)\n"
        "    return _orig(journals, p, manifest_sha256)\n"
        "cli._update_journals = _patched\n"
    )


KILL_AFTER_COMMIT = "import os\ncli._cleanup_journal = lambda *a, **k: os._exit(9)\n"


@pytest.mark.parametrize("phase", ["manifest-intent", "final-sweep"])
def test_recover_rolls_back_unfinished_deploy(pi_agent, phase):
    (pi_agent / SYSTEM_NAME).write_text("ORIGINAL\n", encoding="utf-8")
    token = plan_deploy(pi_agent)["plan"]["confirmation_token"]
    code, _, _ = _invoke(
        ["--yes", "--json", "--pi-dir", str(pi_agent), "--expected-preview-token", token],
        extra_code=_kill_before_phase(phase),
    )
    assert code == 9
    assert (pi_agent / SYSTEM_NAME).read_text(encoding="utf-8") != "ORIGINAL\n"
    assert status(pi_agent, expect=1)["status"]["state"] == "recovery-required"
    plan = plan_op("recover", pi_agent)["plan"]
    assert plan["writes"]
    env = confirm_op("recover", pi_agent, plan["confirmation_token"])
    assert (pi_agent / SYSTEM_NAME).read_text(encoding="utf-8") == "ORIGINAL\n"
    assert not (pi_agent / MANIFEST_NAME).exists()
    assert env["status"]["state"] == "not-installed"
    assert env["result"]["cleaned"] == [f"{JOURNAL_PREFIX}{env['result']['transaction_id']}"]


def test_recover_after_commit_only_cleans(pi_agent, cli):
    token = plan_deploy(pi_agent)["plan"]["confirmation_token"]
    code, _, _ = _invoke(
        ["--yes", "--json", "--pi-dir", str(pi_agent), "--expected-preview-token", token],
        extra_code=KILL_AFTER_COMMIT,
    )
    assert code == 9
    deployed = (pi_agent / SYSTEM_NAME).read_bytes()
    assert deployed == cli.BUILTIN_SYSTEM_PROMPT_MD.encode("utf-8")
    plan = plan_op("recover", pi_agent)["plan"]
    assert [w["action"] for w in plan["writes"]] == ["cleanup"]
    confirm_op("recover", pi_agent, plan["confirmation_token"])
    assert (pi_agent / SYSTEM_NAME).read_bytes() == deployed
    assert status(pi_agent)["status"]["state"] == "active-aligned"


def test_recover_after_uninstall_kill_restores_deployment(pi_agent):
    deploy(pi_agent)
    deployed = (pi_agent / SYSTEM_NAME).read_bytes()
    token = plan_op("uninstall", pi_agent)["plan"]["confirmation_token"]
    code, _, _ = _invoke(
        ["--uninstall", "--yes", "--json", "--pi-dir", str(pi_agent), "--expected-preview-token", token],
        extra_code=_kill_before_phase("final-sweep"),
    )
    assert code == 9
    assert not (pi_agent / SYSTEM_NAME).exists()
    plan = plan_op("recover", pi_agent)["plan"]
    confirm_op("recover", pi_agent, plan["confirmation_token"])
    assert (pi_agent / SYSTEM_NAME).read_bytes() == deployed
    assert status(pi_agent)["status"]["state"] == "active-aligned"


@pytest.mark.parametrize("build", [_temp_only, _marker_only])
def test_recover_cleans_orphans(build, tmp_path, cli):
    pi_dir = build(tmp_path, cli)
    plan = plan_op("recover", pi_dir)["plan"]
    assert plan["writes"]
    env = confirm_op("recover", pi_dir, plan["confirmation_token"])
    assert env["result"]["cleaned"]
    assert env["status"]["state"] == "not-installed"
    assert status(pi_dir)["status"]["state"] != "recovery-required"


def test_recover_noop(pi_agent):
    plan = plan_op("recover", pi_agent)["plan"]
    assert plan["writes"] == [] and plan["confirmation_token"]
    env = confirm_op("recover", pi_agent, plan["confirmation_token"])
    assert env["result"] == {
        "transaction_id": None,
        "applied": True,
        "system_sha256": None,
        "manifest_sha256": None,
        "cleaned": [],
    }


# ─── 10. invariants ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("name", sorted(STATES))
def test_state_invariants(name, tmp_path, cli):
    pi_dir = STATES[name][0](tmp_path, cli)
    st = status(pi_dir, expect=STATES[name][2])["status"]
    recover_plan = _invoke(["--recover", "--json", "--pi-dir", str(pi_dir)])[1]["plan"]
    assert (st["state"] == "recovery-required") == bool(recover_plan["writes"])
    if not st["deployable"]:
        token = cli._preview_token("deploy", cli._inspect(pi_dir), recover_plan and hashlib.sha256(cli.BUILTIN_SYSTEM_PROMPT_MD.encode("utf-8")).hexdigest())
        before = snapshot(pi_dir)
        confirm_deploy(pi_dir, token, expect=1)
        assert snapshot(pi_dir) == before
    if not st["uninstall_ready"]:
        token = cli._preview_token("uninstall", cli._inspect(pi_dir), None)
        before = snapshot(pi_dir)
        confirm_op("uninstall", pi_dir, token, expect=1)
        assert snapshot(pi_dir) == before


# ─── 11. lock ────────────────────────────────────────────────────────────────
@pytest.mark.skipif(os.name == "nt", reason="POSIX flock")
def test_lock_busy(pi_agent):
    import fcntl

    token = plan_deploy(pi_agent)["plan"]["confirmation_token"]
    fd = os.open(str(pi_agent / ".pi-keysmith.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        before = snapshot(pi_agent)
        env = confirm_deploy(pi_agent, token, expect=5)
        assert codes(env["diagnostics"]) == ["lock-busy"]
        assert snapshot(pi_agent) == before
    finally:
        os.close(fd)
    confirm_deploy(pi_agent, token)
    assert status(pi_agent)["status"]["residue"] == {"journals": [], "cleanup_markers": [], "temp_files": []}


# ─── 12. stdout hygiene and end-to-end flow ──────────────────────────────────
def test_full_flow_preserves_neighbors(pi_agent):
    (pi_agent / "APPEND_SYSTEM.md").write_text("PERSONA\n", encoding="utf-8")
    (pi_agent / "AGENTS.md").write_text("AGENTS\n", encoding="utf-8")
    preserved = {n: (pi_agent / n).read_bytes() for n in ("APPEND_SYSTEM.md", "AGENTS.md")}
    assert status(pi_agent)["status"]["state"] == "not-installed"
    deploy(pi_agent)
    assert status(pi_agent)["status"]["state"] == "active-aligned"
    token = plan_op("uninstall", pi_agent)["plan"]["confirmation_token"]
    confirm_op("uninstall", pi_agent, token)
    assert status(pi_agent)["status"]["state"] == "not-installed"
    assert {n: (pi_agent / n).read_bytes() for n in preserved} == preserved


def test_json_has_no_prompt_body(pi_agent, cli):
    deploy(pi_agent)
    code, env, err = _invoke(["--status", "--json", "--pi-dir", str(pi_agent)])
    raw = json.dumps(env)
    assert "You are Pi" not in raw
    assert err == ""


def test_human_mode_still_text(run, pi_agent):
    code, out, err = run(["--pi-dir", str(pi_agent), "--status", "--lang", "en"])
    assert code == 0
    assert not out.lstrip().startswith("{")
    assert "Deployability: ready" in out
