# VariantLens Authentication, Plans & Quotas

This document describes the account and entitlement layer that sits **in front of**
the agent pipeline. The agents themselves are unchanged: they still receive a raw
query and still run their own API-key check. Nothing in `agents/`, `protocol/`
or the agent logic in `main.py` depends on accounts, plans or quotas.

```
Browser ──JWT────────▶ FastAPI (auth + entitlements) ──API key──▶ main.py ──▶ agents
Program ──X-API-Key──▶        │                                      │
                              └──▶ PostgreSQL (users, plans, usage) ──┘
```

---

## 1. Why this exists

Before this layer, `/api/classify` accepted any caller who knew the demo API key.
That is fine for a local demo and useless as a subscription product: there is no
account, no plan, and no way to meter or gate usage. This layer adds the three
things a real deployment needs — **identity**, **entitlements** and **metering** —
without touching the agentic core.

---

## 2. Components

| Path | Responsibility |
|---|---|
| `db/models.py` | `Plan`, `User`, `UsageRecord`, `ApiKey` ORM models |
| `db/database.py` | Engine, session factory, `get_db()` FastAPI dependency |
| `migrations/` + `alembic.ini` | Alembic migrations for the account, audit and key tables |
| `auth/security.py` | Argon2id password hashing, JWT mint/decode |
| `auth/schemas.py` | Pydantic request/response contracts |
| `auth/dependencies.py` | `get_current_user()` (bearer **or** `X-API-Key`), `require_admin()` |
| `auth/routes.py` | `/api/auth/register`, `/login`, `/me` |
| `services/entitlement_service.py` | Quota windows, feature flags, usage recording |
| `services/api_key_service.py` | Issue, resolve, list and revoke scoped API keys |
| `scripts/seed_plans.py` | Inserts the Free / Professional / Enterprise plans |
| `scripts/create_admin.py` | Creates the bootstrap admin account |
| `tests/test_auth.py` | Registration, login, JWT handling |
| `tests/test_entitlements.py` | Quota arithmetic, month windows, row locking |
| `tests/test_classify_api.py` | HTTP auth, plan gate, 429 behaviour |
| `tests/test_api_keys.py` | Key creation gating, resolution, downgrade, revocation |
| `tests/test_audit_export.py` | Audit-export entitlement gate, scoping, CSV |
| `tests/test_admin.py` | Role-based access, privilege guards, password reset |
| `tests/test_review_signoffs.py` | Sign-off persistence, ownership, durability |
| `tests/test_cache_isolation.py` | Cached results are scoped per account |
| `scripts/import_review_signoffs.py` | Backfills the pre-migration JSON store |
| `scripts/smoke_auth.py` | End-to-end check against real PostgreSQL |
| `requirements-api.txt` | Runtime dependency set, separate from the notebook stack |

---

## 3. Running it

```bash
# 1. Database — a free PostgreSQL at https://neon.com, no credit card.
#    Paste its connection string into .env as DATABASE_URL, verbatim.

# 2. Environment
cp .env.example .env
#   add DATABASE_URL, plus GEMINI_API_KEY (optional — Free plan runs rule-based)
#   generate a real signing key:
#   python -c "import secrets; print(secrets.token_urlsafe(48))"

# 3. Schema + reference data
alembic upgrade head
python scripts/seed_plans.py
python scripts/create_admin.py     # prints a generated password, once

# 4. API
uvicorn api:app --reload --port 8000
```

Open <http://localhost:8000>, register an account, sign in, and run a query.

**Why a hosted database:** the team works across macOS, Windows and Linux. One
connection string is the only thing that has to be identical everywhere — no
Docker, no per-OS install instructions. Any PostgreSQL works; Neon just happens
to be the one that requires no installation. `DATABASE_URL` is required and has
**no fallback**, so a misconfigured checkout fails immediately with instructions
rather than a connection error pointing at a localhost port that nothing is
listening on.

> Neon scales to zero after ~5 minutes idle, so the first request after a pause
> takes a second or two. `pool_pre_ping=True` handles the wake-up.

---

## 4. Authentication

