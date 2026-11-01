"""Explicit, bounded repair of complete ZIPs previously published as skipped.

This does not touch failed attempts, active claims, completed images, or URLs.
It runs on the cloud extraction worker under the production-extraction lock.
"""
from fmr_core.extraction_policy import extraction_eligible


def requeue_skipped(db, *, dry_run=False):
    rows = db.query("""SELECT f.id,d.brand_id,f.package_type,f.fast_cdn_url
        FROM stock_firmwares f JOIN devices d ON d.codename=f.device_codename
        WHERE f.extract_status='skipped' AND f.extract_retries=0
        AND f.extract_error IS NULL
        AND NOT EXISTS (SELECT 1 FROM extracted_boot_images b WHERE b.firmware_id=f.id)
        ORDER BY f.id LIMIT 5000"""
    )
    ids = [r['id'] for r in rows if extraction_eligible(
        r['brand_id'], r['package_type'], r['fast_cdn_url'])]
    changed = 0
    if not dry_run:
        for offset in range(0, len(ids), 50):
            batch = ids[offset:offset + 50]
            changed += db.execute("""UPDATE stock_firmwares SET extract_status='pending'
                WHERE extract_status='skipped' AND extract_retries=0 AND extract_error IS NULL
                AND NOT EXISTS (SELECT 1 FROM extracted_boot_images b WHERE b.firmware_id=stock_firmwares.id)
                AND id IN (""" + ','.join('?' for _ in batch) + ')', batch)['changes']
    return {'eligible': len(ids), 'requeued': changed, 'dry_run': dry_run,
            'scan_limit_reached': len(rows) == 5000}
