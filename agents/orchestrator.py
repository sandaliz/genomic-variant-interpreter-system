"""
agents/orchestrator.py

The agentic layer: a **Supervisor** that plans a run and a **Verifier** that
critiques the result and can send it back once.

Why this exists
---------------
The four specialists (Intake, Evidence, Classification, Report) each do a job
well, but on their own they are a fixed chain: every query takes the same path
and the output is accepted whatever it says. That is a pipeline, not agency.

This module adds the two properties the assignment means by *agentic behaviour*:

  * **Planning / dynamic control** -- the Supervisor reads the Intake result and
    decides, per query, how broadly to retrieve and how strict the verification
    should be. The decision is data-dependent, not hard-coded.
  * **Self-correction** -- the Verifier checks the classification against the
    evidence it rests on and, when the call asserts more confidence than the
    evidence supports, returns a ``revise`` verdict with a directive. The
    Supervisor applies it once (a bounded loop), then the Verifier re-checks.

Both speak the same ``AgentMessage`` protocol as every other agent, so the plan
and the verification appear in the audit trail alongside the four specialist
messages -- the reasoning is inspectable, not hidden in the orchestrator.

Design constraints
------------------
* **Bounded.** At most one revision round, so a run can never loop. The budget is
  part of the plan and is visible in the output.
* **Conservative.** The Verifier only ever asks for *less* confidence; it never
  upgrades a call. Self-correction cannot manufacture certainty.
"""

from protocol.message import AgentMessage, MessageType

# Query types where an exact variant match is unlikely by construction (a bare
# HGVS, an rsID, or a position with no gene), plus queries the Intake Agent could
# not resolve at all. The Supervisor retrieves broadly for these and the Verifier
# tolerates a non-exact match accordingly. The values are the query types emitted
# by IntakeAgent._determine_query_type.
_BROAD_QUERY_TYPES = {"variant_only", "rsid", "position_only", "unknown"}


class SupervisorAgent:
    """Plans a pipeline run from the Intake result."""

    name = "supervisor_agent"

    def plan(self, intake_payload: dict, message_log, trace_id: str,
             variant_id: str) -> dict:
        """
        Decide how this query should be handled and record the plan.

        Returns a plan dict; also emits a ``PLAN`` AgentMessage so the decision
        is part of the auditable trail rather than an implicit assumption.
        """
        query_type = intake_payload.get("query_type", "gene_variant")
        intent = intake_payload.get("intent", "unknown")
        broad = query_type in _BROAD_QUERY_TYPES

        if broad:
            reason = (
                f"{query_type} query: an exact variant match is not guaranteed, so "
                "retrieval runs in broad mode and the verifier tolerates a partial "
                "match rather than demanding an exact one."
            )
        else:
            reason = (
                f"{query_type} query: retrieval targets the named variant and the "
                "verifier requires the classification to be justified by an exact "
                "match or a reviewed consensus."
            )

        plan = {
            "query_type": query_type,
            "intent": intent,
            "retrieval_mode": "broad" if broad else "targeted",
            # One revision round at most -- a run can never loop.
            "verification_budget": 1,
            "reason": reason,
        }

        message = AgentMessage(
            variant_id=variant_id,
            stage="plan_ready",
            sender=self.name,
            receiver="pipeline",
            message_type=MessageType.PLAN,
            trace_id=trace_id,
            payload=plan,
        )
        message.encrypt_payload()
        message_log.record(message)
        return plan


class VerifierAgent:
    """
    Critiques a classification against the evidence it rests on.

    It never inspects the world afresh; it checks internal consistency -- whether
    the confidence asserted is actually earned by the retrieved evidence. Its
    only lever is to ask for a more conservative re-statement.
    """

    name = "verifier_agent"

    def verify(self, evidence: dict, classification: dict, message_log,
               trace_id: str, variant_id: str, retrieval_mode: str = "targeted",
               revised: bool = False) -> dict:
        """
        Return a verdict dict and record it as a ``VERIFICATION_RESULT`` message.

        ``verdict`` is ``"accept"`` or ``"revise"``. On ``"revise"`` the plan's
        budget is spent once, and ``directive`` tells the Classification Agent
        how to re-state the call.
        """
        level = (classification.get("confidence") or "low").lower()
        exact = bool(evidence.get("exact_match"))
        found = bool(evidence.get("found"))
        consensus_backed = (
            (classification.get("consensus") or {}).get("final_from")
            == "clinvar_consensus"
        )
        predictors_complete = bool(
            (evidence.get("predictor_available") or {}).get("complete")
        )

        checks = [
            {"check": "evidence_found", "passed": found},
            {"check": "exact_match", "passed": exact,
             "optional": retrieval_mode == "broad"},
            {"check": "predictors_complete", "passed": predictors_complete,
             "optional": True},
            {"check": "confidence_justified",
             "passed": exact or consensus_backed or level == "low"},
        ]
        issues = [c["check"] for c in checks if not c["passed"] and not c.get("optional")]

        verdict = "accept"
        directive = None

        # Over-confidence guard: a moderate or high call must be earned by an
        # exact match or a reviewed consensus. In broad mode the exact match is
        # not expected, so only a *high* call is challenged there.
        threshold = {"high"} if retrieval_mode == "broad" else {"moderate", "high"}
        if level in threshold and not exact and not consensus_backed:
            verdict = "revise"
            directive = "conservative"
            issues.append("confidence asserted without an exact match or reviewed consensus")
        elif not found:
            # Nothing was retrieved: the classifier should already be at VUS/low,
            # but if it is not, the same conservative directive applies.
            if level in ("moderate", "high"):
                verdict = "revise"
                directive = "conservative"
                issues.append("no evidence retrieved but a confident call was made")

        result = {
            "verdict": verdict,
            "directive": directive,
            "checks": checks,
            "issues": issues,
            "retrieval_mode": retrieval_mode,
            "revised": revised,
        }

        message = AgentMessage(
            variant_id=variant_id,
            stage="verification_complete",
            sender=self.name,
            receiver="supervisor_agent",
            message_type=MessageType.VERIFICATION_RESULT,
            trace_id=trace_id,
            payload=result,
        )
        message.encrypt_payload()
        message_log.record(message)
        return result
