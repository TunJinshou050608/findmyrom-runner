DROP TRIGGER search_fw_insert;
DROP TRIGGER search_fw_delete;
DROP TRIGGER search_fw_update;
-- Capacity-reviewed atomic parent/child rebuild. Copy both tables so SQLite
-- never drops a referenced parent and leaves a deferred-FK violation counter.
-- IDs, public tokens, signatures, metrics and extraction state are unchanged.
DROP TRIGGER coverage_device_insert;
DROP TRIGGER coverage_device_delete;
DROP TRIGGER coverage_device_update;
DROP TRIGGER coverage_fw_insert;
DROP TRIGGER coverage_fw_delete;
DROP TRIGGER coverage_fw_update;
DROP TRIGGER coverage_custom_insert;
DROP TRIGGER coverage_custom_delete;
DROP TRIGGER coverage_custom_update;
CREATE TABLE _fmr_identity_sequence(name TEXT PRIMARY KEY,seq INTEGER NOT NULL);
INSERT INTO _fmr_identity_sequence SELECT name,seq FROM sqlite_sequence WHERE name IN ('stock_firmwares','extracted_boot_images');
CREATE TABLE stock_firmwares_next (
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
    updated_at INTEGER DEFAULT (strftime('%s', 'now'))
);
INSERT INTO stock_firmwares_next(id,public_token,device_codename,region_code,model_code,version_str,android_version,package_type,file_size_str,md5_or_sha256,original_official_url,fast_cdn_url,published_date,ed25519_signature,extract_status,extract_retries,extract_error,updated_at) SELECT id,public_token,device_codename,region_code,model_code,version_str,android_version,package_type,file_size_str,md5_or_sha256,original_official_url,fast_cdn_url,published_date,ed25519_signature,extract_status,extract_retries,extract_error,updated_at FROM stock_firmwares;
CREATE TABLE extracted_boot_images_next (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    public_token TEXT NOT NULL UNIQUE,
    firmware_id INTEGER NOT NULL REFERENCES stock_firmwares_next(id),
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
INSERT INTO extracted_boot_images_next(id,public_token,firmware_id,device_codename,version_str,partition_name,variant_type,patch_tool_ver,file_name,file_size_bytes,sha256_hash,gh_private_repo,gh_release_tag,gh_asset_id,ed25519_signature,download_count,working_votes,broken_votes,created_at) SELECT id,public_token,firmware_id,device_codename,version_str,partition_name,variant_type,patch_tool_ver,file_name,file_size_bytes,sha256_hash,gh_private_repo,gh_release_tag,gh_asset_id,ed25519_signature,download_count,working_votes,broken_votes,created_at FROM extracted_boot_images;
DROP TABLE extracted_boot_images;
DROP TABLE stock_firmwares;
ALTER TABLE stock_firmwares_next RENAME TO stock_firmwares;
ALTER TABLE extracted_boot_images_next RENAME TO extracted_boot_images;
UPDATE sqlite_sequence SET seq=MAX(seq,COALESCE((SELECT seq FROM _fmr_identity_sequence saved WHERE saved.name=sqlite_sequence.name),0)) WHERE name IN ('stock_firmwares','extracted_boot_images');
INSERT INTO sqlite_sequence(name,seq) SELECT name,seq FROM _fmr_identity_sequence saved WHERE NOT EXISTS(SELECT 1 FROM sqlite_sequence current WHERE current.name=saved.name);
DROP TABLE _fmr_identity_sequence;
CREATE UNIQUE INDEX idx_fw_identity ON stock_firmwares(device_codename,COALESCE(NULLIF(model_code,''),device_codename),region_code,version_str,package_type);
CREATE INDEX idx_fw_device_ver ON stock_firmwares(device_codename,published_date DESC);
CREATE INDEX idx_fw_queue ON stock_firmwares(extract_status,extract_retries);
CREATE INDEX idx_fw_pending_order ON stock_firmwares(COALESCE(published_date,'') DESC,id DESC) WHERE extract_status='pending' AND extract_retries<3;
CREATE INDEX idx_boot_search ON extracted_boot_images(device_codename,version_str,partition_name);
CREATE TRIGGER coverage_device_insert AFTER INSERT ON devices
WHEN NEW.codename!='__global__' AND NEW.brand_id NOT IN ('huawei','honor') AND NEW.codename NOT LIKE 'huawei-%' AND NEW.codename NOT LIKE 'honor-%' AND NEW.marketing_name NOT LIKE '%Chromebook%'
BEGIN
 UPDATE catalogue_coverage SET devices=devices+CASE WHEN (NEW.codename!='__global__' AND NEW.brand_id NOT IN ('huawei','honor') AND NEW.codename NOT LIKE 'huawei-%' AND NEW.codename NOT LIKE 'honor-%' AND NEW.marketing_name NOT LIKE '%Chromebook%') THEN 1 ELSE 0 END,
 official=official+(CASE WHEN (NEW.codename!='__global__' AND NEW.brand_id NOT IN ('huawei','honor') AND NEW.codename NOT LIKE 'huawei-%' AND NEW.codename NOT LIKE 'honor-%' AND NEW.marketing_name NOT LIKE '%Chromebook%') THEN (SELECT COUNT(*) FROM (SELECT 1 FROM stock_firmwares f WHERE f.device_codename=NEW.codename GROUP BY COALESCE(NULLIF(f.model_code,''),f.device_codename),f.region_code,f.version_str)) ELSE 0 END),custom=custom+(CASE WHEN (NEW.codename!='__global__' AND NEW.brand_id NOT IN ('huawei','honor') AND NEW.codename NOT LIKE 'huawei-%' AND NEW.codename NOT LIKE 'honor-%' AND NEW.marketing_name NOT LIKE '%Chromebook%') AND NEW.phase2_roms_enabled=1 THEN (SELECT COUNT(*) FROM custom_resources r WHERE r.device_codename=NEW.codename AND r.maintainer_opted_out=0 AND r.zone_type='free_rom') ELSE 0 END) WHERE id=1;
END;
CREATE TRIGGER coverage_device_delete AFTER DELETE ON devices
WHEN OLD.codename!='__global__' AND OLD.brand_id NOT IN ('huawei','honor') AND OLD.codename NOT LIKE 'huawei-%' AND OLD.codename NOT LIKE 'honor-%' AND OLD.marketing_name NOT LIKE '%Chromebook%'
BEGIN
 UPDATE catalogue_coverage SET devices=devices-CASE WHEN (OLD.codename!='__global__' AND OLD.brand_id NOT IN ('huawei','honor') AND OLD.codename NOT LIKE 'huawei-%' AND OLD.codename NOT LIKE 'honor-%' AND OLD.marketing_name NOT LIKE '%Chromebook%') THEN 1 ELSE 0 END,
 official=official-(CASE WHEN (OLD.codename!='__global__' AND OLD.brand_id NOT IN ('huawei','honor') AND OLD.codename NOT LIKE 'huawei-%' AND OLD.codename NOT LIKE 'honor-%' AND OLD.marketing_name NOT LIKE '%Chromebook%') THEN (SELECT COUNT(*) FROM (SELECT 1 FROM stock_firmwares f WHERE f.device_codename=OLD.codename GROUP BY COALESCE(NULLIF(f.model_code,''),f.device_codename),f.region_code,f.version_str)) ELSE 0 END),custom=custom-(CASE WHEN (OLD.codename!='__global__' AND OLD.brand_id NOT IN ('huawei','honor') AND OLD.codename NOT LIKE 'huawei-%' AND OLD.codename NOT LIKE 'honor-%' AND OLD.marketing_name NOT LIKE '%Chromebook%') AND OLD.phase2_roms_enabled=1 THEN (SELECT COUNT(*) FROM custom_resources r WHERE r.device_codename=OLD.codename AND r.maintainer_opted_out=0 AND r.zone_type='free_rom') ELSE 0 END) WHERE id=1;
END;
CREATE TRIGGER coverage_device_update AFTER UPDATE OF codename,brand_id,marketing_name,phase2_roms_enabled ON devices
WHEN OLD.codename IS NOT NEW.codename OR (OLD.codename!='__global__' AND OLD.brand_id NOT IN ('huawei','honor') AND OLD.codename NOT LIKE 'huawei-%' AND OLD.codename NOT LIKE 'honor-%' AND OLD.marketing_name NOT LIKE '%Chromebook%')!=(NEW.codename!='__global__' AND NEW.brand_id NOT IN ('huawei','honor') AND NEW.codename NOT LIKE 'huawei-%' AND NEW.codename NOT LIKE 'honor-%' AND NEW.marketing_name NOT LIKE '%Chromebook%') OR OLD.phase2_roms_enabled IS NOT NEW.phase2_roms_enabled
BEGIN
 UPDATE catalogue_coverage SET
 devices=devices-CASE WHEN (OLD.codename!='__global__' AND OLD.brand_id NOT IN ('huawei','honor') AND OLD.codename NOT LIKE 'huawei-%' AND OLD.codename NOT LIKE 'honor-%' AND OLD.marketing_name NOT LIKE '%Chromebook%') THEN 1 ELSE 0 END+CASE WHEN (NEW.codename!='__global__' AND NEW.brand_id NOT IN ('huawei','honor') AND NEW.codename NOT LIKE 'huawei-%' AND NEW.codename NOT LIKE 'honor-%' AND NEW.marketing_name NOT LIKE '%Chromebook%') THEN 1 ELSE 0 END,
 official=official+CASE WHEN OLD.codename IS NOT NEW.codename OR (OLD.codename!='__global__' AND OLD.brand_id NOT IN ('huawei','honor') AND OLD.codename NOT LIKE 'huawei-%' AND OLD.codename NOT LIKE 'honor-%' AND OLD.marketing_name NOT LIKE '%Chromebook%')!=(NEW.codename!='__global__' AND NEW.brand_id NOT IN ('huawei','honor') AND NEW.codename NOT LIKE 'huawei-%' AND NEW.codename NOT LIKE 'honor-%' AND NEW.marketing_name NOT LIKE '%Chromebook%') THEN (CASE WHEN (NEW.codename!='__global__' AND NEW.brand_id NOT IN ('huawei','honor') AND NEW.codename NOT LIKE 'huawei-%' AND NEW.codename NOT LIKE 'honor-%' AND NEW.marketing_name NOT LIKE '%Chromebook%') THEN (SELECT COUNT(*) FROM (SELECT 1 FROM stock_firmwares f WHERE f.device_codename=NEW.codename GROUP BY COALESCE(NULLIF(f.model_code,''),f.device_codename),f.region_code,f.version_str)) ELSE 0 END)-(CASE WHEN (OLD.codename!='__global__' AND OLD.brand_id NOT IN ('huawei','honor') AND OLD.codename NOT LIKE 'huawei-%' AND OLD.codename NOT LIKE 'honor-%' AND OLD.marketing_name NOT LIKE '%Chromebook%') THEN (SELECT COUNT(*) FROM (SELECT 1 FROM stock_firmwares f WHERE f.device_codename=OLD.codename GROUP BY COALESCE(NULLIF(f.model_code,''),f.device_codename),f.region_code,f.version_str)) ELSE 0 END) ELSE 0 END,
 custom=custom+(CASE WHEN (NEW.codename!='__global__' AND NEW.brand_id NOT IN ('huawei','honor') AND NEW.codename NOT LIKE 'huawei-%' AND NEW.codename NOT LIKE 'honor-%' AND NEW.marketing_name NOT LIKE '%Chromebook%') AND NEW.phase2_roms_enabled=1 THEN (SELECT COUNT(*) FROM custom_resources r WHERE r.device_codename=NEW.codename AND r.maintainer_opted_out=0 AND r.zone_type='free_rom') ELSE 0 END)-(CASE WHEN (OLD.codename!='__global__' AND OLD.brand_id NOT IN ('huawei','honor') AND OLD.codename NOT LIKE 'huawei-%' AND OLD.codename NOT LIKE 'honor-%' AND OLD.marketing_name NOT LIKE '%Chromebook%') AND OLD.phase2_roms_enabled=1 THEN (SELECT COUNT(*) FROM custom_resources r WHERE r.device_codename=OLD.codename AND r.maintainer_opted_out=0 AND r.zone_type='free_rom') ELSE 0 END) WHERE id=1;
END;
CREATE TRIGGER coverage_fw_insert AFTER INSERT ON stock_firmwares
WHEN EXISTS(SELECT 1 FROM devices d WHERE d.codename=NEW.device_codename AND (d.codename!='__global__' AND d.brand_id NOT IN ('huawei','honor') AND d.codename NOT LIKE 'huawei-%' AND d.codename NOT LIKE 'honor-%' AND d.marketing_name NOT LIKE '%Chromebook%')) AND NOT EXISTS(SELECT 1 FROM stock_firmwares f WHERE f.device_codename=NEW.device_codename AND COALESCE(NULLIF(f.model_code,''),f.device_codename)=COALESCE(NULLIF(NEW.model_code,''),NEW.device_codename) AND f.region_code=NEW.region_code AND f.version_str=NEW.version_str AND f.id!=NEW.id)
BEGIN
 UPDATE catalogue_coverage SET official=official+1 WHERE id=1;
END;
CREATE TRIGGER coverage_fw_delete AFTER DELETE ON stock_firmwares
WHEN EXISTS(SELECT 1 FROM devices d WHERE d.codename=OLD.device_codename AND (d.codename!='__global__' AND d.brand_id NOT IN ('huawei','honor') AND d.codename NOT LIKE 'huawei-%' AND d.codename NOT LIKE 'honor-%' AND d.marketing_name NOT LIKE '%Chromebook%')) AND NOT EXISTS(SELECT 1 FROM stock_firmwares f WHERE f.device_codename=OLD.device_codename AND COALESCE(NULLIF(f.model_code,''),f.device_codename)=COALESCE(NULLIF(OLD.model_code,''),OLD.device_codename) AND f.region_code=OLD.region_code AND f.version_str=OLD.version_str)
BEGIN
 UPDATE catalogue_coverage SET official=official-1 WHERE id=1;
END;
CREATE TRIGGER coverage_fw_update AFTER UPDATE OF device_codename,model_code,region_code,version_str ON stock_firmwares
WHEN OLD.device_codename IS NOT NEW.device_codename OR COALESCE(NULLIF(OLD.model_code,''),OLD.device_codename) IS NOT COALESCE(NULLIF(NEW.model_code,''),NEW.device_codename) OR OLD.region_code IS NOT NEW.region_code OR OLD.version_str IS NOT NEW.version_str
BEGIN
 UPDATE catalogue_coverage SET official=official-1 WHERE id=1 AND EXISTS(SELECT 1 FROM devices d WHERE d.codename=OLD.device_codename AND (d.codename!='__global__' AND d.brand_id NOT IN ('huawei','honor') AND d.codename NOT LIKE 'huawei-%' AND d.codename NOT LIKE 'honor-%' AND d.marketing_name NOT LIKE '%Chromebook%')) AND NOT EXISTS(SELECT 1 FROM stock_firmwares f WHERE f.device_codename=OLD.device_codename AND COALESCE(NULLIF(f.model_code,''),f.device_codename)=COALESCE(NULLIF(OLD.model_code,''),OLD.device_codename) AND f.region_code=OLD.region_code AND f.version_str=OLD.version_str);
 UPDATE catalogue_coverage SET official=official+1 WHERE id=1 AND EXISTS(SELECT 1 FROM devices d WHERE d.codename=NEW.device_codename AND (d.codename!='__global__' AND d.brand_id NOT IN ('huawei','honor') AND d.codename NOT LIKE 'huawei-%' AND d.codename NOT LIKE 'honor-%' AND d.marketing_name NOT LIKE '%Chromebook%')) AND NOT EXISTS(SELECT 1 FROM stock_firmwares f WHERE f.device_codename=NEW.device_codename AND COALESCE(NULLIF(f.model_code,''),f.device_codename)=COALESCE(NULLIF(NEW.model_code,''),NEW.device_codename) AND f.region_code=NEW.region_code AND f.version_str=NEW.version_str AND f.id!=NEW.id);
END;
CREATE TRIGGER coverage_custom_insert AFTER INSERT ON custom_resources
WHEN (NEW.maintainer_opted_out=0 AND NEW.zone_type='free_rom' AND EXISTS(SELECT 1 FROM devices d WHERE d.codename=NEW.device_codename AND (d.codename!='__global__' AND d.brand_id NOT IN ('huawei','honor') AND d.codename NOT LIKE 'huawei-%' AND d.codename NOT LIKE 'honor-%' AND d.marketing_name NOT LIKE '%Chromebook%') AND d.phase2_roms_enabled=1))
BEGIN
 UPDATE catalogue_coverage SET custom=custom+1 WHERE id=1;
END;
CREATE TRIGGER coverage_custom_delete AFTER DELETE ON custom_resources
WHEN (OLD.maintainer_opted_out=0 AND OLD.zone_type='free_rom' AND EXISTS(SELECT 1 FROM devices d WHERE d.codename=OLD.device_codename AND (d.codename!='__global__' AND d.brand_id NOT IN ('huawei','honor') AND d.codename NOT LIKE 'huawei-%' AND d.codename NOT LIKE 'honor-%' AND d.marketing_name NOT LIKE '%Chromebook%') AND d.phase2_roms_enabled=1))
BEGIN
 UPDATE catalogue_coverage SET custom=custom-1 WHERE id=1;
END;
CREATE TRIGGER coverage_custom_update AFTER UPDATE OF device_codename,zone_type,maintainer_opted_out ON custom_resources
WHEN (OLD.device_codename IS NOT NEW.device_codename OR OLD.zone_type IS NOT NEW.zone_type OR OLD.maintainer_opted_out IS NOT NEW.maintainer_opted_out)
BEGIN
 UPDATE catalogue_coverage SET custom=custom-CASE WHEN (OLD.maintainer_opted_out=0 AND OLD.zone_type='free_rom' AND EXISTS(SELECT 1 FROM devices d WHERE d.codename=OLD.device_codename AND (d.codename!='__global__' AND d.brand_id NOT IN ('huawei','honor') AND d.codename NOT LIKE 'huawei-%' AND d.codename NOT LIKE 'honor-%' AND d.marketing_name NOT LIKE '%Chromebook%') AND d.phase2_roms_enabled=1)) THEN 1 ELSE 0 END+CASE WHEN (NEW.maintainer_opted_out=0 AND NEW.zone_type='free_rom' AND EXISTS(SELECT 1 FROM devices d WHERE d.codename=NEW.device_codename AND (d.codename!='__global__' AND d.brand_id NOT IN ('huawei','honor') AND d.codename NOT LIKE 'huawei-%' AND d.codename NOT LIKE 'honor-%' AND d.marketing_name NOT LIKE '%Chromebook%') AND d.phase2_roms_enabled=1)) THEN 1 ELSE 0 END WHERE id=1;
END;
CREATE TRIGGER search_fw_insert AFTER INSERT ON stock_firmwares BEGIN
 INSERT INTO firmware_search(device_codename,region_code,versions) VALUES (NEW.device_codename,NEW.region_code,CHAR(31)||NEW.version_str||CHAR(31))
 ON CONFLICT(device_codename,region_code) DO UPDATE SET versions=versions||NEW.version_str||CHAR(31)
 WHERE INSTR(versions,CHAR(31)||NEW.version_str||CHAR(31))=0;
END;
CREATE TRIGGER search_fw_delete AFTER DELETE ON stock_firmwares BEGIN
 UPDATE firmware_search SET versions=COALESCE((SELECT CHAR(31)||GROUP_CONCAT(version_str,CHAR(31))||CHAR(31) FROM (SELECT DISTINCT version_str FROM stock_firmwares WHERE device_codename=OLD.device_codename AND region_code=OLD.region_code)),CHAR(31))
 WHERE device_codename=OLD.device_codename AND region_code=OLD.region_code
 AND NOT EXISTS(SELECT 1 FROM stock_firmwares f WHERE f.device_codename=OLD.device_codename AND f.region_code=OLD.region_code AND f.version_str=OLD.version_str);
 DELETE FROM firmware_search WHERE device_codename=OLD.device_codename AND region_code=OLD.region_code AND versions=CHAR(31);
END;
CREATE TRIGGER search_fw_update AFTER UPDATE OF device_codename,region_code,version_str ON stock_firmwares
WHEN OLD.device_codename IS NOT NEW.device_codename OR OLD.region_code IS NOT NEW.region_code OR OLD.version_str IS NOT NEW.version_str
BEGIN
 UPDATE firmware_search SET versions=COALESCE((SELECT CHAR(31)||GROUP_CONCAT(version_str,CHAR(31))||CHAR(31) FROM (SELECT DISTINCT version_str FROM stock_firmwares WHERE device_codename=OLD.device_codename AND region_code=OLD.region_code)),CHAR(31))
 WHERE device_codename=OLD.device_codename AND region_code=OLD.region_code
 AND NOT EXISTS(SELECT 1 FROM stock_firmwares f WHERE f.device_codename=OLD.device_codename AND f.region_code=OLD.region_code AND f.version_str=OLD.version_str);
 DELETE FROM firmware_search WHERE device_codename=OLD.device_codename AND region_code=OLD.region_code AND versions=CHAR(31);
 INSERT INTO firmware_search(device_codename,region_code,versions) VALUES (NEW.device_codename,NEW.region_code,CHAR(31)||NEW.version_str||CHAR(31))
 ON CONFLICT(device_codename,region_code) DO UPDATE SET versions=versions||NEW.version_str||CHAR(31)
 WHERE INSTR(versions,CHAR(31)||NEW.version_str||CHAR(31))=0;
END;
