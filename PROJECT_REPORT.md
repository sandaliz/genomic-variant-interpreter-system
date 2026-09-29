# Genomic Variant Interpretation — Multi-Agent AI System

## IT3041 — Information Retrieval and Web Analytics
### Assignment: Design and Implementation of an Agentic AI System

---

## 1. Project Overview

| Field | Detail |
|---|---|
| **Domain** | Biotechnology (Genetic Variant Classification) |
| **Project Name** | Genomic Variant Interpretation Agent |
| **Core Problem** | Genetic testing labs identify thousands of BRCA1/BRCA2 variants, but experts frequently disagree on how to classify whether a variant causes cancer (Pathogenic), is harmless (Benign), or is of unknown significance (VUS) |
| **Solution** | A 4-agent AI decision-support system that retrieves evidence for a variant and produces a transparent, confidence-rated classification |
| **Target Users** | Genetic counselors, clinical genetics labs, cancer screening programs |

---

## 2. Problem Statement

When a patient undergoes genetic testing for hereditary breast/ovarian cancer (BRCA1/BRCA2 genes), the laboratory detects variants — changes in the DNA sequence. The critical question for the clinician is: **"Does this variant increase cancer risk?"**

The challenge: variants don't come with a simple "yes" or "no" answer. Classification requires weighing multiple lines of evidence:

- **Population frequency** — Is the variant common in the general population? (Common = likely benign)
- **Protein impact** — Does the change disrupt protein function? (SIFT, PolyPhen, CADD scores)
- **Variant consequence** — What kind of change is it? (missense, frameshift, stop-gained)
- **Prior classifications** — Have other experts classified this variant? Did they agree?

Our dataset (3,009 ClinVar variants) includes a `Conflicting_Classification_Flag` — direct evidence that real experts disagree on classification. This is a genuine, real-world problem in clinical genomics.

**Our system is a decision-support assistant:**
- It retrieves all available evidence for a variant
- It applies ACMG-style clinical reasoning rules
- It uses an LLM (Gemini) to reason over the evidence and write plain-language reports
- It is **transparent** — every classification comes with evidence + criteria applied + confidence level
- It **never overstates confidence** — if experts disagreed or evidence is weak, it says so

---

## 3. System Architecture

```
User query (natural language)
   |
   v
[Authentication Check - Security]  protocol/security.py
   |
   v
Agent 1: Intake Agent (agents/intake_agent.py)
   Role:     Parse user query, extract entities
   NLP:      Named Entity Recognition (rule-based)
   Security: Input sanitization + encryption
   Output:   Encrypted AgentMessage with gene/variant
   |
   v  (encrypted message via AgentMessage protocol)
Agent 2: Evidence Retrieval Agent (agents/evidence_agent.py)
   Role:   Retrieve evidence from ClinVar dataset
   IR:     Information Retrieval (TF-IDF, exact/position/gene search, ranking, top-K)
   Output: Encrypted evidence bundle (SIFT, CADD, AF, etc.)
   |
   v  (encrypted message via AgentMessage protocol)
Agent 3: Classification Agent (agents/classification_agent.py)
   Role:   Classify as Pathogenic/Benign/VUS
   LLM:    Gemini LLM for ACMG-style reasoning
   Rules:  Deterministic evidence scoring (transparent)
   Output: Encrypted result with confidence level
   |
   v  (encrypted message via AgentMessage protocol)
Agent 4: Report Agent (agents/report_agent.py)
   Role:   Write plain-language clinical report
   NLP:    Summarization (extractive + abstractive)
   Responsible AI: Disclaimers + honest uncertainty
   Output: Encrypted final report + evidence summary
   |
   v
Final Report (Confidence, Evidence, Disclaimers)
```

---

## 4. Agent Details

### 4.1 Agent 1 — Intake Agent (NLP + Security)

