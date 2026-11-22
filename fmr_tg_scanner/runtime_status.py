"""Honest business status tracking for TG fetch, clean, and publish roles (v2 Section 3.2).

Strictly separates:
1. 'Ran to completion with zero new items' -> business success (updates last_business_success_at, exit 0).
2. 'Failed with unhandled error / missing prerequisite' -> business failure (preserves previous
   last_business_success_at, never backfills with old reports, exits non-zero to stop self-trigger loops).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import time
from typing import Any


def _status_path(root: Path, role: str, worker: int | None = None) -> Path:
    suffix = f"{role}-{worker}" if (role == "fetch" and worker is not None) else role
    return root / f"status-{suffix}.json"


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp"
    raw = (json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    with open(tmp, "wb") as fh:
        fh.write(raw)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def read_role_status(root: str | Path, role: str, worker: int | None = None) -> dict[str, Any]:
    path = _status_path(Path(root), role, worker)
    if not path.is_file():
        return {
            "role": role,
            "worker": worker,
            "last_business_success_at": None,
            "last_attempt_at": None,
            "last_status": "never_run",
            "has_new_items": False,
            "consecutive_failures": 0,
            "last_error": None,
        }
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {
            "role": role,
            "worker": worker,
            "last_business_success_at": None,
            "last_attempt_at": None,
            "last_status": "corrupt_status_file",
            "has_new_items": False,
            "consecutive_failures": 0,
            "last_error": None,
        }


def record_business_success(
    root: str | Path,
    role: str,
    *,
    worker: int | None = None,
    has_new_items: bool = False,
    summary: dict[str, Any] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    root_path = Path(root)
    now_ts = time.time() if now is None else float(now)
    prev = read_role_status(root_path, role, worker)
    payload = {
        "role": role,
        "worker": worker,
        "last_business_success_at": now_ts,
        "last_attempt_at": now_ts,
        "last_status": "ok_with_changes" if has_new_items else "ok_no_changes",
        "has_new_items": bool(has_new_items),
        "consecutive_failures": 0,
        "last_error": None,
        "previous_business_success_at": prev.get("last_business_success_at"),
        "summary": summary or {},
    }
    _atomic_write(_status_path(root_path, role, worker), payload)
    return payload


def record_business_failure(
    root: str | Path,
    role: str,
    error: BaseException | str,
    *,
    worker: int | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    root_path = Path(root)
    now_ts = time.time() if now is None else float(now)
    prev = read_role_status(root_path, role, worker)
    err_str = (
        f"{type(error).__name__}: {error}"
        if isinstance(error, BaseException)
        else str(error)
    )
    payload = {
        "role": role,
        "worker": worker,
        "last_business_success_at": prev.get("last_business_success_at"),
        "last_attempt_at": now_ts,
        "last_status": "failed",
        "has_new_items": False,
        "consecutive_failures": int(prev.get("consecutive_failures") or 0) + 1,
        "last_error": err_str[:500],
    }
    _atomic_write(_status_path(root_path, role, worker), payload)
    return payload
