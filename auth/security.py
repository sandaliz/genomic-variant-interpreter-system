"""
auth/security.py

Password hashing (Argon2id) and JWT access tokens.

This module sits in front of the four-agent pipeline: it decides WHO a caller is
before any agent sees a query. It deliberately knows nothing about the database --
callers pass in values they have already loaded.

Token format
------------
Tokens are **sign-then-encrypt**, producing a 5-part compact JWE:

    header . encrypted_key . iv . ciphertext . tag

The plaintext is itself a signed JWS, so a token gets both properties:

  * integrity - the inner signature makes tampering detectable even to someone who
    holds the encryption key, and
  * confidentiality - the outer encryption means sub/username/role are not
    readable by anyone who captures the token.

PyJWT signs but cannot encrypt, so the envelope is built with jwcrypto while the
inner JWS comes from PyJWT. Decoding reverses that: decrypt to the JWS, then
verify it exactly as before.

Honest note: these claims are the caller's own identity and role, which the
caller already knows, so encryption is defence in depth rather than a fix for a
live disclosure hole. It earns its place by keeping tokens opaque in logs, browser
storage and any proxy capture along the way.

Environment:
    JWT_SECRET_KEY       REQUIRED. HMAC signing key. The process refuses to start
                         without it unless JWT_ALLOW_DEV_SECRET is explicitly
                         true, because the fallback is published in this file and
                         anyone holding it could mint an admin token.
    JWT_ENCRYPTION_KEY   REQUIRED (same escape hatch). AES key for the JWE
                         envelope. Deliberately NOT the signing key: reusing one
                         key for both signing and encryption is a recognised
                         anti-pattern, and it would turn a signing-key leak into
                         a decryption leak as well.
    JWT_ISSUER           "variantlens" by default. Stamped into every token.
    JWT_AUDIENCE         "variantlens-api" by default. Stamped in and checked on
                         the way back in, so a token minted for another service is
                         refused even though its signature is perfectly valid.
    JWT_ALGORITHM        default HS256
    JWT_EXPIRE_MINUTES   default 60
    JWT_ALLOW_DEV_SECRET default false. Test-suite and demo escape hatch only.
"""

import base64
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import jwt
from dotenv import load_dotenv
from jwcrypto import jwe, jwk
from pwdlib import PasswordHash

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

# Argon2id via the pwdlib recommended configuration. Plaintext passwords are
# never stored, logged or returned by the API.
_password_hash = PasswordHash.recommended()

JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
JWT_EXPIRE_MINUTES = int(os.getenv("JWT_EXPIRE_MINUTES", "60"))
JWT_ISSUER = os.getenv("JWT_ISSUER", "variantlens")
JWT_AUDIENCE = os.getenv("JWT_AUDIENCE", "variantlens-api")

_JWE_ALG = "dir"        # direct use of a shared symmetric key
_JWE_ENC = "A256GCM"    # AES-256-GCM authenticated encryption

# Fixed labels so a derived key is stable across restarts and machines.
_HKDF_SIGN_INFO = b"variantlens.jwt.sign.v1"
_HKDF_ENC_INFO = b"variantlens.jwe.enc.v1"

# Claims that must be present on every token we accept.
REQUIRED_CLAIMS = ["sub", "exp", "iat", "iss", "aud"]


class TokenError(Exception):
    """Raised when a token is missing, malformed, expired or wrongly signed."""


class ConfigurationError(RuntimeError):
    """Raised at import time when the security configuration is unusable."""


_MISSING_SECRETS = (
    "\nRefusing to start with the development fallback secret.\n\n"
    "    JWT_SECRET_KEY      signing key for access tokens\n"
    "    JWT_ENCRYPTION_KEY  AES key for the JWE envelope\n\n"
    "Set both in .env (see .env.example), or export\n"
    "    JWT_ALLOW_DEV_SECRET=true\n"
    "to run locally with the published fallback. Never set that in a deployment:\n"
    "anyone holding that value could mint an administrator token.\n"
)