### Passwords
Argon2id through `pwdlib`'s recommended configuration. Plaintext is never stored,
logged or returned by any endpoint. `verify_password()` treats a corrupt or legacy
hash as "wrong password" rather than raising.

### Signing in
`POST /api/auth/login` accepts **either the username or the email address** in the
same field. Both columns are matched in a single query rather than guessing from
the presence of an "@", so a username that looks like an address still works, and
a pasted address in mixed case still matches (addresses are stored lower-cased).

The failure response is identical for "no such account" and "wrong password", so
the endpoint cannot be used to enumerate either.

### Forgotten passwords
A four-step flow: **email → 6-digit code → new password**.

| Endpoint | Purpose |
|---|---|
| `POST /api/auth/password-reset/request` | Issue a code to an address |
| `POST /api/auth/password-reset/confirm` | Redeem it and set a new password |

Properties the implementation holds to:

- **Codes are never stored in plaintext** — only a salted SHA-256 digest, so a
  database leak does not hand out working codes.
- **Single use** — a redeemed row is stamped `consumed_at` and cannot be replayed.
- **10-minute expiry** and **5 attempts**, after which the code is burned. A 6-digit
  code is only tolerable because the attempt budget makes it useless to brute-force.
- **A new request supersedes the old one**, so an earlier code cannot be redeemed
  after a later request.
- **No enumeration** — requesting a code always answers `200` with the same message
  whether or not the address exists, and deactivated accounts are skipped silently.

SHA-256 is correct for the *code* precisely because it is a short-lived,
attempt-limited token, not a password. Passwords use Argon2id.

> A reset replaces the **credential**, not every existing session: tokens already
> issued remain valid until they expire.

### Delivering the code
`services/mailer.py` exposes one interface with three transports:

| `MAIL_DELIVERY` | Behaviour |
|---|---|
| `log` *(default)* | Prints the message; the API also returns the code as `dev_code` |
| `resend` | Posts to the Resend API. Needs `RESEND_API_KEY`. **Never** returns the code |
| `smtp` | stdlib `smtplib`, for an institutional relay |

`log` is the right default here, not a shortcut: demo accounts use `@example.com`
and `@variantlens.local` addresses that no provider can deliver to anyway.

Two rules the module enforces:

- `echo_secret` is `False` for every real transport, so a code can only ever be
  echoed back in development.
- Selecting a real transport **without** its credentials raises
  `MailConfigurationError` at call time rather than silently dropping reset
  emails.

### Tokens
Signed **then** encrypted (JWE, `dir` + A256GCM wrapping a nested HS256 JWS), so
the token is an opaque 5-part compact string rather than a readable JWT.

```
eyJhbGciOiAiZGlyIiwiZW5jIjoiQTI1NkdDTSIsImN0eSI6IkpXVCJ9..gGkS...ivzY1...iGNG
└──────── JWE header ────────┘              └ encrypted payload ┘  └ tag ┘
                    └ encrypted nested JWS (the signed claims) ┘
```

- `sub` = user id, plus `username`, `role`, `iat`, `exp`.
- **`iss` and `aud` are required.** A token minted for another system, or by
  another system, is rejected before the signature is even considered.
- Default lifetime 60 minutes (`JWT_EXPIRE_MINUTES`).
- **No sensitive claims**: no email, no plan, no password material. Encryption
  is defence in depth here, not a licence to put secrets in the payload.
- The signing key and the encryption key are separate settings, so leaking the
  encryption key cannot be used to forge a signature.
- **Missing or default keys fail at import.** A development constant is used only
  when `JWT_ALLOW_DEV_SECRET=true`, and `auth.security.USING_DEV_SECRET` reports
  that fact. In any other case the process refuses to start rather than issuing
  tokens anyone can forge.

### Browser sessions: HttpOnly cookie + CSRF
A browser used to hold its token in `localStorage`, which any XSS bug could read
out. The token now goes into an `HttpOnly` cookie, which script cannot touch.

| Cookie | `HttpOnly` | Purpose |
|---|---|---|
| `variantlens_session` | **yes** | the token itself — unreadable by JavaScript |
| `variantlens_csrf` | no | the double-submit half the client echoes back |

Cookies are `Secure`, `SameSite=lax`, `Path=/`, and expire with the token.

