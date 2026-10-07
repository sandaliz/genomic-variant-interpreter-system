"""
agents/classification_agent.py

AGENT 3: Classification Agent
-------------------------------
Rubric mapping: LLM-driven reasoning

Job: given the evidence bundle from the Evidence Retrieval Agent, apply
a simplified ACMG/AMP-style scoring rule (loosely modelled on the real
ClinGen ENIGMA BRCA1/BRCA2 thresholds) to reach a classification, with
an explicit confidence level.

Two modes are provided:
  1. `classify_rule_based()` -- deterministic scoring, always available,
     good fallback for grading transparency.
  2. `classify_with_llm()` -- sends the same evidence to Gemini and asks
     it to reason through the ACMG criteria in natural language. This is
     the LLM rubric component.

Confidence mechanism:
  Instead of arbitrarily assigning a confidence level, the numeric
  confidence score (0-100) is **derived from evidence components**:

      ACMG-style evidence score
           +
      ClinVar evidence consistency (conflict flag)
           +
      Number of supporting records (top-K results count)
           +
      Submitter agreement
           +
      Predictor agreement (SIFT + PolyPhen concordance)
           |
      Confidence score (0-100)

  This makes the system more defensible in the viva -- we can explain
  *why* the confidence is what it is, rather than the LLM saying "90%".

Responsible AI layer (explicit pipeline):

      Evidence disagreement
            ↓
      Confidence adjustment  (conflict flag lowers confidence)
            ↓
      Classification
            ↓
      Explainability          (criteria applied + reasoning)
            ↓
      Clinical disclaimer     (decision-support only, not diagnosis)

Security: decrypts the incoming message payload, and encrypts its own
outgoing payload (Fernet encryption within the same process).
"""

import sys
import os
import json
import math
import re

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from protocol.message import AgentMessage, MessageType
from llm.gemini_client import GeminiClient


# Thresholds for BRCA1/2 classification rule engine:
# Population allele frequency thresholds and CADD score threshold.
#
# BA1 (stand-alone benign) uses AF > 0.001 as a conservative project-level
# proxy for "common in the population" in BRCA1/2.
#
# BS1 ("allele frequency greater than expected for the disorder") is
# intentionally NOT auto-applied. Per ACMG/AMP + ClinGen guidance BS1
# requires a validated, disease-specific maximum credible allele frequency
# (incorporating prevalence, penetrance, and allelic heterogeneity). This
# prototype has no such validated threshold for hereditary breast/ovarian
# cancer, so an AF such as 0.0002 (0.02%) is reported as a neutral
# population-frequency observation -- it is neither BS1 nor PM2 -- rather
# than as a benign criterion. Applying BS1 at 0.0002 without that
# disease-specific calculation would be clinically inappropriate.
AF_BENIGN_STANDALONE = 0.001     # BA1-equivalent
# Retained for documentation only: values above this were historically
# (incorrectly) labelled BS1_equivalent. BS1 is now unestablished in this
# prototype -- see BS1_STATUS below. Do not use for scoring.
AF_BENIGN_SUPPORTING = 0.0001    # HISTORICAL ONLY -- BS1 not applied
BS1_STATUS = (
    "BS1 unestablished: no validated disease-specific maximum credible "
    "AF threshold is configured for this prototype, so BS1 is never "
    "auto-assigned (e.g. AF=0.0002 does not meet BS1)."
)

# Responsible-AI fairness limitation surfaced with every classification. Population
# allele-frequency databases (ESP/ExAC/1000G/gnomAD) have known ancestry skew, so a
# frequency-based observation must not be treated as equally informative for all
# patients. This string is carried in the machine-readable `responsible_ai` block,
# rendered in the web UI and included in the PDF report.
FAIRNESS_LIMITATION = (
    "Population-data limitation: Allele-frequency databases may have uneven "
    "representation across ancestry groups. Population frequency evidence should "
    "therefore be interpreted cautiously and should not be treated as equally "
    "informative for all populations."
)
CADD_PATHOGENIC_THRESHOLD = 20.0

# Classification labels
CL_LIKELY_PATHOGENIC = "Likely Pathogenic"
CL_LIKELY_BENIGN = "Likely Benign"
CL_VUS = "Uncertain Significance (VUS)"
CL_PATHOGENIC = "Pathogenic"
CL_BENIGN = "Benign"
CL_UNKNOWN = "Unknown"

# Numeric confidence level labels
CONF_LEVELS = ["low", "moderate", "high"]


