# CrewPilot — Evidence-Driven Bug Investigation Squad

> **Flip the Switch Hackathon 2026** — *Best Business Workflow Track*
> Repository: [vincenzo-afk/CrewPilot](https://github.com/vincenzo-afk/CrewPilot)

CrewPilot is an autonomous, evidence-driven bug investigation squad operating directly inside team chat via **[Switch](https://github.com/sandbox-quantum/switch)**.

---

## 1. The Problem

Software engineering teams and open-source maintainers lose countless hours context-switching to investigate ambiguous bug reports. Incoming issues frequently lack reproduction scripts, precise environmental details, or verified failure traces. Maintainers must manually configure isolated environments, reproduce errors, and verify fixes before writing code.

---

## 2. The Solution: Three Role-Specific Switch Identities

Instead of monolithic prompts pretending to be multiple people, CrewPilot deploys **three genuinely independent Switch agent identities** that collaborate with a human engineer in a shared Switch room (mirrored to Discord, Slack, or Mattermost):

1. **`@crewpilot-triager`**:
   - Ingests issue reports and fetches issue text via GitHub API.
   - Generates structured diagnostics (severity, category, difficulty) and a targeted fix plan.
   - Records the task in durable SQLite and binds scope approval to the originating human maintainer.
2. **`@crewpilot-repro-builder`**:
   - Receives an addressed handoff message from the Triager through Switch.
   - Executes an empirical reproduction probe against real bugs (e.g. `psf/requests#7634`) in a disposable workspace with credential reduction.
   - Captures empirical evidence (exact failure traceback, exit code 1), attaches artifacts to a durable SQLite ledger, and saves disk artifacts in `.evidence/task-<id>/`.
3. **`@crewpilot-reviewer`**:
   - An independent, read-only auditor that inspects captured evidence, verifies exit code matching and exact error signatures, and checks for leaked secrets.
   - Prompts the human maintainer for publication authorization and prepares the verified Publication Manifest.

---

## 3. Architecture & Switch Collaboration Flow

```text
Team Chat (Discord / Slack / Mattermost)
         │
         │  addressed messages & threads
         ▼
Switch Platform (Discord Bridge / Room Gateway)
         │
         │  SSE Stream Down:    GET  /agents/{agent_id}/events
         │  HTTP Operations Up: POST /agents/{agent_id}/ops/{op}
         ▼
┌────────────────────────────────────────────────────────────────────────┐
│                        CREWPILOT SQUAD RUNTIME                         │
│                                                                        │
│   ┌───────────────────────┐                                            │
│   │   @crewpilot-triager  │ ── 1. Structured Triage & Fix Plan         │
│   └──────────┬────────────┘                                            │
│              │                                                         │
│              ▼  [Human Scope Approval: "approve task <id>"]            │
│   ┌───────────────────────┐                                            │
│   │@crewpilot-repro-builder│ ── 2. Probe Execution & Evidence Capture   │
│   └──────────┬────────────┘                                            │
│              │                                                         │
│              ▼  [Switch Handoff: "review task <id>"]                   │
│   ┌───────────────────────┐                                            │
│   │  @crewpilot-reviewer  │ ── 3. Read-Only Audit & Security Check     │
│   └──────────┬────────────┘                                            │
│              │                                                         │
│              ▼  [Human Publication Auth: "publish task <id>"]          │
│   ┌───────────────────────┐                                            │
│   │ Publication Manifest  │ ── 4. Branch & Audit Manifest Finalization │
│   └───────────────────────┘                                            │
│                                                                        │
│   Durable SQLite TaskStore (tasks, evidence, approvals, audit ledger)  │
│   Disk Evidence Storage    (.evidence/task-<id>/)                      │
└────────────────────────────────────────────────────────────────────────┘
```

### Why Switch is Essential
- **True Agent-to-Agent Handoffs:** Agents do not communicate via in-memory function calls. The Triager posts `@crewpilot-repro-builder reproduce task <id>` to Switch; Switch routes the addressed message over SSE to the Builder's client. The Builder replies to Switch with evidence and hands off to `@crewpilot-reviewer review task <id>`.
- **Decoupled Transport:** CrewPilot connects to Switch using standard Server-Sent Events (SSE) down and authenticated HTTP operations up. The team chat frontend (Discord, Slack, or Mattermost) is managed entirely by Switch.
- **Auditable Multi-Identity Protocol:** Each agent maintains distinct Switch credentials (`.switch/*.json`) and least-privilege security permissions.

---

## 4. Communication & Handoff Mechanism

The squad workflow follows a strictly enforced state machine backed by SQLite transactions:

```text
awaiting_approval -> approved | changes_requested | cancelled
changes_requested -> awaiting_approval | cancelled
approved          -> reproducing | cancelled
reproducing       -> under_review | changes_requested | cancelled
under_review      -> review_approved | changes_requested | cancelled
review_approved   -> publication_authorized | cancelled
```

### Interaction Sequence
1. **User asks for triage in chat:**
   ```text
   @crewpilot-triager triage https://github.com/psf/requests/issues/7634
   ```
2. **Triager replies with structured brief and UUID `task_id`:**
   ```text
   **Issue triage**
   Task ID: `5e9b4295-6ed3-4cec-a244-3edca4317a00`
   Category: Bug | Severity: Medium | Difficulty: Easy
   Summary: When preparing a multipart request with files, non-string form field names cause an AttributeError...
   To decide: reply 'approve 5e9b4295-6ed3-4cec-a244-3edca4317a00'
   ```
3. **Maintainer approves scope:**
   ```text
   @crewpilot-triager approve task 5e9b4295-6ed3-4cec-a244-3edca4317a00
   ```
4. **Triager emits Switch handoff:**
   ```text
   @crewpilot-repro-builder reproduce task 5e9b4295-6ed3-4cec-a244-3edca4317a00
   ```
5. **Reproducer executes probe and reports empirical evidence:**
   ```text
   ### 🔬 Bug Reproduction Confirmed
   - Task ID: `5e9b4295-6ed3-4cec-a244-3edca4317a00`
   - Command: `python.exe repro_7634.py` (exit 1)
   - Summary: psf/requests#7634 reproduced: AttributeError raised on non-string key with multipart files.
   Evidence recorded in durable SQLite audit store. Handing off to Reviewer...
   @crewpilot-reviewer review task 5e9b4295-6ed3-4cec-a244-3edca4317a00
   ```
6. **Independent Reviewer audits evidence:**
   ```text
   ### 🛡️ Independent Code & Evidence Review: Task `5e9b4295-6ed3-4cec-a244-3edca4317a00`
   - Reviewer: @crewpilot-reviewer
   - Verdict: ✅ EVIDENCE VERIFIED (Approved for Publication)
   - Evidence Audit: Subprocess exit code 1 matches exact signature ('int' object has no attribute 'translate').
   - Security Verification: PASS (Zero credentials leaked; execution strictly confined).
   Human maintainer may authorize publication: reply '@crewpilot-reviewer publish task 5e9b4295-6ed3-4cec-a244-3edca4317a00'.
   ```
7. **Maintainer authorizes publication manifest preparation:**
   ```text
   @crewpilot-reviewer publish task 5e9b4295-6ed3-4cec-a244-3edca4317a00
   ```
8. **Reviewer finalizes Publication Manifest:**
   The task transitions to `publication_authorized` with full immutable audit history.

---

## 5. Security & Safety Engineering

- **Strict Requester Binding:** All human approvals (`approve` and `publish`) bind to the originating `requester_id` and exact `task_id`. Mismatched actors are rejected with explicit audit logging; room-level fallbacks are prohibited.
- **Process Invocation:** Subprocesses run strictly with explicit argument arrays (`shell=False`). No shell string interpolation is permitted.
- **Credential Reduction:** Subprocesses run in a stripped environment removing all API keys, tokens, and credentials (`SWITCH_API_KEY`, `OPENAI_API_KEY`, `GITHUB_TOKEN`, etc.).
- **Automated Leak Scanning:** The Reviewer scans all captured outputs with `SECRET_LEAK_RE` before approving.
- **Execution Isolation Boundary & Limitations:**
  - **What is provided:** Credential-reduced subprocess execution in a disposable temporary workspace (`tempfile.mkdtemp(prefix="crewpilot-task-")`). Probe scripts and outputs are archived to `.evidence/task-<id>/`.
  - **Limitations:** Subprocesses execute under the host OS user account without container-level namespace or cgroup isolation (e.g., Docker, Bubblewrap, or gVisor). Untrusted probe code could access host filesystem paths readable by the current user. True OS containerization is documented as a production roadmap item.
- **Publication Ceiling (No False Draft PR Claim):** The squad finalizes an authorized publication manifest (`publication_authorized`) in durable SQLite. Automated remote PR creation against GitHub is disabled by default to avoid unapproved repository mutations.

---

## 6. Target Bug Verification: `psf/requests#7634`

CrewPilot is verified against real open-source bugs, specifically **`psf/requests#7634`**:
- **Bug:** When preparing a multipart request with files, non-string form field names in `data` cause `AttributeError: 'int' object has no attribute 'translate'` in `urllib3/fields.py`.
- **Validation Criteria:** Strict verification requires exact issue identity (`psf/requests#7634`), exit code `1`, non-empty output, clean secret checks, and the exact error signature `'int' object has no attribute 'translate'`. Any deviation triggers revision request.
- **Persistent Disk Artifacts:** Saved to `.evidence/task-<id>/`:
  - `repro_probe.py`: Reproduction probe script.
  - `output.log`: Execution stdout and stderr.
  - `metadata.json`: Environmental metadata (Python, requests, urllib3, OS platform, execution duration).

---

## 7. Quickstart & How to Run

### Prerequisites
- Python 3.12 (reproduce inside a virtual environment `.venv`)
- Git

### 1. Setup Environment
```bash
git clone https://github.com/vincenzo-afk/CrewPilot.git
cd CrewPilot
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/macOS:
source .venv/bin/activate

pip install -r requirements.txt
```

### 2. Run the Offline Simulation Demo
Execute the full multi-agent workflow instantly without a live Switch server or external API keys:
```bash
python demo/simulate_squad_demo.py
```
This runs the authentic connectors, executes the real reproduction probe in a temporary directory, confirms the exact `requests#7634` error, persists evidence to disk in `.evidence/`, and records 7 immutable audit log entries in `crewpilot_demo.db`.

### 3. Run the Full Test Suite
```bash
pytest -v
```
**84 unit and integration tests passing 100%**, covering:
- Configuration and squad credential validation (`tests/test_config.py`)
- GitHub issue parsing and error handling (`tests/test_github_issues.py`)
- Reproducer sandbox containment and disk evidence persistence (`tests/test_reproducer.py`)
- Multi-agent Switch room handoff integration and negative audits (`tests/test_squad_workflow.py`)
- Switch SSE/HTTP protocol client (`tests/test_switch_agent_client.py`)
- Switch triage room connector (`tests/test_switch_connector.py`)
- SQLite TaskStore FSM state machine transitions (`tests/test_task_store.py`)
- LLM triage agent parsing (`tests/test_triage_agent.py`)

### 4. Running with Live Switch
To deploy against a running Switch daemon:
1. Register the three agent identities:
   ```bash
   python -m switch.register_agent --role squad
   ```
2. Start the multi-agent squad runtime:
   ```bash
   python main.py --role squad
   ```
3. Invite the squad into your Switch room (e.g., in Discord):
   ```text
   !invite-agent @crewpilot-triager
   !invite-agent @crewpilot-repro-builder
   !invite-agent @crewpilot-reviewer
   ```
*See [`docs/live-switch-verification.md`](docs/live-switch-verification.md) for live verification prerequisites and protocol checklists.*

---

## 8. Offline Simulator vs. Live Switch Verification

| Feature | Offline Simulator (`demo/simulate_squad_demo.py`) | Live Switch Runtime (`main.py --role squad`) |
| :--- | :--- | :--- |
| **Status** | **VERIFIED (84/84 tests passing)** | Documented protocol; server offline locally |
| **Transport** | In-memory message delivery | Live HTTP SSE event stream & HTTP POST ops |
| **Identities** | 3 simulated participant handles | 3 authenticated Switch identities (`.switch/*.json`) |
| **Reproduction** | Real Python subprocess (`shell=False`) | Real Python subprocess (`shell=False`) |
| **Evidence on Disk** | Persisted to `.evidence/task-<id>/` | Persisted to `.evidence/task-<id>/` |
| **Persistence** | Durable SQLite TaskStore & Audit Ledger | Durable SQLite TaskStore & Audit Ledger |

*See [`docs/live-switch-verification.md`](docs/live-switch-verification.md) for the 8-point live verification checklist and unverified integration risk analysis.*

---

## 9. Implemented vs. Planned Roadmap

| Component | Status | Implementation Details |
| :--- | :--- | :--- |
| **Triager Agent** | Implemented | GitHub issue ingestion, LLM triage, fix planning, task creation |
| **Human Scope Gate** | Implemented | Task ID and requester-bound approval verification |
| **Reproducer Agent** | Implemented | Subprocess execution (`shell=False`), credential stripping, probe execution |
| **Evidence Disk Store** | Implemented | Saved to `.evidence/task-<id>/` with metadata, logs, and probe script |
| **Reviewer Agent** | Implemented | Read-only audit of exit code 1, exact error signature, and leak checks |
| **Human Publication Gate** | Implemented | Authorized publication manifest preparation (`publication_authorized`) |
| **Durable Audit Trail** | Implemented | SQLite database with append-only immutable audit log |
| **OS Containerization** | Planned | Docker / Bubblewrap / gVisor kernel namespace isolation |
| **Live Switch Verification** | Planned | Live daemon routing verification pending Switch server deployment |
| **Automated Draft PR Push** | Planned | Scoped token push to GitHub remote branch after maintainer sign-off |

---

## 10. Project Structure

```text
CrewPilot/
├── main.py                     # Multi-agent squad runtime entrypoint
├── config.py                   # Configuration & fail-fast squad credential validator
├── github_issues.py            # GitHub issue loader with rate-limit and error handling
├── agent/
│   ├── prompts.py              # Structured triage system prompt & schema
│   ├── triage_agent.py         # Multi-provider LLM triage agent
│   └── reproducer.py           # Subprocess sandbox & reproduction probe engine
├── storage/
│   └── task_store.py           # Durable SQLite persistence & immutable audit ledger
├── switch/
│   ├── agent_client.py         # Official SSE + HTTP Switch agent protocol client
│   ├── credentials.py          # Multi-agent local credential storage
│   ├── register_agent.py       # Switch agent registration CLI (--role squad)
│   ├── switch_connector.py     # Triager Switch room connector
│   ├── repro_connector.py      # Reproducer / Builder Switch room connector
│   └── reviewer_connector.py   # Independent Reviewer Switch room connector
├── demo/
│   ├── demo-script.md          # Comprehensive demonstration script
│   └── simulate_squad_demo.py  # Executable end-to-end simulation runner
├── docs/
│   ├── architecture.md         # Squad architecture & trust boundary specification
│   ├── demo-evidence.md        # Captured empirical evidence for psf/requests#7634
│   ├── live-switch-verification.md # 8-point checklist & protocol claim analysis
│   └── task-store-design.md    # Database schema & FSM transition rules
└── tests/
    ├── test_config.py
    ├── test_github_issues.py
    ├── test_reproducer.py
    ├── test_squad_workflow.py
    ├── test_switch_agent_client.py
    ├── test_switch_connector.py
    ├── test_task_store.py
    └── test_triage_agent.py
```
