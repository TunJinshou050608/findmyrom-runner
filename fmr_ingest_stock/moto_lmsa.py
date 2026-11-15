"""Motorola / Lenovo LMSA (Software Fix) & CDS OTA official firmware client.

Metadata & link discovery only; never downloads firmware binary bodies.
Implements:
1. LMSA RSA public key retrieval (`/common/rsa.jhtml`) & `X-Device-Fingerprint` signing.
2. Lenovo ID OAuth2 login URL lookup (`/dictionary/getApiInfo.jhtml`).
3. Official device catalogue (`/rescueDevice/getModelNames.jhtml`).
4. Parameter-tree exploration without IMEI (`/rescueDevice/getRomMatchParams.jhtml` + `/getResource.jhtml`).
5. IMEI / Serial resource lookup (`/getNewResourceByImei.jhtml`, `/getNewResourceBySN.jhtml`).
6. Motorola CDS device OTA check (`moto-cds.appspot.com/cds/upgrade/1/check/ctx/ota/key/...`).
"""
from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import time
from typing import Any
import uuid

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding
import httpx

LMSA_BASE_URL = 'https://lsa.lenovo.com'
LMSA_API_URL = f'{LMSA_BASE_URL}/Interface'
MOTO_CDS_BASE_URL = 'https://moto-cds.appspot.com/cds/upgrade/1/check/ctx/ota/key'
DEFAULT_CLIENT_VERSION = '7.5.5.19'
DEFAULT_USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
DEFAULT_WINDOWS_INFO = 'Microsoft Windows 10 Pro, 64-bit'
MAX_METADATA_BYTES = 8 * 1024 * 1024


class LmsaAuthError(PermissionError):
    """Raised when LMSA rejects missing/expired Lenovo OAuth2 Authorization token."""


def _read_bounded_json(response: httpx.Response) -> Any:
    response.raise_for_status()
    data = bytearray()
    for chunk in response.iter_bytes(65536):
        data.extend(chunk)
        if len(data) > MAX_METADATA_BYTES:
            raise ValueError('LMSA metadata response exceeds size cap')
    return json.loads(bytes(data))


def sign_device_fingerprint(
    url: str,
    auth_token: str,
    rsa_public_key_b64: str,
    timestamp_ms: int | None = None,
) -> str:
    """Build `X-Device-Fingerprint` header: RSA PKCS#1 v1.5 over `<ts>|<token>|<last_segment>`."""
    if not auth_token or not rsa_public_key_b64:
        raise ValueError('Both auth_token and rsa_public_key_b64 are required')
    ts = str(timestamp_ms if timestamp_ms is not None else int(time.time() * 1000))
    segment = url.split('?', 1)[0].rstrip('/').rsplit('/', 1)[-1]
    plain = f'{ts}|{auth_token}|{segment}'.encode('utf-8')
    der_bytes = base64.b64decode(rsa_public_key_b64.strip())
    pub = serialization.load_der_public_key(der_bytes)
    encrypted = pub.encrypt(plain, padding.PKCS1v15())
    return base64.b64encode(encrypted).decode('ascii')


@dataclass(frozen=True)
class MotoResourceVariant:
    model_name: str
    market_name: str | None
    carrier: str | None
    platform: str | None
    fingerprint: str | None
    rom_name: str | None
    rom_uri: str | None
    publish_date: str | None
    flash_flow: str | None
    selected_parameters: dict[str, str]


