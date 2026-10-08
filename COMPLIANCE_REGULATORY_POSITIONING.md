# Compliance & Regulatory Positioning

## Purpose

The Genomic Variant Interpretation Agent is an **academic research prototype** demonstrating information retrieval, multi-agent communication, explainable classification, NLP summarization, security controls, and responsible-AI design for BRCA1/BRCA2 variant interpretation.

It is **not represented as a clinically validated, regulated medical device or production clinical decision-support product**.

## Intended positioning

| Area | Current prototype position | Production requirement |
|---|---|---|
| Intended use | Academic demonstration and research | Formal intended-use statement and risk analysis |
| Data | Public ClinVar-derived variant records | Validated data governance, provenance and update controls |
| Clinical validity | Simplified ACMG-style project rules | Expert-reviewed ACMG/AMP/ClinGen methodology and validation |
| AI/LLM | Optional Gemini-assisted reasoning and report generation | Model validation, version control, monitoring, change control and safety testing |
| Security | Account authentication (Argon2id + JWT) with an agent-layer API key, role-based access control, plan feature gating, scoped revocable API keys, monthly quota enforcement, input validation, encrypted agent messages, audit trail and a governed audit export | Managed identity, secrets management, TLS, least privilege, threat modelling and penetration testing |
| Privacy | No real patient data required for the demo | Privacy impact assessment, access controls, retention policy and applicable health-data safeguards |
| Human oversight | Reports explicitly require professional review | Defined review workflow, escalation criteria and documented accountability |
| Software quality | Automated unit tests (Python and Node), CI on every push, a container image and an evaluation notebook | Verification/validation plan, traceability, release controls and post-market monitoring where applicable |

## Relevant regulatory / governance areas

A future clinical product would need a jurisdiction-specific regulatory assessment. Depending on intended use, deployment model and claims, relevant frameworks may include:

- **FDA Software as a Medical Device (SaMD) / clinical decision-support considerations** for the United States.
- **EU Medical Device Regulation (MDR)** and applicable software classification requirements for the European Union.
- **UK medical-device requirements** where the product is supplied in the United Kingdom.
- **Local health-data, privacy and cybersecurity requirements** in each deployment jurisdiction.
- **ACMG/AMP and ClinGen guidance** for the scientific interpretation methodology rather than as a software certification.

These frameworks should be assessed by qualified regulatory, legal and clinical professionals before commercialization.

## Safety controls demonstrated in the prototype

1. **Evidence traceability:** ranked evidence records and retrieval rationale are exposed to the user.
2. **Confidence transparency:** confidence is derived from evidence components and is explicitly distinguished from medical certainty.
3. **Conflict handling:** direction-aware — a genuine conflict (ClinVar `Conflicting`
   consensus) lowers confidence, while a Pathogenic-vs-Likely-Pathogenic split is
   treated as agreement rather than conflict.
4. **VUS safeguards:** VUS is not presented as high-confidence output.
5. **Human review:** reports contain a clinical disclaimer and reviewer/sign-off fields.
6. **Security audit:** the project includes an independent security assessment documenting prototype limitations.
7. **Reproducibility:** evaluation scripts, analysis figures, tests and a pinned dependency set are included.
8. **Self-verification:** a Verifier Agent audits the classification against the evidence it
   rests on and re-states an over-confident call conservatively before it is reported. The
   correction is conservative by construction — it can only lower confidence, never raise it —
   and bounded to one round. Both the original and the corrected call remain in the audit trail.
9. **Credential scoping and export control:** programme access uses revocable, plan-bound API
   keys stored only as digests, and audit export is an entitlement-limited, caller-scoped
   endpoint — so access to activity records is itself governed.

## Known prototype limitations

The security audit intentionally records limitations that should **not** be hidden for the sake of commercialization. The account layer has since moved well past the original classroom demo: PostgreSQL accounts with Argon2id hashing, signed-then-encrypted (JWE) tokens, HttpOnly session cookies with CSRF double-submit protection, per-IP and per-account login throttling, role-based access control, per-plan feature gating with monthly quota enforcement and row-locked commit-time re-checks, hashed-identifier auth-event auditing, plan-bound scoped API keys, an entitlement-gated audit export, and CORS restricted to explicit origins.

What remains a genuine prototype limitation is the operational and integration surface, not the authentication model:

- **Process-local encryption keys** — the Fernet key protecting agent-to-agent payloads is generated per process; a multi-process deployment would need a managed key.
- **In-process rate-limit counters** — login throttling and the result cache live in the process, so a multi-worker deployment needs a shared store (e.g. Redis).
- **Approximate retrieval and simplified reasoning** — ranking is TF-IDF-based and the ACMG-style rules are simplified project rules, not expert-reviewed ACMG/AMP/ClinGen.
- **No payment integration and no organisation tenancy** — plan changes are an administrator-reviewed hand-off and every account is a single user, by design.
- **No production observability** — no managed secrets, centralised structured logging, metrics or error tracking.

The appropriate commercialization path is therefore **validation and controlled hardening first**, rather than presenting the current prototype as clinically deployable.

## Commercialization roadmap

**Stage 1 — Research prototype:** demonstrate IR, agent orchestration, explainability, security testing and evaluation.

**Stage 2 — Controlled validation:** establish curated reference sets, expert-reviewed labels, reproducible evaluation, model/version governance and formal risk management.

**Stage 3 — Security and privacy hardening:** managed secrets, production authentication/authorization, TLS, least privilege, monitoring, logging controls and threat modelling.

**Stage 4 — Clinical validation and regulatory assessment:** define intended use, clinical performance, usability, human factors and jurisdiction-specific regulatory pathway.

**Stage 5 — Controlled deployment:** deploy only within the validated intended use with monitoring, incident response, change control and periodic evidence/database updates.

> **Positioning statement:** The project demonstrates a technically structured foundation for future clinical research. It does not claim regulatory approval, clinical validity, diagnostic capability or production readiness.
