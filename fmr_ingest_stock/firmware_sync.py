"""Signed official firmware ingestion; never downloads firmware payloads."""

import re
from urllib.parse import urlsplit
from fmr_core.crypto import generate_public_token, sign_firmware
from .cdn_rewriter import rewrite_to_fast_cdn
from .common import (
    cursor_hash,
    load_source,
    prepare_schema,
    private_key,
    save_cursor,
    text,
    validate_batch_size,
)
from .device_sync import BRANDS, ensure_base_brands_and_anchor


def region_code(code, version):
    for suffix, region in [
        ("eea", "EEA"),
        ("sg", "SG"),
        ("in", "IN"),
        ("jp", "JP"),
        ("tw", "TW"),
        ("ru", "RU"),
        ("tr", "TR"),
        ("id", "ID"),
        ("global", "Global"),
    ]:
        if "_" + suffix in code:
            return region
    for marker, region in [
        ("CNXM", "CN"),
        ("MIXM", "Global"),
        ("EUXM", "EEA"),
        ("INXM", "IN"),
        ("JPXM", "JP"),
        ("TWXM", "TW"),
    ]:
        if marker in version.upper():
            return region
    return "CN" if "_" not in code else "Global"


FIELDS = (
    "public_token",
    "device_codename",
    "region_code",
    "model_code",
    "version_str",
    "android_version",
    "package_type",
    "file_size_str",
    "md5_or_sha256",
    "original_official_url",
    "fast_cdn_url",
    "published_date",
    "ed25519_signature",
    "extract_status",
)
# Re-evaluate protected states inside the write transaction: an SP03 worker may
# claim a pending row between our SELECT and this UPSERT.
FIRMWARE_SQL = f"""INSERT INTO stock_firmwares ({",".join(FIELDS)})
    VALUES ({",".join("?" for _ in FIELDS)})
    ON CONFLICT DO UPDATE SET
    model_code=excluded.model_code,android_version=excluded.android_version,
    file_size_str=COALESCE(excluded.file_size_str,stock_firmwares.file_size_str),
    md5_or_sha256=COALESCE(excluded.md5_or_sha256,stock_firmwares.md5_or_sha256),
    original_official_url=excluded.original_official_url,fast_cdn_url=excluded.fast_cdn_url,
    published_date=COALESCE(excluded.published_date,stock_firmwares.published_date),
    ed25519_signature=excluded.ed25519_signature,
    extract_status=CASE WHEN stock_firmwares.extract_status IN ('done','processing','failed')
        THEN stock_firmwares.extract_status ELSE excluded.extract_status END,
    updated_at=strftime('%s','now')
    WHERE stock_firmwares.public_token=excluded.public_token"""


IDENTITY=('device_codename','region_code','version_str','package_type')

def read_identities(db,items):
    result=[]
    for offset in range(0,len(items),20):
        chunk=items[offset:offset+20]
        args=[item[k] for item in chunk for k in IDENTITY]
        sql="WITH wanted("+','.join(IDENTITY)+") AS (VALUES "+','.join('(?,?,?,?)' for _ in chunk)+") SELECT f.public_token,f.device_codename,f.region_code,f.version_str,f.package_type,f.extract_status FROM wanted w JOIN stock_firmwares f ON f.device_codename=w.device_codename AND f.region_code=w.region_code AND f.version_str=w.version_str AND f.package_type=w.package_type"
        result.extend(db.query(sql,args))
    return result


def normalize(row, brand):
    if not isinstance(row, dict):
        raise ValueError("Firmware must be a mapping")
    model = text(row.get("codename"))
    code = model.lower().split("_", 1)[0]
    if brand=="xiaomi":
        from .xiaomi_devices import base_code
        code=base_code(model)
    version = text(row.get("version"))
    if not re.fullmatch("[a-z0-9-]{2,32}", code) or not re.fullmatch(
        "[A-Za-z0-9._-]{2,64}", version
    ):
        raise ValueError("Invalid codename or version")
    original = text(row.get("link"))
    fast = rewrite_to_fast_cdn(brand, original)
    path = urlsplit(original).path.lower()
    if path.endswith(".zip"):
        package = "recovery_zip"
    elif path.endswith((".tgz", ".tar.gz")):
        package = "fastboot_tgz"
    else:
        raise ValueError("Unsupported firmware suffix")
    size = row.get("size")
    size = f"{size / 1024**3:.2f} GB" if type(size) is int else (text(size) or None)
    digest = None
    for field in ("md5", "sha256"):
        candidate = text(row.get(field)).lower()
        if re.fullmatch("(?:[0-9a-f]{32}|[0-9a-f]{64})", candidate):
            digest = candidate
            break
    date = text(row.get("date"))
    region=region_code(model.lower(),version)
    variant=row.get("variant_id") if brand=="xiaomi" else None
    if variant:
        from .xiaomi_variants import version_key,model_key
        version=version_key(version,variant);model=model_key(code,variant)
    return dict(
        device_codename=code,
        region_code=region,
        model_code=model,
        version_str=version,
        android_version=text(row.get("android")) or "unknown",
        package_type=package,
        file_size_str=size,
        md5_or_sha256=digest,
        original_official_url=original,
        fast_cdn_url=fast,
        published_date=date if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) else None,
    )


