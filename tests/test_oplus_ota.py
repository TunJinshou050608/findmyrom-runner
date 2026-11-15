import base64
import json
import httpx
import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from fmr_ingest_stock.oplus_ota import (
    OPLUS_USER_ID,
    REGION_CONFIGS,
    OPlusGateError,
    OPlusIncrementalSync,
    OPlusOTAClient,
    clean_oplus_cdn_url,
    fetch_coloros_official_catalogue,
    fetch_google_play_devices,
    fetch_oss_branch_signals,
    fetch_realme_official_catalogue,
    zero_tail_seed_version,
)


def test_build_and_decrypt_v5_roundtrip():
    key = b'K' * 32
    iv = b'I' * 16
    url, headers, body, session_key = OPlusOTAClient.build_v5_request(
        model='NE2210',
        ota_version='NE2210_11.J.90',
        region='CN',
        key=key,
        iv=iv,
        now_ms=1700000000000,
    )
    assert url == 'https://component-ota-cn.allawntech.com/update/v5'
    assert headers['model'] == 'NE2210'
    assert headers['otaVersion'] == 'NE2210_11.J.90.01_0001_197001010000'
    assert headers['nvCarrier'] == '10010111'
    assert session_key == key
    params = json.loads(body['params'])
    assert base64.b64decode(params['iv']) == iv

    # Verify decrypt_v5_body decodes AES-CTR server body
    resp_iv = b'R' * 16
    sample_resp = {
        'otaVersion': 'NE2210_11.J.94_4940_202606031902',
        'components': [
            {
                'componentPackets': {
                    'manualUrl': 'https://component-ota-cn.allawntech.com/downloadCheck?c=abc',
                    'size': '7476990622',
                    'vabInfo': {
                        'data': {
                            'vab_package_hash': '53e8751e9ce8ad0f1a1fafa289ee53ac',
                            'header': ['android_version=16', 'security_patch=2026-06-01'],
                        }
                    },
                }
            }
        ],
    }
    enc = Cipher(algorithms.AES(key), modes.CTR(resp_iv)).encryptor()
    cipher_b64 = base64.b64encode(enc.update(json.dumps(sample_resp).encode()) + enc.finalize()).decode()
    envelope = {
        'responseCode': 200,
        'body': json.dumps({'cipher': cipher_b64, 'iv': base64.b64encode(resp_iv).decode()}),
    }
    decrypted = OPlusOTAClient.decrypt_v5_body(envelope, key)
    pkt = OPlusOTAClient.parse_packet('NE2210', 'CN', '10010111', headers['otaVersion'], 200, decrypted)
    assert pkt.manual_url == 'https://component-ota-cn.allawntech.com/downloadCheck?c=abc'
    assert pkt.nv_carrier == '10010111'
    assert pkt.md5 == '53e8751e9ce8ad0f1a1fafa289ee53ac'
    assert pkt.size == 7476990622
    assert pkt.android_version == '16'
    assert pkt.security_patch == '2026-06-01'


def test_probe_branches_tries_eea_for_eu():
    seen_models = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_models.append((request.headers['model'], request.headers['otaVersion']))
        return httpx.Response(200, json={'responseCode': 2004, 'body': None})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with OPlusOTAClient(http_client=client) as ota:
        res = ota.probe_branches('CPH2449', region='EU', branches=('A', 'H'))
    assert res == []
    assert seen_models == [
        ('CPH2449', 'CPH2449_11.A.01_0001_197001010000'),
        ('CPH2449', 'CPH2449_11.H.01_0001_197001010000'),
        ('CPH2449EEA', 'CPH2449_11.A.01_0001_197001010000'),
        ('CPH2449EEA', 'CPH2449_11.H.01_0001_197001010000'),
    ]


