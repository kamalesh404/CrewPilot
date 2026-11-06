"""Tests for the durable SQLite task store."""

from __future__ import annotations

import concurrent.futures
import sqlite3
from pathlib import Path

import pytest
from storage.task_store import (
    DecisionType,
    DuplicateRevisionError,
    InvalidDecisionError,
    TaskNotFoundError,
    TaskRoomMismatchError,
    TaskStateError,
    TaskStatus,
    TaskStore,
    TaskThreadMismatchError,
    UnauthorizedActorError,
)


@pytest.fixture
def store(tmp_path: Path) -> TaskStore:
    """Fixture returning an isolated TaskStore backed by a temporary file."""
    db_file = tmp_path / "crew_test.sqlite"
    return TaskStore(db_file)


def test_task_creation_and_retrieval(store: TaskStore) -> None:
    task = store.create_task(
        room_id="room-dev",
        requester_id="user-123",
        requester_name="Alice",
        issue_source="owner/repo#42",
        request_key="github:owner/repo#42",
        thread_id="thread-99",
        initial_summary="Null pointer in auth handler",
        initial_triage_data={"severity": "high", "category": "bug"},
    )

    assert task.task_id is not None
    assert task.room_id == "room-dev"
    assert task.thread_id == "thread-99"
    assert task.requester_id == "user-123"
    assert task.requester_name == "Alice"
    assert task.status == TaskStatus.AWAITING_APPROVAL.value

    retrieved = store.get_task(task.task_id)
    assert retrieved is not None
    assert retrieved == task

    revisions = store.list_revisions(task.task_id)
    assert len(revisions) == 1
    assert revisions[0].revision_number == 1
    assert revisions[0].triage_summary == "Null pointer in auth handler"
    assert revisions[0].triage_data["severity"] == "high"


def test_task_ids_are_unique(store: TaskStore) -> None:
    t1 = store.create_task(
        room_id="room-1",
        requester_id="u1",
        requester_name="Alice",
        issue_source="repo#1",
        request_key="k1",
    )
    t2 = store.create_task(
        room_id="room-1",
        requester_id="u1",
        requester_name="Alice",
        issue_source="repo#2",
        request_key="k2",
    )

    assert t1.task_id != t2.task_id

    # Duplicate explicit task_id must fail on UNIQUE constraint
    with pytest.raises(sqlite3.IntegrityError):
        store.create_task(
            task_id=t1.task_id,
            room_id="room-1",
            requester_id="u1",
            requester_name="Alice",
            issue_source="repo#3",
            request_key="k3",
        )


def test_task_state_survives_restart(tmp_path: Path) -> None:
    db_file = tmp_path / "persistent.sqlite"

    # Session 1: create and populate
    store1 = TaskStore(db_file)
    task1 = store1.create_task(
        room_id="room-ops",
        requester_id="u-charlie",
        requester_name="Charlie",
        issue_source="repo#88",
        request_key="k-88",
        thread_id="thread-88",
    )
    store1.add_evidence(
        task_id=task1.task_id,
        evidence_type="reproduction",
        description="Reproduction failed with 404",
        command="pytest tests/test_auth.py",
        output="FAILED: 404 Not Found",
    )

    # Session 2: reopen store pointing to same database file
    store2 = TaskStore(db_file)
    reloaded_task = store2.get_task(task1.task_id)
    assert reloaded_task is not None
    assert reloaded_task.task_id == task1.task_id
    assert reloaded_task.status == TaskStatus.AWAITING_APPROVAL.value

    evidence = store2.list_evidence(task1.task_id)
    assert len(evidence) == 1
    assert evidence[0].command == "pytest tests/test_auth.py"
    assert "FAILED" in (evidence[0].output or "")


def test_multiple_tasks_in_same_room(store: TaskStore) -> None:
    t1 = store.create_task(
        room_id="shared-room",
        requester_id="u1",
        requester_name="Alice",
        issue_source="repo#10",
        request_key="k10",
    )
    t2 = store.create_task(
        room_id="shared-room",
        requester_id="u2",
        requester_name="Bob",
        issue_source="repo#20",
        request_key="k20",
    )
    t3 = store.create_task(
        room_id="other-room",
        requester_id="u3",
        requester_name="Carol",
        issue_source="repo#30",
        request_key="k30",
    )

    room_tasks = store.list_tasks(room_id="shared-room")
    assert len(room_tasks) == 2
    task_ids = {t.task_id for t in room_tasks}
    assert t1.task_id in task_ids
    assert t2.task_id in task_ids
    assert t3.task_id not in task_ids


