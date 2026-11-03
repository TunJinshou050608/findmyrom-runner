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
        'catalogue_publish.yml', 'telegram_fetch.yml', 'telegram_clean.yml', 'ci.yml', 'services.yml'}
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


def test_tg_fetch_stays_paused_and_automatically_reads_device_catalogue():
    w = load('telegram_fetch.yml')
    assert set(w['on']) == {'workflow_dispatch'}
    job = w['jobs']['fetch']
    assert "vars.FMR_TG_FETCH_ENABLED == 'true'" in job['if']
    assert 'FMR_CATALOGUE_STATE_DIR' in job['env']
    assert 'from fmr_tg_scanner.search_watch import produce' in str(job['steps'])
    assert "produce(Path('../state').resolve())" in str(job['steps'])
    assert 'search_lab' not in str(job['steps'])
    llm = [step for step in job['steps'] if 'LLM_API_KEY' in step.get('env', {})]
    assert len(llm) == 1
    assert llm[0]['env']['LLM_API_KEY'] == '${{ secrets.DEEPSEEK_API_KEY }}'


def test_tg_state_is_serialized_in_local_container_across_all_three_stages():
    stages = [(load('telegram_fetch.yml'), 'fetch'), (load('telegram_clean.yml'), 'clean'),
              (load('catalogue_publish.yml'), 'telegram')]
    for w, key in stages:
        job = w['jobs'][key]
        assert job['runs-on'] == ['self-hosted', 'findmyrom-metadata']
        assert job.get('concurrency', w.get('concurrency')) == {
            'group': 'tg01-annibale-private-trial', 'cancel-in-progress': False}
        assert not any(step.get('uses', '').startswith('actions/setup-python') for step in job['steps'])


def test_cleaning_cannot_publish_and_catalogue_import_has_one_entry_point():
    for name in ['telegram_fetch.yml', 'telegram_clean.yml']:
        text = (WORKFLOWS / name).read_text()
        assert 'CF_API_TOKEN' not in text
        assert '--publish' not in text
    clean, pub = load('telegram_clean.yml'), load('catalogue_publish.yml')
    assert clean['on']['workflow_run']['workflows'] == [load('telegram_fetch.yml')['name']]
    assert pub['on']['workflow_run']['workflows'] == [clean['name']]
    for name in ['telegram_clean.yml', 'catalogue_publish.yml']:
        for job in load(name)['jobs'].values():
            if 'conclusion' in job['if']:
                assert "conclusion == 'success'" in job['if']
                assert 'head_repository.full_name == github.repository' in job['if']
    for path in WORKFLOWS.glob('*.yml'):
        if path.name != 'catalogue_publish.yml':
            assert 'fmr_tg_scanner.pipeline' not in path.read_text()
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
