"""Unit tests for D1Client local SQLite and remote Cloudflare D1 modes."""

import os
import sqlite3
import tempfile
from unittest.mock import MagicMock, patch
import pytest
import requests

from fmr_core.d1_client import (
    D1BatchError,
    D1Client,
    is_idempotent_statement,
    split_sql_statements,
)


@pytest.fixture
def temp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    if os.path.exists(path):
        os.remove(path)


def test_d1_client_sqlite_crud(temp_db):
    client = D1Client(local_sqlite_path=temp_db)
    assert client.mode == "sqlite"

    # Schema
    client.execute(
        "CREATE TABLE test_items (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, score INTEGER);"
    )

    # Insert
    res = client.execute("INSERT INTO test_items (name, score) VALUES (?, ?);", ("Alice", 100))
    assert res["changes"] == 1
    assert res["last_row_id"] == 1

    # Executemany
    batch_res = client.executemany(
        "INSERT INTO test_items (name, score) VALUES (?, ?);",
        [("Bob", 90), ("Charlie", 80)],
    )
    assert batch_res == 2

    # Query
    rows = client.query("SELECT * FROM test_items ORDER BY score DESC;")
    assert len(rows) == 3
    assert rows[0]["name"] == "Alice"
    assert rows[0]["score"] == 100
    assert rows[1]["name"] == "Bob"
    assert rows[2]["name"] == "Charlie"

    # Execute script
    client.execute_script(
        """
        CREATE TABLE script_table (id INTEGER PRIMARY KEY, note TEXT);
        INSERT INTO script_table VALUES (1, 'hello; world');
        INSERT INTO script_table VALUES (2, 'line 2');
        """
    )
    script_rows = client.query("SELECT count(*) as count FROM script_table;")
    assert script_rows[0]["count"] == 2

    client.close()
    assert client._sqlite_conn is None


def test_d1_client_unconfigured_raises_error():
    """Verify fail-fast behavior when no local path or CF credentials are provided."""
    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(ValueError, match="D1Client initialization failed"):
            D1Client()


def test_split_sql_statements_advanced_boundaries():
    script = """
    -- Comment with a semicolon; here
    CREATE TABLE t1 (id INT, val TEXT);
    /* Block comment with ;
       another line ;
    */
    INSERT INTO t1 VALUES (1, 'first; with semicolon');
    INSERT INTO t1 VALUES (2, 'second; ''quoted'' ; test');
    INSERT INTO t1 VALUES (3, 'contains--double-dash');
    INSERT INTO t1 VALUES (4, 'contains/*block*/comment');
    SELECT `col;name` FROM [tbl;name];

    BEGIN;
    INSERT INTO t1 VALUES (5, 'txn1');
    COMMIT;

    BEGIN IMMEDIATE;
    INSERT INTO t1 VALUES (6, 'txn2');
    COMMIT;

    CREATE TRIGGER update_order_log AFTER INSERT ON t1
    BEGIN
        INSERT INTO t1_log VALUES (new.id, CASE WHEN new.val IS NULL THEN 'empty' ELSE new.val END);
        INSERT INTO t1_stats VALUES (new.id, 'done;log');
    END;
    """
    stmts = split_sql_statements(script)
    assert len(stmts) == 13
    assert "CREATE TABLE t1" in stmts[0]
    assert "first; with semicolon" in stmts[1]
    assert "second; ''quoted'' ; test" in stmts[2]
    assert "contains--double-dash" in stmts[3]
    assert "contains/*block*/comment" in stmts[4]
    assert "`col;name`" in stmts[5]
    # Transactions are split into individual statements
    assert stmts[6] == "BEGIN"
    assert "INSERT INTO t1 VALUES (5, 'txn1')" in stmts[7]
    assert stmts[8] == "COMMIT"
    assert stmts[9] == "BEGIN IMMEDIATE"
    assert "INSERT INTO t1 VALUES (6, 'txn2')" in stmts[10]
    assert stmts[11] == "COMMIT"
    # CREATE TRIGGER is preserved as 1 composite statement despite internal CASE ... END and semicolons
    assert "CREATE TRIGGER update_order_log" in stmts[12]
    assert "CASE WHEN new.val" in stmts[12]
    assert "INSERT INTO t1_stats VALUES (new.id, 'done;log')" in stmts[12]


