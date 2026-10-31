"""Generated metadata and small archives only; no live firmware traffic."""
import hashlib,json
from pathlib import Path
import httpx,pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fmr_core.crypto import generate_keypair
from fmr_core.d1_client import D1Client
from fmr_ingest_stock.oem_sources import (MetadataHTTP,atlas_devices,atlas_releases,resolve_atlas,
    google_catalogue,xiaomi_devices,canonical_code,samsung_history,download_origin)
from fmr_ingest_stock.catalogue_sync import publish_device,publish_release,build_version
from fmr_ingest_stock.device_sync import ensure_base_brands_and_anchor
from fmr_ingest_stock.catalogue_job import task_key


def response(body,media='text/html',status=200,headers=None):
 return httpx.Response(status,headers={'Content-Type':media,**(headers or {})},stream=httpx.ByteStream(body.encode()))


def test_metadata_boundary_refuses_binary_urls_and_bodies_before_reading():
 calls=[]
 class Forbidden(httpx.SyncByteStream):
  def __iter__(self):raise AssertionError('Binary body consumed');yield b''
 def handler(req):calls.append(req);return httpx.Response(200,headers={'Content-Type':'application/zip'},stream=Forbidden())
 with httpx.Client(transport=httpx.MockTransport(handler)) as client:
  meta=MetadataHTTP(client)
  for url in ['https://fwatlas.com/stock.zip','https://127.0.0.1/catalogue','http://fwatlas.com/all']:
   with pytest.raises(ValueError):meta.get(url)
  assert not calls
  with pytest.raises(ValueError):meta.get('https://fwatlas.com/catalogue')
  assert len(calls)==1


def test_all_models_are_enumerated_across_pages_not_a_curated_target_list():
 def handler(req):
  if req.url.query:return response('<a href="/motorola/second">Second</a>')
  return response('<a href="/motorola/first"><h3>First</h3></a><a href="/motorola?page=2">Next</a><a href="https://evil.example/motorola/wrong">Wrong</a>')
 with httpx.Client(transport=httpx.MockTransport(handler)) as client:
  models=atlas_devices('motorola',MetadataHTTP(client))
 assert [m['upstream_id'] for m in models]==['first','second']


def test_xiaomi_dictionary_discovers_models_and_collapses_regions_preserving_route_case():
 data='ishtar: [Phone]\nishtar_global: [Phone Global]\nHM2013022: [Legacy]\n'
 with httpx.Client(transport=httpx.MockTransport(lambda r:response(data,'text/plain'))) as client:
  models,errors=xiaomi_devices(MetadataHTTP(client))
 assert len(models)==2 and not errors
 assert next(m for m in models if m['codename']=='hm2013022')['route_code']=='HM2013022'


def test_google_parses_both_official_sources_and_real_checksums():
 sha='a'*64
 html='<h2 id="sample">Pixel Sample</h2><table><tr><td>12.0.0 (BUILD.123)</td><td><a href="https://dl.google.com/android/aosp/sample.zip">Link</a></td><td>'+sha+'</td></tr></table>'
 seen=[]
 def handler(req):seen.append(req);return response(html)
 with httpx.Client(transport=httpx.MockTransport(handler)) as client:
  models,rows,errors=google_catalogue(MetadataHTTP(client))
 assert len(models)==1 and not errors and models[0]['android_release']==12
 assert {r['kind'] for r in rows['sample']}=={'full_ota','factory'}
 assert rows['sample'][0]['sha256']==sha and len(seen)==2
 assert all('devsite_wall_acks' in req.headers.get('cookie','') for req in seen)


