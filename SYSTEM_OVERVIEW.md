# VariantLens — System Overview

## What it is

A multi-agent decision-support system that interprets BRCA1/BRCA2 genomic
variants from ClinVar evidence. Ask "Is BRCA1 c.5946delT pathogenic?" and get
a classification, a confidence score with its arithmetic, the retrieved
records and why they were chosen, an audit trail of every agent hop, and a
plain-language report. Decision-support, not a diagnosis.

## Why the problem is real

Genetic testing labs find variants that experts disagree about. Of the 3,009
variants in the study workbook (all drawn from ClinVar rows flagged as
conflicting), only 1,168 carry a single determinate consensus label. The rest
are literally "Conflicting classifications of pathogenicity" — a statement
about what submitters did, not a class any system can predict.

## Architecture — six agents, one protocol

```
User query (free text)
   ↓
Auth + entitlement check (JWT → plan gate → monthly quota)
   ↓
Intake Agent          NER + intent classification + input sanitization
   ↓
Supervisor            plans THIS run: broad vs targeted retrieval
   ↓
Evidence Agent        IR: exact / gene / position / rsID lookup + TF-IDF
   ↓                      ranking, top-5 with relevance explanations
Classification Agent  ACMG-style rule engine + optional Gemini reasoning
   ↓
Verifier              critiques the call; may send it back ONCE
   ↓                      (only to lower confidence, never to raise it)
Report Agent          NLP summarization + plain-language report + disclaimer
   ↓
Result (label, confidence breakdown, evidence, audit trail, disclaimers)
```

**Agent communication protocol.** Agents never read each other's internal
state. Every hand-off is an `AgentMessage` (`protocol/message.py`): a
standard header — `message_id`, `trace_id`, `sender`, `receiver`,
`message_type`, `variant_id`, `stage`, `timestamp` — plus a payload that is
Fernet-encrypted before it leaves the producer. This is the same
envelope-style contract as MCP/A2A (standard header + opaque payload).

**Three transports over the same agents:** in-process (the pipeline),
HTTP/REST (`api.py`, JWT auth) and MCP stdio (`mcp_server.py`, for LLM hosts
like Claude Desktop). A result obtained over MCP is identical to one over
HTTP — one implementation, no drift.

## What "rule-based" means here — every rule, spelled out

The word "rule-based" appears throughout the project. It always means the
same thing: a deterministic `if`/regex/score that runs without any model and
can be read top-to-bottom. There are six rule families.

### Rule family 1 — Intake NER (`agents/intake_agent.py`)

Entity extraction is regex + a small gene dictionary (explainable, and it
parses the formal notations this system supports):

| Entity | Rule |
|---|---|
| Gene | Dictionary: `BRCA1`, `BRCA 1`, `BRCA2`, `BRCA 2` (aliases normalised) |
| HGVS | Regex matching `c.` and `g.` forms, with optional transcript/chromosome prefix |
| Position | `position/pos/at <4–10 digits>`, or `BRCA1 41209082` / `BRCA2:32890599`, or a bare number that is the entire query |
| rsID | `\brs\d+\b` |

### Rule family 2 — Intent classification (same file)

A decision table over the extracted entities (first match wins):

| Condition | `query_type` | Default intent |
|---|---|---|
| HGVS + gene | `gene_variant` | `classification` |
| HGVS only | `variant_only` | `variant_search` |
| rsID (checked before gene-only, so "BRCA1 rs… frequency" stays variant-specific) | `rsid` | `rsid_search` |
| gene + position | `position` | `position_search` |
| gene only | `gene_only` | `gene_search` |
| position only | `position_only` | `position_search` |
| none of the above | `unknown` → rejected with help text | — |

A second rule layer refines the *focus* for variant queries: keyword
patterns map to `population_frequency` (common/rare/frequency/AF),
`protein_impact` (protein/SIFT/PolyPhen/CADD), `variant_consequence`
(consequence/effect/missense/frameshift/stop-gained) and
`prior_classifications` (ClinVar/submitters/consensus/agree).

### Rule family 3 — Input sanitizer (`protocol/security.py`)

