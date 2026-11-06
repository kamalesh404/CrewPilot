# CrewPilot — Evidence-Driven Bug Investigation Squad

> **Flip the Switch Hackathon 2026** — *Best Business Workflow Track*
> Repository: [vincenzo-afk/CrewPilot](https://github.com/vincenzo-afk/CrewPilot)

CrewPilot is an autonomous, evidence-driven bug investigation squad operating directly inside team chat via **[Switch](https://github.com/sandbox-quantum/switch)**.

Instead of human maintainers manually diagnosing, reproducing, and auditing bug reports, CrewPilot deploys **three genuinely independent Switch agents** that collaborate with a human engineer in a shared Switch room (mirrored to Discord or Slack):

1. **`@crewpilot-triager`**: Ingests GitHub issues, extracts structured diagnostics (severity, category, difficulty), writes a fix plan, and binds human maintainer scope approval.
2. **`@crewpilot-repro-builder`**: Receives an addressed Switch handoff, spawns an isolated subprocess sandbox (`shell=False`, stripped credentials), executes a reproduction probe against real bugs (e.g. `psf/requests#7634`), captures empirical evidence (exit codes, stack traces), and attaches artifacts to a durable SQLite ledger.
3. **`@crewpilot-reviewer`**: An independent, read-only agent that audits the captured evidence, verifies execution containment, scans for leaked credentials, and prompts the maintainer for Draft PR publication.

---

## Architecture & Switch Collaboration Flow

```text
Team Chat (Discord / Slack)
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
│   │@crewpilot-repro-builder│ ── 2. Isolated Probe & Evidence Capture   │
│   └──────────┬────────────┘                                            │
│              │                                                         │
│              ▼  [Switch Handoff: "review task <id>"]                   │
│   ┌───────────────────────┐                                            │
│   │  @crewpilot-reviewer  │ ── 3. Read-Only Audit & Security Check     │
│   └──────────┬────────────┘                                            │
│              │                                                         │
│              ▼  [Human Publication Auth: "publish task <id>"]          │
│   ┌───────────────────────┐                                            │
│   │   Draft PR Manifest   │ ── 4. Branch & PR Manifest Finalization    │
│   └───────────────────────┘                                            │
│                                                                        │
│   Durable SQLite TaskStore (tasks, evidence, approvals, audit ledger)  │
└────────────────────────────────────────────────────────────────────────┘
```

### Why Switch is Essential
- **True Agent-to-Agent Handoffs:** Agents do not communicate via in-memory function calls. The Triager posts `@crewpilot-repro-builder reproduce task <id>` to Switch; Switch routes the addressed message over SSE to the Builder's client. The Builder replies to Switch with evidence and hands off to `@crewpilot-reviewer review task <id>`.
- **Decoupled Transport:** CrewPilot connects to Switch using standard Server-Sent Events (SSE) and HTTP operations. The team chat frontend (Discord, Slack, or Mattermost) is managed entirely by Switch.
- **Auditable Multi-Identity Protocol:** Each agent maintains distinct Switch credentials (`.switch/*.json`) and least-privilege security permissions.

---

## Empirical Verification: Target Bug `psf/requests#7634`

CrewPilot is verified against real open-source bugs, specifically **`psf/requests#7634`** (non-string form field name causes `AttributeError: 'int' object has no attribute 'translate'` when multipart files are attached).

### Complete Workflow Walkthrough

1. **Maintainer posts in chat:**
   ```text
   @crewpilot-triager triage https://github.com/psf/requests/issues/7634
   ```
2. **Triager replies with structured assessment & Task ID:**
   ```text
   **Issue triage**
   Task ID: `7b20c950-77f2-4140-b1f1-1e09033ad040`
   Category: Bug | Severity: Medium | Difficulty: Easy
   Summary: When preparing a multipart request with files, non-string form field names in data cause an AttributeError...
   To decide: reply 'approve 7b20c950-77f2-4140-b1f1-1e09033ad040'
   ```
3. **Maintainer approves scope:**
   ```text
   @crewpilot-triager approve task 7b20c950-77f2-4140-b1f1-1e09033ad040
   ```
4. **Triager emits Switch handoff:**
   ```text
   @crewpilot-repro-builder reproduce task 7b20c950-77f2-4140-b1f1-1e09033ad040
   ```
5. **Reproducer executes isolated probe and reports empirical evidence:**
   ```text
   ### 🔬 Bug Reproduction Confirmed
   - Task ID: `7b20c950-77f2-4140-b1f1-1e09033ad040`
   - Command: `python.exe repro_7634.py` (exit 1)
   - Summary: psf/requests#7634 reproduced: AttributeError raised on non-string key with multipart files.
   Evidence recorded in durable SQLite audit store. Handing off to Reviewer...
   @crewpilot-reviewer review task 7b20c950-77f2-4140-b1f1-1e09033ad040
   ```
6. **Independent Reviewer audits evidence & prompts publication:**
   ```text
   ### 🛡️ Independent Code & Evidence Review: Task `7b20c950-77f2-4140-b1f1-1e09033ad040`
   - Reviewer: @crewpilot-reviewer
   - Verdict: ✅ EVIDENCE VERIFIED (Approved for Draft PR)
   - Evidence Audit: Subprocess exit code 1 matches reported bug signature.
   - Security Verification: PASS (Zero credentials leaked; execution strictly confined).
   Human maintainer may authorize Draft PR: reply '@crewpilot-reviewer publish task 7b20c950-77f2-4140-b1f1-1e09033ad040'.
   ```
7. **Maintainer authorizes publication:**
   ```text
   @crewpilot-reviewer publish task 7b20c950-77f2-4140-b1f1-1e09033ad040
   ```
   Reviewer records `pr_published` and outputs the verified Draft PR manifest with 7 immutable audit events.

---

## Quickstart & Demo

### 1. Instant End-to-End Simulation Demo (Zero Setup Required)
Run the authentic multi-agent workflow in 5 seconds without needing a local Switch server or LLM credits:
```bash
python demo/simulate_squad_demo.py
```
This executes all 3 Switch agent connectors against `psf/requests#7634`, executes the real reproduction probe in an isolated sandbox, records all evidence in `crewpilot_demo.db`, and verifies the complete audit ledger.

### 2. Run the Full Test Suite
```bash
pytest -v
```
**73 tests passing 100%**, covering:
- Triager LLM parsing & issue fetching (`tests/test_triage_agent.py`, `tests/test_github_issues.py`)
- Switch SSE/HTTP protocol client (`tests/test_switch_agent_client.py`)
- SQLite TaskStore atomic transactions & FSM transitions (`tests/test_task_store.py`)
- Reproducer sandbox containment & environment sanitization (`tests/test_reproducer.py`)
- Multi-agent Switch room handoff integration (`tests/test_squad_workflow.py`)

### 3. Running with Live Switch
Register the 3 squad identities:
```bash
python -m switch.register_agent --role squad
```
Invite them to your Switch room (in Discord):
```text
!invite-agent @crewpilot-triager
!invite-agent @crewpilot-repro-builder
!invite-agent @crewpilot-reviewer
```
Start the squad runtime:
```bash
python main.py --role squad
```

---

## Security & Safety Engineering

- **Subprocess Containment:** Subprocesses run strictly with explicit argument arrays (`shell=False`). No raw LLM-generated shell strings are ever executed.
- **Environment Sanitization:** Subprocesses run in a stripped environment removing all API keys, tokens, and credentials (`SWITCH_API_KEY`, `OPENAI_API_KEY`, `GITHUB_TOKEN`, etc.).
- **Automated Leak Scanning:** The Reviewer scans all captured outputs with `SECRET_LEAK_RE` before approving.
- **Strict Authorization Binding:** Human approvals bind to the exact `task_id` and originating `requester_id`. Room-level fallback is strictly prohibited.
- **Least-Privilege Identities:** The Reproducer and Reviewer do not have LLM API keys or GitHub write tokens.
- **Draft PR Ceiling:** Automated operations can only generate Draft PRs; they cannot merge, deploy, or modify default branches.

---

## Project Structure

```text
CrewPilot/
├── main.py                     # Multi-agent squad runtime entrypoint
├── config.py                   # Environment configuration & credential loader
├── github_issues.py            # GitHub issue resolution
├── agent/
│   ├── prompts.py              # Structured triage system prompt & schema
│   ├── triage_agent.py         # Multi-provider LLM triage agent
│   └── reproducer.py           # Subprocess sandbox & reproduction probe engine
├── storage/
│   └── task_store.py           # Durable SQLite persistence & immutable audit ledger
├── switch/
│   ├── agent_client.py         # Real SSE + HTTP Switch agent protocol client
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
