"""
a2a/auth.py
===========
Simulated A2A Authentication Module
-------------------------------------
In the real A2A protocol, agents exchange cryptographically signed JWTs
(JSON Web Tokens) to prove identity before any task is dispatched.

This module simulates that workflow using:
  - A deterministic "fake JWT" string built from the agent_id + a random
    nonce + a base64-encoded pseudo-signature.
  - An in-memory token store that maps token strings to metadata including
    issue time and TTL so that expiry can be checked.
  - generate_token(agent_id)          -> token string
  - verify_token(token, agent_id)     -> bool
  - revoke_token(token)               -> bool
  - list_active_tokens()              -> list[dict]

No external dependencies – uses only hashlib, base64, time, uuid from stdlib.
"""

from __future__ import annotations

import base64
import hashlib
import time
import uuid
from typing import Dict, Optional

# ---------------------------------------------------------------------------
# Internal token store  { token_string -> metadata_dict }
# ---------------------------------------------------------------------------
_TOKEN_STORE: Dict[str, dict] = {}

# Default token lifetime in seconds (simulated – 1 hour)
_DEFAULT_TTL: int = 3600

# Secret "signing key" – in production this would be an RSA/EC private key
_SIGNING_SECRET: str = "travelmind-a2a-secret-key-2025"


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _make_header() -> str:
    """Return a base64url-encoded fake JWT header."""
    header = '{"alg":"HS256","typ":"JWT"}'
    return base64.urlsafe_b64encode(header.encode()).rstrip(b"=").decode()


def _make_payload(
    agent_id: str, nonce: str, issued_at: float, expires_at: float
) -> str:
    """Return a base64url-encoded fake JWT payload."""
    payload = (
        f'{{"sub":"{agent_id}","nonce":"{nonce}",'
        f'"iat":{int(issued_at)},"exp":{int(expires_at)},'
        f'"iss":"travelmind-auth-service"}}'
    )
    return base64.urlsafe_b64encode(payload.encode()).rstrip(b"=").decode()


def _make_signature(header_b64: str, payload_b64: str) -> str:
    """
    Return a base64url-encoded HMAC-SHA256-like signature.
    We use hashlib.sha256 keyed with our signing secret to simulate HMAC.
    """
    signing_input = f"{header_b64}.{payload_b64}"
    raw = hashlib.sha256((signing_input + _SIGNING_SECRET).encode()).digest()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_token(agent_id: str, ttl: int = _DEFAULT_TTL) -> str:
    """
    Generate a simulated JWT-like bearer token for an agent.

    Parameters
    ----------
    agent_id : str
        The unique identifier of the agent requesting a token.
    ttl : int
        Time-to-live in seconds (default 3600 = 1 hour).

    Returns
    -------
    str
        A dot-separated token string in the form  header.payload.signature
        that uniquely identifies this authentication session.

    Side-effects
    ------------
    Stores token metadata in the in-memory _TOKEN_STORE so that
    verify_token() can later validate it.
    """
    nonce = uuid.uuid4().hex
    issued_at = time.time()
    expires_at = issued_at + ttl

    header_b64 = _make_header()
    payload_b64 = _make_payload(agent_id, nonce, issued_at, expires_at)
    signature_b64 = _make_signature(header_b64, payload_b64)

    token = f"{header_b64}.{payload_b64}.{signature_b64}"

    _TOKEN_STORE[token] = {
        "agent_id": agent_id,
        "nonce": nonce,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "revoked": False,
    }

    return token


def verify_token(token: str, agent_id: str) -> bool:
    """
    Verify that a token is valid, unexpired, unrevoked, and belongs to agent_id.

    Parameters
    ----------
    token    : str   The token string previously returned by generate_token().
    agent_id : str   The agent_id the caller claims the token belongs to.

    Returns
    -------
    bool
        True  if the token is structurally valid, the signature checks out,
              the token has not expired, it has not been revoked, and the
              stored agent_id matches the supplied agent_id.
        False otherwise (any single failure returns False – no exceptions raised
              so that callers can handle auth failures gracefully).
    """
    if not token or not agent_id:
        return False

    # ---- 1. Structural check ----
    parts = token.split(".")
    if len(parts) != 3:
        return False

    header_b64, payload_b64, provided_sig = parts

    # ---- 2. Signature re-derivation ----
    expected_sig = _make_signature(header_b64, payload_b64)
    if provided_sig != expected_sig:
        return False

    # ---- 3. Registry lookup ----
    metadata = _TOKEN_STORE.get(token)
    if metadata is None:
        return False

    # ---- 4. Agent-id ownership check ----
    if metadata["agent_id"] != agent_id:
        return False

    # ---- 5. Revocation check ----
    if metadata.get("revoked", False):
        return False

    # ---- 6. Expiry check ----
    if time.time() > metadata["expires_at"]:
        return False

    return True


def revoke_token(token: str) -> bool:
    """
    Revoke a previously issued token so it can no longer be used.

    Returns True if the token was found and revoked, False if it was not found.
    """
    if token in _TOKEN_STORE:
        _TOKEN_STORE[token]["revoked"] = True
        return True
    return False


def get_token_info(token: str) -> Optional[dict]:
    """
    Return a copy of the metadata dictionary for a given token, or None
    if the token does not exist in the store.

    Useful for debugging and audit logging.
    """
    metadata = _TOKEN_STORE.get(token)
    if metadata is None:
        return None
    info = dict(metadata)
    info["token_preview"] = token[:20] + "..." if len(token) > 20 else token
    info["time_remaining_s"] = max(0.0, metadata["expires_at"] - time.time())
    info["is_valid"] = not metadata["revoked"] and time.time() <= metadata["expires_at"]
    return info


def list_active_tokens() -> list:
    """
    Return a list of metadata dicts for all tokens that are currently valid
    (not revoked, not expired).  Useful for admin / diagnostics.
    """
    now = time.time()
    active = []
    for token, meta in _TOKEN_STORE.items():
        if not meta["revoked"] and now <= meta["expires_at"]:
            entry = dict(meta)
            entry["token_preview"] = token[:20] + "..."
            entry["time_remaining_s"] = round(meta["expires_at"] - now, 1)
            active.append(entry)
    return active


def purge_expired_tokens() -> int:
    """
    Remove all expired tokens from the in-memory store to free memory.
    Returns the number of tokens that were purged.
    """
    now = time.time()
    expired_keys = [t for t, meta in _TOKEN_STORE.items() if now > meta["expires_at"]]
    for key in expired_keys:
        del _TOKEN_STORE[key]
    return len(expired_keys)


# ---------------------------------------------------------------------------
# Quick self-test (run this file directly: python -m a2a.auth)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("=== A2A Auth Module Self-Test ===\n")

    agent = "flight-agent-01"

    # Generate
    tok = generate_token(agent)
    print(f"Generated token (preview): {tok[:40]}...")

    # Verify – should pass
    result = verify_token(tok, agent)
    print(f"verify_token(correct agent)  -> {result}")  # True

    # Verify – wrong agent, should fail
    result2 = verify_token(tok, "wrong-agent-99")
    print(f"verify_token(wrong agent)    -> {result2}")  # False

    # Token info
    info = get_token_info(tok)
    print(
        f"Token info: agent_id={info['agent_id']}, "
        f"valid={info['is_valid']}, "
        f"ttl_remaining={info['time_remaining_s']:.1f}s"
    )

    # Revoke
    revoke_token(tok)
    result3 = verify_token(tok, agent)
    print(f"verify_token(after revoke)   -> {result3}")  # False

    print("\nSelf-test complete.")
