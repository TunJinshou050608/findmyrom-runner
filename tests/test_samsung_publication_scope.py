"""Check Samsung publication stays inside the shared writer and ten-minute window."""
from fmr_ingest_stock import catalogue_publish as module

def test_samsung_only_publication_never_opens_another_vendor_or_remote_d1(tmp_path,monkeypatch):
    calls=[]
    class Artifacts:
        def restore(self,scope,path,**kwargs):
            calls.append(scope)
            return None
        def close(self):pass
    monkeypatch.setenv('FMR_D1_WRITES_PAUSED','false')
    monkeypatch.setenv('FMR_CATALOGUE_PUBLISH_VENDORS','samsung')
    monkeypatch.setattr(module,'ROOT',tmp_path)
    monkeypatch.setattr(module,'Artifacts',Artifacts)
    monkeypatch.setattr(module,'encrypt_file',lambda *a:None)
    monkeypatch.setattr(module.time,'time',lambda:600000)
    def forbidden(**kwargs):raise AssertionError('Empty local queue must not open D1')
    monkeypatch.setattr(module,'BudgetDB',forbidden)
    assert module.main()==0
    assert calls==['publisher','samsung']
    assert module.main()==0
    assert calls==['publisher','samsung','publisher']
    db=module.open_ledger(tmp_path/'publisher.sqlite')
    try:
        assert db.query("SELECT value FROM control WHERE key='slot:samsung'")==[{'value':'1000'}]
        assert not db.query("SELECT value FROM control WHERE key='slot'")
    finally:db.close()
