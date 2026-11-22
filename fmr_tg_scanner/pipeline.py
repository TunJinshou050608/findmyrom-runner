"""Publish the saved audit, independently of the Actions publication run ID."""
import argparse
import json
import os
import re
from pathlib import Path

from .quality_audit import DB_ID, publish, verify_site
from .explicit_publisher import publish_explicit_outbox
from .evidence_store import EvidenceStore
from fmr_ingest_stock.d1_budget import D1BudgetReached


def read_json(path):
    return json.loads(path.read_text()) if path.exists() else None


def _pending_explicit_outbox_count(root: Path) -> int:
    db_path = root / 'search-v3/search.sqlite'
    if not db_path.is_file():
        return 0
    store = EvidenceStore(db_path)
    try:
        rows = store.query("SELECT COUNT(*) AS n FROM tg_publish_outbox WHERE status='pending'")
        return int(rows[0]['n']) if rows else 0
    finally:
        store.close()


def publish_latest(root, db_factory=None, publisher=publish, verifier=verify_site):
    root = Path(root)
    pending_outbox = _pending_explicit_outbox_count(root)
    latest = read_json(root / 'audits/full-quality-latest.json')
    if latest is None and pending_outbox == 0:
        return {'status': 'no_audit'}

    explicit_result = None
    if pending_outbox > 0:
        if os.environ.get('CF_D1_DATABASE_ID') != DB_ID:
            raise ValueError('Production D1 identity must match the established database')
        if db_factory is None:
            from fmr_ingest_stock.d1_budget import BudgetDB
            db_factory = lambda: BudgetDB(cf_database_id=DB_ID, enforce_limits=os.getenv('FMR_D1_BUDGET_ENFORCE', 'true') != 'false')
        db = db_factory()
        store = EvidenceStore(root / 'search-v3/search.sqlite')
        try:
            if hasattr(db, 'acquire') and not db.acquire():
                return {'status': 'deferred_budget', 'pending_outbox': pending_outbox}
            if hasattr(db, 'mode'):
                from fmr_core.schema_migrations import ensure_schema
                ensure_schema(db)
            explicit_result = publish_explicit_outbox(
                store,
                db,
                os.environ.get('SIGN_PRIVATE_KEY_HEX', '').strip(),
            )
        except D1BudgetReached:
            return {'status': 'deferred_budget', 'pending_outbox': pending_outbox}
        finally:
            store.close()
            try:
                if hasattr(db, 'settle'):
                    db.settle()
            finally:
                db.close()
        if latest is None:
            return {'status': 'explicit_published', 'explicit': explicit_result}

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
            from fmr_ingest_stock.d1_budget import BudgetDB
            db_factory = lambda: BudgetDB(cf_database_id=DB_ID, enforce_limits=os.getenv('FMR_D1_BUDGET_ENFORCE','true')!='false')
        db = db_factory()
        try:
            if hasattr(db,'acquire') and not db.acquire():return {'status':'deferred_budget','audit_run_id':run_id}
            if db_factory is not None and hasattr(db,'mode'):
                from fmr_core.schema_migrations import ensure_schema
                ensure_schema(db)
            publisher(out, db, os.environ.get('SIGN_PRIVATE_KEY_HEX', '').strip())
        except D1BudgetReached:
            return {'status':'deferred_budget','audit_run_id':run_id}
        finally:
            try:
                if hasattr(db,'settle'):db.settle()
            finally:db.close()
    # A website failure must retry verification without rewriting an imported cohort.
    if not (out / 'website_verification.json').exists():
        verifier(out)
        res = {'status': 'verified', 'audit_run_id': run_id}
        if explicit_result is not None:
            res['explicit'] = explicit_result
        return res
    res = {'status': 'unchanged', 'audit_run_id': run_id}
    if explicit_result is not None:
        res['explicit'] = explicit_result
    return res


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--state-dir', required=True)
    args = parser.parse_args()
    print(json.dumps(publish_latest(args.state_dir)))


if __name__ == '__main__':
    main()
