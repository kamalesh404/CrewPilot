"""Production SQLite task store for CrewPilot.

Provides durable persistence for tasks, revisions, evidence, approvals,
and immutable audit history with atomic, exact-binding authorization checks.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

# Default busy timeout in milliseconds (5.0 seconds)
DEFAULT_BUSY_TIMEOUT_MS = 5000


class TaskStatus(str, Enum):
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"
    CANCELLED = "cancelled"
    # Extensible states for upcoming hackathon milestones:
    REPRODUCING = "reproducing"
    IMPLEMENTING = "implementing"
    UNDER_REVIEW = "under_review"
    REVIEW_APPROVED = "review_approved"
    PR_PUBLISHED = "pr_published"


class DecisionType(str, Enum):
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"


# Strict state transition matrix
VALID_TRANSITIONS: dict[str, set[str]] = {
    TaskStatus.AWAITING_APPROVAL.value: {
        TaskStatus.APPROVED.value,
        TaskStatus.CHANGES_REQUESTED.value,
        TaskStatus.CANCELLED.value,
    },
    TaskStatus.CHANGES_REQUESTED.value: {
        TaskStatus.AWAITING_APPROVAL.value,
        TaskStatus.CANCELLED.value,
    },
    TaskStatus.APPROVED.value: {
        TaskStatus.REPRODUCING.value,
        TaskStatus.CANCELLED.value,
    },
    TaskStatus.REPRODUCING.value: {
        TaskStatus.IMPLEMENTING.value,
        TaskStatus.UNDER_REVIEW.value,
        TaskStatus.CHANGES_REQUESTED.value,
        TaskStatus.CANCELLED.value,
    },
    TaskStatus.IMPLEMENTING.value: {
        TaskStatus.UNDER_REVIEW.value,
        TaskStatus.CANCELLED.value,
    },
    TaskStatus.UNDER_REVIEW.value: {
        TaskStatus.REVIEW_APPROVED.value,
        TaskStatus.CHANGES_REQUESTED.value,
        TaskStatus.CANCELLED.value,
    },
    TaskStatus.REVIEW_APPROVED.value: {
        TaskStatus.PR_PUBLISHED.value,
        TaskStatus.CANCELLED.value,
    },
    TaskStatus.PR_PUBLISHED.value: set(),
    TaskStatus.CANCELLED.value: set(),
}


class TaskStoreError(RuntimeError):
    """Base exception for all task store errors."""


class TaskNotFoundError(TaskStoreError):
    """Raised when a task does not exist."""


class TaskRoomMismatchError(TaskStoreError):
    """Raised when the room does not match the task's originating room."""


class TaskThreadMismatchError(TaskStoreError):
    """Raised when the thread does not match the task's originating thread."""


class UnauthorizedActorError(TaskStoreError):
    """Raised when a non-requester attempts to approve or decide on a task."""


class TaskStateError(TaskStoreError):
    """Raised when an illegal state transition is attempted or task is already decided."""


class InvalidDecisionError(TaskStoreError):
    """Raised when an invalid decision verdict is supplied."""


class DuplicateRevisionError(TaskStoreError):
    """Raised when a revision number collision occurs."""


@dataclass(frozen=True, slots=True)
class Task:
    task_id: str
    room_id: str
    thread_id: str | None
    requester_id: str
    requester_name: str
    issue_source: str
    request_key: str
    status: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class TaskRevision:
    revision_id: str
    task_id: str
    revision_number: int
    triage_summary: str
    triage_data_json: str
    created_at: str

    @property
    def triage_data(self) -> dict[str, Any]:
        try:
            parsed = json.loads(self.triage_data_json)
            return parsed if isinstance(parsed, dict) else {"raw": parsed}
        except json.JSONDecodeError:
            return {"raw": self.triage_data_json}


@dataclass(frozen=True, slots=True)
class TaskEvidence:
    evidence_id: str
    task_id: str
    evidence_type: str
    description: str
    command: str | None
    output: str | None
    artifact_path: str | None
    commit_sha: str | None
    created_at: str


