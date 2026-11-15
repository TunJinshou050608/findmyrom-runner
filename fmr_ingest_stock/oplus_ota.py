"""OPlus (OPPO / OnePlus / realme) official firmware & incremental sync client.

Metadata and 302 gate resolution only; never fetches firmware binary bodies.
Provides:
1. Official ColorOS / realme catalogues + Google Play & OSS incremental signals.
2. OPlus encrypted `/update/v3` PC-channel & `/update/v5` OTA check + multi-region probing.
3. Zero-tail seed Full-OTA forcing (`<MODEL>_11.<BRANCH>.01_0001_000000000001`),
   Delta (`type == "2"`) & GKA (`gkaReq != 0`) rejection, and CN `/remove-<32hex>` CDN sanitizer.
4. `/downloadCheck` anti-leech gate `GET` 302 resolution (`userId: oplus-ota|16001011`).
5. Stateful, checkpointed `OPlusIncrementalSync` for automated incremental updates.
"""
from __future__ import annotations

import argparse
import base64
from collections.abc import Callable
import csv
from dataclasses import dataclass
import hashlib
import io
import json
import os
from pathlib import Path
import re
import time
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
import httpx

MAX_METADATA_BYTES = 16 * 1024 * 1024
MIN_FULL_OTA_BYTES = 500 * 1024 * 1024
OPLUS_USER_ID = 'oplus-ota|16001011'
GOOGLE_SUPPORTED_DEVICES_URL = 'https://storage.googleapis.com/play_public/supported_devices.csv'
TRANSIENT_HTTP_STATUSES = frozenset({429, 502, 503, 504})

_RE_REMOVE_HEX_SEGMENT = re.compile(r'/remove-[0-9a-fA-F]{32}(?=/|$)')

REGION_CONFIGS: dict[str, dict[str, Any]] = {
    'CN': {
        'host': 'component-ota-cn.allawntech.com',
        'pc_url': 'https://component-otapc-cn.allawntech.com/update/v3',
        'carrier_id': '10010111',
        'carriers': ('10010111',),
        'language': 'zh-CN',
        'negotiation_version': '1615879139745',
        'negotiationVersion': '1615879139745',
        'pubkey_pem': (
            '-----BEGIN RSA PUBLIC KEY-----\n'
            'MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEApXYGXQpNL7gmMzzvajHa\n'
            'oZIHQQvBc2cOEhJc7/tsaO4sT0unoQnwQKfNQCuv7qC1Nu32eCLuewe9LSYhDXr9\n'
            'KSBWjOcCFXVXteLO9WCaAh5hwnUoP/5/Wz0jJwBA+yqs3AaGLA9wJ0+B2lB1vLE4\n'
            'FZNE7exUfwUc03fJxHG9nCLKjIZlrnAAHjRCd8mpnADwfkCEIPIGhnwq7pdkbamZ\n'
            'coZfZud1+fPsELviB9u447C6bKnTU4AaMcR9Y2/uI6TJUTcgyCp+ilgU0JxemrSI\n'
            'PFk3jbCbzamQ6Shkw/jDRzYoXpBRg/2QDkbq+j3ljInu0RHDfOeXf3VBfHSnQ66H\n'
            'CwIDAQAB\n'
            '-----END RSA PUBLIC KEY-----'
        ),
    },
    'EU': {
        'host': 'component-ota-eu.allawnos.com',
        'pc_url': 'https://component-otapc-eu.allawnos.com/update/v3',
        'carrier_id': '01000100',
        'carriers': ('01000100',),
        'language': 'en-GB',
        'negotiation_version': '1615897067573',
        'negotiationVersion': '1615897067573',
        'pubkey_pem': (
            '-----BEGIN RSA PUBLIC KEY-----\n'
            'MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAh8/EThsK3f0WyyPgrtXb\n'
            '/D0Xni6UZNppaQHUqHWo976cybl92VxmehE0ISObnxERaOtrlYmTPIxkVC9MMueD\n'
            'vTwZ1l0KxevZVKU0sJRxNR9AFcw6D7k9fPzzpNJmhSlhpNbt3BEepdgibdRZbacF\n'
            '3NWy3ejOYWHgxC+I/Vj1v7QU5gD+1OhgWeRDcwuV4nGY1ln2lvkRj8EiJYXfkSq/\n'
            'wUI5AvPdNXdEqwou4FBcf6mD84G8pKDyNTQwwuk9lvFlcq4mRqgYaFg9DAgpDgqV\n'
            'K4NTJWM7tQS1GZuRA6PhupfDqnQExyBFhzCefHkEhcFywNyxlPe953NWLFWwbGvF\n'
            'KwIDAQAB\n'
            '-----END RSA PUBLIC KEY-----'
        ),
    },
    'IN': {
        'host': 'component-ota-in.allawnos.com',
        'pc_url': 'https://component-otapc-in.allawnos.com/update/v3',
        'carrier_id': '00011011',
        'carriers': ('00011011',),
        'language': 'en-IN',
        'negotiation_version': '1615896309308',
        'negotiationVersion': '1615896309308',
        'pubkey_pem': (
            '-----BEGIN RSA PUBLIC KEY-----\n'
            'MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAwYtghkzeStC9YvAwOQmW\n'
            'ylbp74Tj8hhi3f9IlK7A/CWrGbLgzz/BeKxNb45zBN8pgaaEOwAJ1qZQV5G4nPro\n'
            'WCPOP1ro1PkemFJvw/vzOOT5uN0ADnHDzZkZXCU/knxqUSfLcwQlHXsYhNsAm7uO\n'
            'KjY9YXF4zWzYN0eFPkML3Pj/zg7hl/ov9clB2VeyI1/blMHFfcNA/fvqDTENXcNB\n'
            'IhgJvXiCpLcZqp+aLZPC5AwY/sCb3j5jTWer0Rk0ZjQBZE1AncwYvUx4mA65U59c\n'
            'WpTyl4c47J29MsQ66hqWv6eBHlDNZSEsQpHePUqgsf7lmO5Wd7teB8ugQki2oz1Y\n'
            '5QIDAQAB\n'
            '-----END RSA PUBLIC KEY-----'
        ),
    },
    'SG': {
        'host': 'component-ota-sg.allawnos.com',
        'pc_url': 'https://component-otapc-sg.allawnos.com/update/v3',
        'carrier_id': '01011010',
        'carriers': ('00101100', '00011011'),
        'language': 'en-SG',
        'negotiation_version': '1615895993238',
        'negotiationVersion': '1615895993238',
        'pubkey_pem': (
            '-----BEGIN RSA PUBLIC KEY-----\n'
            'MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAkA980wxi+eTGcFDiw2I6\n'
            'RrUeO4jL/Aj3Yw4dNuW7tYt+O1sRTHgrzxPD9SrOqzz7G0KgoSfdFHe3JVLPN+U1\n'
            'waK+T0HfLusVJshDaMrMiQFDUiKajb+QKr+bXQhVofH74fjat+oRJ8vjXARSpFk4\n'
            '/41x5j1Bt/2bHoqtdGPcUizZ4whMwzap+hzVlZgs7BNfepo24PWPRujsN3uopl+8\n'
            'u4HFpQDlQl7GdqDYDj2zNOHdFQI2UpSf0aIeKCKOpSKF72KDEESpJVQsqO4nxMwE\n'
            'i2jMujQeCHyTCjBZ+W35RzwT9+0pyZv8FB3c7FYY9FdF/+lvfax5mvFEBd9jO+dp\n'
            'MQIDAQAB\n'
            '-----END RSA PUBLIC KEY-----'
        ),
    },
}

