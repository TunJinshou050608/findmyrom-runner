"""Explicit archive editions; these are not new physical device identities."""
import hashlib
import re
from urllib.parse import unquote,urlsplit

LABELS={'cmonly':'China Mobile edition','lte-ct':'China Telecom LTE edition',
        'china-telecom':'China Telecom edition','pro':'Pro edition','48mp':'48 MP camera edition'}

def filename(url):return unquote(urlsplit(url).path).rsplit('/',1)[-1]

def edition(code,name):
    if code=='hermes' and '_cmonly_' in name:return 'cmonly'
    if code=='cancro' and ('_lte_ct_' in name or re.match(r'^miui_MI4LTECT_',name,re.I)):return 'lte-ct'
    if code=='umi' and '_chinatelecom_' in name:return 'china-telecom'
    if code=='wt88047' and re.match(r'^miui_HM2XWCPro(?:Global)?_',name,re.I):return 'pro'
    if code=='picasso' and re.match(r'^miui_PICASSO48M(?:PRE)?_',name,re.I):return '48mp'
    return None

def engineering(name):return bool(re.search(r'\.debug|[-_]userroot[-_]|[-_]userdebug[-_]|[-_]eng[-_]',name,re.I))

def version_key(version,variant):
    if not variant:return version
    if variant not in LABELS:raise ValueError('Unknown Xiaomi archive edition')
    value=version+'__'+variant
    return value if len(value)<=64 else value[:49]+'-'+hashlib.sha256(value.encode()).hexdigest()[:12]

def model_key(code,variant):
    if variant not in LABELS:raise ValueError('Unknown Xiaomi archive edition')
    return code+'__'+variant
