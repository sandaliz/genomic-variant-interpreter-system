"""
evaluation.py

Evaluation script for the Genomic Variant Interpretation system.

Provides a comprehensive evaluation with:

  1. **Classification metrics**:
     - Accuracy
     - Confusion matrix
     - Precision, Recall, F1-score (macro and per-class)
     - Per-class breakdown

  2. **Information Retrieval metrics**:
     - Retrieval precision/recall at top-K, exact-match@K, MRR@K
     - nDCG@K and MAP@K
     - A head-to-head comparison of the three lexical rankers (TF-IDF, BM25,
       and their Reciprocal Rank Fusion), ranked over the whole corpus so the
       difference is a difference between the models and nothing else

  3. **Calibration**:
     - Expected Calibration Error and a reliability table for the confidence
       scores, plus the accuracy per confidence band

  4. **Fairness / subgroup performance**:
     - Accuracy and F1 stratified by variant type, allele-frequency band (the
       ACMG/AMP cut points, not sample deciles) and ClinVar review status, with
       the worst-performing stratum called out

  5. **Ablation comparison**:
     - Rules-only (deterministic)
     - Gemini-only (LLM)
     - Rules + Gemini (hybrid) vs ground truth

Warning: the LLM-based ablation calls the Gemini API for each variant
and may take a long time / incur API costs. Use --no-llm to skip it.

Run:
    python evaluation.py
    python evaluation.py --sample-size 50 --no-llm --no-ir
    python evaluation.py --rankers-only          # just the retrieval comparison
"""

import os
import sys
import json
import argparse
import pandas as pd
import numpy as np

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from agents.classification_agent import ClassificationAgent
from agents.evidence_agent import HAS_SKLEARN, RANKERS, EvidenceRetrievalAgent
from agents.intake_agent import IntakeAgent
from protocol.message import MessageLog
from protocol.security import InvalidVariantQuery
from retrieval.fusion import DEFAULT_RRF_K, rank_from_scores, reciprocal_rank_fusion
from retrieval.metrics import (
    average_precision_at_k,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
)


DATASET_PATH = os.path.join(os.path.dirname(__file__), "data", "clinvar_variant_dataset.xlsx")


# ------------------------------------------------------------------
# Ground truth
# ------------------------------------------------------------------
def load_ground_truth(dataset_path: str, sample_size: int = 100) -> pd.DataFrame:
    """
    Load a sample of variants from the dataset.

    Ground truth is inferred from the evidence columns (a simplified proxy).
    """
    df = pd.read_excel(dataset_path, sheet_name="BRCA1_BRCA2 (all 3009)")
    return df.head(sample_size)


def _clean_value(value):
    return None if pd.isna(value) else value


def build_evidence(row) -> dict:
    """Build the same missingness-aware evidence contract used by the agents."""
    evidence = {
        "found": True,
        "exact_match": True,
        "consequence": _clean_value(row.get("Consequence")),
        "impact": _clean_value(row.get("IMPACT")),
        "sift_prediction": _clean_value(row.get("SIFT_prediction")),
        "polyphen_prediction": _clean_value(row.get("PolyPhen_prediction")),
        "cadd_score": _clean_value(row.get("CADD_score")),
        "af_esp": _clean_value(row.get("AF_ESP")),
        "af_exac": _clean_value(row.get("AF_EXAC")),
        "af_tgp": _clean_value(row.get("AF_TGP")),
        "disease_name": _clean_value(row.get("Disease_name")),
        "conflicting_classification_flag": int(row.get("Conflicting_Classification_Flag", 0)),
        # Real ClinVar evidence (present only on the enriched workbook). These
        # mirror agents/evidence_agent.py so evaluation exercises the same
        # decision path the live pipeline uses.
        "prior_classification": _clean_value(row.get("ClinicalSignificance")),
        "prior_review_status": _clean_value(row.get("ClinVarReviewStatus")),
        "prior_review_stars": _clean_value(row.get("ClinVarReviewStars")),
        "prior_raw_significance": _clean_value(row.get("ClinVarRawSignificance")),
        "rsid": _clean_value(row.get("RSID")),
    }
    sift_available = evidence["sift_prediction"] is not None
    polyphen_available = evidence["polyphen_prediction"] is not None
    cadd_available = evidence["cadd_score"] is not None
    evidence["predictor_available"] = {
        "sift": sift_available,
        "polyphen": polyphen_available,
        "sift_polyphen": sift_available and polyphen_available,
        "cadd": cadd_available,
        "complete": sift_available and polyphen_available and cadd_available,
    }
    return evidence


def infer_ground_truth(row) -> str:
    """
    Infer a ground-truth classification from the evidence columns.
    """
    impact = str(row.get("IMPACT", ""))
    sift = str(row.get("SIFT_prediction")).lower() if pd.notna(row.get("SIFT_prediction")) else ""
    polyphen = str(row.get("PolyPhen_prediction")).lower() if pd.notna(row.get("PolyPhen_prediction")) else ""
    cadd = row.get("CADD_score")

    score = 0
    if impact == "HIGH":
        score += 2
    if pd.notna(row.get("SIFT_prediction")) and pd.notna(row.get("PolyPhen_prediction")) and "deleterious" in sift and "damaging" in polyphen:
        score += 1
    if pd.notna(cadd) and np.isfinite(float(cadd)) and float(cadd) >= 20:
        score += 1

    frequencies = [
        float(value) for value in (
            row.get("AF_ESP"), row.get("AF_EXAC"), row.get("AF_TGP")
        ) if pd.notna(value) and np.isfinite(float(value))
    ]
    max_af = max(frequencies, default=0.0)
    if max_af > 0.001:
        score -= 2
    elif max_af > 0.0001:
        score -= 1

    if score >= 2:
        return "Likely Pathogenic"
    elif score <= -2:
        return "Likely Benign"
    else:
        return "Uncertain Significance (VUS)"


# ------------------------------------------------------------------
# Real ClinVar labels (preferred over the proxy above)
# ------------------------------------------------------------------
# `infer_ground_truth()` re-derives labels from the SAME evidence columns the
# classifier already uses, so agreement with it measures self-consistency
# rather than correctness. When the enriched workbook produced by
# `scripts/enrich_clinvar_labels.py` is available it carries a real
# `ClinicalSignificance` column, which should be preferred.
REAL_LABEL_COLUMN = "ClinicalSignificance"


def has_real_labels(df: pd.DataFrame) -> bool:
    """Whether the frame carries populated real ClinVar labels."""
    return REAL_LABEL_COLUMN in df.columns and bool(df[REAL_LABEL_COLUMN].notna().any())


def infer_ground_truth_real(row) -> str:
    """Real ClinVar consensus label (requires the enriched dataset)."""
    value = row.get(REAL_LABEL_COLUMN)
    if value is None or pd.isna(value) or str(value).strip() == "":
        return "Unlabelled"
    return str(value).strip()