SG_SUBREGION_DEFAULTS: dict[str, tuple[str, str]] = {
    'GL': ('01011010', 'en-SG'),
    'GLO': ('01011010', 'en-SG'),
    'SG': ('01011010', 'en-SG'),
    'RU': ('00110111', 'ru-RU'),
    'TR': ('01010001', 'tr-TR'),
    'TH': ('00111001', 'th-TH'),
}

# Verified regional nvCarrier and model-header suffix combinations used by OPPO/OnePlus/realme.
REGION_PROBE_PROFILES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    'CN': (('10010111',), ('',)),
    'EU': (('01000100',), ('', 'EEA')),
    'IN': (('00011011', '00011010'), ('', 'IN')),
    'SG': (('00101100', '00011011', '01011010', '00111001', '00111000', '00111100', '00111110', '10100110', '00110111'), ('',)),
}

DEFAULT_PROBE_BRANCHES = ('A', 'B', 'C', 'F', 'H', 'J', 'P')
PC_PROBE_BRANCHES = ('A', 'C', 'F', 'H')


class OPlusGateError(ValueError):
    """Raised when an OPlus /downloadCheck or /download gate rejects or fails."""


def _read_bounded_bytes(response: httpx.Response, max_bytes: int = MAX_METADATA_BYTES) -> bytes:
    response.raise_for_status()
    data = bytearray()
    for chunk in response.iter_bytes(65536):
        data.extend(chunk)
        if len(data) > max_bytes:
            raise ValueError('OPlus metadata response exceeds size cap')
    return bytes(data)


def _read_bounded_json(response: httpx.Response) -> Any:
    return json.loads(_read_bounded_bytes(response))


def _region_config(region: str, *, use_pc_channel: bool = False) -> tuple[dict[str, Any], str, str]:
    code = (region or 'CN').strip().upper()
    if code in SG_SUBREGION_DEFAULTS and code != 'SG':
        cfg = REGION_CONFIGS['SG']
        carrier_id, language = SG_SUBREGION_DEFAULTS[code]
        return cfg, carrier_id, language
    if code in ('GL', 'GLO'):
        cfg = REGION_CONFIGS['SG']
        carriers = cfg.get('carriers') or ()
        default_carrier = str(carriers[0]) if (use_pc_channel and carriers) else str(cfg['carrier_id'])
        return cfg, default_carrier, str(cfg['language'])
    cfg = REGION_CONFIGS.get(code)
    if not cfg:
        raise ValueError(f'Unsupported OPlus region: {region}')
    carriers = cfg.get('carriers') or ()
    default_carrier = str(carriers[0]) if (use_pc_channel and carriers) else str(cfg['carrier_id'])
    return cfg, default_carrier, str(cfg['language'])


