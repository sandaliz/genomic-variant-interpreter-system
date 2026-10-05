"""
tests/test_evidence_agent.py

Unit tests for the Evidence Retrieval Agent (Information Retrieval).
"""

import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from agents.evidence_agent import EvidenceRetrievalAgent
from agents.intake_agent import IntakeAgent
from protocol.message import AgentMessage, MessageLog


DATASET_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "data", "clinvar_variant_dataset.xlsx")


@pytest.fixture
def evidence_agent():
    return EvidenceRetrievalAgent(DATASET_PATH)


@pytest.fixture
def intake_agent():
    return IntakeAgent()


@pytest.fixture
def message_log():
    return MessageLog()


class TestEvidenceRetrievalAgent:
    def test_exact_variant_lookup(self, evidence_agent, intake_agent, message_log):
        """Should find the exact variant by gene + HGVS."""
        intake_msg = intake_agent.process(
            "Is BRCA1 NC_000017.10:g.41197778A>G pathogenic?", message_log
        )
        evidence_msg = evidence_agent.retrieve(intake_msg, message_log)
        payload = evidence_msg.decrypt_payload()

        assert payload["found"] is True
        assert payload["gene"] == "BRCA1"
        assert payload["hgvs"] == "NC_000017.10:g.41197778A>G"
        assert payload["exact_match"] is True
        assert payload["relevance_score"] is not None

    def test_gene_only_retrieval(self, evidence_agent, intake_agent, message_log):
        """Should retrieve variants for a gene with top-K results."""
        intake_msg = intake_agent.process(
            "What variants are in BRCA1?", message_log
        )
        evidence_msg = evidence_agent.retrieve(intake_msg, message_log)
        payload = evidence_msg.decrypt_payload()

        assert payload["found"] is True
        assert payload["gene"] == "BRCA1"
        assert payload["total_variants_in_gene"] > 0
        assert "top_k_results" in payload
        assert len(payload["top_k_results"]) > 0

    def test_variant_only_retrieval(self, evidence_agent, intake_agent, message_log):
        """Should find a variant by HGVS alone."""
        intake_msg = intake_agent.process(
            "What is NC_000017.10:g.41201179C>T?", message_log
        )
        evidence_msg = evidence_agent.retrieve(intake_msg, message_log)
        payload = evidence_msg.decrypt_payload()

        assert payload["found"] is True
        assert payload["hgvs"] == "NC_000017.10:g.41201179C>T"

    def test_position_retrieval(self, evidence_agent, intake_agent, message_log):
        """Should find variants at a genomic position."""
        intake_msg = intake_agent.process(
            "What variant is at position 41197778 in BRCA1?", message_log
        )
        evidence_msg = evidence_agent.retrieve(intake_msg, message_log)
        payload = evidence_msg.decrypt_payload()

        assert payload["found"] is True
        assert payload["gene"] == "BRCA1"

    def test_bare_position_uses_variant_summary_fallback(
        self, evidence_agent, intake_agent, message_log
    ):
        """A bare genomic position should resolve through the ClinVar index."""
        intake_msg = intake_agent.process("41209082", message_log)
        evidence_msg = evidence_agent.retrieve(intake_msg, message_log)
        payload = evidence_msg.decrypt_payload()

        assert payload["found"] is True
        assert payload["query_type"] == "position_only"
        assert payload["coverage_fallback"] is True

    def test_ir_metadata_present(self, evidence_agent, intake_agent, message_log):
        """Should include IR metadata for transparency."""
        intake_msg = intake_agent.process(
            "Is BRCA1 NC_000017.10:g.41197778A>G pathogenic?", message_log
        )
        evidence_msg = evidence_agent.retrieve(intake_msg, message_log)
        payload = evidence_msg.decrypt_payload()

        assert "ir_metadata" in payload
        meta = payload["ir_metadata"]
        assert "retrieval_strategy" in meta
        assert "candidates_retrieved" in meta
        assert "top_k_returned" in meta
        assert "ranking_method" in meta

    def test_missing_predictor_metadata_is_explicit(self, evidence_agent, intake_agent, message_log):
        """Evidence payload exposes predictor availability instead of hiding missingness."""
        intake_msg = intake_agent.process(
            "Is BRCA1 NC_000017.10:g.41197778A>G pathogenic?", message_log
        )
        evidence_msg = evidence_agent.retrieve(intake_msg, message_log)
        payload = evidence_msg.decrypt_payload()

        assert "predictor_available" in payload
        assert set(payload["predictor_available"]) == {
            "sift", "polyphen", "sift_polyphen", "cadd", "complete"
        }

    def test_top_k_results_ranked(self, evidence_agent, intake_agent, message_log):
        """Top-K results should be sorted by relevance score descending."""
        intake_msg = intake_agent.process(
            "What variants are in BRCA1?", message_log
        )
        evidence_msg = evidence_agent.retrieve(intake_msg, message_log)
        payload = evidence_msg.decrypt_payload()

        if "top_k_results" in payload and len(payload["top_k_results"]) > 1:
            scores = [r["relevance_score"] for r in payload["top_k_results"]]
            assert scores == sorted(scores, reverse=True)

    def test_encrypted_payload(self, evidence_agent, intake_agent, message_log):
        """Output message should be encrypted."""
        intake_msg = intake_agent.process(
            "Is BRCA1 NC_000017.10:g.41197778A>G pathogenic?", message_log
        )
        evidence_msg = evidence_agent.retrieve(intake_msg, message_log)
        assert evidence_msg.encrypted is True

    def test_not_found_falls_back_to_tfidf(self, evidence_agent, intake_agent, message_log):
        """When exact match fails, should fall back to TF-IDF and return a similar variant."""
        # Use a variant that doesn't exist in the dataset
        intake_msg = intake_agent.process(
            "Is BRCA1 NC_000017.10:g.99999999A>G pathogenic?", message_log
        )
        evidence_msg = evidence_agent.retrieve(intake_msg, message_log)
        payload = evidence_msg.decrypt_payload()

        # The system should still find something via TF-IDF fallback,
        # but it should NOT be an exact match
        assert payload["found"] is True
        assert payload["exact_match"] is False
        assert "ir_metadata" in payload
        assert payload["ir_metadata"]["candidates_retrieved"] > 0