def test_resolve_download_check_and_rejections():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == '/downloadCheck':
            if request.headers.get('userId') == OPLUS_USER_ID:
                return httpx.Response(302, headers={'Location': 'https://gauss-compota-c-cn.allawnfs.com/pkg.zip?sign=1'})
            return httpx.Response(200, json={'responseCode': 2306})
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with OPlusOTAClient(http_client=client) as ota:
        assert ota.resolve_download_check('https://component-ota-cn.allawntech.com/downloadCheck?c=1') == (
            'https://gauss-compota-c-cn.allawnfs.com/pkg.zip?sign=1'
        )
        with pytest.raises(OPlusGateError):
            ota.resolve_download_check('https://component-ota-cn.allawntech.com/download?c=1')


def test_official_coloros_and_realme_catalogues():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == 'www.coloros.com':
            if request.url.path.endswith('/brandList'):
                return httpx.Response(200, json={'code': 200, 'data': [{'id': 1, 'brandName': 'Find系列'}]})
            if request.url.path.endswith('/productList'):
                return httpx.Response(200, json={'code': 200, 'data': [{'id': '10', 'productName': 'Find X2', 'productCode': 'PDEM10'}]})
            if request.url.path.endswith('/romList'):
                return httpx.Response(200, json={'code': 200, 'data': [{'version': 'A.20', 'fileUrl': 'https://coloroswebsitefs.coloros.com/a.ozip', 'fileMd5': 'abc', 'fileSize': '3GB'}]})
        if request.url.host == 'api-eu.realme.com':
            inner = json.dumps([{'title': 'realme 7i', 'version': 'RMX2191EU_11.A.18', 'downloadUrl': 'https://download.c.realme.com/x.ozip', 'md5': 'def', 'size': '3GB', 'oldList': []}])
            return httpx.Response(200, json={'data': {'content': inner}})
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    cos = fetch_coloros_official_catalogue(http_client=client, include_roms=True)
    assert cos['products'][0]['product_code'] == 'PDEM10'
    assert cos['products'][0]['roms'][0]['file_md5'] == 'abc'

    rm = fetch_realme_official_catalogue(region='eu', http_client=client)
    assert rm[0]['title'] == 'realme 7i'
    assert rm[0]['download_url'] == 'https://download.c.realme.com/x.ozip'


def test_google_play_model_lists():
    csv_text = (
        'Retail Branding,Marketing Name,Device,Model\n'
        'OPPO,OPPO Find X8,OP5D13L1,CPH2651\n'
        'OnePlus,OnePlus 13,OP5CF9L1,CPH2653\n'
        'realme,realme GT 7 Pro,RE5C69,RMX5011\n'
        'motorola,moto g,fogo,XT2435-1\n'
    ).encode('utf-16')

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == 'storage.googleapis.com':
            return httpx.Response(200, content=csv_text)
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    gp = fetch_google_play_devices(http_client=client)
    assert gp['oppo'][0]['model'] == 'CPH2651'
    assert gp['oneplus'][0]['model'] == 'CPH2653'
    assert gp['realme'][0]['model'] == 'RMX5011'
    assert gp['motorola'][0]['model'] == 'XT2435-1'