def zero_tail_seed_version(model: str, branch: str = 'A') -> str:
    """Build zero-tail seed OTA version (`<MODEL>_11.<BRANCH>.01_0001_000000000001`) to force Full OTA."""
    clean_model = (model or '').strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]{2,32}', clean_model):
        raise ValueError(f'Invalid OPlus model: {model!r}')
    clean_branch = (branch or 'A').strip().upper()
    if not re.fullmatch(r'[A-Z0-9]{1,4}', clean_branch):
        raise ValueError(f'Invalid OPlus branch: {branch!r}')
    return f'{clean_model}_11.{clean_branch}.01_0001_000000000001'


def clean_oplus_cdn_url(url: str) -> str:
    """Strip `/remove-<32hex>` path segment from `*.allawnfs.com` CDN URLs."""
    cleaned = (url or '').strip()
    if not cleaned:
        return cleaned
    parsed = urlsplit(cleaned)
    host_lower = (parsed.hostname or '').lower()
    if host_lower == 'allawnfs.com' or host_lower.endswith('.allawnfs.com'):
        new_path = _RE_REMOVE_HEX_SEGMENT.sub('', parsed.path or '') or '/'
        if new_path != parsed.path:
            return urlunsplit((parsed.scheme, parsed.netloc, new_path, parsed.query, parsed.fragment))
    return cleaned


def normalize_ota_version(model: str, ota_version: str) -> str:
    """Ensure an OTA version string has the 4-part `_11.<branch>.<ver>_<build>_<ts>` shape."""
    value = (ota_version or '').strip()
    if not value:
        return f'{model}_11.A.01_0001_197001010000'
    if len(value.split('_')) < 3 or len(value.split('.')) < 3:
        return f'{value}.01_0001_197001010000'
    return value


def device_id_from_imei(imei: str | None = None) -> str:
    """Compute OPlus `deviceId` (64 zeros when no IMEI is supplied, else SHA-256 hex)."""
    cleaned = (imei or '').strip()
    if not cleaned:
        return '0' * 64
    return hashlib.sha256(cleaned.encode('utf-8')).hexdigest().upper()


@dataclass(frozen=True)
class OPlusPacket:
    model: str
    region: str
    nv_carrier: str
    queried_ota_version: str
    response_code: int
    ota_target_version: str | None
    version_name: str | None
    android_version: str | None
    coloros_version: str | None
    security_patch: str | None
    manual_url: str | None
    auto_url: str | None
    size: int | None
    md5: str | None
    streaming_property: str | None
    raw_body: dict[str, Any]
    packet_type: str = ''
    gka_req: int = 0

    @property
    def size_bytes(self) -> int | None:
        return self.size

    @property
    def best_url(self) -> str | None:
        raw = self.manual_url or self.auto_url
        return clean_oplus_cdn_url(raw) if raw else None

    @property
    def is_full_ota(self) -> bool:
        if self.packet_type not in ('1', ''):
            return False
        if self.gka_req != 0:
            return False
        if self.size_bytes is not None and self.size_bytes < MIN_FULL_OTA_BYTES:
            return False
        if self.response_code != 200 and not self.best_url and self.size_bytes is None:
            return False
        return True


