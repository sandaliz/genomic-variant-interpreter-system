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

### 1.1 Team and Individual Contributions

| Contributor | Primary subsystem | Supporting work |
|---|---|---|
| **Sanulya** | Evidence Retrieval Agent — Information Retrieval (TF-IDF indexing, relevance ranking, top-K) + ClinVar data + `scripts/` | `tests/test_evidence_agent.py`, dataset provenance (`data/DATA_SOURCE.md`) |
| **Nihindu** | Intake Agent — NLP entity extraction + input sanitization — plus the REST API (`api.py`) and web frontend (`frontend/`) | `tests/test_intake_agent.py`, API/frontend usage documentation |
| **Nipuni** | Report Agent — summarization + Responsible AI — plus the Gemini client (`llm/`) and the agent communication/security layer (`protocol/`) | `tests/test_llm_resilience.py`, `tests/test_security.py`, `protocol/PROTOCOL.md` |
| **Sandali** | Classification Agent — ACMG-style rules + LLM reasoning — plus orchestration/integration (`main.py`) | `tests/test_classification_agent.py`, integration tests, overall README and project documentation |
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
[Account Authentication + Entitlement Check]  auth/, services/entitlement_service.py
   JWT verify -> re-read account from PostgreSQL -> plan gate -> monthly quota
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
| **IR approach** | TF-IDF-based retrieval with cosine similarity (scikit-learn `TfidfVectorizer` + `cosine_similarity`), filtering, and ranked top-K evidence selection. No BM25 implementation is used. |
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

> 📄 **Full protocol specification:** [`protocol/PROTOCOL.md`](protocol/PROTOCOL.md) documents the
> envelope schema, the message-type vocabulary, the security guarantees on the wire, a complete
> worked four-message flow with real JSON, the audit-trail API and the REST transport layer.
> This is the reference to quote during the viva.

**REST API layer (`api.py`):**
- HTTP endpoints: `POST /api/classify`, `POST /api/report/pdf`, `GET /api/trail/{variant_id}`, `GET /api/pipeline/{variant_id}`, `GET /health`
- Frontend talks to the API via HTTP — an additional, external agent-communication layer
- `POST /api/classify` **requires** an API key (supplied as an `X-API-Key` header or the `api_key` body field) and returns `401` without one — the frontend sends the demo key explicitly, so authentication is genuinely enforced rather than silently defaulted
- CORS is restricted to explicit frontend origins (`FRONTEND_ORIGINS`, defaulting to localhost)

**End-to-end field flow.** Two boolean flags are computed inside the agents and carried
all the way to the API response, so they cannot be silently dropped between layers:

| Flag | Computed in | Meaning |
|---|---|---|
| `review_required` | Classification Agent (`reconcile_with_consensus`) | The evidence-based call disagrees with ClinVar's reviewed consensus — a human reviewer must confirm the final classification. Always present; `True` on **both** disagreement branches (defer-to-consensus and call-stands). |
| `coverage_fallback` | Evidence Agent (`retrieve`) | The variant was resolved from the ClinVar coverage index rather than the study workbook, so functional predictors and allele frequencies may be missing. `evidence_limitation` carries the human-readable explanation. |

Both are declared with a `False` default on every code path, are passed through `main.py`
into `api.py`'s response model, and are rendered in the web UI caveat banner. Regression
tests assert they are always real booleans (`tests/test_classification_agent.py`,
`tests/test_evidence_agent.py`).

---

## 6. Security Implementation

