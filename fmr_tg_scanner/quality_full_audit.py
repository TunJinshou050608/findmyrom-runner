"""Independent all-record evidence checks. Does not import or execute the filter."""
import argparse
from collections import Counter, defaultdict
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import unicodedata


def normalized(text):
    return unicodedata.normalize("NFKC", text or "")


class EvidenceAuditor:
    """A second implementation of role checks; uncertainty remains explicit."""
    def __init__(self, catalogue=None, target="alioth"):
        self.target = target
        names = {
            "alioth": ["POCO F3", "Redmi K40", "Mi 11X"],
            "beryllium": ["POCO F1", "POCOPHONE F1"],
            "apollo": ["Mi 10T", "Mi10T Pro", "Mi 10T Pro"],
            "munch": ["POCO F4"], "vili": ["Mi 11T Pro"],
        }
        for code, labels in (catalogue or {}).items():
            names[code] = list(set([*names.get(code, []), *labels]))
        self.aliases = []
        for code, labels in names.items():
            for label in [code, *labels]:
                for part in re.split(r"\s*/\s*", normalized(label)):
                    compact = re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", part.lower())
                    if len(compact) >= 3:
                        self.aliases.append((code, compact))
        self.lookup = defaultdict(set)
        for code, alias in self.aliases:
            self.lookup[alias].add(code)

    def devices(self, text):
        # Match contiguous sequences of words; retain longest spans independently
        # of the filter's character-regex matcher.
        words = re.findall(r"[a-z0-9\u4e00-\u9fff]+", normalized(text).lower())
        spans = []
        for start in range(len(words)):
            value = ""
            for end in range(start, min(start + 8, len(words))):
                value += words[end]
                if value in self.lookup:
                    spans.append((start, end, self.lookup[value]))
        maximal = [s for s in spans if not any(
            a <= s[0] and b >= s[1] and (a < s[0] or b > s[1]) for a, b, _ in spans)]
        return set().union(*(codes for _, _, codes in maximal)) if maximal else set()

    def device_roles(self, text, attachment, profile):
        direct, donor, uncertain = set(), set(), set()
        citations = []
        for index, line in enumerate(text.splitlines()):
            # Donor clauses end before an explicit recipient clause.
            match = re.search(r"(?i)\b(?:ported?|taken|extracted|based)\s+from\b|移植自|提取自", line)
            if match:
                before = line[:match.start()]
                if index < 3 or re.search(r"(?i)\b(?:for|device)\b", before):
                    direct |= self.devices(before)
                rest = line[match.end():]
                pieces = re.split(r"(?i)\bfor\b|用于|适配", rest, maxsplit=1)
                donor |= self.devices(pieces[0])
                if len(pieces) == 2:
                    direct |= self.devices(pieces[1])
                continue
            if re.match(r"(?i)^\W*(?:follow|join|credits?|thanks|support|subscribe)\b", line):
                continue
            if index < 3 or re.search(r"(?i)\b(?:for|devices?|only|supported|works on|usable on)\b|适配|仅限", line) or line.startswith("#"):
                matches = self.devices(line)
                if "?" in line or "？" in line:
                    uncertain |= matches
                else:
                    direct |= matches
                    if matches:
                        citations.append(line.strip())
        file_devices = self.devices(attachment)
        direct |= file_devices
        profile_devices = self.devices(profile)
        if direct and self.target not in direct:
            return "contradiction", "Explicit recipient/attachment identifies another device", citations
        if self.target in donor and self.target not in direct:
            return "contradiction", "Target appears as donor, not recipient", citations
        if self.target in direct:
            return "supported", "Explicit recipient, tag or attachment", citations
        if self.target in uncertain:
            return "unknown", "Compatibility is explicitly uncertain", citations
        if profile_devices == {self.target}:
            return "supported", "Single-device profile; not independent author verification", [profile]
        return "unknown", "No independent target-role evidence", citations


ROM_NAMES = re.compile(
    r"(?i)\b(?:ROM|AOSP|AICP|AEX|KOSP|DerpFest|LineageOS|PixelExperience|"
    r"Evolution[- ]?X|ProtonPlus|ProtonAOSP|AfterlifeOS|CherishOS|"
    r"Project[- ]?Blaze|Project[- ]?Zephyrus|Project[- ]?Infinity[- ]?X|"
    r"xdroid|ShapeShiftOS|SyberiaOS)\b|[A-Za-z]+(?:OS|Droid)(?=\d|\b)")


