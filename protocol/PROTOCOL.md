# Agent Communication Protocol

**File:** `protocol/message.py` · **Security:** `protocol/security.py` · **Transport layer:** `api.py`

This document is the single reference for how the four agents in this system talk
to each other. It is written to be readable on its own during the viva.

---

## 1. Why a protocol at all?

The system is a **multi-agent** system, so the rubric requires a *defined*
communication protocol. The design rule is simple and enforced everywhere:

> Agents never read each other's internal state. Every hand-off is an
> `AgentMessage` object, and every payload is encrypted before it leaves an agent.

This is the same envelope-style contract used by MCP (Model Context Protocol) and
A2A (Agent2Agent) — a standard metadata header plus an opaque payload — implemented
here over an in-process transport and exposed externally over HTTP.

---

## 2. The `AgentMessage` envelope

Defined as a `@dataclass` in `protocol/message.py`.

| Field | Type | Required | Default | Purpose |
|---|---|---|---|---|
| `variant_id` | `str` | ✅ | — | Subject of the message, e.g. `BRCA1:NC_000017.10:g.41197778A>G` |
| `stage` | `str` | ✅ | — | Pipeline stage label (`intake_complete`, `evidence_retrieved`, `classification_complete`, `report_complete`) |
| `sender` | `str` | ✅ | — | Agent that produced the message (`intake_agent`, `evidence_retrieval_agent`, …) |
| `receiver` | `str` | | `""` | Intended recipient agent; `""` means broadcast/orchestrator |
| `message_type` | `str` | | `SYSTEM_LOG` | Semantic type — see §3 |
| `trace_id` | `str` | | 12-char UUID | End-to-end ID shared by **every** message in one pipeline run |
| `message_id` | `str` | | 8-char UUID | Unique ID for this single message |
| `timestamp` | `str` | | ISO-8601 UTC | Creation time, `datetime.now(timezone.utc).isoformat()` |
| `encrypted` | `bool` | | `False` | `True` once the payload has been encrypted in transit |
| `payload` | `dict` | | `{}` | Structured data (or `{"__encrypted__": "<Fernet token>"}` when encrypted) |

The `variant_id` / `trace_id` pair gives two levels of traceability: *what* was
asked, and *which run* asked it.

### Fluent builder helpers

| Method | Effect |
|---|---|
| `.to(receiver)` | Set the intended recipient |
| `.with_type(message_type)` | Set the semantic message type |
| `.with_trace(trace_id)` | Propagate the run's `trace_id` |
| `.encrypt_payload()` | Encrypt `payload` in place; sets `encrypted = True` |
| `.decrypt_payload()` | Return the plain `dict` (decrypting if needed) |
| `.to_json()` | Serialise metadata + payload (numpy-safe) |

---

## 3. Message types (semantic vocabulary)

| Constant | Value | Meaning |
|---|---|---|
| `MessageType.VARIANT_QUERY` | `VARIANT_QUERY` | Intake → Evidence: a cleaned, structured variant request |
| `MessageType.EVIDENCE_RESULT` | `EVIDENCE_RESULT` | Evidence → Classification: retrieved + ranked evidence |
| `MessageType.CLASSIFICATION_RESULT` | `CLASSIFICATION_RESULT` | Classification → Report: label, confidence, criteria |
| `MessageType.REPORT_RESULT` | `REPORT_RESULT` | Report → Orchestrator: final narrative + disclaimer |
| `MessageType.EVIDENCE_ERROR` / `CLASSIFICATION_ERROR` / `REPORT_ERROR` | … | Explicit failure signalling per stage |
| `MessageType.QUERY_ACK` | `QUERY_ACK` | Acknowledgement |
| `MessageType.SYSTEM_LOG` | `SYSTEM_LOG` | Default for non-pipeline/system messages |

---

## 4. Security guarantees on the wire

| Guarantee | Mechanism |
|---|---|
| **Confidentiality in transit** | Payload JSON is encrypted with **Fernet** (AES-128-CBC + HMAC-SHA256) by `MessageEncryptor` in `protocol/security.py` before leaving an agent |
| **Integrity / tamper detection** | Fernet's HMAC — a modified token fails to decrypt (`InvalidToken`) |
| **Authentication** | API key (SHA-256 hashed) verified by `authenticate()` **before** the Intake Agent runs |
| **Input sanitisation** | `sanitize_query()` rejects SQL/script patterns, over-length input, and prompt-injection phrasing before any agent or LLM sees it |
| **Auditability** | Every message is appended to `MessageLog` and retrievable by `variant_id` or `trace_id` |

Encryption is applied at the *serialisation boundary*, so an agent's Python object
is never shared; only a Fernet token travels.

### Numpy-safe serialisation

Evidence bundles originate from pandas DataFrames, so payloads contain
`numpy.int64` / `numpy.float64` / `numpy.ndarray` values that `json.dumps` cannot
encode. `NumpyJSONEncoder` converts them to native Python types so
`encrypt_payload()` can never fail on a valid record.