def test_is_idempotent_statement():
    # Read-only queries are idempotent
    assert is_idempotent_statement("SELECT * FROM devices") is True
    assert is_idempotent_statement("PRAGMA integrity_check") is True
    assert is_idempotent_statement("EXPLAIN QUERY PLAN SELECT 1") is True

    # Explicit upserts and conflict handlers are idempotent
    assert is_idempotent_statement("INSERT INTO devices VALUES (1) ON CONFLICT(id) DO UPDATE SET x=1") is True
    assert is_idempotent_statement("INSERT OR REPLACE INTO devices VALUES (1)") is True
    assert is_idempotent_statement("INSERT OR IGNORE INTO devices VALUES (1)") is True

    # Safe DDL is idempotent
    assert is_idempotent_statement("CREATE TABLE IF NOT EXISTS tbl (id INT)") is True
    assert is_idempotent_statement("DROP TABLE IF EXISTS tbl") is True

    # Non-idempotent operations must NOT be auto-retried
    assert is_idempotent_statement("UPDATE devices SET cnt = cnt + 1") is False
    assert is_idempotent_statement("UPDATE devices SET name = 'foo'") is False
    assert is_idempotent_statement("DELETE FROM devices WHERE id = 1") is False
    assert is_idempotent_statement("INSERT INTO devices (name) VALUES ('raw_insert')") is False


def test_d1_client_remote_mode_lifecycle_and_close():
    client = D1Client(
        cf_account_id="acc123",
        cf_api_token="token456",
        cf_database_id="db789",
    )
    assert client.mode == "cloudflare_d1"
    assert client.session is not None
    assert client._sqlite_conn is None

    client.close()
    assert client.session is None


def test_d1_client_remote_mock_operations():
    client = D1Client(
        cf_account_id="acc123",
        cf_api_token="token456",
        cf_database_id="db789",
    )

    # 1. Mock response for single statement query
    mock_query_resp = MagicMock()
    mock_query_resp.raise_for_status.return_value = None
    mock_query_resp.json.return_value = {
        "success": True,
        "result": [
            {
                "success": True,
                "results": [{"id": 1, "name": "marble"}],
                "meta": {"changes": 0, "last_row_id": 0},
            }
        ],
    }

    with patch.object(client.session, "post", return_value=mock_query_resp) as mock_post:
        rows = client.query("SELECT * FROM devices WHERE codename = ?", ("marble",))
        assert len(rows) == 1
        assert rows[0]["name"] == "marble"
        assert mock_post.call_count == 1
        mock_post.assert_called_with(
            client.endpoint,
            json={"sql": "SELECT * FROM devices WHERE codename = ?", "params": ["marble"]},
            timeout=client.timeout,
        )

    # 2. Mock response for multi-result / multi-statement query
    mock_multi_query_resp = MagicMock()
    mock_multi_query_resp.raise_for_status.return_value = None
    mock_multi_query_resp.json.return_value = {
        "success": True,
        "result": [
            {
                "success": True,
                "results": [{"id": 1, "name": "marble"}],
                "meta": {"changes": 0},
            },
            {
                "success": True,
                "results": [{"id": 2, "name": "corot"}],
                "meta": {"changes": 0},
            },
        ],
    }

    with patch.object(client.session, "post", return_value=mock_multi_query_resp) as mock_post:
        rows = client.query("SELECT * FROM t1; SELECT * FROM t2;")
        assert len(rows) == 2
        assert rows[0]["name"] == "marble"
        assert rows[1]["name"] == "corot"
        assert mock_post.call_count == 1

    # 3. Mock response for single execute
    mock_exec_resp = MagicMock()
    mock_exec_resp.raise_for_status.return_value = None
    mock_exec_resp.json.return_value = {
        "success": True,
        "result": [
            {
                "success": True,
                "results": [],
                "meta": {"changes": 1, "last_row_id": 42},
            }
        ],
    }

    with patch.object(client.session, "post", return_value=mock_exec_resp) as mock_post:
        exec_res = client.execute("INSERT OR REPLACE INTO devices (id, name) VALUES (?, ?)", (1, "marble"))
        assert exec_res["changes"] == 1
        assert exec_res["last_row_id"] == 42
        assert exec_res["success"] is True
        assert mock_post.call_count == 1

    # 4. Mock response for batch executemany (verifies SINGLE HTTP request)
    mock_batch_resp = MagicMock()
    mock_batch_resp.raise_for_status.return_value = None
    mock_batch_resp.json.return_value = {
        "success": True,
        "result": [
            {"success": True, "meta": {"changes": 1, "last_row_id": 1}},
            {"success": True, "meta": {"changes": 1, "last_row_id": 2}},
            {"success": True, "meta": {"changes": 1, "last_row_id": 3}},
        ],
    }

    with patch.object(client.session, "post", return_value=mock_batch_resp) as mock_post:
        total_changes = client.executemany(
            "INSERT OR REPLACE INTO devices (id, name) VALUES (?, ?)",
            [(1, "a"), (2, "b"), (3, "c")],
        )
        assert total_changes == 3
        # Strict single HTTP request verification
        assert mock_post.call_count == 1
        mock_post.assert_called_with(
            client.endpoint,
            json={
                "batch": [
                    {"sql": "INSERT OR REPLACE INTO devices (id, name) VALUES (?, ?)", "params": [1, "a"]},
                    {"sql": "INSERT OR REPLACE INTO devices (id, name) VALUES (?, ?)", "params": [2, "b"]},
                    {"sql": "INSERT OR REPLACE INTO devices (id, name) VALUES (?, ?)", "params": [3, "c"]},
                ]
            },
            timeout=client.timeout,
        )

    # 5. Mock response for atomic execute_script (verifies transaction stripping & SINGLE batch HTTP request)
    mock_script_resp = MagicMock()
    mock_script_resp.raise_for_status.return_value = None
    mock_script_resp.json.return_value = {
        "success": True,
        "result": [
            {"success": True, "meta": {"changes": 1}},
            {"success": True, "meta": {"changes": 1}},
        ],
    }

    with patch.object(client.session, "post", return_value=mock_script_resp) as mock_post:
        script = """
        BEGIN TRANSACTION;
        INSERT INTO items VALUES (1, 'item1');
        INSERT INTO items VALUES (2, 'item2');
        COMMIT;
        """
        client.execute_script(script)
        # Strict single HTTP request verification
        assert mock_post.call_count == 1
        # Manual transaction wrappers (BEGIN, COMMIT) stripped; statements dispatched in atomic batch
        mock_post.assert_called_with(
            client.endpoint,
            json={
                "batch": [
                    {"sql": "INSERT INTO items VALUES (1, 'item1')"},
                    {"sql": "INSERT INTO items VALUES (2, 'item2')"},
                ]
            },
            timeout=client.timeout,
        )

    client.close()