class OPlusOTAClient:
    """Client for OPlus `/update/v3` PC & `/update/v5` encrypted queries and `/downloadCheck` resolution."""

    def __init__(
        self,
        http_client: httpx.Client | None = None,
        *,
        max_retries: int = 2,
        sleep_fn: Callable[[float], None] = time.sleep,
    ):
        self.client = http_client or httpx.Client(timeout=20, follow_redirects=False)
        self.owned = http_client is None
        self.max_retries = max(0, int(max_retries))
        self._sleep_fn = sleep_fn

    def close(self) -> None:
        if self.owned:
            self.client.close()

    def __enter__(self) -> OPlusOTAClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    @staticmethod
    def build_v5_request(
        model: str,
        ota_version: str,
        region: str = 'CN',
        nv_carrier: str | None = None,
        imei: str | None = None,
        mode: int = 0,
        key: bytes | None = None,
        iv: bytes | None = None,
        now_ms: int | None = None,
        *,
        use_pc_channel: bool = False,
    ) -> tuple[str, dict[str, str], dict[str, str], bytes]:
        """Build encrypted `/update/v3` (PC) or `/update/v5` URL, headers, JSON body, and session AES key."""
        clean_model = model.strip()
        if not re.fullmatch(r'[A-Za-z0-9_-]{2,32}', clean_model):
            raise ValueError(f'Invalid OPlus model: {model!r}')
        cfg, default_carrier, language = _region_config(region, use_pc_channel=use_pc_channel)
        carrier = (nv_carrier or '').strip() or default_carrier
        full_ota_ver = normalize_ota_version(clean_model, ota_version)
        dev_id = device_id_from_imei(imei)

        aes_key = key if key is not None else os.urandom(32)
        aes_iv = iv if iv is not None else os.urandom(16)
        pub = serialization.load_pem_public_key(str(cfg['pubkey_pem']).encode('ascii'))
        encrypted_key = pub.encrypt(
            base64.b64encode(aes_key),
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA1()),
                algorithm=hashes.SHA1(),
                label=None,
            ),
        )
        protected_key_b64 = base64.b64encode(encrypted_key).decode('ascii')

        ts_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        plain_payload = json.dumps(
            {
                'mode': int(mode),
                'time': ts_ms,
                'isRooted': '0',
                'isLocked': True,
                'type': '1',
                'deviceId': dev_id,
            },
            separators=(',', ':'),
        ).encode('utf-8')
        encryptor = Cipher(algorithms.AES(aes_key), modes.CTR(aes_iv)).encryptor()
        cipher_b64 = base64.b64encode(encryptor.update(plain_payload) + encryptor.finalize()).decode('ascii')
        iv_b64 = base64.b64encode(aes_iv).decode('ascii')
        params_json = json.dumps({'cipher': cipher_b64, 'iv': iv_b64}, separators=(',', ':'))

        neg_ver = str(cfg.get('negotiationVersion') or cfg['negotiation_version'])
        protected_header = json.dumps(
            {
                'SCENE_1': {
                    'protectedKey': protected_key_b64,
                    'version': str(time.time_ns() + 86_400_000_000_000),
                    'negotiationVersion': neg_ver,
                }
            },
            separators=(',', ':'),
        )
        headers: dict[str, str] = {
            'language': language,
            'androidVersion': 'unknown',
            'colorOSVersion': 'unknown',
            'otaVersion': full_ota_ver,
            'model': clean_model,
            'mode': 'manual',
            'nvCarrier': carrier,
            'version': '2',
            'deviceId': dev_id,
            'Content-Type': 'application/json; charset=utf-8',
            'protectedKey': protected_header,
        }
        if use_pc_channel:
            headers['channel'] = 'pc'
            url = str(cfg['pc_url'])
        else:
            url = f"https://{cfg['host']}/update/v5"
        return url, headers, {'params': params_json}, aes_key

    @classmethod
    def build_v3_pc_request(
        cls,
        model: str,
        ota_version: str,
        region: str = 'CN',
        nv_carrier: str | None = None,
        imei: str | None = None,
        mode: int = 0,
        key: bytes | None = None,
        iv: bytes | None = None,
        now_ms: int | None = None,
    ) -> tuple[str, dict[str, str], dict[str, str], bytes]:
        """Build encrypted `/update/v3` PC-channel request."""
        return cls.build_v5_request(
            model=model,
            ota_version=ota_version,
            region=region,
            nv_carrier=nv_carrier,
            imei=imei,
            mode=mode,
            key=key,
            iv=iv,
            now_ms=now_ms,
            use_pc_channel=True,
        )

    @staticmethod
    def decrypt_v5_body(envelope: dict[str, Any], aes_key: bytes) -> dict[str, Any]:
        """Decrypt an OPlus `/update/v3` or `/update/v5` JSON `body` field using the session AES-CTR key."""
        raw_body = envelope.get('body')
        if not raw_body:
            return {}
        inner = json.loads(raw_body) if isinstance(raw_body, str) else raw_body
        iv = base64.b64decode(inner['iv'])
        cipher_bytes = base64.b64decode(inner['cipher'])
        decryptor = Cipher(algorithms.AES(aes_key), modes.CTR(iv)).decryptor()
        plain = decryptor.update(cipher_bytes) + decryptor.finalize()
        return json.loads(plain.decode('utf-8'))

    @staticmethod
    def parse_packet(
        model: str,
        region: str,
        nv_carrier: str,
        queried_ota_version: str,
        response_code: int,
        body: dict[str, Any],
    ) -> OPlusPacket:
        components = body.get('components') or []
        first = components[0] if components and isinstance(components[0], dict) else {}
        packets = first.get('componentPackets') or {}
        vab_data = (packets.get('vabInfo') or {}).get('data') or {}
        header_list = vab_data.get('header') or []
        header_kv: dict[str, str] = {}
        for item in header_list:
            if isinstance(item, str) and '=' in item:
                k, v = item.split('=', 1)
                header_kv[k] = v

        raw_size = packets.get('size')
        size = int(raw_size) if str(raw_size or '').isdigit() else None
        md5 = (packets.get('md5') or vab_data.get('vab_package_hash') or '').strip() or None
        packet_type = str(packets.get('type') or '').strip()

        raw_gka = body.get('gkaReq') if body.get('gkaReq') is not None else packets.get('gkaReq')
        if isinstance(raw_gka, bool):
            gka_req = int(raw_gka)
        elif raw_gka is not None and str(raw_gka).strip().lstrip('-').isdigit():
            gka_req = int(str(raw_gka).strip())
        else:
            gka_req = 0

        return OPlusPacket(
            model=model,
            region=region.upper(),
            nv_carrier=nv_carrier,
            queried_ota_version=queried_ota_version,
            response_code=response_code,
            ota_target_version=body.get('otaVersion') or header_kv.get('ota_target_version'),
            version_name=body.get('versionName'),
            android_version=body.get('androidVersion') or header_kv.get('android_version'),
            coloros_version=body.get('colorOSVersion') or header_kv.get('oplus_rom_version'),
            security_patch=body.get('securityPatch') or header_kv.get('security_patch'),
            manual_url=(packets.get('manualUrl') or '').strip() or None,
            auto_url=(packets.get('url') or '').strip() or None,
            size=size,
            md5=md5,
            streaming_property=vab_data.get('otaStreamingProperty'),
            raw_body=body,
            packet_type=packet_type,
            gka_req=gka_req,
        )

    def query_update(
        self,
        model: str,
        ota_version: str,
        region: str = 'CN',
        nv_carrier: str | None = None,
        imei: str | None = None,
        mode: int = 0,
        use_pc_channel: bool = True,
        max_retries: int | None = None,
    ) -> OPlusPacket:
        url, headers, payload, aes_key = self.build_v5_request(
            model=model,
            ota_version=ota_version,
            region=region,
            nv_carrier=nv_carrier,
            imei=imei,
            mode=mode,
            use_pc_channel=use_pc_channel,
        )
        retries = self.max_retries if max_retries is None else max(0, int(max_retries))
        envelope: dict[str, Any] = {}

        for attempt in range(retries + 1):
            try:
                with self.client.stream('POST', url, headers=headers, json=payload) as resp:
                    if resp.status_code in TRANSIENT_HTTP_STATUSES:
                        if attempt < retries:
                            delay = min(4.0, 0.5 * (2**attempt))
                            self._sleep_fn(delay)
                            continue
                        resp.raise_for_status()
                    envelope = _read_bounded_json(resp)
                    break
            except httpx.TransportError:
                if attempt >= retries:
                    raise
                delay = min(4.0, 0.5 * (2**attempt))
                self._sleep_fn(delay)

        code = int(envelope.get('responseCode') or 0)
        body = self.decrypt_v5_body(envelope, aes_key)
        return self.parse_packet(model, region, headers['nvCarrier'], headers['otaVersion'], code, body)

    def probe_branches(
        self,
        model: str,
        region: str = 'CN',
        branches: tuple[str, ...] = DEFAULT_PROBE_BRANCHES,
        nv_carrier: str | None = None,
        try_eea_suffix: bool = True,
        try_region_carriers: bool = False,
        use_pc_channel: bool = False,
        use_zero_tail_seed: bool = False,
    ) -> list[OPlusPacket]:
        """Probe official OPlus OTA across major branches and regional carrier profiles."""
        clean_model = model.strip()
        reg_key = region.upper()
        default_carriers, suffixes = REGION_PROBE_PROFILES.get(
            reg_key, ((_region_config(reg_key, use_pc_channel=use_pc_channel)[1],), ('',))
        )
        if nv_carrier:
            carriers: tuple[str, ...] = (nv_carrier,)
        elif try_region_carriers:
            carriers = default_carriers
        elif use_pc_channel and reg_key in REGION_CONFIGS:
            carriers = tuple(REGION_CONFIGS[reg_key]['carriers'])
        else:
            carriers = (_region_config(reg_key, use_pc_channel=False)[1],)

        header_models: list[str] = []
        for sfx in suffixes:
            if sfx == 'EEA' and not try_eea_suffix:
                continue
            cand = clean_model if not sfx or clean_model.upper().endswith(sfx) else f'{clean_model}{sfx}'
            if cand not in header_models:
                header_models.append(cand)
        if try_eea_suffix and reg_key == 'EU' and not clean_model.upper().endswith('EEA'):
            eea = f'{clean_model}EEA'
            if eea not in header_models:
                header_models.append(eea)

        found: list[OPlusPacket] = []
        seen_targets: set[tuple[str, str]] = set()
        for hdr_model in header_models:
            for carrier in carriers:
                for branch in branches:
                    candidate = (
                        zero_tail_seed_version(clean_model, branch)
                        if use_zero_tail_seed
                        else f'{clean_model}_11.{branch}.01_0001_197001010000'
                    )
                    pkt = self.query_update(
                        model=hdr_model,
                        ota_version=candidate,
                        region=region,
                        nv_carrier=carrier,
                        use_pc_channel=use_pc_channel,
                    )
                    if pkt.best_url:
                        dedup_key = (pkt.ota_target_version or pkt.best_url, carrier)
                        if dedup_key not in seen_targets:
                            seen_targets.add(dedup_key)
                            found.append(pkt)
        return found

    def probe_all_regions(
        self,
        model: str,
        regions: tuple[str, ...] = ('CN', 'EU', 'IN', 'SG'),
        branches: tuple[str, ...] = DEFAULT_PROBE_BRANCHES,
        use_pc_channel: bool = True,
        use_zero_tail_seed: bool = True,
    ) -> list[OPlusPacket]:
        """Probe all major OPlus regions with their verified carrier & suffix matrices."""
        results: list[OPlusPacket] = []
        seen: set[tuple[str, str]] = set()
        for reg in regions:
            for pkt in self.probe_branches(
                model=model,
                region=reg,
                branches=branches,
                try_eea_suffix=True,
                try_region_carriers=not use_pc_channel,
                use_pc_channel=use_pc_channel,
                use_zero_tail_seed=use_zero_tail_seed,
            ):
                key = (pkt.region, pkt.ota_target_version or pkt.best_url or '')
                if key not in seen:
                    seen.add(key)
                    results.append(pkt)
        return results

    def resolve_download_check(self, gate_url: str) -> str:
        """Resolve an OPlus `/downloadCheck` URL via `GET` into its short-lived signed CDN URL."""
        parsed = urlsplit(gate_url)
        if parsed.scheme != 'https' or not parsed.hostname:
            raise OPlusGateError('Invalid gate URL')
        if parsed.path.endswith('/download'):
            raise OPlusGateError('Gate requires device attestation (/download)')
        if not parsed.path.endswith('/downloadCheck'):
            return clean_oplus_cdn_url(gate_url)
        with self.client.stream(
            'GET',
            gate_url,
            headers={'userId': OPLUS_USER_ID, 'Accept': '*/*'},
            follow_redirects=False,
        ) as resp:
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get('Location') or ''
                if not location:
                    raise OPlusGateError('Gate returned redirect without Location')
                return clean_oplus_cdn_url(urljoin(gate_url, location))
            if resp.status_code == 200:
                raise OPlusGateError('Gate rejected caller (missing/invalid userId or expired link)')
            resp.raise_for_status()
        raise OPlusGateError('Unexpected gate response')


