"""Offline URL validation and deterministic Xiaomi CDN rewriting."""

import ipaddress
import re
import unicodedata
from urllib.parse import urlsplit, urlunsplit

FAST_HOST = "bkt-sgp-miui-ota-update-alisgp.oss-ap-southeast-1.aliyuncs.com"
MI_HOSTS = {
    f"{prefix}.d.miui.com"
    for prefix in ("bigota", "hugeota", "ultimota", "ultimateota", "superota")
}


def rewrite_to_fast_cdn(brand_id: str, original_url: str) -> str:
    if not isinstance(original_url, str):
        raise ValueError("URL must be a string")
    url = original_url.strip()
    # urlsplit silently removes some control characters; reject before parsing.
    if re.search(r"[\x00-\x20\x7f]", url):
        raise ValueError("URL contains whitespace or control characters")
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not host
            or "." not in host
            or any(c.isspace() or unicodedata.category(c) == "Cc" for c in host)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 80, 443}
        ):
            raise ValueError("Unsafe URL")
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            ip = None
        if ip and (
            not ip.is_global
            or ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
        ):
            raise ValueError("Non-global IP address")
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Unsafe URL") from exc
    if host.lower() in MI_HOSTS:
        return urlunsplit(
            ("https", FAST_HOST, parsed.path, parsed.query, parsed.fragment)
        )
    return url
