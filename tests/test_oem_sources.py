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
 assert all(r['extract_status']=='pending' for r in db.query('SELECT * FROM stock_firmwares'))
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


def test_progress_logs_only_operational_fields_and_heartbeat_stops(capsys):
 from fmr_ingest_stock.catalogue_job import progress,heartbeat
 import threading
 progress('task_checkpoint_saved',vendor='google',processed=2)
 row=json.loads(capsys.readouterr().out)
 assert row=={'event':'task_checkpoint_saved','vendor':'google','processed':2}
 with pytest.raises(ValueError):progress('unsafe',download_url='https://private.example/file.zip')
 stop=threading.Event();stop.set()
 heartbeat(stop)
 assert not capsys.readouterr().out


def test_vivo_global_and_realme_ozip_are_resolved_without_fetching_rom_bodies():
 from fmr_ingest_stock.catalogue_sync import package_kind
 for vendor,host,suffix in [('vivo','in-sysup-awdl.vivoglobal.com','zip'),('realme','download.c.realme.com','ozip')]:
  seen=[]
  def handler(req):
   seen.append(req);assert req.url.host=='fwatlas.com'
   if not req.url.path.startswith('/download/'):return response('<h1>BUILD.123</h1><a href="/download/123/build">Download</a>')
   if req.url.path.endswith('/go'):return httpx.Response(302,headers={'Location':f'https://{host}/firmware.{suffix}'})
   return response('<a href="/download/123/build/go?t=public">Start download now</a>')
  with httpx.Client(transport=httpx.MockTransport(handler)) as client:
   row=resolve_atlas({'vendor':vendor,'version':'BUILD.123','kind':'ota','detail_page':f'https://fwatlas.com/{vendor}/sample/build'},MetadataHTTP(client))
  assert row['resolution']=='resolved' and row['origin_kind']=='oem' and len(seen)==3
  assert package_kind(vendor,row)==('recovery_ozip' if suffix=='ozip' else 'recovery_zip')


def test_signed_oplus_download_becomes_a_stable_refresh_descriptor(database):
 from fmr_ingest_stock.download_refresh import PREFIX, fingerprint, expiring
 from fmr_ingest_stock.vendor_catalogue import durable_download
 db,pub=database;seen=[]
 def handler(req):
  seen.append(req);assert req.url.host=='fwatlas.com'
  if req.url.path.endswith('/build'):return response('<h1>BUILD.123</h1><a href="/download/123/build-cn">Download</a>')
  if req.url.path.endswith('/go'):return httpx.Response(302,headers={'Location':'https://gauss.allawnfs.com/firmware.zip?x-oss-expires=60&x-oss-signature=secret'})
  return response('<a href="/download/123/build-cn/go?t=public">Start download now</a>')
 with httpx.Client(transport=httpx.MockTransport(handler)) as client:
  row=resolve_atlas({'vendor':'oneplus','version':'BUILD.123','kind':'full','detail_page':'https://fwatlas.com/oneplus/sample/build'},MetadataHTTP(client))
 assert durable_download('oneplus',row) and row['download_url']==PREFIX+fingerprint(row['refresh_source'])
 assert 'secret' not in json.dumps(row) and len(seen)==3
 device={'vendor':'oneplus','upstream_id':'sample','name':'Sample'}
 result=publish_release(db,device,row);stored=db.query('SELECT * FROM stock_firmwares')[0]
 assert stored['original_official_url']==row['download_url'] and result['package_type']=='recovery_zip'
 payload=json.loads(db.query("SELECT last_cursor_val FROM crawler_sources WHERE source_kind='stock_download_refresh'")[0]['last_cursor_val'])
 assert payload['public_token']==stored['public_token']
 for query in ['X-OSS-Expires=60&X-OSS-Signature=secret','Expires=999&Signature=secret','sign=secret&t=1','X-Amz-Signature=secret']:
  assert expiring('https://gauss.allawnfs.com/file.zip?'+query)
  assert not durable_download('oneplus',{'download_url':'https://gauss.allawnfs.com/file.zip?'+query})
 assert not durable_download('oppo',row)
 with pytest.raises(ValueError):durable_download('oneplus',{**row,'refresh_source':{**row['refresh_source'],'landing_page':'https://evil.example/file'}})


