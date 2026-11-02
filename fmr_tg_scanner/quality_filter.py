"""Post-collection, evidence-bound screening. No HTTP, crawler or state mutations."""
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import re
import unicodedata

POLICY_VERSION = "quality-view-v1"
DEFAULT_CATALOGUE = {
    "alioth": ["POCO F3", "Redmi K40", "Mi 11X"],
    "aliothin": ["Mi 11X India"],
    "beryllium": ["POCO F1", "POCOPHONE F1"],
    "apollo": ["Mi 10T", "Mi10T Pro", "Mi 10T Pro"],
    "munch": ["POCO F4"], "vili": ["Mi 11T Pro"],
}
# Fallback identities are cross-device conflict guards, not search seeds.
ROM_PROJECTS = (
    ("PixelExperience", r"pixel\s*experience|\bPE\s+(?:plus|unofficial)"),
    ("LineageOS", r"lineage(?:os)?|\bLOS\b"),
    ("Evolution X", r"evolution\s*x|evolutionx"),
    ("Paranoid Android", r"paranoid\s*android|\bAOSPA\b"),
    ("Project Infinity X", r"project\s*infinity\s*x"),
    ("DerpFest", r"derp\s*fest"), ("crDroid", r"crdroid"),
    ("CherishOS", r"cherish[-\s]*os"), ("PixelOS", r"pixelos"),
    ("AICP", r"\baicp\b|aicp_"),
    ("AOSP Extended", r"aosp\s*extended|\bAEX\b"),
    ("HyperOS", r"hyperos"), ("MIUI", r"\bmiui\b"),
    ("OxygenOS", r"oxygen\s*os|\bOOS\d*"),
    ("ColorOS", r"color\s*os"), ("OriginOS", r"origin\s*os"),
    ("MYUI", r"\bmyui"), ("AfterlifeOS", r"afterlife\s*os"),
    ("AxionOS", r"axion(?:aosp|os)?"),
    ("BlissROM", r"bliss(?:rom)?"), ("Nusantara", r"nusantara"),
    ("Project Blaze", r"project\s*blaze"), ("ProtonPlus", r"protonplus"),
    ("ProtonAOSP", r"protonaosp"), ("xdroid", r"xdroid|xd_rev"),
    ("ShapeShiftOS", r"shapeshiftos"), ("SyberiaOS", r"syberia"),
    ("KOSP", r"\bkosp\b|kosp-"), ("Project Zephyrus", r"project[-\s]*zephyrus"),
    ("AOSP", r"\bAOSP\b"), ("Bootleggers", r"bootleggers"),
    ("Pixel Extended", r"pixel\s*extended"), ("Project 404", r"project[-\s]*404"),
)
MAINSTREAM = {
    "lineageos", "pixelos", "crdroid", "evolution x", "pixelexperience",
    "paranoid android", "blissrom", "project elixir", "xtended",
    "alphadroid", "coltos", "pixelplusui",
}
DONOR = re.compile(r"(?i)\b(?:port(?:ed)?|extract(?:ed)?|taken|based)\s+from\b|"
                   r"^\s*(?:donor|base device|source device)\s*[:：-]|移植自|提取自")
FOOTER = re.compile(r"(?i)^\W*(?:follow|join|subscribe|credits?|thanks|special thanks)\b")
DEPENDENCY = re.compile(r"(?i)^\W*(?:notes?|recommended?|required?|needed?|firmware|"
                        r"fw\b|how to|installation|flashing|credits?|thanks|changelog|"
                        r"download|support|screenshots?|by\b|maintainer)\b")


def norm(value):
    return unicodedata.normalize("NFKC", value or "")


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def build_date(text):
    """A release date is evidence; never substitute today's date or a firmware version."""
    for line in text.splitlines():
        if not re.search(r"(?i)\b(?:updated?|released?|build\s*date|date)\s*[:：-]", line):
            continue
        value = re.sub(r"['’]|(?<=\d)(?:st|nd|rd|th)\b", "", line)
        match = re.search(r"(\d{4}-\d{2}-\d{2}|\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}|"
                          r"\d{1,2}\s+[A-Za-z]+\s+\d{4}|[A-Za-z]+\s+\d{1,2},?\s+\d{4})", value)
        if not match:
            continue
        for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d/%m/%y", "%d.%m.%Y",
                    "%d.%m.%y", "%d %B %Y", "%B %d, %Y", "%B %d %Y"):
            try:
                return datetime.strptime(match[1], fmt).date().isoformat()
            except ValueError:
                pass
    return None


