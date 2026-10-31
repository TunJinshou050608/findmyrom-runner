import gzip,io,json,hashlib,hmac,zipfile
from pathlib import Path
import httpx,pytest
from cryptography.hazmat.primitives.ciphers import Cipher,algorithms,modes
from cryptography.hazmat.primitives.padding import PKCS7
from fmr_ingest_stock.sony_source import (RESOURCE_FORMAT_KEY,decode_resources,devices,releases,manifest_digest,validate_manifest)
from fmr_ingest_stock.sony_bundle import assemble,Uploader,download_chunk
from fmr_ingest_stock.catalogue_sync import publish_release
from fmr_ingest_stock.catalogue_store import Store
from fmr_ingest_stock.catalogue_cache import CachedClient
from fmr_ingest_stock.oem_sources import MetadataHTTP
from fmr_ingest_stock.device_sync import ensure_base_brands_and_anchor
from fmr_core.d1_client import D1Client
from fmr_core.crypto import generate_keypair


def encrypt(text):
 raw=gzip.compress(text.encode());p=PKCS7(128).padder();raw=p.update(raw)+p.finalize();iv=b'I'*16
 c=Cipher(algorithms.AES(RESOURCE_FORMAT_KEY),modes.CBC(iv)).encryptor()
 return iv+c.update(raw)+c.finalize()


def manifest(data=b'PK complete mock firmware'):
 return {'model':'XQ-TEST','version':'1.0','cdf':'1234','cust':'Customized HK','service':'https://app.swup.update.sony.net/ess-distribution/public/api/service',
  'files':[{'key':'APP_SW','name':'firmware.zip','size':len(data),'chunks':[{'number':0,'size':len(data),'url':'https://software.sonymobile.com/content'}]}]}


def test_manifest_validates_whole_components_and_rejects_missing_or_foreign_chunks():
 m=manifest();assert validate_manifest(m)>0
 m['files'][0]['chunks'][0]['url']='https://evil.example/content'
 with pytest.raises(ValueError):validate_manifest(m)
 m=manifest();m['files'][0]['chunks'][0]['number']=2
 with pytest.raises(ValueError):validate_manifest(m)


def test_resource_decryption_and_metadata_only_catalogue(tmp_path):
 db=D1Client(local_sqlite_path=str(tmp_path/'sony.sqlite'));db.execute_script(Path(__file__).parents[1].joinpath('schema.sql').read_text());ensure_base_brands_and_anchor(db)
 xml='<XperiFirm><product id="1" name="platform" label="Xperia"><model id="2" name="XQ-TEST" hwid="3"><cust id="1234" name="Customized HK" /></model></product></XperiFirm>'
 raw=encrypt(xml);assert decode_resources(raw).tag=='XperiFirm'
 root='https://app.swup.update.sony.net/ess-distribution/public/api/'
 def handler(req):
  if req.url.host=='igoreisberg.com':return httpx.Response(200,headers={'Content-Type':'application/octet-stream'},content=raw)
  assert req.url.host=='app.swup.update.sony.net' # Sony binary CDN is never requested.
  if req.url.path.endswith('/match/v2'):
   text=f'<root><software-device-service-info><logic-type>SOFTWARE_UPDATE</logic-type><software-version>OLD</software-version><link rel="self" href="{root}old" /></software-device-service-info><software-device-service-info><logic-type>SOFTWARE_UPDATE_CONTENT_ERASE</logic-type><software-version>NEW</software-version><link rel="self" href="{root}service" /></software-device-service-info></root>'
  elif req.url.path.endswith('/service'):
   text=f'<root><file-resource content-id="x"><file-key>APP_SW</file-key><name>firmware.zip</name><plain-size>4</plain-size><link rel="file-chunk-info" href="{root}chunks" /></file-resource></root>'
  else:text='<root><file-chunk number="0"><size>4</size><link rel="download" href="https://software.sonymobile.com/content" /></file-chunk></root>'
  return httpx.Response(200,headers={'Content-Type':'application/xml'},text=text)
 with httpx.Client(transport=httpx.MockTransport(handler)) as client:
  cache=CachedClient(Store(db,'sony'),client);http=MetadataHTTP(cache)
  found=devices(http);assert len(found)==1
  _,rows,provenance=releases(found[0],http)
  assert not provenance['errors'] and len(rows)==1 and rows[0]['version']=='NEW'
  assert rows[0]['kind']=='sony_bundle'
  assert rows[0]['download_url']=='https://findmyrom.com/sony/bundles/'+manifest_digest(rows[0]['bundle'])
 db.close()


