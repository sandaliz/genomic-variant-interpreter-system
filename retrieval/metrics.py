"""
retrieval/metrics.py

Ranking-quality metrics for a single query's result list.

Every function takes the same input: a list of relevance grades, best-ranked
first, where 0 means "not relevant" and 1 (or more, for graded judgements)
means "relevant". That is the position of the metric -- the rank of each
document, not its score -- which is why a hybrid ranker based on RRF can be
measured with the same functions as a BM25 one.

What each metric answers:

* ``precision_at_k``      -- of the k we showed, how many were relevant?
* ``recall_at_k``         -- of everything relevant, how much did the top k hold?
* ``average_precision_at_k`` -- precision averaged over the ranks where a
  relevant document was found, so finding the right answer *first* scores
  higher than finding it tenth. This is the one that matches what a clinician
  actually experiences.
* ``ndcg_at_k``           -- discounted cumulative gain against the ideal
  ordering. Supports graded relevance (a 2-star review outranks a 0-star one).
* ``reciprocal_rank``     -- 1/rank of the first relevant document, the "how
  far down the list did we have to look" number.

For an exact-variant query there is exactly one relevant document, which makes
these degenerate in a specific way worth stating plainly: recall@k is 1.0 or
0.0, and nDCG@k with a single relevant document reduces to a discount on its
rank. The metrics are still the right ones -- they are computed over the same
ranking the live pipeline produces -- but a comparison between retrieval
models on this corpus is really a comparison of *where the right row lands*,
and the report says so rather than dressing up five numbers that move
together as five independent findings.
"""

from __future__ import annotations

import math
from typing import Sequence

# Logarithmic discount position, so DCG and anything built on it agree on where
# rank 1 starts. log2(position + 1) makes rank 1 worth 1.0 and rank 2 worth
# 0.63, which is the standard convention.
_DISCOUNT_BASE_OFFSET = 1


def _grades(relevances: Sequence) -> list:
    """Coerce any sequence of grades to a list of floats, rejecting negatives."""
    values = []
    for value in relevances:
        number = float(value)
        if number < 0:
            raise ValueError("relevance grades cannot be negative")
        values.append(number)
    return values


def _gain(grade: float) -> float:
    """
    Exponential gain, 2^grade - 1.

    With binary grades in {0, 1} this is the identity, so it changes nothing
    for the exact-match judgements this corpus uses. With graded judgements it
    is what makes a highly relevant document worth more than marginally
    relevant ones, which is the whole point of a graded metric.
    """
    return (2.0 ** grade) - 1.0


def _discount(position: int) -> float:
    return math.log2(position + _DISCOUNT_BASE_OFFSET)


def precision_at_k(relevances: Sequence, k: int) -> float:
    """Fraction of the top k that is relevant. Denominator is k, not the list."""
    if k < 1:
        raise ValueError("k must be at least 1")
    grades = _grades(relevances)[:k]
    if not grades:
        return 0.0
    relevant = sum(1 for grade in grades if grade > 0)
    return relevant / k


def recall_at_k(relevances: Sequence, k: int, total_relevant: int = None) -> float:
    """
    Fraction of all relevant documents that the top k holds.

    ``total_relevant`` defaults to the number of relevant documents *in the
    list*, which for an exact-variant query (one relevant row) is correct -- the
    retrieved list is the whole judgement. Pass it explicitly when the corpus
    holds relevant documents the ranking never returned, or recall will be
    flattered by what the model happened to find.
    """
    if k < 1:
        raise ValueError("k must be at least 1")
    grades = _grades(relevances)
    relevant_in_list = sum(1 for grade in grades if grade > 0)
    expected = relevant_in_list if total_relevant is None else int(total_relevant)
    if expected <= 0:
        # Nothing relevant exists, so there is nothing to recall. 0.0 rather
        # than 1.0: a query with no answer is not a retrieval success.
        return 0.0
    found = sum(1 for grade in grades[:k] if grade > 0)
    return found / expected


def dcg_at_k(relevances: Sequence, k: int = None) -> float:
    """Discounted cumulative gain over the top k positions."""
    grades = _grades(relevances)
    if k is not None:
        if k < 1:
            raise ValueError("k must be at least 1")
        grades = grades[:k]
    return sum(_gain(grade) / _discount(position)
               for position, grade in enumerate(grades, start=1))


def ndcg_at_k(relevances: Sequence, k: int = None) -> float:
    """
    DCG of the ranking divided by the DCG of the best possible ordering.

    Returns 0.0 when nothing relevant was retrieved at all -- the ideal DCG is
    then 0 and the ratio is undefined, and reporting 1.0 would mark a total
    miss as a perfect one.
    """
    grades = _grades(relevances)
    if not grades:
        return 0.0
    ideal = sorted(grades, reverse=True)
    best = dcg_at_k(ideal, k)
    if best <= 0:
        return 0.0
    return dcg_at_k(grades, k) / best


def average_precision_at_k(relevances: Sequence, k: int = None,
                           total_relevant: int = None) -> float:
    """
    Mean of the precision values at each rank where a relevant document sits.

    Divided by the number of relevant documents that exist (not by the number
    found), so a ranking that surfaces only one of five relevant documents is
    penalised for the four it missed rather than scoring a perfect 1.0 for the
    one it caught.
    """
    grades = _grades(relevances)
    if k is not None:
        if k < 1:
            raise ValueError("k must be at least 1")
        grades = grades[:k]
    relevant_found = [position for position, grade
                      in enumerate(grades, start=1) if grade > 0]
    if not relevant_found:
        return 0.0
    expected = len(relevant_found) if total_relevant is None else int(total_relevant)
    if expected <= 0:
        return 0.0
    cumulative = 0.0
    for position in relevant_found:
        hits = sum(1 for earlier in grades[:position] if earlier > 0)
        cumulative += hits / position
    return cumulative / expected


def reciprocal_rank(relevances: Sequence) -> float:
    """1 / rank of the first relevant document, or 0.0 if there is none."""
    for position, grade in enumerate(_grades(relevances), start=1):
        if grade > 0:
            return 1.0 / position
    return 0.0
