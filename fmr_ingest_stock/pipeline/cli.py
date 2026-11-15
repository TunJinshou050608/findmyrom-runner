"""Command-line entrypoint for the v4 decoupled stock firmware pipeline."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Sequence

from fmr_core.d1_client import D1Client
from fmr_ingest_stock.adapters import load_vendor_configs

from .collector import run_collector
from .outbox import DEFAULT_OUTBOX_BATCH_SIZE
from .publisher import get_pipeline_status, run_publisher


def _add_common_args(target: argparse.ArgumentParser, *, suppress_defaults: bool) -> None:
    state_default = (
        argparse.SUPPRESS
        if suppress_defaults
        else os.environ.get("FMR_PIPELINE_STATE_DIR", ".fmr_state")
    )
    vendors_default = argparse.SUPPRESS if suppress_defaults else None
    target.add_argument(
        "--state-dir",
        default=state_default,
        help="Directory for collector SQLite DBs, locks, outbox, and publisher ledger.",
    )
    target.add_argument(
        "--vendors-yaml",
        default=vendors_default,
        help="Optional path to vendors.yaml configuration.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fmr-pipeline-v4",
        description="FindMyROM v4 Official Stock Firmware Collector & Quota-Safe Publisher",
    )
    _add_common_args(parser, suppress_defaults=False)

    sub = parser.add_subparsers(dest="command", required=True)

    collect_p = sub.add_parser("collect", help="Run official vendor collector(s) into Outbox.")
    _add_common_args(collect_p, suppress_defaults=True)
    group = collect_p.add_mutually_exclusive_group(required=True)
    group.add_argument("--vendor", help="Single vendor ID (e.g. google).")
    group.add_argument("--bucket", type=int, help="Run all enabled vendors in schedule bucket 0..11.")
    group.add_argument("--all-enabled", action="store_true", help="Run all enabled vendors.")
    collect_p.add_argument("--verify-workers", type=int, default=None)
    collect_p.add_argument("--max-devices", type=int, default=None, help="Maximum devices to check per vendor run.")
    collect_p.add_argument("--max-seconds", type=int, default=None, help="Maximum wall-clock seconds for vendor collection.")
    collect_p.add_argument("--batch-size", type=int, default=DEFAULT_OUTBOX_BATCH_SIZE)
    collect_p.add_argument("--no-dns-check", action="store_true", help="Skip DNS SSRF check (offline dev).")

    publish_p = sub.add_parser("publish", help="Publish Outbox batches to D1 with quota checkpointing.")
    _add_common_args(publish_p, suppress_defaults=True)
    publish_p.add_argument("--batch-size", type=int, default=DEFAULT_OUTBOX_BATCH_SIZE)
    publish_p.add_argument("--sqlite-path", default=None, help="Optional local SQLite path for D1Client.")
    publish_p.add_argument(
        "--private-key-env",
        default="SIGN_PRIVATE_KEY_HEX",
        help="Environment variable holding the 64-hex Ed25519 signing key.",
    )

    status_p = sub.add_parser("status", help="Output JSON status of Outbox, Ledger, and Collectors.")
    _add_common_args(status_p, suppress_defaults=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    state_dir = Path(args.state_dir)

    if args.command == "status":
        summary = get_pipeline_status(
            state_dir=state_dir,
            vendors_yaml_path=args.vendors_yaml,
        )
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0

    if args.command == "collect":
        configs = load_vendor_configs(args.vendors_yaml)
        if args.vendor:
            target_vendors = [args.vendor.strip().lower()]
        elif args.bucket is not None:
            target_vendors = [
                vid
                for vid, cfg in sorted(configs.items())
                if cfg.enabled and cfg.bucket == args.bucket
            ]
        else:
            target_vendors = [
                vid for vid, cfg in sorted(configs.items()) if cfg.enabled
            ]

        results = []
        for vid in target_vendors:
            res = run_collector(
                vid,
                state_dir=state_dir,
                vendors_yaml_path=args.vendors_yaml,
                verify_workers=args.verify_workers,
                max_devices=args.max_devices,
                resolve_dns=not args.no_dns_check,
                outbox_batch_size=args.batch_size,
                max_collect_seconds=args.max_seconds,
            )
            results.append(res)

        output = {"command": "collect", "results": results}
        print(json.dumps(output, indent=2, sort_keys=True))
        return 0

    if args.command == "publish":
        priv_key = ""
        for env_name in (
            args.private_key_env,
            "SIGN_PRIVATE_KEY_HEX",
            "ED25519_PRIVATE_KEY_HEX",
            "FMR_ED25519_PRIVATE_KEY",
        ):
            priv_key = os.environ.get(env_name, "").strip()
            if priv_key:
                break
        if not priv_key:
            parser.error(
                f"Missing Ed25519 private key in environment variable {args.private_key_env!r}"
            )

        configs = load_vendor_configs(args.vendors_yaml)
        brand_names = {vid: cfg.display_name for vid, cfg in configs.items()}

        db = D1Client(local_sqlite_path=args.sqlite_path)
        try:
            res = run_publisher(
                state_dir=state_dir,
                db=db,
                private_key_hex=priv_key,
                batch_size=args.batch_size,
                brand_names=brand_names,
            )
        finally:
            db.close()

        print(json.dumps(res, indent=2, sort_keys=True))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
