"""Release-artifact build tests."""
from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BUILDER = REPO_ROOT / "scripts" / "build_release.py"


def _git(args):
    return subprocess.run(["git", *args], cwd=str(REPO_ROOT), capture_output=True, text=True)


@pytest.fixture(autouse=True)
def _clean_tree_for_build():
    """Stage+commit any uncommitted work so the release builder (which requires a
    clean tree) can run from the test session."""
    status = _git(["status", "--porcelain"]).stdout.strip()
    if status:
        _git(["add", "-A"])
        _git(["-c", "user.name=test", "-c", "user.email=test@local", "commit", "-q", "-m", "test: stage working tree for build"])
    yield


def test_build_release_candidate_is_reproducible(tmp_path):
    head = _git(["rev-parse", "--verify", "HEAD^{commit}"]).stdout.strip()
    out1 = tmp_path / "d1"
    out2 = tmp_path / "d2"
    p1 = subprocess.run([sys.executable, str(BUILDER), "v0.1.0", "--source-commit", head, "--output-dir", str(out1)], capture_output=True, text=True)
    assert p1.returncode == 0, p1.stderr
    p2 = subprocess.run([sys.executable, str(BUILDER), "v0.1.0", "--source-commit", head, "--output-dir", str(out2)], capture_output=True, text=True)
    assert p2.returncode == 0, p2.stderr
    sums = (out1 / "SHA256SUMS").read_text(encoding="utf-8")
    expected_names = {"pi-keysmith-v0.1.0.zip", "pi-keysmith-v0.1.0.tar.gz", "pi-keysmith-v0.1.0.py"}
    lines = [ln for ln in sums.splitlines() if ln.strip()]
    names = {ln.split("  ", 1)[1] for ln in lines}
    assert names == expected_names
    for name in expected_names | {"SHA256SUMS"}:
        assert (out1 / name).read_bytes() == (out2 / name).read_bytes(), f"non-reproducible: {name}"
    standalone = (out1 / "pi-keysmith-v0.1.0.py").read_text(encoding="utf-8")
    assert standalone.startswith("#!/usr/bin/env python3")
    # The build must ship the current bundled prompt, not a superseded draft.
    assert "You are Pi, an interactive CLI coding agent." in standalone
    assert "You are a senior engineer and writer with twenty years in the field." not in standalone
    with zipfile.ZipFile(out1 / "pi-keysmith-v0.1.0.zip") as zf:
        members = zf.namelist()
        assert any(m.endswith("pi-keysmith.py") for m in members)
        assert any(m.endswith("examples/system-prompt.md") for m in members)
        assert any(m.endswith("VERSION") for m in members)


def test_build_release_rejects_bad_tag(tmp_path):
    head = _git(["rev-parse", "--verify", "HEAD^{commit}"]).stdout.strip()
    proc = subprocess.run([sys.executable, str(BUILDER), "not-a-tag", "--source-commit", head, "--output-dir", str(tmp_path / "d")], capture_output=True, text=True)
    assert proc.returncode == 1


def test_build_release_rejects_bad_source_commit(tmp_path):
    proc = subprocess.run([sys.executable, str(BUILDER), "v0.1.0", "--source-commit", "deadbeef", "--output-dir", str(tmp_path / "d")], capture_output=True, text=True)
    assert proc.returncode == 1
