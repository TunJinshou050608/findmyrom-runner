import httpx
from fmr_ingest_stock import catalogue_diagnostics as diagnostics
from fmr_ingest_stock.catalogue_store import Store
from fmr_ingest_stock.staged_catalogue import open_local


def test_paused_diagnostics_do_not_request_database_backed_download_handlers(tmp_path,monkeypatch):
    monkeypatch.setenv('FMR_CATALOGUE_STATE_DIR',str(tmp_path))
    monkeypatch.setenv('FMR_D1_WRITES_PAUSED','true')
    db=open_local(tmp_path/'oneplus.sqlite')
    try:
        store=Store(db,'oneplus')
        store.put(store.key('release','sample'),{
            'status':'resolved','device':{'vendor':'oneplus'},
            'release':{'kind':'Full OTA','version':'A.1',
                'download_url':'https://findmyrom.com/api/refresh/sample',
                'refresh_source':{'vendor':'oneplus'}}})
    finally:db.close()
    monkeypatch.setattr(diagnostics,'durable_download',lambda *a:True)
    real=httpx.Client
    def forbidden(request):raise AssertionError('Paused diagnostic made a network request')
    monkeypatch.setattr(diagnostics.httpx,'Client',lambda *a,**kw:
        real(*a,**kw,transport=httpx.MockTransport(forbidden)))
    result=diagnostics.inspect('oneplus')
    assert result['download_smoke']==[
        {'kind':'Full OTA','refresh':True,'state':'not_probed_d1_paused'}]