Ambience is exactly what makes cookies convenient and exactly what makes them
CSRFable: the browser attaches the session cookie to a cross-site `PATCH` whether
or not the page meant to make one. So any **unsafe method authenticated by
cookie** must present a matching `X-CSRF-Token` header, and is refused with 403
otherwise. `GET`, `HEAD` and `OPTIONS` are exempt because they do not change
state.

The CSRF cookie is deliberately readable. That is not a weakness: knowing it is
useless unless an attacker can also cause the browser to attach the `HttpOnly`
cookie, which is the thing they cannot do cross-origin.

**Bearer auth is fully supported and never needs a CSRF header**, because a
bearer token is only ever sent by a client that chose to send it — there is
nothing ambient for a hostile page to ride. An explicit `Authorization` header
takes precedence over any cookie present.

**`X-API-Key` needs no CSRF header either**, for the same reason: a header is
only sent by a client that chose to send it. CSRF is checked only on the
cookie path, which is the one credential a browser attaches automatically.

Local development runs on plain HTTP, where browsers silently drop a `Secure`
cookie, so `COOKIE_SECURE=false` is needed for `http://localhost` only. It must
never be set in production.

### Identity is re-read on every request
`get_current_user()` validates the signature, then loads the `User` row again:

```python
user = db.scalars(select(User).where(User.id == user_id)).one_or_none()
if not user.is_active: ...  # 401
```

This is deliberate. Trusting the token alone would mean a deactivated or deleted
account keeps working until its token expires. Re-reading means revocation is
immediate, and the `role` used for authorization always comes from the database —
putting `role: "admin"` in a token changes nothing.

### Login throttling
Argon2id makes each guess slow, but it does not cap *how many* guesses are made —
so an unbounded login endpoint is still a free password oracle. `auth.rate_limit`
bounds it two ways at once:

- **Per IP**, so one host cannot spray many accounts.
- **Per account**, so many hosts cannot spray one account.

Five failures inside the window returns 429 and the `Retry-After` grows. A
successful login clears the account's counter.

> This limiter is **in-process, so its counters are per worker**. Running more
> than one worker (or more than one container) multiplies the budget by the
> number of workers. A multi-worker deployment must move this to a shared store
> such as Redis, or the protection is weaker than it looks.

### The `auth_events` table
Login, registration, password change and every admin mutation write an
`AuthEvent` row: `event`, `user_id`, `actor_user_id`, `ip`, `user_agent`,
`occurred_at`, and a `detail` JSON blob.

- Identifiers (username, email) are stored **hashed, never raw** — enough to
  correlate events about the same person, without turning the audit log into a
  second copy of the user table.
- The `actor` is recorded separately from the `subject` so "admin X changed
  clinician Y's plan" is answerable without guessing.
- Schema: migration `0005_auth_events`.

### Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/api/auth/register` | none | Create an account (always Free, always `clinician`) |
| POST | `/api/auth/login` | none | Exchange credentials (**email only**) for a token |
| POST | `/api/auth/password-reset/request` | none | Issue a 6-digit reset code |
| POST | `/api/auth/password-reset/confirm` | none | Redeem it, set a new password, sign in |
| GET | `/api/auth/me` | Bearer | Current account, plan, usage, entitlements |
| POST | `/api/classify` | Bearer or `X-API-Key` | Run the pipeline (quota-metered) |
| GET | `/api/trail/{variant_id}` | Bearer or `X-API-Key` | Agent-to-agent audit trail |
| GET | `/api/pipeline/{variant_id}` | Bearer or `X-API-Key` | Full pipeline record |
| POST | `/api/keys` | Bearer + `api_access` | Mint a scoped API key (raw value shown **once**) |
| GET | `/api/keys` | Bearer | List own keys (metadata only, any plan) |
| DELETE | `/api/keys/{key_id}` | Bearer | Revoke an own key |
| GET | `/api/export/audit` | Bearer + `audit_export` | Export own usage/auth/sign-off rows (JSON or `?format=csv`) |
| GET | `/api/admin/users` | Bearer + `admin` | Read-only account listing |

