"""Guard production workflow topology and trust boundaries."""
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / '.github/workflows'
VENDORS = {'asus', 'google', 'honor', 'huawei', 'motorola', 'nothing', 'oneplus',
           'oppo', 'realme', 'samsung', 'sony', 'vivo', 'xiaomi'}


def load(name):
    return yaml.safe_load((WORKFLOWS / name).read_text())


def test_only_supported_entry_points_are_active():
    expected = {f'catalogue_{v}.yml' for v in VENDORS} | {
        'catalogue_publish.yml', 'telegram_fetch.yml', 'telegram_clean.yml', 'tg_cache_probe.yml', 'ci.yml', 'services.yml'}
    assert {p.name for p in WORKFLOWS.glob('*.yml')} == expected
    names = [load(name)['name'] for name in expected]
    assert len(names) == len(set(names))


def test_production_jobs_require_main_and_never_use_untrusted_pr_target():
    for path in WORKFLOWS.glob('*.yml'):
        workflow = load(path.name)
        for name, job in workflow['jobs'].items():
            if job.get('environment') != 'production':
                continue
            assert "github.ref == 'refs/heads/main'" in job.get('if', ''), (path.name, name)
            assert 'pull_request_target' not in workflow['on']


def test_tg_has_four_local_collectors_one_cleaner_and_persistent_local_state():
    w=load('telegram_fetch.yml');job=w['jobs']['fetch']
    assert job['strategy']=={'fail-fast':False,'max-parallel':4,'matrix':{'worker':[0,1,2,3]}}
    assert 'schedule' in w['on']
    stages=[job,load('telegram_clean.yml')['jobs']['clean'],load('catalogue_publish.yml')['jobs']['telegram']]
    for stage in stages:
        assert stage['runs-on']==['self-hosted','findmyrom-metadata']
        assert 'FMR_TG_STATE_DIR' in stage['env']
        assert not any(step.get('uses','').startswith('actions/setup-python') for step in stage['steps'])
        assert 'findmyrom-tg01-test' not in str(stage['steps'])
        assert 'publish_private' not in str(stage['steps'])
    assert job['concurrency']['group']=='tg-local-worker-${{ matrix.worker }}'
    assert load('telegram_clean.yml')['jobs']['clean']['concurrency']['group']=='tg-local-cleaner'
    initial=w['jobs']['initialize']['steps']
    checkout=next(step for step in initial if step.get('with',{}).get('repository')=='TunJinshou050608/findmyrom-tg01-test')
    assert checkout['if']=="steps.local.outputs.needed == 'true'"
    assert checkout['with']['persist-credentials'] is False


def test_cleaning_cannot_access_d1_and_only_unified_publication_imports_resources():
    for name in ['telegram_fetch.yml','telegram_clean.yml']:
        text=(WORKFLOWS/name).read_text()
        assert 'CF_API_TOKEN' not in text
        assert 'local_runtime publish' not in text
    pub=load('catalogue_publish.yml')
    assert 'local_runtime publish' in str(pub['jobs']['telegram']['steps'])
    assert 'scope' in pub['on']['workflow_dispatch']['inputs']
    assert 'FMR_TG_D1_WRITES_PAUSED' in pub['jobs']['telegram']['if']
    for path in WORKFLOWS.glob('*.yml'):
        if path.name!='catalogue_publish.yml':
            assert 'fmr_ingest_stock.catalogue_publish_monitor' not in path.read_text()


def test_ci_has_no_production_repair_or_deployment_side_effects():
    assert set(load('ci.yml')['jobs']) == {'verify'}
    assert 'secrets.' not in (WORKFLOWS / 'ci.yml').read_text()


def test_operational_service_cadences_and_locks_are_preserved():
    w = load('services.yml')
    assert {s['cron'] for s in w['on']['schedule']} == {'*/15 * * * *', '43 */6 * * *'}
    assert set(w['jobs']) == {'sony_bundle', 'public_index', 'check'}
    assert w['jobs']['sony_bundle']['runs-on'] == 'ubuntu-latest'
    assert w['jobs']['sony_bundle']['concurrency']['group'] == 'sony-complete-bundle-cache'


def test_every_vendor_and_stock_publisher_require_a_fresh_snapshot_before_upload():
    for vendor in VENDORS | {'publish'}:
        workflow=load('catalogue_'+vendor+'.yml')
        key='publish' if vendor=='publish' else 'catalogue'
        job=workflow['jobs'][key];steps=job['steps']
        checkout=next(s for s in steps if s.get('uses')=='actions/checkout@v4')
        assert checkout['with']['path']==job['defaults']['run']['working-directory']
        check=next(i for i,s in enumerate(steps) if s.get('id')=='export_check')
        upload=next(i for i,s in enumerate(steps) if s.get('id')=='snapshot')
        assert check<upload
        assert "steps.export_check.outcome == 'success'" in steps[upload]['if']
        if vendor!='publish':
            maintain=next(i for i,s in enumerate(steps) if 'maintain-cache --vendor '+vendor in s.get('run',''))
            collect=next(i for i,s in enumerate(steps) if s.get('id')=='collect')
            assert maintain<collect<check
    assert load('catalogue_samsung.yml')['jobs']['catalogue']['if']=="github.ref == 'refs/heads/main'"
    for vendor in ('huawei','honor'):
        assert load('catalogue_'+vendor+'.yml')['jobs']['catalogue']['if']=="github.ref == 'refs/heads/main' && false"