| Aspect | Implementation |
|---|---|
| **File** | `agents/intake_agent.py` |
| **NLP technique** | Rule-based Named Entity Recognition (NER) + intent classification |
| **Responsibilities** | Extract gene name, HGVS notation, position, or rsID from free text; classify query intent; sanitize input |
| **Output** | Encrypted `AgentMessage` with structured payload |

**Supported Query Types (intents):**

| Query | Intent | Example |
|---|---|---|
| Gene + Variant | classification | `"Is BRCA1 NC_000017.10:g.41197778A>G pathogenic?"` |
| Gene only | gene_search | `"What variants are in BRCA1?"` |
| Variant only | variant_search | `"What is NC_000017.10:g.41201179C>T?"` |
| By position | position_search | `"What variant is at position 41197778 in BRCA1?"` |
| rsID | rsid_search | `"Tell me about rs123456"` |

**Entity extraction:**
- Gene aliases: `BRCA1`, `BRCA 1`, `BRCA2`, `BRCA 2`
- HGVS notation: `c.68_69delAG`, `NC_000017.10:g.41197778A>G`
- Position: `position 41197778`
- rsID: `rs123456`

**Security:**
- Input sanitization (rejects SQL injection, XSS, overly long input, empty queries)
- Unsupported gene detection (e.g. TP53 rejected with helpful message)
- Helpful error messages that guide the user to valid query formats

### 4.2 Agent 2 — Evidence Retrieval Agent (Information Retrieval)

| Aspect | Implementation |
|---|---|
| **File** | `agents/evidence_agent.py` |
| **IR approach** | TF-IDF/BM25-based retrieval, filtering, and ranked top-K evidence selection |
| **Dataset** | `data/clinvar_variant_dataset.xlsx` (3,009 BRCA1/BRCA2 ClinVar variants) |
| **Evidence fields** | SIFT prediction, PolyPhen, CADD score, IMPACT, frequency data, disease name, conflict flag |

**IR pipeline:**

```
User Query → Query normalization (via Intake) → Evidence Retrieval Agent
  ├── Exact variant search (by gene + HGVS)
  ├── Gene/position/rsID filtering
  ├── TF-IDF cosine similarity retrieval (scikit-learn)
  ├── Relevance ranking (weighted: exact match +10, gene +5, position +5, HIGH impact +2, conflict penalty -2, evidence completeness +1 each, TF-IDF ×5, review data +1)
  └── Top-K selection (default K=5) with normalized relevance scores (0-1) and explanations
```

**What it retrieves for a variant:**
- Variant consequence (missense, frameshift, etc.)
- Predicted impact (HIGH/MODERATE/LOW)
- SIFT prediction (deleterious/tolerated)
- PolyPhen prediction (probably_damaging/benign)
- CADD score
- Population allele frequencies (ESP/ExAC/1000G)
- Prior ClinVar classifications + conflict flags
- Relevance score and match type explanation for every result

**Explainable retrieval results:**
Every top-K result includes a human-readable match type ("Exact variant match", "Same genomic position", "Same gene", "TF-IDF text similarity") and an explanation string describing why the record was retrieved.

**IR metadata:** every response includes retrieval strategy, candidate count, ranking method, and scoring component breakdown.

**Missing evidence handling:** Annotation coverage varies by variant type. The
system preserves unavailable values as missing and includes a
`predictor_available` map for SIFT, PolyPhen, CADD, and overall predictor
completeness. SIFT/PolyPhen evidence is used only when both predictions are
present; CADD evidence is used only for finite scores. Missing predictors do
not contribute evidence and reduce the confidence score. Reports render these
values as `not available` rather than `NaN` or zero.

### 4.3 Agent 3 — Classification Agent (LLM + Rules)

| Aspect | Implementation |
|---|---|
| **File** | `agents/classification_agent.py` |
| **LLM** | Google Gemini (primary) with graceful fallback |
| **Rule-based mode** | Transparent ACMG-style scoring |

**Two modes:**
1. **Rule-based (deterministic)** — always available, auditable
2. **LLM-based (Gemini)** — sends evidence to Gemini and asks for ACMG-style reasoning

