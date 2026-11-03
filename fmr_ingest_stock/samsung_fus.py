"""Bounded Smart FUS metadata queries; no init/download/binary endpoint exists here.

Protocol adaptation from topjohnwu/samloader-rs (Apache-2.0), copyright 2026
John "topjohnwu" Wu. See docs/licenses/samloader-rs.txt for attribution/license.
"""
import re
import xml.etree.ElementTree as ET
import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

HOST='https://neofussvr.sslcs.cdngc.net/'
# Shared protocol constant published by the upstream client, not an account key.
AUTH_KEY=bytes.fromhex('422e73733617ae2b198940fd4e32b0a5')


class FirmwareUnavailable(ValueError):
    """FUS explicitly declines this exact archived build, rather than a network failure."""


def normalize_version(value):
    if not re.fullmatch(r'[A-Za-z0-9_./-]{4,256}',value):raise ValueError('Invalid Samsung version')
    parts=value.split('/')
    if len(parts)==3:parts.append(parts[0])
    if len(parts)!=4:raise ValueError('Samsung version needs PDA/CSC/PHONE/DATA')
    if not parts[2]:parts[2]=parts[0]
    return '/'.join(parts)


class SmartFUS:
    def __init__(self,client=None):
        self.client=client or httpx.Client(timeout=20,follow_redirects=False)
        self.owned=client is None;self.nonce='';self.auth=''

    def close(self):
        if self.owned:self.client.close()

    def request(self,endpoint,body='',read=True):
        if endpoint not in ('NF_SmartDownloadGenerateNonce.do','NF_SmartDownloadBinaryInform.do'):
            raise ValueError('Only metadata endpoints are permitted')
        headers={'User-Agent':'SMART 2.0','Accept':'application/xml','Content-Type':'application/xml',
            'Authorization':f'FUS nonce="{self.nonce}", signature="{self.auth}", nc="", type="", realm="", newauth="1"'}
        with self.client.stream('POST',HOST+endpoint,headers=headers,content=body.encode()) as response:
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

    def binary_info(self,model,region,version):
        if not re.fullmatch(r'(?:SM|GT)-[A-Z0-9]{3,16}',model) or not re.fullmatch('[A-Z0-9]{3}',region):
            raise ValueError('Invalid Samsung model/CSC')
        version=normalize_version(version)
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
        status=reply.findtext('./FUSBody/Results/Status')
        if status not in ('200','S00'):
            error = FirmwareUnavailable if status in ('404','F00','F01','F02','F03','F05','F11') else ValueError
            raise error('FUS metadata unavailable: '+(status if status and re.fullmatch('[A-Za-z0-9]{1,8}',status) else 'unknown'))
        fields={node.tag:node.findtext('Data') for node in reply.iter() if node.find('Data') is not None}
        actual=normalize_version(fields.get('BINARY_SW_VERSION') or fields.get('LATEST_FW_VERSION') or '')
        if actual!=version:raise ValueError('FUS returned a different build')
        filename=fields.get('BINARY_NAME') or '';path=fields.get('MODEL_PATH') or ''
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,240}\.enc[24]',filename) or not re.fullmatch(r'/[A-Za-z0-9_/-]{1,160}/',path):
            raise ValueError('Invalid FUS package reference')
        size=int(fields.get('BINARY_BYTE_SIZE') or 0)
        if size<=0:raise ValueError('Invalid FUS package size')
        return {'version':actual,'filename':filename,'path':path,'size':size,
            'resolution':'fus_metadata_only','download_service_required':True}


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