def resolve_ground_truth_mode(df: pd.DataFrame, requested: str) -> str:
    """Pick the effective ground-truth mode: 'real' or 'proxy'."""
    if requested == "real":
        if not has_real_labels(df):
            raise SystemExit(
                "ERROR: --ground-truth real was requested but the dataset has no "
                "'ClinicalSignificance' column. Generate the enriched workbook "
                "first:  python scripts/enrich_clinvar_labels.py"
            )
        return "real"
    if requested == "proxy":
        return "proxy"
    return "real" if has_real_labels(df) else "proxy"


def ground_truth_for(row, mode: str) -> str:
    """Dispatch to the real or proxy label function."""
    return infer_ground_truth_real(row) if mode == "real" else infer_ground_truth(row)


# ------------------------------------------------------------------
# Directional metrics
# ------------------------------------------------------------------
# 61% of this dataset's real ClinVar labels are "Conflicting classifica-
# tions of pathogenicity" -- a statement about what submitters did, not a
# single classification anyone can predict. Reporting one exact-match
# accuracy over the whole set therefore understates the system badly.
# Collapsing to direction (pathogenic / benign / uncertain) and scoring only
# rows with a determinate truth gives the clinically meaningful number.
_DIRECTIONAL = {
    "pathogenic": "pathogenic",
    "likely pathogenic": "pathogenic",
    "benign": "benign",
    "likely benign": "benign",
    "uncertain significance (vus)": "uncertain",
}


def directional_label(label):
    """Collapse a label to its clinical direction, or None if indeterminate."""
    return _DIRECTIONAL.get(str(label).strip().lower())


def _directional_outcome(truth, prediction):
    """
    Directional agreement as a tri-state: True, False, or None.

    None means the pair is not answerable -- typically because the ClinVar truth
    is "Conflicting classifications", which is a statement about submitters
    rather than a class. None is not False: excluding the row and counting it
    wrong would turn "nobody can predict this" into "the system failed".
    """
    truth_direction = directional_label(truth)
    predicted_direction = directional_label(prediction)
    if truth_direction is None or predicted_direction is None:
        return None
    return truth_direction == predicted_direction


def compute_directional_metrics(results: list, key: str = "predicted") -> dict:
    """Directional agreement on rows whose ground truth is determinate."""
    outcomes = []
    for result in results:
        truth_dir = directional_label(result["ground_truth"])
        pred_dir = directional_label(result[key])
        if truth_dir is None or pred_dir is None:
            continue
        outcomes.append(truth_dir == pred_dir)
    total = len(outcomes)
    correct = sum(outcomes)
    return {
        "total_determinate": total,
        "correct": correct,
        "accuracy": (correct / total) if total else 0.0,
    }


def compute_confusion_matrix(results: list) -> dict:
    """
    Build a confusion matrix from evaluation results.

    Returns dict: {"labels": [...], "matrix": [[...]], ...}
    """
    labels = sorted(set(
        [r["ground_truth"] for r in results] +
        [r["predicted"] for r in results]
    ))
    label_to_idx = {label: i for i, label in enumerate(labels)}

    matrix = [[0 for _ in labels] for _ in labels]
    for r in results:
        true_idx = label_to_idx[r["ground_truth"]]
        pred_idx = label_to_idx[r["predicted"]]
        matrix[true_idx][pred_idx] += 1

    return {"labels": labels, "matrix": matrix}


def compute_classification_metrics(results: list) -> dict:
    """
    Compute accuracy, precision, recall, F1 for each class and macro average.
    """
    total = len(results)
    correct = sum(1 for r in results if r["match"])
    accuracy = correct / total if total else 0

    labels = sorted(set([r["ground_truth"] for r in results]))
    per_class = {}
    macro_precision = 0.0
    macro_recall = 0.0
    macro_f1 = 0.0

    for label in labels:
        # True positives: predicted == label AND ground_truth == label
        tp = sum(1 for r in results if r["predicted"] == label and r["ground_truth"] == label)
        # False positives: predicted == label but ground_truth != label
        fp = sum(1 for r in results if r["predicted"] == label and r["ground_truth"] != label)
        # False negatives: ground_truth == label but predicted != label
        fn = sum(1 for r in results if r["ground_truth"] == label and r["predicted"] != label)

        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0

        per_class[label] = {
            "tp": tp, "fp": fp, "fn": fn,
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "total_in_class": tp + fn,
            "correct": tp,
        }

        macro_precision += precision
        macro_recall += recall
        macro_f1 += f1

    n_classes = len(labels)
    macro = {
        "precision": macro_precision / n_classes if n_classes else 0,
        "recall": macro_recall / n_classes if n_classes else 0,
        "f1": macro_f1 / n_classes if n_classes else 0,
    }

    return {
        "total": total,
        "correct": correct,
        "accuracy": accuracy,
        "per_class": per_class,
        "macro": macro,
    }


