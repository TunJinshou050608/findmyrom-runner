"""Cloud-only full Sony firmware ZIP assembly. Never extracts SIN/boot images."""
import hashlib,hmac,json,os,re,shutil,tempfile,time,zipfile
from pathlib import Path
from urllib.parse import urlsplit,urljoin
import httpx
from fmr_core.d1_client import D1Client
from .d1_budget import NOT_BEFORE
from .sony_source import validate_manifest,manifest_digest

PART_SIZE=50*1024**2
MAX_ARCHIVE=8*1024**3+1024**2


def digest_file(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(1024**2),b''):h.update(block)
    return h.hexdigest()


def download_chunk(client,url,path,size):
    from .oem_sources import download_origin
    path=Path(path);marker=path.with_suffix('.sha256')
    if path.exists() and marker.exists() and path.stat().st_size==size and digest_file(path)==marker.read_text().strip():return
    temporary=path.with_suffix('.partial')
    for attempt in range(3):
        offset=temporary.stat().st_size if temporary.exists() else 0
        if offset>size:temporary.unlink();offset=0
        if offset==size:break
        current=url
        try:
            for _ in range(5):
                if download_origin('sony',current)!='oem':raise ValueError('Untrusted Sony download')
                with client.stream('GET',current,headers={'Accept-Encoding':'identity',**({'Range':f'bytes={offset}-{size-1}'} if offset else {})}) as response:
                    if response.status_code in (301,302,303,307,308):
                        current=urljoin(current,response.headers.get('Location',''));continue
                    response.raise_for_status()
                    if response.status_code not in (200,206):raise ValueError('Invalid Sony download response')
                    if response.status_code==206:
                        match=re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)',response.headers.get('Content-Range',''))
                        if not match or tuple(map(int,match.groups()))!=(offset,size-1,size):raise ValueError('Invalid Sony range')
                    elif offset:offset=0
                    written=offset
                    with temporary.open('ab' if offset else 'wb') as output:
                        for block in response.iter_bytes(65536):
                            written+=len(block)
                            if written>size:raise ValueError('Sony chunk exceeds declared size')
                            output.write(block)
                    if written!=size:raise ValueError('Truncated Sony chunk')
                    break
            else:raise ValueError('Too many Sony redirects')
            break
        except (httpx.HTTPError,ValueError):
            if attempt==2:raise
            time.sleep(attempt+1)
    if not temporary.exists() or temporary.stat().st_size!=size:raise ValueError('Incomplete Sony chunk')
    os.replace(temporary,path);marker.write_text(digest_file(path))


def assemble(manifest,directory,client,part_size=PART_SIZE):
    total=validate_manifest(manifest);directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    target=directory/'Sony-firmware.zip';complete=directory/'archive-checkpoint.json'
    if target.exists() and complete.exists():
        state=json.loads(complete.read_text())
        if state.get('manifest')==manifest_digest(manifest) and state.get('sha256')==digest_file(target):return target
    if target.exists():target.unlink()
    present=sum(p.stat().st_size for p in directory.glob('*.part') if p.is_file())
    if shutil.disk_usage(directory).free+present<total+256*1024**2:raise ValueError('Insufficient cloud runner disk space')
    components=[]
    for i,component in enumerate(sorted(manifest['files'],key=lambda f:f['name']),1):
        paths=[]
        for chunk in component['chunks']:
            path=directory/(hashlib.sha256((component['name']+':'+str(chunk['number'])).encode()).hexdigest()+'.part')
            download_chunk(client,chunk['url'],path,chunk['size']);paths.append(path)
        components.append((component,paths))
        print(f'Cloud download: component {i}/{len(manifest["files"])} complete; transport chunks remain internal.',flush=True)
    target=directory/'Sony-firmware.zip'
    with zipfile.ZipFile(target,'w',compression=zipfile.ZIP_STORED,allowZip64=True) as archive:
        for component,paths in components:
            info=zipfile.ZipInfo(component['name'],date_time=(1980,1,1,0,0,0));info.file_size=component['size']
            with archive.open(info,'w',force_zip64=True) as dest:
                for path in paths:
                    with path.open('rb') as source:shutil.copyfileobj(source,dest,1024**2)
                    path.unlink();path.with_suffix('.sha256').unlink(missing_ok=True)
    if target.stat().st_size>MAX_ARCHIVE:raise ValueError('Sony archive exceeds temporary cache budget')
    with zipfile.ZipFile(target) as archive:
        if archive.testzip() is not None:raise ValueError('Sony ZIP CRC validation failed')
    complete.write_text(json.dumps({'manifest':manifest_digest(manifest),'sha256':digest_file(target)}))
    return target


class Uploader:
    def __init__(self,client,secret,base='https://findmyrom.com'):
        self.client,self.secret,self.base=client,secret,base
    def request(self,key,action,archive_sha='0'*64,size=0,upload_id='',part='',body=b''):
        exp=int(time.time())+600
        payload=f'v1|sony|{key}|{action}|{upload_id}|{part}|{archive_sha}|{size}'
        sig=hmac.new(self.secret.encode(),f'token={payload}&exp={exp}'.encode(),hashlib.sha256).hexdigest()
        response=self.client.post(self.base+'/internal/sony-builder/'+key+'/'+action,content=body,
            headers={'X-FMR-Expires':str(exp),'X-FMR-Signature':sig,'X-FMR-Upload-Id':upload_id,
                'X-FMR-Part':str(part),'X-FMR-Archive-SHA256':archive_sha,'X-FMR-Archive-Size':str(size),
                'Content-Length':str(len(body))})
        response.raise_for_status();return response.json()