def test_assembly_yields_one_zip_with_original_complete_components_and_reuses_downloads(tmp_path):
 payload=b'PK main firmware bytes'*1000;extra=b'customer data'
 m=manifest(payload);m['files'].append({'key':'CUSTOMER_ID','name':'customer.zip','size':len(extra),'chunks':[{'number':1,'size':len(extra),'url':'https://software.sonymobile.com/customer'}]})
 calls=[]
 def handler(req):calls.append(req);return httpx.Response(200,content=extra if req.url.path=='/customer' else payload)
 with httpx.Client(transport=httpx.MockTransport(handler)) as client:
  path=assemble(m,tmp_path,client)
  with zipfile.ZipFile(path) as z:
   assert sorted(z.namelist())==['customer.zip','firmware.zip']
   assert z.read('firmware.zip')==payload and z.read('customer.zip')==extra
   assert z.testzip() is None
  sha=hashlib.sha256(path.read_bytes()).hexdigest()
  again=assemble(m,tmp_path,client)
  assert hashlib.sha256(again.read_bytes()).hexdigest()==sha
  assert len(calls)==2 # Completed synthetic chunks are not requested again.


def test_interrupted_chunk_uses_range_and_never_appends_a_full_200_response(tmp_path):
 data=b'0123456789';path=tmp_path/'chunk.part';path.with_suffix('.partial').write_bytes(data[:4])
 def handler(req):
  assert req.headers['Range']=='bytes=4-9'
  return httpx.Response(206,headers={'Content-Range':'bytes 4-9/10'},content=data[4:])
 with httpx.Client(transport=httpx.MockTransport(handler)) as c:download_chunk(c,'https://software.sonymobile.com/file',path,10)
 assert path.read_bytes()==data


def test_upload_auth_matches_worker_ticket_contract():
 def handler(req):
  exp=req.headers['X-FMR-Expires'];payload='v1|sony|'+'a'*64+'|preflight|||'+('0'*64)+'|0'
  expected=hmac.new(b'test',f'token={payload}&exp={exp}'.encode(),hashlib.sha256).hexdigest()
  assert req.headers['X-FMR-Signature']==expected
  return httpx.Response(200,json={'ready':True})
 with httpx.Client(transport=httpx.MockTransport(handler)) as client:assert Uploader(client,'test').request('a'*64,'preflight')['ready']


def test_publisher_indexes_one_bundle_and_keeps_transport_manifest_private(tmp_path,monkeypatch):
 db=D1Client(local_sqlite_path=str(tmp_path/'publish.sqlite'));db.execute_script(Path(__file__).parents[1].joinpath('schema.sql').read_text());ensure_base_brands_and_anchor(db)
 key,_=generate_keypair();monkeypatch.setenv('SIGN_PRIVATE_KEY_HEX',key)
 m=manifest();hash=manifest_digest(m)
 row={'version':'1.0','region':'CDF 1234 · Customized HK','kind':'sony_bundle','bundle':m,'download_url':'https://findmyrom.com/sony/bundles/'+hash}
 device={'vendor':'sony','upstream_id':'xq-test','codename':'XQ-TEST','name':'Xperia Test'}
 publish_release(db,device,row)
 records=db.query('SELECT * FROM stock_firmwares');assert len(records)==1
 assert records[0]['package_type']=='sony_bundle' and records[0]['extract_status']=='skipped'
 saved=db.query("SELECT last_cursor_val FROM crawler_sources WHERE source_kind='sony_bundle'")
 assert json.loads(saved[0]['last_cursor_val'])['public_token']==records[0]['public_token']
 assert 'software.sonymobile.com' not in records[0]['fast_cdn_url']
 db.close()
