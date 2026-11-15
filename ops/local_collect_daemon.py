#!/usr/bin/env python3
"""24/7 continuous per-vendor stock firmware collector daemon."""

from __future__ import annotations

import argparse
from collections.abc import Callable
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from typing import Any
import yaml

DEFAULT_FRESHNESS_SECONDS = 1800
DEFAULT_MIN_SLEEP_SECONDS = 30
DEFAULT_MAX_SLEEP_SECONDS = 300
DEFAULT_GIT_PULL_INTERVAL_SECONDS = 1800


def load_enabled_vendors(vendors_yaml_path: Path) -> dict[str, dict[str, Any]]:
    data = yaml.safe_load(vendors_yaml_path.read_text(encoding="utf-8")) or {}
    vendors = data.get("vendors") or {}
    return {
        str(vid).strip().lower(): dict(cfg)
        for vid, cfg in vendors.items()
        if isinstance(cfg, dict) and cfg.get("enabled") is True
    }


def did_batch_hit_cap(
    *,
    last_result: dict[str, Any] | None,
    vendor_cfg: dict[str, Any] | None,
    elapsed_seconds: float,
) -> bool:
    if vendor_cfg is not None:
        max_collect_seconds = vendor_cfg.get("max_collect_seconds")
        if max_collect_seconds is not None and elapsed_seconds >= float(max_collect_seconds):
            return True
    if not last_result:
        return False
    devices_discovered = int(last_result.get("devices_discovered") or 0)
    devices_checked = int(last_result.get("devices_checked") or 0)
    if devices_discovered > 0 and devices_checked < devices_discovered:
        return True
    return False