def test_multiple_tasks_in_different_threads(store: TaskStore) -> None:
    t_no_thread = store.create_task(
        room_id="room-1",
        requester_id="u1",
        requester_name="Alice",
        issue_source="repo#1",
        request_key="k1",
        thread_id=None,
    )
    t_thread_a = store.create_task(
        room_id="room-1",
        requester_id="u1",
        requester_name="Alice",
        issue_source="repo#2",
        request_key="k2",
        thread_id="thread-A",
    )
    t_thread_b = store.create_task(
        room_id="room-1",
        requester_id="u2",
        requester_name="Bob",
        issue_source="repo#3",
        request_key="k3",
        thread_id="thread-B",
    )

    tasks_no_thread = store.list_tasks(room_id="room-1", thread_id=None)
    assert len(tasks_no_thread) == 1
    assert tasks_no_thread[0].task_id == t_no_thread.task_id

    tasks_a = store.list_tasks(room_id="room-1", thread_id="thread-A")
    assert len(tasks_a) == 1
    assert tasks_a[0].task_id == t_thread_a.task_id

    tasks_b = store.list_tasks(room_id="room-1", thread_id="thread-B")
    assert len(tasks_b) == 1
    assert tasks_b[0].task_id == t_thread_b.task_id


def test_wrong_thread_approval_is_rejected(store: TaskStore) -> None:
    # Case A: task is in thread-1, decision arrives for thread-2
    t_thread = store.create_task(
        room_id="room-1",
        requester_id="u-alice",
        requester_name="Alice",
        issue_source="repo#1",
        request_key="k1",
        thread_id="thread-1",
    )

    with pytest.raises(TaskThreadMismatchError) as exc_info:
        store.record_decision(
            task_id=t_thread.task_id,
            room_id="room-1",
            thread_id="thread-2",
            actor_id="u-alice",
            decision=DecisionType.APPROVED.value,
        )
    assert "belongs to thread 'thread-1'" in str(exc_info.value)

    # Case B: task is in thread-1, decision arrives with thread_id=None (room fallback attempt)
    with pytest.raises(TaskThreadMismatchError):
        store.record_decision(
            task_id=t_thread.task_id,
            room_id="room-1",
            thread_id=None,
            actor_id="u-alice",
            decision=DecisionType.APPROVED.value,
        )

    # Case C: task has no thread (thread_id=None), decision arrives with thread_id="thread-1"
    t_no_thread = store.create_task(
        room_id="room-1",
        requester_id="u-alice",
        requester_name="Alice",
        issue_source="repo#2",
        request_key="k2",
        thread_id=None,
    )

    with pytest.raises(TaskThreadMismatchError):
        store.record_decision(
            task_id=t_no_thread.task_id,
            room_id="room-1",
            thread_id="thread-1",
            actor_id="u-alice",
            decision=DecisionType.APPROVED.value,
        )

    # Verify task status was NOT modified
    assert store.get_task(t_thread.task_id).status == TaskStatus.AWAITING_APPROVAL.value
    assert store.get_task(t_no_thread.task_id).status == TaskStatus.AWAITING_APPROVAL.value


def test_wrong_room_approval_is_rejected(store: TaskStore) -> None:
    task = store.create_task(
        room_id="room-backend",
        requester_id="u-alice",
        requester_name="Alice",
        issue_source="repo#1",
        request_key="k1",
    )

    with pytest.raises(TaskRoomMismatchError) as exc_info:
        store.record_decision(
            task_id=task.task_id,
            room_id="room-frontend",
            thread_id=None,
            actor_id="u-alice",
            decision=DecisionType.APPROVED.value,
        )
    assert "belongs to room 'room-backend'" in str(exc_info.value)
    assert store.get_task(task.task_id).status == TaskStatus.AWAITING_APPROVAL.value


def test_different_user_cannot_approve_task(store: TaskStore) -> None:
    task = store.create_task(
        room_id="room-1",
        requester_id="u-alice",
        requester_name="Alice",
        issue_source="repo#1",
        request_key="k1",
    )

    with pytest.raises(UnauthorizedActorError) as exc_info:
        store.record_decision(
            task_id=task.task_id,
            room_id="room-1",
            thread_id=None,
            actor_id="u-bob",
            decision=DecisionType.APPROVED.value,
        )
    assert "Only requester 'u-alice'" in str(exc_info.value)
    assert store.get_task(task.task_id).status == TaskStatus.AWAITING_APPROVAL.value