def sizes(text):
    result = []
    for line in text.splitlines():
        if re.search(r"(?i)\b(?:ram|free space|storage|memory|system partition)\b", line):
            continue
        for match in re.finditer(r"(?i)(?<![\d.,])(\d+(?:[.,]\d+)*)\s*(GiB|MiB|KiB|GB|MB|KB)\b", line):
            raw, unit = match.groups()
            numeric = raw.replace(",", "") if re.fullmatch(r"\d{1,3}(?:,\d{3})+", raw) else raw.replace(",", ".")
            try:
                number = float(numeric)
            except ValueError:
                continue
            # Decimal units and binary units are kept distinct.
            factor = {"kb": 1000, "mb": 1000**2, "gb": 1000**3,
                      "kib": 1024, "mib": 1024**2, "gib": 1024**3}[unit.lower()]
            result.append({"label": match[0], "bytes_claimed": round(number * factor),
                           "evidence": line.strip(), "verified_bytes": False})
    return result


class QualityFilter:
    def __init__(self, catalogue=None):
        combined = {k: list(v) for k, v in DEFAULT_CATALOGUE.items()}
        for code, names in (catalogue or {}).items():
            combined[code] = list(dict.fromkeys([*combined.get(code, []), *names]))
        self.patterns = []
        for code, names in combined.items():
            for value in dict.fromkeys([code, *names]):
                for alias in re.split(r"\s*/\s*|\s+aka\s+", norm(value), flags=re.I):
                    if not alias:
                        continue
                    pattern = re.escape(alias).replace(r"\ ", r"\s*")
                    self.patterns.append((code, re.compile(r"(?<![A-Za-z0-9])" + pattern + r"(?![A-Za-z0-9])", re.I)))

    def devices(self, text):
        spans = [(m.start(), m.end(), code) for code, pattern in self.patterns for m in pattern.finditer(norm(text))]
        accepted = []
        for start, end, code in sorted(spans, key=lambda x: (x[0], -x[1])):
            if any(a <= start and b >= end and (a < start or b > end) for a, b, _ in accepted):
                continue
            accepted.append((start, end, code))
        return {code for _, _, code in accepted}

    def review(self, row, target, profile=""):
        text = norm(row.get("text") or "")
        attachment = norm(row.get("observed_attachment") or row.get("attachment") or "")
        raw_lines = [line.strip() for line in text.splitlines() if line.strip()]
        lines = [line for line in raw_lines if not re.fullmatch(r"(?:#[^\s]+\s*)+", line)]
        title = lines[0] if lines else attachment
        # A caption's first substantive line is primary; no legacy filename fallback.
        release_lines = []
        for line in lines[:5]:
            donor = DONOR.search(line)
            if donor:
                line = line[:donor.start()].strip()
            if release_lines and (DEPENDENCY.search(line) or not line):
                break
            if line and not DEPENDENCY.search(line):
                release_lines.append(line)
        heading = "\n".join(release_lines[:3])
        primary = heading + "\n" + attachment
        result = {
            "target_post_url": row.get("url") or row.get("tg_post_url"),
            "status": "candidate", "reasons": [], "title": title or None,
            "device_codename": target, "resource_kind": None, "project_name": None,
            "build_version_tag": None, "build_date": build_date(text),
            "maintainer_handle": None, "maintainer_display_name": None,
            "publisher_channel": row.get("channel") or row.get("channel_username"),
            "author_identity_verified": False, "sizes": sizes(text),
            "policy_version": POLICY_VERSION, "source_hash": digest([text, attachment]),
        }
        reasons = result["reasons"]
        strong, donors, body = set(), set(), set()
        ambiguous_target = False
        for index, line in enumerate(raw_lines):
            if FOOTER.search(line):
                # Ignore the footer line, but retain explicit device tags below it.
                continue
            if DONOR.search(line):
                before, after = DONOR.split(line, maxsplit=1)
                body |= self.devices(before)
                parts = re.split(r"(?i)\bfor\b|适配|用于", after, maxsplit=1)
                donors |= self.devices(parts[0])
                if len(parts) > 1:
                    strong |= self.devices(parts[1])
                continue
            found = self.devices(line)
            if not found:
                continue
            uncertain = bool(re.search(r"[?？]|(?i:\b(?:untested|maybe|not tested)\b)", line))
            if target in found and uncertain:
                ambiguous_target = True
                found.discard(target)
            # Source chatter is not a target declaration.
            if re.search(r"(?i)\b(?:for|devices?|supported|only|works on|usable on)\b|适配|仅限", line):
                strong |= found
            if index < 4 or "#" in line:
                body |= found
        file_devices = self.devices(attachment)
        if file_devices:
            strong |= file_devices
        incompatible = bool(strong and target not in strong)
        if incompatible:
            reasons.append("explicit_other_target")
        elif target in donors and target not in strong and target not in body:
            reasons.append("target_is_donor")
        elif ambiguous_target and target not in strong:
            reasons.append("unproved_compatibility")
        elif target in strong or target in body:
            result["device_evidence"] = "post_or_attachment"
        else:
            current_profile = profile or (row.get("evidence", {}).get("value", "")
                if row.get("evidence", {}).get("kind") == "dedicated_channel_profile" else "")
            if self.devices(current_profile) == {target}:
                result["device_evidence"] = "single_device_profile"
            else:
                reasons.append("unproved_compatibility")
        if target in donors and target not in strong:
            reasons.append("donor_requires_target_evidence")

        patch = bool(re.search(r"(?i)\b(?:module|magisk module|patch|injector|bootanimation|bootsplash|"
                               r"fix\b|remover|add[- ]?on|theme|dialer)\b|模块|补丁|Модуль|исправление", primary))
        kernel = bool(re.search(r"(?i)kernel|anykernel", primary))
        recovery = bool(re.search(r"(?i)\b(?:twrp|orangefox|ofrp|recovery)\b", primary))
        project = next((name for name, pattern in ROM_PROJECTS if re.search(pattern, heading, re.I)), None)
        if not project:
            project = next((name for name, pattern in ROM_PROJECTS if re.search(pattern, attachment, re.I)), None)
        rom_heading = re.sub(r"(?i)\bAndroid\b", "", heading)
        rom = bool(project or re.search(r"(?i)\bROM\b|[A-Za-z][A-Za-z0-9_-]*(?:OS|Droid|AOSP)(?=\d|\b)|刷机包|第三方系统", rom_heading)
                   or re.search(r"(?i)#ROM\b", "\n".join(raw_lines[:4])))
        # Clear primary labels override incidental words later in the caption.
        first = title or attachment
        if patch and not (rom and re.search(r"(?i)\b(?:hotfix|fix)\b", first)
                          and not re.search(r"(?i)module|patch|fix\.zip", first)):
            result["resource_kind"] = "patch"
            reasons.append("auxiliary_patch_or_tool")
        elif re.search(r"(?i)^\W*Magisk\b|^\W*(?:flash(?:ing)? tool|工具)\b", first):
            result["resource_kind"] = "tool"
            reasons.append("auxiliary_patch_or_tool")
        elif (kernel and re.search(r"(?i)kernel|anykernel", first)) or re.match(r"\d+\.\d+\.\d+-.*\.zip$", attachment):
            result["resource_kind"], result["project_name"] = "kernel", "Kernel"
        elif recovery and re.search(r"(?i)twrp|orangefox|ofrp|recovery", first) and not project:
            result["resource_kind"] = "recovery"
            result["project_name"] = "TWRP" if re.search("twrp", first, re.I) else "OrangeFox" if re.search("orangefox|ofrp", first, re.I) else "Recovery"
        elif rom:
            result["resource_kind"], result["project_name"] = "rom", project or title
        elif re.fullmatch(r"(?i).*(?:init_boot|vendor_boot|boot|dtbo|vbmeta)[^/]*\.img(?:\.gz)?", attachment):
            result["resource_kind"], result["project_name"] = "boot_image", "Partition image"
        elif re.search(r"(?i)^\W*(?:firmware|fw\b)", first):
            result["resource_kind"], result["project_name"] = "firmware", "Firmware"
        elif kernel:
            result["resource_kind"], result["project_name"] = "kernel", "Kernel"
        elif recovery:
            result["resource_kind"], result["project_name"] = "recovery", "Recovery"
        else:
            reasons.append("unproved_primary_resource")
        if re.search(r"(?i)screenshots?\s+credits?|^\W*(?:screenshot|flashing guide|how to|tutorial)\b", text):
            reasons.append("showcase_or_guide")
        if re.search(r"(?i)download\s*:\s*(?:temporarily removed|removed|unavailable)|"
                     r"don't use this|do not use this|jangan pake ini", text):
            reasons.append("withdrawn_or_discouraged")
        for line in lines:
            match = re.match(r"(?i)^\W*(?:by|maintainers?|developer|dev|author|built\s+by|автор)\s*[:：-]?\s+(.+)", line)
            if match:
                credit = match[1].split("|")[0].strip()
                handle = re.search(r"@([A-Za-z0-9_]{4,32})\b", credit)
                result["maintainer_handle"] = "@" + handle[1].lower() if handle else None
                result["maintainer_display_name"] = credit[:160]
                result["maintainer_evidence"] = line
                break
        version_text = "\n".join(line for line in release_lines if not re.search(
            r"(?i)^\W*(?:updated?|released?|build\s*date|date)\s*[:：-]", line))
        version_text = "\n".join(line for line in version_text.splitlines()
                                 if not re.fullmatch(r"\W*\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}\W*", line))
        version_text = re.sub(r"(?i)\bAndroid\s*(?:version\s*[:：-]?\s*)?\d+(?:\.\d+)*(?:L)?\b", "", version_text)
        if result["resource_kind"] == "kernel" and attachment and not re.search(r"(?i)kernel|anykernel", first):
            # Compatibility captions naming several ROMs do not version a kernel.
            version_text = attachment
        version = re.search(r"(?i)(?<![A-Za-z0-9])(?:OS|V|R)\d+(?:\.[A-Za-z0-9_-]+){0,6}\b|"
                            r"(?<![A-Za-z0-9])\d+(?:\.\d+){1,5}\b", version_text)
        if version:
            result["build_version_tag"] = version[0]
            result["version_evidence"] = version.group(0)
        official_scope = heading + "\n" + attachment
        unofficial = bool(re.search(r"(?i)\bunofficial\b|\bcommunity\b|(?<![a-z])port(?:ed)?(?![a-z])", official_scope))
        official = bool(re.search(r"(?i)\bofficial\b", official_scope)) and not unofficial
        result["release_status"] = "unofficial_or_port" if unofficial else "official" if official else "unknown"
        if result["resource_kind"] == "rom" and official:
            reasons.append("excluded_mainstream_official_rom" if (result["project_name"] or "").lower() in MAINSTREAM
                           else "official_rom_scope_unproved")
        elif result["resource_kind"] == "rom" and (result["project_name"] or "").lower() in MAINSTREAM and not unofficial:
            reasons.append("mainstream_release_status_unproved")
        if result["resource_kind"] in ("firmware", "rom") and re.search(r"(?i)\b(?:MIUI|HyperOS)\b", primary) and not unofficial:
            reasons.append("stock_or_custom_scope_unproved")
        rejected = {"explicit_other_target", "target_is_donor", "donor_requires_target_evidence",
                    "showcase_or_guide", "withdrawn_or_discouraged", "excluded_mainstream_official_rom"}
        result["status"] = "rejected" if rejected.intersection(reasons) else "candidate" if reasons else "accepted"
        result["reasons"] = sorted(set(reasons))
        return result