@dataclass(frozen=True, slots=True)
class TaskApproval:
    approval_id: str
    task_id: str
    actor_id: str
    decision: str
    feedback: str | None
    approved_scope: str | None
    created_at: str


@dataclass(frozen=True, slots=True)
class TaskAuditEvent:
    audit_id: str
    task_id: str
    actor_id: str
    event_type: str
    event_data_json: str
    created_at: str

    @property
    def event_data(self) -> dict[str, Any]:
        try:
            parsed = json.loads(self.event_data_json)
            return parsed if isinstance(parsed, dict) else {"raw": parsed}
        except json.JSONDecodeError:
            return {"raw": self.event_data_json}


def _utcnow_iso() -> str:
    """Return current UTC timestamp in ISO 8601 format."""
    return datetime.now(timezone.utc).isoformat()


def _normalize_thread(thread_id: str | None) -> str | None:
    """Normalize thread IDs: empty or whitespace-only strings become None."""
    if thread_id is None:
        return None
    cleaned = str(thread_id).strip()
    return cleaned if cleaned else None


_UNSET: Any = object()


class TaskStore:
    """SQLite-backed durable task, revision, evidence, approval, and audit store."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        timeout: float = 5.0,
        wal_mode: bool = True,
    ) -> None:
        self.db_path = Path(db_path)
        self._timeout = timeout
        self._wal_mode = wal_mode
        self._ensure_parent_dir()
        self._init_db()

    def _ensure_parent_dir(self) -> None:
        if str(self.db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=self._timeout,
            isolation_level=None,  # Explicit transaction management
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute(f"PRAGMA busy_timeout = {int(self._timeout * 1000)};")
        if self._wal_mode and str(self.db_path) != ":memory:":
            conn.execute("PRAGMA journal_mode = WAL;")
        return conn

    @contextmanager
    def _connection(self) -> Generator[sqlite3.Connection, None, None]:
        conn = self._get_connection()
        try:
            yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        """Create tables and indexes if they do not exist."""
        with self._connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    room_id TEXT NOT NULL,
                    thread_id TEXT,
                    requester_id TEXT NOT NULL,
                    requester_name TEXT NOT NULL,
                    issue_source TEXT NOT NULL,
                    request_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS task_revisions (
                    revision_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                    revision_number INTEGER NOT NULL,
                    triage_summary TEXT NOT NULL,
                    triage_data_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    CONSTRAINT uq_task_revision UNIQUE(task_id, revision_number)
                );

                CREATE TABLE IF NOT EXISTS task_evidence (
                    evidence_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                    evidence_type TEXT NOT NULL,
                    description TEXT NOT NULL,
                    command TEXT,
                    output TEXT,
                    artifact_path TEXT,
                    commit_sha TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS task_approvals (
                    approval_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                    actor_id TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    feedback TEXT,
                    approved_scope TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS task_audit (
                    audit_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                    actor_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    event_data_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_tasks_room_thread ON tasks(room_id, thread_id);
                CREATE INDEX IF NOT EXISTS idx_tasks_request_key ON tasks(request_key);
                CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
                CREATE INDEX IF NOT EXISTS idx_task_revisions_task ON task_revisions(task_id);
                CREATE INDEX IF NOT EXISTS idx_task_evidence_task ON task_evidence(task_id);
                CREATE INDEX IF NOT EXISTS idx_task_approvals_task ON task_approvals(task_id);
                CREATE INDEX IF NOT EXISTS idx_task_audit_task ON task_audit(task_id);
                """
            )

    def create_task(
        self,
        *,
        room_id: str,
        requester_id: str,
        requester_name: str,
        issue_source: str,
        request_key: str,
        thread_id: str | None = None,
        task_id: str | None = None,
        initial_summary: str | None = None,
        initial_triage_data: dict[str, Any] | str | None = None,
    ) -> Task:
        """Create a new task in awaiting_approval state and record its initial audit event."""
        norm_room = str(room_id).strip()
        norm_requester_id = str(requester_id).strip()
        norm_requester_name = str(requester_name).strip()
        norm_issue_source = str(issue_source).strip()
        norm_request_key = str(request_key).strip()
        norm_thread_id = _normalize_thread(thread_id)

        if not norm_room:
            raise ValueError("room_id must not be empty.")
        if not norm_requester_id:
            raise ValueError("requester_id must not be empty.")
        if not norm_requester_name:
            raise ValueError("requester_name must not be empty.")
        if not norm_issue_source:
            raise ValueError("issue_source must not be empty.")
        if not norm_request_key:
            raise ValueError("request_key must not be empty.")

        actual_task_id = str(task_id).strip() if task_id else str(uuid.uuid4())
        now = _utcnow_iso()
        status = TaskStatus.AWAITING_APPROVAL.value

        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                conn.execute(
                    """
                    INSERT INTO tasks (
                        task_id, room_id, thread_id, requester_id, requester_name,
                        issue_source, request_key, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        actual_task_id,
                        norm_room,
                        norm_thread_id,
                        norm_requester_id,
                        norm_requester_name,
                        norm_issue_source,
                        norm_request_key,
                        status,
                        now,
                        now,
                    ),
                )

                if initial_summary:
                    rev_id = str(uuid.uuid4())
                    raw_data = initial_triage_data or {}
                    json_data = raw_data if isinstance(raw_data, str) else json.dumps(raw_data)
                    conn.execute(
                        """
                        INSERT INTO task_revisions (
                            revision_id, task_id, revision_number,
                            triage_summary, triage_data_json, created_at
                        ) VALUES (?, ?, 1, ?, ?, ?);
                        """,
                        (rev_id, actual_task_id, initial_summary.strip(), json_data, now),
                    )

                audit_id = str(uuid.uuid4())
                audit_payload = {
                    "action": "task_created",
                    "room_id": norm_room,
                    "thread_id": norm_thread_id,
                    "issue_source": norm_issue_source,
                    "request_key": norm_request_key,
                }
                conn.execute(
                    """
                    INSERT INTO task_audit (
                        audit_id, task_id, actor_id, event_type, event_data_json, created_at
                    ) VALUES (?, ?, ?, 'TASK_CREATED', ?, ?);
                    """,
                    (audit_id, actual_task_id, norm_requester_id, json.dumps(audit_payload), now),
                )

                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise

        return Task(
            task_id=actual_task_id,
            room_id=norm_room,
            thread_id=norm_thread_id,
            requester_id=norm_requester_id,
            requester_name=norm_requester_name,
            issue_source=norm_issue_source,
            request_key=norm_request_key,
            status=status,
            created_at=now,
            updated_at=now,
        )

    def get_task(self, task_id: str) -> Task | None:
        """Fetch a single task by its task_id."""
        with self._connection() as conn:
            cursor = conn.execute(
                """
                SELECT task_id, room_id, thread_id, requester_id, requester_name,
                       issue_source, request_key, status, created_at, updated_at
                FROM tasks WHERE task_id = ?;
                """,
                (str(task_id).strip(),),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return Task(
                task_id=row["task_id"],
                room_id=row["room_id"],
                thread_id=row["thread_id"],
                requester_id=row["requester_id"],
                requester_name=row["requester_name"],
                issue_source=row["issue_source"],
                request_key=row["request_key"],
                status=row["status"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )

    def list_tasks(
        self,
        *,
        room_id: str | None = None,
        thread_id: str | None = _UNSET,
        status: str | None = None,
        request_key: str | None = None,
        limit: int = 100,
    ) -> list[Task]:
        """List tasks matching optional filters.

        If thread_id is explicitly passed as None, filters for tasks where thread_id IS NULL.
        If thread_id is omitted, tasks are not filtered by thread.
        """
        query = [
            "SELECT task_id, room_id, thread_id, requester_id, requester_name,",
            "       issue_source, request_key, status, created_at, updated_at",
            "FROM tasks WHERE 1=1",
        ]
        params: list[Any] = []

        if room_id is not None:
            query.append("AND room_id = ?")
            params.append(str(room_id).strip())

        if thread_id is not _UNSET:
            norm_t = _normalize_thread(thread_id)
            if norm_t is None:
                query.append("AND thread_id IS NULL")
            else:
                query.append("AND thread_id = ?")
                params.append(norm_t)

        if status is not None:
            query.append("AND status = ?")
            params.append(str(status).strip())

        if request_key is not None:
            query.append("AND request_key = ?")
            params.append(str(request_key).strip())

        query.append("ORDER BY created_at DESC LIMIT ?")
        params.append(max(1, limit))

        with self._connection() as conn:
            cursor = conn.execute(" ".join(query), params)
            return [
                Task(
                    task_id=row["task_id"],
                    room_id=row["room_id"],
                    thread_id=row["thread_id"],
                    requester_id=row["requester_id"],
                    requester_name=row["requester_name"],
                    issue_source=row["issue_source"],
                    request_key=row["request_key"],
                    status=row["status"],
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                )
                for row in cursor.fetchall()
            ]

    def add_revision(
        self,
        *,
        task_id: str,
        triage_summary: str,
        triage_data: dict[str, Any] | str,
        revision_number: int | None = None,
        actor_id: str | None = None,
    ) -> TaskRevision:
        """Add a revision to a task. If revision_number is omitted, auto-increments."""
        tid = str(task_id).strip()
        summary = str(triage_summary).strip()
        if not summary:
            raise ValueError("triage_summary must not be empty.")

        json_data = triage_data if isinstance(triage_data, str) else json.dumps(triage_data)
        now = _utcnow_iso()
        rev_id = str(uuid.uuid4())
        actor = str(actor_id).strip() if actor_id else "system"

        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                task = conn.execute(
                    "SELECT task_id, status FROM tasks WHERE task_id = ?;", (tid,)
                ).fetchone()
                if task is None:
                    raise TaskNotFoundError(f"Task '{tid}' does not exist.")

                if revision_number is None:
                    max_rev = conn.execute(
                        "SELECT MAX(revision_number) AS max_rev FROM task_revisions WHERE task_id = ?;",
                        (tid,),
                    ).fetchone()
                    rev_num = 1 if max_rev["max_rev"] is None else max_rev["max_rev"] + 1
                else:
                    rev_num = int(revision_number)

                try:
                    conn.execute(
                        """
                        INSERT INTO task_revisions (
                            revision_id, task_id, revision_number,
                            triage_summary, triage_data_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?);
                        """,
                        (rev_id, tid, rev_num, summary, json_data, now),
                    )
                except sqlite3.IntegrityError as exc:
                    raise DuplicateRevisionError(
                        f"Revision {rev_num} already exists for task '{tid}'."
                    ) from exc

                conn.execute(
                    "UPDATE tasks SET updated_at = ? WHERE task_id = ?;",
                    (now, tid),
                )

                audit_id = str(uuid.uuid4())
                audit_payload = {
                    "revision_number": rev_num,
                    "summary": summary[:200],
                }
                conn.execute(
                    """
                    INSERT INTO task_audit (
                        audit_id, task_id, actor_id, event_type, event_data_json, created_at
                    ) VALUES (?, ?, ?, 'REVISION_ADDED', ?, ?);
                    """,
                    (audit_id, tid, actor, json.dumps(audit_payload), now),
                )

                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise

        return TaskRevision(
            revision_id=rev_id,
            task_id=tid,
            revision_number=rev_num,
            triage_summary=summary,
            triage_data_json=json_data,
            created_at=now,
        )

    def list_revisions(self, task_id: str) -> list[TaskRevision]:
        """List all revisions for a task ordered by revision_number ascending."""
        with self._connection() as conn:
            cursor = conn.execute(
                """
                SELECT revision_id, task_id, revision_number, triage_summary,
                       triage_data_json, created_at
                FROM task_revisions WHERE task_id = ?
                ORDER BY revision_number ASC;
                """,
                (str(task_id).strip(),),
            )
            return [
                TaskRevision(
                    revision_id=row["revision_id"],
                    task_id=row["task_id"],
                    revision_number=row["revision_number"],
                    triage_summary=row["triage_summary"],
                    triage_data_json=row["triage_data_json"],
                    created_at=row["created_at"],
                )
                for row in cursor.fetchall()
            ]

    def add_evidence(
        self,
        *,
        task_id: str,
        evidence_type: str,
        description: str,
        command: str | None = None,
        output: str | None = None,
        artifact_path: str | None = None,
        commit_sha: str | None = None,
        actor_id: str | None = None,
    ) -> TaskEvidence:
        """Attach verified evidence to a task and record an audit log entry."""
        tid = str(task_id).strip()
        etype = str(evidence_type).strip()
        desc = str(description).strip()
        if not etype:
            raise ValueError("evidence_type must not be empty.")
        if not desc:
            raise ValueError("description must not be empty.")

        ev_id = str(uuid.uuid4())
        now = _utcnow_iso()
        actor = str(actor_id).strip() if actor_id else "system"

        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                task = conn.execute(
                    "SELECT task_id FROM tasks WHERE task_id = ?;", (tid,)
                ).fetchone()
                if task is None:
                    raise TaskNotFoundError(f"Task '{tid}' does not exist.")

                conn.execute(
                    """
                    INSERT INTO task_evidence (
                        evidence_id, task_id, evidence_type, description,
                        command, output, artifact_path, commit_sha, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        ev_id,
                        tid,
                        etype,
                        desc,
                        command,
                        output,
                        artifact_path,
                        commit_sha,
                        now,
                    ),
                )

                conn.execute(
                    "UPDATE tasks SET updated_at = ? WHERE task_id = ?;",
                    (now, tid),
                )

                audit_id = str(uuid.uuid4())
                audit_payload = {
                    "evidence_type": etype,
                    "description": desc[:200],
                    "commit_sha": commit_sha,
                }
                conn.execute(
                    """
                    INSERT INTO task_audit (
                        audit_id, task_id, actor_id, event_type, event_data_json, created_at
                    ) VALUES (?, ?, ?, 'EVIDENCE_ADDED', ?, ?);
                    """,
                    (audit_id, tid, actor, json.dumps(audit_payload), now),
                )

                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise

        return TaskEvidence(
            evidence_id=ev_id,
            task_id=tid,
            evidence_type=etype,
            description=desc,
            command=command,
            output=output,
            artifact_path=artifact_path,
            commit_sha=commit_sha,
            created_at=now,
        )

    def list_evidence(self, task_id: str) -> list[TaskEvidence]:
        """List all evidence attached to a task ordered by created_at ascending."""
        with self._connection() as conn:
            cursor = conn.execute(
                """
                SELECT evidence_id, task_id, evidence_type, description,
                       command, output, artifact_path, commit_sha, created_at
                FROM task_evidence WHERE task_id = ?
                ORDER BY created_at ASC;
                """,
                (str(task_id).strip(),),
            )
            return [
                TaskEvidence(
                    evidence_id=row["evidence_id"],
                    task_id=row["task_id"],
                    evidence_type=row["evidence_type"],
                    description=row["description"],
                    command=row["command"],
                    output=row["output"],
                    artifact_path=row["artifact_path"],
                    commit_sha=row["commit_sha"],
                    created_at=row["created_at"],
                )
                for row in cursor.fetchall()
            ]

    def record_decision(
        self,
        *,
        task_id: str,
        room_id: str,
        thread_id: str | None,
        actor_id: str,
        decision: str,
        feedback: str | None = None,
        approved_scope: str | None = None,
    ) -> TaskApproval:
        """Atomically record a scope decision with strict multi-factor binding.

        Enforces:
          1. Exact task_id existence.
          2. Exact room_id match.
          3. Exact thread_id match (including None handling; no fallback).
          4. Requester authorization (actor_id == task.requester_id).
          5. Task currently in awaiting_approval status.
          6. Valid decision ('approved' or 'changes_requested').
        """
        tid = str(task_id).strip()
        norm_room = str(room_id).strip()
        norm_thread = _normalize_thread(thread_id)
        norm_actor = str(actor_id).strip()
        norm_decision = str(decision).strip().lower()

        if norm_decision not in {
            DecisionType.APPROVED.value,
            DecisionType.CHANGES_REQUESTED.value,
        }:
            raise InvalidDecisionError(
                f"Invalid decision '{decision}'. Must be '{DecisionType.APPROVED.value}' or '{DecisionType.CHANGES_REQUESTED.value}'."
            )

        now = _utcnow_iso()
        approval_id = str(uuid.uuid4())

        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                task_row = conn.execute(
                    """
                    SELECT task_id, room_id, thread_id, requester_id, requester_name,
                           status
                    FROM tasks WHERE task_id = ?;
                    """,
                    (tid,),
                ).fetchone()

                if task_row is None:
                    raise TaskNotFoundError(f"Task '{tid}' does not exist.")

                # Check 1: Exact room match
                if task_row["room_id"] != norm_room:
                    raise TaskRoomMismatchError(
                        f"Task '{tid}' belongs to room '{task_row['room_id']}', "
                        f"but approval arrived from room '{norm_room}'."
                    )

                # Check 2: Exact thread match (None strictly matches None)
                task_thread = _normalize_thread(task_row["thread_id"])
                if task_thread != norm_thread:
                    raise TaskThreadMismatchError(
                        f"Task '{tid}' belongs to thread '{task_thread}', "
                        f"but approval arrived for thread '{norm_thread}'."
                    )

                # Check 3: Requester authorization
                if task_row["requester_id"] != norm_actor:
                    raise UnauthorizedActorError(
                        f"Actor '{norm_actor}' is not authorized to decide task '{tid}'. "
                        f"Only requester '{task_row['requester_id']}' ({task_row['requester_name']}) can decide."
                    )

                # Check 4: Status check (must be awaiting_approval)
                current_status = task_row["status"]
                if current_status != TaskStatus.AWAITING_APPROVAL.value:
                    raise TaskStateError(
                        f"Task '{tid}' has status '{current_status}' and cannot receive "
                        f"a decision (expected '{TaskStatus.AWAITING_APPROVAL.value}')."
                    )

                target_status = (
                    TaskStatus.APPROVED.value
                    if norm_decision == DecisionType.APPROVED.value
                    else TaskStatus.CHANGES_REQUESTED.value
                )

                # Check 5: Transition validity
                allowed = VALID_TRANSITIONS.get(current_status, set())
                if target_status not in allowed:
                    raise TaskStateError(
                        f"Cannot transition task '{tid}' from '{current_status}' to '{target_status}'."
                    )

                # Persist approval
                conn.execute(
                    """
                    INSERT INTO task_approvals (
                        approval_id, task_id, actor_id, decision,
                        feedback, approved_scope, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        approval_id,
                        tid,
                        norm_actor,
                        norm_decision,
                        feedback.strip() if feedback else None,
                        approved_scope.strip() if approved_scope else None,
                        now,
                    ),
                )

                # Update task status
                conn.execute(
                    "UPDATE tasks SET status = ?, updated_at = ? WHERE task_id = ?;",
                    (target_status, now, tid),
                )

                # Audit log
                audit_id = str(uuid.uuid4())
                audit_payload = {
                    "decision": norm_decision,
                    "previous_status": current_status,
                    "new_status": target_status,
                    "feedback": feedback[:200] if feedback else None,
                }
                conn.execute(
                    """
                    INSERT INTO task_audit (
                        audit_id, task_id, actor_id, event_type, event_data_json, created_at
                    ) VALUES (?, ?, ?, 'DECISION_RECORDED', ?, ?);
                    """,
                    (audit_id, tid, norm_actor, json.dumps(audit_payload), now),
                )

                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise

        return TaskApproval(
            approval_id=approval_id,
            task_id=tid,
            actor_id=norm_actor,
            decision=norm_decision,
            feedback=feedback.strip() if feedback else None,
            approved_scope=approved_scope.strip() if approved_scope else None,
            created_at=now,
        )

    def update_task_status(
        self,
        task_id: str,
        new_status: str,
        *,
        actor_id: str,
        reason: str | None = None,
    ) -> Task:
        """Atomically transition task status with transition-rule validation."""
        tid = str(task_id).strip()
        norm_status = str(new_status).strip()
        norm_actor = str(actor_id).strip()
        now = _utcnow_iso()

        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                task_row = conn.execute(
                    """
                    SELECT task_id, room_id, thread_id, requester_id, requester_name,
                           issue_source, request_key, status, created_at, updated_at
                    FROM tasks WHERE task_id = ?;
                    """,
                    (tid,),
                ).fetchone()

                if task_row is None:
                    raise TaskNotFoundError(f"Task '{tid}' does not exist.")

                current_status = task_row["status"]
                allowed = VALID_TRANSITIONS.get(current_status, set())
                if norm_status not in allowed:
                    raise TaskStateError(
                        f"Illegal status transition for task '{tid}': '{current_status}' -> '{norm_status}'."
                    )

                conn.execute(
                    "UPDATE tasks SET status = ?, updated_at = ? WHERE task_id = ?;",
                    (norm_status, now, tid),
                )

                audit_id = str(uuid.uuid4())
                audit_payload = {
                    "previous_status": current_status,
                    "new_status": norm_status,
                    "reason": reason,
                }
                conn.execute(
                    """
                    INSERT INTO task_audit (
                        audit_id, task_id, actor_id, event_type, event_data_json, created_at
                    ) VALUES (?, ?, ?, 'STATUS_CHANGED', ?, ?);
                    """,
                    (audit_id, tid, norm_actor, json.dumps(audit_payload), now),
                )

                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise

        return Task(
            task_id=task_row["task_id"],
            room_id=task_row["room_id"],
            thread_id=task_row["thread_id"],
            requester_id=task_row["requester_id"],
            requester_name=task_row["requester_name"],
            issue_source=task_row["issue_source"],
            request_key=task_row["request_key"],
            status=norm_status,
            created_at=task_row["created_at"],
            updated_at=now,
        )

    def list_approvals(self, task_id: str) -> list[TaskApproval]:
        """List all approvals/decisions recorded for a task."""
        with self._connection() as conn:
            cursor = conn.execute(
                """
                SELECT approval_id, task_id, actor_id, decision,
                       feedback, approved_scope, created_at
                FROM task_approvals WHERE task_id = ?
                ORDER BY created_at ASC;
                """,
                (str(task_id).strip(),),
            )
            return [
                TaskApproval(
                    approval_id=row["approval_id"],
                    task_id=row["task_id"],
                    actor_id=row["actor_id"],
                    decision=row["decision"],
                    feedback=row["feedback"],
                    approved_scope=row["approved_scope"],
                    created_at=row["created_at"],
                )
                for row in cursor.fetchall()
            ]

    def list_audit_events(self, task_id: str) -> list[TaskAuditEvent]:
        """List all audit events recorded for a task."""
        with self._connection() as conn:
            cursor = conn.execute(
                """
                SELECT audit_id, task_id, actor_id, event_type,
                       event_data_json, created_at
                FROM task_audit WHERE task_id = ?
                ORDER BY created_at ASC;
                """,
                (str(task_id).strip(),),
            )
            return [
                TaskAuditEvent(
                    audit_id=row["audit_id"],
                    task_id=row["task_id"],
                    actor_id=row["actor_id"],
                    event_type=row["event_type"],
                    event_data_json=row["event_data_json"],
                    created_at=row["created_at"],
                )
                for row in cursor.fetchall()
            ]
