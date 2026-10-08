"""
agents/intake_agent.py

AGENT 1: Intake Agent
---------------------
Rubric mapping: NLP (Named Entity Recognition + Intent Classification) + Security (input sanitization)

Job: take a raw user query (free text or structured), extract the gene
and variant identity, sanitize the input, classify the query **intent**
(rule-based intent classification), and hand a clean structured
message to the next agent.

Supports multiple query types (intents):

  1. gene_variant:   "Is BRCA1 NC_000017.10:g.41197778A>G pathogenic?"
                     intent = classification
  2. gene_only:      "What variants are in BRCA2?"
                     intent = gene_search
  3. variant_only:   "What is NC_000017.10:g.41201179C>T?"
                     intent = variant_search
  4. position:       "What variant is at position 41197778 in BRCA1?"
                     intent = position_search
  5. rsid:           "Tell me about rs123456"
                     intent = rsid_search
  6. position_only:  "What variant is at position 41197778?"
                     intent = position_search

NLP approach:
  - **Entity extraction (NER):** rule-based regex + a small gene dictionary.
    This is an explainable technique that reliably parses the query formats
    this system supports, and can be extended with a trained model
    (e.g. spaCy) if broader free-text coverage is needed.
  - **Intent classification:** rule-based classification of the query
    into one of the supported intents above, based on the combination of
    extracted entities. This demonstrates a second NLP technique
    (intent classification) alongside NER.

Security: outgoing message payload is encrypted before being passed
to the next agent (encryption in transit).
"""

import re
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from protocol.message import AgentMessage, MessageType
from protocol.security import sanitize_query, validate_gene, InvalidVariantQuery, _ALLOWED_GENES


# Simple gene dictionary -- stands in for a trained NER model's vocabulary
_GENE_ALIASES = {
    "BRCA1": "BRCA1",
    "BRCA 1": "BRCA1",
    "BRCA2": "BRCA2",
    "BRCA 2": "BRCA2",
}

# HGVS-style pattern matchers -- supports both coding (c.) and genomic (g.) notation
_HGVS_PATTERN = re.compile(r"(?:c\.[0-9_]+[A-Za-z>]+|NC_[0-9]+\.[0-9]+:g\.[0-9]+[A-Za-z>]+)")

# Position pattern: "position 41197778" or "at 41197778"
_POSITION_PATTERN = re.compile(r"(?:position|pos\.?|at)\s+(\d{4,10})", re.IGNORECASE)

# rsID pattern: "rs123456"
_RSID_PATTERN = re.compile(r"\brs\d+\b", re.IGNORECASE)

# Gene-like pattern for detecting unsupported gene names directly before HGVS
# notation, e.g. "TP53" in "Is TP53 NC_000017.10:g.41197778A>G pathogenic?"
# Requires ALL-CAPS so common words like "Is", "What" are not falsely matched.
_GENE_BEFORE_HGVS = re.compile(
    r"\b([A-Z]{2,}[0-9]*)\s+(NC_[0-9]+\.[0-9]+:g\.[0-9]+[A-Za-z>]+)"
)

# Intent classification patterns
_INTENT_PATTERNS = {
    "classification": re.compile(r"\b(pathogenic|pwp|pathogenic|benign|deleterious|disease|disease risk|cancer risk|harmful|cause cancer|clinical significance)\b", re.IGNORECASE),
    "gene_search": re.compile(r"\b(variants?|mutations?|changes?|polymorphisms?)\b.*\b(in|of|for)\b\s+\w+", re.IGNORECASE),
}


