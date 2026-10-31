"""FindMyROM Database Client.

Dual-mode client supporting:
1. Local SQLite file (development and local integration testing).
2. Remote Cloudflare D1 REST API (GitHub Actions / production runner).
"""

from __future__ import annotations

import os
import re
import sqlite3
import time
from typing import Any, Sequence

import requests

_RE_TRIGGER = re.compile(r"\bCREATE\s+(?:TEMP\s+|TEMPORARY\s+)?TRIGGER\b", re.IGNORECASE)
_RE_BEGIN = re.compile(r"\bBEGIN\b", re.IGNORECASE)
_RE_CASE = re.compile(r"\bCASE\b", re.IGNORECASE)
_RE_END = re.compile(r"\bEND\b", re.IGNORECASE)
_RE_TXN_STMT = re.compile(
    r"^(?:BEGIN(?:\s+TRANSACTION|\s+IMMEDIATE|\s+EXCLUSIVE|\s+DEFERRED)?|COMMIT(?:\s+TRANSACTION)?|END(?:\s+TRANSACTION)?|ROLLBACK(?:\s+TRANSACTION)?|ROLLBACK\s+TO(?:\s+SAVEPOINT)?\s+\w+|SAVEPOINT\s+\w+|RELEASE(?:\s+SAVEPOINT)?\s+\w+)$",
    re.IGNORECASE,
)


class D1BatchError(RuntimeError):
    """Raised when one or more statements in a D1 batch fail.

    In multi-chunk operations over Cloudflare D1 REST API, statements are executed in
    sequential atomic HTTP chunks. If a subsequent chunk fails, Cloudflare D1 has already
    committed earlier chunks. This exception provides full visibility into partial progress:
    - offset: The global index (0-based) of the failed statement.
    - failed_sql: The SQL statement that failed.
    - error: The error payload or message returned by D1.
    - executed_chunks: Number of chunks successfully committed before failure.
    - total_changes: Number of row changes committed by earlier chunks before failure.
    """

    def __init__(
        self,
        message: str,
        offset: int,
        failed_sql: str,
        error: Any,
        executed_chunks: int = 0,
        total_changes: int = 0,
    ) -> None:
        super().__init__(message)
        self.offset = offset
        self.failed_sql = failed_sql
        self.error = error
        self.executed_chunks = executed_chunks
        self.total_changes = total_changes


