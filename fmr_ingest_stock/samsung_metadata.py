"""Official model/CSC update notices through the existing bounded metadata cache."""
import re
from urllib.parse import urljoin, urlsplit
from bs4 import BeautifulSoup
from .release_metadata import iso_date


def parse(page, model, region, source):
    soup = BeautifulSoup(page, 'html.parser')
    text = soup.get_text(' ', strip=True)
    result = {}
    for block in re.split(r'Build Number\s*:\s*', text)[1:]:
        ap = re.match(r'[A-Z0-9]+', block)
        android = re.search(r'Android\s+(\d+(?:\.\d+)*)', block)
        date = re.search(r'Release Date\s*:\s*(\d{4}-\d{2}-\d{2})', block)
        patch = re.search(r'Security patch level\s*:\s*(\d{4}-\d{2}-\d{2})', block)
        if not ap or not ap[0].startswith(model.removeprefix('SM-')) or not android:
            continue
        item = {'model': model, 'csc': region, 'ap': ap[0], 'android': android[1],
                'date': iso_date(date[1]) if date else None,
                'patch': iso_date(patch[1]) if patch else None, 'source': source}
        if ap[0] in result and result[ap[0]] != item:
            raise ValueError('Conflicting official Samsung build metadata')
        result[ap[0]] = item
    return result


def attach(device, rows, http):
    model = device.get('codename') or device['upstream_id']
    if not re.fullmatch(r'SM-[A-Z0-9]{3,16}', model):
        return rows
    cache = getattr(http, '_samsung_notices', None)
    if cache is None:
        cache = http._samsung_notices = {}
    for region in sorted({r.get('region', '') for r in rows}):
        if not re.fullmatch(r'[A-Z0-9]{3}', region):
            continue
        key = (model, region)
        if key not in cache:
            try:
                index = f'https://doc.samsungmobile.com/{model}/{region}/doc.html'
                page = http.get(index)
                soup = BeautifulSoup(page, 'html.parser')
                default = soup.select_one('#dflt_page')
                source = urljoin(index, default.get('value', '')) if default else index
                if urlsplit(source).hostname != 'doc.samsungmobile.com' or not urlsplit(source).path.startswith('/' + model + '/'):
                    raise ValueError('Mismatched Samsung update model')
                cache[key] = parse(http.get(source) if source != index else page, model, region, source)
            except Exception as exc:
                if getattr(exc, 'fmr_batch_deadline', False):
                    raise
                # Optional enrichment must not discard a valid firmware download.
                cache[key] = {}
                print('Samsung metadata: '+type(exc).__name__+'; retain package, retry on next model refresh.', flush=True)
        for row in rows:
            if row.get('region') == region:
                official = cache[key].get(str(row['version']).split('/')[0])
                if official:
                    row['_official_metadata'] = official
    return rows
