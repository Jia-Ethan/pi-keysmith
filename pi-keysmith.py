#!/usr/bin/env python3
# ─── pi-keysmith ──────────────────────────────────────────────────────────────
# Versioned pi instruction deployment for the local pi agent configuration.
# Deploys the bundled or a custom Markdown instruction as the pi global
# `SYSTEM.md` (~/.pi/agent/SYSTEM.md), which pi auto-discovers and uses to
# replace its default system prompt. No pi configuration file is mutated; pi
# has no hooks. APPEND_SYSTEM.md and AGENTS.md are left untouched and continue
# to stack on top as pi's own loader dictates.
#
# Zero runtime dependencies: standard library only.
# License: MIT. See LICENSE.
# ─────────────────────────────────────────────────────────────────────────────
"""pi-keysmith: versioned pi instruction deployment with preview, ownership
manifests, and layered uninstall.

A confirmed deployment writes `<pi-dir>/SYSTEM.md` (creating it, or backing up
and replacing an existing regular file) and records an ownership manifest for
layered uninstall. Before the first mutation, deployment and uninstall publish
a durable transaction journal so an interruption such as SIGKILL can be
inspected and restored through explicit `--recover`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import locale
import os
import re
import stat
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ─── Version ──────────────────────────────────────────────────────────────────
__version__ = "0.1.0"
VERSION = __version__

# ─── Names and constants ─────────────────────────────────────────────────────
TOOL_NAME = "pi-keysmith"
SYSTEM_FILENAME = "SYSTEM.md"
APPEND_SYSTEM_FILENAME = "APPEND_SYSTEM.md"
AGENTS_FILENAME = "AGENTS.md"
CLAUDE_FILENAME = "CLAUDE.md"
MANIFEST_FILENAME = ".pi-keysmith-manifest.json"
MANIFEST_SCHEMA_VERSION = 1
JOURNAL_SCHEMA_VERSION = 1
JOURNAL_PREFIX = ".pi-keysmith-transaction-"
JOURNAL_FILENAME = "journal.json"
JOURNAL_PENDING_FILENAME = "journal.pending.json"
INTENT_FILENAME = "intent.json"
MANIFEST_INTENT_FILENAME = "manifest-intent.json"
MANIFEST_INTENT_PENDING_FILENAME = "manifest-intent.pending.json"
CLEANUP_MARKER_PREFIX = ".pi-keysmith-cleanup-"
CLEANUP_MARKER_SUFFIX = ".intent.json"
CLEANUP_CLAIM_SEPARATOR = ".cleanup-"
RESIDUE_TEMP_PREFIX = ".keysmith-"
DEFAULT_NAME = "system-prompt"
PI_CONFIG_DIRNAME = ".pi"
PI_AGENT_SUBDIR = "agent"

# Deploy resource labels.
RESOURCE_SYSTEM = "system"
RESOURCE_MANIFEST = "manifest"

# Deploy journal phases.
PHASE_INITIALIZING = "initializing"
PHASE_PREPARED = "prepared"
PHASE_SYSTEM_INTENT = "system-intent"
PHASE_MANIFEST_INTENT = "manifest-intent"
PHASE_FINAL_SWEEP = "final-sweep"
PHASE_COMMITTED = "committed"
PHASE_RECOVERING = "recovering"
PHASE_RECOVERED = "recovered"
DEPLOY_PHASES = (
    PHASE_INITIALIZING,
    PHASE_PREPARED,
    PHASE_SYSTEM_INTENT,
    PHASE_MANIFEST_INTENT,
    PHASE_FINAL_SWEEP,
    PHASE_COMMITTED,
    PHASE_RECOVERING,
    PHASE_RECOVERED,
)
DEPLOY_TERMINAL_PHASES = (PHASE_COMMITTED, PHASE_RECOVERED)

# Uninstall journal phases.
PHASE_U_INITIALIZING = "initializing"
PHASE_U_PREPARED = "prepared"
PHASE_U_SYSTEM_INTENT = "system-intent"
PHASE_U_MANIFEST_INTENT = "manifest-intent"
PHASE_U_FINAL_SWEEP = "final-sweep"
PHASE_U_COMMITTED = "committed"
PHASE_U_RECOVERING = "recovering"
PHASE_U_RECOVERED = "recovered"
UNINSTALL_PHASES = (
    PHASE_U_INITIALIZING,
    PHASE_U_PREPARED,
    PHASE_U_SYSTEM_INTENT,
    PHASE_U_MANIFEST_INTENT,
    PHASE_U_FINAL_SWEEP,
    PHASE_U_COMMITTED,
    PHASE_U_RECOVERING,
    PHASE_U_RECOVERED,
)
UNINSTALL_TERMINAL_PHASES = (PHASE_U_COMMITTED, PHASE_U_RECOVERED)

OPERATION_DEPLOY = "deploy"
OPERATION_UNINSTALL = "uninstall"

# ─── Bundled system prompt (byte-identical to examples/system-prompt.md)
BUILTIN_SYSTEM_PROMPT_MD = r"""You are Pi, an interactive CLI coding agent. You help the user complete
software engineering tasks in the current working directory: inspect the
tree, edit files, run commands, and return verifiable results.

## Tools

Use the tools available in this runtime:
- read: open text files
- bash: run commands and inspect runtime state
- edit: make targeted file changes
- write: create or replace files
- grep, find, ls: search and list

Treat tool output as the source of truth for local files and runtime state.
Prefer read-only inspection before edits.

## Operating rules

- Resolve discoverable facts from files, logs, commands, and current state
  before asking the user.
- When information is sufficient, act. Ask only for missing choices that
  require the user's judgment.
- Prefer simple, maintainable implementations that match surrounding code
  and existing platform capabilities.
- Preserve existing user work. Inspect dirty worktrees before editing.
- Confirm before destructive or irreversible actions: deleting files or
  branches, force-pushing, publishing, sending messages, rotating
  credentials, or changing shared systems.
- Never expose API keys, tokens, cookies, passwords, or private-key
  material in output, logs, documentation, or commits.
- Do not fabricate file contents, command results, citations, web
  findings, or runtime state.
- Validate work with the strongest practical tests, builds, logs, or
  direct inspection. Report what was and was not verified.

## Delivery

- Complete every explicit requirement. If you cannot complete one, say so
  and state what you did verify.
- Be concise and precise. Skip filler and customer-service phrasing.
- Show relevant file paths.
- Match the user's language.
- Distinguish verified facts, professional judgment, assumptions, and
  unresolved uncertainty.
- For non-trivial work, cover the primary workflow, important failure
  states, verification evidence, and rollback or maintenance impact.

## Environment

