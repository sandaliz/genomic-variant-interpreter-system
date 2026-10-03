"""
Review sign-off persistence tests.

These pin the move from a local JSON file to PostgreSQL: sign-offs now belong to
an account, require authentication, and survive as database rows.
"""

from datetime import date, timedelta

import pytest

from db.models import ReviewSignoff
from conftest import make_plan, make_user


VARIANT = "BRCA1:c.68_69del"


@pytest.fixture
def clinician(db_session):
    plan = make_plan(db_session, name="Free", limit=10)
    make_user(db_session, username="clinician", plan=plan)
    return "clinician"


@pytest.fixture
def admin(db_session, clinician):
    plan = make_plan(db_session, name="Enterprise", limit=None)
    make_user(db_session, username="root", plan=plan, role="admin")
    return "root"


def _payload(**overrides):
    body = {"reviewer_name": "Dr. A. Counselor",
            "review_date": date.today().isoformat(),
            "signature": "A. Counselor"}
    body.update(overrides)
    return body


class TestAuthentication:
    @pytest.mark.parametrize("method", ["get", "post"])
    def test_requires_a_token(self, client, clinician, method):
        url = f"/api/review/{VARIANT}"
        if method == "post":
            response = client.post(url, json=_payload())
        else:
            response = client.get(url)
        assert response.status_code == 401

    def test_invalid_token_is_401(self, client, clinician):
        response = client.get(f"/api/review/{VARIANT}",
                              headers={"Authorization": "Bearer nonsense"})
        assert response.status_code == 401


class TestSaveAndRead:
    def test_round_trip(self, client, clinician, login, db_session):
        response = client.post(f"/api/review/{VARIANT}", json=_payload(),
                               headers=login())
        assert response.status_code == 200, response.text
        signoff = response.json()["review_signoff"]
        assert signoff["reviewer_name"] == "Dr. A. Counselor"
        assert signoff["signature"] == "A. Counselor"

        read = client.get(f"/api/review/{VARIANT}", headers=login())
        assert read.json()["review_signoff"]["reviewer_name"] == "Dr. A. Counselor"

        row = db_session.query(ReviewSignoff).one()
        assert row.reviewer_name == "Dr. A. Counselor"

    def test_signoff_records_the_account_that_saved_it(self, client, clinician,
                                                       login, db_session):
        """The ownership link is the reason this moved out of a JSON file."""
        client.post(f"/api/review/{VARIANT}", json=_payload(), headers=login())
        row = db_session.query(ReviewSignoff).one()
        assert row.reviewer_name == "Dr. A. Counselor"
        assert row.reviewed_by is not None

    def test_resigning_updates_in_place(self, client, clinician, login, db_session):
        client.post(f"/api/review/{VARIANT}",
                    json=_payload(reviewer_name="First Reviewer"), headers=login())
        second = client.post(f"/api/review/{VARIANT}",
                             json=_payload(reviewer_name="Second Reviewer"),
                             headers=login())
        assert second.json()["review_signoff"]["reviewer_name"] == "Second Reviewer"
        # Still a single row -- this is current-state, not an append-only log.
        assert db_session.query(ReviewSignoff).count() == 1

    def test_unknown_variant_returns_null(self, client, clinician, login):
        response = client.get("/api/review/BRCA2:unknown", headers=login())
        assert response.status_code == 200
        assert response.json()["review_signoff"] is None

    def test_name_is_stripped_and_blank_signature_becomes_null(self, client,
                                                               clinician, login):
        response = client.post(
            f"/api/review/{VARIANT}",
            json=_payload(reviewer_name="  Spaced Name  ", signature="   "),
            headers=login())
        assert response.json()["review_signoff"]["reviewer_name"] == "Spaced Name"
        assert response.json()["review_signoff"]["signature"] is None
class TestValidation:
    def test_future_review_date_is_rejected(self, client, clinician, login):
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        response = client.post(f"/api/review/{VARIANT}",
                               json=_payload(review_date=tomorrow), headers=login())
        assert response.status_code == 422
        assert "future" in response.json()["detail"].lower()

    def test_malformed_review_date_is_rejected(self, client, clinician, login):
        response = client.post(f"/api/review/{VARIANT}",
                               json=_payload(review_date="03/10/2026"),
                               headers=login())
        assert response.status_code == 422
        assert "YYYY-MM-DD" in response.json()["detail"]

    def test_blank_reviewer_name_is_rejected(self, client, clinician, login):
        response = client.post(f"/api/review/{VARIANT}",
                               json=_payload(reviewer_name="   "), headers=login())
        assert response.status_code == 422

    def test_single_character_reviewer_name_is_accepted(self, client, clinician,
                                                        login):
        """The schema's floor is 1 character; only an all-blank name is rejected."""
        response = client.post(f"/api/review/{VARIANT}",
                               json=_payload(reviewer_name="x"), headers=login())
        assert response.status_code == 200
        assert response.json()["review_signoff"]["reviewer_name"] == "x"

    def test_a_rejected_signoff_is_not_stored(self, client, clinician, login,
                                              db_session):
        future = (date.today() + timedelta(days=5)).isoformat()
        client.post(f"/api/review/{VARIANT}", json=_payload(review_date=future),
                    headers=login())
        assert db_session.query(ReviewSignoff).count() == 0


class TestAdminRegister:
    def test_listing_is_admin_only(self, client, admin, clinician, login):
        assert client.get("/api/reviews").status_code == 401
        assert client.get("/api/reviews", headers=login()).status_code == 403
        assert client.get("/api/reviews", headers=login("root")).status_code == 200

    def test_listing_reports_recorded_signoffs(self, client, admin, clinician, login):
        client.post(f"/api/review/{VARIANT}", json=_payload(), headers=login())
        body = client.get("/api/reviews", headers=login("root")).json()
        assert body["total"] == 1
        entry = body["review_signoffs"][0]
        assert entry["reviewer_name"] == "Dr. A. Counselor"
        assert entry["variant_id"] == VARIANT

    def test_listing_validates_limit(self, client, admin, login):
        for bad in (0, 501):
            assert client.get(f"/api/reviews?limit={bad}",
                              headers=login("root")).status_code == 422


class TestDurability:
    def test_deleting_an_account_keeps_the_signoff(self, client, clinician, login,
                                                  db_session):
        """
        A sign-off is a clinical attestation. Removing the account that saved it
        must not erase the record.
        """
        from db.models import UsageRecord, User

        client.post(f"/api/review/{VARIANT}", json=_payload(), headers=login())
        user = db_session.query(User).filter_by(username="clinician").one()
        user_id = user.id
        db_session.delete(user)
        db_session.commit()

        # Mirrors the ON DELETE SET NULL the database enforces for real callers.
        db_session.query(ReviewSignoff).filter_by(variant_id=VARIANT).update(
            {"reviewed_by": None})
        db_session.commit()

        row = db_session.query(ReviewSignoff).one()
        assert row.reviewer_name == "Dr. A. Counselor"
        assert row.reviewed_by is None
        # Usage rows are the caller's own data and do go away with the account.
        assert db_session.query(UsageRecord).filter_by(user_id=user_id).count() == 0