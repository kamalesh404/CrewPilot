# CrewPilot — Demo Script: Evidence-Driven Bug Investigation Squad

This script walks through the complete, authentic multi-agent business workflow for the **Flip the Switch Hackathon (Best Business Workflow track)**.

---

## 1. Scenario & Target Bug

- **Target Issue:** [`psf/requests#7634`](https://github.com/psf/requests/issues/7634)
- **Bug Signature:** In `requests.Request("POST", url, data={1: "val"}, files={"f": b"..."}).prepare()`, a non-string dictionary key in multipart form data causes `AttributeError: 'int' object has no attribute 'translate'`.
- **Participants in Switch Room (`#incident-response`):**
  - **Human Maintainer:** `Vincenzo`
  - **Agent 1 (Triager):** `@crewpilot-triager`
  - **Agent 2 (Reproducer/Builder):** `@crewpilot-repro-builder`
  - **Agent 3 (Reviewer):** `@crewpilot-reviewer`
- **Durable State:** SQLite TaskStore (`storage/task_store.py`) with strict atomic state machine, immutable audit logs, and multi-factor approval binding.

---

## 2. Step-by-Step Walkthrough

### Step 1: Human Maintainer Initiates Triage
In the team chat (bridged by Switch to the room):
```text
@crewpilot-triager triage https://github.com/psf/requests/issues/7634
```
**Triager Agent Response:**
- Generates structured triage assessment (Severity: Medium, Difficulty: Easy, Category: Bug).
- Suggests fix plan:
  1. In `requests/models.py` `_encode_files`, ensure field names are converted to `str(k)`.
  2. Add regression test in `tests/test_requests.py` with integer key in multipart data.
  3. Verify backward compatibility with string and byte keys.
- Creates durable task in SQLite store: `Task ID: <task-id>`.
- Prompts maintainer for scope approval.

### Step 2: Human Maintainer Approves Scope
Only the original requester is authorized to approve:
```text
@crewpilot-triager approve task <task-id>
```
**Triager Agent Response:**
- Validates requester identity and binds approval atomically to the exact `task_id` in SQLite.
- Records immutable audit trail event `DECISION_RECORDED`.
- Posts Switch handoff message addressed to the Reproducer:
  ```text
  @crewpilot-repro-builder reproduce task <task-id>
  ```

### Step 3: Reproducer / Builder Replicates Bug in Isolated Sandbox
Switch delivers the handoff to `@crewpilot-repro-builder`.
**Reproducer Agent Action:**
- Verifies task status is `approved` in SQLite.
- Spawns an isolated temporary workspace with stripped credentials (zero API keys, tokens, or passwords leaked).
- Executes reproduction probe with explicit argument arrays (`shell=False`) under a strict timeout.
- Captures exact evidence: exit code `1`, `AttributeError: 'int' object has no attribute 'translate'` inside `urllib3.fields.format_multipart_header_param`.
- Stores evidence artifact in SQLite TaskStore.
- Transitions task status to `under_review`.
- Posts evidence report to Switch room and hands off to Reviewer:
  ```text
  @crewpilot-reviewer review task <task-id>
  ```

### Step 4: Independent Reviewer Audits Reproduction Evidence
Switch delivers the handoff to `@crewpilot-reviewer`.
**Reviewer Agent Action:**
- Read-only inspection of the task's evidence and audit ledger in SQLite.
- Verifies exit code `1` matches the reported failure mode.
- Audits output for absence of leaked credentials or host secrets.
- Verifies execution containment.
- Transitions task status to `review_approved`.
- Posts independent review audit verdict:
  ```text
  ### 🛡️ Independent Code & Evidence Review: Task `<task-id>`
  - Reviewer: @crewpilot-reviewer
  - Verdict: ✅ EVIDENCE VERIFIED (Approved for Draft PR)
  - Security Verification: PASS (Zero credentials leaked; execution strictly confined)
  ```
- Prompts maintainer: `reply @crewpilot-reviewer publish task <task-id>` to publish Draft PR.

### Step 5: Maintainer Authorizes Draft PR Publication
Maintainer authorizes publication:
```text
@crewpilot-reviewer publish task <task-id>
```
**Reviewer Agent Response:**
- Verifies requester authorization and `review_approved` task state.
- Transitions task status to `pr_published`.
- Generates Draft PR manifest for branch `fix/issue-7634-multipart-key-str` with full audit ledger.
- Finalizes task with 7 immutable audit log records.

---

## 3. How to Reproduce Locally

To run the complete verified simulation in 5 seconds:
```bash
python demo/simulate_squad_demo.py
```

To run with live Switch servers:
```bash
# 1. Register the 3 squad agents
python -m switch.register_agent --role squad

# 2. Start the 3 squad agents concurrently
python main.py --role squad
```