def test_download_resolution_follows_public_go_link_without_fetching_binary():
 seen=[]
 def handler(req):
  seen.append(req)
  if req.url.path=='/oneplus/sample/build':
   return response('<h1>Sample BUILD.123</h1><a href="/download/1/build">Download</a>')
  if req.url.path=='/download/1/build':
   return response('<a href="/download/1/build/go?t=public">Start download now</a>')
  if req.url.path.endswith('/go'):
   return httpx.Response(302,headers={'Location':'https://gauss.example.allawnofs.com/sample.zip'})
  raise AssertionError('Firmware origin must never be fetched by metadata resolver')
 row={'vendor':'oneplus','version':'BUILD.123','detail_page':'https://fwatlas.com/oneplus/sample/build'}
 with httpx.Client(transport=httpx.MockTransport(handler)) as client:
  resolved=resolve_atlas(row,MetadataHTTP(client))
 assert resolved['resolution']=='resolved' and resolved['origin_kind']=='oem'
 assert len(seen)==3 and all(r.url.host=='fwatlas.com' for r in seen)


def test_redirect_to_another_build_cannot_be_published_under_requested_version():
 with httpx.Client(transport=httpx.MockTransport(lambda r:response('<h1>Different BUILD.999</h1>'))) as client:
  with pytest.raises(ValueError):resolve_atlas({'vendor':'oneplus','version':'BUILD.123','detail_page':'https://fwatlas.com/oneplus/sample/build'},MetadataHTTP(client))


def test_samsung_xml_history_is_not_a_download_and_does_not_request_device_identifiers():
 xml='<versioninfo><firmware><version><latest>PDA1/CSC1/MODEM1</latest><upgrade><value>PDA0/CSC0/MODEM0</value></upgrade></version></firmware></versioninfo>'
 seen=[]
 def handler(req):seen.append(req);return response(xml,'application/xml')
 with httpx.Client(transport=httpx.MockTransport(handler)) as client:
  builds=samsung_history('SM-S000B','EUX',MetadataHTTP(client))
 assert builds==['PDA0/CSC0/MODEM0','PDA1/CSC1/MODEM1']
 assert len(seen)==1 and seen[0].url.host=='fota-cloud-dn.ospserver.net' and not seen[0].url.query
 with httpx.Client(transport=httpx.MockTransport(lambda r:response('<!DOCTYPE x><x/>','application/xml'))) as client:
  with pytest.raises(ValueError):samsung_history('SM-S000B','EUX',MetadataHTTP(client))


@pytest.fixture
def database(tmp_path,monkeypatch):
 key,pub=generate_keypair();monkeypatch.setenv('SIGN_PRIVATE_KEY_HEX',key)
 db=D1Client(local_sqlite_path=str(tmp_path/'db.sqlite'))
 db.execute_script(Path(__file__).parents[1].joinpath('schema.sql').read_text());ensure_base_brands_and_anchor(db)
 yield db,pub
 db.close()


def test_publisher_preserves_existing_tokens_signatures_and_extraction_inputs(database):
 db,pub=database
 device={'vendor':'oneplus','upstream_id':'sample','codename':'sample','name':'Sample'}
 row={'version':'CPH123_16.0(EX01)','region':'EU','kind':'full','download_url':'https://ota.oneplus.net/sample.zip'}
 first=publish_release(db,device,row)
 stored=db.query('SELECT * FROM stock_firmwares')[0]
 payload=f"v1|fw|{stored['public_token']}|{stored['fast_cdn_url']}".encode()
 Ed25519PublicKey.from_public_bytes(bytes.fromhex(pub)).verify(bytes.fromhex(stored['ed25519_signature']),payload)
 db.execute("UPDATE stock_firmwares SET extract_status='processing'")
 second=publish_release(db,device,{**row,'download_url':'https://ota.oneplus.net/replaced.zip'})
 after=db.query('SELECT * FROM stock_firmwares')[0]
 assert second['status']=='preserved_existing' and first['public_token']==second['public_token']
 assert after['fast_cdn_url']==stored['fast_cdn_url'] and after['ed25519_signature']==stored['ed25519_signature'] and after['extract_status']=='processing'


def test_unresolved_or_short_lived_download_is_not_faked_as_a_permanent_rom(database):
 db,_=database
 device={'vendor':'oneplus','upstream_id':'sample','name':'Sample'}
 assert publish_release(db,device,{'version':'x'})['status']=='unresolved_download'
 assert publish_release(db,device,{'version':'x','download_url':'https://ota.oneplus.net/sample.zip?token=temporary'})['status']=='requires_download_refresh'
 assert not db.query('SELECT * FROM stock_firmwares')


