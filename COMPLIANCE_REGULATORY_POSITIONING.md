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
| Security | API key, input validation, encrypted agent messages, audit trail | Managed identity, secrets management, TLS, least privilege, threat modelling and penetration testing |
| Privacy | No real patient data required for the demo | Privacy impact assessment, access controls, retention policy and applicable health-data safeguards |
| Human oversight | Reports explicitly require professional review | Defined review workflow, escalation criteria and documented accountability |
| Software quality | Automated unit tests and evaluation notebook | Verification/validation plan, traceability, release controls and post-market monitoring where applicable |

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

## Known prototype limitations

The security audit intentionally records limitations that should **not** be hidden for the sake of commercialization. These include the classroom demo authentication model (a single shared demo key with no per-user quota or rate limiting), permissive CORS configuration, process-local Fernet key generation, and approximate retrieval flowing through the pipeline.

The appropriate commercialization path is therefore **validation and controlled hardening first**, rather than presenting the current prototype as clinically deployable.

## Commercialization roadmap

**Stage 1 — Research prototype:** demonstrate IR, agent orchestration, explainability, security testing and evaluation.

**Stage 2 — Controlled validation:** establish curated reference sets, expert-reviewed labels, reproducible evaluation, model/version governance and formal risk management.

**Stage 3 — Security and privacy hardening:** managed secrets, production authentication/authorization, TLS, least privilege, monitoring, logging controls and threat modelling.

**Stage 4 — Clinical validation and regulatory assessment:** define intended use, clinical performance, usability, human factors and jurisdiction-specific regulatory pathway.

**Stage 5 — Controlled deployment:** deploy only within the validated intended use with monitoring, incident response, change control and periodic evidence/database updates.

> **Positioning statement:** The project demonstrates a technically structured foundation for future clinical research. It does not claim regulatory approval, clinical validity, diagnostic capability or production readiness.
