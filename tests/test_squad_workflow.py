"""End-to-end and component tests for the multi-agent squad workflow.

Verifies:
1. Triager connector + SQLite TaskStore.
2. Reproducer connector + isolated subprocess sandbox + evidence persistence.
3. Reviewer connector + independent evidence audit + Draft PR authorization.
4. Complete 3-agent Switch handoff chain (Triager -> Reproducer -> Reviewer).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from storage.task_store import (
    DecisionType,
    TaskStatus,
    TaskStore,
)
from switch.repro_connector import SwitchReproConnector
from switch.reviewer_connector import SwitchReviewerConnector
from switch.switch_connector import SwitchTriageConnector


class FakeSwitchClient:
    """Mock Switch client that captures all posted messages."""

    def __init__(self, agent_name: str = "agent") -> None:
        self.agent_name = agent_name
        self.posted_messages: list[tuple[str, str, str | None]] = []

    async def post_message(
        self,
        room_id: str,
        text: str,
        thread_id: str | None = None,
    ) -> dict[str, Any]:
        self.posted_messages.append((room_id, text, thread_id))
        return {"id": f"msg-{len(self.posted_messages)}", "status": "ok"}


@pytest.fixture
def store(tmp_path: Path) -> TaskStore:
    db_file = tmp_path / "squad_test.db"
    return TaskStore(db_file)


@pytest.fixture
def fake_repro_client() -> FakeSwitchClient:
    return FakeSwitchClient(agent_name="crewpilot-repro-builder")


@pytest.fixture
def fake_reviewer_client() -> FakeSwitchClient:
    return FakeSwitchClient(agent_name="crewpilot-reviewer")


@pytest.fixture
def repro_connector(fake_repro_client: FakeSwitchClient, store: TaskStore) -> SwitchReproConnector:
    return SwitchReproConnector(
        client=fake_repro_client,  # type: ignore[arg-type]
        store=store,
        agent_name="crewpilot-repro-builder",
        reviewer_agent_name="crewpilot-reviewer",
    )


@pytest.fixture
def reviewer_connector(
    fake_reviewer_client: FakeSwitchClient, store: TaskStore
) -> SwitchReviewerConnector:
    return SwitchReviewerConnector(
        client=fake_reviewer_client,  # type: ignore[arg-type]
        store=store,
        agent_name="crewpilot-reviewer",
    )


@pytest.mark.asyncio
async def test_repro_ignores_unaddressed_event(
    repro_connector: SwitchReproConnector, fake_repro_client: FakeSwitchClient
) -> None:
    event = {
        "type": "message",
        "room_id": "room-1",
        "payload": {
            "body": "@crewpilot-repro-builder reproduce task 12345678",
            "addressed": False,
        },
    }
    await repro_connector.handle_event(event)
    assert len(fake_repro_client.posted_messages) == 0


@pytest.mark.asyncio
async def test_repro_rejects_unapproved_task(
    repro_connector: SwitchReproConnector,
    fake_repro_client: FakeSwitchClient,
    store: TaskStore,
) -> None:
    task = store.create_task(
        room_id="room-1",
        thread_id="th-1",
        requester_id="user-1",
        requester_name="Alice",
        issue_source="psf/requests#7634",
        request_key="k1",
    )

    event = {
        "type": "message",
        "room_id": "room-1",
        "payload": {
            "id": "evt-1",
            "body": f"@crewpilot-repro-builder reproduce task {task.task_id}",
            "addressed": True,
            "thread_id": "th-1",
            "sender": {"id": "user-1", "name": "Alice"},
        },
    }
    await repro_connector.handle_event(event)

    assert len(fake_repro_client.posted_messages) == 1
    assert "awaiting maintainer approval" in fake_repro_client.posted_messages[0][1]


@pytest.mark.asyncio
async def test_repro_executes_for_approved_task(
    repro_connector: SwitchReproConnector,
    fake_repro_client: FakeSwitchClient,
    store: TaskStore,
) -> None:
    task = store.create_task(
        room_id="room-1",
        thread_id="th-1",
        requester_id="user-1",
        requester_name="Alice",
        issue_source="psf/requests#7634",
        request_key="k1",
    )
    # Human maintainer approves the task
    store.record_decision(
        task_id=task.task_id,
        room_id="room-1",
        thread_id="th-1",
        actor_id="user-1",
        decision=DecisionType.APPROVED.value,
    )

    event = {
        "type": "message",
        "room_id": "room-1",
        "payload": {
            "id": "evt-2",
            "body": f"@crewpilot-repro-builder reproduce task {task.task_id}",
            "addressed": True,
            "thread_id": "th-1",
            "sender": {"id": "user-1", "name": "Alice"},
        },
    }
    await repro_connector.handle_event(event)

    # Messages posted:
    # 1. Sandbox progress notification
    # 2. Reproduction report (success)
    # 3. Handoff to @crewpilot-reviewer review task <task-id>
    assert len(fake_repro_client.posted_messages) == 3
    assert "Reproducing bug" in fake_repro_client.posted_messages[0][1]
    assert "Bug Reproduction Confirmed" in fake_repro_client.posted_messages[1][1]
    assert (
        f"@crewpilot-reviewer review task {task.task_id}" == fake_repro_client.posted_messages[2][1]
    )

    # Verify task state in store
    updated = store.get_task(task.task_id)
    assert updated is not None
    assert updated.status == TaskStatus.UNDER_REVIEW.value

    # Verify evidence recorded
    ev_list = store.list_evidence(task.task_id)
    assert len(ev_list) == 1
    assert "exit 1" in (ev_list[0].description or "")
    assert "AttributeError" in (ev_list[0].output or "")


@pytest.mark.asyncio
async def test_reviewer_verifies_evidence_and_approves(
    reviewer_connector: SwitchReviewerConnector,
    fake_reviewer_client: FakeSwitchClient,
    store: TaskStore,
) -> None:
    task = store.create_task(
        room_id="room-1",
        thread_id="th-1",
        requester_id="user-1",
        requester_name="Alice",
        issue_source="psf/requests#7634",
        request_key="k1",
    )
    store.record_decision(
        task_id=task.task_id,
        room_id="room-1",
        thread_id="th-1",
        actor_id="user-1",
        decision=DecisionType.APPROVED.value,
    )
    store.update_task_status(task.task_id, TaskStatus.REPRODUCING.value, actor_id="builder")
    store.add_evidence(
        task_id=task.task_id,
        evidence_type="reproduction_probe",
        description="psf/requests#7634 reproduced (exit 1)",
        command="python repro_probe.py",
        output="REPRODUCED_BUG: AttributeError encountered: 'int' object has no attribute 'translate'",
        artifact_path="/tmp/repro.py",
        actor_id="builder",
        commit_sha=None,
    )
    store.update_task_status(task.task_id, TaskStatus.UNDER_REVIEW.value, actor_id="builder")

    # Reviewer receives review handoff
    event = {
        "type": "message",
        "room_id": "room-1",
        "payload": {
            "id": "evt-rev-1",
            "body": f"@crewpilot-reviewer review task {task.task_id}",
            "addressed": True,
            "thread_id": "th-1",
            "sender": {"id": "crewpilot-repro-builder", "name": "Builder"},
        },
    }
    await reviewer_connector.handle_event(event)

    assert len(fake_reviewer_client.posted_messages) == 1
    review_msg = fake_reviewer_client.posted_messages[0][1]
    assert "EVIDENCE VERIFIED" in review_msg
    assert "Zero credentials leaked" in review_msg

    # Verify task state
    updated = store.get_task(task.task_id)
    assert updated is not None
    assert updated.status == TaskStatus.REVIEW_APPROVED.value


@pytest.mark.asyncio
async def test_reviewer_publish_flow_authorization_enforced(
    reviewer_connector: SwitchReviewerConnector,
    fake_reviewer_client: FakeSwitchClient,
    store: TaskStore,
) -> None:
    task = store.create_task(
        room_id="room-1",
        thread_id="th-1",
        requester_id="user-1",
        requester_name="Alice",
        issue_source="psf/requests#7634",
        request_key="k1",
    )
    store.record_decision(
        task_id=task.task_id,
        room_id="room-1",
        thread_id="th-1",
        actor_id="user-1",
        decision=DecisionType.APPROVED.value,
    )
    store.update_task_status(task.task_id, TaskStatus.REPRODUCING.value, actor_id="builder")
    store.update_task_status(task.task_id, TaskStatus.UNDER_REVIEW.value, actor_id="builder")
    store.update_task_status(task.task_id, TaskStatus.REVIEW_APPROVED.value, actor_id="reviewer")

    # 1. Unauthorized user tries to publish
    unauth_event = {
        "type": "message",
        "room_id": "room-1",
        "payload": {
            "id": "evt-pub-1",
            "body": f"@crewpilot-reviewer publish task {task.task_id}",
            "addressed": True,
            "thread_id": "th-1",
            "sender": {"id": "mallory-attacker", "name": "Mallory"},
        },
    }
    await reviewer_connector.handle_event(unauth_event)
    assert (
        "Only Alice can authorize Draft PR publication"
        in fake_reviewer_client.posted_messages[-1][1]
    )

    # Task status must NOT be changed
    assert store.get_task(task.task_id).status == TaskStatus.REVIEW_APPROVED.value

    # 2. Authorized requester publishes
    auth_event = {
        "type": "message",
        "room_id": "room-1",
        "payload": {
            "id": "evt-pub-2",
            "body": f"@crewpilot-reviewer publish task {task.task_id}",
            "addressed": True,
            "thread_id": "th-1",
            "sender": {"id": "user-1", "name": "Alice"},
        },
    }
    await reviewer_connector.handle_event(auth_event)
    assert "Draft PR Manifest Prepared" in fake_reviewer_client.posted_messages[-1][1]
    assert store.get_task(task.task_id).status == TaskStatus.PR_PUBLISHED.value


@pytest.mark.asyncio
async def test_full_squad_handoff_integration(tmp_path: Path) -> None:
    """Full end-to-end integration test simulating the real 3-agent Squad in Switch."""
    db_file = tmp_path / "full_squad.db"
    store = TaskStore(db_file)

    triager_client = FakeSwitchClient("crewpilot-triager")
    repro_client = FakeSwitchClient("crewpilot-repro-builder")
    reviewer_client = FakeSwitchClient("crewpilot-reviewer")

    mock_agent = MagicMock()
    mock_result = MagicMock()
    mock_result.category = "bug"
    mock_result.severity = "medium"
    mock_result.difficulty = "easy"
    mock_result.summary = "Fix multipart non-string form key encoding"
    mock_result.root_cause = "Non-string key causes AttributeError during encode"
    mock_result.steps_to_fix = ["Convert key to str before translation"]
    mock_result.confidence = 0.95
    mock_result.to_dict.return_value = {
        "category": "bug",
        "severity": "medium",
        "difficulty": "easy",
        "summary": "Fix multipart non-string form key encoding",
        "root_cause": "Non-string key causes AttributeError during encode",
        "steps_to_fix": ["Convert key to str before translation"],
        "confidence": 0.95,
    }
    mock_agent.triage.return_value = mock_result

    triager = SwitchTriageConnector(
        client=triager_client,  # type: ignore[arg-type]
        triage_agent=mock_agent,
        agent_name="crewpilot-triager",
        store=store,
        reproducer_agent_name="crewpilot-repro-builder",
    )
    reproducer = SwitchReproConnector(
        client=repro_client,  # type: ignore[arg-type]
        store=store,
        agent_name="crewpilot-repro-builder",
        reviewer_agent_name="crewpilot-reviewer",
    )
    reviewer = SwitchReviewerConnector(
        client=reviewer_client,  # type: ignore[arg-type]
        store=store,
        agent_name="crewpilot-reviewer",
    )

    room = "test-room"
    thread = "test-thread"

    # Step 1: User requests triage in Switch room
    await triager.handle_event(
        {
            "type": "message",
            "room_id": room,
            "payload": {
                "id": "step1-msg",
                "body": "@crewpilot-triager triage https://github.com/psf/requests/issues/7634",
                "addressed": True,
                "thread_id": thread,
                "sender": {"id": "maintainer-kamal", "name": "Kamal"},
            },
        }
    )

    tasks = store.list_tasks(room_id=room)
    assert len(tasks) == 1
    task_id = tasks[0].task_id
    assert tasks[0].status == TaskStatus.AWAITING_APPROVAL.value

    # Step 2: Human maintainer approves the triage plan
    await triager.handle_event(
        {
            "type": "message",
            "room_id": room,
            "payload": {
                "id": "step2-msg",
                "body": f"@crewpilot-triager approve task {task_id}",
                "addressed": True,
                "thread_id": thread,
                "sender": {"id": "maintainer-kamal", "name": "Kamal"},
            },
        }
    )

    # Triager must have posted the handoff to Reproducer:
    # "@crewpilot-repro-builder reproduce task <task-id>"
    triager_handoff = [
        msg[1] for msg in triager_client.posted_messages if f"reproduce task {task_id}" in msg[1]
    ][0]
    assert triager_handoff == f"@crewpilot-repro-builder reproduce task {task_id}"
    assert store.get_task(task_id).status == TaskStatus.APPROVED.value

    # Step 3: Switch delivers the handoff to Reproducer agent
    await reproducer.handle_event(
        {
            "type": "message",
            "room_id": room,
            "payload": {
                "id": "step3-msg",
                "body": triager_handoff,
                "addressed": True,
                "thread_id": thread,
                "sender": {"id": "crewpilot-triager", "name": "Triager"},
            },
        }
    )

    # Reproducer must have run the reproduction probe and posted handoff to Reviewer:
    # "@crewpilot-reviewer review task <task-id>"
    repro_handoff = [
        msg[1] for msg in repro_client.posted_messages if f"review task {task_id}" in msg[1]
    ][0]
    assert repro_handoff == f"@crewpilot-reviewer review task {task_id}"
    assert store.get_task(task_id).status == TaskStatus.UNDER_REVIEW.value

    # Step 4: Switch delivers the handoff to Reviewer agent
    await reviewer.handle_event(
        {
            "type": "message",
            "room_id": room,
            "payload": {
                "id": "step4-msg",
                "body": repro_handoff,
                "addressed": True,
                "thread_id": thread,
                "sender": {"id": "crewpilot-repro-builder", "name": "Builder"},
            },
        }
    )

    # Reviewer verifies the reproduction evidence
    assert store.get_task(task_id).status == TaskStatus.REVIEW_APPROVED.value
    review_post = reviewer_client.posted_messages[-1][1]
    assert "EVIDENCE VERIFIED" in review_post

    # Step 5: Maintainer authorizes Draft PR publication
    await reviewer.handle_event(
        {
            "type": "message",
            "room_id": room,
            "payload": {
                "id": "step5-msg",
                "body": f"@crewpilot-reviewer publish task {task_id}",
                "addressed": True,
                "thread_id": thread,
                "sender": {"id": "maintainer-kamal", "name": "Kamal"},
            },
        }
    )

    # Task transitions to PR_PUBLISHED with complete audit trail
    assert store.get_task(task_id).status == TaskStatus.PR_PUBLISHED.value
    publish_post = reviewer_client.posted_messages[-1][1]
    assert "Draft PR Manifest Prepared" in publish_post
    assert (
        "All evidence, approval logs, and audit trails are persisted in durable SQLite"
        in publish_post
    )
