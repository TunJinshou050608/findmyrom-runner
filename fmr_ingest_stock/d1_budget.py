"""Conservative project allocations; leave room for visitors and image jobs."""
from contextlib import contextmanager
from datetime import datetime,timezone
import os
from fmr_core.d1_client import D1Client
from .catalogue_cache import CatalogueDeadline

NOT_BEFORE=1793491200 # 2026-11-01T00:00:00Z, current user-requested quota pause
DAILY_READ_RESERVATION=2_000_000
DAILY_WRITE_RESERVATION=40_000
JOB_READ_RESERVATION=100_000
JOB_WRITE_RESERVATION=2_000


class D1BudgetReached(CatalogueDeadline):pass


def provider_quota_error(exc):
    for _ in range(8):
        if exc is None:return False
        message=str(exc).lower()
        response=getattr(exc,'response',None)
        if response is not None:
            try:message+=' '+str(response.json()).lower()
            except (ValueError,TypeError):pass
        if ('daily' in message or 'free tier' in message) and ('quota' in message or 'row write limit' in message or 'row read limit' in message):return True
        exc=exc.__cause__ or exc.__context__
    return False


class BudgetDB(D1Client):
    def __init__(self,*args,enforce_limits=True,**kwargs):
        super().__init__(*args,**kwargs)
        self.enforce_limits=enforce_limits
        self.rows_read=0;self.rows_written=0;self.metering=False;self.checkpointing=False

    def _request_cf_with_retry(self,payload,is_safe_to_retry):
        items=payload.get('batch',[payload])
        estimated_writes=sum(32*(max(1,len(item.get('params',[]))) if ' IN (' in str(item.get('sql','')).upper() else 1) for item in items if str(item.get('sql','')).lstrip().upper().startswith(('INSERT','UPDATE','DELETE')))
        if self.enforce_limits and self.metering and not self.checkpointing and (self.rows_read>=90_000 or self.rows_written>=1_800 or self.rows_written+estimated_writes>JOB_WRITE_RESERVATION or getattr(self,'reservation_day',datetime.now(timezone.utc).strftime('%Y-%m-%d'))!=datetime.now(timezone.utc).strftime('%Y-%m-%d')):
            raise D1BudgetReached('Per-job D1 allocation reached')
        if getattr(self,'provider_quota',False):raise D1BudgetReached('Provider daily quota exhausted')
        try:
            result=super()._request_cf_with_retry(payload,is_safe_to_retry)
        except Exception as exc:
            if provider_quota_error(exc):
                self.provider_quota=True
                raise D1BudgetReached('Provider daily quota exhausted') from exc
            raise
        if self.metering:
            for item in result.get('result',[]):
                self.rows_read+=int(item.get('meta',{}).get('rows_read',0))
                self.rows_written+=int(item.get('meta',{}).get('rows_written',0))
        return result

    def acquire(self):
        day=datetime.now(timezone.utc).strftime('%Y-%m-%d')
        self.reservation_day=day
        if not self.enforce_limits:
            self.metering=True
            return True
        self.execute('''CREATE TABLE IF NOT EXISTS fmr_catalogue_budget(
            utc_day TEXT PRIMARY KEY,reads_reserved INTEGER NOT NULL,writes_reserved INTEGER NOT NULL)''')
        row=self.execute('''INSERT INTO fmr_catalogue_budget(utc_day,reads_reserved,writes_reserved) VALUES (?,?,?)
            ON CONFLICT(utc_day) DO UPDATE SET reads_reserved=reads_reserved+excluded.reads_reserved,
            writes_reserved=writes_reserved+excluded.writes_reserved
            WHERE reads_reserved+excluded.reads_reserved<=? AND writes_reserved+excluded.writes_reserved<=?''',
            (day,JOB_READ_RESERVATION,JOB_WRITE_RESERVATION,DAILY_READ_RESERVATION,DAILY_WRITE_RESERVATION),is_idempotent=False)
        self.metering=bool(row.get('changes'))
        return self.metering

    @contextmanager
    def checkpoint(self):
        self.checkpointing=True
        try:yield
        finally:self.checkpointing=False

    def settle(self):
        """Release unused reservation after a sole, serialized publisher finishes.

        Include a conservative allowance for budget-control/index writes. A crash
        retains the full reservation, which is safe and resets on the next UTC day.
        """
        if not self.metering or getattr(self,'provider_quota',False):return
        self.metering=False
        if not self.enforce_limits:return
        day=getattr(self,'reservation_day',datetime.now(timezone.utc).strftime('%Y-%m-%d'))
        unused_reads=JOB_READ_RESERVATION-self.rows_read-100
        unused_writes=JOB_WRITE_RESERVATION-self.rows_written-20
        self.execute('UPDATE fmr_catalogue_budget SET reads_reserved=MAX(0,reads_reserved-?),writes_reserved=MAX(0,writes_reserved-?) WHERE utc_day=?',
            (unused_reads,unused_writes,day))