def fetch_coloros_official_catalogue(
    http_client: httpx.Client | None = None,
    include_roms: bool = False,
) -> dict[str, Any]:
    """Fetch official OPPO/Ace model list (and optional ColorOS <=7 ROMs) from coloros.com."""
    owned = http_client is None
    client = http_client or httpx.Client(timeout=20, follow_redirects=False)
    base = 'https://www.coloros.com/api'
    try:
        with client.stream('GET', f'{base}/colorOS/business/rom/brandList') as r:
            brands_json = _read_bounded_json(r)
        brands = brands_json.get('data') or []
        products: list[dict[str, Any]] = []
        for brand in brands:
            bid = brand.get('id')
            if bid is None:
                continue
            with client.stream('GET', f'{base}/colorOS/business/rom/productList', params={'brandId': str(bid)}) as r:
                prod_json = _read_bounded_json(r)
            for p in prod_json.get('data') or []:
                entry = {
                    'brand_id': bid,
                    'brand_name': brand.get('brandName'),
                    'product_id': str(p.get('id') or ''),
                    'product_name': p.get('productName'),
                    'product_code': p.get('productCode'),
                }
                if include_roms and entry['product_id']:
                    with client.stream(
                        'GET',
                        f'{base}/colorOS/business/rom/romList',
                        params={'productId': entry['product_id']},
                    ) as rr:
                        rom_json = _read_bounded_json(rr)
                    entry['roms'] = [
                        {
                            'version': rm.get('version'),
                            'android_version': rm.get('androidVersion'),
                            'coloros_version': rm.get('colorosVersion'),
                            'file_url': rm.get('fileUrl'),
                            'file_md5': rm.get('fileMd5'),
                            'file_size': rm.get('fileSize'),
                        }
                        for rm in (rom_json.get('data') or [])
                    ]
                products.append(entry)
        return {'source': 'coloros.com', 'brands': len(brands), 'products': products}
    finally:
        if owned:
            client.close()


