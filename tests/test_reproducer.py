"""Unit tests for agent.reproducer under strict containment and evidence preservation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.reproducer import (
    ReproductionResult,
    get_sanitized_env,
    parse_issue_identity,
    run_reproduction,
)


def test_parse_issue_identity() -> None:
    assert parse_issue_identity("psf/requests#7634") == ("psf", "requests", "7634")
    assert parse_issue_identity("https://github.com/psf/requests/issues/7634") == (
        "psf",
        "requests",
        "7634",
    )
    assert parse_issue_identity("PSF/REQUESTS#7634") == ("psf", "requests", "7634")
    assert parse_issue_identity("unformatted text") is None


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


def test_run_reproduction_unsupported_issue_fails_closed(tmp_path: Path) -> None:
    # Requests issue with different number
    result = run_reproduction(
        task_id="test-task-wrong-num",
        issue_source="psf/requests#1234",
        base_dir=tmp_path,
    )
    assert result.success is False
    assert result.exit_code == -3
    assert "No verified reproduction probe exists" in result.summary

    # Different repository
    result2 = run_reproduction(
        task_id="test-task-other-repo",
        issue_source="pallets/flask#7634",
        base_dir=tmp_path,
    )
    assert result2.success is False
    assert result2.exit_code == -3


def test_run_reproduction_preserves_persistent_artifacts(tmp_path: Path) -> None:
    result = run_reproduction(
        task_id="test-task-persistence",
        issue_source="psf/requests#7634",
        base_dir=tmp_path,
    )
    assert result.artifact_path is not None
    probe_path = Path(result.artifact_path)

    # Artifact must remain on disk AFTER run_reproduction finishes
    assert probe_path.exists()
    assert probe_path.is_file()
    content = probe_path.read_text(encoding="utf-8")
    assert "requests.Request" in content

    # Metadata file must be present with required diagnostic fields
    metadata_path = probe_path.parent / "metadata.json"
    assert metadata_path.exists()
    meta = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert meta["task_id"] == "test-task-persistence"
    assert meta["target_issue"] == "psf/requests#7634"
    assert meta["exit_code"] == 1
    assert "python_version" in meta
    assert "requests_version" in meta
    assert "urllib3_version" in meta
    assert "platform" in meta
    assert "duration_s" in meta
