"""No network, credentials, service restarts or real installations."""
import fcntl
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
from unittest.mock import patch
import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('extractor_auto_update', ROOT/'deploy/extractor/auto_update.py')
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)
A = 'a'*40
B = 'b'*40
SCRIPT = b'#!/bin/bash\nset -euo pipefail\ntrue\n'


def source(commit=A, script=SCRIPT, etag='"new"'):
    calls=[]
    def get(url, previous=None):
        calls.append((url,previous))
        if url==updater.API:return json.dumps([{'sha':commit}]).encode(),etag
        assert f'/{commit}/{updater.SCRIPT}' in url
        return script,None
    return get,calls


def test_apply_once_pins_revision_and_skips_unchanged_script(tmp_path):
    get,calls=source();runs=[]
    def run(path,commit):runs.append((path.read_bytes(),commit));assert not (tmp_path/'applied.json').exists()
    assert updater.check(tmp_path,get,run)=='applied'
    assert runs==[(SCRIPT,A)]
    assert updater.check(tmp_path,get,run)=='unchanged'
    assert len(calls)==3 and len(runs)==1
    assert calls[-1][1]=='"new"'
    state=json.loads((tmp_path/'applied.json').read_text())
    assert state=={'commit':A,'etag':'"new"','sha256':hashlib.sha256(SCRIPT).hexdigest()}


def test_conditional_304_does_not_download_or_execute(tmp_path):
    get,_=source();updater.check(tmp_path,get,lambda *_:None)
    def unchanged(url,etag):assert url==updater.API and etag=='"new"';return None,etag
    assert updater.check(tmp_path,unchanged,lambda *_:pytest.fail('Unexpected execution'))=='unchanged'


def test_failed_update_keeps_old_state_and_retries_next_check(tmp_path):
    get,_=source();updater.check(tmp_path,get,lambda *_:None)
    previous=(tmp_path/'applied.json').read_bytes();get,_=source(B,SCRIPT+b'#changed\n','"next"');runs=[]
    def failure(path,commit):runs.append(commit);raise RuntimeError('Health check failed')
    for _ in range(2):
        with pytest.raises(RuntimeError):updater.check(tmp_path,get,failure)
        assert (tmp_path/'applied.json').read_bytes()==previous
    assert runs==[B,B]


def test_identical_content_at_new_commit_does_not_restart(tmp_path):
    get,_=source();updater.check(tmp_path,get,lambda *_:None)
    get,_=source(B)
    assert updater.check(tmp_path,get,lambda *_:pytest.fail('Unexpected execution'))=='unchanged'
    assert json.loads((tmp_path/'applied.json').read_text())['commit']==B


@pytest.mark.parametrize('commit',['main','../../escape','A'*40,None])
def test_invalid_revision_never_downloads_or_executes(tmp_path,commit):
    def get(url,_etag=None):assert url==updater.API;return json.dumps([{'sha':commit}]).encode(),None
    with pytest.raises(ValueError):updater.check(tmp_path,get,lambda *_:pytest.fail('Unexpected execution'))
    assert not (tmp_path/'applied.json').exists()


def test_syntax_failure_and_network_error_leave_installation_unrecorded(tmp_path):
    get,_=source(script=b'#!/bin/bash\nif then\n')
    with pytest.raises(subprocess.CalledProcessError):updater.check(tmp_path,get,lambda *_:pytest.fail('Executed bad script'))
    def offline(*_args):raise TimeoutError('Network unavailable')
    with pytest.raises(TimeoutError):updater.check(tmp_path,offline,lambda *_:pytest.fail('Unexpected execution'))
    assert not (tmp_path/'applied.json').exists()


def test_overlapping_checks_are_skipped(tmp_path):
    with (tmp_path/'check.lock').open('a') as handle:
        fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        assert updater.check(tmp_path,lambda *_:pytest.fail('Unexpected network'),lambda *_:pytest.fail('Unexpected execution'))=='locked'