def test_asus_legacy_http_and_unescaped_filenames_are_upgraded_only_for_oem_hosts():
 from fmr_ingest_stock.oem_sources import normalize_download
 assert normalize_download('asus','http://dlcdnets.asus.com/pub/ASUS/Phone Firmware.zip')=='https://dlcdnets.asus.com/pub/ASUS/Phone%20Firmware.zip'
 assert normalize_download('asus','http://dlcdnets.asus.com:80/pub/ASUS/file.zip?model=ROG Phone 9')=='https://dlcdnets.asus.com/pub/ASUS/file.zip?model=ROG%20Phone%209'
 assert normalize_download('asus','http://evil.example/Phone.zip')=='http://evil.example/Phone.zip'


def test_legacy_expiring_url_is_repaired_without_changing_token_or_extraction_state(database):
 from fmr_ingest_stock.download_refresh import renewable
 db,pub=database;device={'vendor':'oneplus','upstream_id':'sample','name':'Sample'}
 original={'vendor':'oneplus','version':'BUILD.123','kind':'full','download_url':'https://gauss.allawnfs.com/firmware.zip'}
 result=publish_release(db,device,original)
 # Simulate a signed OSS URL that an older adapter incorrectly accepted.
 db.execute("UPDATE stock_firmwares SET original_official_url=?,fast_cdn_url=?,extract_status='processing'",('https://gauss.allawnfs.com/firmware.zip?x-oss-expires=60','https://gauss.allawnfs.com/firmware.zip?x-oss-expires=60'))
 new=renewable(original,'https://fwatlas.com/download/123/build-cn')
 publish_release(db,device,new);stored=db.query('SELECT * FROM stock_firmwares')[0]
 assert stored['public_token']==result['public_token'] and stored['extract_status']=='processing'
 assert stored['fast_cdn_url']==new['download_url']
 Ed25519PublicKey.from_public_bytes(bytes.fromhex(pub)).verify(bytes.fromhex(stored['ed25519_signature']),f"v1|fw|{stored['public_token']}|{stored['fast_cdn_url']}".encode())


def test_public_oplus_download_check_gate_is_resolved_as_metadata_only():
 from fmr_ingest_stock.vendor_catalogue import durable_download
 seen=[]
 def handler(req):
  seen.append(req)
  if req.url.host=='component-ota-sg.allawnos.com':
   assert req.url.path=='/downloadCheck' and req.headers['userId']=='oplus-ota|16001011'
   return httpx.Response(302,headers={'Location':'https://gauss.allawnofs.com/firmware.zip?Expires=60&Signature=public'})
  assert req.url.host=='fwatlas.com'
  if req.url.path.endswith('/build'):return response('<h1>BUILD.123</h1><a href="/download/123/build-cn">Download</a>')
  if req.url.path.endswith('/go'):return httpx.Response(302,headers={'Location':'https://component-ota-sg.allawnos.com/downloadCheck?token=public'})
  return response('<a href="/download/123/build-cn/go?t=public">Start download now</a>')
 with httpx.Client(transport=httpx.MockTransport(handler)) as client:
  row=resolve_atlas({'vendor':'realme','version':'BUILD.123','kind':'full','detail_page':'https://fwatlas.com/realme/sample/build'},MetadataHTTP(client))
 assert durable_download('realme',row) and row['refresh_source']['filename']=='firmware.zip'
 assert len(seen)==4
 assert download_origin('oneplus','https://android.googleapis.com/packages/ota-api/package/aabb1234.zip')=='oem'
 assert download_origin('oneplus','https://android.googleapis.com/unrelated/file.zip') is None


def test_oplus_gate_never_reads_an_unexpected_binary_body():
 from fmr_ingest_stock.oem_sources import resolve_oplus_gate
 class Forbidden(httpx.SyncByteStream):
  def __iter__(self):raise AssertionError('Binary body consumed');yield b''
 def handler(req):return httpx.Response(200,headers={'Content-Type':'application/zip'},stream=Forbidden())
 with httpx.Client(transport=httpx.MockTransport(handler)) as client:
  with pytest.raises(ValueError):resolve_oplus_gate('realme','https://component-ota-sg.allawnos.com/downloadCheck?token=public',MetadataHTTP(client))


