"""v4 decoupled stock-firmware Collector, RangeProbeVerifier, Outbox, and Quota-Safe Publisher."""

from __future__ import annotations

from .collector import COLLECTOR_SCHEMA_SQL, run_collector
from .locks import LockBusyError, acquire_file_lock
from .outbox import DEFAULT_OUTBOX_BATCH_SIZE, DONE_RETENTION_SECONDS, OutboxSpool
from .publisher import LEDGER_SCHEMA_SQL, get_pipeline_status, run_publisher
from .verifier import (
    DEAD_UPSTREAM_TTL_SECONDS,
    DEFAULT_MIN_FULL_BYTES,
    DEFAULT_MIN_INCREMENTAL_BYTES,
    ProbeOutcome,
    RangeProbeVerifier,
    UnofficialDownloadURLError,
    compute_deferred_backoff,
    format_file_size,
)

__all__ = [
    "COLLECTOR_SCHEMA_SQL",
    "DEAD_UPSTREAM_TTL_SECONDS",
    "DEFAULT_MIN_FULL_BYTES",
    "DEFAULT_MIN_INCREMENTAL_BYTES",
    "DEFAULT_OUTBOX_BATCH_SIZE",
    "DONE_RETENTION_SECONDS",
    "LEDGER_SCHEMA_SQL",
    "LockBusyError",
    "OutboxSpool",
    "ProbeOutcome",
    "RangeProbeVerifier",
    "UnofficialDownloadURLError",
    "acquire_file_lock",
    "compute_deferred_backoff",
    "format_file_size",
    "get_pipeline_status",
    "run_collector",
    "run_publisher",
]
