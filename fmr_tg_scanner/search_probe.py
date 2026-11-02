"""Finite TG-specific capability probes; output stays in the private lab repo."""
import argparse
import json
from pathlib import Path
import re
import time
from urllib.parse import urlsplit,urljoin,urlencode
import httpx
from .discovery import channel_links,bounded_get
from .html_parser import DOM,parse_tg_channel_page
from .trial import atomic_json


def probe(root,targets):
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    observations=[]
    with httpx.Client(timeout=15,follow_redirects=False,headers={'User-Agent':'Mozilla/5.0'}) as client:
        for provider,base in [('lyzem','https://lyzem.com'),('chatcrawler','https://chatcrawler.link')]:
            try:
                home=bounded_get(client,base+'/');dom=DOM();dom.feed(home)
                forms=[]
                for node in dom.root.walk():
                    if node.tag=='form':
                        names=[n.attrs.get('name') for n in node.walk() if n.tag=='input']
                        forms.append({'action':node.attrs.get('action',''),'method':node.attrs.get('method','GET'),'inputs':names})
                observations.append({'provider':provider,'stage':'form','forms':forms})
                routes=[f for f in forms if str(f['method']).upper()=='GET' and any(n in ('q','query','search') for n in f['inputs'])]
                if provider=='lyzem' and not routes:routes=[{'action':'/search','inputs':['q']}]
                for form in routes[:1]:
                    action=urljoin(base+'/',form['action']);field=next(n for n in form['inputs'] if n in ('q','query','search'))
                    if urlsplit(action).hostname!=urlsplit(base).hostname:continue
                    for target in [*targets,'fmr_no_such_device_qzx284760']:
                        start=time.monotonic();html=bounded_get(client,action,params={field:target})
                        candidates=channel_links(html)
                        messages=[v for v in candidates if re.search(r'/[1-9][0-9]*$',v['evidence_url'])]
                        observations.append({'provider':provider,'stage':'search','query':target,'elapsed_ms':round((time.monotonic()-start)*1000),'messages':messages,'channels':len(candidates)})
            except Exception as exc:observations.append({'provider':provider,'error_type':type(exc).__name__})
    atomic_json(root/'capabilities.json',{'observations':observations,'D1_requests':0,'firmware_downloaded':False})
    print(json.dumps({'event':'capability_probe_finished','provider_observations':len(observations),'message_candidates':sum(len(v.get('messages',[])) for v in observations)}),flush=True)
    return observations


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--state-dir',required=True);parser.add_argument('--targets',default=None);args=parser.parse_args()
    from .targets import read_catalogue, select_targets
    targets=args.targets.split(',') if args.targets else select_targets(read_catalogue()[0])
    if not all(re.fullmatch('[a-z0-9_-]{3,32}',v) for v in targets):parser.error('Invalid target')
    probe(Path(args.state_dir)/'experiments'/'search-v3-probe',targets)


if __name__=='__main__':main()