def primary_text(text, attachment):
    meaningful = [line.strip() for line in text.splitlines() if line.strip()
                  and not re.fullmatch(r"\s*(?:#[^\s]+\s*)+", line)]
    return (meaningful[0] if meaningful else attachment), meaningful


def independent_kind(text, attachment):
    title, lines = primary_text(text, attachment)
    auxiliary = title + " " + attachment
    if re.search(r"(?i)\b(?:module|patch|injector|bootanimation|bootsplash|remover|theme|dialer|"
                 r"enhancer|config|icons)\b|模块|补丁|Модуль|исправление", auxiliary) or (
                 re.search(r"(?i)\bfix\b", attachment) and not re.search(r"(?i)\b(?:OS|ROM)\b", attachment)):
        return "patch", title
    if re.search(r"(?i)kernel|anykernel", title) or re.match(r"^\d+\.\d+\.\d+-.*\.zip$", attachment):
        return "kernel", title or attachment
    if re.search(r"(?i)^\W*Magisk\b|flashing tool|刷机工具", title):
        return "tool", title
    if re.search(r"(?i)twrp|orangefox|ofrp|recovery", title) and not re.search(
            r"(?i)\bROM\b|^\W*flash\b", title):
        return "recovery", title
    if re.search(r"(?i)(?:init_boot|vendor_boot|boot|dtbo|vbmeta).*\.img(?:\.gz)?$", attachment):
        return "boot_image", attachment
    stripped = re.sub(r"(?i)\bAndroid\b", "", title)
    if ROM_NAMES.search(stripped) or re.search(r"(?i)#ROM\b", "\n".join(text.splitlines()[:4])):
        return "rom", title
    if re.match(r"(?i)^\W*(?:firmware|fw)\b", title):
        return "firmware", title
    if re.search(r"(?i)kernel|anykernel", attachment):
        return "kernel", attachment
    return None, title


def version_role(text, attachment, claim):
    if not claim:
        return "unknown", "No version asserted"
    folded = normalized(claim).casefold()
    evidence, improper = [], []
    for line in normalized(text).splitlines():
        index = line.casefold().find(folded)
        if index < 0:
            continue
        evidence.append(line.strip())
        if re.search(r"(?i)\b(?:clang|gcc|toolchain|driver|graphics|gpu|vulkan)\b", line[:index]):
            improper.append("toolchain_or_driver")
        elif re.search(r"(?i)\b(?:recommend|firmware|fw|requires?|needed?|base)\b", line[:index]):
            improper.append("dependency")
        elif re.search(r"(?i)\bandroid\s*(?:version\s*[:：-]?\s*)?$", line[:index]):
            improper.append("android_version")
        elif re.match(r"(?i)^\W*(?:updated?|released?|build\s*date|date)\b", line) or re.fullmatch(
                r"\W*\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}\W*", line):
            improper.append("date")
        elif re.search(r"(?i)(?:GB|MB|GiB|MiB)\b", line[index + len(claim):]):
            improper.append("size")
        else:
            return "supported", line.strip()
    if folded in normalized(attachment).casefold():
        return "supported", attachment
    if improper:
        return "contradiction", {"roles": improper, "evidence": evidence}
    return "unknown", "Claim absent from caption/actual attachment; external filenames not verified"