**Rule engine:**

| Evidence | Score |
|---|---|
| Allele frequency > 0.1% (BA1 project proxy) | -2 |
| Absent in ESP/ExAC/1000G, or max AF ≤ 0.01% (PM2) | +1 |
| Intermediate AF (e.g. 0.02% / 0.0002) | 0 — neutral observation, **BS1 unestablished** |
| HIGH impact (PVS-1 equivalent) | +2 |
| Deleterious + damaging predictors | +1 |
| Tolerated + benign predictors | -1 |
| CADD >= 20 (supporting computational signal; **not** an ACMG code) | +1 |

BS1 ("allele frequency greater than expected for the disorder") is
**unestablished** in this prototype: per ACMG/AMP + ClinGen it requires a
validated disease-specific maximum credible AF (prevalence + penetrance +
allelic heterogeneity), which is not configured. An AF such as 0.0002 is
therefore reported as a neutral population-frequency observation
(`population_observation`), never as `BS1_equivalent`. Only AF > 0.001 (BA1
project proxy) scores as benign frequency evidence.

Output layers are strictly separated: `acmg_criteria_applied` holds validated
codes only (`criteria_applied` kept as alias); CADD/SIFT/PolyPhen live in
`computational_annotations`; submitter agreement lives in `clinvar_context`.
VUS reasoning states "insufficient validated ACMG/AMP evidence" rather than
summing discordant flags, and reports distinguish the automated classification
(`VUS / unresolved`) from clinical status (`Not established; requires
certified clinical review`).

**Confidence mechanism (derived from evidence, not arbitrarily set):**

| Component | Weight |
|---|---|
| Evidence strength (|rule_score|) | 0–40 points |
| ClinVar submitter consistency (conflict flag) | +10 or -20 |
| Number of supporting records (top-K count) | 0–20 points |
| Predictor agreement (SIFT + PolyPhen) | 5–20 points |

Numeric confidence (0–100) → categorical: ≥60 = high, ≥35 = moderate, else low.
The categorical label is **always derived from the numeric score** (`_confidence_level()`) —
the LLM's self-reported `confidence` string is discarded, so the two can never disagree
(e.g. show "high" next to 28/100).
A genuine conflict — ClinVar's own `Conflicting` consensus when available,
otherwise the dataset's `Conflicting_Classification_Flag` — always forces "low"
on both the label and the number. VUS is never presented as high confidence.

**Responsible AI in classification:**
- A genuine conflict (`ClinicalSignificance` = `Conflicting`, or the dataset flag
  when no real label is attached) **always degrades confidence to low** (label *and* number)
- VUS is never presented with false-confidence messages
- LLM prompts include instructions to never overstate confidence — and the LLM's own
  confidence value is ignored in favour of the evidence-derived score
- When the call is deferred to a ClinVar consensus, confidence follows the review status
  (3★+ → high, 2★ → moderate) and the weaker evidence-based confidence is preserved in
  the report's provenance block rather than overwritten
- Confidence level ≠ medical certainty (explicitly stated in every report)

### 4.4 Agent 4 — Report Agent (NLP + Responsible AI)

| Aspect | Implementation |
|---|---|
| **File** | `agents/report_agent.py` |
| **NLP** | Extractive summarization (rule-based) + abstractive (LLM) |
| **Output** | Plain-language clinical report + evidence summary |

**Two report modes:**
1. **Template report** (no LLM) — formatted, bulleted clinical report with evidence, ACMG criteria, confidence breakdown, and disclaimers
2. **LLM report** (Gemini) — natural-language counseling report with:
   - Classification and confidence level (numeric + categorical)
   - Key evidence considered
   - Top retrieved evidence records with relevance scores
   - Caveats / limitations
   - Recommendations for next steps
   - Clinical disclaimer

**Responsible AI in report:**
- If confidence is low → suggests expert genetic counselor review
- If VUS → explains meaning without false confidence
- Always includes disclaimer: "Decision-support tool, not a clinical diagnosis"
- Explicitly states: "Classification confidence ≠ medical certainty"

