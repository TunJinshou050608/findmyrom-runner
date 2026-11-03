"""Publish the saved audit, independently of the Actions publication run ID."""
import argparse
import json
import os
import re
from pathlib import Path

from .quality_audit import DB_ID, publish, verify_site


def read_json(path):
    return json.loads(path.read_text()) if path.exists() else None


def publish_latest(root, db_factory=None, publisher=publish, verifier=verify_site):
    root = Path(root)
    latest = read_json(root / 'audits/full-quality-latest.json')
    if latest is None:
        return {'status': 'no_audit'}
    run_id = str(latest['audit_run_id'])
    if not re.fullmatch(r'[0-9]+', run_id):
        raise ValueError('Invalid saved audit run ID')
    out = root / 'audits' / ('full-quality-' + run_id)
    if read_json(out / 'report.json') != latest:
        raise ValueError('Latest audit manifest does not match the saved cohort')
    receipt = read_json(out / 'publication_result.json')
    if receipt is not None and str(receipt.get('audit_run_id')) != run_id:
        raise ValueError('Publication receipt belongs to another audit')
    if receipt is None:
        if os.environ.get('CF_D1_DATABASE_ID') != DB_ID:
            raise ValueError('Production D1 identity must match the established database')
        if db_factory is None:
            from fmr_core.d1_client import D1Client
            db_factory = lambda: D1Client(cf_database_id=DB_ID)
        db = db_factory()
        try:
            publisher(out, db, os.environ.get('SIGN_PRIVATE_KEY_HEX', '').strip())
        finally:
            db.close()
    # A website failure must retry verification without rewriting an imported cohort.
    if not (out / 'website_verification.json').exists():
        verifier(out)
        return {'status': 'verified', 'audit_run_id': run_id}
    return {'status': 'unchanged', 'audit_run_id': run_id}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--state-dir', required=True)
    args = parser.parse_args()
    print(json.dumps(publish_latest(args.state_dir)))


if __name__ == '__main__':
    main()
