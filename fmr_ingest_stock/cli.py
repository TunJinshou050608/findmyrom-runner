"""Run with python -m fmr_ingest_stock.cli; stdout ends with JSON."""

import argparse
import json
import sys
from fmr_core.d1_client import D1Client
from .common import private_key
from .device_sync import sync_devices
from .firmware_sync import sync_firmwares


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode", required=True, choices=["devices", "firmwares", "all"]
    )
    parser.add_argument("--devices-source")
    parser.add_argument("--firmwares-source")
    parser.add_argument("--brand-id", default="xiaomi")
    parser.add_argument("--priority-codenames")
    parser.add_argument("--batch-size", type=int, default=200)
    parser.add_argument("--incremental", action="store_true")
    args = parser.parse_args(argv)
    if not 50 <= args.batch_size <= 500:
        parser.error("--batch-size must be between 50 and 500")
    for mode in ("devices", "firmwares"):
        if args.mode in {mode, "all"} and not getattr(args, mode + "_source"):
            parser.error("--" + mode + "-source is required for this mode")
    db = None
    try:
        private_key()  # Fail before opening or writing the database.
        db = D1Client()
        result = {"mode": args.mode}
        if args.mode in {"devices", "all"}:
            result.update(sync_devices(db, args.devices_source, args.batch_size))
        if args.mode in {"firmwares", "all"}:
            priority = (
                {
                    c.strip().lower()
                    for c in args.priority_codenames.split(",")
                    if c.strip()
                }
                if args.priority_codenames
                else None
            )
            result.update(
                sync_firmwares(
                    db,
                    args.firmwares_source,
                    args.brand_id,
                    priority,
                    args.batch_size,
                    args.incremental,
                )
            )
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:
        # Do not echo exception bodies containing remote credentials or URLs.
        print(f"Ingestion failed ({type(exc).__name__})", file=sys.stderr)
        return 1
    finally:
        if db is not None:
            db.close()


if __name__ == "__main__":
    sys.exit(main())
