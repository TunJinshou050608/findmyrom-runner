"""Explicit archive editions attested by upstream model and package names."""
import hashlib
import re
from urllib.parse import unquote,urlsplit

LABELS={'standard':'Standard edition','cmonly':'China Mobile edition','lte-ct':'China Telecom LTE edition',
        'china-telecom':'China Telecom edition','pro':'Pro edition','48mp':'48 MP camera edition',
        'td-scdma':'TD-SCDMA edition','c3l2':'Redmi 10A (C3L2) edition',
        'poco-m5s':'POCO M5s edition','k6a':'Redmi Note 12 Pro 4G (K6A) edition',
        'rf':'RF hardware edition','fk':'FK hardware edition'}
RULES={
 'hermes':[('cmonly',r'_cmonly_|^miui_HMNote2CM_')],
 'cancro':[('lte-ct',r'_lte_ct_|^miui_MI4LTECT_')],
 'virgo':[('lte-ct',r'_lte_ct_|^miui_MINoteCT_')],
 'umi':[('china-telecom',r'_chinatelecom_')],
 'wt86047':[('pro',r'^wt86047_pro_|^miui_HM2XTDPro(?:Global)?_')],
 'wt88047':[('pro',r'^wt88047_pro_|^miui_HM2XWCPro(?:Global)?_')],
 'picasso':[('48mp',r'^picasso_48m_|^miui_PICASSO48M(?:PRE)?_')],
 'lcsh92':[('td-scdma',r'^lcsh92_wet_(?:xm_td|tdd)_|^miui_HMNoteTD_')],
 'dandelion':[('c3l2',r'^dandelion_c3l2_|^miui_DANDELIONC3L2(?:(?:EEA|IN|ID|RU|TR|TW|JP)?Global)?_')],
 'rosemary':[('poco-m5s',r'^rosemary_p_|^miui_ROSEMARYP(?:(?:EEA|IN|ID|RU|TR|TW|JP)?Global)?_')],
 'sweet':[('k6a',r'^sweet_k6a_|^miui_SWEETK6A(?:(?:EEA|IN|ID|RU|TR|TW|JP)?Global)?_')],
 'curtana':[('rf',r'^curtana_in_rf_|^miui_CURTANAINRFGlobal_')],
 'angelicain':[('rf',r'^angelicain_in_rf_|^miui_ANGELICAININRFGlobal_')],
 'pissarro':[('fk',r'^pissarro_in_fk_|^miui_PISSARROINFKGlobal_')],
}

def filename(url):return unquote(urlsplit(url).path).rsplit('/',1)[-1]

def edition(code,name):
    return next((variant for variant,pattern in RULES.get(code,[]) if re.search(pattern,name,re.I)),None)

def engineering(name):return bool(re.search(r'\.debug|[-_]userroot[-_]|[-_]userdebug[-_]|[-_]eng[-_]',name,re.I))

def android_version(name):
    patterns=(r'_(\d{1,2}\.\d{1,2})\.zip$',
              r'_(\d{1,2}\.\d{1,2})_(?:[A-Za-z0-9]+_)*[0-9a-f]{10}\.(?:tgz|tar\.gz)$',
              r'-(\d{1,2}\.\d{1,2})-(?:[a-z]+-)?[0-9a-f]{10}\.(?:zip|tgz|tar\.gz)$')
    for pattern in patterns:
        match=re.search(pattern,name,re.I)
        if match:return match[1]
    return None

def canonical_version(version,name):
    match=re.search(r'_images_([^_]+)_',name,re.I) or re.match(r'^miui_[^_]+_([^_]+)_[0-9a-f]{10}_',name,re.I) or re.search(r'-images-([^-]+)-(?:user|userroot|userdebug|eng)-',name,re.I)
    candidate=match[1] if match else version
    return candidate if re.fullmatch(r'\d{1,2}\.\d{1,2}\.\d{1,2}',version) and re.fullmatch(re.escape(version)+r'\.\d{1,2}',candidate) else version

def platform_parts(variant):
    match=re.fullmatch(r'(?:(.+)--)?android-(\d{1,2})-(\d{1,2})',variant or '')
    if not match or (match[1] and match[1] not in LABELS):return None
    return match[1],match[2]+'.'+match[3]

def label(variant,code=None):
    if variant in LABELS:
        return {('curtana','rf'):'Redmi Note 10 Lite edition',('angelicain','rf'):'POCO C31 edition',('pissarro','fk'):'Xiaomi 11i 5G edition'}.get((code,variant),LABELS[variant])
    parts=platform_parts(variant)
    if not parts:raise ValueError('Unknown Xiaomi archive edition')
    base,android=parts
    return (label(base,code)+' · ' if base else '')+'Android '+android+' archive'

def matches(code,name,variant):
    if variant=='standard':return edition(code,name) is None
    parts=platform_parts(variant)
    return (edition(code,name)==parts[0] and android_version(name)==parts[1]) if parts else edition(code,name)==variant

def annotate_platforms(rows):
    from .firmware_sync import normalize
    groups={}
    for row in rows:
        found=android_version(filename(row['link']))
        if found:row['android']=found
        normalized=normalize(row,'xiaomi')
        key=(normalized['device_codename'],normalized['region_code'],row['version'],row.get('variant_id'))
        groups.setdefault(key,[]).append(row)
    for key,items in groups.items():
        versions={r.get('android') for r in items if re.fullmatch(r'\d{1,2}\.\d{1,2}',str(r.get('android') or ''))}
        if len(versions)<2:continue
        base=key[3]
        for row in items:
            android=str(row.get('android') or '')
            if android not in versions:continue
            variant=(base+'--' if base else '')+'android-'+android.replace('.','-')
            row.update(variant_id=variant,variant_label=label(variant,key[0]))
    return rows

def version_key(version,variant):
    if not variant:return version
    if variant not in LABELS and not platform_parts(variant):raise ValueError('Unknown Xiaomi archive edition')
    value=version+'__'+variant
    return value if len(value)<=64 else value[:49]+'-'+hashlib.sha256(value.encode()).hexdigest()[:12]

def model_key(code,variant):
    if variant not in LABELS and not platform_parts(variant):raise ValueError('Unknown Xiaomi archive edition')
    return code+'__'+variant
