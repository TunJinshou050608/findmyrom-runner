"""CAS queue claims; DB operations stay on the coordinator thread."""

import inspect
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from pathlib import Path
import shutil
import tempfile
import threading
import time
import httpx
from fmr_core.crypto import generate_public_token, sign_boot_image
from fmr_ingest_stock.common import private_key
from .patcher import safe_component, validate_and_compress_partition, patched_variants
from .range_extractor import RangeNotSupportedError
from .vault_client import VaultAuthError, credentials

IMAGE_FIELDS = (
    "public_token",
    "firmware_id",
    "device_codename",
    "version_str",
    "partition_name",
    "variant_type",
    "patch_tool_ver",
    "file_name",
    "file_size_bytes",
    "sha256_hash",
    "gh_private_repo",
    "gh_release_tag",
    "gh_asset_id",
    "ed25519_signature",
)
IMAGE_SQL = f"""INSERT INTO extracted_boot_images ({",".join(IMAGE_FIELDS)}) VALUES ({",".join("?" for _ in IMAGE_FIELDS)})
 ON CONFLICT(firmware_id,partition_name,variant_type) DO UPDATE SET
 patch_tool_ver=excluded.patch_tool_ver,device_codename=excluded.device_codename,version_str=excluded.version_str,
 file_name=excluded.file_name,file_size_bytes=excluded.file_size_bytes,sha256_hash=excluded.sha256_hash,
 gh_private_repo=excluded.gh_private_repo,gh_release_tag=excluded.gh_release_tag,
 gh_asset_id=excluded.gh_asset_id,ed25519_signature=excluded.ed25519_signature
 WHERE extracted_boot_images.public_token=excluded.public_token"""


def validate_options(batch_limit, max_workers, max_runtime_sec):
    for value, low, high in [
        (batch_limit, 1, 500),
        (max_workers, 1, 6),
        (max_runtime_sec, 10, 3600),
    ]:
        if type(value) is not int or not low <= value <= high:
            raise ValueError("Invalid extraction limits")