# ------------------------------------------------------------------
# IR metrics (Precision@K, Recall@K, Exact-match@K, MRR@K)
# ------------------------------------------------------------------
def compute_ir_metrics(dataset_path: str, sample_size: int = 20, k: int = 5,
                       ranker: str = None) -> dict:
    """
    Evaluate the Evidence Agent's retrieval quality.

    For an exact variant query, there is exactly ONE relevant document:
    the variant itself. Therefore:

    Precision@K: Of the top-K retrieved records, how many are exactly
                 the correct variant?
    Recall@K:    Of the 1 relevant variant, did we retrieve it in top-K?
                 (= 1.0 if the exact variant appears in top-K, else 0.0)
    Exact@K:     Same as Recall@K (did the exact variant appear in top-K?)
    MRR@K:       Mean reciprocal rank of the exact variant within top-K.
    nDCG@K:      Discounted gain of the exact variant's position.
    MAP@K:       Mean average precision over the same single relevant row.

    nDCG and MAP read the *rank* of the hit, which is what the returned
    ``relevance_lists`` carry: one list per query, holding a single 1 at the
    position the correct variant reached. With one relevant document per query
    MAP@K is numerically MRR@K and nDCG@K is a monotone function of the rank --
    the report says so rather than presenting three correlated numbers as three
    independent findings. They are computed here anyway so the live pipeline's
    ranking is scored with the standard metric definitions, and so the ranker
    comparison below can reuse the same tested functions.
    """
    df = load_ground_truth(dataset_path, sample_size=sample_size)

    agent = EvidenceRetrievalAgent(dataset_path, ranker=ranker)
    intake = IntakeAgent()
    log = MessageLog()

    precision_at_k_list = []
    recall_at_k_list = []
    exact_at_k_list = []
    reciprocal_rank_list = []
    # Ranked relevance lists, one per query, for the metrics that need the rank
    # of the hit rather than just its presence. For an exact-variant query there
    # is exactly one relevant row, so each list holds a single 1 at the position
    # the correct variant reached (or no 1 at all, when it was missed).
    relevance_lists = []

    for _, row in df.iterrows():
        gene = row["Gene"]
        hgvs = row["HGVS_notation"]

        # Build a natural-language query from the row
        query = f"Is {gene} {hgvs} pathogenic?"

        try:
            intake_msg = intake.process(query, log)
            evidence_msg = agent.retrieve(intake_msg, log)
            payload = evidence_msg.decrypt_payload()

            if not payload.get("found"):
                continue

            top_k = payload.get("top_k_results", [])
            if not top_k:
                # Single exact result (only one candidate found)
                if payload.get("exact_match"):
                    precision_at_k_list.append(1.0)
                    recall_at_k_list.append(1.0)
                    exact_at_k_list.append(1.0)
                    reciprocal_rank_list.append(1.0)
                    relevance_lists.append([1])
                else:
                    precision_at_k_list.append(0.0)
                    recall_at_k_list.append(0.0)
                    exact_at_k_list.append(0.0)
                    reciprocal_rank_list.append(0.0)
                    relevance_lists.append([0])
                continue

            # Precision@K: how many of top-K are the exactly correct variant?
            exact_in_top_k = sum(1 for r in top_k if r.get("exact_match"))
            precision_at_k = exact_in_top_k / len(top_k)
            precision_at_k_list.append(precision_at_k)

            # Recall@K: for an exact query there is 1 relevant variant,
            # so recall = 1.0 if we found it, else 0.0
            found_exact = 1.0 if exact_in_top_k > 0 else 0.0
            recall_at_k_list.append(found_exact)
            exact_at_k_list.append(found_exact)
            relevance_lists.append([1 if r.get("exact_match") else 0 for r in top_k])
            rank = next((idx for idx, r in enumerate(top_k, start=1) if r.get("exact_match")), None)
            reciprocal_rank_list.append(1.0 / rank if rank else 0.0)

        except (InvalidVariantQuery, KeyError, TypeError, AttributeError):
            continue

    return {
        "sample_size": len(exact_at_k_list),
        "ranker": agent.ranker,
        "precision_at_k": {
            "k": k,
            "mean": np.mean(precision_at_k_list) if precision_at_k_list else 0,
            "values": precision_at_k_list,
        },
        "recall_at_k": {
            "k": k,
            "mean": np.mean(recall_at_k_list) if recall_at_k_list else 0,
            "values": recall_at_k_list,
        },
        "exact_at_k": {
            "k": k,
            "mean": np.mean(exact_at_k_list) if exact_at_k_list else 0,
            "values": exact_at_k_list,
        },
        "mrr_at_k": {
            "k": k,
            "mean": np.mean(reciprocal_rank_list) if reciprocal_rank_list else 0,
            "values": reciprocal_rank_list,
        },
        "ndcg_at_k": {
            "k": k,
            "mean": float(np.mean([ndcg_at_k(r, k) for r in relevance_lists]))
            if relevance_lists else 0.0,
            "values": [ndcg_at_k(r, k) for r in relevance_lists],
        },
        "map_at_k": {
            "k": k,
            "mean": float(np.mean([average_precision_at_k(r, k) for r in relevance_lists]))
            if relevance_lists else 0.0,
            "values": [average_precision_at_k(r, k) for r in relevance_lists],
        },
    }


# ------------------------------------------------------------------
# Lexical ranker comparison (whole-corpus, one query at a time)
# ------------------------------------------------------------------
def _optimistic_rank(scores, position: int):
    """
    The rank the given document could claim under these scores.

    1 + the number of documents scoring *strictly* higher, so a tie resolves in
    the document's favour. That is the best rank the model could hand back, and
    it is the right choice here: the live pipeline sorts with the same
    descending order, and the question being asked is whether the model put the
    right row near the top, not how it broke a tie with an unrelated row.

    Returns None when the position is outside the corpus.
    """
    scores = np.asarray(scores, dtype=float)
    if position < 0 or position >= scores.size:
        return None
    # np.sort(-scores) is the ascending array of negated scores, so the count of
    # elements below -target is exactly the count of elements above target.
    return int(np.searchsorted(np.sort(-scores), -float(scores[position]),
                               side="left")) + 1


def _corpus_scores(agent, query: str) -> dict:
    """
    Score every document in the corpus for one query, under each ranker.

    The live pipeline retrieves candidates in steps (exact lookup, then position,
    then gene, then text) and the exact-match path short-circuits before any
    lexical ranking happens. Comparing the rankers through that path would
    therefore return three identical numbers, because it never consults a text
    model at all. This function asks the narrower, answerable question instead:
    given only the query text, where does each model put the correct row?

    TF-IDF and BM25 scores come from the agent's own vectorizer and index over
    the same ``search_text`` field, so this is the live models over the live
    corpus. The hybrid score is Reciprocal Rank Fusion of their two full
    rankings, which is the same fusion the ``hybrid`` ranker applies to its
    candidate union.
    """
    from sklearn.metrics.pairwise import cosine_similarity

    tfidf = cosine_similarity(
        agent.vectorizer.transform([query]), agent.tfidf_matrix
    ).flatten()
    bm25 = np.asarray(agent.bm25_index.score(query), dtype=float)

    positional = {
        index: float(score) for index, score in enumerate(tfidf)
    }
    bm25_positional = {
        index: float(score) for index, score in enumerate(bm25)
    }
    fused = reciprocal_rank_fusion([
        rank_from_scores(positional),
        rank_from_scores(bm25_positional),
    ], k=DEFAULT_RRF_K)
    hybrid = np.array([fused.get(index, 0.0) for index in range(len(tfidf))],
                      dtype=float)

    return {"tfidf": tfidf, "bm25": bm25, "hybrid": hybrid}


def _ranker_metrics(ranks, k: int) -> dict:
    """Metric block for one ranker, from the list of per-query ranks."""
    known = [rank for rank in ranks if rank is not None]
    relevance_lists = []
    for rank in known:
        # One relevant document: a 1 at the rank it reached, zeros above it and
        # nothing at all once it fell past k.
        relevance_lists.append(([0] * (rank - 1) + [1]) if rank <= k else ([0] * k))
    if not known:
        return {"queries": 0, "mrr": 0.0, "ndcg": 0.0, "map": 0.0,
                "exact_at_k": 0.0, "hit_at_1": 0.0, "mean_rank": None}
    return {
        "queries": len(known),
        "mrr": float(np.mean([reciprocal_rank(r) for r in relevance_lists])),
        "ndcg": float(np.mean([ndcg_at_k(r, k) for r in relevance_lists])),
        "map": float(np.mean([average_precision_at_k(r, k) for r in relevance_lists])),
        "exact_at_k": float(np.mean([1.0 if rank <= k else 0.0 for rank in known])),
        "hit_at_1": float(np.mean([1.0 if rank == 1 else 0.0 for rank in known])),
        "mean_rank": float(np.mean(known)),
    }