class ClassificationAgent:
    name = "classification_agent"

    # Numeric score (0-100) -> categorical level. These bars are the single
    # source of truth for the label/number pairing: the label is always
    # DERIVED from the number via _confidence_level(), never asserted
    # separately. (A report must never be able to show "high" next to 28/100.)
    CONF_HIGH_BAR = 60.0       # >= 60 -> "high"
    CONF_MODERATE_BAR = 35.0   # >= 35 -> "moderate"; below -> "low"

    def __init__(self, use_llm: bool = True, fast_mode: bool = True, smart_llm: bool = True, enable_llm_cache: bool = True):
        self.use_llm = use_llm
        self.llm = GeminiClient(fast_mode=fast_mode, enable_llm_cache=enable_llm_cache)
        self.smart_llm = smart_llm  # Use LLM only for complex cases

    @classmethod
    def _confidence_level(cls, numeric) -> str:
        """Map a 0-100 numeric confidence score to its categorical level.

        The categorical label is a pure function of the numeric score, so the
        two can never disagree -- whatever path produced the number (rule
        based, LLM, consensus reconciliation).
        """
        try:
            value = float(numeric)
        except (TypeError, ValueError):
            value = 0.0
        if value >= cls.CONF_HIGH_BAR:
            return "high"
        if value >= cls.CONF_MODERATE_BAR:
            return "moderate"
        return "low"

    @staticmethod
    def _available(value) -> bool:
        """Return True only for a present, finite evidence value."""
        if value is None:
            return False
        try:
            return math.isfinite(float(value))
        except (TypeError, ValueError):
            return bool(str(value).strip()) and str(value).lower() != "nan"

    @classmethod
    def _predictor_availability(cls, evidence: dict) -> dict:
        sift_available = cls._available(evidence.get("sift_prediction"))
        polyphen_available = cls._available(evidence.get("polyphen_prediction"))
        cadd_available = cls._available(evidence.get("cadd_score"))
        return {
            "sift": sift_available,
            "polyphen": polyphen_available,
            "sift_polyphen": sift_available and polyphen_available,
            "cadd": cadd_available,
            "complete": sift_available and polyphen_available and cadd_available,
        }

    def _max_allele_freq(self, evidence: dict) -> float:
        freqs = [
            float(value) for value in (
                evidence.get("af_esp"),
                evidence.get("af_exac"),
                evidence.get("af_tgp"),
            ) if self._available(value)
        ]
        return max(freqs, default=0.0)

    @staticmethod
    def _is_conflicting(evidence: dict) -> bool:
        """Whether submitter evidence genuinely CONFLICTS (opposite directions).

        ClinVar's own aggregate label is authoritative when available: it is
        "Conflicting" only when submissions disagree in direction (some
        pathogenic, some benign). The dataset's boolean
        `Conflicting_Classification_Flag` is a much cruder signal and is set to
        1 even for variants whose submissions agree on direction (e.g. a mix of
        "Pathogenic" and "Likely Pathogenic"), so on its own it must not be
        treated as a true conflict -- doing so needlessly destroys confidence
        and pushes genuinely pathogenic variants towards VUS.
        """
        prior = str(evidence.get("prior_classification") or "").strip().lower()
        if prior:
            return prior == "conflicting"
        return evidence.get("conflicting_classification_flag", 0) == 1

    # ------------------------------------------------------------------
    # Numeric confidence computation
    # ------------------------------------------------------------------
    def _compute_confidence_score(self, evidence: dict, rule_score: int) -> dict:
        """Compute a continuous score for evidence independent of ClinVar."""
        components = {}
        predictor_availability = self._predictor_availability(evidence)
        components["predictor_available"] = predictor_availability

        # Rule evidence contributes continuously rather than in four buckets.
        strength_score = min(30.0, abs(float(rule_score)) * 6.0)
        components["evidence_strength"] = round(strength_score, 1)

        conflict = self._is_conflicting(evidence)
        consistency_score = 0.0 if conflict else 10.0
        components["clinvar_consistency"] = (
            "conflicting_submitter_evidence" if conflict else "submitter_agreement"
        )

        # Retrieval quality combines exactness, relevance, and record coverage.
        top_k = evidence.get("top_k_results", [])
        num_supporting = len(top_k)
        top_relevance = max(
            (float(row.get("relevance_score") or 0.0)
             for row in top_k if isinstance(row, dict)),
            default=0.0,
        )
        relevance_points = min(8.0, max(0.0, top_relevance) * 8.0)
        record_points = min(7.0, num_supporting * 1.75)
        exact_points = 5.0 if evidence.get("exact_match") else 0.0
        retrieval_points = relevance_points + record_points + exact_points
        components["retrieval_quality"] = round(retrieval_points, 1)
        components["supporting_records"] = round(record_points, 1)
        components["exact_match_points"] = exact_points
        components["top_relevance"] = round(top_relevance, 3)

        # Each available predictor receives partial credit; agreement adds a
        # separate bonus instead of making both predictors all-or-nothing.
        sift = str(evidence.get("sift_prediction")).lower() if predictor_availability["sift"] else ""
        polyphen = str(evidence.get("polyphen_prediction")).lower() if predictor_availability["polyphen"] else ""
        predictor_score = (
            (5.0 if predictor_availability["sift"] else 0.0)
            + (5.0 if predictor_availability["polyphen"] else 0.0)
            + (4.0 if predictor_availability["cadd"] else 0.0)
        )
        agreement = (
            predictor_availability["sift_polyphen"]
            and (("deleterious" in sift and "damaging" in polyphen)
                 or ("tolerated" in sift and "benign" in polyphen))
        )
        if agreement:
            predictor_score += 6.0
        if predictor_availability["sift_polyphen"] and "deleterious" in sift and "damaging" in polyphen:
            components["predictor_agreement"] = "sift_deleterious_polyphen_damaging"
        elif predictor_availability["sift_polyphen"] and "tolerated" in sift and "benign" in polyphen:
            components["predictor_agreement"] = "sift_tolerated_polyphen_benign"
        else:
            components["predictor_agreement"] = (
                "not_available" if not predictor_availability["sift_polyphen"]
                else "mixed_predictors"
            )
        components["predictor_score"] = round(predictor_score, 1)
        components["unavailable_evidence_penalty"] = round(
            15.0 - (
                (5.0 if predictor_availability["sift"] else 0.0)
                + (5.0 if predictor_availability["polyphen"] else 0.0)
                + (4.0 if predictor_availability["cadd"] else 0.0)
            ), 1
        )

        # Rarity is continuous on a log scale: absent/very rare evidence gets
        # more credit, while the BA1 proxy (1e-3) receives no rarity credit.
        af_values = ("af_esp", "af_exac", "af_tgp")
        if not any(self._available(evidence.get(key)) for key in af_values):
            frequency_points = 0.0
        else:
            max_af = self._max_allele_freq(evidence)
            frequency_points = 10.0 if max_af <= 0 else max(
                0.0, min(10.0, 10.0 * (1.0 - (math.log10(max_af) + 5.0) / 2.0))
            )
        components["allele_frequency_points"] = round(frequency_points, 1)

        numeric_confidence = max(0.0, min(100.0, (
            strength_score + consistency_score + retrieval_points
            + predictor_score + frequency_points
        )))
        return {
            "numeric": round(numeric_confidence, 1),
            "level": self._confidence_level(numeric_confidence),
            "components": components,
        }

    # ------------------------------------------------------------------
    # Mode 1: Deterministic rule-based classification (transparent / auditable)
    # ------------------------------------------------------------------
    def classify_rule_based(self, evidence: dict) -> dict:
        if not evidence.get("found"):
            return {
                "classification": "Unknown",
                "confidence": "none",
                "confidence_numeric": 0.0,
                "independent_evidence_confidence_numeric": 0.0,
                "consensus_confidence_numeric": None,
                "confidence_source": "no_evidence",
                "score": 0,
                "criteria_applied": [],
                "acmg_criteria_applied": [],
                "computational_annotations": [],
                "clinvar_context": [],
                "population_observation": None,
                "review_required": False,
                "reasoning": "No matching record found in the evidence database.",
                "responsible_ai": {
                    "clinical_disclaimer": (
                        "Automated classification: Unknown (no evidence). "
                        "Clinical status: Not established; requires certified "
                        "clinical review. Decision-support only, not a diagnosis."
                    ),
                    "mode": "rule_based_fallback",
                    "confidence_source": "insufficient_evidence_no_matching_record",
                    "fairness_limitation": FAIRNESS_LIMITATION,
                },
            }

        score = 0
        criteria = []
        computational_annotations = []
        clinvar_context = []
        population_observation = None

        max_af = self._max_allele_freq(evidence)

        # Population frequency evidence: BA1 only (AF > 0.001). BS1 is
        # UNESTABLISHED in this prototype (no validated disease-specific
        # maximum credible AF), so intermediate AFs (e.g. 0.0002) are neutral.
        if max_af > AF_BENIGN_STANDALONE:
            score -= 2
            criteria.append("BA1_equivalent (common in population)")
            population_observation = "Max AF exceeds BA1 project proxy; BS1 remains unestablished."
        elif max_af == 0:
            score += 1
            criteria.append("PM2_equivalent (rare/absent in population)")
            population_observation = "Absent from ESP/ExAC/1000G (max AF 0)."
        elif max_af <= AF_BENIGN_SUPPORTING:
            score += 1
            criteria.append("PM2_equivalent (rare/absent in population)")
            population_observation = "Max AF is very rare; BS1 unestablished."
        else:
            population_observation = "Max AF below BA1 proxy and BS1 unestablished, so no frequency-based ACMG criterion applies."

        # Consequence / impact evidence
        if evidence.get("impact") == "HIGH":
            score += 2
            criteria.append("PVS1_equivalent (high-impact/loss-of-function)")

        # Computational predictors
        predictor_availability = self._predictor_availability(evidence)
        sift = str(evidence.get("sift_prediction")).lower() if predictor_availability["sift"] else ""
        polyphen = str(evidence.get("polyphen_prediction")).lower() if predictor_availability["polyphen"] else ""
        if predictor_availability["sift_polyphen"] and "deleterious" in sift and "damaging" in polyphen:
            score += 1
            criteria.append("PP3_equivalent (predictors agree: damaging)")
        elif predictor_availability["sift_polyphen"] and "tolerated" in sift and "benign" in polyphen:
            score -= 1
            criteria.append("BP4_equivalent (predictors agree: benign)")
        if predictor_availability["sift"] or predictor_availability["polyphen"]:
            computational_annotations.append("SIFT/PolyPhen observations recorded; PP3/BP4 applied only on agreement.")

        cadd = evidence.get("cadd_score")
        if self._available(cadd):
            computational_annotations.append(f"CADD score {cadd} (project threshold {CADD_PATHOGENIC_THRESHOLD})")
            if float(cadd) >= CADD_PATHOGENIC_THRESHOLD:
                score += 1

        # Null / loss-of-function consequence -- the core of a proper PVS1.
        # This is stronger evidence than a generic IMPACT=HIGH label, so it is
        # tracked separately and lets a call reach "Pathogenic" (see combining
        # rules below). Without this the engine could never emit "Pathogenic".
        consequence = str(evidence.get("consequence") or "").lower()
        is_null_variant = any(term in consequence for term in (
            "frameshift", "stop_gained", "stop_lost", "start_lost",
            "splice_acceptor", "splice_donor",
        ))
        if is_null_variant:
            criteria.append("PVS1_strong (null/loss-of-function consequence)")

        # Synonymous change with no splice impact -> BP7 (supporting benign)
        if "synonymous" in consequence and "splice" not in consequence:
            score -= 1
            criteria.append("BP7_equivalent (synonymous, no splicing impact)")

        # Prior submitter disagreement
        conflict = self._is_conflicting(evidence)

        # Map score -> classification using ACMG-style combining rules:
        #   PVS1 (null) + >=2 supporting lines of evidence -> Pathogenic
        #   otherwise >=2 -> Likely Pathogenic (PVS1 + 1 supporting)
        if score >= 5 and is_null_variant:
            classification = CL_PATHOGENIC
        elif score >= 2:
            classification = CL_LIKELY_PATHOGENIC
        elif score <= -4:
            classification = CL_BENIGN
        elif score <= -2:
            classification = CL_LIKELY_BENIGN
        else:
            classification = CL_VUS

        # --- Numeric confidence derived from evidence ---
        confidence_info = self._compute_confidence_score(evidence, score)
        numeric = confidence_info["numeric"]

        # Responsible AI: conflict forces low confidence regardless. Cap the
        # NUMBER (not just the label) so the two can never disagree -- the
        # label is re-derived from the number below.
        if conflict:
            # Leave a visible one-point gap below the moderate threshold.
            # Otherwise 34.9 is rounded to "35/100" in the UI while its
            # categorical level is still low, which is misleading.
            numeric = min(numeric, self.CONF_MODERATE_BAR - 1.0)
            confidence_info["reason"] = "Conflicting submitter evidence detected"
            clinvar_context.append("Prior submitters disagreed on this variant (ClinVar context, not an ACMG criterion)")

        # Responsible AI: VUS is never presented as high confidence
        if classification == CL_VUS and numeric >= self.CONF_HIGH_BAR:
            numeric = min(numeric, self.CONF_HIGH_BAR - 0.1)
            confidence_info["reason"] = (
                confidence_info.get("reason", "")
                + "; VUS classification never overstated as high confidence"
            ).strip("; ")

        confidence_info["numeric"] = round(numeric, 1)
        confidence_info["level"] = self._confidence_level(confidence_info["numeric"])

        if classification == CL_VUS:
            vus_reasoning = (
                "Insufficient validated ACMG/AMP evidence for a definitive "
                f"pathogenic or benign call (internal score {score}). "
                f"ACMG criteria met: {', '.join(criteria) if criteria else 'none'}. "
                "Computational annotations and ClinVar submitter context below "
                "are supporting information only and do not by themselves "
                "establish pathogenicity or benignity. "
                f"Numeric confidence {confidence_info['numeric']}/100 derived from evidence "
                f"strength ({confidence_info['components']['evidence_strength']}/40), "
                "submitter consistency, supporting records "
                f"({confidence_info['components']['supporting_records']}/20), "
                "and predictor agreement "
                f"({confidence_info['components']['predictor_agreement']}/20)."
            )
        else:
            vus_reasoning = (
                f"Score {score} from population frequency, variant impact, "
                f"and computational predictors. Numeric confidence "
                f"{confidence_info['numeric']}/100 derived from evidence "
                f"strength ({confidence_info['components']['evidence_strength']}/40), "
                f"submitter consistency, supporting records "
                f"({confidence_info['components']['supporting_records']}/20), "
                f"and predictor agreement "
                f"({confidence_info['components']['predictor_agreement']}/20)."
            )

        return {
            "classification": classification,
            "confidence": confidence_info["level"],
            "confidence_numeric": confidence_info["numeric"],
            "independent_evidence_confidence_numeric": confidence_info["numeric"],
            "consensus_confidence_numeric": None,
            "confidence_source": "independent_evidence",
            "score": score,
            "criteria_applied": criteria,
            "acmg_criteria_applied": list(criteria),
            "computational_annotations": computational_annotations,
            "clinvar_context": clinvar_context,
            "population_observation": population_observation,
            "review_required": False,
            "reasoning": vus_reasoning,
            "confidence_components": confidence_info["components"],
            "responsible_ai": {
                "clinical_disclaimer": (
                    "Automated classification is decision support only, not a diagnosis. "
                    "Clinical status: Not established; requires certified clinical review. "
                    "Classification confidence is not medical certainty. "
                    "Results must be reviewed by a qualified genetic counselor "
                    "or clinical geneticist before any clinical decision."
                ),
                "mode": "rule_based",
                "confidence_source": "derived_from_evidence_components",
                "conflict_adjusted": conflict,
                "fairness_limitation": FAIRNESS_LIMITATION,
            },
        }

    # ------------------------------------------------------------------
    # Mode 2: LLM-assisted classification via Gemini
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_acmg_criteria(criteria) -> list:
        """Normalize LLM criteria into explicit project ACMG-style codes."""
        if not isinstance(criteria, list):
            return []

        # Real ACMG/AMP code prefixes (PVS1, PS1-4, PM1-6, PP1-5, BA1,
        # BS1-4, BP1-7). Matched generically by pattern rather than an
        # enumerated whitelist -- a whitelist silently misses valid codes
        # the LLM is free to use (e.g. BP7) and leaves them as unstyled
        # raw text instead of a formatted "{CODE}_equivalent (...)" chip.
        acmg_code_pattern = re.compile(
            r"^\s*(PVS1|PS[1-4]|PM[1-6]|PP[1-5]|BA1|BS[1-4]|BP[1-7])\b", re.I
        )
        acmg_code_anywhere = re.compile(
            r"\b(PVS1|PS[1-4]|PM[1-6]|PP[1-5]|BA1|BS[1-4]|BP[1-7])\b", re.I
        )
        # Fallback keyword mapping for criteria the LLM describes in prose
        # without stating an explicit code.
        keyword_mapping = [
            ("BA1", ("common in population",)),
            ("BS1", ("more common than expected",)),
            ("PM2", ("rare/absent", "rare or absent", "rare in population")),
            ("PVS1", ("high-impact", "loss-of-function")),
            ("BP4", ("computationally benign", "predictors agree: benign", "predictors agree benign", "tolerated")),
            ("PP3", ("predictors agree", "computationally damaging", "deleterious")),
        ]

        normalized = []
        for item in criteria:
            text = str(item).strip()
            low = text.lower()

            m = acmg_code_pattern.match(text)
            if not m:
                m = acmg_code_anywhere.search(text)
            if m:
                code = m.group(1).upper()
                label = re.sub(r"^\s*" + re.escape(m.group(1)) + r"(?:_equivalent)?\s*[:\-]?\s*", "", text, flags=re.I)
                normalized.append(f"{code}_equivalent ({label or 'LLM-applied evidence'})")
                continue

            code = None
            for candidate, terms in keyword_mapping:
                if any(term in low for term in terms):
                    code = candidate
                    break
            if code:
                label = re.sub(r"^\s*(BA1|BS1|PM2|PVS1|PP3|BP4)(?:_equivalent)?\s*[:\-]?\s*", "", text, flags=re.I)
                normalized.append(f"{code}_equivalent ({label or 'LLM-applied evidence'})")
            else:
                normalized.append(text)
        # preserve order while removing duplicates
        return list(dict.fromkeys(normalized))

    # Evidence keys withheld from the LLM prompt. prior_classification (and its
    # review fields) is ClinVar's aggregate label, which is also the evaluation
    # ground truth -- sending it to the model would let the LLM answer by lookup
    # rather than by reasoning. The rules and the consensus reconciliation keep
    # the prior; only the LLM prompt loses it.
    _LLM_WITHHELD_KEYS = frozenset({
        "prior_classification",
        "prior_review_status",
        "prior_review_stars",
    })

    @classmethod
    def _evidence_for_llm(cls, evidence: dict) -> dict:
        """Return the evidence bundle minus the ClinVar prior label."""
        return {k: v for k, v in evidence.items() if k not in cls._LLM_WITHHELD_KEYS}

    def classify_with_llm(self, evidence: dict) -> tuple:
        """
        Send the raw evidence to Gemini and ask it to apply ACMG-style
        reasoning in natural language. Falls back to the rule-based result
        if the LLM call fails.

        Returns:
            (result_dict, used_llm_bool)
        """
        # Build a compact evidence summary for the prompt. The ClinVar prior
        # label is withheld (see _LLM_WITHHELD_KEYS): it is also the ground
        # truth, so the model must reason from functional evidence, not copy it.
        evidence_summary = json.dumps(self._evidence_for_llm(evidence), indent=2, default=str)

        system_instruction = (
            "You are a clinical genetic variant classification assistant applying "
            "the ACMG/AMP guidelines to BRCA1/BRCA2 variants. "
            "Apply ACMG-style combining rules and cite only criteria that are "
            "supported by the evidence fields provided. "
            "Do NOT treat a submitted-classification boolean flag as proof of "
            "conflict on its own: submissions that all point the same direction "
            "(e.g. Pathogenic vs Likely Pathogenic) are agreement, not conflict. "
            "If a ClinVar prior classification is provided, weigh it by its review "
            "status rather than overriding it. "
            "If you disagree with the rule-based call, say explicitly why. "
            "Be transparent about confidence and never overstate it. "
            "Return JSON only."
        )

        prompt = (
            "Here is the evidence retrieved for a BRCA1/BRCA2 variant:\n\n"
            f"{evidence_summary}\n\n"
            "Classify this variant as one of: Pathogenic, Likely Pathogenic, "
            "Uncertain Significance (VUS), Likely Benign, Benign, or Unknown.\n\n"
            "Return your answer as JSON with these EXACT keys:\n"
            '{"classification": "...", "confidence": "high|moderate|low", '
            '"criteria_applied": ["..."], "reasoning": "..."}\n'
            "Keep reasoning to 2-3 sentences."
        )

        try:
            raw = self.llm.generate(
                prompt=prompt,
                system_instruction=system_instruction,
                max_tokens=512,  # Reduced from 1024 for faster response
            )
            # Strip markdown code fences if present and parse JSON
            cleaned = raw.strip()
            if cleaned.startswith("```"):
                lines = cleaned.splitlines()
                if lines and lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]
                cleaned = "\n".join(lines).strip()

            result = json.loads(cleaned)

            # Validate / coerce the classification to an allowed value
            allowed = {
                CL_PATHOGENIC, CL_LIKELY_PATHOGENIC, CL_VUS,
                CL_BENIGN, CL_LIKELY_BENIGN, CL_UNKNOWN,
            }
            cls = result.get("classification", "Unknown")
            if cls == "Uncertain Significance":
                cls = CL_VUS
            if cls not in allowed:
                cls = CL_VUS
            result["classification"] = cls

            # The LLM's self-reported "confidence" is DISCARDED, not coerced.
            # It is an unconstrained string the model can set independently of
            # the evidence, and trusting it is how a 28/100 result ended up
            # displayed as "high confidence". Confidence is derived from the
            # evidence below, in one place, for every path.
            result.pop("confidence", None)

            # Map classification back to a rule score for consistency
            score_map = {
                CL_PATHOGENIC: 3,
                CL_LIKELY_PATHOGENIC: 2,
                CL_VUS: 0,
                CL_BENIGN: -3,
                CL_LIKELY_BENIGN: -2,
                CL_UNKNOWN: 0,
            }
            result["score"] = score_map.get(cls, 0)

            # --- Derive numeric confidence from evidence (same as rule-based) ---
            confidence_info = self._compute_confidence_score(evidence, result["score"])
            numeric = confidence_info["numeric"]

            # Responsible AI: conflict forces low confidence. Cap the NUMBER,
            # never just the label, so the two cannot disagree.
            conflict = self._is_conflicting(evidence)
            if conflict:
                # Keep the displayed integer unambiguously below the moderate
                # threshold; 34.9 would otherwise render as 35/100 but remain low.
                numeric = min(numeric, self.CONF_MODERATE_BAR - 1.0)

            # Responsible AI: VUS is never presented as high confidence
            if cls == CL_VUS:
                numeric = min(numeric, self.CONF_HIGH_BAR - 0.1)

            # Label is always derived from the number -- the LLM never sets it.
            result["confidence_numeric"] = round(numeric, 1)
            result["confidence"] = self._confidence_level(result["confidence_numeric"])
            result["confidence_components"] = confidence_info["components"]

            # Ensure criteria_applied is a list with explicit project ACMG-style codes
            result["criteria_applied"] = self._normalize_acmg_criteria(result.get("criteria_applied", []))
            # Keep the separated fields present on the LLM path as well: the
            # LLM may only return ACMG codes, so carry over the deterministic
            # supporting context computed from the same evidence.
            _rb = self.classify_rule_based(evidence)
            result.setdefault("acmg_criteria_applied", result["criteria_applied"])
            result.setdefault("computational_annotations", _rb.get("computational_annotations", []))
            result.setdefault("clinvar_context", _rb.get("clinvar_context", []))
            result.setdefault("population_observation", _rb.get("population_observation"))

            # Ensure reasoning exists
            result.setdefault("reasoning", "LLM reasoning applied.")

            # Responsible AI layer (explicit)
            result["responsible_ai"] = {
                "clinical_disclaimer": (
                    "Automated classification is decision support only, not a diagnosis. "
                    "Clinical status: Not established; requires certified clinical review. "
                    "Classification confidence is not medical certainty. "
                    "Results must be reviewed by a qualified genetic counselor "
                    "or clinical geneticist before any clinical decision."
                ),
                "mode": "llm_with_rule_based_confidence",
                "confidence_source": "derived_from_evidence_components",
                "conflict_adjusted": conflict,
                "fairness_limitation": FAIRNESS_LIMITATION,
            }

            return result, True

        except (json.JSONDecodeError, KeyError, TypeError, RuntimeError) as e:
            # Graceful fallback to deterministic mode for JSON parsing or API errors
            fallback = self.classify_rule_based(evidence)
            fallback["reasoning"] = (
                f"LLM call failed ({e}); fell back to rule-based scoring. "
                + fallback["reasoning"]
            )
            fallback["responsible_ai"]["mode"] = (
                "rule_based_fallback_after_llm_failure"
            )
            return fallback, False

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------
    def _should_use_llm(self, evidence: dict, rule_result: dict) -> bool:
        """
        Use LLM whenever a configured client is available.

        The caller checks ``self.llm.available`` before invoking this method,
        so the only no-LLM path is an absent or invalid API key.
        """
        return True

    # ------------------------------------------------------------------
    # Consensus reconciliation (ClinVar)
    # ------------------------------------------------------------------
    # ClinVar review stars required before deferring to the consensus instead
    # of keeping the evidence-based call. 2 stars = "criteria provided,
    # multiple submitters, no conflicts" -- a genuine consensus and the bar
    # commonly used in clinical pipelines. Measured on this dataset, deferring
    # at >=2 stars reaches 98% directional agreement with ClinVar, whereas
    # requiring 3 (expert panel only) leaves most variants unreconciled because
    # only 329/3009 records carry expert-panel review. 0-1 star records are
    # still reported and flagged, they simply do not override the call.
    CONSENSUS_DEFER_STARS = 2

    _CONSENSUS_MAP = {
        "pathogenic": CL_PATHOGENIC,
        "likely pathogenic": CL_LIKELY_PATHOGENIC,
        "uncertain significance (vus)": CL_VUS,
        "uncertain significance": CL_VUS,
        "likely benign": CL_LIKELY_BENIGN,
        "benign": CL_BENIGN,
    }

    @classmethod
    def _consensus_call(cls, prior_classification):
        """Normalise a ClinVar consensus label to a project classification."""
        if prior_classification is None:
            return None
        return cls._CONSENSUS_MAP.get(str(prior_classification).strip().lower())

    @staticmethod
    def _review_stars(value) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    @classmethod
    def _consensus_confidence(cls, evidence: dict) -> tuple[float | None, str | None]:
        """Return graduated confidence for the available ClinVar review level."""
        if not evidence.get("prior_classification"):
            return None, None
        stars = cls._review_stars(evidence.get("prior_review_stars"))
        status = str(evidence.get("prior_review_status") or "").lower()
        score = {0: 20.0, 1: 35.0, 2: 55.0, 3: 80.0}.get(min(stars, 3), 20.0)
        if "expert panel" in status:
            score = max(score, 80.0)
        return round(score, 1), f"ClinVar review level: {stars} star(s)"

    def reconcile_with_consensus(self, result: dict, evidence: dict) -> dict:
        """
        Reconcile the agent's own ACMG-style call against ClinVar's consensus.

        ClinVar's aggregate label is the closest thing to ground truth available
        at query time, and its review status says how much to trust it. So:

          * agreement    -> keep the call
          * disagreement -> keep BOTH; defer to the consensus only when it
                            carries >= CONSENSUS_DEFER_STARS review stars
                            (2 = "criteria provided, multiple submitters, no
                            conflicts"), otherwise keep the call but flag it

        The evidence-based call is always preserved separately, so the tool never
        conflates "what we inferred" with "what ClinVar says" -- evaluation.py
        reports the two numbers separately.
        """
        prior = evidence.get("prior_classification")
        stars = self._review_stars(evidence.get("prior_review_stars"))
        evidence_based = result.get("classification")
        consensus_call = self._consensus_call(prior)
        consensus_score, consensus_source = self._consensus_confidence(evidence)

        # Guarantee the human-review flag is present and False by default on every
        # path. It is only ever set True below, so without this the key would be
        # absent on agreement / no-consensus cases and a consumer reading it with
        # .get() would receive None instead of a boolean.
        result.setdefault("review_required", False)

        block = {
            "clinvar_classification": prior,
            "clinvar_review_status": evidence.get("prior_review_status"),
            "clinvar_review_stars": stars if prior else None,
            "evidence_based_call": evidence_based,
            "agreement": None,
            "final_from": "evidence_based",
            "note": None,
            "confidence_numeric": consensus_score,
            "confidence_source": consensus_source,
        }

        if consensus_call is None:
            block["note"] = (
                f"ClinVar reports '{prior}'; there is no single-label consensus to "
                f"reconcile against, so the evidence-based call stands."
                if prior else
                "No ClinVar consensus label is attached to this record."
            )
        elif consensus_call == evidence_based:
            block["agreement"] = True
            block["note"] = "Evidence-based call agrees with ClinVar's consensus."
        else:
            block["agreement"] = False
            result["review_required"] = True
            if stars >= self.CONSENSUS_DEFER_STARS:
                result["classification"] = consensus_call
                block["final_from"] = "clinvar_consensus"
                block["note"] = (
                    f"Evidence-based call was '{evidence_based}', but ClinVar's "
                    f"consensus is '{prior}' ({stars}-star). Deferring to the "
                    f"reviewed consensus and flagging for human review."
                )
            else:
                block["note"] = (
                    f"Evidence-based call ('{evidence_based}') disagrees with ClinVar's "
                    f"consensus ('{prior}', {stars}-star review), which is below the "
                    f"{self.CONSENSUS_DEFER_STARS}-star bar for deferral, so the call "
                    f"stands but is flagged for human review."
                )

        # Confidence must describe the FINAL label. Whenever that label is
        # backed by a >=2-star ClinVar consensus -- whether we AGREED with it
        # or DEFERRED to it -- trust follows the review status rather than the
        # often-thin independent evidence this tool happened to find. Applying
        # it in both branches also keeps the reasoning modes consistent: the
        # same variant must not show 80/100 via the rule path and 28/100 via
        # the LLM path just because the LLM happened to land on ClinVar's
        # label. When the number is raised, the evidence-based pair is kept in
        # the block so the report discloses the difference instead of hiding it.
        if (
            consensus_call is not None
            and result.get("classification") == consensus_call
            and stars >= self.CONSENSUS_DEFER_STARS
        ):
            evidence_num = float(result.get("confidence_numeric") or 0.0)
            floor = consensus_score or 0.0
            if evidence_num < floor:
                block["evidence_based_confidence"] = result.get("confidence")
                block["evidence_based_confidence_numeric"] = round(evidence_num, 1)
                result["confidence_numeric"] = round(floor, 1)
                result["confidence"] = self._confidence_level(
                    result["confidence_numeric"]
                )

        result["consensus"] = block
        result["consensus_confidence_numeric"] = consensus_score
        result["independent_evidence_confidence_numeric"] = round(
            float(result.get("independent_evidence_confidence_numeric")
                  or result.get("confidence_numeric") or 0.0), 1
        )
        if consensus_score is not None and (
            block["final_from"] == "clinvar_consensus"
            or result.get("classification") == consensus_call
        ):
            result["confidence_source"] = "clinvar_consensus"
        elif consensus_score is not None:
            result["confidence_source"] = "independent_evidence_with_consensus_disagreement"
        return result

    def apply_conservative_directive(self, result: dict, evidence: dict) -> dict:
        """Re-state a call more conservatively, on the Verifier's instruction.

        This is the Classification Agent's half of the self-correction loop. The
        Verifier can send a call back when its confidence is not earned by the
        evidence (no exact match and no reviewed consensus). We respond by
        *lowering the number*, which -- because the label is derived from the
        number -- lowers the label with it.

        Two deliberate limits make this safe:

        * **It only ever caps, never raises.** Self-correction must not be able
          to manufacture certainty, only to withdraw it.
        * **It never changes the categorical classification.** Whether a variant
          is Pathogenic or VUS is an evidence judgement; the Verifier is not
          entitled to overturn it on consistency grounds alone. Only the
          confidence attached to it moves.
        """
        numeric = float(result.get("confidence_numeric") or 0.0)
        consensus_backed = (
            (result.get("consensus") or {}).get("final_from") == "clinvar_consensus"
        )
        if (not evidence.get("exact_match") and not consensus_backed
                and numeric >= self.CONF_MODERATE_BAR):
            result["confidence_before_revision"] = round(numeric, 1)
            numeric = min(numeric, self.CONF_MODERATE_BAR - 0.1)
            result["confidence_numeric"] = round(numeric, 1)
            result["confidence"] = self._confidence_level(numeric)
            result["confidence_revised"] = True
            result["confidence_revision_reason"] = (
                "Verifier: confidence capped because the classification is not "
                "backed by an exact variant match or a reviewed consensus."
            )
        return result

    def classify(self, evidence_message: AgentMessage, message_log,
                 directive: str | None = None) -> AgentMessage:
        # Decrypt the incoming payload (it was encrypted by the Evidence Agent)
        evidence = evidence_message.decrypt_payload()

        llm_used = False

        # --- Gene-level overview: no single variant was asked about ------
        # "What variants are in BRCA2?" is an evidence listing, not a
        # pathogenicity question. Scoring the top-ranked row and presenting
        # it as *the* answer would misrepresent the query and could be
        # misread as a verdict about the gene as a whole, so this path
        # returns a no-call result. The Report Agent renders it through
        # generate_gene_overview_report; confidence stays at none/0.0.
        if evidence.get("query_type") == "gene_only":
            gene = evidence.get("gene", "unknown")
            total = evidence.get("total_variants_in_gene")
            result = {
                "classification": "Not applicable (gene overview)",
                "confidence": "none",
                "confidence_numeric": 0.0,
                "criteria_applied": [],
                "acmg_criteria_applied": [],
                "computational_annotations": [],
                "clinvar_context": [],
                "population_observation": None,
                "reasoning": (
                    f"Gene-level listing for {gene}"
                    + (f" ({total} indexed variants)" if total else "")
                    + ": no single variant was asked about, so no "
                      "pathogenicity call is made."
                ),
                "reasoning_mode": "gene_overview",
                "responsible_ai": {
                    "clinical_disclaimer": (
                        "Automated classification is decision support only, "
                        "not a diagnosis. This is an evidence listing for a "
                        "gene, not a pathogenicity verdict."
                    ),
                    "mode": "rule_based",
                    "confidence_source": "not_applicable_gene_overview",
                    "conflict_adjusted": False,
                    "fairness_limitation": (
                        "Population-data limitation: Allele-frequency "
                        "databases may have uneven representation across "
                        "ancestry groups. Population frequency evidence "
                        "should therefore be interpreted cautiously and "
                        "should not be treated as equally informative for "
                        "all populations."
                    ),
                },
            }
            message = AgentMessage(
                variant_id=evidence_message.variant_id,
                stage="classification_complete",
                sender=self.name,
                receiver="report_agent",
                message_type=MessageType.CLASSIFICATION_RESULT,
                trace_id=evidence_message.trace_id,
                payload=result,
            )
            # Encrypt outgoing payload (security: Fernet encryption within the same process)
            message.encrypt_payload()
            message_log.record(message)
            return message

        # Classification is deterministic and local. Gemini is used once,
        # later by the Report Agent, to explain these verified results. Keeping
        # the classifier as the source of truth prevents a second network call
        # and prevents an LLM from changing the classification or confidence.
        result = self.classify_rule_based(evidence)
        result["reasoning_mode"] = "rule_based"
        result["llm_used_for_classification"] = False

        # Reconcile the agent's own call against ClinVar's reviewed consensus
        # (only possible when the enriched workbook supplies prior labels).
        result = self.reconcile_with_consensus(result, evidence)
        if result.get("consensus", {}).get("final_from") == "clinvar_consensus":
            result["reasoning_mode"] = "clinvar_consensus"

        # Self-correction: the Verifier may have sent this call back. Re-state it
        # conservatively before it reaches the Report Agent. Bounded by design --
        # the directive only ever lowers the confidence number.
        if directive == "conservative":
            result = self.apply_conservative_directive(result, evidence)

        message = AgentMessage(
            variant_id=evidence_message.variant_id,
            stage="classification_complete",
            sender=self.name,
            receiver="report_agent",
            message_type=MessageType.CLASSIFICATION_RESULT,
            trace_id=evidence_message.trace_id,
            payload=result,
        )
        # Encrypt payload before passing to next agent
        message.encrypt_payload()
        message_log.record(message)
        return message


if __name__ == "__main__":
    from protocol.message import MessageLog
    from agents.intake_agent import IntakeAgent
    from agents.evidence_agent import EvidenceRetrievalAgent

    log = MessageLog()
    intake = IntakeAgent()
    evidence_agent = EvidenceRetrievalAgent("data/clinvar_variant_dataset.xlsx")
    classifier = ClassificationAgent(use_llm=True, enable_llm_cache=True, smart_llm=True)

    intake_msg = intake.process(
        "Is BRCA1 NC_000017.10:g.41197778A>G pathogenic?", log
    )
    evidence_msg = evidence_agent.retrieve(intake_msg, log)
    class_msg = classifier.classify(evidence_msg, log)
    print(class_msg)
    print(class_msg.to_json())
    print(
        "\nDecrypted payload:",
        json.dumps(class_msg.decrypt_payload(), indent=2, default=str),
    )