def split_sql_statements(script: str) -> list[str]:
    """Split a SQL script into discrete statements.

    Properly handles:
    - Single quote string literals ('...') with escaped quotes ('' or \\')
    - Double quote identifiers ("...") with escaped quotes ("" or \\")
    - Backtick identifiers (`...`)
    - Bracket identifiers ([...])
    - Single line comments (--) and block comments (/* ... */) only when outside quotes
    - Standalone transactions (BEGIN, BEGIN TRANSACTION, COMMIT, ROLLBACK)
    - SQLite CREATE TRIGGER compound statement bodies (BEGIN ... END;) including nested CASE ... END expressions
    """
    statements: list[str] = []
    current: list[str] = []

    in_single_quote = False
    in_double_quote = False
    in_backtick = False
    in_bracket = False
    in_line_comment = False
    in_block_comment = False

    in_trigger_decl = False
    in_trigger_body = False
    case_depth = 0

    i = 0
    n = len(script)

    while i < n:
        c = script[i]
        next_c = script[i + 1] if i + 1 < n else ""

        # 1. Handle comments (only when outside quotes and identifiers)
        if (
            not in_single_quote
            and not in_double_quote
            and not in_backtick
            and not in_bracket
        ):
            if in_line_comment:
                if c == "\n":
                    in_line_comment = False
                    current.append(c)
                i += 1
                continue
            if in_block_comment:
                if c == "*" and next_c == "/":
                    in_block_comment = False
                    i += 2
                    continue
                i += 1
                continue
            if c == "-" and next_c == "-":
                in_line_comment = True
                i += 2
                continue
            if c == "/" and next_c == "*":
                in_block_comment = True
                i += 2
                continue

        # 2. Handle quotes & identifiers (only when outside comments)
        if not in_line_comment and not in_block_comment:
            if (
                c == "'"
                and not in_double_quote
                and not in_backtick
                and not in_bracket
            ):
                if in_single_quote and next_c == "'":
                    current.append("''")
                    i += 2
                    continue
                in_single_quote = not in_single_quote
                current.append(c)
                i += 1
                continue

            if (
                c == '"'
                and not in_single_quote
                and not in_backtick
                and not in_bracket
            ):
                if in_double_quote and next_c == '"':
                    current.append('""')
                    i += 2
                    continue
                in_double_quote = not in_double_quote
                current.append(c)
                i += 1
                continue

            if (
                c == "`"
                and not in_single_quote
                and not in_double_quote
                and not in_bracket
            ):
                in_backtick = not in_backtick
                current.append(c)
                i += 1
                continue

            if (
                c == "["
                and not in_single_quote
                and not in_double_quote
                and not in_backtick
            ):
                in_bracket = True
                current.append(c)
                i += 1
                continue

            if c == "]" and in_bracket:
                in_bracket = False
                current.append(c)
                i += 1
                continue

            # 3. Handle keyword detection outside quotes/comments (O(n) via regex position matching without slicing)
            if (
                not in_single_quote
                and not in_double_quote
                and not in_backtick
                and not in_bracket
            ):
                # Detect CREATE TRIGGER declaration
                if not in_trigger_decl and not in_trigger_body:
                    if _RE_TRIGGER.match(script, i):
                        in_trigger_decl = True

                # When inside trigger declaration, BEGIN starts the trigger body
                if in_trigger_decl and not in_trigger_body:
                    if _RE_BEGIN.match(script, i):
                        in_trigger_decl = False
                        in_trigger_body = True
                        case_depth = 0

                # Inside trigger body, track nested CASE ... END vs trigger END
                elif in_trigger_body:
                    if _RE_CASE.match(script, i):
                        case_depth += 1
                    elif _RE_END.match(script, i):
                        if case_depth > 0:
                            case_depth -= 1
                        else:
                            in_trigger_body = False  # Trigger procedural body terminated

            # 4. Semicolon splitting (only when outside quotes/comments AND outside trigger body)
            if (
                c == ";"
                and not in_single_quote
                and not in_double_quote
                and not in_backtick
                and not in_bracket
                and not in_trigger_body
            ):
                stmt = "".join(current).strip()
                if stmt:
                    statements.append(stmt)
                current = []
                in_trigger_decl = False
                in_trigger_body = False
                case_depth = 0
                i += 1
                continue

        current.append(c)
        i += 1

    last_stmt = "".join(current).strip()
    if last_stmt:
        statements.append(last_stmt)

    return statements


def is_idempotent_statement(sql: str) -> bool:
    """Determine whether a SQL statement is strictly idempotent and safe to automatically retry.

    Considered safe to retry:
    - Read queries: SELECT, PRAGMA, EXPLAIN
    - Explicit upserts: INSERT OR REPLACE, INSERT OR IGNORE, or containing ON CONFLICT
    - Idempotent DDL: CREATE ... IF NOT EXISTS, DROP ... IF EXISTS

    NOT safe to blindly retry:
    - Arbitrary UPDATE (e.g. UPDATE t SET cnt = cnt + 1)
    - Arbitrary DELETE (e.g. DELETE FROM t LIMIT 1)
    - Plain INSERT (e.g. INSERT INTO t VALUES (...))
    """
    normalized = sql.strip().upper()
    if normalized.startswith(("SELECT", "PRAGMA", "EXPLAIN")):
        return True
    if "INSERT OR REPLACE" in normalized or "INSERT OR IGNORE" in normalized:
        return True
    if "ON CONFLICT" in normalized:
        return True
    if normalized.startswith(
        ("CREATE TABLE IF NOT EXISTS", "CREATE INDEX IF NOT EXISTS", "DROP TABLE IF EXISTS", "DROP INDEX IF EXISTS")
    ):
        return True
    return False