def test_d1_client_remote_retry_distinction():
    """Verify that non-idempotent statements are not retried, while idempotent statements are retried."""
    client = D1Client(
        cf_account_id="acc123",
        cf_api_token="token456",
        cf_database_id="db789",
    )

    mock_500 = MagicMock()
    mock_500.status_code = 500
    http_error_500 = requests.exceptions.HTTPError(response=mock_500)

    # A. Non-idempotent statement (UPDATE counter): fails immediately without retrying
    with patch.object(client.session, "post", side_effect=http_error_500) as mock_post:
        with pytest.raises(RuntimeError):
            client.execute("UPDATE stats SET cnt = cnt + 1 WHERE id = 1;")
        assert mock_post.call_count == 1  # Only 1 attempt, no duplicate retry

    # B. Idempotent statement (SELECT or ON CONFLICT): retries on transient 500 and recovers
    mock_success = MagicMock()
    mock_success.raise_for_status.return_value = None
    mock_success.json.return_value = {
        "success": True,
        "result": [{"results": [{"id": 1}], "success": True}],
    }

    with patch.object(client.session, "post", side_effect=[http_error_500, mock_success]) as mock_post:
        rows = client.query("SELECT 1 as id;")
        assert len(rows) == 1
        assert mock_post.call_count == 2  # Retried on attempt 2

    client.close()


def test_d1_client_sqlite_execute_script_atomic_rollback(temp_db):
    """Verify that SQLite execute_script rolls back all statements atomically on error."""
    client = D1Client(local_sqlite_path=temp_db)
    failing_script = """
    CREATE TABLE atomic_test (id INT, val TEXT);
    INSERT INTO atomic_test VALUES (1, 'success');
    INSERT INTO nonexistent_missing_table VALUES (2, 'fail');
    """
    import sqlite3
    with pytest.raises(sqlite3.OperationalError):
        client.execute_script(failing_script)

    # Prove complete rollback: table atomic_test must NOT exist in the database
    rows = client.query("SELECT name FROM sqlite_master WHERE type='table' AND name='atomic_test';")
    assert len(rows) == 0, "Expected atomic_test to be rolled back on script failure!"
    client.close()


