"""Explicit archive editions attested by upstream model and package names."""
import hashlib
import re
from urllib.parse import unquote,urlsplit

LABELS={'cmonly':'China Mobile edition','lte-ct':'China Telecom LTE edition',
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

def version_key(version,variant):
    if not variant:return version
    if variant not in LABELS:raise ValueError('Unknown Xiaomi archive edition')
    value=version+'__'+variant
    return value if len(value)<=64 else value[:49]+'-'+hashlib.sha256(value.encode()).hexdigest()[:12]

def model_key(code,variant):
    if variant not in LABELS:raise ValueError('Unknown Xiaomi archive edition')
    return code+'__'+variant
