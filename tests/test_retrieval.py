"""
tests/test_retrieval.py

Unit tests for the lexical retrieval models and the ranking metrics.

These are the only tests in the suite that check arithmetic against
hand-computed values rather than checking behaviour. That is deliberate: the
report claims specific things about BM25 (that IDF is positive for a
corpus-wide term, that b penalises long documents, that k1 saturates term
frequency) and about the metrics (that nDCG@k discounts by rank, that AP@k
rewards finding the right answer first). Each of those is a statement about a
number, so each of them is asserted as a number here rather than being
described and hoped for.

No dataset and no pipeline are involved -- retrieval/ is importable on its own
for exactly this reason.
"""

import math
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from agents.evidence_agent import RANKERS, EvidenceRetrievalAgent
from agents.intake_agent import IntakeAgent
from protocol.message import MessageLog
from retrieval.bm25 import BM25Index, tokenize
from retrieval.fusion import rank_from_scores, reciprocal_rank_fusion, score_fusion
from retrieval.metrics import (
    average_precision_at_k,
    dcg_at_k,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
)


DATASET_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "data", "clinvar_variant_dataset.xlsx")


class TestTokenisation:
    def test_tokens_are_lowercased_alphanumeric_runs(self):
        # Punctuation is *dropped*, not treated as a word boundary: this corpus
        # writes HGVS as "c.5946delT", and losing the dot leaves the digits and
        # the letters glued into one token rather than split into two.
        assert tokenize("BRCA1 c.5946delT") == ["brca1", "c", "5946delt"]

    def test_punctuation_is_a_separator_not_merely_deleted(self):
        # The dot separates "c" from the rest, so removing it does *not* glue
        # the token together: "c.5946delT" and "c5946delt" are different
        # tokenisations. bm25.py says so explicitly -- getting this backwards
        # would mean a query written either way silently searched different
        # postings.
        assert tokenize("c.5946delT") == ["c", "5946delt"]
        assert tokenize("c5946delt") == ["c5946delt"]

    def test_underscore_and_angle_bracket_split_where_tfidf_keeps_them(self):
        # TF-IDF's token pattern is \b[a-zA-Z0-9_]+[a-zA-Z0-9_>]*\b, which keeps
        # "_" and ">" inside a token. BM25's does not. The models share the
        # input field, not the tokeniser, and the report must not claim more.
        assert tokenize("NC_000017.10") == ["nc", "000017", "10"]
        assert tokenize("41197778A>G") == ["41197778a", "g"]

    def test_punctuation_only_text_tokenises_to_nothing(self):
        assert tokenize("-- ... --") == []
        assert tokenize(None) == []


class TestBM25:
    """
    A three-document corpus small enough to work through by hand.

    Documents: d1 "brca1 variant" (len 2), d2 "brca1" (len 1), d3 "brca2
    variant" (len 2). N=3, avgdl=5/3. "brca2" appears once, in d3, so:

        IDF(brca2) = ln(1 + (3 - 1 + 0.5) / (1 + 0.5)) = ln(2.6667) = 0.98083
        normaliser = 0.25 + 0.75 * (2 / (5/3)) = 1.15
        score(d3)  = 0.98083 * (1 * 2.2) / (1 + 1.2 * 1.15) = 0.90665
    """

    @pytest.fixture
    def index(self):
        return BM25Index(["brca1 variant", "brca1", "brca2 variant"])

    def test_only_the_document_holding_the_term_scores(self, index):
        scores = index.score("brca2")
        assert scores[0] == 0.0
        assert scores[1] == 0.0
        assert scores[2] == pytest.approx(0.90665, abs=1e-3)

    def test_a_rarer_term_has_a_higher_idf(self, index):
        assert index.idf("brca2") > index.idf("brca1") > 0

    def test_a_term_in_every_document_still_has_positive_idf(self, index):
        # The unsmoothed Robertson-Sparck Jones IDF is negative here, which
        # would make a document containing the term score *lower* for it.
        assert index.idf("variant") > 0

    def test_an_absent_term_has_no_weight(self, index):
        assert index.idf("notinanydocument") == 0.0
        assert index.score("notinanydocument") == [0.0, 0.0, 0.0]

    def test_average_document_length(self, index):
        assert len(index) == 3
        assert index.average_document_length == pytest.approx(5 / 3)

    def test_b_penalises_long_documents(self):
        # Same term frequency in a short and a long document. With the length
        # penalty off, they must tie; with it on, the short one must win.
        documents = ["q", "q filler filler filler"]
        unpenalised = BM25Index(documents, b=0.0).score("q")
        assert unpenalised[0] == pytest.approx(unpenalised[1])

        penalised = BM25Index(documents, b=0.75).score("q")
        assert penalised[0] > penalised[1]

    def test_k1_zero_makes_term_frequency_irrelevant(self):
        # k1=0 collapses the saturation term to a constant, so repeating a term
        # cannot help. With the length penalty also off there is nothing left
        # for a repetition to change, and the two documents must tie.
        index = BM25Index(["q", "q q q"], k1=0.0, b=0.0)
        assert index.score("q")[0] == pytest.approx(index.score("q")[1])

    def test_term_frequency_helps_when_k1_is_on(self):
        index = BM25Index(["q", "q q q"], b=0.0)
        assert index.score("q")[1] > index.score("q")[0]

    def test_empty_query_scores_every_document_zero(self, index):
        assert index.score("") == [0.0, 0.0, 0.0]

    def test_empty_corpus_is_survivable(self):
        index = BM25Index([])
        assert len(index) == 0
        assert index.score("brca1") == []

    def test_explain_reports_the_contributing_terms(self, index):
        top = index.explain("brca2 variant", 2)
        assert top, "no contributing terms reported"
        terms = [term for term, _ in top]
        assert set(terms) <= {"brca2", "variant"}
        # Contributions are reported in descending order.
        values = [value for _, value in top]
        assert values == sorted(values, reverse=True)


