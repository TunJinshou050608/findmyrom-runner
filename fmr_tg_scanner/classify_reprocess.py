"""Classify a dry-run TG reprocess diff into safe corrections and review rows.

This module never invents a semantic value. The only values it can propose are
the ones the model already returned. Regex is used exclusively to prove that an
*old* D1 value is mechanically wrong (for example, a project name that is
exactly the new name plus the new version). Anything that would require a
judgement call stays in the review bucket.
"""
import argparse
import json
import re
from collections import Counter
from pathlib import Path

DEVICE_WORDS = re.compile(
    r'\b(?:xiaomi|redmi|poco|samsung|galaxy|oneplus|oppo|vivo|realme|motorola|sony|google|pixel|honor|huawei)\b',
    re.I,
)
ANDROID_PREFIX = re.compile(r'^(?:android|a)\s*(?=\d)', re.I)
ANDROID_WORDS = {
    'oreo': '8',
    'pie': '9',
    'ten': '10',
    'eleven': '11',
    'twelve': '12',
    'thirteen': '13',
    'fourteen': '14',
    'fifteen': '15',
    'sixteen': '16',
    'seventeen': '17',
}
ANDROID_LETTERS = {'q': '10', 'r': '11', 's': '12', 't': '13', 'u': '14'}
VERSION_PREFIX = re.compile(r'^v(?:ersion)?\s*', re.I)
ANDROID_VERSION_VALUE = re.compile(r'^(?:android|a)\s*\d+[a-z]?$', re.I)


def _text(value):
    return '' if value is None else str(value).strip()


def _norm(value):
    return ' '.join(_text(value).split()).casefold()


def _alpha(value):
    return re.sub(r'[^a-z0-9]', '', _norm(value))


def same_name(old, new):
    return bool(_text(old) and _text(new) and _alpha(old) == _alpha(new))


def version_key(value):
    value = VERSION_PREFIX.sub('', _norm(value))
    return re.sub(r'\s+', '', value)


def same_version(old, new):
    return version_key(old) == version_key(new)


def android_key(value):
    value = _norm(value)
    value = ANDROID_PREFIX.sub('', value)
    value = value.strip()
    return ANDROID_WORDS.get(value, ANDROID_LETTERS.get(value, value))


def same_android(old, new):
    return _norm(old) == _norm(new)


def looks_like_device(value):
    return bool(DEVICE_WORDS.search(_text(value)))


def node_value(cleaned, field):
    node = (cleaned or {}).get(field) or {}
    return node.get('value') if isinstance(node, dict) else None


def decompose_name(old_name, new_name, new_version, new_android=None):
    """True when old_name is exactly '<new_name> <new_version>' (optionally
    followed by an Android version suffix)."""
    if not (_text(old_name) and _text(new_name) and _text(new_version)):
        return False
    candidates = [_norm(old_name)]
    if _text(new_android):
        target = android_key(new_android)
        stripped = re.sub(
            r'\s+(?:android|a)\s*' + re.escape(target) + r'\s*$', '', old_name, flags=re.I
        )
        candidates.append(_norm(stripped))
    return any(candidate == _norm(new_name + ' ' + new_version) for candidate in candidates)


def decompose_android_name(old_name, new_name, new_android):
    """True when old_name is exactly '<new_name> Android <version>'."""
    if not (_text(old_name) and _text(new_name) and _text(new_android)):
        return False
    target = android_key(new_android)
    if not target:
        return False
    return _norm(old_name) in {
        _norm(new_name + ' Android ' + target),
        _norm(new_name + ' A' + target),
    }


