import base64
import json
import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from fmr_ingest_stock.moto_lmsa import (
    LmsaAuthError,
    MotoIncrementalSync,
    MotoLMSAClient,
    check_cds_ota,
    sign_device_fingerprint,
)


def _gen_rsa_pair():
    priv = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    spki_der = priv.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return priv, base64.b64encode(spki_der).decode('ascii')


def test_sign_device_fingerprint_roundtrip():
    priv, spki_b64 = _gen_rsa_pair()
    fp = sign_device_fingerprint(
        'https://lsa.lenovo.com/Interface/rescueDevice/getModelNames.jhtml',
        'tok123',
        spki_b64,
        timestamp_ms=1700000000123,
    )
    decrypted = priv.decrypt(base64.b64decode(fp), padding.PKCS1v15()).decode('utf-8')
    assert decrypted == '1700000000123|tok123|getModelNames.jhtml'


def test_lmsa_model_list_and_resource_tree_discovery():
    priv, spki_b64 = _gen_rsa_pair()

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith('/common/rsa.jhtml'):
            return httpx.Response(200, json={'code': '0000', 'desc': spki_b64})
        if path.endswith('/dictionary/getApiInfo.jhtml'):
            return httpx.Response(200, json={'code': '0000', 'content': 'https://passport-glb.lenovo.com/v1.0/utility/lenovoid/oauth2/authorize'})

        # Validate fingerprint header on authenticated calls
        fp = request.headers.get('X-Device-Fingerprint')
        assert fp, 'Missing X-Device-Fingerprint'
        plain = priv.decrypt(base64.b64decode(fp), padding.PKCS1v15()).decode('utf-8')
        assert request.headers.get('Authorization') in plain

        body = json.loads(request.content.decode('utf-8'))
        dp = body['dparams']
        if path.endswith('/rescueDevice/getModelNames.jhtml'):
            return httpx.Response(200, json={'code': '0000', 'content': [{'modelName': 'XT2435-1', 'marketName': 'moto g'}]})
        if path.endswith('/rescueDevice/getResource.jhtml'):
            if 'country' not in dp:
                return httpx.Response(200, json={'code': '0000', 'content': [{'paramProperty': {'property': 'country'}, 'paramValues': ['US', 'GB']}]})
            if dp['country'] == 'US':
                return httpx.Response(200, json={'code': '0000', 'content': [{'modelName': 'XT2435-1', 'carrier': 'retus', 'romResource': {'name': 'XT2435-1_US.zip', 'uri': 'https://s3.amazonaws.com/moto/us.zip', 'publishDate': '2026-01-01'}}]})
            return httpx.Response(200, json={'code': '0000', 'content': [{'modelName': 'XT2435-1', 'carrier': 'reteu', 'romResource': {'name': 'XT2435-1_EU.zip', 'uri': 'https://s3.amazonaws.com/moto/eu.zip', 'publishDate': '2026-01-02'}}]})
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with MotoLMSAClient(auth_token='jwt.tok.sig', http_client=client) as lmsa:
        assert lmsa.get_login_url().startswith('https://passport-glb.lenovo.com/')
        models = lmsa.get_model_names()
        assert models == [{'modelName': 'XT2435-1', 'marketName': 'moto g'}]
        variants = lmsa.discover_model_resources('XT2435-1')
        assert len(variants) == 2
        assert {v.rom_uri for v in variants} == {
            'https://s3.amazonaws.com/moto/us.zip',
            'https://s3.amazonaws.com/moto/eu.zip',
        }


def test_lmsa_requires_token_and_cds_ota():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == 'moto-cds.appspot.com':
            return httpx.Response(200, json={'proceed': False, 'trackingId': 't1'})
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with MotoLMSAClient(auth_token='', http_client=client) as lmsa:
        with pytest.raises(LmsaAuthError):
            lmsa.get_model_names()

    res = check_cds_ota('XT2435-1', 'fogo', 'U1UF34.41', '14', 'ZY22ABC123', http_client=client)
    assert res == {'proceed': False, 'trackingId': 't1'}


def test_moto_incremental_sync_with_checkpoint_and_budget(tmp_path):
    priv, spki_b64 = _gen_rsa_pair()

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith('/common/rsa.jhtml'):
            return httpx.Response(200, json={'code': '0000', 'desc': spki_b64})
        if path.endswith('/rescueDevice/getModelNames.jhtml'):
            return httpx.Response(
                200,
                json={
                    'code': '0000',
                    'content': [
                        {'modelName': 'XT2435-1', 'marketName': 'moto g', 'platform': 'qcom', 'brand': 'moto'},
                        {'modelName': 'XT2431-1', 'marketName': 'moto edge', 'platform': 'mtk', 'brand': 'moto'},
                    ],
                },
            )
        if path.endswith('/rescueDevice/getResource.jhtml'):
            dp = json.loads(request.content.decode('utf-8'))['dparams']
            m = dp['modelName']
            return httpx.Response(
                200,
                json={
                    'code': '0000',
                    'content': [
                        {
                            'modelName': m,
                            'carrier': 'reteu',
                            'fingerPrint': f'{m}_fp',
                            'romResource': {'name': f'{m}.zip', 'uri': f'https://s3.amazonaws.com/{m}.zip', 'publishDate': '2026-01-01'},
                        }
                    ],
                },
            )
        return httpx.Response(404)

    state_file = tmp_path / 'moto_state.json'
    client = httpx.Client(transport=httpx.MockTransport(handler))
    with MotoLMSAClient(auth_token='tok', http_client=client) as lmsa:
        sync = MotoIncrementalSync(state_file, lmsa_client=lmsa)
        assert sync.register_pending_signals([{'model': 'XT2435-1'}], now=50.0) == 1
        r1 = sync.sync_official_batch(max_models=1, now=100.0)
        assert r1['total_official_models'] == 2
        assert r1['new_models_discovered'] == 2
        assert r1['models_probed'] == 1
        assert r1['new_releases'] == 1
        assert 'XT2435-1' not in sync.state['pending_signals']

        # Resume from checkpoint for remaining 1 model
        sync2 = MotoIncrementalSync(state_file, lmsa_client=lmsa)
        r2 = sync2.sync_official_batch(max_models=1, now=110.0)
        assert r2['new_models_discovered'] == 0
        assert r2['models_probed'] == 1
        assert r2['total_releases'] == 2
