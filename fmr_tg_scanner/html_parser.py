"""Bounded stdlib DOM parser; no browser, account login, or file downloads."""
from datetime import datetime
from html.parser import HTMLParser
import re
from urllib.parse import parse_qs, urlsplit


class ChannelUnavailableError(RuntimeError): pass


def channel_name(value):
    value=value.lstrip('@').lower()
    if not re.fullmatch(r'[a-z][a-z0-9_]{3,31}',value):
        raise ValueError('Invalid public channel username')
    return value


class Node:
    def __init__(self,tag='',attrs=()):
        self.tag=tag;self.attrs=dict(attrs);self.children=[]
    def has(self,name):return name in self.attrs.get('class','').split()
    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child,Node):yield from child.walk()
    def text(self):
        return ''.join(c.text() if isinstance(c,Node) else c for c in self.children)


class DOM(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True);self.root=Node();self.stack=[self.root]
    def handle_starttag(self,tag,attrs):
        node=Node(tag,attrs);self.stack[-1].children.append(node)
        if tag=='br':node.children.append('\n')
        if tag not in {'br','img','hr','meta','link','input','source','wbr','area','base','embed','param','track','col'}:
            if len(self.stack)>128:raise ValueError('HTML nesting limit')
            self.stack.append(node)
    def handle_startendtag(self,tag,attrs):self.handle_starttag(tag,attrs);self.handle_endtag(tag)
    def handle_endtag(self,tag):
        for pos in range(len(self.stack)-1,0,-1):
            if self.stack[pos].tag==tag:
                del self.stack[pos:];break
    def handle_data(self,data):self.stack[-1].children.append(data)


def parse_tg_channel_page(html_text: str, channel_username: str) -> dict:
    channel=channel_name(channel_username)
    if len(html_text.encode('utf-8'))>4*1024*1024:raise ValueError('Preview page exceeds limit')
    dom=DOM();dom.feed(html_text)
    if not any(n.has('tgme_widget_history') for n in dom.root.walk()):
        raise ChannelUnavailableError('Public channel preview unavailable')
    messages={};before=[]
    for node in dom.root.walk():
        if node.has('tgme_widget_message'):
            match=re.fullmatch(r'([A-Za-z0-9_]+)/([1-9][0-9]*)',node.attrs.get('data-post',''))
            if not match or match[1].lower()!=channel:continue
            children=list(node.walk());text=next((n.text() for n in children if n.has('tgme_widget_message_text')),'')[:65536]
            forward=next((n for n in children if n.has('tgme_widget_message_forwarded_from_name')),None)
            author=next((n.text().strip() for n in children if n.has('tgme_widget_message_author')),None)
            title=next((n.text().strip() for n in children if n.has('tgme_widget_message_document_title')),None)
            links=[n.attrs['href'] for n in children if n.tag=='a' and 'href' in n.attrs and not n.has('tgme_widget_message_date')]
            stamp=None
            try:
                date=next((n.attrs.get('datetime') for n in children if n.tag=='time'),None)
                if date:stamp=int(datetime.fromisoformat(date.replace('Z','+00:00')).timestamp())
            except (ValueError,OverflowError):pass
            mid=int(match[2])
            messages[mid]={'channel_username':channel,'msg_id':mid,'tg_post_url':f'https://t.me/{channel}/{mid}',
                'text':text,'links':links,'forwarded_from_url':forward.attrs.get('href') if forward else None,
                'has_forward_header':forward is not None,'sender_author_signature':author,
                'has_tg_attachment':bool(title),'file_attachment_name':title,'published_timestamp':stamp}
        if node.tag=='a' and node.has('tme_messages_more'):
            url=urlsplit(node.attrs.get('href',''));values=parse_qs(url.query).get('before',[])
            if values and values[0].isdigit():before.append(int(values[0]))
    ids=sorted(messages)
    return {'messages':[messages[i] for i in reversed(ids)],'min_msg_id':min(ids) if ids else None,
            'max_msg_id':max(ids) if ids else None,'prev_before_id':min(before) if before else None}