class TestCoverageFallbackFlag:
    # Regression guard: the flag used to be set only on the record-found path,
    # so a consumer reading it with .get() saw None instead of False.

    def test_exact_workbook_match_reports_false(self, evidence_agent, intake_agent, message_log):
        intake_msg = intake_agent.process(
            "Is BRCA1 NC_000017.10:g.41197778A>G pathogenic?", message_log
        )
        evidence_msg = evidence_agent.retrieve(intake_msg, message_log)
        payload = evidence_msg.decrypt_payload()
        assert payload["coverage_fallback"] is False
        assert isinstance(payload["coverage_fallback"], bool)

    def test_flag_true_when_resolved_from_clinvar_index(self, evidence_agent, intake_agent, message_log):
        index_path = os.path.join(os.path.dirname(DATASET_PATH), "variant_summary_brca.tsv")
        if not os.path.exists(index_path):
            pytest.skip("variant_summary_brca.tsv not built (see data/DATA_SOURCE.md)")

        # BRCA2 c.5946delT is absent from the study workbook, so the Evidence
        # Agent must resolve it from the ClinVar coverage index instead.
        intake_msg = intake_agent.process("Classify BRCA2 c.5946delT", message_log)
        evidence_msg = evidence_agent.retrieve(intake_msg, message_log)
        payload = evidence_msg.decrypt_payload()

        assert payload["coverage_fallback"] is True
        assert payload["evidence_limitation"] is not None
        assert payload["ir_metadata"]["evidence_source"].startswith("clinvar_variant_summary")
