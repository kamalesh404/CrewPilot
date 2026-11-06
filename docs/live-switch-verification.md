# Live Switch Verification & Verification Protocol

## 1. Overview & Current Status

This document establishes the formal verification criteria for CrewPilot's integration with the Switch agent platform and records the current live environment status.

### Current Live Status (As of November 6, 2026)
- **Local Switch Server Status:** **OFFLINE** (Ports 8000, 3000, and 8080 checked; connection refused / unreachable).
- **Credentials:** No active `.switch/` or live agent tokens provisioned in local working tree.
- **Offline Simulation & Test Suite:** **PASSING (84/84 tests passed, end-to-end simulator verified)**.

> [!IMPORTANT]
> **Honest Evidence Boundary:**
> `demo/simulate_squad_demo.py` and `tests/test_squad_workflow.py` verify connector orchestration, exact error reproduction, payload compatibility, and durable SQLite state. **They do not constitute proof of live network routing through a running Switch daemon.** Live Switch verification requires completing the 8-point checklist below against a live server.

---

## 2. Distinction: Offline Simulator vs. Live Switch

| Attribute | Offline Simulator (`demo/simulate_squad_demo.py`) | Live Switch Integration (`main.py --role squad`) |
| :--- | :--- | :--- |
| **Transport** | In-memory `DemoSwitchClient` event delivery | Live HTTP SSE event stream (`GET /agents/{id}/events`) & HTTP POST operations (`ops/post_message`) |
| **Agent Identities** | 3 simulated participant IDs in memory | 3 authenticated Switch identities with distinct cryptographic API keys |
| **Room Routing** | Programmatic event dispatch to connectors | Switch server daemon message dispatch, subscription filters, and room routing |
| **Reproduction Sandbox** | Real Python subprocess in disposable directory (`shell=False`) | Real Python subprocess in disposable directory (`shell=False`) |
| **Evidence Preservation** | Real artifacts persisted to `.evidence/task-<id>/` | Real artifacts persisted to `.evidence/task-<id>/` |
| **Audit Ledger** | Real durable SQLite transaction log | Real durable SQLite transaction log |
| **Verification Purpose** | Component logic, state machine, payload contracts, reproduction | End-to-end protocol compatibility, daemon claim handling, network resilience |

---

## 3. The 8-Point Live Switch Verification Checklist

To certify CrewPilot for live Switch operation, the following 8 criteria must be evidenced:

### 1. Three Registered, Distinct Switch Agent IDs
- **Requirement:** `crewpilot-triager`, `crewpilot-repro-builder`, and `crewpilot-reviewer` must have distinct participant IDs registered in Switch.
- **Verification Command:**
  ```bash
  python switch/register_agent.py --name crewpilot-triager
  python switch/register_agent.py --name crewpilot-repro-builder
  python switch/register_agent.py --name crewpilot-reviewer
  ```
- **Expected Artifact:** `.switch/crewpilot_triager.json`, `.switch/crewpilot_repro_builder.json`, `.switch/crewpilot_reviewer.json` with distinct non-colliding `agent_id` values.
- **Enforcement:** `main.py::validate_squad_credentials()` fails fast before startup if any identity is missing or duplicated.
- **Status:** Verified via schema & mock tests; pending live Switch daemon registration.

### 2. Three Authenticated SSE Connections
- **Requirement:** All three agents establish long-lived HTTP SSE connections to `GET /agents/{id}/events?connection_id=...&scope=all&filter=addressed`.
- **Expected Frames:**
  - `connection_state` received with protocol version, rooms, cursor, and heartbeat interval.
  - Periodic heartbeat (`POST /agents/{id}/connection/beat`) dispatched every 2 seconds without HTTP 401/409 errors.
- **Status:** Client connection loop implemented and unit-tested in `tests/test_switch_agent_client.py`; pending live network validation.

