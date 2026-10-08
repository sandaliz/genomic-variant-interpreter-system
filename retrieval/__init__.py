"""
retrieval/

Lexical retrieval models and ranking-quality metrics for the Evidence Agent.

This is deliberately its own package rather than part of agents/: the offline
evaluation scores the same models over a labelled sample, and the unit tests
check them against hand-computed values with no dataset and no pipeline
involved. Anything that lives in agents/ can only be exercised by building a
real pipeline, which is a poor place for arithmetic that has a right answer.

Contents
--------
* bm25    -- BM25 (Okapi) scoring over the same text field TF-IDF sees.
* fusion  -- Reciprocal Rank Fusion, and a simple score-level alternative.
* metrics -- Precision/Recall@k, AP@k, nDCG@k and MRR for one query's ranking.
"""

from retrieval.bm25 import DEFAULT_B, DEFAULT_K1, BM25Index, tokenize
from retrieval.fusion import reciprocal_rank_fusion, score_fusion
from retrieval.metrics import (
    average_precision_at_k,
    dcg_at_k,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
)

__all__ = [
    "BM25Index",
    "tokenize",
    "DEFAULT_K1",
    "DEFAULT_B",
    "reciprocal_rank_fusion",
    "score_fusion",
    "dcg_at_k",
    "ndcg_at_k",
    "average_precision_at_k",
    "precision_at_k",
    "recall_at_k",
    "reciprocal_rank",
]