---

## 5. Agent Communication Protocol

All agents communicate exclusively through a structured `AgentMessage` class defined in `protocol/message.py`, following a formal protocol schema (similar to MCP / A2A standards):

```json
{
  "message_id": "...",
  "trace_id": "...",
  "sender": "intake_agent",
  "receiver": "evidence_retrieval_agent",
  "message_type": "VARIANT_QUERY",
  "timestamp": "2026-08-18T10:39:40.813146+00:00",
  "variant_id": "BRCA1:NC_000017.10:g.41197778A>G",
  "stage": "intake_complete",
  "encrypted": true,
  "payload": { "__encrypted__": "..." }
}
```

**Communication flow:**

```
Intake Agent → AgentMessage(VARIANT_QUERY) → Evidence Agent
Evidence Agent → AgentMessage(EVIDENCE_RESULT) → Classification Agent
Classification Agent → AgentMessage(CLASSIFICATION_RESULT) → Report Agent
Report Agent → AgentMessage(REPORT_RESULT) → Orchestrator
```

**Why this satisfies the requirement:**
- Agents never access each other's internal state directly — all data flows through `AgentMessage`
- Formal protocol fields: `message_id`, `trace_id`, `sender`, `receiver`, `message_type`, `timestamp`, `stage`, `encrypted`
- Every message payload is **encrypted** with Fernet symmetric encryption
- Every message is **logged** to `MessageLog` for a full audit trail

**REST API layer (`api.py`):**
- HTTP endpoints: `POST /api/classify`, `GET /api/trail/{variant_id}`, `GET /health`
- Frontend talks to the API via HTTP — an additional, external agent-communication layer
- API key authentication required for pipeline retrieval and audit trail endpoints

---

## 6. Security Implementation

| Feature | Where | How |
|---|---|---|
| **Authentication** | `protocol/security.py` | API key hashed with SHA-256; demo key prefilled |
| **Input sanitization** | Intake Agent | Rejects SQL/script injection, over-length input, empty queries |
| **Prompt-injection defense** | `protocol/security.py` (`is_prompt_injection`) | Regex blocklist for system-prompt overrides ("ignore previous instructions"), exfiltration ("reveal system prompt"), role-hijack and jailbreak phrasing; enforced in `sanitize_query()` before any NLP/LLM sees the query |
| **Encryption in transit** | `protocol/message.py` | All agent payloads encrypted with Fernet symmetric encryption |
| **Audit logging** | `protocol/message.py` | Every message saved to `MessageLog` with trace IDs |
| **503 fallback** | `llm/gemini_client.py` + agents | 503/429 across all fallback models → plain `RuntimeError` → rule-based classification + template report (covered by `TestServiceUnavailableCascade`) |
| **Numpy-safe serialization** | `protocol/message.py` | Custom `NumpyJSONEncoder` handles pandas/numpy types during encryption |

---

## 7. LLM Integration (Google Gemini)

| Component | How Gemini is used |
|---|---|
| **Classification Agent** | Reason over evidence and apply ACMG rules in natural language, return JSON result |
| **Report Agent** | Generate plain-language clinical report + abstractive summary |
| **Report Agent** | Extractive summarization of evidence (fallback when LLM unavailable) |

**Gemini client (`llm/gemini_client.py`):**
- Lazy-loaded for testability without API key
- Fallback model chain if primary model overloaded
- Graceful fallback to rule-based when no API key

---

## 8. Responsible AI Implementation

