"""
tests/test_llm_resilience.py

Regression tests for LLM-failure handling and conflict semantics.

These lock in three defects that were fixed but previously verified only by a
throwaway script:

  1. `google.genai.errors.APIError` (503 UNAVAILABLE / 429) does NOT inherit
     from ConnectionError/TimeoutError/RuntimeError, so it escaped the fallback
     loop and crashed the request after ~11-20s of hidden SDK retries.
  2. The SDK's default retry policy (5 attempts, delays doubling toward 60s)
     made one overloaded-model response stall a web request.
  3. The report agent read the raw boolean conflict flag, so a variant whose
     ClinVar consensus was "Pathogenic" (3-star expert panel) was still shown as
     "submitter disagreement" -- contradicting the classifier.

All tests are hermetic: no network calls, no generated data files required.
"""

import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from google.genai import errors as genai_errors

from llm.gemini_client import GeminiClient
from agents.classification_agent import ClassificationAgent
from agents.report_agent import ReportAgent, _is_directional_conflict


class _RaisingLLM:
    """Stub client whose generate() always fails, as an exhausted Gemini would."""

    available = True

    def __init__(self, exc):
        self._exc = exc

    def generate(self, **kwargs):
        raise self._exc


BASE_EVIDENCE = {
    "found": True,
    "impact": "MODERATE",
    "sift_prediction": "tolerated",
    "polyphen_prediction": "benign",
    "cadd_score": 10.0,
    "af_esp": 0.0,
    "af_exac": 0.0,
    "af_tgp": 0.0,
    "conflicting_classification_flag": 0,
    "top_k_results": [],
}


class TestGeminiRetryPolicy:
    def test_sdk_retry_is_bounded(self):
        """The SDK's built-in exponential retry must be capped."""
        options = GeminiClient._bounded_http_options()
        if options is None:  # pragma: no cover - only if the SDK schema changes
            pytest.skip("installed google-genai exposes a different options schema")
        retry = getattr(options, "retry_options", None)
        assert retry is not None, "retry cap was not applied"
        assert retry.attempts == GeminiClient._RETRY_CAP["attempts"]

    def test_api_error_degrades_to_runtime_error_after_all_models(self):
        """A 503 must walk the fallback chain, never leak Google's APIError."""
        client = GeminiClient(enable_llm_cache=False)
        client.api_key = "test-key-not-real"

        attempts = {"n": 0}

        def boom(*args, **kwargs):
            attempts["n"] += 1
            raise genai_errors.ServerError(503, {"error": {"message": "overloaded"}})

        client._get_client = lambda: object()
        client._generate_with_model = boom

        with pytest.raises(RuntimeError) as excinfo:
            client.generate("ping", max_tokens=5)

        assert not isinstance(excinfo.value, genai_errors.APIError)
        expected = len(dict.fromkeys([client.model] + client.FALLBACK_MODELS))
        assert attempts["n"] == expected


class TestAgentDegradation:
    def test_classifier_falls_back_to_rules_when_llm_fails(self):
        agent = ClassificationAgent(use_llm=True)
        agent.llm = _RaisingLLM(RuntimeError("All Gemini models failed. Last error: 503"))

        result, used_llm = agent.classify_with_llm(dict(BASE_EVIDENCE))

        assert used_llm is False
        assert result["classification"]
        # Confidence stays internally consistent even on the fallback path
        assert result["confidence"] == ClassificationAgent._confidence_level(
            result["confidence_numeric"]
        )

    def test_reporter_falls_back_to_template_when_llm_fails(self):
        agent = ReportAgent(use_llm=True)
        agent.llm = _RaisingLLM(RuntimeError("All Gemini models failed. Last error: 503"))

        text = agent.generate_report_with_llm(
            dict(BASE_EVIDENCE),
            {
                "classification": "Uncertain Significance (VUS)",
                "confidence": "low",
                "confidence_numeric": 28.0,
                "criteria_applied": [],
            },
            "BRCA1:TEST",
        )

        assert "using template report" in text
        assert "Uncertain Significance (VUS)" in text


class TestServiceUnavailableCascade:
    """End-to-end 503 fallback: primary + every fallback model fail, so the
    pipeline must still degrade gracefully to rule-based classification and
    the template report -- never crash, never leak Google's APIError."""

    def test_503_cascade_falls_back_to_rules_and_template(self):
        from protocol.message import AgentMessage, MessageLog, MessageType

        client = GeminiClient(enable_llm_cache=False)
        client.api_key = "test-key-not-real"

        def overloaded(*args, **kwargs):
            raise genai_errors.ServerError(503, {"error": {"message": "overloaded"}})

        client._get_client = lambda: object()
        client._generate_with_model = overloaded

        # 1. Gemini layer converts the 503 cascade into a plain RuntimeError
        with pytest.raises(RuntimeError) as excinfo:
            client.generate("ping", max_tokens=5)
        assert not isinstance(excinfo.value, genai_errors.APIError)

        # 2. Classification agent degrades to rule-based scoring
        classifier = ClassificationAgent(use_llm=True)
        classifier.llm = client
        rule_evidence = dict(BASE_EVIDENCE)
        result, used_llm = classifier.classify_with_llm(rule_evidence)
        assert used_llm is False
        assert result["classification"]
        assert result["confidence"] == ClassificationAgent._confidence_level(
            result["confidence_numeric"]
        )

        # 3. Full classify() message path still emits an encrypted message
        log = MessageLog()
        evidence_msg = AgentMessage(
            variant_id="BRCA1:TEST",
            stage="evidence_retrieved",
            sender="evidence_retrieval_agent",
            receiver="classification_agent",
            message_type=MessageType.EVIDENCE_RESULT,
            payload=dict(BASE_EVIDENCE),
        )
        evidence_msg.encrypt_payload()
        log.record(evidence_msg)
        out_msg = classifier.classify(evidence_msg, log)
        assert out_msg.encrypted is True
        assert out_msg.decrypt_payload()["classification"]

        # 4. Report agent degrades to the template report
        reporter = ReportAgent(use_llm=True)
        reporter.llm = client
        text = reporter.generate_report_with_llm(
            dict(BASE_EVIDENCE),
            {
                "classification": "Uncertain Significance (VUS)",
                "confidence": "low",
                "confidence_numeric": 28.0,
                "criteria_applied": [],
            },
            "BRCA1:TEST",
        )
        assert "using template report" in text


class TestDirectionalConflict:
    """A Pathogenic-vs-Likely-Pathogenic split is agreement, not a conflict."""

    def test_clinvar_label_wins_over_boolean_flag(self):
        evidence = dict(
            BASE_EVIDENCE,
            conflicting_classification_flag=1,
            prior_classification="Pathogenic",
        )
        assert _is_directional_conflict(evidence) is False

    def test_true_conflict_is_detected(self):
        evidence = dict(BASE_EVIDENCE, prior_classification="Conflicting")
        assert _is_directional_conflict(evidence) is True

    def test_boolean_flag_used_only_without_a_real_label(self):
        assert _is_directional_conflict(
            dict(BASE_EVIDENCE, conflicting_classification_flag=1)
        ) is True
        assert _is_directional_conflict(dict(BASE_EVIDENCE)) is False
