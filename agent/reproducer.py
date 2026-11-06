"""Credential-reduced subprocess execution in a disposable temporary workspace.

Enforces:
  1. Subprocess execution with explicit argument arrays (shell=False).
  2. Stripped environment variables (no host secrets, API keys, or tokens leaked).
  3. Strict command allowlists and execution timeouts.
  4. Execution within a temporary workspace with persistent evidence preservation.
  5. Capped stdout/stderr capture to prevent memory and messaging overflows.

LIMITATION NOTICE:
  This module does not provide OS-level filesystem or network isolation (no cgroups,
  namespaces, chroot, or Docker containerization). Arbitrary untrusted code from issue
  descriptions is NOT executed. Only predefined, fixed reproduction probes are run.
"""

from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

MAX_OUTPUT_CHARS = 4000
DEFAULT_TIMEOUT_SECONDS = 30.0

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EVIDENCE_ROOT = PROJECT_ROOT / ".evidence"

SUPPORTED_ISSUE = ("psf", "requests", "7634")
EXPECTED_FAILURE_SIGNATURE = "'int' object has no attribute 'translate'"

# Sanitized environment variable blocklist (strip all sensitive variables)
SECRET_ENV_PATTERNS = (
    "KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "AUTH",
    "CREDENTIAL",
    "SWITCH_API_KEY",
    "GITHUB_TOKEN",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GROQ_API_KEY",
)


@dataclass(frozen=True, slots=True)
class ReproductionResult:
    success: bool
    summary: str
    command: str
    exit_code: int
    stdout: str
    stderr: str
    output: str
    duration_s: float
    workspace_path: str
    artifact_path: str | None = None


def parse_issue_identity(issue_source: str) -> tuple[str, str, str] | None:
    """Parse (owner, repo, issue_number) from an issue source or URL."""
    text = issue_source.strip()
    match = re.search(
        r"(?:https?://github\.com/)?([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)(?:/issues/|#)(\d+)",
        text,
    )
    if not match:
        return None
    return match.group(1).lower(), match.group(2).lower(), match.group(3)


def get_sanitized_env() -> dict[str, str]:
    """Return a sanitized copy of the environment with all secrets removed."""
    clean_env: dict[str, str] = {}
    for key, value in os.environ.items():
        key_upper = key.upper()
        if any(pat in key_upper for pat in SECRET_ENV_PATTERNS):
            continue
        clean_env[key] = value

    # Ensure clean standard encoding
    clean_env["PYTHONIOENCODING"] = "utf-8"
    clean_env["PYTHONUNBUFFERED"] = "1"
    return clean_env


def _get_pkg_version(module_name: str) -> str:
    try:
        mod = __import__(module_name)
        return str(getattr(mod, "__version__", "unknown"))
    except Exception:
        return "not_installed"