def _truthy(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


try:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    # cryptography wants its own HashAlgorithm, not the hashlib equivalent.
    _HASH_ALGORITHM = hashes.SHA256()
except ImportError:  # pragma: no cover - cryptography is a hard dependency
    class HKDF:  # type: ignore[no-redef]
        """Fallback so the module still imports without cryptography."""

        def __init__(self, algorithm=None, length=32, salt=None, info=None):
            self._length = length
            self._info = info or b""

        def derive(self, key_material: bytes) -> bytes:
            return hashlib.sha256(key_material + self._info).digest()[: self._length]

    _HASH_ALGORITHM = None


def _derive_key(secret: str, info: bytes, length: int = 32) -> bytes:
    """
    Derive a fixed-length key from a configured secret (HKDF-SHA256).

    The .env value is a passphrase of whatever length the operator chose, but
    JWE 'dir' with A256GCM needs exactly 32 bytes. Deriving gives every
    deployment the same shape regardless of what was typed, while still keeping
    the signing and encryption keys distinct.
    """
    if _HASH_ALGORITHM is None:  # pragma: no cover
        return HKDF(length=length, salt=None, info=info).derive(secret.encode("utf-8"))
    return HKDF(
        algorithm=_HASH_ALGORITHM, length=length, salt=None, info=info
    ).derive(secret.encode("utf-8"))


_DEV_SIGNING_SECRET = "variantlens-development-secret-do-not-use-in-production"
_DEV_ENCRYPTION_SECRET = "variantlens-development-encryption-key-do-not-use"

_configured_signing = os.getenv("JWT_SECRET_KEY")
_configured_encryption = os.getenv("JWT_ENCRYPTION_KEY")

# Fail loudly rather than silently falling back. The previous code computed
# USING_DEV_SECRET and then never used it, so a deployment that forgot to set
# the key signed tokens with a string published in this repository.
if not (_configured_signing and _configured_encryption):
    if not _truthy("JWT_ALLOW_DEV_SECRET"):
        raise ConfigurationError(_MISSING_SECRETS)
    _configured_signing = _configured_signing or _DEV_SIGNING_SECRET
    _configured_encryption = _configured_encryption or _DEV_ENCRYPTION_SECRET

USING_DEV_SECRET = not (
    os.getenv("JWT_SECRET_KEY") and os.getenv("JWT_ENCRYPTION_KEY")
)
if USING_DEV_SECRET:  # pragma: no cover - only reachable on the escape hatch
    print(
        "WARNING: running with the published development secrets. "
        "Set JWT_SECRET_KEY and JWT_ENCRYPTION_KEY before deploying.",
        file=sys.stderr,
    )

JWT_SECRET_KEY = _configured_signing
JWT_ENCRYPTION_KEY = _configured_encryption

_SIGNING_KEY = _derive_key(JWT_SECRET_KEY, _HKDF_SIGN_INFO)
# jwk.JWK wants the key material base64url-encoded when passed as a mapping,
# not as raw bytes.
_ENC_KEY_BYTES = _derive_key(JWT_ENCRYPTION_KEY, _HKDF_ENC_INFO)
_ENCRYPTION_KEY = jwk.JWK(
    kty="oct",
    k=base64.urlsafe_b64encode(_ENC_KEY_BYTES).rstrip(b"=").decode("ascii"),
)


def hash_password(password: str) -> str:
    """Hash a plaintext password with Argon2id."""
    return _password_hash.hash(password)


# A valid Argon2id digest of a random password, used only as a timing shield:
# the login route verifies the supplied password against this when no account
# matches the email, so "no such account" costs the same Argon2id work as
# "wrong password" and response time cannot reveal which addresses exist.
_DUMMY_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4"
    "$c2VjdXJpdHktYXVkaXQtZHVtbXk"
    "$h4lrV0lJdPbKkVxCwFX+h8lPDMgZ7T7Ci4srbFJ8R7k"
)


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a plaintext password against its stored hash (constant time)."""
    try:
        return _password_hash.verify(password, password_hash)
    except Exception:
        # A malformed or legacy hash must read as wrong password, never a 500.
        return False


def verify_password_or_dummy(password: str, password_hash: str | None) -> bool:
    """Verify, or burn equal Argon2id work when there is no stored hash.

    The login route calls this with None for unknown emails so the two failure
    modes are indistinguishable by timing as well as by message.
    """
    if password_hash is None:
        _password_hash.verify(password, _DUMMY_PASSWORD_HASH)
        return False
    return verify_password(password, password_hash)


def _encrypt(plaintext: str) -> str:
    envelope = jwe.JWE(
        plaintext.encode("utf-8"),
        json.dumps({"alg": _JWE_ALG, "enc": _JWE_ENC, "cty": "JWT"}),
        recipient=_ENCRYPTION_KEY,
    )
    return envelope.serialize(compact=True)


def _decrypt(token: str) -> str:
    envelope = jwe.JWE()
    envelope.deserialize(token, _ENCRYPTION_KEY)
    return envelope.payload.decode("utf-8")


def create_access_token(user_id: int, username: str, role: str) -> str:
    """
    Mint a short-lived, signed and encrypted access token.

    Only identity and role go into the token -- never the email, the plan or any
    other sensitive value. Authorization details are re-read from the database on
    every request (see auth/dependencies.py), so nothing in here has to stay
    current after issuance.
    """
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "username": username,
        "role": role,
        "iat": now,
        "exp": now + timedelta(minutes=JWT_EXPIRE_MINUTES),
        "iss": JWT_ISSUER,
        "aud": JWT_AUDIENCE,
    }
    # Sign first, then encrypt the signature. A captured token is opaque, and a
    # tampered one still fails the inner signature after decryption.
    signed = jwt.encode(payload, _SIGNING_KEY, algorithm=JWT_ALGORITHM)
    return _encrypt(signed)


def decode_access_token(token: str) -> dict:
    """
    Decrypt and verify a token, raising TokenError on any problem.

    Only the encrypted form is accepted. A bare 3-part JWS is refused even when it
    is correctly signed, so the confidentiality property cannot be bypassed by
    simply omitting the envelope.
    """
    if not token or not isinstance(token, str):
        raise TokenError("Invalid or malformed token.")
    # 5-part compact JWE: header . encrypted_key . iv . ciphertext . tag
    if len(token.split(".")) != 5:
        raise TokenError("Token is not in the expected encrypted form.")
    try:
        signed = _decrypt(token)
    except Exception as exc:
        # Wrong key, truncated blob, mangled ciphertext -- indistinguishable to an
        # attacker, so they are not distinguished from each other here either.
        raise TokenError("Invalid or malformed token.") from exc
    try:
        return jwt.decode(
            signed,
            _SIGNING_KEY,
            algorithms=[JWT_ALGORITHM],
            audience=JWT_AUDIENCE,
            issuer=JWT_ISSUER,
            options={"require": REQUIRED_CLAIMS},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("Token has expired.") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError("Invalid or malformed token.") from exc
