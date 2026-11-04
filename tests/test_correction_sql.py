from fmr_tg_scanner.correction_sql import build, sql_literal, statement


def record(decision='apply', proposed=None, old=None):
    return {
        'id': 42,
        'decision': decision,
        'proposed': proposed or {'project_name': 'LineageOS'},
        'old': old or {
            'project_name': 'LineageOS 18.1',
            'project_version': '0.4',
            'android_version': '11',
        },
    }


def test_statement_guards_on_all_old_fields():
    sql = statement(record())
    assert sql.startswith('UPDATE custom_resources SET project_name=\'LineageOS\' WHERE ')
    assert 'id=42' in sql
    assert "project_name IS 'LineageOS 18.1'" in sql
    assert "build_version_tag IS '0.4'" in sql
    assert "android_version IS '11'" in sql


def test_null_guard_is_null_safe():
    sql = statement(record(old={
        'project_name': 'LineageOS 18.1',
        'project_version': None,
        'android_version': '11',
    }))
    assert 'build_version_tag IS NULL' in sql


def test_single_quotes_are_escaped():
    assert sql_literal("Ken's ROM") == "'Ken''s ROM'"
    sql = statement(record(proposed={'project_name': "Ken's ROM"}))
    assert "project_name='Ken''s ROM'" in sql


def test_review_rows_are_skipped():
    statements, chunks = build([record(decision='review'), record()])
    assert len(statements) == 1
    assert len(chunks) == 1


def test_chunking():
    statements, chunks = build([record() for _ in range(5)], chunk_size=2)
    assert len(statements) == 5
    assert [len(chunk) for chunk in chunks] == [2, 2, 1]
