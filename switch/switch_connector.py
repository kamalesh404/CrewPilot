"""Triage workflow running as a native agent inside Switch rooms.

Chat text arrives from Switch over SSE; results are posted back with Switch
operations. Switch's Discord bridge mirrors both directions to the channel, so
this module never talks to Discord directly.

Human approvals use exact task IDs and are recorded in the durable SQLite store:
  mention the agent and reply `approve <task-id>` or `changes <task-id> <feedback>`.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from agent.triage_agent import TriageAgent, TriageError, TriageResult
from github_issues import GITHUB_ISSUE_RE, IssueFetchError, load_issue
from storage.task_store import (
    DecisionType,
    TaskNotFoundError,
    TaskRoomMismatchError,
    TaskStateError,
    TaskStatus,
    TaskStore,
    TaskThreadMismatchError,
    UnauthorizedActorError,
)
from switch.agent_client import SwitchAgentClient, SwitchAuthError, SwitchError

logger = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 1_900
MAX_SEEN_MESSAGES = 500
MAX_SUMMARY_CHARS = 500
MAX_STEP_CHARS = 240
MAX_ERROR_CHARS = 400

APPROVE_RE = re.compile(
    r"^(?:please\s+)?(?:approve|approved|lgtm|looks\s+good|ship\s+it)(?:\s+plan)?(?:\s+task)?(?:\s+([0-9a-fA-F-]{8,36}))?[.!]*$",
    re.IGNORECASE,
)
CHANGES_RE = re.compile(
    r"^(?:please\s+)?"
    r"(?:changes?|request(?:ed)?\s+changes|needs\s+changes|reject(?:ed)?|decline|deny)"
    r"(?:\s+task)?(?:\s+([0-9a-fA-F-]{8,36}))?"
    r"\b[:,;\s-]*(.*)$",
    re.IGNORECASE | re.DOTALL,
)
HELP_RE = re.compile(r"^/?(?:help|usage|commands)[.!]?$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class PendingTriage:
    requester_id: str
    requester_name: str
    source: str
    summary: str
    request_key: str


class SwitchTriageConnector:
    """Routes addressed Switch messages through the triage agent and durable store."""

    def __init__(
        self,
        *,
        client: SwitchAgentClient,
        triage_agent: TriageAgent,
        agent_name: str = "crewpilot-triager",
        github_token: str | None = None,
        store: TaskStore | None = None,
        reproducer_agent_name: str = "crewpilot-repro-builder",
    ) -> None:
        self._client = client
        self._agent = triage_agent
        self.agent_name = agent_name
        self._github_token = github_token
        self._store = store
        self._reproducer_agent_name = reproducer_agent_name
        self._pending: dict[tuple[str, str | None], PendingTriage] = {}
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

        # Sender identity extraction:
        # Documented protocol: payload.sender is a participant ID string
        # payload.sender_name is the user display name
        raw_name = payload.get("sender_name")
        raw_sender = payload.get("sender")
        if isinstance(raw_sender, str) and raw_sender.strip():
            sender_id = raw_sender.strip()
            sender_name = str(raw_name or sender_id).strip()
        elif isinstance(raw_sender, dict):
            sender_id = str(raw_sender.get("id") or raw_sender.get("name") or "user").strip()
            sender_name = str(raw_sender.get("name") or raw_name or sender_id).strip()
        else:
            sender_id = str(payload.get("sender_id") or raw_name or "user").strip()
            sender_name = str(raw_name or sender_id).strip()

        if sender_name.casefold() == self.agent_name.casefold() or sender_id.casefold() == self.agent_name.casefold():
            return  # Never react to our own posts.

        # Deduplication using message_id (official protocol) with fallback to id
        message_id = str(payload.get("message_id") or payload.get("id") or "")
        if message_id:
            if message_id in self._seen_messages:
                logger.debug("Ignoring duplicate Switch message %s", message_id)
                return
            self._seen_messages[message_id] = None
            if len(self._seen_messages) > MAX_SEEN_MESSAGES:
                self._seen_messages.popitem(last=False)

        raw_thread = payload.get("thread_id")
        thread_id = str(raw_thread) if raw_thread else None
        normalized = self._normalize(body)

        if HELP_RE.fullmatch(normalized):
            await self._send(room_id, self._help_text(), thread_id)
            return

        # Check for approval or changes decision
        decision = self._parse_decision(normalized)
        if decision is not None:
            verdict, task_id, feedback = decision
            if self._store is not None:
                await self._handle_store_decision(
                    room_id=room_id,
                    thread_id=thread_id,
                    sender_id=sender_id,
                    sender_name=sender_name,
                    task_id=task_id,
                    verdict=verdict,
                    feedback=feedback,
                )
                return

            # Fallback legacy in-memory handling if store is not configured
            pending_key, pending = self._find_pending(room_id, thread_id)
            if pending is not None:
                await self._handle_legacy_decision(
                    room_id=room_id,
                    thread_id=thread_id,
                    sender_id=sender_id,
                    sender_name=sender_name,
                    pending_key=pending_key,
                    pending=pending,
                    verdict=verdict,
                    feedback=feedback,
                )
                return
            else:
                await self._send(
                    room_id,
                    "There is no triage waiting for approval here. "
                    "Mention me with an issue first.",
                    thread_id,
                )
                return

        issue_text = self._issue_text(body)
        if not issue_text:
            await self._send(room_id, self._help_text(), thread_id)
            return

        # Require an explicit intent so ordinary conversation does not invoke
        # the LLM: a GitHub issue URL, or the word "triage".
        if (
            GITHUB_ISSUE_RE.search(body) is None
            and re.search(r"\btriage\b", normalized, re.IGNORECASE) is None
        ):
            await self._send(room_id, self._help_text(), thread_id)
            return

        request_key = self._request_key(issue_text)

        # Check for existing duplicate pending requests
        if self._store is not None:
            existing_pending = self._store.list_tasks(
                room_id=room_id,
                request_key=request_key,
                status=TaskStatus.AWAITING_APPROVAL.value,
            )
            if existing_pending:
                dup = existing_pending[0]
                await self._send(
                    room_id,
                    f"This issue is already awaiting a decision ({dup.issue_source}, Task: `{dup.task_id}`). "
                    f"Approve with 'approve {dup.task_id}' or request changes first.",
                    thread_id,
                )
                return
        else:
            duplicate = self._find_pending_for_request(room_id, request_key)
            if duplicate is not None:
                await self._send(
                    room_id,
                    f"This issue is already awaiting a decision ({duplicate.source}). "
                    "Approve the existing plan or request changes first.",
                    thread_id,
                )
                return

        await self._triage(
            room_id=room_id,
            thread_id=thread_id,
            sender_id=sender_id,
            sender_name=sender_name,
            issue_text=issue_text,
            request_key=request_key,
        )

    async def _triage(
        self,
        *,
        room_id: str,
        thread_id: str | None,
        sender_id: str,
        sender_name: str,
        issue_text: str,
        request_key: str,
    ) -> None:
        logger.info("Triaging issue from %s in room %s", sender_name, room_id)
        await self._send(
            room_id,
            "Triaging... I'll post the plan in a few seconds.",
            thread_id,
        )

        try:
            issue = await load_issue(issue_text, self._github_token)
            result = await asyncio.to_thread(self._agent.triage, issue.text)
        except (IssueFetchError, TriageError) as exc:
            logger.warning("Triage failed: %s", exc)
            await self._send(
                room_id,
                f"Triage failed: {_clip(str(exc), MAX_ERROR_CHARS)}",
                thread_id,
            )
            return
        except Exception:
            logger.exception("Unexpected triage failure in room %s", room_id)
            await self._send(
                room_id,
                "Triage failed unexpectedly. Check the agent logs.",
                thread_id,
            )
            return

        task_id: str | None = None
        if self._store is not None:
            task = self._store.create_task(
                room_id=room_id,
                thread_id=thread_id,
                requester_id=sender_id,
                requester_name=sender_name,
                issue_source=issue.source,
                request_key=request_key,
                initial_summary=result.summary,
                initial_triage_data=result.to_dict(),
            )
            task_id = task.task_id
        else:
            self._pending[(room_id, thread_id)] = PendingTriage(
                requester_id=sender_id,
                requester_name=sender_name,
                source=issue.source,
                summary=result.summary,
                request_key=request_key,
            )

        try:
            await self._client.post_message(
                room_id,
                self._format_triage(result, issue.source, task_id=task_id),
                thread_id,
            )
        except SwitchAuthError:
            raise
        except SwitchError as exc:
            logger.error("Could not post triage result: %s", exc)
            return

        logger.info(
            "Posted triage for %s (task_id=%s category=%s severity=%s difficulty=%s)",
            issue.source,
            task_id,
            result.category,
            result.severity,
            result.difficulty,
        )

    async def _handle_store_decision(
        self,
        *,
        room_id: str,
        thread_id: str | None,
        sender_id: str,
        sender_name: str,
        task_id: str | None,
        verdict: str,
        feedback: str,
    ) -> None:
        """Handle a decision backed by the durable SQLite store."""
        if not task_id:
            action_desc = "approve" if verdict == "approve" else "request changes on"
            await self._send(
                room_id,
                f"Please specify the exact task ID to {action_desc}: "
                f"reply '@{self.agent_name} {verdict} <task-id>'.",
                thread_id,
            )
            return

        assert self._store is not None
        task = self._store.get_task(task_id)
        if task is None:
            await self._send(
                room_id,
                f"Task '{task_id}' was not found. Please verify the task ID.",
                thread_id,
            )
            return

        if sender_id != task.requester_id:
            await self._send(
                room_id,
                f"Only {task.requester_name} can approve this triage.",
                thread_id,
            )
            return

        decision_enum = (
            DecisionType.APPROVED.value
            if verdict == "approve"
            else DecisionType.CHANGES_REQUESTED.value
        )

        try:
            self._store.record_decision(
                task_id=task_id,
                room_id=room_id,
                thread_id=thread_id,
                actor_id=sender_id,
                decision=decision_enum,
                feedback=feedback if feedback else None,
            )
        except TaskNotFoundError:
            await self._send(
                room_id,
                f"Task '{task_id}' was not found. Please verify the task ID.",
                thread_id,
            )
            return
        except TaskRoomMismatchError:
            await self._send(
                room_id,
                f"Task '{task_id}' belongs to another room.",
                thread_id,
            )
            return
        except TaskThreadMismatchError:
            await self._send(
                room_id,
                f"Task '{task_id}' must be decided within its original thread.",
                thread_id,
            )
            return
        except TaskStateError as exc:
            await self._send(
                room_id,
                f"Task '{task_id}' cannot receive a decision: {exc}",
                thread_id,
            )
            return
        except UnauthorizedActorError:
            await self._send(
                room_id,
                f"Only {task.requester_name} can approve this triage.",
                thread_id,
            )
            return

        if verdict == "approve":
            message = (
                f"Approved by {sender_name}. {task.issue_source} (Task `{task_id}`): "
                f"{_clip(task.issue_source, 300)}\n\n"
                "Approval recorded in durable SQLite audit log. Dispatching to Reproducer..."
            )
            await self._send(room_id, message, thread_id)

            # Send actual Switch handoff message addressed to the Reproducer
            handoff = f"@{self._reproducer_agent_name} reproduce task {task_id}"
            await self._send(room_id, handoff, thread_id)
            logger.info("Dispatched task %s handoff to %s", task_id, self._reproducer_agent_name)
        else:
            message = (
                f"Changes requested by {sender_name} for task `{task_id}` ({task.issue_source}).\n"
                f"Feedback: {_clip(feedback or 'No feedback supplied.', MAX_ERROR_CHARS)}"
            )
            await self._send(room_id, message, thread_id)

    async def _handle_legacy_decision(
        self,
        *,
        room_id: str,
        thread_id: str | None,
        sender_id: str,
        sender_name: str,
        pending_key: tuple[str, str | None] | None,
        pending: PendingTriage,
        verdict: str,
        feedback: str,
    ) -> None:
        """Legacy fallback decision handler for unconfigured task store."""
        if sender_id != pending.requester_id:
            await self._send(
                room_id,
                f"Only {pending.requester_name} can approve this triage.",
                thread_id,
            )
            return

        if verdict == "approve":
            message = (
                f"Approved by {sender_name}. {pending.source}: "
                f"{_clip(pending.summary, 300)}\n\n"
                "Approval recorded in the room. No GitHub changes were made."
            )
        else:
            message = (
                f"Changes requested by {sender_name} for {pending.source}.\n"
                f"Feedback: {_clip(feedback or 'No feedback supplied.', MAX_ERROR_CHARS)}"
            )

        if await self._send(room_id, message, thread_id) and pending_key is not None:
            self._pending.pop(pending_key, None)
            logger.info(
                "Triage %s for %s by %s",
                "approved" if verdict == "approve" else "returned for changes",
                pending.source,
                sender_name,
            )

    def _find_pending(
        self, room_id: str, thread_id: str | None
    ) -> tuple[tuple[str, str | None] | None, PendingTriage | None]:
        exact = (room_id, thread_id)
        if exact in self._pending:
            return exact, self._pending[exact]
        # Legacy room fallback retained only when TaskStore is not attached
        for key in reversed(self._pending):
            if key[0] == room_id:
                return key, self._pending[key]
        return None, None

    def _find_pending_for_request(
        self, room_id: str, request_key: str
    ) -> PendingTriage | None:
        for pending_key, pending in reversed(self._pending.items()):
            if pending_key[0] == room_id and pending.request_key == request_key:
                return pending
        return None

    @staticmethod
    def _request_key(issue_text: str) -> str:
        match = GITHUB_ISSUE_RE.search(issue_text)
        if match:
            owner, repo, number = match.groups()
            return f"github:{owner.lower()}/{repo.lower()}#{number}"
        normalized = re.sub(r"\s+", " ", issue_text).strip().lower()
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
        return f"text:{digest}"

    @staticmethod
    def _normalize(body: str) -> str:
        without_mentions = re.sub(r"@\S+", " ", body)
        return re.sub(r"\s+", " ", without_mentions).strip()

    def _issue_text(self, body: str) -> str:
        text = re.sub(
            rf"@{re.escape(self.agent_name)}\b", " ", body, flags=re.IGNORECASE
        )
        text = re.sub(r"^\s*@\S+\s+", "", text).strip()
        text = re.sub(r"^triage\b[:\s]*", "", text, flags=re.IGNORECASE).strip()
        return text

    @staticmethod
    def _parse_decision(normalized: str) -> tuple[str, str | None, str] | None:
        """Parse approve or changes command, returning (verdict, task_id, feedback)."""
        app_match = APPROVE_RE.fullmatch(normalized)
        if app_match:
            task_id = app_match.group(1)
            return "approve", task_id.strip() if task_id else None, ""

        changes_match = CHANGES_RE.match(normalized)
        if changes_match:
            task_id = changes_match.group(1)
            feedback = changes_match.group(2).strip()
            return "changes", task_id.strip() if task_id else None, feedback
        return None

    def _format_triage(
        self, result: TriageResult, source: str, task_id: str | None = None
    ) -> str:
        task_line = f"Task ID: `{task_id}`\n" if task_id else ""
        if task_id:
            footer = (
                f"\n\nTo decide, mention me and reply `approve {task_id}` "
                f"or `changes {task_id} <feedback>`."
            )
        else:
            footer = (
                "\n\nTo decide, mention me and reply `approve` "
                "or `changes <feedback>`."
            )

        header = (
            "**Issue triage**\n"
            f"{task_line}"
            f"Source: {_clip(source, 120)}\n"
            f"Category: {result.category.title()} | "
            f"Severity: {result.severity.title()} | "
            f"Difficulty: {result.difficulty.title()}\n\n"
            "**Summary**\n"
            f"{_clip(result.summary, MAX_SUMMARY_CHARS)}\n\n"
            "**Suggested fix plan**\n"
        )
        budget = MAX_MESSAGE_CHARS - len(header) - len(footer)

        lines: list[str] = []
        for index, step in enumerate(result.suggested_fix_plan, start=1):
            line = f"{index}. {_clip(step, MAX_STEP_CHARS)}"
            if len("\n".join([*lines, line])) > budget:
                break
            lines.append(line)

        if len(lines) < len(result.suggested_fix_plan):
            lines.append("(remaining steps omitted for length)")
        if not lines:
            lines.append("See the issue for details.")

        return header + "\n".join(lines) + footer

    def _help_text(self) -> str:
        return (
            "**CrewPilot Auto-Triager**\n"
            "Mention me with:\n"
            "`triage <GitHub issue URL or description>`\n"
            "`approve <task-id>` to accept an investigation plan\n"
            "`changes <task-id> <feedback>` to request revisions\n"
            "`help` to show this message"
        )

    async def _send(self, room_id: str, body: str, thread_id: str | None) -> bool:
        try:
            await self._client.post_message(
                room_id, _clip(body, MAX_MESSAGE_CHARS), thread_id
            )
            return True
        except SwitchAuthError:
            raise
        except SwitchError:
            logger.exception("Could not post to Switch room %s", room_id)
            return False


def _clip(text: str, limit: int) -> str:
    cleaned = str(text).strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3].rstrip() + "..."
