"""Pure-stdlib URL validation, SSRF protection, and Xiaomi CDN rewriting."""

import ipaddress
import json
from pathlib import Path
import re
import socket
import unicodedata
from typing import Any
from urllib.parse import urlsplit, urlunsplit

FAST_HOST = "bkt-sgp-miui-ota-update-alisgp.oss-ap-southeast-1.aliyuncs.com"
MI_HOSTS = {
    f"{prefix}.d.miui.com"
    for prefix in ("bigota", "hugeota", "ultimota", "ultimateota", "superota")
}
LEGACY_HTTP_UPGRADE_DOMAINS = (
    "d.miui.com",
    "sonymobile.com",
    "sony.net",
)
CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")
_EXTRACTION_MATRIX_PATH = (
    Path(__file__).resolve().parents[1] / "fmr_core" / "contracts" / "extraction_matrix.v1.json"
)

# Standalone container fallback when fmr_ingest_stock + fmr_extractor are deployed without fmr_core;
# verified in tests to stay 100% identical to extraction_matrix.v1.json["official_cdn_hosts"].
_FALLBACK_OFFICIAL_CDN_HOSTS: dict[str, tuple[str, ...]] = {
    "asus": ("asus.com", "asus.com.cn"),
    "google": ("dl.google.com", "dl.googleusercontent.com"),
    "infinix": ("android.googleapis.com",),
    "itel": ("android.googleapis.com",),
    "motorola": ("android.googleapis.com", "lenovo.com", "motorola.com"),
    "nothing": ("android.googleapis.com", "nothing.tech"),
    "oneplus": (
        "allawnfs.com",
        "allawnofs.com",
        "allawnos.com",
        "allawntech.com",
        "android.googleapis.com",
        "bbk.com",
        "coloros.com",
        "heytapdl.com",
        "heytapmobile.com",
        "myoas.com",
        "oneplus.com",
        "oneplus.net",
        "oneplusmobile.com",
        "oplusmobile.com",
        "oppomobile.com",
    ),
    "oppo": (
        "allawnfs.com",
        "allawnofs.com",
        "allawnos.com",
        "allawntech.com",
        "android.googleapis.com",
        "bbk.com",
        "coloros.com",
        "heytapdl.com",
        "heytapmobile.com",
        "myoas.com",
        "oplusmobile.com",
        "oppo.com",
        "oppomobile.com",
    ),
    "realme": (
        "allawnfs.com",
        "allawnofs.com",
        "allawnos.com",
        "allawntech.com",
        "android.googleapis.com",
        "bbk.com",
        "coloros.com",
        "heytapdl.com",
        "heytapmobile.com",
        "myoas.com",
        "oplusmobile.com",
        "oppomobile.com",
        "realme.com",
        "realme.net",
        "realmemobile.com",
    ),
    "rog": ("asus.com", "asus.com.cn"),
    "samsung": (
        "ospserver.net",
        "samsung.com",
        "samsungmobile.com",
        "sslcs.cdngc.net",
    ),
    "sony": ("sony.net", "sonymobile.com"),
    "tecno": ("android.googleapis.com",),
    "vivo": ("bbk.com", "vivo.com", "vivo.com.cn", "vivoglobal.com"),
    "xiaomi": (FAST_HOST, "d.miui.com", "miui.com", "xiaomi.com"),
}


class DNSTransientError(ValueError):
    """Raised when DNS resolution fails transiently via OSError."""


def _load_official_oem_hosts() -> dict[str, tuple[str, ...]]:
    if _EXTRACTION_MATRIX_PATH.is_file():
        raw = json.loads(_EXTRACTION_MATRIX_PATH.read_text(encoding="utf-8"))
        cdn_hosts: dict[str, Any] = raw["official_cdn_hosts"]
        hosts: dict[str, tuple[str, ...]] = {
            str(brand): tuple(str(h) for h in host_list)
            for brand, host_list in cdn_hosts.items()
        }
    else:
        hosts = dict(_FALLBACK_OFFICIAL_CDN_HOSTS)
    hosts["pixel"] = hosts["google"]
    hosts["redmi"] = hosts["xiaomi"]
    hosts["poco"] = hosts["xiaomi"]
    hosts["iqoo"] = hosts["vivo"]
    return hosts


# Official OEM download host allowlist derived from extraction_matrix.v1.json.
# Security invariant: NEVER wildcard aliyuncs.com; only exact FAST_HOST is permitted.
OFFICIAL_OEM_HOSTS: dict[str, tuple[str, ...]] = _load_official_oem_hosts()
OFFICIAL_HOST_MAP = OFFICIAL_OEM_HOSTS
ALL_OFFICIAL_HOSTS: tuple[str, ...] = tuple(
    dict.fromkeys(host for hosts in OFFICIAL_OEM_HOSTS.values() for host in hosts)
)


def _is_forbidden_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    candidates: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = [ip]
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        candidates.append(ip.ipv4_mapped)
    for addr in candidates:
        if (
            not addr.is_global
            or addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_multicast
            or addr.is_unspecified
            or addr.is_reserved
            or (isinstance(addr, ipaddress.IPv4Address) and addr in CGNAT_NETWORK)
        ):
            return True
    return False