Pi may append additional context after this prompt, including
APPEND_SYSTEM.md, AGENTS.md, CLAUDE.md, skills, and session metadata.
Those blocks describe the environment and stacked instructions. Follow
them unless they conflict with a real operational limit in this session.
"""
# ─── i18n ─────────────────────────────────────────────────────────────────────
_OUTPUT_LANGUAGE = "zh-CN"


def _resolve_output_language(value: str) -> str:
    if value == "zh-CN" or value == "en":
        return value
    if value == "auto":
        for env in ("LC_ALL", "LC_MESSAGES", "LANG"):
            raw = os.environ.get(env, "")
            if raw:
                low = raw.lower()
                if low.startswith("zh"):
                    return "zh-CN"
                if low.startswith("en"):
                    return "en"
                return "zh-CN"
        try:
            sysloc = locale.getlocale()
        except Exception:
            sysloc = (None, None)
        if sysloc and sysloc[0]:
            low = sysloc[0].lower()
            if low.startswith("zh"):
                return "zh-CN"
            if low.startswith("english") or "english" in low:
                return "en"
        return "zh-CN"
    return "zh-CN"


def _set_output_language(value: str) -> None:
    global _OUTPUT_LANGUAGE
    _OUTPUT_LANGUAGE = _resolve_output_language(value)


def _configure_output_streams() -> None:
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


def _language_from_argv(argv: List[str]) -> str:
    lang = "auto"
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--lang":
            if i + 1 < len(argv):
                lang = argv[i + 1]
                break
        elif a.startswith("--lang="):
            lang = a.split("=", 1)[1]
            break
        i += 1
    return lang


_STRINGS: Dict[str, Dict[str, str]] = {
    "found_dirs": {
        "zh-CN": "[状态] 找到 {n} 个 pi 配置目录（只读检查）:",
        "en": "[Status] Found {n} pi configuration location(s) (read-only inspection):",
    },
    "status_dir": {
        "zh-CN": "── 状态目录: {dir} ──",
        "en": "── Status directory: {dir} ──",
    },
    "regular_file": {"zh-CN": "regular file", "en": "regular file"},
    "missing": {"zh-CN": "missing", "en": "missing"},
    "deploy_manifest": {
        "zh-CN": "部署清单",
        "en": "deployment manifest",
    },
    "transaction_residue": {"zh-CN": "事务残留", "en": "Transaction residue"},
    "none_word": {"zh-CN": "无", "en": "none"},
    "structural_health": {"zh-CN": "结构健康", "en": "Structural health"},
    "uninstall_readiness": {"zh-CN": "卸载就绪度", "en": "Uninstall readiness"},
    "deployability": {"zh-CN": "可部署性", "en": "Deployability"},
    "ready": {"zh-CN": "ready", "en": "ready"},
    "blocked": {"zh-CN": "blocked", "en": "blocked"},
    "healthy": {"zh-CN": "healthy", "en": "healthy"},
    "prompt_source_builtin": {
        "zh-CN": "[提示词] 来源: 内置 examples/system-prompt.md；SHA-256: {sha}",
        "en": "[Prompt] Source: built-in examples/system-prompt.md; SHA-256: {sha}",
    },
    "prompt_source_external": {
        "zh-CN": "[提示词] 来源: 外部文件 {path}；SHA-256: {sha}",
        "en": "[Prompt] Source: external file {path}; SHA-256: {sha}",
    },
    "behavior_notice_title": {
        "zh-CN": "[行为提示] {name} 会作为 pi 全局 SYSTEM.md 部署，影响所有加载该配置的新 pi 会话，并非项目级沙箱。",
        "en": "[Behavior notice] {name} deploys as the pi global SYSTEM.md and affects every new pi session loading this config; it is not a per-project sandbox.",
    },
    "behavior_notice_preserve": {
        "zh-CN": "APPEND_SYSTEM.md 与 AGENTS.md 保持不变并按 pi 原机制堆叠其上。部署前请先阅读内置提示词。",
        "en": "APPEND_SYSTEM.md and AGENTS.md are left unchanged and stack on top per pi's own loader. Read the bundled prompt before deploying.",
    },
    "preview_only": {
        "zh-CN": "[预览] 未提供 --yes，未写入任何文件。",
        "en": "[Preview] --yes not provided; no files written.",
    },
    "deployed": {
        "zh-CN": "[完成] 已部署到 {path}。请关闭旧任务并开启新 pi 会话验证。",
        "en": "[Done] Deployed to {path}. Close the old task and start a new pi session to verify.",
    },
    "no_pi_dirs": {
        "zh-CN": "[错误] 未找到 pi 配置目录。请用 --pi-dir 显式指定。",
        "en": "[Error] No pi configuration directory found. Specify one with --pi-dir.",
    },
    "windows_beta_warning": {
        "zh-CN": "[BETA 警告] Windows 上的 pi-keysmith 部署为 EXPLICIT_BETA，不构成正式支持。",
        "en": "[BETA warning] pi-keysmith deployment on Windows is EXPLICIT_BETA and is not a formal support claim.",
    },
    "recover_preview_only": {
        "zh-CN": "[恢复预览] 未提供 --yes，未修改任何文件。保留全部 journal/快照/证据。",
        "en": "[Recover preview] --yes not provided; no files changed. All journals/snapshots/evidence preserved.",
    },
    "recovered": {
        "zh-CN": "[恢复完成] 事务 {tid} 已恢复到部署前状态。",
        "en": "[Recovered] Transaction {tid} restored to pre-operation state.",
    },
    "uninstall_preview_only": {
        "zh-CN": "[卸载预览] 未提供 --yes，未修改任何文件。",
        "en": "[Uninstall preview] --yes not provided; no files changed.",
    },
    "uninstalled": {
        "zh-CN": "[卸载完成] 已撤销最新一层受管理部署。",
        "en": "[Uninstalled] Removed the newest managed deployment layer.",
    },
    "no_manifest_noop": {
        "zh-CN": "[卸载] 未找到 manifest，无操作。",
        "en": "[Uninstall] No manifest found; no-op.",
    },
}


def _localized(zh_cn: str, english: str) -> str:
    return zh_cn if _OUTPUT_LANGUAGE == "zh-CN" else english


def _tr(value: str) -> str:
    table = _STRINGS.get(value)
    if not table:
        return value
    return table.get(_OUTPUT_LANGUAGE, table["en"])


def _print(*values, **kwargs) -> None:
    kwargs.setdefault("file", sys.stdout)
    print(*values, **kwargs)


def _print_windows_fresh_deployment_beta_warning() -> None:
    if _is_windows_platform():
        _print(_tr("windows_beta_warning"))


# ─── Platform helpers ─────────────────────────────────────────────────────────
def _is_windows_platform() -> bool:
    return os.name == "nt" or sys.platform.startswith("win")


# ─── Filesystem backend ──────────────────────────────────────────────────────
# Centralized, no-follow, exclusive-create primitives. POSIX path uses O_NOFOLLOW
# and atomic same-volume no-replace renames; Windows uses a protected-ACL fallback
# treated as EXPLICIT_BETA. Symlinks and non-regular nodes fail closed.


class FilesystemError(OSError):
    pass


class AtomicRenameUnavailable(OSError):
    pass


class IntegrityConflict(OSError):
    pass


def _is_regular_path(path: Path) -> bool:
    try:
        if os.path.islink(path):
            return False
        st = os.lstat(path)
        return stat.S_ISREG(st.st_mode)
    except OSError:
        return False


def _identity_from_stat(file_stat: os.stat_result) -> Dict[str, int]:
    return {"device": int(file_stat.st_dev), "inode": int(file_stat.st_ino)}


def _fingerprint_descriptor(file_stat: os.stat_result, sha256: str) -> Dict[str, Any]:
    return {
        "size": int(file_stat.st_size),
        "mtime_ns": int(file_stat.st_mtime_ns),
        "sha256": sha256,
    }


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_regular_bytes_with_fingerprint(path: Path) -> Tuple[bytes, Dict[str, Any]]:
    fd = -1
    try:
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise IntegrityConflict(f"{path}: not a regular file")
        chunks: List[bytes] = []
        sha = hashlib.sha256()
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            chunks.append(chunk)
            sha.update(chunk)
        data = b"".join(chunks)
        return data, _fingerprint_descriptor(st, sha.hexdigest())
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass


def _fingerprint_regular_file(path: Path) -> Dict[str, Any]:
    data, fp = _read_regular_bytes_with_fingerprint(path)
    return fp


def _fingerprint_or_none(path: Path) -> Optional[Dict[str, Any]]:
    if not _is_regular_path(path):
        return None
    return _fingerprint_regular_file(path)


def _path_has_fingerprint(path: Path, fingerprint: Optional[Dict[str, Any]]) -> bool:
    if fingerprint is None:
        return not path.exists() and not os.path.islink(path)
    if not _is_regular_path(path):
        return False
    current = _fingerprint_regular_file(path)
    return (
        current["size"] == fingerprint["size"]
        and current["sha256"] == fingerprint["sha256"]
    )


def _portable_fingerprint(path: Path) -> Optional[Dict[str, Any]]:
    fp = _fingerprint_or_none(path)
    if fp is None:
        return None
    return {"size": fp["size"], "mtime_ns": fp["mtime_ns"], "sha256": fp["sha256"]}


def _portable_identity(path: Path) -> Optional[Dict[str, int]]:
    try:
        if os.path.islink(path) or not path.exists():
            return None
        st = os.stat(path)
        if not stat.S_ISREG(st.st_mode):
            return None
        return _identity_from_stat(st)
    except OSError:
        return None


def _validate_portable_fingerprint(value: Any, label: str) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise IntegrityConflict(f"{label}: fingerprint must be an object or null")
    expected = {"size", "mtime_ns", "sha256"}
    if set(value.keys()) != expected:
        raise IntegrityConflict(f"{label}: fingerprint keys must be {expected}")
    if not isinstance(value["size"], int) or value["size"] < 0:
        raise IntegrityConflict(f"{label}: size must be a non-negative int")
    if not isinstance(value["mtime_ns"], int) or value["mtime_ns"] < 0:
        raise IntegrityConflict(f"{label}: mtime_ns must be a non-negative int")
    if not isinstance(value["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["sha256"]):
        raise IntegrityConflict(f"{label}: sha256 must be 64 lowercase hex chars")
    return value


def _fsync_fd(fd: int) -> None:
    try:
        os.fsync(fd)
    except OSError:
        pass


def _fsync_directory(path: Path) -> None:
    if _is_windows_platform():
        return
    fd = -1
    try:
        fd = os.open(str(path), os.O_RDONLY)
        os.fsync(fd)
    except OSError:
        pass
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass


class _PosixFilesystem:
    """No-follow, exclusive-create, fsync-backed filesystem primitives."""

    def create_private_directory(self, path: Path) -> None:
        try:
            os.makedirs(str(path), exist_ok=False)
        except FileExistsError as exc:
            raise IntegrityConflict(f"{path}: directory already exists") from exc
        if not _is_windows_platform():
            try:
                os.chmod(str(path), 0o700)
            except OSError as exc:
                raise FilesystemError(f"{path}: chmod 0700 failed: {exc}") from exc

    def open_exclusive_private_file(self, path: Path) -> int:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        return os.open(str(path), flags, 0o600)

    def write_private_text(self, path: Path, content: str) -> None:
        data = content.encode("utf-8")
        fd = -1
        try:
            fd = self.open_exclusive_private_file(path)
            os.write(fd, data)
            _fsync_fd(fd)
        finally:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        if not _is_windows_platform():
            try:
                os.chmod(str(path), 0o600)
            except OSError:
                pass

    def atomic_write_text(self, path: Path, content: str, expected_fingerprint: Optional[Dict[str, Any]] = None) -> None:
        data = content.encode("utf-8")
        parent = path.parent
        fd = -1
        tmp_path: Optional[Path] = None
        try:
            fd, tmp_str = tempfile.mkstemp(prefix=RESIDUE_TEMP_PREFIX, dir=str(parent), text=False)
            tmp_path = Path(tmp_str)
            os.write(fd, data)
            _fsync_fd(fd)
            os.close(fd)
            fd = -1
            try:
                os.chmod(str(tmp_path), 0o600)
            except OSError:
                pass
            self._atomic_replace(tmp_path, path, expected_fingerprint)
            _fsync_directory(parent)
        finally:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if tmp_path is not None and tmp_path.exists():
                try:
                    os.unlink(str(tmp_path))
                except OSError:
                    pass

    def _atomic_replace(self, source: Path, destination: Path, expected_fingerprint: Optional[Dict[str, Any]]) -> None:
        if destination.is_symlink() or (destination.exists() and not _is_regular_path(destination)):
            raise IntegrityConflict(f"{destination}: not a regular file; refuse to replace")
        if expected_fingerprint is not None:
            if not _path_has_fingerprint(destination, expected_fingerprint):
                raise IntegrityConflict(f"{destination}: current content does not match expected fingerprint; concurrent drift refused")
        os.replace(str(source), str(destination))

    def copy_to_backup(self, source: Path, dest: Path) -> None:
        """Copy a regular file's bytes to `dest` (exclusive, no-follow)."""
        if not _is_regular_path(source):
            raise IntegrityConflict(f"{source}: source must be a regular file")
        data, _ = _read_regular_bytes_with_fingerprint(source)
        fd = -1
        try:
            fd = self.open_exclusive_private_file(dest)
            os.write(fd, data)
            _fsync_fd(fd)
            os.close(fd)
            fd = -1
            if not _is_windows_platform():
                try:
                    st = os.stat(str(source))
                    os.chmod(str(dest), stat.S_IMODE(st.st_mode))
                except OSError:
                    try:
                        os.chmod(str(dest), 0o600)
                    except OSError:
                        pass
            _fsync_directory(dest.parent)
        finally:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def atomic_move(self, source: Path, dest: Path, expected_fingerprint: Optional[Dict[str, Any]] = None) -> None:
        """Atomically move `source` onto `dest`. Destination must be absent or
        match `expected_fingerprint` (which may be None for 'must be absent')."""
        if dest.is_symlink() or (dest.exists() and not _is_regular_path(dest)):
            raise IntegrityConflict(f"{dest}: not a regular file; refuse to replace")
        if not _is_regular_path(source):
            raise IntegrityConflict(f"{source}: source must be a regular file")
        if expected_fingerprint is None:
            if dest.exists():
                raise IntegrityConflict(f"{dest}: destination exists; no-replace move refused")
        else:
            if not _path_has_fingerprint(dest, expected_fingerprint):
                raise IntegrityConflict(f"{dest}: current content does not match expected; concurrent drift refused")
        os.replace(str(source), str(dest))
        _fsync_directory(dest.parent)

    def remove_owned_file(self, path: Path, expected: Dict[str, Any]) -> None:
        if not _path_has_fingerprint(path, expected):
            raise IntegrityConflict(f"{path}: current content does not match expected; refuse to remove")
        os.unlink(str(path))
        _fsync_directory(path.parent)

    def replace_owned_from_backup(self, backup: Path, dest: Path, expected_before: Optional[Dict[str, Any]]) -> None:
        """Restore `backup` onto `dest`. `dest` must currently be absent (expected_before None)
        or match expected_before (we accept either the before-state or already-restored state)."""
        if not _is_regular_path(backup):
            raise IntegrityConflict(f"{backup}: backup must be a regular file")
        if dest.is_symlink() or (dest.exists() and not _is_regular_path(dest)):
            raise IntegrityConflict(f"{dest}: not a regular file; refuse to restore over")
        if expected_before is not None:
            if not (_path_has_fingerprint(dest, expected_before) or _path_has_fingerprint(dest, None)):
                # allow already-restored (matches backup content)
                backup_fp = _fingerprint_regular_file(backup)
                if not _path_has_fingerprint(dest, backup_fp):
                    raise IntegrityConflict(f"{dest}: current content is neither before-state nor absent")
        elif dest.exists():
            raise IntegrityConflict(f"{dest}: destination exists; no-replace restore refused")
        data, _ = _read_regular_bytes_with_fingerprint(backup)
        fd = -1
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(str(dest), flags, 0o600)
            os.write(fd, data)
            _fsync_fd(fd)
        finally:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        if not _is_windows_platform():
            try:
                os.chmod(str(dest), 0o600)
            except OSError:
                pass
        _fsync_directory(dest.parent)

    def write_snapshot(self, source: Path, dest: Path) -> None:
        """Copy `source` bytes into a snapshot file `dest` (exclusive, no-follow)."""
        self.copy_to_backup(source, dest)

    def safe_remove_owned_directory(self, path: Path, expected_members: Dict[str, Optional[Dict[str, Any]]]) -> None:
        """Remove a private directory after verifying its member set and fingerprints."""
        try:
            entries = sorted(os.listdir(str(path)))
        except OSError as exc:
            raise IntegrityConflict(f"{path}: cannot enumerate for cleanup: {exc}") from exc
        expected_names = sorted(expected_members.keys())
        if entries != expected_names:
            raise IntegrityConflict(f"{path}: member set {entries} != expected {expected_names}")
        for name, fp in expected_members.items():
            member = path / name
            if not _path_has_fingerprint(member, fp):
                raise IntegrityConflict(f"{member}: fingerprint mismatch during cleanup")
        for name in expected_names:
            try:
                os.unlink(str(path / name))
            except OSError as exc:
                raise IntegrityConflict(f"{path / name}: cannot remove during cleanup: {exc}") from exc
        try:
            os.rmdir(str(path))
        except OSError as exc:
            raise IntegrityConflict(f"{path}: cannot remove directory: {exc}") from exc
        _fsync_directory(path.parent)


