#!/usr/bin/env python3
"""promptfoo provider: run the pi coding agent under an isolated config.

The prompt under test (the file passed in `prompts:`) is written to
SYSTEM.md inside a throwaway PI_CODING_AGENT_DIR, so the run tests exactly
what `pi-keysmith` deploys -- not whatever the developer happens to have in
~/.pi/agent.

Each call gets a fresh temp workspace seeded from `evals/fixtures/<fixture>`.
The workspace is a git repo, so the provider can report what the agent
actually changed instead of trusting its prose.

Only the Python standard library is used.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
from pathlib import Path

EVALS_DIR = Path(__file__).resolve().parent.parent
FIXTURES_DIR = EVALS_DIR / "fixtures"
DIRTY_SUFFIX = ".dirty"
DEFAULT_TIMEOUT = 300
MAX_TEXT = 20000


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _snapshot(root: Path) -> dict:
    """Map relative path -> sha256 for every regular file (skips .git)."""
    out = {}
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        if rel.startswith(".git/"):
            continue
        out[rel] = _sha256(p)
    return out


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "pi-eval",
            "GIT_AUTHOR_EMAIL": "pi-eval@example.invalid",
            "GIT_COMMITTER_NAME": "pi-eval",
            "GIT_COMMITTER_EMAIL": "pi-eval@example.invalid",
        },
    )


def _seed_workspace(fixture: str, dest: Path) -> list:
    """Copy fixture, commit a clean baseline, then apply *.dirty overlays.

    Returns the list of paths that are intentionally dirty before the agent
    starts (so we can prove they survived untouched).
    """
    src = FIXTURES_DIR / fixture
    if not src.is_dir():
        raise FileNotFoundError(f"unknown fixture: {fixture}")

    dirty_overlays = []
    for p in src.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(src)
        if rel.name.endswith(DIRTY_SUFFIX):
            dirty_overlays.append(rel)
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, target)

    if (dest / ".git").exists():
        shutil.rmtree(dest / ".git")
    _git(dest, "init", "-q")
    _git(dest, "add", "-A")
    _git(dest, "commit", "-q", "-m", "baseline")

    dirty = []
    for rel in dirty_overlays:
        target = dest / rel.with_name(rel.name[: -len(DIRTY_SUFFIX)])
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src / rel, target)
        dirty.append(target.relative_to(dest).as_posix())
    return dirty


def _status(repo: Path) -> dict:
    res = _git(repo, "status", "--porcelain=v1", "-uall")
    changed, deleted, new = [], [], []
    for line in res.stdout.splitlines():
        if not line:
            continue
        code, path = line[:2], line[3:]
        if " -> " in path:  # rename: "old -> new"
            path = path.split(" -> ", 1)[1]
        if code.strip() == "??":
            new.append(path)
        elif "D" in code:
            deleted.append(path)
        else:
            changed.append(path)
    return {"changed": changed, "deleted": deleted, "new": new}


def _build_agent_config(system_prompt: str, provider: str, model: str) -> Path:
    """Create an isolated PI_CODING_AGENT_DIR holding only what pi needs."""
    cfg = Path(tempfile.mkdtemp(prefix="pi-eval-agent-"))
    cfg.chmod(stat.S_IRWXU)

    source = Path(
        os.environ.get("PI_EVAL_SOURCE_AGENT_DIR", str(Path.home() / ".pi" / "agent"))
    )
    for name in ("auth.json", "models.json"):
        candidate = source / name
        if candidate.is_file():
            shutil.copy2(candidate, cfg / name)

    settings = {}
    if provider and model:
        settings = {"defaultProvider": provider, "defaultModel": model}
    (cfg / "settings.json").write_text(
        json.dumps(settings, ensure_ascii=False), encoding="utf-8"
    )
    (cfg / "SYSTEM.md").write_text(system_prompt, encoding="utf-8")
    return cfg


def _extract(events: list) -> dict:
    """Pull final answer text and usage out of pi's NDJSON event stream."""
    texts, usage = [], {"input": 0, "output": 0, "total": 0, "cost": 0.0}
    tool_calls = 0
    stop_reason = None

    for ev in events:
        if ev.get("type") == "agent_end":
            messages = ev.get("messages") or []
        else:
            continue
        for msg in messages:
            if msg.get("role") != "assistant":
                continue
            for block in msg.get("content") or []:
                if block.get("type") == "text" and block.get("text"):
                    texts.append(block["text"])
                if block.get("type") == "toolCall":
                    tool_calls += 1
            u = msg.get("usage") or {}
            usage["input"] += int(u.get("input") or 0)
            usage["output"] += int(u.get("output") or 0)
            usage["total"] += int(u.get("totalTokens") or 0)
            usage["cost"] += float((u.get("cost") or {}).get("total") or 0)
            stop_reason = msg.get("stopReason") or stop_reason
        break

    return {
        "text": "\n".join(texts)[:MAX_TEXT],
        "usage": usage,
        "tool_calls": tool_calls,
        "stop_reason": stop_reason,
    }


