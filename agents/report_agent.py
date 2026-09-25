"""
agents/report_agent.py

AGENT 4: Report Agent
-----------------------
Rubric mapping: LLM (explanation generation) + NLP (summarization) +
Responsible AI (explainability, honest uncertainty communication)

Job: take the classification + evidence and produce a plain-language
report a clinician (or student, for demo purposes) can read. Critically,
this agent must NEVER overstate confidence -- if the Classification
Agent says "low confidence" or "VUS", the report must say so clearly
and recommend expert review, not present a false-confident answer.

Responsible AI layer (explicit pipeline, visible in every report):

      Evidence disagreement (conflict flag)
            ↓
      Confidence adjustment  (low confidence forced)
            ↓
      Classification         (LLM + rule-based reasoning)
            ↓
      Explainability          (evidence + ACMG criteria + retrieval rationale)
            ↓
      Clinical disclaimer     (decision-support only, not diagnosis)

      Explicitly distinguishes:
      Classification confidence ≠ medical certainty

NLP: this agent performs **summarization** -- it condenses the raw
evidence bundle into a concise, human-readable summary before generating
the final report. This satisfies the "Summarization" NLP requirement.

Two report modes:
  1. `generate_report_template()` -- deterministic template-based report,
     always available as a fallback. This mode is fully auditable and
     transparent -- no hidden LLM decisions.
  2. `generate_report_with_llm()` -- sends the evidence + classification
     to Gemini to produce a natural-language, plain-English report. The LLM
     output is guided by the system instruction to never overstate confidence.

Security: decrypts the incoming message payload, and encrypts its own
outgoing payload (encryption in transit between agents).
"""

import sys
import os
import json
import math

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from protocol.message import AgentMessage, MessageType
from llm.gemini_client import GeminiClient


def _is_directional_conflict(evidence: dict) -> bool:
    """Direction-aware conflict test (same semantics as the Classification Agent).

    ClinVar's own consensus label is authoritative when present: it reads
    "Conflicting" only when submissions disagree in DIRECTION. The dataset's
    boolean flag is cruder and is 1 even for agreement-in-direction cases
    (e.g. Pathogenic vs Likely Pathogenic), so it is only a fallback.
    """
    prior = str(evidence.get("prior_classification") or "").strip().lower()
    if prior:
        return prior == "conflicting"
    return evidence.get("conflicting_classification_flag", 0) == 1


