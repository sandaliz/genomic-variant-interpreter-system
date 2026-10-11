# AI Vulnerability Assessment — Information Retrieval & Security

**System:** VariantLens (Genomic Variant Interpretation Agent) — IT 3041
**Specialization:** Student 4 — Information Retrieval and Security Assessment

> **Draft note.** Findings below were read from the committed code
> (`retrieval/`, `agents/evidence_agent.py`, `api.py`) and the documented
> evaluation. Re-verify, add screenshots/logs, insert your name, and own every
> severity call at the viva.

---

## 1. Executive Summary

The Evidence Agent is the retrieval core: it turns a parsed query into a ranked
list of ClinVar/gnomAD evidence rows using **exact lookup + TF-IDF + BM25 +
Reciprocal Rank Fusion (RRF)**. This assessment asks whether that pipeline is
**accurate, robust to manipulation, safe from retrieval-induced hallucination,
sourced reliably, and properly secured at the API/protocol layer**.

I evaluated the retrieval code, the ranking fusion, the evidence-metadata design,
and the HTTP/communication surface. Findings:

- **Strengths (verified in code):** retrieval is **explainable** — every result
  carries `retrieval_strategy` and `evidence_source`, and BM25 exposes
  per-term relevance explanations; fusion uses the standard **RRF (k=60)**, which
  the code documents as deliberately robust when one ranker misranks; CORS is
  **restrictive by default** (localhost only, env-overridable); and the API
  enforces **API-key auth + per-plan quota** before any retrieval runs.
- **Weaknesses found:** the ranking **bonus weights are heuristic** and the
  hybrid score is **batch-dependent** (max-normalisation), so results can shift
  with the candidate set (**VULN-I3, Low**); retrieval "confidence" feeds the
  classifier but a **retrieved-but-irrelevant row can still influence a call** if
  it survives fusion (**VULN-I1, Medium**); and the retrieval path trusts the
  ingested ClinVar/coverage data without cryptographic source verification
  (**VULN-I2, Low**).

No Critical/High. The retrieval layer is well-built and honestly documented.
The realistic issues are **retrieval-induced over-trust (VULN-I1)** and **source
trust (VULN-I2)**. Top finding: **VULN-I1 — Medium.**

---

## 2. Scope of Testing

**System evaluated.** VariantLens retrieval pipeline: `retrieval/bm25.py`,
`retrieval/fusion.py`, `retrieval/metrics.py`, `agents/evidence_agent.py`, and
the FastAPI surface (`api.py`).

**Specialization.** IR + security: retrieval accuracy, retrieval manipulation,
hallucination due to retrieval, source reliability, authentication,
authorization, API security, communication-protocol security.

**Components evaluated.**
- Ranking/fusion (TF-IDF, BM25, RRF, max-normalisation, heuristic bonuses).
- Evidence metadata (`retrieval_strategy`, `evidence_source`, coverage-fallback
  and rsID caution flags).
- API surface: CORS, `verify_api_key`, quota/entitlement gating, error opacity.

**Out of scope.** Live red-teaming of a deployed instance, the LLM path, and
non-IR database internals.

**Limitations.** Documented offline metrics already acknowledge a
"degeneracy" — the self-retrieval comparison reaches 1.000 and is a sanity
check, not an accuracy result. This assessment relies on code review plus the
documented robustness framing.

---

## 3. Evaluation Methodology

**Criteria.**
1. **Retrieval accuracy** — does the right evidence rank first, and is the
   metric honest (non-circular)?
2. **Retrieval manipulation** — can a crafted query poison the ranking?
3. **Hallucination due to retrieval** — can a poor retrieval cause a wrong call?
4. **Source reliability** — is ingested evidence trustworthy/verifiable?
5. **Authentication / Authorization** — is retrieval access-controlled?
6. **API security** — CORS, error leakage, input handling.
7. **Communication-protocol security** — message envelope integrity between agents.

**Method.** Code review of the ranking and API layers, plus a runtime probe
(`audit/probe_s4.py`) that (a) runs a live retrieval and confirms the
`retrieval_strategy`/`evidence_source` metadata and RRF/top-k shape, and (b)
checks that an unauthenticated request is rejected and that CORS defaults are
restrictive. No live LLM cost.

**Severity scale.** Critical / High / Medium / Low / Informational by impact ×
likelihood, weighted by how much a bad retrieval can corrupt a clinical call.

---

---

## 4. Test Cases Performed

Evidence = `audit/probe_s4.py` output (Appendix D) + code citations. "Pass" = control holds.

