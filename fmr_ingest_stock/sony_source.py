"""Sony ESS metadata only; internal chunks form ONE complete firmware bundle."""
import hashlib
import json
import re
from urllib.parse import urlencode,urlsplit,unquote
import xml.etree.ElementTree as ET
import zlib
from cryptography.hazmat.primitives.ciphers import Cipher,algorithms,modes
from cryptography.hazmat.primitives.padding import PKCS7

API='https://app.swup.update.sony.net/ess-distribution/public/api'
RESOURCES='https://igoreisberg.com/xperifirm/resources'
# Public XperiFirm resource-format key from the user's script, not account auth.
RESOURCE_FORMAT_KEY=bytes.fromhex('f278bfe19cb5cbcfd5c13262536ec198')
MAX_XML=8*1024*1024


def xml(data):
    if len(data)>MAX_XML or b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():raise ValueError('Unsafe Sony metadata')
    return ET.fromstring(data)


def api_url(value):
    value=unquote(value)
    p=urlsplit(value)
    if p.scheme!='https' or p.hostname!='app.swup.update.sony.net' or p.port or p.username or p.password or not p.path.startswith('/ess-distribution/public/api/'):
        raise ValueError('Untrusted Sony metadata link')
    return value


def decode_resources(raw):
    if len(raw)<32 or len(raw)>2*1024*1024 or (len(raw)-16)%16:raise ValueError('Invalid Sony resource envelope')
    decryptor=Cipher(algorithms.AES(RESOURCE_FORMAT_KEY),modes.CBC(raw[:16])).decryptor()
    unpadder=PKCS7(128).unpadder();packed=decryptor.update(raw[16:])+decryptor.finalize()
    packed=unpadder.update(packed)+unpadder.finalize()
    decoder=zlib.decompressobj(31);data=decoder.decompress(packed,MAX_XML+1)
    if len(data)>MAX_XML or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:raise ValueError('Sony resource expansion exceeds bound')
    return xml(data)


def devices(http):
    with http.client.stream('GET',RESOURCES) as response:
        response.raise_for_status();raw=response.read()
    root=decode_resources(raw)
    if root.tag!='XperiFirm':raise ValueError('Invalid Sony catalogue')
    rows=[]
    for product in root.iter('product'):
        for model in product.findall('model'):
            name=model.get('name','')
            if not re.fullmatch('[A-Za-z0-9-]{2,32}',name):continue
            custs=[{'id':c.get('id'),'name':c.get('name'),'spc':c.get('spc')} for c in model.findall('cust') if c.get('id')]
            if not custs:continue
            rows.append({'vendor':'sony','upstream_id':name.lower(),'codename':name,'models':name,
                'name':(product.get('label') or product.get('name') or 'Xperia')+' ('+name+')',
                'source_kind':'sony_ess','sony':{'pid':product.get('id'),'mid':model.get('id'),
                'hwid':model.get('hwid'),'custs':custs}})
    if not rows:raise ValueError('No Sony models found')
    return rows


def local_tag(tag):return tag.rsplit('}',1)[-1]


def nodes(root,name):return [e for e in root.iter() if local_tag(e.tag)==name]


def field(node,name):
    return next(((e.text or '').strip() for e in node.iter() if local_tag(e.tag)==name),'')


def link(node,rel):
    return next((e.get('href','') for e in node.iter() if local_tag(e.tag)=='link' and e.get('rel')==rel),'')


def manifest_digest(manifest):
    return hashlib.sha256(json.dumps(manifest,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def validate_manifest(manifest):
    from .oem_sources import download_origin
    files=manifest.get('files',[])
    if not files or len(files)>20:raise ValueError('Invalid Sony component count')
    total=0;names=set()
    for f in files:
        name=f.get('name','')
        if not re.fullmatch('[A-Za-z0-9._-]{1,180}',name) or name in ('.','..') or name in names:raise ValueError('Invalid Sony filename')
        names.add(name)
        chunks=f.get('chunks',[])
        if not chunks or len(chunks)>256:raise ValueError('Invalid Sony chunk count')
        start=chunks[0]['number']
        if start not in (0,1) or [c['number'] for c in chunks]!=list(range(start,start+len(chunks))):raise ValueError('Sony chunk sequence gap')
        for c in chunks:
            if type(c['size']) is not int or c['size']<=0 or c['size']>128*1024*1024 or download_origin('sony',c['url'])!='oem':raise ValueError('Untrusted Sony chunk')
        if sum(c['size'] for c in chunks)!=f['size']:raise ValueError('Sony component size mismatch')
        total+=f['size']
    if total>8*1024**3:raise ValueError('Complete Sony bundle exceeds temporary storage budget')
    return total


def releases(device,http):
    config=device['sony'];rows=[];errors=[]
    prefer=['SOFTWARE_UPDATE_CONTENT_ERASE','CR_SOFTWARE_UPDATE','SOFTWARE_UPDATE']
    for cust in config['custs']:
        try:
            query={'CDFId':cust['id'],'HwSetupKey':'Default','HWVariantId':config['hwid'],
                'ModelObjectId':config['mid'],'ProductObjectId':config['pid'],'SecurityStateType':'COMMERCIAL'}
            if cust.get('spc'):query['SonyProductCode']=cust['spc']
            root=xml(http.get(API+'/device-service/match/v2?'+urlencode(query)))
            services=[]
            for node in nodes(root,'software-device-service-info'):
                version=field(node,'software-version');logic=field(node,'logic-type');url=link(node,'self')
                if version and logic and url:services.append({'version':version,'logic':logic,'url':api_url(url)})
            if not services:raise ValueError('No Sony firmware service')
            # Version belongs to the chosen service, not an unrelated first result.
            service=min(services,key=lambda s:prefer.index(s['logic']) if s['logic'] in prefer else 99)
            listing=xml(http.get(service['url']));files=[]
            for node in nodes(listing,'file-resource'):
                chunks_xml=xml(http.get(api_url(link(node,'file-chunk-info'))))
                chunks=[]
                for chunk in nodes(chunks_xml,'file-chunk'):
                    url=link(chunk,'download')
                    # Transport upgrades are allowed only for Sony's own CDN.
                    if url.startswith('http://') and urlsplit(url).hostname in ('software.sonymobile.com','software.sony.net'):url='https://'+url[7:]
                    chunks.append({'number':int(chunk.get('number','0')),'size':int(field(chunk,'size')),'url':url})
                chunks.sort(key=lambda c:c['number'])
                files.append({'key':field(node,'file-key'),'name':field(node,'name'),
                    'size':int(field(node,'plain-size')),'chunks':chunks})
            manifest={'model':device['codename'],'cust':cust['name'],'cdf':cust['id'],
                'version':service['version'],'service':service['url'],'files':files}
            total=validate_manifest(manifest);key=manifest_digest(manifest)
            rows.append({'vendor':'sony','device_id':device['upstream_id'],'version':service['version'],
                'region':(cust['name'] or 'Customization')[:40]+' · CDF '+cust['id'],'kind':'sony_bundle',
                'android_version':'unknown','size':f'{total/1024**3:.2f} GiB · complete firmware ZIP',
                'bundle':manifest,'download_url':'https://findmyrom.com/sony/bundles/'+key,
                'origin_kind':'sony_assembled_bundle','resolution':'resolved'})
        except Exception as exc:
            if getattr(exc,'fmr_batch_deadline',False):raise
            errors.append({'cdf':cust['id'],'error':type(exc).__name__,'reason':str(exc)[:180]})
    return device,rows,{'provider':'sony_ess','errors':errors,'coverage':'currently offered customization services, not withdrawn firmware history'}
