import json

import pytest

from fmr_tg_scanner.pipeline import DB_ID, publish_latest


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def cohort(root, run_id='123'):
    report = {'audit_run_id': run_id, 'qualified': 2}
    save(root / 'audits/full-quality-latest.json', report)
    out = root / 'audits' / ('full-quality-' + run_id)
    save(out / 'report.json', report)
    return out


def test_no_audit_or_already_verified_never_opens_d1(tmp_path):
    def forbidden(*args):
        pytest.fail('Unchanged state must not access D1 or the website')
    assert publish_latest(tmp_path, forbidden, forbidden, forbidden)['status'] == 'no_audit'
    out = cohort(tmp_path)
    save(out / 'publication_result.json', {'audit_run_id': '123'})
    save(out / 'website_verification.json', {'verified_resources': 2})
    assert publish_latest(tmp_path, forbidden, forbidden, forbidden)['status'] == 'unchanged'


def test_separate_publisher_uses_saved_audit_id_and_retries_website_only(tmp_path, monkeypatch):
    out = cohort(tmp_path)
    monkeypatch.setenv('GITHUB_RUN_ID', '999')
    monkeypatch.setenv('CF_D1_DATABASE_ID', DB_ID)
    calls = []
    class DB:
        def close(self):
            calls.append('closed')
    def publish(path, db, key):
        assert path == out
        calls.append('published')
        save(path / 'publication_result.json', {'audit_run_id': '123'})
    def failed_verify(path):
        raise RuntimeError('Temporary website failure')
    with pytest.raises(RuntimeError):
        publish_latest(tmp_path, DB, publish, failed_verify)
    assert calls == ['published', 'closed']
    def verify(path):
        assert path == out
        calls.append('verified')
        save(path / 'website_verification.json', {'verified_resources': 2})
    publish_latest(tmp_path, lambda: pytest.fail('Must not republish'), publish, verify)
    assert calls == ['published', 'closed', 'verified']


def test_manifest_mismatch_and_path_escape_fail_before_d1(tmp_path):
    out = cohort(tmp_path)
    save(out / 'report.json', {'audit_run_id': '456'})
    with pytest.raises(ValueError, match='manifest'):
        publish_latest(tmp_path)
    save(tmp_path / 'audits/full-quality-latest.json', {'audit_run_id': '../../elsewhere'})
    with pytest.raises(ValueError, match='run ID'):
        publish_latest(tmp_path)
