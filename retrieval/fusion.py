"""
retrieval/fusion.py

Combining two retrieval models into one ranking.

The problem this solves: TF-IDF cosine similarity and BM25 are not on the same
scale. Cosine is bounded in [0, 1]; BM25 is unbounded and corpus-dependent --
a score of 12 might be strong in one corpus and weak in another. Comparing
them directly, or weighting one by a constant, silently assumes a
comparability that does not exist.

Two standard answers, both implemented here:

* **Reciprocal Rank Fusion** (Cormack, Clarke & Buettcher, 2009) works on
  ranks alone. It never looks at a score, so the scale problem disappears
  entirely. This is what the hybrid ranker uses.
* **Score fusion** normalises each model's scores across the candidate set
  first, then averages. Simple, and it keeps relative score information that
  RRF discards, but the normalisation is batch-dependent: a candidate's
  hybrid score depends on what else was retrieved. Offline comparison only --
  the live pipeline does not use it, precisely because of that dependence.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Mapping, Sequence

# Cormack et al. use 60 and report that results are insensitive to it between
# roughly 10 and 100. Fixed here rather than exposed as a knob: a constant with
# a published justification is easier to defend than a tuned parameter.
DEFAULT_RRF_K = 60


def reciprocal_rank_fusion(
    rankings: Iterable[Sequence],
    k: int = DEFAULT_RRF_K,
) -> Dict:
    """
    Fuse ranked id lists into one score map.

    score(d) = Σ_r 1 / (k + rank_r(d)) over the rankings r that contain d, with
    ranks counted from 1. A document near the top of either list scores highly;
    a document that both models put mid-table beats one that only a single
    model liked, which is the behaviour that makes RRF robust when one model is
    confidently wrong.

    A repeated id inside one ranking counts once, at its best position -- a
    malformed list should not be able to buy a document extra weight.
    """
    if k < 1:
        raise ValueError("k must be at least 1")
    fused: Dict = {}
    for ranking in rankings:
        seen = set()
        for position, doc_id in enumerate(ranking, start=1):
            if doc_id in seen:
                continue
            seen.add(doc_id)
            fused[doc_id] = fused.get(doc_id, 0.0) + 1.0 / (k + position)
    return fused


def score_fusion(
    score_maps: Iterable[Mapping],
    weights: Sequence[float] = None,
) -> Dict:
    """
    Average several score maps after normalising each to [0, 1] by its maximum.

    Max-normalisation rather than min-max, and that is deliberate. Min-max maps
    the *worst* candidate to 0 and the best to 1 whether or not either is
    actually good, so a batch where every candidate is a weak match still
    produces a candidate that reads as a perfect hit -- the exact failure mode
    documented in ``EvidenceRetrievalAgent._rank_candidates``. Dividing by the
    maximum keeps a batch of weak matches weak.

    Keys absent from a map contribute 0 for that map's share of the average,
    which is what "this model did not retrieve it" should mean.
    """
    maps = [dict(m) for m in score_maps]
    if not maps:
        return {}
    if weights is None:
        weights = [1.0] * len(maps)
    if len(weights) != len(maps):
        raise ValueError("weights must match the number of score maps")

    total_weight = sum(weights)
    if total_weight <= 0:
        raise ValueError("weights must sum to a positive number")

    normalised = []
    for score_map, weight in zip(maps, weights):
        peak = max(score_map.values(), default=0.0)
        if peak <= 0:
            # Nothing scored: the map cannot express a preference, so it must
            # not be allowed to shift the average.
            normalised.append(({}, 0.0))
            continue
        normalised.append(
            ({key: value / peak for key, value in score_map.items()}, weight)
        )

    effective_weight = sum(weight for _, weight in normalised)
    if effective_weight <= 0:
        return {}

    fused: Dict = {}
    for score_map, weight in normalised:
        share = weight / effective_weight
        for key, value in score_map.items():
            fused[key] = fused.get(key, 0.0) + value * share
    return fused


def rank_from_scores(score_map: Mapping, descending: bool = True) -> List:
    """
    Score map to a ranked id list.

    Ties break on the id itself so a ranking is reproducible run to run --
    dict ordering is not part of the contract, and a metric computed from a
    randomly ordered tie is not a measurement.
    """
    return [key for key, _ in sorted(
        score_map.items(), key=lambda pair: (pair[1], pair[0]), reverse=descending
    )]
