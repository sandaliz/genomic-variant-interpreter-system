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
