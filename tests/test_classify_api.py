"""
HTTP-surface tests for the authenticated pipeline endpoints.

The four agents are stubbed out: these tests are about who may call
/api/classify and what the plan gate does, not about classification accuracy
(covered by the agent test modules).
"""

from datetime import timedelta
import uuid

import pytest

from db.models import UsageRecord, User
from services.entitlement_service import month_start_utc, usage_this_month
from conftest import make_plan, make_user


def _record_last_month(db_session, user_id):
    """
    Insert a usage row dated inside the previous UTC month.

    usage_this_month() only counts rows from month_start_utc() onwards, so this
    proves a later month charges again rather than inheriting an older id.
    """
    row = UsageRecord(user_id=user_id, request_id="prior-month-row", query_count=1)
    row.created_at = month_start_utc() - timedelta(days=1)
    db_session.add(row)
    db_session.commit()


class _StubSystem:
    """Stands in for a VariantInterpretationSystem variant."""

    def __init__(self, name, calls, result=None):
        self.name = name
        self.calls = calls
        self.result = result or stub_result()

    def run(self, raw_query, api_key, user_id=""):
        self.calls.append({"query": raw_query, "api_key": api_key,
                           "user_id": user_id})
        return dict(self.result)


def stub_result(**overrides):
    result = {
        "variant_id": "BRCA1:c.68_69del",
        "report_text": "Stub report text.",
        "evidence_summary": "Stub evidence summary.",
        "classification": "Pathogenic",
        "confidence": "moderate",
        "confidence_numeric": 60.0,
        "criteria_applied": ["PVS1"],
        "pipeline_steps": [{"stage": "intake"}],
        "audit_trail": [],
        "top_k_results": [],
        "ir_metadata": {},
        "responsible_ai": {},
        "intent": "variant_classification",
        "extracted_entities": {"gene": "BRCA1"},
    }
    result.update(overrides)
    return result


@pytest.fixture
def stub_systems(monkeypatch):
    """Replace both system variants with recording stubs."""
    import api

    # Each stub keeps its own list so a test can assert that the *other*
    # system variant was never invoked at all.
    llm = _StubSystem("llm", [])
    rules = _StubSystem("rules", [])
    monkeypatch.setattr(api, "_system_llm", llm)
    monkeypatch.setattr(api, "_system_rules", rules)
    return {"llm": llm, "rules": rules,
            "calls": llm.calls + rules.calls}


@pytest.fixture
def free_user(db_session):
    plan = make_plan(db_session, name="Free", limit=3, llm=False)
    make_user(db_session, username="clinician", plan=plan)
    return "clinician"


def login_user_id(client, headers):
    return client.get("/api/auth/me", headers=headers).json()["user"]["id"]


QUERY = {"query": "Is BRCA1 c.68_69del pathogenic?"}


class TestClassifyRequiresAuthentication:
    def test_missing_token_is_401(self, client, free_user):
        assert client.post("/api/classify", json=QUERY).status_code == 401

    def test_invalid_token_is_401(self, client, free_user):
        response = client.post("/api/classify", json=QUERY,
                               headers={"Authorization": "Bearer nonsense"})
        assert response.status_code == 401

    def test_api_key_alone_no_longer_authenticates(self, client, free_user):
        # The legacy X-API-Key header must not become a back door.
        response = client.post("/api/classify", json=QUERY,
                               headers={"X-API-Key": "demo-clinician-key"})
        assert response.status_code == 401
class TestClassifyPlanGate:
    def test_free_plan_runs_on_the_rules_only_system(self, client, stub_systems,
                                                      free_user, login):
        response = client.post("/api/classify", json=QUERY, headers=login())
        assert response.status_code == 200, response.text
        assert response.json()["plan"] == "Free"
        assert response.json()["entitlements"]["llm_enabled"] is False
        assert stub_systems["rules"].calls, "rules-only plan must use the rules system"
        assert not stub_systems["llm"].calls, "LLM system must stay untouched"

    def test_professional_plan_runs_on_the_llm_system(self, client, stub_systems,
                                                      db_session, login):
        make_user(db_session, username="pro", plan=make_plan(
            db_session, name="Professional", limit=50, llm=True, price_cents=4900))
        response = client.post("/api/classify", json=QUERY, headers=login("pro"))
        assert response.status_code == 200, response.text
        assert response.json()["entitlements"]["llm_enabled"] is True
        assert stub_systems["llm"].calls
        assert not stub_systems["rules"].calls

    def test_identity_is_passed_to_the_agent_layer(self, client, stub_systems, free_user,
                                                   login, db_session):
        client.post("/api/classify", json=QUERY, headers=login())
        expected = str(db_session.query(User).filter_by(username="clinician").one().id)
        assert stub_systems["rules"].calls[0]["user_id"] == expected

    def test_agent_layer_denial_is_surfaced_as_400(self, client, stub_systems, free_user,
                                                   login, monkeypatch):
        # Defence-in-depth: the agent layer keeps its own credential check, so a
        # refusal there is an upstream failure (400), never a silent success.
        import api

        monkeypatch.setattr(api._system_rules, "run",
                            lambda *a, **k: {"error": "Access denied: unknown API key."})
        response = client.post("/api/classify", json=QUERY, headers=login())
        assert response.status_code == 400
        assert "denied" in response.json()["detail"].lower()