def _matches_domain_suffix(host_lower: str, domains: tuple[str, ...]) -> bool:
    return any(
        host_lower == domain or host_lower.endswith("." + domain) for domain in domains
    )


def _is_forbidden_hostname(host: str) -> bool:
    lowered = host.lower()
    if lowered.endswith(".") or "." not in lowered:
        return True
    if lowered == "localhost" or lowered.endswith((".localhost", ".local", ".internal")):
        return True
    if lowered.rsplit(".", 1)[-1].isdigit():
        return True
    return False


def _parse_and_validate_url(original_url: str) -> tuple[str, str, str, str, str, str]:
    if not isinstance(original_url, str):
        raise ValueError("URL must be a string")
    url = original_url.strip()
    # urlsplit silently removes some control characters; reject before parsing.
    if not url or re.search(r"[\x00-\x20\x7f]", url):
        raise ValueError("URL contains whitespace or control characters")
    try:
        parsed = urlsplit(url)
        scheme = parsed.scheme.lower()
        host = parsed.hostname
        port = parsed.port
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Unsafe URL") from exc

    if (
        scheme not in {"http", "https"}
        or not host
        or any(c.isspace() or unicodedata.category(c) == "Cc" for c in host)
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("Unsafe URL")

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None

    if ip is not None:
        if _is_forbidden_ip(ip):
            raise ValueError("Non-global IP address")
    else:
        if _is_forbidden_hostname(host):
            raise ValueError("Unsafe hostname")

    host_lower = host.lower()
    if scheme == "http":
        if ip is not None or not _matches_domain_suffix(
            host_lower, LEGACY_HTTP_UPGRADE_DOMAINS
        ):
            raise ValueError("HTTPS scheme required")
        if port not in {None, 80}:
            raise ValueError("Unsafe URL port")
    else:
        if port not in {None, 443}:
            raise ValueError("Unsafe URL port")

    return url, scheme, host_lower, parsed.path, parsed.query, parsed.fragment


def rewrite_to_fast_cdn(brand_id: str, original_url: str) -> str:
    url, scheme, host_lower, path, query, fragment = _parse_and_validate_url(
        original_url
    )
    if host_lower in MI_HOSTS:
        return urlunsplit(("https", FAST_HOST, path, query, fragment))
    if scheme == "http":
        return urlunsplit(("https", host_lower, path, query, fragment))
    return url


def _host_matches_allowlist(host_lower: str, allowed_hosts: tuple[str, ...]) -> bool:
    for entry in allowed_hosts:
        candidate = entry.strip().lower().lstrip(".")
        if not candidate or "." not in candidate:
            continue
        if candidate == "aliyuncs.com" or candidate.endswith(".aliyuncs.com"):
            if candidate == FAST_HOST and host_lower == FAST_HOST:
                return True
            if candidate != "aliyuncs.com" and not candidate.startswith("oss-") and host_lower == candidate:
                return True
            continue
        if host_lower == candidate or host_lower.endswith("." + candidate):
            return True
    return False


def _verify_resolved_dns(hostname: str) -> None:
    try:
        infos = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise DNSTransientError(f"DNS resolution failed for {hostname}: {exc}") from exc
    if not infos:
        raise ValueError(f"DNS resolution returned no addresses for {hostname}")
    for info in infos:
        raw_ip = str(info[4][0]).split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(raw_ip)
        except ValueError as exc:
            raise ValueError(f"Invalid resolved IP address for {hostname}") from exc
        if _is_forbidden_ip(ip):
            raise ValueError(f"Resolved non-global IP address for {hostname}")


def validate_official_download_url(
    brand: str,
    url: str,
    *,
    allowed_hosts: tuple[str, ...] | None = None,
    resolve_dns: bool = False,
) -> str:
    if not isinstance(brand, str):
        raise ValueError("Brand must be a string")
    brand_key = brand.strip().lower()
    if allowed_hosts is None:
        if brand_key:
            if brand_key not in OFFICIAL_OEM_HOSTS:
                raise ValueError(f"Unsupported official brand: {brand}")
            effective_hosts = OFFICIAL_OEM_HOSTS[brand_key]
        else:
            effective_hosts = ALL_OFFICIAL_HOSTS
    else:
        effective_hosts = tuple(allowed_hosts)
        if not effective_hosts:
            raise ValueError("Empty allowed_hosts")

    _, scheme, host_lower, path, query, fragment = _parse_and_validate_url(url)

    can_rewrite_xiaomi = not brand_key or brand_key in {"xiaomi", "redmi", "poco"}
    if can_rewrite_xiaomi and host_lower in MI_HOSTS:
        final_host = FAST_HOST
        final_url = urlunsplit(("https", FAST_HOST, path, query, fragment))
    elif scheme == "http":
        final_host = host_lower
        final_url = urlunsplit(("https", host_lower, path, query, fragment))
    else:
        final_host = host_lower
        final_url = urlunsplit(("https", host_lower, path, query, fragment))

    if not _host_matches_allowlist(final_host, effective_hosts):
        raise ValueError(f"Host {final_host!r} is not in official allowlist")

    if resolve_dns:
        _verify_resolved_dns(final_host)

    return final_url
