"""Extraction CLI; production executions belong on cloud runners."""

import argparse
import json
import sys
from fmr_core.d1_client import D1Client
from fmr_ingest_stock.common import private_key
from . import VaultClient, extract_partitions_via_range, run_extraction_batch
from .scheduler import validate_options
from .vault_client import credentials


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-limit", type=int, default=200)
    parser.add_argument("--max-workers", type=int, default=4)
    parser.add_argument("--max-runtime-sec", type=int, default=900)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        validate_options(args.batch_limit, args.max_workers, args.max_runtime_sec)
    except ValueError as exc:
        parser.error(str(exc))
    db = vault = None
    try:
        private_key()
        credentials()
        db = D1Client()
        vault = VaultClient()
        result = run_extraction_batch(
            db, vault, extract_partitions_via_range, **vars(args)
        )
        print(json.dumps(result, sort_keys=True))
        return 1 if result["circuit_broken"] else 0
    except Exception as exc:
        print(f"Extraction failed ({type(exc).__name__})", file=sys.stderr)
        return 1
    finally:
        if db:
            db.close()
        if vault:
            vault.close()


if __name__ == "__main__":
    sys.exit(main())
