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
| **Solution** | A multi-agent AI decision-support system (four specialists plus a Supervisor/Verifier control plane) that retrieves evidence for a variant and produces a transparent, confidence-rated classification |
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
Supervisor Agent (agents/orchestrator.py)
   Role:   Plan the run from the intake result - retrieval mode + verification budget
   Agency: The path is chosen per query, not fixed
   Output: Encrypted PLAN message
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
Agent 4: Verifier Agent (agents/orchestrator.py)
   Role:   Critique the classification against its evidence
   Agency: Can send the call back ONCE for a conservative re-statement
   Output: Encrypted VERIFICATION_RESULT message
   |
   |  (if revise -> Agent 3 re-states with lower confidence -> Agent 4 re-checks)
   v  (encrypted message via AgentMessage protocol)
Agent 5: Report Agent (agents/report_agent.py)
   Role:   Write plain-language clinical report
   NLP:    Summarization (structured + abstractive)
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
| **IR approach** | A ladder of increasingly weak evidence (exact variant → ClinVar coverage index → same position → same gene → lexical only), with the lexical rung ranked by one of three selectable models: TF-IDF cosine similarity, hand-rolled Okapi BM25, or a Reciprocal Rank Fusion hybrid of the two. Default is TF-IDF, so live behaviour is unchanged from the pre-BM25 implementation. |
| **Dataset** | `data/clinvar_variant_dataset.xlsx` (3,009 BRCA1/BRCA2 ClinVar variants) |
| **Evidence fields** | SIFT prediction, PolyPhen, CADD score, IMPACT, frequency data, disease name, conflict flag |

**IR pipeline:**

```
User Query → Query normalization (via Intake) → Evidence Retrieval Agent
  ├── Exact variant search (by gene + HGVS)                    [rung 1]
  ├── Exact search in the ClinVar coverage index               [rung 2]
  ├── Gene/position/rsID filtering                             [rungs 3–4]
  ├── Lexical retrieval (active ranker: TF-IDF | BM25 | RRF hybrid)  [rung 5]
  ├── Relevance ranking (weighted: exact match +10, gene +5, position +5, HIGH impact +2, conflict penalty -2, evidence completeness +6, lexical ×5, review data +1; theoretical max 34.0)
  └── Top-K selection (default K=5) with normalized relevance scores (0-1) and explanations
```

**Why a ladder rather than pure lexical search.** A plausible-looking answer built
on the *wrong* variant row is harder to notice than a crash, so retrieval prefers
the strongest available evidence and labels which rung produced the answer. The
exact rungs deliberately short-circuit the lexical models: if the variant is in
the corpus, ranking by text similarity is a strictly worse way to find it. The
consequence — that a pipeline-level comparison of TF-IDF against BM25 returns
three identical numbers — is why the ranker comparison in §4.2.1 scores the whole
corpus per query instead.

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
The Verifier's conservative cap (`apply_conservative_directive`, at most one
round) lowers the headline to ≤34.9 when no exact match or reviewed consensus
backs it; the pre-cap number is kept as `confidence_before_revision` and the
report/UI state both ("scored X, lowered to Y") because the component bars
describe the pre-cap inputs.

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
| **NLP** | Structured evidence summarization (rule-based) + abstractive (LLM) |
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

### 4.5 Supervisor and Verifier — the Control Plane

`agents/orchestrator.py`. These two are what turn a fixed chain of specialists
into an **agentic** system: the route through the pipeline is planned per query,
and a result can be rejected and corrected before it is reported.

| Aspect | Supervisor | Verifier |
|---|---|---|
| **File** | `agents/orchestrator.py` | `agents/orchestrator.py` |
| **Role** | Plan the run from the Intake result | Critique the classification against its evidence |
| **Decides** | `retrieval_mode` (`broad` / `targeted`) and `verification_budget` | `verdict` (`accept` / `revise`) and a directive |
| **Message** | `MessageType.PLAN`, stage `plan_ready` | `MessageType.VERIFICATION_RESULT`, stage `verification_complete` |
| **Receiver** | `pipeline` | `supervisor_agent` |

**What the Supervisor decides.** A bare HGVS, an rsID or a position with no gene
cannot be expected to match a record exactly, so those queries are planned as
**broad**; a gene + variant query is planned as **targeted**. The plan carries a
`verification_budget` of 1 and a human-readable `reason`, and is emitted as an
ordinary `PLAN` message so the decision is auditable.

**What the Verifier decides.** It checks whether the confidence the
Classification Agent asserted is *earned* by the retrieved evidence — in
particular, whether a moderate/high call is backed by an exact match or a
reviewed ClinVar consensus. In broad mode an exact match is not expected, so only
a *high* call is challenged. Its verdicts:

| Verdict | Directive | Meaning |
|---|---|---|
| `accept` | `null` | The call stands and goes to the Report Agent |
| `revise` | `conservative` | The call is re-stated with lower confidence, then verified once more |

**Three properties keep the loop safe** (each is pinned by a test in
`tests/test_orchestrator.py`):

1. **Bounded** — the budget is 1, so a run can never cycle. `revision_rounds` is
   exposed in the result and asserted to be at most 1.
2. **Conservative** — the directive only ever *lowers* the confidence number, and
   the categorical classification is never changed. Self-correction cannot
   manufacture certainty, nor can it overturn an evidence judgement on
   consistency grounds.
3. **Auditable** — both control messages travel encrypted on the same `trace_id`
   as the specialist messages, and a revised run records *both* the original and
   the corrected `CLASSIFICATION_RESULT`, so the self-correction is part of the
   permanent record rather than a silent overwrite.

**Why this counts as agency rather than a longer pipeline.** The specialists are
stateless transforms: given the same input they always produce the same output.
The control plane adds (a) a decision point whose result *changes the path* the
data takes, and (b) a feedback edge from a later stage back to an earlier one,
bounded so it terminates. Both are decided from data at run time.

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
Supervisor Agent → AgentMessage(PLAN) → pipeline
Evidence Agent → AgentMessage(EVIDENCE_RESULT) → Classification Agent
Classification Agent → AgentMessage(CLASSIFICATION_RESULT) → Report Agent
Verifier Agent → AgentMessage(VERIFICATION_RESULT) → Supervisor Agent
   (on `revise`: Classification Agent re-emits CLASSIFICATION_RESULT, then the
    Verifier re-checks — at most once)
