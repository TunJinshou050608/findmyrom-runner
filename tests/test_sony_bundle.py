from collections import namedtuple
import hashlib
import hmac
import json
from pathlib import Path
import shutil
import zipfile

import httpx
import pytest

import fmr_ingest_stock.sony_bundle as sony_bundle
import fmr_ingest_stock.sony_source as sony_source
from fmr_ingest_stock.sony_source import releases, manifest_digest, validate_manifest
from fmr_ingest_stock.sony_bundle import assemble, Uploader, download_chunk, load, save
from fmr_ingest_stock.device_sync import ensure_base_brands_and_anchor
from fmr_core.d1_client import D1Client


def manifest(data=b'PK complete mock firmware'):
    return {
        'model': 'XQ-TEST',
        'version': '1.0',
        'cdf': '1234',
        'cust': 'Customized HK',
        'service': 'https://app.swup.update.sony.net/ess-distribution/public/api/service',
        'files': [
            {
                'key': 'APP_SW',
                'name': 'firmware.zip',
                'size': len(data),
                'chunks': [{'number': 0, 'size': len(data), 'url': 'https://software.sonymobile.com/content'}],
            }
        ],
    }


def test_sony_source_has_zero_third_party_resource_scrapers():
    assert not hasattr(sony_source, 'RESOURCES')
    assert not hasattr(sony_source, 'RESOURCE_FORMAT_KEY')
    assert not hasattr(sony_source, 'decode_resources')
    assert not hasattr(sony_source, 'devices')


def test_manifest_validates_whole_components_and_rejects_missing_or_foreign_chunks():
    m = manifest()
    assert validate_manifest(m) > 0
    m['files'][0]['chunks'][0]['url'] = 'https://evil.example/content'
    with pytest.raises(ValueError):
        validate_manifest(m)
    m = manifest()
    m['files'][0]['chunks'][0]['number'] = 2
    with pytest.raises(ValueError):
        validate_manifest(m)


def test_official_ess_releases_and_subdomain_https_upgrade():
    root = 'https://app.swup.update.sony.net/ess-distribution/public/api/'
    device = {
        'vendor': 'sony',
        'upstream_id': 'xq-test',
        'codename': 'XQ-TEST',
        'models': 'XQ-TEST',
        'name': 'Xperia (XQ-TEST)',
        'sony': {
            'pid': '1',
            'mid': '2',
            'hwid': '3',
            'custs': [{'id': '1234', 'name': 'Customized HK', 'spc': 'SPC1'}],
        },
    }

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.host == 'app.swup.update.sony.net'
        if req.url.path.endswith('/match/v2'):
            text = (
                f'<root>'
                f'<software-device-service-info><logic-type>SOFTWARE_UPDATE</logic-type><software-version>OLD</software-version><link rel="self" href="{root}old" /></software-device-service-info>'
                f'<software-device-service-info><logic-type>SOFTWARE_UPDATE_CONTENT_ERASE</logic-type><software-version>NEW</software-version><link rel="self" href="{root}service" /></software-device-service-info>'
                f'</root>'
            )
        elif req.url.path.endswith('/service'):
            text = (
                f'<root>'
                f'<file-resource content-id="x"><file-key>APP_SW</file-key><name>firmware.zip</name><plain-size>8</plain-size><link rel="file-chunk-info" href="{root}chunks" /></file-resource>'
                f'</root>'
            )
        else:
            text = (
                '<root>'
                '<file-chunk number="0"><size>4</size><link rel="download" href="http://cdn.dl.sonymobile.com/content0" /></file-chunk>'
                '<file-chunk number="1"><size>4</size><link rel="download" href="http://edge.swup.sony.net/content1" /></file-chunk>'
                '</root>'
            )
        return httpx.Response(200, headers={'Content-Type': 'application/xml'}, text=text)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        _, rows, provenance = releases(device, client)
        assert not provenance['errors'] and len(rows) == 1 and rows[0]['version'] == 'NEW'
        assert rows[0]['kind'] == 'sony_bundle'
        chunks = rows[0]['bundle']['files'][0]['chunks']
        assert [c['url'] for c in chunks] == [
            'https://cdn.dl.sonymobile.com/content0',
            'https://edge.swup.sony.net/content1',
        ]
        assert rows[0]['download_url'] == 'https://findmyrom.com/sony/bundles/' + manifest_digest(rows[0]['bundle'])


