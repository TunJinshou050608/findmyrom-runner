"""Tests for honest business status tracking and non-zero failure exits (v2 Section 3.2)."""
import json
import pytest

from fmr_tg_scanner.local_runtime import loop
from fmr_tg_scanner.runtime_status import (
    read_role_status,
    record_business_failure,
    record_business_success,
)


def test_runtime_status_distinguishes_no_changes_success_from_failure(tmp_path):
    s1 = record_business_success(tmp_path, "fetch", worker=0, has_new_items=False, now=1000.0)
    assert s1["last_business_success_at"] == 1000.0
    assert s1["last_status"] == "ok_no_changes"

    f1 = record_business_failure(tmp_path, "fetch", RuntimeError("catalogue missing"), worker=0, now=1200.0)
    assert f1["last_status"] == "failed"
    assert f1["last_business_success_at"] == 1000.0
    assert f1["consecutive_failures"] == 1
    assert "catalogue missing" in f1["last_error"]

    loaded = read_role_status(tmp_path, "fetch", worker=0)
    assert loaded["last_business_success_at"] == 1000.0
    assert loaded["last_status"] == "failed"


def test_local_runtime_loop_exits_nonzero_on_error_instead_of_swallowing(tmp_path, monkeypatch):
    (tmp_path / "initialized.json").write_text(json.dumps({"workers": 4}))
    import fmr_tg_scanner.search_watch as sw

    def boom(*args, **kwargs):
        raise RuntimeError("Local device catalogue is missing or empty")

    monkeypatch.setattr(sw, "produce", boom)
    with pytest.raises(SystemExit) as exc:
        loop(tmp_path, "fetch", worker=0, duration=5, period=1)
    assert exc.value.code == 1
    status = read_role_status(tmp_path, "fetch", worker=0)
    assert status["last_status"] == "failed"
    assert "missing or empty" in status["last_error"]
