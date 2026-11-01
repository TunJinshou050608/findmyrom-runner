import httpx
import pytest
from fmr_ingest_stock.samsung_fus import SmartFUS,normalize_version


def test_smart_fus_confirms_exact_binary_metadata_without_requesting_a_binary():
    calls=[]
    def handler(req):
        calls.append(req)
        assert req.method=='POST' and req.url.host=='neofussvr.sslcs.cdngc.net'
        if req.url.path.endswith('GenerateNonce.do'):
            return httpx.Response(200,headers={'nonce':'abcdefghijklmnop'},content=b'')
        assert req.url.path=='/NF_SmartDownloadBinaryInform.do'
        assert 'signature=""' not in req.headers['Authorization']
        assert b'IMEI' not in req.content and b'PDA/CSC/PHONE/PDA' in req.content
        return httpx.Response(200,headers={'Content-Type':'application/xml'},content=b'''<FUSMsg><FUSBody><Results><Status>S00</Status></Results><Put>
        <BINARY_SW_VERSION><Data>PDA/CSC/PHONE/PDA</Data></BINARY_SW_VERSION>
        <BINARY_NAME><Data>SM-S000B_package.zip.enc4</Data></BINARY_NAME>
        <MODEL_PATH><Data>/neofus/9/</Data></MODEL_PATH>
        <BINARY_BYTE_SIZE><Data>123456789</Data></BINARY_BYTE_SIZE>
        </Put></FUSBody></FUSMsg>''')
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        fus=SmartFUS(client)
        result=fus.binary_info('SM-S000B','EUX','PDA/CSC/PHONE')
        assert result['resolution']=='fus_metadata_only' and result['size']==123456789
        with pytest.raises(ValueError):fus.request('NF_SmartDownloadBinaryForMass.do')
    assert len(calls)==2


def test_fus_rejects_silent_version_substitution_and_never_reads_binary_content():
    class Forbidden(httpx.SyncByteStream):
        def __iter__(self):raise AssertionError('firmware consumed');yield b''
    def handler(req):
        return httpx.Response(200,headers={'Content-Type':'application/zip'},stream=Forbidden())
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError):SmartFUS(client).request('NF_SmartDownloadBinaryInform.do')
    assert normalize_version('PDA/CSC//PDA')=='PDA/CSC/PDA/PDA'
    with pytest.raises(ValueError):normalize_version('<not-xml>')


def test_fus_response_for_a_different_build_is_rejected():
    def handler(req):
        if req.url.path.endswith('GenerateNonce.do'):return httpx.Response(200,headers={'nonce':'abcdefghijklmnop'},content=b'')
        return httpx.Response(200,headers={'Content-Type':'text/xml'},content=b'<FUSMsg><FUSBody><Results><Status>S00</Status></Results><Put><BINARY_SW_VERSION><Data>OTHER/CSC/PHONE/OTHER</Data></BINARY_SW_VERSION></Put></FUSBody></FUSMsg>')
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError,match='different build'):SmartFUS(client).binary_info('SM-S000B','EUX','PDA/CSC/PHONE')
