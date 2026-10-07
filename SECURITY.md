# Security policy

VariantLens is a clinical *decision-support* tool. It stores accounts, query
history and an audit trail, and it sends variant identifiers to a third-party
language model. It does not store patient records, and it must not be pointed at
any. This document says what is implemented, where the code for it lives, and
what is deliberately **not** covered.

## Reporting a vulnerability

Use GitHub's private reporting on the repository: **Security → Report a
vulnerability → Report privately**. That opens a draft advisory visible only to
the maintainers, so a working exploit is never posted in a public issue.

Please include the request or input that reproduces it and what you expected
instead. Do not open a public issue for anything exploitable, and do not test
against an instance you do not own or have written permission to test.

There is no bug-bounty programme and no formal response-time commitment. This is
a project being prepared for commercialisation, and security reports are taken
seriously on that basis, not as an SLA.

## What this system handles

| Data | Where it lives | Sensitivity |
| --- | --- | --- |
| Account credentials | `users` table (Argon2id digests) | Secret — never logged, never returned |
| Session tokens | HttpOnly cookie / `Authorization` header | Secret — encrypted envelope, 60 min default |
| API keys | `api_keys` table (SHA-256 digest + visible prefix) | Secret — plaintext shown once at issue |
| Query history | `query_history` table, per account | Confidential to that account |
| Audit events | `auth_events` table | Confidential; identifiers are keyed hashes |
| Variant data | Public (ClinVar). Sent to Gemini on LLM-enabled plans | Not personal data |

**No protected health information.** The input is a variant identifier
(`BRCA1 c.5977T>C`), not a person. Nothing in the request schema carries a name,
date of birth, MRN or free-text clinical note, and the free-text query field is
sanitised and structurally validated as a variant question before it reaches any
agent (`protocol/security.py` → `sanitize_query`, `validate_gene`). If you build
a deployment that attaches patient identifiers to queries, the compliance
position changes completely and this document no longer describes it.

## Implemented controls

### Authentication and sessions

- **Password hashing — Argon2id.** `auth/security.py` uses `pwdlib`'s
  recommended configuration. Plaintext passwords are never stored, logged or
  returned. Hashing is deliberately slow, which is why the login throttle below
  exists.
- **Encrypted tokens — JWE-wrapped JWS.** PyJWT signs; `jwcrypto` wraps the
  signed token in AES-256-GCM. A token is therefore both tamper-evident and
  unreadable to anyone who intercepts it but does not hold `JWT_ENCRYPTION_KEY`.
  Required claims (`sub`, `exp`, `iat`, `iss`, `aud`) are enforced on decode.
- **Refuses to start without secrets.** `auth/security.py` raises
  `ConfigurationError` at import time if `JWT_SECRET_KEY` or
  `JWT_ENCRYPTION_KEY` is unset — unless `JWT_ALLOW_DEV_SECRET=true` is set
  explicitly, which exists for the test suite and local demos only. A published
  fallback key would let anyone mint an administrator token, so the default
  posture is to fail closed.
- **HttpOnly session cookie, not localStorage.** `auth/cookies.py`. A token in
  localStorage is readable by any injected script, so a single XSS bug exfiltrates
  a working session. `Secure` is on by default; `SameSite=Lax` still blocks
  cross-site POSTs. Bearer tokens keep working for scripted clients.
- **CSRF double-submit, only where it applies.** The same module. A readable
  CSRF cookie is echoed in `X-CSRF-Token`; the comparison uses
  `hmac.compare_digest`. It is enforced **only** on state-changing requests
  authenticated *by cookie*, because a bearer token is never attached
  ambiently and cannot be ridden by a third-party page.
- **Login throttling.** `auth/rate_limit.py` keeps two sliding windows — per
  client IP and per account identifier. Both are needed: IP alone lets a botnet
  walk one account, identifier alone lets one attacker lock out every user by
  cycling usernames. The check runs *before* password verification, so a blocked
  caller costs no Argon2id work, and a correct password clears the counter.
- **Deactivation is immediate.** `auth/dependencies.py` re-reads the account on
  every request, so a deactivated user loses access with an unexpired token still
  in hand.
- **API keys are digests, not secrets at rest.** `services/api_key_service.py`
  stores a SHA-256 digest of a 256-bit `secrets.token_urlsafe` value and shows
  the plaintext exactly once. A database leak does not hand out working
  credentials.

### Authorisation

- Every query-scoped endpoint filters on the caller's own user id — history,
  exports and API keys are not enumerable across accounts.
- Administrator routes depend on a role check in `auth/dependencies.py`; the
  role is read from the database row, not from the token body.
- Plan entitlements (`api_access`, `audit_export`, LLM on/off) are resolved
  server-side from the plan table. The client is never trusted to declare its own
  entitlements.

### Input handling and the LLM boundary

- **Query sanitisation.** Only `BRCA1`/`BRCA2` are accepted and HGVS notation is
  parsed structurally (`protocol/security.py`). Malformed input is rejected with
  `InvalidVariantQuery` before any agent sees it.
- **Prompt-injection filtering.** A pattern set covering instruction-override,
  system-prompt exfiltration and role-hijacking framing runs on the raw query
  before it reaches an LLM agent. This reduces a real attack surface; it is not
  a guarantee, which is why the LLM's output is schema-validated and reconciled
  against ClinVar rather than trusted.
- **SQL injection.** All persistence goes through SQLAlchemy Core/ORM with bound
  parameters. There is no string-built SQL in the application code.

### Audit trail