def fetch_realme_official_catalogue(
    region: str = 'in',
    http_client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    """Fetch official realme support software-update phone & ROM list for a region."""
    reg = region.strip().lower()
    if not re.fullmatch(r'[a-z]{2}(?:-[a-z]{2})?', reg):
        raise ValueError(f'Invalid realme site region: {region!r}')
    host = 'api-eu.realme.com' if reg == 'eu' else 'api.realme.com'
    url = f'https://{host}/{reg}/after/sale/config/detail'
    owned = http_client is None
    client = http_client or httpx.Client(timeout=20, follow_redirects=False)
    try:
        with client.stream('GET', url, params={'parentType': '1'}) as r:
            payload = _read_bounded_json(r)
        content = (payload.get('data') or {}).get('content')
        items = json.loads(content) if isinstance(content, str) else (content or [])
        out: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            history = [
                {
                    'version': h.get('OldSoftwareVersion'),
                    'download_url': h.get('OldDownloadLink'),
                }
                for h in (item.get('oldList') or [])
                if isinstance(h, dict) and h.get('OldDownloadLink')
            ]
            out.append(
                {
                    'region': reg,
                    'title': item.get('title') or item.get('productName'),
                    'version': item.get('version'),
                    'system': item.get('system'),
                    'download_url': item.get('downloadUrl'),
                    'md5': item.get('md5'),
                    'size': item.get('size'),
                    'history': history,
                }
            )
        return out
    finally:
        if owned:
            client.close()


def fetch_google_play_devices(
    brands: tuple[str, ...] = ('oppo', 'oneplus', 'realme', 'motorola'),
    http_client: httpx.Client | None = None,
) -> dict[str, list[dict[str, str]]]:
    """Fetch Google Play's official supported_devices.csv grouped by brand."""
    wanted = {b.strip().lower() for b in brands}
    owned = http_client is None
    client = http_client or httpx.Client(timeout=30, follow_redirects=False)
    grouped: dict[str, list[dict[str, str]]] = {b: [] for b in wanted}
    try:
        with client.stream('GET', GOOGLE_SUPPORTED_DEVICES_URL) as resp:
            raw_bytes = _read_bounded_bytes(resp)
        text = raw_bytes.decode('utf-16') if raw_bytes[:2] in (b'\xff\xfe', b'\xfe\xff') else raw_bytes.decode('utf-8-sig')
        for row in csv.DictReader(io.StringIO(text)):
            brand = (row.get('Retail Branding') or '').strip().lower()
            if brand not in wanted:
                continue
            grouped[brand].append(
                {
                    'model': (row.get('Model') or '').strip(),
                    'device': (row.get('Device') or '').strip(),
                    'marketing_name': (row.get('Marketing Name') or '').strip(),
                }
            )
        return grouped
    finally:
        if owned:
            client.close()


def fetch_oss_branch_signals(
    org: str,
    repo: str,
    http_client: httpx.Client | None = None,
) -> list[str]:
    """Read branch names from an official OEM GitHub repo via git smart HTTP (no API quota)."""
    if not re.fullmatch(r'[A-Za-z0-9_-]+', org) or not re.fullmatch(r'[A-Za-z0-9_.-]+', repo):
        raise ValueError('Invalid GitHub org/repo')
    url = f'https://github.com/{org}/{repo}.git/info/refs?service=git-upload-pack'
    owned = http_client is None
    client = http_client or httpx.Client(timeout=20, follow_redirects=False)
    try:
        with client.stream('GET', url) as resp:
            raw = _read_bounded_bytes(resp, max_bytes=2 * 1024 * 1024)
        text = raw.decode('utf-8', 'replace')
        branches = sorted(set(re.findall(r'refs/heads/([A-Za-z0-9_./-]+)', text)))
        return branches
    finally:
        if owned:
            client.close()


class OPlusIncrementalSync:
    """Checkpoint-backed incremental model discovery + official OPlus OTA version sync."""

    def __init__(self, state_path: Path | str, ota_client: OPlusOTAClient | None = None):
        self.state_path = Path(state_path)
        self.ota = ota_client or OPlusOTAClient()
        self.owned_ota = ota_client is None
        self.state: dict[str, Any] = self._load()

    def close(self) -> None:
        if self.owned_ota:
            self.ota.close()

    def __enter__(self) -> OPlusIncrementalSync:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def _load(self) -> dict[str, Any]:
        if self.state_path.exists():
            return json.loads(self.state_path.read_text(encoding='utf-8'))
        return {'models': {}, 'tracks': {}, 'releases': {}}

    def save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding='utf-8')

    def register_models(self, entries: list[dict[str, str]], now: float | None = None) -> list[str]:
        """Add newly seen candidate models incrementally; return list of newly added model codes."""
        ts = now if now is not None else time.time()
        added: list[str] = []
        for entry in entries:
            model = (entry.get('model') or '').strip()
            if not re.fullmatch(r'[A-Za-z0-9_-]{4,20}', model):
                continue
            if model not in self.state['models']:
                self.state['models'][model] = {
                    'brand': entry.get('brand') or '',
                    'marketing_name': entry.get('marketing_name') or '',
                    'source': entry.get('source') or '',
                    'discovered_at': ts,
                    'probed_at': 0.0,
                    'regions_hint': [entry['region']] if entry.get('region') else [],
                }
                added.append(model)
        return added

    def _record_packet(self, base_model: str, pkt: OPlusPacket, ts: float) -> bool:
        if not pkt.best_url or not pkt.ota_target_version or not pkt.is_full_ota:
            return False
        branch_m = re.search(r'_11\.([A-Z])\.', pkt.ota_target_version)
        branch = branch_m.group(1) if branch_m else 'A'
        track_key = f'{base_model}|{pkt.region}|{pkt.nv_carrier}|{pkt.model}|{branch}'
        prev = self.state['tracks'].get(track_key, {})
        changed = prev.get('latest_ota_version') != pkt.ota_target_version
        self.state['tracks'][track_key] = {
            'base_model': base_model,
            'header_model': pkt.model,
            'region': pkt.region,
            'nv_carrier': pkt.nv_carrier,
            'branch': branch,
            'latest_ota_version': pkt.ota_target_version,
            'last_checked': ts,
        }
        rel_key = f'{base_model}|{pkt.region}|{pkt.nv_carrier}|{pkt.ota_target_version}'
        self.state['releases'][rel_key] = {
            'model': base_model,
            'header_model': pkt.model,
            'region': pkt.region,
            'nv_carrier': pkt.nv_carrier,
            'ota_version': pkt.ota_target_version,
            'version_name': pkt.version_name,
            'android_version': pkt.android_version,
            'security_patch': pkt.security_patch,
            'url': pkt.best_url,
            'md5': pkt.md5,
            'size': pkt.size,
            'updated_at': ts,
        }
        return changed

    def sync_batch(
        self,
        max_new_models: int = 5,
        max_track_refreshes: int = 20,
        refresh_ttl_seconds: float = 6 * 3600,
        now: float | None = None,
    ) -> dict[str, int]:
        """Run one bounded incremental batch: probe new models, then snowball existing tracks."""
        ts = now if now is not None else time.time()
        new_probed = 0
        new_releases = 0
        tracks_checked = 0

        for model, meta in list(self.state['models'].items()):
            if new_probed >= max_new_models:
                break
            if meta.get('probed_at', 0.0) > 0:
                continue
            hints = tuple(r for r in (meta.get('regions_hint') or []) if r in REGION_CONFIGS)
            regions = hints if hints else ('CN', 'EU', 'IN', 'SG')
            for pkt in self.ota.probe_all_regions(model, regions=regions):
                if self._record_packet(model, pkt, ts):
                    new_releases += 1
            meta['probed_at'] = ts
            new_probed += 1
            self.save()

        for track_key, track in list(self.state['tracks'].items()):
            if tracks_checked >= max_track_refreshes:
                break
            if ts - track.get('last_checked', 0.0) < refresh_ttl_seconds:
                continue
            pkt = self.ota.query_update(
                model=track['header_model'],
                ota_version=track['latest_ota_version'],
                region=track['region'],
                nv_carrier=track['nv_carrier'],
            )
            track['last_checked'] = ts
            if self._record_packet(track['base_model'], pkt, ts):
                new_releases += 1
            tracks_checked += 1
            self.save()

        self.save()
        return {
            'total_models': len(self.state['models']),
            'new_models_probed': new_probed,
            'tracks_refreshed': tracks_checked,
            'new_or_updated_releases': new_releases,
            'total_tracks': len(self.state['tracks']),
            'total_releases': len(self.state['releases']),
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='OPlus (OPPO / OnePlus / realme) official OTA & catalogue client')
    sub = parser.add_subparsers(dest='cmd', required=True)

    p_cat = sub.add_parser('catalogue', help='Fetch official ColorOS / realme model & ROM catalogues')
    p_cat.add_argument('--source', choices=('coloros', 'realme'), required=True)
    p_cat.add_argument('--region', default='in', help='realme site region (in, eu, cn, ...)')
    p_cat.add_argument('--include-roms', action='store_true', help='Fetch ColorOS <=7 romList per product')

    p_q = sub.add_parser('query', help='Query official OPlus /update/v3 PC channel with known otaVersion')
    p_q.add_argument('--model', required=True)
    p_q.add_argument('--ota-version', required=True)
    p_q.add_argument('--region', default='CN')
    p_q.add_argument('--carrier', default=None)

    p_probe = sub.add_parser('probe', help='Probe official OPlus branches & regional carriers for a model')
    p_probe.add_argument('--model', required=True)
    p_probe.add_argument('--region', default='ALL', help='CN, EU, IN, SG, or ALL')
    p_probe.add_argument('--carrier', default=None)

    p_res = sub.add_parser('resolve', help='Resolve a /downloadCheck URL to a signed CDN URL')
    p_res.add_argument('url')

    args = parser.parse_args(argv)
    if args.cmd == 'catalogue':
        if args.source == 'coloros':
            data = fetch_coloros_official_catalogue(include_roms=args.include_roms)
        else:
            data = fetch_realme_official_catalogue(region=args.region)
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    with OPlusOTAClient() as client:
        if args.cmd == 'query':
            pkt = client.query_update(args.model, args.ota_version, region=args.region, nv_carrier=args.carrier)
            print(json.dumps(pkt.__dict__, ensure_ascii=False, indent=2))
        elif args.cmd == 'probe':
            if args.region.upper() == 'ALL':
                pkts = client.probe_all_regions(args.model)
            else:
                pkts = client.probe_branches(
                    args.model,
                    region=args.region,
                    nv_carrier=args.carrier,
                    try_region_carriers=(args.carrier is None),
                    use_pc_channel=True,
                    use_zero_tail_seed=True,
                )
            print(json.dumps([p.__dict__ for p in pkts], ensure_ascii=False, indent=2))
        elif args.cmd == 'resolve':
            print(client.resolve_download_check(args.url))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
