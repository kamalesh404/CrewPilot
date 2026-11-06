"""Executable end-to-end demo script for CrewPilot multi-agent squad.

Simulates the full Flip the Switch Hackathon workflow with authentic components:
  1. Human maintainer mentions Triager with GitHub issue (psf/requests#7634).
  2. Triager generates structured plan and stores task in durable SQLite store.
  3. Maintainer approves triage plan: `@crewpilot-triager approve task <task-id>`.
  4. Triager emits handoff: `@crewpilot-repro-builder reproduce task <task-id>`.
  5. Reproducer executes isolated subprocess probe reproducing psf/requests#7634
     (AttributeError on non-string key in multipart files).
  6. Reproducer records evidence in SQLite and hands off to Reviewer:
     `@crewpilot-reviewer review task <task-id>`.
  7. Reviewer independently audits evidence, verifies zero credential leaks and
     exact exit code, and approves evidence.
  8. Human maintainer authorizes publication: `@crewpilot-reviewer publish task <task-id>`.
  9. Reviewer generates Draft PR manifest and audit log.

Run with:
    python demo/simulate_squad_demo.py
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Any

from agent.triage_agent import TriageResult
from storage.task_store import TaskStatus, TaskStore
from switch.repro_connector import SwitchReproConnector
from switch.reviewer_connector import SwitchReviewerConnector
from switch.switch_connector import SwitchTriageConnector

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class DemoSwitchClient:
    """Simulated Switch client that outputs live room messages."""

    def __init__(self, agent_name: str) -> None:
        self.agent_name = agent_name
        self.history: list[tuple[str, str]] = []

    async def post_message(
        self,
        room_id: str,
        text: str,
        thread_id: str | None = None,
    ) -> dict[str, Any]:
        self.history.append((room_id, text))
        prefix = f"[{self.agent_name.upper()}]"
        print(f"\n{prefix} posted to #{room_id} (thread: {thread_id}):")
        print("-" * 70)
        print(text)
        print("-" * 70)
        await asyncio.sleep(0.3)
        return {"status": "ok"}


class MockTriageAgent:
    """Mock LLM agent producing structured triage for psf/requests#7634."""

    def triage(self, issue_text: str) -> TriageResult:
        return TriageResult(
            category="bug",
            severity="medium",
            difficulty="easy",
            summary=(
                "When preparing a multipart request with files, non-string form field names "
                "in `data` cause an AttributeError ('int' object has no attribute 'translate') "
                "in urllib3/fields.py."
            ),
            suggested_fix_plan=[
                "In requests/models.py `_encode_files`, ensure field names are converted to `str(k)`.",
                "Add regression test in tests/test_requests.py with integer key in multipart data.",
                "Verify backward compatibility with string and byte keys.",
            ],
        )


