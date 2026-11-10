"""Separate physical products from shared upstream archive families.

Only aliases attested by the upstream device dictionary and package names are
mapped. Region suffixes and hardware editions must not create new devices.
"""
import re

FAMILIES={'emeraldr':'emerald','shennongt':'shennong'}
NAMES={'emeraldr':'Redmi Note 14S','shennongt':'Xiaomi 14 Pro Ti Satellite'}

def source_model(value):
    value=str(value).lower()
    for raw,public in (('emerald_r','emeraldr'),('shennong_t','shennongt')):
        if value==raw or value.startswith(raw+'_'):return public+value[len(raw):]
    return value

def base_code(value):
    return source_model(value).split('_',1)[0]

def archive_route(code,route=None):
    return FAMILIES.get(code,route or code)

def physical_code(code,name):
    family=FAMILIES.get(code,code)
    if family=='emerald':
        special=bool(re.match(r'^emerald_r(?:_|-)',name,re.I) or re.match(r'^miui_EMERALDR(?:Global|EEAGlobal|RUGlobal|TRGlobal)_',name,re.I))
        return 'emeraldr' if special else 'emerald'
    if family=='shennong':
        special=bool(re.match(r'^shennong_t(?:_|-)',name,re.I) or re.match(r'^miui_SHENNONGT_',name,re.I))
        return 'shennongt' if special else 'shennong'
    return code