class TestReciprocalRankFusion:
    def test_mirrored_rankings_tie(self):
        # 0 and 2 are mirror images: first in one list, last in the other. They
        # must score the same.
        fused = reciprocal_rank_fusion([[0, 1, 2], [2, 1, 0]])
        assert set(fused) == {0, 1, 2}
        assert fused[0] == pytest.approx(fused[2])
        # The document both models place mid-table lands right beside them.
        # At k=60 the constant term dominates the rank differences, which is
        # the insensitivity Cormack et al. report -- and the reason RRF is not
        # derailed by one model ranking a good document a place or two lower.
        assert fused[1] == pytest.approx(fused[0], abs=1e-4)

    def test_a_document_in_one_list_only_still_scores(self):
        fused = reciprocal_rank_fusion([[0, 1], [2]])
        assert set(fused) == {0, 1, 2}
        # 2 is first in the list it appears in, so it ties with 0, the other
        # first place, and both beat 1, which is second everywhere.
        assert fused[0] == pytest.approx(fused[2])
        assert fused[0] > fused[1]

    def test_a_repeated_id_counts_once_at_its_best_rank(self):
        fused = reciprocal_rank_fusion([[0, 0, 1]])
        assert fused[0] == pytest.approx(1 / 61)
        assert fused[1] == pytest.approx(1 / 63)

    def test_agreement_beats_a_lone_first_place(self):
        # 1 is ranked by both models (first in one, second in the other); 0 is
        # ranked first by one model and not at all by the other. Fusion prefers
        # the document both models agreed on.
        fused = reciprocal_rank_fusion([[0, 1], [2, 1]])
        assert fused[1] > fused[0]
        assert fused[1] > fused[2]

    def test_k_must_be_positive(self):
        with pytest.raises(ValueError):
            reciprocal_rank_fusion([[0]], k=0)


class TestScoreFusion:
    def test_normalised_scores_are_averaged(self):
        fused = score_fusion([{"a": 10.0, "b": 5.0}, {"a": 0.0, "b": 1.0}])
        assert fused["b"] > fused["a"]
        assert fused["a"] == pytest.approx(0.5)
        assert fused["b"] == pytest.approx(0.75)

    def test_a_model_that_scored_nothing_cannot_shift_the_average(self):
        assert score_fusion([{"a": 1.0}, {"a": 0.0}]) == {"a": pytest.approx(1.0)}

    def test_weights_must_match_the_maps(self):
        with pytest.raises(ValueError):
            score_fusion([{"a": 1.0}], weights=[1.0, 1.0])

    def test_weights_must_sum_to_something_positive(self):
        with pytest.raises(ValueError):
            score_fusion([{"a": 1.0}], weights=[0.0])

    def test_no_maps_is_an_empty_fusion(self):
        assert score_fusion([]) == {}

    def test_rank_from_scores_breaks_ties_on_the_id(self):
        # Reproducibility: a tie must not resolve by dict ordering.
        assert rank_from_scores({"b": 1.0, "a": 1.0}) == ["b", "a"]
        assert rank_from_scores({"a": 1.0, "b": 2.0}) == ["b", "a"]
        assert rank_from_scores({"a": 1.0, "b": 2.0}, descending=False) == ["a", "b"]