def test_correct_requester_can_approve_exact_task(store: TaskStore) -> None:
    task = store.create_task(
        room_id="room-1",
        thread_id="thread-1",
        requester_id="u-alice",
        requester_name="Alice",
        issue_source="repo#1",
        request_key="k1",
    )

    approval = store.record_decision(
        task_id=task.task_id,
        room_id="room-1",
        thread_id="thread-1",
        actor_id="u-alice",
        decision=DecisionType.APPROVED.value,
        feedback="Looks solid, proceed to repro",
        approved_scope="Fix auth boundary without breaking existing tokens",
    )

    assert approval.task_id == task.task_id
    assert approval.actor_id == "u-alice"
    assert approval.decision == DecisionType.APPROVED.value
    assert approval.approved_scope == "Fix auth boundary without breaking existing tokens"

    updated = store.get_task(task.task_id)
    assert updated is not None
    assert updated.status == TaskStatus.APPROVED.value


def test_changes_requested_decision_recorded_correctly(store: TaskStore) -> None:
    task = store.create_task(
        room_id="room-1",
        requester_id="u-alice",
        requester_name="Alice",
        issue_source="repo#1",
        request_key="k1",
    )

    approval = store.record_decision(
        task_id=task.task_id,
        room_id="room-1",
        thread_id=None,
        actor_id="u-alice",
        decision=DecisionType.CHANGES_REQUESTED.value,
        feedback="Please add a regression test for Windows paths",
    )

    assert approval.decision == DecisionType.CHANGES_REQUESTED.value
    assert approval.feedback == "Please add a regression test for Windows paths"

    updated = store.get_task(task.task_id)
    assert updated is not None
    assert updated.status == TaskStatus.CHANGES_REQUESTED.value


def test_second_decision_cannot_overwrite_first(store: TaskStore) -> None:
    task = store.create_task(
        room_id="room-1",
        requester_id="u-alice",
        requester_name="Alice",
        issue_source="repo#1",
        request_key="k1",
    )

    # First decision: APPROVED
    store.record_decision(
        task_id=task.task_id,
        room_id="room-1",
        thread_id=None,
        actor_id="u-alice",
        decision=DecisionType.APPROVED.value,
    )

    # Attempt second decision
    with pytest.raises(TaskStateError) as exc_info:
        store.record_decision(
            task_id=task.task_id,
            room_id="room-1",
            thread_id=None,
            actor_id="u-alice",
            decision=DecisionType.CHANGES_REQUESTED.value,
            feedback="Wait, reconsider!",
        )
    assert "has status 'approved' and cannot receive a decision" in str(exc_info.value)

    # Status must remain 'approved'
    assert store.get_task(task.task_id).status == TaskStatus.APPROVED.value
    approvals = store.list_approvals(task.task_id)
    assert len(approvals) == 1


def test_invalid_decision_verdict_rejected(store: TaskStore) -> None:
    task = store.create_task(
        room_id="room-1",
        requester_id="u-alice",
        requester_name="Alice",
        issue_source="repo#1",
        request_key="k1",
    )

    with pytest.raises(InvalidDecisionError):
        store.record_decision(
            task_id=task.task_id,
            room_id="room-1",
            thread_id=None,
            actor_id="u-alice",
            decision="maybe_later",
        )


def test_invalid_state_transitions_rejected(store: TaskStore) -> None:
    task = store.create_task(
        room_id="room-1",
        requester_id="u-alice",
        requester_name="Alice",
        issue_source="repo#1",
        request_key="k1",
    )

    # Cannot jump directly from awaiting_approval to pr_published
    with pytest.raises(TaskStateError):
        store.update_task_status(
            task.task_id,
            TaskStatus.PR_PUBLISHED.value,
            actor_id="system",
        )

    # Transition to cancelled is allowed
    store.update_task_status(
        task.task_id,
        TaskStatus.CANCELLED.value,
        actor_id="u-alice",
        reason="User cancelled",
    )
    assert store.get_task(task.task_id).status == TaskStatus.CANCELLED.value

    # From cancelled, no further transitions are allowed
    with pytest.raises(TaskStateError):
        store.update_task_status(
            task.task_id,
            TaskStatus.APPROVED.value,
            actor_id="u-alice",
        )


def test_revisions_persist_and_numbering_correct(store: TaskStore) -> None:
    task = store.create_task(
        room_id="room-1",
        requester_id="u1",
        requester_name="Alice",
        issue_source="repo#1",
        request_key="k1",
        initial_summary="First plan",
    )

    rev2 = store.add_revision(
        task_id=task.task_id,
        triage_summary="Updated plan with mock fixtures",
        triage_data={"step": "revised"},
    )
    assert rev2.revision_number == 2

    rev3 = store.add_revision(
        task_id=task.task_id,
        triage_summary="Third revision after reviewer feedback",
        triage_data={"step": "final"},
    )
    assert rev3.revision_number == 3

    revisions = store.list_revisions(task.task_id)
    assert len(revisions) == 3
    assert [r.revision_number for r in revisions] == [1, 2, 3]

    # Explicit collision test on unique revision number per task
    with pytest.raises(DuplicateRevisionError):
        store.add_revision(
            task_id=task.task_id,
            revision_number=2,
            triage_summary="Conflicting revision 2",
            triage_data={},
        )


