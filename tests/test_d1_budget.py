import json
from unittest.mock import Mock
import pytest
from fmr_ingest_stock.d1_budget import BudgetDB,D1BudgetReached
from fmr_ingest_stock.catalogue_store import Store
from fmr_core.d1_client import D1Client
from pathlib import Path


def test_daily_allocations_stop_without_network_and_reset_per_utc_day(tmp_path):
 db=BudgetDB(local_sqlite_path=str(tmp_path/'budget.sqlite'))
 try:
  assert all(db.acquire() for _ in range(20))
  assert not db.acquire()
  row=db.query('SELECT * FROM fmr_catalogue_budget')[0]
  assert row['reads_reserved']==2_000_000 and row['writes_reserved']==40_000
 finally:db.close()


def test_response_metadata_meter_stops_next_request_and_allows_final_checkpoint(monkeypatch):
 db=BudgetDB(cf_account_id='a',cf_api_token='test',cf_database_id='d')
 response=Mock();response.json.return_value={'success':True,'result':[{'success':True,'results':[], 'meta':{'rows_read':90000,'rows_written':1800}}]}
 post=Mock(return_value=response);monkeypatch.setattr(db.session,'post',post)
 db.metering=True
 try:
  assert db.query('SELECT 1')==[]
  with pytest.raises(D1BudgetReached):db.query('SELECT 1')
  assert post.call_count==1
  with db.checkpoint():db.query('SELECT 1')
  assert post.call_count==2
 finally:db.close()


def test_unlimited_mode_meters_without_blocking(monkeypatch):
 db=BudgetDB(cf_account_id='a',cf_api_token='test',cf_database_id='d',enforce_limits=False)
 response=Mock();response.json.return_value={'success':True,'result':[{'success':True,'results':[], 'meta':{'rows_read':90000,'rows_written':1800}}]}
 post=Mock(return_value=response);monkeypatch.setattr(db.session,'post',post)
 db.metering=True
 try:
  assert db.query('SELECT 1')==[]
  assert db.query('SELECT 1')==[]
  assert post.call_count==2
 finally:db.close()


def test_persistent_counts_do_not_scan_history_on_every_batch_and_are_atomic(tmp_path):
 db=D1Client(local_sqlite_path=str(tmp_path/'counts.sqlite'))
 from fmr_ingest_stock.device_sync import ensure_base_brands_and_anchor
 db.execute_script(Path(__file__).parents[1].joinpath('schema.sql').read_text());ensure_base_brands_and_anchor(db)
 try:
  store=Store(db,'google');logs=[]
  db._get_sqlite_conn().set_trace_callback(logs.append)
  store.put_many([(store.key('release',i),{'status':'resolved','next_check':9999999999,'published':False}) for i in range(200)])
  for _ in range(20):assert Store(db,'google').counts()['releases']=={'resolved':200}
  assert sum('GROUP BY stage,status' in sql for sql in logs)==1
  assert store.ready_count==200
  key,value=store.ready(1)[0];value['published']=True;store.put(key,value)
  assert store.ready_count==199
  # Cursor searches use the existing ordered compound index, not JSON sorting.
  logs.clear();store.advance('publish',key);store.ready(1)
  query=next(sql for sql in logs if 'ORDER BY source_target LIMIT' in sql)
  plan=db.query('EXPLAIN QUERY PLAN '+query)
  assert any('INDEX' in str(r).upper() for r in plan)
 finally:db.close()


def test_quota_pause_rejects_cloud_database_start_before_reset(monkeypatch):
 from fmr_ingest_stock import vendor_catalogue as job,quota_resume,sony_bundle
 from fmr_ingest_stock.d1_budget import NOT_BEFORE
 monkeypatch.setenv('GITHUB_ACTIONS','true')
 monkeypatch.setattr(job.time,'time',lambda:NOT_BEFORE-1)
 monkeypatch.setattr(job,'BudgetDB',lambda **kw:pytest.fail('Database touched before reset'))
 assert job.main(['--vendor','google'])==0
 assert quota_resume.main()==0
 assert sony_bundle.main()==0


def test_budget_exception_survives_atomic_batch_wrapper(monkeypatch):
 db=BudgetDB(cf_account_id='a',cf_api_token='test',cf_database_id='d')
 db.metering=True;db.rows_read=90_000
 try:
  with pytest.raises(D1BudgetReached):db.execute_batch([('SELECT 1',[])])
 finally:db.close()


def test_settlement_charges_actual_overage_instead_of_hiding_it(tmp_path):
 db=BudgetDB(local_sqlite_path=str(tmp_path/'overage.sqlite'))
 try:
  assert db.acquire();db.rows_read=100100;db.rows_written=2100;db.settle()
  row=db.query('SELECT * FROM fmr_catalogue_budget')[0]
  assert row['reads_reserved']==100200 and row['writes_reserved']==2120
 finally:db.close()