def audit_one(row, decision, auditor, profiles, canonical_rows):
    text = normalized(row.get("text"))
    attachment = normalized(row.get("observed_attachment") or "")
    channel = row["channel"]
    checks = {}
    notes = {}
    def put(name, state, evidence=None):
        checks[name] = state
        if evidence is not None:
            notes[name] = evidence
    exact = re.fullmatch(r"https://t\.me/([a-z0-9_]+)/([1-9][0-9]*)", row["url"], re.I)
    put("source_identity", "pass" if exact and exact[1].lower() == channel.lower()
        and int(exact[2]) == row["mid"] and decision["target_post_url"] == row["url"] else "fail")
    raw_hash = hashlib.sha256((row.get("text") or "").encode()).hexdigest()
    put("frozen_body_integrity", "pass" if raw_hash == row["hash"] else "fail")
    title = normalized(decision.get("title"))
    put("title_evidence", "pass" if title and (title in text or title == attachment) else
        "unknown", "Title must bind to caption or actual attachment, not an arbitrary external filename")
    role, evidence, snippets = auditor.device_roles(text, attachment, profiles.get(channel, ""))
    put("device_role", {"supported": "pass", "contradiction": "fail", "unknown": "unknown"}[role],
        {"assessment": evidence, "quotes": snippets})
    kind, kind_evidence = independent_kind(text, attachment)
    actual_kind = decision.get("resource_kind")
    put("primary_resource", "unknown" if kind is None or actual_kind is None else
        "pass" if kind == actual_kind else "fail",
        {"independent_kind": kind, "claim": actual_kind, "quote": kind_evidence})
    role, evidence = version_role(text, attachment, decision.get("build_version_tag"))
    put("version_role", {"supported": "pass", "contradiction": "fail", "unknown": "unknown"}[role], evidence)
    handle = decision.get("maintainer_handle")
    creator_lines = [line for line in text.splitlines() if re.search(
        r"(?i)\b(?:by|maintainer|developer|author|built by)\b|автор|👨|👤|🧑", line)]
    if handle:
        put("maintainer_attribution", "pass" if any(handle.casefold() in line.casefold()
            for line in creator_lines) else "unknown",
            "Public credit is not verified Telegram account identity")
    else:
        put("maintainer_attribution", "unknown", "No maker handle asserted")
    all_sizes = decision.get("sizes") or []
    size_errors = []
    for size in all_sizes:
        if size.get("label") not in text or size.get("verified_bytes") is not False:
            size_errors.append(size)
            continue
        match = re.fullmatch(r"(?i)(\d+(?:[.,]\d+)*)\s*(GiB|MiB|KiB|GB|MB|KB)", size["label"])
        if not match:
            size_errors.append(size)
            continue
        number, unit = match.groups()
        number = number.replace(",", "") if re.fullmatch(r"\d{1,3}(?:,\d{3})+", number) else number.replace(",", ".")
        unit = unit.lower()
        base = 1024 if "i" in unit else 1000
        exponent = {"k": 1, "m": 2, "g": 3}[unit[0]]
        if int(Decimal(number) * base**exponent) != size.get("bytes_claimed"):
            size_errors.append(size)
    put("size_claims", "fail" if size_errors else "pass" if all_sizes else "unknown",
        size_errors or "No binary or attachment-size probe was performed")
    status = decision["status"]
    if status == "accepted":
        put("decision_support", "fail" if checks["device_role"] == "fail" or
            checks["primary_resource"] == "fail" else "unknown" if checks["device_role"] == "unknown"
            or checks["primary_resource"] == "unknown" else "pass")
    elif status == "candidate":
        put("decision_support", "unknown", "Retained without publication; incomplete evidence is not a correctness pass")
    elif status == "rejected":
        reasons = decision.get("reasons", [])
        supported = checks["device_role"] == "fail"
        supported |= any(reason in reasons for reason in
            ("showcase_or_guide", "withdrawn_or_discouraged", "excluded_mainstream_official_rom"))
        false_exclusion = checks["device_role"] == "pass" and "explicit_other_target" in reasons \
            and not any(reason in reasons for reason in
                        ("showcase_or_guide", "withdrawn_or_discouraged", "excluded_mainstream_official_rom"))
        put("decision_support", "fail" if false_exclusion else "pass" if supported else "unknown",
            "Reject reasons must have source evidence; they are not ground-truth labels")
    else:
        parent = canonical_rows.get(decision.get("duplicate_of"))
        if parent is None:
            put("duplicate_integrity", "fail", "Canonical message absent")
        else:
            ptext = normalized(parent.get("text"))
            pat = normalized(parent.get("observed_attachment") or "")
            hashes = lambda value: set(re.findall(r"(?i)\b(?:checksum|sha[- ]?256)\s*[:：-]\s*([a-f0-9]{64})\b", value))
            checksum_match = bool(hashes(text) & hashes(ptext))
            def external(record):
                return {u for u in record.get("observed_links", [])
                        if not re.match(r"(?i)^https?://(?:t\.me|telegram\.me)/", u)}
            caption_match = len(text.strip()) >= 80 and text.strip() == ptext.strip() \
                and attachment == pat and external(row) == external(parent)
            put("duplicate_integrity", "pass" if checksum_match or caption_match else "fail",
                "Equal AOSP/empty captions or shared dependency links alone never prove duplicate")
            put("decision_support", checks["duplicate_integrity"])
    # Invalid compatibility is an exclusion success for rejected messages, not a
    # failure of the screening decision. Keep the field evidence visible either way.
    blocking = [name for name, state in checks.items() if state == "fail" and not
                (status == "rejected" and name in ("device_role", "primary_resource"))]
    unresolved = checks["decision_support"] == "unknown"
    outcome = "conflict" if blocking else "unresolved" if unresolved else "evidence_consistent"
    return {"url": row["url"], "screening_status": status, "audit_outcome": outcome,
            "blocking_checks": blocking, "checks": checks, "evidence": notes,
            "not_independent_ground_truth": True}