class MotoLMSAClient:
    """Official Lenovo/Motorola Software Fix (LMSA) API client."""

    def __init__(
        self,
        auth_token: str = '',
        client_version: str = DEFAULT_CLIENT_VERSION,
        http_client: httpx.Client | None = None,
    ):
        self.auth_token = auth_token.strip()
        self.client_version = client_version
        self.guid = str(uuid.uuid4())
        self.client_uuid = str(uuid.uuid4())
        self.rsa_public_key = ''
        self.client = http_client or httpx.Client(timeout=25, follow_redirects=False)
        self.owned = http_client is None

    def close(self) -> None:
        if self.owned:
            self.client.close()

    def __enter__(self) -> MotoLMSAClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def _base_headers(self) -> dict[str, str]:
        return {
            'Content-Type': 'application/json',
            'Request-Tag': 'lmsa',
            'User-Agent': DEFAULT_USER_AGENT,
            'Guid': self.guid,
            'clientUUID': self.client_uuid,
            'clientVersion': self.client_version,
            'language': 'en-US',
            'windowsInfo': base64.b64encode(DEFAULT_WINDOWS_INFO.encode('utf-8')).decode('ascii'),
        }

    def _envelope(self, dparams: dict[str, Any]) -> dict[str, Any]:
        return {
            'client': {'version': self.client_version},
            'language': 'en-US',
            'windowsInfo': DEFAULT_WINDOWS_INFO,
            'dparams': dparams,
        }

    def fetch_rsa_public_key(self) -> str:
        """Fetch current LMSA server SPKI RSA public key (no auth required)."""
        url = f'{LMSA_API_URL}/common/rsa.jhtml'
        with self.client.stream('POST', url, headers=self._base_headers(), json=self._envelope({})) as resp:
            data = _read_bounded_json(resp)
        key_b64 = (data.get('desc') or '').strip()
        if data.get('code') != '0000' or len(key_b64) < 32:
            raise ValueError(f'Failed to fetch LMSA RSA key: {data}')
        self.rsa_public_key = key_b64
        return key_b64

    def get_login_url(self) -> str:
        """Fetch official Lenovo ID OAuth2 login URL (`TIP_URL`)."""
        data = self.post('/dictionary/getApiInfo.jhtml', {'key': 'TIP_URL'}, authenticated=False)
        content = data.get('content') or data.get('msg') or ''
        if not isinstance(content, str) or not content.startswith('https://'):
            raise ValueError(f'Unexpected TIP_URL payload: {data}')
        return content

    def post(
        self,
        path: str,
        dparams: dict[str, Any],
        authenticated: bool = True,
    ) -> dict[str, Any]:
        if not path.startswith('/') or '..' in path:
            raise ValueError(f'Invalid LMSA endpoint path: {path!r}')
        url = f'{LMSA_API_URL}{path}'
        headers = self._base_headers()
        if authenticated:
            if not self.auth_token:
                raise LmsaAuthError('LMSA endpoint requires Lenovo OAuth2 Authorization token')
            if not self.rsa_public_key:
                self.fetch_rsa_public_key()
            headers['Authorization'] = self.auth_token
            headers['X-Device-Fingerprint'] = sign_device_fingerprint(
                url, self.auth_token, self.rsa_public_key
            )
        with self.client.stream('POST', url, headers=headers, json=self._envelope(dparams)) as resp:
            new_auth = resp.headers.get('Authorization')
            if new_auth:
                self.auth_token = new_auth
            data = _read_bounded_json(resp)
        if data.get('code') in ('401', '403', '1001'):
            raise LmsaAuthError(f"LMSA auth rejected ({data.get('code')}): {data.get('desc') or data.get('msg')}")
        return data

    def get_model_names(self) -> list[dict[str, Any]]:
        """Fetch official Moto / Lenovo device catalog (`/rescueDevice/getModelNames.jhtml`)."""
        data = self.post('/rescueDevice/getModelNames.jhtml', {})
        if data.get('code') != '0000':
            raise ValueError(f"getModelNames failed ({data.get('code')}): {data.get('desc')}")
        content = data.get('content')
        if isinstance(content, list):
            return content
        if isinstance(content, dict) and isinstance(content.get('models'), list):
            return content['models']
        return []

    def get_rom_match_params(self, model_name: str) -> dict[str, Any]:
        """Query manual/auto matching parameter names for a Moto model."""
        data = self.post('/rescueDevice/getRomMatchParams.jhtml', {'modelName': model_name})
        if data.get('code') != '0000':
            raise ValueError(f"getRomMatchParams failed ({data.get('code')}): {data.get('desc')}")
        return data.get('content') or {}

    def discover_model_resources(
        self,
        model_name: str,
        market_name: str = '',
        explore_all: bool = True,
        max_depth: int = 12,
        max_requests: int = 64,
    ) -> list[MotoResourceVariant]:
        """Explore `/rescueDevice/getResource.jhtml` parameter tree without IMEI."""
        variants: list[MotoResourceVariant] = []
        seen_states: set[str] = set()
        seen_uris: set[str] = set()
        requests_used = 0

        def walk(params: dict[str, str], depth: int) -> None:
            nonlocal requests_used
            if depth > max_depth or requests_used >= max_requests:
                return
            state_key = json.dumps(sorted(params.items()))
            if state_key in seen_states:
                return
            seen_states.add(state_key)
            requests_used += 1

            data = self.post('/rescueDevice/getResource.jhtml', params)
            if data.get('code') != '0000':
                return
            content = data.get('content')
            if not isinstance(content, list) or not content:
                return
            first = content[0]
            if not isinstance(first, dict):
                return
            rom = first.get('romResource')
            if isinstance(rom, dict) and rom.get('uri'):
                uri = str(rom['uri'])
                dedup = f'{uri}|{state_key}'
                if dedup not in seen_uris:
                    seen_uris.add(dedup)
                    variants.append(
                        MotoResourceVariant(
                            model_name=first.get('modelName') or model_name,
                            market_name=first.get('marketName') or market_name or None,
                            carrier=first.get('carrier'),
                            platform=first.get('platform'),
                            fingerprint=first.get('fingerPrint'),
                            rom_name=rom.get('name'),
                            rom_uri=uri,
                            publish_date=rom.get('publishDate'),
                            flash_flow=first.get('flashFlow'),
                            selected_parameters=dict(params),
                        )
                    )
                return

            next_key = (first.get('paramProperty') or {}).get('property')
            values = first.get('paramValues') or []
            if not isinstance(next_key, str) or not next_key or not isinstance(values, list) or not values:
                return
            branch_values = values if explore_all else values[:1]
            for val in branch_values:
                if not isinstance(val, str) or not val:
                    continue
                walk({**params, next_key: val}, depth + 1)

        initial: dict[str, str] = {'modelName': model_name}
        if market_name:
            initial['marketName'] = market_name
        walk(initial, 0)
        return variants

    def get_resource_by_identifier(
        self,
        model_code: str,
        carrier: str,
        identifier: str,
        kind: str = 'imei',
    ) -> MotoResourceVariant | None:
        """Query `/rescueDevice/getNewResourceByImei.jhtml` or `getNewResourceBySN.jhtml`."""
        encrypt_code = os.urandom(4).hex().upper()
        dparams: dict[str, Any] = {
            'modelCode': model_code,
            'roCarrier': carrier,
            'encryptCode': encrypt_code,
            'sku': model_code,
            'carrierSku': model_code,
        }
        if kind == 'serial':
            dparams['serialno'] = identifier
            endpoints = ('/rescueDevice/getNewResourceBySN.jhtml', '/rescueDevice/getNewResource.jhtml')
        else:
            dparams['imei'] = identifier
            endpoints = ('/rescueDevice/getNewResourceByImei.jhtml',)

        for ep in endpoints:
            data = self.post(ep, dparams)
            content = data.get('content')
            if data.get('code') == '0000' and isinstance(content, list) and content:
                first = content[0]
                rom = first.get('romResource') or {}
                if rom.get('uri'):
                    return MotoResourceVariant(
                        model_name=first.get('modelName') or model_code,
                        market_name=first.get('marketName'),
                        carrier=first.get('carrier') or carrier,
                        platform=first.get('platform'),
                        fingerprint=first.get('fingerPrint'),
                        rom_name=rom.get('name'),
                        rom_uri=rom.get('uri'),
                        publish_date=rom.get('publishDate'),
                        flash_flow=first.get('flashFlow'),
                        selected_parameters={'roCarrier': carrier},
                    )
        return None