| Principle | How It's Met |
|---|---|
| **Fairness** | Disclosed that population allele frequency databases underrepresent some ancestries |
| **Transparency** | Both rule-based and LLM modes are auditable; full message trail with trace IDs |
| **Explainability** | Every report lists evidence + ACMG criteria applied + confidence components |
| **Accountability** | Full audit trail; every agent's decision can be traced via `trace_id` |
| **Privacy / data protection** | All messages encrypted in transit with Fernet |
| **Confidence never overstated** | LLM prompts instruct to be conservative and the LLM's own confidence value is discarded; a genuine conflict forces low confidence; the categorical label is always derived from the numeric score; a consensus-backed final call is floored by ClinVar review stars with the evidence-only figure disclosed |
| **VUS handled honestly** | VUS results are never presented as confident answers; downgraded if LLM says otherwise |
| **Always a disclaimer** | Every report includes the decision-support disclaimer; confidence ≠ medical certainty explicitly stated |

**Responsible AI pipeline (explicit, visible in code and UI):**

```
Evidence disagreement (ClinVar `Conflicting` consensus, else dataset flag)
      ↓
Confidence adjustment (conflict → low; VUS → not high; consensus-backed final
call → floored by review stars, evidence-only figure disclosed)
      ↓
Classification (LLM + rule-based reasoning)
      ↓
Explainability (evidence + ACMG criteria + retrieval rationale + confidence breakdown)
      ↓
Clinical disclaimer (decision-support only, not diagnosis)
```

---

## 9. Evaluation

### 9.1 Methodology (`evaluation.py`)

- Samples N variants (configurable via `--sample-size`; full run uses 3,009)
- Uses **real ClinVar labels** when the enriched workbook is present
  (`--ground-truth real|proxy|auto`), so accuracy is measured, not assumed
- Runs the rule-based classifier on each variant
- Computes classification metrics: accuracy, precision, recall, F1-score (macro + per-class)
- Reports **two** accuracy figures: the agent's own evidence-based call, and the
  consensus-reconciled output a user actually sees
- Computes **directional** agreement (pathogenic/benign/uncertain) restricted to rows
  whose ClinVar truth is a single determinate class
- Computes confusion matrix (both modes)
- Reports accuracy separately for complete and incomplete predictor records
- Computes IR metrics: Precision@K, Recall@K and Exact-match@K
- Runs ablation comparison: rules-only vs Gemini-only vs combined

### 9.2 Results (verified by running the code)

```
> Run:  python evaluation.py --dataset data/clinvar_variant_dataset_enriched.xlsx \
                            --ground-truth real --sample-size 3009 --no-ir --no-ablation --no-llm

CLASSIFICATION METRICS — against REAL ClinVar labels (3,009 variants)
  [1] Evidence-based call only (the agent's own ACMG reasoning):
      Correct: 144/3009   ->  4.8%
  [2] After ClinVar consensus reconciliation (what a user sees):
      Correct: 1145/3009  ->  38.1%

DIRECTIONAL AGREEMENT  (pathogenic / benign / uncertain)
  Determinate rows:                1168   (the other 1841 are ClinVar
                                           "Conflicting" — a statement about
                                           submitters, not a predicable class)
  Evidence-based call:             497/1168  (42.6%)
  After consensus reconciliation:  1148/1168  (98.3%)

SAFETY-RELEVANT FALSE POSITIVES
  Benign-truth called pathogenic:  53/497 (10.7%)  ->  1/497 (0.2%) reconciled
```

The evidence-based figures measure the agent's own reasoning; the reconciled figures
include deferring to a reviewed ClinVar consensus (≥2-star, disclosed in the report).
The older `--ground-truth proxy` mode reproduces the previous "89% agreement" number,
but those labels are derived from the same evidence rules the classifier applies, so
they measure self-consistency rather than accuracy (see `data/DATA_SOURCE.md`).

Retrieval metrics are independent of the label mode and remain available:

```
INFORMATION RETRIEVAL METRICS (30-variant sample)
  Precision@5:   0.200
  Recall@5:      1.000
  Exact-match@5: 1.000

ABLATION COMPARISON (30-variant sample, proxy labels)
  Rules only:       90.0%
  Gemini only:       N/A (no API key)
  Rules + Gemini:    90.0%
```

