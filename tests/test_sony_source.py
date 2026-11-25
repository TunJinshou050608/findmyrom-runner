"""Unit tests for Sony ESS metadata helpers still used by the live adapter."""

import httpx
import pytest

import fmr_ingest_stock.sony_source as sony_source
from fmr_ingest_stock.sony_source import api_url, manifest_digest, releases, validate_manifest, xml


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


def chunk(number, size, url='https://software.sonymobile.com/content'):
    return {'number': number, 'size': size, 'url': url}


def component(name, size, chunks):
    return {'key': name, 'name': name, 'size': size, 'chunks': chunks}


def test_sony_source_has_zero_third_party_resource_scrapers():
    assert not hasattr(sony_source, 'RESOURCES')
    assert not hasattr(sony_source, 'RESOURCE_FORMAT_KEY')
    assert not hasattr(sony_source, 'decode_resources')
    assert not hasattr(sony_source, 'devices')


def test_manifest_digest_is_order_independent():
    m = manifest()
    assert manifest_digest(m) == manifest_digest({key: m[key] for key in reversed(list(m))})


def test_manifest_validates_whole_components_and_rejects_missing_or_foreign_chunks():
    m = manifest()
    assert validate_manifest(m) > 0
    m['files'][0]['chunks'][0]['url'] = 'https://evil.example/content'
    with pytest.raises(ValueError, match='Untrusted Sony chunk'):
        validate_manifest(m)
    m = manifest()
    m['files'][0]['chunks'][0]['number'] = 2
    with pytest.raises(ValueError, match='sequence gap'):
        validate_manifest(m)


def test_manifest_accepts_chunk_sequence_starting_at_one():
    m = manifest(b'1234')
    m['files'][0]['chunks'] = [chunk(1, 4)]
    assert validate_manifest(m) == 4


def test_manifest_rejects_bad_or_duplicate_component_names():
    m = manifest()
    m['files'][0]['name'] = 'a' * 181
    with pytest.raises(ValueError, match='Invalid Sony filename'):
        validate_manifest(m)
    m = manifest()
    m['files'].append(dict(m['files'][0]))
    with pytest.raises(ValueError, match='Invalid Sony filename'):
        validate_manifest(m)


def test_manifest_rejects_more_than_twenty_components():
    m = manifest()
    m['files'] = [component(f'f{i}.zip', 1, [chunk(0, 1)]) for i in range(21)]
    with pytest.raises(ValueError, match='component count'):
        validate_manifest(m)


def test_manifest_enforces_256_mib_chunk_ceiling():
    m = manifest()
    oversized = 256 * 1024 * 1024 + 1
    m['files'] = [component('big.zip', oversized, [chunk(0, oversized)])]
    with pytest.raises(ValueError, match='Untrusted Sony chunk'):
        validate_manifest(m)


def test_manifest_enforces_400_total_chunk_budget():
    m = manifest()
    m['files'] = [
        component('a.zip', 256, [chunk(i, 1) for i in range(256)]),
        component('b.zip', 145, [chunk(i, 1) for i in range(145)]),
    ]
    with pytest.raises(ValueError, match='streaming budget'):
        validate_manifest(m)


def test_manifest_enforces_16_gib_total_ceiling():
    mib = 256 * 1024 * 1024
    m = manifest()
    m['files'] = [component('a.zip', mib * 64, [chunk(i, mib) for i in range(64)])]
    assert validate_manifest(m) == 16 * 1024**3
    m['files'] = [component('a.zip', mib * 65, [chunk(i, mib) for i in range(65)])]
    with pytest.raises(ValueError, match='streaming archive limit'):
        validate_manifest(m)


def test_manifest_rejects_component_size_mismatch():
    m = manifest()
    m['files'][0]['size'] += 1
    with pytest.raises(ValueError, match='size mismatch'):
        validate_manifest(m)


def test_api_url_only_accepts_official_ess_https_paths():
    good = 'https://app.swup.update.sony.net/ess-distribution/public/api/service'
    assert api_url(good) == good
    for bad in (
        'http://app.swup.update.sony.net/ess-distribution/public/api/service',
        'https://evil.example/ess-distribution/public/api/service',
        'https://app.swup.update.sony.net/other/path',
        'https://app.swup.update.sony.net:8443/ess-distribution/public/api/service',
    ):
        with pytest.raises(ValueError, match='Untrusted Sony metadata link'):
            api_url(bad)


def test_xml_rejects_doctype_and_entity_payloads():
    assert xml(b'<root><item>1</item></root>').tag == 'root'
    for payload in (
        b'<!DOCTYPE root [<!ENTITY x "1">]><root>&x;</root>',
        b'<root><!ENTITY x "1">1</root>',
    ):
        with pytest.raises(ValueError, match='Unsafe Sony metadata'):
            xml(payload)


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
