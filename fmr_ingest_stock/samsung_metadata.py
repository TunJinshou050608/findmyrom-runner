"""Official model/CSC update notices through the existing bounded metadata cache."""
import re
from urllib.parse import urljoin, urlsplit
from bs4 import BeautifulSoup
from .release_metadata import iso_date


def _as_html_text(page) -> str:
    if hasattr(page, 'text'):
        return page.text
    if isinstance(page, (bytes, bytearray)):
        return bytes(page).decode('utf-8', errors='replace')
    return str(page)


def parse(page, model, region, source):
    html_text = _as_html_text(page)
    soup = BeautifulSoup(html_text, 'html.parser')
    text = soup.get_text(' ', strip=True)
    model_prefix = model.upper().removeprefix('SM-').removeprefix('GT-')
    result = {}
    conflicts: dict[str, set[str]] = {}
    for block in re.split(r'Build Number\s*:\s*', text)[1:]:
        ap = re.match(r'[A-Z0-9]+', block)
        android = re.search(r'Android\s+(\d+(?:\.\d+)*)', block)
        date = re.search(r'Release Date\s*:\s*(\d{4}[-/]\d{2}[-/]\d{2})', block)
        patch = re.search(r'Security patch level\s*:\s*(\d{4}[-/]\d{2}[-/]\d{2})', block)
        if not ap or not ap[0].startswith(model_prefix) or not (android or date or patch):
            continue
        ap_code = ap[0]
        item = {
            'model': model,
            'csc': region,
            'ap': ap_code,
            'android': android[1] if android else None,
            'date': iso_date(date[1]) if date else None,
            'patch': iso_date(patch[1]) if patch else None,
            'source': source,
        }
        if ap_code in result:
            prior = result[ap_code]
            ap_conflicts = conflicts.setdefault(ap_code, set())
            merged = dict(prior)
            # Samsung can publish one AP more than once across notices or CSC/CP variants.
            # Preserve non-null fields when consistent or complementary; clear only on genuine conflict.
            for field in ('android', 'date', 'patch'):
                if field in ap_conflicts:
                    merged[field] = None
                    continue
                prior_val = prior.get(field)
                new_val = item.get(field)
                if prior_val is not None and new_val is not None:
                    if prior_val != new_val:
                        ap_conflicts.add(field)
                        merged[field] = None
                    else:
                        merged[field] = prior_val
                else:
                    merged[field] = prior_val if prior_val is not None else new_val
            result[ap_code] = merged
        else:
            conflicts[ap_code] = set()
            result[ap_code] = item
    return result


def resolve_notice_url(index_url: str, page, model: str) -> str:
    """Resolve Samsung doc.html selector options (preferring eng.html) or #dflt_page redirect."""
    html_text = _as_html_text(page)
    soup = BeautifulSoup(html_text, 'html.parser')

    def _validate_target(raw_value: str) -> str:
        candidate = urljoin(index_url, raw_value.strip())
        parsed = urlsplit(candidate)
        if (
            parsed.scheme != 'https'
            or parsed.hostname != 'doc.samsungmobile.com'
            or not parsed.path.startswith('/' + model + '/')
        ):
            raise ValueError('Mismatched Samsung update model')
        return candidate

    option_urls: list[str] = []
    for opt in soup.find_all('option'):
        raw_val = (opt.get('value') or '').strip()
        if not raw_val:
            continue
        try:
            resolved = _validate_target(raw_val)
        except ValueError:
            continue
        if resolved not in option_urls:
            option_urls.append(resolved)

    for opt_url in option_urls:
        if urlsplit(opt_url).path.lower().endswith('/eng.html'):
            return opt_url

    default = soup.select_one('#dflt_page')
    if default is not None:
        dflt_val = (default.get('value') or '').strip()
        if dflt_val:
            return _validate_target(dflt_val)

    if option_urls:
        return option_urls[0]

    return index_url


def fetch_model_csc_notices(http, model: str, region: str) -> dict[str, dict]:
    """Fetch and parse Samsung official doc.samsungmobile.com build notices for (model, CSC)."""
    if not re.fullmatch(r'(?:SM|GT)-[A-Z0-9]{3,16}', model) or not re.fullmatch(r'[A-Z0-9]{3}', region):
        return {}
    index = f'https://doc.samsungmobile.com/{model}/{region}/doc.html'
    page = http.get(index)
    source = resolve_notice_url(index, page, model)
    target_page = http.get(source) if source != index else page
    return parse(target_page, model, region, source)


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
                cache[key] = fetch_model_csc_notices(http, model, region)
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
