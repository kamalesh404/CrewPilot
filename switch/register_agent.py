"""Register CrewPilot agents on a Switch server.

Usage:
    # Register the complete 3-agent squad:
    python -m switch.register_agent --role squad

    # Register an individual agent role:
    python -m switch.register_agent --role triager
    python -m switch.register_agent --role builder
    python -m switch.register_agent --role reviewer

    # Or custom registration:
    python -m switch.register_agent --name custom-agent --description "..."

The registration token belongs to a Switch user and becomes the agent's owner.
Saves API credentials per-agent into .switch/<sanitized_name>.json and .switch/agent.json.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys

import aiohttp
from dotenv import load_dotenv

from switch.credentials import load_saved_credentials, save_credentials

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

SQUAD_ROLES: dict[str, dict[str, str]] = {
    "triager": {
        "name": "crewpilot-triager",
        "description": (
            "Triages GitHub issues or bug reports into structured categories, severity, "
            "difficulty, and an actionable fix plan, then binds human maintainer scope decisions."
        ),
    },
    "builder": {
        "name": "crewpilot-repro-builder",
        "description": (
            "Executes isolated bug reproduction probes in a secure, credential-stripped "
            "sandbox and records reproduction evidence in the durable SQLite store."
        ),
    },
    "reviewer": {
        "name": "crewpilot-reviewer",
        "description": (
            "Independently audits reproduction evidence, checks security containment, "
            "and authorizes Draft PR publication upon maintainer confirmation."
        ),
    },
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m switch.register_agent",
        description="Register CrewPilot agents as always-on custom agents on Switch.",
    )
    parser.add_argument(
        "--role",
        choices=["squad", "triager", "builder", "reviewer"],
        help="Register a predefined CrewPilot squad role, or 'squad' to register all three",
    )
    parser.add_argument("--name", help="Custom agent identifier (lowercase, e.g. crewpilot-triager)")
    parser.add_argument("--description", help="What the agent does, shown in Switch")
    parser.add_argument("--api-url", help="Switch bridge base URL, e.g. http://localhost:8000")
    parser.add_argument(
        "--token",
        help="Switch registration token (default: SWITCH_REGISTRATION_TOKEN)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace an existing agent with the same name and issue a new API key",
    )
    return parser


async def _register_single(
    *,
    name: str,
    description: str,
    api_url: str,
    token: str,
    overwrite: bool,
) -> int:
    if not NAME_RE.fullmatch(name):
        print(
            f"Invalid agent name '{name}'. Use lowercase letters, digits, '.', '_' or '-', "
            "starting with a letter or digit.",
            file=sys.stderr,
        )
        return 2

    payload = {
        "name": name,
        "description": description,
        "connector_type": "custom",
        "overwrite": overwrite,
        "integration_profile": {
            "connection_model": "always_on",
            "message_exchange": True,
            "pre_invocation_mediation": [],
            "post_invocation_mediation": [],
            "event_reporting": [],
        },
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    timeout = aiohttp.ClientTimeout(total=30)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{api_url}/agents", json=payload, headers=headers
            ) as response:
                text = await response.text()
                status = response.status
    except aiohttp.ClientError as exc:
        print(f"Could not reach Switch at {api_url}: {exc}", file=sys.stderr)
        return 1

    if status in {401, 403}:
        print(
            "Switch rejected the registration token. Check SWITCH_REGISTRATION_TOKEN.",
            file=sys.stderr,
        )
        return 1
    if status == 409:
        print(
            f"An agent named '{name}' already exists on this server. "
            "Use --overwrite to replace it (this issues a new API key).",
            file=sys.stderr,
        )
        return 1
    if status == 404:
        print(
            f"Switch returned 404. Is {api_url} the agent bridge/API URL (not Gateway UI URL)?",
            file=sys.stderr,
        )
        return 1
    if status >= 400:
        print(f"Registration failed (HTTP {status}): {text[:500]}", file=sys.stderr)
        return 1

    try:
        body = json.loads(text)
        agent_id = body["id"]
        api_key = body["api_key"]
    except (json.JSONDecodeError, KeyError, TypeError):
        print(f"Unexpected registration response: {text[:500]}", file=sys.stderr)
        return 1

    path = save_credentials(
        {
            "api_url": api_url,
            "agent_id": agent_id,
            "api_key": api_key,
            "agent_name": name,
        },
        agent_name=name,
    )

    print(f"Registered Switch agent '{name}' (id: {agent_id}).")
    print(f"API credentials saved to {path}.")
    return 0


async def _register(args: argparse.Namespace) -> int:
    saved = load_saved_credentials()
    api_url = (
        args.api_url
        or os.getenv("SWITCH_API_URL")
        or saved.get("api_url")
        or "http://localhost:8000"
    ).strip().rstrip("/")
    token = (args.token or os.getenv("SWITCH_REGISTRATION_TOKEN") or "").strip()

    if not (api_url.startswith("http://") or api_url.startswith("https://")):
        print("--api-url must start with http:// or https://.", file=sys.stderr)
        return 2
    if not token:
        print(
            "A registration token is required. Set SWITCH_REGISTRATION_TOKEN in .env "
            "or pass --token.",
            file=sys.stderr,
        )
        return 2

    if args.role == "squad":
        print(f"Registering full CrewPilot Squad on Switch ({api_url})...")
        for role_key, role_info in SQUAD_ROLES.items():
            res = await _register_single(
                name=role_info["name"],
                description=role_info["description"],
                api_url=api_url,
                token=token,
                overwrite=bool(args.overwrite),
            )
            if res != 0:
                print(f"Failed to register role '{role_key}'.", file=sys.stderr)
                return res
        print("\nAll 3 CrewPilot agents registered successfully!")
        print("To invite them to a Discord channel in Switch:")
        print("  !invite-agent @crewpilot-triager")
        print("  !invite-agent @crewpilot-repro-builder")
        print("  !invite-agent @crewpilot-reviewer")
        print("\nTo start the squad:")
        print("  python main.py --role squad")
        return 0

    if args.role in SQUAD_ROLES:
        role_info = SQUAD_ROLES[args.role]
        name = role_info["name"]
        description = args.description or role_info["description"]
    else:
        name = (args.name or os.getenv("SWITCH_AGENT_NAME") or saved.get("agent_name") or "crewpilot").strip()
        description = (
            args.description
            or os.getenv("SWITCH_AGENT_DESCRIPTION")
            or SQUAD_ROLES["triager"]["description"]
        ).strip()

    return await _register_single(
        name=name,
        description=description,
        api_url=api_url,
        token=token,
        overwrite=bool(args.overwrite),
    )


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = _parser().parse_args(argv)
    try:
        return asyncio.run(_register(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
