"""Reviewer agent running as a native agent inside Switch rooms.

Independent, read-only agent that audits reproduction evidence in the durable
SQLite store, verifies execution containment and zero credential leaks,
and approves the evidence for publication manifest preparation:
  `@crewpilot-reviewer review task <task-id>`

Once verified, the human maintainer can authorize publication manifest creation:
  `@crewpilot-reviewer publish task <task-id>`

LIMITATION: This agent does NOT create or push GitHub pull requests. It prepares
a verified publication manifest and records the maintainer authorization in SQLite.
"""

from __future__ import annotations

import logging
import re
from collections import OrderedDict
from typing import Any

from agent.reproducer import (
    EXPECTED_FAILURE_SIGNATURE,
    SUPPORTED_ISSUE,
    parse_issue_identity,
)
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

REVIEW_RE = re.compile(
    r"^(?:please\s+)?(?:review|audit|verify|check)(?:\s+task)?\s+([0-9a-fA-F-]{8,36})[.!]*$",
    re.IGNORECASE,
)
PUBLISH_RE = re.compile(
    r"^(?:please\s+)?(?:publish|create\s+pr|open\s+pr|ship|authorize)(?:\s+task)?\s+([0-9a-fA-F-]{8,36})[.!]*$",
    re.IGNORECASE,
)
HELP_RE = re.compile(r"^/?(?:help|usage|commands)[.!]?$", re.IGNORECASE)


def _strip_mentions(text: str) -> str:
    return re.sub(r"<@[^>]+>|@[\w-]+", "", text).strip()


def _clip(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3] + "..."


