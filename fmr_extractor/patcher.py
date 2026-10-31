"""Validate raw Android images before deterministic compression."""

import gzip
import hashlib
from pathlib import Path
import re
import shutil

LIMITS = {
    "init_boot": (65536, 67108864, b"ANDROID!"),
    "boot": (65536, 201326592, b"ANDROID!"),
    "vendor_boot": (65536, 201326592, b"VNDRBOOT"),
    "vbmeta": (4096, 1048576, b"AVB0"),
}


def safe_component(value):
    result = re.sub(r"[^A-Za-z0-9._-]", "_", str(value))
    while ".." in result:
        result = result.replace("..", "_")
    return result or "_"


def validate_and_compress_partition(
    raw_img_path: Path,
    partition_name: str,
    codename: str,
    version_str: str,
    variant_type: str = "stock",
    out_dir: Path | None = None,
) -> dict:
    raw_img_path = Path(raw_img_path)
    if partition_name not in LIMITS or variant_type not in {
        "stock",
        "magisk",
        "kernelsu",
        "apatch",
    }:
        raise ValueError("Invalid partition image magic or size")
    minimum, maximum, magic = LIMITS[partition_name]
    with raw_img_path.open("rb") as source:
        if (
            not minimum <= raw_img_path.stat().st_size <= maximum
            or source.read(len(magic)) != magic
        ):
            raise ValueError("Invalid partition image magic or size")
    name = f"{safe_component(codename)}_{safe_component(version_str)}_{partition_name}_{variant_type}.img.gz"
    directory = Path(out_dir) if out_dir else raw_img_path.parent
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    with raw_img_path.open("rb") as source, target.open("wb") as dest:
        with gzip.GzipFile(filename="", fileobj=dest, mode="wb", mtime=0) as zipped:
            shutil.copyfileobj(source, zipped, 1024 * 1024)
    digest = hashlib.sha256()
    with target.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return dict(
        partition_name=partition_name,
        variant_type=variant_type,
        file_name=name,
        gz_path=target,
        file_size_bytes=target.stat().st_size,
        sha256_hash=digest.hexdigest(),
    )


# Hooks are installed by trusted Runner code, never from an upstream YAML field.
# No Magisk/KernelSU/APatch binary is fetched or executed by default.
PATCH_HOOKS = {}


def register_patch_hook(variant_type, tool_version, hook):
    if (
        variant_type not in {"magisk", "kernelsu", "apatch"}
        or not tool_version
        or not callable(hook)
    ):
        raise ValueError("Invalid patch hook")
    PATCH_HOOKS[variant_type] = (str(tool_version), hook)


def patched_variants(raw_img_path, partition_name, codename, version_str, out_dir):
    results = []
    for variant, (version, hook) in tuple(PATCH_HOOKS.items()):
        directory = Path(out_dir) / variant / partition_name
        directory.mkdir(parents=True, exist_ok=True)
        candidate = directory / "input.img"
        shutil.copyfile(raw_img_path, candidate)
        output = Path(hook(candidate))
        if output.is_symlink() or not output.resolve().is_relative_to(
            directory.resolve()
        ):
            raise ValueError("Patch hook output escaped workspace")
        item = validate_and_compress_partition(
            output, partition_name, codename, version_str, variant, directory
        )
        item["patch_tool_ver"] = version
        results.append(item)
    return results
