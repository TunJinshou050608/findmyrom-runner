-- Additive columns are checked by the shared migration runner before this file.
CREATE INDEX IF NOT EXISTS idx_custom_audit_batch ON custom_resources(audit_batch_id);
CREATE INDEX IF NOT EXISTS idx_custom_audit_owner ON custom_resources(json_extract(CASE WHEN json_valid(metadata_json) THEN metadata_json ELSE '{}' END,'$.audit_version'),id) WHERE maintainer_opted_out=0;
CREATE INDEX IF NOT EXISTS idx_fw_version_lookup ON stock_firmwares(version_str COLLATE NOCASE,device_codename);
CREATE INDEX IF NOT EXISTS idx_fw_pending_order ON stock_firmwares(COALESCE(published_date,'') DESC,id DESC) WHERE extract_status='pending' AND extract_retries<3;
DROP TABLE IF EXISTS vote_rate_limits;
