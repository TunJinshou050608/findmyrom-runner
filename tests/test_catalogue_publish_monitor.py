from fmr_ingest_stock.catalogue_publish_monitor import run


def test_ten_minute_windows_never_busy_retry_and_continue_after_network_failure():
    clock=[605.0];calls=[];sleeps=[]
    def sleep(seconds):
        assert 0<seconds<=30
        sleeps.append(seconds);clock[0]+=seconds
    def publish():
        calls.append(clock[0])
        if len(calls)==1:raise ConnectionError('network interrupted')
    assert run(duration=1300,clock=lambda:clock[0],sleep=sleep,publish=publish)==1
    assert calls==[605,1200,1800]
    assert clock[0]==1905 and sleeps


def test_slow_publish_skips_missed_windows_instead_of_replaying_backlog():
    clock=[605.0];calls=[]
    def publish():calls.append(clock[0]);clock[0]+=1300
    run(duration=2000,clock=lambda:clock[0],sleep=lambda seconds:clock.__setitem__(0,clock[0]+seconds),publish=publish)
    assert calls==[605,2400]



def test_single_window_reports_batch_failure(monkeypatch):
    monkeypatch.setenv('FMR_CATALOGUE_PUBLISH_ONCE', 'true')
    assert run(publish=lambda: 1) == 1


def test_once_mode_runs_one_publish_window(monkeypatch):
    monkeypatch.setenv('FMR_CATALOGUE_PUBLISH_ONCE','true')
    calls=[]
    assert run(duration=3000, clock=lambda:0, sleep=lambda seconds:None, publish=lambda:calls.append(1))==0
    assert calls==[1]

