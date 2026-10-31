"""Unit tests for fmr_core.crypto module."""

import re
import pytest
from fmr_core.crypto import (
    ALLOWED_TOKEN_PREFIXES,
    build_boot_payload,
    build_custom_resource_payload,
    build_firmware_payload,
    build_tool_payload,
    generate_keypair,
    generate_public_token,
    sign_boot_image,
    sign_custom_resource,
    sign_firmware,
    sign_payload,
    sign_tool,
    verify_signature,
)


def test_generate_keypair():
    priv_hex, pub_hex = generate_keypair()
    assert isinstance(priv_hex, str)
    assert isinstance(pub_hex, str)
    assert len(priv_hex) == 64
    assert len(pub_hex) == 64
    # Ensure hexadecimal
    int(priv_hex, 16)
    int(pub_hex, 16)


def test_generate_public_token_valid():
    for prefix in ALLOWED_TOKEN_PREFIXES:
        token = generate_public_token(prefix)
        assert token.startswith(f"{prefix}_")
        assert 13 <= len(token) <= 16
        # Matches contract: prefix + '_' + alphanumeric
        assert re.match(r"^[a-z]{2}_[a-zA-Z0-9]{10,12}$", token)


def test_generate_public_token_with_trailing_underscore():
    token = generate_public_token("bt_")
    assert token.startswith("bt_")
    assert 13 <= len(token) <= 16


def test_generate_public_token_invalid_prefix():
    with pytest.raises(ValueError, match="Invalid prefix"):
        generate_public_token("invalid")


def test_generate_public_token_invalid_length():
    with pytest.raises(ValueError, match="random_len must be between"):
        generate_public_token("bt", random_len=5)

    with pytest.raises(ValueError, match="random_len must be between"):
        generate_public_token("bt", random_len=20)


def test_payload_builders():
    boot = build_boot_payload("bt_123", "ABCDEF", "user/repo", 999)
    assert boot == "v1|bt|bt_123|abcdef|user/repo|999"

    fw = build_firmware_payload("fw_123", "https://fast.cdn/rom.zip")
    assert fw == "v1|fw|fw_123|https://fast.cdn/rom.zip"

    cr = build_custom_resource_payload("cr_123", "free_rom", "https://post.url")
    assert cr == "v1|cr|cr_123|free_rom|https://post.url"

    tl = build_tool_payload("tl_123", "https://tool.url")
    assert tl == "v1|tl|tl_123|https://tool.url"


def test_ed25519_sign_and_verify():
    priv_hex, pub_hex = generate_keypair()
    payload = "v1|bt|bt_3nM8vB2xQ1zR|e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855|user/repo|12345"

    sig_hex = sign_payload(payload, priv_hex)
    assert len(sig_hex) == 128
    int(sig_hex, 16)

    # Valid
    assert verify_signature(payload, sig_hex, pub_hex) is True

    # Tampered payload
    tampered_payload = payload + "_modified"
    assert verify_signature(tampered_payload, sig_hex, pub_hex) is False

    # Tampered signature
    tampered_sig = ("00" if sig_hex[:2] != "00" else "ff") + sig_hex[2:]
    assert verify_signature(payload, tampered_sig, pub_hex) is False

    # Wrong public key
    _, wrong_pub = generate_keypair()
    assert verify_signature(payload, sig_hex, wrong_pub) is False


def test_convenience_signers():
    priv_hex, pub_hex = generate_keypair()

    boot_sig = sign_boot_image("bt_123", "aabbcc", "owner/repo", 123, priv_hex)
    assert verify_signature("v1|bt|bt_123|aabbcc|owner/repo|123", boot_sig, pub_hex) is True

    fw_sig = sign_firmware("fw_123", "https://cdn.url", priv_hex)
    assert verify_signature("v1|fw|fw_123|https://cdn.url", fw_sig, pub_hex) is True

    cr_sig = sign_custom_resource("cr_123", "free_rom", "https://post.url", priv_hex)
    assert verify_signature("v1|cr|cr_123|free_rom|https://post.url", cr_sig, pub_hex) is True

    tl_sig = sign_tool("tl_123", "https://tool.url", priv_hex)
    assert verify_signature("v1|tl|tl_123|https://tool.url", tl_sig, pub_hex) is True
