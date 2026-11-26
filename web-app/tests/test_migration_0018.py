"""
Migration nommée 0018_routing_coverage_state (lot Z2 : routing-zone-donnees ZD-2, ADR-0012), sur une base RÉELLE.

Les deux tables (état et catalogue) sont créées par LA MÊME migration, à une seule ligne chacune (`id = 1`) ; aucune donnée existante
n'est touchée ; la ligne d'état naît « sources historiques » (zone active vide), statut `idle` ; la migration est idempotente et ne
dépend ni du registre des réglages ni du service de routage (copie figée, comme 0017).
"""

import ast
import json
import sqlite3
from pathlib import Path

import pytest

from app import db as database
from tests.pw_harness import connect, make_app

MIGRATION = '0018_routing_coverage_state'
STATE_COLUMNS = {'id', 'active_polygon', 'active_extract_bbox', 'active_regions', 'active_graph', 'data_date', 'task_tiles', 'status',
                 'reason', 'status_at', 'request_id', 'request_polygon', 'claimed_at', 'claim_id', 'claim_payload', 'last_failure_code',
                 'catalog_at', 'dem_wakeup', 'budget_day', 'budget_builds', 'budget_bytes', 'last_report_hash', 'invalid_report_hash'}


def old_database(tmp_path):
    """Base d'avant Z2 dans `<tmp_path>/data` (migrations 0001 à 0017 jouées), avec des données qui ne doivent pas bouger."""
    folder = tmp_path / 'data'
    folder.mkdir(parents=True, exist_ok=True)
    path = str(folder / 'kmlviewer.sqlite3')
    conn = database.connect(path)
    conn.executescript(database.SCHEMA)
    database.migrate(conn, named=[m for m in database.NAMED_MIGRATIONS if m[0] != MIGRATION])
    conn.execute("INSERT INTO settings (key, value, updated_at, updated_by) VALUES ('routing.max_concurrent', '3', '2026-01-01T00:00:00Z', 'admin')")
    conn.execute("INSERT INTO audit_log (at, actor_type, actor_id, actor_label, action, target_type, target_id, details) "
                 "VALUES ('2026-01-01T00:00:00Z', 'cli', NULL, 'cli', 'setting.update', 'setting', 'routing.max_concurrent', '{}')")
    conn.commit()
    assert conn.execute("SELECT 1 FROM schema_migrations WHERE name = ?", (MIGRATION,)).fetchone() is None
    assert not conn.execute("SELECT name FROM sqlite_master WHERE name IN ('routing_coverage_state', 'routing_catalog')").fetchall()
    conn.close()
    return path