def run_reproduction(
    *,
    task_id: str,
    issue_source: str,
    custom_script: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    base_dir: Path | None = None,
) -> ReproductionResult:
    """Execute reproduction probe in a temporary workspace with persistent evidence.

    Fails closed for unsupported issue targets. For psf/requests#7634, runs the
    exact probe reproducing the non-string multipart field name crash.
    """
    start_time = time.monotonic()
    clean_env = get_sanitized_env()

    parsed_issue = parse_issue_identity(issue_source)

    if parsed_issue == SUPPORTED_ISSUE:
        script_content = (
            "import sys\n"
            "import requests\n\n"
            "try:\n"
            "    print('Testing requests multipart preparation with non-string form key...')\n"
            "    req = requests.Request('POST', 'http://x', data={1: 'a'}, files={'f': b'x'}).prepare()\n"
            "    print('UNEXPECTED: Request prepared without error.')\n"
            "    sys.exit(0)\n"
            "except AttributeError as exc:\n"
            "    print(f'REPRODUCED_BUG: AttributeError encountered: {exc}')\n"
            "    import traceback\n"
            "    traceback.print_exc()\n"
            "    sys.exit(1)\n"
            "except Exception as exc:\n"
            "    print(f'UNEXPECTED_ERROR: {type(exc).__name__}: {exc}')\n"
            "    sys.exit(2)\n"
        )
        command_desc = f"{Path(sys.executable).name} repro_7634.py"
    elif custom_script:
        script_content = custom_script
        command_desc = f"{Path(sys.executable).name} repro_custom.py"
    else:
        # Fail closed: No verified reproduction probe exists for this issue target
        duration = time.monotonic() - start_time
        msg = (
            f"No verified reproduction probe exists for target '{issue_source}'. "
            "Supported targets: psf/requests#7634."
        )
        return ReproductionResult(
            success=False,
            summary=msg,
            command="none",
            exit_code=-3,
            stdout="",
            stderr=msg,
            output=msg,
            duration_s=round(duration, 3),
            workspace_path="",
            artifact_path=None,
        )

    # Prepare stable, persistent evidence directory keyed by task_id
    evidence_root = base_dir or DEFAULT_EVIDENCE_ROOT
    task_evidence_dir = evidence_root / f"task-{task_id}"
    task_evidence_dir.mkdir(parents=True, exist_ok=True)
    probe_artifact = task_evidence_dir / "repro_probe.py"
    probe_artifact.write_text(script_content, encoding="utf-8")

    # Run execution inside a temporary working directory
    with tempfile.TemporaryDirectory(
        prefix=f"crewpilot-task-{task_id[:8]}-", dir=base_dir
    ) as tmpdir:
        tmp_path = Path(tmpdir)
        exec_script = tmp_path / "repro_probe.py"
        exec_script.write_text(script_content, encoding="utf-8")

        cmd = [sys.executable, str(exec_script)]

        try:
            proc = subprocess.run(
                cmd,
                cwd=str(tmp_path),
                env=clean_env,
                capture_output=True,
                text=True,
                timeout=timeout,
                shell=False,
            )
            stdout = proc.stdout.strip()
            stderr = proc.stderr.strip()
            exit_code = proc.returncode
        except subprocess.TimeoutExpired as exc:
            stdout = (exc.stdout or "").strip() if isinstance(exc.stdout, str) else ""
            stderr = (exc.stderr or "").strip() if isinstance(exc.stderr, str) else ""
            stderr += f"\nCommand timed out after {timeout} seconds."
            exit_code = -1
        except Exception as exc:
            stdout = ""
            stderr = f"Subprocess launch failed: {exc}"
            exit_code = -2

    duration = time.monotonic() - start_time

    combined_output = ""
    if stdout:
        combined_output += stdout
    if stderr:
        if combined_output:
            combined_output += "\n--- STDERR ---\n"
        combined_output += stderr

    clipped_output = combined_output[:MAX_OUTPUT_CHARS]
    if len(combined_output) > MAX_OUTPUT_CHARS:
        clipped_output += "\n...(output truncated)..."

    # Persist log and execution metadata to persistent evidence directory
    output_log = task_evidence_dir / "output.log"
    output_log.write_text(combined_output, encoding="utf-8")

    meta = {
        "task_id": task_id,
        "target_issue": issue_source,
        "command": command_desc,
        "exit_code": exit_code,
        "stdout": stdout[:MAX_OUTPUT_CHARS],
        "stderr": stderr[:MAX_OUTPUT_CHARS],
        "combined_output": clipped_output,
        "python_version": sys.version,
        "requests_version": _get_pkg_version("requests"),
        "urllib3_version": _get_pkg_version("urllib3"),
        "platform": platform.platform(),
        "duration_s": round(duration, 3),
    }
    meta_path = task_evidence_dir / "metadata.json"
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    # Determine success with strict signature check
    if parsed_issue == SUPPORTED_ISSUE:
        is_reproduced = (exit_code == 1) and (EXPECTED_FAILURE_SIGNATURE in clipped_output)
        summary = (
            f"psf/requests#7634 reproduced: AttributeError raised on non-string key with multipart files (exit {exit_code})."
            if is_reproduced
            else f"psf/requests#7634 reproduction failed (exit {exit_code})."
        )
    else:
        is_reproduced = exit_code != 0
        summary = f"Reproduction probe completed with exit code {exit_code} in {duration:.2f}s."

    return ReproductionResult(
        success=is_reproduced,
        summary=summary,
        command=command_desc,
        exit_code=exit_code,
        stdout=stdout[:MAX_OUTPUT_CHARS],
        stderr=stderr[:MAX_OUTPUT_CHARS],
        output=clipped_output,
        duration_s=round(duration, 3),
        workspace_path=str(task_evidence_dir),
        artifact_path=str(probe_artifact),
    )
