"""
protocol/security.py

Security features:
  - Input sanitization (reject malformed variant queries)
  - Basic authentication (only registered callers can query the system)
  - Simple symmetric encryption for data in transit between agents

The implementation is intentionally lightweight to keep the system
portable while still covering the core security concerns for a
clinical decision-support prototype.
"""

import hashlib
import re
from cryptography.fernet import Fernet


# ---------- Input sanitization ----------

_ALLOWED_GENES = {"BRCA1", "BRCA2"}

# Explicit prompt-injection / jailbreak defenses. These run BEFORE any variant
# query reaches the intake NLP or any LLM agent, so instructions smuggled in
# as "variant questions" can never become system instructions. All patterns
# are matched case-insensitively on the raw query string.
_PROMPT_INJECTION_PATTERNS = [
    # Direct system-prompt override attempts
    r"ignore\s+(all\s+)?previous\s+instructions",
    r"ignore\s+(the\s+)?(system|above|prior)\s+(prompt|instructions|rules)",
    r"disregard\s+(all\s+)?(previous|prior|above)\s+(instructions|prompts?|rules)",
    r"forget\s+(all\s+)?(previous|prior|above)\s+(instructions|prompts?|rules)",
    r"override\s+(the\s+)?(system|previous|prior)\s+(prompt|instructions|rules)",
    r"bypass\s+(the\s+)?(system|security|safety)\s+(prompt|instructions|rules|checks?)",
    # System-prompt exfiltration attempts
    r"reveal\s+(your\s+|the\s+)?system\s+prompt",
    r"show\s+(me\s+)?(your\s+|the\s+)?system\s+prompt",
    r"print\s+(your\s+|the\s+)?system\s+instructions",
    r"what\s+(is|are)\s+your\s+(system\s+)?instructions",
    r"dump\s+(your\s+|the\s+)?(system|internal)\s+(prompt|instructions|config)",
    # Role / identity hijacking
    r"you\s+are\s+now\s+(?!a\s+clinical)",  # "you are now DAN / ..." but not legit text
    r"act\s+as\s+(if\s+you\s+were\s+)?(?!a\s+clinical)",
    r"pretend\s+(you\s+are|to\s+be)\s+(?!a\s+clinical)",
    r"new\s+role\s*:",
    r"developer\s+mode",
    r"jailbreak",
    r"do\s+anything\s+now",
    # Instruction smuggling via framing
    r"hypothetically,?\s+if\s+you\s+had\s+no\s+(rules|restrictions|safety)",
    r"as\s+an?\s+unrestricted\s+ai",
]
_COMPILED_PROMPT_INJECTION = [re.compile(p, re.IGNORECASE) for p in _PROMPT_INJECTION_PATTERNS]


class InvalidVariantQuery(Exception):
    pass


def is_prompt_injection(raw_query: str) -> bool:
    """Return True if the query matches a known prompt-injection pattern."""
    if not isinstance(raw_query, str):
        return False
    return any(rx.search(raw_query) for rx in _COMPILED_PROMPT_INJECTION)


def sanitize_query(raw_query: str) -> str:
    """
    Cleans and validates a raw user query before it enters the pipeline.
    Blocks obviously malicious or malformed input.
    """
    if not isinstance(raw_query, str):
        raise InvalidVariantQuery("Query must be a string.")

    cleaned = raw_query.strip()

    if not cleaned:
        raise InvalidVariantQuery("Query cannot be empty.")

    if len(cleaned) > 200:
        raise InvalidVariantQuery("Query too long -- possible malicious input.")

    # Block obvious injection attempts (SQL/script-like patterns)
    danger_patterns = ["--", ";", "<script", "DROP ", "SELECT ", "../"]
    if any(p.lower() in cleaned.lower() for p in danger_patterns):
        raise InvalidVariantQuery("Query contains disallowed characters/patterns.")

    # Block explicit prompt-injection / jailbreak attempts before any NLP or
    # LLM sees the query (system-prompt overrides, exfiltration, role hijack).
    if is_prompt_injection(cleaned):
        raise InvalidVariantQuery(
            "Query blocked: prompt-injection pattern detected. "
            "System instructions cannot be overridden via the query box."
        )

    return cleaned


def validate_gene(gene: str) -> str:
    gene = gene.upper().strip()
    if gene not in _ALLOWED_GENES:
        raise InvalidVariantQuery(
            f"Gene '{gene}' not supported. This system only covers {_ALLOWED_GENES}."
        )
    return gene


# ---------- Basic authentication ----------

# Demo-only: in a real system this would be OAuth/JWT against a user DB.
_REGISTERED_API_KEYS = {
    hashlib.sha256(b"demo-clinician-key").hexdigest(): "clinician_demo_user",
}


class AuthenticationError(Exception):
    pass


def authenticate(api_key: str) -> str:
    """Returns the username if the key is valid, else raises."""
    hashed = hashlib.sha256(api_key.encode()).hexdigest()
    if hashed not in _REGISTERED_API_KEYS:
        raise AuthenticationError("Invalid or unregistered API key.")
    return _REGISTERED_API_KEYS[hashed]


# ---------- Encryption for data "in transit" between agents ----------

class MessageEncryptor:
    """
    Wraps Fernet symmetric encryption so payloads are encrypted
    in transit between agents, protecting patient data.
    """

    def __init__(self, key: bytes = None):
        self.key = key or Fernet.generate_key()
        self._fernet = Fernet(self.key)

    def encrypt(self, plaintext: str) -> bytes:
        return self._fernet.encrypt(plaintext.encode())

    def decrypt(self, token: bytes) -> str:
        return self._fernet.decrypt(token).decode()
