from copy import deepcopy
import hashlib
import pytest
from fmr_tg_scanner.grounded_cleaner import verify,convert,extract_all,DeferredAudit
from fmr_tg_scanner.search_store import SearchStore
TEXT='Hydrogen Kernel 4.14.336 for alioth\nBy darkphnx & Ziad Fekry\nDownload https://example.org/alioth.zip'
def output():
 return dict(is_release=True,resource_kind='kernel',project_name=dict(value='Hydrogen Kernel',evidence_quote='Hydrogen Kernel 4.14.336'),project_version=dict(value='4.14.336',evidence_quote='4.14.336'),android_version=dict(value=None,evidence_quote=None),system_base=dict(value='unknown',evidence_quote=None),release_stage=dict(value='unknown',evidence_quote=None),maintainer=dict(handle=None,display_name='darkphnx & Ziad Fekry',evidence_quote='By darkphnx & Ziad Fekry'),target_devices=dict(codenames=['alioth'],marketing_names=[],evidence_quote='alioth'),build_variants=[])
def message():return dict(text=TEXT,channel_username='testupdates',msg_id=1,tg_post_url='https://t.me/testupdates/1',links=['https://example.org/alioth.zip'],link_items=[],published_timestamp=1234567890)
@pytest.mark.parametrize('field',['project_name','project_version','android_version','system_base','release_stage'])
def test_values_must_match_quotes(field):
 o=output();o[field]=dict(value='invented',evidence_quote='alioth')
 with pytest.raises(ValueError):verify(o,TEXT)
def test_every_device_and_both_author_fields_verified():
 for change in ('device','handle','quote'):
  o=output()
  if change=='device':o['target_devices']['codenames'].append('ishtar')
  elif change=='handle':o['maintainer']['handle']='fake_person'
  else:o['maintainer']['evidence_quote']=None
  with pytest.raises(ValueError):verify(o,TEXT)
def test_non_boolean_and_non_evidenced_variant_fail():
 o=output();o['is_release']='true'
 with pytest.raises(ValueError):verify(o,TEXT)
 o=output();o['build_variants']=['gapps']
 with pytest.raises(ValueError):verify(o,TEXT)
def test_kernel_version_and_literal_multi_author_survive():
 msg=message();old=dict(device_codename='alioth',raw_text_hash=hashlib.sha256(TEXT.encode()).hexdigest(),tg_post_url=msg['tg_post_url'],channel_username=msg['channel_username'],msg_id=1)
 r,_=convert(old,msg,'alioth',{'alioth':['POCO F3']},output());p=r['package']
 assert p['build_version_tag']=='4.14.336' and p['taxonomy']['maintenance']['branch_id']
 assert p['taxonomy']['maintenance']['identity_verified'] is False
 assert p['maintainer_handle'] is None
 assert old.get('taxonomy') is None

def test_missing_key_defers_without_creating_qualified_cohort(tmp_path,monkeypatch):
 monkeypatch.delenv('LLM_API_KEY',raising=False)
 store=SearchStore(tmp_path/'test.sqlite')
 try:
  with pytest.raises(DeferredAudit):extract_all([('alioth',message())],{'alioth':['POCO F3']},store)
 finally:store.close()

def test_bounded_async_calls_resume_from_cache_and_report_usage(tmp_path,monkeypatch):
 import httpx
 from fmr_tg_scanner import grounded_cleaner as module
 monkeypatch.setenv('LLM_API_KEY','fixture-only');monkeypatch.setenv('FMR_TG_CLEAN_MAX_CALLS','1')
 original=httpx.AsyncClient;calls=[]
 def handler(request):
  import json
  body=json.loads(request.content);calls.append(body)
  assert body['thinking']=={'type':'disabled'} and 'extra_body' not in body
  assert body['messages'][0]['content']==module.PROMPT
  return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(output())}}],'usage':{'prompt_cache_hit_tokens':100,'prompt_cache_miss_tokens':20,'completion_tokens':50}})
 monkeypatch.setattr(module.httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(handler),**kw))
 first=message();second=message();second['msg_id']=2;second['text']=TEXT+'\nNew release'
 store=SearchStore(tmp_path/'cache.sqlite')
 try:
  with pytest.raises(DeferredAudit):extract_all([('alioth',first),('alioth',second)],{'alioth':['POCO F3']},store)
  result,stats=extract_all([('alioth',first),('alioth',second)],{'alioth':['POCO F3']},store)
  assert len(result)==2 and len(calls)==2 and stats['local_cache_hits']==1
  assert stats['prompt_cache_hit_tokens']==100
 finally:store.close()