def compute_vendor_sleep_seconds(
    db_path: Path,
    *,
    now_ts: int,
    freshness_seconds: int = DEFAULT_FRESHNESS_SECONDS,
    min_sleep_seconds: int = DEFAULT_MIN_SLEEP_SECONDS,
    max_sleep_seconds: int = DEFAULT_MAX_SLEEP_SECONDS,
    last_rc: int = 0,
    last_result: dict[str, Any] | None = None,
    vendor_cfg: dict[str, Any] | None = None,
    elapsed_seconds: float = 0.0,
) -> int:
    """Compute adaptive per-vendor sleep seconds after a collector pass."""
    if last_rc != 0:
        return min_sleep_seconds

    if did_batch_hit_cap(
        last_result=last_result,
        vendor_cfg=vendor_cfg,
        elapsed_seconds=elapsed_seconds,
    ):
        return 0

    if not db_path.is_file():
        return 0

    cutoff = now_ts - freshness_seconds
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS total_devices,
                COALESCE(
                    SUM(CASE WHEN last_checked_at = 0 OR last_checked_at < ? THEN 1 ELSE 0 END),
                    0
                ) AS due_devices,
                MIN(last_checked_at) AS oldest_checked_at
            FROM collector_devices
            """,
            (cutoff,),
        ).fetchone()
        total_devices = int(row["total_devices"] or 0) if row is not None else 0
        due_devices = int(row["due_devices"] or 0) if row is not None else 0
        oldest_checked_at = (
            int(row["oldest_checked_at"])
            if row is not None and row["oldest_checked_at"] is not None
            else None
        )

        if total_devices == 0:
            return max_sleep_seconds
        if due_devices > 0:
            return 0

        assert oldest_checked_at is not None
        candidates = [oldest_checked_at + freshness_seconds]

        deferred_row = conn.execute(
            """
            SELECT MIN(next_retry_at) AS next_retry_at
            FROM collector_releases
            WHERE verify_state = 'DEFERRED_VERIFY' AND next_retry_at > 0
            """
        ).fetchone()
        if deferred_row is not None and deferred_row["next_retry_at"] is not None:
            candidates.append(int(deferred_row["next_retry_at"]))

        next_due_at = min(candidates)
        raw_sleep = next_due_at - now_ts
        return max(min_sleep_seconds, min(max_sleep_seconds, int(raw_sleep)))
    except sqlite3.Error:
        return min_sleep_seconds
    finally:
        conn.close()


def _sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sync_git_checkout(runner_dir: Path) -> bool:
    """Pull origin/main non-fatally; return True if daemon/venv files changed and restart is advised."""
    if not (runner_dir / ".git").exists():
        return False

    watched = (
        runner_dir / "requirements.txt",
        runner_dir / "ops" / "local_collect_daemon.py",
        runner_dir / "ops" / "run_local_collect.sh",
    )
    before_hashes = {p: _sha256_file(p) for p in watched}

    try:
        fetch = subprocess.run(
            ["git", "-C", str(runner_dir), "fetch", "--depth=1", "origin", "main"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if fetch.returncode != 0:
            print(
                f"WARNING: git fetch failed (rc={fetch.returncode}): {fetch.stderr.strip()}",
                file=sys.stderr,
                flush=True,
            )
            return False

        reset = subprocess.run(
            ["git", "-C", str(runner_dir), "reset", "--hard", "origin/main"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if reset.returncode != 0:
            print(
                f"WARNING: git reset failed (rc={reset.returncode}): {reset.stderr.strip()}",
                file=sys.stderr,
                flush=True,
            )
            return False
    except Exception as exc:
        print(f"WARNING: git sync error: {exc}", file=sys.stderr, flush=True)
        return False

    after_hashes = {p: _sha256_file(p) for p in watched}
    return before_hashes != after_hashes


def _parse_collect_result(raw_stdout: str) -> dict[str, Any] | None:
    stripped = raw_stdout.strip()
    if not stripped:
        return None
    try:
        data = json.loads(stripped)
        results = data.get("results") or [data]
        if results and isinstance(results[0], dict):
            return results[0]
    except Exception:
        return None
    return None


def _run_vendor_once(
    *,
    vendor: str,
    python_bin: str,
    runner_dir: Path,
    state_dir: Path,
    vendors_yaml_path: Path,
    active_procs: dict[str, subprocess.Popen[str]],
    proc_lock: threading.Lock,
) -> tuple[int, dict[str, Any] | None, float]:
    cmd = [
        python_bin,
        "-m",
        "fmr_ingest_stock.pipeline.cli",
        "--vendors-yaml",
        str(vendors_yaml_path),
        "collect",
        "--vendor",
        vendor,
        "--state-dir",
        str(state_dir),
    ]
    started = time.monotonic()
    proc = subprocess.Popen(
        cmd,
        cwd=str(runner_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    with proc_lock:
        active_procs[vendor] = proc
    try:
        stdout_text, stderr_text = proc.communicate()
    finally:
        with proc_lock:
            active_procs.pop(vendor, None)
    elapsed = time.monotonic() - started

    if stderr_text:
        for line in stderr_text.splitlines():
            print(f"[{vendor} stderr] {line}", file=sys.stderr, flush=True)

    parsed = _parse_collect_result(stdout_text or "")
    if proc.returncode != 0:
        print(f"[{vendor}] FAILED (exit={proc.returncode})", file=sys.stderr, flush=True)
        if stdout_text and stdout_text.strip():
            print(stdout_text.strip(), file=sys.stderr, flush=True)
    elif parsed is not None:
        print(json.dumps(parsed, separators=(",", ":"), sort_keys=True), flush=True)
    elif stdout_text and stdout_text.strip():
        print(f"[{vendor}] {stdout_text.strip()}", flush=True)

    return proc.returncode, parsed, elapsed


def _acquire_daemon_lock(state_dir: Path) -> Any:
    if os.environ.get("FMR_DAEMON_LOCK_HELD") == "1":
        return None
    lock_dir = state_dir / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / "local-collect-daemon.lock"
    fh = open(lock_path, "a+", encoding="utf-8")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        print(
            json.dumps({"status": "skipped_locked", "lock_file": str(lock_path)}),
            flush=True,
        )
        raise SystemExit(0)
    return fh


def run_daemon(
    *,
    runner_dir: Path,
    state_dir: Path,
    vendors_yaml_path: Path,
    once: bool = False,
    freshness_seconds: int = DEFAULT_FRESHNESS_SECONDS,
    min_sleep_seconds: int = DEFAULT_MIN_SLEEP_SECONDS,
    max_sleep_seconds: int = DEFAULT_MAX_SLEEP_SECONDS,
    git_pull_interval_seconds: int = DEFAULT_GIT_PULL_INTERVAL_SECONDS,
    python_bin: str | None = None,
    now_fn: Callable[[], int] | None = None,
) -> int:
    lock_fh = _acquire_daemon_lock(state_dir)
    py_bin = python_bin or sys.executable
    clock = now_fn or (lambda: int(time.time()))

    try:
        enabled_vendors = load_enabled_vendors(vendors_yaml_path)
        if not enabled_vendors:
            print(f"ERROR: No enabled vendors found in {vendors_yaml_path}", file=sys.stderr)
            return 1

        stop_event = threading.Event()
        active_procs: dict[str, subprocess.Popen[str]] = {}
        proc_lock = threading.Lock()
        failures: dict[str, int] = {}
        failures_lock = threading.Lock()

        def _handle_signal(signum: int, _frame: Any) -> None:
            print(f"Received signal {signum}; shutting down collector workers cleanly...", flush=True)
            stop_event.set()
            with proc_lock:
                procs = list(active_procs.values())
            for p in procs:
                if p.poll() is None:
                    try:
                        p.terminate()
                    except OSError:
                        pass

        prev_sigterm = signal.signal(signal.SIGTERM, _handle_signal)
        prev_sigint = signal.signal(signal.SIGINT, _handle_signal)

        try:
            def _worker(vendor: str, initial_cfg: dict[str, Any]) -> None:
                db_path = state_dir / "collectors" / f"{vendor}.sqlite"
                cfg = initial_cfg
                while not stop_event.is_set():
                    try:
                        latest = load_enabled_vendors(vendors_yaml_path)
                        if vendor in latest:
                            cfg = latest[vendor]
                    except Exception:
                        pass

                    rc, parsed, elapsed = _run_vendor_once(
                        vendor=vendor,
                        python_bin=py_bin,
                        runner_dir=runner_dir,
                        state_dir=state_dir,
                        vendors_yaml_path=vendors_yaml_path,
                        active_procs=active_procs,
                        proc_lock=proc_lock,
                    )
                    if rc != 0:
                        with failures_lock:
                            failures[vendor] = rc

                    if once or stop_event.is_set():
                        break

                    sleep_sec = compute_vendor_sleep_seconds(
                        db_path,
                        now_ts=clock(),
                        freshness_seconds=freshness_seconds,
                        min_sleep_seconds=min_sleep_seconds,
                        max_sleep_seconds=max_sleep_seconds,
                        last_rc=rc,
                        last_result=parsed,
                        vendor_cfg=cfg,
                        elapsed_seconds=elapsed,
                    )
                    if sleep_sec > 0:
                        print(
                            json.dumps(
                                {
                                    "vendor": vendor,
                                    "status": "sleeping",
                                    "sleep_seconds": sleep_sec,
                                },
                                separators=(",", ":"),
                                sort_keys=True,
                            ),
                            flush=True,
                        )
                        stop_event.wait(timeout=sleep_sec)

            threads = [
                threading.Thread(
                    target=_worker,
                    args=(vid, cfg),
                    name=f"collect-{vid}",
                    daemon=False,
                )
                for vid, cfg in enabled_vendors.items()
            ]
            for t in threads:
                t.start()

            if once:
                for t in threads:
                    t.join()
                return 1 if failures else 0

            last_git_sync = time.monotonic()
            while not stop_event.is_set():
                if stop_event.wait(timeout=min(30.0, float(git_pull_interval_seconds))):
                    break
                if (time.monotonic() - last_git_sync) >= git_pull_interval_seconds:
                    last_git_sync = time.monotonic()
                    if _sync_git_checkout(runner_dir):
                        print(
                            "Detected runner/requirements update on origin/main; stopping cleanly for systemd restart.",
                            flush=True,
                        )
                        stop_event.set()
                        break

            for t in threads:
                t.join(timeout=30.0)
            return 0
        finally:
            signal.signal(signal.SIGTERM, prev_sigterm)
            signal.signal(signal.SIGINT, prev_sigint)
    finally:
        if lock_fh is not None:
            lock_fh.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="local-collect-daemon",
        description="Continuous 24/7 per-vendor local collector daemon",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--daemon",
        action="store_true",
        help="Run continuous 24/7 per-vendor worker loops (default).",
    )
    mode.add_argument(
        "--once",
        action="store_true",
        help="Run a single parallel pass across all enabled vendors and exit.",
    )
    parser.add_argument(
        "--runner-dir",
        default=os.environ.get("FMR_RUNNER_DIR", "/opt/findmyrom-stock-runner"),
    )
    parser.add_argument(
        "--state-dir",
        default=os.environ.get("FMR_STAGING_V4_DIR", "/var/lib/findmyrom/staging-v4"),
    )
    parser.add_argument("--vendors-yaml", default=None)
    parser.add_argument("--freshness-seconds", type=int, default=DEFAULT_FRESHNESS_SECONDS)
    parser.add_argument("--min-sleep-seconds", type=int, default=DEFAULT_MIN_SLEEP_SECONDS)
    parser.add_argument("--max-sleep-seconds", type=int, default=DEFAULT_MAX_SLEEP_SECONDS)
    parser.add_argument(
        "--git-pull-interval-seconds",
        type=int,
        default=DEFAULT_GIT_PULL_INTERVAL_SECONDS,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    runner_dir = Path(args.runner_dir)
    state_dir = Path(args.state_dir)
    vendors_yaml = Path(args.vendors_yaml) if args.vendors_yaml else (runner_dir / "config" / "vendors.yaml")
    return run_daemon(
        runner_dir=runner_dir,
        state_dir=state_dir,
        vendors_yaml_path=vendors_yaml,
        once=bool(args.once),
        freshness_seconds=args.freshness_seconds,
        min_sleep_seconds=args.min_sleep_seconds,
        max_sleep_seconds=args.max_sleep_seconds,
        git_pull_interval_seconds=args.git_pull_interval_seconds,
    )


if __name__ == "__main__":
    raise SystemExit(main())
