"""Shared fixtures for pi-keysmith tests."""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CLI_PATH = REPO_ROOT / "pi-keysmith.py"


def load_cli_module():
    """Import pi-keysmith.py as a module (its filename is not importable)."""
    spec = importlib.util.spec_from_file_location("pi_keysmith_under_test", str(CLI_PATH))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["pi_keysmith_under_test"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def cli():
    return load_cli_module()


@pytest.fixture()
def pi_agent(tmp_path: Path) -> Path:
    """A fresh, empty pi agent config directory."""
    agent = tmp_path / ".pi" / "agent"
    agent.mkdir(parents=True)
    return agent


def run_cli(args: list, *, expect_code: int = 0) -> tuple[int, str, str]:
    """Run pi-keysmith.py as a subprocess and return (code, stdout, stderr)."""
    import subprocess

    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    proc = subprocess.run(
        [sys.executable, str(CLI_PATH), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
    )
    if expect_code is not None and proc.returncode != expect_code:
        raise AssertionError(
            f"expected exit {expect_code}, got {proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
    return proc.returncode, proc.stdout, proc.stderr


@pytest.fixture()
def run():
    return run_cli
