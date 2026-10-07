# Human Review / Genetic Counselor Sign-off

The **Reviewer / genetic counselor** and **Review date** fields are not merely decorative. They are a human-in-the-loop sign-off layer for the academic prototype.

## What is stored?

After a variant is analyzed, the UI can save:

- reviewer name
- review date (`YYYY-MM-DD`)
- optional signature/initials
- server save timestamp
- the variant ID used as the key
- **which account recorded it** (`reviewed_by`)

These live in the `review_signoffs` table in PostgreSQL. Before that they were
kept in a local `data/review_signoffs.json` file, which meant an attestation had
no owner, no backup and no audit trail. Run
`python scripts/import_review_signoffs.py` once after upgrading to backfill any
sign-offs recorded under the old store; `reviewed_by` is left null for those,
because the file never recorded who saved them.

Two properties are enforced rather than hoped for:

- **One sign-off per variant.** Re-signing updates the row in place, so the
  current state is always a single record.
- **Deleting an account does not delete a sign-off.** The foreign key is
  `ON DELETE SET NULL`: the reviewer name and date survive, only the link to the
  account is cleared. Removing an account must never be able to erase a clinical
  attestation.

Saving a sign-off requires a signed-in account, so the record always has an
authorised author behind it.

## What appears in the report?

The saved sign-off is returned with the variant result and is inserted into the generated PDF. The browser report also displays the saved values. If no reviewer has signed off, the report clearly shows that human review is pending.

`GET /api/reviews` (administrators only) lists every recorded sign-off, newest
first, as an attestation register.

## Important limitation

This is demonstration storage for an academic project, not an electronic medical-record or validated clinical sign-off system. It now has authenticated authors and database persistence, but a production implementation would still require immutable audit logging, encryption and key management, retention policies, electronic signatures, and a validated clinical records platform.
