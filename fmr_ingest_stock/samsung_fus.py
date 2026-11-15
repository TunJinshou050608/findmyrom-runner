"""Bounded Smart FUS metadata queries; no init/download/binary endpoint exists here.

Protocol adaptation from topjohnwu/samloader-rs (Apache-2.0), copyright 2026
John "topjohnwu" Wu. See docs/licenses/samloader-rs.txt for attribution/license.
"""
import datetime
import re
import time
import xml.etree.ElementTree as ET
import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

HOST='https://neofussvr.sslcs.cdngc.net/'
# Shared protocol constant published by the upstream client, not an account key.
AUTH_KEY=bytes.fromhex('422e73733617ae2b198940fd4e32b0a5')
TRANSIENT_FUS_HTTP_STATUSES=frozenset({429, 500, 502, 503, 504})
DEFAULT_FUS_MAX_RETRIES=2
DEFAULT_FUS_RETRY_BASE_SECONDS=0.4
UNAVAILABLE_FUS_STATUSES=frozenset(
    {
        '400',
        '403',
        '404',
        '410',
        *(f'F{i:02d}' for i in range(100)),
        *(f'S{i:02d}' for i in range(1, 100)),
    }
)
MAX_FUS_BINARY_BYTES=64*1024**3
_RE_UNAVAILABLE_FUS_STATUS=re.compile(r'^(?:400|403|404|410|F\d{2}|S\d{2})$')
_RE_NORMALIZED_VERSION=re.compile(r'^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+){3}$')
_RE_BINARY_TIMESTAMP=re.compile(r'(?:^|_)(\d{4})(\d{2})(\d{2})\d{6}(?:_|\.)')


class FirmwareUnavailable(ValueError):
    """FUS explicitly declines this exact archived build, rather than a network failure."""


class FUSAuthError(ValueError):
    """FUS session/authentication rejected with Status 401."""


def normalize_version(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_./-]{4,256}',value):
        raise ValueError('Invalid Samsung version')
    parts=value.split('/')
    if len(parts)==3:parts.append(parts[0])
    if len(parts)!=4:raise ValueError('Samsung version needs PDA/CSC/PHONE/DATA')
    if not parts[2]:parts[2]=parts[0]
    normalized='/'.join(parts)
    if len(normalized)>256 or not _RE_NORMALIZED_VERSION.fullmatch(normalized):
        raise ValueError('Invalid Samsung version')
    return normalized


def parse_version_xml(xml_payload: bytes | str) -> tuple[tuple[str, str | None] | None, list[str]]:
    """Safely parse official fota-cloud-dn.ospserver.net version.xml.

    Returns ((latest_normalized_version, latest_android_os), upgrade_normalized_versions).
    """
    raw_bytes = xml_payload.encode('utf-8') if isinstance(xml_payload, str) else bytes(xml_payload)
    upper_bytes = raw_bytes.upper()
    if b'<!DOCTYPE' in upper_bytes or b'<!ENTITY' in upper_bytes:
        raise ValueError('Unsafe Samsung version.xml')
    if len(raw_bytes) > 1024 * 1024:
        raise ValueError('Samsung version.xml exceeds size limit')
    try:
        root = ET.fromstring(raw_bytes.decode('utf-8', errors='replace'))
    except ET.ParseError as exc:
        raise ValueError('Malformed Samsung version.xml') from exc

    latest_tuple: tuple[str, str | None] | None = None
    seen_versions: set[str] = set()
    latest_node = root.find('.//version/latest')
    if latest_node is None:
        latest_node = root.find('.//latest')
    if latest_node is not None and latest_node.text and latest_node.text.strip():
        try:
            norm_latest = normalize_version(latest_node.text.strip())
        except ValueError:
            norm_latest = None
        if norm_latest:
            raw_os = (latest_node.attrib.get('o') or '').strip()
            android_os = raw_os if raw_os and re.fullmatch(r'\d+(?:\.\d+)*L?', raw_os, re.I) else None
            latest_tuple = (norm_latest, android_os)
            seen_versions.add(norm_latest)

    upgrades: list[str] = []
    upgrade_nodes = root.findall('.//version/upgrade/value') or root.findall('.//upgrade/value')
    for val_node in upgrade_nodes:
        raw_val = (val_node.text or '').strip()
        if not raw_val:
            continue
        try:
            norm_val = normalize_version(raw_val)
        except ValueError:
            continue
        if norm_val not in seen_versions:
            seen_versions.add(norm_val)
            upgrades.append(norm_val)

    return latest_tuple, upgrades


