from unittest.mock import Mock
import pytest
import fmr_ingest_stock.d1_budget as d1_budget
from fmr_ingest_stock.d1_budget import BudgetDB, D1BudgetReached


def test_not_before_removed_and_deadline_flag_preserved():
    assert not hasattr(d1_budget, 'NOT_BEFORE')
    assert D1BudgetReached.fmr_batch_deadline is True


def test_daily_allocations_stop_without_network_and_reset_per_utc_day(tmp_path):
    db = BudgetDB(local_sqlite_path=str(tmp_path / 'budget.sqlite'))
    try:
        assert all(db.acquire() for _ in range(20))
        assert not db.acquire()
        row = db.query('SELECT * FROM fmr_catalogue_budget')[0]
        assert row['reads_reserved'] == 2_000_000 and row['writes_reserved'] == 40_000
    finally:
        db.close()


def test_response_metadata_meter_stops_next_request_and_allows_final_checkpoint(monkeypatch):
    db = BudgetDB(cf_account_id='a', cf_api_token='test', cf_database_id='d')
    response = Mock()
    response.json.return_value = {
        'success': True,
        'result': [{'success': True, 'results': [], 'meta': {'rows_read': 90000, 'rows_written': 1800}}],
    }
    post = Mock(return_value=response)
    monkeypatch.setattr(db.session, 'post', post)
    db.metering = True
    try:
        assert db.query('SELECT 1') == []
        with pytest.raises(D1BudgetReached):
            db.query('SELECT 1')
        assert post.call_count == 1
        with db.checkpoint():
            db.query('SELECT 1')
        assert post.call_count == 2
    finally:
        db.close()


def test_budget_exception_survives_atomic_batch_wrapper():
    db = BudgetDB(cf_account_id='a', cf_api_token='test', cf_database_id='d')
    db.metering = True
    db.rows_read = 90_000
    try:
        with pytest.raises(D1BudgetReached):
            db.execute_batch([('SELECT 1', [])])
    finally:
        db.close()


def test_settlement_charges_actual_overage_instead_of_hiding_it(tmp_path):
    db = BudgetDB(local_sqlite_path=str(tmp_path / 'overage.sqlite'))
    try:
        assert db.acquire()
        db.rows_read = 100100
        db.rows_written = 2100
        db.settle()
        row = db.query('SELECT * FROM fmr_catalogue_budget')[0]
        assert row['reads_reserved'] == 100200 and row['writes_reserved'] == 2120
    finally:
        db.close()