Self-registration can never create an admin: the route hard-codes
`role="clinician"`. Duplicate username or email returns `409`. Unknown address
and wrong password return the same `401` body **and burn the same Argon2id work**
(`verify_password_or_dummy` verifies against a dummy hash when no account exists),
so accounts cannot be enumerated by message or by response time.

### Email is the identity
`username` is a **display handle**, not a credential. It stays unique and
immutable, but `/api/auth/login` accepts `email` and nothing else — there is no
`or_(username, email)` fallback, and a handle that happens to be shaped like an
address will not authenticate.

Two things fall out of that:

- **One account, one name.** Previously anyone who guessed or harvested a
  username could aim a sign-in attempt at that account. A handle is now public
  information with no access attached to it.
- **Recovery and sign-in share an address.** The address that receives a reset
  code is the same one you sign in with, so there is no second identity to keep
  consistent.

Addresses are lower-cased on registration, on profile change and on login, so
`Alice@X.com` reaches the same account as `alice@x.com`.

> **This was a breaking change.** `{"username": ...}` on the login payload is
> rejected with `422`, and scripted clients must send `{"email": ...}`. The
> field is validated as an address, so a bare handle fails at the schema rather
> than silently reaching the password check.

The freed-address hole is *not* closed by this change: a user can still change
their email, which releases the old address for someone else to register. That
wants its own fix.
---

## 5. Plans and entitlements

### Plans

`scripts/seed_plans.py` is the single source of truth (values below are verbatim):

| Plan | Price | Monthly queries | LLM | API access | Audit export |
|---|---|---|---|---|---|
| Free | $0.00 | 10 | ✗ rules-only | ✗ | ✗ |
| Professional | $99.00 | 500 | ✓ | ✓ | ✓ |
| Enterprise | custom | unlimited (`NULL`) | ✓ | ✓ | ✓ |

Two modelling decisions worth stating explicitly:

- **`monthly_query_limit = NULL` means unlimited**, not zero. `NULL` is never
  coerced to `0`, which would read as "no queries left".
- **Prices are integer cents.** Floating-point money would introduce rounding
  errors into plan comparisons.

Role and plan are **independent axes**. An admin can sit on the Free plan; a
clinician can sit on Enterprise. Billing must never decide who is an
administrator.

### Feature gating
`plan.llm_enabled` selects which system instance serves the request:

```python
system = _system_llm if llm_allowed else _system_rules
```

Two instances are built at startup rather than mutating one shared instance, so a
plan change can never leak into a concurrent request. The Free plan still runs the
**full four-agent pipeline** — it is rule-based reasoning and template reporting,
not a reduced pipeline.

The other two entitlements are now enforced gates too:

- **`api_access`** gates `POST /api/keys` — only a plan with this flag can mint a
  scoped API key. `GET /api/keys` and `DELETE /api/keys/{id}` work for any plan,
  so a downgraded account can still see and revoke its keys.
- **`audit_export`** gates `GET /api/export/audit` (JSON or `?format=csv`) with a
  `403` when the flag is absent.

### Scoped API keys

An API key authenticates a programme the same way a session does: `get_current_user()`
checks for an `X-API-Key` header first and, if present, resolves it instead of the
bearer token (CSRF does not apply — there is no cookie to forge).

| Property | How |
|---|---|
| Storage | SHA-256 digest only (`key_hash`, unique + indexed). The raw `vl_…` value is returned **once**, at creation |
| Display | A short `prefix` is stored so the UI can show which key is which without the secret |
| Revocation | `revoked_at` — a revoked key fails resolution immediately |
| Plan binding | `resolve_key()` checks the owner's plan still carries `api_access` **on every request**, so a downgrade disables existing keys at once |
| Audit | `last_used_at` is stamped on each successful resolution |

`tests/test_api_keys.py` covers creation gating, the downgrade-revokes behaviour,
revocation, unknown keys, and that key material never appears in listings.

---

## 6. Quotas

The window is the **current UTC month**, computed from `usage_records.created_at`:

```python
def month_start_utc(now=None):
    moment = now or datetime.now(timezone.utc)
    return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
```

Usage is a **sum of rows**, never a mutable counter. That keeps the audit trail
(the system is meant to be auditable) and makes months unable to bleed into each
other.

