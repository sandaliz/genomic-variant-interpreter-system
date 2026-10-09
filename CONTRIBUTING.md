# Contributing

VariantLens is a multi-agent system that interprets BRCA1/BRCA2 variants. It is
an academic project being developed toward a real product, so contributions are
judged by one question: **does this make the tool more defensible to a clinical
reviewer?** A faster pipeline that is harder to justify is not an improvement.

## Setup

Python 3.13 and Node 20 (the Node side is only for the frontend test runner).

```bash
git clone <repo> && cd variant_agent_system
python3.13 -m venv .venv && source .venv/bin/activate

pip install -r requirements-api.txt   # runtime only -- enough for the test suite
pip install -r requirements.txt       # adds the notebook/plotting stack

cp .env.example .env                  # then fill in the values
python -m pytest tests/ -q
```

`requirements-api.txt` is the lean server set; `requirements.txt` adds Jupyter,
matplotlib and seaborn for the analysis notebook. Installing only the first is
the fastest way to a working checkout.

`.env` needs `JWT_SECRET_KEY` and `JWT_ENCRYPTION_KEY` before the API will start
— `auth/security.py` refuses to boot with the published fallback unless
`JWT_ALLOW_DEV_SECRET=true` is set, which is for local demos and the test suite
only. **Never commit `.env`.** The test suite needs no real secrets and no
network.

`make help` lists the common tasks.

## Running things

| Command | What it does |
| --- | --- |
| `make run` | API with autoreload on <http://127.0.0.1:8000> |
| `make test` | Python suite |
| `make test-js` | Frontend suite (Node's built-in runner) |
| `make test-all` | Both |
| `make coverage` | Python suite with a coverage report |
| `make lint` / `make types` / `make audit` | ruff / mypy / pip-audit |
| `make evaluate-rankers` | TF-IDF vs BM25 vs hybrid over the corpus |
| `make evaluate` | Full evaluation (add `evaluate-real` to include the LLM ablation) |
| `make migrate` | Apply Alembic migrations to `DATABASE_URL` |

`make evaluate` and `make run` need a populated `DATABASE_URL`; nothing else
does.

## Layout

```
agents/          the pipeline: intake, evidence, classification and report
                 specialists, plus orchestrator.py -- the Supervisor that plans a
                 run and the Verifier that can send a call back once
protocol/        the encrypted message envelope, validation, and the message log
retrieval/       BM25, rank fusion and ranking metrics -- pure maths, no agents
services/        account-scoped work: history, API keys, entitlements, analytics
auth/            password hashing, JWT issuing, cookies, CSRF, rate limiting, audit
db/              SQLAlchemy models and the session factory
migrations/      Alembic revisions
frontend/        index.html + app.css + app.js (a static SPA, no build step)
tests/           pytest suite (Python) and tests/js/ (Node)
evaluation.py    the offline evaluation: classification, IR, calibration, fairness
```

Two structural rules worth knowing before you add code:

- **Pure computation goes in `retrieval/` or a small helper, not inside an
  agent.** An agent's job is to fetch, decide and explain; when the arithmetic
  lives in a module with no dependencies it can be tested against hand-computed
  values. `retrieval/` is importable on its own for exactly that reason.
- **A new persistence concern needs a migration.** `db/models.py` and a new file
  under `migrations/versions/` — the tests build their schema from
  `Base.metadata.create_all`, so a model change with no migration passes CI and
  breaks a real deployment.

## Tests

The suite is hermetic on purpose: `conftest.py` sets a placeholder
`DATABASE_URL` and redirects the DB dependency to in-memory SQLite, so no test
needs PostgreSQL, the network, or a Gemini key. Keep it that way — a test that
reaches out to a real service is a test that fails in CI for reasons unrelated to
your change.

What to write, by kind of change:

- **A number the report claims** → assert the number. `tests/test_retrieval.py`
  hand-computes BM25 and nDCG scores and asserts them, because "IDF is positive
  for a corpus-wide term" is a statement about a value.
- **A rule or boundary** → assert the boundary. `tests/test_evaluation_metrics.py`
  uses the ACMG allele-frequency thresholds as its cases.
- **A new API surface** → an integration test through the FastAPI test client,
  including the authorisation boundary (another account must get a 404 or 403,
  not the row).
- **Frontend changes** → `tests/test_frontend.py` enforces a contract instead of
  a style: every CSS class used in `index.html`/`app.js` must be styled, every
  called function must be defined, and a destructive action must open a
  confirmation before it calls `authFetch`. Running `make test` after editing
  `frontend/` will tell you exactly which class or function you left dangling.

## Style

Match the file you are editing. Beyond that, three house rules:

- **Docstrings explain why, not what.** The interesting information is the
  trade-off that was rejected. If a function has a subtle reason for existing,
  that reason belongs above it.
- **Type hints on new module-level functions**, at least on signatures. The
  `retrieval/` package is checked by mypy in full; other modules are annotated
  in patches.
- **British spelling in prose comments**, matching the existing files.

Commits follow the prefixes already in the log: `feat:`, `fix:`, `docs:`,
`test:`, `refactor:`, `chore:`. A commit message should say what changed and, for
anything non-obvious, why the other option was worse.

## Clinical safety — non-negotiable

This is decision support, not a diagnosis. Every output path carries a
disclaimer and a `review_required` flag, and the system is built to be
*conservative*: the Verifier can only lower confidence, never raise it, and VUS
is never presented as high confidence.

Do not remove or soften those guards to make a demo look better. Do not add a
code path that presents a classification as clinical certainty, and do not let a
confidence number rise without evidence to support it. If a change makes the
system more confident, it needs a test proving the evidence justifies it.

## Security

Report vulnerabilities privately — see [SECURITY.md](SECURITY.md). Do not commit
credentials, do not weaken the CSRF or rate-limit controls for convenience, and
do not add a dependency that phones home without saying so in the PR.

## Reporting a problem

Open an issue with the query, what you expected, and what happened. For a
classification you believe is wrong, include the variant, the evidence the tool
retrieved, and the ClinVar record — the interesting bugs in this project are
about *evidence selection*, and without the retrieved evidence it is not
possible to tell a bad rule from a missed lookup.
