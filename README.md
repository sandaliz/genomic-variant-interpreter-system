# Genomic Variant Interpretation Agent

**An AI-powered decision-support system for interpreting BRCA1/BRCA2 genetic variants using information retrieval, rule-based reasoning and LLM-based analysis.**

Built for IT3041 — Information Retrieval and Web Analytics.

---

## Problem

Genetic testing labs identify thousands of BRCA1/BRCA2 variants, but experts frequently disagree on how to classify them — whether a variant is **Pathogenic** (cancer-causing), **Benign** (harmless), or of **Uncertain Significance (VUS)**. This is visible directly in our dataset via the `Conflicting_Classification_Flag`.

When a patient receives a genetic test result, a clinician needs a clear, evidence-based answer: **"Does this variant increase cancer risk?"**

## Research / Technical Challenge

The system must:

1. **Retrieve relevant genomic evidence** from a large variant database using proper Information Retrieval techniques.
2. **Classify the variant** using both clinical rules (ACMG-style) and an LLM — without treating the LLM as an unquestionable authority.
3. **Communicate results transparently** with explicit confidence levels and appropriate caveats.

## Solution

A **4-agent AI pipeline** that separates responsibilities:

```
Query → NLP → Information Retrieval → Reasoning → Summarization → Responsible AI Report
```

| Agent | Module | Technology |
|---|---|---|
| **Intake Agent** | NLP | Named Entity Recognition, input sanitization |
| **Evidence Agent** | Information Retrieval | TF-IDF indexing, candidate retrieval, relevance ranking, top-K selection |
| **Classification Agent** | LLM + Rules | Google Gemini + deterministic ACMG-style scoring |
| **Report Agent** | NLP + Responsible AI | Extractive/abstractive summarization, explainability, disclaimers |

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
Agent 2: Evidence Retrieval Agent --- Information Retrieval (ClinVar lookup, TF-IDF, Top-K)
   |  (encrypted message)
   v
Agent 3: Classification Agent ------- LLM (Gemini) + rule-based ACMG-style reasoning
   |  (encrypted message)
   v
Agent 4: Report Agent ---------------- LLM (Gemini) + NLP (summarization) + Responsible AI
   |  (encrypted message)
   v
Final report
```

All agents communicate exclusively through a **standardized AgentMessage protocol** defined in `protocol/message.py`, which includes: `sender`, `receiver`, `message_type`, `trace_id`, `timestamp`, and an encrypted payload. Every interaction is **encrypted in transit** using Fernet symmetric encryption and logged to `MessageLog` for a full audit trail.

## Information Retrieval

The Evidence Agent is a genuine IR system with:

- **Indexing**: TF-IDF text index over all variant descriptions using scikit-learn
- **Candidate Retrieval**: exact variant match, gene lookup, genomic position, rsID, and TF-IDF cosine similarity
- **Relevance Ranking**: weighted scoring (exact match +10, gene match +5, position +5, HIGH impact +2, evidence completeness, TF-IDF similarity)
- **Top-K Selection**: returns the top 5 most relevant evidence records with relevance scores
- **IR Metadata**: every result includes retrieval strategy, candidate count, and ranking method

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

## Setup & Running

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
- Python 3.10+
- A free PostgreSQL connection string (Neon recommended — works everywhere)
- Gemini API key *(optional — the Free plan runs rule-based without it)*

### 1. Install & configure

```bash
pip install -r requirements.txt        # or requirements-api.txt for a lean install
cp .env.example .env                   # add DATABASE_URL + JWT_SECRET_KEY + GEMINI_API_KEY
```

Generate a real signing key with
`python -c "import secrets; print(secrets.token_urlsafe(48))"`.

### 2. Initialise the database

```bash
alembic upgrade head          # plans, users, usage_records, review_signoffs,
                              # password_resets, upgrade_requests