def sync_firmwares(
    db_client,
    source_path_or_url: str,
    brand_id: str = "xiaomi",
    priority_codenames: set[str] | None = None,
    batch_size: int = 200,
    incremental: bool = False,
) -> dict:
    validate_batch_size(batch_size)
    key = private_key()
    rows, digest = load_source(source_path_or_url)
    prepare_schema(db_client, key)
    stats = dict(
        inserted_or_updated=0,
        firmwares_rejected=0,
        pending_count=0,
        skipped_count=0,
        status="completed",
    )
    if (
        incremental
        and cursor_hash(db_client, "stock_firmwares", source_path_or_url) == digest
    ):
        stats["status"] = "skipped_unchanged"
        return stats
    ensure_base_brands_and_anchor(db_client)
    brand = brand_id if brand_id in {b[0] for b in BRANDS} else "other"
    normalized = {}
    for row in rows:
        try:
            item = normalize(row, brand)
        except ValueError:
            stats["firmwares_rejected"] += 1
            continue
        unique = tuple(
            item[f]
            for f in ("device_codename", "region_code", "version_str", "package_type")
        )
        normalized[unique] = item
    entries = list(normalized.items())
    for offset in range(0, len(entries), batch_size):
        chunk = entries[offset : offset + batch_size]
        codes = list(dict.fromkeys(item["device_codename"] for _, item in chunk))
        # Keep every read below D1's 100 bound-parameter limit, even when
        # the caller chooses a 500-row write batch with distinct devices.
        existing = read_identities(db_client,[item for _,item in chunk])
        by_key = {
            tuple(
                r[f]
                for f in (
                    "device_codename",
                    "region_code",
                    "version_str",
                    "package_type",
                )
            ): r
            for r in existing
        }
        db_client.execute_batch(
            [
                (
                    "INSERT OR IGNORE INTO devices(codename,brand_id,marketing_name) VALUES (?,?,?)",
                    (code, brand, code),
                )
                for code in codes
            ]
        )
        statements = []
        for unique, item in chunk:
            old = by_key.get(unique)
            token = old["public_token"] if old else generate_public_token("fw")
            if not re.fullmatch(r"fw_[A-Za-z0-9_-]{10,13}", token):
                raise ValueError("Invalid existing firmware public_token")
            eligible = item["package_type"] == "recovery_zip" and (
                priority_codenames is None
                or item["device_codename"] in priority_codenames
            )
            status = "pending" if eligible else "skipped"
            if old and old["extract_status"] in {"done", "processing", "failed"}:
                status = old["extract_status"]
            item.update(
                public_token=token,
                ed25519_signature=sign_firmware(token, item["fast_cdn_url"], key),
                extract_status=status,
            )
            statements.append((FIRMWARE_SQL, tuple(item[f] for f in FIELDS)))
            if status in {"pending", "skipped"}:
                stats[status + "_count"] += 1
        db_client.execute_batch(statements)
        # D1 changes includes trigger effects. Verify persisted identity/token
        # instead of comparing physical writes with the number of source rows.
        persisted={tuple(r[k] for k in IDENTITY):r['public_token'] for r in read_identities(db_client,[item for _,item in chunk])}
        verified=[persisted.get(tuple(item[k] for k in IDENTITY))==item['public_token'] for _,item in chunk]
        if not all(verified):
            # Another ingestion inserted the same natural key after our read.
            # The guarded UPSERT preserves its valid token/signature pair.
            # Abort without advancing the cursor; rerunning reads that token.
            raise RuntimeError("Concurrent firmware insertion; rerun synchronization")
        stats["inserted_or_updated"] += len(chunk)
    save_cursor(db_client, "stock_firmwares", source_path_or_url, digest)
    return stats
