# AI Vulnerability Assessment — Privacy & Data Leakage

**System:** VariantLens (Genomic Variant Interpretation Agent) — IT 3041
**Specialization:** Student 2 — Privacy and Data Leakage Assessment

> **Draft note.** Facts below were read from the committed code
> (`auth/security.py`, `auth/audit.py`, `db/database.py`, `api.py`). Re-verify,
> add your own screenshots, insert your name, and own every severity call at
> the viva.

---

## 1. Executive Summary

VariantLens is a clinical decision-support web app with user accounts, per-user
analyses, an audit trail, and subscription plans. For a privacy audit the
questions are: is authentication sound, are secrets handled correctly, is user
data (queries, analyses, audit records) protected at rest and in transit, is PII
minimised, and does the audit log itself avoid becoming a new leak?

I reviewed the auth stack, the token design, the password handling, the audit
pseudonymisation, and the data paths. Findings:

- **Strengths (verified in code):** passwords are **Argon2id**-hashed; login is
  **timing-safe** against account enumeration (a dummy hash is verified for
  unknown emails); access tokens are **encrypted (JWE)**, not merely signed, and
  carry only identity/role — never email or plan; the app **fails loudly** if
  JWT secrets are unset rather than silently booting on defaults; and the audit
  trail stores a **keyed pseudonym** (SHA-256 salted with the signing secret),
  not a reversible identifier.
- **Weaknesses found:** tokens are **stateless and not revocable** — logout
  clears the cookie but a stolen token stays valid until it expires, and
  **changing a password does not revoke tokens already issued** (**VULN-D1,
  Medium**); the audit trail links every event to a **per-user keyed pseudonym
  and user_id**, forming a re-identifiable activity profile (**VULN-D3, Low**);
  and a **published demo API key** (`demo-clinician-key`) exists for the CLI
  path (**VULN-D2, Low/Informational**).

No Critical or High privacy defect was found; the design is genuinely
privacy-conscious (encrypted tokens, keyed pseudonyms, minimised token payload,
enumeration resistance). The main residual risk is **token lifecycle**: no
server-side revocation. Top finding: **VULN-D1 — Medium.**

---

## 2. Scope of Testing

**System evaluated.** VariantLens — FastAPI service with JWT account auth,
Argon2id password storage, an append-only audit log, subscription plans/quotas,
and a PostgreSQL (or in-memory for tests) datastore.

**Specialization.** Privacy and data leakage: sensitive-information leakage,
PII exposure, conversation/memory leakage, authentication weaknesses, user-data
protection, privacy compliance.

**Components evaluated.**
- `auth/security.py` — password hashing, JWT mint/verify, key derivation.
- `auth/audit.py` — the audit trail's pseudonymisation of the actor.
- `db/database.py`, `api.py` — what is persisted per user and how it is read.
- `api.py` — auth middleware/dependencies and error responses.

