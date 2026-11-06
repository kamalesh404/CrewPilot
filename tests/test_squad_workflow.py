"""End-to-end and component tests for the multi-agent squad workflow.

Verifies:
1. Triager connector + SQLite TaskStore with official Switch event payloads (string sender, message_id).
2. Reproducer connector + isolated subprocess sandbox + evidence persistence.
3. Reviewer connector + strict evidence verification (exact signature, exit code 1, clean leaks).
4. Truthful publication manifest authorization (no fake GitHub PRs).
5. Comprehensive negative tests (wrong error, wrong exit code, wrong repo, secret leaks).
6. Complete 3-agent Switch handoff chain (Triager -> Reproducer -> Reviewer).
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
from github_issues import IssueInput
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
            "message_id": "msg-00",
            "body": "@crewpilot-repro-builder reproduce task 12345678",
            "addressed": False,
            "sender": "user-1",
            "sender_name": "Alice",
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

    # Realistic official payload with string sender and message_id
    event = {
        "type": "message",
        "room_id": "room-1",
        "payload": {
            "message_id": "evt-1",
            "body": f"@crewpilot-repro-builder reproduce task {task.task_id}",
            "addressed": True,
            "thread_id": "th-1",
            "sender": "user-1",
            "sender_name": "Alice",
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
            "message_id": "evt-2",
            "body": f"@crewpilot-repro-builder reproduce task {task.task_id}",
            "addressed": True,
            "thread_id": "th-1",
            "sender": "user-1",
            "sender_name": "Alice",
        },
    }
    await repro_connector.handle_event(event)

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
    assert "'int' object has no attribute 'translate'" in (ev_list[0].output or "")


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

    # Reviewer receives review handoff with string sender
    event = {
        "type": "message",
        "room_id": "room-1",
        "payload": {
            "message_id": "evt-rev-1",
            "body": f"@crewpilot-reviewer review task {task.task_id}",
            "addressed": True,
            "thread_id": "th-1",
            "sender": "crewpilot-repro-builder",
            "sender_name": "Builder",
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
async def test_reviewer_rejects_wrong_attribute_error(
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
    # Output has wrong AttributeError ('str' instead of 'int' has no attribute 'translate')
    store.add_evidence(
        task_id=task.task_id,
        evidence_type="reproduction_probe",
        description="psf/requests#7634 (exit 1)",
        command="python repro_probe.py",
        output="AttributeError: 'str' object has no attribute 'unrelated_field'",
        artifact_path="/tmp/repro.py",
        actor_id="builder",
    )
    store.update_task_status(task.task_id, TaskStatus.UNDER_REVIEW.value, actor_id="builder")

    await reviewer_connector.handle_event(
        {
            "type": "message",
            "room_id": "room-1",
            "payload": {
                "message_id": "evt-rev-wrong-err",
                "body": f"@crewpilot-reviewer review task {task.task_id}",
                "addressed": True,
                "thread_id": "th-1",
                "sender": "crewpilot-repro-builder",
                "sender_name": "Builder",
            },
        }
    )

    assert "REVISION REQUIRED" in fake_reviewer_client.posted_messages[-1][1]
    assert "Missing expected error signature" in fake_reviewer_client.posted_messages[-1][1]
    assert store.get_task(task.task_id).status == TaskStatus.CHANGES_REQUESTED.value


@pytest.mark.asyncio
async def test_reviewer_rejects_empty_output(
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
        description="psf/requests#7634 (exit 1)",
        command="python repro_probe.py",
        output="",  # Empty output!
        artifact_path="/tmp/repro.py",
        actor_id="builder",
    )
    store.update_task_status(task.task_id, TaskStatus.UNDER_REVIEW.value, actor_id="builder")

    await reviewer_connector.handle_event(
        {
            "type": "message",
            "room_id": "room-1",
            "payload": {
                "message_id": "evt-rev-empty",
                "body": f"@crewpilot-reviewer review task {task.task_id}",
                "addressed": True,
                "thread_id": "th-1",
                "sender": "crewpilot-repro-builder",
                "sender_name": "Builder",
            },
        }
    )

    assert "REVISION REQUIRED" in fake_reviewer_client.posted_messages[-1][1]
    assert "Empty output" in fake_reviewer_client.posted_messages[-1][1]
    assert store.get_task(task.task_id).status == TaskStatus.CHANGES_REQUESTED.value


@pytest.mark.asyncio
async def test_reviewer_rejects_unexpected_exit_code(
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
        description="psf/requests#7634 passed (exit 0)",  # Exit 0 instead of 1
        command="python repro_probe.py",
        output="AttributeError: 'int' object has no attribute 'translate'",
        artifact_path="/tmp/repro.py",
        actor_id="builder",
    )
    store.update_task_status(task.task_id, TaskStatus.UNDER_REVIEW.value, actor_id="builder")

    await reviewer_connector.handle_event(
        {
            "type": "message",
            "room_id": "room-1",
            "payload": {
                "message_id": "evt-rev-exit0",
                "body": f"@crewpilot-reviewer review task {task.task_id}",
                "addressed": True,
                "thread_id": "th-1",
                "sender": "crewpilot-repro-builder",
                "sender_name": "Builder",
            },
        }
    )

    assert "REVISION REQUIRED" in fake_reviewer_client.posted_messages[-1][1]
    assert "Exit code was not 1" in fake_reviewer_client.posted_messages[-1][1]


@pytest.mark.asyncio
async def test_reviewer_rejects_unsupported_issue_target(
    reviewer_connector: SwitchReviewerConnector,
    fake_reviewer_client: FakeSwitchClient,
    store: TaskStore,
) -> None:
    # Issue target is flask instead of requests
    task = store.create_task(
        room_id="room-1",
        thread_id="th-1",
        requester_id="user-1",
        requester_name="Alice",
        issue_source="pallets/flask#7634",
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

    await reviewer_connector.handle_event(
        {
            "type": "message",
            "room_id": "room-1",
            "payload": {
                "message_id": "evt-rev-unsupported",
                "body": f"@crewpilot-reviewer review task {task.task_id}",
                "addressed": True,
                "thread_id": "th-1",
                "sender": "crewpilot-repro-builder",
                "sender_name": "Builder",
            },
        }
    )

    assert "Unsupported Target" in fake_reviewer_client.posted_messages[-1][1]


@pytest.mark.asyncio
async def test_reviewer_rejects_secret_leak_in_output(
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
    # Output has leaked token!
    store.add_evidence(
        task_id=task.task_id,
        evidence_type="reproduction_probe",
        description="psf/requests#7634 (exit 1)",
        command="python repro_probe.py",
        output="AttributeError: 'int' object has no attribute 'translate'\nToken: sk-proj12345678901234567890",
        artifact_path="/tmp/repro.py",
        actor_id="builder",
    )
    store.update_task_status(task.task_id, TaskStatus.UNDER_REVIEW.value, actor_id="builder")

    await reviewer_connector.handle_event(
        {
            "type": "message",
            "room_id": "room-1",
            "payload": {
                "message_id": "evt-rev-leak",
                "body": f"@crewpilot-reviewer review task {task.task_id}",
                "addressed": True,
                "thread_id": "th-1",
                "sender": "crewpilot-repro-builder",
                "sender_name": "Builder",
            },
        }
    )

    assert "REVISION REQUIRED" in fake_reviewer_client.posted_messages[-1][1]
    assert "secret or credential pattern detected" in fake_reviewer_client.posted_messages[-1][1]


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

    # 1. Unauthorized user tries to publish (string sender: "mallory-attacker")
    unauth_event = {
        "type": "message",
        "room_id": "room-1",
        "payload": {
            "message_id": "evt-pub-1",
            "body": f"@crewpilot-reviewer publish task {task.task_id}",
            "addressed": True,
            "thread_id": "th-1",
            "sender": "mallory-attacker",
            "sender_name": "Mallory",
        },
    }
    await reviewer_connector.handle_event(unauth_event)
    assert (
        "Only Alice can authorize publication manifest preparation"
        in fake_reviewer_client.posted_messages[-1][1]
    )

    # Task status must NOT be changed
    assert store.get_task(task.task_id).status == TaskStatus.REVIEW_APPROVED.value

    # 2. Authorized requester publishes with official string sender ("user-1")
    auth_event = {
        "type": "message",
        "room_id": "room-1",
        "payload": {
            "message_id": "evt-pub-2",
            "body": f"@crewpilot-reviewer publish task {task.task_id}",
            "addressed": True,
            "thread_id": "th-1",
            "sender": "user-1",
            "sender_name": "Alice",
        },
    }
    await reviewer_connector.handle_event(auth_event)
    assert "Publication Manifest Prepared" in fake_reviewer_client.posted_messages[-1][1]
    assert "No remote PR published" in fake_reviewer_client.posted_messages[-1][1]
    assert store.get_task(task.task_id).status == TaskStatus.PUBLICATION_AUTHORIZED.value


@pytest.mark.asyncio
async def test_full_squad_handoff_integration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Full end-to-end integration test simulating the real 3-agent Squad in Switch."""
    db_file = tmp_path / "full_squad.db"
    store = TaskStore(db_file)

    async def mock_load_issue(raw: str, token: str | None = None) -> IssueInput:
        return IssueInput(
            text="Multipart non-string field keys cause AttributeError during encode",
            source="psf/requests#7634",
        )

    monkeypatch.setattr("switch.switch_connector.load_issue", mock_load_issue)

    triager_client = FakeSwitchClient("crewpilot-triager")
    repro_client = FakeSwitchClient("crewpilot-repro-builder")
    reviewer_client = FakeSwitchClient("crewpilot-reviewer")

    mock_agent = MagicMock()
    mock_result = MagicMock()
    mock_result.category = "bug"
    mock_result.severity = "medium"
    mock_result.difficulty = "easy"
    mock_result.summary = "Fix multipart non-string form key encoding"
    mock_result.suggested_fix_plan = ["Convert key to str before translation"]
    mock_result.to_dict.return_value = {
        "category": "bug",
        "severity": "medium",
        "difficulty": "easy",
        "summary": "Fix multipart non-string form key encoding",
        "suggested_fix_plan": ["Convert key to str before translation"],
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

    # Step 1: User requests triage in Switch room with documented string sender
    await triager.handle_event(
        {
            "type": "message",
            "room_id": room,
            "payload": {
                "message_id": "step1-msg",
                "body": "@crewpilot-triager triage https://github.com/psf/requests/issues/7634",
                "addressed": True,
                "thread_id": thread,
                "sender": "maintainer-kamal",
                "sender_name": "Kamal",
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
                "message_id": "step2-msg",
                "body": f"@crewpilot-triager approve task {task_id}",
                "addressed": True,
                "thread_id": thread,
                "sender": "maintainer-kamal",
                "sender_name": "Kamal",
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
                "message_id": "step3-msg",
                "body": triager_handoff,
                "addressed": True,
                "thread_id": thread,
                "sender": "crewpilot-triager",
                "sender_name": "Triager",
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
                "message_id": "step4-msg",
                "body": repro_handoff,
                "addressed": True,
                "thread_id": thread,
                "sender": "crewpilot-repro-builder",
                "sender_name": "Builder",
            },
        }
    )

    # Reviewer verifies the reproduction evidence
    assert store.get_task(task_id).status == TaskStatus.REVIEW_APPROVED.value
    review_post = reviewer_client.posted_messages[-1][1]
    assert "EVIDENCE VERIFIED" in review_post

    # Step 5: Maintainer authorizes publication manifest preparation
    await reviewer.handle_event(
        {
            "type": "message",
            "room_id": room,
            "payload": {
                "message_id": "step5-msg",
                "body": f"@crewpilot-reviewer publish task {task_id}",
                "addressed": True,
                "thread_id": thread,
                "sender": "maintainer-kamal",
                "sender_name": "Kamal",
            },
        }
    )

    # Task transitions to PUBLICATION_AUTHORIZED with complete audit trail
    assert store.get_task(task_id).status == TaskStatus.PUBLICATION_AUTHORIZED.value
    publish_post = reviewer_client.posted_messages[-1][1]
    assert "Publication Manifest Prepared" in publish_post
    assert "No remote PR published" in publish_post
    assert (
        "The publication manifest and complete audit trail are recorded in durable SQLite"
        in publish_post
    )
