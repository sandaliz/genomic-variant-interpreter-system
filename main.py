"""
Orchestrator for the Genomic Variant Interpretation multi-agent system.

Pipeline:

  User query → [Authentication check] → Intake Agent (NLP + input sanitization)
  → Supervisor Agent (plans retrieval mode + verification budget)
  → Evidence Retrieval Agent (Information Retrieval)
  → Classification Agent (LLM / rule-based reasoning)
  → Verifier Agent (critiques the call; can send it back once)
  → Report Agent (LLM explanation + NLP summarization)
  → Final report

The Supervisor and Verifier are the agentic control plane: the path through the
pipeline is planned per query, and a classification whose confidence is not
earned by the evidence is re-stated more conservatively before it is reported.
See agents/orchestrator.py.

Run:
    python main.py
"""

import os
import sys
import json
import hashlib
from functools import lru_cache

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv

# Load .env (Gemini API key, model, etc.)
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from protocol.message import MessageLog, MessageType
from protocol.security import authenticate, AuthenticationError, InvalidVariantQuery
from agents.intake_agent import IntakeAgent
from agents.evidence_agent import EvidenceRetrievalAgent
from agents.classification_agent import ClassificationAgent
from agents.report_agent import ReportAgent
from agents.orchestrator import SupervisorAgent, VerifierAgent


_DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

# Prefer the ClinVar-enriched workbook when it has been generated
# (`scripts/enrich_clinvar_labels.py`). It carries the real ClinicalSignificance,
# review status and rsID columns the agents use for consensus reconciliation and
# rsID lookup. If it is absent the app still runs on the original
# evidence-only workbook, so a fresh clone works without that build step.
_ENRICHED_DATASET_PATH = os.path.join(_DATA_DIR, "clinvar_variant_dataset_enriched.xlsx")
DATASET_PATH = (
    _ENRICHED_DATASET_PATH
    if os.path.exists(_ENRICHED_DATASET_PATH)
    else os.path.join(_DATA_DIR, "clinvar_variant_dataset.xlsx")
)


