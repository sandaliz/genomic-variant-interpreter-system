"""
Security regression tests for the P0 fixes.

Covers two findings from the pre-submission audit:

  1. POST /api/report/pdf previously had no auth dependency, so anonymous
     callers could generate PDFs and enumerate stored reviewer names and
     signatures by variant_id. It must now answer 401 without credentials.
  2. GET /api/trail/{variant_id} and GET /api/pipeline/{variant_id} read a
     shared in-memory log keyed only by variant_id. They must now answer 404
     for a variant the caller never ran, so one account cannot read another
     account's decrypted payloads or raw_query.
"""

from db.models import QueryHistory
from conftest import make_plan, make_user


def _free_plan(session):
    return make_plan(session, name="Free", limit=50, llm=False)


def _pdf_payload(variant_id="BRCA1:c.68_69del"):
    return {
        "variant_id": variant_id,
        "classification": "Uncertain Significance",
        "confidence": "low",
        "confidence_numeric": 20.0,
        "reasoning_mode": "rule_based",
        "report_text": "Stub report.",
    }


class TestPdfRequiresAuthentication:
    def test_anonymous_pdf_is_401(self, client, db_session):
        make_user(db_session, username="clinician", plan=_free_plan(db_session))
        assert client.post("/api/report/pdf", json=_pdf_payload()).status_code == 401

    def test_authenticated_pdf_is_generated(self, client, db_session, login):
        make_user(db_session, username="clinician", plan=_free_plan(db_session))
        response = client.post("/api/report/pdf", json=_pdf_payload(), headers=login())
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("application/pdf")

    def test_variant_id_markup_is_escaped(self, client, db_session, login):
        """A variant_id containing markup must be treated as literal text."""
        from api import _pdf_text

        assert _pdf_text("<script>alert(1)</script>") == (
            "&lt;script&gt;alert(1)&lt;/script&gt;"
        )
        make_user(db_session, username="clinician", plan=_free_plan(db_session))
        payload = _pdf_payload(variant_id="<b>BRCA1:x</b>")
        response = client.post("/api/report/pdf", json=payload, headers=login())
        assert response.status_code == 200, response.text


class TestTrailIsScopedToCaller:
    def _seed_history(self, db_session, user_id, variant_id, trace_id=None):
        db_session.add(
            QueryHistory(
                user_id=user_id,
                request_id=f"req-{user_id}-{trace_id or 'none'}-{variant_id}",
                query_text="Is BRCA1 pathogenic?",
                variant_id=variant_id,
                trace_id=trace_id,
            )
        )
        db_session.commit()

    def test_other_users_variant_is_404_on_trail(
        self, client, db_session, login, token_for
    ):
        plan = _free_plan(db_session)
        me = make_user(db_session, username="clinician", plan=plan)
        other = make_user(db_session, username="other", plan=plan)
        self._seed_history(db_session, other.id, "BRCA1:c.68_69del", trace_id="trace-other")
        # Caller ran nothing: even though messages may exist in the shared
        # in-memory log, ownership comes from query_history, so this is 404.
        assert (
            client.get("/api/trail/BRCA1:c.68_69del", headers=login()).status_code
            == 404
        )
        assert (
            client.get("/api/pipeline/BRCA1:c.68_69del", headers=login()).status_code
            == 404
        )
        assert me.id != other.id

    def test_same_variant_two_users_see_only_own_run(
        self, client, db_session, login, token_for, monkeypatch
    ):
        """Overlapping variant_id: each caller sees only their own trace.

        Regression test for the variant-level scoping hole: the shared
        in-memory log holds both runs under one variant_id, so the endpoint
        must filter by the caller's owned trace_ids, not by variant alone.
        """
        import api
        from protocol.message import AgentMessage

        plan = _free_plan(db_session)
        me = make_user(db_session, username="clinician", plan=plan)
        other = make_user(db_session, username="other", plan=plan)
        self._seed_history(db_session, me.id, "BRCA1:c.68_69del", trace_id="trace-mine")
        self._seed_history(
            db_session, other.id, "BRCA1:c.68_69del", trace_id="trace-theirs"
        )
        mine = AgentMessage(
            variant_id="BRCA1:c.68_69del",
            stage="intake_complete",
            sender="intake_agent",
            trace_id="trace-mine",
            payload={"raw_query": "my secret query"},
        )
        theirs = AgentMessage(
            variant_id="BRCA1:c.68_69del",
            stage="intake_complete",
            sender="intake_agent",
            trace_id="trace-theirs",
            payload={"raw_query": "their secret query"},
        )
        monkeypatch.setattr(api, "_history_for", lambda variant_id: [mine, theirs])

        body = client.get("/api/trail/BRCA1:c.68_69del", headers=login()).json()
        assert [m["trace_id"] for m in body] == ["trace-mine"]
        assert "their secret query" not in str(body)

        other_headers = token_for("other")
        other_body = client.get(
            "/api/trail/BRCA1:c.68_69del", headers=other_headers
        ).json()
        assert [m["trace_id"] for m in other_body] == ["trace-theirs"]
        assert "my secret query" not in str(other_body)

    def test_pre_migration_row_without_trace_id_is_404(
        self, client, db_session, login, monkeypatch
    ):
        """A NULL trace_id can never match: fail-closed for old rows."""
        import api

        plan = _free_plan(db_session)
        me = make_user(db_session, username="clinician", plan=plan)
        self._seed_history(db_session, me.id, "BRCA1:c.68_69del", trace_id=None)
        monkeypatch.setattr(api, "_history_for", lambda variant_id: ["anything"])
        assert (
            client.get("/api/trail/BRCA1:c.68_69del", headers=login()).status_code
            == 404
        )
