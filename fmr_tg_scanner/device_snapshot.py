"""Unified atomic read-only device catalogue snapshot for 103 TG workers (v2 Section 3.1).

Eliminates the catalogue-v3 vs staging-v4 directory split and prevents the 4 TG
fetch shards from repeatedly querying production D1 or scraping website pages.
"""
from __future__ import annotations

from itertools import zip_longest
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from typing import Any

from fmr_ingest_stock.device_identity import clean_model_name, marketing_name
from fmr_ingest_stock.oem_sources import canonical_code
from fmr_ingest_stock.pipeline.publisher import DEFAULT_BRAND_SORT_ORDERS

SNAPSHOT_SCHEMA_VERSION = "tg-device-snapshot-v2"
SNAPSHOT_FILENAME = "device_catalogue_snapshot.json"
IDENTITY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{2,31}$")
DEFAULT_REPRESENTATIVE_CODENAMES = ("alioth", "marble")


def _fsync_dir(directory: Path) -> None:
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atomic_write_json(target: Path, payload: dict[str, Any]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.parent / f".{target.name}.{os.getpid()}.{time.time_ns()}.tmp"
    raw = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    with open(tmp_path, "wb") as fh:
        fh.write(raw)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp_path, target)
    _fsync_dir(target.parent)


def _extract_aliases(
    code: str,
    brand_id: str,
    mkt_name: str,
    model_numbers: list[str],
    extra_labels: list[str] | None = None,
) -> list[str]:
    device_stub = {
        "vendor": brand_id,
        "codename": code,
        "upstream_id": code,
        "name": mkt_name,
        "models": model_numbers,
    }
    raw_labels: list[str] = [
        marketing_name(device_stub) if brand_id else mkt_name,
        mkt_name,
        *(extra_labels or []),
        *model_numbers,
    ]
    if "-" in code and brand_id and code.startswith(f"{brand_id}-"):
        raw_labels.append(code[len(brand_id) + 1 :])

    aliases: list[str] = []
    for label in raw_labels:
        cleaned = clean_model_name(label) if label else ""
        if not cleaned:
            continue
        for part in [cleaned, *re.split(r"\s*/\s*|\s+aka\s+", cleaned, flags=re.I)]:
            part = part.strip()
            if part and part.lower() != code.lower() and part not in aliases:
                aliases.append(part)
    return aliases


def _interleave_devices_by_brand(devices: dict[str, dict[str, Any]]) -> list[str]:
    by_brand: dict[str, list[str]] = {}
    for code, entry in devices.items():
        brand = str(entry.get("brand_id") or "other").lower()
        by_brand.setdefault(brand, []).append(code)

    for brand, codes in by_brand.items():
        codes.sort(
            key=lambda c: (
                int(devices[c].get("sort_order", {}).get("device_rank", 0)),
                str(devices[c].get("marketing_name") or c).lower(),
                c,
            )
        )

    sorted_brands = sorted(
        by_brand.keys(),
        key=lambda b: (DEFAULT_BRAND_SORT_ORDERS.get(b, 99), b),
    )
    if not sorted_brands:
        return []
    return [
        code
        for row in zip_longest(*(by_brand[b] for b in sorted_brands))
        for code in row
        if code is not None
    ]