> **Note:** Gemini-only ablation requires a valid Gemini API key. Without a key, the system falls back to rule-based mode (marked N/A).

> **Missingness note:** The evaluation labels a record `complete` only when
> SIFT, PolyPhen, and CADD are all available and finite, and reports separate
> complete/incomplete metrics so overall accuracy cannot conceal annotation
> coverage. Ground truth is now the **real ClinVar clinical significance**
> carried by the enriched workbook; the evidence-derived proxy labels remain
> available via `--ground-truth proxy`, but they re-use the classifier's own
> rules and therefore measure self-consistency, not correctness.

### 9.3 Unit Tests

```
67 passed
```

- **Intake Agent (13)** — entity extraction, query types, security, rejections, plus prompt-injection override/exfiltration rejection
- **Evidence Agent (9)** — exact lookup, gene-only, variant-only, position, IR metadata, top-K ranking, missingness metadata, encryption, TF-IDF fallback
- **Classification Agent (16)** — pathogenic/benign/VUS, missing predictors, conflict lowering confidence, encryption, LLM fallback, plus 7 confidence-consistency tests (label↔number invariant, LLM self-reported confidence discarded, conflict caps the number, 2★/3★/0★ consensus behaviour), plus BS1-neutrality (AF 0.0002) and CADD/ClinVar separation tests
- **LLM resilience (8)** — bounded SDK retry, 503 → fallback-model chain → `RuntimeError`, classifier and reporter degradation, direction-aware conflict semantics, plus end-to-end 503 cascade (rules + template fallback, encrypted message path)
- **Security module (21)** — sanitization, auth, encryption, plus prompt-injection override/exfiltration/role-hijack/jailbreak blocking

### 9.4 Analysis & Visualization Notebook

In addition to the production evaluation script, the project includes `analysis.ipynb` — a Jupyter notebook for data exploration, model performance visualization, and presentation materials.

**Purpose:** Complement the production system with research-grade visualizations for presentations and assignments.

**Generated Visualizations (`analysis_results/` directory):**
- **Data Exploration**: Gene distribution, variant types, impact levels, allele frequencies, CADD scores
- **Model Performance**: Confusion matrix, ROC curves, feature importance, per-class metrics
- **System Performance**: IR metrics (Precision@K, Recall@K), system architecture diagram
- **Integration**: Uses production evaluation script results for consistency

**Usage for Assignments:**
- **Week 6 Mid Evaluation**: Visual demonstrations of data understanding, problem relevance, and system architecture
- **Week 10 Final Submission**: Materials for Gen AI video creation and technical report figures
- **Viva Defense**: Visual aids to explain system components and performance

**Note:** The production system (`main.py`, `api.py`) is designed for deployment with text-based metrics, while the analysis notebook is designed for research and presentation. Both use the same underlying algorithms and evaluation methodology.

---

## 10. Commercialization Plan

### Target Users / Market

| Segment | Description |
|---|---|
| **Primary** | Genetic counselors and clinical genetics labs |
| **Secondary** | Hereditary cancer screening programs, research institutions |
| **Tertiary** | Direct-to-consumer genetic testing companies (as a value-add) |

### Pricing Model

| Plan | Price | Features |
|---|---|---|
| **Free / Research** | $0 | 10 queries/month, rule-based mode, basic evidence |
| **Professional** | $99/month | 500 queries/month, Gemini LLM mode, API access, audit trail export |
| **Enterprise** | Custom | Unlimited queries, on-premise deployment, LIMS integration, SLA, dedicated support |

### Deployment Ideas

| Option | Description |
|---|---|
| **Cloud-based API** | FastAPI on AWS/GCP/Azure with auto-scaling |
| **On-premise** | Docker deployment for hospitals/labs with strict data privacy |
| **LIMS integration** | REST API into existing lab information management workflows |
| **Batch processing** | Population-scale variant analysis (e.g. research cohorts) |

---

## 11. Project Structure

