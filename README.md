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

### Prerequisites
- Python 3.10+
- Gemini API key (optional for demos — rule-based fallback works without it)

### 1. Install & configure

```bash
pip install -r requirements.txt
cp .env.example .env   # add GEMINI_API_KEY
```

### 2. Run the CLI

```bash
python3 main.py                                    # default demo query
python3 main.py --query "What variants are in BRCA1?"
python3 main.py --no-llm                            # rule-based only
```

### 3. Run the Web UI

```bash
uvicorn api:app --reload --port 8000
# open http://localhost:8000
```

### 4. Run tests

```bash
python3 -m pytest tests/ -v    # 67 tests
```

### 5. Run evaluation

```bash
python3 evaluation.py                    # full suite
python3 evaluation.py --sample-size 50 --no-llm  # quick
```

### 6. Run analysis notebook (for visualizations)

```bash
jupyter notebook analysis.ipynb
```

The analysis notebook provides:
- Data exploration graphs and statistical analysis
- Model performance visualizations (confusion matrix, ROC curves, feature importance)
- Information retrieval performance metrics
- System architecture diagrams
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
- `frontend/index.html` — responsive clinical-style dashboard with retrieval evidence, ACMG-style codes, responsible-AI controls, audit timeline and PDF actions.
- `POST /api/report/pdf` — server-generated PDF report with evidence summary and reviewer/sign-off fields.

The PDF feature is a **report-generation convenience**, not a claim that the output is a certified clinical document.

## Security Features

- **Input sanitization** (`protocol/security.py`) blocks malformed/malicious queries
- **API key authentication** before any agent runs
- **Symmetric encryption** — all agent messages encrypted in transit using Fernet
- **Full audit logging** with trace IDs for every agent-to-agent message

## Responsible AI

- **Confidence calibration**: a genuine conflict (ClinVar's own `Conflicting` consensus when available, otherwise the dataset flag) forces low confidence; the categorical label is always derived from the numeric score; VUS never presented as confident
- **Explainability**: every report lists the evidence + ACMG criteria applied
- **Transparency**: full audit trail with `trace_id` for end-to-end traceability
- **Data protection**: all messages encrypted in transit
- **Fairness**: disclosed that population allele frequency databases underrepresent some ancestries
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
│   └── index.html               # Responsive clinical-style web UI + print/PDF workflow
├── llm/
│   └── gemini_client.py         # Gemini API wrapper (bounded retries, graceful fallback)
├── protocol/
│   ├── message.py               # Agent communication protocol + encryption
│   └── security.py              # Auth, sanitization, encryption
├── scripts/
│   ├── enrich_clinvar_labels.py          # Join ClinVar labels onto the dataset
│   └── build_variant_summary_index.py    # Build the BRCA rsID/coverage index
├── tests/
│   ├── test_intake_agent.py
│   ├── test_evidence_agent.py
│   ├── test_classification_agent.py
│   └── test_security.py
├── .env.example                 # Template for API keys
├── .gitignore                   # Generated ClinVar artifacts are gitignored
├── LICENSE                      # MIT License
├── analysis.ipynb               # Data analysis & visualization notebook
├── analysis_results/            # Generated visualizations for presentations
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

| Member | Contribution |
|---|---|
| Member 1 | Intake Agent (NLP + input sanitization), Security module, Intake Agent tests |
| Member 2 | Evidence Retrieval Agent (TF-IDF indexing, ranking, top-K), Evidence Agent tests |
| Member 3 | Report Agent (summarization + responsible AI), Frontend UI, REST API layer |
| Member 4 | Classification Agent (rule-based + LLM reasoning), Gemini client, Classification Agent tests |

