"""Re-extract already-published TG custom resources from their original text.

This is a dry-run research tool: it reads D1, calls the model, normalizes the
result, and writes a JSONL diff. It never updates D1.
"""
import argparse
import asyncio
import json
import os
import re
import time
from pathlib import Path

import httpx

from .cache_probe import PROMPT, from_payload
from .grounded_cleaner import NAMED_REPAIR_PROMPT, compact_to_nodes


def parse_url(url):
    match = re.search(r'(?:https?://)?t\.me/([^/]+)/(\d+)', url or '')
    return (match.group(1), int(match.group(2))) if match else (None, None)


def export_d1(output):
    from fmr_core.d1_client import D1Client
    db = D1Client()
    try:
        rows = db.query(
            "SELECT id,public_token,device_codename,target_post_url,published_at,"
            "project_name,build_version_tag,android_version,maintainer_name,"
            "json_extract(metadata_json,'$.evidence.observed_text') AS observed_text "
            "FROM custom_resources ORDER BY device_codename,id"
        )
    finally:
        db.close()
    with Path(output).open('w') as handle:
        for row in rows:
            channel, msg_id = parse_url(row['target_post_url'])
            handle.write(json.dumps({
                'id': row['id'],
                'public_token': row['public_token'],
                'device': row['device_codename'],
                'channel': channel,
                'msg_id': msg_id,
                'target_post_url': row['target_post_url'],
                'published_at': row['published_at'],
                'old': {
                    'project_name': row['project_name'],
                    'build_version_tag': row['build_version_tag'],
                    'android_version': row['android_version'],
                    'maintainer_name': row['maintainer_name'],
                },
                'observed_text': row['observed_text'],
            }, ensure_ascii=False) + '\n')
    return len(rows)


async def call(client, base, key, model, max_tokens, timeout, messages):
    payload = {
        'model': model,
        'thinking': {'type': 'disabled'},
        'temperature': 0,
        'max_tokens': max_tokens,
        'response_format': {'type': 'json_object'},
        'messages': messages,
    }
    async with client.stream('POST', base + '/chat/completions',
                             headers={'Authorization': 'Bearer ' + key}, json=payload) as response:
        response.raise_for_status()
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > 131072:
                raise ValueError('Oversized response')
    data = json.loads(body)
    choice = data['choices'][0]
    if choice.get('finish_reason') != 'stop':
        raise ValueError('Truncated output')
    return choice['message']['content'], data.get('usage', {})


async def extract_one(client, base, key, model, max_tokens, timeout, item, semaphore):
    async with semaphore:
        text = item.get('observed_text') or ''
        if len(text.strip()) < 20:
            return {**item, 'cleaned': None, 'error': 'no_observed_text'}
        names = {item['device']: [item['device']]}
        messages = [
            {'role': 'system', 'content': PROMPT},
            {'role': 'user', 'content': json.dumps(
                {'target': item['device'], 'raw_text': text[:6000]}, ensure_ascii=False)},
        ]
        repaired = False
        while True:
            try:
                content, usage = await call(client, base, key, model, max_tokens, timeout, messages)
                parsed = json.loads(content)
                if isinstance(parsed, dict) and ('v' in parsed or 'is_release' in parsed):
                    cleaned = compact_to_nodes(parsed, text, item['device'], names)
                else:
                    raise ValueError('invalid_envelope')
                return {**item, 'cleaned': cleaned,
                        'normalization': cleaned.get('_normalization'),
                        'usage': usage, 'error': None}
            except Exception as error:
                if repaired:
                    return {**item, 'cleaned': None,
                            'error': f'{type(error).__name__}:{error}'[:300]}
                repaired = True
                messages = [
                    {'role': 'system', 'content': NAMED_REPAIR_PROMPT},
                    {'role': 'user', 'content': json.dumps(
                        {'target': item['device'], 'raw_text': text[:6000],
                         'invalid_output': content[:4000] if 'content' in locals() else ''},
                        ensure_ascii=False)},
                ]


async def run(input_path, output_path, limit, concurrency):
    items = [json.loads(line) for line in Path(input_path).read_text().splitlines() if line.strip()]
    if limit:
        items = items[:limit]
    done = set()
    if Path(output_path).exists():
        for line in Path(output_path).read_text().splitlines():
            try:
                done.add(json.loads(line)['id'])
            except Exception:
                pass
    items = [item for item in items if item['id'] not in done]
    key = os.getenv('LLM_API_KEY', '').strip()
    if not key:
        raise SystemExit('Missing LLM_API_KEY')
    base = os.getenv('LLM_API_BASE_URL', 'https://api.deepseek.com/v1').rstrip('/')
    model = os.getenv('LLM_MODEL_NAME', 'deepseek-flash')
    max_tokens = int(os.getenv('LLM_MAX_TOKENS', '1500'))
    timeout = int(os.getenv('LLM_TIMEOUT_SECONDS', '120'))
    semaphore = asyncio.Semaphore(concurrency)
    started = time.time()
    completed = failed = 0
    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=timeout, write=30, pool=10),
                                 trust_env=False, follow_redirects=False) as client:
        tasks = [asyncio.create_task(extract_one(client, base, key, model, max_tokens, timeout, item, semaphore))
                 for item in items]
        with Path(output_path).open('a') as handle:
            for task in asyncio.as_completed(tasks):
                result = await task
                handle.write(json.dumps(result, ensure_ascii=False) + '\n')
                handle.flush()
                completed += 1
                failed += bool(result.get('error'))
                if completed % 50 == 0:
                    print(json.dumps({'event': 'tg_reprocess_progress', 'completed': completed,
                                      'failed': failed, 'total': len(items),
                                      'elapsed': round(time.time() - started, 1)}), flush=True)
    print(json.dumps({'event': 'tg_reprocess_finished', 'completed': completed,
                      'failed': failed, 'total': len(items),
                      'elapsed': round(time.time() - started, 1)}), flush=True)
    return 0 if failed == 0 else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input')
    parser.add_argument('--output', required=True)
    parser.add_argument('--export-d1', action='store_true')
    parser.add_argument('--limit', type=int, default=0)
    parser.add_argument('--concurrency', type=int, default=4)
    args = parser.parse_args()
    if args.export_d1:
        count = export_d1(args.output)
        print(json.dumps({'event': 'tg_reprocess_exported', 'rows': count, 'output': args.output}), flush=True)
        return 0
    if not args.input:
        parser.error('--input is required unless --export-d1 is used')
    return asyncio.run(run(args.input, args.output, args.limit, max(1, min(args.concurrency, 8))))


if __name__ == '__main__':
    raise SystemExit(main())