class IntakeAgent:
    name = "intake_agent"

    def extract_entities(self, raw_text: str) -> dict:
        """
        Rule-based NER: pull gene name, HGVS notation, position, or rsID
        out of free text.

        Returns a dict with keys: gene, hgvs, position, rsid (each None if
        not found).
        """
        gene = None
        for alias, canonical in _GENE_ALIASES.items():
            if alias.lower() in raw_text.lower():
                gene = canonical
                break

        hgvs_match = _HGVS_PATTERN.search(raw_text)
        hgvs = hgvs_match.group(0) if hgvs_match else None

        pos_match = _POSITION_PATTERN.search(raw_text)
        position = pos_match.group(1) if pos_match else None

        rsid_match = _RSID_PATTERN.search(raw_text)
        rsid = rsid_match.group(0) if rsid_match else None

        return {"gene": gene, "hgvs": hgvs, "position": position, "rsid": rsid}

    def _determine_query_type(self, entities: dict) -> str:
        """
        Classify the query intent based on the extracted entities.

        This is the **intent classification** NLP step. It maps a combination
        of extracted entities to one of the supported query types (intents).
        """
        if entities["hgvs"]:
            return "gene_variant" if entities["gene"] else "variant_only"
        if entities["gene"] and entities["position"]:
            return "position"
        if entities["gene"]:
            return "gene_only"
        if entities["rsid"]:
            return "rsid"
        if entities["position"]:
            return "position_only"
        return "unknown"

    def _determine_intent(self, query_type: str) -> str:
        """Map a query_type to a human-readable intent label."""
        intents = {
            "gene_variant": "classification",
            "gene_only": "gene_search",
            "variant_only": "variant_search",
            "position": "position_search",
            "rsid": "rsid_search",
            "position_only": "position_search",
        }
        return intents.get(query_type, "unknown")

    def process(self, raw_query: str, message_log) -> AgentMessage:
        # --- Security step: sanitize before doing anything else ---
        try:
            cleaned = sanitize_query(raw_query)
        except InvalidVariantQuery:
            raise

        # --- NLP step: extract entities ---
        entities = self.extract_entities(cleaned)

        # --- NLP step: intent classification ---
        query_type = self._determine_query_type(entities)
        intent = self._determine_intent(query_type)

        # Detect if the query mentions an unsupported gene (e.g. TP53, KRAS)
        # right before the HGVS notation -- this should be rejected,
        # not silently treated as variant_only
        if query_type == "variant_only" and entities["hgvs"]:
            gene_before = _GENE_BEFORE_HGVS.search(cleaned)
            if gene_before:
                candidate = gene_before.group(1).upper()
                if candidate not in _ALLOWED_GENES:
                    raise InvalidVariantQuery(
                        f"Gene '{candidate}' not supported. This system only covers {_ALLOWED_GENES}."
                    )

        if query_type == "unknown":
            raise InvalidVariantQuery(
                "Could not understand the query. Try something like:\n"
                "  •  'Is BRCA1 NC_000017.10:g.41197778A>G pathogenic?'\n"
                "  •  'What variants are in BRCA1?'\n"
                "  •  'What is NC_000017.10:g.41201179C>T?'\n"
                "  •  'What variant is at position 41197778 in BRCA1?'"
            )

        # Build variant_id for the message
        if query_type == "gene_variant":
            gene = validate_gene(entities["gene"])
            variant_id = f"{gene}:{entities['hgvs']}"
        elif query_type == "variant_only":
            # Gene will be inferred by the Evidence Agent from the dataset
            variant_id = f"unknown:{entities['hgvs']}"
        elif query_type == "gene_only":
            gene = validate_gene(entities["gene"])
            variant_id = f"{gene}:*"
        elif query_type == "position":
            gene = validate_gene(entities["gene"])
            variant_id = f"{gene}:pos{entities['position']}"
        elif query_type == "rsid":
            variant_id = f"unknown:{entities['rsid']}"
        elif query_type == "position_only":
            variant_id = f"unknown:pos{entities['position']}"
        else:
            raise InvalidVariantQuery("Could not understand the query.")

        message = AgentMessage(
            variant_id=variant_id,
            stage="intake_complete",
            sender=self.name,
            receiver="evidence_retrieval_agent",
            message_type=MessageType.VARIANT_QUERY,
            payload={
                "gene": entities.get("gene"),
                "hgvs": entities.get("hgvs"),
                "position": entities.get("position"),
                "rsid": entities.get("rsid"),
                "query_type": query_type,
                "intent": intent,
                "raw_query": cleaned,
            },
        )
        # Encrypt payload before passing to next agent (security: encryption in transit)
        message.encrypt_payload()
        message_log.record(message)
        return message


if __name__ == "__main__":
    from protocol.message import MessageLog

    log = MessageLog()
    agent = IntakeAgent()

    test_queries = [
        "Is BRCA1 NC_000017.10:g.41197778A>G pathogenic?",
        "What variants are in BRCA1?",
        "What is NC_000017.10:g.41201179C>T?",
        "What variant is at position 41197778 in BRCA1?",
        "Tell me about rs123456",
    ]

    for q in test_queries:
        try:
            result = agent.process(q, log)
            payload = result.decrypt_payload()
            print(f"QUERY: {q}")
            print(f"  -> intent={payload['intent']}, type={payload['query_type']}, "
                  f"gene={payload['gene']}, hgvs={payload['hgvs']}, "
                  f"position={payload['position']}, rsid={payload['rsid']}")
            print()
        except InvalidVariantQuery as e:
            print(f"QUERY: {q}")
            print(f"  -> REJECTED: {e}")
            print()
