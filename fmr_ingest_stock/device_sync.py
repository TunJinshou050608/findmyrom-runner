"""Brand bootstrap and conservative device metadata upserts."""

import json
import re
from .common import (
    load_source,
    prepare_schema,
    private_key,
    save_cursor,
    text,
    validate_batch_size,
)

BRANDS = [
    (
        "xiaomi",
        "Xiaomi / Redmi / POCO",
        "account_wait",
        10,
        ("xiaomi", "redmi", "poco", "mi", "blackshark"),
    ),
    ("oneplus", "OnePlus", "easy", 20, ("oneplus", "one plus")),
    ("sony", "Sony Xperia", "code_required", 30, ("sony", "xperia", "sony ericsson")),
    ("google", "Google Pixel", "easy", 40, ("google", "pixel")),
    ("samsung", "Samsung", "easy", 50, ("samsung", "galaxy")),
    ("motorola", "Motorola", "code_required", 60, ("motorola", "moto", "lenovo")),
    ("oppo", "OPPO", "unverified", 65, ("oppo",)),
    ("vivo", "vivo", "unverified", 66, ("vivo", "iqoo")),
    ("realme", "realme", "unverified", 67, ("realme",)),
    ("rog", "ROG", "unverified", 68, ("rog", "asus rog")),
    ("asus", "ASUS", "unverified", 69, ("asus", "zenfone")),
    ("nothing", "Nothing", "unverified", 70, ("nothing", "cmf")),
    ("other", "Other Brands", "code_required", 99, ()),
]


def brand_id(value):
    value = "".join(text(value).lower().split())
    return next(
        (b[0] for b in BRANDS if value in {"".join(a.split()) for a in b[4]}), "other"
    )


def ensure_base_brands_and_anchor(db_client):
    for bid, name, unlock, order, _ in BRANDS:
        db_client.execute(
            """INSERT INTO brands(id,name,bl_unlock_status,sort_order) VALUES (?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET name=excluded.name,
            bl_unlock_status=excluded.bl_unlock_status,sort_order=excluded.sort_order""",
            (bid, name, unlock, order),
        )
    db_client.execute(
        "INSERT INTO devices(codename,brand_id,marketing_name) VALUES ('__global__','other','Global System Anchor') ON CONFLICT(codename) DO UPDATE SET brand_id=excluded.brand_id,marketing_name=excluded.marketing_name"
    )
    db_client.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_crawler_src_uniq ON crawler_sources(device_codename,source_kind,source_target)"
    )


DEVICE_SQL = """INSERT INTO devices(codename,brand_id,marketing_name,model_numbers_json,soc_name,boot_partition_type)
    VALUES (?,?,?,?,?,?) ON CONFLICT(codename) DO UPDATE SET
    brand_id=excluded.brand_id,
    marketing_name=CASE WHEN excluded.marketing_name != excluded.codename THEN excluded.marketing_name ELSE devices.marketing_name END,
    model_numbers_json=CASE WHEN excluded.model_numbers_json='[]' THEN devices.model_numbers_json ELSE excluded.model_numbers_json END,
    soc_name=COALESCE(excluded.soc_name,devices.soc_name),updated_at=strftime('%s','now')"""


def sync_devices(db_client, source_path_or_url: str, batch_size: int = 200) -> dict:
    validate_batch_size(batch_size)
    key = private_key()
    rows, digest = load_source(source_path_or_url)
    devices, rejected = {}, 0
    for row in rows:
        if not isinstance(row, dict):
            rejected += 1
            continue
        if text(row.get("brand") or row.get("oem")).lower() in {'huawei','honor'}:
            rejected += 1
            continue
        code = text(row.get("codename")).lower()
        if code == "__global__" or not re.fullmatch("[a-z0-9_-]{2,32}", code):
            rejected += 1
            continue
        models = row.get("models")
        models = (
            list(dict.fromkeys(text(m) for m in models if text(m)))
            if isinstance(models, list)
            else []
        )
        if code in devices:
            models = list(dict.fromkeys(json.loads(devices[code][3]) + models))
        boot = row.get("boot_partition")
        if not isinstance(boot, str) or boot not in {"init_boot", "boot"}:
            try:
                boot = (
                    "init_boot" if int(row.get("android_release", 13)) >= 13 else "boot"
                )
            except (ValueError, TypeError):
                boot = "init_boot"
        from .device_identity import marketing_name
        bid=brand_id(row.get("brand") or row.get("oem"))
        label=marketing_name({'vendor':bid,'upstream_id':code,'codename':code,
            'name':text(row.get("name") or row.get("model")) or code,'models':','.join(models)})
        if bid=='asus' and re.search(r'\bROG\b',label,re.I):bid='rog'
        devices[code] = (
            code,
            bid,
            label[:128],
            json.dumps(models),
            text(row.get("soc")) or None,
            boot,
        )
    prepare_schema(db_client, key)
    ensure_base_brands_and_anchor(db_client)
    values = list(devices.values())
    for offset in range(0, len(values), batch_size):
        db_client.execute_batch(
            [(DEVICE_SQL, p) for p in values[offset : offset + batch_size]]
        )
    save_cursor(db_client, "stock_devices", source_path_or_url, digest)
    return {
        "brands_upserted": len(BRANDS),
        "devices_upserted": len(devices),
        "devices_rejected": rejected,
    }
