"""
protocol/message.py

Defines the shared message format all agents use to talk to each other.
This is the agent communication protocol for the system.

The protocol follows a standardized schema (similar to MCP / A2A standards):

    {
        "message_id":    unique ID for this message
        "trace_id":      end-to-end trace ID for the whole pipeline run
        "sender":        which agent produced this message
        "receiver":      which agent is the intended recipient
        "message_type":  semantic type (VARIANT_QUERY, EVIDENCE_RESULT, etc.)
        "variant_id":    e.g. "BRCA1:NC_000017.10:g.41197778A>G"
        "stage":         pipeline stage (intake_complete, evidence_retrieved, ...)
        "timestamp":     ISO-8601 UTC timestamp
        "encrypted":     whether the payload is encrypted in transit
        "payload":       dict with clinical evidence / results (may be encrypted)
    }

Agents never exchange arbitrary Python objects -- every interaction uses
this standardized AgentMessage schema. In a full deployment this would
travel over MCP or REST between separate processes. Here agents run in
the same process but STILL communicate only through this protocol, never
by reaching into each other's internal state directly.

Security: payloads are encrypted in transit between agents using the
MessageEncryptor from protocol/security.py.
"""

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, Optional
import json
import uuid

from protocol.security import MessageEncryptor

# Custom JSON encoder that handles numpy types (np.int64, np.float64, etc.)
# from pandas DataFrames so payloads can always be serialized/encrypted.
try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False


class NumpyJSONEncoder(json.JSONEncoder):
    """JSON encoder that converts numpy scalar/array types to native Python."""

    def default(self, obj):
        if HAS_NUMPY:
            if isinstance(obj, np.integer):
                return int(obj)
            if isinstance(obj, np.floating):
                return float(obj)
            if isinstance(obj, np.ndarray):
                return obj.tolist()
        return super().default(obj)


# Shared encryptor for agent-to-agent message payloads.
_ENCRYPTOR = MessageEncryptor()


# Message type constants (semantic protocol)
class MessageType:
    """Semantic message types understood by all agents."""

    VARIANT_QUERY = "VARIANT_QUERY"
    QUERY_ACK = "QUERY_ACK"
    EVIDENCE_RESULT = "EVIDENCE_RESULT"
    EVIDENCE_ERROR = "EVIDENCE_ERROR"
    CLASSIFICATION_RESULT = "CLASSIFICATION_RESULT"
    CLASSIFICATION_ERROR = "CLASSIFICATION_ERROR"
    REPORT_RESULT = "REPORT_RESULT"
    REPORT_ERROR = "REPORT_ERROR"
    SYSTEM_LOG = "SYSTEM_LOG"


@dataclass
class AgentMessage:
    """A single message exchanged between agents using the shared protocol."""

    variant_id: str                     # e.g. "BRCA1:c.68_69delAG"
    stage: str                          # e.g. "intake_complete", "evidence_retrieved"
    sender: str                         # which agent produced this message

    # --- Formal protocol fields ---
    receiver: str = ""                   # intended recipient agent
    message_type: str = MessageType.SYSTEM_LOG   # semantic message type
    trace_id: str = field(default_factory=lambda: str(uuid.uuid4())[:12])

    payload: Dict[str, Any] = field(default_factory=dict)
    message_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    encrypted: bool = False             # True if payload was encrypted in transit

    def to_json(self) -> str:
        """Serialize the message (metadata + encrypted payload if any)."""
        return json.dumps(asdict(self), indent=2, cls=NumpyJSONEncoder)

    def __repr__(self) -> str:
        return (
            f"<AgentMessage id={self.message_id} type={self.message_type} "
            f"stage={self.stage} from={self.sender} to={self.receiver or '*'} "
            f"variant={self.variant_id} encrypted={self.encrypted}>"
        )

    # ------------------------------------------------------------------
    # Protocol helpers
    # ------------------------------------------------------------------
    def to(self, receiver: str) -> "AgentMessage":
        """Set the intended recipient. Returns self for chaining."""
        self.receiver = receiver
        return self

    def with_type(self, message_type: str) -> "AgentMessage":
        """Set the semantic message type. Returns self for chaining."""
        self.message_type = message_type
        return self

    def with_trace(self, trace_id: str) -> "AgentMessage":
        """Set the end-to-end trace ID. Returns self for chaining."""
        self.trace_id = trace_id
        return self

    # ------------------------------------------------------------------
    # Encryption helpers -- payload is encrypted when sent, decrypted when read
    # ------------------------------------------------------------------
    def encrypt_payload(self) -> "AgentMessage":
        """Encrypt the payload in place. Returns self for chaining."""
        if not self.encrypted:
            plaintext = json.dumps(self.payload, cls=NumpyJSONEncoder)
            self.payload = {"__encrypted__": _ENCRYPTOR.encrypt(plaintext).decode()}
            self.encrypted = True
        return self

    def decrypt_payload(self) -> Dict[str, Any]:
        """Decrypt the payload if it was encrypted. Returns the plain dict."""
        if self.encrypted and "__encrypted__" in self.payload:
            token = self.payload["__encrypted__"].encode()
            plaintext = _ENCRYPTOR.decrypt(token)
            return json.loads(plaintext)
        return self.payload


class MessageLog:
    """
    Keeps an ordered audit trail of every message exchanged for
    security auditing and end-to-end traceability of agent decisions.
    """

    def __init__(self):
        self._log = []

    def record(self, message: AgentMessage):
        self._log.append(message)

    def history_for(self, variant_id: str):
        return [m for m in self._log if m.variant_id == variant_id]

    def trail_for(self, trace_id: str):
        """Get all messages belonging to a specific trace (pipeline run)."""
        return [m for m in self._log if m.trace_id == trace_id]

    def print_trail(self, variant_id: str):
        print(f"\n--- Message trail for {variant_id} ---")
        for m in self.history_for(variant_id):
            print(f"[{m.timestamp}] {m.sender} -> {m.receiver or '*'} | "
                  f"type={m.message_type} stage={m.stage} "
                  f"(encrypted={m.encrypted}) trace={m.trace_id}")
