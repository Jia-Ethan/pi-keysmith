"""Prompt parity, CLI surface, and exit-code tests."""
from __future__ import annotations

import hashlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CLI_PATH = REPO_ROOT / "pi-keysmith.py"
EXAMPLE_PROMPT = REPO_ROOT / "examples" / "system-prompt.md"


def test_embedded_prompt_byte_identical_to_example(cli):
    embedded = cli.BUILTIN_SYSTEM_PROMPT_MD.encode("utf-8")
    disk = EXAMPLE_PROMPT.read_bytes()
    assert embedded == disk
    assert hashlib.sha256(embedded).hexdigest() == "4b2a5833b928635df096c0dd06f05d4632bda949750fcf8872e7f94e6e727edb"


def test_version_matches_version_file(cli):
    assert cli.VERSION == "0.2.0"
    assert (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip() == "0.2.0"


def test_cli_version_flag(run):
    code, out, _ = run(["--version"])
    assert code == 0
    assert "pi-keysmith" in out and "0.2.0" in out


def test_cli_help_lists_operations(run):
    code, out, _ = run(["--help"])
    assert code == 0
    for flag in ("--dry-run", "--status", "--uninstall", "--recover", "--yes", "--pi-dir", "--lang"):
        assert flag in out


def test_mutually_exclusive_operations(run, pi_agent):
    code, out, err = run(["--pi-dir", str(pi_agent), "--dry-run", "--status"], expect_code=2)
    assert code == 2


def test_status_rejects_yes(run, pi_agent):
    code, out, err = run(["--pi-dir", str(pi_agent), "--status", "--yes"], expect_code=2)
    assert code == 2


def test_status_rejects_explicit_file(run, pi_agent, tmp_path):
    f = tmp_path / "p.md"
    f.write_text("x", encoding="utf-8")
    code, out, err = run(["--pi-dir", str(pi_agent), "--status", "--file", str(f)], expect_code=2)
    assert code == 2


def test_uninstall_rejects_file(run, pi_agent, tmp_path):
    f = tmp_path / "p.md"
    f.write_text("x", encoding="utf-8")
    code, out, err = run(["--pi-dir", str(pi_agent), "--uninstall", "--file", str(f)], expect_code=2)
    assert code == 2


def test_recover_rejects_file(run, pi_agent, tmp_path):
    f = tmp_path / "p.md"
    f.write_text("x", encoding="utf-8")
    code, out, err = run(["--pi-dir", str(pi_agent), "--recover", "--file", str(f)], expect_code=2)
    assert code == 2


def test_missing_pi_dir_errors(run, tmp_path):
    missing = tmp_path / "nope" / "agent"
    code, out, err = run(["--pi-dir", str(missing), "--status"], expect_code=1)
    assert code == 1


def test_dry_run_writes_nothing(run, pi_agent):
    code, out, _ = run(["--pi-dir", str(pi_agent), "--dry-run", "--lang", "en"])
    assert code == 0
    assert "SHA-256" in out
    assert "preview" in out.lower() or "Preview" in out
    assert not (pi_agent / "SYSTEM.md").exists()
    assert not (pi_agent / cli_manifest_name()).exists()


def cli_manifest_name() -> str:
    return ".pi-keysmith-manifest.json"