---

## 5. One complete message flow

Query: `Is BRCA1 NC_000017.10:g.41197778A>G pathogenic?`

```
User query
   │
   ▼  [API authentication — protocol/security.py]
Agent 1: Intake Agent            sender=intake_agent
   │   type=VARIANT_QUERY  stage=intake_complete
   │   to=evidence_retrieval_agent   encrypted=True
   ▼
Agent 2: Evidence Retrieval      sender=evidence_retrieval_agent
   │   type=EVIDENCE_RESULT  stage=evidence_retrieved
   │   to=classification_agent  encrypted=True
   ▼
Agent 3: Classification          sender=classification_agent
   │   type=CLASSIFICATION_RESULT  stage=classification_complete
   │   to=report_agent  encrypted=True
   ▼
Agent 4: Report                  sender=report_agent
   │   type=REPORT_RESULT  stage=report_complete
   │   to=orchestrator  encrypted=True
   ▼
Final report (text + JSON + PDF)
```

### Message 1 — Intake → Evidence

```json
{
  "message_id": "e3c9f78e",
  "trace_id": "9f2b1c7a4d10",
  "sender": "intake_agent",
  "receiver": "evidence_retrieval_agent",
  "message_type": "VARIANT_QUERY",
  "variant_id": "BRCA1:NC_000017.10:g.41197778A>G",
  "stage": "intake_complete",
  "timestamp": "2026-09-30T10:39:40.813146+00:00",
  "encrypted": true,
  "payload": { "__encrypted__": "gAAAAABm... (Fernet token)" }
}
```

Decrypted payload (what the Evidence Agent reads after `decrypt_payload()`):

```json
{
  "gene": "BRCA1",
  "hgvs": "NC_000017.10:g.41197778A>G",
  "position": null,
  "rsid": null,
  "query_type": "gene_variant",
  "intent": "classification"
}
```

### Messages 2–4 — same envelope, different `message_type` / `stage`

| # | sender → receiver | message_type | stage |
|---|---|---|---|
| 2 | `evidence_retrieval_agent` → `classification_agent` | `EVIDENCE_RESULT` | `evidence_retrieved` |
| 3 | `classification_agent` → `report_agent` | `CLASSIFICATION_RESULT` | `classification_complete` |
| 4 | `report_agent` → `orchestrator` | `REPORT_RESULT` | `report_complete` |

All four messages share the **same `trace_id`**, which is what makes the audit
trail reconstructable (`MessageLog.trail_for(trace_id)`).

---

## 6. Audit trail

```python
class MessageLog:
    def record(self, message: AgentMessage): ...      # append on every hand-off
    def history_for(self, variant_id: str): ...       # all messages for one variant
    def trail_for(self, trace_id: str): ...           # all messages for one pipeline run
    def print_trail(self, variant_id: str): ...       # human-readable console dump
```

Exposed over HTTP (authentication required):

| Endpoint | Returns |
|---|---|
| `GET /api/trail/{variant_id}` | Ordered list of every agent message (metadata + decrypted payload) |
| `GET /api/pipeline/{variant_id}` | Full pipeline result with the same per-message detail |

---

## 7. External transport (HTTP / REST)

`api.py` wraps the in-process protocol for external clients, which is the
"API-based" protocol option in the brief:

| Method | Path | Purpose | Auth |
|---|---|---|---|
| `GET` | `/` | Web UI | — |
| `GET` | `/health`, `/healthz` | Health check | — |
| `POST` | `/api/classify` | Run the 4-agent pipeline | API key |
| `POST` | `/api/report/pdf` | Print-ready PDF of a report | — |
| `POST` / `GET` | `/api/review/{variant_id}` | Save / fetch human review sign-off | — |
| `GET` | `/api/pipeline/{variant_id}` | Full pipeline + audit detail | `X-API-Key` |
| `GET` | `/api/trail/{variant_id}` | Agent-to-agent audit trail | `X-API-Key` |

Authentication is performed with the `X-API-Key` header (or the `api_key` body
field for `/api/classify`):

```
POST /api/classify
X-API-Key: demo-clinician-key
Content-Type: application/json

{ "query": "Is BRCA1 NC_000017.10:g.41197778A>G pathogenic?" }
```

Keys are registered as SHA-256 digests (`_REGISTERED_API_KEYS`), so a raw key is
never stored in memory. Demo key: `demo-clinician-key` → user `clinician_demo_user`.

---

## 8. Design notes / limitations

- Agents run **in the same process** for this prototype; the protocol is
  transport-agnostic, so moving each agent behind its own HTTP/MCP endpoint would
  not change any message schema.
- The demo API key is a **prototype credential**, not a production identity
  provider; a production deployment would use OAuth 2.0 / OIDC with per-user
  scopes.
- Fernet uses a per-process key for the demo, so messages are not decryptable
  across restarts — intentional for the academic prototype, documented in
  `COMPLIANCE_REGULATORY_POSITIONING.md`.