| ID | Area | Scenario | Expected | Actual | Outcome |
|----|------|----------|----------|--------|---------|
| TC-01 | IR explainability | Retrieve a variant | Strategy + source reported | `ir_metadata` = "exact variant match + same-gene context + TF-IDF fallback"; source "study workbook" | Pass |
| TC-02 | IR accuracy metric | Self-retrieval comparison | Non-circular metric | Documented as degenerate sanity check (1.000) | Pass (honest) |
| TC-03 | Ranking robustness | One ranker misranks | RRF is robust | RRF k=60 documented as robust to single-model misranking | Pass |
| TC-04 | Ranking stability | Same query, different candidate set | Stable top hit | Hybrid uses batch-dependent max-normalisation | **VULN-I3** |
| TC-05 | Hallucination (retrieval) | Weak/no evidence query | No fabricated evidence | `found:False` path returns Unknown with no evidence | Pass |
| TC-06 | Hallucination (retrieval) | Irrelevant row survives fusion | Filtered or down-ranked | Heuristic bonuses; no semantic relevance floor | **VULN-I1** |
| TC-07 | Source reliability | Ingested ClinVar/coverage row | Verified provenance | No cryptographic source verification of ingest | **VULN-I2** |
| TC-08 | Source reliability | Coverage-fallback row | Flagged as lower-trust | `coverage_fallback` boolean surfaced | Pass |
| TC-09 | Source reliability | rsID unsupported | Caution flag set | `rsid_lookup_unsupported` surfaced | Pass |
| TC-10 | Auth | No API key | Rejected (401/403) | `verify_api_key` / dependency rejects | Pass |
| TC-11 | AuthZ | Non-entitled plan | Quota 429 before retrieval | Entitlement/quota gate precedes agents | Pass |
| TC-12 | API security | Default CORS | Restrictive | localhost origins only; no wildcard | Pass |
| TC-13 | API security | Error responses | No internal leakage | Generic error details; no stack/SQL echoed | Pass |
| TC-14 | Retrieval manipulation | Crafted query (prompt/SQL) | Reached IR only if sanitized | Sanitized upstream in Intake; IR sees structured fields only | Pass |
| TC-15 | Protocol security | Inter-agent message | Tamper-evident | Fernet-encrypted payload + trace IDs | Pass |
| TC-16 | Protocol security | Message replay/spoofing | Bounded | In-process envelope; no cross-service auth needed | Informational |

**Coverage.** 16 cases across all eight suggested areas: accuracy (TC-01/02),
manipulation (TC-14), hallucination (TC-05/06), source reliability (TC-07/08/09),
authentication (TC-10), authorization (TC-11), API security (TC-12/13),
protocol security (TC-15/16).

---

## 5. Vulnerabilities Identified

### VULN-I1 — Retrieval-induced over-trust (no semantic relevance floor)
- **Description.** A retrieved-but-poorly-relevant row can survive fusion
  (TF-IDF + BM25 + RRF + heuristic bonuses) and be fed to the classifier as
  "evidence". There is no hard relevance floor below which a candidate is
  discarded as non-evidence.
- **Evidence.** TC-06; `agents/evidence_agent.py` ranking + fusion.
- **Impact.** A weak or tangential row could nudge a classification, i.e. a
  retrieval error becomes a decision error (hallucination due to retrieval).
- **Likelihood.** Medium.
- **Severity: Medium.** Mitigated by the classifier treating many fields as
  supporting-only and by confidence being derived from evidence strength; but a
  spurious high-scoring candidate is not actively filtered.
- **Technical explanation.** Lexical rankers (TF-IDF/BM25) score term overlap,
  not meaning. Without a relevance gate or a semantic check, a term-overlapping
  but semantically wrong row can rank highly.

### VULN-I2 — Ingested evidence lacks cryptographic source verification
- **Description.** Retrieval serves rows ingested from ClinVar / a coverage
  index / the study workbook. Provenance is recorded as a **label**
  (`evidence_source`, `coverage_fallback`) but the ingest is not
  cryptographically verified against the upstream source.
- **Evidence.** TC-07; `evidence_source`/`coverage_fallback` metadata.
- **Impact.** A tampered or mis-labelled ingest would be served as trusted
  evidence; the "source" string is self-asserted.
- **Likelihood.** Low (requires write access to the data store).
- **Severity: Low.** Acceptable for an academic prototype over a curated
  dataset; a production system would pin/checksum upstream pulls.
- **Technical explanation.** Trusting a provenance *string* is not the same as
  verifying the data. Signed or checksummed ingest closes the gap.

### VULN-I3 — Hybrid ranking is batch-dependent (max-normalisation)
- **Description.** Score-fusion uses **max-normalisation**, so a candidate's
  hybrid score depends on the other candidates in the batch. RRF is
  batch-stable, but the score-fusion path is not.
