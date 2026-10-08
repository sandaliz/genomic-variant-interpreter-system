"""
evaluation.py

Evaluation script for the Genomic Variant Interpretation system.

Provides a comprehensive evaluation with:

  1. **Classification metrics**:
     - Accuracy
     - Confusion matrix
     - Precision, Recall, F1-score (macro and per-class)
     - Per-class breakdown

  2. **Information Retrieval metrics**: (Precision@K, Recall@K)
     - Retrieval precision at top-K
     - Retrieval recall at top-K

  3. **Ablation comparison**:
     - Rules-only (deterministic)
     - Gemini-only (LLM)
     - Rules + Gemini (hybrid) vs ground truth

Warning: the LLM-based ablation calls the Gemini API for each variant
and may take a long time / incur API costs. Use --no-llm to skip it.

Run:
    python evaluation.py
    python evaluation.py --sample-size 50 --no-llm --no-ir
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
from agents.evidence_agent import EvidenceRetrievalAgent
from agents.intake_agent import IntakeAgent
from protocol.message import MessageLog
from protocol.security import InvalidVariantQuery


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
def compute_ir_metrics(dataset_path: str, sample_size: int = 20, k: int = 5) -> dict:
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
    """
    df = load_ground_truth(dataset_path, sample_size=sample_size)

    agent = EvidenceRetrievalAgent(dataset_path)
    intake = IntakeAgent()
    log = MessageLog()

    precision_at_k_list = []
    recall_at_k_list = []
    exact_at_k_list = []
    reciprocal_rank_list = []

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
                else:
                    precision_at_k_list.append(0.0)
                    recall_at_k_list.append(0.0)
                    exact_at_k_list.append(0.0)
                    reciprocal_rank_list.append(0.0)
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
            rank = next((idx for idx, r in enumerate(top_k, start=1) if r.get("exact_match")), None)
            reciprocal_rank_list.append(1.0 / rank if rank else 0.0)

        except (InvalidVariantQuery, KeyError, TypeError, AttributeError):
            continue

    return {
        "sample_size": len(exact_at_k_list),
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
    }


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
            from protocol.message import AgentMessage
            evidence_msg = AgentMessage(
                variant_id=f"{row['Gene']}:{row['HGVS_notation']}",
                stage="evidence_retrieved",
                sender="evaluation",
                payload=evidence,
            )
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
             dataset_path: str = None) -> dict:
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
    log = MessageLog()

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
            "completeness": "complete" if evidence["predictor_available"]["complete"] else "incomplete",
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

    # IR metrics
    ir_metrics = None
    if include_ir:
        ir_metrics = compute_ir_metrics(dataset_path, sample_size=min(sample_size, 30), k=5)

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
        "ir_metrics": ir_metrics,
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
    print(f"  Sample size: {ir_metrics['sample_size']}")
    print(f"  Precision@{ir_metrics['precision_at_k']['k']}:  {ir_metrics['precision_at_k']['mean']:.3f}")
    print(f"  Recall@{ir_metrics['recall_at_k']['k']}:     {ir_metrics['recall_at_k']['mean']:.3f}")
    print(f"  Exact-match@{ir_metrics['exact_at_k']['k']}: {ir_metrics['exact_at_k']['mean']:.3f}")
    print(f"  MRR@{ir_metrics['mrr_at_k']['k']}:          {ir_metrics['mrr_at_k']['mean']:.3f}")


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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Evaluate the Genomic Variant Interpretation system")
    parser.add_argument("--sample-size", type=int, default=100,
                        help="Number of variants to evaluate (default: 100)")
    parser.add_argument("--no-ir", action="store_true",
                        help="Skip IR metrics (faster)")
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

    print("=" * 70)
    print("EVALUATION: Genomic Variant Interpretation System")
    print("=" * 70)

    result = evaluate(
        sample_size=args.sample_size,
        include_ir=not args.no_ir,
        include_ablation=not args.no_ablation,
        abort_llm=args.no_llm,
        ground_truth_mode=args.ground_truth,
        dataset_path=args.dataset,
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

    # IR metrics
    print_ir_metrics(result["ir_metrics"])

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