def test_d1_client_remote_batch_statement_failure_raises():
    """Verify that a statement-level failure in a remote batch raises RuntimeError."""
    client = D1Client(
        cf_account_id="acc123",
        cf_api_token="token456",
        cf_database_id="db789",
    )

    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = {
        "success": True,
        "result": [
            {"success": True, "meta": {"changes": 1}},
            {"success": False, "error": "no such column: missing_col"},
        ],
    }

    with patch.object(client.session, "post", return_value=mock_resp):
        with pytest.raises(RuntimeError, match="batch statement failed.*missing_col"):
            client.executemany("INSERT INTO t VALUES (?, ?)", [(1, "a"), (2, "b")])

        with pytest.raises(RuntimeError, match="batch statement failed.*missing_col"):
            client.execute_script("INSERT INTO t VALUES (1); INSERT INTO t VALUES (2);")

    client.close()


def test_d1_client_remote_batch_empty_result_raises():
    """Verify that an empty result array returned for a non-empty batch raises RuntimeError."""
    client = D1Client(
        cf_account_id="acc123",
        cf_api_token="token456",
        cf_database_id="db789",
    )

    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = {"success": True, "result": []}

    with patch.object(client.session, "post", return_value=mock_resp):
        with pytest.raises(RuntimeError, match="returned empty or invalid result array"):
            client.executemany("INSERT INTO t VALUES (?)", [(1,), (2,)])

    client.close()


def test_d1_client_remote_batch_chunking():
    """Verify that batches larger than max_batch_size are correctly chunked into multiple HTTP calls."""
    client = D1Client(
        cf_account_id="acc123",
        cf_api_token="token456",
        cf_database_id="db789",
    )

    def make_mock_resp(chunk_size):
        m = MagicMock()
        m.raise_for_status.return_value = None
        m.json.return_value = {
            "success": True,
            "result": [{"success": True, "meta": {"changes": 1}} for _ in range(chunk_size)],
        }
        return m

    # 7 items with max_batch_size=3 -> 3 chunks (3, 3, 1)
    mock_resps = [make_mock_resp(3), make_mock_resp(3), make_mock_resp(1)]

    with patch.object(client.session, "post", side_effect=mock_resps) as mock_post:
        items = [(i,) for i in range(7)]
        total_changes = client.executemany("INSERT INTO t VALUES (?)", items, max_batch_size=3)
        assert total_changes == 7
        assert mock_post.call_count == 3
        # Check chunk sizes
        assert len(mock_post.call_args_list[0].kwargs["json"]["batch"]) == 3
        assert len(mock_post.call_args_list[1].kwargs["json"]["batch"]) == 3
        assert len(mock_post.call_args_list[2].kwargs["json"]["batch"]) == 1

    client.close()


def test_d1_client_execute_script_filters_savepoint_and_release():
    """Verify that SAVEPOINT, RELEASE, and ROLLBACK TO are filtered out from D1 batch requests."""
    client = D1Client(
        cf_account_id="acc123",
        cf_api_token="token456",
        cf_database_id="db789",
    )

    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = {
        "success": True,
        "result": [{"success": True, "meta": {"changes": 1}}],
    }

    script = """
    SAVEPOINT sp1;
    INSERT INTO test_tbl VALUES (10);
    RELEASE SAVEPOINT sp1;
    ROLLBACK TO sp1;
    """

    with patch.object(client.session, "post", return_value=mock_resp) as mock_post:
        client.execute_script(script)
        assert mock_post.call_count == 1
        batch = mock_post.call_args_list[0].kwargs["json"]["batch"]
        # Only the actual statement should remain in the batch
        assert len(batch) == 1
        assert batch[0]["sql"] == "INSERT INTO test_tbl VALUES (10)"

    client.close()


