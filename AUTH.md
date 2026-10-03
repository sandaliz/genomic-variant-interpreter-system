# VariantLens Authentication, Plans & Quotas

This document describes the account and entitlement layer that sits **in front of**
the four-agent pipeline. The agents themselves are unchanged: they still receive a
raw query and still run their own API-key check. Nothing in `agents/`, `protocol/`
or the four-agent logic in `main.py` depends on accounts, plans or quotas.

```
Browser ──JWT──▶ FastAPI (auth + entitlements) ──API key──▶ main.py ──▶ 4 agents
                        │                                     │
                        └──▶ PostgreSQL (users, plans, usage) ┘
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
| `db/models.py` | `Plan`, `User`, `UsageRecord` ORM models |
| `db/database.py` | Engine, session factory, `get_db()` FastAPI dependency |
| `migrations/` + `alembic.ini` | Alembic migration for the three tables |
| `auth/security.py` | Argon2id password hashing, JWT mint/decode |
| `auth/schemas.py` | Pydantic request/response contracts |
| `auth/dependencies.py` | `get_current_user()`, `require_admin()` |
| `auth/routes.py` | `/api/auth/register`, `/login`, `/me` |
| `services/entitlement_service.py` | Quota windows, feature flags, usage recording |
| `scripts/seed_plans.py` | Inserts the Free / Professional / Enterprise plans |
| `scripts/create_admin.py` | Creates the bootstrap admin account |
| `tests/test_auth.py` | Registration, login, JWT handling |
| `tests/test_entitlements.py` | Quota arithmetic, month windows, row locking |
| `tests/test_classify_api.py` | HTTP auth, plan gate, 429 behaviour |
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
- HS256 JWT, `sub` = user id, plus `username`, `role`, `iat`, `exp`.
- Default lifetime 60 minutes (`JWT_EXPIRE_MINUTES`).
- **No sensitive claims**: no email, no plan, no password material.
- `JWT_SECRET_KEY` falls back to a clearly-labelled development constant if
  unset, and `auth.security.USING_DEV_SECRET` reports that fact. Always set a
  real secret in a deployment.

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

### Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/api/auth/register` | none | Create an account (always Free, always `clinician`) |
| POST | `/api/auth/login` | none | Exchange credentials (username **or** email) for a token |
| POST | `/api/auth/password-reset/request` | none | Issue a 6-digit reset code |
| POST | `/api/auth/password-reset/confirm` | none | Redeem it, set a new password, sign in |
| GET | `/api/auth/me` | Bearer | Current account, plan, usage, entitlements |
| POST | `/api/classify` | Bearer | Run the pipeline (quota-metered) |
| GET | `/api/trail/{variant_id}` | Bearer | Agent-to-agent audit trail |
| GET | `/api/pipeline/{variant_id}` | Bearer | Full pipeline record |
| GET | `/api/admin/users` | Bearer + `admin` | Read-only account listing |

Self-registration can never create an admin: the route hard-codes
`role="clinician"`. Duplicate username or email returns `409`. Unknown user and
wrong password return the same `401` body so usernames cannot be enumerated.
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

`api_access` and `audit_export` are stored, returned by `/api/auth/me` and shown in
the UI, but are **not yet enforced as gates** — `api_access` will gate the
programmatic endpoint once one exists, and `audit_export` will gate audit-trail
export. That work is deliberately out of this scope rather than silently implied.

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

### Idempotency
`usage_records.request_id` is unique, and the API derives it deterministically:

```python
request_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{current_user.id}:{request.query.strip()}"))
```

A client retrying the same query therefore matches an existing row, the insert
collides, and the request is **not charged twice**. Distinct queries are charged
separately.

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

## 11. Where the secrets live

There is no container image and no infrastructure-as-code file in this project, so
there is nowhere for a credential to be baked into by accident:

- `.env` is gitignored, and `DATABASE_URL` / `JWT_SECRET_KEY` are supplied per
  developer (or per deployment) rather than committed.
- The database connection requires TLS (`sslmode=require`).
- `pool_pre_ping=True` means a hosted database that scales to zero between
  requests does not surface as a stale-connection error.
- `requirements-api.txt` pins the runtime dependency set separately from the
  notebook stack, so a server install is lean and auditable.

---

## 12. Tests

```bash
python3 -m pytest tests/ -v      # 240 tests
```

| File | Tests | Covers |
|---|---|---|
| `tests/test_auth.py` | 27 | hashing, registration, login, JWT tampering/expiry, revocation |
| `tests/test_entitlements.py` | 19 | month windows, quota math, idempotency, `FOR UPDATE` |
| `tests/test_classify_api.py` | 16 | 401/403/429, plan gate, usage recording |
| `tests/test_admin.py` | 31 | role checks, privilege guards, password reset, plan catalogue |
| `tests/test_review_signoffs.py` | 17 | sign-off persistence, ownership, durability |
| `tests/test_password_reset.py` | 23 | email login, code hashing, expiry, replay, attempt cap |
| `tests/test_upgrades.py` | 25 | upgrade lifecycle, plan swap, terminal states |
| `tests/test_cache_isolation.py` | 6 | cache keys and cached results are per-account |
| existing agent tests | 75 | unchanged four-agent behaviour |

The account layer is tested against **in-memory SQLite** so the suite needs no
database server; the models are deliberately portable (`native_enum=False`,
generic column types) to make that possible. Production, the end-to-end smoke
check (`python scripts/smoke_auth.py`) and the container all run against real
PostgreSQL.

---

## 13. Security checklist before production

- [ ] Set a real `JWT_SECRET_KEY`; never rely on the development fallback.
- [ ] Set `FRONTEND_ORIGINS` to the real deployment origins.
- [ ] Terminate TLS in front of the API so bearer tokens are never sent in clear.
- [ ] Replace the Compose development credentials and restrict the database port.
- [ ] Confirm `.env` is excluded from the built image before shipping it.
- [ ] Add refresh-token rotation or short session lifetimes for shared terminals.
- [ ] Consider an external identity provider; the local password store is intended
      for a prototype, not for regulated clinical data.