Runs before anything else. Rejects: empty/oversized (>200 chars), SQL
statement shapes (`;DROP`, `UNION SELECT`, tautologies, comment openers), markup (`<script>` etc.), path traversal, and a prompt-injection denylist
(ignore-previous-instructions, system-prompt exfiltration, role hijack).

### Rule family 4 — The ACMG-style rule engine (`agents/classification_agent.py`)

A numeric score is built from evidence, then mapped to a label:

| Evidence | Rule | Score |
|---|---|---|
| Max allele frequency > 0.001 (BA1 proxy) | common in population | −2 |
| Max AF == 0 (or ≤ 0.0001) | rare/absent (PM2) | +1 |
| IMPACT == HIGH | loss-of-function evidence (PVS1) | +2 |
| SIFT deleterious **and** PolyPhen damaging | predictors agree (PP3) | +1 |

| SIFT tolerated **and** PolyPhen benign | predictors agree (BP4) | −1 |
| CADD ≥ 20 | computational support | +1 |
| Consequence is frameshift/stop/splice/… | null variant (PVS1_strong, recorded) | gates Pathogenic |
| Synonymous, no splice impact | benign (BP7) | −1 |
| Intermediate AF (e.g. 0.0002) | **neutral observation** — BS1 is deliberately never auto-applied (no validated disease-specific threshold exists) | 0 |

Missing predictors are never treated as benign: SIFT/PolyPhen rules apply
only when **both** are present; missing values reduce confidence instead.

**Combining rules (score → label):** `score ≥ 5` **and** null variant →
Pathogenic; `score ≥ 2` → Likely Pathogenic; `score ≤ −2` → Likely Benign;
`score ≤ −4` → Benign; otherwise → VUS. (The null-variant gate is what
lets the engine emit *Pathogenic* at all — PVS1 plus supporting evidence.)

### Rule family 5 — Confidence is derived, never asserted

Confidence (0–100) is a weighted sum of five auditable components:
evidence strength (≤30), ClinVar submitter consistency (0 or 10), retrieval
quality (≤20: relevance, record count, exact-match), predictor agreement
(≤20), allele-frequency plausibility (≤10). The categorical label is a pure
function of the number: ≥60 → "high", ≥35 → "moderate", else "low".

**Responsible-AI caps (rules that can only lower confidence):**

- Conflicting submitters → numeric capped below 35 ("low"), regardless of score.
- A VUS is never shown as high confidence → capped just below 60.
- The label is always re-derived from the (possibly capped) number, so the
two can never disagree in the UI or the PDF.
- Consensus reconciliation: when a reviewed ClinVar consensus (≥2 stars)
exists and differs, the final call defers to it — and the report discloses
both the evidence-only figure and the fact that the final call is
consensus-backed, plus `review_required` when they disagree.

### Rule family 6 — Supervisor and Verifier (`agents/orchestrator.py`)

**Supervisor plan rule:** if the query type is `variant_only`, `rsid`,
`position_only` or `unknown` (an exact match is not guaranteed by
construction), retrieval runs in **broad** mode and the Verifier tolerates a
partial match; otherwise **targeted** mode, where the Verifier demands an
exact match or a reviewed consensus. The plan is emitted as a `PLAN`
message — visible in the audit trail, not hidden in code.

**Verifier rules:** a call at moderate/high confidence must be *earned* by
an exact match or a reviewed consensus (in broad mode only high is
challenged). If not earned → verdict `revise` with directive `conservative`,
and the classification is re-stated with lower confidence — **one** bounded
round; the Verifier can never raise confidence.

### Retrieval ranking rules (`agents/evidence_agent.py`)

Lookup order (strongest evidence first): exact gene+HGVS → same gene →
genomic position → rsID → lexical search. Candidates are then scored by a
weighted rule: **+10** exact match, **+5** gene, **+5** position, **+2**
HIGH impact, **−2** conflict penalty, **≤6** evidence-completeness, **≤5**
lexical similarity (TF-IDF cosine; BM25 and RRF-hybrid are selectable),
**≤1** ClinVar review data — maximum 34. Top-5 are returned, each with its
relevance score and a written explanation. The ranking method, candidate
count and active ranker are reported in `ir_metadata` on every result.

