"""
mcp_server.py

Model Context Protocol (MCP) interface to the VariantLens agent pipeline.

The REST API in ``api.py`` is an HTTP transport; this is a second, standardised
transport over the same agents, using **MCP** -- the protocol named in the
assignment brief for exposing tools to an LLM host. An MCP host (Claude Desktop,
an IDE agent, or any MCP client) can connect and call the pipeline as tools
without knowing anything about the internal ``AgentMessage`` protocol; the tools
below are the adapter from MCP to that protocol.

Transport
---------
Stdio (the default for local MCP clients)::

    pip install mcp
    python mcp_server.py

Then register it with an MCP host, e.g. a Claude Desktop ``mcpServers`` entry::

    {
      "mcpServers": {
        "variantlens": { "command": "python", "args": ["/path/to/mcp_server.py"] }
      }
    }

Design notes
------------
* ``mcp`` is an **optional** dependency: this module imports cleanly without it
  (the SDK import is inside ``build_server()``), so the test suite and the REST
  app never require it.
* The tools call the same agents as the API, so a result obtained over MCP is
  identical to one obtained over HTTP -- there is no second implementation to
  drift.
* The MCP path is rule-based by default (no Gemini key required), so the tools
  are deterministic and safe to expose.
"""

import os
import sys
from typing import Any

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from main import VariantInterpretationSystem
from protocol.message import MessageLog

# Built lazily and reused: constructing the system loads the evidence index,
# which we do not want to pay for at import time (or more than once).
_system: VariantInterpretationSystem | None = None


def _get_system() -> VariantInterpretationSystem:
    global _system
    if _system is None:
        # Rule-based: deterministic output and no API key needed to run the tools.
        _system = VariantInterpretationSystem(use_llm=False)
    return _system


# ----------------------------------------------------------------------
# MCP tools -- thin adapters onto the existing four-agent pipeline
# ----------------------------------------------------------------------
def classify_variant(query: str) -> dict[str, Any]:
    """
    Classify a BRCA1/BRCA2 variant from a natural-language query.

    Runs the full four-agent pipeline (Intake -> Evidence -> Classification ->
    Report) and returns the classification, confidence, ACMG criteria applied
    and the plain-language report. Use this for questions like
    "Is BRCA1 NC_000017.10:g.41197778A>G pathogenic?".
    """
    result = _get_system().run(query)
    if "error" in result:
        return {"error": result["error"]}
    return {
        "variant_id": result.get("variant_id"),
        "classification": result.get("classification"),
        "confidence": result.get("confidence"),
        "confidence_numeric": result.get("confidence_numeric"),
        "reasoning_mode": result.get("reasoning_mode"),
        "acmg_criteria_applied": result.get("acmg_criteria_applied", []),
        "report_text": result.get("report_text"),
        "evidence_summary": result.get("evidence_summary"),
        "review_required": result.get("review_required", False),
        "responsible_ai": result.get("responsible_ai", {}),
        "trace_id": (result.get("audit_trail") or [{}])[0].get("trace_id"),
    }


def retrieve_evidence(query: str, top_k: int = 5) -> dict[str, Any]:
    """
    Retrieve ranked evidence for a variant without classifying it.

    Runs only the Intake and Evidence Retrieval agents and returns the top-K
    records with their relevance scores and the IR metadata. Use this to inspect
    what the system retrieved, or to answer "what evidence exists for ...?".
    """
    system = _get_system()
    log = MessageLog()
    try:
        intake_msg = system.intake_agent.process(query, log)
    except Exception as exc:  # InvalidVariantQuery and friends
        return {"error": f"Query rejected: {exc}"}

    evidence_msg = system.evidence_agent.retrieve(intake_msg, log)
    payload = evidence_msg.decrypt_payload()
    results = payload.get("top_k_results", [])[: max(1, top_k)]
    return {
        "variant_id": evidence_msg.variant_id,
        "found": payload.get("found"),
        "gene": payload.get("gene"),
        "top_k_results": results,
        "ir_metadata": payload.get("ir_metadata", {}),
    }


def list_example_queries() -> list[str]:
    """Return example queries the system understands, for host auto-suggestion."""
    return [
        "Is BRCA1 NC_000017.10:g.41197778A>G pathogenic?",
        "What variants are in BRCA2?",
        "What is NC_000017.10:g.41201179C>T?",
        "What variant is at position 41197778 in BRCA1?",
    ]


# ----------------------------------------------------------------------
# MCP server wiring (requires the optional `mcp` package)
# ----------------------------------------------------------------------
def build_server():
    """
    Build the FastMCP server exposing the tools above.

    Imported lazily so this module (and the test suite) works without the `mcp`
    package installed. Raises a clear error if it is missing.
    """
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - exercised only when missing
        raise SystemExit(
            "The MCP server needs the 'mcp' package:\n\n    pip install mcp\n\n"
            "It is optional -- the REST API (uvicorn api:app) has no such dependency."
        ) from exc

    mcp = FastMCP(
        "variantlens",
        instructions=(
            "BRCA1/BRCA2 variant interpretation. Decision support only, never a "
            "diagnosis; every result must be reviewed by a qualified professional."
        ),
    )
    mcp.tool()(classify_variant)
    mcp.tool()(retrieve_evidence)
    mcp.tool()(list_example_queries)
    return mcp


if __name__ == "__main__":
    build_server().run()