| Feature | Where | How |
|---|---|---|
| **Agent-layer authentication** | `protocol/security.py` | API key hashed with SHA-256; `main.py` refuses an untrusted caller before any agent runs (retained as defence-in-depth behind the JWT layer) |
| **Account authentication** | `auth/security.py`, `auth/dependencies.py` | Argon2id password hashing; HS256 access tokens (60 min). Sign-in accepts username **or** email. `POST /api/classify`, `/api/trail/*` and `/api/pipeline/*` require `Authorization: Bearer <token>` and return 401 without one. Identity is re-read from PostgreSQL on every request, so deactivation revokes access immediately and the authorization role always comes from the database |
| **Role-based access** | `auth/dependencies.py`, `api.py` | `require_admin()` guards `/api/admin/users`, `/api/admin/plans`, `PATCH /api/admin/users/{id}`, password reset and `/api/reviews`; a clinician gets 403. Guards prevent an admin changing their own role/active state, and prevent removing the last active administrator |
| **Plan feature gating** | `services/entitlement_service.py`, `api.py` | `plan.llm_enabled` selects the LLM or rules-only system instance at request time; Free runs the full four-agent pipeline with rule-based reasoning |
| **Quota enforcement** | `services/entitlement_service.py` | Per-UTC-month usage rows. Pre-flight `check_quota()` returns 429 before the pipeline runs; `record_usage()` re-checks under a PostgreSQL `SELECT … FOR UPDATE` row lock so concurrent requests cannot overshoot. Deterministic `request_id` keeps client retries from being charged twice |
| **Input sanitization** | Intake Agent | Rejects SQL/script injection, over-length input, empty queries |
| **Prompt-injection defense** | `protocol/security.py` (`is_prompt_injection`) | Regex blocklist for system-prompt overrides ("ignore previous instructions"), exfiltration ("reveal system prompt"), role-hijack and jailbreak phrasing; enforced in `sanitize_query()` before any NLP/LLM sees the query |
| **Encryption in transit** | `protocol/message.py` | All agent payloads encrypted with Fernet symmetric encryption |
| **Audit logging** | `protocol/message.py` | Every message saved to `MessageLog` with trace IDs |
| **503 fallback** | `llm/gemini_client.py` + agents | 503/429 across all fallback models → plain `RuntimeError` → rule-based classification + template report (covered by `TestServiceUnavailableCascade`) |
| **Numpy-safe serialization** | `protocol/message.py` | Custom `NumpyJSONEncoder` handles pandas/numpy types during encryption |
| **Attestation durability** | `db/models.py` (`ReviewSignoff`) | Human sign-offs live in PostgreSQL with an accountable owner (`reviewed_by`); the FK is `ON DELETE SET NULL` so deleting an account cannot erase a clinical attestation |
| **Account recovery** | `services/password_reset.py`, `services/mailer.py` | Forgotten-password flow: 6-digit code delivered through a pluggable mailer. Codes are stored only as salted SHA-256 digests, are single-use, expire in 10 minutes and burn after 5 attempts; the request endpoint answers identically for unknown addresses so it cannot enumerate accounts |
| **Profile integrity** | `auth/routes.py` | Signed-in users may edit `full_name` freely; changing `email` requires the current password because that address is where recovery goes; `username` is immutable because it is the primary identifier baked into issued tokens and the audit trail. Unknown fields are rejected rather than silently ignored |
| **Operational visibility** | `services/analytics_service.py` | Administrator dashboard figures (accounts, query volume, plan mix, upgrade queue, sign-offs) computed live from the same tables the app writes, so a displayed number cannot drift from the data it describes. The daily series is gap-filled so a quiet day reads as a real dip |
| **Subscription lifecycle** | `services/upgrade_service.py` | Upgrade hand-off: a user requests a paid plan, an administrator approves/declines/marks contacted. Approval applies the plan change in the same transaction as the decision; decided states are terminal; the requester keeps their working plan and quota while waiting and is notified by email |
| **Credential isolation** | `.gitignore`, `.env.example` | `.env` is gitignored and `DATABASE_URL` / `JWT_SECRET_KEY` are supplied per developer or per deployment. The project ships no container image and no infrastructure-as-code file, so there is nothing for a credential to be baked into. Database connections require TLS (`sslmode=require`) |

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

### 8.1 Explicit Fairness Limitation — Population / Ancestry Representation

Beyond disclosure, the system surfaces an explicit fairness limitation so that a
population-frequency observation is never presented as equally informative for
every patient. This text is carried in the machine-readable `responsible_ai`
block (`agents/classification_agent.py`), rendered next to the clinical
disclaimer in the web UI (`frontend/index.html`), and reproduced here:

> **Population-data limitation:** Allele-frequency databases (e.g. ESP, ExAC,
> 1000 Genomes/gnomAD) may have uneven representation across ancestry groups.
> Population frequency evidence should therefore be interpreted cautiously and
> should not be treated as equally informative for all populations.