```
variant_agent_system/
├── agents/
│   ├── intake_agent.py          # Agent 1: NLP (NER + intent) + Security
│   ├── evidence_agent.py        # Agent 2: Information Retrieval (TF-IDF, Top-K)
│   ├── classification_agent.py  # Agent 3: LLM + Rules
│   └── report_agent.py          # Agent 4: LLM + Summarization + Responsible AI
├── data/
│   ├── clinvar_variant_dataset.xlsx   # 3,009 variants
│   └── DATA_SOURCE.md
├── frontend/
│   └── index.html               # Modern web UI with pipeline visualization
├── llm/
│   └── gemini_client.py         # Gemini API wrapper
├── protocol/
│   ├── message.py               # Agent communication protocol + NumpyJSONEncoder
│   └── security.py              # Auth, sanitization, encryption (Fernet)
├── tests/
│   ├── test_intake_agent.py     # 10 tests
│   ├── test_evidence_agent.py   # 9 tests
│   ├── test_classification_agent.py  # 7 tests
│   └── test_security.py         # 17 tests
├── analysis_results/            # Generated visualizations for presentations
│   └── README.md               # Guide to using analysis visualizations
├── .env.example                 # API key template
├── .gitignore
├── LICENSE                      # MIT License
├── analysis.ipynb               # Data analysis & visualization notebook
├── api.py                       # FastAPI REST API
├── evaluation.py                # Evaluation metrics (classification + IR + ablation)
├── main.py                      # Orchestrator (4-agent pipeline)
├── pytest.ini                   # Pytest config
└── requirements.txt
```

---

## 12. How to Run

```bash
cd variant_agent_system
pip install -r requirements.txt
cp .env.example .env   # add GEMINI_API_KEY (optional — rule-based fallback works)

# CLI
python3 main.py                               # default demo query
python3 main.py --query "What variants are in BRCA1?"
python3 main.py --no-llm                       # rule-based only (no Gemini API calls)

# Web UI
uvicorn api:app --reload --port 8000
# open http://localhost:8000

# Tests
python3 -m pytest tests/ -v    # 67 tests

# Evaluation
python3 evaluation.py                              # full suite (100 variants)
python3 evaluation.py --sample-size 50 --no-llm    # quick (no LLM costs)

# Analysis & Visualization (for presentations)
jupyter notebook analysis.ipynb
# Run all cells to generate visualizations in analysis_results/ directory
```

---

## 13. Conclusion

This project fulfills all assignment requirements:

1. ✅ **Real-world problem** in Biotechnology (BRCA1/BRCA2 variant classification)
2. ✅ **4 interacting intelligent agents** (Intake, Evidence, Classification, Report)
3. ✅ **LLM**: Google Gemini (classification reasoning + report generation)
4. ✅ **NLP**: Named Entity Recognition (gene, HGVS, position, rsID) + intent classification + extractive/abstractive summarization
5. ✅ **Information Retrieval**: TF-IDF/BM25-based retrieval, filtering, relevance ranking, and ranked top-K evidence selection with explainable scores
6. ✅ **Security**: authentication (SHA-256 hashed API keys), input sanitization + explicit prompt-injection defense, Fernet encryption in transit, full audit logging, 503→rules/template fallback
7. ✅ **Agent Communication Protocol**: Formal `AgentMessage` schema (message_id, trace_id, sender, receiver, message_type, timestamp) + REST API layer
8. ✅ **Responsible AI**: Confidence derived from evidence (not arbitrary), never overstated, VUS handled honestly, conflict-adjusted, full transparency, clinical disclaimers
9. ✅ **Commercialization**: Pricing model (Free/Professional/Enterprise), target users, deployment ideas
10. ✅ **Evaluation**: honest evaluation against **real ClinVar labels** (4.8% exact / 42.6% directional agreement from the agent's own ACMG reasoning, rising to 38.1% / 98.3% after ClinVar-consensus reconciliation), confusion matrix, precision/recall/F1, Precision@K/Recall@K, ablation comparison, **67 passing unit tests**