def test_brand_namespaces_prevent_cross_vendor_codename_collisions():
 assert canonical_code('google','sample')!=canonical_code('motorola','sample')
 assert canonical_code('xiaomi','ishtar')=='ishtar'
 assert len(canonical_code('motorola','very-long-model-identity-'*5))<=32
 assert build_version('X'*90+'(A)')!=build_version('X'*90+'(B)')
 assert task_key('run','release','a')!=task_key('run','release','b')
 assert download_origin('motorola','https://mirrors.lolinet.com/sample.zip')=='archive_mirror'
 assert download_origin('google','https://dl.google.com.evil.example/sample.zip') is None


def test_controller_real_database_discovery_resolution_publication_and_report(database,monkeypatch):
 from fmr_ingest_stock.catalogue_job import start,step,report,get,tasks
 db,_=database
 monkeypatch.setenv('GITHUB_REF_NAME','oem-test-frozen');monkeypatch.setenv('GITHUB_SHA','a'*40)
 sha='b'*64
 html='<h2 id="sample">Pixel Sample</h2><table><tr><td>12 (BUILD.123)</td><td><a href="https://dl.google.com/android/sample.zip">Link</a></td><td>'+sha+'</td></tr></table>'
 seen=[]
 def handler(req):
  seen.append(req)
  assert req.url.host=='developers.google.com'
  return response(html)
 with httpx.Client(transport=httpx.MockTransport(handler)) as client:
  meta=MetadataHTTP(client)
  control=start(db,'test_run',['google'],'publish',meta)
  assert tasks(db,'test_run','model')
  assert step(db,control,meta) is False
  result=report(db,control)
 assert result['complete'] and not result['firmware_extraction_verified']
 assert len(seen)==2 and len(db.query('SELECT * FROM stock_firmwares'))==2
 assert all(r['extract_status']=='skipped' for r in db.query('SELECT * FROM stock_firmwares'))
 assert get(db,'test_run')['generation']==1
 assert not tasks(db,'test_run','release')


def test_controller_retries_transient_model_failure_without_claiming_success(database,monkeypatch):
 from fmr_ingest_stock.catalogue_job import save,step,tasks,task_key,report
 db,_=database
 control={'run':'retry_run','mode':'audit','generation':0,'summary':{'oneplus':{'models':1,'release_records':0,'resolved_downloads':0,'published':0,'errors':{}}}}
 device={'vendor':'oneplus','upstream_id':'sample','name':'Sample','page':'https://fwatlas.com/oneplus/sample'}
 save(db,task_key('retry_run','model','oneplus:sample'),{'device':device,'done':False})
 with httpx.Client(transport=httpx.MockTransport(lambda req:response('unavailable',status=503))) as client:
  meta=MetadataHTTP(client)
  assert step(db,control,meta) is True
  assert step(db,control,meta) is True
  assert step(db,control,meta) is False
 assert not tasks(db,'retry_run','model')
 assert control['summary']['oneplus']['errors']=={'HTTPStatusError':1}
 assert not report(db,control)['complete']


def test_missing_catalogue_and_deferred_sony_never_count_as_coverage(database,monkeypatch):
 from fmr_ingest_stock.catalogue_job import start,step,report
 db,_=database
 monkeypatch.setenv('GITHUB_REF_NAME','test');monkeypatch.setenv('GITHUB_SHA','a'*40)
 with httpx.Client(transport=httpx.MockTransport(lambda req:response('no models'))) as client:
  meta=MetadataHTTP(client);control=start(db,'blocked_run',['sony','samsung'],'audit',meta)
  assert step(db,control,meta) is False
 result=report(db,control)
 assert not result['complete']
 assert result['manufacturers']['sony']['status']=='deferred_by_user'
 assert result['manufacturers']['samsung']['status']=='discovery_failed'
 assert not db.query('SELECT * FROM stock_firmwares')


