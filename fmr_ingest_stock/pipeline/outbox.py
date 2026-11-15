"""Atomic JSONL Outbox spooler (`tmp/` -> `fsync` -> `os.replace` -> `pending/` -> `done/`)."""

from __future__ import annotations

from collections.abc import Sequence
import json
import os
from pathlib import Path
import time
import uuid

from fmr_core.publish_spec import PublishSpecV1

DEFAULT_OUTBOX_BATCH_SIZE = 40
DONE_RETENTION_SECONDS = 72 * 3600  # 72 hours


def _fsync_directory(dir_path: Path) -> None:
    try:
        dir_fd = os.open(str(dir_path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


class OutboxSpool:
    """Crash-safe filesystem outbox decoupling vendor collectors from D1 publishing."""

    def __init__(self, state_dir: str | Path) -> None:
        self.state_dir = Path(state_dir)
        self.root_dir = self.state_dir / "outbox"
        self.tmp_dir = self.root_dir / "tmp"
        self.pending_dir = self.root_dir / "pending"
        self.done_dir = self.root_dir / "done"
        for d in (self.tmp_dir, self.pending_dir, self.done_dir):
            d.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _serialize_specs_jsonl(specs: Sequence[PublishSpecV1]) -> bytes:
        lines = [
            json.dumps(spec.to_dict(), sort_keys=True, separators=(",", ":"))
            for spec in specs
        ]
        return ("\n".join(lines) + "\n").encode("utf-8")

    def enqueue_specs(
        self,
        vendor: str,
        specs: Sequence[PublishSpecV1],
        *,
        batch_size: int = DEFAULT_OUTBOX_BATCH_SIZE,
    ) -> list[Path]:
        """Atomically write verified PublishSpecV1 batches into `outbox/pending/`."""
        if batch_size <= 0:
            raise ValueError(f"batch_size must be positive, got {batch_size}")
        if not specs:
            return []

        clean_vendor = vendor.strip().lower()
        created_files: list[Path] = []

        for offset in range(0, len(specs), batch_size):
            chunk = specs[offset : offset + batch_size]
            epoch_ms = time.time_ns() // 1_000_000
            uid8 = uuid.uuid4().hex[:8]
            filename = f"{clean_vendor}_{epoch_ms}_{uid8}.jsonl"
            tmp_path = self.tmp_dir / filename
            pending_path = self.pending_dir / filename

            payload = self._serialize_specs_jsonl(chunk)
            with open(tmp_path, "wb") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())

            os.replace(tmp_path, pending_path)
            _fsync_directory(self.pending_dir)
            created_files.append(pending_path)

        return created_files

    def list_pending(self) -> list[Path]:
        """Return pending JSONL files sorted in deterministic FIFO order."""
        return sorted(self.pending_dir.glob("*.jsonl"))

    def list_done(self) -> list[Path]:
        return sorted(self.done_dir.glob("*.jsonl"))

    @staticmethod
    def read_specs(path: str | Path) -> list[PublishSpecV1]:
        """Parse all PublishSpecV1 lines from a JSONL outbox file."""
        file_path = Path(path)
        specs: list[PublishSpecV1] = []
        with open(file_path, "r", encoding="utf-8") as fh:
            for line_no, raw_line in enumerate(fh, start=1):
                stripped = raw_line.strip()
                if not stripped:
                    continue
                try:
                    data = json.loads(stripped)
                    specs.append(PublishSpecV1.from_dict(data))
                except Exception as exc:
                    raise ValueError(
                        f"Corrupt PublishSpecV1 in {file_path} at line {line_no}: {exc}"
                    ) from exc
        return specs

    def mark_done(self, pending_path: str | Path) -> Path:
        """Atomically move a fully committed pending file into `outbox/done/`."""
        src = Path(pending_path)
        dst = self.done_dir / src.name
        if dst.exists():
            dst = self.done_dir / f"{src.stem}_{uuid.uuid4().hex[:6]}.jsonl"
        os.replace(src, dst)
        try:
            os.utime(dst, None)
        except OSError:
            pass
        _fsync_directory(self.pending_dir)
        _fsync_directory(self.done_dir)
        return dst

    def rewrite_uncommitted(
        self,
        pending_path: str | Path,
        remaining_specs: Sequence[PublishSpecV1],
    ) -> Path:
        """Atomically replace a partially-published pending file with ONLY uncommitted specs."""
        target = Path(pending_path)
        if not remaining_specs:
            return self.mark_done(target)

        tmp_path = self.tmp_dir / f"{target.stem}_rem_{uuid.uuid4().hex[:8]}.jsonl"
        payload = self._serialize_specs_jsonl(remaining_specs)
        with open(tmp_path, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())

        os.replace(tmp_path, target)
        _fsync_directory(self.pending_dir)
        return target

    def prune_done(
        self,
        max_age_seconds: int = DONE_RETENTION_SECONDS,
        *,
        now_ts: float | None = None,
    ) -> int:
        """Delete `outbox/done/` files older than `max_age_seconds` (default 72h)."""
        ref_now = time.time() if now_ts is None else float(now_ts)
        cutoff = ref_now - max(0, int(max_age_seconds))
        pruned = 0
        for path in self.done_dir.glob("*.jsonl"):
            try:
                st = path.stat()
                if st.st_mtime < cutoff:
                    path.unlink()
                    pruned += 1
            except FileNotFoundError:
                continue
        return pruned


__all__ = [
    "DEFAULT_OUTBOX_BATCH_SIZE",
    "DONE_RETENTION_SECONDS",
    "OutboxSpool",
]