class TestClassifyQuota:
    def test_successful_call_records_usage(self, client, stub_systems, free_user, login,
                                          db_session):
        assert client.post("/api/classify", json=QUERY, headers=login()).status_code == 200
        assert usage_this_month(db_session, login_user_id(client, login())) == 1

    def test_repeating_the_same_query_is_charged_every_time(
            self, client, stub_systems, free_user, login, db_session):
        """
        Regression: request_id used to be uuid5(user_id + query text), so asking
        the same question again produced the same id, hit the unique constraint
        and was silently not charged. Three identical calls cost one.
        """
        for _ in range(3):
            assert client.post("/api/classify", json=QUERY,
                               headers=login()).status_code == 200
        assert usage_this_month(db_session, login_user_id(client, login())) == 3

    def test_repeating_a_query_does_not_get_free_forever_across_months(
            self, client, stub_systems, free_user, login, db_session):
        """
        The old id was uuid5(user_id + query) with no month in it, so it collided
        with the identical query run in a previous month and went uncharged. With
        the row already there, this month's call must still be counted: 1.
        """
        user_id = login_user_id(client, login())
        _record_last_month(db_session, user_id)
        assert usage_this_month(db_session, user_id) == 0  # last month only
        assert client.post("/api/classify", json=QUERY,
                           headers=login()).status_code == 200
        assert usage_this_month(db_session, user_id) == 1

    def test_an_idempotency_key_still_makes_a_genuine_retry_free(
            self, client, stub_systems, free_user, login, db_session):
        """
        Retry-safety is worth keeping -- a client that cannot tell whether a
        timed-out request landed should not be billed twice for it. That is what
        the unique constraint is for, but only where the client opts in.
        """
        payload = dict(QUERY, idempotency_key="retry-me")
        for _ in range(3):
            assert client.post("/api/classify", json=payload,
                               headers=login()).status_code == 200
        assert usage_this_month(db_session, login_user_id(client, login())) == 1

    def test_different_idempotency_keys_are_charged_separately(
            self, client, stub_systems, free_user, login, db_session):
        for key in ("a", "b", "c"):
            client.post("/api/classify", json=dict(QUERY, idempotency_key=key),
                        headers=login())
        assert usage_this_month(db_session, login_user_id(client, login())) == 3

    def test_an_idempotency_key_is_scoped_to_the_month(
            self, client, stub_systems, free_user, login, db_session):
        """
        The key is folded into the request id together with the UTC month. If the
        month were left out, reusing a key next month would collide with its own
        row from the previous month and go uncharged -- the permanent-free bug
        reached by a different route.

        The planted row carries the id this key *would* have produced last month,
        so the two calls this month are only charged if the month really is part
        of the derivation.
        """
        user_id = login_user_id(client, login())
        key = "reused-across-months"
        last_month = (month_start_utc() - timedelta(days=1)).strftime("%Y-%m")
        prior = UsageRecord(
            user_id=user_id,
            request_id=str(uuid.uuid5(
                uuid.NAMESPACE_URL, f"{user_id}:{last_month}:{key}")),
            query_count=1,
        )
        prior.created_at = month_start_utc() - timedelta(days=1)
        db_session.add(prior)
        db_session.commit()

        payload = dict(QUERY, idempotency_key=key)
        assert client.post("/api/classify", json=payload,
                           headers=login()).status_code == 200
        # Last month's row must not suppress this month's charge. If the month
        # were absent from the derivation, this id would collide with the planted
        # row and the call would go uncharged, leaving usage at 0.
        assert usage_this_month(db_session, user_id) == 1

    def test_distinct_queries_are_charged_separately(self, client, stub_systems,
                                                     free_user, login, db_session):
        client.post("/api/classify", json=QUERY, headers=login())
        client.post("/api/classify",
                    json={"query": "Is BRCA2 c.5946del pathogenic?"}, headers=login())
        assert usage_this_month(db_session, login_user_id(client, login())) == 2

    def test_exhausted_quota_returns_429_and_skips_the_agents(self, client, stub_systems,
                                                              free_user, login,
                                                              db_session):
        user = db_session.query(User).filter_by(username="clinician").one()
        for i in range(3):  # the Free plan limit is 3
            db_session.add(UsageRecord(user_id=user.id, request_id=f"seed-{i}"))
        db_session.commit()
        calls_before = len(stub_systems["calls"])

        response = client.post("/api/classify", json=QUERY, headers=login())
        assert response.status_code == 429
        assert "quota" in response.json()["detail"].lower()
        # The expensive pipeline must not run for an over-quota caller.
        assert len(stub_systems["calls"]) == calls_before

    def test_response_reports_plan_and_usage(self, client, stub_systems, free_user, login):
        body = client.post("/api/classify", json=QUERY, headers=login()).json()
        assert body["plan"] == "Free"
        assert body["usage"] == {"used_this_month": 1, "monthly_limit": 3,
                                 "remaining": 2}
        assert body["entitlements"] == {"llm_enabled": False, "api_access": True,
                                        "audit_export": False}


class TestAuditEndpointsRequireAuth:
    @pytest.mark.parametrize("path", ["/api/trail/BRCA1:c.68_69del",
                                      "/api/pipeline/BRCA1:c.68_69del"])
    def test_missing_token_is_401(self, client, path):
        assert client.get(path).status_code == 401

    @pytest.mark.parametrize("path", ["/api/trail/BRCA1:c.68_69del",
                                      "/api/pipeline/BRCA1:c.68_69del"])
    def test_authenticated_caller_reaches_the_endpoint(self, client, free_user, login,
                                                      path):
        # A valid token passes auth; an unknown variant then yields a clean 404.
        assert client.get(path, headers=login()).status_code == 404