- **Evidence.** TC-04; `retrieval/fusion.py` documents this explicitly as
  "offline comparison only".
- **Impact.** Result ordering can shift if the candidate set changes, hurting
  reproducibility of "top hit".
- **Likelihood.** Low.
- **Severity: Low.** The primary path uses RRF (stable); score-fusion is
  documented as offline-only.
- **Technical explanation.** Max-normalisation divides by the batch maximum,
  coupling each score to the batch. RRF avoids this by using ranks only.

---

## 6. Risk Assessment

| Vulnerability | Impact | Likelihood | Risk Level | Justification |
|---------------|--------|------------|------------|---------------|
| VULN-I1 Retrieval-induced over-trust | High (decision) | Medium | **Medium** | No relevance floor; mitigated by derived confidence + supporting-only fields |
| VULN-I2 Unverified source | Medium (trust) | Low | **Low** | Self-asserted provenance; acceptable over curated data |
| VULN-I3 Batch-dependent fusion | Low (reproducibility) | Low | **Low** | RRF is primary path; score-fusion offline-only |

**Aggregate.** No Critical/High. The retrieval layer is genuinely well-built:
explainable metadata, standard RRF, restrictive CORS, auth+quota gating, and
honest acknowledgement of the degenerate self-retrieval metric. The one Medium
— retrieval-induced over-trust — is the substantive IR finding to raise at the
viva.

---


## 7. Mitigation Strategies

1. **Add a relevance floor (closes VULN-I1).** Discard candidates below a tuned
   score threshold (or require a minimum term/semantic overlap) so a
   term-matching-but-irrelevant row never reaches the classifier as evidence.
   This directly prevents retrieval-induced hallucination.
2. **Verify ingest (VULN-I2).** Pin upstream ClinVar releases by version/date and
   store a checksum of each ingest; surface the pinned version as
   `evidence_source`. This turns a self-asserted string into verifiable
   provenance.
3. **Prefer RRF for the primary path (VULN-I3).** Keep score-fusion offline-only
   (as documented) and, if used online, switch to rank-based (RRF) fusion for
   batch-stable, reproducible top hits.
4. **Keep and harden the API controls.** Maintain restrictive CORS defaults
   (reject wildcards in production), the pre-retrieval auth + quota gate, and
   opaque error responses; add tests that fail if CORS ever defaults to `*`.
5. **Non-circular IR metrics.** Replace the self-retrieval sanity check with
   labelled queries and real relevance grades for any accuracy claim in the
   report, so the 1.000 is never presented as a result.

**Short term:** #1 and #5 have the most impact on the report's credibility.
**Strategic:** #2 makes the evidence chain auditable; #3 guarantees
reproducibility.

---

## 8. Reflection

**Challenges.** The hardest part was separating a real IR weakness from a
documented, deliberate trade-off. Max-normalisation being batch-dependent, and
self-retrieval being circular, are both *stated* in the code — so they are
honest limitations, not hidden bugs, and I graded them Low/Informational rather
than inflating them. The genuinely undocumented gap is the absence of a
relevance floor.

**Lessons learned.** (1) Explainability metadata (`retrieval_strategy`,
`evidence_source`, per-term BM25 explanations) is what makes an IR system
auditable — this one has it, and it materially changed how I could assess it.
(2) "Robust fusion" (RRF) is not the same as "correct retrieval"; robustness to
one bad ranker does not stop a lexically-similar but semantically-wrong row.
(3) Self-retrieval metrics are circular by construction; reporting them as
accuracy would be misleading.

**Recommendations.** Add a relevance floor, pin/checksum data ingest, use RRF
online, and build a labelled IR evaluation set so accuracy claims are
non-circular.

---

## Appendix D — Reproducible Probe & Sample Output

Run: `source /Users/sandali/projects/.venv/bin/activate` then
`python audit/probe_s4.py`. Script: `audit/probe_s4.py`.

```
P1 found: True
P1 strategy: exact variant match + same-gene context + TF-IDF fallback
P1 source: study workbook
P1 coverage_fallback: False
P1 rsid_lookup_unsupported: False
P1 top_k count: 5
P2 CORS restricted-by-default (no wildcard '*' in origins): True
P2 default origins: ['http://localhost:8000', 'http://127.0.0.1:8000', 'http://localhost:5173']
```

This evidences TC-01 (explainable strategy/source metadata), TC-08/09
(coverage-fallback and rsID-caution flags), and TC-12 (restrictive CORS).
VULN-I1/I2/I3 are code-reading findings cited from `agents/evidence_agent.py`,
`retrieval/fusion.py`, and `api.py`.