def test_d1_client_sqlite_executemany_atomic_rollback(tmp_path):
    """Verify that local SQLite executemany rolls back completely on constraint failure."""
    db_file = str(tmp_path / "executemany_atomic.db")
    client = D1Client(local_sqlite_path=db_file)
    client.execute("CREATE TABLE t (id INTEGER PRIMARY KEY);")

    # Attempt inserting duplicate keys: 1, 1 (collision), 2
    with pytest.raises(sqlite3.IntegrityError):
        client.executemany("INSERT INTO t VALUES (?);", [(1,), (1,), (2,)])

    # Assert complete rollback: 0 rows exist
    rows = client.query("SELECT * FROM t;")
    assert len(rows) == 0, "Expected all rows to be rolled back on executemany failure!"
    client.close()


def test_d1_client_batch_size_validation():
    """Verify that non-positive or non-integer max_batch_size raises ValueError with clear message."""
    client = D1Client(cf_account_id="acc", cf_api_token="tok", cf_database_id="db")
    for invalid_val in [0, -1, -50, "100", None]:
        with pytest.raises(ValueError, match="max_batch_size must be a positive integer"):
            client.executemany("INSERT INTO t VALUES (?)", [(1,)], max_batch_size=invalid_val)
        with pytest.raises(ValueError, match="max_batch_size must be a positive integer"):
            client.execute_script("INSERT INTO t VALUES (1);", max_batch_size=invalid_val)
    client.close()


def test_d1_client_remote_batch_incomplete_results_raises():
    """Verify that receiving fewer results than statements in a chunk raises D1BatchError."""
    client = D1Client(cf_account_id="acc", cf_api_token="tok", cf_database_id="db")
    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    # Chunk has 2 statements, but result array only returns 1
    mock_resp.json.return_value = {
        "success": True,
        "result": [{"success": True, "meta": {"changes": 1}}],
    }
    with patch.object(client.session, "post", return_value=mock_resp):
        with pytest.raises(D1BatchError) as exc_info:
            client.executemany("INSERT INTO t VALUES (?)", [(1,), (2,)])
        assert "incomplete results" in str(exc_info.value)
        assert exc_info.value.executed_chunks == 0
        assert exc_info.value.total_changes == 0
    client.close()


def test_d1_client_remote_batch_chunking_failure_semantics():
    """Verify chunking failure semantics: partial commit tracking in D1BatchError."""
    client = D1Client(cf_account_id="acc", cf_api_token="tok", cf_database_id="db")

    # Case 1: Chunk 1 succeeds (3 items), Chunk 2 fails at first statement
    def make_success_resp(n):
        m = MagicMock()
        m.raise_for_status.return_value = None
        m.json.return_value = {
            "success": True,
            "result": [{"success": True, "meta": {"changes": 1}} for _ in range(n)],
        }
        return m

    fail_resp = MagicMock()
    fail_resp.raise_for_status.return_value = None
    fail_resp.json.return_value = {
        "success": True,
        "result": [
            {"success": False, "error": "UNIQUE constraint failed: t.id"},
            {"success": True, "meta": {"changes": 1}},
            {"success": True, "meta": {"changes": 1}},
        ],
    }

    # 7 items with max_batch_size=3 -> chunk 1 (3 items), chunk 2 (3 items), chunk 3 (1 item)
    with patch.object(client.session, "post", side_effect=[make_success_resp(3), fail_resp]) as mock_post:
        items = [(i,) for i in range(7)]
        with pytest.raises(D1BatchError) as exc_info:
            client.executemany("INSERT INTO t VALUES (?)", items, max_batch_size=3)

        err = exc_info.value
        # Assert fast-fail after chunk 2; chunk 3 was never called
        assert mock_post.call_count == 2
        # Chunk 1 was committed by D1 before chunk 2 failed
        assert err.executed_chunks == 1
        assert err.total_changes == 3
        # Failed at global offset 3 (statement 0 of chunk 2)
        assert err.offset == 3
        assert "UNIQUE constraint failed" in str(err)
        assert "UNIQUE constraint failed" in str(err.error)

    # Case 2: Chunk 1 fails immediately
    with patch.object(client.session, "post", side_effect=[fail_resp]) as mock_post:
        items = [(i,) for i in range(7)]
        with pytest.raises(D1BatchError) as exc_info:
            client.executemany("INSERT INTO t VALUES (?)", items, max_batch_size=3)

        err = exc_info.value
        assert mock_post.call_count == 1
        assert err.executed_chunks == 0
        assert err.total_changes == 0
        assert err.offset == 0

    client.close()

