"""
Backfill review sign-offs from the old local JSON store into PostgreSQL.

Before migration 0002, sign-offs lived in data/review_signoffs.json -- a
gitignored local file with no ownership and no backup. This script imports it
once so nothing is lost when upgrading.

Idempotent and safe to re-run: an existing row for a variant_id is updated
rather than duplicated. reviewed_by is left NULL because the JSON store never
recorded which account saved the sign-off -- the reviewer name and date are
carried over intact, which is what the attestation actually rests on.

    python scripts/import_review_signoffs.py
    python scripts/import_review_signoffs.py --path /old/backup/review_signoffs.json
"""

import argparse
import json
import os
import sys
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from db.database import SessionLocal  # noqa: E402
from db.models import ReviewSignoff  # noqa: E402

DEFAULT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "review_signoffs.json",
)


def _parse_review_date(value):
    """Accept the YYYY-MM-DD the API stores; anything unparseable is skipped."""
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _parse_saved_at(value):
    """The old store wrote e.g. 2026-10-03T12:30:00Z."""
    if not value:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", default=DEFAULT_PATH,
                        help="path to the legacy review_signoffs.json")
    args = parser.parse_args()

    if not os.path.exists(args.path):
        print(f"nothing to import: {args.path} does not exist")
        return 0

    try:
        with open(args.path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except json.JSONDecodeError as exc:
        print(f"could not parse {args.path}: {exc}")
        return 1
    if not isinstance(data, dict):
        print(f"unexpected format in {args.path}: expected an object keyed by variant_id")
        return 1

    imported = updated = skipped = 0
    db = SessionLocal()
    try:
        for variant_id, payload in data.items():
            if not isinstance(payload, dict):
                skipped += 1
                continue
            review_date = _parse_review_date(payload.get("review_date"))
            reviewer = (payload.get("reviewer_name") or "").strip()
            if review_date is None or not reviewer:
                # A sign-off without a reviewer or date is not an attestation.
                print(f"  skipped {variant_id}: missing reviewer_name or review_date")
                skipped += 1
                continue

            row = db.scalars(
                select(ReviewSignoff).where(ReviewSignoff.variant_id == variant_id)
            ).one_or_none()
            if row is None:
                row = ReviewSignoff(variant_id=variant_id)
                db.add(row)
                imported += 1
            else:
                updated += 1

            row.reviewer_name = reviewer
            row.review_date = review_date
            row.signature = (payload.get("signature") or "").strip() or None
            row.saved_at = _parse_saved_at(payload.get("saved_at"))
            # reviewed_by stays NULL: the old store never recorded the account.
            row.reviewed_by = None

        db.commit()
        total = db.query(ReviewSignoff).count()
    finally:
        db.close()

    print(f"imported {imported}, updated {updated}, skipped {skipped}")
    print(f"review_signoffs table now holds {total} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())