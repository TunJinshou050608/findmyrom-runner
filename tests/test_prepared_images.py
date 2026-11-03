"""Synthetic image bytes only: progress, hashing and bounded cleanup."""
import contextlib
import hashlib
import json
import threading
import time
from types import SimpleNamespace
import pytest
from fmr_extractor.prepared import PreparedImages, verify_control
from test_extractor_service import payload, SECRET


def store(chunks=None, size=65536):
    @contextlib.contextmanager
    def opener(_job):
        yield iter(chunks or [b'ANDROID!'+bytes(65528)]), {'output_size':size}, SimpleNamespace(used=10)
    server=SimpleNamespace(opener=opener,slot=threading.BoundedSemaphore(1))
    return PreparedImages(server),server


def await_state(manager):
    deadline=time.monotonic()+2
    while time.monotonic()<deadline:
        result=manager.status('00000000-0000-0000-0000-000000000000')
        if result and result['state']!='preparing':return result
        time.sleep(0.005)
    raise AssertionError('Producer did not finish')


def job():return json.loads(payload()[0])


def test_ready_checksum_precedes_delivery_and_file_deleted_after_download():
    manager,server=store()
    try:
        assert manager.start(job());ready=await_state(manager)
        assert ready['state']=='ready' and ready['total']==65536
        assert not manager.start(job())
        with manager.claim(job()['id']) as item:
            output=item['file'];data=output.read()
            assert hashlib.sha256(data).hexdigest()==ready['sha256']
            with pytest.raises(ValueError):
                with manager.claim(job()['id']):pass
        assert output.closed and manager.status(job()['id']) is None
        assert server.slot.acquire(False)
    finally:manager.close()


def test_bad_image_never_exposes_checksum_or_downloadable_file():
    manager,_=store([b'ANDROID!'],size=65536)
    try:
        assert manager.start(job());assert await_state(manager)['state']=='failed'
        assert manager.status(job()['id'])['sha256'] is None
        with pytest.raises(ValueError):
            with manager.claim(job()['id']):pass
    finally:manager.close()


def test_ready_expiry_and_cancel_release_temp_file():
    for action in ('expiry','cancel'):
        manager,server=store()
        try:
            assert manager.start(job());await_state(manager);output=manager.job['file']
            if action=='expiry':manager.job['deadline']=0;manager.expire()
            else:manager.cancel(job()['id'])
            assert output.closed and manager.job is None and server.slot.acquire(False)
        finally:manager.close()


def test_control_signed_exact_bytes_and_expiry():
    body,sig=payload(op='status');assert verify_control(body,sig,SECRET)['op']=='status'
    with pytest.raises(ValueError):verify_control(body+b' ',sig,SECRET)
    body,sig=payload(op='shell')
    with pytest.raises(ValueError):verify_control(body,sig,SECRET)


def test_real_http_prepare_status_and_native_download():
    import httpx
    import hmac
    from fmr_extractor.service import Server
    @contextlib.contextmanager
    def opener(_job):
        yield iter([b'ANDROID!'+bytes(65528)]), {'output_size':65536}, SimpleNamespace(used=10)
    try:server=Server(('127.0.0.1',0),SECRET,opener=opener,bytes_per_second=6250000)
    except PermissionError:pytest.skip('Host sandbox forbids listening sockets')
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    def post(client,path,data):
        body=json.dumps(data,separators=(',',':')).encode()
        sig=hmac.new(SECRET.encode(),b'token=extract-job:'+body+f'&exp={data["expires"]}'.encode(),hashlib.sha256).hexdigest()
        return client.post(f'http://127.0.0.1:{server.server_port}'+path,content=body,headers={'X-FMR-Signature':sig})
    try:
        with httpx.Client(trust_env=False) as client:
            data=job();assert post(client,'/v1/prepare',data).status_code==202
            control=dict(id=data['id'],op='status',expires=int(time.time())+60)
            deadline=time.monotonic()+2
            while time.monotonic()<deadline:
                state=post(client,'/v1/job',control).json()
                if state['state']=='ready':break
                time.sleep(0.005)
            assert state['state']=='ready' and len(state['sha256'])==64
            control['op']='download';response=post(client,'/v1/job',control)
            assert response.status_code==200 and response.headers['content-length']=='65536'
            assert hashlib.sha256(response.content).hexdigest()==state['sha256']==response.headers['x-fmr-sha256']
            assert response.headers['content-disposition']=='attachment; filename="akita_v1_init_boot.img"'
            assert post(client,'/v1/job',control).status_code==409
            deadline=time.monotonic()+1
            while server.prepared.job is not None and time.monotonic()<deadline:time.sleep(0.005)
            assert server.prepared.job is None
    finally:server.shutdown();server.server_close();thread.join(timeout=2)