class D1Client:
    """Unified client for local SQLite and remote Cloudflare D1."""

    def __init__(
        self,
        local_sqlite_path: str | None = None,
        cf_account_id: str | None = None,
        cf_api_token: str | None = None,
        cf_database_id: str | None = None,
        timeout: int = 30,
    ) -> None:
        self.timeout = timeout
        self._sqlite_conn: sqlite3.Connection | None = None
        self.session: requests.Session | None = None

        # Resolve mode: explicit argument > environment variable
        if local_sqlite_path is not None:
            self.mode = "sqlite"
            self.local_path = local_sqlite_path
            self.cf_account_id = None
            self.cf_api_token = None
            self.cf_database_id = None
        elif any(arg is not None for arg in (cf_account_id, cf_api_token, cf_database_id)):
            self.cf_account_id = cf_account_id or os.environ.get("CF_ACCOUNT_ID")
            self.cf_api_token = cf_api_token or os.environ.get("CF_API_TOKEN")
            self.cf_database_id = cf_database_id or os.environ.get("CF_D1_DATABASE_ID")
            if not (self.cf_account_id and self.cf_api_token and self.cf_database_id):
                raise ValueError(
                    "D1Client initialization failed: incomplete Cloudflare D1 credentials provided. "
                    "Ensure CF_ACCOUNT_ID, CF_API_TOKEN, and CF_D1_DATABASE_ID are all set."
                )
            self.mode = "cloudflare_d1"
            self.local_path = None
            self.endpoint = (
                f"https://api.cloudflare.com/client/v4/accounts/"
                f"{self.cf_account_id}/d1/database/{self.cf_database_id}/query"
            )
            self.session = requests.Session()
            self.session.headers.update(
                {
                    "Authorization": f"Bearer {self.cf_api_token}",
                    "Content-Type": "application/json",
                }
            )
        elif os.environ.get("LOCAL_SQLITE_PATH"):
            self.mode = "sqlite"
            self.local_path = os.environ.get("LOCAL_SQLITE_PATH")
            self.cf_account_id = None
            self.cf_api_token = None
            self.cf_database_id = None
        elif (
            os.environ.get("CF_ACCOUNT_ID")
            and os.environ.get("CF_API_TOKEN")
            and os.environ.get("CF_D1_DATABASE_ID")
        ):
            self.mode = "cloudflare_d1"
            self.local_path = None
            self.cf_account_id = os.environ.get("CF_ACCOUNT_ID")
            self.cf_api_token = os.environ.get("CF_API_TOKEN")
            self.cf_database_id = os.environ.get("CF_D1_DATABASE_ID")
            self.endpoint = (
                f"https://api.cloudflare.com/client/v4/accounts/"
                f"{self.cf_account_id}/d1/database/{self.cf_database_id}/query"
            )
            self.session = requests.Session()
            self.session.headers.update(
                {
                    "Authorization": f"Bearer {self.cf_api_token}",
                    "Content-Type": "application/json",
                }
            )
        else:
            raise ValueError(
                "D1Client initialization failed: either local_sqlite_path (or LOCAL_SQLITE_PATH env var) "
                "or full Cloudflare D1 credentials (CF_ACCOUNT_ID, CF_API_TOKEN, CF_D1_DATABASE_ID) must be configured."
            )

    def _get_sqlite_conn(self) -> sqlite3.Connection:
        if self._sqlite_conn is None:
            assert self.local_path is not None
            dir_name = os.path.dirname(self.local_path)
            if dir_name:
                os.makedirs(dir_name, exist_ok=True)
            # isolation_level=None enables autocommit mode, compatible with Python 3.11 and 3.12+
            self._sqlite_conn = sqlite3.connect(
                self.local_path,
                check_same_thread=False,
                isolation_level=None,
            )
            self._sqlite_conn.row_factory = sqlite3.Row
            self._sqlite_conn.execute("PRAGMA foreign_keys = ON;")
        return self._sqlite_conn

    def query(
        self,
        sql: str,
        params: Sequence[Any] = (),
    ) -> list[dict[str, Any]]:
        """Execute a read query and return rows as dictionaries."""
        if self.mode == "sqlite":
            conn = self._get_sqlite_conn()
            cursor = conn.cursor()
            cursor.execute(sql, tuple(params))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

        return self._cf_query(sql, params)

    def execute(
        self,
        sql: str,
        params: Sequence[Any] = (),
        is_idempotent: bool | None = None,
    ) -> dict[str, Any]:
        """Execute a write statement (INSERT, UPDATE, DELETE)."""
        if self.mode == "sqlite":
            conn = self._get_sqlite_conn()
            cursor = conn.cursor()
            cursor.execute(sql, tuple(params))
            return {
                "changes": cursor.rowcount,
                "last_row_id": cursor.lastrowid,
                "success": True,
            }

        safe_to_retry = is_idempotent if is_idempotent is not None else is_idempotent_statement(sql)
        return self._cf_execute(sql, params, is_safe_to_retry=safe_to_retry)

    def execute_batch(self, statements: Sequence[tuple[str, Sequence[Any]]]) -> dict[str, Any]:
        """Execute one heterogeneous atomic batch (no implicit chunk splitting)."""
        if not statements:
            return {"changes": 0, "success": True}
        if self.mode != "sqlite":
            return self._cf_batch(
                [{"sql": sql, "params": list(params)} for sql, params in statements],
                is_safe_to_retry=all(is_idempotent_statement(sql) for sql, _ in statements),
                max_batch_size=len(statements),
            )
        conn = self._get_sqlite_conn()
        conn.execute("BEGIN")
        try:
            changes = 0
            for sql, params in statements:
                changes += max(0, conn.execute(sql, tuple(params)).rowcount)
            conn.execute("COMMIT")
            return {"changes": changes, "success": True}
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def executemany(
        self,
        sql: str,
        params_list: Sequence[Sequence[Any]],
        is_idempotent: bool | None = None,
        max_batch_size: int = 100,
    ) -> int:
        """Execute a parameterized statement over multiple sets of parameters.

        Atomicity guarantees:
        - SQLite mode: 100% transactional atomicity. Wrapped inside an explicit transaction
          (`BEGIN TRANSACTION ... COMMIT`) with automatic `ROLLBACK` on any error (e.g. unique
          constraint violation), leaving zero partial rows.
        - Cloudflare D1 mode:
          - If len(params_list) <= max_batch_size, dispatched in 1 single HTTP request, executed
            100% atomically within D1's implicit batch transaction.
          - If len(params_list) > max_batch_size, dispatched in sequential atomic chunks. If a
            subsequent chunk fails, earlier chunks remain committed in D1, and a `D1BatchError`
            is raised with `executed_chunks` and `total_changes` recorded so far.
        """
        if not isinstance(max_batch_size, int) or max_batch_size <= 0:
            raise ValueError(f"max_batch_size must be a positive integer, got {max_batch_size!r}")

        if not params_list:
            return 0

        if self.mode == "sqlite":
            conn = self._get_sqlite_conn()
            cursor = conn.cursor()
            cursor.execute("BEGIN TRANSACTION;")
            try:
                cursor.executemany(sql, [tuple(p) for p in params_list])
                rowcount = cursor.rowcount
                cursor.execute("COMMIT;")
                return rowcount
            except Exception:
                try:
                    cursor.execute("ROLLBACK;")
                except Exception:
                    pass
                raise

        safe_to_retry = is_idempotent if is_idempotent is not None else is_idempotent_statement(sql)
        batch_items = [{"sql": sql, "params": list(p)} for p in params_list]
        res = self._cf_batch(batch_items, is_safe_to_retry=safe_to_retry, max_batch_size=max_batch_size)
        return res.get("changes", 0)

    def execute_script(self, sql_script: str, max_batch_size: int = 100) -> None:
        """Execute multiple SQL statements in a script.

        Atomicity guarantees:
        - SQLite mode: 100% transactional DDL & DML atomicity. Wrapped inside an explicit
          transaction (`BEGIN TRANSACTION ... COMMIT`) with automatic `ROLLBACK` on any error,
          ensuring partial scripts are completely reverted.
        - Cloudflare D1 mode:
          - If total statement count <= max_batch_size (e.g. standard migrations), dispatched in
            a single batch HTTP request, executed 100% atomically within D1's implicit batch transaction.
          - If statement count > max_batch_size, dispatched in sequential chunks; each chunk is
            executed atomically by D1, but D1 REST API does not support multi-request interactive
            transactions across chunks. If a chunk fails, a `D1BatchError` is raised detailing
            the failure offset and previous committed changes.
        """
        if not isinstance(max_batch_size, int) or max_batch_size <= 0:
            raise ValueError(f"max_batch_size must be a positive integer, got {max_batch_size!r}")

        if self.mode == "sqlite":
            conn = self._get_sqlite_conn()
            statements = split_sql_statements(sql_script)
            cursor = conn.cursor()
            # Wrap in an explicit transaction to ensure true all-or-nothing atomicity.
            cursor.execute("BEGIN TRANSACTION;")
            try:
                for stmt in statements:
                    clean = stmt.strip()
                    if not clean or _RE_TXN_STMT.match(clean):
                        continue
                    cursor.execute(clean)
                cursor.execute("COMMIT;")
            except Exception:
                try:
                    cursor.execute("ROLLBACK;")
                except Exception:
                    pass
                raise
            return

        statements = split_sql_statements(sql_script)
        clean_stmts = [
            s.strip()
            for s in statements
            if s.strip() and not _RE_TXN_STMT.match(s.strip())
        ]
        if not clean_stmts:
            return

        # Cloudflare D1 batch operates within an implicit atomic transaction
        batch_items = [{"sql": stmt} for stmt in clean_stmts]
        self._cf_batch(batch_items, is_safe_to_retry=False, max_batch_size=max_batch_size)

    def _cf_batch(
        self,
        batch_items: list[dict[str, Any]],
        is_safe_to_retry: bool = False,
        max_batch_size: int = 100,
    ) -> dict[str, Any]:
        """Execute a batch of statements in atomic HTTP request(s) to Cloudflare D1.

        Cloudflare D1 executes each single batch request atomically within an implicit transaction.
        If statement count > max_batch_size, statements are chunked sequentially.
        Fails fast if any individual statement returns success=False or if results are incomplete.
        """
        if not isinstance(max_batch_size, int) or max_batch_size <= 0:
            raise ValueError(f"max_batch_size must be a positive integer, got {max_batch_size!r}")

        if not batch_items:
            return {"changes": 0, "last_row_id": None, "success": True, "results": [], "raw_results": []}

        total_changes = 0
        last_row_id = None
        all_results = []
        all_raw = []

        for chunk_idx, offset in enumerate(range(0, len(batch_items), max_batch_size)):
            chunk = batch_items[offset : offset + max_batch_size]
            payload = {"batch": chunk}
            try:
                response_data = self._request_cf_with_retry(payload, is_safe_to_retry=is_safe_to_retry)
            except Exception as e:
                if getattr(e,'fmr_batch_deadline',False):raise
                raise D1BatchError(
                    f"Cloudflare D1 batch HTTP request failed at chunk {chunk_idx} (offset {offset}): {e}",
                    offset=offset,
                    failed_sql=chunk[0].get("sql", "<unknown>"),
                    error=str(e),
                    executed_chunks=chunk_idx,
                    total_changes=total_changes,
                ) from e

            result_array = response_data.get("result", [])

            if not result_array or not isinstance(result_array, list):
                raise D1BatchError(
                    f"Cloudflare D1 batch returned empty or invalid result array for {len(chunk)} statement(s)",
                    offset=offset,
                    failed_sql=chunk[0].get("sql", "<unknown>"),
                    error=response_data.get("errors") or "Empty or invalid result array",
                    executed_chunks=chunk_idx,
                    total_changes=total_changes,
                )

            if len(result_array) != len(chunk):
                raise D1BatchError(
                    f"Cloudflare D1 batch returned incomplete results: expected {len(chunk)} results for chunk, got {len(result_array)}",
                    offset=offset,
                    failed_sql=chunk[0].get("sql", "<unknown>"),
                    error=f"Expected {len(chunk)} results, got {len(result_array)}",
                    executed_chunks=chunk_idx,
                    total_changes=total_changes,
                )

            # Inspect statement-level execution status. Fail fast if ANY statement failed!
            for idx, r in enumerate(result_array):
                if isinstance(r, dict) and r.get("success") is False:
                    err = r.get("error") or r.get("errors") or "Statement execution failed"
                    failed_sql = chunk[idx].get("sql", "<unknown>")
                    raise D1BatchError(
                        f"Cloudflare D1 batch statement failed at statement {offset + idx}: {err}. SQL: {failed_sql}",
                        offset=offset + idx,
                        failed_sql=failed_sql,
                        error=err,
                        executed_chunks=chunk_idx,
                        total_changes=total_changes,
                    )

            chunk_changes = sum(
                r.get("meta", {}).get("changes", 0) for r in result_array if isinstance(r, dict)
            )
            total_changes += chunk_changes

            for r in reversed(result_array):
                if isinstance(r, dict) and "meta" in r and "last_row_id" in r["meta"]:
                    last_row_id = r["meta"]["last_row_id"]
                    break

            for r in result_array:
                if isinstance(r, dict):
                    all_results.append(r.get("results", []))
                    all_raw.append(r)

        return {
            "changes": total_changes,
            "last_row_id": last_row_id,
            "success": True,
            "results": all_results,
            "raw_results": all_raw,
        }

    def _cf_query(self, sql: str, params: Sequence[Any]) -> list[dict[str, Any]]:
        payload = {"sql": sql, "params": list(params)}
        response_data = self._request_cf_with_retry(payload, is_safe_to_retry=True)
        result_array = response_data.get("result", [])
        if not result_array or not isinstance(result_array, list):
            raise RuntimeError("Cloudflare D1 query returned empty or invalid statement results")

        for item in result_array:
            if not isinstance(item, dict) or item.get("success") is False:
                raise RuntimeError("Cloudflare D1 query statement failed")

        # Return rows aggregated across all statement results in the response
        all_rows: list[dict[str, Any]] = []
        for item in result_array:
            if isinstance(item, dict):
                rows = item.get("results") or []
                all_rows.extend(rows)
        return all_rows

    def _cf_execute(
        self, sql: str, params: Sequence[Any], is_safe_to_retry: bool = False
    ) -> dict[str, Any]:
        payload = {"sql": sql, "params": list(params)}
        response_data = self._request_cf_with_retry(payload, is_safe_to_retry=is_safe_to_retry)
        result_array = response_data.get("result", [])
        if not result_array or not isinstance(result_array, list):
            raise RuntimeError("Cloudflare D1 execute returned empty or invalid statement results")

        for result in result_array:
            if not isinstance(result, dict) or result.get("success") is False:
                raise RuntimeError("Cloudflare D1 execute statement failed")

        total_changes = sum(
            r.get("meta", {}).get("changes", 0) for r in result_array if isinstance(r, dict)
        )
        last_row_id = None
        for r in reversed(result_array):
            if isinstance(r, dict) and "meta" in r and "last_row_id" in r["meta"]:
                last_row_id = r["meta"]["last_row_id"]
                break

        all_results = [r.get("results", []) for r in result_array if isinstance(r, dict)]
        return {
            "changes": total_changes,
            "last_row_id": last_row_id,
            "success": True,
            "results": all_results[0] if len(all_results) == 1 else all_results,
            "raw_results": result_array,
        }

    def _request_cf_with_retry(
        self,
        payload: dict[str, Any],
        is_safe_to_retry: bool = False,
        max_retries: int = 3,
    ) -> dict[str, Any]:
        assert self.session is not None, "HTTP session not initialized for Cloudflare D1"
        last_error = None
        retries = max_retries if is_safe_to_retry else 1

        for attempt in range(retries):
            try:
                resp = self.session.post(
                    self.endpoint,
                    json=payload,
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                data = resp.json()
                if not data.get("success", False):
                    errors = data.get("errors", [])
                    raise RuntimeError(f"Cloudflare D1 query failed: {errors}")
                return data
            except requests.exceptions.HTTPError as http_err:
                status_code = getattr(http_err.response, "status_code", 0)
                if 400 <= status_code < 500:
                    raise RuntimeError(f"Cloudflare D1 HTTP {status_code} client error: {http_err}") from http_err
                last_error = http_err
                if attempt < retries - 1:
                    time.sleep(0.5 * (attempt + 1))
            except Exception as e:
                last_error = e
                if attempt < retries - 1:
                    time.sleep(0.5 * (attempt + 1))

        raise RuntimeError(
            f"Cloudflare D1 request failed after {retries} attempt(s): {last_error}"
        )

    def close(self) -> None:
        """Safely close active SQLite connection or requests session."""
        if self._sqlite_conn is not None:
            self._sqlite_conn.close()
            self._sqlite_conn = None
        if self.session is not None:
            self.session.close()
            self.session = None


__all__ = [
    "D1Client",
    "D1BatchError",
    "split_sql_statements",
    "is_idempotent_statement",
]

