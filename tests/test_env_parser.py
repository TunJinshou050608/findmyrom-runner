"""Unit tests for the custom .env parser in seed_dev_data.py."""

import os
import tempfile
from seed_dev_data import parse_env_line, load_env_file_if_present


def test_parse_env_line_basic():
    assert parse_env_line("FOO=bar") == ("FOO", "bar")
    assert parse_env_line("  FOO = bar  ") == ("FOO", "bar")


def test_parse_env_line_export_prefix():
    assert parse_env_line("export FOO=bar") == ("FOO", "bar")
    assert parse_env_line("export\tFOO=bar") == ("FOO", "bar")
    assert parse_env_line("export   DB_NAME=test_db") == ("DB_NAME", "test_db")


def test_parse_env_line_inline_comment():
    assert parse_env_line("FOO=bar # this is a comment") == ("FOO", "bar")
    assert parse_env_line("FOO=bar\t# this is a tab comment") == ("FOO", "bar")


def test_parse_env_line_unquoted_url_with_hash():
    # Hashes without leading space (e.g. URLs, hex color codes) are preserved
    assert parse_env_line("URL=https://example.com/page#section") == ("URL", "https://example.com/page#section")
    assert parse_env_line("COLOR=#ffffff") == ("COLOR", "#ffffff")


def test_parse_env_line_quoted_values_with_hashes():
    assert parse_env_line('FOO="bar # not a comment"') == ("FOO", "bar # not a comment")
    assert parse_env_line("FOO='bar # not a comment'") == ("FOO", "bar # not a comment")


def test_parse_env_line_escaped_quotes():
    assert parse_env_line(r'MSG="hello \"world\""') == ("MSG", 'hello "world"')


def test_parse_env_line_empty_and_comments():
    assert parse_env_line("") is None
    assert parse_env_line("   ") is None
    assert parse_env_line("# full line comment") is None
    assert parse_env_line("INVALID_LINE_NO_EQUALS") is None


def test_load_env_file_if_present_integration(monkeypatch):
    with tempfile.NamedTemporaryFile("w+", suffix=".env", delete=False) as tf:
        tf.write("""
        # Sample configuration
        export TEST_PARSER_NEW_VAR=hello_world
        export\tTEST_PARSER_TAB_VAR=tab_value
        TEST_PARSER_URL=https://example.com/foo#bar
        TEST_PARSER_COMMENT=value # inline comment
        TEST_PARSER_EXISTING=from_file
        """)
        tf_path = tf.name

    try:
        # Pre-set TEST_PARSER_EXISTING to prove shell env precedence
        monkeypatch.setenv("TEST_PARSER_EXISTING", "from_shell")
        monkeypatch.delenv("TEST_PARSER_NEW_VAR", raising=False)
        monkeypatch.delenv("TEST_PARSER_TAB_VAR", raising=False)
        monkeypatch.delenv("TEST_PARSER_URL", raising=False)
        monkeypatch.delenv("TEST_PARSER_COMMENT", raising=False)

        loaded = load_env_file_if_present(tf_path)

        assert loaded["TEST_PARSER_NEW_VAR"] == "hello_world"
        assert loaded["TEST_PARSER_TAB_VAR"] == "tab_value"
        assert loaded["TEST_PARSER_URL"] == "https://example.com/foo#bar"
        assert loaded["TEST_PARSER_COMMENT"] == "value"
        assert loaded["TEST_PARSER_EXISTING"] == "from_file"

        # Verify os.environ: new vars populated, existing vars untouched
        assert os.environ["TEST_PARSER_NEW_VAR"] == "hello_world"
        assert os.environ["TEST_PARSER_TAB_VAR"] == "tab_value"
        assert os.environ["TEST_PARSER_URL"] == "https://example.com/foo#bar"
        assert os.environ["TEST_PARSER_COMMENT"] == "value"
        assert os.environ["TEST_PARSER_EXISTING"] == "from_shell"

        # Non-existent file handles gracefully
        assert load_env_file_if_present("/nonexistent/file/path/.env") == {}
    finally:
        if os.path.exists(tf_path):
            os.remove(tf_path)
