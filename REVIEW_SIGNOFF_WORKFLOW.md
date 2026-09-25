# Human Review / Genetic Counselor Sign-off

The **Reviewer / genetic counselor** and **Review date** fields are not merely decorative. They are a human-in-the-loop sign-off layer for the academic prototype.

## What is stored?

After a variant is analyzed, the UI can save:

- reviewer name
- review date (`YYYY-MM-DD`)
- optional signature/initials
- server save timestamp
- the variant ID used as the key

The prototype stores these records locally in `data/review_signoffs.json`. That file is ignored by Git so real reviewer entries are not accidentally committed.

## What appears in the report?

The saved sign-off is returned with the variant result and is inserted into the generated PDF. The browser report also displays the saved values. If no reviewer has signed off, the report clearly shows that human review is pending.

## Important limitation

This is demonstration storage for an academic project, not an electronic medical-record or validated clinical sign-off system. A production implementation would require authenticated users, role-based access control, immutable audit logging, encryption/key management, retention policies, and a validated clinical records platform.
