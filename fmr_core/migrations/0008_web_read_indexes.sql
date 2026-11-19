CREATE INDEX IF NOT EXISTS idx_fw_device_region_ver_date
  ON stock_firmwares(device_codename, region_code, model_code, version_str, published_date DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_fw_device_ver_lookup
  ON stock_firmwares(device_codename, version_str, published_date DESC, id DESC);

CREATE TABLE IF NOT EXISTS device_stock_facets (
  device_codename TEXT NOT NULL,
  region_code TEXT NOT NULL,
  model_key TEXT NOT NULL,
  release_count INTEGER NOT NULL,
  PRIMARY KEY(device_codename, region_code, model_key)
) WITHOUT ROWID;

DELETE FROM device_stock_facets;
INSERT INTO device_stock_facets(device_codename, region_code, model_key, release_count)
SELECT device_codename, region_code, model_key, COUNT(*)
FROM (
  SELECT DISTINCT device_codename, region_code, COALESCE(NULLIF(model_code,''), device_codename) AS model_key, version_str
  FROM stock_firmwares
)
GROUP BY device_codename, region_code, model_key;

CREATE TRIGGER IF NOT EXISTS facets_fw_insert AFTER INSERT ON stock_firmwares
WHEN NOT EXISTS(
  SELECT 1 FROM stock_firmwares f
  WHERE f.device_codename = NEW.device_codename
    AND COALESCE(NULLIF(f.model_code,''), f.device_codename) = COALESCE(NULLIF(NEW.model_code,''), NEW.device_codename)
    AND f.region_code = NEW.region_code
    AND f.version_str = NEW.version_str
    AND f.id != NEW.id
)
BEGIN
  INSERT INTO device_stock_facets(device_codename, region_code, model_key, release_count)
  VALUES (NEW.device_codename, NEW.region_code, COALESCE(NULLIF(NEW.model_code,''), NEW.device_codename), 1)
  ON CONFLICT(device_codename, region_code, model_key) DO UPDATE SET release_count = release_count + 1;
END;

CREATE TRIGGER IF NOT EXISTS facets_fw_delete AFTER DELETE ON stock_firmwares
WHEN NOT EXISTS(
  SELECT 1 FROM stock_firmwares f
  WHERE f.device_codename = OLD.device_codename
    AND COALESCE(NULLIF(f.model_code,''), f.device_codename) = COALESCE(NULLIF(OLD.model_code,''), OLD.device_codename)
    AND f.region_code = OLD.region_code
    AND f.version_str = OLD.version_str
)
BEGIN
  UPDATE device_stock_facets SET release_count = release_count - 1
  WHERE device_codename = OLD.device_codename
    AND region_code = OLD.region_code
    AND model_key = COALESCE(NULLIF(OLD.model_code,''), OLD.device_codename);
  DELETE FROM device_stock_facets
  WHERE device_codename = OLD.device_codename
    AND region_code = OLD.region_code
    AND model_key = COALESCE(NULLIF(OLD.model_code,''), OLD.device_codename)
    AND release_count <= 0;
END;

CREATE TRIGGER IF NOT EXISTS facets_fw_update AFTER UPDATE OF device_codename, model_code, region_code, version_str ON stock_firmwares
WHEN OLD.device_codename IS NOT NEW.device_codename
  OR COALESCE(NULLIF(OLD.model_code,''), OLD.device_codename) IS NOT COALESCE(NULLIF(NEW.model_code,''), NEW.device_codename)
  OR OLD.region_code IS NOT NEW.region_code
  OR OLD.version_str IS NOT NEW.version_str
BEGIN
  UPDATE device_stock_facets SET release_count = release_count - 1
  WHERE device_codename = OLD.device_codename
    AND region_code = OLD.region_code
    AND model_key = COALESCE(NULLIF(OLD.model_code,''), OLD.device_codename)
    AND NOT EXISTS(
      SELECT 1 FROM stock_firmwares f
      WHERE f.device_codename = OLD.device_codename
        AND COALESCE(NULLIF(f.model_code,''), f.device_codename) = COALESCE(NULLIF(OLD.model_code,''), OLD.device_codename)
        AND f.region_code = OLD.region_code
        AND f.version_str = OLD.version_str
    );
  DELETE FROM device_stock_facets
  WHERE device_codename = OLD.device_codename
    AND region_code = OLD.region_code
    AND model_key = COALESCE(NULLIF(OLD.model_code,''), OLD.device_codename)
    AND release_count <= 0;
  INSERT INTO device_stock_facets(device_codename, region_code, model_key, release_count)
  SELECT NEW.device_codename, NEW.region_code, COALESCE(NULLIF(NEW.model_code,''), NEW.device_codename), 1
  WHERE NOT EXISTS(
    SELECT 1 FROM stock_firmwares f
    WHERE f.device_codename = NEW.device_codename
      AND COALESCE(NULLIF(f.model_code,''), f.device_codename) = COALESCE(NULLIF(NEW.model_code,''), NEW.device_codename)
      AND f.region_code = NEW.region_code
      AND f.version_str = NEW.version_str
      AND f.id != NEW.id
  )
  ON CONFLICT(device_codename, region_code, model_key) DO UPDATE SET release_count = release_count + 1;
END;
