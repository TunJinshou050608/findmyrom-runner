import json
import pytest
from fmr_tg_scanner.taxonomy import classify, access_claim, project_identity, VERSION
from fmr_tg_scanner.search_policy import analyse
from fmr_tg_scanner.search_store import SearchStore


def tags(text, **kwargs):
    package={'device_codename':'alioth','resource_kind':'rom','project_name':'DerpFest',
             'android_version':'15','maintainer_handle':None}
    package.update(kwargs.pop('package',{}))
    return classify({'text':text},package,accepted=True,**kwargs)


@pytest.mark.parametrize('text,status',[
    ('Download: https://example.org/rom.zip','free'),
    ('Donations welcome. Buy me a coffee.','free'),
    ('Free download. Optional donations welcome.','free'),
    ('免费使用，接受自愿捐赠','free'),
    ('无需付费','free'),('不收费','free'),
    ('Free download demo. Paid build requires payment.','paid'),
    ('付费下载，捐赠后获取密码','paid'),
    ('Not a free ROM','paid'),('Free trial','paid'),
    ('免费试用，完整版收费','paid'),
])
def test_access_evidence_and_publication_gate(text,status):
    result=tags(text)
    assert result['access']==status
    assert result['publication']['eligible']==(status=='free')
    quote=result['evidence']['access']['evidence']
    assert quote is None or quote in text or result['evidence']['access']['source']=='user_policy'


def test_author_labels_and_companion_files_are_distinct():
    result=tags('DerpFest UNOFFICIAL | Android 15 | Beta | Vanilla + GApps\nFree download\nFlash stable TWRP recovery first')
    assert result['resource_type']=='rom'
    assert result['system_base']=='AOSP' and result['rom_family']=='aosp_like'
    assert result['project_recognition']=='unofficial'
    assert result['release_stage']=='beta'
    assert result['build_variants']==['vanilla','gapps']


def test_same_project_name_aliases_have_one_id():
    assert project_identity('EvoX')['id']==project_identity('Evolution X')['id']
    assert project_identity('crDroid')['id']==project_identity('crdroid')['id']
    assert project_identity('Custom ROM')['id'] is None


@pytest.mark.parametrize('heading,family,base',[
    ('HyperOS 3 PORT for alioth','manufacturer_port','HyperOS'),
    ('MIUI Modified for alioth','manufacturer_modified','MIUI'),
    ('OxygenOS Port for alioth','manufacturer_port','OxygenOS'),
    ('UnknownProject Official','pending','unknown'),
])
def test_family_is_separate_from_project_identity(heading,family,base):
    result=tags(heading,package={'project_name':'UnknownProject'})
    assert result['rom_family']==family and result['system_base']==base
    assert result['project']['name']=='UnknownProject'


def test_versions_dates_and_branches_preserve_build_identity():
    text='DerpFest Unofficial Android 15\nVersion: v2.5\nBuild ID: ABC.123\nBuild date: 2026-11-02\nBranch: experimental\nBy @alice_dev\nFree download'
    a=tags(text,package={'maintainer_handle':'@alice_dev'})
    b=tags(text.replace('@alice_dev','@bob_dev'),package={'maintainer_handle':'@bob_dev'})
    assert a['versions']=={'android':'15','project_version':'2.5','build_id':'ABC.123','build_date':'2026-11-02'}
    assert a['maintenance']['branch_id']!=b['maintenance']['branch_id']
    assert a['maintenance']['identity_verified'] is False
    assert tags('DerpFest\nUpdated: 04/05/23')['versions']['build_date'] is None
    assert tags('DerpFest',package={'maintainer_handle':'@channel_admin'})['maintenance']['maintainer_id'] is None


def test_gsi_and_excluded_models_remain_review_only():
    result=tags('DerpFest GSI | Android 15\nARM64 A/B Treble\nFree download\nNot for POCO F3',device_names=['POCO F3'])
    assert result['package_form']=='gsi'
    assert result['devices']['excluded_models']==['POCO F3']
    assert result['devices']['applicable_models']==[]
    assert result['devices']['compatibility_conditions']
    assert not result['publication']['eligible']


