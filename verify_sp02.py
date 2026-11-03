#!/usr/bin/env python3
"""Self-contained, offline SP02 acceptance suite; run from Repo-Runner."""

import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parent


def run(args, env=None, expected=0):
    result = subprocess.run(args, cwd=ROOT, env=env, text=True, capture_output=True)
    print(result.stdout, end="")
    if result.returncode != expected:
        raise RuntimeError(
            f"Command failed: {args}; expected {expected}, got {result.returncode}\n{result.stderr}"
        )
    return result.stdout


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    print(sys.version)
    run([sys.executable, "-m", "pytest", "--version"])
    for base in ["fmr_ingest_stock", "tests"]:
        for path in sorted(ROOT.joinpath(base).rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                print(path.relative_to(ROOT))
    output = run([sys.executable, "-m", "pytest", "-v"])
    require(re.search(r"\b[1-9]\d*\s+skipped\b", output, re.I) is None, "Tests must run without skips")
    with tempfile.TemporaryDirectory(prefix="fmr-sp02-") as temp:
        db_path = str(Path(temp) / "dod.db")
        env = {
            **os.environ,
            "LOCAL_SQLITE_PATH": db_path,
            "SIGN_PRIVATE_KEY_HEX": "11" * 32,
        }
        args = [
            sys.executable,
            "-m",
            "fmr_ingest_stock.cli",
            "--mode",
            "all",
            "--devices-source",
            "tests/fixtures/sp02_devices_sample.yaml",
            "--firmwares-source",
            "tests/fixtures/sp02_firmwares_sample.yml",
        ]
        first = json.loads(run(args, env).splitlines()[-1])
        for key, value in dict(
            mode="all",
            brands_upserted=13,
            devices_upserted=4,
            devices_rejected=1,
            inserted_or_updated=5,
            firmwares_rejected=4,
            pending_count=3,
            skipped_count=2,
            status="completed",
        ).items():
            require(first.get(key) == value, f"CLI result mismatch: {key}")
        second = json.loads(run(args + ["--incremental"], env).splitlines()[-1])
        require(
            second["status"] == "skipped_unchanged"
            and second["inserted_or_updated"] == 0,
            "Incremental skip failed",
        )
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT public_token,fast_cdn_url,ed25519_signature,extract_status FROM stock_firmwares"
            ).fetchall()
        require(len(rows) == 5, "Expected five firmware rows")
        require(
            sum(r[3] == "pending" for r in rows) == 3, "Expected three pending rows"
        )
        require(sum(r[3] == "skipped" for r in rows) == 2, "Expected two skipped rows")
        pub = Ed25519PrivateKey.from_private_bytes(
            bytes.fromhex("11" * 32)
        ).public_key()
        for token, url, sig, _ in rows:
            require(re.fullmatch(r"fw_[A-Za-z0-9_-]{10,13}", token), "Bad token")
            require(
                "bkt-sgp-miui-ota-update-alisgp.oss-ap-southeast-1.aliyuncs.com" in url,
                "Bad CDN",
            )
            pub.verify(bytes.fromhex(sig), f"v1|fw|{token}|{url}".encode())
        print("CRYPTO_AND_DB_VERIFIED_OK")
        for extra in [["--batch-size", "49"], ["--batch-size", "501"]]:
            run(args + extra, env, expected=2)
        run(
            [sys.executable, "-m", "fmr_ingest_stock.cli", "--mode", "all"],
            env,
            expected=2,
        )
        # Invalid credentials must fail before even creating a database file.
        for key in ["", "not-a-key"]:
            missing = str(Path(temp) / "must-not-exist.db")
            run(
                args,
                {**env, "SIGN_PRIVATE_KEY_HEX": key, "LOCAL_SQLITE_PATH": missing},
                expected=1,
            )
            require(not Path(missing).exists(), "Invalid credentials caused DB writes")
    for path in ROOT.joinpath("fmr_ingest_stock").rglob("*.py"):
        source = path.read_text()
        require(
            "yaml.load(" not in source and "yaml.unsafe_load(" not in source,
            "Unsafe YAML loader",
        )
    print("ALL SP02 DEFINITION OF DONE VERIFICATIONS PASSED")
    print(
        "Scope: metadata only; no firmware downloads, Range requests or Vault uploads."
    )


if __name__ == "__main__":
    main()
