"""Extraction CLI; production executions belong on cloud runners."""

import argparse
import json
import os
import sys
from fmr_core.d1_client import D1Client
from fmr_ingest_stock.common import private_key
from . import VaultClient, extract_partitions_via_range, run_extraction_batch
from .scheduler import validate_options
from .queue_repair import requeue_skipped
from .vault_client import credentials


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-limit", type=int, default=200)
    parser.add_argument("--max-workers", type=int, default=4)
    parser.add_argument("--max-runtime-sec", type=int, default=900)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--requeue-skipped", action="store_true",
                        help="Repair untouched complete ZIPs incorrectly skipped by catalogue publishing")
    args = parser.parse_args(argv)
    repair_skipped = args.requeue_skipped
    del args.requeue_skipped
    try:
        validate_options(args.batch_limit, args.max_workers, args.max_runtime_sec)
    except ValueError as exc:
        parser.error(str(exc))
    if os.environ.get('FMR_D1_WRITES_PAUSED') == 'true':
        print('D1 publication is paused; no database requests, firmware reads or Vault uploads.')
        return 0
    db = vault = None
    try:
        private_key()
        credentials()
        db = D1Client()
        vault = VaultClient()
        if repair_skipped:
            print(json.dumps({'queue_repair': requeue_skipped(db, dry_run=args.dry_run)}, sort_keys=True), flush=True)
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