class ReportAgent:
    name = "report_agent"

    def __init__(self, use_llm: bool = True, fast_mode: bool = True, batch_llm: bool = True, enable_llm_cache: bool = True):
        self.use_llm = use_llm
        self.llm = GeminiClient(fast_mode=fast_mode, enable_llm_cache=enable_llm_cache)
        self.batch_llm = batch_llm  # Combine summarization + report generation

    @staticmethod
    def _display(value):
        if value is None:
            return "not available"
        try:
            if not math.isfinite(float(value)):
                return "not available"
        except (TypeError, ValueError):
            pass
        return str(value)

    # ------------------------------------------------------------------
    # NLP: Summarization
    # ------------------------------------------------------------------
    def summarize_evidence(self, evidence: dict) -> str:
        """
        NLP technique: extractive summarization of the evidence bundle.

        Condenses the raw evidence dict into a concise, human-readable
        summary that highlights the most clinically relevant facts.
        This is a deterministic extractive summarizer (no LLM needed),
        which is transparent and auditable.
        """
        if not evidence.get("found"):
            return "No matching evidence record found in the database."

        parts = []

        # Variant consequence / impact
        consequence = evidence.get("consequence")
        impact = evidence.get("impact")
        if consequence:
            parts.append(f"Variant consequence: {consequence}")
        if impact:
            parts.append(f"Predicted impact: {impact}")

        # Computational predictors
        parts.append(f"SIFT prediction: {self._display(evidence.get('sift_prediction'))}")
        parts.append(f"PolyPhen prediction: {self._display(evidence.get('polyphen_prediction'))}")

        # CADD score
        parts.append(f"CADD score: {self._display(evidence.get('cadd_score'))}")

        # Population frequency
        frequencies = [
            value for value in (
                evidence.get("af_esp"), evidence.get("af_exac"),
                evidence.get("af_tgp"), evidence.get("af_gnomad"),
            ) if value is not None
        ]
        max_af = max(frequencies) if frequencies else "not available"
        parts.append(f"Max population allele frequency: {max_af}")

        # Conflicting classification (direction-aware -- see helper above)
        conflict = _is_directional_conflict(evidence)
        if evidence.get("prior_classification"):
            parts.append(
                f"ClinVar consensus: {evidence.get('prior_classification')} "
                f"({evidence.get('prior_review_status') or 'review status unknown'})"
            )
        parts.append(
            "Prior submitters DISAGREED on this variant's classification"
            if conflict else
            "Prior submitters agreed on this variant's classification"
        )

        # IR metadata summary
        top_k = evidence.get("top_k_results", [])
        if top_k:
            parts.append(f"Retrieved {len(top_k)} ranked evidence records (top relevance: {max(r.get('relevance_score', 0) for r in top_k):.2f})")

        return "; ".join(parts)

    def summarize_with_llm(self, evidence: dict, result: dict) -> str:
        """
        NLP technique: abstractive summarization using Gemini.

        Sends the evidence + classification to Gemini and asks it to
        produce a concise 2-3 sentence clinical summary. Falls back to
        the extractive summarizer if the LLM call fails.
        """
        evidence_summary = json.dumps(evidence, indent=2, default=str)
        result_summary = json.dumps(result, indent=2, default=str)

        system_instruction = (
            "You are a clinical genetics summarizer. "
            "Produce a concise, accurate 2-3 sentence summary of the "
            "evidence and classification for a clinician. "
            "Do not add information that is not present in the input. "
            "Never overstate confidence."
        )

        prompt = (
            f"Evidence:\n{evidence_summary}\n\n"
            f"Classification:\n{result_summary}\n\n"
            "Summarize this in 2-3 sentences for a clinician."
        )

        try:
            summary = self.llm.generate(
                prompt=prompt,
                system_instruction=system_instruction,
                max_tokens=200,  # Reduced from 300 for faster response
            )
            return summary.strip()
        except (RuntimeError, ConnectionError, TimeoutError) as e:
            # Fallback to extractive summarization for API/Network errors
            return self.summarize_evidence(evidence)

    # ------------------------------------------------------------------
    # Mode 1: Deterministic template-based report (transparent / auditable)
    # ------------------------------------------------------------------
    def generate_report_template(self, evidence: dict, result: dict,
                                 variant_id: str,
                                 top_k_results: list = None) -> str:
        lines = []
        lines.append(f"Variant Interpretation Report: {variant_id}")
        lines.append("=" * 50)

        if not evidence.get("found"):
            # Provide a helpful message based on the query type
            msg = evidence.get("message", "No matching record was found in our evidence database.")
            lines.append(msg)
            lines.append("")
            lines.append(
                "This system cannot classify variants it has no evidence for. "
                "Please try a different query, or consult ClinVar directly or a "
                "certified genetic counselor."
            )
        else:
            # Classification result
            classification = result.get("classification", "Unknown")
            confidence = result.get("confidence", "none")
            confidence_numeric = result.get("confidence_numeric", 0.0)
            lines.append(f"Classification: {classification}")
            lines.append(f"Confidence: {confidence.upper()} ({confidence_numeric}/100)")

            # Provenance of the final label. The agent's own ACMG-style call and
            # ClinVar's consensus are deliberately kept distinct, and the user
            # must be able to tell which one produced the line above -- a call
            # deferred to ClinVar is not the agent's own reasoning.
            consensus = result.get("consensus") or {}
            final_from = consensus.get("final_from")
            if final_from == "clinvar_consensus":
                lines.append("  - Basis of final call: deferred to ClinVar's reviewed consensus")
                lines.append(
                    f"  - ClinVar: {consensus.get('clinvar_classification')} "
                    f"[{consensus.get('clinvar_review_status')}] "
                    f"({consensus.get('clinvar_review_stars')}-star)"
                )
                if consensus.get("evidence_based_confidence_numeric") is not None:
                    lines.append(
                        "  - The confidence above follows the ClinVar review "
                        "status; on this tool's own evidence alone it was "
                        f"{consensus['evidence_based_confidence_numeric']}/100 "
                        f"({consensus.get('evidence_based_confidence')})."
                    )
            elif consensus.get("clinvar_classification"):
                lines.append(
                    "  - Basis of final call: evidence-based ACMG-style reasoning"
                )
                if consensus.get("agreement") is True:
                    lines.append("  - Agrees with ClinVar's consensus for this variant.")
            if consensus.get("agreement") is False and consensus.get("note"):
                lines.append(f"  - {consensus['note']}")
            lines.append("")

            # Evidence summary
            lines.append("Evidence considered:")
            lines.append(f"  - Gene: {evidence.get('gene')}")
            lines.append(f"  - Variant: {evidence.get('hgvs')}")
            lines.append(f"  - Variant consequence: {self._display(evidence.get('consequence'))}")
            lines.append(f"  - Predicted impact: {self._display(evidence.get('impact'))}")
            lines.append(f"  - SIFT prediction: {self._display(evidence.get('sift_prediction'))}")
            lines.append(f"  - PolyPhen prediction: {self._display(evidence.get('polyphen_prediction'))}")
            lines.append(f"  - CADD score: {self._display(evidence.get('cadd_score'))}")
            frequencies = [
                value for value in (
                    evidence.get("af_esp"), evidence.get("af_exac"),
                    evidence.get("af_tgp"), evidence.get("af_gnomad"),
                ) if value is not None
            ]
            lines.append(
                "  - Population frequency (max across ESP/ExAC/1000G/gnomAD): "
                + (str(max(frequencies)) if frequencies else "not available")
            )
            conflict = _is_directional_conflict(evidence)
            if evidence.get("prior_classification"):
                lines.append(
                    "  - ClinVar consensus: "
                    f"{evidence.get('prior_classification')} "
                    f"[{evidence.get('prior_review_status') or 'review status unknown'}]"
                )
            lines.append(
                "  - Submitter agreement: "
                + ("DISAGREED (ClinVar reports conflicting classifications)" if conflict
                   else "Agreed (no directional conflict on record)")
            )
            lines.append("")

            # --- Explainable IR results ---
            if top_k_results:
                lines.append("Retrieved Evidence (top-ranked):")
                for i, r in enumerate(top_k_results, 1):
                    lines.append(
                        f"  {i}. {r.get('match_type', 'TF-IDF text similarity')}"
                        f" — {r.get('gene', '?')}:{r.get('hgvs', '?')}"
                        f" (Relevance: {r.get('relevance_score', 0):.2f})"
                    )
                    explanation = r.get("relevance_explanation", "")
                    if explanation:
                        lines.append(f"     {explanation}")
                lines.append("")

            # ACMG criteria (validated codes only) + separated supporting context
            lines.append("ACMG criteria applied (validated codes only):")
            acmg = result.get("acmg_criteria_applied", result.get("criteria_applied", []))
            if acmg:
                for c in acmg:
                    lines.append(f"  - {c}")
            else:
                lines.append("  - None. No definitive pathogenic or benign ACMG criterion was met.")
            lines.append("")
            comp = result.get("computational_annotations", [])
            if comp:
                lines.append("Computational annotations (supporting only, not ACMG criteria):")
                for c in comp:
                    lines.append(f"  - {c}")
                lines.append("")
            pop_obs = result.get("population_observation")
            if pop_obs:
                lines.append("Population-frequency observation (BS1 unestablished):")
                lines.append(f"  - {pop_obs}")
                lines.append("")
            clinvar_ctx = result.get("clinvar_context", [])
            if clinvar_ctx:
                lines.append("ClinVar context (database status, not an ACMG criterion):")
                for c in clinvar_ctx:
                    lines.append(f"  - {c}")
                lines.append("")

            # Confidence components
            components = result.get("confidence_components", {})
            if components:
                lines.append("Confidence breakdown:")
                lines.append(f"  - Evidence strength: {components.get('evidence_strength', 'N/A')}/40")
                lines.append("  - Submitter consistency: "
                             + str(components.get("clinvar_consistency", "N/A")).replace("_", " "))
                lines.append(f"  - Supporting records: {components.get('supporting_records', 'N/A')}/20")
                lines.append("  - Predictor agreement: "
                             + str(components.get("predictor_agreement", "N/A")).replace("_", " "))
                lines.append("")

            # Responsible AI: confidence ≠ medical certainty
            lines.append(
                "IMPORTANT: Classification confidence ≠ medical certainty. "
                "This score reflects algorithmic confidence based on available evidence, "
                "not a clinical diagnosis."
            )
            lines.append("")

            # Confidence-based caveats. State the actual reason rather than one
            # blanket sentence: conflating "our call rested on little evidence"
            # with "submitters disagree with each other" would misreport a
            # variant that in fact has a clean reviewed consensus.
            if conflict:
                lines.append(
                    "IMPORTANT: Prior submitters have disagreed on this "
                    "variant's classification. This result should be treated as "
                    "preliminary and reviewed by a certified genetic counselor "
                    "before being used in any clinical decision."
                )
            elif confidence == "low":
                lines.append(
                    "IMPORTANT: Confidence in this classification is low because "
                    "little independent evidence was available for it. Treat the "
                    "result as preliminary and have it reviewed by a certified "
                    "genetic counselor before being used in any clinical decision."
                )
                if evidence.get("coverage_fallback"):
                    lines.append(
                        "NOTE: This variant is absent from the study workbook and was "
                        "resolved from ClinVar's variant_summary index, which supplies "
                        "no SIFT/PolyPhen/CADD scores or allele frequencies -- the "
                        "result therefore rests on the ClinVar consensus rather than "
                        "on independent functional evidence."
                    )
            elif classification == "Uncertain Significance (VUS)":
                lines.append(
                    "This variant has insufficient validated ACMG/AMP evidence for "
                    "a definitive pathogenic or benign call, so its automated "
                    "classification is VUS / unresolved. Clinical status: Not "
                    "established; requires certified clinical review. This is common "
                    "and does not mean the variant is dangerous -- it means more "
                    "evidence is needed. Reclassification may occur as more data "
                    "becomes available."
                )
            else:
                lines.append(
                    "While this result indicates a likely classification, it "
                    "should still be reviewed by a qualified genetic counselor "
                    "as part of a comprehensive clinical assessment."
                )

            lines.append("")
            lines.append(
                "IMPORTANT DISCLAIMER: This is an academic decision-support prototype. "
                "It does not provide medical diagnoses or replace qualified genetic "
                "counselors, clinicians, or laboratory interpretation. Genetic test "
                "results must always be interpreted within the broader context of a "
                "patient's clinical and family history by a qualified healthcare "
                "professional. This tool is not intended for standalone clinical use."
            )

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Mode 2: LLM-generated report via Gemini API
    # ------------------------------------------------------------------
    def generate_report_with_llm(self, evidence: dict, result: dict,
                                  variant_id: str,
                                  top_k_results: list = None) -> str:
        """
        Send the evidence + classification to Gemini and ask it to write a
        plain-language report. Falls back to the template if the LLM fails.
        """
        evidence_summary = json.dumps(evidence, indent=2, default=str)
        result_summary = json.dumps(result, indent=2, default=str)

        top_k_text = ""
        if top_k_results:
            top_k_text = "\n\nRetrieved evidence (top-K with relevance scores):\n"
            for i, r in enumerate(top_k_results, 1):
                top_k_text += (
                    f"  {i}. [{r.get('match_type', 'N/A')}] "
                    f"{r.get('gene', '?')}:{r.get('hgvs', '?')} "
                    f"(Relevance: {r.get('relevance_score', 0):.2f})\n"
                )
                if r.get("relevance_explanation"):
                    top_k_text += f"     {r['relevance_explanation']}\n"

        system_instruction = (
            "You are a clinical genetic counseling report writer. "
            "Write clear, plain-language reports for clinicians and patients. "
            "NEVER overstate confidence. If confidence is low or the result is VUS, "
            "say so clearly and recommend expert review. "
            "ALWAYS explicitly state: 'Classification confidence ≠ medical certainty.' "
            "Always include a prominent disclaimer that this is an academic "
            "decision-support prototype and does not provide medical diagnoses "
            "or replace qualified genetic counselors, clinicians, or laboratory "
            "interpretation. This tool is not intended for standalone clinical use."
        )

        prompt = (
            f"Variant: {variant_id}\n\n"
            f"Evidence:\n{evidence_summary}\n"
            f"Classification:\n{result_summary}\n"
            f"{top_k_text}\n\n"
            "Write a concise, plain-language clinical report (max 300 words) "
            "that includes:\n"
            "1. The classification and confidence level (numeric and categorical). "
            "Copy BOTH values exactly as they appear in the Classification JSON -- "
            "never invent, round or recompute them. If the JSON says "
            '"confidence": "low" with confidence_numeric 28, you must write '
            '"low (28/100)", never "high".\n'
            "2. The key evidence considered\n"
            "3. The top retrieved evidence records with relevance scores\n"
            "4. Any caveats or limitations\n"
            "5. A recommendation for next steps\n"
            "6. Explicitly state that classification confidence ≠ medical certainty\n"
            "7. A disclaimer that this is a decision-support tool, not a diagnosis"
        )

        try:
            report = self.llm.generate(
                prompt=prompt,
                system_instruction=system_instruction,
                max_tokens=768,  # Reduced from 1024 for faster response
            )
            return report.strip()
        except (RuntimeError, ConnectionError, TimeoutError) as e:
            # Fallback to template for API/Network errors
            fallback = self.generate_report_template(
                evidence, result, variant_id, top_k_results=top_k_results
            )
            return (
                f"[LLM report generation failed ({e}); using template report]\n\n"
                + fallback
            )

    def generate_report_with_llm_batch(self, evidence: dict, result: dict,
                                  variant_id: str,
                                  top_k_results: list = None) -> tuple:
        """
        Combined LLM call for both summarization and report generation.
        Returns (report_text, evidence_summary) tuple.
        """
        evidence_summary = json.dumps(evidence, indent=2, default=str)
        result_summary = json.dumps(result, indent=2, default=str)

        top_k_text = ""
        if top_k_results:
            top_k_text = "\n\nRetrieved evidence (top-K with relevance scores):\n"
            for i, r in enumerate(top_k_results, 1):
                top_k_text += (
                    f"  {i}. [{r.get('match_type', 'N/A')}] "
                    f"{r.get('gene', '?')}:{r.get('hgvs', '?')} "
                    f"(Relevance: {r.get('relevance_score', 0):.2f})\n"
                )
                if r.get("relevance_explanation"):
                    top_k_text += f"     {r['relevance_explanation']}\n"

        system_instruction = (
            "You are a clinical genetic counseling report writer. "
            "Write clear, plain-language reports for clinicians and patients. "
            "NEVER overstate confidence. If confidence is low or the result is VUS, "
            "say so clearly and recommend expert review. "
            "ALWAYS explicitly state: 'Classification confidence ≠ medical certainty.' "
            "Always include a prominent disclaimer that this is an academic "
            "decision-support prototype and does not provide medical diagnoses "
            "or replace qualified genetic counselors, clinicians, or laboratory "
            "interpretation. This tool is not intended for standalone clinical use."
        )

        prompt = (
            f"Variant: {variant_id}\n\n"
            f"Evidence:\n{evidence_summary}\n"
            f"Classification:\n{result_summary}\n"
            f"{top_k_text}\n\n"
            "Provide TWO outputs in this exact JSON format:\n"
            "{\n"
            '  "evidence_summary": "2-3 sentence clinical summary of evidence",\n'
            '  "report_text": "Full clinical report (max 300 words) with classification, confidence, evidence, caveats, and recommendation"\n'
            "}\n\n"
            "Requirements:\n"
            "1. evidence_summary: Concise 2-3 sentence summary for clinicians\n"
            "2. report_text: Full report with classification, confidence level (numeric and categorical), key evidence, top retrieved evidence with relevance scores, caveats, recommendation, explicit statement that classification confidence ≠ medical certainty, and disclaimer that this is a decision-support tool not a diagnosis. Reproduce the confidence label and numeric score EXACTLY as given in the Classification JSON -- never recompute or reinterpret them"
        )

        try:
            combined_response = self.llm.generate(
                prompt=prompt,
                system_instruction=system_instruction,
                max_tokens=1024,  # Combined response for both
            )
            
            # Parse the combined response
            cleaned = combined_response.strip()
            if cleaned.startswith("```"):
                lines = cleaned.splitlines()
                if lines and lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]
                cleaned = "\n".join(lines).strip()
            
            response_dict = json.loads(cleaned)
            
            return (
                response_dict.get("report_text", ""),
                response_dict.get("evidence_summary", "")
            )
            
        except (RuntimeError, ConnectionError, TimeoutError, json.JSONDecodeError) as e:
            # Fallback to separate calls if batch fails
            print(f"[report_agent] Batch LLM failed, using separate calls: {e}")
            return (
                self.generate_report_with_llm(evidence, result, variant_id, top_k_results),
                self.summarize_with_llm(evidence, result)
            )

    # ------------------------------------------------------------------
    # Gene-level overview (no single variant was asked about)
    # ------------------------------------------------------------------
    def generate_gene_overview_report(self, evidence: dict, result: dict) -> str:
        """
        Deterministic report for gene-level queries ("What variants are in
        BRCA1?"). Lists the retrieved variants and their raw evidence facts
        without asserting a single pathogenicity verdict for the gene --
        that would misrepresent a broad question as a specific-variant answer.
        """
        gene = evidence.get("gene", "this gene")
        total = evidence.get("total_variants_in_gene")
        top_k = evidence.get("top_k_results", [])

        lines = []
        lines.append(f"GENE VARIANT OVERVIEW: {gene}")
        lines.append("=" * 50)
        lines.append(
            f"{total if total is not None else len(top_k)} variant record(s) are indexed "
            f"for {gene} in this dataset. Showing the {len(top_k)} most evidence-complete, "
            "ranked by relevance."
        )
        lines.append("")
        lines.append(
            "IMPORTANT: This is a list of individual variant records, not a "
            "single classification for the gene. BRCA1/BRCA2 variants vary "
            "widely in clinical significance -- some are pathogenic, many are "
            "benign polymorphisms, and many remain of uncertain significance. "
            "Ask about a specific HGVS notation, rsID, or genomic position for "
            "a full classification of that one variant."
        )
        lines.append("")

        if not top_k:
            lines.append(f"No indexed variant records were found for {gene}.")
        else:
            lines.append("Variants retrieved:")
            for i, r in enumerate(top_k, 1):
                lines.append(
                    f"  {i}. {r.get('gene', '?')}:{r.get('hgvs', '?')} "
                    f"(Relevance: {r.get('relevance_score', 0):.2f}, {r.get('match_type', 'ranked match')})"
                )
                lines.append(
                    f"     Impact: {self._display(r.get('impact'))} | "
                    f"SIFT: {self._display(r.get('sift_prediction'))} | "
                    f"PolyPhen: {self._display(r.get('polyphen_prediction'))} | "
                    f"CADD: {self._display(r.get('cadd_score'))}"
                )
                if r.get("conflicting_classification_flag"):
                    lines.append(
                        "     Note: the source dataset records differing submitter "
                        "classifications for this variant."
                    )

            top = top_k[0]
            lines.append("")
            lines.append(
                f"Most evidence-complete single variant: {top.get('gene', '?')}:{top.get('hgvs', '?')} -- "
                f"individually classifies as {result.get('classification', 'Unknown')} "
                f"({result.get('confidence', 'none').upper()}, {result.get('confidence_numeric', 0.0)}/100). "
                "This classification applies to that one variant only, not to the gene as a whole. "
                "Ask about it by HGVS notation for the full single-variant report."
            )

        lines.append("")
        lines.append(
            "DISCLAIMER: This is an academic decision-support prototype. It does "
            "not provide medical diagnoses or replace qualified genetic counselors, "
            "clinicians, or laboratory interpretation. This tool is not intended "
            "for standalone clinical use."
        )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------
    def generate_report(self, evidence_message: AgentMessage,
                        classification_message: AgentMessage,
                        message_log) -> AgentMessage:

        # Decrypt incoming payloads (they were encrypted by previous agents)
        evidence = evidence_message.decrypt_payload()
        result = classification_message.decrypt_payload()
        variant_id = classification_message.variant_id

        # Extract top-K results for the report (explainable retrieval)
        top_k_results = evidence.get("top_k_results", [])

        # --- Gene-level ("tell me about variants in X") queries get a
        # distinct, honest overview instead of a single-variant verdict.
        # A broad gene query has no specific variant to classify, so
        # presenting one arbitrarily top-ranked variant's classification
        # as "the answer" would misrepresent what was asked and could be
        # misread as a statement about the gene as a whole. ---
        if evidence.get("query_type") == "gene_only":
            report_text = self.generate_gene_overview_report(evidence, result)
            evidence_summary = (
                f"Showing the {len(top_k_results)} most evidence-complete of "
                f"{evidence.get('total_variants_in_gene', len(top_k_results))} indexed "
                f"variants for {evidence.get('gene', 'this gene')}. This is an evidence "
                f"listing, not a single pathogenicity verdict for the gene."
            )
        # --- NLP: Summarization step ---
        # Generate a concise summary of the evidence (extractive or abstractive)
        elif self.use_llm and self.llm.available:
            if self.batch_llm:
                # Use batch LLM call for both summary and report
                report_text, evidence_summary = self.generate_report_with_llm_batch(
                    evidence, result, variant_id, top_k_results=top_k_results
                )
            else:
                # Separate LLM calls
                evidence_summary = self.summarize_with_llm(evidence, result)
                report_text = self.generate_report_with_llm(
                    evidence, result, variant_id, top_k_results=top_k_results
                )
        else:
            evidence_summary = self.summarize_evidence(evidence)
            report_text = self.generate_report_template(
                evidence, result, variant_id, top_k_results=top_k_results
            )

        message = AgentMessage(
            variant_id=variant_id,
            stage="report_complete",
            sender=self.name,
            receiver="orchestrator",
            message_type=MessageType.REPORT_RESULT,
            trace_id=evidence_message.trace_id,
            payload={
                "report_text": report_text,
                "evidence_summary": evidence_summary,
                "classification": result.get("classification", ""),
                "confidence": result.get("confidence", "none"),
                "confidence_numeric": result.get("confidence_numeric", 0.0),
                "responsible_ai": result.get("responsible_ai", {}),
                "criteria_applied": result.get("criteria_applied", []),
                "acmg_criteria_applied": result.get("acmg_criteria_applied", result.get("criteria_applied", [])),
                "computational_annotations": result.get("computational_annotations", []),
                "clinvar_context": result.get("clinvar_context", []),
                "population_observation": result.get("population_observation"),
                "reasoning_mode": result.get("reasoning_mode", "rule_based"),
                "ir_metadata": evidence.get("ir_metadata", {}),
            },
        )
        # Encrypt payload before returning to the orchestrator
        message.encrypt_payload()
        message_log.record(message)
        return message


if __name__ == "__main__":
    from protocol.message import MessageLog
    from agents.intake_agent import IntakeAgent
    from agents.evidence_agent import EvidenceRetrievalAgent
    from agents.classification_agent import ClassificationAgent

    log = MessageLog()
    intake = IntakeAgent()
    evidence_agent = EvidenceRetrievalAgent("data/clinvar_variant_dataset.xlsx")
    classifier = ClassificationAgent(use_llm=True, enable_llm_cache=True, smart_llm=True)
    reporter = ReportAgent(use_llm=True, enable_llm_cache=True, batch_llm=True)

    intake_msg = intake.process(
        "Is BRCA1 NC_000017.10:g.41197778A>G pathogenic?", log
    )
    evidence_msg = evidence_agent.retrieve(intake_msg, log)
    class_msg = classifier.classify(evidence_msg, log)
    report_msg = reporter.generate_report(evidence_msg, class_msg, log)

    payload = report_msg.decrypt_payload()
    print("Evidence Summary (NLP):")
    print(payload["evidence_summary"])
    print("\n" + "=" * 60)
    print(payload["report_text"])