### Two-layer enforcement

1. **`check_quota()` before the pipeline** — rejects an exhausted account with
   `429` immediately, so the expensive four-agent run never starts.
2. **`record_usage()` inside the write transaction** — re-checks the limit in the
   same transaction that inserts the row.

The pre-check alone is not sufficient: two concurrent requests can both read
`used = limit - 1` and both proceed. `record_usage()` therefore takes a
**row-level lock** first:

```python
bind = db.get_bind()
if bind.dialect.name in _ROW_LOCK_DIALECTS:
    db.execute(select(User.id).where(User.id == user_id).with_for_update())
```

`SELECT … FOR UPDATE` serialises concurrent requests for the same account; the
second one re-reads usage *after* the first commits and sees the true count.
SQLite has no row-level locks, so the lock is skipped there and the unit tests
exercise the insert-then-recheck fallback. SQLite's single-writer model still
serialises those transactions, so the fallback is correct for testing.

### Every accepted query is charged
`usage_records.request_id` is unique, and the API mints a **fresh id per
request**:

```python
request_id = str(uuid.uuid4())
```

Asking the same question twice costs two queries. There is no text-matching or
"we already answered this" discount — a cache hit is still a caller query, and
the unit of work that gets metered is one caller query.

> This replaced a genuinely broken scheme that derived the id as
> `uuid5(user_id + query_text)`. That conflated "the same request" with "the same
> words", so repeating a query produced the same id, the insert collided, and the
> call silently went uncharged. Worse, the id carried no month, so a query paid
> for once could be repeated **free in every month thereafter**. A Free-plan user
> could burn their quota on distinct queries and then re-run their favourite one
> indefinitely.

### Retry-safety, opt-in
Genuine retries still need handling: a client that times out cannot tell whether
its request landed, and billing it twice is unfair. Send an explicit key and the
id becomes deterministic again:

```python
request_id = str(uuid.uuid5(
    uuid.NAMESPACE_URL,
    f"{current_user.id}:{month:%Y-%m}:{request.idempotency_key.strip()}",
))
```

The month is part of the derivation, so reusing a key next month is a new charge
rather than a collision with its own history. Omit the key and every call is
billed, which is the default and the right default for a quota system.

### What is charged
Only an accepted query. A `4xx` rejection (bad query, agent-layer refusal) does
not consume quota. Cache hits *are* charged — the unit of work is one caller
query, and that is the honest thing to meter.

---

## 7. Defence in depth

The agent layer keeps its own API-key check in `main.py`. JWT runs at the edge,
and the pipeline still refuses an untrusted caller, so a direct call that skips
FastAPI is still rejected. Neither layer replaces the other.

Caching is also identity-scoped:

```python
identity = user_id or api_key
query_string = f"{raw_query}:{identity}"
```

One account can never be served another account's cached result.

---

## 8. Admin scope

| Endpoint | Purpose |
|---|---|
| `GET /api/admin/users` | Account listing (never password material) |
| `GET /api/admin/plans` | Plan catalogue with subscriber counts |
| `PATCH /api/admin/users/{id}` | Change plan, active state or role |
| `POST /api/admin/users/{id}/password` | Reset another account's password |
| `GET /api/reviews` | Register of recorded human sign-offs |

There is also one **non-admin** endpoint used by the profile page:

| Endpoint | Purpose |
|---|---|
| `GET /api/plans` | Plan catalogue for any signed-in account, for the plan comparison and upgrade requests on the profile page. Same shape as `/api/admin/plans` minus `subscribers`, which stays admin-only. |

There is **no account creation** endpoint: self-registration already covers that
path, and an admin-create endpoint would be a way to mint accounts without the
registration validation. There is also **no account deletion** — deactivation is
the reversible equivalent, and deletion is exactly the operation that must not be
able to erase clinical sign-offs.

Two guards exist on `PATCH`, because it is the one place a token holder can
gain or lose reach:

```python
# An admin cannot change their own role or active state...
if target.id == current_user.id and self_change:
    raise HTTPException(status_code=400, ...)

# ...and the last active administrator cannot be demoted or deactivated,
# because afterwards nobody could administer the deployment through the API.
if demoting_or_disabling_admin and _active_admin_count(db, exclude_user_id=target.id) == 0:
    raise HTTPException(status_code=409, ...)
```

