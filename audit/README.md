# AI Security Audit — Individual Vulnerability Assessment (IT 3041)

This folder holds the **individual AI Security Audit** deliverables for the
follow-up assignment to the VariantLens group project. Each student acts as an
AI Security Analyst (Red Team member) and independently assesses the *same*
system from an assigned specialization.

> **Purpose of the system under test.** VariantLens is an agentic genomic
> variant-interpretation system (Intake → Evidence → Classification → Report,
> with an optional Gemini LLM, TF-IDF/BM25/RRF retrieval, and a FastAPI +
> JWT/API-key product layer). Every student is expected to understand the whole
> architecture even while specialising in one area.

## Reports

| File | Student specialization | Focus areas (from the brief) |
|------|------------------------|------------------------------|
| `student1_prompt_injection_report.md` | Prompt Injection & Jailbreak | Injection, jailbreak, prompt leakage, instruction override, manipulation, robustness |
| `student2_privacy_report.md` | Privacy & Data Leakage | Sensitive/PII leakage, memory leakage, auth weaknesses, data protection, privacy compliance |
| `student3_responsible_ai_report.md` | Responsible AI & Bias | Hallucinations, bias, toxicity, fairness, transparency, explainability, harmful content |
| `student4_ir_security_report.md` | Information Retrieval & Security | Retrieval accuracy/manipulation, retrieval-hallucination, source reliability, authn/authz, API & protocol security |

Each report follows the required structure (Executive Summary → Scope →
Methodology → Test Cases → Vulnerabilities → Risk Matrix → Mitigation →
Reflection → Evidence) and contains **16 test cases**, above the 15 minimum.


## Reproducible probes

Each report's evidence appendix cites a runnable probe. All probes are **hermetic**
(no network, no live LLM, no database needed) and read the committed code.

python audit/probe_s1.py     # Student 1: sanitizer + injection (16 cases)
python audit/probe_s1b.py    # Student 1: NER containment of sanitizer bypasses
python audit/probe_s2.py     # Student 2: token opacity, Argon2id, keyed pseudonym
python audit/probe_s3.py     # Student 3: derived confidence, VUS-never-high, hallucination verifier
python audit/probe_s4.py     # Student 4: retrieval metadata, RRF shape, CORS posture
```

| Probe | Report | What it demonstrates |
|-------|--------|----------------------|
| `probe_s1.py` | Student 1 | 11 attacks blocked; TC-07/TC-10 bypass sanitizer; TC-13/14/16 false positives |
| `probe_s1b.py` | Student 1 | Bypassed text is contained by Intake NER (no valid entity → rejected) |
| `probe_s2.py` | Student 2 | Encrypted JWE token (opaque, no email/plan), Argon2id salted hashes, keyed audit pseudonym |
| `probe_s3.py` | Student 3 | VUS → low confidence (16.0), label derived from number, fairness + disclaimer present, hallucinated report caught |
| `probe_s4.py` | Student 4 | Retrieval returns named strategy + source metadata; CORS restricted to localhost (no wildcard) |

## Findings at a glance

| ID | Finding | Severity | Report |
|----|---------|----------|--------|
| VULN-P1 | Paraphrased instruction override bypasses denylist (contained by NER) | Medium | Student 1 |
| VULN-P2 | SQL tautology not filtered at query layer (no exploit — parameterised queries) | Low | Student 1 |
| FP-1/2/3 | Legitimate clinical queries wrongly blocked (`;`, `--`, `act as…`) | Low | Student 1 |
| VULN-D1 | Stateless tokens not revoked on logout / password change | Medium | Student 2 |
| VULN-D2 | Published demo API key (`demo-clinician-key`) | Low/Info | Student 2 |
| VULN-D3 | Audit trail is a re-identifiable activity profile | Low | Student 2 |
| VULN-R1 | Fairness limitation disclosed but not measured | Medium | Student 3 |
| VULN-R2 | LLM narrative verified for label/number/disclaimer, not facts | Medium | Student 3 |
| VULN-R3 | Low-confidence numeric calls still shown as bare numbers | Low | Student 3 |
| VULN-I1 | No semantic relevance floor → retrieval-induced over-trust | Medium | Student 4 |
| VULN-I2 | Ingested evidence lacks cryptographic source verification | Low | Student 4 |
| VULN-I3 | Hybrid score-fusion is batch-dependent (RRF is primary path) | Low | Student 4 |

No **Critical** or **High** findings. The system is genuinely strong (Argon2id,
encrypted tokens, fail-loud secrets, derived confidence, explainable retrieval,
restrictive CORS, auth + quota gating); the residual risks are token lifecycle,
unmeasured fairness, unverified narrative, and a missing relevance floor.


## Marking & what each student must still do

The audit is **80 marks (report) + 20 marks (individual viva)**. The rubric
rewards methodology & coverage (20), quality of vulnerability analysis (25),
risk assessment & severity classification (15), mitigation strategies (10), and
report quality/evidence/technical writing (10).

These reports are **code-verified drafts**, not finished individual submissions.
Before the viva, each student must:

1. **Re-run their probe** and paste the real output (the appendices already show
   a representative run).
2. **Capture screenshots/logs** as the brief requires — a probe transcript plus
   one UI/API screenshot per finding is ideal.
3. **Insert their name** and personalise the wording (the reports share a voice
   on purpose; the viva checks *individual* understanding).
4. **Own every severity call** — be ready to justify each level on impact ×
   likelihood, and to explain why bypasses succeeded or were contained.

## Status

All four reports and probes exist and run cleanly against the committed code
(`pytest tests/ -q` still passes at 652 — the audit added no source changes).
Nothing here is committed; review, personalise, then commit as a group.