def check_cds_ota(
    model: str,
    codename: str,
    build_id: str,
    os_version: str,
    serial_or_key: str,
    imei: str = '',
    http_client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Query Motorola device OTA server (`moto-cds.appspot.com`)."""
    clean_key = re.sub(r'[^A-Za-z0-9._-]', '', serial_or_key)
    if not clean_key:
        raise ValueError('Invalid CDS device key')
    url = f'{MOTO_CDS_BASE_URL}/{clean_key}'
    payload = {
        'id': clean_key,
        'contentTimestamp': 0,
        'deviceInfo': {
            'manufacturer': 'motorola',
            'brand': 'motorola',
            'model': model,
            'product': codename,
            'device': codename,
            'hardware': codename,
            'osVersion': os_version,
            'buildId': build_id,
        },
        'extraInfo': {},
        'identityInfo': {'imei': imei, 'serial': clean_key},
        'triggeredBy': 'polling',
        'idType': 'serialNumber',
    }
    owned = http_client is None
    client = http_client or httpx.Client(timeout=20, follow_redirects=False)
    try:
        with client.stream('POST', url, json=payload) as resp:
            return _read_bounded_json(resp)
    finally:
        if owned:
            client.close()


class MotoIncrementalSync:
    """Checkpoint-backed incremental Moto/Lenovo LMSA model & firmware sync."""

    def __init__(self, state_path: Path | str, lmsa_client: MotoLMSAClient | None = None):
        self.state_path = Path(state_path)
        self.lmsa = lmsa_client or MotoLMSAClient(auth_token=os.environ.get('LMSA_AUTH_TOKEN', ''))
        self.owned_lmsa = lmsa_client is None
        self.state: dict[str, Any] = self._load()

    def close(self) -> None:
        if self.owned_lmsa:
            self.lmsa.close()

    def __enter__(self) -> MotoIncrementalSync:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def _load(self) -> dict[str, Any]:
        if self.state_path.exists():
            return json.loads(self.state_path.read_text(encoding='utf-8'))
        return {'models': {}, 'releases': {}, 'pending_signals': {}}

    def save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding='utf-8')

    def register_pending_signals(self, entries: list[dict[str, str]], now: float | None = None) -> int:
        """Queue non-LMSA model signals (e.g. Google Play / OSS) awaiting LMSA token sync."""
        ts = now if now is not None else time.time()
        added = 0
        for e in entries:
            key = (e.get('model') or e.get('device') or '').strip()
            if not key or key in self.state['models'] or key in self.state['pending_signals']:
                continue
            self.state['pending_signals'][key] = {**e, 'discovered_at': ts, 'status': 'pending_lmsa_token'}
            added += 1
        self.save()
        return added

    def sync_official_batch(
        self,
        max_models: int = 10,
        refresh_ttl_seconds: float = 7 * 86400,
        now: float | None = None,
    ) -> dict[str, int]:
        """Sync official LMSA `getModelNames` diff and discover S3 variants incrementally."""
        ts = now if now is not None else time.time()
        catalog = self.lmsa.get_model_names()
        new_models = 0
        for item in catalog:
            m = (item.get('modelName') or '').strip()
            if not m:
                continue
            if m not in self.state['models']:
                self.state['models'][m] = {
                    'market_name': item.get('marketName') or '',
                    'platform': item.get('platform') or '',
                    'brand': item.get('brand') or '',
                    'discovered_at': ts,
                    'last_checked': 0.0,
                }
                self.state['pending_signals'].pop(m, None)
                new_models += 1

        probed = 0
        new_releases = 0
        for model_name, meta in list(self.state['models'].items()):
            if probed >= max_models:
                break
            last_checked = meta.get('last_checked', 0.0)
            if last_checked > 0 and ts - last_checked < refresh_ttl_seconds:
                continue
            variants = self.lmsa.discover_model_resources(
                model_name=model_name,
                market_name=meta.get('market_name', ''),
                explore_all=True,
            )
            for v in variants:
                if not v.rom_uri:
                    continue
                rkey = f'{model_name}|{v.carrier or ""}|{v.fingerprint or v.rom_uri}'
                if rkey not in self.state['releases']:
                    new_releases += 1
                self.state['releases'][rkey] = {
                    'model_name': v.model_name,
                    'market_name': v.market_name,
                    'carrier': v.carrier,
                    'fingerprint': v.fingerprint,
                    'rom_name': v.rom_name,
                    'rom_uri': v.rom_uri,
                    'publish_date': v.publish_date,
                    'updated_at': ts,
                }
            meta['last_checked'] = ts
            probed += 1
            self.save()

        self.save()
        return {
            'total_official_models': len(self.state['models']),
            'new_models_discovered': new_models,
            'models_probed': probed,
            'new_releases': new_releases,
            'total_releases': len(self.state['releases']),
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Motorola / Lenovo LMSA & CDS official firmware client')
    parser.add_argument('--token', default=os.environ.get('LMSA_AUTH_TOKEN', ''), help='Lenovo OAuth2 token')
    sub = parser.add_subparsers(dest='cmd', required=True)

    sub.add_parser('rsa-key', help='Fetch live LMSA RSA public key (no auth required)')
    sub.add_parser('login-url', help='Fetch official Lenovo ID OAuth2 login URL (no auth required)')
    sub.add_parser('models', help='Fetch official Moto/Lenovo device list (requires --token)')

    p_var = sub.add_parser('variants', help='Discover firmware variants via getResource tree (requires --token)')
    p_var.add_argument('--model', required=True)
    p_var.add_argument('--market-name', default='')

    p_imei = sub.add_parser('by-imei', help='Lookup firmware by IMEI or SN (requires --token)')
    p_imei.add_argument('--model', required=True)
    p_imei.add_argument('--carrier', default='reteu')
    p_imei.add_argument('--id', required=True)
    p_imei.add_argument('--kind', choices=('imei', 'serial'), default='imei')

    args = parser.parse_args(argv)
    with MotoLMSAClient(auth_token=args.token) as client:
        if args.cmd == 'rsa-key':
            print(client.fetch_rsa_public_key())
        elif args.cmd == 'login-url':
            print(client.get_login_url())
        elif args.cmd == 'models':
            print(json.dumps(client.get_model_names(), ensure_ascii=False, indent=2))
        elif args.cmd == 'variants':
            rows = client.discover_model_resources(args.model, market_name=args.market_name)
            print(json.dumps([r.__dict__ for r in rows], ensure_ascii=False, indent=2))
        elif args.cmd == 'by-imei':
            res = client.get_resource_by_identifier(args.model, args.carrier, args.id, kind=args.kind)
            print(json.dumps(res.__dict__ if res else None, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