The first guard stops a session from locking its own owner out; the second stops
a deployment from reaching a state where recovery requires a direct database
shell. Both are covered by tests in `tests/test_admin.py`.

The web UI exposes these to `role=admin` through an Administration panel. That
panel is **presentation, not a control** — a clinician who hid or forged the
panel would still get `403` from the endpoints.

## 9. Review sign-offs in PostgreSQL

Sign-offs moved from a local JSON file to a `review_signoffs` table
(`migrations/versions/0002_review_signoffs.py`), because a clinical attestation
stored in a gitignored file has no owner, no backup and no audit trail. See
[REVIEW_SIGNOFF_WORKFLOW.md](REVIEW_SIGNOFF_WORKFLOW.md) for the workflow and
`scripts/import_review_signoffs.py` for backfilling an existing JSON store.

## 10. Upgrades: a hand-off, not a checkout

There is **no payment integration**, and that is deliberate. What is modelled is
the part that is honest and interesting: who asked for what, who decided, and
when. Flipping a plan on a self-serve request would make the commercialization
story look finished when it is not.

| Endpoint | Auth | Purpose |
|---|---|---|
| `POST /api/upgrade-requests` | user | Ask for a paid plan |
| `GET /api/upgrade-requests/mine` | user | This account's request history |
| `GET /api/admin/upgrade-requests` | admin | The queue, with requester contact details |
| `PATCH /api/admin/upgrade-requests/{id}` | admin | `approved` / `declined` / `contacted` |

Lifecycle:

```
pending ──> contacted ──> approved   (applies the plan change)
           └─────────────> declined
```

Rules, enforced in `services/upgrade_service.py` so they hold whichever endpoint
is called:

- **A justification is required.** `POST /api/upgrade-requests` rejects a missing,
  null or under-10-character `note`, and `strip_whitespace=True` means ten spaces
  is not a justification either. This was `Optional[str] = None` and the UI said
  "(optional)", which left the administrator approving spend with nothing to
  weigh. The admin queue now renders the reason as the most prominent element on
  the card; requests raised before this change render an explicit "No
  justification was recorded" rather than a blank box.
- **Only paid plans are requestable.** Free is the starting tier, not an upgrade.
- **You cannot request the plan you are already on.**
- **One open request per account.** A declined or contacted request still blocks
  a second, so the admin is never chasing duplicates.
- **`approved` and `declined` are terminal.** A decided request cannot be
  re-opened, which keeps the history readable.
- **Approval applies the plan change in the same transaction as the decision**, so
  the two can never disagree — a test asserts `/api/auth/me` actually reports the
  new plan and its entitlements immediately after.

Requesting changes **nothing**: the account keeps its working plan and quota
while it waits. The requester is emailed when a decision is made, and can read
the administrator's note on their own profile.

`contacted` exists as its own state because "the admin reached out" is a real,
reportable step that happens before a decision — collapsing it into `pending`
would hide that from both the user and the audit trail.

## 11. Editing your own details

`PATCH /api/auth/me` updates the signed-in account, and `POST /api/auth/change-password`
changes its password. What each field is allowed to do is a deliberate decision:

| Field | Policy | Why |
|---|---|---|
| `full_name` | Free to change | Cosmetic; blank clears it back to null |
| `email` | **Requires the current password** | The address is where password recovery goes. Without that check, anyone holding a stolen session could redirect recovery to an inbox they control and take the account over permanently |
| `username` | **Immutable** | It is the primary identifier, it is baked into issued tokens, and it appears in the audit trail. Renaming would rewrite history retroactively |
| password | `POST /api/auth/change-password` | Proves ownership with the credential still held — the opposite situation from the forgot-password flow, which needs a mailed code because the credential is gone |

Setting an email to its current value is a no-op and does not demand the
password, because nothing changes. Neither response ever contains password
material.

Unknown fields are **rejected** (`422`), not ignored. Pydantic discards extras by
default, which would mean a client sending `{"is_admin": true}` gets a cheerful
`200` while nothing happens — exactly the kind of silent no-op that hides a bug
until it matters.

