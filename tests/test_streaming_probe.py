from contextlib import closing
import hashlib
import io
import struct
import zipfile

import httpx
import pytest
import zstandard as zstd

from fmr_extractor.streaming_probe import CHUNK, DecryptedReader, RangeReader, image_stream, http_delivery_probe


def make_zip(entries):
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w') as archive:
        for name,data,method in entries:
            archive.writestr(name,data,compress_type=method)
    return out.getvalue()


def remote_for(data, encoding='identity', host='dl.google.com'):
    def serve(request):
        a,b=map(int,request.headers['range'][6:].split('-'))
        return httpx.Response(206,headers={'Content-Range':f'bytes {a}-{b}/{len(data)}',
                                          'Content-Encoding':encoding,'ETag':'"fixed"'},stream=httpx.ByteStream(data[a:b+1]))
    client=httpx.Client(transport=httpx.MockTransport(serve))
    return RangeReader('https://'+host+'/test.zip',client),client


def test_nested_zip_stream_crc_and_cancel():
    img=b'ANDROID!'+bytes(CHUNK*3-8)
    inner=make_zip([('boot.img',img,zipfile.ZIP_DEFLATED)])
    outer=make_zip([('factory/image-test.zip',inner,zipfile.ZIP_STORED)])
    remote,client=remote_for(outer)
    with closing(client):
        metrics={}
        assert b''.join(image_stream(remote,'boot',metrics))==img
        assert metrics['crc32_verified']
        stream=image_stream(remote,'boot',{})
        assert len(next(stream))==CHUNK
        used=remote.used
        stream.close()
        assert remote.used==used


class UnreadBody(httpx.SyncByteStream):
    consumed=False
    closed=False
    def __iter__(self):
        self.consumed=True
        raise AssertionError('Full-ROM body must never be consumed')
        yield b''
    def close(self): self.closed=True


def test_ignored_range_is_closed_without_reading():
    body=UnreadBody()
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200,stream=body))) as client:
        with pytest.raises(ValueError,match='Range HTTP 200'):
            RangeReader('https://dl.google.com/rom.zip',client)
    assert body.closed and not body.consumed


def test_only_oplus_utf8_header_is_tolerated():
    remote,client=remote_for(b'abc',encoding='UTF-8',host='a.allawnfs.com')
    with closing(client): assert remote.fetch(1,2)==b'bc'
    with pytest.raises(ValueError,match='encoded Range'):
        remote_for(b'abc',encoding='UTF-8')


def varint(n):
    out=bytearray()
    while n>=128: out.append((n&127)|128);n>>=7
    out.append(n)
    return bytes(out)


def integer(k,n): return varint(k<<3)+varint(n)
def blob(k,data): return varint((k<<3)|2)+varint(len(data))+data


def payload_zip(corrupt=False, kind=14, gap=False):
    image=b'ANDROID!'+bytes(8*1024*1024-8)
    compressed=zstd.ZstdCompressor().compress(image)
    extent=integer(1,1 if gap else 0)+integer(2,len(image)//4096)
    op=(integer(1,kind)+integer(2,0)+integer(3,len(compressed))+
        blob(6,extent)+blob(8,hashlib.sha256(compressed).digest()))
    expected=b'x'*32 if corrupt else hashlib.sha256(image).digest()
    part=blob(1,b'init_boot')+blob(7,integer(1,len(image))+blob(2,expected))+blob(8,op)
    manifest=integer(3,4096)+blob(13,part)
    payload=struct.pack('>4sQQI',b'CrAU',2,len(manifest),0)+manifest+compressed
    return image,make_zip([('payload.bin',payload,zipfile.ZIP_STORED)])


def test_payload_stream_verified_against_partition_sha256():
    image,data=payload_zip()
    remote,client=remote_for(data)
    with closing(client):
        metrics={}
        assert b''.join(image_stream(remote,'init_boot',metrics))==image
        assert metrics['sha256_verified']


def test_actual_http_delivery_and_disconnection():
    image,data=payload_zip()
    remote,client=remote_for(data)
    with closing(client):
        result=http_delivery_probe(remote,'init_boot')
        assert result['status']=='passed'
        assert result['sha256']==hashlib.sha256(image).hexdigest()
        assert result['cancel']['producer_stopped']


def test_samsung_aligned_decryption_and_deflated_ap_stream():
    import tarfile
    import lz4.frame
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    image=b'ANDROID!'+bytes(CHUNK*2-8)
    tar=io.BytesIO()
    with tarfile.open(fileobj=tar,mode='w') as archive:
        member=lz4.frame.compress(image,content_checksum=True)
        info=tarfile.TarInfo('boot.img.lz4');info.size=len(member)
        archive.addfile(info,io.BytesIO(member))
    plain=make_zip([('AP_fixture.tar.md5',tar.getvalue(),zipfile.ZIP_DEFLATED)])
    key=b'fmr-test-fixture'
    padding=16-len(plain)%16
    padded=plain+bytes([padding])*padding
    encoder=Cipher(algorithms.AES(key),modes.ECB()).encryptor()
    encrypted=encoder.update(padded)+encoder.finalize()
    remote,client=remote_for(encrypted)
    with closing(client):
        decrypted=DecryptedReader(remote,key)
        decrypted.seek(3)
        assert decrypted.read(77)==plain[3:80]
        metrics={}
        assert b''.join(image_stream(decrypted,'boot',metrics))==image
        assert metrics['ap_compression']==zipfile.ZIP_DEFLATED


@pytest.mark.parametrize('kwargs,match',[
    ({'corrupt':True},'Partition SHA256 mismatch'),
    ({'kind':4},'Needs source image'),
    ({'gap':True},'Invalid extent'),
])
def test_payload_refuses_bad_hash_deltas_and_extents(kwargs,match):
    _,data=payload_zip(**kwargs)
    remote,client=remote_for(data)
    with closing(client),pytest.raises(ValueError,match=match):
        list(image_stream(remote,'init_boot',{}))
