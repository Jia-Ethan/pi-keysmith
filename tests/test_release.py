"""Release-artifact build tests."""
from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
# Must match VERSION. The snapshot repository has no tags, so a candidate build
# works even after this tag has been published in the real repository.
TAG = "v0.2.0"


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True)


@pytest.fixture(scope="module")
def snapshot_repo(tmp_path_factory):
    """A throwaway repository holding the working tree as one commit, without tags.

    The builder needs a clean, complete checkout. Building from a snapshot keeps
    the developer's repository untouched (no staging commits) and lets the
    tests build a candidate even after the real tag has been published.
    """
    repo = tmp_path_factory.mktemp("release-snapshot") / "pi-keysmith"
    tracked = _git(["ls-files", "-z", "--cached", "--others", "--exclude-standard"], REPO_ROOT).stdout
    for rel in filter(None, tracked.split("\0")):
        src = REPO_ROOT / rel
        if not src.is_file():
            continue
        dest = repo / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
    _git(["init", "-q"], repo)
    _git(["add", "-A"], repo)
    _git(["-c", "user.name=test", "-c", "user.email=test@local", "commit", "-q", "-m", "snapshot"], repo)
    return repo


def _head(repo: Path) -> str:
    return _git(["rev-parse", "--verify", "HEAD^{commit}"], repo).stdout.strip()


def _build(repo: Path, tag: str, commit: str, out: Path):
    return subprocess.run(
        [sys.executable, str(repo / "scripts" / "build_release.py"), tag, "--source-commit", commit, "--output-dir", str(out)],
        capture_output=True,
        text=True,
        cwd=str(repo),
    )


def test_build_release_candidate_is_reproducible(snapshot_repo, tmp_path):
    head = _head(snapshot_repo)
    out1 = tmp_path / "d1"
    out2 = tmp_path / "d2"
    p1 = _build(snapshot_repo, TAG, head, out1)
    assert p1.returncode == 0, p1.stderr
    p2 = _build(snapshot_repo, TAG, head, out2)
    assert p2.returncode == 0, p2.stderr
    sums = (out1 / "SHA256SUMS").read_text(encoding="utf-8")
    expected_names = {f"pi-keysmith-{TAG}.zip", f"pi-keysmith-{TAG}.tar.gz", f"pi-keysmith-{TAG}.py"}
    lines = [ln for ln in sums.splitlines() if ln.strip()]
    names = {ln.split("  ", 1)[1] for ln in lines}
    assert names == expected_names
    for name in expected_names | {"SHA256SUMS"}:
        assert (out1 / name).read_bytes() == (out2 / name).read_bytes(), f"non-reproducible: {name}"
    standalone = (out1 / f"pi-keysmith-{TAG}.py").read_text(encoding="utf-8")
    assert standalone.startswith("#!/usr/bin/env python3")
    # The build must ship the current bundled prompt, not a superseded draft.
    assert "You are Pi, an interactive CLI coding agent." in standalone
    assert "You are a senior engineer and writer with twenty years in the field." not in standalone
    with zipfile.ZipFile(out1 / f"pi-keysmith-{TAG}.zip") as zf:
        members = zf.namelist()
        assert any(m.endswith("pi-keysmith.py") for m in members)
        assert any(m.endswith("examples/system-prompt.md") for m in members)
        assert any(m.endswith("docs/json-contract.md") for m in members)
        assert any(m.endswith("VERSION") for m in members)


def test_build_release_rejects_bad_tag(snapshot_repo, tmp_path):
    proc = _build(snapshot_repo, "not-a-tag", _head(snapshot_repo), tmp_path / "d")
    assert proc.returncode == 1


def test_build_release_rejects_bad_source_commit(snapshot_repo, tmp_path):
    proc = _build(snapshot_repo, TAG, "deadbeef", tmp_path / "d")
    assert proc.returncode == 1


def test_build_release_rejects_existing_tag_elsewhere(snapshot_repo, tmp_path):
    head = _head(snapshot_repo)
    _git(["tag", "v9.9.9", head], snapshot_repo)
    try:
        _git(["-c", "user.name=test", "-c", "user.email=test@local", "commit", "-q", "--allow-empty", "-m", "next"], snapshot_repo)
        proc = _build(snapshot_repo, "v9.9.9", _head(snapshot_repo), tmp_path / "d")
        assert proc.returncode == 1
        assert "already points to" in proc.stderr
    finally:
        _git(["reset", "-q", "--hard", head], snapshot_repo)
        _git(["tag", "-d", "v9.9.9"], snapshot_repo)