---

## 12. The web interface

`frontend/` is three files rather than one long page, so each is reviewable:

| File | Contents |
|---|---|
| `index.html` | Landing page, workspace, profile, admin dashboard, auth modal |
| `app.css` | Every style, built from the same `:root` tokens as the report |
| `app.js` | View switching, auth flows, report rendering, admin dashboard |

Four views, one visible at a time:

| View | Who sees it | What it is |
|---|---|---|
| **Landing** | Everyone | Marketing page: hero with a sample report, real dataset figures, the four-agent pipeline, pricing, FAQ. **No query bar** — a visitor should understand the product before being asked to sign in |
| **Workspace** | Signed-in users | The query rail and the report: retrieval evidence, ACMG-style codes, audit timeline, review sign-off, PDF |
| **Profile** | Signed-in users | Account details, password change, current tier and an upgrade request form |
| **Admin** | `role=admin` | Analytics cards, a 30-day query chart, the upgrade queue and account controls |

Auth is a **modal** over whatever view you were on, with tabs for sign in
(username *or* email), register, and forgot-password → code → new password.

Visibility is driven by a single global utility:

```css
.hidden{display:none !important;}
```

It is `!important` on purpose. `.modal` is `display:flex`, `.app` is
`display:grid`, and the views animate — without the utility winning over those,
every view renders stacked on one page. `tests/test_frontend.py` asserts the rule
exists, is `!important`, and that each view ships with the class it needs, both in
the file and as served over HTTP.

A landing example clicked while signed out queues the query and opens the modal;
after signing in the query is already in the box and the workspace opens. The
workspace itself is unreachable while signed out, so nobody lands on a page they
cannot use.

The **Admin** link appears in the header only for `role=admin`, and a clinician
who navigates there directly is refused by `require_admin` on the endpoint. UI
visibility is a convenience; the server is the control.

### Marketing copy on the landing page

Every figure shown to a visitor is derived from `data/clinvar_labels.csv` or
from `scripts/seed_plans.py` — the 3,009 records, the BRCA1/BRCA2 split, the
pathogenic counts, and the three plan tiers. There are no invented testimonials,
star ratings or user counts, and none should be added: this is an academic
prototype, and a fabricated trust signal on a clinical tool is a liability rather
than a selling point. The hero's sample report is labelled as illustrative.

The pricing section mirrors the real plans. No payment is taken anywhere in the
application; an upgrade is a request that an administrator decides.

### URLs

The four views are one document, so navigation is client-side over
`history.pushState` and each view owns a real slug:

| URL | View |
|---|---|
| `/` | Landing |
| `/analyse` | Workspace |
| `/profile` | Profile and plan |
| `/admin-dashboard` | Admin dashboard |

This means a refresh, a pasted link and the browser back button all land in the
right place. The server serves `index.html` for exactly these four slugs
(`FRONTEND_VIEWS` in `api.py`); anything else is a real 404, so a typo in a URL
fails loudly instead of quietly returning HTML. A test asserts the server's slug
set and the client's match, because a slug the server serves but the client
cannot map back would render the landing page with no explanation.

An administrator lands on the dashboard rather than the query surface after
signing in — the query surface stays one click away in the header. A clinician
lands on `/analyse`.

### Confirmations

Every consequential action routes through one in-app dialog (`askConfirm()` in
`app.js`) instead of `window.prompt` / `window.confirm` / `window.alert`. The
browser versions cannot express a required justification, cannot be styled, and
read as browser chrome rather than as part of the product. The dialog supports a
textarea with a live character counter, a destructive variant for irreversible
actions, and cancels on Escape or a backdrop click.

| Action | Confirmed? | Notes |
|---|---|---|
| Sign in / register / reset | busy button only | the modal closes on success, stays open on failure so the error is readable |
| Request an upgrade | yes | justification **required**, min 10 characters |
| Approve / decline / mark contacted | yes | decline is styled destructive; all accept an optional note for the requester |
| Change an account's plan | yes | cancelling restores the select to its stored value |
| Activate / deactivate an account | yes | deactivation is styled destructive |
| Change your email | yes | only when the address actually changes; a name-only save is frictionless |
| Change your password | yes | |
| Record a clinical sign-off | yes | names the reviewer and the date being attested to |