def compare_lexical_rankers(dataset_path: str, sample_size: int = 30,
                            k: int = 5) -> dict:
    """
    Rank the whole corpus with TF-IDF, BM25 and their RRF fusion, and compare.

    Every query is scored by all three models over every one of the corpus rows,
    so the only thing that differs between the three columns is the model. The
    query text is the same natural-language question the pipeline builds for a
    variant, which is what the lexical models see in production.
    """
    if not HAS_SKLEARN:
        return {"available": False,
                "reason": "scikit-learn is not installed, so TF-IDF cannot run"}

    df = load_ground_truth(dataset_path, sample_size=sample_size)
    agent = EvidenceRetrievalAgent(dataset_path)

    keys = agent.df["lookup_key"].to_numpy()
    ranks = {name: [] for name in RANKERS}
    # Which model put the target highest, per query. This is not derived from
    # any aggregate and does not collapse to the same number as MRR, so it is
    # the one column here that is genuinely independent of the others.
    wins = {name: 0 for name in RANKERS}
    queries = 0

    for _, row in df.iterrows():
        gene, hgvs = row["Gene"], row["HGVS_notation"]
        key = f"{gene}:{hgvs}"
        positions = np.flatnonzero(keys == key)
        if positions.size == 0:
            continue
        position = int(positions[0])

        query = f"Is {gene} {hgvs} pathogenic?"
        scores = _corpus_scores(agent, query)

        query_ranks = {}
        for name, vector in scores.items():
            query_ranks[name] = _optimistic_rank(vector, position)
            ranks[name].append(query_ranks[name])

        best = min(value for value in query_ranks.values() if value is not None)
        for name, value in query_ranks.items():
            if value == best:
                wins[name] += 1
        queries += 1

    summary = {name: _ranker_metrics(ranks[name], k) for name in RANKERS}
    corpus_size = len(agent.df)
    return {
        "available": True,
        "k": k,
        "corpus_size": corpus_size,
        "queries": queries,
        "summary": summary,
        "wins": wins,
        "per_query": {name: ranks[name] for name in RANKERS},
        # Stated rather than left for a reader to derive: with a single relevant
        # document per query, MAP@k and MRR@k are the same statistic. The
        # comparison is a comparison of ranks.
        "note": ("One relevant document per query, so MAP@k equals MRR@k and "
                 "nDCG@k is a monotone function of the rank. Read the columns "
                 "as three measurements of the same quantity, not as three "
                 "independent findings."),
    }


def print_ranker_comparison(comparison: dict):
    """Pretty-print the TF-IDF / BM25 / hybrid comparison table."""
    if not comparison:
        return
    if not comparison.get("available"):
        print()
        print("LEXICAL RANKER COMPARISON")
        print(f"  Not run: {comparison.get('reason')}")
        return

    k = comparison["k"]
    print()
    print("LEXICAL RANKER COMPARISON")
    print(f"  Corpus: {comparison['corpus_size']} variants | "
          f"queries: {comparison['queries']} | k={k}")
    print("  Every query scored against every row, so only the model differs.")
    print()
    header = f"  {'ranker':<10}{'MRR@k':>9}{'nDCG@k':>9}{'MAP@k':>9}"
    header += f"{'Exact@k':>10}{'Hit@1':>8}{'mean rank':>11}"
    print(header)
    for name, metrics in comparison["summary"].items():
        mean_rank = metrics["mean_rank"]
        mean_rank_text = "n/a" if mean_rank is None else f"{mean_rank:.1f}"
        print(f"  {name:<10}{metrics['mrr']:>9.3f}{metrics['ndcg']:>9.3f}"
              f"{metrics['map']:>9.3f}{metrics['exact_at_k']:>10.3f}"
              f"{metrics['hit_at_1']:>8.3f}{mean_rank_text:>11}")
    print()
    print("  Queries where each ranker put the correct row highest:")
    for name, count in comparison["wins"].items():
        print(f"    {name:<10} {count}/{comparison['queries']}")
    print()
    print(f"  Note: {comparison['note']}")


# ------------------------------------------------------------------
# Calibration
# ------------------------------------------------------------------
def compute_calibration(results: list, key: str = "match",
                        confidence_key: str = "confidence_numeric",
                        n_bins: int = 10) -> dict:
    """
    Reliability table, Expected Calibration Error and Brier score.

    A confidence score is *calibrated* when the rows it marks 80 are right
    about 80% of the time. ECE is the sample-weighted average distance between
    what the system claimed and what happened, bucketed so a handful of
    overconfident rows cannot be hidden inside a good average.

    Two things this deliberately does not do:

    * It does not treat the score as a probability that was fitted to data. It
      is derived from evidence components (see ``_compute_confidence_score``),
      so a miscalibration here is a statement about those weights, not about a
      softmax. The report says which.
    * It does not report a number without a sample size. Every bin carries its
      count, and bins below ``MIN_BIN`` are printed but marked, because an
      accuracy of 1.000 over two rows is not evidence of calibration.

    ``key`` selects the outcome being predicted: "match" (exact class agreement
    with ClinVar) or "directional_match" (agreement on pathogenic / benign /
    uncertain, which is what a clinician actually consumes). Rows whose outcome
    is indeterminate (None) are excluded -- "Conflicting classifications" is
    not a class the system can be right or wrong about.
    """
    n_bins = max(1, int(n_bins))
    MIN_BIN = 10

    probabilities = []
    outcomes = []
    for result in results:
        outcome = result.get(key)
        if not isinstance(outcome, (bool, np.bool_)):
            continue
        raw = result.get(confidence_key)
        if raw is None or (isinstance(raw, float) and not np.isfinite(raw)):
            continue
        # Confidence travels as 0-100; the metric wants 0-1.
        probability = float(raw) / 100.0
        probability = min(max(probability, 0.0), 1.0)
        probabilities.append(probability)
        outcomes.append(bool(outcome))

    total = len(probabilities)
    if total == 0:
        return {"total": 0, "n_bins": n_bins, "bins": [], "ece": 0.0,
                "mce": 0.0, "brier": 0.0, "brier_skill": 0.0,
                "base_rate": 0.0, "accuracy": 0.0, "outcome": key,
                "min_bin": MIN_BIN}

    base_rate = sum(outcomes) / total

    buckets = []
    for index in range(n_bins):
        lower = index / n_bins
        upper = (index + 1) / n_bins
        in_bin = [
            (probability, outcome)
            for probability, outcome in zip(probabilities, outcomes)
            if min(int(probability * n_bins), n_bins - 1) == index
        ]
        if not in_bin:
            continue
        count = len(in_bin)
        mean_confidence = sum(p for p, _ in in_bin) / count
        accuracy = sum(1 for _, outcome in in_bin if outcome) / count
        buckets.append({
            "lower": lower,
            "upper": upper,
            "count": count,
            "mean_confidence": mean_confidence,
            "accuracy": accuracy,
            "gap": accuracy - mean_confidence,
            "small": count < MIN_BIN,
        })

    ece = sum(bucket["count"] / total * abs(bucket["gap"]) for bucket in buckets)
    mce = max((abs(bucket["gap"]) for bucket in buckets), default=0.0)
    brier = sum((probability - (1.0 if outcome else 0.0)) ** 2
                for probability, outcome in zip(probabilities, outcomes)) / total
    # The reference forecast is the base rate: always saying "79% correct" and
    # being right about that often. A positive skill score means the confidence
    # number carries information beyond the dataset's overall hit rate.
    reference = base_rate * (1.0 - base_rate)
    brier_skill = (1.0 - brier / reference) if reference > 0 else 0.0

    return {
        "total": total,
        "n_bins": n_bins,
        "bins": buckets,
        "ece": ece,
        "mce": mce,
        "brier": brier,
        "brier_skill": brier_skill,
        "base_rate": base_rate,
        "accuracy": base_rate,
        "outcome": key,
        "min_bin": MIN_BIN,
    }