def test_assembly_yields_one_zip_checks_peak_disk_and_reuses_checkpoint(tmp_path, monkeypatch):
    payload = b'PK main firmware bytes' * 1000
    extra = b'customer data'
    m = manifest(payload)
    m['files'].append({
        'key': 'CUSTOMER_ID',
        'name': 'customer.zip',
        'size': len(extra),
        'chunks': [{'number': 1, 'size': len(extra), 'url': 'https://software.sonymobile.com/customer'}],
    })
    total = len(payload) + len(extra)
    Usage = namedtuple('Usage', ['total', 'used', 'free'])
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(200, content=extra if req.url.path == '/customer' else payload)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        # Rejects when free < 2 * total + 256 MiB
        monkeypatch.setattr(shutil, 'disk_usage', lambda _: Usage(10**10, 0, 2 * total + 256 * 1024 * 1024 - 1))
        with pytest.raises(ValueError, match='Insufficient cloud runner disk space'):
            assemble(m, tmp_path / 'lowdisk', client)

        monkeypatch.setattr(shutil, 'disk_usage', lambda _: Usage(10**10, 0, 2 * total + 256 * 1024 * 1024))
        path = assemble(m, tmp_path, client)
        assert list(tmp_path.glob('*.part')) == []
        with zipfile.ZipFile(path) as z:
            assert sorted(z.namelist()) == ['customer.zip', 'firmware.zip']
            assert z.read('firmware.zip') == payload and z.read('customer.zip') == extra
            assert z.testzip() is None
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        again = assemble(m, tmp_path, client)
        assert hashlib.sha256(again.read_bytes()).hexdigest() == sha
        assert len(calls) == 2


def test_interrupted_chunk_uses_range_and_never_appends_a_full_200_response(tmp_path):
    data = b'0123456789'
    path = tmp_path / 'chunk.part'
    path.with_suffix('.partial').write_bytes(data[:4])

    def handler(req):
        assert req.headers['Range'] == 'bytes=4-9'
        return httpx.Response(206, headers={'Content-Range': 'bytes 4-9/10'}, content=data[4:])

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        download_chunk(c, 'https://software.sonymobile.com/file', path, 10)
    assert path.read_bytes() == data


def test_upload_auth_matches_worker_ticket_contract():
    def handler(req):
        exp = req.headers['X-FMR-Expires']
        payload = 'v1|sony|' + 'a' * 64 + '|preflight|||' + ('0' * 64) + '|0'
        expected = hmac.new(b'test', f'token={payload}&exp={exp}'.encode(), hashlib.sha256).hexdigest()
        assert req.headers['X-FMR-Signature'] == expected
        return httpx.Response(200, json={'ready': True})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert Uploader(client, 'test').request('a' * 64, 'preflight')['ready']


def test_main_increments_daily_builds_only_after_assemble_and_upload_succeed(tmp_path, monkeypatch):
    sqlite_path = tmp_path / 'bundle_main.sqlite'
    setup_db = D1Client(local_sqlite_path=str(sqlite_path))
    setup_db.execute_script(Path(__file__).parents[1].joinpath('schema.sql').read_text())
    ensure_base_brands_and_anchor(setup_db)
    m = manifest(b'1234')
    key = manifest_digest(m)
    save(setup_db, 'sony_bundle', key, {'manifest': m, 'public_token': 'fw_sony1'})
    save(setup_db, 'sony_bundle_job', key, {'state': 'pending'})
    setup_db.close()

    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    monkeypatch.setenv('SONY_BUNDLE_UPLOAD_SECRET', 'sec')
    monkeypatch.setenv('CF_ACCOUNT_ID', 'acc')
    monkeypatch.setenv('CF_API_TOKEN', 'tok')
    monkeypatch.setenv('CF_D1_DATABASE_ID', 'db')
    monkeypatch.setattr(
        sony_bundle,
        'D1Client',
        lambda **_: D1Client(local_sqlite_path=str(sqlite_path)),
    )

    fail_upload = {'fail': True}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == 'software.sonymobile.com':
            return httpx.Response(200, content=b'1234')
        if req.url.path.endswith('/preflight'):
            return httpx.Response(200, json={'ready': True})
        if req.url.path.endswith('/create'):
            return httpx.Response(200, json={'upload_id': 'up1'})
        if req.url.path.endswith('/part'):
            if fail_upload['fail']:
                return httpx.Response(502, text='bad gateway')
            return httpx.Response(200, json={'partNumber': 1, 'etag': 'e1'})
        if req.url.path.endswith('/abort'):
            return httpx.Response(200, json={'aborted': True})
        if req.url.path.endswith('/complete'):
            return httpx.Response(200, json={'completed': True})
        return httpx.Response(404)

    real_client = httpx.Client
    monkeypatch.setattr(httpx, 'Client', lambda **kw: real_client(transport=httpx.MockTransport(handler)))

    # 1. Upload fails -> daily builds is NOT incremented!
    assert sony_bundle.main() == 1
    check_db = D1Client(local_sqlite_path=str(sqlite_path))
    day = sony_bundle.time.strftime('%Y-%m-%d', sony_bundle.time.gmtime())
    assert load(check_db, 'sony_bundle_control', day) is None
    assert load(check_db, 'sony_bundle_job', key)['state'] == 'failed'

    # 2. Reset job to pending and allow upload to succeed -> daily builds becomes 1!
    save(check_db, 'sony_bundle_job', key, {'state': 'pending'})
    check_db.close()
    fail_upload['fail'] = False
    assert sony_bundle.main() == 0

    check_db2 = D1Client(local_sqlite_path=str(sqlite_path))
    assert load(check_db2, 'sony_bundle_control', day) == {'builds': 1}
    assert load(check_db2, 'sony_bundle_job', key)['state'] == 'ready'
    check_db2.close()