def test_site_device_id_is_distinct_from_actual_codename():
    result=tags('DerpFest for Google Pixel 7',package={'device_codename':'google-panther'},device_names=['Google Pixel 7','panther'])
    assert result['devices']['device_id']=='google-panther'
    assert result['devices']['codename']=='panther'
    assert result['package_form']=='device_specific'


def test_model_cannot_invent_free_access_or_override_paid_gate():
    output={'accepted':True,'taxonomy':{'access':{'value':'free','evidence':'Download link'}}}
    assert tags('Download link',llm_output=output)['evidence']['access']['source']=='user_policy'
    output['taxonomy']['access']['evidence']='Free download'
    assert tags('Free download. Paid build.',llm_output=output)['access']=='paid'
    output['taxonomy']['rom_family']={'value':'manufacturer_port','evidence':'this quote is absent'}
    assert tags('NovelProject',package={'project_name':'NovelProject'},llm_output=output)['rom_family']=='pending'
    output['taxonomy']['access']={'value':['free'],'evidence':'Free download'}
    assert tags('Public download',llm_output=output)['access']=='free'


def test_taxonomy_is_persisted_with_candidate_and_product(tmp_path):
    db=SearchStore(tmp_path/'search.sqlite')
    message={'text':'#alioth DerpFest unofficial Android 15\nFree download',
             'channel_username':'testchannel','msg_id':1,'tg_post_url':'https://t.me/testchannel/1','links':[]}
    result,reason=analyse(message,'alioth',[],{'alioth':[]})
    assert reason=='accepted'
    db.save(result['package'],result['evidence'],result['classification'],reason,True)
    payload=json.loads(db.query('SELECT payload FROM tg_search_records')[0]['payload'])
    assert payload['taxonomy']['schema_version']==VERSION and payload['taxonomy']['publication']['eligible']
    db.close()


@pytest.mark.parametrize('package,text',[
    ({'channel_username':'EliteDevelopment'},'Free download'),
    ({'channel_username':'regularupdates','original_channel_username':'EliteROMs'},'Download'),
    ({'original_post_url':'https://t.me/EliteDevelopment/123'},'Download'),
    ({},'EliteRomLite 23.8.28 for alioth'),
    ({},'Download: https://www.elitedevelopment.com.pk/rom.zip'),
    ({},'Public Releases Channel: @EliteDevelopment'),
])
def test_elite_origin_and_reposts_never_enter_default_free_publication(package,text):
    result=tags(text,package=package)
    assert result['access']=='unknown'
    assert not result['publication']['eligible']
    assert 'excluded_elite_source' in result['publication']['blockers']
    from fmr_tg_scanner.taxonomy import apply_access_policy
    assert apply_access_policy(result,package=package)==result


def test_generic_elite_word_does_not_exclude_unrelated_resources():
    result=tags('Project Radiant Unofficial Android 11\nBy Elite @darkphnx',package={'channel_username':'pocof3globalupdates'})
    assert result['access']=='free' and result['publication']['eligible']


def test_existing_classification_access_is_retagged_idempotently():
    from fmr_tg_scanner.taxonomy import apply_access_policy
    old={'schema_version':'release-taxonomy-v1','access':'unknown','release_stage':'beta',
         'publication':{'eligible':False,'blockers':['access_unknown']}}
    new=apply_access_policy(old,package={'channel_username':'aresupdates'})
    assert new['access']=='free' and new['publication']['eligible']
    assert new['release_stage']=='beta' and old['access']=='unknown'
    assert apply_access_policy(new,package={'channel_username':'aresupdates'})==new
    old['publication']['blockers'].append('gsi_compatibility_requires_review')
    assert not apply_access_policy(old)['publication']['eligible']
    assert apply_access_policy({})=={}
