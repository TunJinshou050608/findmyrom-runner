"""One bounded, private, short-lived image; never a firmware archive cache."""
import contextlib
import hashlib
import hmac
import json
import re
import tempfile
import threading
import time
from .streaming_probe import LIMITS


def verify_control(body, signature, secret, now=None):
    now = int(time.time()) if now is None else now
    if len(body) > 1024 or not re.fullmatch('[0-9a-f]{64}', signature):
        raise ValueError('Invalid authentication')
    data = json.loads(body)
    expires = data.get('expires')
    if type(expires) is not int or not now <= expires <= now + 90:
        raise ValueError('Expired request')
    expected = hmac.new(secret.encode(), b'token=extract-job:' + body + f'&exp={expires}'.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise ValueError('Invalid authentication')
    if data.get('op') not in {'status', 'download', 'cancel'} or not re.fullmatch('[0-9a-f-]{36}', data.get('id', '')):
        raise ValueError('Invalid control')
    return data


class PreparedImages:
    def __init__(self, server):
        self.server = server
        self.lock = threading.RLock()
        self.job = None
        self.closed = threading.Event()
        threading.Thread(target=self._reaper, daemon=True).start()

    def _reaper(self):
        while not self.closed.wait(5):
            self.expire()

    def _dispose(self, item):
        item['cancel'].set()
        if item['file'] is not None:
            item['file'].close()
            item['file'] = None
        if not item['released']:
            item['released'] = True
            self.server.slot.release()
        if self.job is item:
            self.job = None

    def expire(self):
        with self.lock:
            item = self.job
            if item is None:
                return
            now = time.monotonic()
            if item['state'] == 'preparing' and (now - item['seen'] > 120 or now - item['started'] > 480):
                item['cancel'].set()  # Producer closes its own file/decoder.
            elif item['state'] in {'ready', 'failed'} and now > item['deadline']:
                self._dispose(item)

    def start(self, job):
        self.expire()
        with self.lock:
            if self.job is not None or not self.server.slot.acquire(False):
                return False
            now = time.monotonic()
            item = dict(id=job['id'], state='preparing', bytes=0, total=None, sha256=None,
                        file=None, started=now, seen=now, deadline=now+480,
                        cancel=threading.Event(), released=False)
            item['filename'] = re.sub(r'[^A-Za-z0-9_.-]', '_', f"{job['device']}_{job['version']}_{job['partition']}.img")[-180:]
            self.job = item
            threading.Thread(target=self._produce, args=(item, job), daemon=True).start()
            return True

    def _produce(self, item, job):
        # TemporaryFile is unlinked immediately on Unix; close/process exit frees it.
        # PrivateTmp=yes isolates it from other VPS services.
        output = None
        try:
            output = tempfile.TemporaryFile(mode='w+b', prefix='fmr-image-')
            digest = hashlib.sha256()
            with self.server.opener(job) as (chunks, metrics, raw):
                for chunk in chunks:
                    if item['cancel'].is_set() or time.monotonic()-item['started'] > 480:
                        raise TimeoutError('Preparation cancelled')
                    total = item['bytes'] + len(chunk)
                    if total > LIMITS[job['partition']][1]:
                        raise ValueError('Image exceeds limit')
                    output.write(chunk)
                    digest.update(chunk)
                    with self.lock:
                        item['bytes'] = total
                        size = metrics.get('output_size')
                        item['total'] = size if type(size) is int and 0 < size <= LIMITS[job['partition']][1] else None
                if not LIMITS[job['partition']][0] <= item['bytes'] <= LIMITS[job['partition']][1] or item['bytes'] != metrics.get('output_size'):
                    raise ValueError('Incomplete image')
            output.flush()
            output.seek(0)
            with self.lock:
                if item['cancel'].is_set():
                    raise TimeoutError('Preparation cancelled')
                item.update(state='ready', file=output, total=item['bytes'], sha256=digest.hexdigest(), deadline=time.monotonic()+300)
                output = None
        except Exception:
            with self.lock:
                item.update(state='failed', sha256=None, deadline=time.monotonic()+30)
                if not item['released']:
                    item['released'] = True
                    self.server.slot.release()
        finally:
            if output is not None:
                output.close()
            if item['cancel'].is_set():
                with self.lock:
                    self._dispose(item)

    def status(self, job_id):
        self.expire()
        with self.lock:
            item = self.job
            if item is None or item['id'] != job_id:
                return None
            item['seen'] = time.monotonic()
            elapsed = max(time.monotonic()-item['started'], 0.001)
            return dict(state=item['state'], bytes=item['bytes'], total=item['total'],
                        bytes_per_second=int(item['bytes']/elapsed),
                        sha256=item['sha256'] if item['state']=='ready' else None)

    @contextlib.contextmanager
    def claim(self, job_id):
        with self.lock:
            item = self.job
            if item is None or item['id'] != job_id or item['state'] != 'ready' or time.monotonic() > item['deadline']:
                raise ValueError('Image expired or not ready')
            item['state'] = 'downloading'
        try:
            yield item
        finally:
            with self.lock:
                self._dispose(item)

    def cancel(self, job_id):
        with self.lock:
            item = self.job
            if item is None or item['id'] != job_id:
                return
            item['cancel'].set()
            if item['state'] in {'ready', 'failed'}:
                self._dispose(item)

    def close(self):
        self.closed.set()
        with self.lock:
            if self.job is not None:
                self.cancel(self.job['id'])
