"""Single-job native-image HTTP service, intended ONLY behind Cloudflare Tunnel.

No shell execution, proxy settings, firmware archive files or catalogue writes.
Prepared downloads use one private temporary image, deleted after delivery/expiry.
"""
import contextlib
import hashlib
import hmac
import json
import logging
import os
import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import httpx
from .streaming_probe import (RangeReader, DecryptedReader, image_stream,
                              samsung_source, LIMITS, ALLOWED)

from .prepared import PreparedImages, verify_control

LOG = logging.getLogger('fmr-extractor')
MAX_BODY = 8192
BRANDS = {'xiaomi', 'google', 'oneplus', 'oppo', 'realme', 'vivo', 'samsung'}


def verify_job(body, signature, secret, now=None):
    if len(body) > MAX_BODY or not re.fullmatch('[0-9a-f]{64}', signature):
        raise ValueError('Invalid authentication')
    job = json.loads(body)
    now = int(time.time()) if now is None else now
    expires = job.get('expires')
    if type(expires) is not int or not now <= expires <= now + 90:
        raise ValueError('Expired request')
    # Identical byte sequence to the existing TS signTicket helper.
    message = b'token=extract-job:' + body + f'&exp={expires}'.encode()
    expected = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise ValueError('Invalid authentication')
    if (job.get('schema') != 1 or job.get('brand') not in BRANDS
            or job.get('partition') not in {'boot', 'init_boot', 'recovery'}
            or job.get('package_type') not in {'factory_zip', 'recovery_zip', 'samsung_zip', 'samsung_encrypted'}
            or not re.fullmatch(r'fw_[A-Za-z0-9_-]{10,13}', job.get('token', ''))
            or not re.fullmatch(r'[0-9a-f-]{36}', job.get('id', ''))):
        raise ValueError('Invalid job')
    for field in ('device', 'version'):
        if not isinstance(job.get(field), str) or not re.fullmatch(r'[A-Za-z0-9_./+-]{1,200}', job[field]):
            raise ValueError('Invalid identity')
    u = urlsplit(job.get('url', ''))
    host = (u.hostname or '').lower()
    # FUS jobs resolve authorization internally; URLs may not supply credentials.
    if (u.scheme != 'https' or u.username or u.password or u.port not in (None, 443)
            or u.fragment or not any(host == d or host.endswith('.' + d) for d in ALLOWED)):
        raise ValueError('Source not on official CDN allowlist')
    return job


