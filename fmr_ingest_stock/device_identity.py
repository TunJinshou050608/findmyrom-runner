"""Display names contain product identities, never catalogue counts or dates."""
import re


def marketing_name(device):
    vendor=device['vendor']
    if vendor=='motorola' and (device.get('canonical_code')=='motorola-aito25' or device.get('codename')=='aito25' or device['upstream_id']=='aito25'):
        return 'Motorola Razr 60 / Razr 2025'
    name=str(device.get('name') or device['upstream_id']).strip()
    name=re.sub(r'\s+\d+\s+(?:files?|firmwares?|versions?|builds?)\b.*$', '',name,flags=re.I)
    name=re.sub(r'^"[^"]+"\s+for\s+', '',name)
    models=[m.strip() for m in str(device.get('models') or '').split(',') if m.strip()]
    for model in sorted(models,key=len,reverse=True):
        if name.lower()==model.lower():continue
        name=re.sub(r'\(\s*'+re.escape(model)+r'\s*\)',' ',name,flags=re.I)
        name=re.sub(r'(?:\s|,)'+re.escape(model)+r'(?=\s|,|$)',' ',name,flags=re.I)
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
    return name