def table_rows(conn, table):
    return [tuple(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY 1")]


def test_the_migration_is_registered_after_0017_and_creates_both_tables_with_one_born_row_for_the_state(tmp_path):
    names = [name for name, _ in database.NAMED_MIGRATIONS]
    assert MIGRATION in names and names.index(MIGRATION) == names.index('0017_coverage_polygon_only') + 1
    old_database(tmp_path)
    app = make_app(tmp_path)
    conn = connect(app)
    assert conn.execute("SELECT 1 FROM schema_migrations WHERE name = ?", (MIGRATION,)).fetchone()
    assert {r['name'] for r in conn.execute("PRAGMA table_info(routing_coverage_state)")} >= STATE_COLUMNS
    rows = conn.execute("SELECT * FROM routing_coverage_state").fetchall()
    assert len(rows) == 1
    row = dict(rows[0])
    assert row['id'] == 1 and row['status'] == 'idle' and row['active_polygon'] is None            # « sources historiques »
    assert json.loads(row['active_regions']) == [] and json.loads(row['task_tiles']) == []
    assert row['request_id'] == 0 and row['claim_id'] is None and row['last_failure_code'] is None and row['dem_wakeup'] == 0
    assert conn.execute("SELECT COUNT(*) FROM routing_catalog").fetchone()[0] == 0               # catalogue : aucune ligne tant que l'hôte ne l'a pas envoyé
    assert {r['name'] for r in conn.execute("PRAGMA table_info(routing_catalog)")} >= {'id', 'content', 'sha256', 'loaded_at'}
    conn.close()


def test_no_existing_data_is_touched_and_the_zone_setting_is_unchanged(tmp_path):
    path = old_database(tmp_path)
    conn = database.connect(path)
    before = {t: table_rows(conn, t) for t in ('settings', 'audit_log', 'settings_meta', 'users', 'activities')}
    tables_before = {r['name'] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    conn.close()
    make_app(tmp_path)
    conn = database.connect(path)
    after = {t: table_rows(conn, t) for t in before}
    assert after == before
    assert {r['name'] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")} - tables_before == {
        'routing_coverage_state', 'routing_catalog'}
    conn.close()


def test_replaying_the_migration_changes_nothing(tmp_path):
    path = old_database(tmp_path)
    make_app(tmp_path)
    conn = database.connect(path)
    conn.execute("UPDATE routing_coverage_state SET status = 'pending', request_id = 4, active_regions = '[\"belgium\"]'")
    conn.execute("INSERT INTO routing_catalog (id, content, sha256, loaded_at) VALUES (1, '[]', 'abc', '2026-11-26T12:00:00Z')")
    conn.commit()
    snapshot = (table_rows(conn, 'routing_coverage_state'), table_rows(conn, 'routing_catalog'))
    conn.execute("DELETE FROM schema_migrations WHERE name = ?", (MIGRATION,))
    conn.commit()
    database.migrate(conn)
    assert (table_rows(conn, 'routing_coverage_state'), table_rows(conn, 'routing_catalog')) == snapshot       # ni écrasé, ni dupliqué
    assert conn.execute("SELECT 1 FROM schema_migrations WHERE name = ?", (MIGRATION,)).fetchone()
    conn.close()


def test_a_new_database_has_the_tables_and_the_migration_registered(tmp_path):
    app = make_app(tmp_path)
    conn = connect(app)
    assert conn.execute("SELECT COUNT(*) FROM routing_coverage_state").fetchone()[0] == 1
    assert conn.execute("SELECT 1 FROM schema_migrations WHERE name = ?", (MIGRATION,)).fetchone()
    conn.close()


def test_each_table_holds_one_row_only(tmp_path):
    conn = connect(make_app(tmp_path))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO routing_coverage_state (id) VALUES (2)")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO routing_catalog (id, content, sha256, loaded_at) VALUES (2, '[]', 'x', 'y')")
    conn.rollback()
    conn.close()


@pytest.mark.parametrize('column, value', [('status', 'running'), ('status', ''), ('reason', 'weekly'), ('dem_wakeup', 2)])
def test_the_database_refuses_a_status_or_a_reason_the_spec_does_not_know(tmp_path, column, value):
    conn = connect(make_app(tmp_path))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(f"UPDATE routing_coverage_state SET {column} = ?", (value,))
    conn.rollback()
    conn.close()


@pytest.mark.parametrize('status', ['idle', 'pending', 'building', 'starting', 'failed'])
@pytest.mark.parametrize('reason', ['zone', 'mensuel', 'activation'])
def test_every_status_and_every_reason_of_the_spec_is_accepted(tmp_path, status, reason):
    conn = connect(make_app(tmp_path))
    conn.execute("UPDATE routing_coverage_state SET status = ?, reason = ?", (status, reason))
    conn.rollback()
    conn.close()


def test_the_migration_is_a_frozen_copy_it_imports_neither_the_settings_registry_nor_the_routing_service_nor_the_state_module():
    source = (Path(__file__).resolve().parent.parent / 'app' / 'db.py').read_text(encoding='utf-8')
    module = ast.parse(source)
    function = next(n for n in ast.walk(module) if isinstance(n, ast.FunctionDef) and n.name == '_routing_coverage_state')
    assert not [n for n in ast.walk(function) if isinstance(n, (ast.Import, ast.ImportFrom))]
    names = {n.id for n in ast.walk(function) if isinstance(n, ast.Name)}
    assert names <= {'conn', 'sqlite3'}, names           # sqlite3 : l'annotation de la signature
    for node in ast.walk(module):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert not node.module.startswith(('app.settings', 'app.services.routing')), (node.module, node.lineno)