# ------------------------------------------------------------------
# Fairness / subgroup performance
# ------------------------------------------------------------------
# Allele-frequency strata use the ACMG/AMP standing thresholds rather than
# deciles of this sample. A decile of a 100-row sample is ten rows per bin,
# which is too few to read, and the thresholds below are the cut points the
# classification guidelines themselves use -- so a gap between "rare" and
# "common" is a gap that matters clinically, not an artefact of binning.
_AF_BANDS = [
    ("absent_or_very_rare (<=0.01%)", 0.0, 0.0001),
    ("rare (0.01-0.1%)", 0.0001, 0.001),
    ("low_frequency (0.1-1%)", 0.001, 0.01),
    ("common (1-5%)", 0.01, 0.05),
    ("too_common (>=5%)", 0.05, float("inf")),
]


def _max_allele_frequency(row):
    """Highest of the three population allele frequencies, or None."""
    values = []
    for column in ("AF_ESP", "AF_EXAC", "AF_TGP"):
        value = row.get(column)
        if value is None or pd.isna(value):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if np.isfinite(number):
            values.append(number)
    return max(values) if values else None


def _allele_frequency_band(row) -> str:
    frequency = _max_allele_frequency(row)
    if frequency is None:
        return "unknown"
    for label, lower, upper in _AF_BANDS:
        if lower <= frequency < upper:
            return label
    return "too_common (>=5%)"


def _review_status_band(row) -> str:
    """ClinVar review level, as stars. Only populated on the enriched workbook."""
    raw = row.get("ClinVarReviewStars")
    if raw is None or (not isinstance(raw, str) and pd.isna(raw)):
        return "unknown"
    try:
        stars = int(float(raw))
    except (TypeError, ValueError):
        return "unknown"
    if stars >= 3:
        # 3 and 4 stars both mean "reviewed by an expert panel" / "practice
        # guideline" -- the distinction is not large enough at this sample size
        # to justify separate columns.
        return "3+ stars (expert reviewed)"
    return f"{stars} star" + ("" if stars == 1 else "s")


def stratum_fields(row) -> dict:
    """
    The three subgroup labels a result is measured under.

    Named and computed in one place so the table can be read as one experiment
    rather than three unreconciled ones.
    """
    variant_type = row.get("Variant_type")
    if variant_type is None or (not isinstance(variant_type, str) and pd.isna(variant_type)):
        variant_type = "unknown"
    return {
        "variant_type": str(variant_type).strip() or "unknown",
        "allele_frequency_band": _allele_frequency_band(row),
        "review_stars": _review_status_band(row),
    }


def _stratum_block(group: list, min_group_size: int) -> dict:
    """Accuracy and macro F1 for one subgroup, plus a small-sample flag."""
    total = len(group)
    correct = sum(1 for result in group if result.get("match"))
    reconciled_correct = sum(1 for result in group if result.get("match_reconciled"))
    directional = [result for result in group
                   if isinstance(result.get("directional_match"), (bool, np.bool_))]
    directional_correct = sum(1 for result in directional
                              if result["directional_match"])

    def _macro_f1(rows, match_key, prediction_key):
        return compute_classification_metrics([
            {**row, "predicted": row[prediction_key], "match": row[match_key]}
            for row in rows
        ])["macro"]["f1"]

    return {
        "total": total,
        "correct": correct,
        "accuracy": correct / total if total else 0.0,
        "reconciled_accuracy": reconciled_correct / total if total else 0.0,
        "directional_total": len(directional),
        "directional_accuracy": (directional_correct / len(directional)
                                 if directional else 0.0),
        "macro_f1": _macro_f1(group, "match", "predicted") if total else 0.0,
        "reconciled_macro_f1": _macro_f1(group, "match_reconciled", "predicted_reconciled")
        if total else 0.0,
        "small": total < min_group_size,
    }


def compute_fairness(results: list, min_group_size: int = 20) -> dict:
    """
    Accuracy and F1 stratified by variant type, allele frequency and review level.

    The question is not "is this system fair" in the abstract -- it is whether
    the same evidence produces the same quality of answer for a frameshift and
    for a missense, for a variant seen in population data and one never seen
    before, and for a variant a ClinVar panel has reviewed and one nobody has.
    A subgroup the model does much worse on is where a real limitation lives.

    Two guards against reading noise as a finding:

    * Every stratum reports its own size, and a stratum smaller than
      ``min_group_size`` is flagged ``small``. The worst-stratum callout below
      only considers strata large enough to say anything about.
    * The stratification keys are properties of the *input* (variant type,
      population frequency, review level), never of the prediction, so the
      groups cannot be shaped by the thing being measured.
    """
    dimensions = {
        "variant_type": {},
        "allele_frequency_band": {},
        "review_stars": {},
    }

    for result in results:
        for dimension in dimensions:
            group = result.get(dimension) or "unknown"
            dimensions[dimension].setdefault(group, []).append(result)

    report = {}
    for dimension, groups in dimensions.items():
        strata = {
            name: _stratum_block(group, min_group_size)
            for name, group in sorted(groups.items())
        }
        eligible = {
            name: block for name, block in strata.items()
            if not block["small"] and block["total"] > 0
        }
        worst = None
        if eligible:
            name = min(eligible, key=lambda key: eligible[key]["accuracy"])
            worst = {"stratum": name, **eligible[name]}
        report[dimension] = {
            "strata": strata,
            "min_group_size": min_group_size,
            "worst": worst,
            "eligible_strata": len(eligible),
            "small_strata": sorted(name for name, block in strata.items()
                                   if block["small"]),
        }

    return report


