"""FindMyROM Cryptography & Token Utilities.

Strictly adheres to:
- 00 MASTER BLUEPRINT Section 3.1 (Token Prefix Contract)
- 00 MASTER BLUEPRINT Section 3.2 (Ed25519 Anti-tamper Payload Contract)
"""

from __future__ import annotations

import secrets
import string
from typing import Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

# Token prefixes defined in 00 Master Blueprint Section 3.1
ALLOWED_TOKEN_PREFIXES = {"fw", "bt", "cr", "tl"}
TOKEN_ALPHABET = string.ascii_letters + string.digits  # URL-safe alphanumeric


def generate_keypair() -> tuple[str, str]:
    """Generate an Ed25519 keypair.

    Returns:
        tuple[str, str]: (private_key_hex, public_key_hex)
        Each is a lowercase hex string (64 characters / 32 bytes).
    """
    private_key = ed25519.Ed25519PrivateKey.generate()
    private_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return private_bytes.hex().lower(), public_bytes.hex().lower()


def generate_public_token(
    prefix: Literal["fw", "bt", "cr", "tl"] | str,
    random_len: int = 12,
) -> str:
    """Generate a public_token adhering to the prefix and length constraints.

    Prefix must be one of: 'fw', 'bt', 'cr', 'tl' (or 'fw_', 'bt_', etc.).
    Total length will be len(prefix) + 1 + random_len (default 3 + 12 = 15 chars).
    Must strictly be between 13 and 16 characters.

    Args:
        prefix: Token prefix identifier.
        random_len: Length of random suffix (10 to 12 chars).

    Returns:
        Formatted public_token, e.g., 'bt_3nM8vB2xQ1zR'.
    """
    clean_prefix = prefix.rstrip("_").lower()
    if clean_prefix not in ALLOWED_TOKEN_PREFIXES:
        raise ValueError(
            f"Invalid prefix '{prefix}'. Must be one of {sorted(ALLOWED_TOKEN_PREFIXES)}"
        )

    if not (10 <= random_len <= 12):
        raise ValueError(f"random_len must be between 10 and 12, got {random_len}")

    rand_chars = "".join(secrets.choice(TOKEN_ALPHABET) for _ in range(random_len))
    token = f"{clean_prefix}_{rand_chars}"

    if not (13 <= len(token) <= 16):
        raise ValueError(
            f"Resulting token length {len(token)} out of contract bounds (13-16)"
        )

    return token


def build_boot_payload(
    public_token: str,
    sha256_hash: str,
    gh_private_repo: str,
    gh_asset_id: int | str,
) -> str:
    """Construct Ed25519 signing payload for extracted_boot_images.

    Format: v1|bt|{public_token}|{sha256_hash}|{gh_private_repo}|{gh_asset_id}
    """
    return f"v1|bt|{public_token}|{sha256_hash.lower()}|{gh_private_repo}|{gh_asset_id}"


def build_firmware_payload(public_token: str, fast_cdn_url: str) -> str:
    """Construct Ed25519 signing payload for stock_firmwares.

    Format: v1|fw|{public_token}|{fast_cdn_url}
    """
    return f"v1|fw|{public_token}|{fast_cdn_url}"


def build_custom_resource_payload(
    public_token: str, zone_type: str, target_post_url: str
) -> str:
    """Construct Ed25519 signing payload for custom_resources.

    Format: v1|cr|{public_token}|{zone_type}|{target_post_url}
    """
    return f"v1|cr|{public_token}|{zone_type}|{target_post_url}"


def build_tool_payload(public_token: str, target_redirect_url: str) -> str:
    """Construct Ed25519 signing payload for brand_universal_tools.

    Format: v1|tl|{public_token}|{target_redirect_url}
    """
    return f"v1|tl|{public_token}|{target_redirect_url}"


def sign_payload(payload_str: str, private_key_hex: str) -> str:
    """Sign UTF-8 string payload with Ed25519 private key.

    Returns:
        128-character lowercase hex string (64 bytes signature).
    """
    clean_hex = private_key_hex.strip().lower()
    if len(clean_hex) != 64:
        raise ValueError(
            f"Ed25519 private key hex must be 64 characters, got {len(clean_hex)}"
        )

    private_bytes = bytes.fromhex(clean_hex)
    private_key = ed25519.Ed25519PrivateKey.from_private_bytes(private_bytes)

    data = payload_str.encode("utf-8")
    signature = private_key.sign(data)
    return signature.hex().lower()


def verify_signature(
    payload_str: str, signature_hex: str, public_key_hex: str
) -> bool:
    """Verify Ed25519 signature against payload string and public key hex."""
    try:
        clean_pub = public_key_hex.strip().lower()
        clean_sig = signature_hex.strip().lower()

        if len(clean_pub) != 64 or len(clean_sig) != 128:
            return False

        public_bytes = bytes.fromhex(clean_pub)
        signature_bytes = bytes.fromhex(clean_sig)
        public_key = ed25519.Ed25519PublicKey.from_public_bytes(public_bytes)

        public_key.verify(signature_bytes, payload_str.encode("utf-8"))
        return True
    except (InvalidSignature, ValueError):
        return False


def sign_boot_image(
    public_token: str,
    sha256_hash: str,
    gh_private_repo: str,
    gh_asset_id: int | str,
    private_key_hex: str,
) -> str:
    """Convenience helper to build and sign boot image payload."""
    payload = build_boot_payload(
        public_token, sha256_hash, gh_private_repo, gh_asset_id
    )
    return sign_payload(payload, private_key_hex)


def sign_firmware(
    public_token: str,
    fast_cdn_url: str,
    private_key_hex: str,
) -> str:
    """Convenience helper to build and sign firmware payload."""
    payload = build_firmware_payload(public_token, fast_cdn_url)
    return sign_payload(payload, private_key_hex)


def sign_custom_resource(
    public_token: str,
    zone_type: str,
    target_post_url: str,
    private_key_hex: str,
) -> str:
    """Convenience helper to build and sign custom resource payload."""
    payload = build_custom_resource_payload(public_token, zone_type, target_post_url)
    return sign_payload(payload, private_key_hex)


def sign_tool(
    public_token: str,
    target_redirect_url: str,
    private_key_hex: str,
) -> str:
    """Convenience helper to build and sign universal tool payload."""
    payload = build_tool_payload(public_token, target_redirect_url)
    return sign_payload(payload, private_key_hex)
