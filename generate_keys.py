#!/usr/bin/env python3
"""Cryptographic Keypair & Secret Generator for FindMyROM.

Generates:
1. Ed25519 private and public keys (64-char lowercase hex).
2. 32-byte cryptographically secure HMAC secret for download tickets.

Features:
- Restricted file permissions (0600) on generated files.
- Masked stdout output unless explicitly requested via --show-private.
- Overwrite protection unless --force is provided.
- Operates smoothly both in monorepo root and in standalone Repo-Runner checkout.
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys

RUNNER_DIR = os.path.dirname(os.path.abspath(__file__))
if RUNNER_DIR not in sys.path:
    sys.path.insert(0, RUNNER_DIR)

parent_dir = os.path.dirname(RUNNER_DIR)
candidate_web_dir = os.path.join(parent_dir, "findmyrom-web")
WEB_DIR = candidate_web_dir if os.path.isdir(candidate_web_dir) else None

from fmr_core.crypto import generate_keypair


def generate_dev_credentials() -> tuple[str, str, str]:
    """Generate (private_key_hex, public_key_hex, hmac_secret_hex)."""
    priv_hex, pub_hex = generate_keypair()
    hmac_secret = secrets.token_hex(32)
    return priv_hex, pub_hex, hmac_secret


def write_dev_files(priv_hex: str, pub_hex: str, hmac_secret: str, force: bool = False) -> None:
    runner_env_path = os.path.join(RUNNER_DIR, ".env")
    web_dev_vars_path = os.path.join(WEB_DIR, ".dev.vars") if WEB_DIR else None

    paths_to_check = [runner_env_path]
    if web_dev_vars_path:
        paths_to_check.append(web_dev_vars_path)

    for path in paths_to_check:
        if os.path.exists(path) and not force:
            raise FileExistsError(
                f"Configuration file already exists at '{path}'. "
                "Pass '--force' to overwrite existing development credentials."
            )

    runner_env_content = f"""# Auto-generated local development configuration (git-ignored)
SIGN_PRIVATE_KEY_HEX={priv_hex}
LOCAL_SQLITE_PATH=dev_local.db
"""

    def write_secure_file(file_path: str, content: str) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        fd = os.open(file_path, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.chmod(file_path, 0o600)

    write_secure_file(runner_env_path, runner_env_content)
    print(f"[+] Wrote private key to: {runner_env_path} (mode 0600)")

    if web_dev_vars_path:
        web_dev_vars_content = f"""# Auto-generated local development configuration for wrangler dev (git-ignored)
SIGN_PUBLIC_KEY_HEX={pub_hex}
TICKET_HMAC_SECRET={hmac_secret}
"""
        write_secure_file(web_dev_vars_path, web_dev_vars_content)
        print(f"[+] Wrote public key & HMAC secret to: {web_dev_vars_path} (mode 0600)")
    else:
        print("[i] findmyrom-web directory not present; skipped .dev.vars (standalone runner mode).")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate FindMyROM keys and secrets.")
    parser.add_argument(
        "--write-dev-files",
        action="store_true",
        help="Write generated credentials directly to findmyrom-runner/.env (and .dev.vars if web exists)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing .env and .dev.vars files if they already exist",
    )
    parser.add_argument(
        "--show-private",
        action="store_true",
        help="Print private key and HMAC secret to stdout (use with caution)",
    )
    args = parser.parse_args()

    priv_hex, pub_hex, hmac_secret = generate_dev_credentials()

    print("=" * 65)
    print("         FindMyROM Cryptographic Key Generation")
    print("=" * 65)
    if args.show_private:
        print(f"Ed25519 Private Key (64 hex): {priv_hex}")
    else:
        print("Ed25519 Private Key (64 hex): [HIDDEN - pass --show-private to display in console]")
    print(f"Ed25519 Public Key  (64 hex): {pub_hex}")
    if args.show_private:
        print(f"HMAC-SHA256 Secret (64 hex):  {hmac_secret}")
    else:
        print("HMAC-SHA256 Secret (64 hex):  [HIDDEN - pass --show-private to display in console]")
    print("=" * 65)

    if args.write_dev_files:
        write_dev_files(priv_hex, pub_hex, hmac_secret, force=args.force)
        print("\n[✓] Local development environment successfully initialized!")
    else:
        print("\nTip: Pass '--write-dev-files' to automatically configure local development files.")


if __name__ == "__main__":
    main()