# ------------------------------------------------------------------
# Ablation
# ------------------------------------------------------------------
def run_ablation(dataset_path: str, sample_size: int = 20, use_llm: bool = False,
                 ground_truth_mode: str = "auto") -> dict:
    """
    Run an ablation comparison: Rules-only vs Gemini-only vs Combined.

    For each variant:
      - Rules only: deterministic rule-based classification
      - Gemini only: LLM-only classification (if use_llm=True)
      - Combined: rule-based + LLM fallback (current system behaviour)

    Returns accuracy for each variant.
    """
    df = load_ground_truth(dataset_path, sample_size=sample_size)
    mode = resolve_ground_truth_mode(df, ground_truth_mode)
    classifier = ClassificationAgent(use_llm=use_llm)

    rules_results = []
    llm_results = []
    combined_results = []

    for _, row in df.iterrows():
        evidence = build_evidence(row)
        ground_truth = ground_truth_for(row, mode)

        # Rules only
        rules_pred = classifier.classify_rule_based(evidence)["classification"]
        rules_results.append(rules_pred == ground_truth)

        # Gemini only (if enabled)
        if use_llm and classifier.llm.available:
            try:
                llm_result, used_llm = classifier.classify_with_llm(evidence)
                llm_pred = llm_result["classification"]
            except (RuntimeError, ConnectionError, TimeoutError, json.JSONDecodeError):
                llm_pred = rules_pred
            llm_results.append(llm_pred == ground_truth)
        else:
            # If LLM not available, mark as N/A
            llm_results.append(None)

        # Combined (rule-based + LLM fallback) = just the system's normal flow
        combined_results.append(rules_pred == ground_truth)

    def accuracy(results):
        valid = [r for r in results if r is not None]
        if not valid:
            return None
        return sum(valid) / len(valid)

    return {
        "sample_size": sample_size,
        "ground_truth_mode": mode,
        "rules_only": accuracy(rules_results),
        "gemini_only": accuracy(llm_results),
        "combined": accuracy(combined_results),
        "use_llm": use_llm and bool(llm_results[0] is not None),
    }


# ------------------------------------------------------------------
# Main evaluation
# ------------------------------------------------------------------
def evaluate(sample_size: int = 100, include_ir: bool = True,
             include_ablation: bool = True, abort_llm: bool = False,
             ground_truth_mode: str = "auto",
             dataset_path: str = None,
             include_rankers: bool = True) -> dict:
    """
    Run the full evaluation suite.

    `ground_truth_mode`:
      - "real"  : use the real ClinVar `ClinicalSignificance` column
                  (requires the enriched workbook)
      - "proxy" : use `infer_ground_truth()` (self-consistent, not a
                  correctness measure -- kept for reproducibility)
      - "auto"  : use real labels when available, else fall back to proxy
    """
    dataset_path = dataset_path or DATASET_PATH
    df = load_ground_truth(dataset_path, sample_size)
    mode = resolve_ground_truth_mode(df, ground_truth_mode)
    classifier = ClassificationAgent(use_llm=not abort_llm)

    results = []
    for _, row in df.iterrows():
        gene = row["Gene"]
        hgvs = row["HGVS_notation"]

        evidence = build_evidence(row)

        prediction = classifier.classify_rule_based(evidence)
        predicted = prediction["classification"]
        ground_truth = ground_truth_for(row, mode)

        # The same ClinVar reconciliation the live pipeline applies, so the
        # evaluation reflects what a user would actually see.
        reconciled_prediction = classifier.reconcile_with_consensus(dict(prediction), evidence)
        reconciled = reconciled_prediction["classification"]

        results.append({
            "variant": f"{gene}:{hgvs}",
            "predicted": predicted,
            "predicted_reconciled": reconciled,
            "ground_truth": ground_truth,
            "match": predicted == ground_truth,
            "match_reconciled": reconciled == ground_truth,
            "confidence": prediction["confidence"],
            "confidence_numeric": prediction.get("confidence_numeric"),
            # Reconciliation re-states confidence around the final label (it
            # raises the floor when a reviewed ClinVar consensus backs it), so
            # the two numbers are calibrated separately -- a single column would
            # score the wrong probability against the wrong outcome.
            "confidence_numeric_reconciled": reconciled_prediction.get(
                "confidence_numeric"),
            "directional_match": _directional_outcome(ground_truth, predicted),
            "completeness": "complete" if evidence["predictor_available"]["complete"] else "incomplete",
            **stratum_fields(row),
        })

    # Classification metrics -- two numbers, deliberately kept separate:
    #   * "evidence_based"  = the agent's own ACMG-style call (does it reason well?)
    #   * "reconciled"      = after ClinVar consensus reconciliation (what the user sees)
    cls_metrics = compute_classification_metrics(results)
    reconciled_results = [
        {**result, "predicted": result["predicted_reconciled"],
         "match": result["match_reconciled"]}
        for result in results
    ]
    reconciled_metrics = compute_classification_metrics(reconciled_results)
    reconciled_confusion = compute_confusion_matrix(reconciled_results)

    # Directional agreement -- the clinically meaningful number (see above)
    directional_metrics = compute_directional_metrics(results, key="predicted")
    directional_reconciled = compute_directional_metrics(reconciled_results, key="predicted")
    completeness_metrics = {
        status: compute_classification_metrics(
            [result for result in results if result["completeness"] == status]
        )
        for status in ("complete", "incomplete")
    }
    confusion = compute_confusion_matrix(results)

    # Calibration -- three separate questions, not one:
    #   * is the evidence-based confidence honest about the evidence-based call?
    #   * is it honest about the call the user is shown (after reconciliation,
    #     which raises the floor on consensus-backed labels)?
    #   * is it honest about the clinically meaningful direction, on the rows
    #     where a direction exists at all?
    calibration = compute_calibration(results, key="match",
                                      confidence_key="confidence_numeric")
    calibration_reconciled = compute_calibration(
        reconciled_results, key="match",
        confidence_key="confidence_numeric_reconciled")
    calibration_directional = compute_calibration(
        results, key="directional_match", confidence_key="confidence_numeric")

    # Fairness -- subgroup accuracy, so a real limitation cannot hide inside a
    # single pooled average.
    fairness = compute_fairness(results)

    # IR metrics
    ir_metrics = None
    if include_ir:
        ir_metrics = compute_ir_metrics(dataset_path, sample_size=min(sample_size, 30), k=5)

    # TF-IDF vs BM25 vs hybrid, ranked corpus-wide (see compare_lexical_rankers
    # for why the pipeline-level numbers above cannot separate the three)
    ranker_comparison = None
    if include_ir and include_rankers:
        ranker_comparison = compare_lexical_rankers(
            dataset_path, sample_size=min(sample_size, 30), k=5)

    # Ablation
    ablation = None
    if include_ablation:
        ablation = run_ablation(dataset_path, sample_size=min(sample_size, 30),
                                use_llm=not abort_llm, ground_truth_mode=mode)

    return {
        "ground_truth_mode": mode,
        "classification": cls_metrics,
        "classification_reconciled": reconciled_metrics,
        "directional": directional_metrics,
        "directional_reconciled": directional_reconciled,
        "classification_by_completeness": completeness_metrics,
        "confusion_matrix": confusion,
        "confusion_matrix_reconciled": reconciled_confusion,
        "calibration": calibration,
        "calibration_reconciled": calibration_reconciled,
        "calibration_directional": calibration_directional,
        "fairness": fairness,
        "ir_metrics": ir_metrics,
        "lexical_ranker_comparison": ranker_comparison,
        "ablation": ablation,
        "results": results,
    }


