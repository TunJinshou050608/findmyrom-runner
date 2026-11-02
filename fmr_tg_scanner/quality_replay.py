"""Replay a frozen text inventory only; no collector imports, HTTP, LLM or D1."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
from .quality_filter import POLICY_VERSION, screen_records


def check_expectation(value, expected):
    failures = []
    for key, expectation in expected.items():
        if key == "status_in":
            ok = value.get("status") in expectation
        elif key == "title_contains":
            ok = expectation.lower() in (value.get("title") or "").lower()
        elif key == "title_excludes":
            ok = expectation.lower() not in (value.get("title") or "").lower()
        else:
            ok = value.get(key) == expectation
        if not ok:
            failures.append(key)
    return failures


def run(rows, benchmark, catalogue=None):
    before_hash = hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    target = benchmark["target"]
    output = screen_records(rows, target, catalogue)
    originals = {r["url"]: r for r in rows}
    if len(originals) != len(rows):
        raise ValueError("Duplicate input message identities")
    revised = {r["target_post_url"]: r for r in output["records"]}
    references = []
    for check in benchmark["checks"]:
        url = check["url"]
        if url not in originals:
            raise ValueError("Benchmark URL is outside the frozen inventory")
        row = originals[url]
        old = {"status": "accepted", "resource_kind": row["kind"], "title": row["title"],
               "build_version_tag": row["version"], "duplicate_of": None}
        references.append({
            **check, "before_failures": check_expectation(old, check["expect"]),
            "after_failures": check_expectation(revised[url], check["expect"]),
            "before": old, "after": revised[url],
        })
    after_hash = hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    if before_hash != after_hash:
        raise AssertionError("Raw input was modified")
    changed_kinds = Counter()
    title_changes = version_changes = 0
    for url, old in originals.items():
        new = revised[url]
        if new["resource_kind"] != old["kind"]:
            changed_kinds[f'{old["kind"]}->{new["resource_kind"] or "unknown"}'] += 1
        title_changes += old["title"] != new["title"]
        version_changes += old["version"] != new["build_version_tag"]
    comparison = {
        "policy_version": POLICY_VERSION, "input_count": len(rows), "input_sha256": before_hash,
        "input_preserved": True, "acquisition_changed": False,
        "new_fetch_requests": 0, "D1_requests": 0, "llm_calls": 0,
        "before_status_counts": {"accepted": len(rows)}, "after_status_counts": output["status_counts"],
        "kind_transitions": dict(changed_kinds), "changed_titles": title_changes,
        "changed_versions": version_changes, "reference_checks": len(references),
        "before_passed": sum(not c["before_failures"] for c in references),
        "after_passed": sum(not c["after_failures"] for c in references),
        "regressions": [c["url"] for c in references if not c["before_failures"] and c["after_failures"]],
        "references": references,
        "evaluation_scope": benchmark["scope"],
        "independent_precision_or_recall": False,
        "candidate_policy": "Retained for further evidence; not deleted and not auto-published",
    }
    return output, comparison


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="Frozen audit JSON list or records-*.json directory")
    parser.add_argument("--benchmark", required=True)
    parser.add_argument("--catalogue", help="Optional offline codename-to-alias-list JSON")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    source = Path(args.input)
    if source.is_dir():
        rows = []
        for file in sorted(source.glob("records-*.json")):
            rows.extend(json.loads(file.read_text()))
    else:
        rows = json.loads(source.read_text())
    benchmark = json.loads(Path(args.benchmark).read_text())
    catalogue = json.loads(Path(args.catalogue).read_text()) if args.catalogue else None
    output, comparison = run(rows, benchmark, catalogue)
    dest = Path(args.output)
    dest.mkdir(parents=True, exist_ok=True)
    for index in range(0, len(output["records"]), 40):
        (dest / f"screened-{index // 40:03}.json").write_text(
            json.dumps(output["records"][index:index + 40], ensure_ascii=False, indent=2))
    (dest / "resources.json").write_text(json.dumps(output["resources"], ensure_ascii=False, indent=2))
    (dest / "comparison.json").write_text(json.dumps(comparison, ensure_ascii=False, indent=2))
    summary = {k: v for k, v in output.items() if k not in ("records", "resources")}
    (dest / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps({k: comparison[k] for k in (
        "input_count", "before_status_counts", "after_status_counts", "reference_checks",
        "before_passed", "after_passed", "regressions", "new_fetch_requests")}), flush=True)
    if comparison["regressions"] or comparison["after_passed"] != comparison["reference_checks"]:
        raise SystemExit("Previously reviewed references did not all pass; private outputs retained")


if __name__ == "__main__":
    main()
