"""
Tests for the agentic control plane: SupervisorAgent and VerifierAgent.

These lock in the two properties that make the pipeline agentic rather than a
fixed chain:

  * the plan is *derived from the query* (broad vs targeted), and
  * the Verifier only ever asks for **less** confidence -- never more.

The second is the safety property. If a future change let the Verifier raise a
call, these tests should fail.
"""

from agents.classification_agent import ClassificationAgent
from agents.orchestrator import SupervisorAgent, VerifierAgent
from protocol.message import MessageLog, MessageType


def _evidence(**overrides):
    evidence = {
        "found": True,
        "exact_match": True,
        "predictor_available": {"complete": True},
    }
    evidence.update(overrides)
    return evidence


def _classification(level="high", consensus_from=None):
    return {
        "classification": "Pathogenic",
        "confidence": level,
        "confidence_numeric": {"high": 80.0, "moderate": 50.0, "low": 10.0}[level],
        "consensus": {"final_from": consensus_from} if consensus_from else {},
    }


class TestSupervisorPlan:
    def test_gene_variant_query_gets_targeted_retrieval(self):
        plan = SupervisorAgent().plan(
            {"query_type": "gene_variant", "intent": "interpret"},
            MessageLog(), "trace-1", "BRCA1:c.68_69delAG",
        )
        assert plan["retrieval_mode"] == "targeted"
        assert plan["verification_budget"] == 1

    def test_bare_variant_query_gets_broad_retrieval(self):
        plan = SupervisorAgent().plan(
            {"query_type": "variant_only", "intent": "interpret"},
            MessageLog(), "trace-2", "NC_000017.10:g.41197778A>G",
        )
        assert plan["retrieval_mode"] == "broad"

    def test_plan_is_recorded_as_a_protocol_message(self):
        log = MessageLog()
        SupervisorAgent().plan(
            {"query_type": "gene_variant"}, log, "trace-3", "BRCA1:c.1A>G"
        )
        trail = log.trail_for("trace-3")
        assert len(trail) == 1
        assert trail[0].message_type == MessageType.PLAN
        assert trail[0].sender == "supervisor_agent"
        # The plan travels encrypted, like every other agent message.
        assert trail[0].encrypted is True


class TestVerifierAcceptsWellFoundedCalls:
    def test_exact_match_and_high_confidence_is_accepted(self):
        verdict = VerifierAgent().verify(
            _evidence(), _classification("high"), MessageLog(), "t", "v"
        )
        assert verdict["verdict"] == "accept"
        assert verdict["directive"] is None

    def test_reviewed_consensus_backs_a_confident_call(self):
        verdict = VerifierAgent().verify(
            _evidence(exact_match=False),
            _classification("high", consensus_from="clinvar_consensus"),
            MessageLog(), "t", "v",
        )
        assert verdict["verdict"] == "accept"

    def test_low_confidence_is_never_challenged(self):
        verdict = VerifierAgent().verify(
            _evidence(exact_match=False), _classification("low"),
            MessageLog(), "t", "v",
        )
        assert verdict["verdict"] == "accept"


class TestVerifierChallengesOverconfidence:
    def test_confident_call_without_an_exact_match_is_sent_back(self):
        verdict = VerifierAgent().verify(
            _evidence(exact_match=False), _classification("high"),
            MessageLog(), "t", "v",
        )
        assert verdict["verdict"] == "revise"
        assert verdict["directive"] == "conservative"
        assert verdict["issues"]

    def test_broad_retrieval_tolerates_a_partial_match_at_moderate(self):
        # In broad mode an exact match is not expected, so a *moderate* call
        # stands -- only a high one is challenged.
        verdict = VerifierAgent().verify(
            _evidence(exact_match=False), _classification("moderate"),
            MessageLog(), "t", "v", retrieval_mode="broad",
        )
        assert verdict["verdict"] == "accept"

    def test_broad_retrieval_still_challenges_a_high_call(self):
        verdict = VerifierAgent().verify(
            _evidence(exact_match=False), _classification("high"),
            MessageLog(), "t", "v", retrieval_mode="broad",
        )
        assert verdict["verdict"] == "revise"

    def test_verification_is_recorded_as_a_protocol_message(self):
        log = MessageLog()
        VerifierAgent().verify(
            _evidence(exact_match=False), _classification("high"), log, "trace-v", "v"
        )
        trail = log.trail_for("trace-v")
        assert trail[0].message_type == MessageType.VERIFICATION_RESULT