def save(db,kind,key,value):
    db.execute('''INSERT INTO crawler_sources(device_codename,source_kind,source_target,last_cursor_val,last_checked_at)
        VALUES ('__global__',?,?,?,?) ON CONFLICT(device_codename,source_kind,source_target)
        DO UPDATE SET last_cursor_val=excluded.last_cursor_val,last_checked_at=excluded.last_checked_at''',
        (kind,key,json.dumps(value,sort_keys=True,separators=(',',':')),int(time.time())))


def load(db,kind,key):
    rows=db.query("SELECT last_cursor_val FROM crawler_sources WHERE device_codename='__global__' AND source_kind=? AND source_target=?",(kind,key))
    return json.loads(rows[0]['last_cursor_val']) if rows else None


def main():
    if os.environ.get('GITHUB_ACTIONS')!='true':raise ValueError('Real Sony downloads are cloud-only')
    if time.time()<NOT_BEFORE:print('D1 quota pause: no downloads or database requests.');return 0
    secret=os.environ.get('SONY_BUNDLE_UPLOAD_SECRET')
    if not secret:print('Complete-bundle upload service is not configured; no firmware was downloaded.');return 0
    db=None;current=None;key=None;uploader=None;upload=None;archive_sha='0'*64;size=0
    try:
        with httpx.Client(timeout=120,follow_redirects=False) as client:
            uploader=Uploader(client,secret)
            uploader.request('0'*64,'preflight') # Storage must exist BEFORE reading D1 or downloading ROMs.
            db=D1Client(cf_account_id=os.environ['CF_ACCOUNT_ID'],cf_api_token=os.environ['CF_API_TOKEN'],cf_database_id=os.environ['CF_D1_DATABASE_ID'])
            day=time.strftime('%Y-%m-%d',time.gmtime());daily=load(db,'sony_bundle_control',day) or {'builds':0}
            if daily['builds']>=2:print('Daily full-bundle build budget reached; pending requests are retained.');return 0
            # Abort any previous interrupted upload before allocating the single cache slot.
            active=load(db,'sony_bundle_control','active')
            if active:
                try:uploader.request(active['key'],'abort',active['sha256'],active['size'],active['upload_id'])
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code not in (404,409):raise
                save(db,'sony_bundle_control','active',None)
            rows=db.query("""SELECT source_target,last_cursor_val FROM crawler_sources WHERE device_codename='__global__' AND source_kind='sony_bundle_job'
                AND (json_extract(last_cursor_val,'$.state')='pending' OR (json_extract(last_cursor_val,'$.state') IN ('processing','uploading') AND last_checked_at<?))
                ORDER BY last_checked_at LIMIT 1""",(int(time.time())-3600,))
            if not rows:print('No complete firmware bundle is currently requested.');return 0
            key=rows[0]['source_target'];current=json.loads(rows[0]['last_cursor_val'])
            record=load(db,'sony_bundle',key);manifest=record['manifest']
            if manifest_digest(manifest)!=key:raise ValueError('Sony manifest integrity mismatch')
            validate_manifest(manifest);daily['builds']+=1;save(db,'sony_bundle_control',day,daily)
            current.update(state='processing',checked_at=int(time.time()));save(db,'sony_bundle_job',key,current)
            with tempfile.TemporaryDirectory(prefix='sony-complete-firmware-') as directory:
                archive=assemble(manifest,directory,client);archive_sha=digest_file(archive);size=archive.stat().st_size
                upload=uploader.request(key,'create',archive_sha,size)['upload_id']
                save(db,'sony_bundle_control','active',{'key':key,'sha256':archive_sha,'size':size,'upload_id':upload})
                parts=[]
                with archive.open('rb') as source:
                    while block:=source.read(PART_SIZE):
                        result=uploader.request(key,'part',archive_sha,size,upload,len(parts)+1,block)
                        parts.append(result)
                        print(f'Complete ZIP upload: {len(parts)} parts uploaded internally.',flush=True)
                uploader.request(key,'complete',archive_sha,size,upload,body=json.dumps(parts).encode())
                upload=None;save(db,'sony_bundle_control','active',None)
                current.update(state='ready',checked_at=int(time.time()),sha256=archive_sha,size=size)
                save(db,'sony_bundle_job',key,current)
                print('ONE complete Sony firmware ZIP is ready on the site. No partition images were extracted.',flush=True)
        return 0
    except Exception as exc:
        print('Sony bundle not completed: '+type(exc).__name__+'; checkpoints remain private.',flush=True)
        if uploader and upload:
            try:uploader.request(key,'abort',archive_sha,size,upload)
            except Exception:pass
        if db and current and key:
            current.update(state='failed',checked_at=int(time.time()),error=type(exc).__name__)
            try:save(db,'sony_bundle_job',key,current)
            except Exception:pass
        return 1
    finally:
        if db:db.close()


if __name__=='__main__':raise SystemExit(main())
