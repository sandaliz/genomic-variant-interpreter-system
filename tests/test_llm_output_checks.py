"""
Regression tests for the pre-submission hardening pass.

Locks in three fixes:

  1. The LLM prompt no longer carries ClinVar's prior classification -- which
     is also the evaluation ground truth -- so the model cannot answer by
     lookup (classification._evidence_for_llm).
  2. LLM-written reports are verified, not just prompted: the classification
     label, the numeric confidence and the decision-support disclaimer are
     checked, and a dropped disclaimer is repaired (report.verify_llm_report /
     report.ensure_disclaimer).
  3. Login burns equal Argon2id work for unknown emails as for wrong
     passwords, so response time cannot enumerate accounts
     (auth.security.verify_password_or_dummy).
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.classification_agent import ClassificationAgent
from agents.report_agent import ReportAgent
from auth.security import hash_password, verify_password_or_dummy


EVIDENCE_WITH_PRIOR = {
    "found": True,
    "impact": "MODERATE",
    "prior_classification": "Pathogenic",
    "prior_review_status": "criteria provided, multiple submitters, no conflicts",
    "prior_review_stars": 2,
    "top_k_results": [],
}


class TestLLMPromptWithholdsClinVarLabel:
    def test_prior_keys_are_stripped(self):
        stripped = ClassificationAgent._evidence_for_llm(EVIDENCE_WITH_PRIOR)
        assert "prior_classification" not in stripped
        assert "prior_review_status" not in stripped
        assert "prior_review_stars" not in stripped

    def test_functional_evidence_is_kept(self):
        stripped = ClassificationAgent._evidence_for_llm(EVIDENCE_WITH_PRIOR)
        assert stripped["impact"] == "MODERATE"
        assert stripped["found"] is True

    def test_original_evidence_untouched(self):
        ClassificationAgent._evidence_for_llm(EVIDENCE_WITH_PRIOR)
        assert EVIDENCE_WITH_PRIOR["prior_classification"] == "Pathogenic"

    def test_prompt_json_never_contains_the_label(self):
        import json

        prompt_payload = json.dumps(
            ClassificationAgent._evidence_for_llm(EVIDENCE_WITH_PRIOR)
        )
        assert "Pathogenic" not in prompt_payload


class TestLLMReportVerification:
    def test_clean_report_has_no_issues(self):
        report = (
            "Classification: Pathogenic, confidence low (28/100). "
            "Decision-support tool, not a diagnosis."
        )
        issues = ReportAgent.verify_llm_report(
            report, "Pathogenic", "low", 28.0
        )
        assert issues == []

    def test_altered_classification_is_flagged(self):
        report = "This variant is Benign (28/100). Decision-support tool."
        issues = ReportAgent.verify_llm_report(
            report, "Pathogenic", "low", 28.0
        )
        assert any("classification" in i for i in issues)

    def test_missing_disclaimer_is_flagged(self):
        report = "Classification: Pathogenic, confidence low (28/100)."
        issues = ReportAgent.verify_llm_report(
            report, "Pathogenic", "low", 28.0
        )
        assert any("disclaimer" in i for i in issues)

    def test_ensure_disclaimer_appends_only_once(self):
        once = ReportAgent.ensure_disclaimer("Some report body.")
        assert once.count("IMPORTANT DISCLAIMER") == 1
        assert "decision-support" in once.lower()
        twice = ReportAgent.ensure_disclaimer(once)
        assert twice.count("IMPORTANT DISCLAIMER") == 1

    def test_template_renders_the_shared_disclaimer(self):
        template = ReportAgent().generate_report_template(
            EVIDENCE_WITH_PRIOR,
            {"classification": "Uncertain Significance (VUS)", "confidence": "low",
             "confidence_numeric": 20.0},
            "BRCA1:c.68_69del",
        )
        assert "IMPORTANT DISCLAIMER" in template


class TestLoginTimingShield:
    def test_unknown_email_still_returns_false(self):
        assert verify_password_or_dummy("whatever", None) is False

    def test_wrong_password_on_real_hash_returns_false(self):
        digest = hash_password("correct-horse-battery")
        assert verify_password_or_dummy("wrong", digest) is False

    def test_right_password_on_real_hash_returns_true(self):
        digest = hash_password("correct-horse-battery")
        assert verify_password_or_dummy("correct-horse-battery", digest) is True