def test_model_release_history_pagination_is_not_latest_only():
 def handler(req):
  version='BUILD.OLD' if req.url.query else 'BUILD.NEW'
  table='<table><tbody><tr data-region="EU"><td><a href="/oneplus/sample/'+version+'"><code>'+version+'</code></a></td><td data-label="Type">Full OTA</td></tr></tbody></table>'
  return response(table+('' if req.url.query else '<a href="?page=2">Next</a>'))
 device={'vendor':'oneplus','upstream_id':'sample','page':'https://fwatlas.com/oneplus/sample'}
 with httpx.Client(transport=httpx.MockTransport(handler)) as client:
  _,rows=atlas_releases(device,MetadataHTTP(client))
 assert {r['version'] for r in rows}=={'BUILD.NEW','BUILD.OLD'}


def test_cloud_entrypoint_autonomously_continues_same_frozen_tag_without_binary_traffic(tmp_path,monkeypatch):
 import fmr_ingest_stock.catalogue_job as job
 dbpath=str(tmp_path/'cloud.db')
 db=D1Client(local_sqlite_path=dbpath)
 db.execute_script(Path(__file__).parents[1].joinpath('schema.sql').read_text());db.close()
 key,_=generate_keypair()
 for name,value in {'GITHUB_ACTIONS':'true','GITHUB_REF_TYPE':'tag','GITHUB_REF_NAME':'oem-frozen-test',
     'GITHUB_SHA':'a'*40,'GITHUB_RUN_ID':'123','GITHUB_REPOSITORY':'owner/runner',
     'GH_WORKFLOW_TOKEN':'ephemeral_test','CF_ACCOUNT_ID':'test','CF_API_TOKEN':'test',
     'CF_D1_DATABASE_ID':'test','SIGN_PRIVATE_KEY_HEX':key}.items():monkeypatch.setenv(name,value)
 monkeypatch.setattr(job,'D1Client',lambda **kwargs:D1Client(local_sqlite_path=dbpath))
 dispatched=[];requested=[]
 def handler(req):
  requested.append(req)
  if req.url.host=='api.github.com':
   assert req.headers['authorization']=='Bearer ephemeral_test'
   dispatched.append(json.loads(req.content));return httpx.Response(204)
  assert req.url.host=='fwatlas.com' # Any binary-host request fails this test.
  if req.url.path=='/oneplus':return response(''.join('<a href="/oneplus/sample-'+str(n)+'">Sample '+str(n)+'</a>' for n in range(9)))
  if req.url.path.endswith('/build'):
   return response('<h1>BUILD.123</h1><a href="https://ota.oneplus.net/sample.zip">Download</a>')
  return response('<table><tbody><tr data-region="EU"><td><a href="'+req.url.path+'/build"><code>BUILD.123</code></a></td><td data-label="Type">Full OTA</td></tr></tbody></table>')
 real_client=httpx.Client
 monkeypatch.setattr(job.httpx,'Client',lambda *args,**kwargs:real_client(*args,**kwargs,transport=httpx.MockTransport(handler)))
 assert job.main(['--brands','oneplus','--mode','publish'])==0
 assert len(dispatched)==1 and dispatched[0]['ref']=='oem-frozen-test'
 assert dispatched[0]['inputs']['run_id']=='oem_123' and dispatched[0]['inputs']['generation']=='1'
 assert job.main(['--run-id','oem_123','--generation','1'])==0
 assert len(dispatched)==1
 db=D1Client(local_sqlite_path=dbpath)
 assert job.get(db,'oem_123')['state']=='finished'
 assert len(db.query('SELECT * FROM stock_firmwares'))==9
 assert job.get(db,'oem_123:report')['firmware_extraction_verified'] is False
 db.close()
 assert job.main(['--run-id','oem_123','--generation','1'])==0 # Stale duplicate cannot repeat or dispatch.
 assert len(dispatched)==1
