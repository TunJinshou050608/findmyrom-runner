"""Tests for v2 unified atomic device snapshot and runtime targets resolution."""
import json
import sqlite3
import time

import pytest

from fmr_tg_scanner.device_snapshot import (
    SNAPSHOT_FILENAME,
    SNAPSHOT_SCHEMA_VERSION,
    build_snapshot_from_staging,
    refresh_or_load_snapshot,
)
from fmr_tg_scanner.targets import read_catalogue


def _seed_collector_db(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute(
            """
            CREATE TABLE collector_devices (
                codename TEXT PRIMARY KEY,
                brand_id TEXT NOT NULL,
                marketing_name TEXT NOT NULL,
                model_numbers_json TEXT NOT NULL,
                boot_partition_type TEXT NOT NULL,
                device_sha256 TEXT NOT NULL,
                updated_at INTEGER NOT NULL,
                last_checked_at INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        for code, brand, name, models, updated in rows:
            db.execute(
                "INSERT INTO collector_devices VALUES (?, ?, ?, ?, 'boot', 'sha', ?, 0)",
                (code, brand, name, json.dumps(models), updated),
            )


def _seed_ledger_db(path, brands, devices):
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE ledger_brands(brand_id TEXT PRIMARY KEY, brand_name TEXT NOT NULL, updated_at INTEGER NOT NULL)")
        db.execute("CREATE TABLE ledger_devices(codename TEXT PRIMARY KEY, device_sha256 TEXT NOT NULL, updated_at INTEGER NOT NULL)")
        for bid, bname in brands:
            db.execute("INSERT INTO ledger_brands VALUES (?, ?, 1)", (bid, bname))
        for code in devices:
            db.execute("INSERT INTO ledger_devices VALUES (?, 'sha', 1)", (code,))


def test_build_snapshot_from_staging_interleaves_brands_and_extracts_aliases(tmp_path):
    staging = tmp_path / "staging-v4"
    _seed_collector_db(
        staging / "collectors" / "xiaomi.sqlite",
        [
            ("alioth", "xiaomi", "POCO F3 / Redmi K40", ["M2012K11AC"], 100),
            ("marble", "xiaomi", "POCO F5", ["23049PCD8G"], 90),
            ("__global__", "xiaomi", "Global Anchor", [], 10),
        ],
    )
    _seed_collector_db(
        staging / "collectors" / "google.sqlite",
        [
            ("panther", "google", "Pixel 7", ["GVU6C"], 80),
            ("asus-cb", "asus", "ASUS Chromebook", [], 70),
        ],
    )
    _seed_ledger_db(staging / "publisher_ledger.sqlite", [("xiaomi", "Xiaomi"), ("google", "Google")], ["alioth", "marble", "panther"])

    snap = build_snapshot_from_staging(staging, representative_codenames=("alioth", "marble"), now=1000.0)
    assert snap["schema_version"] == SNAPSHOT_SCHEMA_VERSION
    assert set(snap["devices"]) == {"alioth", "marble", "panther"}
    assert "POCO F3" in snap["devices"]["alioth"]["aliases"]
    assert "Redmi K40" in snap["devices"]["alioth"]["aliases"]
    assert "M2012K11AC" in snap["devices"]["alioth"]["aliases"]
    # Brand interleaving: xiaomi has lower sort order than google
    assert snap["ordered_devices"][0] == "alioth"
    assert snap["ordered_devices"][1] == "panther"


def test_refresh_or_load_snapshot_falls_back_to_previous_valid_snapshot_on_failure(tmp_path):
    staging = tmp_path / "staging-v4"
    _seed_collector_db(
        staging / "collectors" / "xiaomi.sqlite",
        [("alioth", "xiaomi", "POCO F3", ["M2012K11AC"], 100), ("marble", "xiaomi", "POCO F5", [], 90)],
    )
    snap1, meta1 = refresh_or_load_snapshot(staging, max_age_seconds=100, now=1000.0)
    assert meta1["refreshed"] is True and meta1["fallback_used"] is False
    assert (staging / SNAPSHOT_FILENAME).is_file()

    # Corrupt/delete collectors to simulate upstream failure after expiry
    (staging / "collectors" / "xiaomi.sqlite").unlink()
    snap2, meta2 = refresh_or_load_snapshot(staging, max_age_seconds=100, now=1500.0)
    assert meta2["fallback_used"] is True
    assert meta2["snapshot_age_seconds"] == 500.0
    assert "RuntimeError" in meta2["refresh_error"]
    assert set(snap2["devices"]) == {"alioth", "marble"}


def test_read_catalogue_auto_resolves_v4_staging_when_catalogue_v3_has_no_sqlite(tmp_path, monkeypatch):
    cat_v3 = tmp_path / "catalogue-v3"
    cat_v3.mkdir()
    staging = tmp_path / "staging-v4"
    _seed_collector_db(
        staging / "collectors" / "xiaomi.sqlite",
        [("alioth", "xiaomi", "POCO F3", ["M2012K11AC"], 100), ("marble", "xiaomi", "POCO F5", [], 90)],
    )
    monkeypatch.setenv("FMR_CATALOGUE_STATE_DIR", str(cat_v3))
    monkeypatch.setenv("FMR_STAGING_V4_DIR", str(staging))

    names, sources = read_catalogue()
    assert set(names) == {"alioth", "marble"}
    assert sources and "snapshot_age_seconds" in sources[0]


def test_build_snapshot_rejects_missing_representative_device(tmp_path):
    staging = tmp_path / "staging-v4"
    _seed_collector_db(staging / "collectors" / "xiaomi.sqlite", [("alioth", "xiaomi", "POCO F3", [], 100)])
    with pytest.raises(RuntimeError, match="missing representative"):
        build_snapshot_from_staging(staging, representative_codenames=("alioth", "marble"))
