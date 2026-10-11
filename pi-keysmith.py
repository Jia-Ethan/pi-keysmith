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
__version__ = "0.2.0"
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
LOCK_FILENAME = ".pi-keysmith.lock"
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
OPERATION_RECOVER = "recover"
OPERATION_STATUS = "status"

# Machine-readable contract (`--json`). See docs/json-contract.md.
ENVELOPE_SCHEMA = "pi-keysmith.envelope.v1"
PREVIEW_TOKEN_SCHEMA = "pi-keysmith.preview-token.v1"
EXIT_OK = 0
EXIT_BLOCKED = 1
EXIT_USAGE = 2
EXIT_TOKEN_MISMATCH = 3
EXIT_WRITE_FAILED = 4
EXIT_LOCK_BUSY = 5
STATE_NOT_INSTALLED = "not-installed"
STATE_CONFLICT = "conflict"
STATE_RECOVERY_REQUIRED = "recovery-required"
STATE_DRIFT = "drift"
STATE_ACTIVE_ALIGNED = "active-aligned"
PRESERVED_PATHS = (APPEND_SYSTEM_FILENAME, AGENTS_FILENAME, CLAUDE_FILENAME, "settings.json", "models.json")

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
    "state_label": {"zh-CN": "状态", "en": "State"},
    "nothing_to_recover": {
        "zh-CN": "[恢复] 没有需要恢复的残留，无操作。",
        "en": "[Recover] No residue to recover; no-op.",
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
        self.atomic_write_bytes(path, content.encode("utf-8"), expected_fingerprint)

    def atomic_write_bytes(self, path: Path, data: bytes, expected_fingerprint: Optional[Dict[str, Any]] = None) -> None:
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
        # Publish through a temp file so an interruption never leaves a torn file.
        self.atomic_write_bytes(dest, data)

    def write_snapshot(self, source: Path, dest: Path) -> None:
        """Copy `source` bytes into a snapshot file `dest` (exclusive, no-follow)."""
        self.copy_to_backup(source, dest)


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


# ─── Manifest validation and loading ─────────────────────────────────────────
_TRANSACTION_ID_RE = re.compile(r"[0-9a-f]{32}")
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _is_plain_basename(name: Any) -> bool:
    return (
        isinstance(name, str)
        and name not in ("", ".", "..")
        and not any(sep in name for sep in ("/", "\\", "\0"))
    )


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
        if not _is_plain_basename(system["backup"]) or not system["backup"].startswith(f"{SYSTEM_FILENAME}.bak_"):
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
        if not _is_plain_basename(prev["backup"]) or not prev["backup"].startswith(f"{MANIFEST_FILENAME}.bak_"):
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
                allowed_absent=system_after_fp is None,
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


def _manifest_backup_name(timestamp: str) -> str:
    return f"{MANIFEST_FILENAME}.bak_{timestamp}"


def _system_backup_name(timestamp: str) -> str:
    return f"{SYSTEM_FILENAME}.bak_{timestamp}"


def _manifest_archive_name(timestamp: str) -> str:
    return f"{MANIFEST_FILENAME}.uninstalled_{timestamp}"


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


def _valid_cleanup_intent(intent: Any, transaction_id: str) -> bool:
    if not isinstance(intent, dict):
        return False
    if set(intent.keys()) != {"transaction_id", "terminal_phase", "journal_dir", "members"}:
        return False
    if intent["transaction_id"] != transaction_id or intent["journal_dir"] != f"{JOURNAL_PREFIX}{transaction_id}":
        return False
    if intent["terminal_phase"] not in (PHASE_COMMITTED, PHASE_RECOVERED):
        return False
    if not isinstance(intent["members"], dict):
        return False
    try:
        for name, fp in intent["members"].items():
            if not _is_plain_basename(name):
                return False
            _validate_portable_fingerprint(fp, f"cleanup.members[{name}]")
    except IntegrityConflict:
        return False
    return True


def _journal_members_match(journal_dir: Path, members: Dict[str, Optional[Dict[str, Any]]]) -> bool:
    """True when every remaining member of `journal_dir` is a recorded, unchanged regular file."""
    try:
        entries = os.listdir(str(journal_dir))
    except OSError:
        return False
    for entry in entries:
        if entry not in members or not _path_has_fingerprint(journal_dir / entry, members[entry]):
            return False
    return True


def _remove_journal_dir(journal_dir: Path, members: Dict[str, Optional[Dict[str, Any]]]) -> None:
    """Remove a private journal directory after verifying its remaining members."""
    if not _journal_members_match(journal_dir, members):
        raise IntegrityConflict(f"{journal_dir.name}: journal members changed; refuse to clean")
    for entry in sorted(os.listdir(str(journal_dir))):
        os.unlink(str(journal_dir / entry))
    os.rmdir(str(journal_dir))
    _fsync_directory(journal_dir.parent)


def _cleanup_journal(root: Path, transaction_id: str, terminal_phase: str) -> str:
    """Re-enterable terminal cleanup: marker -> journal removal -> marker removal."""
    journal_dir = _journal_dir(root, transaction_id)
    members = _enumerate_journal_members(journal_dir)
    _publish_cleanup_intent(root, transaction_id, members, terminal_phase)
    _remove_journal_dir(journal_dir, members)
    _remove_cleanup_intent(root, transaction_id)
    return journal_dir.name


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


def _unique_path(root: Path, base_name: str) -> Path:
    """Return a non-colliding sibling path under `root`, suffixing `_<n>` if needed."""
    candidate = root / base_name
    counter = 1
    while os.path.lexists(str(candidate)):
        candidate = root / f"{base_name}_{counter}"
        counter += 1
    return candidate


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


# ─── Prompt ──────────────────────────────────────────────────────────────────
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


# ─── Contract diagnostics ────────────────────────────────────────────────────
class UsageError(Exception):
    def __init__(self, code: str, detail: Optional[str] = None):
        super().__init__(code if detail is None else f"{code}: {detail}")
        self.code = code
        self.detail = detail


class ContractExit(Exception):
    """Stop an operation with a fixed exit code and diagnostic codes."""

    def __init__(self, exit_code: int, codes: List[str], detail: Optional[str] = None):
        super().__init__(", ".join(codes))
        self.exit_code = exit_code
        self.codes = codes
        self.detail = detail


_DIAGNOSTICS: Dict[str, Tuple[str, str]] = {
    "pi-dir-missing": ("pi 目录不存在；pi-keysmith 不会创建它", "pi directory does not exist; pi-keysmith never creates it"),
    "pi-dir-not-directory": ("pi 目录路径不是目录", "pi directory path is not a directory"),
    "system-not-regular": ("SYSTEM.md 不是普通文件", "SYSTEM.md is not a regular file"),
    "manifest-not-regular": ("manifest 不是普通文件", "manifest is not a regular file"),
    "manifest-invalid": ("manifest 校验失败", "manifest failed validation"),
    "journal-invalid": ("事务 journal 或清理标记无法解析", "transaction journal or cleanup marker cannot be parsed"),
    "journal-ambiguous": ("存在多个未完成事务；需人工处理", "multiple unfinished transactions; manual review required"),
    "residue-not-regular": ("残留临时文件不是普通文件", "residue temp entry is not a regular file"),
    "recover-resource-unexpected": ("资源当前状态既非 before 也非计划 after", "resource is neither its before state nor a planned after state"),
    "residue-present": ("存在事务残留；请先 --recover", "transaction residue present; run --recover first"),
    "preserved-not-regular": ("受保护文件不是普通文件；拒绝部署", "preserved file is not a regular file; deploy refused"),
    "not-installed": ("没有有效的 pi-keysmith 部署", "no valid pi-keysmith deployment"),
    "system-fingerprint-mismatch": ("SYSTEM.md 与 manifest 记录的部署内容不一致", "SYSTEM.md differs from the deployed content recorded in the manifest"),
    "system-backup-missing": ("SYSTEM.md 备份缺失", "SYSTEM.md backup is missing"),
    "system-backup-mismatch": ("SYSTEM.md 备份指纹不符", "SYSTEM.md backup fingerprint mismatch"),
    "manifest-backup-missing": ("上一层 manifest 备份缺失", "previous manifest backup is missing"),
    "manifest-backup-mismatch": ("上一层 manifest 备份指纹不符", "previous manifest backup fingerprint mismatch"),
    "preserved-changed": ("该文件自部署后已变化；卸载不会改写它", "file changed since deploy; uninstall will not touch it"),
    "preview-token-mismatch": ("状态已在预览后变化；请重新预览", "state changed since preview; preview again"),
    "preview-token-required": ("确认写需要 --expected-preview-token", "confirmed writes require --expected-preview-token"),
    "preview-token-invalid": ("预览 token 必须是 64 位小写 hex", "preview token must be 64 lowercase hex characters"),
    "preview-token-unexpected": ("--expected-preview-token 只用于确认写", "--expected-preview-token is only valid for confirmed writes"),
    "pi-dir-required": ("--json 需要 --pi-dir", "--json requires --pi-dir"),
    "pi-dir-not-absolute": ("--pi-dir 必须是绝对路径", "--pi-dir must be an absolute path"),
    "name-invalid": ("--name 只能包含字母、数字、点、下划线和连字符", "--name may only contain letters, digits, dot, underscore, and hyphen"),
    "prompt-file-invalid": ("--file 必须是 UTF-8 普通文件", "--file must be a UTF-8 regular file"),
    "usage-error": ("参数错误", "usage error"),
    "lock-busy": ("另一个 pi-keysmith 进程持有目录锁", "another pi-keysmith process holds the directory lock"),
    "lock-unavailable": ("无法打开目录锁文件", "cannot open the directory lock file"),
    "write-failed": ("写入开始后失败；可能需要 --recover", "failed after writes began; --recover may be required"),
    "windows-beta": (
        "Windows 上的 pi-keysmith 部署为 EXPLICIT_BETA，不构成正式支持",
        "pi-keysmith deployment on Windows is EXPLICIT_BETA and is not a formal support claim",
    ),
}


def _diagnostic(code: str, detail: Optional[str] = None) -> str:
    base, _, qualifier = code.partition(":")
    zh_cn, english = _DIAGNOSTICS.get(base, (base, base))
    text = _localized(zh_cn, english)
    extras = [x for x in (qualifier, detail) if x]
    if extras:
        text = f"{text} ({'; '.join(extras)})"
    return f"{base}: {text}"


# ─── Inspection ──────────────────────────────────────────────────────────────
def _add_code(codes: List[str], code: str) -> None:
    if code not in codes:
        codes.append(code)


def _short_fingerprint(path: Path) -> Optional[Dict[str, Any]]:
    fp = _fingerprint_or_none(path)
    return None if fp is None else {"sha256": fp["sha256"], "size": fp["size"]}


def _file_sha256(path: Path) -> Optional[str]:
    fp = _fingerprint_or_none(path)
    return None if fp is None else fp["sha256"]


def _node(path: Path) -> Dict[str, Any]:
    kind = _classify_node(path)
    return {"kind": kind, "path": str(path), "fingerprint": _short_fingerprint(path) if kind == "regular" else None}


def _pi_dir_kind(path: Path) -> str:
    if os.path.isdir(str(path)):
        return "directory"
    if not os.path.lexists(str(path)):
        return "missing"
    return _classify_node(path)


def _public_manifest(manifest: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "deployment_id": manifest["deployment_id"],
        "tool_version": manifest["tool_version"],
        "system": {
            "before": manifest["system"]["before"],
            "after": manifest["system"]["after"],
            "backup": manifest["system"]["backup"],
        },
        "append_system": {"preserved": manifest["append_system"]["preserved"]},
        "agents_md": {"preserved": manifest["agents_md"]["preserved"]},
        "previous_manifest": {
            "before": manifest["previous_manifest"]["before"],
            "backup": manifest["previous_manifest"]["backup"],
        },
    }


def _scan_residue(root: Path, conflicts: List[str]) -> Dict[str, Any]:
    """Classify journals, cleanup markers, and temp files under `root`."""
    journal_dirs: List[Path] = []
    markers: Dict[str, Tuple[Path, Dict[str, Any]]] = {}
    temps: List[Path] = []
    for entry in sorted(os.listdir(str(root))):
        path = root / entry
        if entry.startswith(JOURNAL_PREFIX):
            if os.path.islink(str(path)) or not path.is_dir():
                _add_code(conflicts, "journal-invalid")
            else:
                journal_dirs.append(path)
        elif entry.startswith(CLEANUP_MARKER_PREFIX) and entry.endswith(CLEANUP_MARKER_SUFFIX):
            tid = entry[len(CLEANUP_MARKER_PREFIX):-len(CLEANUP_MARKER_SUFFIX)]
            if not _is_regular_path(path):
                _add_code(conflicts, "residue-not-regular")
                continue
            try:
                intent = _load_cleanup_intent(root, tid) if _TRANSACTION_ID_RE.fullmatch(tid) else None
            except (IntegrityConflict, OSError):
                intent = None
            if intent is None or not _valid_cleanup_intent(intent, tid):
                _add_code(conflicts, "journal-invalid")
                continue
            markers[tid] = (path, intent)
        elif entry.startswith(RESIDUE_TEMP_PREFIX):
            if _is_regular_path(path):
                temps.append(path)
            else:
                _add_code(conflicts, "residue-not-regular")

    live: List[Dict[str, Any]] = []
    cleaning: List[Dict[str, Any]] = []
    for jdir in journal_dirs:
        tid = jdir.name[len(JOURNAL_PREFIX):]
        journal_file = jdir / JOURNAL_FILENAME
        if not _is_regular_path(journal_file):
            journal_file = jdir / JOURNAL_PENDING_FILENAME
        if tid in markers:
            # Terminal cleanup was interrupted: finish it, never parse or roll back.
            marker, intent = markers[tid]
            if not _journal_members_match(jdir, intent["members"]):
                _add_code(conflicts, "journal-invalid")
                continue
            cleaning.append({
                "dir": jdir,
                "transaction_id": tid,
                "marker": marker,
                "members": intent["members"],
                "phase": intent["terminal_phase"],
                "sha256": _file_sha256(journal_file),
            })
            continue
        try:
            data = _load_journal(jdir)
            resources = data["directories"][str(root)]["resources"]
            if (
                data["transaction_id"] != tid
                or data["participants"] != [str(root)]
                or set(resources.keys()) != {RESOURCE_SYSTEM, RESOURCE_MANIFEST}
                or resources[RESOURCE_SYSTEM]["path"] != SYSTEM_FILENAME
                or resources[RESOURCE_MANIFEST]["path"] != MANIFEST_FILENAME
            ):
                raise IntegrityConflict("journal: unexpected shape")
        except Exception:
            _add_code(conflicts, "journal-invalid")
            continue
        live.append({
            "dir": jdir,
            "data": data,
            "phase": data["phase"],
            "terminal": data["phase"] in (PHASE_COMMITTED, PHASE_RECOVERED),
            "sha256": _file_sha256(journal_file),
            "actions": [],
        })
    if len(live) > 1:
        _add_code(conflicts, "journal-ambiguous")
    for journal in live:
        if not journal["terminal"]:
            journal["actions"] = _recovery_actions(root, journal, conflicts)
    paired = {c["transaction_id"] for c in cleaning}
    return {
        "live": live,
        "cleaning": cleaning,
        "orphan_markers": [m[0] for tid, m in sorted(markers.items()) if tid not in paired],
        "markers": [m[0] for _, m in sorted(markers.items())],
        "temp_files": temps,
    }


def _recovery_actions(root: Path, journal: Dict[str, Any], conflicts: List[str]) -> List[Tuple[str, str]]:
    """Reverse actions for an unfinished journal; unexpected live state becomes a conflict."""
    resources = journal["data"]["directories"][str(root)]["resources"]
    actions: List[Tuple[str, str]] = []
    for label in (RESOURCE_MANIFEST, RESOURCE_SYSTEM):
        res = resources[label]
        rpath = root / res["path"]
        if _path_has_fingerprint(rpath, res["before"]):
            continue
        kind = _classify_node(rpath)
        live_fp = _portable_fingerprint(rpath) if kind == "regular" else None
        allowed = kind in ("missing", "regular") and (
            (live_fp is not None and live_fp["sha256"] in res["allowed_sha256"])
            or any(ap is not None and _path_has_fingerprint(rpath, ap) for ap in res["allowed_portable"])
            or (res["allowed_absent"] and kind == "missing")
        )
        if not allowed:
            _add_code(conflicts, f"recover-resource-unexpected:{label}")
            continue
        if res["before"] is None:
            actions.append((label, "remove"))
            continue
        snapshot = res["snapshot"]
        if not _is_plain_basename(snapshot) or not _path_has_fingerprint(journal["dir"] / snapshot, res["before"]):
            _add_code(conflicts, "journal-invalid")
            continue
        actions.append((label, "restore"))
    return actions


def _inspect(pi_dir: Path) -> Dict[str, Any]:
    """Read-only snapshot shared by status, plans, and the locked confirm path."""
    root = Path(os.path.realpath(str(pi_dir)))
    pi_kind = _pi_dir_kind(pi_dir)
    is_dir = pi_kind == "directory"
    nodes: Dict[str, Any] = {"pi_dir": {"kind": pi_kind, "path": str(pi_dir)}}
    for key, filename in (
        ("system", SYSTEM_FILENAME),
        ("manifest", MANIFEST_FILENAME),
        ("append_system", APPEND_SYSTEM_FILENAME),
        ("agents_md", AGENTS_FILENAME),
        ("claude_md", CLAUDE_FILENAME),
    ):
        nodes[key] = _node(pi_dir / filename) if is_dir else {"kind": "missing", "path": str(pi_dir / filename), "fingerprint": None}

    conflicts: List[str] = []
    drift: List[str] = []
    preserved_changed: List[str] = []
    manifest: Optional[Dict[str, Any]] = None
    residue = {"live": [], "cleaning": [], "orphan_markers": [], "markers": [], "temp_files": []}
    if pi_kind != "missing" and not is_dir:
        conflicts.append("pi-dir-not-directory")
    if is_dir:
        if nodes["system"]["kind"] not in ("missing", "regular"):
            conflicts.append("system-not-regular")
        if nodes["manifest"]["kind"] not in ("missing", "regular"):
            conflicts.append("manifest-not-regular")
        elif nodes["manifest"]["kind"] == "regular":
            try:
                manifest, _ = _load_manifest(root / MANIFEST_FILENAME)
            except Exception:
                conflicts.append("manifest-invalid")
        residue = _scan_residue(root, conflicts)
        if manifest is not None:
            if not _path_has_fingerprint(root / SYSTEM_FILENAME, manifest["system"]["after"]):
                drift.append("system-fingerprint-mismatch")
            for key, filename in (("append_system", APPEND_SYSTEM_FILENAME), ("agents_md", AGENTS_FILENAME)):
                if not _path_has_fingerprint(root / filename, manifest[key]["fingerprint"]):
                    preserved_changed.append(key)

    residue_names = {
        "journals": sorted(j["dir"].name for j in residue["live"] + residue["cleaning"]),
        "cleanup_markers": sorted(m.name for m in residue["markers"]),
        "temp_files": sorted(t.name for t in residue["temp_files"]),
    }
    has_residue = any(residue_names.values())

    if pi_kind == "missing":
        state = STATE_NOT_INSTALLED
    elif conflicts:
        state = STATE_CONFLICT
    elif has_residue:
        state = STATE_RECOVERY_REQUIRED
    elif manifest is None:
        state = STATE_NOT_INSTALLED
    elif drift:
        state = STATE_DRIFT
    else:
        state = STATE_ACTIVE_ALIGNED

    deploy_blockers = list(conflicts)
    uninstall_blockers = list(conflicts)
    if pi_kind == "missing":
        deploy_blockers.append("pi-dir-missing")
        uninstall_blockers.append("pi-dir-missing")
    if has_residue:
        deploy_blockers.append("residue-present")
    for key in ("append_system", "agents_md", "claude_md"):
        if nodes[key]["kind"] not in ("missing", "regular"):
            deploy_blockers.append(f"preserved-not-regular:{key}")
    if manifest is None:
        uninstall_blockers.append("not-installed")
    if has_residue:
        uninstall_blockers.append("residue-present")
    if manifest is not None:
        uninstall_blockers.extend(drift)
        uninstall_blockers.extend(_backup_blockers(root, manifest))

    status = {
        "state": state,
        "recovery_required": state == STATE_RECOVERY_REQUIRED,
        "nodes": nodes,
        "manifest": _public_manifest(manifest) if manifest is not None else None,
        "drift": drift,
        "conflicts": conflicts,
        "residue": residue_names,
        "preserved_changed": preserved_changed,
        "blockers": {"deploy": deploy_blockers, "uninstall": uninstall_blockers},
        "deployable": not deploy_blockers,
        "uninstall_ready": not uninstall_blockers,
    }
    return {"pi_dir": pi_dir, "root": root, "is_dir": is_dir, "manifest": manifest, "residue": residue, "status": status}


def _backup_blockers(root: Path, manifest: Dict[str, Any]) -> List[str]:
    blockers: List[str] = []
    for before, backup, prefix in (
        (manifest["system"]["before"], manifest["system"]["backup"], "system"),
        (manifest["previous_manifest"]["before"], manifest["previous_manifest"]["backup"], "manifest"),
    ):
        if before is None:
            continue
        path = root / backup
        if not _is_regular_path(path):
            blockers.append(f"{prefix}-backup-missing")
        elif not _path_has_fingerprint(path, before):
            blockers.append(f"{prefix}-backup-mismatch")
    return blockers


# ─── Plans and confirmation tokens ───────────────────────────────────────────
def _preview_token(operation: str, insp: Dict[str, Any], prompt_sha256: Optional[str]) -> str:
    status = insp["status"]
    root: Path = insp["root"]
    residue = insp["residue"]
    backups = None
    if operation == OPERATION_UNINSTALL and insp["manifest"] is not None:
        manifest = insp["manifest"]
        backups = {
            "system": _file_sha256(root / manifest["system"]["backup"]) if manifest["system"]["backup"] else None,
            "manifest": _file_sha256(root / manifest["previous_manifest"]["backup"]) if manifest["previous_manifest"]["backup"] else None,
        }
    journals = sorted(
        ({"name": j["dir"].name, "phase": j["phase"], "sha256": j["sha256"]} for j in residue["live"] + residue["cleaning"]),
        key=lambda j: j["name"],
    )

    def node_sha(key: str) -> Optional[str]:
        fp = status["nodes"][key]["fingerprint"]
        return fp["sha256"] if fp else None

    payload = {
        "schema": PREVIEW_TOKEN_SCHEMA,
        "operation": operation,
        "pi_dir": str(root),
        "state": status["state"],
        "system_sha256": node_sha("system"),
        "manifest_sha256": node_sha("manifest"),
        "prompt_sha256": prompt_sha256 if operation == OPERATION_DEPLOY else None,
        "backups": backups,
        "journals": journals,
        "orphans": sorted(p.name for p in residue["markers"] + residue["temp_files"]),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _plan_writes(operation: str, insp: Dict[str, Any]) -> List[Dict[str, Any]]:
    nodes = insp["status"]["nodes"]
    writes: List[Dict[str, Any]] = []
    if operation == OPERATION_DEPLOY:
        for key, filename in (("system", SYSTEM_FILENAME), ("manifest", MANIFEST_FILENAME)):
            if nodes[key]["kind"] == "regular":
                writes.append({"path": filename, "action": "replace", "backup": f"{filename}.bak_<ts>"})
            else:
                writes.append({"path": filename, "action": "create"})
        return writes
    if operation == OPERATION_UNINSTALL:
        manifest = insp["manifest"]
        if manifest["system"]["before"] is None:
            writes.append({"path": SYSTEM_FILENAME, "action": "remove"})
        else:
            writes.append({"path": SYSTEM_FILENAME, "action": "restore", "source": manifest["system"]["backup"]})
        writes.append({"path": MANIFEST_FILENAME, "action": "archive", "archive": _manifest_archive_name("<ts>")})
        if manifest["previous_manifest"]["before"] is not None:
            writes.append({"path": MANIFEST_FILENAME, "action": "restore", "source": manifest["previous_manifest"]["backup"]})
        return writes
    residue = insp["residue"]
    for journal in residue["live"]:
        resources = journal["data"]["directories"][str(insp["root"])]["resources"]
        for label, action in journal["actions"]:
            write = {"path": resources[label]["path"], "action": action}
            if action == "restore":
                write["source"] = f"{journal['dir'].name}/{resources[label]['snapshot']}"
            writes.append(write)
        writes.append({"path": journal["dir"].name, "action": "cleanup"})
    for journal in residue["cleaning"]:
        writes.append({"path": journal["dir"].name, "action": "cleanup"})
        writes.append({"path": journal["marker"].name, "action": "cleanup"})
    for marker in residue["orphan_markers"]:
        writes.append({"path": marker.name, "action": "cleanup"})
    for temp in residue["temp_files"]:
        writes.append({"path": temp.name, "action": "remove"})
    return writes


def _build_plan(operation: str, insp: Dict[str, Any], prompt: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    status = insp["status"]
    if operation == OPERATION_DEPLOY:
        blockers = list(status["blockers"]["deploy"])
    elif operation == OPERATION_UNINSTALL:
        blockers = list(status["blockers"]["uninstall"])
    else:
        blockers = list(status["conflicts"])
        if not insp["is_dir"] and "pi-dir-missing" not in blockers:
            blockers.append("pi-dir-missing")
    prompt_sha = prompt["sha256"] if prompt else None
    plan: Dict[str, Any] = {
        "confirmation_token": None if blockers else _preview_token(operation, insp, prompt_sha),
        "blockers": blockers,
        "writes": [] if blockers else _plan_writes(operation, insp),
        "preserves": list(PRESERVED_PATHS),
    }
    if operation == OPERATION_DEPLOY:
        plan["prompt"] = {"name": prompt["name"], "source": prompt["source"], "sha256": prompt["sha256"], "size": prompt["size"]}
    return plan


# ─── Directory lock ──────────────────────────────────────────────────────────
class _DirectoryLock:
    """Non-blocking exclusive lock on `<pi_dir>/.pi-keysmith.lock` for confirmed writes."""

    def __init__(self, root: Path):
        self.path = root / LOCK_FILENAME
        self.fd = -1

    def __enter__(self) -> "_DirectoryLock":
        try:
            self.fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
            if not stat.S_ISREG(os.fstat(self.fd).st_mode):
                raise IntegrityConflict(f"{LOCK_FILENAME}: not a regular file")
        except OSError as exc:
            self._close()
            raise ContractExit(EXIT_BLOCKED, ["lock-unavailable"]) from exc
        try:
            if _is_windows_platform():
                import msvcrt

                msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._close()
            raise ContractExit(EXIT_LOCK_BUSY, ["lock-busy"]) from exc
        return self

    def __exit__(self, *exc_info) -> None:
        self._close()

    def _close(self) -> None:
        if self.fd >= 0:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = -1


class _NoLock:
    def __enter__(self) -> "_NoLock":
        return self

    def __exit__(self, *exc_info) -> None:
        return None


# ─── Confirmed writes ────────────────────────────────────────────────────────
def _write_result(root: Path, transaction_id: Optional[str], cleaned: List[str]) -> Dict[str, Any]:
    return {
        "transaction_id": transaction_id,
        "applied": True,
        "system_sha256": _file_sha256(root / SYSTEM_FILENAME),
        "manifest_sha256": _file_sha256(root / MANIFEST_FILENAME),
        "cleaned": cleaned,
    }


def _apply_deploy(insp: Dict[str, Any], prompt_bytes: bytes) -> Dict[str, Any]:
    root: Path = insp["root"]
    resolved = str(root)
    sys_path = root / SYSTEM_FILENAME
    man_path = root / MANIFEST_FILENAME
    append_path = root / APPEND_SYSTEM_FILENAME
    agents_path = root / AGENTS_FILENAME
    transaction_id = _new_transaction_id()
    timestamp = _timestamp()
    system_before = _portable_fingerprint(sys_path)
    manifest_before = _portable_fingerprint(man_path)
    journals = _create_deployment_journals(
        [root], transaction_id, {resolved: system_before}, {resolved: manifest_before}, _sha256_bytes(prompt_bytes)
    )
    _update_journals(journals, PHASE_PREPARED)
    system_backup: Optional[str] = None
    if system_before is not None:
        backup_path = _unique_path(root, _system_backup_name(timestamp))
        _FILESYSTEM.copy_to_backup(sys_path, backup_path)
        system_backup = backup_path.name
    _update_journals(journals, PHASE_SYSTEM_INTENT)
    _FILESYSTEM.atomic_write_bytes(sys_path, prompt_bytes, expected_fingerprint=system_before)
    manifest_backup: Optional[str] = None
    if manifest_before is not None:
        backup_path = _unique_path(root, _manifest_backup_name(timestamp))
        _FILESYSTEM.copy_to_backup(man_path, backup_path)
        manifest_backup = backup_path.name
    manifest = _build_deployment_manifest(
        transaction_id=transaction_id,
        system_before=system_before,
        system_after=_portable_fingerprint(sys_path),
        system_backup=system_backup,
        append_system_path=APPEND_SYSTEM_FILENAME,
        append_system_preserved=_is_regular_path(append_path),
        append_system_fp=_portable_fingerprint(append_path),
        agents_md_path=AGENTS_FILENAME,
        agents_md_preserved=_is_regular_path(agents_path),
        agents_md_fp=_portable_fingerprint(agents_path),
        previous_manifest_before=manifest_before,
        previous_manifest_backup=manifest_backup,
    )
    content = _serialize_manifest(manifest)
    manifest_sha = _sha256_bytes(content.encode("utf-8"))
    # Record the exact manifest bytes before publishing them so recovery can recognize them.
    _update_journals(journals, PHASE_MANIFEST_INTENT, manifest_sha256={resolved: manifest_sha})
    _FILESYSTEM.atomic_write_text(man_path, content, expected_fingerprint=manifest_before)
    if not _path_has_fingerprint(sys_path, manifest["system"]["after"]):
        raise IntegrityConflict(f"{SYSTEM_FILENAME}: post-deploy fingerprint drift")
    if _file_sha256(man_path) != manifest_sha:
        raise IntegrityConflict(f"{MANIFEST_FILENAME}: post-deploy manifest sha drift")
    _update_journals(journals, PHASE_FINAL_SWEEP)
    _update_journals(journals, PHASE_COMMITTED)
    _cleanup_journal(root, transaction_id, PHASE_COMMITTED)
    return _write_result(root, transaction_id, [])


def _apply_uninstall(insp: Dict[str, Any]) -> Dict[str, Any]:
    root: Path = insp["root"]
    manifest = insp["manifest"]
    sys_path = root / SYSTEM_FILENAME
    man_path = root / MANIFEST_FILENAME
    system_before = manifest["system"]["before"]
    prev_before = manifest["previous_manifest"]["before"]
    manifest_live = _portable_fingerprint(man_path)
    transaction_id = _new_transaction_id()
    timestamp = _timestamp()
    journals = _create_uninstall_journals(
        [root], transaction_id, manifest, system_after_fp=system_before, previous_manifest_after=prev_before
    )
    _update_journals(journals, PHASE_U_PREPARED)
    _update_journals(journals, PHASE_U_SYSTEM_INTENT)
    if system_before is None:
        _FILESYSTEM.remove_owned_file(sys_path, manifest["system"]["after"])
    else:
        _FILESYSTEM.replace_owned_from_backup(root / manifest["system"]["backup"], sys_path, manifest["system"]["after"])
    _update_journals(journals, PHASE_U_MANIFEST_INTENT)
    _FILESYSTEM.copy_to_backup(man_path, _unique_path(root, _manifest_archive_name(timestamp)))
    _FILESYSTEM.remove_owned_file(man_path, manifest_live)
    if prev_before is not None:
        _FILESYSTEM.replace_owned_from_backup(root / manifest["previous_manifest"]["backup"], man_path, None)
    if not _path_has_fingerprint(sys_path, system_before):
        raise IntegrityConflict(f"{SYSTEM_FILENAME}: not restored to its before state")
    if not _path_has_fingerprint(man_path, prev_before):
        raise IntegrityConflict(f"{MANIFEST_FILENAME}: not restored to the previous layer")
    _update_journals(journals, PHASE_U_FINAL_SWEEP)
    _update_journals(journals, PHASE_U_COMMITTED)
    _cleanup_journal(root, transaction_id, PHASE_U_COMMITTED)
    return _write_result(root, transaction_id, [])


def _apply_recover(insp: Dict[str, Any]) -> Dict[str, Any]:
    root: Path = insp["root"]
    residue = insp["residue"]
    cleaned: List[str] = []
    transaction_id: Optional[str] = None
    for temp in residue["temp_files"]:
        if not _is_regular_path(temp):
            raise IntegrityConflict(f"{temp.name}: not a regular file")
        os.unlink(str(temp))
        cleaned.append(temp.name)
    for journal in residue["cleaning"]:
        _remove_journal_dir(journal["dir"], journal["members"])
        _remove_cleanup_intent(root, journal["transaction_id"])
        cleaned.extend([journal["dir"].name, journal["marker"].name])
        transaction_id = transaction_id or journal["transaction_id"]
    for marker in residue["orphan_markers"]:
        if not _is_regular_path(marker):
            raise IntegrityConflict(f"{marker.name}: not a regular file")
        os.unlink(str(marker))
        cleaned.append(marker.name)
        transaction_id = transaction_id or marker.name[len(CLEANUP_MARKER_PREFIX):-len(CLEANUP_MARKER_SUFFIX)]
    for journal in residue["live"]:
        data = journal["data"]
        transaction_id = data["transaction_id"]
        terminal_phase = data["phase"]
        if not journal["terminal"]:
            # Unfinished forward operation: reverse-restore manifest, then system.
            journals = {str(root): data}
            resources = data["directories"][str(root)]["resources"]
            _update_journals(journals, PHASE_RECOVERING)
            for label, action in journal["actions"]:
                res = resources[label]
                rpath = root / res["path"]
                if action == "remove":
                    _FILESYSTEM.remove_owned_file(rpath, _portable_fingerprint(rpath))
                else:
                    snapshot, _ = _read_regular_bytes_with_fingerprint(journal["dir"] / res["snapshot"])
                    _FILESYSTEM.atomic_write_bytes(rpath, snapshot, expected_fingerprint=_portable_fingerprint(rpath))
                if not _path_has_fingerprint(rpath, res["before"]):
                    raise IntegrityConflict(f"{res['path']}: does not match its before state after recovery")
            _update_journals(journals, PHASE_RECOVERED)
            terminal_phase = PHASE_RECOVERED
        # A terminal journal means the forward operation finished; only cleanup remains.
        cleaned.append(_cleanup_journal(root, transaction_id, terminal_phase))
    if cleaned:
        _fsync_directory(root)
    return _write_result(root, transaction_id, cleaned)


# ─── Operations ──────────────────────────────────────────────────────────────
_TOKEN_RE = re.compile(r"[0-9a-f]{64}")


def _load_prompt(args) -> Tuple[bytes, Dict[str, Any]]:
    try:
        content = load_md_content(args.file)
    except (IntegrityConflict, OSError, UnicodeDecodeError) as exc:
        raise UsageError("prompt-file-invalid") from exc
    data = content.encode("utf-8")
    prompt = {
        "name": args.name,
        "source": "file" if args.file else "builtin",
        "sha256": _sha256_bytes(data),
        "size": len(data),
    }
    return data, prompt


def _run_operation(operation: str, pi_dir: Path, args) -> Dict[str, Any]:
    """Run one operation and return the envelope body (without schema metadata)."""
    out: Dict[str, Any] = {
        "operation": operation,
        "preview": True,
        "apply": False,
        "ok": True,
        "exit_code": EXIT_OK,
        "status": None,
        "plan": None,
        "result": None,
        "diagnostics": [],
    }
    prompt_bytes: Optional[bytes] = None
    prompt: Optional[Dict[str, Any]] = None
    if operation == OPERATION_DEPLOY:
        prompt_bytes, prompt = _load_prompt(args)
        if _is_windows_platform():
            out["diagnostics"].append(_diagnostic("windows-beta"))

    insp = _inspect(pi_dir)
    out["status"] = insp["status"]
    if operation == OPERATION_STATUS:
        if insp["status"]["state"] in (STATE_CONFLICT, STATE_RECOVERY_REQUIRED):
            out["ok"] = False
            out["exit_code"] = EXIT_BLOCKED
        out["diagnostics"].extend(_diagnostic(c) for c in insp["status"]["conflicts"] + insp["status"]["drift"])
        return out

    plan = _build_plan(operation, insp, prompt)
    out["plan"] = plan
    if operation == OPERATION_UNINSTALL and not plan["blockers"]:
        out["diagnostics"].extend(_diagnostic(f"preserved-changed:{key}") for key in insp["status"]["preserved_changed"])
    if not args.yes:
        if plan["blockers"]:
            out["ok"] = False
            out["exit_code"] = EXIT_BLOCKED
            out["diagnostics"].extend(_diagnostic(c) for c in plan["blockers"])
        return out

    try:
        _confirm(operation, pi_dir, insp, args, prompt, prompt_bytes, out)
    except ContractExit as exc:
        out["ok"] = False
        out["exit_code"] = exc.exit_code
        out["result"] = None
        out["diagnostics"].extend(_diagnostic(code, exc.detail) for code in exc.codes)
    return out


def _confirm(
    operation: str,
    pi_dir: Path,
    insp: Dict[str, Any],
    args,
    prompt: Optional[Dict[str, Any]],
    prompt_bytes: Optional[bytes],
    out: Dict[str, Any],
) -> None:
    """Confirmed write: re-inspect under the directory lock and re-check the token."""
    lock = _DirectoryLock(insp["root"]) if insp["is_dir"] else _NoLock()
    with lock:
        insp = _inspect(pi_dir)
        plan = _build_plan(operation, insp, prompt)
        out["plan"] = plan
        out["status"] = insp["status"]
        # The token binds the preview's snapshot; any change since then is a
        # mismatch even if it also produced a blocker.
        expected = args.expected_preview_token
        if expected is not None and expected != _preview_token(operation, insp, prompt["sha256"] if prompt else None):
            raise ContractExit(EXIT_TOKEN_MISMATCH, ["preview-token-mismatch"])
        if plan["blockers"]:
            raise ContractExit(EXIT_BLOCKED, plan["blockers"])
        out["apply"] = True
        out["preview"] = False
        try:
            if operation == OPERATION_DEPLOY:
                assert prompt_bytes is not None
                out["result"] = _apply_deploy(insp, prompt_bytes)
            elif operation == OPERATION_UNINSTALL:
                out["result"] = _apply_uninstall(insp)
            else:
                out["result"] = _apply_recover(insp) if plan["writes"] else {
                    "transaction_id": None,
                    "applied": True,
                    "system_sha256": _file_sha256(insp["root"] / SYSTEM_FILENAME),
                    "manifest_sha256": _file_sha256(insp["root"] / MANIFEST_FILENAME),
                    "cleaned": [],
                }
        except (OSError, AssertionError) as exc:
            out["status"] = _inspect(pi_dir)["status"]
            raise ContractExit(EXIT_WRITE_FAILED, ["write-failed"], detail=type(exc).__name__) from exc
        out["status"] = _inspect(pi_dir)["status"]


# ─── Envelope ────────────────────────────────────────────────────────────────
def _envelope(operation: Optional[str], pi_dir: Optional[Path], body: Dict[str, Any]) -> Dict[str, Any]:
    target: Dict[str, Any] = {}
    if pi_dir is not None:
        target = {"pi_dir": str(pi_dir), "pi_dir_resolved": os.path.realpath(str(pi_dir)), "scope": "user"}
    return {
        "schema": ENVELOPE_SCHEMA,
        "tool": TOOL_NAME,
        "version": VERSION,
        "operation": operation,
        "preview": body.get("preview", True),
        "apply": body.get("apply", False),
        "ok": body["ok"],
        "exit_code": body["exit_code"],
        "target": target,
        "status": body.get("status"),
        "plan": body.get("plan"),
        "result": body.get("result"),
        "diagnostics": body.get("diagnostics", []),
    }


def _emit_json(envelope: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(envelope, ensure_ascii=False, sort_keys=False) + "\n")
    sys.stdout.flush()


# ─── Human output ────────────────────────────────────────────────────────────
def _print_status(status: Dict[str, Any]) -> None:
    labels = {
        "system": "SYSTEM.md",
        "append_system": "APPEND_SYSTEM.md",
        "agents_md": "AGENTS.md",
        "claude_md": "CLAUDE.md",
        "manifest": _tr("deploy_manifest"),
    }
    _print(_tr("status_dir").format(dir=status["nodes"]["pi_dir"]["path"]))
    for key, label in labels.items():
        node = status["nodes"][key]
        _print(f"    {label}: {node['kind']} ({node['path']})")
        if key == "manifest" and status["manifest"] is not None:
            manifest = status["manifest"]
            _print(f"        deployment_id: {manifest['deployment_id']}")
            _print(f"        system.after.sha256: {manifest['system']['after']['sha256']}")
            _print(f"        append_system.preserved: {manifest['append_system']['preserved']}")
            _print(f"        agents_md.preserved: {manifest['agents_md']['preserved']}")
    residue = status["residue"]
    parts = []
    if residue["journals"]:
        parts.append(f"{len(residue['journals'])} journal dir(s)")
    if residue["cleanup_markers"]:
        parts.append(f"{len(residue['cleanup_markers'])} cleanup marker(s)")
    if residue["temp_files"]:
        parts.append(f"{len(residue['temp_files'])} temp file(s)")
    _print(f"    {_tr('transaction_residue')}: {', '.join(parts) if parts else _tr('none_word')}")
    _print(f"    {_tr('state_label')}: {status['state']}")
    for code in status["conflicts"] + status["drift"]:
        _print(f"    - {_diagnostic(code)}")
    _print(f"    {_tr('uninstall_readiness')}: {_tr('ready') if status['uninstall_ready'] else _tr('blocked')}")
    _print(f"    {_tr('deployability')}: {_tr('ready') if status['deployable'] else _tr('blocked')}")


def _print_human(body: Dict[str, Any], args) -> None:
    operation = body["operation"]
    plan = body["plan"]
    if operation == OPERATION_STATUS:
        _print(_tr("found_dirs").format(n=1))
        _print_status(body["status"])
        return
    if operation == OPERATION_DEPLOY and plan is not None:
        prompt = plan["prompt"]
        if prompt["source"] == "builtin":
            _print(_tr("prompt_source_builtin").format(sha=prompt["sha256"]))
        else:
            _print(_tr("prompt_source_external").format(path=args.file, sha=prompt["sha256"]))
        _print(_tr("behavior_notice_title").format(name=TOOL_NAME))
        _print(_tr("behavior_notice_preserve"))
        _print_windows_fresh_deployment_beta_warning()
    if not body["ok"]:
        for line in body["diagnostics"]:
            _print(f"[{_localized('阻止', 'Blocked')}] {line}")
        return
    for line in body["diagnostics"]:
        if line.startswith("preserved-changed:"):
            _print(f"[{_localized('警告', 'Warning')}] {line}")
    if body["result"] is None:
        _print(_tr({OPERATION_DEPLOY: "preview_only", OPERATION_UNINSTALL: "uninstall_preview_only"}.get(operation, "recover_preview_only")))
        for write in plan["writes"]:
            _print(f"    {write['action']}: {write['path']}")
        if plan["writes"]:
            _print(_localized(
                "确认请追加 --yes（可选 --expected-preview-token 绑定本次预览）：",
                "Confirm with --yes (optionally --expected-preview-token to bind this preview):",
            ))
            _print(f"    --expected-preview-token {plan['confirmation_token']}")
        return
    result = body["result"]
    if operation == OPERATION_DEPLOY:
        _print(_tr("deployed").format(path=Path(body["status"]["nodes"]["system"]["path"])))
    elif operation == OPERATION_UNINSTALL:
        _print(_tr("uninstalled"))
    elif result["cleaned"]:
        _print(_tr("recovered").format(tid=result["transaction_id"]))
        for name in result["cleaned"]:
            _print(f"    cleaned: {name}")
    else:
        _print(_tr("nothing_to_recover"))


# ─── CLI ──────────────────────────────────────────────────────────────────────
class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str):  # type: ignore[override]
        raise UsageError("usage-error", message)


def _build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
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
    parser.add_argument("--json", action="store_true", help=_localized("stdout 只输出一个 pi-keysmith.envelope.v1 JSON 对象", "Print exactly one pi-keysmith.envelope.v1 JSON object on stdout"))
    parser.add_argument("--expected-preview-token", default=None, metavar="TOKEN", help=_localized("确认写时绑定预览的 plan.confirmation_token", "Bind a confirmed write to the preview's plan.confirmation_token"))
    parser.add_argument("--pi-dir", default=None, help=_localized("显式选择单个 pi 配置目录（--json 时必填且须为绝对路径）", "Explicitly select one pi configuration directory (required and absolute with --json)"))
    parser.add_argument("--lang", choices=("auto", "zh-CN", "en"), default="auto", help=_localized("CLI 输出语言", "CLI output language"))
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def _parse(parser: argparse.ArgumentParser, argv: List[str]) -> Tuple[Any, str]:
    args = parser.parse_args(argv)
    file_explicit = hasattr(args, "file")
    name_explicit = hasattr(args, "name")
    if not file_explicit:
        args.file = None
    if not name_explicit:
        args.name = DEFAULT_NAME
    if args.status:
        if file_explicit or name_explicit or args.yes:
            raise UsageError("usage-error", "--status conflicts with --file/--name/--yes")
        operation = OPERATION_STATUS
    elif args.uninstall or args.recover:
        if file_explicit or name_explicit:
            raise UsageError("usage-error", "--uninstall/--recover conflict with --file/--name")
        operation = OPERATION_UNINSTALL if args.uninstall else OPERATION_RECOVER
    else:
        operation = OPERATION_DEPLOY
        if args.dry_run:
            # --dry-run is always a pure preview, even alongside --yes.
            args.yes = False
    if name_explicit and not _NAME_RE.fullmatch(args.name or ""):
        raise UsageError("name-invalid")
    confirmed = args.yes and operation != OPERATION_STATUS
    if args.expected_preview_token is not None:
        if not confirmed:
            raise UsageError("preview-token-unexpected")
        if not _TOKEN_RE.fullmatch(args.expected_preview_token):
            raise UsageError("preview-token-invalid")
    if args.json:
        if not args.pi_dir:
            raise UsageError("pi-dir-required")
        if not os.path.isabs(args.pi_dir):
            raise UsageError("pi-dir-not-absolute")
        if confirmed and args.expected_preview_token is None:
            raise UsageError("preview-token-required")
    return args, operation


def _json_wanted(argv: List[str]) -> bool:
    return "--json" in argv


def main(argv: Optional[List[str]] = None) -> int:
    _configure_output_streams()
    argv = list(sys.argv[1:] if argv is None else argv)
    _set_output_language(_language_from_argv(argv))
    json_mode = _json_wanted(argv)
    parser = _build_parser()
    operation: Optional[str] = None
    pi_dir: Optional[Path] = None
    try:
        args, operation = _parse(parser, argv)
        json_mode = args.json
        if args.pi_dir:
            pi_dir = Path(args.pi_dir).expanduser()
        elif not json_mode:
            pi_dir = _resolve_pi_dir(None)
            if pi_dir is None:
                _print(_tr("no_pi_dirs"))
                return EXIT_BLOCKED
        assert pi_dir is not None
        if not json_mode:
            # Human mode keeps the 0.1.0 behavior for these two edge cases.
            if not pi_dir.is_dir():
                _print(_localized(f"[错误] 指定的 pi 目录不存在: {pi_dir}", f"[Error] Specified pi directory does not exist: {pi_dir}"))
                return EXIT_BLOCKED
            if operation == OPERATION_UNINSTALL and _classify_node(pi_dir / MANIFEST_FILENAME) == "missing":
                _print(_tr("no_manifest_noop"))
                return EXIT_OK
        body = _run_operation(operation, pi_dir, args)
    except UsageError as exc:
        if not json_mode:
            parser.print_usage(sys.stderr)
            sys.stderr.write(f"{TOOL_NAME}: error: {_diagnostic(exc.code, exc.detail)}\n")
            return EXIT_USAGE
        body = {"ok": False, "exit_code": EXIT_USAGE, "diagnostics": [_diagnostic(exc.code, exc.detail)]}
        _emit_json(_envelope(operation, pi_dir, body))
        return EXIT_USAGE
    if json_mode:
        _emit_json(_envelope(operation, pi_dir, body))
    else:
        _print_human(body, args)
    return body["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