### Entitlement rules (`services/entitlement_service.py`)

Plan rows (Free/Professional/Enterprise) gate features and monthly quotas.
The quota check runs **before** any agent: exhausted → HTTP 429. Free runs
the full four-agent pipeline in rules-only mode; Professional enables the
LLM. Usage is counted per UTC month from immutable rows.

**Where the LLM fits.** Gemini is used for two things only: ACMG-style
reasoning over the structured evidence (classification mode) and
abstractive summarisation/report writing (report mode). Its self-reported
confidence is discarded; its output is validated (label, confidence, disclaimer)
and it falls back to the rules on any API failure.

## Assignment requirements → how this system satisfies them

| # | Requirement | Where it is satisfied |
|---|---|---|
| 1 | Solve a real problem in a selected domain | BRCA1/BRCA2 variant interpretation for genetic-testing labs (biotech/healthcare); real ClinVar data, real expert disagreement |
| 2 | At least two interacting intelligent agents | Six: Intake, Evidence, Classification, Report + Supervisor & Verifier control plane — all exchanging typed `AgentMessage`s |
| 3a | One or more LLMs | Google Gemini (`llm/gemini_client.py`) with multi-model failover, caching, and a rules-only fallback; used in classification and report generation |
| 3b | NLP techniques (NER, summarisation) | Rule-based NER + intent classification (Intake); structured and LLM abstractive summarisation (Report) |
| 3c | Information Retrieval module | TF-IDF / BM25 / RRF-hybrid ranking, weighted relevance scoring, top-k with explanations, evaluated with nDCG@k, MAP@k, MRR@k (`retrieval/`, `docs/retrieval.md`) |
| 3d | Security features | Input sanitization, prompt-injection denylist, Fernet encryption of every agent payload, JWT accounts + Argon2id, scoped API keys, rate limiting, audit trail/export |
| 3e | Defined agent communication protocols | `AgentMessage` envelope (MCP/A2A-style) in-process, HTTP/REST externally, MCP stdio server (`protocol/PROTOCOL.md`, `mcp_server.py`) |
| 4 | Responsible AI | **Fairness**: ancestry-skew limitation attached to every result, UI and PDF; fairness strata measured in evaluation. **Explainability**: confidence components, ACMG criteria, retrieval rationale, per-hop audit trail with trace IDs. **Transparency**: rules-only mode auditable, consensus deferral disclosed, degenerate metrics called out. **Privacy**: encrypted message log, hashed credentials, per-user data scoping |
| 5 | Commercialization + pricing | Implemented plan tiers: Free $0 (10 queries, rules), Professional $99/mo (500 queries, LLM, API), Enterprise custom — enforced as DB rows with quotas and a 429 gate; upgrade workflow with admin approval; deployment ideas in `PROJECT_REPORT.md` §10 |

## Where to look in the repo

| Question | Document |
|---|---|
| How to run it | `README.md` (Quickstart) |
| Full technical report & evaluation numbers | `PROJECT_REPORT.md` |
| Why 652 tests, what each module protects | `TESTING.md` |
| Agent message contract & transports | `protocol/PROTOCOL.md` |
| Security controls and their honest limits | `SECURITY.md`, `AUTH.md` |
| Retrieval design rationale | `docs/retrieval.md` |
| Dataset provenance & label caveats | `data/DATA_SOURCE.md` |
| Clinical/regulatory positioning | `COMPLIANCE_REGULATORY_POSITIONING.md` |

**Honest headline numbers** (`PROJECT_REPORT.md` §9): the system's own
reasoning agrees with ClinVar's determinate labels directionally 42.6% of
the time; after disclosed consensus reconciliation the user-visible figure
is 98.3% — largely ClinVar's own label reflected back, which the report
says plainly instead of claiming it as model accuracy. Safety-relevant
false positives (benign called pathogenic) fell from 10.7% to 0.2%.
