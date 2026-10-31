"""Test synchronization of database schema files across repository locations."""

import hashlib
import os

CANONICAL_SCHEMA_SHA256 = "f5d10d21f18ff276efd7be657817c7c662cbb41d043956a6574a1cba4b66ec2e"


def test_schema_files_identical_hash():
    runner_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    runner_schema = os.path.join(runner_dir, "schema.sql")
    core_schema = os.path.join(runner_dir, "fmr_core", "schema.sql")

    # In standalone runner checkout, Runner and Core schema must always be present and identical
    assert os.path.exists(runner_schema), f"Missing Runner schema at {runner_schema}"
    assert os.path.exists(core_schema), f"Missing Core schema at {core_schema}"

    def get_hash(path: str) -> str:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()

    runner_hash = get_hash(runner_schema)
    core_hash = get_hash(core_schema)
    assert runner_hash == core_hash, "findmyrom-runner schema and fmr_core schema must be identical!"
    assert runner_hash == CANONICAL_SCHEMA_SHA256, (
        f"findmyrom-runner schema hash {runner_hash} does not match canonical SP01 hash {CANONICAL_SCHEMA_SHA256}!"
    )

    # Cross-repo check with Web migration (when Web repository is co-located)
    parent_dir = os.path.dirname(runner_dir)
    web_schema = os.path.join(parent_dir, "findmyrom-web", "migrations", "0001_initial_schema.sql")

    if os.path.exists(web_schema):
        web_hash = get_hash(web_schema)
        assert web_hash == runner_hash, "findmyrom-web schema and findmyrom-runner schema must be identical!"

