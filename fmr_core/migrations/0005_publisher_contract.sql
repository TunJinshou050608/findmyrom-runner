-- Additive columns are checked by the shared migration runner before this file.
CREATE INDEX IF NOT EXISTS idx_custom_audit_batch ON custom_resources(audit_batch_id);
CREATE INDEX IF NOT EXISTS idx_custom_audit_owner ON custom_resources(json_extract(CASE WHEN json_valid(metadata_json) THEN metadata_json ELSE '{}' END,'$.audit_version'),id) WHERE maintainer_opted_out=0;
-- Compact substring-search text, one row per device/region rather than per
-- firmware. No LIKE capability is removed. Control separators cannot match
-- validated public query parameters. Inserts append only genuinely new versions;
-- rare deletes/rekeys rebuild the affected region, not the whole catalogue.
CREATE TABLE firmware_search (
 device_codename TEXT NOT NULL,region_code TEXT NOT NULL,versions TEXT NOT NULL,
 PRIMARY KEY(device_codename,region_code)
) WITHOUT ROWID;
INSERT INTO firmware_search
SELECT device_codename,region_code,CHAR(31)||GROUP_CONCAT(version_str,CHAR(31))||CHAR(31)
FROM (SELECT DISTINCT device_codename,region_code,version_str FROM stock_firmwares)
GROUP BY device_codename,region_code;
CREATE INDEX IF NOT EXISTS idx_fw_pending_order ON stock_firmwares(COALESCE(published_date,'') DESC,id DESC) WHERE extract_status='pending' AND extract_retries<3;
DROP TABLE IF EXISTS vote_rate_limits;

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
