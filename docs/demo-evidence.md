# CrewPilot — Demo Evidence: psf/requests#7634 Bug Reproduction

This document records the empirical evidence captured during the automated execution and verification of **CrewPilot's multi-agent squad**.

---

## 1. Issue Information

- **Target Issue:** [`psf/requests#7634`](https://github.com/psf/requests/issues/7634)
- **Title:** Multipart request fails with non-string key in form data
- **Root Cause:** When `data` containing non-string keys (such as integer `1`) is passed along with `files`, `requests/models.py:250` forwards fields directly to `urllib3.filepost.encode_multipart_formdata`, which passes them to `urllib3.fields.RequestField.from_tuples`. Inside `urllib3/fields.py:113`, `format_multipart_header_param` attempts `value.translate(...)` without converting `value` to `str`.
- **Expected Exit Code:** `1` (AttributeError triggered).

---

## 2. Empirical Execution Trace

### Subprocess Execution Parameters
- **Working Directory:** Disposable temporary directory `crewpilot-task-<uuid>/`
- **Command Invocation:** `[sys.executable, "repro_probe.py"]` (`shell=False`, argument array)
- **Environment Sanitization:** All environment variables containing `KEY`, `TOKEN`, `SECRET`, `PASSWORD`, `AUTH`, `CREDENTIAL`, `SWITCH_API_KEY`, `GITHUB_TOKEN`, `OPENAI_API_KEY`, etc. stripped.
- **Timeout:** 30.0s max.

### Reproduction Probe Script (`repro_probe.py`)
```python
import sys
import requests

try:
    print("Testing requests multipart preparation with non-string form key...")
    req = requests.Request("POST", "http://x", data={1: "a"}, files={"f": b"x"}).prepare()
    print("UNEXPECTED: Request prepared without error.")
    sys.exit(0)
except AttributeError as exc:
    print(f"REPRODUCED_BUG: AttributeError encountered: {exc}")
    import traceback

    traceback.print_exc()
    sys.exit(1)
except Exception as exc:
    print(f"UNEXPECTED_ERROR: {type(exc).__name__}: {exc}")
    sys.exit(2)
```

### Actual Captured Output
```text
Testing requests multipart preparation with non-string form key...
REPRODUCED_BUG: AttributeError encountered: 'int' object has no attribute 'translate'
--- STDERR ---
Traceback (most recent call last):
  File "C:\Users\kamal\AppData\Local\Temp\crewpilot-task-7b20c950-fa7y7fj2\repro_probe.py", line 6, in <module>
    req = requests.Request('POST', 'http://x', data={1: 'a'}, files={'f': b'x'}).prepare()
          ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "C:\Users\kamal\OneDrive\Documents\ChatGPT\CrewPilot\.venv\Lib\site-packages\requests\models.py", line 361, in prepare
    p.prepare(
  File "C:\Users\kamal\OneDrive\Documents\ChatGPT\CrewPilot\.venv\Lib\site-packages\requests\models.py", line 442, in prepare
    self.prepare_body(data, files, json)
  File "C:\Users\kamal\OneDrive\Documents\ChatGPT\CrewPilot\.venv\Lib\site-packages\requests\models.py", line 635, in prepare_body
    (body, content_type) = self._encode_files(files, raw_data)
                           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "C:\Users\kamal\OneDrive\Documents\ChatGPT\CrewPilot\.venv\Lib\site-packages\requests\models.py", line 250, in _encode_files
    body, content_type = encode_multipart_formdata(new_fields)
                         ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "C:\Users\kamal\OneDrive\Documents\ChatGPT\CrewPilot\.venv\Lib\site-packages\urllib3\filepost.py", line 69, in encode_multipart_formdata
    for field in iter_field_objects(fields):
                 ^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "C:\Users\kamal\OneDrive\Documents\ChatGPT\CrewPilot\.venv\Lib\site-packages\urllib3\filepost.py", line 48, in iter_field_objects
    yield RequestField.from_tuples(*field)
          ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "C:\Users\kamal\OneDrive\Documents\ChatGPT\CrewPilot\.venv\Lib\site-packages\urllib3\fields.py", line 240, in from_tuples
    request_param.make_multipart(content_type=content_type)
  File "C:\Users\kamal\OneDrive\Documents\ChatGPT\CrewPilot\.venv\Lib\site-packages\urllib3\fields.py", line 333, in make_multipart
    self._render_parts(
  File "C:\Users\kamal\OneDrive\Documents\ChatGPT\CrewPilot\.venv\Lib\site-packages\urllib3\fields.py", line 287, in _render_parts
    parts.append(self._render_part(name, value))
                 ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "C:\Users\kamal\OneDrive\Documents\ChatGPT\CrewPilot\.venv\Lib\site-packages\urllib3\fields.py", line 258, in _render_part
    return self.header_formatter(name, value)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "C:\Users\kamal\OneDrive\Documents\ChatGPT\CrewPilot\.venv\Lib\site-packages\urllib3\fields.py", line 113, in format_multipart_header_param
    value = value.translate({10: "%0A", 13: "%0D", 34: "%22"})
            ^^^^^^^^^^^^^^^
AttributeError: 'int' object has no attribute 'translate'
```

---

## 3. SQLite TaskStore Audit Records

The durable database records the full transaction history across all 3 Switch agents:

| Timestamp (ISO UTC) | Actor | Event Type | Description |
|---------------------|-------|------------|-------------|
| `2026-11-06T14:34:44.992` | `maintainer-vincenzo` | `TASK_CREATED` | Task initialized from `psf/requests#7634` |
| `2026-11-06T14:34:45.320` | `maintainer-vincenzo` | `DECISION_RECORDED` | Status transitioned from `awaiting_approval` -> `approved` |
| `2026-11-06T14:34:45.946` | `crewpilot-repro-builder` | `STATUS_CHANGED` | Status transitioned from `approved` -> `reproducing` |
| `2026-11-06T14:34:46.551` | `crewpilot-repro-builder` | `EVIDENCE_ADDED` | Reproduction probe artifact attached (exit 1) |
| `2026-11-06T14:34:46.560` | `crewpilot-repro-builder` | `STATUS_CHANGED` | Status transitioned from `reproducing` -> `under_review` |
| `2026-11-06T14:34:47.209` | `crewpilot-reviewer` | `STATUS_CHANGED` | Status transitioned from `under_review` -> `review_approved` |
| `2026-11-06T14:34:47.556` | `maintainer-vincenzo` | `STATUS_CHANGED` | Status transitioned from `review_approved` -> `pr_published` |

---

## 4. Security Audit Verification

- **Process Execution:** Zero shell string interpolation; 100% array invocation (`shell=False`).
- **Environment Isolation:** Zero host tokens (`SWITCH_API_KEY`, `OPENAI_API_KEY`, `GITHUB_TOKEN`) exposed to the reproduction subprocess.
- **Reviewer Independence:** Read-only verification; Reviewer cannot modify evidence or review changes without audit logging.
- **Draft PR Publication Protection:** Only the authenticated human requester (`maintainer-vincenzo`) can authorize publication to `pr_published`.