async def run_demo() -> None:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    print("=" * 70)
    print("  CREWPILOT — EVIDENCE-DRIVEN BUG INVESTIGATION SQUAD")
    print("  Flip the Switch Hackathon: Best Business Workflow Demo")
    print("=" * 70)
    print(f"Timestamp (UTC): {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())}")
    print("Target Bug: psf/requests#7634 (Multipart non-string field name crash)")
    print()

    db_path = PROJECT_ROOT / "crewpilot_demo.db"
    if db_path.exists():
        try:
            db_path.unlink()
        except OSError:
            pass

    store = TaskStore(db_path)
    print(f"[SYSTEM] Durable SQLite TaskStore initialized at: {db_path.name}")

    triager_client = DemoSwitchClient("crewpilot-triager")
    repro_client = DemoSwitchClient("crewpilot-repro-builder")
    reviewer_client = DemoSwitchClient("crewpilot-reviewer")

    triager = SwitchTriageConnector(
        client=triager_client,  # type: ignore[arg-type]
        triage_agent=MockTriageAgent(),  # type: ignore[arg-type]
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

    room = "incident-response"
    thread = "thread-7634"
    maintainer_id = "maintainer-vincenzo"
    maintainer_name = "Vincenzo"

    print("\n>>> STEP 1: Maintainer reports issue in Switch room")
    print(
        f"[{maintainer_name}] @crewpilot-triager triage https://github.com/psf/requests/issues/7634"
    )
    await triager.handle_event(
        {
            "type": "message",
            "room_id": room,
            "payload": {
                "id": "msg-01",
                "body": "@crewpilot-triager triage https://github.com/psf/requests/issues/7634",
                "addressed": True,
                "thread_id": thread,
                "sender": {"id": maintainer_id, "name": maintainer_name},
            },
        }
    )

    tasks = store.list_tasks(room_id=room)
    assert tasks, "Task was not created"
    task = tasks[0]
    task_id = task.task_id
    print(f"[STORE] Task created: {task_id} (Status: {task.status})")

    print(f"\n>>> STEP 2: Maintainer approves triage plan for task {task_id}")
    print(f"[{maintainer_name}] @crewpilot-triager approve task {task_id}")
    await triager.handle_event(
        {
            "type": "message",
            "room_id": room,
            "payload": {
                "id": "msg-02",
                "body": f"@crewpilot-triager approve task {task_id}",
                "addressed": True,
                "thread_id": thread,
                "sender": {"id": maintainer_id, "name": maintainer_name},
            },
        }
    )

    # Handoff message from Triager to Reproducer
    triager_handoff = [
        msg[1] for msg in triager_client.history if f"reproduce task {task_id}" in msg[1]
    ][0]

    print("\n>>> STEP 3: Switch delivers handoff to Reproducer / Builder agent")
    print(f"[SWITCH BRIDGE] Delivered to @crewpilot-repro-builder: '{triager_handoff}'")
    await reproducer.handle_event(
        {
            "type": "message",
            "room_id": room,
            "payload": {
                "id": "msg-03",
                "body": triager_handoff,
                "addressed": True,
                "thread_id": thread,
                "sender": {"id": "crewpilot-triager", "name": "Triager"},
            },
        }
    )

    # Handoff message from Reproducer to Reviewer
    repro_handoff = [msg[1] for msg in repro_client.history if f"review task {task_id}" in msg[1]][
        0
    ]

    print("\n>>> STEP 4: Switch delivers handoff to Reviewer agent")
    print(f"[SWITCH BRIDGE] Delivered to @crewpilot-reviewer: '{repro_handoff}'")
    await reviewer.handle_event(
        {
            "type": "message",
            "room_id": room,
            "payload": {
                "id": "msg-04",
                "body": repro_handoff,
                "addressed": True,
                "thread_id": thread,
                "sender": {"id": "crewpilot-repro-builder", "name": "Builder"},
            },
        }
    )

    print("\n>>> STEP 5: Maintainer authorizes Draft PR publication")
    print(f"[{maintainer_name}] @crewpilot-reviewer publish task {task_id}")
    await reviewer.handle_event(
        {
            "type": "message",
            "room_id": room,
            "payload": {
                "id": "msg-05",
                "body": f"@crewpilot-reviewer publish task {task_id}",
                "addressed": True,
                "thread_id": thread,
                "sender": {"id": maintainer_id, "name": maintainer_name},
            },
        }
    )

    print("\n" + "=" * 70)
    print("  DEMO EXECUTION COMPLETE — VERIFYING DURABLE AUDIT LOG")
    print("=" * 70)

    final_task = store.get_task(task_id)
    assert final_task is not None
    print(f"Final Task Status: {final_task.status}")
    assert final_task.status == TaskStatus.PR_PUBLISHED.value

    evidence = store.list_evidence(task_id)
    print(f"Audited Evidence Items: {len(evidence)}")
    for ev in evidence:
        print(f"  - [{ev.evidence_type}] {ev.description}")
        print(f"    Command: {ev.command}")

    approvals = store.list_approvals(task_id)
    print(f"Recorded Approvals: {len(approvals)}")
    for app in approvals:
        print(f"  - Actor: {app.actor_id} -> Decision: {app.decision}")

    audit_events = store.list_audit_events(task_id)
    print(f"Immutable Audit Trail Records: {len(audit_events)}")
    for ev in audit_events:
        print(f"  - [{ev.created_at}] {ev.event_type} by {ev.actor_id}")

    print("\nSUCCESS: End-to-end evidence-driven workflow verified 100%!")


if __name__ == "__main__":
    asyncio.run(run_demo())
