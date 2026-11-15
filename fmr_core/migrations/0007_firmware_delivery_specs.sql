CREATE TABLE IF NOT EXISTS firmware_delivery_specs (
    public_token TEXT PRIMARY KEY,
    delivery_mode TEXT NOT NULL,
    resolver_json TEXT NOT NULL CHECK (json_valid(resolver_json)),
    content_hash TEXT NOT NULL,
    updated_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_delivery_specs_hash ON firmware_delivery_specs(content_hash);