def test_executor_does_not_inherit_proxy_or_secret_environment(monkeypatch,tmp_path):
    monkeypatch.setenv('HTTPS_PROXY','http://private-proxy.invalid')
    monkeypatch.setenv('EXTRACTOR_HMAC_SECRET','must-never-be-inherited')
    with patch.object(updater.subprocess,'Popen') as process:
        process.return_value.wait.return_value=0
        updater.execute(tmp_path/'update.sh',A)
        args,kwargs=process.call_args
        assert args[0]==['/bin/bash',str(tmp_path/'update.sh')]
        assert kwargs['env']=={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','FMR_UPDATE_REF':A}
        assert kwargs['start_new_session'] is True


def test_network_is_bounded_and_environment_proxies_disabled():
    with patch.object(updater,'build_opener') as factory:
        factory.return_value.open.return_value.__enter__.return_value.read.return_value=bytes(131073)
        with pytest.raises(ValueError):updater.fetch(updater.API)
        assert factory.call_args.args[0].proxies=={}
    with pytest.raises(ValueError):updater.NoRedirect().redirect_request(None,None,None,None,None,None)


def test_published_updater_integrity_pins_match_exact_modules():
    for script in ['update-preparation.sh','update-xiaomi.sh']:
        text=(ROOT/'deploy/extractor'/script).read_text()
        assert 'FMR_UPDATE_REF' in text and '$update_ref/fmr_extractor' in text
        assert 'http://127.0.0.1:18765/v1/capabilities' in text
        for name in ['service.py','streaming_probe.py','prepared.py']:
            assert hashlib.sha256((ROOT/'fmr_extractor'/name).read_bytes()).hexdigest() in text


@pytest.mark.parametrize('healthy',[True,False])
def test_real_update_script_stages_and_rolls_back_in_isolated_fake_installation(tmp_path,healthy):
    import os
    import shutil
    import sys
    installation=tmp_path/'installation'
    app=installation/'app'
    shutil.copytree(ROOT/'fmr_extractor',app/'fmr_extractor',ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copytree(ROOT/'fmr_ingest_stock',app/'fmr_ingest_stock',ignore=shutil.ignore_patterns('__pycache__'))
    service=app/'fmr_extractor/service.py'
    previous=service.read_bytes()+b'\n# previously installed release\n'
    service.write_bytes(previous)
    (installation/'venv/bin').mkdir(parents=True)
    (installation/'venv/bin/python').write_text('#!/bin/bash\nexec '+__import__('shlex').quote(sys.executable)+' "$@"\n')
    os.chmod(installation/'venv/bin/python',0o700)
    binaries=tmp_path/'bin';binaries.mkdir()
    commands={
      'id': '#!/bin/sh\necho 0\n',
      'flock': '#!/bin/sh\nexit 0\n',
      'sleep': '#!/bin/sh\nexit 0\n',
      'curl': '#!'+sys.executable+'''\nimport sys,os,json,shutil
from pathlib import Path
args=sys.argv[1:];target=Path(args[args.index('--output')+1])
urls=[arg for arg in args if arg.startswith(('http://','https://'))];assert len(urls)==1
url=urls[0]
if url.startswith('http://127.0.0.1:18765/'):
 target.write_text(json.dumps({'prepared_downloads':os.environ['FMR_TEST_HEALTH']=='1','samsung_full_stream':os.environ['FMR_TEST_HEALTH']=='1'}))
else:
 assert '/'+os.environ['FMR_UPDATE_REF']+'/fmr_extractor/' in url
 name=url.rsplit('/',1)[1];assert name in {'service.py','streaming_probe.py','prepared.py'}
 shutil.copyfile(Path(os.environ['FMR_TEST_SOURCE'])/name,target)
''',
      'systemctl': '#!'+sys.executable+'''\nimport os,sys
with open(os.environ['FMR_TEST_LOG'],'a') as handle:handle.write(' '.join(sys.argv[1:])+'\\n')
''',
      'sha256sum': '#!'+sys.executable+'''\nimport hashlib,sys
from pathlib import Path
for line in sys.stdin:
 digest,path=line.strip().split(None,1)
 if hashlib.sha256(Path(path).read_bytes()).hexdigest()!=digest:sys.exit(1)
''',
    }
    for name,body in commands.items():
        path=binaries/name;path.write_text(body);os.chmod(path,0o700)
    script=tmp_path/'update.sh'
    script.write_text((ROOT/'deploy/extractor/update-preparation.sh').read_text().replace('/opt/fmr-extractor',str(installation)))
    log=tmp_path/'calls.log'
    env={**os.environ,'PATH':str(binaries)+os.pathsep+os.environ['PATH'],'FMR_TEST_SOURCE':str(ROOT/'fmr_extractor'),'FMR_UPDATE_REF':A,'FMR_TEST_LOG':str(log),'FMR_TEST_HEALTH':'1' if healthy else '0'}
    result=subprocess.run(['/bin/bash',str(script)],env=env,capture_output=True,text=True,timeout=20)
    if healthy:
        assert result.returncode==0,result.stderr
        assert service.read_bytes()==(ROOT/'fmr_extractor/service.py').read_bytes()
        assert log.read_text().count('restart fmr-extractor.service')==1
    else:
        assert result.returncode!=0
        assert service.read_bytes()==previous
        assert log.read_text().count('restart fmr-extractor.service')==2
    assert not list(tmp_path.glob('**/agent.env'))
