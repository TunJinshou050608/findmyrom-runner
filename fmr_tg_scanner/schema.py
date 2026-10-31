"""Independent TG01 schema; no main-site foreign keys or D1 access required."""
SCHEMA = '''
CREATE TABLE IF NOT EXISTS tg_scan_cursors (
 channel_username TEXT PRIMARY KEY,
 max_scanned_msg_id INTEGER NOT NULL DEFAULT 0,
 min_scanned_msg_id INTEGER NOT NULL DEFAULT 0,
 history_exhausted INTEGER NOT NULL DEFAULT 0,
 total_packages_found INTEGER NOT NULL DEFAULT 0,
 updated_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
 scan_state_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS tg_discovered_packages (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 channel_username TEXT NOT NULL, msg_id INTEGER NOT NULL, tg_post_url TEXT NOT NULL,
 origin_type TEXT NOT NULL, is_forwarded INTEGER NOT NULL DEFAULT 0,
 original_channel_username TEXT, original_msg_id INTEGER, original_post_url TEXT NOT NULL,
 sender_author_signature TEXT, maintainer_handle TEXT, maintainer_display_name TEXT,
 device_codename TEXT, resource_kind TEXT NOT NULL, project_name TEXT NOT NULL,
 android_version TEXT, build_version_tag TEXT, download_links_json TEXT NOT NULL,
 has_tg_attachment INTEGER NOT NULL DEFAULT 0, file_attachment_name TEXT,
 extraction_method TEXT NOT NULL, rule_confidence REAL NOT NULL,
 published_timestamp INTEGER, raw_text_hash TEXT NOT NULL,
 UNIQUE(channel_username,msg_id)
);
CREATE INDEX IF NOT EXISTS idx_tg_pkg_codename ON tg_discovered_packages(device_codename,published_timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_tg_pkg_maintainer ON tg_discovered_packages(maintainer_handle);
CREATE INDEX IF NOT EXISTS idx_tg_pkg_hash ON tg_discovered_packages(raw_text_hash);
'''
COLUMNS = ('channel_username msg_id tg_post_url origin_type is_forwarded original_channel_username original_msg_id '
 'original_post_url sender_author_signature maintainer_handle maintainer_display_name device_codename resource_kind '
 'project_name android_version build_version_tag download_links_json has_tg_attachment file_attachment_name '
 'extraction_method rule_confidence published_timestamp raw_text_hash').split()


def initialize(db):
    db.execute_script(SCHEMA)
    if 'scan_state_json' not in {c['name'] for c in db.query('PRAGMA table_info(tg_scan_cursors)')}:
        db.execute("ALTER TABLE tg_scan_cursors ADD COLUMN scan_state_json TEXT NOT NULL DEFAULT '{}'")
