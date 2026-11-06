"""Entrypoint for CrewPilot running as a native Switch multi-agent squad.

Supports running:
  - Complete 3-agent squad concurrently: `python main.py --role squad` (default)
  - Individual role instances:
      `python main.py --role triager`
      `python main.py --role builder`
      `python main.py --role reviewer`

Wiring:
    Settings -> TaskStore (SQLite durable state)
             -> SwitchAgentClient (SSE + HTTP operations)
             -> Connectors (TriageConnector, ReproConnector, ReviewerConnector)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from agent.triage_agent import TriageAgent
from config import ConfigError, Settings
from storage.task_store import TaskStore
from switch.agent_client import SwitchAgentClient
from switch.repro_connector import SwitchReproConnector
from switch.reviewer_connector import SwitchReviewerConnector
from switch.switch_connector import SwitchTriageConnector

logger = logging.getLogger("crewpilot")

DEFAULT_TRIAGER_NAME = "crewpilot-triager"
DEFAULT_BUILDER_NAME = "crewpilot-repro-builder"
DEFAULT_REVIEWER_NAME = "crewpilot-reviewer"
DEFAULT_DB_PATH = "crewpilot.db"


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s]: %(message)s",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python main.py",
        description="Run CrewPilot multi-agent bug investigation squad on Switch.",
    )
    parser.add_argument(
        "--role",
        choices=["squad", "triager", "builder", "reviewer"],
        default="squad",
        help="Which agent role to run (default: squad - runs all three concurrently)",
    )
    parser.add_argument(
        "--db",
        default=DEFAULT_DB_PATH,
        help=f"Path to SQLite task store database (default: {DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--triager-name",
        default=DEFAULT_TRIAGER_NAME,
        help="Agent identifier for Triager",
    )
    parser.add_argument(
        "--builder-name",
        default=DEFAULT_BUILDER_NAME,
        help="Agent identifier for Reproducer/Builder",
    )
    parser.add_argument(
        "--reviewer-name",
        default=DEFAULT_REVIEWER_NAME,
        help="Agent identifier for Reviewer",
    )
    return parser


async def _run_triager(
    store: TaskStore,
    triager_name: str,
    builder_name: str,
) -> None:
    try:
        settings = Settings.from_env(agent_name=triager_name, require_llm=True)
    except ConfigError as exc:
        logger.error("Triager configuration error: %s", exc)
        return

    triage_agent = TriageAgent.from_settings(settings)
    client = SwitchAgentClient(
        api_url=settings.switch_api_url,
        agent_id=settings.switch_agent_id,
        api_key=settings.switch_api_key,
        agent_name=settings.switch_agent_name,
    )
    connector = SwitchTriageConnector(
        client=client,
        triage_agent=triage_agent,
        agent_name=settings.switch_agent_name,
        github_token=settings.github_token,
        store=store,
        reproducer_agent_name=builder_name,
    )
    logger.info("Starting Triager agent '%s' on Switch...", settings.switch_agent_name)
    await client.run(connector.handle_event)


async def _run_builder(
    store: TaskStore,
    builder_name: str,
    reviewer_name: str,
) -> None:
    try:
        settings = Settings.from_env(agent_name=builder_name, require_llm=False)
    except ConfigError as exc:
        logger.error("Builder configuration error: %s", exc)
        return

    client = SwitchAgentClient(
        api_url=settings.switch_api_url,
        agent_id=settings.switch_agent_id,
        api_key=settings.switch_api_key,
        agent_name=settings.switch_agent_name,
    )
    connector = SwitchReproConnector(
        client=client,
        store=store,
        agent_name=settings.switch_agent_name,
        reviewer_agent_name=reviewer_name,
    )
    logger.info("Starting Builder agent '%s' on Switch...", settings.switch_agent_name)
    await client.run(connector.handle_event)


async def _run_reviewer(
    store: TaskStore,
    reviewer_name: str,
) -> None:
    try:
        settings = Settings.from_env(agent_name=reviewer_name, require_llm=False)
    except ConfigError as exc:
        logger.error("Reviewer configuration error: %s", exc)
        return

    client = SwitchAgentClient(
        api_url=settings.switch_api_url,
        agent_id=settings.switch_agent_id,
        api_key=settings.switch_api_key,
        agent_name=settings.switch_agent_name,
    )
    connector = SwitchReviewerConnector(
        client=client,
        store=store,
        agent_name=settings.switch_agent_name,
    )
    logger.info("Starting Reviewer agent '%s' on Switch...", settings.switch_agent_name)
    await client.run(connector.handle_event)


async def run(args: argparse.Namespace) -> None:
    db_path = Path(args.db)
    store = TaskStore(db_path)
    logger.info("Initialized durable SQLite TaskStore at: %s", db_path.resolve())

    role = args.role
    if role == "triager":
        await _run_triager(store, args.triager_name, args.builder_name)
    elif role == "builder":
        await _run_builder(store, args.builder_name, args.reviewer_name)
    elif role == "reviewer":
        await _run_reviewer(store, args.reviewer_name)
    elif role == "squad":
        logger.info(
            "Launching full CrewPilot Squad (Triager='%s', Builder='%s', Reviewer='%s')...",
            args.triager_name,
            args.builder_name,
            args.reviewer_name,
        )
        tasks = [
            asyncio.create_task(
                _run_triager(store, args.triager_name, args.builder_name),
                name="triager",
            ),
            asyncio.create_task(
                _run_builder(store, args.builder_name, args.reviewer_name),
                name="builder",
            ),
            asyncio.create_task(
                _run_reviewer(store, args.reviewer_name),
                name="reviewer",
            ),
        ]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for task in done:
            if task.exception():
                exc = task.exception()
                logger.error("Agent '%s' failed with error: %s", task.get_name(), exc)
                for p in pending:
                    p.cancel()
                raise exc


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    configure_logging("INFO")
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nCrewPilot Squad stopped.")
    except Exception as exc:
        print(f"\nExecution error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
