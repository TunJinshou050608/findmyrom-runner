-- Apply atomically before deploying the reader. Baseline schema remains frozen.
-- Existing UNIQUE(device_codename,region_code,version_str,package_type) bounds
-- sibling probes to one release; avoid another firmware index.
-- Package types intentionally share a release; preserve the existing homepage semantics.
CREATE INDEX idx_devices_brand_name ON devices(brand_id,marketing_name,codename);
-- Replace the two old zone-prefixed indexes instead of keeping redundant copies.
DROP INDEX idx_custom_alpha;
DROP INDEX idx_custom_pop;
CREATE INDEX idx_custom_alpha ON custom_resources(device_codename,LOWER(project_name),published_at DESC,id ASC) WHERE maintainer_opted_out=0;
CREATE INDEX idx_custom_pop ON custom_resources(device_codename,(ext_download_count+site_click_count) DESC,LOWER(project_name)) WHERE maintainer_opted_out=0;
CREATE INDEX idx_custom_latest ON custom_resources(device_codename,published_at DESC,LOWER(project_name)) WHERE maintainer_opted_out=0;

CREATE TABLE catalogue_coverage (
 id INTEGER PRIMARY KEY CHECK(id=1),
 devices INTEGER NOT NULL CHECK(devices>=0),
 official INTEGER NOT NULL CHECK(official>=0),
 custom INTEGER NOT NULL CHECK(custom>=0)
);
-- One-time backfill. Subsequent traffic never executes this aggregation.
INSERT INTO catalogue_coverage
WITH visible AS (SELECT d.codename FROM devices d WHERE (d.codename!='__global__' AND d.brand_id NOT IN ('huawei','honor') AND d.codename NOT LIKE 'huawei-%' AND d.codename NOT LIKE 'honor-%' AND d.marketing_name NOT LIKE '%Chromebook%'))
SELECT 1,(SELECT COUNT(*) FROM visible),
 (SELECT COUNT(*) FROM (SELECT 1 FROM stock_firmwares f JOIN visible v ON v.codename=f.device_codename GROUP BY f.device_codename,COALESCE(NULLIF(f.model_code,''),f.device_codename),f.region_code,f.version_str)),
 (SELECT COUNT(*) FROM custom_resources r JOIN visible v ON v.codename=r.device_codename JOIN devices d ON d.codename=r.device_codename WHERE r.maintainer_opted_out=0 AND r.zone_type='free_rom' AND d.phase2_roms_enabled=1);

-- Triggers run in the source statement's transaction, including rollback/retry.
-- Unrelated metadata, extraction state, clicks and votes never update coverage.
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