@contextlib.contextmanager
def open_image(job):
    """Own every upstream connection and decoder for the lifetime of a response."""
    with httpx.Client(timeout=30, follow_redirects=False, trust_env=False) as client:
        headers = {}; key = None; refresh = None
        url = job['url']
        if job['brand'] == 'samsung' and job['package_type'] == 'samsung_encrypted':
            # Encrypted Samsung records need exact model/CSC supplied by the
            # signed gateway. Never silently upgrade a requested historical build.
            model, region = job.get('model', ''), job.get('region', '')
            if not re.fullmatch(r'SM-[A-Z0-9]+', model) or not re.fullmatch(r'[A-Z0-9]{3}', region):
                raise ValueError('Missing exact Samsung model/CSC')
            url, headers, key, identity, refresh = samsung_source(
                client, model, region, expected_version=job['version'])
        raw = RangeReader(url, client, headers, refresh=refresh, prefetch=job['brand'] == 'google')
        with contextlib.closing(raw):
            remote = DecryptedReader(raw, key) if key else raw
            metrics = {}
            with contextlib.closing(image_stream(remote, job['partition'], metrics)) as chunks:
                yield chunks, metrics, raw


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 8

    def __init__(self, address, secret, opener=open_image, bytes_per_second=2500000):
        self.secret = secret
        self.opener = opener
        self.bytes_per_second = bytes_per_second
        self.slot = threading.BoundedSemaphore(1)
        self.connections = threading.BoundedSemaphore(12)
        self.replays = {}
        self.lock = threading.Lock()
        super().__init__(address, Handler)
        self.prepared = PreparedImages(self)

    def server_close(self):
        if hasattr(self, 'prepared'):
            self.prepared.close()
        super().server_close()

    def process_request(self, request, client_address):
        if not self.connections.acquire(False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.connections.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.connections.release()


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'FindMyROM'

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, *_args):
        pass  # Never log request bodies, source URLs, signatures or credentials.

    def reply(self, code, message):
        payload = message.encode()
        self.send_response(code)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Connection', 'close')
        if code == 429:
            self.send_header('Retry-After', '20')
        self.end_headers()
        self.wfile.write(payload)
        self.close_connection = True

    def do_GET(self):
        if self.path == '/v1/capabilities':
            return self.reply(200, json.dumps({'schema': 3, 'prepared_downloads': True, 'brands': sorted(BRANDS), 'partitions': ['boot', 'init_boot', 'recovery']}))
        self.reply(404, 'Not found')

    def do_POST(self):
        if self.path not in {'/v1/extract', '/v1/prepare', '/v1/job'}:
            return self.reply(404, 'Not found')
        length = self.headers.get('Content-Length', '')
        if (self.headers.get('Transfer-Encoding') or not length.isdecimal()
                or not 0 < int(length) <= MAX_BODY):
            return self.reply(400, 'Invalid request size')
        try:
            body = self.rfile.read(int(length))
            if len(body) != int(length):
                raise ValueError('Truncated job')
            if self.path == '/v1/job':
                control = verify_control(body, self.headers.get('X-FMR-Signature', ''), self.server.secret)
                return self.control(control)
            job = verify_job(body, self.headers.get('X-FMR-Signature', ''), self.server.secret)
        except Exception:
            return self.reply(403, 'Forbidden')
        if self.path == '/v1/prepare':
            with self.server.lock:
                now = int(time.time())
                self.server.replays = {k: exp for k, exp in self.server.replays.items() if exp >= now}
                if job['id'] in self.server.replays:
                    return self.reply(409, 'Request already used')
                if not self.server.prepared.start(job):
                    return self.reply(429, 'Extraction server busy')
                self.server.replays[job['id']] = job['expires']
            return self.reply(202, json.dumps({'state': 'preparing'}))
        if not self.server.slot.acquire(False):
            return self.reply(429, 'Extraction server busy')
        started = False
        begin = time.monotonic()
        total = 0
        try:
            with self.server.lock:
                now = int(time.time())
                self.server.replays = {k: exp for k, exp in self.server.replays.items() if exp >= now}
                if job['id'] in self.server.replays:
                    return self.reply(409, 'Request already used')
                self.server.replays[job['id']] = job['expires']
            with self.server.opener(job) as (chunks, metrics, raw):
                first = next(chunks)  # Fail before headers when possible.
                size = metrics.get('output_size')
                self.send_response(200)
                self.send_header('Content-Type', 'application/octet-stream')
                self.send_header('Cache-Control', 'no-store, no-transform')
                self.send_header('Content-Disposition', 'attachment; filename="' + job['partition'] + '.img"')
                known = type(size) is int and 0 < size <= LIMITS[job['partition']][1]
                self.send_header('Content-Length', str(size)) if known else self.send_header('Transfer-Encoding', 'chunked')
                self.send_header('Connection', 'close')
                self.end_headers()
                started = True
                digest = hashlib.sha256()
                stream_begin = time.monotonic()
                chunk = first
                while True:
                    if time.monotonic() - begin > 480:
                        raise TimeoutError('Job deadline exceeded')
                    # OS sends block on slow clients: no unbounded producer queue.
                    if known:
                        self.wfile.write(chunk)
                    else:
                        self.wfile.write(f'{len(chunk):x}\r\n'.encode() + chunk + b'\r\n')
                    self.wfile.flush()
                    total += len(chunk)
                    digest.update(chunk)
                    delay = total / self.server.bytes_per_second - (time.monotonic() - stream_begin)
                    if delay > 0:
                        time.sleep(min(delay, 1))
                    chunk = next(chunks, None)
                    if chunk is None:
                        break
                if total != metrics['output_size']:
                    raise ValueError('Incomplete image')
                if not known:
                    self.wfile.write(b'0\r\n\r\n')
                    self.wfile.flush()
                LOG.info(json.dumps({'event': 'complete', 'job': job['id'], 'bytes': total,
                                     'sha256': digest.hexdigest(), 'upstream_bytes': raw.used}))
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            LOG.info(json.dumps({'event': 'cancelled', 'job': job['id'], 'bytes': total}))
        except Exception as exc:
            LOG.warning(json.dumps({'event': 'failed', 'job': job['id'], 'type': type(exc).__name__, 'bytes': total}))
            if not started:
                self.reply(422, 'Package or partition unavailable')
            # Once streaming starts, deliberately omit a chunk terminator / final
            # withheld bytes. A failed extraction must not look like success.
        finally:
            self.close_connection = True
            self.server.slot.release()

    def control(self, data):
        if data['op'] == 'status':
            result = self.server.prepared.status(data['id'])
            return self.reply(200, json.dumps(result)) if result else self.reply(404, 'Job expired')
        if data['op'] == 'cancel':
            self.server.prepared.cancel(data['id'])
            return self.reply(200, '{}')
        started = False
        try:
            with self.server.prepared.claim(data['id']) as item:
                self.send_response(200)
                self.send_header('Content-Type', 'application/octet-stream')
                self.send_header('Content-Length', str(item['total']))
                self.send_header('X-FMR-SHA256', item['sha256'])
                self.send_header('Content-Disposition', 'attachment; filename="' + item['filename'] + '"')
                self.send_header('Cache-Control', 'no-store, no-transform')
                self.send_header('Connection', 'close')
                self.end_headers()
                started = True
                begin = time.monotonic()
                sent = 0
                while True:
                    if item['cancel'].is_set() or time.monotonic()-begin > 480:
                        raise TimeoutError('Download cancelled')
                    chunk = item['file'].read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    sent += len(chunk)
                    delay = sent/self.server.bytes_per_second - (time.monotonic()-begin)
                    if delay > 0:
                        time.sleep(min(delay, 1))
        except Exception:
            if not started:
                self.reply(409, 'Image expired or not ready')
        finally:
            self.close_connection = True


def main():
    secret = os.environ.get('EXTRACTOR_HMAC_SECRET', '')
    if len(secret) < 32:
        raise SystemExit('EXTRACTOR_HMAC_SECRET must be at least 32 characters')
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
    rate = int(os.environ.get('FMR_BYTES_PER_SECOND', '2500000'))
    if not 65536 <= rate <= 6250000:
        raise SystemExit('Invalid bandwidth cap')
    # Always loopback: internet access requires the separate tunnel connector.
    server = Server(('127.0.0.1', 18765), secret, bytes_per_second=rate)
    LOG.info('Extraction service listening on loopback port 18765')
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
