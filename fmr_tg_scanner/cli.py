"""TG01 CLI: defaults to independent local state, never production D1."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

from .storage import SQLiteDB
from .crawler import scan_channel,ChannelUnavailableError
from .bridge import export_to_custom_resources


def export_jsonl(db,channel,path):
    rows=db.query('SELECT * FROM tg_discovered_packages WHERE channel_username=? ORDER BY msg_id',(channel,))
    # SQLite row IDs are storage details; stable archive contains only contract fields.
    data=''.join(json.dumps({k:v for k,v in r.items() if k!='id'},ensure_ascii=False,sort_keys=True)+'\n' for r in rows).encode()
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists() and hashlib.sha256(path.read_bytes()).digest()==hashlib.sha256(data).digest():return False
    fd,name=tempfile.mkstemp(prefix='.tg-jsonl-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as out:out.write(data)
        os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)
    return True


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--channel',required=True);parser.add_argument('--default-codename')
    parser.add_argument('--max-pages',type=int,default=50);parser.add_argument('--direction',choices=['backfill','incremental','both'],default='both')
    parser.add_argument('--no-llm',action='store_true');parser.add_argument('--max-llm-calls',type=int,default=20)
    parser.add_argument('--offline-pages');parser.add_argument('--export-jsonl');parser.add_argument('--sync-to-sp07',action='store_true')
    args=parser.parse_args(argv)
    if not 1<=args.max_pages<=5000 or not 0<=args.max_llm_calls<=500:parser.error('Page/call limit out of range')
    db=None
    try:
        # Explicit local default avoids accidentally consuming a configured D1 account.
        local=os.environ.get('LOCAL_SQLITE_PATH','tg01-state.sqlite')
        db=SQLiteDB(local)
        fetcher=None
        if args.offline_pages:
            paths=iter(args.offline_pages.split(','))
            def offline(_url):
                try:return Path(next(paths)).read_text()
                except StopIteration:return '<section class="tgme_widget_history"></section>'
            fetcher=offline
        stats=scan_channel(db,args.channel,args.default_codename,args.max_pages,args.direction,not args.no_llm,args.max_llm_calls,fetcher,
            on_page=lambda s:print(json.dumps({'event':'page_committed',**s},sort_keys=True),file=sys.stderr,flush=True))
        if args.export_jsonl:export_jsonl(db,stats['channel'],args.export_jsonl)
        if args.sync_to_sp07:export_to_custom_resources(db)
        print(json.dumps(stats,sort_keys=True));return 0
    except Exception as exc:
        print(f'Telegram scan stopped ({type(exc).__name__}); committed pages retained.',file=sys.stderr);return 1
    finally:
        if db:db.close()


if __name__=='__main__':sys.exit(main())