**Out of scope.** Transport/TLS termination (assumed the deployment's job),
OS/host hardening, third-party (Gemini) data handling beyond noting it, and any
live penetration of the hosting provider.

**Limitations.** Static + unit-level review plus targeted runtime probes; no
traffic capture or production database inspection. Findings describe this
commit.

---

## 3. Evaluation Methodology

**Approach.** Map every place personal data is *created, stored, transmitted,
logged, or deleted* (a data-flow review), then attack each: can I authenticate
as someone else, read someone's data, recover a password, enumerate accounts,
or deanonymise the audit log?

**Attack/check families.**
1. **Authentication weaknesses** — password storage, timing/enumeration, token
   forgery/replay/expiry, secret handling.
2. **PII exposure** — what identifiers live in tokens, DB rows, logs, and error
   messages; is data minimised?
3. **Conversation/memory leakage** — are past queries/analyses isolated per
   user, or readable cross-user?
4. **Compliance posture** — data minimisation, retention, right-to-erasure,
   pseudonymisation quality.

**Method.** Code review of the auth/crypto/audit modules, plus a runtime probe
(`audit/probe_s2.py`) that (a) mints a token and inspects what it reveals, and
(b) checks whether audit rows leak raw identifiers. No production data touched.

**Severity scale.** Critical / High / Medium / Low / Informational by
impact × likelihood, in a health-data context where re-identification and
credential compromise are the dominant risks.

---

---

## 4. Test Cases Performed

Evidence = `audit/probe_s2.py` output (Appendix B) plus code citations. "Pass" = privacy control holds as designed.

| ID | Objective | Input / Scenario | Expected | Actual | Outcome |
|----|-----------|------------------|----------|--------|---------|
| TC-01 | Token confidentiality | Mint a token, inspect string | Opaque; no PII readable | 5-part JWE; `alice`/`@` absent from blob | Pass |
| TC-02 | Token payload minimisation | Decode token claims | Only identity/role | claims = aud, exp, iat, iss, role, sub, username; no email/plan | Pass |
| TC-03 | Password storage | Hash same password twice | Argon2id, salted | `$argon2id$…`; two hashes differ | Pass |
| TC-04 | Password verification | Verify correct vs wrong | Correct→true, wrong→false | true / false | Pass |
| TC-05 | Enumeration resistance | Timing of unknown-email login | Same cost as wrong password | `verify_password_or_dummy` burns equal Argon2id work | Pass |
| TC-06 | Secret boot behaviour | Start with no JWT env vars | Fail, not default | raises `ConfigurationError` unless `JWT_ALLOW_DEV_SECRET` | Pass |
| TC-07 | Audit pseudonym quality | `hash_identifier(email)` | Keyed, non-reversible | 64-hex, case-normalised, no raw text | Pass |
| TC-08 | **Token revocation on logout** | Steal token, sign out, reuse | Reuse fails | Logout only clears cookie; token valid until `exp` | **VULN-D1** |
| TC-09 | **Token revocation on password change** | Reset password, reuse old token | Old tokens die | Documented: reset revokes credential, not session | **VULN-D1** |
| TC-10 | Reset-code leakage | Request reset for unknown email | Uniform response | Always 200, same body; `dev_code` null on real transport | Pass |
| TC-11 | Cross-user data isolation | Query another user's analysis | 403/404 | Ownership checked via `current_user` in routes | Pass |
| TC-12 | API-key exposure | Inspect stored API key | Digest only | Only SHA-256 digest persisted (`api_key_service`) | Pass |
| TC-13 | Published demo key | Use `demo-clinician-key` | Not accepted in prod | Present as constant for CLI demo path | **VULN-D2** |
| TC-14 | Audit linkability | Join audit rows by pseudonym | Hard to re-identify | Keyed pseudonym + user_id linkable server-side | **VULN-D3** |
| TC-15 | Error-message leakage | Trigger auth/validation errors | Generic detail | Errors return generic strings, not stack/SQL | Pass |
| TC-16 | Log transport of codes | Reset code in dev | Only dev logs it | `dev_code` populated only by log transport | Pass |

**Coverage.** 16 cases across all six suggested areas: sensitive leakage
(TC-01/02), PII exposure (TC-07/14), conversation/memory leakage (TC-11/12),
authentication weaknesses (TC-03/04/05/06/08/09), user-data protection
(TC-10/12/15/16), and privacy compliance/posture (TC-01/02/07).

---

## 5. Vulnerabilities Identified

### VULN-D1 — Stateless tokens are not revocable (logout and password change)
- **Description.** JWTs are verified statelessly. `logout()` clears the browser
  cookie but, as its own docstring states, "anything already holding the token
  keeps its access until it expires." Password reset likewise "revokes the
  credential, not the session." There is no server-side deny-list or token
  version check.
- **Evidence.** TC-08, TC-09; `auth/routes.py` `logout()` and the password-reset
  comment.
- **Impact.** A stolen/leaked token remains usable for up to
  `JWT_EXPIRE_MINUTES`; a compromised account cannot be fully locked out until
  natural expiry. In a clinical tool this is a session-hijack window.
- **Likelihood.** Medium (tokens leak via XSS, logs, shared devices).
- **Severity: Medium.** Bounded by short token lifetime and by the fact that
  deactivating the account re-checks the DB on every request
  (`auth/dependencies.py` re-reads the account row), so *account disable* does
  cut access — but a mere logout/password-change does not.
- **Technical explanation.** Stateless JWT auth trades revocability for
  scalability. Without a deny-list or a per-user token-version claim checked at
  decode time, individual tokens cannot be invalidated before `exp`.

### VULN-D2 — Published demo API key
- **Description.** `DEMO_API_KEY = "demo-clinician-key"` is a constant in the
  source (and `protocol/security.py`), used for the CLI/demo path.
- **Evidence.** TC-13; `api.py:197`.
- **Impact.** If honoured on the public API, it is a known credential. It is
  intended only for local/CLI use.
- **Likelihood.** Low.
- **Severity: Low / Informational.** Documented as demo-only; the real API uses
  JWT/API-key accounts. Rated for completeness, to recommend removal from the
  shipped default.

### VULN-D3 — Audit trail is a re-identifiable activity profile
- **Description.** Each audit event stores a per-user keyed pseudonym **and**
  `user_id`, linking every action to an identity server-side.
- **Evidence.** TC-14; `auth/audit.py` `record()`.
- **Impact.** The pseudonym is strong (keyed, non-reversible without the signing
  secret), but combined with `user_id` and timestamps the log is a behavioural
  profile. If DB and app secret leak together, activity is attributable.
- **Likelihood.** Low.
- **Severity: Low.** Largely by design (auditing requires accountability). Rated
  Low with a recommendation to bound retention and restrict log access.

---

## 6. Risk Assessment

| Vulnerability | Impact | Likelihood | Risk Level | Justification |
|---------------|--------|------------|------------|---------------|
| VULN-D1 No token revocation | Medium (session window) | Medium | **Medium** | Stolen token lives until `exp`; mitigated by short TTL + account-disable re-check |
| VULN-D2 Published demo key | Medium if honoured | Low | **Low** | Demo/CLI-only; real API uses accounts |
| VULN-D3 Audit re-identification | Low | Low | **Low** | Keyed pseudonym; by-design accountability, bounded by retention |

**Aggregate.** No Critical/High. The auth design is strong (Argon2id, encrypted
tokens, enumeration resistance, fail-loud secrets). The single Medium — token
revocation — is the realistic one to raise at the viva, together with its
mitigating controls.

---

## 7. Mitigation Strategies

1. **Add token revocation (closes VULN-D1, the only Medium).** Introduce a
   per-user `token_version` (or a small deny-list of `jti`s) and check it at
   decode time; increment it on logout, password change, and account
   deactivation. Alternatively shorten `JWT_EXPIRE_MINUTES` and pair with a
   rotating refresh token. This converts "revoke the credential" into "revoke
   the session."
2. **Make password change revoke live sessions.** Wire the existing password
   reset to bump `token_version` so already-issued tokens die immediately
   (one-line change once #1 exists).
3. **Remove the published demo key from the served default (VULN-D2).** Require
   `DEMO_CLINICIAN_KEY` from the environment on any non-local deployment; fail
   to start if it is the published constant.
4. **Bound audit retention and access (VULN-D3).** Define a retention window
   (e.g. 90 days) with a purge job, restrict audit-table reads to admins, and
   avoid storing `user_id` alongside the pseudonym where a pseudonym alone
   suffices.
5. **Preserve the strengths.** Keep Argon2id, the encrypted JWE tokens, the
   minimised token payload, the fail-loud secret check, and the dummy-hash
   timing shield — and add regression tests so none are accidentally removed.

**Short term:** #2 and #3 are tiny and close the practical exposure.
**Strategic:** #1 makes session lifecycle correct; #4 aligns the audit log with
data-minimisation principles.

---

## 8. Reflection

**Challenges.** The main risk in a privacy audit is overstating: several things
that look like leaks (a hex string in the audit table, a token that "contains a
username") turned out, on inspection, to be well-designed (keyed pseudonym;
encrypted token whose *decoded* claims are intentionally just identity/role). I
had to verify each before scoring it. A second challenge was separating a real
gap (no revocation) from a documented, accepted trade-off (stateless JWTs).

**Lessons learned.** (1) Encrypted ≠ signed and minimised ≠ empty: the token is
opaque in transit yet still carries the minimum needed claims — both matter.
(2) The difference between "logout" and "revocation" is a genuine, commonly
missed class of vulnerability. (3) Good crypto (Argon2id, keyed digests) is
necessary but not sufficient; lifecycle (revocation, retention) is where
privacy is usually lost.

**Recommendations.** Implement token versioning, purge audit logs on a schedule,
strip the demo key from production defaults, and add a privacy regression test
that fails if a token ever carries email/plan or if a raw identifier reaches the
audit table.

---

## Appendix B — Reproducible Probe & Sample Output

Run: `source /Users/sandali/projects/.venv/bin/activate` then
`JWT_ALLOW_DEV_SECRET=1 python audit/probe_s2.py`. Script: `audit/probe_s2.py`.

```
TOKEN parts: 5 (5 = encrypted JWE)
TOKEN contains 'alice'?: False (should be False: opaque)
DECODED claims keys: ['aud', 'exp', 'iat', 'iss', 'role', 'sub', 'username']
DECODED has email/plan?: False
Argon2id?: True
salted (h1 != h2)?: True
verify correct?: True   verify wrong?: False
pseudonym is hex digest?: True
case-normalised (a==b)?: True
not raw email?: True
```

This output evidences TC-01/02 (opaque, minimised token), TC-03/04 (Argon2id,
salted, correct verify), and TC-07 (keyed, non-reversible pseudonym). The
revocation gap (TC-08/09) is a code-reading finding cited from
`auth/routes.py`.

