"""No firmware downloads; exercise real HTTP handlers using in-memory sockets."""
import contextlib
import hashlib
import hmac
import io
import json
import threading
import time
from types import SimpleNamespace
import pytest
from fmr_extractor.service import Handler, verify_job

SECRET = 'fixture-secret-' * 4


def payload(**changes):
    data = dict(schema=1, id='00000000-0000-0000-0000-000000000000', expires=int(time.time()) + 60,
                token='fw_fixture00001', brand='google', device='akita', version='v1',
                partition='init_boot', package_type='factory_zip',
                url='https://dl.google.com/dl/android/aosp/akita-v1-factory-test.zip')
    data.update(changes)
    body = json.dumps(data, separators=(',', ':')).encode()
    sig = hmac.new(SECRET.encode(), b'token=extract-job:' + body + f'&exp={data["expires"]}'.encode(), hashlib.sha256).hexdigest()
    return body, sig


@pytest.mark.parametrize('changes', [dict(expires=0), dict(expires=int(time.time())+1000), dict(url='http://127.0.0.1/a.zip'), dict(url='https://dl.google.com.evil.test/a.zip'), dict(url='https://a:pass@dl.google.com/a.zip'), dict(partition='system'), dict(brand='unknown')])
def test_reject_invalid_job(changes):
    body, sig = payload(**changes)
    with pytest.raises(ValueError): verify_job(body, sig, SECRET)


def test_auth_exact_bytes_and_separate_secret():
    body, sig = payload()
    assert verify_job(body, sig, SECRET)['partition'] == 'init_boot'
    for modified, key in ((body+b' ', SECRET), (body, 'wrong-secret')):
        with pytest.raises(ValueError): verify_job(modified, sig, key)


class Connection:
    def __init__(self, request, fail_write=False):
        self.input = io.BytesIO(request)
        self.output = io.BytesIO()
        self.fail_write = fail_write
    def makefile(self, *_args): return self.input
    def sendall(self, value):
        if self.fail_write and value.startswith(b'ANDROID!'): raise BrokenPipeError()
        self.output.write(value)
    def settimeout(self, *_args): pass


def serve(chunks=None, size=12, fail_write=False, slot=None, replays=None):
    closed = []; consumed = []
    def stream():
        try:
            for chunk in chunks or [b'ANDROID!', b'1234']:
                consumed.append(chunk)
                yield chunk
        finally: closed.append(True)
    @contextlib.contextmanager
    def opener(_job):
        with contextlib.closing(stream()) as source:
            yield source, {'output_size': size}, SimpleNamespace(used=123)
    server = SimpleNamespace(secret=SECRET, slot=slot or threading.BoundedSemaphore(1),
        lock=threading.Lock(), replays=replays or {}, opener=opener, bytes_per_second=6250000)
    body, sig = payload()
    request = b'POST /v1/extract HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\nContent-Length: ' + str(len(body)).encode() + b'\r\nX-FMR-Signature: ' + sig.encode() + b'\r\n\r\n' + body
    connection = Connection(request, fail_write)
    Handler(connection, ('127.0.0.1', 1), server)
    return connection.output.getvalue(), closed, consumed, server


def test_http_stream_and_release_slot():
    result, closed, _, server = serve()
    assert b'200 OK' in result
    assert result.split(b'\r\n\r\n', 1)[1] == b'ANDROID!1234'
    assert b'Content-Length: 12' in result
    assert closed and server.slot.acquire(False)


def test_cancel_closes_upstream_without_consuming_rest():
    result, closed, consumed, server = serve(fail_write=True)
    assert closed and consumed == [b'ANDROID!']
    assert server.slot.acquire(False)


def test_busy_does_not_start_extraction():
    slot = threading.BoundedSemaphore(1); slot.acquire()
    result, closed, _, _ = serve(slot=slot)
    assert b'429 Too Many Requests' in result and not closed


def test_replay_does_not_start_extraction():
    result, closed, _, _ = serve(replays={'00000000-0000-0000-0000-000000000000': int(time.time())+90})
    assert b'409 Conflict' in result and not closed


def test_decoder_failure_does_not_finish_chunked_response():
    def bad():
        yield b'ANDROID!'
        raise ValueError('failed integrity check')
    result, closed, _, _ = serve(chunks=bad(), size=None)
    assert b'Transfer-Encoding: chunked' in result
    assert not result.endswith(b'0\r\n\r\n') and closed


def test_real_loopback_http_download():
    from fmr_extractor.service import Server
    @contextlib.contextmanager
    def opener(_job):
        with contextlib.closing(iter_image()) as chunks:
            yield chunks, {'output_size': 65536}, SimpleNamespace(used=1024)
    def iter_image():
        yield b'ANDROID!' + bytes(65528)
    try:
        server = Server(('127.0.0.1', 0), SECRET, opener=opener)
    except PermissionError:
        pytest.skip('Host sandbox forbids listening sockets; exercised on Linux runner')
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        import httpx
        body, sig = payload()
        with httpx.Client(trust_env=False) as client:
            response = client.post(f'http://127.0.0.1:{server.server_port}/v1/extract', content=body, headers={'X-FMR-Signature': sig})
            assert response.status_code == 200
            assert len(response.content) == 65536 and response.content.startswith(b'ANDROID!')
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


@pytest.mark.parametrize('host', ['bigota.d.miui.com', 'hugeota.d.miui.com', 'bkt-sgp-miui-ota-update-alisgp.oss-ap-southeast-1.aliyuncs.com'])
def test_xiaomi_jobs_accept_only_known_firmware_hosts(host):
    body, sig = payload(brand='xiaomi', device='alioth', partition='boot', package_type='recovery_zip', url=f'https://{host}/v1/miui_ALIOTH.zip')
    assert verify_job(body, sig, SECRET)['brand'] == 'xiaomi'


def test_xiaomi_cdn_lookalikes_and_arbitrary_buckets_rejected():
    for host in ('bigota.d.miui.com.evil.test', 'unrelated.oss-ap-southeast-1.aliyuncs.com'):
        body, sig = payload(brand='xiaomi', url=f'https://{host}/miui.zip')
        with pytest.raises(ValueError): verify_job(body, sig, SECRET)