def test_oss_branch_signals_and_incremental_sync(tmp_path):
    class FakeOTA:
        def __init__(self):
            self.calls = []
            self.target_version = 'PKB110_11.A.90_0900_202508252310'

        def probe_all_regions(self, model, regions=('CN', 'EU', 'IN', 'SG')):
            self.calls.append(('probe_all', model, regions))
            from fmr_ingest_stock.oplus_ota import OPlusPacket
            return [
                OPlusPacket(
                    model=model,
                    region='CN',
                    nv_carrier='10010111',
                    queried_ota_version=f'{model}_11.A.01_0001_197001010000',
                    response_code=200,
                    ota_target_version=self.target_version,
                    version_name='PKB110_15.0.0.860(CN01)',
                    android_version='Android 15',
                    coloros_version='ColorOS 15',
                    security_patch='2025-09-01',
                    manual_url='https://component-ota-cn.allawntech.com/downloadCheck?c=1',
                    auto_url=None,
                    size=7000000000,
                    md5='abc',
                    streaming_property=None,
                    raw_body={},
                )
            ]

        def query_update(self, model, ota_version, region='CN', nv_carrier=None):
            self.calls.append(('snowball', model, ota_version, region, nv_carrier))
            from fmr_ingest_stock.oplus_ota import OPlusPacket
            return OPlusPacket(
                model=model,
                region=region,
                nv_carrier=nv_carrier or '10010111',
                queried_ota_version=ota_version,
                response_code=200,
                ota_target_version=self.target_version,
                version_name='PKB110_16.0.0.100(CN01)',
                android_version='Android 16',
                coloros_version='ColorOS 16',
                security_patch='2026-01-01',
                manual_url='https://component-ota-cn.allawntech.com/downloadCheck?c=2',
                auto_url=None,
                size=7500000000,
                md5='def',
                streaming_property=None,
                raw_body={},
            )

    def git_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'0030refs/heads/oneplus/sm8750_b_16.0.0_ace_6\x00')

    git_client = httpx.Client(transport=httpx.MockTransport(git_handler))
    assert fetch_oss_branch_signals('OnePlusOSS', 'repo', http_client=git_client) == [
        'oneplus/sm8750_b_16.0.0_ace_6'
    ]

    state_file = tmp_path / 'oplus_state.json'
    fake_ota = FakeOTA()
    sync = OPlusIncrementalSync(state_file, ota_client=fake_ota)
    assert sync.register_models([{'model': 'PKB110', 'brand': 'oppo', 'region': 'CN'}], now=100.0) == ['PKB110']
    # Duplicate registration is ignored incrementally
    assert sync.register_models([{'model': 'PKB110', 'brand': 'oppo', 'region': 'CN'}], now=101.0) == []

    # First batch probes new model
    r1 = sync.sync_batch(max_new_models=1, max_track_refreshes=5, refresh_ttl_seconds=3600, now=100.0)
    assert r1['new_models_probed'] == 1
    assert r1['new_or_updated_releases'] == 1

    # Reload from checkpoint and advance clock + vendor pushes new version -> snowball check!
    fake_ota.target_version = 'PKB110_11.A.95_0950_202601010000'
    sync2 = OPlusIncrementalSync(state_file, ota_client=fake_ota)
    r2 = sync2.sync_batch(max_new_models=1, max_track_refreshes=5, refresh_ttl_seconds=3600, now=5000.0)
    assert r2['new_models_probed'] == 0
    assert r2['tracks_refreshed'] == 1
    assert r2['new_or_updated_releases'] == 1
    assert fake_ota.calls[-1][0] == 'snowball'
    assert fake_ota.calls[-1][2] == 'PKB110_11.A.90_0900_202508252310'


def test_v3_pc_region_configs_and_helpers():
    assert REGION_CONFIGS['CN']['pc_url'] == 'https://component-otapc-cn.allawntech.com/update/v3'
    assert REGION_CONFIGS['SG']['pc_url'] == 'https://component-otapc-sg.allawnos.com/update/v3'
    assert REGION_CONFIGS['IN']['pc_url'] == 'https://component-otapc-in.allawnos.com/update/v3'
    assert REGION_CONFIGS['EU']['pc_url'] == 'https://component-otapc-eu.allawnos.com/update/v3'
    assert REGION_CONFIGS['SG']['carriers'] == ('00101100', '00011011')

    seed = zero_tail_seed_version('PJD110', 'A')
    assert seed == 'PJD110_11.A.01_0001_000000000001'
    url, headers, _, _ = OPlusOTAClient.build_v3_pc_request('PJD110', seed, region='CN')
    assert url == 'https://component-otapc-cn.allawntech.com/update/v3'
    assert headers['channel'] == 'pc'
    assert headers['otaVersion'] == seed

    assert (
        clean_oplus_cdn_url(
            'https://gauss-compotacostauto-cn.allawnfs.com/remove-11223344556677889900aabbccddeeff/a/b.zip'
        )
        == 'https://gauss-compotacostauto-cn.allawnfs.com/a/b.zip'
    )