_FILESYSTEM = _PosixFilesystem()


# ─── pi directory discovery ───────────────────────────────────────────────────
def _default_pi_agent_dir() -> Path:
    home = os.path.expanduser("~")
    return Path(home) / PI_CONFIG_DIRNAME / PI_AGENT_SUBDIR


def _resolve_pi_dir(explicit: Optional[str]) -> Optional[Path]:
    if explicit:
        candidate = Path(explicit).expanduser()
        return candidate
    default = _default_pi_agent_dir()
    if default.is_dir():
        return default
    return None


def find_pi_dirs() -> List[str]:
    resolved = _resolve_pi_dir(None)
    return [str(resolved)] if resolved is not None else []


def _classify_node(path: Path) -> str:
    if os.path.islink(path):
        return "symlink"
    if not path.exists():
        return "missing"
    st = os.lstat(path)
    if stat.S_ISREG(st.st_mode):
        return "regular"
    if stat.S_ISDIR(st.st_mode):
        return "directory"
    return "special"


def _node_desc(label: str, path: Path) -> str:
    kind = _classify_node(path)
    if kind in ("regular", "directory"):
        return f"{label}: {kind} ({path})"
    return f"{label}: {kind} ({path})"


# ─── Manifest validation and loading ─────────────────────────────────────────
_TRANSACTION_ID_RE = re.compile(r"[0-9a-f]{32}")
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def normalize_name(name: str) -> str:
    if not name or not _NAME_RE.fullmatch(name):
        raise IntegrityConflict(f"invalid name: {name!r}")
    if "/" in name or "\\" in name or name in {".", ".."}:
        raise IntegrityConflict(f"unsafe name: {name!r}")
    return name


