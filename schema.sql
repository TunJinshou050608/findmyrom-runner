-- ==============================================================================
-- 0001_initial_schema.sql
-- FindMyROM D1 Database Initial Schema
-- Follows: 00 MASTER BLUEPRINT & SP01
-- ==============================================================================

-- 1. 厂商表 (Brands)
CREATE TABLE IF NOT EXISTS brands (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    bl_unlock_status TEXT NOT NULL,
    bl_unlock_guide_md TEXT,
    sort_order INTEGER DEFAULT 100
);

-- 2. 厂商通用刷机工具表 (Brand Universal Tools)
CREATE TABLE IF NOT EXISTS brand_universal_tools (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    public_token TEXT NOT NULL UNIQUE,
    brand_id TEXT NOT NULL REFERENCES brands(id),
    tool_name TEXT NOT NULL,
    version_str TEXT,
    os_platform TEXT NOT NULL,
    description TEXT,
    target_redirect_url TEXT NOT NULL,
    ed25519_signature TEXT NOT NULL
);

-- 3. 设备档案主表 (Devices)
CREATE TABLE IF NOT EXISTS devices (
    codename TEXT PRIMARY KEY,
    brand_id TEXT NOT NULL REFERENCES brands(id),
    marketing_name TEXT NOT NULL,
    model_numbers_json TEXT NOT NULL DEFAULT '[]',
    soc_name TEXT,
    boot_partition_type TEXT NOT NULL DEFAULT 'init_boot', -- 'init_boot' 或 'boot'
    has_vbmeta INTEGER DEFAULT 1,
    flashing_notes_md TEXT,
    phase2_roms_enabled INTEGER DEFAULT 0,
    phase3_parts_enabled INTEGER DEFAULT 0,
    updated_at INTEGER DEFAULT (strftime('%s', 'now'))
);

-- 4. 设备硬件维修配件规格表 (Device Parts Specs)
CREATE TABLE IF NOT EXISTS device_parts_specs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_codename TEXT NOT NULL REFERENCES devices(codename),
    part_type TEXT NOT NULL,
    part_model_code TEXT NOT NULL,
    spec_summary TEXT NOT NULL,
    repair_warning TEXT
);

-- 5. 官方固件与流式提取队列表 (Stock Firmwares)
CREATE TABLE IF NOT EXISTS stock_firmwares (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    public_token TEXT NOT NULL UNIQUE,
    device_codename TEXT NOT NULL REFERENCES devices(codename),
    region_code TEXT NOT NULL,
    model_code TEXT,
    version_str TEXT NOT NULL,
    android_version TEXT NOT NULL,
    package_type TEXT NOT NULL,            -- 'recovery_zip' 或 'fastboot_tgz'
    file_size_str TEXT,
    md5_or_sha256 TEXT,
    original_official_url TEXT NOT NULL,
    fast_cdn_url TEXT NOT NULL,
    published_date TEXT,
    ed25519_signature TEXT NOT NULL,       -- 对 v1|fw|{public_token}|{fast_cdn_url} 的签名
    extract_status TEXT DEFAULT 'pending', -- 'pending', 'processing', 'done', 'failed', 'skipped'
    extract_retries INTEGER DEFAULT 0,
    extract_error TEXT,
    updated_at INTEGER DEFAULT (strftime('%s', 'now')),
    UNIQUE(device_codename, region_code, version_str, package_type)
);

-- 6. 独立提取与预修补引导镜像表 (Extracted Boot Images)
CREATE TABLE IF NOT EXISTS extracted_boot_images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    public_token TEXT NOT NULL UNIQUE,
    firmware_id INTEGER NOT NULL REFERENCES stock_firmwares(id),
    device_codename TEXT NOT NULL,
    version_str TEXT NOT NULL,
    partition_name TEXT NOT NULL,          -- 'init_boot', 'boot', 'vendor_boot', 'vbmeta'
    variant_type TEXT NOT NULL,            -- 'stock', 'magisk', 'kernelsu', 'apatch'
    patch_tool_ver TEXT,
    file_name TEXT NOT NULL,
    file_size_bytes INTEGER NOT NULL,
    sha256_hash TEXT NOT NULL,
    gh_private_repo TEXT NOT NULL,
    gh_release_tag TEXT NOT NULL,
    gh_asset_id INTEGER NOT NULL,
    ed25519_signature TEXT NOT NULL,       -- 对 v1|bt|{public_token}|{sha256_hash}|{gh_private_repo}|{gh_asset_id} 的签名
    download_count INTEGER DEFAULT 0,
    working_votes INTEGER DEFAULT 0,
    broken_votes INTEGER DEFAULT 0,
    created_at INTEGER DEFAULT (strftime('%s', 'now')),
    UNIQUE(firmware_id, partition_name, variant_type)
);

-- 7. 第三方刷机资源表 (Custom Resources)
CREATE TABLE IF NOT EXISTS custom_resources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    public_token TEXT NOT NULL UNIQUE,
    device_codename TEXT NOT NULL REFERENCES devices(codename),
    zone_type TEXT NOT NULL,               -- 'free_rom', 'paid_rom', 'essential_tool'
    category_tag TEXT NOT NULL,
    project_name TEXT NOT NULL,
    is_official INTEGER DEFAULT 0,
    android_version TEXT,
    build_version_tag TEXT,
    maintainer_name TEXT,
    maintainer_contact_tg TEXT,
    maintainer_contact_email TEXT,
    maintainer_opted_out INTEGER DEFAULT 0,
    target_post_url TEXT NOT NULL,
    backup_gh_asset_id INTEGER,
    companion_recovery_token TEXT,
    firmware_req_note TEXT,
    gapps_type TEXT,
    known_bugs_json TEXT DEFAULT '[]',
    ext_download_count INTEGER DEFAULT 0,
    site_click_count INTEGER DEFAULT 0,
    ed25519_signature TEXT NOT NULL,       -- 对 v1|cr|{public_token}|{zone_type}|{target_post_url} 的签名
    working_votes INTEGER DEFAULT 0,
    broken_votes INTEGER DEFAULT 0,
    published_at INTEGER,
    UNIQUE(device_codename, project_name, build_version_tag, target_post_url)
);

-- 8. 爬虫信源与增量游标表 (Crawler Sources)
CREATE TABLE IF NOT EXISTS crawler_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_codename TEXT NOT NULL REFERENCES devices(codename),
    source_kind TEXT NOT NULL,
    source_target TEXT NOT NULL,
    last_cursor_val TEXT,
    last_checked_at INTEGER,
    is_active INTEGER DEFAULT 1,
    UNIQUE(device_codename, source_kind, source_target)
);

-- 索引定义 (Indexes)
CREATE INDEX IF NOT EXISTS idx_fw_device_ver ON stock_firmwares(device_codename, published_date DESC);
CREATE INDEX IF NOT EXISTS idx_fw_queue ON stock_firmwares(extract_status, extract_retries);
CREATE INDEX IF NOT EXISTS idx_boot_search ON extracted_boot_images(device_codename, version_str, partition_name);
CREATE INDEX IF NOT EXISTS idx_custom_alpha ON custom_resources(device_codename, zone_type, project_name ASC);
CREATE INDEX IF NOT EXISTS idx_custom_pop ON custom_resources(device_codename, zone_type, (ext_download_count + site_click_count) DESC);
