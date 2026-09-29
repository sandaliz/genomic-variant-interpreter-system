"""
tests/test_classification_agent.py

Unit tests for the Classification Agent (LLM + rule-based reasoning).
"""

import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import json

from agents.classification_agent import ClassificationAgent
from protocol.message import AgentMessage, MessageLog


@pytest.fixture
def classifier():
    # Use rule-based mode for deterministic tests (no API calls)
    return ClassificationAgent(use_llm=False)


@pytest.fixture
def message_log():
    return MessageLog()


class _StubLLM:
    """Deterministic stand-in for Gemini: returns a canned JSON payload.

    Used to prove the pipeline cannot be talked into a confidence level its
    evidence does not support.
    """

    available = True

    def __init__(self, payload: dict):
        self._payload = payload

    def generate(self, **kwargs) -> str:
        return json.dumps(self._payload)


class TestClassificationAgent:
    def test_classify_rule_based_pathogenic(self, classifier):
        """High-impact + rare + damaging predictors -> Likely Pathogenic."""
        evidence = {
            "found": True,
            "impact": "HIGH",
            "sift_prediction": "deleterious",
            "polyphen_prediction": "probably_damaging",
            "cadd_score": 25.0,
            "af_esp": 0.0,
            "af_exac": 0.0,
            "af_tgp": 0.0,
            "conflicting_classification_flag": 0,
        }
        result = classifier.classify_rule_based(evidence)
        assert result["classification"] == "Likely Pathogenic"
        assert result["confidence"] == "high"

    def test_classify_rule_based_benign(self, classifier):
        """Common in population -> Likely Benign."""
        evidence = {
            "found": True,
            "impact": "MODERATE",
            "sift_prediction": "tolerated",
            "polyphen_prediction": "benign",
            "cadd_score": 5.0,
            "af_esp": 0.01,
            "af_exac": 0.02,
            "af_tgp": 0.015,
            "conflicting_classification_flag": 0,
        }
        result = classifier.classify_rule_based(evidence)
        assert result["classification"] == "Likely Benign"

    def test_classify_rule_based_vus(self, classifier):
        """Weak evidence -> VUS with insufficient-evidence reasoning."""
        evidence = {
            "found": True,
            "impact": "MODERATE",
            "sift_prediction": "tolerated",
            "polyphen_prediction": "benign",
            "cadd_score": 10.0,
            "af_esp": 0.0,
            "af_exac": 0.0,
            "af_tgp": 0.0,
            "conflicting_classification_flag": 0,
        }
        result = classifier.classify_rule_based(evidence)
        assert result["classification"] == "Uncertain Significance (VUS)"
        assert "Insufficient validated ACMG/AMP evidence" in result["reasoning"]

    def test_moderate_af_does_not_trigger_bs1(self, classifier):
        """AF=0.0002 must NOT produce BS1: BS1 is unestablished without a
        validated disease-specific threshold. It is a neutral observation."""
        evidence = {
            "found": True,
            "impact": "MODERATE",
            "consequence": "missense_variant",
            "sift_prediction": "deleterious",
            "polyphen_prediction": "benign",
            "cadd_score": 25.9,
            "af_esp": 0.0002,
            "af_exac": 0.0,
            "af_tgp": 0.0,
            "conflicting_classification_flag": 0,
        }
        result = classifier.classify_rule_based(evidence)
        assert result["classification"] == "Uncertain Significance (VUS)"
        assert not any("BS1" in c for c in result["acmg_criteria_applied"])
        assert not any("BS1" in c for c in result["criteria_applied"])
        assert any("CADD" in a for a in result["computational_annotations"])
        assert result["population_observation"] is not None
        assert "BS1" in result["population_observation"]

    def test_cadd_and_conflict_are_not_acmg_criteria(self, classifier):
        """CADD is a computational annotation; submitter disagreement is
        ClinVar context -- neither may appear in the ACMG criteria list."""
        evidence = {
            "found": True,
            "impact": "MODERATE",
            "consequence": "missense_variant",
            "sift_prediction": "deleterious",
            "polyphen_prediction": "benign",
            "cadd_score": 25.9,
            "af_esp": 0.0002,
            "af_exac": 0.0,
            "af_tgp": 0.0,
            "conflicting_classification_flag": 1,
            "prior_classification": "Conflicting",
        }
        result = classifier.classify_rule_based(evidence)
        assert not any("CADD" in c for c in result["acmg_criteria_applied"])
        assert not any("disagreed" in c for c in result["acmg_criteria_applied"])
        assert any("CADD" in a for a in result["computational_annotations"])
        assert any("disagreed" in c for c in result["clinvar_context"])

    def test_missing_predictors_do_not_create_evidence(self, classifier):
        """Missing predictors remain unavailable and lower confidence."""
        complete = classifier.classify_rule_based({
            "found": True,
            "impact": "HIGH",
            "sift_prediction": "deleterious",
            "polyphen_prediction": "probably_damaging",
            "cadd_score": 25.0,
            "af_esp": 0.0,
            "af_exac": 0.0,
            "af_tgp": 0.0,
            "conflicting_classification_flag": 0,
        })
        incomplete = classifier.classify_rule_based({
            "found": True,
            "impact": "HIGH",
            "sift_prediction": None,
            "polyphen_prediction": None,
            "cadd_score": float("nan"),
            "af_esp": 0.0,
            "af_exac": 0.0,
            "af_tgp": 0.0,
            "conflicting_classification_flag": 0,
        })

        assert "PP3_equivalent (predictors agree: damaging)" not in incomplete["criteria_applied"]
        assert not incomplete["confidence_components"]["predictor_available"]["complete"]
        assert incomplete["confidence_numeric"] < complete["confidence_numeric"]

    def test_classify_rule_based_not_found(self, classifier):
        """No evidence -> Unknown."""
        result = classifier.classify_rule_based({"found": False})
        assert result["classification"] == "Unknown"
        assert result["confidence"] == "none"

    def test_classify_rule_based_conflict_lowers_confidence(self, classifier):
        """Conflicting submitters -> low confidence."""
        evidence = {
            "found": True,
            "impact": "HIGH",
            "sift_prediction": "deleterious",
            "polyphen_prediction": "probably_damaging",
            "cadd_score": 25.0,
            "af_esp": 0.0,
            "af_exac": 0.0,
            "af_tgp": 0.0,
            "conflicting_classification_flag": 1,
        }
        result = classifier.classify_rule_based(evidence)
        assert result["confidence"] == "low"

    def test_classify_encrypts_payload(self, classifier, message_log):
        """Output message should be encrypted."""
        evidence_msg = AgentMessage(
            variant_id="BRCA1:NC_000017.10:g.41197778A>G",
            stage="evidence_retrieved",
            sender="evidence_retrieval_agent",
            payload={
                "found": True,
                "impact": "HIGH",
                "sift_prediction": "deleterious",
                "polyphen_prediction": "probably_damaging",
                "cadd_score": 25.0,
                "af_esp": 0.0,
                "af_exac": 0.0,
                "af_tgp": 0.0,
                "conflicting_classification_flag": 0,
            },
        )
        result_msg = classifier.classify(evidence_msg, message_log)
        assert result_msg.encrypted is True
        payload = result_msg.decrypt_payload()
        assert "classification" in payload
        assert "reasoning_mode" in payload


