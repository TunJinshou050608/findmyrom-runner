#!/usr/bin/env python3
"""Self-contained SP03 offline DoD; no real ROM or GitHub requests."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import yaml

ROOT = Path(__file__).resolve().parent


def run(args, env, expected=0):
    result = subprocess.run(args, cwd=ROOT, env=env, text=True, capture_output=True)
    print(result.stdout, end="")
    if result.returncode != expected:
        raise RuntimeError(
            f"Expected exit {expected}, got {result.returncode}: {result.stderr}"
        )
    return result.stdout


def main():
    env = {**os.environ, "FMR_TEST_NO_SLEEP": "1"}
    output = run([sys.executable, "-m", "pytest", "-v"], env)
    if "skipped" in output.lower():
        raise AssertionError("No skips allowed")
    with tempfile.TemporaryDirectory(prefix="fmr-sp03-dod-") as temp:
        env.update(
            SIGN_PRIVATE_KEY_HEX="11" * 32,
            GH_VAULT_TOKEN="ghp_testtoken123",
            GH_VAULT_REPO="owner/findmyrom-vault-01",
            LOCAL_SQLITE_PATH=str(Path(temp) / "dod.db"),
        )
        run(
            [
                sys.executable,
                "-m",
                "fmr_ingest_stock.cli",
                "--mode",
                "all",
                "--devices-source",
                "tests/fixtures/sp02_devices_sample.yaml",
                "--firmwares-source",
                "tests/fixtures/sp02_firmwares_sample.yml",
            ],
            env,
        )
        result = json.loads(
            run(
                [
                    sys.executable,
                    "-m",
                    "fmr_extractor.cli",
                    "--dry-run",
                    "--batch-limit",
                    "10",
                    "--max-workers",
                    "4",
                ],
                env,
            ).splitlines()[-1]
        )
        if result["status"] != "dry_run" or result["selected"] != 3:
            raise AssertionError("Dry-run queue mismatch")
        run([sys.executable, "-m", "fmr_extractor.cli", "--max-workers", "20"], {}, 2)
        print("EXIT_2_OK")
        for args in [["--batch-limit", "0"], ["--max-runtime-sec", "9"]]:
            run([sys.executable, "-m", "fmr_extractor.cli", *args], {}, 2)
        run(
            [sys.executable, "-m", "fmr_extractor.cli", "--dry-run"],
            {"PATH": os.environ["PATH"]},
            1,
        )
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/services.yml").read_text()
    )
    triggers = workflow.get("on") or workflow.get(True)
    if "schedule" not in triggers or "workflow_dispatch" not in triggers:
        raise AssertionError("Missing workflow triggers")
    if workflow["permissions"] != {"contents": "read"}:
        raise AssertionError("Excess workflow token permissions")
    if "check" not in workflow["jobs"]:
        raise AssertionError("Missing extraction service verification")
    if (ROOT / ".github/workflows/batch_extract.yml").exists():
        raise AssertionError("Legacy extraction workflow must remain archived")
    print("WORKFLOW_VALID_OK")
    print("ALL SP03 OFFLINE DOD CHECKS PASSED")


if __name__ == "__main__":
    main()