def run(rows, decisions, catalogue=None):
    indexed = {d["target_post_url"]: d for d in decisions}
    originals = {r["url"]: r for r in rows}
    if len(rows) != len(originals) or set(indexed) != set(originals) or len(decisions) != len(rows):
        raise ValueError("All input identities must appear exactly once in screening output")
    profiles = defaultdict(set)
    for row in rows:
        evidence = row.get("evidence", {})
        if evidence.get("kind") == "dedicated_channel_profile":
            profiles[row["channel"]].add(evidence.get("value", ""))
    profiles = {key: "\n".join(sorted(values)) for key, values in profiles.items()}
    auditor = EvidenceAuditor(catalogue)
    records = [audit_one(row, indexed[row["url"]], auditor, profiles, originals) for row in rows]
    before = []
    for row in rows:
        old = {"target_post_url": row["url"], "status": "accepted", "title": row["title"],
               "resource_kind": row["kind"], "build_version_tag": row["version"],
               "maintainer_handle": row["maintainer"], "sizes": []}
        before.append(audit_one(row, old, auditor, profiles, originals))
    def totals(items):
        matrix = defaultdict(Counter)
        for record in items:
            for check, state in record["checks"].items():
                matrix[check][state] += 1
        return {"outcomes": dict(Counter(r["audit_outcome"] for r in items)),
                "checks": {key: dict(values) for key, values in matrix.items()},
                "accepted_outcomes": dict(Counter(r["audit_outcome"] for r in items if r["screening_status"] == "accepted"))}
    return {"input_count": len(rows), "tested_count": len(records), "coverage": f"{len(records)}/{len(rows)}",
            "network_requests": 0, "llm_calls": 0, "D1_requests": 0,
            "method": "Independent field/role evidence implementation; not a second run of quality_filter",
            "not_a_human_ground_truth_accuracy_score": True,
            "before": totals(before), "after": totals(records),
            "records": records, "before_records": before}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--screened", required=True)
    parser.add_argument("--catalogue")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    def load_files(path, pattern):
        source = Path(path)
        if source.is_file():
            return json.loads(source.read_text())
        return [row for file in sorted(source.glob(pattern)) for row in json.loads(file.read_text())]
    rows = load_files(args.input, "records-*.json")
    decisions = load_files(args.screened, "screened-*.json")
    catalogue = json.loads(Path(args.catalogue).read_text()) if args.catalogue else None
    report = run(rows, decisions, catalogue)
    dest = Path(args.output); dest.mkdir(parents=True, exist_ok=True)
    for i in range(0, len(report["records"]), 40):
        (dest / f"audit-{i//40:03}.json").write_text(json.dumps(
            report["records"][i:i+40], ensure_ascii=False, indent=2))
    (dest / "conflicts.json").write_text(json.dumps(
        [r for r in report["records"] if r["audit_outcome"] == "conflict"], ensure_ascii=False, indent=2))
    (dest / "before-conflicts.json").write_text(json.dumps(
        [r for r in report["before_records"] if r["audit_outcome"] == "conflict"], ensure_ascii=False, indent=2))
    summary = {k: v for k, v in report.items() if k not in ("records", "before_records")}
    (dest / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()