class TestConservativeDirective:
    """The Classification Agent's response to a `revise` verdict."""

    def test_directive_lowers_the_number_and_therefore_the_label(self):
        agent = ClassificationAgent.__new__(ClassificationAgent)  # no LLM client
        result = {"classification": "Pathogenic", "confidence": "high",
                  "confidence_numeric": 80.0, "consensus": {}}
        out = agent.apply_conservative_directive(result, _evidence(exact_match=False))
        assert out["confidence_numeric"] < ClassificationAgent.CONF_MODERATE_BAR
        assert out["confidence"] == "low"
        assert out["confidence_revised"] is True

    def test_directive_never_changes_the_classification_itself(self):
        agent = ClassificationAgent.__new__(ClassificationAgent)
        result = {"classification": "Pathogenic", "confidence": "high",
                  "confidence_numeric": 80.0, "consensus": {}}
        out = agent.apply_conservative_directive(result, _evidence(exact_match=False))
        assert out["classification"] == "Pathogenic"

    def test_directive_is_inert_when_the_evidence_supports_the_call(self):
        agent = ClassificationAgent.__new__(ClassificationAgent)
        result = {"classification": "Pathogenic", "confidence": "high",
                  "confidence_numeric": 80.0, "consensus": {}}
        out = agent.apply_conservative_directive(result, _evidence(exact_match=True))
        assert out["confidence_numeric"] == 80.0
        assert out.get("confidence_revised") is None

    def test_directive_never_raises_confidence(self):
        """The safety property: self-correction can only withdraw certainty."""
        agent = ClassificationAgent.__new__(ClassificationAgent)
        result = {"classification": "Pathogenic", "confidence": "low",
                  "confidence_numeric": 10.0, "consensus": {}}
        out = agent.apply_conservative_directive(result, _evidence(exact_match=False))
        assert out["confidence_numeric"] <= 10.0
        assert out["confidence"] == "low"


class TestPipelineIntegration:
    """The control plane is actually wired into the run, end to end."""

    def test_run_reports_a_plan_and_a_verification(self, monkeypatch):
        import main as main_module

        system = main_module.VariantInterpretationSystem(
            use_llm=False, use_cache=False, lazy_load=False
        )
        result = system.run("Is BRCA1 NC_000017.10:g.41197778A>G pathogenic?")
        assert "error" not in result, result

        agentic = result["agentic"]
        assert agentic["plan"]["retrieval_mode"] in {"broad", "targeted"}
        assert agentic["verification"]["verdict"] in {"accept", "revise"}
        assert agentic["revision_rounds"] in {0, 1}

        # Both control-plane steps are visible in the pipeline trace.
        agents = [step["agent"] for step in result["pipeline_steps"]]
        assert "supervisor_agent" in agents
        assert "verifier_agent" in agents

        # ...and in the audit trail, as first-class protocol messages.
        types = [m["message_type"] for m in result["audit_trail"]]
        assert MessageType.PLAN in types
        assert MessageType.VERIFICATION_RESULT in types

    def test_self_correction_is_bounded_to_one_round(self, monkeypatch):
        import main as main_module

        system = main_module.VariantInterpretationSystem(
            use_llm=False, use_cache=False, lazy_load=False
        )
        # Force the Verifier to ask for a revision on any query, then confirm the
        # loop stops after a single round rather than retrying forever.
        monkeypatch.setattr(
            main_module.VerifierAgent, "verify",
            lambda self, *a, **kw: {
                "verdict": "revise", "directive": "conservative",
                "checks": [], "issues": ["forced"], "retrieval_mode": "targeted",
                "revised": kw.get("revised", False),
            },
        )
        result = system.run("Is BRCA1 NC_000017.10:g.41197778A>G pathogenic?")
        assert result["agentic"]["revision_rounds"] == 1
