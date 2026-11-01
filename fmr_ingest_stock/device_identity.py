"""Display names contain product identities, never catalogue counts or dates."""
import re

MARKETS=r'(?:China|Global|EEA|Europe|India|Indonesia|Russia|Turkey|Taiwan|Japan|Singapore|Hong Kong|South Korea|Korea|Thailand|Vietnam|Malaysia)'
MARKET_SUFFIX=re.compile(r'(?:\s*[(\[]\s*|\s+|\s*[·,–—]\s*)'+MARKETS+r'(?:\s*[/,]\s*'+MARKETS+r')*(?:\s+(?:ROM|Version|Edition))?\s*[)\]]?$',re.I)
HARDWARE_CODE=re.compile(r'\b(?:CPH\d{4,5}|RMX\d{4,5}|SM-[A-Z0-9]+|PD\d{4}[A-Z]*|P[A-Z]{2}\d{3}|AI\d{4}[A-Z]?|ZS\d{3}[A-Z]{2})\b',re.I)


def clean_model_name(value):
    name=str(value).strip()
    while MARKET_SUFFIX.search(name):name=MARKET_SUFFIX.sub('',name).strip()
    return name


def marketing_name(device):
    vendor=device['vendor']
    if vendor=='motorola' and (device.get('canonical_code')=='motorola-aito25' or device.get('codename')=='aito25' or device['upstream_id']=='aito25'):
        return 'Motorola Razr 60 / Razr 2025'
    name=str(device.get('name') or device['upstream_id']).strip()
    name=re.sub(r'\s+\d+\s+(?:files?|firmwares?|versions?|builds?)\b.*$', '',name,flags=re.I)
    name=re.sub(r'^"[^"]+"\s+for\s+', '',name)
    if vendor=='google':
        name=re.sub(r'\s*["“][a-z0-9_-]{2,32}["”]\s*',' ',name,flags=re.I)
        name=re.sub(r'\s*\(with Google Wallet\)','',name,flags=re.I)
    name=clean_model_name(name)
    models=[m.strip() for m in str(device.get('models') or '').split(',') if m.strip()]
    for model in sorted(models,key=len,reverse=True):
        if name.lower()==model.lower():continue
        name=re.sub(r'\(\s*'+re.escape(model)+r'\s*\)',' ',name,flags=re.I)
        name=re.sub(r'(?:\s|,)'+re.escape(model)+r'(?=\s|,|$)',' ',name,flags=re.I)
    name=HARDWARE_CODE.sub(lambda match:match[0] if name.lower()==match[0].lower() else ' ',name)
    if vendor in ('asus','rog'):
        name=re.sub(r'\s+(?:AI\d{4}[A-Z]?|ZS\d{3}[A-Z]{2})\b.*$','',name,flags=re.I)
        name=re.sub(r'^ROG\s+(?=\d)','ROG Phone ',name,flags=re.I)
    name=re.sub(r'\(\s*\)','',name)
    name=re.sub(r'\s+',' ',name.strip(' ,'))
    prefixes={'google':('Google',r'^(?:Pixel|Nexus)\b'),'samsung':('Samsung',r'^Galaxy\b'),
              'sony':('Sony',r'^Xperia\b'),'nothing':('Nothing',r'^Phone\b'),
              'motorola':('Motorola',r'^(?:Edge|Razr|Moto\b)')}
    if vendor in prefixes:
        prefix,pattern=prefixes[vendor]
        if re.search(pattern,name,re.I):name=prefix+' '+name
    if vendor=='asus' and not re.match(r'^(?:ASUS|ROG)\b',name,re.I):name='ASUS '+name
    for pattern,value in [(r'^Realme\b','realme'),(r'^Oppo\b','OPPO'),(r'^Vivo\b','vivo')]:
        name=re.sub(pattern,value,name,flags=re.I)
    return clean_model_name(name)
