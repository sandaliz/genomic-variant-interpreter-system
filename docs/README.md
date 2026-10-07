# Documentation

Longer-form material that would overload the README. Everything here is meant to
be readable on its own; diagrams are Mermaid, so they render on GitHub and in
most editors.

| Document | What it answers |
| --- | --- |
| [architecture.md](architecture.md) | What the components are, how a query flows through them, and where the agentic behaviour actually lives |
| [sequence.md](sequence.md) | The same flow as a sequence diagram, including the revision loop and the HTTP-layer steps |
| [data-model.md](data-model.md) | The nine tables, what each one owns, and the deletion/retention rules |
| [retrieval.md](retrieval.md) | How evidence is retrieved and ranked: TF-IDF, BM25, hybrid fusion, and the metrics that score them |

Design documents that predate this directory and are still current:

| Document | Where |
| --- | --- |
| The encrypted agent protocol and message schema | [../protocol/PROTOCOL.md](../protocol/PROTOCOL.md) |
| Accounts, sessions, cookies and the audit trail | [../AUTH.md](../AUTH.md) |
| The clinical review and sign-off workflow | [../REVIEW_SIGNOFF_WORKFLOW.md](../REVIEW_SIGNOFF_WORKFLOW.md) |
| Regulatory positioning and compliance posture | [../COMPLIANCE_REGULATORY_POSITIONING.md](../COMPLIANCE_REGULATORY_POSITIONING.md) |
| Security policy and known limitations | [../SECURITY.md](../SECURITY.md) |

The full technical report, with the evaluation numbers, is
[../PROJECT_REPORT.md](../PROJECT_REPORT.md).

## Conventions used in these documents

- **"Stage"** means one `AgentMessage` hop in the pipeline. Every stage writes
  one encrypted message into the log, which is what the audit trail is built
  from.
- **"Evidence"** is the retrieved, structured record set a classification rests
  on — not the raw dataset row.
- **"Confidence"** is always the 0–100 numeric score derived from evidence
  components. The categorical label (`low`/`moderate`/`high`) is a pure function
  of that number and is never stored or reported independently of it.