Report Agent → AgentMessage(REPORT_RESULT) → Orchestrator
```

**Why this satisfies the requirement:**
- Agents never access each other's internal state directly — all data flows through `AgentMessage`
- Formal protocol fields: `message_id`, `trace_id`, `sender`, `receiver`, `message_type`, `timestamp`, `stage`, `encrypted`
- Every message payload is **encrypted** with Fernet symmetric encryption
- Every message is **logged** to `MessageLog` for a full audit trail
- The control messages (`PLAN`, `VERIFICATION_RESULT`) use the same envelope, so the system's *reasoning about its own run* is auditable, not just its results

> 📄 **Full protocol specification:** [`protocol/PROTOCOL.md`](protocol/PROTOCOL.md) documents the
> envelope schema, the message-type vocabulary (including the agentic control messages), the
> security guarantees on the wire, a complete worked message flow with real JSON, the audit-trail
> API and the REST transport layer. This is the reference to quote during the viva.
>
> 📄 **MCP transport:** the same agents are also exposed over the **Model Context Protocol** in
> `mcp_server.py` (`classify_variant`, `retrieve_evidence`, `list_example_queries`), so an MCP
> host — Claude Desktop, an IDE agent — can call the pipeline as tools. This is the second,
> standardised agent-communication transport alongside REST.

**REST API layer (`api.py`):**
- HTTP endpoints: `POST /api/classify`, `POST /api/report/pdf`, `GET /api/trail/{variant_id}`, `GET /api/pipeline/{variant_id}`, `GET /health`, the `/api/auth/*` account endpoints, `/api/keys` (scoped API keys), `/api/export/audit` (audit export), `/api/reviews`, and the `/api/admin/*` dashboard endpoints
- Frontend talks to the API via HTTP — an additional, external agent-communication layer
- `POST /api/classify`, `/api/trail/*` and `/api/pipeline/*` **require** an authenticated account — a JWT bearer token (`Authorization: Bearer …`), the HttpOnly session cookie, or a scoped `X-API-Key` — and return `401` without one. The agent layer keeps its own SHA-256 API-key check as defence-in-depth, so a call that bypasses FastAPI is still refused
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
| **Account authentication** | `auth/security.py`, `auth/dependencies.py` | Argon2id password hashing; 60-minute access tokens. **Email is the sole login identifier** — the username is a display handle and is never accepted as a credential, so there is exactly one name per account and a harvested username cannot be aimed at a sign-in. Addresses are lower-cased on write and on lookup. Identity is re-read from PostgreSQL on every request, so deactivation revokes access immediately and the authorization role always comes from the database, never from the token |
| **Signed-then-encrypted tokens** | `auth/security.py` | Tokens are a nested JWS inside a JWE (A256GCM), so the wire format is an opaque 5-part compact string rather than a base64-readable JWT that hands an attacker the user id, role and expiry. The inner JWS is still verified, so encryption adds confidentiality without weakening integrity. Signing and encryption use **separate keys**, so leaking one cannot forge the other |
| **Token binding** | `auth/security.py` | `iss` and `aud` are mandatory claims. A token minted by another system, or for another system, is rejected before its signature is trusted. Missing or default keys **fail at import** rather than silently minting tokens anyone can forge (`JWT_ALLOW_DEV_SECRET=true` is the only escape hatch, and `USING_DEV_SECRET` reports its use) |
| **HttpOnly session cookie** | `auth/cookies.py`, `frontend/app.js` | The browser session moved out of `localStorage` — readable by any XSS bug — into an `HttpOnly` cookie that script cannot touch. `Secure`, `SameSite=lax`, `Path=/`, expiring with the token |
| **CSRF defense** | `auth/dependencies.py`, `auth/cookies.py` | Cookie auth is ambient by nature, which is exactly what makes it CSRF-able. Any unsafe method authenticated **by cookie** must present a matching `X-CSRF-Token` header and is refused with 403 otherwise; safe methods are exempt. The CSRF cookie is deliberately readable, which is safe only because an attacker who learns it still cannot make the browser attach the `HttpOnly` cookie cross-origin. Bearer auth and `X-API-Key` auth are never ambient and need no CSRF token, so scripted clients are unaffected; an explicit header wins over any cookie |
| **Login throttling** | `auth/rate_limit.py` | Argon2id slows each guess but does not cap how many are made, so failures are limited **per IP and per account** together — one host cannot spray many accounts, many hosts cannot spray one account. Excess attempts return 429 with a growing `Retry-After`. Counters are in-process, so a multi-worker deployment needs a shared store such as Redis |
| **Auth audit trail** | `auth/audit.py`, `db/models.py` (`AuthEvent`), migration `0005_auth_events` | Login, registration, password change and every admin mutation write a row with subject and **actor** recorded separately, so "admin X changed clinician Y's plan" is answerable directly. Username/email are stored **hashed**, never raw, so the audit log is not a second copy of the user table |
| **Role-based access** | `auth/dependencies.py`, `api.py` | `require_admin()` guards `/api/admin/users`, `/api/admin/plans`, `PATCH /api/admin/users/{id}`, password reset and `/api/reviews`; a clinician gets 403. Guards prevent an admin changing their own role/active state, and prevent removing the last active administrator |
| **Plan feature gating** | `services/entitlement_service.py`, `api.py` | `plan.llm_enabled` selects the LLM or rules-only system instance at request time; Free runs the full four-agent pipeline with rule-based reasoning |
| **Quota enforcement** | `services/entitlement_service.py` | Per-UTC-month usage rows. Pre-flight `check_quota()` returns 429 before the pipeline runs; `record_usage()` re-checks under a PostgreSQL `SELECT … FOR UPDATE` row lock so concurrent requests cannot overshoot. **Every accepted query is charged**, including an exact repeat — the request id is a fresh `uuid4()` per call, so asking the same question twice costs twice. Retry-safety is opt-in via an `idempotency_key` (folded together with the user and the UTC month, so reusing a key next month is a new charge rather than a collision) |
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
| **Credential isolation** | `.gitignore`, `.env.example`, `Dockerfile`, `.dockerignore` | `.env` is gitignored and `.dockerignore`d, and `DATABASE_URL` / `JWT_SECRET_KEY` / `JWT_ENCRYPTION_KEY` are supplied per deployment as environment variables, so no credential is ever baked into the image. Database connections require TLS (`sslmode=require`) |
| **Scoped API keys** | `services/api_key_service.py`, `db/models.py` (`ApiKey`), migration `0006_api_keys` | Programme access via `X-API-Key` instead of a session. Only a SHA-256 digest is stored (the raw `vl_…` value is shown once at creation); a display prefix identifies a key without revealing it; `revoked_at` revokes it; and `resolve_key()` re-checks the owner's plan on **every** request, so a downgrade disables existing keys immediately. Issuing is gated by `plan.api_access` |
| **Audit export** | `api.py` (`GET /api/export/audit`), `plan.audit_export` | The caller's own usage records, auth events and review sign-offs as JSON or CSV. The query is scoped by user id, so one account can never export another's activity; a plan without the entitlement gets 403 |

---

## 7. LLM Integration (Google Gemini)

| Component | How Gemini is used |
|---|---|
| **Classification Agent** | Reason over evidence and apply ACMG rules in natural language, return JSON result |
| **Report Agent** | Generate plain-language clinical report + abstractive summary |
| **Report Agent** | Structured evidence summarization of evidence (fallback when LLM unavailable) |

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
| **Confidence never overstated** | LLM prompts instruct to be conservative and the LLM's own confidence value is discarded; a genuine conflict forces low confidence; the categorical label is always derived from the numeric score; a consensus-backed final call is floored by ClinVar review stars with the evidence-only figure disclosed; and the **Verifier Agent audits the assertion itself** — an over-confident call is sent back and re-stated, and the directive can only ever *lower* confidence |
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
Verification (Verifier Agent checks the confidence is EARNED by the evidence;
over-confident calls are re-stated conservatively — one bounded round)
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
- Computes IR metrics: Precision@K, Recall@K, Exact-match@K, **MRR@K, nDCG@K and MAP@K**
- Runs a **head-to-head lexical ranker comparison** (TF-IDF vs BM25 vs RRF hybrid)
  over the whole corpus per query — see §9.2.1
- Computes **confidence calibration** (ECE, MCE, Brier score and its skill score
  against the base rate) on the raw call, the reconciled call and the directional
  outcome — see §9.2.2
- Computes **subgroup fairness** by variant type, ACMG/AMP allele-frequency band
  and ClinVar review status, with a minimum group size — see §9.2.3
- Runs ablation comparison: rules-only vs Gemini-only vs combined

Three of these deserve their degeneracy stated up front rather than hidden:

- **With one relevant row per exact-variant query, `MAP@K` is numerically `MRR@K`**
  and `recall@K` is 1.0 or 0.0. Reporting them as independent findings would be
  dishonest; they are computed because they are the standard definitions and
  because the ranker comparison reuses the tested metric functions.
- **Calibration on a stratified sample is a statement about the sample**, and the
  bins carry a `MIN_BIN` (10) below which a bin is reported but flagged `small`.
- **Fairness strata are built only from input properties** (variant type, allele
  frequency, review status), never from the prediction, so a group cannot be
  shaped by the outcome being tested.

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

**Honest quality note (the weakest findings, stated plainly).** On this dataset the
agent's own reasoning has Pathogenic recall ≈ 5/378 and Benign recall 0/256: the rules
essentially never emit Benign. The cause is specific and fixable — BA1/BS1 are held
back deliberately (no validated disease-specific maximum credible frequency, see
`agents/classification_agent.py`), and without those benign criteria the score rarely
reaches a benign call, so unclassified benign rows fall to VUS instead. Negative Brier
skill (§9.2.2) says the confidence numbers do not beat a constant predictor either.
**Improvement plan:** (1) add the BA1/BS1 frequency thresholds from an external
authoritative source (ClinGen) rather than a project proxy; (2) recalibrate the
confidence components against real labels so skill > 0; (3) run the Gemini ablation
(§9.2.4) with the prior label withheld to measure LLM-only reasoning; (4) add fuzzy IR
queries (§9.2.1) so retrieval quality is not measured on exact-string lookups.
The reconciled figures (98.3% directional) are what a user sees, but they are
substantially ClinVar's own label — disclosed in every report's provenance block — so
this system currently *surfaces and reconciles* evidence rather than replacing expert
interpretation.

Retrieval metrics are independent of the label mode and remain available:

```
INFORMATION RETRIEVAL METRICS
  Sample size: 30  (ranker: tfidf)
  Precision@5:   0.200
  Recall@5:      1.000
  Exact-match@5: 1.000
  MRR@5:         1.000 (single relevant row per exact-variant query)
  nDCG@5:        1.000 (same run)
  MAP@5:         1.000 (numerically equal to MRR@5 -- see below)
  (with one relevant variant per query, MAP@k and MRR@k are the
   same statistic -- see the ranker comparison below)
```

> **Reading these numbers.** `Precision@5` is 0.200 because the denominator is K,
> not the list length: exactly one of the five returned rows is the relevant one.
> `Recall@5` and `Exact-match@5` are 1.000 because that one row is always inside
> the top five. `MRR@5`, `nDCG@5` and `MAP@5` are not independent of those — they
> are three different functions of the *rank* at which the same single hit
> landed, so reporting them as three findings would be double-counting. That is
> why §9.2.1 compares the rankers over the whole corpus, where a rank is a real
> measurement rather than a formality.
>
> These three rank-based numbers come from the same run as the block above;
> regenerate with
> `python evaluation.py --dataset data/clinvar_variant_dataset_enriched.xlsx --ground-truth real`.

### 9.2.1 Lexical ranker comparison — TF-IDF vs BM25 vs RRF hybrid

A pipeline-level comparison of the three rankers returns three identical numbers,
because the exact-variant rungs short-circuit lexical ranking entirely (§4.2). The
experiment therefore asks the narrower question that has a real answer:

> Given only the query text, where does each model place the correct row in the
> whole corpus?

Each sampled variant is turned into the query `"Is <gene> <hgvs> pathogenic?"`. All
three models score **every row** of the corpus; the correct row's rank is
`1 + |{documents scoring strictly higher}|`, so ties resolve in the target's
favour — the best rank the model could have returned. TF-IDF and BM25 use the
agent's own vectorizer and index over the same `search_text` field; the hybrid is
RRF (k = 60) over their two full rankings. Nothing varies between the columns but
the model.

```bash
# retrieval only: no LLM, no labels, rebuilds in seconds
python evaluation.py --rankers-only --sample-size 50
# or:  make evaluate-rankers
```

```
LEXICAL RANKER COMPARISON (50 queries, k=5, corpus-wide ranking)
  TF-IDF:   MRR@5 1.000, nDCG@5 1.000, Hit@1 1.000, mean rank 1.0, won 50/50
  BM25:     MRR@5 0.970, nDCG@5 0.978, Hit@1 0.940, mean rank 1.1, won 47/50
  RRF (k=60 over both rankings): MRR@5 1.000, nDCG@5 1.000, Hit@1 1.000,
            mean rank 1.0, won 50/50
```

| Ranker | MRR@5 | nDCG@5 | MAP@5 | Exact-match@5 | Hit@1 | Mean rank | Queries won |
|---|---|---|---|---|---|---|---|
| TF-IDF (default) | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.0 | 50/50 |
| BM25 (Okapi) | 0.970 | 0.978 | 0.970 | 1.000 | 0.940 | 1.1 | 47/50 |
| Hybrid (RRF) | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.0 | 50/50 |

**Queries won** is the one column not derivable from the others: how many sampled
queries each ranker placed the correct row highest. MRR, nDCG, MAP and mean rank
are correlated by construction on a one-relevant-row task, and the report says so
rather than presenting four agreeing numbers as four independent confirmations.
`Exact-match@5` is the column with clinical meaning: did the ranker surface the
correct variant at all.

> **Honest degeneracy note.** Each query contains the exact HGVS string that is
> in the target row, so TF-IDF scores near-perfectly and the comparison has
> little discriminating power on this query style. The table is reported as
> measured (BM25 drops 3 queries RRF recovers), and fuzzier queries — gene
> plus consequence, position only, typos — are the follow-up that would show
> real ranker differences. The numbers above regenerate with the command in
> this section.

### 9.2.2 Confidence calibration

Accuracy answers "how often is the call right?". Calibration answers the separate
and more useful question for a clinician: "when the system says 80, is it right
80% of the time?" A model can be accurate and badly calibrated — which is exactly
the failure a confidence number invites a reader to rely on.

`compute_calibration` bins predictions by confidence (10 bins, `MIN_BIN = 10`) and
reports, per bin, the mean claimed confidence against the observed accuracy, plus:

| Metric | Reads |
|---|---|
| **Expected Calibration Error (ECE)** | the count-weighted mean of \|accuracy − confidence\| across bins |
| **Maximum Calibration Error (MCE)** | the worst single bin — the error a user is most likely to be burned by |
| **Brier score** | mean squared error of the probability, over every row |
| **Brier skill score** | the Brier score relative to always predicting the base rate. **0 means "no better than a constant"**; negative means worse |

Computed three times: on the agent's own raw call, on the consensus-reconciled
call a user actually sees, and on the directional outcome. The `print_calibration`
renderer also draws a reliability strip per bin — `=` for the observed accuracy,
`^` for the claimed confidence — so a systematically over-confident bin is visible
at a glance rather than inferred from two columns of numbers.

```bash
python evaluation.py --dataset data/clinvar_variant_dataset_enriched.xlsx --ground-truth real
```

| Call | ECE | MCE | Brier | Skill vs base rate | Base rate |
|---|---|---|---|---|---|
| Raw evidence-based | 0.115 | 0.433 | 0.076 | −0.668 | 0.048 |
| Consensus-reconciled | 0.201 | 0.485 | 0.081 | +0.659 | 0.381 |
| Directional (determinate rows) | 0.199 | 0.477 | 0.242 | +0.010 | 0.426 |

> **Regenerate:** `python evaluation.py --dataset
> data/clinvar_variant_dataset_enriched.xlsx --ground-truth real`. The negative
> raw skill score is the honest finding: the agent's own confidence numbers are
> worse than a constant base-rate predictor, while the reconciled skill
> describes ClinVar's consensus rather than this system's reasoning.

**Reading the skill score honestly:** the reconciled call defers to a reviewed
ClinVar consensus, so it is accurate in the same direction-by-construction way
that makes its accuracy figure flattering (§9.2). A skill score near 1 there is a
statement about ClinVar's consensus, not about this system's reasoning. The raw
evidence-based row is the one that describes the agents.

### 9.2.3 Subgroup fairness

A single accuracy figure can hide a stratum the system is systematically bad for.
`compute_fairness` re-computes accuracy, reconciled accuracy, directional accuracy
and macro F1 within three input-defined strata:

| Dimension | Strata |
|---|---|
| **Variant type** | the consequence classes the corpus carries (missense, frameshift, nonsense, splice, …) |
| **Allele-frequency band** | absent/very rare (≤0.01%), rare (0.01–0.1%), low-frequency (0.1–1%), common (1–5%), too-common (≥5%) — ACMG/AMP cut points, not deciles |
| **ClinVar review status** | 3+ stars (expert reviewed), 2 stars, 1 star, 0 stars, unknown |

ACMG/AMP thresholds are used rather than sample deciles because the thresholds are
the clinically meaningful cut points (BA1/BS1/PM2 sit on this axis), whereas a
decile of a 100-row sample is a 10-row bin — too small to read anything from. For
the same reason every stratum below `min_group_size` (default 20) is computed but
flagged `small` and cannot be reported as the headline worst stratum; the `worst`
field always names an eligible stratum, or none.

Reported per dimension: the worst eligible stratum, the number of eligible and
small strata, and the full per-stratum table. The `eligible_strata` /
`small_strata` split is what stops a two-row group from becoming a finding.

```bash
python evaluation.py --dataset data/clinvar_variant_dataset_enriched.xlsx --ground-truth real
```

The per-dimension tables print under `FAIRNESS …`; the worst stratum per dimension
is:

| Dimension | Worst eligible stratum | Accuracy | Reconciled accuracy | Small strata excluded |
|---|---|---|---|---|
| Variant type | single-nucleotide variants (n=2762): 0.040 agent / 0.348 reconciled | 0.040 | 0.348 | groups < 20 |
| Allele-frequency band | low-frequency (n=41): 0.000 agent / 0.756 reconciled | 0.000 | 0.756 | groups < 20 |
| ClinVar review status | 1-star (n=1864): 0.001 agent / 0.001 reconciled | 0.001 | 0.001 | groups < 20 |

> **Regenerate:** same full-evaluation command as §9.2.2. The agent-call column
> is the system's own reasoning; the reconciled column defers to ClinVar
> consensus where available, which is why low-frequency reconciled accuracy is
> high while the agent's own call is 0.000 on that stratum.

> **What this is not.** These are subgroup-agreement figures against ClinVar
> labels, not a fairness audit in the regulatory sense. They say nothing about
> ancestry representation, which remains the sharpest limitation in this project
> (§8.1) — the corpus carries population-frequency fields but not per-ancestry
> breakdowns, and a frequency-based benign call can be a false negative for an
> under-represented population.

### 9.2.4 Ablation — rules-only vs Gemini-only vs combined

```
ABLATION COMPARISON (30-variant sample, proxy labels)
  Rules only:       90.0%
  Gemini only:      not run (see note)
  Rules + Gemini:   not run (see note)
```

> **Note:** Gemini-only ablation requires a valid Gemini API key. The table above
> reports the rules-only baseline; both Gemini arms are marked **not run** — they
> were never executed for this submission, so no LLM accuracy figure is claimed.
> Runnable on demand:
> `python evaluation.py --dataset data/clinvar_variant_dataset_enriched.xlsx
> --ground-truth real --sample-size 30` (without `--no-llm`). At two Gemini Flash
> calls per variant (classification + report) and published list prices
> (https://ai.google.dev/gemini-api/docs/pricing), a 30-variant ablation costs on
> the order of cents in API charges; the reason it is not included is run time
> and key availability, not methodology. Without a key the system falls back to
> rule-based mode.

> **Missingness note:** The evaluation labels a record `complete` only when
> SIFT, PolyPhen, and CADD are all available and finite, and reports separate
> complete/incomplete metrics so overall accuracy cannot conceal annotation
> coverage. Ground truth is now the **real ClinVar clinical significance**
> carried by the enriched workbook; the evidence-derived proxy labels remain
> available via `--ground-truth proxy`, but they re-use the classifier's own
> rules and therefore measure self-consistency, not correctness.

### 9.3 Unit Tests

**645 passing across 24 files** in the last full run — 73 covering the
agent/pipeline layer and 490 covering auth, security, entitlements, roles, admin,
the MCP tools, frontend and review; **plus 82 further cases** added with the
retrieval models and the analysis arithmetic: 47 in `tests/test_retrieval.py` and
35 in `tests/test_evaluation_metrics.py`. Those two files are the only ones in the
suite that assert arithmetic against hand-computed values (`make test` prints the
current total).

**Agent / pipeline (73)**
- **Intake Agent (19)** — entity extraction, query types, security, rejections, plus prompt-injection override/exfiltration rejection
- **Evidence Agent (14)** — exact lookup, gene-only, variant-only, position, IR metadata, top-K ranking, missingness metadata, encryption, TF-IDF fallback, plus `coverage_fallback` presence/value tests, plus the gene-prefixed-HGVS fallback test ("BRCA2:c.5946delT" still hits the ClinVar coverage index) and the `_normalise_hgvs` unit test (gene token stripped, transcript kept)
- **Classification Agent (23)** — pathogenic/benign/VUS, missing predictors, conflict lowering confidence, encryption, LLM fallback, plus 7 confidence-consistency tests (label↔number invariant, LLM self-reported confidence discarded, conflict caps the number, 2★/3★/0★ consensus behaviour), plus BS1-neutrality (AF 0.0002), CADD/ClinVar separation, 6 `review_required` tests (always present, boolean on every path, true on both disagreement branches), and the gene-overview no-call test (gene-level listing carries no variant verdict)
- **Orchestrator / control plane (17)** — Supervisor plans broad vs targeted retrieval; the Verifier accepts exact-match, consensus-backed and low-confidence calls and sends back over-confident ones; the conservative directive lowers the number (and therefore the label) without changing the classification; **nothing can ever raise confidence**; gene-overview no-call bypass; and the pipeline integration test asserts a plan and a verdict appear in both `pipeline_steps` and the audit trail, with self-correction bounded to one round
**Web / auth / product (490)**
- **Frontend (146)** — static checks over `index.html`/`app.css`/`app.js` plus served-asset checks: page slugs, view routing, email-only sign-in, cookie/CSRF session policy, confirmation-dialog state, the profile skeleton-restoration fix, admin panel loading
- **API keys (10)** — creation is gated on `plan.api_access` (403 on Free), the raw secret is returned once and never listed, `X-API-Key` authenticates, a plan downgrade disables existing keys, unknown and revoked keys are 401, and keys are scoped to their owner
- **Audit export (5)** — the `plan.audit_export` gate returns 403 without the entitlement and 401 unauthenticated; the JSON export contains only the caller's own rows; the CSV export carries a header and an attachment disposition; an unknown format is a 422
- **MCP tools (6)** — the module imports with no `mcp` package installed and `build_server()` fails with a clear "pip install mcp" message; `classify_variant` returns a structured result carrying the Responsible-AI block; a prompt-injection query is rejected; `retrieve_evidence` returns ranked records and honours `top_k`; the example queries are non-empty
- **Admin (83)** — role gates, plan management, account mutation, login throttling (per-IP and per-account), and the cookie/CSRF session contract
- **Auth (35)**, **upgrades (32)**, **password reset (25)**, **security (21)**, **profile (20)**, **entitlements (19)**, **analytics (19)**, **review sign-offs (17)**, **classify API (20)**
- **LLM resilience (8)** — bounded SDK retry, 503 → fallback-model chain → `RuntimeError`, classifier and reporter degradation, direction-aware conflict semantics, plus end-to-end 503 cascade (rules + template fallback, encrypted message path)
- **Cache isolation (6)** — per-user query-cache separation
- **LLM output checks (12)** — ClinVar label withheld from the LLM prompt, LLM report verification, login timing shield
- **PDF auth (6)** — PDF authentication and cross-account trail isolation, including the two-users-one-variant regression

**Retrieval and analysis arithmetic (82)** — the only hand-computed assertions in the suite
- **`test_retrieval.py` (47)** — BM25 against a three-document corpus worked through by hand (a term in every document must still have *positive* IDF; `b` must penalise long documents; `k1 = 0` must make term frequency irrelevant; the documented score of 0.90665); tokenisation against the HGVS forms the corpus actually contains, including the ways BM25's tokeniser deliberately differs from TF-IDF's; RRF symmetry, single-list documents and the k-insensitivity; score fusion normalisation; and every ranking metric — that `precision@k` divides by `k` and not by the list length, that nDCG@k discounts by rank and supports graded relevance, that AP@k rewards finding the right answer first, and that a total miss scores 0.0 rather than 1.0
- **`test_evaluation_metrics.py` (35)** — ECE against hand-computed values (including that ECE *weights bins by size*, which is the difference between ECE and the unweighted mean of bin gaps), Brier and its skill score, bin-edge behaviour at confidence 100, the tri-state directional outcome (including that a VUS prediction is a *directional miss*, not an unscorable row), the ACMG/AMP allele-frequency boundaries, review-star banding, `_optimistic_rank` tie handling, and the small-stratum guard that stops a two-row group being reported as the worst fairness finding

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
| **Free / Research** | $0 · Rs. 0 | 10 queries/month, rule-based mode, basic evidence |
| **Professional** | $99/month · Rs. 29,700 | 500 queries/month, Gemini LLM mode, API access, audit trail export |
| **Enterprise** | Custom (`price_cents = NULL`) | Unlimited queries (`monthly_query_limit = NULL`), on-premise deployment, LIMS integration, SLA, dedicated support |

Prices are quoted in USD and shown in LKR in the product, converted at the rate the
frontend applies (`LKR_PER_USD` in `frontend/app.js`). `price_cents = NULL` for
Enterprise is "custom pricing", not free — `NULL` and `0` mean different things, and
a `0` would sort Enterprise below Free in any price-based comparison.

Each account is identified by a JWT, and the pipeline is gated per plan: the Free
tier runs the **full four-agent pipeline** with rule-based reasoning rather than a
reduced workflow. Monthly quotas are counted per UTC calendar month from immutable
usage rows, with a `429` returned before any agent work starts once the limit is
reached.

Unit economics (illustrative, not a quote): a Professional query costs the
deployment one pipeline run — PostgreSQL + hosting amortised over the month plus,
on LLM-enabled plans, two Gemini Flash calls (classification + report). At
$99 / 500 queries the list price is $0.198 per query; at Gemini Flash list prices
(see https://ai.google.dev/gemini-api/docs/pricing) the two model calls cost on
the order of a cent or less, so inference is a small share of the margin and
hosting is the dominant cost. Free ($0 / 10 queries) runs rules-only, so its
marginal cost is hosting alone. Enterprise is custom-priced because on-premise
deployment and LIMS integration dominate the cost, not per-query inference.
Market sizing is an estimate, not a measurement: the target is genetics labs,
hereditary cancer screening programmes and research cohorts doing batch
re-analysis — a niche where per-query pricing suits bursty case-review usage
better than seat pricing.

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
│   ├── evidence_agent.py        # Agent 2: Information Retrieval (ladder + TF-IDF/BM25/RRF)
│   ├── classification_agent.py  # Agent 3: LLM + Rules + ClinVar reconciliation
│   ├── orchestrator.py          # Supervisor (planning) + Verifier (bounded self-correction)
│   └── report_agent.py          # Agent 4: LLM + Summarization + Responsible AI
├── retrieval/                   # Pure retrieval maths, no agent dependencies
│   ├── bm25.py                  # Okapi BM25 (hand-rolled)
│   ├── fusion.py                # Reciprocal Rank Fusion + score fusion
│   └── metrics.py               # P@k, R@k, nDCG@k, MAP@k, MRR
├── data/
│   ├── clinvar_variant_dataset.xlsx           # 3,009 variants
│   ├── clinvar_variant_dataset_enriched.xlsx  # + real ClinVar labels (generated)
│   ├── clinvar_labels.csv                     # ClinVar label sidecar the enrichment writes
│   ├── variant_summary_brca.tsv               # BRCA coverage / rsID index (generated)
│   ├── review_signoffs.example.json           # shape of the pre-0002 local sign-off store
│   └── DATA_SOURCE.md
├── docs/                        # Architecture, sequence, data-model, retrieval (Mermaid)
├── frontend/
│   ├── index.html               # Landing, workspace, profile, admin views + auth modal
│   ├── app.css                  # Styling, built from the same design tokens
│   └── app.js                   # Views, auth flows, renderers, admin dashboard
├── llm/
│   └── gemini_client.py         # Gemini API wrapper (bounded retries, graceful fallback)
├── protocol/
│   ├── message.py               # Agent communication protocol + NumpyJSONEncoder
│   ├── security.py              # Auth, sanitization, encryption (Fernet)
│   └── PROTOCOL.md              # Full protocol spec
├── auth/                        # JWT account layer in front of the agents
│   ├── dependencies.py          # get_current_user(), require_admin()
│   ├── routes.py                # /api/auth/register, /login, /me, reset, profile
│   ├── schemas.py               # Pydantic auth contracts
│   ├── security.py              # Argon2id hashing + JWE/JWS mint/decode
│   ├── cookies.py               # HttpOnly session cookie + CSRF double-submit
│   ├── rate_limit.py            # Per-IP and per-account login throttling
│   └── audit.py                 # Hashed-identifier auth-event recording
├── db/                          # Account/entitlement persistence
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
├── migrations/                  # Alembic migrations 0001–0007 (nine tables)
├── scripts/
│   ├── enrich_clinvar_labels.py          # Join ClinVar labels onto the dataset
│   ├── build_variant_summary_index.py    # Build the BRCA rsID/coverage index
│   ├── seed_plans.py                     # Free / Professional / Enterprise plans
│   ├── create_admin.py                   # Bootstrap admin account
│   ├── import_review_signoffs.py         # One-off backfill of the pre-0002 JSON sign-off store
│   └── smoke_auth.py                     # End-to-end auth check against PostgreSQL
├── tests/
│   ├── conftest.py              # Shared fixtures: in-memory SQLite, placeholder secrets
│   ├── test_intake_agent.py     # 13 tests
│   ├── test_evidence_agent.py   # 11 tests
│   ├── test_classification_agent.py  # 22 tests
│   ├── test_llm_resilience.py   # 8 tests
│   ├── test_cache_isolation.py  # 6 tests — per-account result caching
│   ├── test_security.py         # 21 tests
│   ├── test_auth.py             # 35 tests — registration, login, JWT, session
│   ├── test_entitlements.py     # 19 tests — quotas, month windows, row locking
│   ├── test_classify_api.py     # 20 tests — 401/429, plan gate, usage recording
│   ├── test_admin.py            # 83 tests — roles, privilege guards, password reset
│   ├── test_review_signoffs.py  # 17 tests — attestation persistence and durability
│   ├── test_analytics.py        # 19 tests — dashboard figures, gap-filled daily series
│   ├── test_upgrades.py         # 32 tests — upgrade request lifecycle
│   ├── test_password_reset.py   # 25 tests — one-time code issue/redeem
│   ├── test_profile.py          # 20 tests — self-service profile editing
│   ├── test_orchestrator.py     # 16 tests — supervisor plans, verifier bounds
│   ├── test_retrieval.py        # BM25 / fusion / ranking-metric arithmetic
│   ├── test_evaluation_metrics.py  # calibration + fairness arithmetic
│   ├── test_api_keys.py         # 10 tests — key issuance, scoping, revocation
│   ├── test_audit_export.py     # 5 tests — export gating and ownership scoping
│   ├── test_mcp_tools.py        # 6 tests — MCP tool surface
│   ├── test_frontend.py         # 134 tests — view visibility, JS/DOM wiring, assets
│   ├── test_pdf_auth.py         # 5 tests — PDF auth, escaping, cross-account trail isolation
│   └── test_llm_output_checks.py # 13 tests — LLM prior withholding, report verification, login timing
├── tests/js/
│   └── apply_session_visibility.test.mjs   # Node test for the session-visibility JS
├── analysis_results/            # Generated visualizations for presentations
├── .github/workflows/ci.yml     # pytest + coverage, Node tests, advisory lint/types/audit
├── .dockerignore                # Keeps secrets out of the image build context
├── .env.example                 # API key + DATABASE_URL + JWT_SECRET_KEY template
├── .gitignore                   # Generated ClinVar artifacts are gitignored
├── alembic.ini                  # Alembic config
├── ANALYSIS_GUIDE.md            # How to reproduce the notebook and its figures
├── AUTH.md                      # Auth, plan and quota design documentation
├── COMPLIANCE_REGULATORY_POSITIONING.md  # Regulatory positioning for the commercialisation case
├── CONTRIBUTING.md              # Setup, layout, test conventions, clinical-safety rules
├── Dockerfile                   # Production image (non-root, uvicorn)
├── Makefile                     # make test / run / evaluate / evaluate-rankers / migrate
├── REVIEW_SIGNOFF_WORKFLOW.md   # Human review / sign-off workflow
├── SECURITY.md                  # Security policy and known limitations
├── LICENSE                      # MIT License
├── analysis.ipynb               # Data analysis & visualization notebook
├── api.py                       # FastAPI REST API
├── evaluation.py                # Evaluation (classification + IR + calibration + fairness)
├── main.py                      # Orchestrator (4-agent pipeline)
├── mcp_server.py                # MCP tool server (optional `mcp` dependency)
├── pyproject.toml               # ruff / mypy / coverage configuration
├── pytest.ini                   # Pytest config
├── requirements.txt             # Full development stack (incl. the notebook deps)
└── requirements-api.txt         # Lean runtime set the API and the image install
```

---

## 12. How to Run

```bash
cd variant_agent_system
pip install -r requirements.txt
cp .env.example .env   # add DATABASE_URL (a free Neon PostgreSQL, no Docker)
                      # + GEMINI_API_KEY (optional — rule-based fallback works)
                      # + BOTH JWT keys (JWT_SECRET_KEY and JWT_ENCRYPTION_KEY;
                      #   the process refuses to start with fewer than two)

# Database (account layer) — the same three commands on macOS, Windows and Linux
alembic upgrade head
python scripts/seed_plans.py    # Free / Professional / Enterprise
python scripts/create_admin.py  # bootstrap admin (prompts for a password)

# CLI (no account needed — talks to the agents directly)
python3 main.py                               # default demo query
python3 main.py --query "What variants are in BRCA1?"
python3 main.py --no-llm                       # rule-based only (no Gemini API calls)

# Web UI (register an account, sign in)
uvicorn api:app --reload --port 8000
# open http://localhost:8000

# Tests
python3 -m pytest tests/ -v    # 645 tests (no database required)
node --test tests/js/          # frontend JS tests
python3 scripts/smoke_auth.py  # end-to-end auth check against real PostgreSQL

# or, the same jobs through the Makefile
make test          # pytest
make test-js       # node --test
make coverage      # pytest with a coverage report
make migrate       # alembic upgrade head

# Evaluation
python3 evaluation.py                              # full suite (100 variants)
python3 evaluation.py --sample-size 50 --no-llm    # quick (no LLM costs)
python3 evaluation.py --rankers-only               # TF-IDF vs BM25 vs RRF, no LLM, no labels

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
4. ✅ **NLP**: Named Entity Recognition (gene, HGVS, position, rsID) + intent classification + structured/abstractive summarization
5. ✅ **Information Retrieval**: a labelled evidence ladder (exact variant → ClinVar coverage index → same position → same gene → lexical only), with the lexical rung rankable by TF-IDF cosine similarity (scikit-learn `TfidfVectorizer` + `cosine_similarity`), a **hand-rolled Okapi BM25**, or an **RRF hybrid** of the two — plus ranked top-K selection with explainable relevance scores, MRR/nDCG/MAP metrics and a head-to-head ranker comparison (§9.2.1).
6. ✅ **Security**: two-layer authentication (PostgreSQL accounts with Argon2id + JWE signed-then-encrypted tokens at the API edge, SHA-256 agent-layer API keys as defence-in-depth), **email as the sole login identity** (username is a display handle, never a credential), **HttpOnly session cookies with CSRF double-submit protection** (bearer auth kept for scripted clients), **per-IP and per-account login throttling**, a dedicated **`auth_events` audit table** with hashed identifiers and separate actor/subject, mandatory `iss`/`aud` binding and fail-fast JWT configuration, role-based access control, plan feature gating, monthly quota enforcement where **every accepted query is charged** and retry-safety is opt-in via `idempotency_key`, row-locked commit-time re-checks, input sanitization + explicit prompt-injection defense, Fernet encryption in transit, full audit logging, 503→rules/template fallback
7. ✅ **Agent Communication Protocol**: Formal `AgentMessage` schema (message_id, trace_id, sender, receiver, message_type, timestamp) + REST API layer
8. ✅ **Responsible AI**: Confidence derived from evidence (not arbitrary), never overstated, VUS handled honestly, conflict-adjusted, full transparency, clinical disclaimers
9. ✅ **Commercialization**: pricing model (Free/Professional/Enterprise) **implemented** as PostgreSQL-backed plans with per-plan feature gating and monthly quotas, plus target users and deployment ideas
10. ✅ **Evaluation**: honest evaluation against **real ClinVar labels** (4.8% exact / 42.6% directional agreement from the agent's own ACMG reasoning, rising to 38.1% / 98.3% after ClinVar-consensus reconciliation), confusion matrix, precision/recall/F1, IR metrics (Precision@K, Recall@K, Exact-match@K, **MRR@K, nDCG@K, MAP@K**), a **head-to-head TF-IDF/BM25/RRF ranker comparison**, **confidence calibration (ECE, MCE, Brier, Brier skill)** and **subgroup fairness by variant type, allele-frequency band and review status**, ablation comparison, and **645 passing unit tests** (73 agent/pipeline + 490 auth/security/entitlement/role/API-key/audit-export/MCP/frontend/review, of which 82 are retrieval-model/fusion/metric/calibration/fairness arithmetic and 18 are hardening-regression cases for PDF auth, cross-account trail isolation, LLM output checks and the login timing shield, plus 2 gene-overview no-call cases and 2 gene-prefixed-HGVS fallback cases) (§9.3)

## 14. References

**Clinical genomics and variant interpretation**

1. Richards, S., Aziz, N., Bale, S., et al. (2015). Standards and guidelines for the interpretation of sequence variants: a joint consensus recommendation of the American College of Medical Genetics and Genomics and the Association for Molecular Pathology. *Genetics in Medicine*, 17(5), 405–424.
2. Landrum, M. J., Lee, J. M., Benson, M., et al. (2018). ClinVar: improving access to variant interpretations and supporting evidence. *Nucleic Acids Research*, 46(D1), D1062–D1067.
3. Rehm, H. L., Berg, J. S., Brooks, L. D., et al. (2015). ClinGen — The Clinical Genome Resource. *New England Journal of Medicine*, 372(23), 2235–2242.
4. Karczewski, K. J., Francioli, L. C., Tiao, G., et al. (2020). The mutational constraint spectrum quantified from variation in 141,456 humans (gnomAD). *Nature*, 581, 434–443.

**Retrieval models**

5. Sparck Jones, K. (1972). A statistical interpretation of term specificity and its application in retrieval. *Journal of Documentation*, 28(1), 11–21.
6. Salton, G., & Buckley, C. (1988). Term-weighting approaches in automatic text retrieval. *Information Processing & Management*, 24(5), 513–523.
7. Robertson, S. E., & Walker, S. (1994). Some simple effective approximations to the 2-Poisson model for probabilistic weighted retrieval. *Proceedings of SIGIR '94*, 232–241.
8. Sparck Jones, K., Walker, S., & Robertson, S. E. (2000). A probabilistic model of information retrieval: development and comparative experiments. *Information Processing & Management*, 36(6), 779–808 and 809–840.
9. Cormack, G. V., Clarke, C. L. A., & Büttcher, S. (2009). Reciprocal rank fusion outperforms Condorcet and individual rank learning methods. *Proceedings of SIGIR '09*, 758–759.
10. Manning, C. D., Raghavan, P., & Schütze, H. (2008). *Introduction to Information Retrieval*. Cambridge University Press.

**Retrieval evaluation**

11. Järvelin, K., & Kekäläinen, J. (2002). Cumulated gain-based evaluation of IR techniques. *ACM Transactions on Information Systems*, 20(4), 422–446.
12. Voorhees, E. M. (1999). The TREC-8 Question Answering Track Report. *Proceedings of TREC-8*, NIST.
13. Voorhees, E. M., & Harman, D. K. (2005). *TREC: Experiment and Evaluation in Information Retrieval*. MIT Press.

**Confidence calibration and fairness**

14. Brier, G. W. (1950). Verification of forecasts expressed in terms of probability. *Monthly Weather Review*, 78(1), 1–3.
15. Murphy, A. H. (1973). A new vector partition of the probability score. *Journal of Applied Meteorology*, 12(4), 595–600.
16. Naeini, M. P., Cooper, G. F., & Hauskrecht, M. (2015). Obtaining well calibrated probabilities using Bayesian binning. *Proceedings of AAAI 2015*.
17. Guo, C., Pleiss, G., Sun, Y., & Weinberger, K. Q. (2017). On calibration of modern neural networks. *Proceedings of ICML 2017*, PMLR 70, 1321–1330.
18. Barocas, S., Hardt, M., & Narayanan, A. (2019). *Fairness and Machine Learning: Limitations and Opportunities*. fairmlbook.org.

**Tooling and standards**

19. Pedregosa, F., Varoquaux, G., Gramfort, A., et al. (2011). Scikit-learn: Machine learning in Python. *Journal of Machine Learning Research*, 12, 2825–2830.
20. Biryukov, A., Dinu, D., & Khovratovich, D. (2016). Argon2: New generation of memory-hard functions for password hashing and other applications. *Proceedings of IEEE EuroS&P 2016*.
21. Jones, M., Bradley, J., & Sakimura, N. (2015). *JSON Web Token (JWT)*. RFC 7519, IETF.
22. Jones, M., & Hildebrand, J. (2015). *JSON Web Encryption (JWE)*. RFC 7516, IETF.
23. OWASP Foundation. *Cross-Site Request Forgery Prevention Cheat Sheet*.