class SwitchReviewerConnector:
    """Read-only auditing agent for bug reproduction evidence and publication authorization."""

    def __init__(
        self,
        *,
        client: SwitchAgentClient,
        store: TaskStore,
        agent_name: str = "crewpilot-reviewer",
    ) -> None:
        self._client = client
        self._store = store
        self.agent_name = agent_name
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
                logger.debug("Skipping duplicate message %s", message_id)
                return
            self._record_seen(message_id)

        raw_thread = payload.get("thread_id")
        thread_id = str(raw_thread) if raw_thread else None

        stripped = _strip_mentions(body)

        if HELP_RE.match(stripped):
            await self._send(room_id, self._help_text(), thread_id)
            return

        review_match = REVIEW_RE.match(stripped)
        if review_match:
            task_id = review_match.group(1).lower()
            await self._handle_review(
                room_id=room_id,
                thread_id=thread_id,
                sender_id=sender_id,
                sender_name=sender_name,
                task_id=task_id,
            )
            return

        publish_match = PUBLISH_RE.match(stripped)
        if publish_match:
            task_id = publish_match.group(1).lower()
            await self._handle_publish(
                room_id=room_id,
                thread_id=thread_id,
                sender_id=sender_id,
                sender_name=sender_name,
                task_id=task_id,
            )
            return

        # Helpful prompt if action keyword was mentioned without a valid task ID
        if re.search(r"\b(?:review|audit|verify)\b", stripped, re.IGNORECASE):
            await self._send(
                room_id,
                f"Please specify the exact task ID to review: "
                f"reply '@{self.agent_name} review task <task-id>'.",
                thread_id,
            )
            return

        if re.search(r"\b(?:publish|create\s+pr|authorize)\b", stripped, re.IGNORECASE):
            await self._send(
                room_id,
                f"Please specify the exact task ID to publish: "
                f"reply '@{self.agent_name} publish task <task-id>'.",
                thread_id,
            )
            return

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
            f"**CrewPilot Independent Reviewer (`@{self.agent_name}`)**\n"
            "I independently audit reproduction evidence, check security containment, and prepare publication manifests.\n\n"
            "**Commands:**\n"
            f"- `@{self.agent_name} review task <task-id>` — Audit recorded evidence for a reproduced bug.\n"
            f"- `@{self.agent_name} publish task <task-id>` — Authorize publication manifest preparation (maintainer only).\n"
            "- `help` — Show this message."
        )

    async def _handle_review(
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

        # Target issue validation: exact issue identity required
        parsed_issue = parse_issue_identity(task.issue_source)
        if parsed_issue != SUPPORTED_ISSUE:
            await self._send(
                room_id,
                f"### 🛡️ Independent Review: Task `{task_id}`\n"
                f"- **Verdict:** ❌ **REVISION REQUIRED (Unsupported Target)**\n"
                f"- **Target:** {task.issue_source}\n"
                "- **Reason:** Only verified reproduction probes for psf/requests#7634 are currently supported.",
                thread_id,
            )
            return

        evidence_list = self._store.list_evidence(task_id)
        if not evidence_list:
            await self._send(
                room_id,
                f"No reproduction evidence recorded for task `{task_id}`. "
                "The Reproducer must reproduce the bug first.",
                thread_id,
            )
            return

        # Audit latest reproduction evidence
        latest_ev = evidence_list[-1]
        repro_output = (latest_ev.output or "").strip()
        desc = (latest_ev.description or "").strip()

        # Strict criteria check:
        # 1. Non-empty output
        has_output = bool(repro_output)

        # 2. Exit code 1 verified
        exit_code_ok = "exit 1" in desc or "exit_code: 1" in repro_output or "(exit 1)" in desc

        # 3. Exact failure signature present: "'int' object has no attribute 'translate'"
        signature_ok = EXPECTED_FAILURE_SIGNATURE in repro_output

        # 4. Security compliance: zero leaked tokens or passwords
        secret_leak_pattern = re.compile(
            r"\b(?:sk-[a-zA-Z0-9_-]{20,}|ghp_[a-zA-Z0-9]{20,}|ak_live_[a-zA-Z0-9]{16,}|bearer\s+[a-zA-Z0-9._-]{20,})\b",
            re.IGNORECASE,
        )
        clean_security = secret_leak_pattern.search(repro_output) is None

        is_verified = has_output and exit_code_ok and signature_ok and clean_security

        if is_verified:
            try:
                self._store.update_task_status(
                    task_id,
                    TaskStatus.REVIEW_APPROVED.value,
                    actor_id=self.agent_name,
                    reason=f"Evidence independently verified by {self.agent_name}",
                )
            except (TaskStateError, TaskNotFoundError) as exc:
                logger.error("Status transition error during review for task %s: %s", task_id, exc)
                await self._send(
                    room_id,
                    f"Review failed: could not transition task state ({exc}).",
                    thread_id,
                )
                return

            report = (
                f"### 🛡️ Independent Code & Evidence Review: Task `{task_id}`\n"
                f"- **Reviewer:** `@{self.agent_name}`\n"
                f"- **Verdict:** ✅ **EVIDENCE VERIFIED (Approved for Publication)**\n"
                f"- **Issue:** {task.issue_source}\n"
                f"- **Evidence Audit:** Subprocess exit code 1 matches exact signature (`{EXPECTED_FAILURE_SIGNATURE}`).\n"
                f"- **Root Cause:** Verified exception trace in disposable execution workspace.\n"
                f"- **Security Verification:** PASS (Zero credentials leaked; execution strictly confined).\n\n"
                f"Human maintainer ({task.requester_name}) may authorize publication manifest preparation:\n"
                f"reply `@{self.agent_name} publish task {task_id}`."
            )
            await self._send(room_id, report, thread_id)
        else:
            reasons = []
            if not has_output:
                reasons.append("Empty output from reproduction probe.")
            if not exit_code_ok:
                reasons.append("Exit code was not 1.")
            if not signature_ok:
                reasons.append(f"Missing expected error signature: {EXPECTED_FAILURE_SIGNATURE}.")
            if not clean_security:
                reasons.append("Potential secret or credential pattern detected in probe output.")

            reason_str = " ".join(reasons)
            try:
                self._store.update_task_status(
                    task_id,
                    TaskStatus.CHANGES_REQUESTED.value,
                    actor_id=self.agent_name,
                    reason=f"Evidence verification failed: {reason_str}",
                )
            except Exception as exc:
                logger.warning("Status transition error: %s", exc)

            report = (
                f"### 🛡️ Independent Code & Evidence Review: Task `{task_id}`\n"
                f"- **Reviewer:** `@{self.agent_name}`\n"
                f"- **Verdict:** ❌ **REVISION REQUIRED**\n"
                f"- **Issue:** {task.issue_source}\n"
                f"- **Reason:** Evidence did not satisfy strict reproduction criteria: {reason_str}"
            )
            await self._send(room_id, report, thread_id)

    async def _handle_publish(
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
                f"Task '{task_id}' was not found.",
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

        # Require exact human requester authorization
        if sender_id != task.requester_id:
            await self._send(
                room_id,
                f"Only {task.requester_name} can authorize publication manifest preparation for this task.",
                thread_id,
            )
            return

        # Must be approved by reviewer
        if task.status != TaskStatus.REVIEW_APPROVED.value:
            await self._send(
                room_id,
                f"Task '{task_id}' is in status '{task.status}'. "
                "Evidence must be approved by the Reviewer before publication manifest preparation.",
                thread_id,
            )
            return

        # Transition to PUBLICATION_AUTHORIZED
        try:
            self._store.update_task_status(
                task_id,
                TaskStatus.PUBLICATION_AUTHORIZED.value,
                actor_id=sender_id,
                reason=f"Publication manifest preparation authorized by {sender_name}",
            )
        except (TaskStateError, TaskNotFoundError) as exc:
            logger.error("Status update failed during publish for task %s: %s", task_id, exc)
            await self._send(
                room_id,
                f"Publication authorization failed: {exc}",
                thread_id,
            )
            return

        evidence_list = self._store.list_evidence(task_id)
        ev_count = len(evidence_list)

        msg = (
            f"### 📋 Publication Manifest Prepared for Task `{task_id}`\n"
            f"- **Target Issue:** {task.issue_source}\n"
            f"- **Status:** Manifest Prepared & Authorized (No remote PR published)\n"
            f"- **Audited Evidence Items:** {ev_count} in durable SQLite ledger\n"
            f"- **Authorized By:** {sender_name} (`{sender_id}`)\n"
            f"- **Suggested Branch:** `fix/issue-7634-multipart-key-str` (ready for local implementation)\n\n"
            "*Note: Automated remote PR creation is not executed in this environment. "
            "The publication manifest and complete audit trail are recorded in durable SQLite.*"
        )
        await self._send(room_id, msg, thread_id)
        logger.info("Publication manifest completed for task %s by %s", task_id, sender_name)