def test_display_version_can_contain_spaces_without_losing_identity():
 from fmr_ingest_stock.download_refresh import renewable,valid_release
 row=renewable({'vendor':'oneplus','version':'BUILD.123 (EX01)','kind':'full','download_url':'https://gauss.allawnfs.com/firmware.zip'},'https://fwatlas.com/download/123/build-cn')
 assert valid_release('oneplus',row) and row['refresh_source']['version']=='BUILD.123 (EX01)'


def test_unicode_display_version_hash_matches_web_protocol_vector():
 from fmr_ingest_stock.download_refresh import fingerprint
 source={'schema':1,'vendor':'oneplus','version':'BUILD.123\u00a0(EX01)','landing_page':'https://fwatlas.com/download/123/build-cn','filename':'firmware.zip'}
 assert fingerprint(source)=='a677aaa0f6366c4881af9ea76e700329ff173065d107de2cd0640e85940914da'


def test_samsung_fota_versions_never_become_invented_catalogue_urls():
 from fmr_ingest_stock.catalogue_job import collect_model
 seen=[]
 def handler(req):
  seen.append(req)
  if req.url.host=='fwatlas.com':return response('<dl><dt>Model numbers</dt><dd>SM-S000B</dd></dl><table><tbody><tr data-region="EUX"><td><a href="/samsung/sample/actual-detail"><code>PDA.NEW</code></a></td><td data-label="Type">Full</td></tr></tbody></table>')
  assert req.url.host=='fota-cloud-dn.ospserver.net'
  return response('<versioninfo><latest>PDA.NEW/CSC.NEW/PHONE.NEW</latest><value>PDA.OLD/CSC.OLD/PHONE.OLD</value></versioninfo>','text/xml')
 device={'vendor':'samsung','upstream_id':'sample','page':'https://fwatlas.com/samsung/sample'}
 with httpx.Client(transport=httpx.MockTransport(handler)) as client:
  _,rows,_=collect_model(device,None,MetadataHTTP(client))
 fota=[r for r in rows if r.get('metadata_source')=='samsung_fota_version_xml']
 assert len(fota)==1 and fota[0]['version'].startswith('PDA.OLD/')
 assert fota[0]['resolution']=='requires_samsung_fus' and 'detail_page' not in fota[0]
 assert len(seen)==2


def test_legacy_controller_does_not_send_fota_only_references_to_web_resolver(database):
 from fmr_ingest_stock.catalogue_job import save,step,task_key
 db,_=database
 control={'run':'fota_only','mode':'audit','generation':0,'summary':{'samsung':{'models':1,'release_records':1,'resolved_downloads':0,'published':0,'errors':{}}}}
 device={'vendor':'samsung','upstream_id':'sample','name':'Sample'}
 save(db,task_key('fota_only','release','ref'),{'device':device,'done':False,'release':{'vendor':'samsung','version':'PDA/CSC/PHONE','resolution':'requires_samsung_fus'}})
 with httpx.Client(transport=httpx.MockTransport(lambda req: (_ for _ in ()).throw(AssertionError('Invented URL requested')))) as client:
  assert step(db,control,MetadataHTTP(client)) is False
 assert control['summary']['samsung']['errors']=={'requires_samsung_fus':1}


def test_attested_download_gate_is_reported_without_attempting_device_impersonation():
 from fmr_ingest_stock.oem_sources import resolve_oplus_gate,DeviceAttestationRequired
 with httpx.Client(transport=httpx.MockTransport(lambda req: (_ for _ in ()).throw(AssertionError('Attested gate requested')))) as client:
  with pytest.raises(DeviceAttestationRequired):resolve_oplus_gate('realme','https://component-ota-sg.allawnos.com/download?g=public',MetadataHTTP(client))


def test_google_keeps_firmware_target_distinct_from_quoted_hardware_codename():
 sha='a'*64
 html='<h2 id="takju">Galaxy Nexus "maguro" (GSM/HSPA+) (with Google Wallet)</h2><table><tr><td>4.3 (JWR66Y)</td><td><a href="https://dl.google.com/android/aosp/takju.zip">Link</a></td><td>'+sha+'</td></tr></table>'
 with httpx.Client(transport=httpx.MockTransport(lambda request:response(html))) as client:
  devices,rows,errors=google_catalogue(MetadataHTTP(client))
 assert devices[0]['upstream_id']=='takju' and devices[0]['codename']=='takju'
 assert devices[0]['models']=='maguro'
 assert rows['takju'] and not errors
