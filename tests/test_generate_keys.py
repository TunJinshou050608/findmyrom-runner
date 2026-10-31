"""Unit tests for generate_keys.py."""

import os
import stat
import subprocess
import sys
import tempfile
import pytest

# Ensure runner root is in sys.path
RUNNER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT_DIR = os.path.dirname(RUNNER_DIR)
if RUNNER_DIR not in sys.path:
    sys.path.insert(0, RUNNER_DIR)

import generate_keys
from fmr_core.crypto import verify_signature, sign_payload


def test_generate_dev_credentials():
    priv, pub, secret = generate_keys.generate_dev_credentials()
    assert len(priv) == 64
    assert len(pub) == 64
    assert len(secret) == 64
    assert all(c in "0123456789abcdef" for c in priv)
    assert all(c in "0123456789abcdef" for c in pub)
    assert all(c in "0123456789abcdef" for c in secret)

    # Verify keypair validity
    test_msg = "hello test"
    sig = sign_payload(test_msg, priv)
    assert verify_signature(test_msg, sig, pub) is True


def test_write_dev_files_security_and_permissions(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        runner_dir = os.path.join(td, "findmyrom-runner")
        web_dir = os.path.join(td, "findmyrom-web")
        os.makedirs(runner_dir)
        os.makedirs(web_dir)

        monkeypatch.setattr(generate_keys, "RUNNER_DIR", runner_dir)
        monkeypatch.setattr(generate_keys, "WEB_DIR", web_dir)

        priv, pub, secret = generate_keys.generate_dev_credentials()

        # 1. First write should succeed and set mode 0600
        generate_keys.write_dev_files(priv, pub, secret, force=False)

        runner_env = os.path.join(runner_dir, ".env")
        web_dev_vars = os.path.join(web_dir, ".dev.vars")

        assert os.path.exists(runner_env)
        assert os.path.exists(web_dev_vars)

        runner_mode = stat.S_IMODE(os.stat(runner_env).st_mode)
        web_mode = stat.S_IMODE(os.stat(web_dev_vars).st_mode)
        assert runner_mode == 0o600, f"Expected 0600, got {oct(runner_mode)}"
        assert web_mode == 0o600, f"Expected 0600, got {oct(web_mode)}"

        with open(runner_env, "r") as f:
            content = f.read()
            assert f"SIGN_PRIVATE_KEY_HEX={priv}" in content

        with open(web_dev_vars, "r") as f:
            content = f.read()
            assert f"SIGN_PUBLIC_KEY_HEX={pub}" in content
            assert f"TICKET_HMAC_SECRET={secret}" in content

        # 2. Overwriting without force should fail
        with pytest.raises(FileExistsError):
            generate_keys.write_dev_files(priv, pub, secret, force=False)

        # 3. Overwriting with force should succeed
        new_priv, new_pub, new_secret = generate_keys.generate_dev_credentials()
        generate_keys.write_dev_files(new_priv, new_pub, new_secret, force=True)

        with open(runner_env, "r") as f:
            new_content = f.read()
            assert f"SIGN_PRIVATE_KEY_HEX={new_priv}" in new_content


def test_write_dev_files_standalone_runner(monkeypatch):
    """Verify write_dev_files functions properly when WEB_DIR is None (standalone runner)."""
    with tempfile.TemporaryDirectory() as td:
        runner_dir = os.path.join(td, "findmyrom-runner")
        os.makedirs(runner_dir)

        monkeypatch.setattr(generate_keys, "RUNNER_DIR", runner_dir)
        monkeypatch.setattr(generate_keys, "WEB_DIR", None)

        priv, pub, secret = generate_keys.generate_dev_credentials()
        generate_keys.write_dev_files(priv, pub, secret, force=False)

        runner_env = os.path.join(runner_dir, ".env")
        assert os.path.exists(runner_env)
        assert stat.S_IMODE(os.stat(runner_env).st_mode) == 0o600


def test_generate_keys_cli_wrapper():
    """Verify CLI interface of generate_keys in runner and scripts wrapper."""
    script_runner = os.path.join(RUNNER_DIR, "generate_keys.py")
    script_wrapper = os.path.join(ROOT_DIR, "scripts", "generate_keys.py")

    scripts_to_test = [script_runner]
    if os.path.exists(script_wrapper):
        scripts_to_test.append(script_wrapper)

    for script_path in scripts_to_test:
        # A. Default run (masks private key)
        proc_default = subprocess.run([sys.executable, script_path], capture_output=True, text=True)
        assert proc_default.returncode == 0
        assert "Ed25519 Public Key" in proc_default.stdout
        assert "[HIDDEN" in proc_default.stdout

        # B. Show private key flag
        proc_show = subprocess.run([sys.executable, script_path, "--show-private"], capture_output=True, text=True)
        assert proc_show.returncode == 0
        assert "Ed25519 Private Key (64 hex):" in proc_show.stdout
        assert "[HIDDEN" not in proc_show.stdout