def release_keys(row, reviewed):
    """Strong grouping evidence only. Arbitrary shared dependency URLs are not keys."""
    if reviewed["status"] != "accepted":
        return []
    text = norm(row.get("text") or "")
    attachment = row.get("observed_attachment") or row.get("attachment") or ""
    prefix = [reviewed["device_codename"], reviewed["resource_kind"]]
    keys = []
    checksum = re.search(r"(?i)\b(?:sha[- ]?256|checksum)\s*[:：-]\s*([a-f0-9]{64})\b", text)
    if checksum:
        keys.append(digest([*prefix, "sha256_claim", checksum[1].lower()]))
    if len(text.strip()) >= 80:
        keys.append(digest([*prefix, "caption_and_attachment", text.strip(), attachment]))
    # Same named release/date/maintainer/variant is a duplicate candidate only:
    # missing metadata or same version alone cannot auto-merge builds.
    return keys


def screen_records(rows, target, catalogue=None, profiles=None):
    engine = QualityFilter(catalogue)
    # Profile TEXT is reusable evidence, never the old dedicated-profile judgment.
    # Mixed-model descriptions are independently rejected by devices().
    profile_texts = {}
    for row in rows:
        if row.get("evidence", {}).get("kind") == "dedicated_channel_profile":
            channel = row.get("channel") or row.get("channel_username")
            profile_texts.setdefault(channel, set()).add(row["evidence"].get("value", ""))
    observed_profiles = {channel: "\n".join(sorted(values)) for channel, values in profile_texts.items()}
    observed_profiles.update(profiles or {})
    reviews, canonical, groups = [], {}, {}
    for row in sorted(rows, key=lambda r: r.get("url") or r.get("tg_post_url") or ""):
        channel = row.get("channel") or row.get("channel_username")
        reviewed = engine.review(row, target, observed_profiles.get(channel, ""))
        keys = release_keys(row, reviewed)
        match = next((canonical[key] for key in keys if key in canonical), None)
        if match:
            reviewed["status"] = "duplicate"
            reviewed["duplicate_of"] = match
            reviewed["reasons"] = ["same_announced_checksum_or_full_caption_and_attachment"]
            groups[match].append(reviewed["target_post_url"])
        elif reviewed["status"] == "accepted":
            match = reviewed["target_post_url"]
            groups[match] = [match]
        if match:
            for key in keys:
                canonical[key] = match
        reviews.append(reviewed)
    return {
        "policy_version": POLICY_VERSION, "input_count": len(rows),
        "status_counts": dict(Counter(r["status"] for r in reviews)),
        "reason_counts": dict(Counter(reason for r in reviews for reason in r["reasons"])),
        "resources": [{"canonical_post_url": url, "source_posts": urls} for url, urls in groups.items()],
        "records": reviews, "network_requests": 0, "D1_requests": 0, "llm_calls": 0,
    }