def classify_row(row):
    """Return a classification record for one reprocess row."""
    old = row.get('old') or {}
    cleaned = row.get('cleaned') or {}
    proposed = {}
    reasons = []
    review_fields = []

    is_release = cleaned.get('is_release')
    if is_release is not True:
        return {
            'id': row.get('id'),
            'public_token': row.get('public_token'),
            'target_post_url': row.get('target_post_url'),
            'device': row.get('device'),
            'decision': 'review',
            'reasons': ['not_release'],
            'review_fields': ['is_release'],
            'proposed': {},
            'old': old,
            'new': {
                'project_name': node_value(cleaned, 'project_name'),
                'project_version': node_value(cleaned, 'project_version'),
                'android_version': node_value(cleaned, 'android_version'),
            },
        }

    old_name = old.get('project_name')
    old_version = old.get('build_version_tag')
    old_android = old.get('android_version')
    new_name = node_value(cleaned, 'project_name')
    new_version = node_value(cleaned, 'project_version')
    new_android = node_value(cleaned, 'android_version')

    # project_name: only split an embedded version that the new extraction
    # proves, or an embedded Android version that the new extraction proves.
    if not same_name(old_name, new_name):
        if decompose_name(old_name, new_name, new_version, new_android):
            proposed['project_name'] = new_name
            if not same_version(old_version, new_version):
                proposed['build_version_tag'] = new_version
            reasons.append('split_project_version')
        elif decompose_android_name(old_name, new_name, new_android):
            proposed['project_name'] = new_name
            proposed['android_version'] = new_android
            reasons.append('split_project_android')
        else:
            review_fields.append('project_name')

    # build_version_tag: only clear/set it when the old value is provably an
    # Android version or a device name, never because the model preferred a
    # different release string.
    if 'build_version_tag' not in proposed and not same_version(old_version, new_version):
        # A bare number that happens to equal android_version may still be a
        # legitimate ROM version (OxygenOS 11, PixelExperience 14). Only an
        # explicitly labelled Android value proves the field is wrong.
        old_is_android = bool(ANDROID_VERSION_VALUE.fullmatch(_text(old_version)))
        if old_version and old_is_android:
            proposed['build_version_tag'] = new_version
            reasons.append('version_field_was_android')
        elif old_version and looks_like_device(old_version):
            proposed['build_version_tag'] = new_version
            reasons.append('version_field_was_device')
        else:
            review_fields.append('build_version_tag')

    # android_version: normalize a non-canonical old value, or clear a value
    # that is a device/unknown marker. Conflicting numeric versions go to
    # review instead of being overwritten.
    if 'android_version' not in proposed and not same_android(old_android, new_android):
        if old_android and looks_like_device(old_android):
            proposed['android_version'] = new_android
            reasons.append('android_field_was_device')
        elif _norm(old_android) in ('unknown', 'null', 'none'):
            proposed['android_version'] = new_android
            reasons.append('android_field_was_unknown')
        elif new_android is None:
            review_fields.append('android_version')
        elif old_android and android_key(old_android) == android_key(new_android):
            proposed['android_version'] = new_android
            reasons.append('android_format')
        elif not old_android:
            review_fields.append('android_version')
        else:
            review_fields.append('android_version')

    if proposed:
        decision = 'apply'
    elif review_fields:
        decision = 'review'
    else:
        decision = 'clean'

    return {
        'id': row.get('id'),
        'public_token': row.get('public_token'),
        'target_post_url': row.get('target_post_url'),
        'device': row.get('device'),
        'decision': decision,
        'reasons': reasons,
        'review_fields': review_fields,
        'proposed': proposed,
        'old': {
            'project_name': old_name,
            'project_version': old_version,
            'android_version': old_android,
        },
        'new': {
            'project_name': new_name,
            'project_version': new_version,
            'android_version': new_android,
        },
    }


def classify(rows):
    records = [classify_row(row) for row in rows]
    counts = Counter()
    reason_counts = Counter()
    field_counts = Counter()
    for record in records:
        counts[record['decision']] += 1
        for reason in record['reasons']:
            reason_counts[reason] += 1
        for field in record['proposed']:
            field_counts[field] += 1
    return records, {
        'rows': len(records),
        'decisions': dict(counts),
        'reasons': dict(reason_counts),
        'fields': dict(field_counts),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True)
    parser.add_argument('--output', required=True, help='JSONL of apply/review records')
    parser.add_argument('--report', help='Optional JSON summary path')
    args = parser.parse_args(argv)

    rows = [json.loads(line) for line in Path(args.input).read_text().splitlines() if line.strip()]
    records, summary = classify(rows)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open('w') as handle:
        for record in records:
            if record['decision'] != 'clean':
                handle.write(json.dumps(record, ensure_ascii=False) + '\n')
    if args.report:
        Path(args.report).write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == '__main__':
    main()