| Why this matters | How the system handles it |
|---|---|
| gnomAD/ExAC/1000G are historically skewed toward European-ancestry cohorts, so "rare" may be an artifact of under-sampling rather than true rarity | The classifier reports an intermediate AF as a **neutral population-frequency observation** and never auto-assigns `BS1` (which requires a validated disease-specific maximum credible AF) |
| A frequency-based benign call could be a false negative for an under-represented ancestry | BA1-equivalent scoring requires AF > 0.001, and the fairness limitation is attached to every result reachable through the API |
| Patients cannot see which caveats applied | The limitation is rendered in the UI footer and included in the PDF report disclaimer block |

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
75 passed
```

- **Intake Agent (13)** — entity extraction, query types, security, rejections, plus prompt-injection override/exfiltration rejection
- **Evidence Agent (11)** — exact lookup, gene-only, variant-only, position, IR metadata, top-K ranking, missingness metadata, encryption, TF-IDF fallback, plus `coverage_fallback` presence/value tests
 - **Classification Agent (22)** — pathogenic/benign/VUS, missing predictors, conflict lowering confidence, encryption, LLM fallback, plus 7 confidence-consistency tests (label↔number invariant, LLM self-reported confidence discarded, conflict caps the number, 2★/3★/0★ consensus behaviour), plus BS1-neutrality (AF 0.0002), CADD/ClinVar separation, and 6 `review_required` tests (always present, boolean on every path, true on both disagreement branches)
- **LLM resilience (8)** — bounded SDK retry, 503 → fallback-model chain → `RuntimeError`, classifier and reporter degradation, direction-aware conflict semantics, plus end-to-end 503 cascade (rules + template fallback, encrypted message path)
- **Security module (21)** — sanitization, auth, encryption, plus prompt-injection override/exfiltration/role-hijack/jailbreak blocking

### 9.4 Analysis & Visualization Notebook

In addition to the production evaluation script, the project includes `analysis.ipynb` — a
**production evaluation and visualisation notebook** that profiles the dataset, evaluates the
deployed multi-agent pipeline against real ClinVar labels, and generates the figures used in the
report, video and viva. It contains **no separate machine-learning model**: it measures the
system that ships.

**Purpose:** Complement the production system with research-grade visualizations for presentations
and assignments.

**Generated Visualizations (`analysis_results/` directory):**
- **Data Exploration**: Gene distribution, variant types, impact levels, allele frequencies, CADD scores
- **Real labels & system evaluation**: ClinVar classification distribution, evidence-based vs
  consensus-reconciled results, directional agreement, predictor completeness, confidence
  distribution, ClinVar review-star distribution, per-class precision/recall/F1
- **Information Retrieval**: Precision@5, Recall@5, Exact-Match@5, MRR@5
- **System**: 4-agent architecture diagram
- **Integration**: The notebook calls `evaluation.py` directly, so its numbers and the CLI
  evaluation cannot drift apart

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

These tiers are **implemented**, not aspirational: they exist as rows in the
`plans` table (`scripts/seed_plans.py` is the source of truth) and are enforced
before every pipeline run. See [AUTH.md](AUTH.md).

| Plan | Price | Features |
|---|---|---|
| **Free / Research** | $0 | 10 queries/month, rule-based mode, basic evidence |
| **Professional** | $99/month | 500 queries/month, Gemini LLM mode, API access, audit trail export |
| **Enterprise** | Custom | Unlimited queries (`monthly_query_limit = NULL`), on-premise deployment, LIMS integration, SLA, dedicated support |

Each account is identified by a JWT, and the pipeline is gated per plan: the Free
tier runs the **full four-agent pipeline** with rule-based reasoning rather than a
reduced workflow. Monthly quotas are counted per UTC calendar month from immutable
usage rows, with a `429` returned before any agent work starts once the limit is
reached.

Moving between tiers is modelled as a **hand-off rather than a checkout**: there is
no payment integration, because pretending to take money would misrepresent the
project's status. A user requests a paid plan, an administrator reviews the queue
and approves, declines or records that they made contact, and the approved plan
change is applied in the same transaction as the decision. Requests are immutable
once decided, and the user keeps their working plan and quota while waiting.

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
│   ├── index.html               # Landing, workspace, profile, admin views + auth modal
│   ├── app.css                  # Styling, built from the same design tokens
│   └── app.js                   # Views, auth flows, renderers, admin dashboard
├── llm/
│   └── gemini_client.py         # Gemini API wrapper
├── protocol/
│   ├── message.py               # Agent communication protocol + NumpyJSONEncoder
│   └── security.py              # Auth, sanitization, encryption (Fernet)
├── auth/                        # JWT account layer in front of the agents
│   ├── dependencies.py          # get_current_user(), require_admin()
│   ├── routes.py                # /api/auth/register, /login, /me
│   ├── schemas.py               # Pydantic auth contracts
│   └── security.py              # Argon2id hashing + JWT mint/decode
├── db/                          # Account/entitlement persistence
│   ├── database.py              # Engine, SessionLocal, get_db()
│   └── models.py                # Plan, User, UsageRecord
├── services/
│   ├── entitlement_service.py   # Quota windows, feature gates, usage recording
│   ├── analytics_service.py     # Admin dashboard figures (gap-filled daily series)
│   ├── mailer.py                # Pluggable outbound email (log / resend / smtp)
│   ├── password_reset.py        # Issue + redeem one-time reset codes
│   └── upgrade_service.py       # Upgrade request lifecycle
├── migrations/                  # Alembic migrations (plans/users/usage, review_signoffs)
├── tests/
│   ├── test_intake_agent.py     # 13 tests
│   ├── test_evidence_agent.py   # 11 tests
│   ├── test_classification_agent.py  # 22 tests
│   ├── test_llm_resilience.py   # 8 tests
│   ├── test_security.py         # 21 tests
│   ├── test_auth.py             # 27 tests — registration, login, JWT
│   ├── test_entitlements.py     # 19 tests — quotas, month windows, row locking
│   ├── test_classify_api.py     # 16 tests — 401/429, plan gate, usage recording
│   ├── test_admin.py            # 31 tests — roles, privilege guards, password reset
│   ├── test_review_signoffs.py  # 17 tests — attestation persistence and durability
│   ├── test_analytics.py       # 19 tests — dashboard figures, gap-filled daily series
│   ├── test_cache_isolation.py # 6 tests — per-account result caching
│   └── test_frontend.py        # 37 tests — view visibility, JS/DOM wiring, assets
├── analysis_results/            # Generated visualizations for presentations
│   └── README.md               # Guide to using analysis visualizations
├── .env.example                 # API key + DATABASE_URL + JWT_SECRET_KEY template
├── .gitignore
├── alembic.ini                  # Alembic config
├── AUTH.md                      # Auth, plan and quota design documentation
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
cp .env.example .env   # add DATABASE_URL (a free Neon PostgreSQL, no Docker)
                      # + GEMINI_API_KEY (optional — rule-based fallback works)
                      # + a generated JWT_SECRET_KEY

# Database (account layer) — the same three commands on macOS, Windows and Linux
alembic upgrade head
python scripts/seed_plans.py    # Free / Professional / Enterprise
python scripts/create_admin.py  # bootstrap admin (password printed once)

# CLI (no account needed — talks to the agents directly)
python3 main.py                               # default demo query
python3 main.py --query "What variants are in BRCA1?"
python3 main.py --no-llm                       # rule-based only (no Gemini API calls)

# Web UI (register an account, sign in)
uvicorn api:app --reload --port 8000
# open http://localhost:8000

# Tests
python3 -m pytest tests/ -v    # 324 tests (no database required)
python3 scripts/smoke_auth.py  # end-to-end auth check against real PostgreSQL

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
5. ✅ **Information Retrieval**: TF-IDF-based retrieval with cosine similarity (scikit-learn `TfidfVectorizer` + `cosine_similarity`) — filtering, relevance ranking, and ranked top-K evidence selection with explainable scores. (No BM25 implementation is used.)
6. ✅ **Security**: two-layer authentication (PostgreSQL accounts with Argon2id + JWT at the API edge, SHA-256 agent-layer API keys as defence-in-depth), role-based access control, plan feature gating, monthly quota enforcement with row-locked commit-time re-checks, input sanitization + explicit prompt-injection defense, Fernet encryption in transit, full audit logging, 503→rules/template fallback
7. ✅ **Agent Communication Protocol**: Formal `AgentMessage` schema (message_id, trace_id, sender, receiver, message_type, timestamp) + REST API layer
8. ✅ **Responsible AI**: Confidence derived from evidence (not arbitrary), never overstated, VUS handled honestly, conflict-adjusted, full transparency, clinical disclaimers
9. ✅ **Commercialization**: pricing model (Free/Professional/Enterprise) **implemented** as PostgreSQL-backed plans with per-plan feature gating and monthly quotas, plus target users and deployment ideas
10. ✅ **Evaluation**: honest evaluation against **real ClinVar labels** (4.8% exact / 42.6% directional agreement from the agent's own ACMG reasoning, rising to 38.1% / 98.3% after ClinVar-consensus reconciliation), confusion matrix, precision/recall/F1, Precision@K/Recall@K, ablation comparison, **215 passing unit tests** (75 agent/protocol + 249 auth/entitlement/role/frontend/review)