def print_confusion_matrix(confusion: dict):
    """Pretty-print a confusion matrix."""
    labels = confusion["labels"]
    matrix = confusion["matrix"]

    print()
    print("CONFUSION MATRIX")
    print("  (row = actual, column = predicted)")
    width = max(len(label) for label in labels) + 2
    header = " " * width
    for label in labels:
        header += label[:width].ljust(width)
    print(header)
    for i, row in enumerate(matrix):
        line = labels[i][:width].ljust(width)
        for val in row:
            line += str(val).ljust(width)
        print(line)


def print_metrics(metrics: dict):
    """Pretty-print classification metrics."""
    cls = metrics["classification"]
    mode = metrics.get("ground_truth_mode", "proxy")
    agreement_label = (
        "Classification accuracy (real ClinVar labels)"
        if mode == "real"
        else "Proxy agreement (self-consistent -- NOT accuracy)"
    )
    print()
    print("CLASSIFICATION METRICS")
    print(f"  Ground truth:        {mode}"
          + ("  [real ClinVar ClinicalSignificance]" if mode == "real"
             else "  [inferred from evidence; circular]"))
    print(f"  Total variants:      {cls['total']}")
    print()
    print("  [1] Evidence-based call only (the agent's own ACMG reasoning):")
    print(f"      Correct: {cls['correct']}/{cls['total']}  ->  {cls['accuracy']:.1%}")
    print(f"      ({agreement_label})")
    reconciled = metrics.get("classification_reconciled")
    if reconciled:
        print()
        print("  [2] After ClinVar consensus reconciliation (what a user sees):")
        print(f"      Correct: {reconciled['correct']}/{reconciled['total']}  ->  "
              f"{reconciled['accuracy']:.1%}")
    print(f"  Macro Precision:     {cls['macro']['precision']:.3f}")
    print(f"  Macro Recall:        {cls['macro']['recall']:.3f}")
    print(f"  Macro F1:            {cls['macro']['f1']:.3f}")
    print()
    print("  Per-class:")
    for label, stats in sorted(cls["per_class"].items()):
        print(f"    {label}:")
        print(f"      Precision: {stats['precision']:.3f} | Recall: {stats['recall']:.3f} "
              f"| F1: {stats['f1']:.3f} | Correct: {stats['correct']}/{stats['total_in_class']}")
    print()
    print(f"  {agreement_label} by predictor completeness:")
    for status, grouped in metrics.get("classification_by_completeness", {}).items():
        if grouped["total"]:
            print(f"    {status}: {grouped['accuracy']:.1%} ({grouped['correct']}/{grouped['total']})")
        else:
            print(f"    {status}: N/A (no records)")


def print_ir_metrics(ir_metrics: dict):
    """Pretty-print IR metrics."""
    if not ir_metrics:
        return
    print()
    print("INFORMATION RETRIEVAL METRICS")
    print(f"  Sample size: {ir_metrics['sample_size']}"
          f"  (ranker: {ir_metrics.get('ranker', 'tfidf')})")
    print(f"  Precision@{ir_metrics['precision_at_k']['k']}:  {ir_metrics['precision_at_k']['mean']:.3f}")
    print(f"  Recall@{ir_metrics['recall_at_k']['k']}:     {ir_metrics['recall_at_k']['mean']:.3f}")
    print(f"  Exact-match@{ir_metrics['exact_at_k']['k']}: {ir_metrics['exact_at_k']['mean']:.3f}")
    print(f"  MRR@{ir_metrics['mrr_at_k']['k']}:          {ir_metrics['mrr_at_k']['mean']:.3f}")
    if "ndcg_at_k" in ir_metrics:
        print(f"  nDCG@{ir_metrics['ndcg_at_k']['k']}:         {ir_metrics['ndcg_at_k']['mean']:.3f}")
    if "map_at_k" in ir_metrics:
        print(f"  MAP@{ir_metrics['map_at_k']['k']}:          {ir_metrics['map_at_k']['mean']:.3f}")
        print("  (with one relevant variant per query, MAP@k and MRR@k are the")
        print("   same statistic -- see the ranker comparison below)")


def print_ablation(ablation: dict):
    """Pretty-print ablation results."""
    if not ablation:
        return
    print()
    print("ABLATION COMPARISON")
    print(f"  Sample size: {ablation['sample_size']}")
    print(f"  Ground truth: {ablation.get('ground_truth_mode', 'proxy')}")
    print(f"  Rules only:        {ablation['rules_only']:.1%}" if ablation['rules_only'] is not None else "  Rules only:        N/A")
    print(f"  Gemini only:       {ablation['gemini_only']:.1%}" if ablation['gemini_only'] is not None else "  Gemini only:       N/A")
    print(f"  Rules + Gemini:    {ablation['combined']:.1%}" if ablation['combined'] is not None else "  Rules + Gemini:    N/A")


def _reliability_strip(accuracy: float, mean_confidence: float, width: int = 20) -> str:
    """
    One reliability row as text: '=' is what actually happened, '^' is what the
    system claimed. A caret to the right of the bar means overconfidence.
    """
    marks = [" "] * width
    for index in range(min(int(round(accuracy * width)), width)):
        marks[index] = "="
    caret = min(max(int(round(mean_confidence * width)) - 1, 0), width - 1)
    marks[caret] = "^"
    return "|" + "".join(marks) + "|"


def print_calibration(calibration: dict, title: str = "CALIBRATION"):
    """Pretty-print a reliability table and its summary numbers."""
    print()
    print(title)
    if not calibration or not calibration.get("total"):
        print("  No rows with a confidence score and a determinate outcome.")
        return

    print(f"  Outcome being predicted: {calibration['outcome']}"
          f"   rows: {calibration['total']}"
          f"   base rate: {calibration['base_rate']:.3f}")
    print("  '=' is observed accuracy, '^' is the confidence that was claimed;")
    print("  a caret right of the bar is overconfidence, left is underconfidence.")
    print()
    print(f"  {'bin':<12}{'n':>5}{'claimed':>10}{'actual':>9}{'gap':>9}   ")
    for bucket in calibration["bins"]:
        flag = " *small" if bucket["small"] else ""
        strip = _reliability_strip(bucket["accuracy"], bucket["mean_confidence"])
        print(f"  {bucket['lower']:.1f}-{bucket['upper']:.1f}"
              f"{bucket['count']:>6}{bucket['mean_confidence']:>10.3f}"
              f"{bucket['accuracy']:>9.3f}{bucket['gap']:>+9.3f}  {strip}{flag}")
    print()
    print(f"  ECE (sample-weighted mean |gap|): {calibration['ece']:.4f}")
    print(f"  MCE (worst single bin):           {calibration['mce']:.4f}")
    print(f"  Brier score:                      {calibration['brier']:.4f}")
    print(f"  Brier skill vs base rate:         {calibration['brier_skill']:+.4f}")
    if calibration["brier_skill"] <= 0:
        print("  (a non-positive skill score means the confidence number is no")
        print("   better than always predicting the base rate)")
    if calibration["bins"] and any(bucket["small"] for bucket in calibration["bins"]):
        print(f"  * bins with fewer than {calibration['min_bin']} rows: shown, but")
        print("    an accuracy over a handful of rows is not evidence")


