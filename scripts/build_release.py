#!/usr/bin/env python3
# ─── build_release.py — deterministic pi-keysmith release builder ─────────────
# Produces a deterministic zip, tar.gz, standalone .py, and SHA256SUMS for a
# given release tag. Candidate builds require --source-commit; formal builds
# require refs/tags/<tag> to point at HEAD. Refuses shallow clones and dirty
# trees. Never overwrites an existing asset unless it is byte-identical.
from __future__ import annotations

import argparse
import gzip
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from typing import List, Optional

TOOL_NAME = "pi-keysmith"
TAG_PATTERN = re.compile(r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
FULL_COMMIT_PATTERN = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
TAR_TIMESTAMP = 0
MIT_MARKERS = ("MIT License", "Permission is hereby granted", "WITHOUT WARRANTY OF ANY KIND")
ARCHIVE_FILES = [
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "README.md",
    "SECURITY.md",
    "VERSION",
    "pi-keysmith.py",
    "docs/hooks-transactions.md",
    "examples/system-prompt.md",
]
CLI_VERSION_PATTERN = re.compile(rb"^(?:__version__|VERSION)\s*=\s*['\"]([^'\"]+)['\"]\s*$", re.MULTILINE)


class ReleaseError(Exception):
    pass


def _run_git(args: List[str], cwd: Path, timeout: int = 30) -> str:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        result = subprocess.run(
            ["git"] + args, cwd=str(cwd), capture_output=True, text=True, env=env, timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        raise ReleaseError(f"git {' '.join(args)} timed out") from exc
    if result.returncode != 0:
        raise ReleaseError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def _require_complete_git_checkout(repo_root: Path) -> None:
    shallow = _run_git(["rev-parse", "--is-shallow-repository"], repo_root).strip()
    if shallow == "true":
        raise ReleaseError("refusing to build from a shallow clone")
    try:
        config = _run_git(["config", "--get", "extensions.partialClone"], repo_root).strip()
    except ReleaseError:
        config = ""
    if config:
        raise ReleaseError(f"refusing to build from a partial clone: {config}")
    missing = _run_git(
        ["rev-list", "--objects", "--missing=print", "--all"], repo_root, timeout=120
    )
    for line in missing.splitlines():
        if line.startswith("?"):
            raise ReleaseError(f"refusing to build: missing object {line}")


def _require_clean_repository(repo_root: Path, output_dir: Path) -> None:
    status = _run_git(["status", "--porcelain=v1", "--untracked-files=all"], repo_root).strip()
    if not status:
        return
    out_abs = output_dir.resolve()
    for line in status.splitlines():
        path = line[3:].strip()
        if not path:
            continue
        candidate = (repo_root / path).resolve()
        try:
            candidate.relative_to(out_abs)
            continue
        except ValueError:
            pass
        raise ReleaseError(f"refusing to build from a dirty tree: {path}")


def _validate_output_location(output_dir: Path, repo_root: Path) -> None:
    out_abs = output_dir.resolve()
    repo_abs = repo_root.resolve()
    if out_abs == repo_abs:
        raise ReleaseError("output dir must not be the repo root")
    if out_abs == repo_abs / ".git":
        raise ReleaseError("output dir must not be .git")
    # reject symlink ancestors
    cur = out_abs
    while cur != cur.parent:
        if cur.is_symlink():
            raise ReleaseError(f"output dir has a symlink ancestor: {cur}")
        cur = cur.parent


def _regular_file_bytes(path: Path) -> bytes:
    if os.path.islink(str(path)):
        raise ReleaseError(f"{path}: refusing to read a symlink")
    fd = -1
    try:
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        st = os.fstat(fd)
        if not (st.st_mode & 0o170000 == 0o100000):
            raise ReleaseError(f"{path}: not a regular file")
        data = b""
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            data += chunk
        if os.fstat(fd).st_mode & 0o170000 != 0o100000:
            raise ReleaseError(f"{path}: changed type during read")
        return data
    finally:
        if fd >= 0:
            os.close(fd)


def _read_and_validate_sources(repo_root: Path, version: str) -> dict:
    sources: dict = {}
    for rel in ARCHIVE_FILES:
        path = repo_root / rel
        if not path.is_file() or os.path.islink(str(path)):
            raise ReleaseError(f"{rel}: missing or not a regular file")
        sources[rel] = _regular_file_bytes(path)
    # VERSION file must equal tag[1:]
    version_text = sources["VERSION"].decode("utf-8").strip()
    if version_text != version:
        raise ReleaseError(f"VERSION file is {version_text!r}, expected {version!r}")
    # CLI declares the same version.
    cli = sources["pi-keysmith.py"]
    declared = {m.decode("utf-8") for m in CLI_VERSION_PATTERN.findall(cli)}
    if declared != {version}:
        raise ReleaseError(f"pi-keysmith.py declares versions {sorted(declared)}, expected only {version!r}")
    # CHANGELOG has a heading for the version.
    changelog = sources["CHANGELOG.md"].decode("utf-8", "replace")
    if not re.search(rf"^## \[?{re.escape(version)}\]?(?:\s|$)", changelog, re.MULTILINE):
        raise ReleaseError(f"CHANGELOG.md has no heading for version {version!r}")
    # LICENSE contains MIT markers.
    license_text = sources["LICENSE"].decode("utf-8", "replace")
    for marker in MIT_MARKERS:
        if marker not in license_text:
            raise ReleaseError(f"LICENSE missing marker: {marker!r}")
    return sources


def _validate_release_note(repo_root: Path, tag: str) -> None:
    note = repo_root / "docs" / "releases" / f"{tag}.md"
    if not note.is_file() or os.path.islink(str(note)):
        raise ReleaseError(f"docs/releases/{tag}.md: missing release note")


def _resolve_source_commit(repo_root: Path, tag: str, source_commit: Optional[str]) -> str:
    head = _run_git(["rev-parse", "--verify", "HEAD^{commit}"], repo_root).strip()
    if not FULL_COMMIT_PATTERN.fullmatch(head):
        raise ReleaseError(f"HEAD is not a full commit id: {head}")
    if source_commit is not None:
        if not FULL_COMMIT_PATTERN.fullmatch(source_commit):
            raise ReleaseError(f"--source-commit must be a full 40/64-hex id: {source_commit}")
        resolved = _run_git(["rev-parse", "--verify", f"{source_commit}^{{commit}}"], repo_root).strip()
        if resolved != source_commit:
            raise ReleaseError(f"--source-commit {source_commit} resolved to {resolved}")
        # If tag exists locally, it must point at the candidate.
        try:
            tag_commit = _run_git(["rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}"], repo_root).strip()
        except ReleaseError:
            tag_commit = ""
        if tag_commit and tag_commit != source_commit:
            raise ReleaseError(
                f"release tag {tag} already points to {tag_commit}, not candidate {source_commit}"
            )
        return source_commit
    # Formal build: tag must point at HEAD.
    tag_commit = _run_git(["rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}"], repo_root).strip()
    if tag_commit != head:
        raise ReleaseError(f"refs/tags/{tag} points to {tag_commit}, not HEAD {head}")
    return head


def _archive_member_name(tag: str, rel: str) -> str:
    return f"{TOOL_NAME}-{tag}/{rel}"


def _build_zip(sources: dict, tag: str, out_path: Path) -> None:
    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_STORED) as zf:
        for rel in sorted(sources):
            info = zipfile.ZipInfo(_archive_member_name(tag, rel), date_time=ZIP_TIMESTAMP)
            info.external_attr = (0o755 if rel == "pi-keysmith.py" else 0o644) << 16
            info.create_system = 3
            zf.writestr(info, sources[rel])


def _build_tar(sources: dict, tag: str, out_path: Path) -> None:
    with open(out_path, "wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=TAR_TIMESTAMP, compresslevel=9) as gz:
            with tarfile.open(fileobj=gz, mode="w", format=tarfile.USTAR_FORMAT) as tf:
                for rel in sorted(sources):
                    data = sources[rel]
                    info = tarfile.TarInfo(_archive_member_name(tag, rel))
                    info.size = len(data)
                    info.mtime = TAR_TIMESTAMP
                    info.mode = 0o755 if rel == "pi-keysmith.py" else 0o644
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    tf.addfile(info, io_bytesio(data))


def io_bytesio(data: bytes):
    import io
    return io.BytesIO(data)


def _build_standalone(sources: dict, out_path: Path) -> None:
    cli = sources["pi-keysmith.py"]
    license_text = sources["LICENSE"].decode("utf-8")
    lines = cli.splitlines(keepends=True)
    if not lines or not lines[0].startswith(b"#!"):
        raise ReleaseError("pi-keysmith.py missing shebang")
    shebang = lines[0]
    commented_license = "".join(f"# {ln}" if ln.strip() else "#\n" for ln in license_text.splitlines(keepends=True))
    body = b"".join(lines[1:]).decode("utf-8")
    standalone = shebang.decode("utf-8") + "\n# " + "-" * 76 + "\n" + commented_license + "# " + "-" * 76 + "\n\n" + body
    out_path.write_bytes(standalone.encode("utf-8"))
    os.chmod(str(out_path), 0o755)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _publish_asset(staged: Path, dest: Path) -> None:
    if dest.exists():
        if _sha256_file(dest) != _sha256_file(staged):
            raise ReleaseError(f"refuse to overwrite differing asset: {dest}")
        return
    try:
        os.replace(str(staged), str(dest))
    except OSError:
        shutil.copyfile(str(staged), str(dest))


def _parse_args(argv: List[str]) -> argparse.Namespace:
    here = Path(__file__).resolve()
    default_root = here.parents[1]
    parser = argparse.ArgumentParser(description=f"Deterministic release builder for {TOOL_NAME}")
    parser.add_argument("version", help="release tag, e.g. v0.1.0")
    parser.add_argument("--repo-root", type=Path, default=default_root, help=argparse.SUPPRESS)
    parser.add_argument("--output-dir", type=Path, default=Path("./dist"), help="asset output directory")
    parser.add_argument("--source-commit", default=None, help="full pre-tag candidate commit (candidate build)")
    return parser.parse_args(argv)


def build_release(version: str, repo_root: Path, output_dir: Path, source_commit: Optional[str]) -> int:
    if not TAG_PATTERN.fullmatch(version):
        raise ReleaseError(f"version must match {TAG_PATTERN.pattern}; got {version!r}")
    tag = version
    bare_version = version[1:]
    _require_complete_git_checkout(repo_root)
    _validate_output_location(output_dir, repo_root)
    _resolve_source_commit(repo_root, tag, source_commit)
    _require_clean_repository(repo_root, output_dir)
    _validate_release_note(repo_root, tag)
    sources = _read_and_validate_sources(repo_root, bare_version)

    output_dir.mkdir(parents=True, exist_ok=True)
    asset_names = {
        "zip": f"{TOOL_NAME}-{tag}.zip",
        "tar": f"{TOOL_NAME}-{tag}.tar.gz",
        "py": f"{TOOL_NAME}-{tag}.py",
        "sums": "SHA256SUMS",
    }
    staged_dir = output_dir / f".{TOOL_NAME}-release-staging"
    if staged_dir.exists():
        shutil.rmtree(staged_dir)
    staged_dir.mkdir(parents=True)

    try:
        _build_zip(sources, tag, staged_dir / asset_names["zip"])
        _build_tar(sources, tag, staged_dir / asset_names["tar"])
        _build_standalone(sources, staged_dir / asset_names["py"])
        # SHA256SUMS
        sums_lines = []
        for key in ("py", "tar", "zip"):
            name = asset_names[key]
            sha = _sha256_file(staged_dir / name)
            sums_lines.append(f"{sha}  {name}")
        sums_lines.sort()
        sums_content = "\n".join(sums_lines) + "\n"
        (staged_dir / asset_names["sums"]).write_text(sums_content, encoding="ascii")
        os.chmod(str(staged_dir / asset_names["sums"]), 0o644)
        # Publish without overwrite of differing assets.
        for name in asset_names.values():
            _publish_asset(staged_dir / name, output_dir / name)
    finally:
        shutil.rmtree(staged_dir, ignore_errors=True)

    # Re-validate after publish.
    _require_complete_git_checkout(repo_root)
    _require_clean_repository(repo_root, output_dir)
    for name in asset_names.values():
        if not (output_dir / name).is_file():
            raise ReleaseError(f"missing published asset: {name}")
    sums_sha = _sha256_file(output_dir / asset_names["sums"])
    print(sums_sha)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    try:
        return build_release(args.version, args.repo_root, args.output_dir, args.source_commit)
    except ReleaseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
