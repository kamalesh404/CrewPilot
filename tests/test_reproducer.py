"""Unit tests for agent.reproducer under strict isolation and security bounds."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.reproducer import (
    ReproductionResult,
    get_sanitized_env,
    run_reproduction,
)


def test_sanitized_env_removes_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-12345")
    monkeypatch.setenv("SWITCH_API_KEY", "ak_live_abcdef")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_tokensecret")
    monkeypatch.setenv("DATABASE_PASSWORD", "supersecret")
    monkeypatch.setenv("SOME_AUTH_HEADER", "bearer xyz")
    monkeypatch.setenv("SAFE_VAR", "visible-value")

    clean = get_sanitized_env()

    assert "OPENAI_API_KEY" not in clean
    assert "SWITCH_API_KEY" not in clean
    assert "GITHUB_TOKEN" not in clean
    assert "DATABASE_PASSWORD" not in clean
    assert "SOME_AUTH_HEADER" not in clean
    assert clean.get("SAFE_VAR") == "visible-value"
    assert clean.get("PYTHONIOENCODING") == "utf-8"
    assert clean.get("PYTHONUNBUFFERED") == "1"


def test_run_reproduction_custom_script_success(tmp_path: Path) -> None:
    script = "import sys\nprint('Probe passed')\nsys.exit(0)\n"
    result = run_reproduction(
        task_id="test-task-1",
        issue_source="custom-issue",
        custom_script=script,
        base_dir=tmp_path,
    )
    assert isinstance(result, ReproductionResult)
    assert result.exit_code == 0
    assert "Probe passed" in result.stdout
    assert not result.success  # For generic scripts, exit 0 is not a bug reproduction


def test_run_reproduction_custom_script_failure(tmp_path: Path) -> None:
    script = "import sys\nprint('Intentional failure')\nsys.exit(1)\n"
    result = run_reproduction(
        task_id="test-task-2",
        issue_source="custom-issue",
        custom_script=script,
        base_dir=tmp_path,
    )
    assert result.exit_code == 1
    assert "Intentional failure" in result.stdout
    assert result.success  # For generic scripts, non-zero is reproduction


def test_run_reproduction_timeout(tmp_path: Path) -> None:
    script = "import time\ntime.sleep(5)\n"
    result = run_reproduction(
        task_id="test-task-3",
        issue_source="custom-issue",
        custom_script=script,
        timeout=0.2,
        base_dir=tmp_path,
    )
    assert result.exit_code == -1
    assert "timed out" in result.stderr


def test_run_reproduction_requests_7634(tmp_path: Path) -> None:
    result = run_reproduction(
        task_id="test-task-7634",
        issue_source="psf/requests#7634",
        base_dir=tmp_path,
    )
    assert result.exit_code == 1
    assert result.success is True
    assert "AttributeError" in result.output
    assert "psf/requests#7634 reproduced" in result.summary