def extract_binary_name_date(binary_name: str | None) -> str | None:
    """Extract YYYY-MM-DD build date from Samsung FUS BINARY_NAME timestamp when present."""
    if not binary_name:
        return None
    m = _RE_BINARY_TIMESTAMP.search(binary_name)
    if not m:
        return None
    year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (2010 <= year <= 2099):
        return None
    try:
        return datetime.date(year, month, day).isoformat()
    except ValueError:
        return None


def clear_fus_session_cookies(client: httpx.Client) -> None:
    """Clear Samsung FUS session cookies (`SESSION`, `SCOUTER`, `JSESSIONID`) before starting a fresh nonce."""
    jar = getattr(client.cookies, 'jar', None)
    if jar is None:
        return
    for cookie in list(jar):
        dom = (cookie.domain or '').lstrip('.').lower()
        if (
            dom == 'cdngc.net'
            or dom.endswith('.cdngc.net')
            or dom.startswith('neofussvr.sslcs.cdngc.net.')
            or (not dom and cookie.name.upper() in {'SESSION', 'SCOUTER', 'JSESSIONID'})
        ):
            try:
                jar.clear(cookie.domain, cookie.path, cookie.name)
            except KeyError:
                pass


class SmartFUS:
    def __init__(
        self,
        client=None,
        *,
        max_retries: int = DEFAULT_FUS_MAX_RETRIES,
        sleep_fn=time.sleep,
    ):
        self.client=client or httpx.Client(timeout=20,follow_redirects=False)
        self.owned=client is None;self.nonce='';self.auth=''
        self.cookies: dict[str, str] = {}
        self.max_retries = max(0, int(max_retries))
        self._sleep = sleep_fn

    def close(self):
        if self.owned:self.client.close()

    def _reset_session(self) -> None:
        self.nonce = ''
        self.auth = ''
        self.cookies.clear()
        clear_fus_session_cookies(self.client)

    def request(self,endpoint,body='',read=True):
        if endpoint not in ('NF_SmartDownloadGenerateNonce.do','NF_SmartDownloadBinaryInform.do'):
            raise ValueError('Only metadata endpoints are permitted')
        headers={'User-Agent':'SMART 2.0','Accept':'application/xml','Content-Type':'application/xml',
            'Authorization':f'FUS nonce="{self.nonce}", signature="{self.auth}", nc="", type="", realm="", newauth="1"'}
        if self.cookies:
            headers['Cookie']='; '.join(f'{k}={v}' for k,v in self.cookies.items())
        with self.client.stream('POST',HOST+endpoint,headers=headers,content=body.encode()) as response:
            for raw_cookie in response.headers.get_list('set-cookie'):
                pair=raw_cookie.split(';',1)[0].strip()
                if '=' in pair:
                    ck,cv=pair.split('=',1)
                    if ck.strip():
                        self.cookies[ck.strip()]=cv.strip()
            if response.status_code==404 and endpoint=='NF_SmartDownloadBinaryInform.do':
                raise FirmwareUnavailable('FUS metadata unavailable: 404')
            response.raise_for_status()
            nonce=response.headers.get('nonce')
            if nonce:
                if not re.fullmatch('[A-Za-z0-9+/=_-]{1,128}',nonce):raise ValueError('Invalid FUS nonce')
                self.nonce=nonce
                block=nonce.encode()[:16].ljust(16,b'0')
                enc=Cipher(algorithms.AES(AUTH_KEY),modes.ECB()).encryptor()
                self.auth=(enc.update(block)+enc.finalize()).hex()
            if not read:return b''
            media=response.headers.get('Content-Type','').lower().split(';')[0].strip()
            if media not in ('application/xml','text/xml','text/plain','text/html'):
                raise ValueError('FUS metadata client refused binary content')
            data=bytearray()
            for chunk in response.iter_bytes(65536):
                data.extend(chunk)
                if len(data)>1024*1024:raise ValueError('FUS metadata exceeds limit')
        if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():raise ValueError('Unsafe FUS XML')
        return bytes(data)

    def _binary_info_once(self, model: str, region: str, version: str) -> dict:
        self.request('NF_SmartDownloadGenerateNonce.do',read=False)
        if not self.nonce:raise ValueError('FUS handshake did not return a nonce')
        logic=''.join(version[ord(c)&15] if (ord(c)&15)<len(version) else '.' for c in self.nonce)
        root=ET.Element('FUSMsg');hdr=ET.SubElement(root,'FUSHdr')
        for key,value in [('ProtoVer','1.0'),('SessionID','0'),('MsgID','1')]:ET.SubElement(hdr,key).text=value
        body=ET.SubElement(root,'FUSBody');put=ET.SubElement(body,'Put');ET.SubElement(put,'CmdID').text='1'
        for key,value in {'ACCESS_MODE':'1','BINARY_NATURE':'1','REQUEST_TYPE':'2','LOGIC_CHECK':logic,
            'BINARY_SW_VERSION':version,'BINARY_LOCAL_CODE':region,'BINARY_MODEL_NAME':model}.items():
            ET.SubElement(ET.SubElement(put,key),'Data').text=value
        get=ET.SubElement(body,'Get');ET.SubElement(get,'CmdID').text='2';ET.SubElement(get,'BINARY_SW_VERSION')
        reply=ET.fromstring(self.request('NF_SmartDownloadBinaryInform.do',ET.tostring(root,encoding='unicode')))
        status=(reply.findtext('./FUSBody/Results/Status') or '').strip()
        if status not in ('200','S00'):
            if status=='401':
                raise FUSAuthError('FUS session unauthorized: 401')
            safe_status=status if status and re.fullmatch('[A-Za-z0-9]{1,8}',status) else 'unknown'
            if status in UNAVAILABLE_FUS_STATUSES or (
                status!='S00' and _RE_UNAVAILABLE_FUS_STATUS.fullmatch(status) is not None
            ):
                raise FirmwareUnavailable(f'FUS metadata unavailable: {safe_status}')
            raise ValueError(f'FUS metadata unavailable: {safe_status}')
        fields={node.tag:node.findtext('Data') for node in reply.iter() if node.find('Data') is not None}
        actual=normalize_version(fields.get('BINARY_SW_VERSION') or fields.get('LATEST_FW_VERSION') or '')
        if actual!=version:raise ValueError('FUS returned a different build')
        filename=fields.get('BINARY_NAME') or '';path=fields.get('MODEL_PATH') or ''
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,240}\.enc[24]',filename) or not re.fullmatch(r'/[A-Za-z0-9_/-]{1,160}/',path):
            raise ValueError('Invalid FUS package reference')
        try:
            size=int(fields.get('BINARY_BYTE_SIZE') or 0)
        except ValueError as exc:
            raise ValueError('Invalid FUS package size') from exc
        if not (0<size<=MAX_FUS_BINARY_BYTES):
            raise ValueError('Invalid FUS package size')
        return {'version':actual,'filename':filename,'path':path,'size':size,
            'resolution':'fus_metadata_only','download_service_required':True}

    def binary_info(self,model,region,version):
        if not re.fullmatch(r'(?:SM|GT)-[A-Z0-9]{3,16}',model) or not re.fullmatch('[A-Z0-9]{3}',region):
            raise ValueError('Invalid Samsung model/CSC')
        version=normalize_version(version)
        for attempt in range(self.max_retries + 1):
            if attempt > 0:
                self._reset_session()
            try:
                return self._binary_info_once(model, region, version)
            except httpx.TransportError:
                if attempt >= self.max_retries:
                    raise
                self._reset_session()
                self._sleep(DEFAULT_FUS_RETRY_BASE_SECONDS * (2 ** attempt))
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in TRANSIENT_FUS_HTTP_STATUSES or attempt >= self.max_retries:
                    raise
                self._reset_session()
                self._sleep(DEFAULT_FUS_RETRY_BASE_SECONDS * (2 ** attempt))
        raise AssertionError('unreachable')


def resolve_release(device, release, client):
    """Only nonce/Inform metadata here; binary bytes and session keys stay on VPS."""
    from .download_refresh import fingerprint, PREFIX
    model=device.get('codename') or device['upstream_id']
    region=release['region']
    info=SmartFUS(client).binary_info(model,region,release['version'])
    source={'schema':1,'vendor':'samsung','model':model,'region':region,
            **{k:info[k] for k in ('version','filename','path','size')}}
    return {**release,'version':info['version'],'download_url':PREFIX+fingerprint(source),
            'refresh_source':source,'filename':info['filename'],'size':str(info['size'])+' bytes',
            'resolution':'resolved','origin_kind':'oem_on_demand'}
