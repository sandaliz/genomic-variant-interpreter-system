# Testing — why there are 652 tests

This repository carries 28 Python test modules plus one Node test: about 650
assertions. Every claim the project report makes — a metric, a confidence
score, a quota, an authorisation boundary — is a number or a boundary that
can silently become wrong while the demo still looks fine. In a clinical
decision-support prototype, "looks fine" is exactly how errors ship.

The guiding rule:

> A test exists wherever a wrong value would be **invisible** in a demo but
> **dangerous** in use.

Four kinds of test follow from that:

1. **Arithmetic tests** — IR metrics, confidence maths and calibration are
   asserted against hand-computed values, not against whatever the code
   currently returns.
2. **Boundary tests** — auth, quotas and plan gates are tested at the exact
   line where "allowed" becomes "refused".
3. **Regression tests** — every bug found and fixed keeps its failing case as
   a permanent test (several are documented inline).
4. **Contract tests** — the `AgentMessage` protocol, the MCP adapter and the
   LLM output validators pin interfaces so a change in one place cannot
   silently break another.

## The suite is hermetic

No test needs a running PostgreSQL, a `.env` file, network access or a Gemini
API key. `tests/conftest.py` points `DATABASE_URL` at a placeholder and swaps
every DB session for an in-memory SQLite engine (`StaticPool` +
`check_same_thread=False`, because the TestClient runs the app on a worker
thread and an in-memory database is per-connection). LLM tests run in
rules-only mode or against a stub client. A contributor clones and runs.

## The test modules

Grouped by what they protect:

### The four agents and the control plane

| Module | What it pins |
|---|---|
| `test_intake_agent.py` (21) | NER extraction (gene/HGVS/position/rsID), intent classification, rejection of unsupported genes and malformed queries |
| `test_evidence_agent.py` (15) | Lookup order, HGVS normalisation, top-k shape, IR metadata, encryption of the outgoing payload |
| `test_classification_agent.py` (25) | The rule engine score table, confidence derivation, VUS/conflict caps, criteria naming |
| `test_orchestrator.py` (17) | Supervisor plan is query-derived; Verifier challenges over-confidence, never upgrades; one revision round |

### Information retrieval

| Module | What it pins |
|---|---|
| `test_retrieval.py` (45) | **The only tests that assert numbers against hand-computed values**: BM25 IDF positivity, `b` penalising long documents, `k1` saturation, RRF fusion ordering, nDCG/MAP/MRR discounting. `retrieval/` is importable with no dataset, so the arithmetic is checked in isolation |
| `test_cache_isolation.py` (6) | Two accounts querying the same variant never see each other's cached index state |

### LLM boundary

| Module | What it pins |
|---|---|
| `test_llm_output_checks.py` (12) | The prompt no longer carries ClinVar's prior label (which is also the ground truth); report output is validated — label, confidence and disclaimer checked, missing disclaimer repaired; login does equal work for unknown emails |
| `test_llm_resilience.py` (8) | Model-failover and fallback: an LLM failure degrades to rules-only, never crashes a request |

### Security and accounts

| Module | What it pins |
|---|---|
| `test_security.py` (21) | Sanitizer rejects injection/SQL/markup and long or empty input; prompt-injection denylist; Fernet round-trip; API-key auth accepts only registered keys |
| `test_auth.py` (29) | Register/login/JWT flow, wrong password, expired/forged token, password-change auth, account-deleted revokes access |
| `test_password_reset.py` (25) | 6-digit codes hashed at rest, single use, expiry, attempt budget, no account enumeration |
| `test_pdf_auth.py` (6) | PDF endpoint requires auth; other users' variants are 404; variant markup is escaped |
| `test_api_keys.py` (10) | Scoped keys issued/revoked; the plan gate applies to key calls too |

### The product layer (plans, quotas, admin, UI)

| Module | What it pins |
|---|---|
| `test_entitlements.py` (19) | Monthly quota arithmetic and plan feature gates; quota hit returns 429 **before** any agent runs |
| `test_upgrades.py` (32) | A request alone changes nothing; approval must actually move the account; decided requests stay decided |
| `test_admin.py` (71) | Admin-only endpoints refuse clinicians; no response leaks password material |
| `test_analytics.py` (19) | Counts reflect real rows; empty days are present, not skipped (a skipped day makes a chart lie) |
| `test_classify_api.py` (18) | `/api/classify` contract: auth, quota, response shape |

### Reports, audit, metrics and UI

| Module | What it pins |
|---|---|
| `test_review_signoffs.py` (16) | Sign-off flow: accounts required, auth required, rows survive |
| `test_audit_export.py` (5) | Export contains only the caller's rows, in JSON and CSV |
| `test_profile.py` (20) | Credential change proves account ownership first |
| `test_focused_reports.py` (1) | Population-frequency report path stays neutral |
| `test_evaluation_metrics.py` (35) | Calibration (ECE/Brier) and subgroup fairness: per-stratum, not pooled; small strata flagged; worst stratum named |
| `test_frontend.py` (105) + `tests/js/` | The SPA switches views by toggling one `hidden` class — wrong markup still "renders", just everything at once. HTML/JS is checked structurally |

### Protocols

| Module | What it pins |
|---|---|
| `test_mcp_tools.py` (6) | The MCP tool *functions* (not SDK wiring) import and run **without** the `mcp` package installed — which is what keeps the REST app and the suite dependency-light |

## Running the suite

```bash
source .venv/bin/activate
make test          # python -m pytest tests/ -q          -> 652 passed
make coverage      # same, with a per-module coverage report
make test-js       # node --test tests/js/
make test-all      # both
```

CI (`.github/workflows/ci.yml`) runs the Python suite with coverage on every
push and PR, the JS tests, and — advisory, non-blocking — ruff, mypy (on
`retrieval/` only) and pip-audit.

Counts in brackets above are `def test` functions; parametrised tests expand
to more cases (e.g. `test_retrieval.py`'s 45 functions run far more than 45
assertions).