def _run_verify(repo: Path, cmd: str, timeout: int) -> dict:
    if not cmd:
        return {"cmd": "", "exit": None, "output_tail": ""}
    try:
        res = subprocess.run(
            cmd,
            shell=True,
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=min(timeout, 300),
            stdin=subprocess.DEVNULL,
        )
        out = (res.stdout + res.stderr)[-2000:]
        return {"cmd": cmd, "exit": res.returncode, "output_tail": out}
    except subprocess.TimeoutExpired:
        return {"cmd": cmd, "exit": 124, "output_tail": "verify command timed out"}


def call_api(prompt, options, context):
    cfg = (options or {}).get("config", {}) or {}
    variables = (context or {}).get("vars", {}) or {}

    task = variables.get("task")
    if not task:
        return {"output": json.dumps({"ok": False, "error": "missing vars.task"})}

    fixture = variables.get("fixture", "")
    verify_cmd = variables.get("verify", "")
    timeout = int(cfg.get("timeout_s") or DEFAULT_TIMEOUT)
    provider = cfg.get("provider") or os.environ.get("PI_EVAL_PROVIDER", "")
    model = cfg.get("model") or os.environ.get("PI_EVAL_MODEL", "")
    pi_bin = cfg.get("pi_bin") or os.environ.get("PI_EVAL_BIN", "pi")

    started = time.time()
    workdir = Path(tempfile.mkdtemp(prefix="pi-eval-ws-"))
    repo = workdir / "repo"
    repo.mkdir()
    cfg_dir = None

    try:
        dirty = _seed_workspace(fixture, repo) if fixture else []
        pre = _snapshot(repo)
        cfg_dir = _build_agent_config(prompt, provider, model)

        args = [
            pi_bin,
            "-p",
            task,
            "--mode",
            "json",
            "--no-session",
            "--no-context-files",
            "--no-skills",
            "--no-extensions",
            "--no-prompt-templates",
            "--no-mcp",
            "--no-approve",
        ]
        if provider and model:
            args[1:1] = ["--provider", provider, "--model", model]

        env = {k: v for k, v in os.environ.items() if not k.startswith("PI_CODING_AGENT")}
        env["PI_CODING_AGENT_DIR"] = str(cfg_dir)

        try:
            res = subprocess.run(
                args,
                cwd=str(repo),
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
                stdin=subprocess.DEVNULL,
            )
            stdout, stderr, exit_code = res.stdout, res.stderr, res.returncode
        except subprocess.TimeoutExpired as exc:
            stdout = (exc.stdout or b"").decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = (exc.stderr or b"").decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            exit_code = 124

        events = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue

        parsed = _extract(events)
        post = _snapshot(repo)
        status = _status(repo)
        # Files that were already dirty before the run are the user's work,
        # not the agent's edits; report them separately.
        pre_dirty = set(dirty)
        status["changed"] = [p for p in status["changed"] if p not in pre_dirty]

        dirty_preserved = all(
            pre.get(path) == post.get(path) for path in dirty
        ) if dirty else True

        payload = {
            "ok": exit_code == 0,
            "exit_code": exit_code,
            "text": parsed["text"],
            "changed_files": sorted(status["changed"]),
            "deleted_files": sorted(status["deleted"]),
            "new_files": sorted(status["new"]),
            "dirty_files": sorted(pre_dirty),
            "dirty_preserved": dirty_preserved,
            "verify": _run_verify(repo, verify_cmd, timeout),
            "usage": parsed["usage"],
            "tool_calls": parsed["tool_calls"],
            "stop_reason": parsed["stop_reason"],
            "duration_ms": int((time.time() - started) * 1000),
            "stderr_tail": stderr[-2000:],
        }
        if exit_code == 124:
            payload["error"] = f"pi timed out after {timeout}s"
        return {"output": json.dumps(payload, ensure_ascii=False)}

    except FileNotFoundError as exc:
        return {
            "output": json.dumps(
                {
                    "ok": False,
                    "error": f"provider setup failed: {exc}",
                    "hint": "is `pi` on PATH? set PI_EVAL_BIN to override",
                }
            )
        }
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        if cfg_dir:
            shutil.rmtree(cfg_dir, ignore_errors=True)


def _main(argv: list) -> int:
    """CLI entry point for promptfoo's `exec:` provider.

    Usage: python3 evals/providers/pi_agent.py <prompt> <options-json> <context-json>
    """
    prompt = argv[1] if len(argv) > 1 else ""
    try:
        options = json.loads(argv[2]) if len(argv) > 2 and argv[2].strip() else {}
    except json.JSONDecodeError:
        options = {}
    try:
        context = json.loads(argv[3]) if len(argv) > 3 and argv[3].strip() else {}
    except json.JSONDecodeError:
        context = {}
    print(call_api(prompt, options, context)["output"])
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv))