## 13. Where the secrets live

There is no container image and no infrastructure-as-code file in this project, so
there is nowhere for a credential to be baked into by accident:

- `.env` is gitignored, and `DATABASE_URL` / `JWT_SECRET_KEY` are supplied per
  developer (or per deployment) rather than committed.
- The database connection requires TLS (`sslmode=require`).
- `pool_pre_ping=True` means a hosted database that scales to zero between
  requests does not surface as a stale-connection error.
- `get_db()` additionally **retries once** when the connection is severed
  *mid-query*, which `pool_pre_ping` cannot cover. Neon occasionally drops a
  connection on wake; before this, that surfaced as a 500 and a blank admin
  dashboard. The retry is gated by `_is_connection_dead()`, which matches only
  genuine connection loss — a constraint violation, bad syntax or deadlock is
  re-raised immediately rather than retried.
- `requirements-api.txt` pins the runtime dependency set separately from the
  notebook stack, so a server install is lean and auditable.

---

## 14. Tests

```bash
python -m pytest tests/ -v      # 652 tests
```

| File | Tests | Covers |
|---|---|---|
| `tests/test_auth.py` | 35 | hashing, registration, login, JWT tampering/expiry, revocation |
| `tests/test_entitlements.py` | 19 | month windows, quota math, idempotency, `FOR UPDATE` |
| `tests/test_classify_api.py` | 20 | 401/403/429, plan gate, usage recording |
| `tests/test_admin.py` | 83 | role checks, privilege guards, password reset, plan catalogue |
| `tests/test_review_signoffs.py` | 17 | sign-off persistence, ownership, durability |
| `tests/test_password_reset.py` | 25 | email login, code hashing, expiry, replay, attempt cap |
| `tests/test_upgrades.py` | 32 | upgrade lifecycle, plan swap, terminal states |
| `tests/test_profile.py` | 20 | profile editing, email-change guard, self-service password change |
| `tests/test_analytics.py` | 19 | dashboard figures, gap-filled daily series, role checks |
| `tests/test_cache_isolation.py` | 6 | cache keys and cached results are per-account |
| `tests/test_api_keys.py` | 10 | key creation gated on `plan.api_access`, digest-only storage, `X-API-Key` resolution, downgrade disables keys, revocation, ownership scoping |
| `tests/test_audit_export.py` | 5 | `plan.audit_export` gate (403/401), caller-scoped JSON rows, CSV header + disposition, bad format rejected |
| `tests/test_mcp_tools.py` | 6 | imports without the `mcp` package, clear error from `build_server()`, tool results, rejection, `top_k`, examples |
| `tests/test_frontend.py` | 146 | view visibility, auth panes, JS↔DOM wiring, served assets |
| `tests/test_pdf_auth.py` | 6 | PDF auth, escaping, cross-account trail isolation |
| `tests/test_llm_output_checks.py` | 12 | LLM prior withholding, report verification, login timing |
| agent tests (intake 22, evidence 15, classification 25, orchestrator 17, retrieval 47, evaluation 35, resilience 8, focused 1, cache 6, security 21, plus classify/profile counted above) | rest | four-agent behaviour, IR arithmetic, calibration/fairness, control plane |

The account layer is tested against **in-memory SQLite** so the suite needs no
database server; the models are deliberately portable (`native_enum=False`,
generic column types) to make that possible. Production, the end-to-end smoke
check (`python scripts/smoke_auth.py`) and the container all run against real
PostgreSQL.

---

## 15. Security checklist before production

- [ ] Set a real `JWT_SECRET_KEY`; never rely on the development fallback.
- [ ] Set `FRONTEND_ORIGINS` to the real deployment origins.
- [ ] Terminate TLS in front of the API so bearer tokens are never sent in clear.
- [ ] Replace the Compose development credentials and restrict the database port.
- [ ] Confirm `.env` is excluded from the built image before shipping it.
- [ ] Add refresh-token rotation or short session lifetimes for shared terminals.
- [ ] Consider an external identity provider; the local password store is intended
      for a prototype, not for regulated clinical data.