"""
API-key tests: issue, authenticate, revoke, and the api_access entitlement gate.

This exercises the programmatic-access surface -- the feature the "API access"
line on the paid plans actually gates -- through the real HTTP API on the
in-memory SQLite session from conftest.py.
"""

from conftest import make_plan, make_user


def _free_plan(session):
    return make_plan(session, name="Free", limit=3, api_access=False)


def _pro_plan(session):
    return make_plan(session, name="Professional", limit=50, llm=True, api_access=True)


class TestKeyCreation:
    def test_free_plan_cannot_create_a_key(self, client, db_session, login):
        make_user(db_session, username="clinician", plan=_free_plan(db_session))
        response = client.post("/api/keys", json={"name": "LIMS"}, headers=login())
        assert response.status_code == 403
        assert "api access" in response.json()["detail"].lower()

    def test_professional_plan_creates_a_key_shown_once(self, client, db_session, login):
        make_user(db_session, username="clinician", plan=_pro_plan(db_session))
        response = client.post("/api/keys", json={"name": "LIMS"}, headers=login())
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["key"].startswith("vl_")
        assert body["revoked"] is False
        # The list endpoint must never return the secret again -- only metadata.
        listed = client.get("/api/keys", headers=login()).json()
        assert listed["total"] == 1
        assert "key" not in listed["keys"][0]
        assert listed["keys"][0]["prefix"] == body["prefix"]

    def test_key_creation_requires_authentication(self, client, db_session):
        make_user(db_session, username="clinician", plan=_pro_plan(db_session))
        assert client.post("/api/keys", json={"name": "x"}).status_code == 401


class TestKeyAuthentication:
    def test_key_authenticates_a_request(self, client, db_session, login):
        make_user(db_session, username="clinician", plan=_pro_plan(db_session))
        key = client.post(
            "/api/keys", json={"name": "LIMS"}, headers=login()
        ).json()["key"]
        me = client.get("/api/auth/me", headers={"X-API-Key": key})
        assert me.status_code == 200, me.text
        assert me.json()["user"]["username"] == "clinician"

    def test_key_stops_working_immediately_on_downgrade(self, client, db_session, login):
        plan = _pro_plan(db_session)
        make_user(db_session, username="clinician", plan=plan)
        key = client.post(
            "/api/keys", json={"name": "LIMS"}, headers=login()
        ).json()["key"]
        assert client.get("/api/auth/me", headers={"X-API-Key": key}).status_code == 200

        # The entitlement is re-checked on every request, not only at issue time,
        # so leaving the paid plan revokes programmatic access at once.
        plan.api_access = False
        db_session.commit()
        assert client.get("/api/auth/me", headers={"X-API-Key": key}).status_code == 401

    def test_unknown_key_is_401(self, client, db_session):
        make_user(db_session, username="clinician", plan=_pro_plan(db_session))
        assert client.get(
            "/api/auth/me", headers={"X-API-Key": "vl_not-a-real-key"}
        ).status_code == 401

    def test_revoked_key_stops_working(self, client, db_session, login):
        make_user(db_session, username="clinician", plan=_pro_plan(db_session))
        created = client.post(
            "/api/keys", json={"name": "LIMS"}, headers=login()
        ).json()
        headers = {"X-API-Key": created["key"]}
        assert client.get("/api/auth/me", headers=headers).status_code == 200

        revoke = client.delete(f"/api/keys/{created['id']}", headers=login())
        assert revoke.status_code == 200, revoke.text
        assert client.get("/api/auth/me", headers=headers).status_code == 401

    def test_revoking_twice_is_404(self, client, db_session, login):
        make_user(db_session, username="clinician", plan=_pro_plan(db_session))
        created = client.post(
            "/api/keys", json={"name": "LIMS"}, headers=login()
        ).json()
        assert client.delete(f"/api/keys/{created['id']}", headers=login()).status_code == 200
        assert client.delete(f"/api/keys/{created['id']}", headers=login()).status_code == 404


class TestKeyScoping:
    def test_one_account_cannot_revoke_anothers_key(self, client, db_session, login):
        pro = _pro_plan(db_session)
        make_user(db_session, username="owner", plan=pro)
        make_user(db_session, username="intruder", plan=pro)
        created = client.post(
            "/api/keys", json={"name": "LIMS"}, headers=login("owner")
        ).json()
        # Scoped by user_id: the intruder cannot revoke by guessing the id.
        response = client.delete(f"/api/keys/{created['id']}", headers=login("intruder"))
        assert response.status_code == 404

    def test_listing_only_returns_own_keys(self, client, db_session, login):
        pro = _pro_plan(db_session)
        make_user(db_session, username="owner", plan=pro)
        make_user(db_session, username="other", plan=pro)
        client.post("/api/keys", json={"name": "owner-key"}, headers=login("owner"))
        client.post("/api/keys", json={"name": "other-key"}, headers=login("other"))
        listed = client.get("/api/keys", headers=login("owner")).json()
        assert listed["total"] == 1
        assert listed["keys"][0]["name"] == "owner-key"