### 3. Real Human Message Received by Triager
- **Requirement:** A real human maintainer mentions `@crewpilot-triager triage <issue-url>` in a Switch room (or bridged Discord/Mattermost channel).
- **Expected Event:** SSE event `message` received with string `sender`, display `sender_name`, `message_id`, and `addressed: true`.
- **Status:** Supported and unit-tested in `tests/test_squad_workflow.py`.

### 4. Real Addressed Triager-to-Builder Switch Handoff
- **Requirement:** After maintainer approval (`@crewpilot-triager approve task <task-id>`), Triager emits a directed message:
  ```
  @crewpilot-repro-builder reproduce task <task-id>
  ```
- **Expected Switch Behavior:** Switch routes the message to the Builder's SSE stream with `addressed: true`.
- **Status:** Verified in offline multi-agent simulation; pending live daemon routing.

### 5. Real Builder-to-Reviewer Switch Handoff
- **Requirement:** Upon successful execution of the reproduction probe, Builder posts:
  ```
  @crewpilot-reviewer review task <task-id>
  ```
- **Expected Switch Behavior:** Switch routes the message to the Reviewer's SSE stream with `addressed: true`.
- **Status:** Verified in offline multi-agent simulation; pending live daemon routing.

### 6. Real Maintainer Approval and Rejection Behavior
- **Requirement:** Human approval binding strictly checked:
  - Unauthorized participants attempting `approve` or `publish` receive explicit rejection notices.
  - Reviewer rejects malformed or unapproved task transitions.
  - Legitimate requester approvals trigger valid state transitions.
- **Status:** Fully enforced by `storage/task_store.py` and connector handlers, verified in tests.

### 7. Reproduction Result Visible in the Actual Switch Room
- **Requirement:** Summary of the reproduction probe (duration, exit code 1, confirmed traceback, sanitized command) posted to the room thread for visibility.
- **Status:** Verified in offline simulation output; ready for live room posting.

### 8. Persistent Task Evidence and Audit Records
- **Requirement:** 
  - Complete reproduction probe script, stdout, stderr, and execution metadata persisted to `.evidence/task-<id>/`.
  - Immutable audit trail records stored in durable SQLite database.
- **Status:** **VERIFIED ON DISK & DATABASE** (confirmed by tests and offline demo).

---

## 4. Protocol Claim Analysis & Unverified Integration Risks

### Analysis of `connect_to_room` on `post_message`
In `switch/agent_client.py`:
```python
async def post_message(
    self, room_id: str, body: str, thread_id: str | None = None
) -> dict[str, Any]:
    await self.call_operation(
        "connect_to_room",
        {"room_id": room_id, "include_general_instructions": False},
    )
    ...
```

#### The Protocol Contract
According to the official Switch agent protocol (`agent-protocol.md`):
1. `connect_to_room` causes the calling agent to take over the active room claim.
2. In single-agent scenarios, claiming before posting guarantees the agent is bound to the room.
3. In a 3-agent Squad scenario (`crewpilot-triager`, `crewpilot-repro-builder`, `crewpilot-reviewer` all active in `#incident-response`):
   - Every time Triager posts, it claims the room.
   - When Builder posts its handoff, Builder claims the room.
   - When Reviewer posts its review, Reviewer claims the room.

#### Identified Integration Risk
- **Risk:** Does rapid back-and-forth room claiming cause eviction of other squad members, emit noisy `room_released` frames, or interfere with message delivery under `filter=addressed`?
- **Current Mitigation:**
  - With `scope=all` and `filter=addressed`, message routing is addressed-based, so agents receive events addressed to their handle even if another agent currently holds the room claim.
  - However, because the live Switch daemon was unreachable during pre-event audit, the exact runtime behavior of frequent room claim handover between 3 co-operating agents in the same room remains an **unverified integration risk**.
- **Recommended Live Hardening:**
  - When live Switch is deployed, monitor SSE frames for `room_released` or `evicted`.
  - If room claim contention occurs, update `agent_client.py` to claim the room once on connection rather than prior to each message.
