# CrewPilot implementation backlog

This backlog tracks the implementation progress for the Flip the Switch Hackathon (Best Business Workflow track).

## Preparation work (before 2026-11-04 00:01 Asia/Kolkata) — COMPLETED

### PREP-01 — Reproducible quality baseline [COMPLETE]
- Isolated dependencies, Python 3.12 pinned in `.venv`.
- `ruff check .` and `pytest -q` configured and passing.
- GitHub Actions CI workflow in `.github/workflows/ci.yml`.

### PREP-02 — Regression tests for current behaviour [COMPLETE]
- Comprehensive unit tests covering URL parsing, JSON normalization, duplicate prevention, and authorization.
- 44 pre-hackathon baseline tests passing.

### PREP-03 — Switch deployment compatibility fixture & protocol alignment plan [COMPLETE]
- Documented official Switch protocol alignment (SSE `/agents/{id}/events` down, authenticated HTTP `/agents/{id}/ops/{op}` up).
- Verified against `sandbox-quantum/switch docs/official/internals/agent-protocol.md`.

### PREP-04 — Security and delivery design [COMPLETE]
- Approved state-machine, schema, permission model, and threat model in `docs/architecture.md`.

### PREP-05 — Standalone SQLite task persistence layer [COMPLETE]
- Standalone `storage/task_store.py` with 18 dedicated unit tests in `tests/test_task_store.py`.

---

## Hackathon implementation (Live Sprint) — COMPLETED & VERIFIED

### M1 — Durable task state wiring and identities [COMPLETE]
- Integrated `storage/task_store.py` into `switch/switch_connector.py`.
- Exact task approvals (`approve <task-id>`) enforce authorized requester binding and prevent cross-thread fallback.
- Multi-agent credentials supported in `switch/credentials.py` (`.switch/<sanitized_name>.json`).
- **Verified by:** `tests/test_switch_connector.py` and `tests/test_task_store.py`.

### M2 — Triager to Builder handoff [COMPLETE]
- Triage creates durable task in SQLite store in `awaiting_approval` status.
- Requester approval records atomic decision in SQLite and emits Switch handoff:
  `@crewpilot-repro-builder reproduce task <task-id>`
- **Verified by:** `tests/test_squad_workflow.py::test_full_squad_handoff_integration`.

### M3 — Constrained Reproducer / Builder [COMPLETE]
- Implemented `agent/reproducer.py` and `switch/repro_connector.py`.
- Executes reproduction probes under strict containment:
  - Explicit argument arrays (`shell=False`).
  - Sanitized environment stripping all API keys, tokens, and credentials.
  - Disposable temporary directories.
  - Dedicated probe capturing empirical evidence for `psf/requests#7634` (exit code 1, `AttributeError`).
- Attaches evidence to durable SQLite store and emits handoff to Reviewer:
  `@crewpilot-reviewer review task <task-id>`
- **Verified by:** `tests/test_reproducer.py` and `tests/test_squad_workflow.py::test_repro_executes_for_approved_task`.

### M4 — Independent Reviewer and revision loop [COMPLETE]
- Implemented `switch/reviewer_connector.py`.
- Independent read-only agent that inspects reproduction evidence in SQLite.
- Audits exit code, exception traceback, and verifies absence of leaked tokens via `SECRET_LEAK_RE`.
- Transitions task status to `review_approved`.
- Prompts human maintainer for Draft PR publication.
- **Verified by:** `tests/test_squad_workflow.py::test_reviewer_verifies_evidence_and_approves`.

### M5 — Human-authorized Draft PR executor [COMPLETE]
- Requester mentions Reviewer with `publish task <task-id>`.
- Strict authorization check: only original requester (`task.requester_id`) can authorize publication; unapproved tasks or third-party users are denied.
- Transitions task status to `pr_published`.
- Generates Draft PR manifest for branch `fix/issue-7634-multipart-key-str` and commits audit ledger.
- **Verified by:** `tests/test_squad_workflow.py::test_reviewer_publish_flow_authorization_enforced`.

### M6 — End-to-end demo and submission material [COMPLETE]
- Executable demo simulation: `python demo/simulate_squad_demo.py` verifying full Triager -> Builder -> Reviewer flow.
- Complete demo walkthrough: `demo/demo-script.md`.
- Empirical evidence records: `docs/demo-evidence.md`.
- Comprehensive test suite: 73 tests passing 100%.
- README and documentation updated with truthful, verified capabilities.
