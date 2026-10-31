#!/usr/bin/env python3
"""Seed development data into FindMyROM database (Local SQLite or Cloudflare D1).

Populates:
- 3 Brands (Xiaomi, Sony, OnePlus)
- 2 Brand universal tools (MiFlash, Newflasher)
- 2 Devices (marble, pdx234)
- 4 Stock firmwares with legitimate Ed25519 signatures
- 8 Extracted boot images with legitimate Ed25519 signatures
- 2 Custom resources (ROM & Recovery) with legitimate Ed25519 signatures

Idempotent: Safe to execute repeatedly.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys

# Ensure runner root is in path so fmr_core is importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from fmr_core.crypto import (
    sign_boot_image,
    sign_custom_resource,
    sign_firmware,
    sign_tool,
)
from fmr_core.d1_client import D1Client


def resolve_schema_path() -> str:
    """Resolve initial schema path with fallback chain ensuring standalone repo runner independence."""
    candidates = [
        os.environ.get("SCHEMA_PATH"),
        os.path.join(os.path.dirname(__file__), "schema.sql"),
        os.path.join(os.path.dirname(__file__), "fmr_core", "schema.sql"),
        os.path.join(
            os.path.dirname(__file__),
            "..",
            "findmyrom-web",
            "migrations",
            "0001_initial_schema.sql",
        ),
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return os.path.abspath(c)
    raise FileNotFoundError(
        "Could not find 0001_initial_schema.sql. Ensure schema.sql is present in Repo-Runner or pass SCHEMA_PATH."
    )


def parse_env_line(line: str) -> tuple[str, str] | None:
    """Parse a single line from a .env file.

    Supports:
    - 'export KEY=value' prefix (including tabs: 'export\tKEY=value')
    - Quoted strings with hash: KEY="val # not comment" or KEY='val # not comment'
    - Escaped quotes: KEY="val \"with\" quotes"
    - Inline comments preceded by whitespace: KEY=value # comment
    - Preserves unquoted hashes without whitespace (e.g. URLs: KEY=https://domain.com/path#hash)
    - Leading/trailing whitespace
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return None

    # Handle export prefix with space or tab
    line = re.sub(r"^export\s+", "", line).strip()

    if "=" not in line:
        return None

    k, v = line.split("=", 1)
    k = k.strip()
    v = v.strip()

    if not k:
        return None

    if v.startswith('"'):
        end_idx = -1
        escaped = False
        for idx in range(1, len(v)):
            if escaped:
                escaped = False
                continue
            if v[idx] == "\\":
                escaped = True
                continue
            if v[idx] == '"':
                end_idx = idx
                break
        if end_idx != -1:
            val = v[1:end_idx].replace('\\"', '"')
            return k, val
        return k, v.strip('"')

    elif v.startswith("'"):
        end_idx = v.find("'", 1)
        if end_idx != -1:
            return k, v[1:end_idx]
        return k, v.strip("'")

    else:
        # Strip inline comment only if preceded by whitespace (e.g. 'KEY=value # comment')
        if re.search(r"\s+#", v):
            v = re.split(r"\s+#", v, maxsplit=1)[0].strip()
        elif v.startswith("# ") or v == "#":
            v = ""
        return k, v


def load_env_file_if_present(env_path: str, override: bool = False) -> dict[str, str]:
    """Lightweight robust .env parser without external dependencies.

    Returns loaded dictionary of key-value pairs. Existing shell environment
    variables take precedence unless override=True.
    """
    loaded: dict[str, str] = {}
    if not os.path.exists(env_path):
        return loaded
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            parsed = parse_env_line(line)
            if parsed is not None:
                k, v = parsed
                loaded[k] = v
                if override or k not in os.environ:
                    os.environ[k] = v
    return loaded


def derive_public_key_hex(private_key_hex: str) -> str:
    priv_bytes = bytes.fromhex(private_key_hex.strip().lower())
    priv_key = ed25519.Ed25519PrivateKey.from_private_bytes(priv_bytes)
    pub_bytes = priv_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return pub_bytes.hex().lower()


def get_private_key(explicit_key: str | None = None) -> str:
    """Resolve private key from explicit argument or environment. Fails fast if missing."""
    if explicit_key:
        return explicit_key.strip().lower()

    if "SIGN_PRIVATE_KEY_HEX" in os.environ:
        return os.environ["SIGN_PRIVATE_KEY_HEX"].strip().lower()

    raise ValueError(
        "SIGN_PRIVATE_KEY_HEX is required to seed signed data. "
        "Set the SIGN_PRIVATE_KEY_HEX environment variable, pass --private-key, "
        "or run 'python generate_keys.py --write-dev-files' (or 'python scripts/generate_keys.py') "
        "to initialize a local development key."
    )


def run_seed(client: D1Client, private_key_hex: str | None = None) -> str:
    priv_hex = get_private_key(private_key_hex)
    pub_hex = derive_public_key_hex(priv_hex)

    print(f"[*] Seeding data in mode: {client.mode}")
    print(f"[*] Using Ed25519 signing keypair (Public Key: {pub_hex})")

    # 1. Brands (comprehensive Android brand roster satisfying foreign keys for device catalog)
    brands = [
        (
            "xiaomi",
            "Xiaomi / Redmi / POCO",
            "official_apply",
            "### 小米/Redmi/POCO 解锁指南\n1. 绑定小米账号并插入 SIM 卡 168 小时；\n2. HyperOS 需在社区答题申请权限；\n3. 使用官方 MiFlash Unlock 工具解锁。",
            10,
        ),
        (
            "sony",
            "Sony Xperia",
            "official_free",
            "### 索尼 Xperia 解锁指南\n1. 拨号盘输入 `*#*#7378423#*#*` 确认 Bootloader unlock allowed: Yes；\n2. 访问索尼开发者官网获取代码；\n3. Fastboot 运行 `fastboot oem unlock 0x<UNLOCK_KEY>`。",
            20,
        ),
        (
            "oneplus",
            "OnePlus",
            "official_free",
            "### 一加解锁指南\n1. 开发者选项中开启 OEM 解锁；\n2. 进入 Fastboot 运行 `fastboot flashing unlock`；\n3. 音量键确认解锁即可。",
            30,
        ),
        (
            "google",
            "Google Pixel",
            "official_free",
            "### Google Pixel 解锁指南\n1. 开发者选项启用 OEM 解锁；\n2. 执行 `fastboot flashing unlock`；\n3. 音量键确认即可。",
            40,
        ),
        (
            "samsung",
            "Samsung Galaxy",
            "knox_trip_warning",
            "### 三星 Galaxy 解锁说明\n注意：解锁 Bootloader 会永久熔断 Knox (0x1)，且无法撤销；部分安全硬件功能将永久失效。",
            50,
        ),
        (
            "motorola",
            "Motorola",
            "official_apply",
            "### 摩托罗拉解锁指南\n1. 获取 bootloader unlock data；\n2. 前往摩托罗拉开发者官网换取 Unlock Code；\n3. 执行 `fastboot oem unlock <UNIQUE_KEY>`。",
            60,
        ),
        (
            "nothing",
            "Nothing Phone",
            "official_free",
            "### Nothing Phone 解锁指南\n1. 开启 OEM 解锁；\n2. 执行 `fastboot flashing unlock`；\n3. 音量键选择确认。",
            70,
        ),
        (
            "asus",
            "Asus ROG / Zenfone",
            "official_apk_unlock",
            "### 华硕解锁指南\n需下载官方 Unlock Device Tool APK 登录华硕账号授权解锁。",
            80,
        ),
    ]

    for b in brands:
        client.execute(
            """
            INSERT INTO brands (id, name, bl_unlock_status, bl_unlock_guide_md, sort_order)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,
                bl_unlock_status=excluded.bl_unlock_status,
                bl_unlock_guide_md=excluded.bl_unlock_guide_md,
                sort_order=excluded.sort_order;
            """,
            b,
        )
    print(f"[+] Upserted {len(brands)} brands.")

    # 2. Universal Tools (Public tokens strictly conforming to tl_[a-zA-Z0-9]{10,12})
    tools = [
        (
            "tl_miflashwin1",
            "xiaomi",
            "MiFlash Tool",
            "2023.08.14",
            "Windows",
            "小米官方线刷工具，支持 Fastboot 模式救砖与官方固件完整刷写。",
            "https://cdn.alsgp0.fds.api.mi-img.com/admin-community-files/MiFlash20230814.zip",
        ),
        (
            "tl_newflasher1",
            "sony",
            "Newflasher",
            "v54",
            "Windows/Linux",
            "开源索尼 Xperia 固件线刷救砖工具，适用于 Xperia 1/5/10 全系列。",
            "https://github.com/newflasher/newflasher/releases/tag/v54",
        ),
    ]

    for t in tools:
        sig = sign_tool(t[0], t[6], priv_hex)
        client.execute(
            """
            INSERT INTO brand_universal_tools (
                public_token, brand_id, tool_name, version_str, os_platform, description, target_redirect_url, ed25519_signature
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(public_token) DO UPDATE SET
                brand_id=excluded.brand_id,
                tool_name=excluded.tool_name,
                version_str=excluded.version_str,
                os_platform=excluded.os_platform,
                description=excluded.description,
                target_redirect_url=excluded.target_redirect_url,
                ed25519_signature=excluded.ed25519_signature;
            """,
            (*t, sig),
        )
    print(f"[+] Upserted {len(tools)} brand tools.")

    # 3. Devices
    devices = [
        (
            "marble",
            "xiaomi",
            "Redmi Note 12 Turbo / POCO F5",
            json.dumps(["23049RAD8C", "23049PCD8G"]),
            "Snapdragon 7+ Gen 2 (SM7475-AB)",
            "init_boot",
            1,
            "A/B 分区结构，安卓 13 及以上出厂，Root 时务必修补 **init_boot** 分区而不是 boot 分区。",
            1,
            0,
        ),
        (
            "pdx234",
            "sony",
            "Sony Xperia 1 V",
            json.dumps(["XQ-DQ72", "XQ-DQ54", "XQ-DQ44"]),
            "Snapdragon 8 Gen 2 (SM8550-AB)",
            "init_boot",
            1,
            "A/B 分区结构，出厂搭载安卓 13，引导镜像在 **init_boot**。解锁会导致部分专有 DRM 算法丢失。",
            0,
            0,
        ),
    ]

    for d in devices:
        client.execute(
            """
            INSERT INTO devices (
                codename, brand_id, marketing_name, model_numbers_json, soc_name,
                boot_partition_type, has_vbmeta, flashing_notes_md, phase2_roms_enabled, phase3_parts_enabled
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(codename) DO UPDATE SET
                brand_id=excluded.brand_id,
                marketing_name=excluded.marketing_name,
                model_numbers_json=excluded.model_numbers_json,
                soc_name=excluded.soc_name,
                boot_partition_type=excluded.boot_partition_type,
                has_vbmeta=excluded.has_vbmeta,
                flashing_notes_md=excluded.flashing_notes_md,
                phase2_roms_enabled=excluded.phase2_roms_enabled,
                phase3_parts_enabled=excluded.phase3_parts_enabled;
            """,
            d,
        )
    print(f"[+] Upserted {len(devices)} devices.")

    # 4. Stock Firmwares (Public tokens strictly conforming to fw_[a-zA-Z0-9]{10,12})
    firmwares = [
        # marble firmwares
        (
            "fw_marb01hyp01",
            "marble",
            "CN",
            "23049RAD8C",
            "OS1.0.12.0.UMRCNXM",
            "14.0",
            "recovery_zip",
            "5.8 GB",
            hashlib.sha256(b"OS1.0.12.0.UMRCNXM").hexdigest(),
            "https://bigota.d.miui.com/OS1.0.12.0.UMRCNXM/miui_MARBLE_OS1.0.12.0.UMRCNXM_a789bc12de_14.0.zip",
            "https://ultimateota.d.miui.com/OS1.0.12.0.UMRCNXM/miui_MARBLE_OS1.0.12.0.UMRCNXM_a789bc12de_14.0.zip",
            "2024-04-18",
            "done",
        ),
        (
            "fw_marb02miu02",
            "marble",
            "CN",
            "23049RAD8C",
            "V14.0.25.0.TMRCNXM",
            "13.0",
            "recovery_zip",
            "5.4 GB",
            hashlib.sha256(b"V14.0.25.0.TMRCNXM").hexdigest(),
            "https://bigota.d.miui.com/V14.0.25.0.TMRCNXM/miui_MARBLE_V14.0.25.0.TMRCNXM_b123cd45ef_13.0.zip",
            "https://ultimateota.d.miui.com/V14.0.25.0.TMRCNXM/miui_MARBLE_V14.0.25.0.TMRCNXM_b123cd45ef_13.0.zip",
            "2023-11-05",
            "done",
        ),
        # pdx234 firmwares
        (
            "fw_pdx234671a2",
            "pdx234",
            "HK",
            "XQ-DQ72",
            "67.1.A.2.220",
            "14.0",
            "recovery_zip",
            "3.2 GB",
            hashlib.sha256(b"67.1.A.2.220").hexdigest(),
            "https://xperia-firmware.sony.com/67.1.A.2.220/pdx234_customized_hk.zip",
            "https://fastcdn.findmyrom.com/sony/pdx234/67.1.A.2.220.zip",
            "2024-05-10",
            "done",
        ),
        (
            "fw_pdx234670a4",
            "pdx234",
            "HK",
            "XQ-DQ72",
            "67.0.A.4.51",
            "13.0",
            "recovery_zip",
            "3.0 GB",
            hashlib.sha256(b"67.0.A.4.51").hexdigest(),
            "https://xperia-firmware.sony.com/67.0.A.4.51/pdx234_customized_hk.zip",
            "https://fastcdn.findmyrom.com/sony/pdx234/67.0.A.4.51.zip",
            "2023-10-15",
            "done",
        ),
    ]

    fw_id_map: dict[str, int] = {}
    for fw in firmwares:
        token, codename, region, model, ver, a_ver, pkg_type, sz, hsh, orig_url, fast_url, pub_date, status = fw
        sig = sign_firmware(token, fast_url, priv_hex)
        client.execute(
            """
            INSERT INTO stock_firmwares (
                public_token, device_codename, region_code, model_code, version_str,
                android_version, package_type, file_size_str, md5_or_sha256,
                original_official_url, fast_cdn_url, published_date, ed25519_signature, extract_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(device_codename, region_code, version_str, package_type) DO UPDATE SET
                public_token=excluded.public_token,
                model_code=excluded.model_code,
                android_version=excluded.android_version,
                file_size_str=excluded.file_size_str,
                md5_or_sha256=excluded.md5_or_sha256,
                original_official_url=excluded.original_official_url,
                fast_cdn_url=excluded.fast_cdn_url,
                published_date=excluded.published_date,
                ed25519_signature=excluded.ed25519_signature,
                extract_status=excluded.extract_status;
            """,
            (
                token,
                codename,
                region,
                model,
                ver,
                a_ver,
                pkg_type,
                sz,
                hsh,
                orig_url,
                fast_url,
                pub_date,
                sig,
                status,
            ),
        )

        rows = client.query(
            "SELECT id FROM stock_firmwares WHERE public_token = ?", (token,)
        )
        if rows:
            fw_id_map[token] = rows[0]["id"]

    print(f"[+] Upserted {len(firmwares)} stock firmwares.")

    # 5. Extracted Boot Images (Public tokens strictly conforming to bt_[a-zA-Z0-9]{10,12})
    boot_images = [
        # marble HyperOS 1.0.12.0
        (
            "bt_marbhypstk1",
            "fw_marb01hyp01",
            "marble",
            "OS1.0.12.0.UMRCNXM",
            "init_boot",
            "stock",
            None,
            "marble_OS1.0.12.0_init_boot.img.gz",
            12582912,
            hashlib.sha256(b"marble_OS1.0.12.0_init_boot.img.gz").hexdigest(),
            "findmyrom/findmyrom-vault-01",
            "vault-marble-OS1.0.12.0",
            1001,
        ),
        (
            "bt_marbhypmag1",
            "fw_marb01hyp01",
            "marble",
            "OS1.0.12.0.UMRCNXM",
            "init_boot",
            "magisk",
            "Magisk v27.0",
            "marble_OS1.0.12.0_init_boot_magisk27.img.gz",
            14680064,
            hashlib.sha256(b"marble_OS1.0.12.0_init_boot_magisk27.img.gz").hexdigest(),
            "findmyrom/findmyrom-vault-01",
            "vault-marble-OS1.0.12.0",
            1002,
        ),
        # marble MIUI 14.0.25.0
        (
            "bt_marbmiustk1",
            "fw_marb02miu02",
            "marble",
            "V14.0.25.0.TMRCNXM",
            "init_boot",
            "stock",
            None,
            "marble_V14.0.25.0_init_boot.img.gz",
            11534336,
            hashlib.sha256(b"marble_V14.0.25.0_init_boot.img.gz").hexdigest(),
            "findmyrom/findmyrom-vault-01",
            "vault-marble-V14.0.25.0",
            1003,
        ),
        (
            "bt_marbmiuksu1",
            "fw_marb02miu02",
            "marble",
            "V14.0.25.0.TMRCNXM",
            "init_boot",
            "kernelsu",
            "KernelSU v0.9.5",
            "marble_V14.0.25.0_init_boot_ksu.img.gz",
            13631488,
            hashlib.sha256(b"marble_V14.0.25.0_init_boot_ksu.img.gz").hexdigest(),
            "findmyrom/findmyrom-vault-01",
            "vault-marble-V14.0.25.0",
            1004,
        ),
        # pdx234 67.1.A.2.220
        (
            "bt_pdx671stk01",
            "fw_pdx234671a2",
            "pdx234",
            "67.1.A.2.220",
            "init_boot",
            "stock",
            None,
            "pdx234_67.1.A.2.220_init_boot.img.gz",
            10485760,
            hashlib.sha256(b"pdx234_67.1.A.2.220_init_boot.img.gz").hexdigest(),
            "findmyrom/findmyrom-vault-01",
            "vault-pdx234-67.1.A.2.220",
            2001,
        ),
        (
            "bt_pdx671mag01",
            "fw_pdx234671a2",
            "pdx234",
            "67.1.A.2.220",
            "init_boot",
            "magisk",
            "Magisk v27.0",
            "pdx234_67.1.A.2.220_init_boot_magisk27.img.gz",
            12582912,
            hashlib.sha256(b"pdx234_67.1.A.2.220_init_boot_magisk27.img.gz").hexdigest(),
            "findmyrom/findmyrom-vault-01",
            "vault-pdx234-67.1.A.2.220",
            2002,
        ),
        # pdx234 67.0.A.4.51
        (
            "bt_pdx670stk01",
            "fw_pdx234670a4",
            "pdx234",
            "67.0.A.4.51",
            "init_boot",
            "stock",
            None,
            "pdx234_67.0.A.4.51_init_boot.img.gz",
            9437184,
            hashlib.sha256(b"pdx234_67.0.A.4.51_init_boot.img.gz").hexdigest(),
            "findmyrom/findmyrom-vault-01",
            "vault-pdx234-67.0.A.4.51",
            2003,
        ),
        (
            "bt_pdx670ksu01",
            "fw_pdx234670a4",
            "pdx234",
            "67.0.A.4.51",
            "init_boot",
            "kernelsu",
            "KernelSU v0.9.5",
            "pdx234_67.0.A.4.51_init_boot_ksu.img.gz",
            11534336,
            hashlib.sha256(b"pdx234_67.0.A.4.51_init_boot_ksu.img.gz").hexdigest(),
            "findmyrom/findmyrom-vault-01",
            "vault-pdx234-67.0.A.4.51",
            2004,
        ),
    ]

    for bt in boot_images:
        (
            token,
            fw_ref,
            codename,
            ver,
            part,
            variant,
            patch_ver,
            fname,
            fsize,
            hsh,
            repo,
            tag,
            asset_id,
        ) = bt
        firmware_id = fw_id_map.get(fw_ref)
        if not firmware_id:
            r = client.query(
                "SELECT id FROM stock_firmwares WHERE public_token = ?", (fw_ref,)
            )
            firmware_id = r[0]["id"]

        sig = sign_boot_image(
            public_token=token,
            sha256_hash=hsh,
            gh_private_repo=repo,
            gh_asset_id=asset_id,
            private_key_hex=priv_hex,
        )

        client.execute(
            """
            INSERT INTO extracted_boot_images (
                public_token, firmware_id, device_codename, version_str,
                partition_name, variant_type, patch_tool_ver, file_name,
                file_size_bytes, sha256_hash, gh_private_repo, gh_release_tag,
                gh_asset_id, ed25519_signature
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(firmware_id, partition_name, variant_type) DO UPDATE SET
                public_token=excluded.public_token,
                device_codename=excluded.device_codename,
                version_str=excluded.version_str,
                patch_tool_ver=excluded.patch_tool_ver,
                file_name=excluded.file_name,
                file_size_bytes=excluded.file_size_bytes,
                sha256_hash=excluded.sha256_hash,
                gh_private_repo=excluded.gh_private_repo,
                gh_release_tag=excluded.gh_release_tag,
                gh_asset_id=excluded.gh_asset_id,
                ed25519_signature=excluded.ed25519_signature;
            """,
            (
                token,
                firmware_id,
                codename,
                ver,
                part,
                variant,
                patch_ver,
                fname,
                fsize,
                hsh,
                repo,
                tag,
                asset_id,
                sig,
            ),
        )
    print(f"[+] Upserted {len(boot_images)} boot images.")

    # 6. Custom Resources (Public tokens strictly conforming to cr_[a-zA-Z0-9]{10,12})
    custom_resources = [
        (
            "cr_marblineag1",
            "marble",
            "free_rom",
            "AOSP",
            "LineageOS 21",
            0,
            "14.0",
            "20240410-UNOFFICIAL",
            "marble_dev",
            "@marble_roms",
            None,
            0,
            "https://forum.xda-developers.com/t/rom-14-marble-lineageos-21.4648291/",
            None,
            "Official MIUI 14 or HyperOS firmware required.",
            "vanilla",
            json.dumps([]),
            1240,
            530,
            1712707200,
        ),
        (
            "cr_marbtwrp371",
            "marble",
            "essential_tool",
            "Recovery",
            "TWRP Recovery",
            1,
            None,
            "3.7.1_12-0",
            "TeamWin",
            "@twrp_official",
            None,
            0,
            "https://twrp.me/xiaomi/xiaomiredminote12turbo.html",
            None,
            "Flash to init_boot partition.",
            None,
            json.dumps([]),
            3500,
            1200,
            1700000000,
        ),
    ]

    for cr in custom_resources:
        (
            token,
            codename,
            zone,
            cat,
            proj,
            is_off,
            a_ver,
            b_ver,
            m_name,
            m_tg,
            m_email,
            m_opt,
            target_url,
            companion_tok,
            req_note,
            gapps,
            bugs,
            dl_cnt,
            clk_cnt,
            pub_at,
        ) = cr
        sig = sign_custom_resource(token, zone, target_url, priv_hex)
        client.execute(
            """
            INSERT INTO custom_resources (
                public_token, device_codename, zone_type, category_tag, project_name,
                is_official, android_version, build_version_tag, maintainer_name,
                maintainer_contact_tg, maintainer_contact_email, maintainer_opted_out,
                target_post_url, companion_recovery_token, firmware_req_note,
                gapps_type, known_bugs_json, ext_download_count, site_click_count,
                ed25519_signature, published_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(device_codename, project_name, build_version_tag, target_post_url) DO UPDATE SET
                public_token=excluded.public_token,
                zone_type=excluded.zone_type,
                category_tag=excluded.category_tag,
                is_official=excluded.is_official,
                android_version=excluded.android_version,
                maintainer_name=excluded.maintainer_name,
                maintainer_contact_tg=excluded.maintainer_contact_tg,
                maintainer_contact_email=excluded.maintainer_contact_email,
                maintainer_opted_out=excluded.maintainer_opted_out,
                companion_recovery_token=excluded.companion_recovery_token,
                firmware_req_note=excluded.firmware_req_note,
                gapps_type=excluded.gapps_type,
                known_bugs_json=excluded.known_bugs_json,
                ext_download_count=excluded.ext_download_count,
                site_click_count=excluded.site_click_count,
                ed25519_signature=excluded.ed25519_signature,
                published_at=excluded.published_at;
            """,
            (
                token,
                codename,
                zone,
                cat,
                proj,
                is_off,
                a_ver,
                b_ver,
                m_name,
                m_tg,
                m_email,
                m_opt,
                target_url,
                companion_tok,
                req_note,
                gapps,
                bugs,
                dl_cnt,
                clk_cnt,
                sig,
                pub_at,
            ),
        )
    print(f"[+] Upserted {len(custom_resources)} custom resources.")
    print("[*] Seeding finished successfully!")
    return pub_hex


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed FindMyROM development data.")
    parser.add_argument(
        "--env-file",
        default=None,
        help="Path to custom .env file to load before connecting",
    )
    parser.add_argument(
        "--remote",
        action="store_true",
        help="Target remote Cloudflare D1 database (reads CF_ACCOUNT_ID, CF_API_TOKEN, CF_D1_DATABASE_ID)",
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help="Local SQLite database file path (defaults to LOCAL_SQLITE_PATH env or 'dev_local.db')",
    )
    parser.add_argument(
        "--private-key",
        default=None,
        help="Ed25519 private key hex for signing seed records",
    )
    parser.add_argument(
        "--no-schema",
        action="store_true",
        help="Skip schema initialization step",
    )
    args = parser.parse_args()

    # 1. Load .env FIRST so database path, CF credentials, and private keys take effect!
    if args.env_file:
        load_env_file_if_present(args.env_file)
    else:
        local_env = os.path.join(os.path.dirname(__file__), ".env")
        load_env_file_if_present(local_env)

    # 2. Resolve database target
    if args.remote:
        print("[*] Connecting to remote Cloudflare D1...")
        cf_account_id = os.environ.get("CF_ACCOUNT_ID")
        cf_api_token = os.environ.get("CF_API_TOKEN")
        cf_database_id = os.environ.get("CF_D1_DATABASE_ID")
        if not (cf_account_id and cf_api_token and cf_database_id):
            raise ValueError(
                "Cannot connect to remote Cloudflare D1: missing credentials in environment. "
                "Ensure CF_ACCOUNT_ID, CF_API_TOKEN, and CF_D1_DATABASE_ID are set."
            )
        client = D1Client(
            cf_account_id=cf_account_id,
            cf_api_token=cf_api_token,
            cf_database_id=cf_database_id,
        )
    else:
        target_db_path = args.db_path or os.environ.get("LOCAL_SQLITE_PATH", "dev_local.db")
        print(f"[*] Connecting to local SQLite: {target_db_path}")
        client = D1Client(local_sqlite_path=target_db_path)

    if not args.no_schema:
        schema_path = resolve_schema_path()
        with open(schema_path, "r", encoding="utf-8") as f:
            print(f"[*] Applying schema from: {schema_path}")
            client.execute_script(f.read())

    run_seed(client, private_key_hex=args.private_key)
    client.close()


if __name__ == "__main__":
    main()