def run_extraction_batch(
    db_client,
    vault_client,
    extractor_fn,
    batch_limit=200,
    max_workers=4,
    max_runtime_sec=900,
    dry_run=False,
) -> dict:
    validate_options(batch_limit, max_workers, max_runtime_sec)
    key = private_key()
    credentials()  # Fail before recovery or other writes, including dry-run.
    now = int(time.time())
    recovered = db_client.execute(
        """UPDATE stock_firmwares SET extract_status=CASE WHEN extract_retries+1>=3 THEN 'failed' ELSE 'pending' END,
       extract_retries=extract_retries+1,extract_error='Stale processing lock timeout',updated_at=?
       WHERE extract_status='processing' AND updated_at<=?""",
        (now, now - 1800),
    )["changes"]
    rows = db_client.query(
        "SELECT * FROM stock_firmwares WHERE extract_status='pending' AND extract_retries<3 ORDER BY id LIMIT ?",
        (batch_limit,),
    )
    stats = dict(
        stale_recovered=recovered,
        selected=len(rows),
        succeeded=0,
        failed=0,
        retried=0,
        circuit_broken=False,
        status="dry_run" if dry_run else "completed",
    )
    if dry_run:
        return stats
    stop = threading.Event()
    deadline = time.monotonic() + max_runtime_sec
    vault_client.deadline = deadline

    def process(row):
        directory = Path(tempfile.mkdtemp(prefix="fw_"))
        try:
            if stop.is_set():
                raise VaultAuthError("Batch stopped")
            options = (
                {"deadline": deadline}
                if "deadline" in inspect.signature(extractor_fn).parameters
                else {}
            )
            images = extractor_fn(row["fast_cdn_url"], directory, **options)
            if not ({"init_boot", "boot"} & images.keys()):
                raise ValueError("No boot partition extracted")
            # Validate ALL images before uploading the first asset.
            prepared = [
                validate_and_compress_partition(
                    path, part, row["device_codename"], row["version_str"]
                )
                for part, path in images.items()
            ]
            for part, path in images.items():
                if part in {"boot", "init_boot"}:
                    prepared.extend(
                        patched_variants(
                            path,
                            part,
                            row["device_codename"],
                            row["version_str"],
                            directory,
                        )
                    )
            tag = (
                safe_component(row["device_codename"])
                + "-"
                + safe_component(row["version_str"])
            )
            for item in prepared:
                if stop.is_set():
                    raise VaultAuthError("Batch stopped")
                if time.monotonic() >= deadline:
                    raise TimeoutError("Batch deadline exceeded")
                item["gh_asset_id"] = vault_client.upload_partition_asset(
                    tag, item["gz_path"], item["sha256_hash"]
                )
                item["gh_release_tag"] = tag
            return prepared
        except VaultAuthError:
            stop.set()
            raise
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def finish(row, future):
        try:
            images = future.result()
            statements = []
            for image in images:
                old = db_client.query(
                    "SELECT public_token FROM extracted_boot_images WHERE firmware_id=? AND partition_name=? AND variant_type=?",
                    (row["id"], image["partition_name"], image["variant_type"]),
                )
                token = old[0]["public_token"] if old else generate_public_token("bt")
                image.update(
                    public_token=token,
                    firmware_id=row["id"],
                    device_codename=row["device_codename"],
                    version_str=row["version_str"],
                    patch_tool_ver=image.get("patch_tool_ver"),
                    gh_private_repo=vault_client.repo,
                    ed25519_signature=sign_boot_image(
                        token,
                        image["sha256_hash"],
                        vault_client.repo,
                        image["gh_asset_id"],
                        key,
                    ),
                )
                statements.append((IMAGE_SQL, tuple(image[f] for f in IMAGE_FIELDS)))
            boot_type = (
                "init_boot"
                if any(i["partition_name"] == "init_boot" for i in images)
                else "boot"
            )
            statements.extend(
                [
                    (
                        "UPDATE devices SET boot_partition_type=? WHERE codename=?",
                        (boot_type, row["device_codename"]),
                    ),
                    (
                        "UPDATE stock_firmwares SET extract_status='done',extract_error=NULL,updated_at=? WHERE id=? AND extract_status='processing'",
                        (int(time.time()), row["id"]),
                    ),
                ]
            )
            db_client.execute_batch(statements)
            stats["succeeded"] += 1
        except VaultAuthError:
            stop.set()
            stats["circuit_broken"] = True
            stats["status"] = "circuit_broken"
            db_client.execute(
                "UPDATE stock_firmwares SET extract_status='pending',updated_at=? WHERE id=? AND extract_status='processing'",
                (int(time.time()), row["id"]),
            )
        except Exception as exc:
            terminal = isinstance(exc, (ValueError, RangeNotSupportedError)) or (
                isinstance(exc, httpx.HTTPStatusError)
                and exc.response.status_code == 404
            )
            retries = row["extract_retries"] + 1
            status = "failed" if terminal or retries >= 3 else "pending"
            # Only exception class names enter public CI logs or DB error fields;
            # upstream exception messages can contain credentials and URLs.
            db_client.execute(
                "UPDATE stock_firmwares SET extract_status=?,extract_retries=extract_retries+1,extract_error=?,updated_at=? WHERE id=? AND extract_status='processing'",
                (status, type(exc).__name__, int(time.time()), row["id"]),
            )
            stats["failed" if status == "failed" else "retried"] += 1

    iterator = iter(rows)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        active = {}
        exhausted = False
        while active or not exhausted:
            while (
                len(active) < max_workers
                and not exhausted
                and not stop.is_set()
                and time.monotonic() < deadline
            ):
                row = next(iterator, None)
                if row is None:
                    exhausted = True
                    break
                claimed = db_client.execute(
                    "UPDATE stock_firmwares SET extract_status='processing',updated_at=? WHERE id=? AND extract_status='pending' AND extract_retries<3",
                    (int(time.time()), row["id"]),
                    is_idempotent=False,
                )
                if claimed["changes"] == 1:
                    active[pool.submit(process, row)] = row
            if not active:
                break
            done, _ = wait(active, return_when=FIRST_COMPLETED)
            for future in done:
                finish(active.pop(future), future)
            if stop.is_set() or time.monotonic() >= deadline:
                exhausted = True
    return stats
