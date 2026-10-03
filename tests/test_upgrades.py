"""
Tests for the upgrade hand-off.

The behaviour that matters is the lifecycle, not the plumbing: a request must
not change anything on its own, an approval must actually move the account, and
a decided request must stay decided.
"""

import pytest

from db.models import UpgradeRequest
from conftest import make_user, plan_named


@pytest.fixture
def plans(db_session):
    return {
        "free": plan_named(db_session, "Free", limit=10, llm=False, price_cents=0),
        "pro": plan_named(db_session, "Professional", limit=500, llm=True,
                          price_cents=9900),
        "enterprise": plan_named(db_session, "Enterprise", limit=None, llm=True,
                                 price_cents=None),
    }


@pytest.fixture
def clinician(db_session, plans):
    return make_user(db_session, username="clinician", plan=plans["free"])


@pytest.fixture
def admin(db_session, plans, clinician):
    return make_user(db_session, username="root", plan=plans["enterprise"],
                     role="admin")


@pytest.fixture
def as_admin(token_for, admin):
    """Admin bearer headers. Depends on `admin` so the account actually exists."""
    return token_for("root")


def _request(client, plan_id, headers, note=None):
    return client.post("/api/upgrade-requests",
                       json={"target_plan_id": plan_id, "note": note},
                       headers=headers)


class TestCreateRequest:
    def test_requires_authentication(self, client, plans, clinician):
        assert _request(client, plans["pro"].id, {}).status_code == 401

    def test_creates_a_pending_request(self, client, plans, clinician, login):
        response = _request(client, plans["pro"].id, login(),
                            note="We evaluate 300 variants a month")
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["status"] == "pending"
        assert body["target_plan"] == "Professional"
        assert body["note"] == "We evaluate 300 variants a month"
        assert body["admin_note"] is None

    def test_requesting_does_not_change_the_plan(self, client, plans, clinician,
                                                 login, db_session):
        """The hand-off must not take effect until an admin decides."""
        _request(client, plans["pro"].id, login())
        db_session.refresh(clinician)
        assert clinician.plan_id == plans["free"].id

    def test_free_is_not_requestable(self, client, plans, clinician, login):
        response = _request(client, plans["free"].id, login())
        assert response.status_code == 409
        assert "default plan" in response.json()["detail"].lower()

    def test_cannot_request_the_plan_already_on(self, client, plans, db_session,
                                               login):
        make_user(db_session, username="pro_user", plan=plans["pro"])
        response = client.post("/api/upgrade-requests",
                               json={"target_plan_id": plans["pro"].id},
                               headers=login("pro_user"))
        assert response.status_code == 409
        assert "already on" in response.json()["detail"].lower()

    def test_unknown_plan_is_404(self, client, clinician, login):
        assert client.post("/api/upgrade-requests",
                           json={"target_plan_id": 9999},
                           headers=login()).status_code == 404

    def test_one_open_request_at_a_time(self, client, plans, clinician, login):
        assert _request(client, plans["pro"].id, login()).status_code == 201
        second = _request(client, plans["enterprise"].id, login())
        assert second.status_code == 409
class TestMyRequests:
    def test_lists_this_accounts_requests(self, client, plans, clinician, login):
        _request(client, plans["pro"].id, login(), note="first")
        body = client.get("/api/upgrade-requests/mine", headers=login()).json()
        assert body["total"] == 1
        assert body["requests"][0]["note"] == "first"

    def test_does_not_leak_other_accounts(self, client, plans, clinician, admin,
                                          login):
        _request(client, plans["pro"].id, login())
        body = client.get("/api/upgrade-requests/mine",
                          headers=login("root")).json()
        assert body["total"] == 0

    def test_requires_authentication(self, client):
        assert client.get("/api/upgrade-requests/mine").status_code == 401


class TestAdminQueue:
    def test_is_admin_only(self, client, admin, clinician, login):
        assert client.get("/api/admin/upgrade-requests").status_code == 401
        assert client.get("/api/admin/upgrade-requests",
                          headers=login()).status_code == 403
        assert client.get("/api/admin/upgrade-requests",
                          headers=login("root")).status_code == 200

    def test_includes_requester_contact_details(self, client, plans, clinician, admin,
                                                login):
        _request(client, plans["pro"].id, login(), note="need more volume")
        body = client.get("/api/admin/upgrade-requests",
                          headers=login("root")).json()
        row = body["requests"][0]
        assert row["username"] == "clinician"
        assert row["email"] == "clinician@example.com"
        assert row["current_plan"] == "Free"
        assert row["target_plan"] == "Professional"

    def test_filters_by_status(self, client, plans, clinician, admin, login):
        _request(client, plans["pro"].id, login())
        everything = client.get("/api/admin/upgrade-requests",
                                 headers=login("root")).json()
        pending = client.get("/api/admin/upgrade-requests?status=pending",
                             headers=login("root")).json()
        approved = client.get("/api/admin/upgrade-requests?status=approved",
                              headers=login("root")).json()
        assert everything["total"] == 1
        assert pending["total"] == 1
        assert approved["total"] == 0

    def test_rejects_an_unknown_status_filter(self, client, admin, login):
        assert client.get("/api/admin/upgrade-requests?status=bogus",
                          headers=login("root")).status_code == 422

    def test_limit_is_validated(self, client, admin, login):
        for bad in (0, 501):
            assert client.get(f"/api/admin/upgrade-requests?limit={bad}",
                              headers=login("root")).status_code == 422