python scripts/seed_plans.py  # Free / Professional / Enterprise
python scripts/create_admin.py
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
python3 -m pytest tests/ -v    # 324 tests — no database needed at all
python3 scripts/smoke_auth.py  # end-to-end check against the real PostgreSQL
```

### 6. Run evaluation

```bash
python3 evaluation.py                    # full suite
python3 evaluation.py --sample-size 50 --no-llm  # quick
```

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
- Presentation materials for Week 6 Mid Evaluation and Week 10 Final Submission

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

## Final submission artifacts

The final submission also includes:

- `COMPLIANCE_REGULATORY_POSITIONING.md` — one-page commercialization/compliance positioning with explicit prototype limitations.
- `data/DATA_SOURCE.md` — dataset provenance, the ClinVar label enrichment and the honest
  evaluation methodology.
- `scripts/` — `enrich_clinvar_labels.py` (ClinVar label join) and
  `build_variant_summary_index.py` (BRCA coverage index used for rsID lookup and
  variants absent from the study workbook).
- `REVIEW_SIGNOFF_WORKFLOW.md` — human review / sign-off workflow.
- `ANALYSIS_GUIDE.md` — how to reproduce the analysis notebook and figures.
- `protocol/PROTOCOL.md` — full agent communication protocol spec (AgentMessage envelope,
  message types, encryption guarantees, a complete worked message flow, audit-trail API).
- `frontend/` — the web app in three files: `index.html` (landing page, workspace, profile, admin dashboard and the auth modal), `app.css`, and `app.js`. The landing page explains the tool and offers example queries; the query surface only appears once you are signed in; administrators get an **Admin** link in the header.
- `POST /api/report/pdf` — server-generated PDF report with evidence summary and reviewer/sign-off fields.

The PDF feature is a **report-generation convenience**, not a claim that the output is a certified clinical document.

## Security Features

- **Input sanitization** (`protocol/security.py`) blocks malformed/malicious queries
- **JWT account authentication** — `POST /api/classify`, `/api/trail/*` and
  `/api/pipeline/*` **require** `Authorization: Bearer <token>` and return **401**
  without one. Accounts, passwords (Argon2id) and plans live in PostgreSQL; see
  [AUTH.md](AUTH.md).
- **Identity re-checked per request** — deactivating or deleting an account
  revokes access immediately, and the `role` used for authorization always comes
  from the database, never from the token
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
- **Prompt-injection defense** — a regex blocklist rejects system-prompt overrides,
  exfiltration, role-hijack and jailbreak phrasing before any NLP or LLM sees the query
- **Symmetric encryption** — all agent messages encrypted in transit using Fernet
- **Full audit logging** with trace IDs for every agent-to-agent message
- **Secrets stay out of version control** — `.env` is gitignored, and there is no
  container image to leak them into. Every deployment sets `DATABASE_URL` and
  `JWT_SECRET_KEY` in its own environment
- **Encrypted in transit** — the database connection requires TLS (`sslmode=require`)
- **Cold-start tolerant** — `pool_pre_ping=True` handles a hosted database that
  scales to zero between requests
- **Restricted CORS** — allowed origins are explicit (`FRONTEND_ORIGINS`, defaulting to localhost)
  rather than `*`

## Responsible AI

- **Confidence calibration**: a genuine conflict (ClinVar's own `Conflicting` consensus when available, otherwise the dataset flag) forces low confidence; the categorical label is always derived from the numeric score; VUS never presented as confident
- **Explainability**: every report lists the evidence + ACMG criteria applied
- **Transparency**: full audit trail with `trace_id` for end-to-end traceability
- **Data protection**: all messages encrypted in transit
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

| Tier | Price | Features |
|---|---|---|
| **Free / Research** | $0 | 10 queries/month, rule-based mode |
| **Professional** | $99/month | 500 queries/month, LLM mode, API access |
| **Enterprise** | Custom | Unlimited queries, on-prem deploy, LIMS integration, SLA |

> These tiers are **illustrative scenarios, not a shipping offer**. The prototype is
> not clinically validated or regulatory-cleared; see
> `COMPLIANCE_REGULATORY_POSITIONING.md` for the validation, hardening and
> regulatory path required before any commercial use.

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
│   ├── evidence_agent.py        # Agent 2: Information Retrieval (TF-IDF, Top-K, rsID)
│   ├── classification_agent.py  # Agent 3: LLM + Rules + ClinVar consensus reconciliation
│   └── report_agent.py          # Agent 4: LLM + Summarization + Responsible AI
├── data/
│   ├── clinvar_variant_dataset.xlsx           # 3,009 variants (tracked)
│   ├── clinvar_variant_dataset_enriched.xlsx  # + real ClinVar labels (generated)
│   ├── variant_summary_brca.tsv               # BRCA coverage / rsID index (generated)
│   └── DATA_SOURCE.md                         # provenance + evaluation methodology
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
│   ├── routes.py                # /api/auth/register, /login, /me
│   ├── schemas.py               # Pydantic auth contracts
│   └── security.py              # Argon2id hashing + JWT mint/decode
├── db/                          # Account/entitlement persistence (agents never import it)
│   ├── database.py              # Engine, SessionLocal, get_db()
│   └── models.py                # Plan, User, UsageRecord
├── services/
│   ├── entitlement_service.py   # Quota windows, feature gates, usage recording
│   ├── analytics_service.py     # Admin dashboard figures (gap-filled daily series)
│   ├── mailer.py                # Pluggable outbound email (log / resend / smtp)
│   └── password_reset.py        # Issue + redeem one-time reset codes
│   ├── password_reset.py        # Issue + redeem one-time reset codes
│   └── upgrade_service.py       # Upgrade request lifecycle
├── migrations/                  # Alembic migrations for all six tables
├── scripts/
│   ├── enrich_clinvar_labels.py          # Join ClinVar labels onto the dataset
│   ├── build_variant_summary_index.py    # Build the BRCA rsID/coverage index
│   ├── seed_plans.py                     # Free / Professional / Enterprise plans
│   ├── create_admin.py                   # Bootstrap admin account
│   └── smoke_auth.py                     # End-to-end auth check against PostgreSQL
├── tests/
│   ├── test_intake_agent.py
│   ├── test_evidence_agent.py
│   ├── test_classification_agent.py
│   ├── test_llm_resilience.py
│   ├── test_security.py
│   ├── test_auth.py
│   ├── test_entitlements.py
│   ├── test_classify_api.py
│   ├── test_admin.py
│   ├── test_review_signoffs.py
│   ├── test_password_reset.py
│   ├── test_upgrades.py
│   ├── test_profile.py
│   ├── test_analytics.py
│   ├── test_cache_isolation.py
│   └── test_frontend.py
├── .env.example                 # Template for API keys
├── .gitignore                   # Generated ClinVar artifacts are gitignored
├── AUTH.md                      # Auth, plan and quota design documentation
├── LICENSE                      # MIT License
├── analysis.ipynb               # Production evaluation & visualization notebook
├── analysis_results/            # Generated visualizations for presentations
├── alembic.ini                  # Alembic config
├── api.py                       # FastAPI REST API
├── COMPLIANCE_REGULATORY_POSITIONING.md
├── evaluation.py                # Evaluation metrics (real-label + proxy modes)
├── main.py                      # Orchestrator
├── PROJECT_REPORT.md
├── pytest.ini                   # Pytest config
└── requirements.txt
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