def test_evidence_persists_and_belongs_to_correct_task(store: TaskStore) -> None:
    t1 = store.create_task(
        room_id="room-1",
        requester_id="u1",
        requester_name="Alice",
        issue_source="repo#1",
        request_key="k1",
    )
    t2 = store.create_task(
        room_id="room-1",
        requester_id="u1",
        requester_name="Alice",
        issue_source="repo#2",
        request_key="k2",
    )

    ev1 = store.add_evidence(
        task_id=t1.task_id,
        evidence_type="repro",
        description="Bug reproduced with pytest",
        command="pytest -k test_repro",
        output="1 failed, exit code 1",
        commit_sha="a" * 40,
    )

    ev2 = store.add_evidence(
        task_id=t2.task_id,
        evidence_type="build",
        description="Patch build succeeded",
        command="pytest tests/",
        output="44 passed, exit code 0",
        commit_sha="b" * 40,
    )

    t1_evidence = store.list_evidence(t1.task_id)
    assert len(t1_evidence) == 1
    assert t1_evidence[0].evidence_id == ev1.evidence_id
    assert t1_evidence[0].commit_sha == "a" * 40

    t2_evidence = store.list_evidence(t2.task_id)
    assert len(t2_evidence) == 1
    assert t2_evidence[0].evidence_id == ev2.evidence_id


def test_audit_events_recorded_for_all_actions(store: TaskStore) -> None:
    task = store.create_task(
        room_id="room-1",
        requester_id="u-alice",
        requester_name="Alice",
        issue_source="repo#1",
        request_key="k1",
    )
    store.add_revision(task_id=task.task_id, triage_summary="Rev 1", triage_data={})
    store.add_evidence(task_id=task.task_id, evidence_type="test", description="Proof")
    store.record_decision(
        task_id=task.task_id,
        room_id="room-1",
        thread_id=None,
        actor_id="u-alice",
        decision=DecisionType.APPROVED.value,
    )
    store.update_task_status(task.task_id, TaskStatus.REPRODUCING.value, actor_id="builder-agent")

    events = store.list_audit_events(task.task_id)
    event_types = [e.event_type for e in events]
    assert event_types == [
        "TASK_CREATED",
        "REVISION_ADDED",
        "EVIDENCE_ADDED",
        "DECISION_RECORDED",
        "STATUS_CHANGED",
    ]


def test_foreign_key_constraints_prevent_orphaned_records(store: TaskStore) -> None:
    non_existent = "00000000-0000-0000-0000-000000000000"

    with pytest.raises(TaskNotFoundError):
        store.add_revision(
            task_id=non_existent,
            triage_summary="Orphan revision",
            triage_data={},
        )

    with pytest.raises(TaskNotFoundError):
        store.add_evidence(
            task_id=non_existent,
            evidence_type="orphan",
            description="Orphan evidence",
        )

    with pytest.raises(TaskNotFoundError):
        store.record_decision(
            task_id=non_existent,
            room_id="r",
            thread_id=None,
            actor_id="a",
            decision=DecisionType.APPROVED.value,
        )

    # Test direct SQL foreign-key enforcement
    with store._connection() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO task_evidence (
                    evidence_id, task_id, evidence_type, description, created_at
                ) VALUES ('e1', 'non-existent', 'raw', 'test', '2026-01-01');
                """
            )


def test_concurrent_decisions_race_condition(tmp_path: Path) -> None:
    db_file = tmp_path / "race_test.sqlite"
    store = TaskStore(db_file)

    task = store.create_task(
        room_id="room-race",
        requester_id="u-alice",
        requester_name="Alice",
        issue_source="repo#99",
        request_key="k99",
    )

    # Run two approvals simultaneously in separate threads
    results: list[str] = []

    def try_decide(actor: str) -> None:
        local_store = TaskStore(db_file)
        try:
            local_store.record_decision(
                task_id=task.task_id,
                room_id="room-race",
                thread_id=None,
                actor_id=actor,
                decision=DecisionType.APPROVED.value,
            )
            results.append("SUCCESS")
        except TaskStateError:
            results.append("TASK_STATE_ERROR")
        except Exception as exc:
            results.append(type(exc).__name__)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(try_decide, "u-alice")
        f2 = executor.submit(try_decide, "u-alice")
        f1.result()
        f2.result()

    # Exactly one decision must succeed, the other must be rejected by TaskStateError
    assert results.count("SUCCESS") == 1
    assert results.count("TASK_STATE_ERROR") == 1

    approvals = store.list_approvals(task.task_id)
    assert len(approvals) == 1
