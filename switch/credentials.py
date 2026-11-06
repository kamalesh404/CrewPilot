"""Local storage for credentials issued by a Switch server."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CREDENTIALS_PATH = PROJECT_ROOT / ".switch" / "agent.json"

_KEYS = ("api_url", "agent_id", "api_key", "agent_name")


def load_saved_credentials(agent_name: str | None = None) -> dict[str, str]:
    """Return credentials saved for a specific agent, or default agent.json."""
    paths_to_check: list[Path] = []
    if agent_name:
        sanitized = re.sub(r"[^a-zA-Z0-9_.-]", "_", agent_name.strip().lower())
        paths_to_check.append(PROJECT_ROOT / ".switch" / f"{sanitized}.json")
    paths_to_check.append(CREDENTIALS_PATH)

    for path in paths_to_check:
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if isinstance(data, dict):
                creds = {
                    key: data[key].strip()
                    for key in _KEYS
                    if isinstance(data.get(key), str) and data[key].strip()
                }
                if creds.get("agent_id") and creds.get("api_key"):
                    return creds
        except (OSError, json.JSONDecodeError):
            continue
    return {}


def save_credentials(values: dict[str, str], agent_name: str | None = None) -> Path:
    """Persist credentials locally, readable only by the current user where possible."""
    credentials = {key: values[key] for key in _KEYS if values.get(key)}
    target_name = agent_name or values.get("agent_name")
    if target_name:
        sanitized = re.sub(r"[^a-zA-Z0-9_.-]", "_", target_name.strip().lower())
        target_path = PROJECT_ROOT / ".switch" / f"{sanitized}.json"
    else:
        target_path = CREDENTIALS_PATH

    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(
        json.dumps(credentials, indent=2) + "\n",
        encoding="utf-8",
    )
    try:
        os.chmod(target_path, 0o600)
    except OSError:
        pass

    # Also populate default agent.json if missing
    if target_path != CREDENTIALS_PATH and not CREDENTIALS_PATH.exists():
        try:
            CREDENTIALS_PATH.write_text(
                json.dumps(credentials, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass

    return target_path
