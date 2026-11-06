"""Reproducer / Builder agent running as a native agent inside Switch rooms.

Listens for handoffs from the Triager (or direct maintainer requests):
  `@crewpilot-repro-builder reproduce task <task-id>`

Executes reproduction in an isolated sandbox with sanitized environment
(no secrets/tokens leaked, shell=False, strict timeouts), saves evidence
to the durable SQLite store, and hands off to the Reviewer:
  `@crewpilot-reviewer review task <task-id>`
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import OrderedDict
from typing import Any

from agent.reproducer import ReproductionResult, run_reproduction
from storage.task_store import (
    TaskNotFoundError,
    TaskStateError,
    TaskStatus,
    TaskStore,
)
from switch.agent_client import SwitchAgentClient, SwitchAuthError, SwitchError

logger = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 1_900
MAX_SEEN_MESSAGES = 500
MAX_ERROR_CHARS = 400

REPRO_RE = re.compile(
    r"^(?:please\s+)?(?:reproduce|repro|run\s+repro|investigate|test)(?:\s+task)?\s+([0-9a-fA-F-]{8,36})[.!]*$",
    re.IGNORECASE,
)
STATUS_RE = re.compile(
    r"^(?:please\s+)?(?:status|check|info)(?:\s+task)?\s+([0-9a-fA-F-]{8,36})[.!]*$",
    re.IGNORECASE,
)
HELP_RE = re.compile(r"^/?(?:help|usage|commands)[.!]?$", re.IGNORECASE)


def _strip_mentions(text: str) -> str:
    """Remove @mentions (e.g. '@crewpilot-repro-builder' or '<@123>') from message text."""
    return re.sub(r"<@[^>]+>|@[\w-]+", "", text).strip()


def _clip(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3] + "..."


class SwitchReproConnector:
    """Routes addressed Switch messages to the reproduction engine and durable store."""

    def __init__(
        self,
        *,
        client: SwitchAgentClient,
        store: TaskStore,
        agent_name: str = "crewpilot-repro-builder",
        reviewer_agent_name: str = "crewpilot-reviewer",
    ) -> None:
        self._client = client
        self._store = store
        self.agent_name = agent_name
        self._reviewer_agent_name = reviewer_agent_name
        self._seen_messages: OrderedDict[str, None] = OrderedDict()

    async def handle_event(self, event: dict[str, Any]) -> None:
        """Handle one Switch event. Only addressed messages do work."""
        if event.get("type") != "message":
            return

        payload = event.get("payload")
        if not isinstance(payload, dict) or payload.get("addressed") is False:
            return

        room_id = str(event.get("room_id") or "")
        body = str(payload.get("body") or "").strip()
        if not room_id or not body:
            return

        message_id = str(payload.get("id") or "")
        if message_id and message_id in self._seen_messages:
            logger.debug("Skipping duplicate message %s", message_id)
            return
        if message_id:
            self._record_seen(message_id)

        thread_id = payload.get("thread_id")
        if thread_id is not None:
            thread_id = str(thread_id).strip() or None

        sender = payload.get("sender") if isinstance(payload.get("sender"), dict) else {}
        sender_id = str(sender.get("id") or "")
        sender_name = str(sender.get("name") or sender.get("username") or "user")

        stripped = _strip_mentions(body)

        if HELP_RE.match(stripped):
            await self._send(room_id, self._help_text(), thread_id)
            return

        repro_match = REPRO_RE.match(stripped)
        if repro_match:
            task_id = repro_match.group(1).lower()
            await self._handle_reproduce(
                room_id=room_id,
                thread_id=thread_id,
                sender_id=sender_id,
                sender_name=sender_name,
                task_id=task_id,
            )
            return

        status_match = STATUS_RE.match(stripped)
        if status_match:
            task_id = status_match.group(1).lower()
            await self._handle_status(
                room_id=room_id,
                thread_id=thread_id,
                task_id=task_id,
            )
            return

        # Check if the user mentioned reproduce/repro without a valid task ID
        if re.search(r"\b(?:reproduce|repro|investigate)\b", stripped, re.IGNORECASE):
            await self._send(
                room_id,
                f"Please specify the exact task ID to reproduce: "
                f"reply '@{self.agent_name} reproduce task <task-id>'.",
                thread_id,
            )
            return

        # Any other message directed to this agent gets help text
        await self._send(room_id, self._help_text(), thread_id)

    def _record_seen(self, message_id: str) -> None:
        self._seen_messages[message_id] = None
        if len(self._seen_messages) > MAX_SEEN_MESSAGES:
            self._seen_messages.popitem(last=False)

    async def _send(
        self,
        room_id: str,
        text: str,
        thread_id: str | None = None,
    ) -> None:
        clipped = _clip(text, MAX_MESSAGE_CHARS)
        try:
            await self._client.post_message(room_id, clipped, thread_id)
        except SwitchAuthError:
            raise
        except SwitchError as exc:
            logger.error("Could not post message to room %s: %s", room_id, exc)

    def _help_text(self) -> str:
        return (
            f"**CrewPilot Reproducer / Builder (`@{self.agent_name}`)**\n"
            "I execute isolated bug reproduction probes in a secure, credential-stripped sandbox.\n\n"
            "**Commands:**\n"
            f"- `@{self.agent_name} reproduce task <task-id>` — Run reproduction probe for an approved task.\n"
            f"- `@{self.agent_name} status task <task-id>` — View task evidence and status.\n"
            "- `help` — Show this message."
        )

    async def _handle_status(
        self,
        *,
        room_id: str,
        thread_id: str | None,
        task_id: str,
    ) -> None:
        task = self._store.get_task(task_id)
        if task is None:
            await self._send(room_id, f"Task '{task_id}' was not found.", thread_id)
            return

        evidence_list = self._store.list_evidence(task_id)
        evidence_summary = (
            f"{len(evidence_list)} evidence item(s) recorded."
            if evidence_list
            else "No evidence recorded yet."
        )
        msg = (
            f"**Task Status:** `{task.status}`\n"
            f"- **Issue:** {task.issue_source}\n"
            f"- **Requester:** {task.requester_name}\n"
            f"- **Evidence:** {evidence_summary}"
        )
        await self._send(room_id, msg, thread_id)

    async def _handle_reproduce(
        self,
        *,
        room_id: str,
        thread_id: str | None,
        sender_id: str,
        sender_name: str,
        task_id: str,
    ) -> None:
        task = self._store.get_task(task_id)
        if task is None:
            await self._send(
                room_id,
                f"Task '{task_id}' was not found. Please verify the task ID.",
                thread_id,
            )
            return

        if task.room_id != room_id:
            await self._send(
                room_id,
                f"Task '{task_id}' belongs to another room.",
                thread_id,
            )
            return

        if task.status == TaskStatus.AWAITING_APPROVAL.value:
            await self._send(
                room_id,
                f"Task '{task_id}' is still awaiting maintainer approval. "
                "A human maintainer must approve the triage plan before reproduction can start.",
                thread_id,
            )
            return

        if task.status in {TaskStatus.CHANGES_REQUESTED.value, TaskStatus.CANCELLED.value}:
            await self._send(
                room_id,
                f"Task '{task_id}' is in '{task.status}' state and cannot be reproduced.",
                thread_id,
            )
            return

        # Transition task status to REPRODUCING
        try:
            self._store.update_task_status(
                task_id,
                TaskStatus.REPRODUCING.value,
                actor_id=self.agent_name,
                reason=f"Reproduction probe launched by {sender_name}",
            )
        except (TaskStateError, TaskNotFoundError) as exc:
            logger.warning("Could not transition task %s: %s", task_id, exc)

        await self._send(
            room_id,
            f"🔬 Reproducing bug for task `{task_id}` ({task.issue_source}) "
            "in isolated sandbox environment (shell=False, stripped credentials)...",
            thread_id,
        )

        try:
            result = await asyncio.to_thread(
                run_reproduction,
                task_id=task.task_id,
                issue_source=task.issue_source,
            )
        except Exception as exc:
            logger.exception("Reproduction sandbox crashed for task %s", task_id)
            await self._send(
                room_id,
                f"Reproduction sandbox encountered an error: {_clip(str(exc), MAX_ERROR_CHARS)}",
                thread_id,
            )
            return

        # Persist evidence in durable SQLite store
        self._store.add_evidence(
            task_id=task_id,
            evidence_type="reproduction_probe",
            description=result.summary,
            command=result.command,
            output=result.output,
            artifact_path=result.artifact_path,
            actor_id=self.agent_name,
        )

        if result.success:
            try:
                self._store.update_task_status(
                    task_id,
                    TaskStatus.UNDER_REVIEW.value,
                    actor_id=self.agent_name,
                    reason="Reproduction probe confirmed bug; submitted to Reviewer",
                )
            except Exception as exc:
                logger.warning("Status update to under_review failed: %s", exc)

            report = self._format_success_report(task_id, task.issue_source, result)
            await self._send(room_id, report, thread_id)

            # Emit handoff to Reviewer
            handoff = f"@{self._reviewer_agent_name} review task {task_id}"
            await self._send(room_id, handoff, thread_id)
            logger.info("Dispatched task %s handoff to %s", task_id, self._reviewer_agent_name)
        else:
            try:
                self._store.update_task_status(
                    task_id,
                    TaskStatus.CHANGES_REQUESTED.value,
                    actor_id=self.agent_name,
                    reason="Reproduction probe failed to trigger expected error",
                )
            except Exception as exc:
                logger.warning("Status update failed: %s", exc)

            report = self._format_failure_report(task_id, task.issue_source, result)
            await self._send(room_id, report, thread_id)

    def _format_success_report(
        self,
        task_id: str,
        issue_source: str,
        result: ReproductionResult,
    ) -> str:
        out_clipped = _clip(result.output, 800)
        return (
            f"### 🔬 Bug Reproduction Confirmed\n"
            f"- **Task ID:** `{task_id}`\n"
            f"- **Target:** {issue_source}\n"
            f"- **Command:** `{result.command}` (exit `{result.exit_code}`)\n"
            f"- **Duration:** {result.duration_s:.2f}s\n"
            f"- **Summary:** {result.summary}\n\n"
            f"```\n{out_clipped}\n```\n\n"
            "Evidence recorded in durable SQLite audit store. Handing off to Reviewer..."
        )

    def _format_failure_report(
        self,
        task_id: str,
        issue_source: str,
        result: ReproductionResult,
    ) -> str:
        out_clipped = _clip(result.output, 600)
        return (
            f"### ⚠️ Bug Reproduction Failed\n"
            f"- **Task ID:** `{task_id}`\n"
            f"- **Target:** {issue_source}\n"
            f"- **Exit Code:** `{result.exit_code}`\n"
            f"- **Summary:** {result.summary}\n\n"
            f"```\n{out_clipped}\n```\n\n"
            "Reproduction probe did not produce the expected failure. Human maintainer review required."
        )