class TestConfidenceConsistency:
    """The categorical confidence label must always be DERIVED from the
    numeric score. A report must never be able to show "high" next to
    28/100, or "high" next to 58/100."""

    def test_confidence_threshold_bands(self):
        assert ClassificationAgent._confidence_level(28) == "low"
        assert ClassificationAgent._confidence_level(34.9) == "low"
        assert ClassificationAgent._confidence_level(35) == "moderate"
        assert ClassificationAgent._confidence_level(58) == "moderate"
        assert ClassificationAgent._confidence_level(60) == "high"
        assert ClassificationAgent._confidence_level(87.5) == "high"
        # Degenerate input must not crash or invent a level
        assert ClassificationAgent._confidence_level(None) == "low"

    def test_conflict_caps_number_not_just_label(self, classifier):
        """Conflict forces low confidence on BOTH the label and the number."""
        evidence = {
            "found": True,
            "impact": "HIGH",
            "sift_prediction": "deleterious",
            "polyphen_prediction": "probably_damaging",
            "cadd_score": 25.0,
            "af_esp": 0.0,
            "af_exac": 0.0,
            "af_tgp": 0.0,
            "conflicting_classification_flag": 1,
        }
        result = classifier.classify_rule_based(evidence)
        assert result["confidence"] == "low"
        assert result["confidence_numeric"] < ClassificationAgent.CONF_MODERATE_BAR
        assert result["confidence"] == ClassificationAgent._confidence_level(
            result["confidence_numeric"]
        )

    def test_llm_self_reported_confidence_is_discarded(self):
        """An LLM claiming "high" confidence must not survive a weak
        evidence score -- this is the reported 28/100-shown-as-high bug."""
        agent = ClassificationAgent(use_llm=True)
        agent.llm = _StubLLM({
            "classification": "Pathogenic",
            "confidence": "high",
            "criteria_applied": ["PVS1"],
            "reasoning": "Frameshift in a tumour suppressor gene.",
        })
        evidence = {
            "found": True,
            "impact": "MODERATE",
            "sift_prediction": None,
            "polyphen_prediction": None,
            "cadd_score": float("nan"),
            "af_esp": 0.0,
            "af_exac": 0.0,
            "af_tgp": 0.0,
            "conflicting_classification_flag": 0,
            "top_k_results": [],
        }
        result, used_llm = agent.classify_with_llm(evidence)
        assert used_llm is True
        assert result["confidence"] != "high"
        assert result["confidence_numeric"] < ClassificationAgent.CONF_HIGH_BAR
        assert result["confidence"] == ClassificationAgent._confidence_level(
            result["confidence_numeric"]
        )

    def test_consensus_deferral_confidence_follows_review_stars(self, classifier):
        """Deferring to a 3-star ClinVar label: confidence describes the
        FINAL label, and the weaker evidence-based confidence is preserved
        rather than silently overwritten."""
        evidence = {
            "found": True,
            "impact": "MODERATE",
            "sift_prediction": "tolerated",
            "polyphen_prediction": "benign",
            "cadd_score": 10.0,
            "af_esp": 0.0,
            "af_exac": 0.0,
            "af_tgp": 0.0,
            "conflicting_classification_flag": 0,
            "prior_classification": "Pathogenic",
            "prior_review_status": "reviewed by expert panel",
            "prior_review_stars": 3,
        }
        result = classifier.classify_rule_based(evidence)
        assert result["classification"] == "Uncertain Significance (VUS)"

        reconciled = classifier.reconcile_with_consensus(result, evidence)
        assert reconciled["classification"] == "Pathogenic"
        assert reconciled["consensus"]["final_from"] == "clinvar_consensus"
        assert reconciled["confidence"] == "high"
        assert reconciled["confidence_numeric"] >= 80.0
        # Evidence-based numbers survive for the report to disclose
        assert (
            reconciled["consensus"]["evidence_based_confidence_numeric"]
            < reconciled["confidence_numeric"]
        )
        assert reconciled["confidence"] == ClassificationAgent._confidence_level(
            reconciled["confidence_numeric"]
        )

    def test_two_star_deferral_lands_in_moderate(self, classifier):
        """2-star consensus (multiple submitters, no conflict) -> moderate."""
        evidence = {
            "found": True,
            "impact": "MODERATE",
            "sift_prediction": "tolerated",
            "polyphen_prediction": "benign",
            "cadd_score": 10.0,
            "af_esp": 0.0,
            "af_exac": 0.0,
            "af_tgp": 0.0,
            "conflicting_classification_flag": 0,
            "prior_classification": "Pathogenic",
            "prior_review_status": "criteria provided, multiple submitters, no conflicts",
            "prior_review_stars": 2,
        }
        result = classifier.classify_rule_based(evidence)
        reconciled = classifier.reconcile_with_consensus(result, evidence)
        assert reconciled["consensus"]["final_from"] == "clinvar_consensus"
        assert reconciled["confidence"] == "moderate"
        assert reconciled["confidence"] == ClassificationAgent._confidence_level(
            reconciled["confidence_numeric"]
        )

    def test_consensus_agreement_gets_same_confidence_as_deferral(self, classifier):
        """Agreement with a 3-star consensus floors confidence just like
        deferral does, so the same variant cannot show 80/100 via the rule
        path and 28/100 via the LLM path."""
        evidence = {
            "found": True,
            "impact": "HIGH",
            "sift_prediction": "deleterious",
            "polyphen_prediction": "probably_damaging",
            "cadd_score": 25.0,
            "af_esp": 0.0,
            "af_exac": 0.0,
            "af_tgp": 0.0,
            "conflicting_classification_flag": 0,
            "prior_classification": "Likely Pathogenic",
            "prior_review_status": "reviewed by expert panel",
            "prior_review_stars": 3,
        }
        result = classifier.classify_rule_based(evidence)
        assert result["classification"] == "Likely Pathogenic"
        evidence_num = result["confidence_numeric"]

        reconciled = classifier.reconcile_with_consensus(result, evidence)
        assert reconciled["consensus"]["agreement"] is True
        assert reconciled["consensus"]["final_from"] == "evidence_based"
        # Never lowers, and never below the 3-star floor
        assert reconciled["confidence_numeric"] >= max(evidence_num, 80.0)
        assert reconciled["confidence"] == ClassificationAgent._confidence_level(
            reconciled["confidence_numeric"]
        )
        # If it was raised, the evidence-based pair is preserved for disclosure
        if evidence_num < 80.0:
            assert (
                reconciled["consensus"]["evidence_based_confidence_numeric"]
                == evidence_num
            )

    def test_unreviewed_consensus_does_not_floor_confidence(self, classifier):
        """0-star (unreviewed) ClinVar labels must not inflate confidence."""
        evidence = {
            "found": True,
            "impact": "MODERATE",
            "sift_prediction": "tolerated",
            "polyphen_prediction": "benign",
            "cadd_score": 10.0,
            "af_esp": 0.0,
            "af_exac": 0.0,
            "af_tgp": 0.0,
            "conflicting_classification_flag": 0,
            "prior_classification": "Pathogenic",
            "prior_review_status": "no assertion criteria provided",
            "prior_review_stars": 0,
        }
        result = classifier.classify_rule_based(evidence)
        reconciled = classifier.reconcile_with_consensus(result, evidence)
        assert reconciled["consensus"]["final_from"] == "evidence_based"
        assert reconciled["confidence_numeric"] == result["confidence_numeric"]
        assert "evidence_based_confidence_numeric" not in reconciled["consensus"]
