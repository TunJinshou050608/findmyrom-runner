"""Public channel keyword search with an independent, durable pagination state."""
import hashlib
import json
from urllib.parse import urlencode,urljoin,urlsplit,parse_qs
from .html_parser import parse_tg_channel_page,channel_name,DOM


def search_page(fetch,channel,query,state=None):
    channel=channel_name(channel);state=dict(state or {})
    if not isinstance(query,str) or not 1<=len(query)<=128:raise ValueError('Invalid query')
    params={'q':query}
    if state.get('before'):params['before']=state['before']
    url='https://t.me/s/'+channel+'?'+urlencode(params)
    html=fetch(url);page=parse_tg_channel_page(html,channel)
    fingerprint=hashlib.sha256(json.dumps(sorted(m['msg_id'] for m in page['messages'])).encode()).hexdigest()
    dom=DOM();dom.feed(html);next_before=[];query_lost=False
    for node in dom.root.walk():
        if node.tag!='a' or not node.has('tme_messages_more'):continue
        link=urlsplit(urljoin(url,node.attrs.get('href','')));q=parse_qs(link.query)
        if link.scheme!='https' or link.netloc!='t.me' or link.path!='/s/'+channel:continue
        before=q.get('before',[])
        if len(before)!=1 or not before[0].isdigit():continue
        if q.get('q')!=[query]:query_lost=True;continue
        if int(before[0])>0:next_before.append(int(before[0]))
    reason=None
    if state.get('fingerprint')==fingerprint and state.get('before'):reason='repeated_search_page'
    elif query_lost and not next_before:reason='pagination_lost_query'
    elif next_before and state.get('before') and min(next_before)>=state['before']:reason='pagination_did_not_advance'
    anchor=state.get('anchor',0)
    reached_anchor=bool(anchor and (not page['messages'] or (page['min_msg_id'] or 0)<=anchor))
    more=bool(next_before) and not reason and not reached_anchor
    new=dict(state,fingerprint=fingerprint,highest=max(state.get('highest',0),page['max_msg_id'] or 0))
    if more:new['before']=min(next_before)
    else:new.update(before=None,anchor=new['highest'])
    return {'messages':page['messages'],'state':new,'has_more':more,
            'status':reason or ('more' if more else 'search_exhausted'),
            'source_url':url,'page_min':page['min_msg_id'],'page_max':page['max_msg_id']}