def _require_manifest_object(value: Any, keys: set, label: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise IntegrityConflict(f"{label}: must be an object")
    if set(value.keys()) != keys:
        raise IntegrityConflict(f"{label}: keys must be {sorted(keys)}, got {sorted(value.keys())}")
    return value


def _validate_manifest(data: Any) -> Dict[str, Any]:
    if not isinstance(data, dict):
        raise IntegrityConflict("manifest: must be an object")
    required = {
        "schema_version",
        "tool_version",
        "deployment_id",
        "created_at",
        "system",
        "append_system",
        "agents_md",
        "previous_manifest",
    }
    if set(data.keys()) != required:
        raise IntegrityConflict(f"manifest: keys must be {sorted(required)}, got {sorted(data.keys())}")
    if data["schema_version"] != MANIFEST_SCHEMA_VERSION:
        raise IntegrityConflict(f"manifest: schema_version must be {MANIFEST_SCHEMA_VERSION}")
    if not isinstance(data["tool_version"], str) or not data["tool_version"]:
        raise IntegrityConflict("manifest: tool_version must be a non-empty string")
    if not isinstance(data["deployment_id"], str) or not _TRANSACTION_ID_RE.fullmatch(data["deployment_id"]):
        raise IntegrityConflict("manifest: deployment_id must be 32 lowercase hex chars")
    if not isinstance(data["created_at"], str) or not data["created_at"]:
        raise IntegrityConflict("manifest: created_at must be a non-empty string")

    system = _require_manifest_object(data["system"], {"path", "before", "after", "backup"}, "manifest.system")
    if system["path"] != SYSTEM_FILENAME:
        raise IntegrityConflict(f"manifest.system: path must be {SYSTEM_FILENAME!r}")
    system["before"] = _validate_portable_fingerprint(system["before"], "manifest.system.before")
    system["after"] = _validate_portable_fingerprint(system["after"], "manifest.system.after")
    if system["after"] is None:
        raise IntegrityConflict("manifest.system: after must not be null")
    if (system["before"] is None) != (system["backup"] is None):
        raise IntegrityConflict("manifest.system: before and backup must agree in nullness")
    if system["backup"] is not None:
        if not isinstance(system["backup"], str) or not system["backup"].startswith(f"{SYSTEM_FILENAME}.bak_"):
            raise IntegrityConflict(f"manifest.system: backup must start with {SYSTEM_FILENAME}.bak_")

    for block_key, block_label in (("append_system", "manifest.append_system"), ("agents_md", "manifest.agents_md")):
        block = _require_manifest_object(data[block_key], {"path", "preserved", "fingerprint"}, block_label)
        if not isinstance(block["path"], str) or not block["path"]:
            raise IntegrityConflict(f"{block_label}: path must be a non-empty string")
        if not isinstance(block["preserved"], bool):
            raise IntegrityConflict(f"{block_label}: preserved must be a bool")
        block["fingerprint"] = _validate_portable_fingerprint(block["fingerprint"], f"{block_label}.fingerprint")

    prev = _require_manifest_object(data["previous_manifest"], {"before", "backup"}, "manifest.previous_manifest")
    prev["before"] = _validate_portable_fingerprint(prev["before"], "manifest.previous_manifest.before")
    if prev["backup"] is not None:
        if not isinstance(prev["backup"], str) or not prev["backup"].startswith(f"{MANIFEST_FILENAME}.bak_"):
            raise IntegrityConflict(f"manifest.previous_manifest: backup must start with {MANIFEST_FILENAME}.bak_")
    if (prev["before"] is None) != (prev["backup"] is None):
        raise IntegrityConflict("manifest.previous_manifest: before and backup must agree in nullness")
    return data


def _load_manifest(path: Path) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    data, _ = _read_regular_bytes_with_fingerprint(path)
    try:
        manifest = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntegrityConflict(f"{path}: manifest is not valid JSON: {exc}") from exc
    return _validate_manifest(manifest), _portable_fingerprint(path)


# ─── Transaction journal ─────────────────────────────────────────────────────
def _new_transaction_id() -> str:
    return os.urandom(16).hex()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _journal_dir(pi_dir: Path, transaction_id: str) -> Path:
    return pi_dir / f"{JOURNAL_PREFIX}{transaction_id}"


def _journal_dirs(pi_dir: Path) -> List[Path]:
    if not pi_dir.is_dir():
        return []
    result: List[Path] = []
    for entry in sorted(os.listdir(str(pi_dir))):
        if entry.startswith(JOURNAL_PREFIX):
            p = pi_dir / entry
            if p.is_dir() and not p.is_symlink():
                result.append(p)
    return result


def _cleanup_claim_dirs(pi_dir: Path) -> List[Path]:
    if not pi_dir.is_dir():
        return []
    result: List[Path] = []
    for entry in sorted(os.listdir(str(pi_dir))):
        if entry.startswith(CLEANUP_MARKER_PREFIX) and entry.endswith(CLEANUP_MARKER_SUFFIX):
            result.append(pi_dir / entry)
    return result


def _keysmith_temp_dirs(pi_dir: Path) -> List[Path]:
    if not pi_dir.is_dir():
        return []
    result: List[Path] = []
    for entry in sorted(os.listdir(str(pi_dir))):
        if entry.startswith(RESIDUE_TEMP_PREFIX):
            result.append(pi_dir / entry)
    return result


def _residue_authorization_digest(transaction_id: str, directory: str, residue: Dict[str, Any]) -> str:
    payload = json.dumps(
        {"transaction_id": transaction_id, "directory": directory, "name": residue["name"], "members": residue["members"]},
        sort_keys=True,
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _journal_resource(
    path: str,
    before: Optional[Dict[str, Any]],
    snapshot: Optional[str],
    allowed_absent: bool,
    allowed_sha256: List[str],
    allowed_portable: List[Optional[Dict[str, Any]]],
) -> Dict[str, Any]:
    return {
        "path": path,
        "before": before,
        "snapshot": snapshot,
        "allowed_absent": allowed_absent,
        "allowed_sha256": sorted(set(allowed_sha256)),
        "allowed_portable": allowed_portable,
    }


def _immutable_journal_intent(data: Dict[str, Any]) -> Dict[str, Any]:
    intent = json.loads(json.dumps(data, sort_keys=True))
    if intent.get("operation") == OPERATION_DEPLOY:
        dirs = intent.get("directories", {})
        for d in dirs.values():
            res = d.get("resources", {})
            if RESOURCE_MANIFEST in res:
                res[RESOURCE_MANIFEST]["allowed_sha256"] = []
    return intent


def _publish_immutable_intent(journal_dir: Path, intent: Dict[str, Any]) -> None:
    _FILESYSTEM.write_private_text(journal_dir / INTENT_FILENAME, json.dumps(intent, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _publish_journal(journal_dir: Path, journal: Dict[str, Any]) -> None:
    persisted = dict(journal)
    persisted["owner_directory"] = str(journal_dir.parent.resolve())
    _FILESYSTEM.atomic_write_text(journal_dir / JOURNAL_FILENAME, json.dumps(persisted, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _publish_journal_pending(journal_dir: Path, journal: Dict[str, Any]) -> None:
    persisted = dict(journal)
    persisted["owner_directory"] = str(journal_dir.parent.resolve())
    pending_path = journal_dir / JOURNAL_PENDING_FILENAME
    # Pending is rewritten on each phase transition; remove any stale copy first
    # so the exclusive create stays valid across re-entrant phase updates.
    if _is_regular_path(pending_path):
        os.unlink(str(pending_path))
    _FILESYSTEM.write_private_text(pending_path, json.dumps(persisted, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _reconcile_pending(journal_dir: Path) -> None:
    pending = journal_dir / JOURNAL_PENDING_FILENAME
    journal = journal_dir / JOURNAL_FILENAME
    if pending.exists() and _is_regular_path(pending):
        if not journal.exists() or not _is_regular_path(journal):
            data, _ = _read_regular_bytes_with_fingerprint(pending)
            _FILESYSTEM.atomic_write_text(journal, data.decode("utf-8"))
        try:
            os.unlink(str(pending))
        except OSError:
            pass


def _load_journal_file(path: Path) -> Dict[str, Any]:
    data, _ = _read_regular_bytes_with_fingerprint(path)
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntegrityConflict(f"{path}: journal is not valid JSON: {exc}") from exc


def _load_journal(journal_dir: Path) -> Dict[str, Any]:
    pending = journal_dir / JOURNAL_PENDING_FILENAME
    journal = journal_dir / JOURNAL_FILENAME
    source: Optional[Path] = None
    if pending.exists() and _is_regular_path(pending):
        source = pending
    if journal.exists() and _is_regular_path(journal):
        source = journal
    if source is None:
        raise IntegrityConflict(f"{journal_dir}: no journal.json or journal.pending.json")
    data = _load_journal_file(source)
    _validate_journal(data, journal_dir)
    return data


def _validate_journal(data: Any, journal_dir: Path) -> None:
    if not isinstance(data, dict):
        raise IntegrityConflict("journal: must be an object")
    if data.get("schema_version") != JOURNAL_SCHEMA_VERSION:
        raise IntegrityConflict(f"journal: schema_version must be {JOURNAL_SCHEMA_VERSION}")
    operation = data.get("operation")
    if operation not in (OPERATION_DEPLOY, OPERATION_UNINSTALL):
        raise IntegrityConflict(f"journal: operation must be deploy or uninstall, got {operation!r}")
    tid = data.get("transaction_id")
    if not isinstance(tid, str) or not _TRANSACTION_ID_RE.fullmatch(tid):
        raise IntegrityConflict("journal: transaction_id must be 32 lowercase hex chars")
    phase = data.get("phase")
    valid_phases = UNINSTALL_PHASES if operation == OPERATION_UNINSTALL else DEPLOY_PHASES
    if phase not in valid_phases:
        raise IntegrityConflict(f"journal: phase {phase!r} not in {valid_phases}")
    owner = data.get("owner_directory")
    if owner != str(journal_dir.parent.resolve()):
        raise IntegrityConflict(f"journal: owner_directory {owner!r} != {str(journal_dir.parent.resolve())!r}")
    participants = data.get("participants")
    if not isinstance(participants, list) or not participants:
        raise IntegrityConflict("journal: participants must be a non-empty list")
    seen = set()
    for p in participants:
        if not isinstance(p, str) or not p:
            raise IntegrityConflict("journal: each participant must be a non-empty string")
        if p in seen:
            raise IntegrityConflict("journal: participants must be unique")
        seen.add(p)
    dirs = data.get("directories")
    if not isinstance(dirs, dict) or set(dirs.keys()) != seen:
        raise IntegrityConflict("journal: directories keys must match participants")
    for d, dinfo in dirs.items():
        if not isinstance(dinfo, dict):
            raise IntegrityConflict(f"journal.directories[{d}]: must be an object")
        jdir_name = dinfo.get("journal_dir")
        if jdir_name != f"{JOURNAL_PREFIX}{tid}":
            raise IntegrityConflict(f"journal.directories[{d}]: journal_dir must be {JOURNAL_PREFIX}{tid}")
        resources = dinfo.get("resources")
        if not isinstance(resources, dict):
            raise IntegrityConflict(f"journal.directories[{d}]: resources must be an object")
        for label, res in resources.items():
            _validate_journal_resource(res, f"journal.directories[{d}].resources[{label}]")
        residues = dinfo.get("residues")
        if not isinstance(residues, list):
            raise IntegrityConflict(f"journal.directories[{d}]: residues must be a list")
        for r in residues:
            _validate_residue(r, tid, d)


def _validate_journal_resource(res: Any, label: str) -> None:
    if not isinstance(res, dict):
        raise IntegrityConflict(f"{label}: must be an object")
    expected = {"path", "before", "snapshot", "allowed_absent", "allowed_sha256", "allowed_portable"}
    if set(res.keys()) != expected:
        raise IntegrityConflict(f"{label}: keys must be {sorted(expected)}")
    if not isinstance(res["path"], str) or not res["path"]:
        raise IntegrityConflict(f"{label}: path must be a non-empty string")
    res["before"] = _validate_portable_fingerprint(res["before"], f"{label}.before")
    if res["snapshot"] is not None and not isinstance(res["snapshot"], str):
        raise IntegrityConflict(f"{label}: snapshot must be null or a string")
    if not isinstance(res["allowed_absent"], bool):
        raise IntegrityConflict(f"{label}: allowed_absent must be a bool")
    if not isinstance(res["allowed_sha256"], list):
        raise IntegrityConflict(f"{label}: allowed_sha256 must be a list")
    for s in res["allowed_sha256"]:
        if not isinstance(s, str) or not re.fullmatch(r"[0-9a-f]{64}", s):
            raise IntegrityConflict(f"{label}: allowed_sha256 entries must be 64 hex chars")
    if not isinstance(res["allowed_portable"], list):
        raise IntegrityConflict(f"{label}: allowed_portable must be a list")
    for fp in res["allowed_portable"]:
        _validate_portable_fingerprint(fp, f"{label}.allowed_portable entry")


def _validate_residue(r: Any, tid: str, directory: str) -> None:
    if not isinstance(r, dict):
        raise IntegrityConflict("residue: must be an object")
    if set(r.keys()) != {"name", "identity", "members", "auth"}:
        raise IntegrityConflict("residue: keys must be name, identity, members, auth")
    if not isinstance(r["name"], str) or not r["name"]:
        raise IntegrityConflict("residue: name must be a non-empty string")
    if r["identity"] is not None and not isinstance(r["identity"], dict):
        raise IntegrityConflict("residue: identity must be null or an object")
    if not isinstance(r["members"], dict):
        raise IntegrityConflict("residue: members must be an object")
    for m, fp in r["members"].items():
        _validate_portable_fingerprint(fp, f"residue.members[{m}]")
    if r["auth"] != _residue_authorization_digest(tid, directory, r):
        raise IntegrityConflict("residue: auth digest mismatch")


# ─── Journal creation ─────────────────────────────────────────────────────────
def _create_deployment_journals(
    pi_dirs: List[Path],
    transaction_id: str,
    system_before: Dict[str, Dict[str, Any]],
    manifest_before: Dict[str, Optional[Dict[str, Any]]],
    system_content_sha256: str,
) -> Dict[str, Dict[str, Any]]:
    """Create a private journal dir per participant with immutable intent + snapshots.

    Returns the journal dicts keyed by participant path.
    """
    participants = [str(p.resolve()) for p in pi_dirs]
    journals: Dict[str, Dict[str, Any]] = {}
    for pi_dir in pi_dirs:
        resolved = str(pi_dir.resolve())
        jdir = _journal_dir(pi_dir, transaction_id)
        _FILESYSTEM.create_private_directory(jdir)
        sys_before_fp = system_before[resolved]
        manifest_before_fp = manifest_before[resolved]
        # Snapshots of before-state.
        sys_snapshot = None
        manifest_snapshot = None
        sys_path = pi_dir / SYSTEM_FILENAME
        manifest_path = pi_dir / MANIFEST_FILENAME
        if sys_before_fp is not None:
            sys_snapshot = f"snapshot-{RESOURCE_SYSTEM}"
            _FILESYSTEM.write_snapshot(sys_path, jdir / sys_snapshot)
        if manifest_before_fp is not None:
            manifest_snapshot = f"snapshot-{RESOURCE_MANIFEST}"
            _FILESYSTEM.write_snapshot(manifest_path, jdir / manifest_snapshot)
        resources = {
            RESOURCE_SYSTEM: _journal_resource(
                SYSTEM_FILENAME,
                sys_before_fp,
                sys_snapshot,
                allowed_absent=False,
                allowed_sha256=[system_content_sha256],
                allowed_portable=[],
            ),
            RESOURCE_MANIFEST: _journal_resource(
                MANIFEST_FILENAME,
                manifest_before_fp,
                manifest_snapshot,
                allowed_absent=True,
                allowed_sha256=[],
                allowed_portable=[manifest_before_fp] if manifest_before_fp else [],
            ),
        }
        residues: List[Dict[str, Any]] = []
        journal = {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "operation": OPERATION_DEPLOY,
            "transaction_id": transaction_id,
            "phase": PHASE_INITIALIZING,
            "owner_directory": resolved,
            "participants": participants,
            "directories": {
                resolved: {
                    "journal_dir": jdir.name,
                    "journal_identity": _portable_identity(jdir),
                    "resources": resources,
                    "residues": residues,
                }
            },
        }
        intent = _immutable_journal_intent(journal)
        _publish_immutable_intent(jdir, intent)
        _publish_journal(jdir, journal)
        journals[resolved] = journal
    return journals


def _create_uninstall_journals(
    pi_dirs: List[Path],
    transaction_id: str,
    manifest: Dict[str, Any],
    system_after_fp: Optional[Dict[str, Any]],
    previous_manifest_after: Optional[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    participants = [str(p.resolve()) for p in pi_dirs]
    journals: Dict[str, Dict[str, Any]] = {}
    for pi_dir in pi_dirs:
        resolved = str(pi_dir.resolve())
        jdir = _journal_dir(pi_dir, transaction_id)
        _FILESYSTEM.create_private_directory(jdir)
        sys_path = pi_dir / SYSTEM_FILENAME
        manifest_path = pi_dir / MANIFEST_FILENAME
        sys_before_fp = _portable_fingerprint(sys_path)
        manifest_before_fp = _portable_fingerprint(manifest_path)
        sys_snapshot = f"snapshot-{RESOURCE_SYSTEM}" if sys_before_fp is not None else None
        manifest_snapshot = f"snapshot-{RESOURCE_MANIFEST}" if manifest_before_fp is not None else None
        if sys_before_fp is not None:
            _FILESYSTEM.write_snapshot(sys_path, jdir / sys_snapshot)
        if manifest_before_fp is not None:
            _FILESYSTEM.write_snapshot(manifest_path, jdir / manifest_snapshot)
        resources = {
            RESOURCE_SYSTEM: _journal_resource(
                SYSTEM_FILENAME,
                sys_before_fp,
                sys_snapshot,
                allowed_absent=False,
                allowed_sha256=[],
                allowed_portable=[system_after_fp] if system_after_fp is not None else [],
            ),
            RESOURCE_MANIFEST: _journal_resource(
                MANIFEST_FILENAME,
                manifest_before_fp,
                manifest_snapshot,
                allowed_absent=True,
                allowed_sha256=[],
                allowed_portable=[previous_manifest_after] if previous_manifest_after is not None else [],
            ),
        }
        journal = {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "operation": OPERATION_UNINSTALL,
            "transaction_id": transaction_id,
            "phase": PHASE_U_INITIALIZING,
            "owner_directory": resolved,
            "participants": participants,
            "directories": {
                resolved: {
                    "journal_dir": jdir.name,
                    "journal_identity": _portable_identity(jdir),
                    "resources": resources,
                    "residues": [],
                }
            },
        }
        intent = _immutable_journal_intent(journal)
        _publish_immutable_intent(jdir, intent)
        _publish_journal(jdir, journal)
        journals[resolved] = journal
    return journals


def _update_journals(journals: Dict[str, Dict[str, Any]], phase: str, manifest_sha256: Optional[Dict[str, str]] = None) -> None:
    for resolved, journal in journals.items():
        journal["phase"] = phase
        jdir = _journal_dir(Path(resolved), journal["transaction_id"])
        _publish_journal_pending(jdir, journal)
        if manifest_sha256 is not None and resolved in manifest_sha256:
            # Update the manifest resource allowed_sha256 and publish companion.
            res = journal["directories"][resolved]["resources"][RESOURCE_MANIFEST]
            res["allowed_sha256"] = [manifest_sha256[resolved]]
            companion = {
                "transaction_id": journal["transaction_id"],
                "manifest_sha256": {resolved: manifest_sha256[resolved]},
            }
            _FILESYSTEM.write_private_text(
                jdir / MANIFEST_INTENT_PENDING_FILENAME,
                json.dumps(companion, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            )
        _publish_journal(jdir, journal)
        # Promote companion pending -> canonical if present.
        pend = jdir / MANIFEST_INTENT_PENDING_FILENAME
        canon = jdir / MANIFEST_INTENT_FILENAME
        if pend.exists() and _is_regular_path(pend):
            if not (canon.exists() and _is_regular_path(canon)):
                data, _ = _read_regular_bytes_with_fingerprint(pend)
                _FILESYSTEM.write_private_text(canon, data.decode("utf-8"))
            try:
                os.unlink(str(pend))
            except OSError:
                pass


# ─── Manifest build / publish ────────────────────────────────────────────────
def _serialize_manifest(manifest: Dict[str, Any]) -> str:
    return json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _build_deployment_manifest(
    transaction_id: str,
    system_before: Optional[Dict[str, Any]],
    system_after: Dict[str, Any],
    system_backup: Optional[str],
    append_system_path: str,
    append_system_preserved: bool,
    append_system_fp: Optional[Dict[str, Any]],
    agents_md_path: str,
    agents_md_preserved: bool,
    agents_md_fp: Optional[Dict[str, Any]],
    previous_manifest_before: Optional[Dict[str, Any]],
    previous_manifest_backup: Optional[str],
) -> Dict[str, Any]:
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "tool_version": VERSION,
        "deployment_id": transaction_id,
        "created_at": _utc_now(),
        "system": {
            "path": SYSTEM_FILENAME,
            "before": system_before,
            "after": system_after,
            "backup": system_backup,
        },
        "append_system": {
            "path": append_system_path,
            "preserved": append_system_preserved,
            "fingerprint": append_system_fp,
        },
        "agents_md": {
            "path": agents_md_path,
            "preserved": agents_md_preserved,
            "fingerprint": agents_md_fp,
        },
        "previous_manifest": {
            "before": previous_manifest_before,
            "backup": previous_manifest_backup,
        },
    }
    return _validate_manifest(manifest)


def _publish_deployment_manifest(pi_dir: Path, manifest: Dict[str, Any], expected_before: Optional[Dict[str, Any]]) -> str:
    """Publish manifest atomically; return its SHA-256."""
    path = pi_dir / MANIFEST_FILENAME
    content = _serialize_manifest(manifest)
    _FILESYSTEM.atomic_write_text(path, content, expected_fingerprint=expected_before)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _manifest_backup_name(timestamp: str) -> str:
    return f"{MANIFEST_FILENAME}.bak_{timestamp}"


def _system_backup_name(timestamp: str) -> str:
    return f"{SYSTEM_FILENAME}.bak_{timestamp}"


def _manifest_archive_name(timestamp: str) -> str:
    return f"{MANIFEST_FILENAME}.uninstalled_{timestamp}"


def _unique_archive_path(pi_dir: Path, base_name: str) -> Path:
    """Return a non-colliding archive path under `pi_dir`, suffixing if needed."""
    candidate = pi_dir / base_name
    if not candidate.exists():
        return candidate
    stem, _, suffix = base_name.partition(".uninstalled_")
    i = 1
    while True:
        alt = pi_dir / f"{stem}.uninstalled_{suffix}_{i}"
        if not alt.exists():
            return alt
        i += 1


# ─── Cleanup markers (re-enterable terminal cleanup) ─────────────────────────
def _cleanup_marker_path(pi_dir: Path, transaction_id: str) -> Path:
    return pi_dir / f"{CLEANUP_MARKER_PREFIX}{transaction_id}{CLEANUP_MARKER_SUFFIX}"


def _publish_cleanup_intent(pi_dir: Path, transaction_id: str, journal_members: Dict[str, Optional[Dict[str, Any]]], terminal_phase: str) -> None:
    payload = {
        "transaction_id": transaction_id,
        "terminal_phase": terminal_phase,
        "journal_dir": f"{JOURNAL_PREFIX}{transaction_id}",
        "members": journal_members,
    }
    _FILESYSTEM.write_private_text(_cleanup_marker_path(pi_dir, transaction_id), json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _load_cleanup_intent(pi_dir: Path, transaction_id: str) -> Optional[Dict[str, Any]]:
    p = _cleanup_marker_path(pi_dir, transaction_id)
    if not _is_regular_path(p):
        return None
    data, _ = _read_regular_bytes_with_fingerprint(p)
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntegrityConflict(f"{p}: cleanup intent is not valid JSON: {exc}") from exc


def _remove_cleanup_intent(pi_dir: Path, transaction_id: str) -> None:
    p = _cleanup_marker_path(pi_dir, transaction_id)
    if _is_regular_path(p):
        os.unlink(str(p))
        _fsync_directory(pi_dir)


# ─── Deployment ──────────────────────────────────────────────────────────────
def load_md_content(file_path: Optional[str]) -> str:
    if not file_path:
        return BUILTIN_SYSTEM_PROMPT_MD
    path = Path(file_path)
    if not _is_regular_path(path):
        raise IntegrityConflict(f"{file_path}: external prompt must be a regular file (no symlinks)")
    data, _ = _read_regular_bytes_with_fingerprint(path)
    return data.decode("utf-8")


def _prompt_sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _deploy_locked(args, pi_dirs: Optional[List[Path]] = None) -> None:
    if pi_dirs is None:
        dirs = find_pi_dirs()
        if not dirs:
            _print(_tr("no_pi_dirs"))
            sys.exit(1)
        pi_dirs = [Path(d) for d in dirs]
    md_content = load_md_content(args.file)
    prompt_sha = _prompt_sha256(md_content)
    if args.file is None:
        _print(_tr("prompt_source_builtin").format(sha=prompt_sha))
    else:
        _print(_tr("prompt_source_external").format(path=args.file, sha=prompt_sha))
    _print(_tr("behavior_notice_title").format(name=TOOL_NAME))
    _print(_tr("behavior_notice_preserve"))
    if not args.yes:
        _print(_tr("preview_only"))
        _print_windows_fresh_deployment_beta_warning()
        return
    _print_windows_fresh_deployment_beta_warning()
    transaction_id = _new_transaction_id()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    # Preflight: classify nodes.
    system_before: Dict[str, Optional[Dict[str, Any]]] = {}
    manifest_before: Dict[str, Optional[Dict[str, Any]]] = {}
    for pi_dir in pi_dirs:
        resolved = str(pi_dir.resolve())
        sys_path = pi_dir / SYSTEM_FILENAME
        man_path = pi_dir / MANIFEST_FILENAME
        for p in (sys_path, man_path, pi_dir / APPEND_SYSTEM_FILENAME, pi_dir / AGENTS_FILENAME, pi_dir / CLAUDE_FILENAME):
            if p.is_symlink() or (p.exists() and not _is_regular_path(p)):
                raise IntegrityConflict(f"{p}: managed target must be a regular file; refuse to proceed")
        system_before[resolved] = _portable_fingerprint(sys_path)
        manifest_before[resolved] = _portable_fingerprint(man_path)
    journals = _create_deployment_journals(pi_dirs, transaction_id, system_before, manifest_before, prompt_sha)
    _update_journals(journals, PHASE_PREPARED)
    system_backup_names: Dict[str, Optional[str]] = {}
    prev_manifest_backup: Dict[str, Optional[str]] = {}
    try:
        # system-intent phase: write SYSTEM.md (backup existing first).
        for pi_dir in pi_dirs:
            resolved = str(pi_dir.resolve())
            sys_path = pi_dir / SYSTEM_FILENAME
            before_fp = system_before[resolved]
            if before_fp is not None:
                backup_name = _system_backup_name(timestamp)
                backup_path = pi_dir / backup_name
                _FILESYSTEM.copy_to_backup(sys_path, backup_path)
                system_backup_names[resolved] = backup_name
            else:
                system_backup_names[resolved] = None
        _update_journals(journals, PHASE_SYSTEM_INTENT)
        for pi_dir in pi_dirs:
            resolved = str(pi_dir.resolve())
            sys_path = pi_dir / SYSTEM_FILENAME
            before_fp = system_before[resolved]
            _FILESYSTEM.atomic_write_text(sys_path, md_content, expected_fingerprint=before_fp)
        # manifest-intent phase: backup previous manifest, publish new.
        for pi_dir in pi_dirs:
            resolved = str(pi_dir.resolve())
            man_path = pi_dir / MANIFEST_FILENAME
            before_fp = manifest_before[resolved]
            if before_fp is not None:
                backup_name = _manifest_backup_name(timestamp)
                backup_path = pi_dir / backup_name
                _FILESYSTEM.copy_to_backup(man_path, backup_path)
                prev_manifest_backup[resolved] = backup_name
            else:
                prev_manifest_backup[resolved] = None
        # Build manifests per dir.
        manifests: Dict[str, Dict[str, Any]] = {}
        manifest_shas: Dict[str, str] = {}
        for pi_dir in pi_dirs:
            resolved = str(pi_dir.resolve())
            sys_path = pi_dir / SYSTEM_FILENAME
            append_path = pi_dir / APPEND_SYSTEM_FILENAME
            agents_path = pi_dir / AGENTS_FILENAME
            sys_after_fp = _portable_fingerprint(sys_path)
            manifests[resolved] = _build_deployment_manifest(
                transaction_id=transaction_id,
                system_before=system_before[resolved],
                system_after=sys_after_fp,
                system_backup=system_backup_names[resolved],
                append_system_path=APPEND_SYSTEM_FILENAME,
                append_system_preserved=append_path.exists() and _is_regular_path(append_path),
                append_system_fp=_portable_fingerprint(append_path),
                agents_md_path=AGENTS_FILENAME,
                agents_md_preserved=agents_path.exists() and _is_regular_path(agents_path),
                agents_md_fp=_portable_fingerprint(agents_path),
                previous_manifest_before=manifest_before[resolved],
                previous_manifest_backup=prev_manifest_backup[resolved],
            )
            manifest_shas[resolved] = _publish_deployment_manifest(pi_dir, manifests[resolved], manifest_before[resolved])
        _update_journals(journals, PHASE_MANIFEST_INTENT, manifest_sha256=manifest_shas)
        # final-sweep: re-fingerprint system + manifest match expectations.
        for pi_dir in pi_dirs:
            resolved = str(pi_dir.resolve())
            sys_path = pi_dir / SYSTEM_FILENAME
            man_path = pi_dir / MANIFEST_FILENAME
            if not _path_has_fingerprint(sys_path, manifests[resolved]["system"]["after"]):
                raise IntegrityConflict(f"{sys_path}: post-deploy fingerprint drift")
            live_manifest = _portable_fingerprint(man_path)
            if live_manifest is None or live_manifest["sha256"] != manifest_shas[resolved]:
                raise IntegrityConflict(f"{man_path}: post-deploy manifest sha drift")
        _update_journals(journals, PHASE_FINAL_SWEEP)
        _update_journals(journals, PHASE_COMMITTED)
        # Terminal cleanup: publish cleanup intent, remove journal, remove marker.
        for pi_dir in pi_dirs:
            resolved = str(pi_dir.resolve())
            jdir = _journal_dir(pi_dir, transaction_id)
            members = _enumerate_journal_members(jdir)
            _publish_cleanup_intent(pi_dir, transaction_id, members, PHASE_COMMITTED)
            _safe_remove_journal(pi_dir, transaction_id, members)
            _remove_cleanup_intent(pi_dir, transaction_id)
        for pi_dir in pi_dirs:
            _print(_tr("deployed").format(path=pi_dir / SYSTEM_FILENAME))
    except BaseException:
        # On any failure after journal creation, leave the durable journal in place
        # for explicit --recover; do NOT auto-rollback (SIGKILL cannot run Python).
        raise


def _safe_remove_journal(pi_dir: Path, transaction_id: str, members: Dict[str, Optional[Dict[str, Any]]]) -> None:
    jdir = _journal_dir(pi_dir, transaction_id)
    _FILESYSTEM.safe_remove_owned_directory(jdir, members)


def _enumerate_journal_members(jdir: Path) -> Dict[str, Optional[Dict[str, Any]]]:
    """Fingerprint every regular member of a journal directory (no symlinks)."""
    members: Dict[str, Optional[Dict[str, Any]]] = {}
    if not jdir.is_dir():
        return members
    for entry in sorted(os.listdir(str(jdir))):
        member = jdir / entry
        if os.path.islink(str(member)):
            raise IntegrityConflict(f"{member}: symlink in journal dir; refuse to clean")
        if not member.exists() or not _is_regular_path(member):
            continue
        members[entry] = _portable_fingerprint(member)
    return members


# ─── Status ───────────────────────────────────────────────────────────────────
def show_status(pi_dirs: List[str], lang_preview: bool = False) -> None:
    if not pi_dirs:
        _print(_tr("no_pi_dirs"))
        sys.exit(1)
    _print(_tr("found_dirs").format(n=len(pi_dirs)))
    any_blocker = False
    for raw in pi_dirs:
        pi_dir = Path(raw)
        _print(_tr("status_dir").format(dir=pi_dir))
        _print(f"    {_node_desc('SYSTEM.md', pi_dir / SYSTEM_FILENAME)}")
        _print(f"    {_node_desc('APPEND_SYSTEM.md', pi_dir / APPEND_SYSTEM_FILENAME)}")
        _print(f"    {_node_desc('AGENTS.md', pi_dir / AGENTS_FILENAME)}")
        _print(f"    {_node_desc('CLAUDE.md', pi_dir / CLAUDE_FILENAME)}")
        man_path = pi_dir / MANIFEST_FILENAME
        _print(f"    {_node_desc(_tr('deploy_manifest'), man_path)}")
        # Manifest detail.
        manifest_valid = False
        if _is_regular_path(man_path):
            try:
                manifest, _ = _load_manifest(man_path)
                _print(f"        deployment_id: {manifest['deployment_id']}")
                _print(f"        system.after.sha256: {manifest['system']['after']['sha256']}")
                _print(f"        append_system.preserved: {manifest['append_system']['preserved']}")
                _print(f"        agents_md.preserved: {manifest['agents_md']['preserved']}")
                manifest_valid = True
            except IntegrityConflict as exc:
                _print(f"        [invalid manifest] {exc}")
                any_blocker = True
        # Transaction residue.
        jdirs = _journal_dirs(pi_dir)
        cleanup = _cleanup_claim_dirs(pi_dir)
        temps = _keysmith_temp_dirs(pi_dir)
        residue_parts: List[str] = []
        if jdirs:
            residue_parts.append(f"{len(jdirs)} journal dir(s)")
        if cleanup:
            residue_parts.append(f"{len(cleanup)} cleanup marker(s)")
        if temps:
            residue_parts.append(f"{len(temps)} temp dir(s)")
        residue_str = ", ".join(residue_parts) if residue_parts else _tr("none_word")
        _print(f"    {_tr('transaction_residue')}: {residue_str}")
        if jdirs or cleanup or temps:
            any_blocker = True
        # Readiness.
        structural = _tr("healthy") if manifest_valid or not _is_regular_path(man_path) else _tr("blocked")
        uninstall_ready = _tr("ready") if manifest_valid and not (jdirs or cleanup or temps) else _tr("blocked")
        deploy_ready = _tr("ready") if not (jdirs or cleanup or temps) else _tr("blocked")
        _print(f"    {_tr('structural_health')}: {structural}")
        _print(f"    {_tr('uninstall_readiness')}: {uninstall_ready}")
        _print(f"    {_tr('deployability')}: {deploy_ready}")
    if any_blocker:
        sys.exit(1)


# ─── Recovery ─────────────────────────────────────────────────────────────────
def _journal_operation(journal_dir: Path) -> str:
    data = _load_journal(journal_dir)
    return data["operation"]


def _recover_deployment_locked(pi_dirs: List[str], yes: bool) -> None:
    # Collect journals across selected dirs.
    journal_map: Dict[str, List[Path]] = {}
    for raw in pi_dirs:
        pi_dir = Path(raw)
        for jd in _journal_dirs(pi_dir):
            try:
                op = _journal_operation(jd)
            except IntegrityConflict:
                continue
            journal_map.setdefault(op, []).append(jd)
    if not journal_map:
        _print(_localized("未找到需要恢复的事务 journal。", "No transaction journal requiring recovery found."))
        return
    if OPERATION_DEPLOY in journal_map and OPERATION_UNINSTALL in journal_map:
        raise IntegrityConflict("同一恢复范围包含 deploy 与 uninstall 事务；请分别指定其参与目录恢复。")
    operations = set(journal_map.keys())
    if operations == {OPERATION_UNINSTALL}:
        _recover_uninstall(journal_map[OPERATION_UNINSTALL], yes)
        return
    # Deploy recovery.
    journals = journal_map[OPERATION_DEPLOY]
    # Validate a single transaction id and gather participants.
    tid_set = set()
    ref_journal: Optional[Dict[str, Any]] = None
    for jd in journals:
        data = _load_journal(jd)
        tid_set.add(data["transaction_id"])
        if ref_journal is None:
            ref_journal = data
    if len(tid_set) != 1:
        raise IntegrityConflict(f"多个 transaction ID 被发现: {sorted(tid_set)}；请分别恢复。")
    tid = tid_set.pop()
    assert ref_journal is not None
    participants = ref_journal["participants"]
    # Build per-participant journal lookup.
    by_participant: Dict[str, Dict[str, Any]] = {}
    for jd in journals:
        data = _load_journal(jd)
        owner = data["owner_directory"]
        by_participant[owner] = data
    # Preflight: each participant journal must exist and live resources match before/after/residue.
    blockers: List[str] = []
    for p in participants:
        if p not in by_participant:
            blockers.append(f"{p}: 缺少该参与目录的 journal")
            continue
        jdata = by_participant[p]
        pi_dir = Path(p)
        for label, res in jdata["directories"][p]["resources"].items():
            rpath = pi_dir / res["path"]
            live_fp = _portable_fingerprint(rpath)
            ok = False
            if live_fp is not None and live_fp["sha256"] in res["allowed_sha256"]:
                ok = True
            if _path_has_fingerprint(rpath, res["before"]):
                ok = True
            for ap in res["allowed_portable"]:
                if ap is not None and _path_has_fingerprint(rpath, ap):
                    ok = True
            if res["allowed_absent"] and live_fp is None:
                ok = True
            if not ok:
                blockers.append(f"{p}: {label} 当前状态既非 before 也非计划 after")
    if blockers:
        for b in blockers:
            _print(f"[blocker] {b}")
        raise IntegrityConflict("恢复预检失败；保留 journal/快照/证据供人工核对。")
    if not yes:
        _print(_tr("recover_preview_only"))
        _print(_localized(f"将按反向顺序恢复事务 {tid}（manifest → system）。", f"Would reverse-restore transaction {tid} (manifest -> system)."))
        return
    # Set recovering phase.
    journals_to_update: Dict[str, Dict[str, Any]] = {}
    for p in participants:
        journals_to_update[p] = by_participant[p]
    _update_journals(journals_to_update, PHASE_RECOVERING)
    # Reverse rollback: manifest first, then system.
    for p in reversed(participants):
        pi_dir = Path(p)
        jdata = by_participant[p]
        resources = jdata["directories"][p]["resources"]
        for label in (RESOURCE_MANIFEST, RESOURCE_SYSTEM):
            res = resources[label]
            rpath = pi_dir / res["path"]
            if _path_has_fingerprint(rpath, res["before"]):
                continue
            if res["before"] is None:
                # Remove the created file.
                if rpath.exists():
                    os.unlink(str(rpath))
                    _fsync_directory(pi_dir)
            else:
                snapshot_path = _journal_dir(pi_dir, tid) / res["snapshot"]
                _FILESYSTEM.replace_owned_from_backup(snapshot_path, rpath, res["before"])
            # Verify.
            if not _path_has_fingerprint(rpath, res["before"]):
                raise IntegrityConflict(f"{rpath}: 恢复后指纹不匹配 before")
    _update_journals(journals_to_update, PHASE_RECOVERED)
    # Cleanup.
    for p in participants:
        pi_dir = Path(p)
        jdir = _journal_dir(pi_dir, tid)
        members = _enumerate_journal_members(jdir)
        _publish_cleanup_intent(pi_dir, tid, members, PHASE_RECOVERED)
        _safe_remove_journal(pi_dir, tid, members)
        _remove_cleanup_intent(pi_dir, tid)
    _print(_tr("recovered").format(tid=tid))


def _recover_uninstall(journals: List[Path], yes: bool) -> None:
    # For an interrupted uninstall, the forward operation was removing the managed
    # layer; recovery restores the before-state captured in snapshots (manifest +
    # system). This mirrors deploy recovery because the captured before-state is
    # the pre-uninstall state we want back.
    _recover_deployment_locked([str(jd.parent) for jd in journals], yes)


def recover_deployment(pi_dirs: List[str], yes: bool) -> None:
    if not pi_dirs:
        _print(_tr("no_pi_dirs"))
        sys.exit(1)
    _recover_deployment_locked(pi_dirs, yes)


# ─── Uninstall ────────────────────────────────────────────────────────────────
def _uninstall_locked(pi_dirs: List[str], yes: bool) -> None:
    if not pi_dirs:
        _print(_tr("no_pi_dirs"))
        sys.exit(1)
    pi_dir = Path(pi_dirs[0])
    man_path = pi_dir / MANIFEST_FILENAME
    if not _is_regular_path(man_path):
        _print(_tr("no_manifest_noop"))
        return
    manifest, manifest_before_fp = _load_manifest(man_path)
    sys_path = pi_dir / SYSTEM_FILENAME
    # Verify current SYSTEM.md matches manifest system.after.
    if not _path_has_fingerprint(sys_path, manifest["system"]["after"]):
        raise IntegrityConflict(f"{sys_path}: 当前内容与 manifest 记录的 after 不一致；中止卸载。")
    # Verify backup exists if before was non-null.
    system_before = manifest["system"]["before"]
    system_backup_name = manifest["system"]["backup"]
    if system_before is not None:
        if system_backup_name is None:
            raise IntegrityConflict("manifest: system.before 非空但 backup 缺失")
        backup_path = pi_dir / system_backup_name
        if not _is_regular_path(backup_path):
            raise IntegrityConflict(f"{backup_path}: system 备份缺失")
        if not _path_has_fingerprint(backup_path, system_before):
            raise IntegrityConflict(f"{backup_path}: system 备份内容与 before 不一致")
    # Verify append_system / agents_md unchanged (we never touch them; just report drift).
    append_path = pi_dir / APPEND_SYSTEM_FILENAME
    agents_path = pi_dir / AGENTS_FILENAME
    if manifest["append_system"]["preserved"] and not _path_has_fingerprint(append_path, manifest["append_system"]["fingerprint"]):
        _print(_localized("[警告] APPEND_SYSTEM.md 自部署后已变化；卸载不会改写它。", "[Warning] APPEND_SYSTEM.md changed since deploy; uninstall will not touch it."))
    if manifest["agents_md"]["preserved"] and not _path_has_fingerprint(agents_path, manifest["agents_md"]["fingerprint"]):
        _print(_localized("[警告] AGENTS.md 自部署后已变化；卸载不会改写它。", "[Warning] AGENTS.md changed since deploy; uninstall will not touch it."))
    # Verify previous manifest backup if present.
    prev_before = manifest["previous_manifest"]["before"]
    prev_backup_name = manifest["previous_manifest"]["backup"]
    if prev_before is not None:
        if prev_backup_name is None:
            raise IntegrityConflict("manifest: previous_manifest.before 非空但 backup 缺失")
        prev_backup_path = pi_dir / prev_backup_name
        if not _is_regular_path(prev_backup_path):
            raise IntegrityConflict(f"{prev_backup_path}: previous manifest 备份缺失")
    if not yes:
        _print(_tr("uninstall_preview_only"))
        _print(_localized(
            f"将恢复 SYSTEM.md 到部署前状态，归档当前 manifest，并{'恢复上一层 manifest' if prev_before is not None else '删除当前 manifest'}。",
            f"Would restore SYSTEM.md to pre-deploy state, archive the current manifest, and {'restore the previous manifest layer' if prev_before is not None else 'remove the current manifest'}.",
        ))
        return
    # Create durable journal for the uninstall.
    transaction_id = _new_transaction_id()
    # For uninstall, system "after" target = manifest.system.before; manifest "after" = previous_manifest.before.
    journals = _create_uninstall_journals(
        [pi_dir],
        transaction_id,
        manifest,
        system_after_fp=system_before,
        previous_manifest_after=prev_before,
    )
    _update_journals(journals, PHASE_U_PREPARED)
    try:
        # system-intent: restore SYSTEM.md from backup (or remove if it was created).
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        if system_before is None:
            if sys_path.exists():
                os.unlink(str(sys_path))
                _fsync_directory(pi_dir)
        else:
            backup_path = pi_dir / system_backup_name
            _FILESYSTEM.replace_owned_from_backup(backup_path, sys_path, manifest["system"]["after"])
        _update_journals(journals, PHASE_U_SYSTEM_INTENT)
        # manifest-intent: archive current manifest, restore previous if present.
        archive_path = _unique_archive_path(pi_dir, _manifest_archive_name(timestamp))
        # Move current manifest to archive via copy-then-remove (atomic move across same dir).
        _FILESYSTEM.copy_to_backup(man_path, archive_path)
        os.unlink(str(man_path))
        _fsync_directory(pi_dir)
        if prev_before is not None:
            prev_backup_path = pi_dir / prev_backup_name
            _FILESYSTEM.replace_owned_from_backup(prev_backup_path, man_path, None)
        _update_journals(journals, PHASE_U_MANIFEST_INTENT)
        # final-sweep: verify system matches before; manifest matches prev_before or absent.
        if not _path_has_fingerprint(sys_path, system_before):
            raise IntegrityConflict(f"{sys_path}: 卸载后 SYSTEM.md 未恢复到 before")
        if prev_before is not None:
            if not _path_has_fingerprint(man_path, prev_before):
                raise IntegrityConflict(f"{man_path}: 卸载后 manifest 未恢复到上一层")
        else:
            if man_path.exists():
                raise IntegrityConflict(f"{man_path}: 卸载后 manifest 应不存在")
        _update_journals(journals, PHASE_U_FINAL_SWEEP)
        _update_journals(journals, PHASE_U_COMMITTED)
        # Cleanup journal.
        jdir = _journal_dir(pi_dir, transaction_id)
        members = _enumerate_journal_members(jdir)
        _publish_cleanup_intent(pi_dir, transaction_id, members, PHASE_U_COMMITTED)
        _safe_remove_journal(pi_dir, transaction_id, members)
        _remove_cleanup_intent(pi_dir, transaction_id)
    except BaseException:
        raise
    _print(_tr("uninstalled"))


def uninstall(pi_dirs: List[str], yes: bool) -> None:
    _uninstall_locked(pi_dirs, yes)


# ─── CLI ──────────────────────────────────────────────────────────────────────
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=TOOL_NAME,
        description=_localized(
            "为本地 pi 配置目录部署版本化指令（SYSTEM.md），支持预览、所有权清单、中断恢复与分层卸载。",
            "Versioned instruction deployment for a local pi configuration directory, with preview, ownership manifest, interruption recovery, and layered uninstall.",
        ),
        add_help=True,
    )
    parser.add_argument("--file", "-f", default=argparse.SUPPRESS, help=_localized("外部 Markdown 指令；省略则使用内置提示词", "External Markdown instruction; omit for the bundled prompt"))
    parser.add_argument("--name", "-n", default=argparse.SUPPRESS, help=_localized("审计用输出名（部署目标恒为 SYSTEM.md）", "Audit output name (deploy target is always SYSTEM.md)"))
    operation_group = parser.add_mutually_exclusive_group()
    operation_group.add_argument("--dry-run", action="store_true", help=_localized("预览部署，不写文件", "Preview deployment without writes"))
    operation_group.add_argument("--status", action="store_true", help=_localized("只读状态", "Read-only status"))
    operation_group.add_argument("--uninstall", action="store_true", help=_localized("预览或撤销最新一层受管理部署", "Preview or remove the newest managed layer"))
    operation_group.add_argument("--recover", action="store_true", help=_localized("预览或恢复中断的 deploy/uninstall", "Preview or restore an interrupted deploy/uninstall"))
    parser.add_argument("--yes", action="store_true", help=_localized("确认部署、卸载或恢复", "Confirm deploy, uninstall, or recovery"))
    parser.add_argument("--pi-dir", default=None, help=_localized("显式选择单个 pi 配置目录", "Explicitly select one pi configuration directory"))
    parser.add_argument("--lang", choices=("auto", "zh-CN", "en"), default="auto", help=_localized("CLI 输出语言", "CLI output language"))
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def deploy(args, pi_dirs: Optional[List[str]] = None) -> None:
    _deploy_locked(args, pi_dirs=[Path(d) for d in pi_dirs] if pi_dirs else None)


def main() -> None:
    _configure_output_streams()
    lang = _language_from_argv(sys.argv)
    _set_output_language(lang)
    parser = _build_parser()
    raw = parser.parse_args()
    # Detect explicitly-provided --file/--name before default-filling SUPPRESS args.
    file_explicit = hasattr(raw, "file")
    name_explicit = hasattr(raw, "name")
    args = raw
    # Default-fill SUPPRESS args.
    if not hasattr(args, "file"):
        args.file = None
    if not hasattr(args, "name"):
        args.name = DEFAULT_NAME
    # Manual cross-checks: operation modes reject explicitly-provided deploy args.
    if args.status:
        if file_explicit:
            parser.error("--status 与 --file 互斥 / --status conflicts with --file")
        if name_explicit:
            parser.error("--status 与 --name 互斥 / --status conflicts with --name")
        if args.yes:
            parser.error("--status 与 --yes 互斥 / --status conflicts with --yes")
    if args.uninstall and (file_explicit or name_explicit):
        parser.error("--uninstall 与 --file/--name 互斥 / --uninstall conflicts with --file/--name")
    if args.recover and (file_explicit or name_explicit):
        parser.error("--recover 与 --file/--name 互斥 / --recover conflicts with --file/--name")
    # Resolve pi dir.
    pi_dirs: Optional[List[str]] = None
    if args.pi_dir:
        resolved = _resolve_pi_dir(args.pi_dir)
        if resolved is None or not resolved.is_dir():
            _print(_localized(f"[错误] 指定的 pi 目录不存在: {args.pi_dir}", f"[Error] Specified pi directory does not exist: {args.pi_dir}"))
            sys.exit(1)
        pi_dirs = [str(resolved)]
    if args.status:
        dirs = pi_dirs if pi_dirs is not None else find_pi_dirs()
        show_status(dirs)
        return
    if args.uninstall:
        dirs = pi_dirs if pi_dirs is not None else find_pi_dirs()
        uninstall(dirs, args.yes)
        return
    if args.recover:
        dirs = pi_dirs if pi_dirs is not None else find_pi_dirs()
        recover_deployment(dirs, args.yes)
        return
    # Default: deploy (dry-run unless --yes).
    if not args.yes:
        args.dry_run = True
    deploy(args, pi_dirs=pi_dirs)


if __name__ == "__main__":
    main()
