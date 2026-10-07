# VariantLens — Genomic Variant Interpretation Agent

**An agentic decision-support system that interprets BRCA1/BRCA2 variants from
ClinVar evidence: retrieval, ACMG-style reasoning, self-critique, and a
report that shows its working.**

![Python](https://img.shields.io/badge/python-3.13-blue)
![Tests](https://img.shields.io/badge/tests-652-brightgreen)
![Coverage](https://img.shields.io/badge/coverage-reported%20in%20CI-blue)
![License](https://img.shields.io/badge/license-MIT-green)

Built for IT3041 — Information Retrieval and Web Analytics.

> **Decision support, not a diagnosis.** Every output carries a confidence score,
> the evidence it rests on, and a disclaimer. See
> [COMPLIANCE_REGULATORY_POSITIONING.md](COMPLIANCE_REGULATORY_POSITIONING.md)
> for what is required before any clinical use.

**Start here:** [Quickstart](#quickstart) ·
[Architecture](docs/architecture.md) ·
[Retrieval & ranking](docs/retrieval.md) ·
[Data model](docs/data-model.md) ·
[Security](SECURITY.md) ·
[Full report](PROJECT_REPORT.md)
---

## What it does

Ask a question in plain English — *"Is BRCA1 c.5946delT pathogenic?"* — and get
back a classification, a confidence score with the arithmetic behind it, the
ClinVar records it retrieved and why those records, an audit trail of every
agent hop, and a plain-language report.

What makes it more than a lookup:

| | |
| --- | --- |
| **Agentic, not a fixed chain** | A Supervisor plans each run — retrieval breadth and verification strictness follow the *query* — and a Verifier can reject a call it finds unearned |
| **Conservative by construction** | Self-correction can only *lower* confidence; a VUS is never presented as high confidence; conflicting evidence caps the call |
| **Genuine IR** | TF-IDF, hand-rolled BM25 and their Reciprocal Rank Fusion, scored with nDCG@k / MAP@k / MRR@k — [docs/retrieval.md](docs/retrieval.md) |
| **Honest evaluation** | Real ClinVar labels, directional agreement, calibration (ECE/Brier), and accuracy stratified by variant type, allele frequency and review level |
| **A product, not just a pipeline** | Accounts, plans, quotas, API keys, audit export, admin analytics, a static SPA, CI, and a security policy that lists its own limitations |

---

## Problem

Genetic testing labs identify thousands of BRCA1/BRCA2 variants, but experts
frequently disagree on how to classify them — whether a variant is
**Pathogenic** (cancer-causing), **Benign** (harmless), or of **Uncertain
Significance (VUS)**.

This is not an edge case in our data. The study workbook is drawn entirely from
ClinVar rows flagged as conflicting, and of its 3,009 variants only **1,168 carry
a single determinate consensus label**. The rest are literally *"Conflicting
classifications of pathogenicity"* — a statement about what submitters did, not a
class any system can predict. That distinction shapes the whole evaluation: a
naive exact-match accuracy over all 3,009 rows measures the dataset's ambiguity
more than the system's quality.

When a patient receives a genetic test result, a clinician needs a clear,
evidence-based answer: **"Does this variant increase cancer risk?"** — and, just
as importantly, needs to know how much the answer can be trusted.

## Research / Technical Challenge

The system must:

1. **Retrieve relevant genomic evidence** from a large variant database using proper Information Retrieval techniques.
2. **Classify the variant** using both clinical rules (ACMG-style) and an LLM — without treating the LLM as an unquestionable authority.
3. **Communicate results transparently** with explicit confidence levels and appropriate caveats.

## Solution

A **four-agent pipeline under a supervisor and a verifier**, where every hop is an
encrypted, logged message:

```
Query → NLP → Information Retrieval → Reasoning → Summarization → Responsible AI Report
```

| Agent | Module | Technology |
|---|---|---|
| **Intake Agent** | NLP | Named Entity Recognition, input sanitization |
| **Evidence Agent** | Information Retrieval | TF-IDF / BM25 / RRF hybrid indexing, candidate retrieval, relevance ranking, top-K selection |
| **Classification Agent** | LLM + Rules | Google Gemini + deterministic ACMG-style scoring |
| **Report Agent** | NLP + Responsible AI | Structured/abstractive summarization, explainability, disclaimers |

## Architecture

```
User query (natural language)
   |
   v
[Auth check] --------- protocol/security.py
   |
   v
Agent 1: Intake Agent -------------- NLP (NER) + input sanitization
   |  (encrypted message)
   v
Supervisor Agent ------------------- reads the intake result, plans the run
   |  (encrypted PLAN message: retrieval mode + verification budget)
   v
Agent 2: Evidence Retrieval Agent --- Information Retrieval (ClinVar lookup, TF-IDF, Top-K)
   |  (encrypted message)
   v
Agent 3: Classification Agent ------- LLM (Gemini) + rule-based ACMG-style reasoning
   |  (encrypted message)
   v
Agent 4: Verifier Agent ------------- critiques the call; may send it back ONCE
   |  (encrypted VERIFICATION_RESULT message)
   |  ...if over-confident, Agent 3 re-states it conservatively, then Agent 4 re-checks
   v
Agent 5: Report Agent ---------------- LLM (Gemini) + NLP (summarization) + Responsible AI
   |  (encrypted message)
   v
Final report
```

All agents communicate exclusively through a **standardized AgentMessage protocol** defined in `protocol/message.py`, which includes: `sender`, `receiver`, `message_type`, `trace_id`, `timestamp`, and an encrypted payload. Every interaction is **message-encrypted with Fernet** (AES-128-CBC + HMAC-SHA256) before being written to the shared `MessageLog`, with trace IDs for end-to-end audit. Note: for the academic prototype, the Fernet key is generated per-process — each agent instance decrypts only its own messages. A production deployment would load a shared key from a secret manager. See `SECURITY.md` for the full security policy.

### Agentic behaviour: planning and self-correction

Four specialists in a fixed chain is a pipeline; the Control Plane in
`agents/orchestrator.py` is what makes it *agentic*:

| Agent | Decides | Why it matters |
|---|---|---|
| **Supervisor** | Per query: retrieve *broad* (bare HGVS / rsID / position) or *targeted* (gene + variant), and how strict verification should be | The path through the pipeline is **derived from the query**, not hard-coded |
| **Verifier** | Whether a classification's confidence is *earned* by the evidence | Catches a confident call that is not backed by an exact match or reviewed consensus |

When the Verifier returns `revise`, the Classification Agent re-states the call
with **lower** confidence and the Verifier checks it again. Three properties keep
this safe and bounded:

* **Bounded** — `verification_budget` in the plan is 1, so a run can never loop.
* **Conservative** — the directive only ever *lowers* confidence. Self-correction
  cannot manufacture certainty, and it never changes the categorical
  classification (that is an evidence judgement, not a consistency one).
* **Auditable** — the plan and the verdict are ordinary `PLAN` /
  `VERIFICATION_RESULT` messages, so both appear in the audit trail next to the
  four specialist messages.

The result is exposed as `result["agentic"]` (`plan`, `verification`,
`revision_rounds`) and as two entries in `pipeline_steps`.

## Information Retrieval

The Evidence Agent is a genuine IR system with:

- **Indexing**: a text index over every variant's combined annotation text, with three interchangeable rankers — **TF-IDF cosine similarity** (default), **BM25** (Okapi, hand-rolled), and **hybrid** (Reciprocal Rank Fusion of the two). Select one with the `ranker=` argument to `EvidenceRetrievalAgent` or the `VL_RANKER` environment variable; the default is unchanged, so existing behaviour and expectations are untouched
- **Candidate Retrieval**: exact variant match, gene lookup, genomic position, rsID, and lexical search — in that order of decreasing evidence strength
- **Relevance Ranking**: weighted scoring (exact match +10, gene match +5, position +5, HIGH impact +2, conflict −2, evidence completeness 0–6, lexical similarity 0–5, ClinVar review data +1; theoretical maximum 34.0)
- **Top-K Selection**: returns the top 5 most relevant evidence records with relevance scores and a written explanation of each
- **IR Metadata**: every result includes the retrieval strategy, candidate count, ranking method and the active lexical ranker
- **Ranking Metrics**: Precision@k, Recall@k, nDCG@k, MAP@k and MRR@k over the live pipeline's ranking

The three rankers are compared head-to-head **over the whole corpus** — every
query scored against every row — because the exact-variant path short-circuits
lexical ranking entirely, so a pipeline-level comparison would compare nothing:

```bash
python evaluation.py --rankers-only --sample-size 50   # no LLM calls, no labels needed
```

Measured results are in [PROJECT_REPORT.md](PROJECT_REPORT.md); the design
rationale (why RRF rather than score averaging, why the `+1` in BM25's IDF, why
MAP@k equals MRR@k on this corpus) is in **[docs/retrieval.md](docs/retrieval.md)**.

### Missing Evidence Policy

Some annotations are not applicable to every variant. In particular, SIFT and
PolyPhen may be unavailable for non-protein-changing or structural variants,
and CADD may be unavailable for some structural variants. Missing values are
preserved as missing and are exposed through the `predictor_available` metadata;
they are never converted to zero or interpreted as benign evidence.

The classifier applies SIFT/PolyPhen rules only when both predictions are
available and applies the CADD signal only when the score is finite. Missing
predictors reduce confidence rather than changing the classification directly.
Reports display unavailable evidence as `not available`.

### ACMG Evidence Presentation

The classifier keeps three output layers strictly separate:

- `acmg_criteria_applied` — validated ACMG/AMP-style codes only
  (`BA1/PM2/PVS1/PP3/BP4/BP7` equivalents). `criteria_applied` is kept as a
  backward-compatible alias.
- `computational_annotations` — CADD score, SIFT/PolyPhen observations.
  Supporting information only; a high CADD score is never itself an ACMG code.
- `clinvar_context` + `population_observation` — ClinVar submitter agreement
  and the neutral allele-frequency note. Database context, not ACMG criteria.
- `review_required` + `coverage_fallback` — always-present booleans surfaced in the
  API response. `review_required` is true when the evidence-based call disagrees with
  ClinVar's reviewed consensus, so the result is flagged for human review;
  `coverage_fallback` is true when the variant was resolved from the ClinVar coverage
  index instead of the study workbook, with `evidence_limitation` explaining the
  reduced evidence behind the call.

BS1 ("allele frequency greater than expected for the disorder") is
**unestablished** in this prototype: it requires a validated disease-specific
maximum credible AF (prevalence + penetrance + allelic heterogeneity), which is
not configured. An AF such as 0.0002 is therefore reported as a neutral
population-frequency observation, never as `BS1_equivalent`. Only AF > 0.001
(BA1 project proxy) scores as benign frequency evidence.

VUS reasoning states "insufficient validated ACMG/AMP evidence" rather than
summing discordant flags, and the report distinguishes the automated
classification (`VUS / unresolved`) from the clinical status
(`Not established; requires certified clinical review`).

### Security: Prompt-Injection Defense

`sanitize_query()` blocks explicit prompt-injection / jailbreak patterns
(e.g. "ignore previous instructions", "reveal system prompt", "developer
mode", role-hijack phrasing) **before** any query reaches the intake NLP or
any LLM agent, via `is_prompt_injection()` in `protocol/security.py`. Covered
by `tests/test_security.py` (pattern unit tests) and
`tests/test_intake_agent.py` (end-to-end rejection through `IntakeAgent`).

### Resilience: 503 Fallback

If Gemini returns HTTP 503 (or 429) on the primary model *and* every fallback
model, `GeminiClient.generate()` raises a plain `RuntimeError` (never a raw
Google `APIError`), the Classification Agent degrades to deterministic
rule-based scoring, and the Report Agent degrades to the template report.
Locked in by `TestServiceUnavailableCascade` in
`tests/test_llm_resilience.py` — a hermetic end-to-end 503 cascade test with
no network calls.

### Accounts, Plans & Quotas

`/api/classify`, `/api/trail/*` and `/api/pipeline/*` require a JWT bearer
token. Accounts, subscription plans and monthly usage live in PostgreSQL and are
enforced **before** any agent runs:

- **Argon2id** password hashing, HS256 access tokens (60 min default).
- Identity is **re-read from the database on every request**, so deactivating an
  account revokes access immediately even with an unexpired token.
- **Plans** (Free / Professional / Enterprise) gate the LLM path: Free runs the
  full four-agent pipeline with rule-based reasoning instead.
- **Quotas** are counted per UTC month from usage rows. A pre-flight check returns
  `429` before the pipeline starts, and the commit-time re-check takes a
  PostgreSQL row lock so concurrent requests cannot overshoot the limit.

The agent layer keeps its own API-key check as defence-in-depth, and results are
cached per identity so one account can never be served another's cached verdict.
See **[AUTH.md](AUTH.md)** for the full design and endpoint list.
## Supported Query Types

You don't need special syntax — just ask in plain English:

| Query Type | Example |
|---|---|
| **Gene + Variant** | "Is BRCA1 NC_000017.10:g.41197778A>G pathogenic?" |
| **Gene only** | "What variants are in BRCA1?" |
| **Variant only** | "What is NC_000017.10:g.41201179C>T?" |
| **By position** | "What variant is at position 41197778 in BRCA1?" |
| **rsID** | "Tell me about rs123456" |

## LLM Integration (Google Gemini)

- **Classification Agent** — ACMG-style reasoning over evidence
- **Report Agent** — plain-language clinical reports + abstractive summarization

If no API key is configured, the system gracefully falls back to deterministic rule-based classification and template-based reports.

## Quickstart

### Get running in 5 minutes

Identical on **macOS, Windows and Linux**. The only thing you need beyond Python
is a free PostgreSQL database — there is no Docker step and nothing to install
system-wide.

```bash
git clone <repo-url>
cd variant_agent_system
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                   # Windows: copy .env.example .env
```

Create a free database at **https://neon.com** (sign up, *Create project* — no
credit card) and paste the connection string Neon gives you into `.env` as
`DATABASE_URL`. Paste it **verbatim**; do not hand-edit it.

```bash
# one-time schema + reference data
alembic upgrade head
python scripts/seed_plans.py        # Free / Professional / Enterprise
python scripts/create_admin.py      # bootstrap admin; prints the password once

# run it
uvicorn api:app --reload --port 8000
```

Open <http://localhost:8000>, register an account, sign in.

**If the first request feels slow:** Neon scales to zero after ~5 minutes idle,
so the first call after a break takes a second or two while it wakes up. That is
normal, not a hang.

### Prerequisites
- Python 3.13 — the version CI, the Docker image and CONTRIBUTING.md all pin.
  The pinned notebook stack (`numpy`, `scikit-learn`) does not ship wheels for
  3.10, so the old "3.10+" line here was wrong.
- A free PostgreSQL connection string (Neon recommended — works everywhere)
- Gemini API key *(optional — the Free plan runs rule-based without it)*

### 1. Install & configure

```bash
pip install -r requirements.txt        # or requirements-api.txt for a lean install
cp .env.example .env                   # add DATABASE_URL + both JWT keys + GEMINI_API_KEY
```

Two secrets are required, not one, and the process refuses to start without
both: `JWT_SECRET_KEY` (signs the token) and `JWT_ENCRYPTION_KEY` (encrypts it).
Generate each with
`python -c "import secrets; print(secrets.token_urlsafe(48))"` and keep them
different — one key for both jobs would turn a signing-key leak into a
decryption leak as well.

### 2. Initialise the database

```bash
alembic upgrade head          # plans, users, usage_records, query_history,
                              # review_signoffs, password_resets, upgrade_requests,
                              # auth_events, api_keys  (nine tables)
python scripts/seed_plans.py  # Free / Professional / Enterprise
python scripts/create_admin.py  # prompts for the first admin's password
```

### 3. Run the CLI

The CLI talks to the agents directly and needs no account:

```bash
python3 main.py                                    # default demo query
python3 main.py --query "What variants are in BRCA1?"
python3 main.py --no-llm                            # rule-based only
```

### 4. Run the Web UI

```bash
uvicorn api:app --reload --port 8000
# open http://localhost:8000, register an account, sign in
```

### 5. Run tests

```bash
python -m pytest tests/ -v    # 652 tests — no database needed at all
node --test tests/js/          # frontend JS unit tests
python scripts/smoke_auth.py  # end-to-end check against the real PostgreSQL
```

On Windows the interpreter is normally `python` rather than `python3`; read
`python3 ...` above as `python ...`.

### 6. Run evaluation

```bash
python3 evaluation.py                    # full suite
python3 evaluation.py --sample-size 50 --no-llm  # quick
```

### 7. Run the MCP server (optional)

The agents are also exposed over the **Model Context Protocol**, so an MCP host
(Claude Desktop, an IDE agent, any MCP client) can call the pipeline as tools
without knowing anything about the internal `AgentMessage` protocol. Tools:
`classify_variant`, `retrieve_evidence`, `list_example_queries`.

```bash
pip install mcp        # optional — the REST API does not need it
python mcp_server.py   # stdio transport
```

An example Claude Desktop `mcpServers` entry:

```json
{
  "mcpServers": {
    "variantlens": { "command": "python", "args": ["/path/to/mcp_server.py"] }
  }
}
```

`mcp` is an optional dependency: `mcp_server.py` imports cleanly without it (the
SDK import is inside `build_server()`), so the test suite and the REST app stay
dependency-light. The MCP tools are rule-based by default, so they are
deterministic and need no Gemini key.

### 8. Run analysis notebook (for visualizations)

```bash
jupyter notebook analysis.ipynb
```

The analysis notebook provides:
- Data exploration graphs and statistical analysis
- Real ClinVar label, review-star and system-performance visualizations
- Evaluation of the deployed agents against real ClinVar labels (evidence-based vs
  consensus-reconciled results, directional agreement, predictor completeness)
- Information retrieval performance metrics (Precision@5, Recall@5, Exact-Match@5, MRR@5)
- Confidence-score distribution (Responsible AI calibration) and system architecture diagram

Evaluation reports classification accuracy separately for complete and
incomplete predictor records. Ground truth defaults to the **real ClinVar
clinical significance** in the enriched workbook (`--ground-truth proxy` restores
the older evidence-derived labels, which only measure self-consistency), and the
subgroup metrics should be reported alongside overall accuracy rather than hidden
by it.

Generated visualizations are saved to `analysis_results/` directory.

## Evaluation Results

Running `python3 evaluation.py` provides honest metrics against **real ClinVar
labels** when the enriched workbook is present:

```
CLASSIFICATION METRICS (real ClinVar labels, 3,009 variants)
  [1] Evidence-based call (the agent's own ACMG reasoning):
      144/3009  ->  4.8%
  [2] After ClinVar consensus reconciliation (what a user sees):
      1145/3009 ->  38.1%

DIRECTIONAL AGREEMENT (pathogenic / benign / uncertain, 1,168 determinate rows)
  Evidence-based call:             497/1168  (42.6%)
  After consensus reconciliation: 1148/1168  (98.3%)

SAFETY-RELEVANT FALSE POSITIVES
  Benign-truth called pathogenic:  53/497 (10.7%) -> 1/497 (0.2%) reconciled
```

```bash
python evaluation.py --dataset data/clinvar_variant_dataset_enriched.xlsx \
                     --ground-truth real --sample-size 3009 --no-ir --no-ablation --no-llm
```

The evidence-based figures measure the agent's own reasoning; the reconciled
figures include deferring to a reviewed ClinVar consensus (≥2-star, always
disclosed in the report). Re-run deterministically with `--no-llm`
(rule-based baseline, no Gemini calls); without that flag any configured
`GEMINI_API_KEY` makes live LLM calls and the evidence-based counts will vary
run to run. `--ground-truth proxy` reproduces the older
self-consistency number (≈89%) and is reported only as such — those labels are
derived from the same evidence rules the classifier applies. See
`data/DATA_SOURCE.md` for the full methodology.

### Beyond accuracy: calibration, fairness and the ranker comparison

`evaluation.py` also reports the three things a pooled accuracy number hides.

**Calibration** — an ECE, a reliability table (with an ASCII reliability strip)
and a Brier score with its skill score against the base rate, computed three
ways: the evidence-based confidence against the evidence-based call, the
reconciled confidence against the call the user is shown, and the evidence-based
confidence against clinical *direction*. The point of the exercise: a confidence
score is only worth showing if the rows it marks 80 are right about 80% of the
time.

**Fairness** — accuracy, reconciled accuracy, directional accuracy and macro F1
stratified by **variant type**, **allele-frequency band** (the ACMG/AMP
thresholds, not arbitrary deciles) and **ClinVar review level**. Every stratum
carries its own count, and strata too small to read are flagged `small` so a
2-row group cannot be reported as the worst finding. The stratification keys are
properties of the *input*, never of the prediction, so the groups cannot be
shaped by the thing being measured.

**The lexical rankers** — TF-IDF vs BM25 vs RRF-hybrid, ranked over the whole
corpus. See [docs/retrieval.md](docs/retrieval.md) for why the experiment has to
be structured that way.

```bash
python evaluation.py --dataset data/clinvar_variant_dataset_enriched.xlsx --no-llm
python evaluation.py --rankers-only --sample-size 50     # retrieval only, seconds
```

The measured tables — including the ranker comparison and the reliability and
subgroup tables — are in **[PROJECT_REPORT.md](PROJECT_REPORT.md)**. Unit tests
for the arithmetic behind them live in `tests/test_evaluation_metrics.py` and
`tests/test_retrieval.py`, asserted against hand-computed values.

## Documentation

The project includes:

- `docs/` — the engineering documentation set: [architecture](docs/architecture.md) (component, verification-loop and trust-boundary diagrams), [sequence](docs/sequence.md) (the query and sign-in flows as executed), [data-model](docs/data-model.md) (ER diagram, retention and the migration table) and [retrieval](docs/retrieval.md) (the retrieval ladder, the three lexical rankers and the ranking metrics). All Mermaid, all rendered by GitHub.
- `SECURITY.md` — the security policy: data inventory, implemented controls with file references, and a deliberately blunt list of ten known limitations.
- `CONTRIBUTING.md` — setup, layout, test conventions, and the two structural rules (pure retrieval maths lives in `retrieval/`; a model change needs a migration).
- `COMPLIANCE_REGULATORY_POSITIONING.md` — one-page commercialization/compliance positioning with explicit prototype limitations.
- `data/DATA_SOURCE.md` — dataset provenance, the ClinVar label enrichment and the honest
  evaluation methodology.
- `scripts/` — `enrich_clinvar_labels.py` (ClinVar label join) and
  `build_variant_summary_index.py` (BRCA coverage index used for rsID lookup and
  variants absent from the study workbook), plus the setup scripts
  (`seed_plans.py`, `create_admin.py`, `smoke_auth.py`) and the one-off
  `import_review_signoffs.py` backfill.
- `REVIEW_SIGNOFF_WORKFLOW.md` — human review / sign-off workflow.
- `ANALYSIS_GUIDE.md` — how to reproduce the analysis notebook and figures.
- `protocol/PROTOCOL.md` — full agent communication protocol spec (AgentMessage envelope,
  message types — including the agentic `PLAN` / `VERIFICATION_RESULT` control messages —
  encryption guarantees, a complete worked message flow, audit-trail API).
- `mcp_server.py` — the same agents exposed over the Model Context Protocol, for MCP hosts
  (Claude Desktop, IDE agents) to call as tools.
- `Dockerfile` / `Makefile` — a production image (non-root, uvicorn) and a single entry point for the common jobs (`make test`, `make run`, `make evaluate-rankers`, `make migrate`).
- `.github/workflows/ci.yml` — CI running the Python suite with coverage and the Node test suite on every push, plus non-blocking lint, type and dependency-advisory checks.
- `frontend/` — the web app in three files: `index.html` (landing page, workspace, profile, admin dashboard and the auth modal), `app.css`, and `app.js`. The landing page explains the tool and offers example queries; the query surface only appears once you are signed in; administrators get an **Admin** link in the header.
- `POST /api/report/pdf` — server-generated PDF report with evidence summary and reviewer/sign-off fields.

The PDF feature is a **report-generation convenience**, not a claim that the output is a certified clinical document.

## Security Features

- **Input sanitization** (`protocol/security.py`) blocks malformed/malicious queries
- **JWT account authentication** — `POST /api/classify`, `/api/trail/*` and
  `/api/pipeline/*` **require** a credential and return **401** without one. Three
  are accepted, in precedence order: an `X-API-Key: vl_…` header (plans with
  `api_access`), `Authorization: Bearer <token>`, or the HttpOnly session cookie.
  Accounts, passwords (Argon2id) and plans live in PostgreSQL; see
  [AUTH.md](AUTH.md).
- **Identity re-checked per request** — deactivating an account
  revokes access immediately, and the `role` used for authorization always comes
  from the database, never from the token
  (there is no account-deletion endpoint; see [SECURITY.md](SECURITY.md))
- **Role-based access with self-lockout guards** — an admin cannot change their own
  role or active state, and the last active administrator cannot be demoted or
  deactivated (otherwise recovery needs a database shell)
- **Plan feature gating** — the Free plan runs the full pipeline with rule-based
  reasoning; LLM-assisted reasoning requires Professional or Enterprise
- **Monthly quota enforcement** — pre-flight `429` before the pipeline runs, plus a
  commit-time re-check under a PostgreSQL row lock so concurrent requests cannot
  overshoot the limit
- **Defence in depth** — the agent layer keeps its own API-key check, so a call that
  bypasses FastAPI is still refused; cached results are keyed per identity
- **Forgot password** — a 6-digit code delivered through a pluggable mailer
  (`log` / `resend` / `smtp`). Codes are stored only as salted SHA-256 digests,
  are single-use, expire in 10 minutes and burn after 5 attempts; the request
  endpoint answers identically for unknown addresses so it cannot enumerate accounts
- **Plan changes are a hand-off** — a user requests a paid plan, an administrator
  decides. No payment integration is claimed; the approval applies the plan change
  in the same transaction as the decision, and decided requests are terminal
- **Scoped API keys** — programme level calls can authenticate with
  `X-API-Key: vl_…` instead of a session. Keys are stored only as SHA-256 digests
  (the raw value is shown once, at creation), carry a display prefix, can be
  revoked at any time, and are **re-checked against the owner's plan on every
  request** — downgrading a plan disables its keys immediately. Issuing a key is
  an Enterprise entitlement (`plan.api_access`)
- **Audit export** — a Professional/Enterprise entitlement
  (`plan.audit_export`). `GET /api/export/audit` returns the **caller's own**
  usage records, auth events and review sign-offs as JSON, or `?format=csv` for a
  spreadsheet. The query is scoped by user id, so one account can never export
  another's activity
- **Prompt-injection defense** — a regex blocklist rejects system-prompt overrides,
  exfiltration, role-hijack and jailbreak phrasing before any NLP or LLM sees the query
- **Symmetric encryption** — all agent messages are encrypted with Fernet before being written to the shared message log. Note: the demo uses a per-process key (see `SECURITY.md`); a production deployment loads a shared key from environment variables.
- **Full audit logging** with trace IDs for every agent-to-agent message
- **Secrets stay out of version control** — `.env` is gitignored and
  `.dockerignore` keeps it out of the image build context, so no image layer ever
  contains it. Every deployment sets `DATABASE_URL` and `JWT_SECRET_KEY` in its
  own environment
- **Encrypted in transit** — the database connection requires TLS (`sslmode=require`); agent-to-agent payloads are encrypted with Fernet before being written to the shared message log (demo uses a per-process key).
- **Cold-start tolerant** — `pool_pre_ping=True` handles a hosted database that
  scales to zero between requests
- **Restricted CORS** — allowed origins are explicit (`FRONTEND_ORIGINS`, defaulting to localhost)
  rather than `*`

## Responsible AI

- **Confidence calibration**: a genuine conflict (ClinVar's own `Conflicting` consensus when available, otherwise the dataset flag) forces low confidence; the categorical label is always derived from the numeric score; VUS never presented as confident
- **Explainability**: every report lists the evidence + ACMG criteria applied
- **Transparency**: full audit trail with `trace_id` for end-to-end traceability
- **Data protection**: all messages are encrypted with Fernet before being written to the shared message log. The demo uses a per-process key (see `SECURITY.md`); a production deployment loads a shared key from environment variables.
- **Fairness (explicit limitation)**: every result carries a population-data limitation in its
  machine-readable `responsible_ai` block, rendered in the UI and the PDF:

  > *Population-data limitation: Allele-frequency databases may have uneven representation across
  > ancestry groups. Population frequency evidence should therefore be interpreted cautiously and
  > should not be treated as equally informative for all populations.*

  This is why an intermediate allele frequency is reported as a neutral observation and `BS1` is
  never auto-assigned.
- **Strong disclaimer**: every report states it is an academic decision-support prototype, not a clinical diagnosis

## Commercialization Strategy

### Target Users / Market

- **Primary:** Genetic counselors and clinical genetics labs
- **Secondary:** Hereditary cancer screening programs, research institutions
- **Tertiary:** Direct-to-consumer genetic testing companies

### Pricing Model

Prices are shown in LKR in the product (converted from the USD figures below at
the rate the frontend applies, `LKR_PER_USD` in `frontend/app.js`).

| Tier | Price | Features |
|---|---|---|
| **Free / Research** | $0 · Rs. 0 | 10 queries/month, rule-based mode |
| **Professional** | $99/month · Rs. 29,700 | 500 queries/month, LLM mode, API access, audit export |
| **Enterprise** | Custom | Unlimited queries, LLM mode, API access, audit export, on-prem deploy, SLA |

> These tiers are **illustrative scenarios, not a shipping offer**. The prototype is
> not clinically validated or regulatory-cleared; see
> `COMPLIANCE_REGULATORY_POSITIONING.md` for the validation, hardening and
> regulatory path required before any commercial use.
>
> Plan changes are a hand-off, not a checkout: a user requests a paid plan and an
> administrator decides. **No payment integration is claimed or implemented.**
> An administrator cannot request an upgrade for themselves — accounts move
> between plans from the admin dashboard.

### Deployment Ideas

- Cloud-hosted API (AWS/GCP/Azure) with FastAPI
- On-premise deployment for hospitals/labs with strict data privacy
- LIMS integration via REST API
- Batch processing for mass-scale variant analysis

## Project Structure

```
variant_agent_system/
├── agents/
│   ├── intake_agent.py          # Agent 1: NLP + Security
│   ├── evidence_agent.py        # Agent 2: Information Retrieval (TF-IDF / BM25 / hybrid, Top-K, rsID)
│   ├── classification_agent.py  # Agent 3: LLM + Rules + ClinVar consensus reconciliation
│   ├── orchestrator.py          # Control plane: Supervisor (planning) + Verifier (self-correction)
│   └── report_agent.py          # Agent 4: LLM + Summarization + Responsible AI
├── retrieval/                   # Pure retrieval maths -- importable with no agent dependencies
│   ├── bm25.py                  # Okapi BM25 (hand-rolled; k1=1.2, b=0.75, Lucene IDF)
│   ├── fusion.py                # Reciprocal Rank Fusion + score fusion
│   └── metrics.py               # P@k, R@k, nDCG@k, MAP@k, MRR
├── data/
│   ├── clinvar_variant_dataset.xlsx           # 3,009 variants (tracked)
│   ├── clinvar_variant_dataset_enriched.xlsx  # + real ClinVar labels (generated)
│   ├── clinvar_labels.csv                     # ClinVar label sidecar the enrichment writes
│   ├── variant_summary_brca.tsv               # BRCA coverage / rsID index (generated)
│   ├── review_signoffs.example.json           # shape of the pre-0002 local sign-off store
│   └── DATA_SOURCE.md                         # provenance + evaluation methodology
├── docs/                        # Architecture, sequence, data-model and retrieval documents (Mermaid)
├── frontend/
│   ├── index.html               # Landing, workspace, profile, admin views + auth modal
│   ├── app.css                  # All styling, built from the same design tokens
│   └── app.js                   # Views, auth flows, renderers, admin dashboard
├── llm/
│   └── gemini_client.py         # Gemini API wrapper (bounded retries, graceful fallback)
├── protocol/
│   ├── message.py               # Agent communication protocol + encryption
│   ├── security.py              # Auth, sanitization, encryption
│   └── PROTOCOL.md              # Full protocol spec: envelope, flows, audit trail
├── auth/                        # JWT account layer (sits in front of the agents)
│   ├── dependencies.py          # get_current_user(), require_admin()
│   ├── routes.py                # /api/auth/register, /login, /me, reset, profile
│   ├── schemas.py               # Pydantic auth contracts
│   ├── security.py              # Argon2id hashing + JWE/JWS token mint/decode
│   ├── cookies.py               # HttpOnly session cookie + CSRF double-submit
│   ├── rate_limit.py            # Per-IP and per-account login throttling
│   └── audit.py                 # Hashed-identifier auth-event recording
├── db/                          # Account/entitlement persistence (agents never import it)
│   ├── database.py              # Engine, SessionLocal, get_db()
│   └── models.py                # Plan, User, UsageRecord, ReviewSignoff, PasswordReset,
│                                #   UpgradeRequest, AuthEvent, ApiKey, QueryHistory
├── services/
│   ├── entitlement_service.py   # Quota windows, feature gates, usage recording
│   ├── api_key_service.py       # Scoped API keys: issue, resolve, list, revoke
│   ├── history_service.py       # Per-account query history reads
│   ├── analytics_service.py     # Admin dashboard figures (gap-filled daily series)
│   ├── mailer.py                # Pluggable outbound email (log / resend / smtp)
│   ├── password_reset.py        # Issue + redeem one-time reset codes
│   └── upgrade_service.py       # Upgrade request lifecycle
├── migrations/                  # Alembic migrations (nine tables)
├── scripts/
│   ├── enrich_clinvar_labels.py          # Join ClinVar labels onto the dataset
│   ├── build_variant_summary_index.py    # Build the BRCA rsID/coverage index
│   ├── seed_plans.py                     # Free / Professional / Enterprise plans
│   ├── create_admin.py                   # Bootstrap admin account
│   ├── import_review_signoffs.py         # One-off backfill of the pre-0002 JSON sign-off store
│   └── smoke_auth.py                     # End-to-end auth check against PostgreSQL
├── tests/
│   ├── conftest.py                      # Shared fixtures: in-memory SQLite, placeholder secrets
│   ├── test_intake_agent.py         ├── test_admin.py
│   ├── test_evidence_agent.py       ├── test_review_signoffs.py
│   ├── test_classification_agent.py ├── test_password_reset.py
│   ├── test_llm_resilience.py       ├── test_upgrades.py
│   ├── test_security.py             ├── test_profile.py
│   ├── test_auth.py                 ├── test_analytics.py
│   ├── test_entitlements.py         ├── test_cache_isolation.py
│   ├── test_classify_api.py         ├── test_orchestrator.py
│   ├── test_retrieval.py            ├── test_api_keys.py
│   ├── test_evaluation_metrics.py   ├── test_audit_export.py
│   ├── test_mcp_tools.py            ├── test_frontend.py
│   ├── test_pdf_auth.py             └── test_llm_output_checks.py
├── tests/js/
│   └── apply_session_visibility.test.mjs   # Node test for the session-visibility JS
├── .github/workflows/ci.yml     # CI: pytest (+coverage) + Node JS tests + advisory lint/types/audit
├── .dockerignore                # Keeps secrets and local state out of the image
├── .env.example                 # Template for API keys
├── .gitignore                   # Generated ClinVar artifacts are gitignored
├── ANALYSIS_GUIDE.md            # How to reproduce the notebook and its figures
├── AUTH.md                      # Auth, plan and quota design documentation
├── CONTRIBUTING.md              # Setup, layout, test conventions, clinical-safety rules
├── Dockerfile                   # Production image (non-root, uvicorn)
├── LICENSE                      # MIT License
├── Makefile                     # make test / run / evaluate / migrate / lint
├── REVIEW_SIGNOFF_WORKFLOW.md   # Human review / sign-off workflow
├── SECURITY.md                  # Security policy and the list of known limitations
├── analysis.ipynb               # Production evaluation & visualization notebook
├── analysis_results/            # Generated visualizations for presentations
├── alembic.ini                  # Alembic config
├── api.py                       # FastAPI REST API
├── COMPLIANCE_REGULATORY_POSITIONING.md
├── evaluation.py                # Evaluation: classification, IR, calibration, fairness, ablation
├── main.py                      # Orchestrator
├── mcp_server.py                # MCP tool server (optional `mcp` dependency)
├── PROJECT_REPORT.md
├── pyproject.toml               # ruff / mypy / coverage configuration
├── pytest.ini                   # Pytest config
├── requirements.txt             # Full development stack (incl. the notebook deps)
└── requirements-api.txt         # Lean runtime set the API and the image install
```

Generated data files (`*_enriched.xlsx`, `variant_summary_brca.tsv`, the ClinVar
source archives and TF-IDF caches) are gitignored and reproducible via the
scripts in `scripts/` — see `data/DATA_SOURCE.md`.

## Team / Contributors

Each member owned one major subsystem; tests and documentation for that subsystem
were owned by the same person, so responsibility was evenly distributed.

| Contributor | Primary responsibility |
|---|---|
| **Sanulya** | Evidence Retrieval Agent (Information Retrieval: TF-IDF indexing, ranking, top-K) + ClinVar data + `scripts/` |
| **Nihindu** | Intake Agent (NLP entity extraction + input sanitization) + REST API (`api.py`) + web frontend (`frontend/`: landing page, workspace, profile, admin dashboard) |
| **Nipuni** | Report Agent (summarization + Responsible AI) + Gemini client (`llm/`) + agent communication/security (`protocol/`) |
| **Sandali** | Classification Agent (ACMG-style rules + LLM reasoning) + orchestration/integration (`main.py`) |

**Supporting work (tests, evaluation and documentation), distributed per component:**

| Contributor | Supporting work |
|---|---|
| Sanulya | `tests/test_evidence_agent.py`, dataset provenance (`data/DATA_SOURCE.md`), evaluation support |
| Nihindu | `tests/test_intake_agent.py`, API/frontend usage docs |
| Nipuni | `tests/test_llm_resilience.py`, `tests/test_security.py`, protocol specification (`protocol/PROTOCOL.md`) |
| Sandali | `tests/test_classification_agent.py`, integration tests, overall README and project documentation |

