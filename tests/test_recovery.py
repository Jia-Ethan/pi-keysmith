"""Durable journal / interrupted-transaction recovery tests."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CLI_PATH = REPO_ROOT / "pi-keysmith.py"
MANIFEST_NAME = ".pi-keysmith-manifest.json"
SYSTEM_NAME = "SYSTEM.md"
JOURNAL_PREFIX = ".pi-keysmith-transaction-"


def _run(args, **kw):
    return subprocess.run([sys.executable, str(CLI_PATH), *args], capture_output=True, text=True, **kw)


def _status(pi_agent: Path) -> str:
    return _run(["--pi-dir", str(pi_agent), "--status", "--lang", "en"]).stdout


def _simulate_interrupted_deploy(pi_agent: Path, original_content: str | None) -> str:
    """Create a journal + snapshot as if a deploy was SIGKILLed before publishing SYSTEM.md."""
    from conftest import load_cli_module  # type: ignore
    cli = load_cli_module()
    sys_before = cli._portable_fingerprint(pi_agent / SYSTEM_NAME) if original_content is not None else None
    man_before = cli._portable_fingerprint(pi_agent / MANIFEST_NAME)
    tid = "abcdef0123456789abcdef0123456789"
    cli._create_deployment_journals(
        [pi_agent],
        tid,
        {str(pi_agent.resolve()): sys_before},
        {str(pi_agent.resolve()): man_before},
        cli._prompt_sha256("NEW-CONTENT"),
    )
    return tid


def test_journal_residue_blocks_status_and_deployability(pi_agent):
    _simulate_interrupted_deploy(pi_agent, original_content=None)
    st = _status(pi_agent)
    assert "Transaction residue: 1 journal dir(s)" in st
    assert "Deployability: blocked" in st
    assert "Uninstall readiness: blocked" in st


def test_recover_preview_does_not_modify(pi_agent):
    _simulate_interrupted_deploy(pi_agent, original_content=None)
    proc = _run(["--pi-dir", str(pi_agent), "--recover", "--lang", "en"])
    assert proc.returncode == 0
    assert "Recover preview" in proc.stdout or "recover preview" in proc.stdout.lower()
    # Journal must still be present after a preview.
    assert any(p.name.startswith(JOURNAL_PREFIX) for p in pi_agent.iterdir())


def test_recover_yes_restores_and_cleans_journal(pi_agent):
    original = "ORIGINAL-SYSTEM\n"
    (pi_agent / SYSTEM_NAME).write_text(original, encoding="utf-8")
    _simulate_interrupted_deploy(pi_agent, original_content=original)
    proc = _run(["--pi-dir", str(pi_agent), "--recover", "--yes", "--lang", "en"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    # SYSTEM.md restored to original.
    assert (pi_agent / SYSTEM_NAME).read_text(encoding="utf-8") == original
    # Journal cleaned.
    assert not any(p.name.startswith(JOURNAL_PREFIX) for p in pi_agent.iterdir())
    # Status ready again.
    st = _status(pi_agent)
    assert "Deployability: ready" in st


def test_recover_when_system_was_created_removes_it(pi_agent):
    # No pre-existing SYSTEM.md; an interrupted deploy that created a journal but
    # never wrote SYSTEM.md leaves no SYSTEM.md. Recovery should keep it absent.
    _simulate_interrupted_deploy(pi_agent, original_content=None)
    assert not (pi_agent / SYSTEM_NAME).exists()
    proc = _run(["--pi-dir", str(pi_agent), "--recover", "--yes", "--lang", "en"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not (pi_agent / SYSTEM_NAME).exists()
    assert not any(p.name.startswith(JOURNAL_PREFIX) for p in pi_agent.iterdir())
