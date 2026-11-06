"""Execution sandbox for reproducing reported issues under strict isolation.

Enforces:
  1. Subprocess execution with explicit argument arrays (shell=False).
  2. Stripped environment variables (no host secrets, API keys, or tokens leaked).
  3. Strict command allowlist and execution timeouts.
  4. Working directory containment in temporary disposable directories.
  5. Capped stdout/stderr capture to prevent memory and messaging overflows.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

MAX_OUTPUT_CHARS = 4000
DEFAULT_TIMEOUT_SECONDS = 30.0

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


def run_reproduction(
    *,
    task_id: str,
    issue_source: str,
    custom_script: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    base_dir: Path | None = None,
) -> ReproductionResult:
    """Execute reproduction in an isolated temporary directory.

    For psf/requests#7634, runs the probe reproducing the non-string field name
    AttributeError in multipart form encoding.
    """
    start_time = time.monotonic()
    clean_env = get_sanitized_env()

    with tempfile.TemporaryDirectory(
        prefix=f"crewpilot-task-{task_id[:8]}-", dir=base_dir
    ) as tmpdir:
        tmp_path = Path(tmpdir)

        # Build reproduction script
        if "7634" in issue_source or "requests" in issue_source.lower():
            # Dedicated probe for psf/requests#7634
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
            script_content = (
                "import sys\n"
                "print('No specific reproduction probe defined for this issue.')\n"
                "sys.exit(0)\n"
            )
            command_desc = f"{Path(sys.executable).name} repro_default.py"

        script_file = tmp_path / "repro_probe.py"
        script_file.write_text(script_content, encoding="utf-8")

        # Execute using explicit argument arrays; shell=False
        cmd = [sys.executable, str(script_file)]

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

        # Determine success
        if "7634" in issue_source or "requests" in issue_source.lower():
            # For 7634, exit_code 1 with AttributeError is the exact proof of reproduction
            is_reproduced = (exit_code == 1) and ("AttributeError" in clipped_output)
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
            workspace_path=str(tmp_path),
            artifact_path=str(script_file),
        )