class TestAdminDecision:
    def test_requires_admin(self, client, plans, clinician, login, as_admin):
        created = _request(client, plans["pro"].id, login()).json()
        url = f"/api/admin/upgrade-requests/{created['id']}"
        assert client.patch(url, json={"status": "approved"}).status_code == 401
        assert client.patch(url, json={"status": "approved"},
                            headers=login()).status_code == 403
        assert client.patch(url, json={"status": "approved"},
                            headers=as_admin).status_code == 200

    def test_approval_applies_the_plan(self, client, plans, clinician, login,
                                       as_admin, db_session):
        created = _request(client, plans["pro"].id, login()).json()
        client.patch(f"/api/admin/upgrade-requests/{created['id']}",
                     json={"status": "approved"}, headers=as_admin)
        db_session.refresh(clinician)
        assert clinician.plan_id == plans["pro"].id

    def test_approval_grants_the_new_plan_entitlements(self, client, plans,
                                                       clinician, login, as_admin):
        """The plan swap must actually change what the caller may do."""
        created = _request(client, plans["pro"].id, login()).json()
        assert client.get("/api/auth/me", headers=login()).json()[
            "entitlements"]["llm_enabled"] is False
        client.patch(f"/api/admin/upgrade-requests/{created['id']}",
                     json={"status": "approved"}, headers=as_admin)
        after = client.get("/api/auth/me", headers=login()).json()
        assert after["plan"]["name"] == "Professional"
        assert after["entitlements"]["llm_enabled"] is True

    def test_decline_leaves_the_plan_alone(self, client, plans, clinician, login,
                                           as_admin, db_session):
        created = _request(client, plans["pro"].id, login()).json()
        client.patch(f"/api/admin/upgrade-requests/{created['id']}",
                     json={"status": "declined", "admin_note": "Budget for now"},
                     headers=as_admin)
        db_session.refresh(clinician)
        assert clinician.plan_id == plans["free"].id
        assert db_session.query(UpgradeRequest).one().admin_note == "Budget for now"

    def test_contacted_keeps_it_open(self, client, plans, clinician, login,
                                     as_admin, db_session):
        created = _request(client, plans["pro"].id, login()).json()
        client.patch(f"/api/admin/upgrade-requests/{created['id']}",
                     json={"status": "contacted", "admin_note": "Called them"},
                     headers=as_admin)
        row = db_session.query(UpgradeRequest).one()
        assert row.status == "contacted"
        assert row.is_open is True
        db_session.refresh(clinician)
        assert clinician.plan_id == plans["free"].id

    def test_decided_requests_are_terminal(self, client, plans, clinician, login,
                                           as_admin):
        created = _request(client, plans["pro"].id, login()).json()
        url = f"/api/admin/upgrade-requests/{created['id']}"
        assert client.patch(url, json={"status": "approved"},
                            headers=as_admin).status_code == 200
        second = client.patch(url, json={"status": "declined"}, headers=as_admin)
        assert second.status_code == 409
        assert "already approved" in second.json()["detail"]

    def test_invalid_status_is_422(self, client, plans, clinician, login, as_admin):
        created = _request(client, plans["pro"].id, login()).json()
        response = client.patch(f"/api/admin/upgrade-requests/{created['id']}",
                                json={"status": "banana"}, headers=as_admin)
        assert response.status_code == 422

    def test_unknown_request_is_404(self, client, admin, as_admin):
        assert client.patch("/api/admin/upgrade-requests/9999",
                            json={"status": "approved"},
                            headers=as_admin).status_code == 404

    def test_admin_note_reaches_the_requester(self, client, plans, clinician, login,
                                              as_admin):
        """A decision the user cannot see is not a hand-off."""
        created = _request(client, plans["pro"].id, login()).json()
        client.patch(f"/api/admin/upgrade-requests/{created['id']}",
                     json={"status": "approved", "admin_note": "Welcome aboard"},
                     headers=as_admin)
        mine = client.get("/api/upgrade-requests/mine", headers=login()).json()
        assert mine["requests"][0]["admin_note"] == "Welcome aboard"
        assert mine["requests"][0]["status"] == "approved"

    def test_decision_records_a_timestamp(self, client, plans, clinician, login,
                                          as_admin, db_session):
        created = _request(client, plans["pro"].id, login()).json()
        assert db_session.query(UpgradeRequest).one().decided_at is None
        client.patch(f"/api/admin/upgrade-requests/{created['id']}",
                     json={"status": "approved"}, headers=as_admin)
        assert db_session.query(UpgradeRequest).one().decided_at is not None