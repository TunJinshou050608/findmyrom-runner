"""Keep production credentials off non-main workflow jobs."""
from pathlib import Path
import yaml


def test_production_jobs_require_main_and_never_use_untrusted_pr_target():
    root = Path(__file__).resolve().parents[1] / '.github/workflows'
    production = 0
    for path in root.glob('*.yml'):
        workflow = yaml.safe_load(path.read_text())
        triggers = workflow.get('on', workflow.get(True, {})) or {}
        for name, job in workflow.get('jobs', {}).items():
            if job.get('environment') != 'production':
                continue
            production += 1
            assert "github.ref == 'refs/heads/main'" in str(job.get('if', '')), (path.name, name)
            assert 'pull_request_target' not in triggers, path.name
    assert production >= 10


def test_tg_resident_and_lab_receive_llm_secret():
    root = Path(__file__).resolve().parents[1] / '.github/workflows'
    for name in ('telegram_trial.yml', 'telegram_search_lab.yml'):
        workflow = yaml.safe_load((root / name).read_text())
        steps = [step for job in workflow['jobs'].values() for step in job['steps']]
        configured = [step for step in steps if step.get('env', {}).get('LLM_API_KEY') == '${{ secrets.DEEPSEEK_API_KEY }}']
        assert len(configured) == 1
        assert configured[0]['env']['LLM_MODEL_NAME'] == 'deepseek-flash'


def test_tg_retrieval_is_only_scheduled_on_local_metadata_runners():
    root=Path(__file__).resolve().parents[1]/'.github/workflows'
    for name in ('telegram_trial.yml','telegram_scan.yml','telegram_search_lab.yml'):
        workflow=yaml.safe_load((root/name).read_text())
        for job in workflow['jobs'].values():
            assert job['runs-on']==['self-hosted','findmyrom-metadata']
    for name in ('telegram_trial.yml','telegram_search_lab.yml'):
        workflow=yaml.safe_load((root/name).read_text())
        assert 'FMR_CATALOGUE_STATE_DIR' in next(iter(workflow['jobs'].values()))['env']
    workflow=yaml.safe_load((root/'telegram_search_lab.yml').read_text())
    triggers=workflow.get('on',workflow.get(True))
    assert 'targets' not in triggers['workflow_dispatch']['inputs']
    assert 'runner' not in triggers['workflow_dispatch']['inputs']