class TestRankingMetrics:
    def test_precision_at_k_divides_by_k_not_by_list_length(self):
        assert precision_at_k([1, 0, 1, 0], 4) == pytest.approx(0.5)
        assert precision_at_k([1, 0, 1, 0], 2) == pytest.approx(0.5)
        # One hit in the top three is 1/3, not 1/2.
        assert precision_at_k([1, 0, 1, 0], 3) == pytest.approx(2 / 3)
        # A short list is not a perfect one.
        assert precision_at_k([1], 5) == pytest.approx(0.2)

    def test_recall_at_k_uses_the_stated_number_of_relevant_documents(self):
        assert recall_at_k([1, 0, 1], 2, total_relevant=3) == pytest.approx(1 / 3)
        assert recall_at_k([1, 0, 1], 3, total_relevant=3) == pytest.approx(2 / 3)

    def test_recall_is_zero_when_nothing_is_relevant(self):
        assert recall_at_k([0, 0], 2) == 0.0

    def test_dcg_discounts_by_rank(self):
        # gain 1 at rank 1, gain 1 at rank 3: 1/log2(2) + 1/log2(4) = 1.5
        assert dcg_at_k([1, 0, 1]) == pytest.approx(1.5)

    def test_ndcg_is_one_when_the_ideal_row_comes_first(self):
        assert ndcg_at_k([1, 0, 0, 0]) == pytest.approx(1.0)

    def test_ndcg_penalises_a_relevant_row_in_second_place(self):
        assert ndcg_at_k([0, 1, 0, 0]) == pytest.approx(1 / math.log2(3))

    def test_ndcg_is_zero_when_nothing_relevant_was_retrieved(self):
        # 0.0, not 1.0: a total miss must not read as a perfect score.
        assert ndcg_at_k([0, 0, 0]) == 0.0
        assert ndcg_at_k([]) == 0.0

    def test_ndcg_supports_graded_relevance(self):
        # A 2-grade document first is the ideal ordering: 1.0.
        assert ndcg_at_k([2, 0]) == pytest.approx(1.0)
        # The same document second: DCG is 3/log2(3) against an ideal DCG of
        # 3/log2(2) = 3, so nDCG = 1/log2(3) -- 0.63, not 1.89. nDCG is a ratio
        # against the best ordering, so it can never exceed 1.
        assert ndcg_at_k([0, 2]) == pytest.approx(1 / math.log2(3))

    def test_average_precision_rewards_finding_it_first(self):
        # (1/1 + 2/3) / 2
        assert average_precision_at_k([1, 0, 1]) == pytest.approx(5 / 6)
        assert average_precision_at_k([0, 1]) == pytest.approx(0.5)

    def test_average_precision_counts_the_documents_that_were_missed(self):
        # One of three relevant found, and found first: 1.0/3, not 1.0.
        assert average_precision_at_k([1], total_relevant=3) == pytest.approx(1 / 3)

    def test_average_precision_is_zero_without_a_hit(self):
        assert average_precision_at_k([0, 0, 0]) == 0.0

    def test_reciprocal_rank_reports_the_first_hit(self):
        assert reciprocal_rank([0, 0, 1]) == pytest.approx(1 / 3)
        assert reciprocal_rank([1, 0, 0]) == pytest.approx(1.0)
        assert reciprocal_rank([0, 0, 0]) == 0.0

    def test_k_must_be_at_least_one(self):
        with pytest.raises(ValueError):
            precision_at_k([1], 0)
        with pytest.raises(ValueError):
            recall_at_k([1], 0)
        with pytest.raises(ValueError):
            ndcg_at_k([1], 0)
        with pytest.raises(ValueError):
            average_precision_at_k([1], 0)

    def test_negative_grades_are_rejected(self):
        with pytest.raises(ValueError):
            ndcg_at_k([1, -1])


class TestRankerSelection:
    def test_the_default_ranker_is_unchanged(self):
        agent = EvidenceRetrievalAgent(DATASET_PATH)
        assert agent.ranker == "tfidf"

    def test_an_unknown_ranker_is_rejected_loudly(self):
        # Silence here would mean scoring every query with the wrong model.
        with pytest.raises(ValueError):
            EvidenceRetrievalAgent(DATASET_PATH, ranker="bm42")

    def test_a_ranker_can_come_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("VL_RANKER", "bm25")
        assert EvidenceRetrievalAgent(DATASET_PATH).ranker == "bm25"

    @pytest.mark.parametrize("ranker", RANKERS)
    def test_every_ranker_answers_an_exact_lookup(self, ranker):
        agent = EvidenceRetrievalAgent(DATASET_PATH, ranker=ranker)
        intake = IntakeAgent()
        log = MessageLog()
        message = agent.retrieve(
            intake.process("Is BRCA1 NC_000017.10:g.41197778A>G pathogenic?", log),
            log,
        )
        payload = message.decrypt_payload()
        assert payload["found"] is True
        assert payload["exact_match"] is True
        assert payload["ir_metadata"]["lexical_ranker"] == ranker
        scores = [r["relevance_score"] for r in payload["top_k_results"]]
        assert scores == sorted(scores, reverse=True)

    def test_bm25_ranker_produces_bm25_scores(self):
        agent = EvidenceRetrievalAgent(DATASET_PATH, ranker="bm25")
        hits = agent._bm25_search("BRCA1 c.68_69delAG", top_k=5)
        assert "bm25_score" in hits.columns
        assert len(hits) == 5
        values = list(hits["bm25_score"])
        assert values == sorted(values, reverse=True)
