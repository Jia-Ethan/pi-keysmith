"""Deploy, manifest, layered uninstall, and recovery tests."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CLI_PATH = REPO_ROOT / "pi-keysmith.py"
MANIFEST_NAME = ".pi-keysmith-manifest.json"
SYSTEM_NAME = "SYSTEM.md"


def _bundled_prompt_sha() -> str:
    from conftest import load_cli_module  # type: ignore

    cli = load_cli_module()
    return hashlib.sha256(cli.BUILTIN_SYSTEM_PROMPT_MD.encode("utf-8")).hexdigest()


PROMPT_SHA = _bundled_prompt_sha()


def _deploy(pi_agent: Path, extra=None) -> tuple[int, str]:
    args = ["--pi-dir", str(pi_agent), "--yes", "--lang", "en"]
    if extra:
        args.extend(extra)
    proc = subprocess.run([sys.executable, str(CLI_PATH), *args], capture_output=True, text=True)
    return proc.returncode, proc.stdout + proc.stderr


def _status(pi_agent: Path) -> str:
    proc = subprocess.run(
        [sys.executable, str(CLI_PATH), "--pi-dir", str(pi_agent), "--status", "--lang", "en"],
        capture_output=True, text=True,
    )
    return proc.stdout + proc.stderr


def _uninstall(pi_agent: Path, yes=True) -> int:
    args = ["--pi-dir", str(pi_agent), "--uninstall", "--lang", "en"]
    if yes:
        args.append("--yes")
    proc = subprocess.run([sys.executable, str(CLI_PATH), *args], capture_output=True, text=True)
    return proc.returncode


def _recover(pi_agent: Path, yes=True) -> int:
    args = ["--pi-dir", str(pi_agent), "--recover", "--lang", "en"]
    if yes:
        args.append("--yes")
    proc = subprocess.run([sys.executable, str(CLI_PATH), *args], capture_output=True, text=True)
    return proc.returncode


def test_deploy_creates_system_md_and_manifest(pi_agent):
    code, out = _deploy(pi_agent)
    assert code == 0, out
    sysmd = pi_agent / SYSTEM_NAME
    man = pi_agent / MANIFEST_NAME
    assert sysmd.is_file() and not sysmd.is_symlink()
    assert man.is_file() and not man.is_symlink()
    # SYSTEM.md content equals the bundled prompt.
    from conftest import load_cli_module  # type: ignore
    cli = load_cli_module()
    assert sysmd.read_bytes() == cli.BUILTIN_SYSTEM_PROMPT_MD.encode("utf-8")
    # Manifest structure.
    m = json.loads(man.read_text(encoding="utf-8"))
    assert m["schema_version"] == 1
    assert m["tool_version"] == "0.2.0"
    assert m["system"]["path"] == "SYSTEM.md"
    assert m["system"]["before"] is None
    assert m["system"]["after"]["sha256"] == PROMPT_SHA
    assert m["system"]["backup"] is None
    assert m["append_system"]["preserved"] is False
    assert m["agents_md"]["preserved"] is False
    assert m["previous_manifest"]["before"] is None


def test_deploy_preserves_append_system_and_agents_md(pi_agent):
    (pi_agent / "APPEND_SYSTEM.md").write_text("PERSONA-CARD\n", encoding="utf-8")
    (pi_agent / "AGENTS.md").write_text("GLOBAL-INSTRUCTIONS\n", encoding="utf-8")
    persona_before = (pi_agent / "APPEND_SYSTEM.md").read_bytes()
    agents_before = (pi_agent / "AGENTS.md").read_bytes()
    code, out = _deploy(pi_agent)
    assert code == 0, out
    assert (pi_agent / "APPEND_SYSTEM.md").read_bytes() == persona_before
    assert (pi_agent / "AGENTS.md").read_bytes() == agents_before
    m = json.loads((pi_agent / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert m["append_system"]["preserved"] is True
    assert m["agents_md"]["preserved"] is True
    assert m["append_system"]["fingerprint"]["size"] == len(persona_before)
    assert m["agents_md"]["fingerprint"]["size"] == len(agents_before)


def test_status_after_deploy_is_ready(pi_agent):
    _deploy(pi_agent)
    st = _status(pi_agent)
    assert "Uninstall readiness: ready" in st
    assert "Deployability: ready" in st
    assert "Transaction residue: none" in st
    assert PROMPT_SHA in st


def test_redeploy_layers_backup(pi_agent):
    code, _ = _deploy(pi_agent)
    assert code == 0
    code, _ = _deploy(pi_agent)
    assert code == 0
    backups = [p for p in pi_agent.iterdir() if p.name.startswith("SYSTEM.md.bak_")]
    man_backups = [p for p in pi_agent.iterdir() if p.name.startswith(f"{MANIFEST_NAME}.bak_")]
    assert len(backups) == 1
    assert len(man_backups) == 1
    # Current manifest references the prior manifest before-state.
    m = json.loads((pi_agent / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert m["previous_manifest"]["before"] is not None
    assert m["system"]["before"] is not None
    assert m["system"]["backup"] is not None


def test_uninstall_removes_layer_and_restores_absent(pi_agent):
    _deploy(pi_agent)
    assert (pi_agent / SYSTEM_NAME).exists()
    assert _uninstall(pi_agent) == 0
    assert not (pi_agent / SYSTEM_NAME).exists()
    assert not (pi_agent / MANIFEST_NAME).exists()
    archives = [p for p in pi_agent.iterdir() if p.name.startswith(f"{MANIFEST_NAME}.uninstalled_")]
    assert len(archives) == 1


def test_uninstall_noop_without_manifest(pi_agent):
    assert _uninstall(pi_agent) == 0
    assert not (pi_agent / MANIFEST_NAME).exists()


def test_layered_uninstall_peels_back(pi_agent):
    _deploy(pi_agent)
    _deploy(pi_agent)
    # Two layers -> two SYSTEM.md backups.
    assert len([p for p in pi_agent.iterdir() if p.name.startswith("SYSTEM.md.bak_")]) == 1
    # First uninstall restores the first-deployed SYSTEM.md (content identical, since same prompt).
    assert _uninstall(pi_agent) == 0
    assert (pi_agent / SYSTEM_NAME).exists()
    assert (pi_agent / MANIFEST_NAME).exists()
    # Second uninstall removes the last layer.
    assert _uninstall(pi_agent) == 0
    assert not (pi_agent / SYSTEM_NAME).exists()
    assert not (pi_agent / MANIFEST_NAME).exists()


def test_uninstall_preview_writes_nothing(pi_agent):
    _deploy(pi_agent)
    proc = subprocess.run(
        [sys.executable, str(CLI_PATH), "--pi-dir", str(pi_agent), "--uninstall", "--lang", "en"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0
    assert (pi_agent / SYSTEM_NAME).exists()
    assert (pi_agent / MANIFEST_NAME).exists()


def test_custom_prompt_deploy(run, pi_agent, tmp_path):
    custom = tmp_path / "custom.md"
    custom.write_text("CUSTOM-PROMPT-CONTENT\n", encoding="utf-8")
    code, out, _ = run(["--pi-dir", str(pi_agent), "--yes", "--file", str(custom), "--name", "custom-rules", "--lang", "en"])
    assert code == 0
    # Deploy target is always SYSTEM.md, regardless of --name.
    assert (pi_agent / SYSTEM_NAME).read_text(encoding="utf-8") == "CUSTOM-PROMPT-CONTENT\n"
    m = json.loads((pi_agent / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert m["system"]["after"]["sha256"] == hashlib.sha256(b"CUSTOM-PROMPT-CONTENT\n").hexdigest()