class VariantInterpretationSystem:
    """Coordinates all four agents through the shared message protocol."""

    def __init__(self, dataset_path: str = DATASET_PATH, use_llm: bool = True, use_cache: bool = True, fast_mode: bool = True, lazy_load: bool = True, smart_llm: bool = True, batch_llm: bool = True, enable_llm_cache: bool = True):
        self.log = MessageLog()
        self.intake_agent = IntakeAgent()
        self.evidence_agent = EvidenceRetrievalAgent(dataset_path, use_cache=use_cache, lazy_load=lazy_load)
        self.classification_agent = ClassificationAgent(use_llm=use_llm, fast_mode=fast_mode, smart_llm=smart_llm, enable_llm_cache=enable_llm_cache)
        self.report_agent = ReportAgent(use_llm=use_llm, fast_mode=fast_mode, batch_llm=batch_llm, enable_llm_cache=enable_llm_cache)
        # Agentic control plane: the Supervisor plans each run and the Verifier
        # checks the classification before it is reported (see agents/orchestrator.py).
        self.supervisor = SupervisorAgent()
        self.verifier = VerifierAgent()
        self.use_cache = use_cache

    def _generate_cache_key(self, raw_query: str, api_key: str, user_id: str = "") -> str:
        """Generate a unique cache key for a query.

        Bound to the identity (the JWT user id when known, else the legacy
        API key) so cached results can never leak across users.
        """
        identity = user_id or api_key
        query_string = f"{raw_query}:{identity}"
        return hashlib.md5(query_string.encode()).hexdigest()

    @lru_cache(maxsize=50)
    def _run_cached(self, cache_key: str, raw_query: str, api_key: str, user_id: str = "") -> dict:
        """Cached version of the run method for repeated queries.

        user_id is a plain string (the lru key requires hashable values) and is
        part of both keys, so one account can never receive another account result.
        """
        return self._run_uncached(raw_query, api_key, user_id=user_id)

    def _run_uncached(self, raw_query: str, api_key: str, user_id: str = "") -> dict:
        """Uncached version of the run method - actual implementation."""
        # --- Security: authenticate the caller first (agent-layer check). When
        # the API is called behind JWT auth that check already ran; this one is
        # kept as defence-in-depth, so the legacy security boundary is unchanged.
        try:
            user = authenticate(api_key)
        except AuthenticationError as e:
            return {"error": f"Access denied: {e}"}

        print(f"[auth] Authenticated as: {user}")

        pipeline_steps = []

        # --- Agent 1: Intake ---
        try:
            intake_msg = self.intake_agent.process(raw_query, self.log)
        except InvalidVariantQuery as e:
            return {"error": f"Query rejected: {e}"}

        intake_payload = intake_msg.decrypt_payload()
        pipeline_steps.append({
            "agent": "intake_agent",
            "stage": intake_msg.stage,
            "status": "complete",
            "output": {
                "intent": intake_payload.get("intent"),
                "query_type": intake_payload.get("query_type"),
                "extracted_entities": {
                    "gene": intake_payload.get("gene"),
                    "hgvs": intake_payload.get("hgvs"),
                    "position": intake_payload.get("position"),
                    "rsid": intake_payload.get("rsid"),
                },
                "message_type": intake_msg.message_type,
                "encrypted": intake_msg.encrypted,
            },
        })

        print(f"[1/4] {intake_msg}")

        # --- Agent 0: Supervisor plans the run (agentic control, not a fixed chain) ---
        # The plan is derived from the intake result: a bare HGVS/rsID/position
        # query retrieves broadly and is verified leniently; a gene+variant query
        # is targeted and strictly verified. The decision is recorded as a PLAN
        # message so it appears in the audit trail.
        plan = self.supervisor.plan(
            intake_payload, self.log, intake_msg.trace_id, intake_msg.variant_id
        )
        pipeline_steps.append({
            "agent": "supervisor_agent",
            "stage": "plan_ready",
            "status": "complete",
            "output": {
                "message_type": MessageType.PLAN,
                "retrieval_mode": plan["retrieval_mode"],
                "verification_budget": plan["verification_budget"],
                "reason": plan["reason"],
            },
        })
        print(f"[plan] {plan['retrieval_mode']} retrieval: {plan['reason']}")

        # --- Agent 2: Evidence Retrieval (IR) ---
        evidence_msg = self.evidence_agent.retrieve(intake_msg, self.log)
        evidence_payload = evidence_msg.decrypt_payload()
        pipeline_steps.append({
            "agent": "evidence_retrieval_agent",
            "stage": evidence_msg.stage,
            "status": "complete",
            "output": {
                "message_type": evidence_msg.message_type,
                "encrypted": evidence_msg.encrypted,
                "found": evidence_payload.get("found"),
                "exact_match": evidence_payload.get("exact_match"),
                "relevance_score": evidence_payload.get("relevance_score"),
                "relevance_explanation": evidence_payload.get("relevance_explanation"),
                "top_k_count": len(evidence_payload.get("top_k_results", [])),
                "ir_metadata": evidence_payload.get("ir_metadata", {}),
            },
        })

        print(f"[2/4] {evidence_msg}")

        # --- Agent 3: Classification, then the Verifier's check (bounded self-correction) ---
        class_msg = self.classification_agent.classify(evidence_msg, self.log)
        class_payload = class_msg.decrypt_payload()

        verification = self.verifier.verify(
            evidence_payload, class_payload, self.log, class_msg.trace_id,
            class_msg.variant_id, retrieval_mode=plan["retrieval_mode"],
        )
        revision_rounds = 0

        # The Supervisor applies the Verifier's directive at most once, then the
        # Verifier re-checks. This is the loop that makes the pipeline agentic --
        # the run can correct itself, but it can never cycle.
        if verification["verdict"] == "revise" and plan["verification_budget"] > 0:
            print(f"[verify] revising: {verification['issues']}")
            class_msg = self.classification_agent.classify(
                evidence_msg, self.log, directive=verification["directive"]
            )
            class_payload = class_msg.decrypt_payload()
            verification = self.verifier.verify(
                evidence_payload, class_payload, self.log, class_msg.trace_id,
                class_msg.variant_id, retrieval_mode=plan["retrieval_mode"],
                revised=True,
            )
            revision_rounds = 1

        pipeline_steps.append({
            "agent": "classification_agent",
            "stage": class_msg.stage,
            "status": "complete",
            "output": {
                "message_type": class_msg.message_type,
                "encrypted": class_msg.encrypted,
                "classification": class_payload.get("classification"),
                "confidence": class_payload.get("confidence"),
                "confidence_numeric": class_payload.get("confidence_numeric"),
                "score": class_payload.get("score"),
                "reasoning_mode": class_payload.get("reasoning_mode"),
                "criteria_applied": class_payload.get("criteria_applied", []),
                "acmg_criteria_applied": class_payload.get("acmg_criteria_applied", class_payload.get("criteria_applied", [])),
                "computational_annotations": class_payload.get("computational_annotations", []),
                "clinvar_context": class_payload.get("clinvar_context", []),
                "population_observation": class_payload.get("population_observation"),
                "responsible_ai": class_payload.get("responsible_ai", {}),
                "confidence_revised": bool(class_payload.get("confidence_revised")),
            },
        })

        print(f"[3/4] {class_msg}")

        verification_step = {
            "agent": "verifier_agent",
            "stage": "verification_complete",
            "status": "complete",
            "output": {
                "message_type": MessageType.VERIFICATION_RESULT,
                "verdict": verification["verdict"],
                "directive": verification["directive"],
                "issues": verification["issues"],
                "checks": verification["checks"],
                "revision_rounds": revision_rounds,
            },
        }
        pipeline_steps.append(verification_step)

        # --- Agent 4: Report ---
        report_msg = self.report_agent.generate_report(evidence_msg, class_msg, self.log)
        report_payload = report_msg.decrypt_payload()
        pipeline_steps.append({
            "agent": "report_agent",
            "stage": report_msg.stage,
            "status": "complete",
            "output": {
                "message_type": report_msg.message_type,
                "encrypted": report_msg.encrypted,
            },
        })

        print(f"[4/4] {report_msg}")

        # Build the audit trail from the message log
        audit_trail = self._build_audit_trail(report_msg.trace_id)

        # If intake couldn't determine the gene (e.g. an rsID or bare HGVS
        # query with no gene name in the text), but evidence retrieval
        # matched a record and resolved one, use that for display -- the
        # header and entity panel showing "unknown" / "-" when the body of
        # the same report clearly names the gene is confusing and looks
        # like the system doesn't know something it actually does know.
        resolved_gene = intake_payload.get("gene") or evidence_payload.get("gene")
        if not resolved_gene and evidence_payload.get("top_k_results"):
            resolved_gene = evidence_payload["top_k_results"][0].get("gene")

        display_variant_id = report_msg.variant_id
        if resolved_gene and display_variant_id.startswith("unknown:"):
            display_variant_id = display_variant_id.replace("unknown:", f"{resolved_gene}:", 1)
        report_text = report_payload.get("report_text", "")
        if display_variant_id != report_msg.variant_id:
            report_text = report_text.replace(
                report_msg.variant_id, display_variant_id
            )

        return {
            "report_text": report_text,
            "evidence_summary": report_payload.get("evidence_summary", ""),
            "variant_id": display_variant_id,
            # End-to-end id of this run. Returned so the API can persist it
            # next to the caller's query_history row and later scope the
            # trail/pipeline endpoints to the caller's own runs.
            "trace_id": report_msg.trace_id,
            "classification": report_payload.get("classification", ""),
            "confidence": report_payload.get("confidence", "none"),
            "confidence_numeric": report_payload.get("confidence_numeric", 0.0),
            "independent_evidence_confidence_numeric": class_payload.get(
                "independent_evidence_confidence_numeric"
            ),
            "consensus_confidence_numeric": class_payload.get(
                "consensus_confidence_numeric"
            ),
            "confidence_source": class_payload.get("confidence_source"),
            "reasoning_mode": report_payload.get("reasoning_mode", "rule_based"),
            "report_mode": report_payload.get("report_mode", "rule_based"),
            "consensus": class_payload.get("consensus"),
            # The bars in the UI render the raw scorer inputs, which describe
            # the evidence *before* the Verifier's conservative cap. Without
            # these two fields the panel shows full bars next to a capped
            # number with no way to explain the gap.
            "confidence_revised": bool(class_payload.get("confidence_revised", False)),
            "confidence_before_revision": class_payload.get("confidence_before_revision"),
            "review_required": bool(class_payload.get("review_required", False)),
            "coverage_fallback": bool(evidence_payload.get("coverage_fallback", False)),
            "evidence_limitation": evidence_payload.get("evidence_limitation"),
            "criteria_applied": report_payload.get("criteria_applied", []),
            "acmg_criteria_applied": report_payload.get("acmg_criteria_applied", report_payload.get("criteria_applied", [])),
            "computational_annotations": report_payload.get("computational_annotations", []),
            "clinvar_context": report_payload.get("clinvar_context", []),
            "population_observation": report_payload.get("population_observation"),
            "pipeline_steps": pipeline_steps,
            "audit_trail": audit_trail,
            # Agentic control plane: what the Supervisor decided and what the
            # Verifier concluded, so the reasoning is visible to the caller and
            # not only in the logs.
            "agentic": {
                "plan": plan,
                "verification": verification,
                "revision_rounds": revision_rounds,
            },
            "top_k_results": evidence_payload.get("top_k_results", []),
            "exact_match": bool(evidence_payload.get("exact_match", False)),
            "ir_metadata": evidence_payload.get("ir_metadata", {}),
            # The inputs the confidence score was computed from. The report text
            # has always carried this breakdown as prose; exposing the numbers
            # lets a client draw the same picture instead of only quoting it.
            "confidence_components": class_payload.get("confidence_components", {}),
            "responsible_ai": report_payload.get("responsible_ai", {}),
            "intent": intake_payload.get("intent"),
            "query_type": intake_payload.get("query_type"),
            "is_gene_overview": intake_payload.get("query_type") == "gene_only",
            "total_variants_in_gene": evidence_payload.get("total_variants_in_gene"),
            "rsid_lookup_unsupported": evidence_payload.get("rsid_lookup_unsupported", False),
            "retrieval_caveat": evidence_payload.get("ir_metadata", {}).get("retrieval_caveat"),
            "extracted_entities": {
                "gene": resolved_gene,
                "hgvs": intake_payload.get("hgvs"),
                "position": intake_payload.get("position"),
                "rsid": intake_payload.get("rsid"),
            },
        }

    def run(self, raw_query: str, api_key: str = "demo-clinician-key", user_id: str = "") -> dict:
        """
        Run the full pipeline and return a dict with the report and summary.

        Args:
            raw_query: Free-text query about a genetic variant
            api_key: API key for authentication (defaults to demo key)
            user_id: authenticated account id from JWT auth, empty for legacy calls.
                Bound into the cache key so results never leak across users.

        Returns:
            dict with keys: report_text, evidence_summary, variant_id,
            classification, confidence, confidence_numeric, pipeline_steps,
            audit_trail, top_k_results, ir_metadata, responsible_ai
        """
        if self.use_cache:
            cache_key = self._generate_cache_key(raw_query, api_key, user_id)
            result = self._run_cached(cache_key, raw_query, api_key, user_id)
            if "from_cache" not in result:
                result["from_cache"] = True
            return result
        else:
            return self._run_uncached(raw_query, api_key, user_id=user_id)

    def _build_audit_trail(self, trace_id: str) -> list:
        """
        Build a structured audit trail from the message log for a given trace.
        Shows each agent's message with sender, receiver, message_type,
        timestamp, and encrypted status.
        """
        messages = self.log.trail_for(trace_id)
        trail = []
        for m in messages:
            trail.append({
                "message_id": m.message_id,
                "trace_id": m.trace_id,
                "sender": m.sender,
                "receiver": m.receiver,
                "message_type": m.message_type,
                "stage": m.stage,
                "timestamp": m.timestamp,
                "encrypted": m.encrypted,
                "variant_id": m.variant_id,
            })
        return trail

    def show_message_trail(self, variant_id: str):
        """Print the full JSON audit trail for a given variant -- provides an end-to-end audit trail of all agent communication for demonstration and grading purposes."""
        self.log.print_trail(variant_id)
        for m in self.log.history_for(variant_id):
            print(m.to_json())
            print()
            # Show decrypted payload for each message to demonstrate traceability
            print("  decrypted_payload:", json.dumps(m.decrypt_payload(), indent=2, default=str))
            print()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Genomic Variant Interpretation System")
    parser.add_argument(
        "--query",
        type=str,
        default="Is BRCA1 NC_000017.10:g.41197778A>G pathogenic?",
        help="Query about a genetic variant (default: demo query)",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Use rule-based mode only (no Gemini API calls)",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Disable caching for this run (useful for testing)",
    )
    parser.add_argument(
        "--slow-llm",
        action="store_true",
        help="Use slower but potentially more accurate LLM model",
    )
    parser.add_argument(
        "--no-smart-llm",
        action="store_true",
        help="Disable smart LLM usage (use LLM for all cases)",
    )
    parser.add_argument(
        "--no-batch-llm",
        action="store_true",
        help="Disable batch LLM calls (use separate calls)",
    )
    parser.add_argument(
        "--no-llm-cache",
        action="store_true",
        help="Disable LLM response caching",
    )
    args = parser.parse_args()

    system = VariantInterpretationSystem(
        use_llm=not args.no_llm, 
        use_cache=not args.no_cache,
        fast_mode=not args.slow_llm,
        smart_llm=not args.no_smart_llm,
        batch_llm=not args.no_batch_llm,
        enable_llm_cache=not args.no_llm_cache
    )

    print(f"\nQUERY: {args.query}\n")
    if not args.no_cache:
        print("[performance] Caching enabled - repeated queries will be faster\n")
    if not args.slow_llm and not args.no_llm:
        print("[performance] Fast LLM mode enabled for quicker responses\n")
    if not args.no_smart_llm and not args.no_llm:
        print("[performance] Smart LLM usage enabled - skipping LLM for clear cases\n")
    if not args.no_batch_llm and not args.no_llm:
        print("[performance] Batch LLM calls enabled - combining summarization + report generation\n")
    if not args.no_llm_cache and not args.no_llm:
        print("[performance] LLM response caching enabled - similar prompts will be faster\n")

    result = system.run(args.query)

    if "error" in result:
        print(result["error"])
    elif result.get("is_gene_overview"):
        print("\n" + "=" * 60)
        print("GENE OVERVIEW (no pathogenicity call — evidence listing):")
        print(f"  {result['evidence_summary']}")
        print()
        print("REPORT:")
        print(result["report_text"])
        print("=" * 60)
    else:
        print("\n" + "=" * 60)
        print("CLASSIFICATION:")
        print(f"  {result['classification']} (Confidence: {result['confidence'].upper()} / {result['confidence_numeric']}/100)")
        print()

        print("EVIDENCE SUMMARY (NLP):")
        print(result["evidence_summary"])
        print()

        print("=" * 60)
        print("REPORT:")
        print(result["report_text"])
        print("=" * 60)

        # Show pipeline steps
        print("\nPIPELINE STEPS:")
        for step in result["pipeline_steps"]:
            print(f"  {step['agent']} ({step['stage']}): {step['status']}")

        # Show cache status
        if result.get("from_cache"):
            print("\n[performance] Result served from cache")

        # Show the full agent-to-agent message trail (proof of multi-agent communication)
        system.show_message_trail(result["variant_id"])
