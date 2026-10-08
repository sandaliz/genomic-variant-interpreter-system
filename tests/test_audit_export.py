"""
Audit-export tests: the Professional/Enterprise "Audit export" entitlement.

Confirms the endpoint is gated by the entitlement and returns only the caller's
own rows, in both JSON and CSV.
"""

from db.models import AuthEvent, UsageRecord
from conftest import make_plan, make_user


def _plan(session, name, audit_export):
    return make_plan(session, name=name, limit=50, llm=True, audit_export=audit_export)


class TestAuditExportGate:
    def test_plan_without_the_entitlement_cannot_export(self, client, db_session, login):
        make_user(db_session, username="clinician",
                  plan=_plan(db_session, "Free", audit_export=False))
        response = client.get("/api/export/audit", headers=login())
        assert response.status_code == 403
        assert "audit export" in response.json()["detail"].lower()

    def test_requires_authentication(self, client, db_session):
        make_user(db_session, username="clinician",
                  plan=_plan(db_session, "Free", audit_export=False))
        assert client.get("/api/export/audit").status_code == 401


class TestAuditExportContent:
    def test_json_export_returns_only_the_callers_rows(self, client, db_session, login):
        plan = _plan(db_session, "Professional", audit_export=True)
        me = make_user(db_session, username="clinician", plan=plan)
        other = make_user(db_session, username="other", plan=plan)
        db_session.add(UsageRecord(user_id=me.id, request_id="mine", query_count=1))
        db_session.add(UsageRecord(user_id=other.id, request_id="theirs", query_count=1))
        db_session.add(AuthEvent(event_type="login_success", user_id=me.id,
                                 succeeded=True, detail="mine"))
        db_session.add(AuthEvent(event_type="login_success", user_id=other.id,
                                 succeeded=True, detail="theirs"))
        db_session.commit()

        # Signing in through the real endpoint writes its own auth event for the
        # caller, so the assertion is on *scoping* (the other account's rows must
        # never appear) rather than on an exact count.
        body = client.get("/api/export/audit", headers=login("clinician")).json()
        assert body["user"] == "clinician"
        assert len(body["usage"]) == 1
        details = [event["detail"] for event in body["auth_events"]]
        assert "mine" in details
        assert "theirs" not in details

    def test_csv_export_has_a_header_and_rows(self, client, db_session, login):
        plan = _plan(db_session, "Professional", audit_export=True)
        me = make_user(db_session, username="clinician", plan=plan)
        db_session.add(UsageRecord(user_id=me.id, request_id="r1", query_count=2))
        db_session.commit()

        response = client.get("/api/export/audit?format=csv", headers=login())
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")
        assert "attachment; filename=" in response.headers["content-disposition"]
        assert response.text.splitlines()[0] == "section,timestamp,detail"
        assert "usage" in response.text

    def test_bad_format_is_rejected(self, client, db_session, login):
        make_user(db_session, username="clinician",
                  plan=_plan(db_session, "Professional", audit_export=True))
        assert client.get("/api/export/audit?format=xml",
                          headers=login()).status_code == 422