def build_snapshot_from_staging(
    staging_dir: str | Path,
    *,
    representative_codenames: tuple[str, ...] | None = None,
    min_device_count: int = 1,
    now: float | None = None,
) -> dict[str, Any]:
    """Read staging-v4/collectors/*.sqlite and publisher_ledger.sqlite to build a validated snapshot."""
    root = Path(staging_dir)
    now_ts = time.time() if now is None else float(now)
    collectors_dir = root / "collectors" if (root / "collectors").is_dir() else root
    ledger_path = root / "publisher_ledger.sqlite"

    ledger_devices: set[str] = set()
    ledger_brands: dict[str, str] = {}
    if ledger_path.is_file():
        with sqlite3.connect(f"file:{ledger_path.resolve()}?mode=ro", uri=True, timeout=15.0) as ldb:
            tables = {r[0] for r in ldb.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "ledger_devices" in tables:
                ledger_devices = {
                    str(r[0])
                    for r in ldb.execute("SELECT codename FROM ledger_devices")
                    if r[0]
                }
            if "ledger_brands" in tables:
                ledger_brands = {
                    str(r[0]): str(r[1])
                    for r in ldb.execute("SELECT brand_id, brand_name FROM ledger_brands")
                    if r[0]
                }

    devices: dict[str, dict[str, Any]] = {}
    sources: list[dict[str, Any]] = []

    sqlite_paths = [
        p
        for p in sorted(collectors_dir.glob("*.sqlite"))
        if p.name != "publisher_ledger.sqlite"
    ]
    for path in sqlite_paths:
        with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True, timeout=15.0) as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            count = 0
            if "collector_devices" in tables:
                rows = db.execute(
                    """
                    SELECT codename, brand_id, marketing_name, model_numbers_json, updated_at
                    FROM collector_devices
                    ORDER BY updated_at DESC, codename ASC
                    """
                ).fetchall()
                for rank, (raw_code, brand_id, mkt_name, models_json, updated_at) in enumerate(rows):
                    brand = str(brand_id or path.stem).strip().lower()
                    if brand in ("huawei", "honor") or "chromebook" in str(mkt_name or "").lower():
                        continue
                    code = str(raw_code or "").strip().lower()
                    if code == "__global__":
                        continue
                    if not IDENTITY_RE.fullmatch(code):
                        code = canonical_code(brand, code)
                    if not IDENTITY_RE.fullmatch(code) or code == "__global__":
                        continue
                    try:
                        parsed_models = json.loads(models_json or "[]")
                        if not isinstance(parsed_models, list):
                            parsed_models = [str(parsed_models)]
                    except Exception:
                        parsed_models = []
                    model_numbers = [str(m).strip() for m in parsed_models if str(m).strip()]
                    aliases = _extract_aliases(code, brand, str(mkt_name or code), model_numbers)
                    brand_rank = DEFAULT_BRAND_SORT_ORDERS.get(brand, 99)
                    existing = devices.get(code)
                    if existing is None:
                        devices[code] = {
                            "device_id": code,
                            "codename": code,
                            "brand_id": brand,
                            "brand_name": ledger_brands.get(brand, brand.upper()),
                            "marketing_name": clean_model_name(str(mkt_name or code)) or code,
                            "model_numbers": model_numbers,
                            "aliases": aliases,
                            "published_in_ledger": (code in ledger_devices) if ledger_devices else True,
                            "sort_order": {
                                "brand_rank": brand_rank,
                                "device_rank": rank,
                                "updated_at": int(updated_at or 0),
                            },
                        }
                    else:
                        for m in model_numbers:
                            if m not in existing["model_numbers"]:
                                existing["model_numbers"].append(m)
                        for a in aliases:
                            if a not in existing["aliases"]:
                                existing["aliases"].append(a)
                    count += 1
            sources.append({"file": path.name, "devices_read": count})

    if len(devices) < max(1, min_device_count):
        raise RuntimeError(
            f"Snapshot validation failed: found {len(devices)} devices (required >= {max(1, min_device_count)})"
        )

    if len(devices) != len({d["device_id"] for d in devices.values()}):
        raise RuntimeError("Snapshot validation failed: duplicate device_id detected")

    rep_checks = representative_codenames
    if rep_checks is None:
        # Automatically enforce representative check when Xiaomi collector is present.
        has_xiaomi = any(s["file"] == "xiaomi.sqlite" and s["devices_read"] >= 10 for s in sources)
        rep_checks = DEFAULT_REPRESENTATIVE_CODENAMES if has_xiaomi else ()

    missing_reps = [rep for rep in rep_checks if rep not in devices]
    if missing_reps:
        raise RuntimeError(
            f"Snapshot validation failed: missing representative device(s): {', '.join(missing_reps)}"
        )

    ordered = _interleave_devices_by_brand(devices)
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "generated_at": now_ts,
        "device_count": len(devices),
        "ledger_device_count": len(ledger_devices),
        "representative_codenames_verified": list(rep_checks),
        "ordered_devices": ordered,
        "devices": devices,
        "sources": sources,
    }


def refresh_or_load_snapshot(
    staging_dir: str | Path,
    *,
    snapshot_path: str | Path | None = None,
    max_age_seconds: int = 1800,
    representative_codenames: tuple[str, ...] | None = None,
    min_device_count: int = 1,
    now: float | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Atomically refresh the unified device snapshot or fall back to last valid snapshot on failure."""
    root = Path(staging_dir)
    now_ts = time.time() if now is None else float(now)
    target = Path(snapshot_path) if snapshot_path is not None else (root / SNAPSHOT_FILENAME)

    existing: dict[str, Any] | None = None
    if target.is_file():
        try:
            loaded = json.loads(target.read_text(encoding="utf-8"))
            if (
                isinstance(loaded, dict)
                and loaded.get("schema_version") == SNAPSHOT_SCHEMA_VERSION
                and isinstance(loaded.get("devices"), dict)
                and loaded["devices"]
            ):
                existing = loaded
        except Exception:
            existing = None

    if existing is not None:
        age = max(0.0, now_ts - float(existing.get("generated_at") or 0.0))
        if age < max_age_seconds:
            return existing, {
                "snapshot_path": str(target),
                "snapshot_age_seconds": round(age, 3),
                "refreshed": False,
                "fallback_used": False,
                "refresh_error": None,
            }

    try:
        fresh = build_snapshot_from_staging(
            root,
            representative_codenames=representative_codenames,
            min_device_count=min_device_count,
            now=now_ts,
        )
        _atomic_write_json(target, fresh)
        return fresh, {
            "snapshot_path": str(target),
            "snapshot_age_seconds": 0.0,
            "refreshed": True,
            "fallback_used": False,
            "refresh_error": None,
        }
    except Exception as exc:
        if existing is not None:
            age = max(0.0, now_ts - float(existing.get("generated_at") or 0.0))
            return existing, {
                "snapshot_path": str(target),
                "snapshot_age_seconds": round(age, 3),
                "refreshed": False,
                "fallback_used": True,
                "refresh_error": f"{type(exc).__name__}: {exc}",
            }
        raise


def snapshot_to_catalogue_names(
    snapshot: dict[str, Any],
) -> tuple[dict[str, list[str]], list[dict[str, Any]]]:
    """Convert a v2 device snapshot into the (names, sources) tuple expected by TG scanners."""
    names: dict[str, list[str]] = {}
    for code, info in (snapshot.get("devices") or {}).items():
        aliases = list(info.get("aliases") or [])
        mkt = info.get("marketing_name")
        if mkt and mkt.lower() != code.lower() and mkt not in aliases:
            aliases.insert(0, mkt)
        names[code] = aliases
    sources = list(snapshot.get("sources") or [])
    return names, sources