`auth/audit.py` records sign-in, sign-out, failed sign-in, password change,
plan change, key issue/revoke and administrative reads in `auth_events`. Two
deliberate choices:

- **Auditing never breaks the operation it describes.** A failed audit write is
  swallowed and logged, because an insert that can turn a successful sign-in into
  a 500 is itself a denial-of-service primitive.
- **Identifiers are keyed hashes.** A plain SHA-256 of an email address is
  reversible by hashing a wordlist, and this table holds attempts against
  addresses that may belong to nobody. The digest is salted with the JWT signing
  key, so it cannot be computed without a secret the database never receives.

### Agent-to-agent transport

- Agent payloads are encrypted with Fernet before being passed between stages
  (`protocol/message.py`, `protocol/security.py`) — see the limitation below.
- The in-memory message log is shared by every request in the process and is
  keyed by variant_id only. `/api/classify` returns the current run's events
  directly; `/api/trail/{variant_id}` and `/api/pipeline/{variant_id}` return
  only messages whose trace_id the caller owns (stored on their
  `query_history` rows at classify time, `api._history_for_user`). A variant
  the caller never ran -- or a run predating the trace_id column -- reads as
  404. The log itself is unbounded, process-local and lost on restart
  (limitation 11).

## Known limitations

These are real and are stated rather than implied away. Each one is a deployment
task, not a bug report.

1. **The Fernet key is generated per process.** `MessageEncryptor.__init__`
   calls `Fernet.generate_key()` when no key is passed, so agent-message
   encryption is *in-process* confidentiality, not a managed secret: the key does
   not survive a restart and is identical across every worker started from the
   same parent only by accident. Give it a real key from the environment before
   any multi-process deployment.
2. **A hard-coded demo API key still exists.** `protocol/security.py` holds
   `demo-clinician-key` for the agent-layer check used by the CLI path
   (`main.py`). No HTTP route reaches it — `verify_api_key` in `api.py` is
   explicitly unused — but the CLI entry point still authenticates through that
   static key. Replace it with the account system or delete the CLI path.
3. **Rate limiting is per worker.** `auth/rate_limit.py` keeps counters in
   process memory, which is correct for the single-process uvicorn this project
   runs. Behind several workers the effective limit multiplies by the worker
   count; a shared store (Redis) is required.
4. **No multi-factor authentication, no session revocation list.** A stolen but
   unexpired token is valid until it expires (60 minutes by default) unless the
   account is deactivated. There is no "sign out everywhere".
5. **CORS defaults are local-only.** `api.py` allows
   `localhost:8000`/`127.0.0.1:8000`/`localhost:5173` unless
   `FRONTEND_ORIGINS` is set. A deployment that forgets this ships a broken
   frontend rather than an open one — deliberately, but it is still configuration
   that must be done.
6. **TLS is not terminated by this application.** `Secure` cookies assume
   HTTPS. Terminating TLS (and setting `TRUST_PROXY_HEADERS` only behind a proxy
   that actually sets those headers) is the deployment's responsibility.
7. **Variant data leaves the deployment on LLM-enabled plans.** On those plans,
   evidence for the query is sent to Google's Gemini API. The payload is variant
   annotation and ClinVar labels, not personal data, but it is a data-egress path
   that a compliance review must know about. Plans with LLM off never make the
   call.
8. **No dependency scanning gate.** `pip-audit` runs in CI as an advisory job
   (see `.github/workflows/ci.yml`); it does not fail the build.
9. **Frontend CSP is not set.** The server does not send a
   `Content-Security-Policy` header. The app is three static files with no
   third-party scripts, so a strict policy is straightforward to add — it simply
   has not been.
10. **The `analysis_results/` directory and the notebook are not part of the
    served surface.** They are excluded from the image by `.dockerignore`; keep
    it that way.
11. **The agent message log is in-memory, unbounded and per-process.**
    `MessageLog` in `protocol/message.py` grows without bound and is lost on
    restart. A production deployment needs a bounded, persisted trail store
    (with retention) rather than this prototype log.
12. **TF-IDF caches are pickles and version-sensitive.** `data/*_tfidf_cache.pkl`
    files were built with a newer scikit-learn than the pinned `1.8.0` (see
    `ANALYSIS_GUIDE.md`), which surfaces an `InconsistentVersionWarning` on
    load. Unpickling is also an unsafe-deserialization surface: never load a
    cache file from an untrusted source. Rebuild with
    `scripts/enrich_clinvar_labels.py` (or delete the cache and let it rebuild)
    after changing the scikit-learn version.

## Before handling real patient data

Not a checklist this project claims to have completed. It is what a reviewer
would ask for:

- A named data controller and a lawful basis; a DPIA if you are in the EU/UK.
- A signed business-associate agreement with any model provider, and a
  no-training-on-submitted-data guarantee.
- A managed secret store for `JWT_SECRET_KEY`, `JWT_ENCRYPTION_KEY` and the
  database URL — never `.env` on disk in production.
- A shared rate-limit store, MFA for administrator accounts, and session
  revocation.
- Encryption at rest for the database and backups, plus a retention policy for
  `query_history` and `auth_events`.
- A documented clinical-review workflow, which this repository has in
  `REVIEW_SIGNOFF_WORKFLOW.md` and `COMPLIANCE_REGULATORY_POSITIONING.md`.

## Dependencies

Runtime dependencies are pinned in `requirements-api.txt` (lean server set) and
`requirements.txt` (adds the notebook stack). `pip-audit` runs against the
runtime set in CI on every change. There is no automated update bot; versions
move by hand.
