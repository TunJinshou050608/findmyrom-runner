"""Bounded optional JSON completion; never let model output alter links/provenance."""
import json
import os
import re
import httpx
from .rule_extractor import handle, safe_url

FIELDS = (
    'project_name', 'device_codename', 'resource_kind', 'android_version',
    'build_version_tag', 'maintainer_handle', 'maintainer_display_name'
)

SYSTEM_PROMPT = """You are an expert Android Custom ROM release classifier and metadata extractor for FindMyRom.
FindMyRom tracks Android custom ROMs, kernels, recoveries (TWRP, OrangeFox), and custom ports on Telegram.

Classification Rules:
1. ACCEPT genuine community custom releases:
   - Community AOSP custom ROMs (LineageOS community builds, PixelOS community builds, RisingOS, AwakenOS, VoidUI, SkylineUI, EvolutionX, DerpFest, Matrixx, etc.).
   - Official or Unofficial builds of community projects.
   - Custom OEM ports (Xiaomi.eu, MIUIVN, KaoriOS, MiOSP).
   - Custom recoveries (TWRP, OrangeFox, PBRP, SHRP).
   - Custom kernels and custom boot images.
2. REJECT:
   - OEM stock factory firmware releases (official MIUI / HyperOS / ColorOS / OneUI OTA or fastboot releases from manufacturer).
   - Upstream official centralized releases of mainstream ROMs that have their own official website portals (e.g. crdroid.net, lineageos.org official direct releases) ONLY if it is the official centralized release, BUT ACCEPT community-maintained or unofficial builds of these ROMs (e.g. "by @developer", test builds, unofficial ports).
   - Non-releases: web app announcements (like "ROM list 2.0 web app"), discussion, guides/tutorials without release zip, bug reports, farewells, hardware discussion, pure Magisk/KSU modules, APKs, wallpapers, scam/security warnings.

Output JSON strictly adhering to schema:
{
  "is_release": boolean,
  "resource_kind": "rom" | "recovery" | "kernel" | "boot_image" | null,
  "project_name": string or null,
  "device_codename": string or null,
  "is_oem_stock_firmware": boolean,
  "is_community_resource": boolean,
  "android_version": string or null,
  "build_version_tag": string or null,
  "maintainer_handle": string or null,
  "maintainer_display_name": string or null,
  "classification": "community_official" | "unofficial" | "custom_or_port" | "stock_official" | "mainstream_official" | "not_a_release",
  "accepted": boolean,
  "confidence": float,
  "reason": string
}"""


def completion(text, target=None, rule_reason=None):
    base = os.environ.get('LLM_API_BASE_URL', 'https://api.deepseek.com/v1').rstrip('/')
    if not safe_url(base):
        raise ValueError('Invalid LLM API origin')
    model = os.environ.get('LLM_MODEL_NAME', 'deepseek-flash')
    effort = os.environ.get('LLM_REASONING_EFFORT', 'low')
    max_tokens = int(os.environ.get('LLM_MAX_TOKENS', '1024'))

    user_content = f"Target device: {target}\nRule reason: {rule_reason}\n\nPost text:\n{text[:12000]}" if (target or rule_reason) else text[:12000]

    payload = {
        'model': model,
        'temperature': 0,
        'max_tokens': max_tokens,
        'messages': [
            {'role': 'system', 'content': SYSTEM_PROMPT},
            {'role': 'user', 'content': user_content}
        ],
        'response_format': {'type': 'json_object'}
    }
    if 'flash' in model or 'reasoner' in model or os.environ.get('LLM_REASONING_EFFORT'):
        payload['reasoning_effort'] = effort

    with httpx.Client(timeout=25, follow_redirects=False) as client:
        with client.stream('POST', base + '/chat/completions', json=payload, headers={'Authorization': 'Bearer ' + os.environ['LLM_API_KEY']}) as response:
            response.raise_for_status()
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > 131072:
                    raise ValueError('LLM response exceeds limit')
    res_obj = json.loads(body)
    raw_content = res_obj['choices'][0]['message']['content'].strip()
    if raw_content.startswith('```'):
        raw_content = re.sub(r'^```(?:json)?\s*', '', raw_content)
        raw_content = re.sub(r'\s*```$', '', raw_content)
    return json.loads(raw_content)


def disambiguate(text, target=None, rule_reason=None):
    return completion(text, target=target, rule_reason=rule_reason)


def merge(package, output, text=None):
    if isinstance(output, str):
        output = json.loads(output)
    if not isinstance(output, dict):
        raise ValueError('LLM result must be an object')
    result = dict(package)
    source_text = (text or '').lower()
    for name in FIELDS:
        value = output.get(name)
        if not isinstance(value, str) or not value.strip():
            continue
        value = value.strip()[:160]
        # Anti-hallucination Grounding Guard: verify that extracted entities
        # actually appear in the raw Telegram post text.
        if source_text and len(source_text) > 10:
            if name == 'maintainer_handle':
                raw_handle = value.lstrip('@').lower()
                if raw_handle not in source_text:
                    continue
            elif name == 'project_name':
                proj_base = re.sub(r'(?i)\s+(?:os|rom|ui|aosp)$', '', value).strip().lower()
                if proj_base not in source_text and value.lower() not in source_text:
                    continue
            elif name == 'build_version_tag':
                ver_base = value.lstrip('v').strip().lower()
                if ver_base not in source_text and value.lower() not in source_text:
                    continue

        if name == 'device_codename':
            value = value.lower()
            if not re.fullmatch(r'[a-z0-9_-]{3,24}', value):
                continue
        if name == 'maintainer_handle':
            value = handle(value)
            if not value:
                continue
        if name == 'resource_kind' and value not in ('rom', 'recovery', 'kernel', 'firmware', 'boot_image'):
            continue
        # Fill metadata gaps, preserving deterministic evidence where available.
        missing = not result.get(name) or (name == 'project_name' and (result.get('rule_confidence', 0) < .8 or result.get('project_name') in ('Custom ROM', 'ROM', None)))
        if missing:
            result[name] = value
    result['extraction_method'] = 'rule_plus_llm'
    return result