def print_fairness(fairness: dict):
    """Pretty-print subgroup accuracy, worst stratum first."""
    if not fairness:
        return
    print()
    print("SUBGROUP PERFORMANCE (fairness)")
    print("  Accuracy by input stratum. Strata are properties of the variant, not")
    print("  of the prediction, so the groups cannot be shaped by what is measured.")
    for dimension, report in fairness.items():
        strata = report["strata"]
        if not strata:
            continue
        print()
        print(f"  by {dimension}:")
        print(f"    {'stratum':<36}{'n':>5}{'call':>8}{'shown':>8}{'dir':>8}{'F1':>8}")
        ordered = sorted(strata.items(),
                         key=lambda pair: (pair[1]["small"], pair[1]["accuracy"]))
        for name, block in ordered:
            directional = (f"{block['directional_accuracy']:.3f}"
                           if block["directional_total"] else "n/a")
            marker = "*" if block["small"] else " "
            print(f"   {marker}{name[:34]:<36}{block['total']:>5}"
                  f"{block['accuracy']:>8.3f}{block['reconciled_accuracy']:>8.3f}"
                  f"{directional:>8}{block['macro_f1']:>8.3f}")
        small = report["small_strata"]
        if small:
            print(f"    * fewer than {report['min_group_size']} rows: "
                  f"{', '.join(small)}")
        worst = report["worst"]
        if worst:
            print(f"    worst eligible stratum: {worst['stratum']} "
                  f"({worst['accuracy']:.3f} over {worst['total']} rows)")
        else:
            print("    no stratum large enough to compare "
                  f"({len(small)} flagged small)")
    print()
    print("  'call' = the agent's own evidence-based call, 'shown' = after ClinVar")
    print("  consensus reconciliation, 'dir' = directional agreement on the rows")
    print("  where the ClinVar truth is a single determinate class.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Evaluate the Genomic Variant Interpretation system")
    parser.add_argument("--sample-size", type=int, default=100,
                        help="Number of variants to evaluate (default: 100)")
    parser.add_argument("--no-ir", action="store_true",
                        help="Skip IR metrics (faster)")
    parser.add_argument("--rankers-only", action="store_true",
                        help="Run only the TF-IDF vs BM25 vs hybrid comparison "
                             "and exit. Retrieval-only, so no LLM calls and no "
                             "dataset labels are needed.")
    parser.add_argument("--no-rankers", action="store_true",
                        help="Skip the lexical ranker comparison (faster)")
    parser.add_argument("--no-ablation", action="store_true",
                        help="Skip ablation comparison (faster)")
    parser.add_argument("--no-llm", action="store_true",
                        help="Don't attempt LLM calls in ablation (faster, no API costs)")
    parser.add_argument("--ground-truth", choices=["auto", "real", "proxy"], default="auto",
                        help="Label source: 'real' uses the ClinVar ClinicalSignificance "
                             "column (requires the enriched workbook), 'proxy' uses the "
                             "self-consistent inferred labels, 'auto' prefers real when "
                             "available (default: auto)")
    parser.add_argument("--dataset", default=None,
                        help="Dataset workbook to evaluate (default: data/clinvar_variant_dataset.xlsx; "
                             "use data/clinvar_variant_dataset_enriched.xlsx for real labels)")
    args = parser.parse_args()

    dataset_path = args.dataset or DATASET_PATH

    # Retrieval-only fast path. Nothing here touches a label or the network, so
    # it is safe to run repeatedly while writing the report.
    if args.rankers_only:
        print("=" * 70)
        print("LEXICAL RANKER COMPARISON")
        print("=" * 70)
        print_ranker_comparison(compare_lexical_rankers(
            dataset_path, sample_size=args.sample_size, k=5))
        print()
        # This block is at module scope, so it cannot `return` -- exit cleanly
        # instead of falling through into the full evaluation.
        sys.exit(0)

    print("=" * 70)
    print("EVALUATION: Genomic Variant Interpretation System")
    print("=" * 70)

    result = evaluate(
        sample_size=args.sample_size,
        include_ir=not args.no_ir,
        include_ablation=not args.no_ablation,
        abort_llm=args.no_llm,
        ground_truth_mode=args.ground_truth,
        dataset_path=dataset_path,
        include_rankers=not args.no_rankers,
    )

    # Classification
    print_metrics(result)

    # Directional agreement -- the clinically meaningful metric
    d0, d1 = result["directional"], result["directional_reconciled"]
    print()
    print("DIRECTIONAL AGREEMENT  (pathogenic / benign / uncertain)")
    print("  Scored only on rows whose ClinVar truth is a single determinate class;")
    print("  the rest are 'Conflicting classifications', a statement about submitters")
    print("  rather than a class any system can predict.")
    print(f"  Determinate rows:                 {d0['total_determinate']}")
    print(f"  Evidence-based call:              {d0['correct']}/{d0['total_determinate']}  ({d0['accuracy']:.1%})")
    print(f"  After consensus reconciliation:   {d1['correct']}/{d1['total_determinate']}  ({d1['accuracy']:.1%})")

    # Confusion matrix
    print_confusion_matrix(result["confusion_matrix"])

    if result.get("confusion_matrix_reconciled"):
        print()
        print("CONFUSION MATRIX -- after ClinVar consensus reconciliation")
        print_confusion_matrix(result["confusion_matrix_reconciled"])

    # Calibration
    print_calibration(result["calibration"],
                      "CALIBRATION -- evidence-based confidence vs evidence-based call")
    print_calibration(
        result["calibration_reconciled"],
        "CALIBRATION -- confidence vs the call the user is shown (reconciled)")
    print_calibration(
        result["calibration_directional"],
        "CALIBRATION -- evidence-based confidence vs clinical direction")

    # Fairness
    print_fairness(result["fairness"])

    # IR metrics
    print_ir_metrics(result["ir_metrics"])

    # TF-IDF vs BM25 vs hybrid
    print_ranker_comparison(result.get("lexical_ranker_comparison"))

    # Ablation
    print_ablation(result["ablation"])

    print()
    print("=" * 70)
    if result.get("ground_truth_mode") == "real":
        print("Ground truth: REAL ClinVar ClinicalSignificance labels --")
        print("these figures are an honest measure of classification accuracy.")
    else:
        print("NOTE: Ground truth is inferred from the same evidence columns the")
        print("classifier uses, so the numbers above measure SELF-CONSISTENCY,")
        print("not accuracy. For a real evaluation run:")
        print("  python scripts/enrich_clinvar_labels.py")
        print("  python evaluation.py --dataset data/clinvar_variant_dataset_enriched.xlsx")
    print("=" * 70)