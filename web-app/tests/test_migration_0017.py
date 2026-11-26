"""
Migration nommée 0017_coverage_polygon_only sur une base RÉELLE (lot Z1 : admin-space EF-6.20, CA-87 ; ADR-0012).

Principe testé : la zone en vigueur juste avant la migration est la zone en vigueur juste après. Chaque cas part d'un fichier SQLite au
schéma d'avant Z1 (migrations 0001 à 0016 jouées, les deux lignes `settings` posées telles quelles), puis l'application démarre : c'est
`create_app` qui joue la migration, comme en production.

« Zone en vigueur avant » = ce que le code d'AVANT Z1 calculait : le polygone s'il était valide et non vide, sinon le rectangle s'il
était valide, sinon le rectangle par défaut. Elle est écrite en dur dans chaque cas (jamais recalculée par le code courant).

 (a) à (h) : les huit cas du CA-87 ; relecture (idempotence) ; aucune coordonnée dans les journaux ; test statique : la migration
 n'importe ni le registre (`app.settings`) ni `app.services.routing`.
"""

import ast
import inspect
import json
import logging
import re
from pathlib import Path

import pytest

from app import db as database
from app.services.routing import zone
from tests.pw_harness import call, connect, make_app
from tests.test_route_builder_api_edge import BASE, build_world

MIGRATION = '0017_coverage_polygon_only'
POLY, BBOX = 'routing.coverage_polygon', 'routing.coverage_bbox'
DEFAULT = [[1.35, 48.83], [6.42, 48.83], [6.42, 51.55], [1.35, 51.55]]
LILLE_BOX = [3.0, 50.6, 3.2, 50.7]
LILLE_ZONE = [[3.0, 50.6], [3.2, 50.6], [3.2, 50.7], [3.0, 50.7]]
U = [[2, 49], [5, 49], [5, 51], [4, 51], [4, 50], [3, 50], [3, 51], [2, 51]]          # concave, valide
BOW_TIE = [[0, 0], [1, 1], [1, 0], [0, 1]]


def old_database(tmp_path, **rows):
    """Base au schéma d'avant Z1 dans `<tmp_path>/data` (là où `make_app` la cherche), avec les lignes `settings` données (JSON en texte)."""
    folder = tmp_path / 'data'
    folder.mkdir(parents=True, exist_ok=True)
    path = str(folder / 'kmlviewer.sqlite3')
    conn = database.connect(path)
    conn.executescript(database.SCHEMA)
    before = [m for m in database.NAMED_MIGRATIONS if m[0] != MIGRATION]
    database.migrate(conn, named=before)
    for key, text in rows.items():
        conn.execute("INSERT INTO settings (key, value, updated_at, updated_by) VALUES (?, ?, '2026-01-01T00:00:00Z', 'admin')",
                     (key.replace('__', '.'), text))
    conn.commit()
    version = conn.execute("SELECT version FROM settings_meta").fetchone()[0]
    assert conn.execute("SELECT 1 FROM schema_migrations WHERE name = ?", (MIGRATION,)).fetchone() is None
    conn.close()
    return path, version


def snapshot(app):
    conn = connect(app)
    try:
        settings = {r['key']: (json.loads(r['value']), r['updated_by']) for r in conn.execute(
            "SELECT key, value, updated_by FROM settings WHERE key IN (?, ?)", (POLY, BBOX))}
        return {'settings': settings,
                'version': conn.execute("SELECT version FROM settings_meta").fetchone()[0],
                'migrated': conn.execute("SELECT 1 FROM schema_migrations WHERE name = ?", (MIGRATION,)).fetchone() is not None,
                'audit': [(r['actor_type'], r['action'], r['target_id'], json.loads(r['details'] or '{}')) for r in conn.execute(
                    "SELECT actor_type, action, target_id, details FROM audit_log WHERE details LIKE ? ORDER BY id", (f'%{MIGRATION}%',))]}
    finally:
        conn.close()


def zone_now(app):
    store = app.config['SETTINGS_STORE']
    conn = connect(app)
    try:
        store.refresh(conn, force=True)
    finally:
        conn.close()
    return zone.effective_zone(store.get), store.source(POLY)


def started(tmp_path, **rows):
    """Base d'avant Z1 avec ces lignes, puis démarrage de l'application (= la migration). Renvoie (app, version avant)."""
    _, version = old_database(tmp_path, **rows)
    return make_app(tmp_path), version


def same_zone(vertices, expected):
    return [[float(a), float(b)] for a, b in vertices] == [[float(a), float(b)] for a, b in expected]


# ============================================================ les huit cas du CA-87 ==========================================

def test_a_no_row_at_all_changes_nothing_not_even_the_version(tmp_path):
    app, version = started(tmp_path)
    state = snapshot(app)
    assert state['migrated'] and BBOX not in state['settings'] and POLY not in state['settings']
    assert state['version'] == version and state['audit'] == []
    area, origin = zone_now(app)
    assert same_zone(area, DEFAULT) and origin == 'défaut'


def test_b_a_custom_rectangle_without_polygon_becomes_the_four_vertices_written_by_the_system(tmp_path):
    app, version = started(tmp_path, **{'routing__coverage_bbox': json.dumps(LILLE_BOX)})
    state = snapshot(app)
    assert state['migrated'] and BBOX not in state['settings']
    assert state['settings'][POLY] == (LILLE_ZONE, 'system')                                  # sud-ouest, sud-est, nord-est, nord-ouest
    assert state['version'] == version + 1
    kinds = {(actor, action, target) for actor, action, target, _ in state['audit']}
    assert kinds == {('system', 'setting.update', POLY), ('system', 'setting.reset', BBOX)}
    assert all(details.get('migration') == MIGRATION for *_, details in state['audit'])
    area, origin = zone_now(app)
    assert same_zone(area, LILLE_ZONE) and origin == 'base'


def test_b_the_zone_in_force_gives_the_same_answers_for_points_before_and_after(tmp_path):
    """Un point à (3,1 ; 50,65) et Lille (3,06 ; 50,63) acceptés, Liège refusé en 400, sans appel au moteur pour ce dernier."""
    old_database(tmp_path, **{'routing__coverage_bbox': json.dumps(LILLE_BOX)})
    world = build_world(tmp_path)                                                              # démarre l'application sur cette base
    try:
        assert BBOX not in snapshot(world.app)['settings']
        assert world.route(points=[[50.65, 3.1], [50.66, 3.12]], distance_km=None).status_code == 200
        assert world.route(points=[[50.63, 3.06], [50.64, 3.07]], distance_km=None).status_code == 200            # Lille
        calls = len(world.provider.calls)
        liege = world.route(points=[[50.63, 5.57], [50.64, 5.58]], distance_km=None)                              # Liège
        assert liege.status_code == 400 and liege.get_json()['error'] == "Le point 1 est hors de la zone couverte."
        assert len(world.provider.calls) == calls
        options = call(world.a, 'get', f'{BASE}/options').get_json()
        assert options['coverage_bbox'] == LILLE_BOX and same_zone(options['coverage_polygon'], LILLE_ZONE)
    finally:
        world.conn.close()


def test_c_the_same_rectangle_with_an_empty_polygon_is_taken_over_like_b(tmp_path):
    app, version = started(tmp_path, **{'routing__coverage_bbox': json.dumps(LILLE_BOX), 'routing__coverage_polygon': '[]'})
    state = snapshot(app)
    assert BBOX not in state['settings'] and state['settings'][POLY] == (LILLE_ZONE, 'system') and state['version'] == version + 1
    assert same_zone(zone_now(app)[0], LILLE_ZONE)


def test_d_a_valid_polygon_is_kept_untouched_and_only_the_rectangle_goes(tmp_path):
    app, version = started(tmp_path, **{'routing__coverage_bbox': json.dumps(LILLE_BOX), 'routing__coverage_polygon': json.dumps(U)})
    state = snapshot(app)
    assert BBOX not in state['settings'] and state['settings'][POLY] == (U, 'admin')          # même valeur, même auteur d'origine
    assert state['version'] == version + 1                                                    # une ligne supprimée : la version bouge
    assert [(a, b, c) for a, b, c, _ in state['audit']] == [('system', 'setting.reset', BBOX)]
    assert same_zone(zone_now(app)[0], U)


@pytest.mark.parametrize('polygon', [None, '[]'], ids=['sans polygone', 'polygone vide'])
def test_e_a_rectangle_equal_to_the_default_leaves_the_polygon_row_deleted_and_the_default_origin(tmp_path, polygon):
    rows = {'routing__coverage_bbox': json.dumps([1.35, 48.83, 6.42, 51.55])}
    if polygon:
        rows['routing__coverage_polygon'] = polygon
    app, _ = started(tmp_path, **rows)
    state = snapshot(app)
    assert BBOX not in state['settings'] and POLY not in state['settings']
    area, origin = zone_now(app)
    assert same_zone(area, DEFAULT) and origin == 'défaut'


@pytest.mark.parametrize('forged', ['"x"', '[0,0,100,100]', '[NaN,0,1,1]', 'null', '[1,2,3]', '[3.2,50.6,3.0,50.7]', '[true,0,1,1]'])
def test_f_a_forged_rectangle_without_polygon_gives_the_default_zone(tmp_path, forged):
    app, _ = started(tmp_path, **{'routing__coverage_bbox': forged})
    state = snapshot(app)
    assert state['migrated'] and BBOX not in state['settings'] and POLY not in state['settings']
    area, origin = zone_now(app)
    assert same_zone(area, DEFAULT) and origin == 'défaut'


def test_g_a_rectangle_beyond_the_map_projection_falls_back_to_the_default_with_a_warning_without_coordinate(tmp_path, caplog):
    _, version = old_database(tmp_path, **{'routing__coverage_bbox': '[1, 80, 2, 89]'})
    with caplog.at_level(logging.DEBUG):
        app = make_app(tmp_path)
    state = snapshot(app)
    assert BBOX not in state['settings'] and POLY not in state['settings']                    # ni l'un ni l'autre : la zone par défaut
    assert same_zone(zone_now(app)[0], DEFAULT) and state['version'] == version + 1
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and 'projection' in r.getMessage()]
    assert warnings == ["routing.coverage_bbox hors de la projection de la carte : zone remise au défaut"]
    assert not re.search(r'\b(80|89)\b', ' '.join(warnings))


def test_h_a_forged_polygon_with_a_custom_rectangle_takes_the_four_vertices_of_the_rectangle(tmp_path):
    app, version = started(tmp_path, **{'routing__coverage_bbox': json.dumps(LILLE_BOX), 'routing__coverage_polygon': json.dumps(BOW_TIE)})
    state = snapshot(app)
    assert BBOX not in state['settings'] and state['settings'][POLY] == (LILLE_ZONE, 'system') and state['version'] == version + 1
    assert same_zone(zone_now(app)[0], LILLE_ZONE)


@pytest.mark.parametrize('rows, expected', [
    ({}, DEFAULT),
    ({'routing__coverage_bbox': json.dumps(LILLE_BOX)}, LILLE_ZONE),
    ({'routing__coverage_polygon': json.dumps(U)}, U),                                        # polygone seul : rien à faire, jamais perdu
    ({'routing__coverage_polygon': json.dumps(U), 'routing__coverage_bbox': json.dumps(LILLE_BOX)}, U),
    ({'routing__coverage_polygon': '[]', 'routing__coverage_bbox': json.dumps(LILLE_BOX)}, LILLE_ZONE),
    ({'routing__coverage_polygon': json.dumps(BOW_TIE)}, DEFAULT),
    ({'routing__coverage_bbox': '"x"'}, DEFAULT),
    ({'routing__coverage_bbox': json.dumps([-5.0, 40.0, 5.0, 50.0])}, [[-5.0, 40.0], [5.0, 40.0], [5.0, 50.0], [-5.0, 50.0]]),
], ids=['aucune', 'rectangle', 'polygone seul', 'polygone + rectangle', 'polygone vide + rectangle', 'polygone forgé seul', 'rectangle forgé',
        'rectangle large'])
def test_the_zone_in_force_before_is_the_zone_in_force_after(tmp_path, rows, expected):
    """Principe général (CA-87) : `expected` est la zone que le code d'avant Z1 appliquait, écrite en dur."""
    app, _ = started(tmp_path, **rows)
    state = snapshot(app)
    assert state['migrated'] and BBOX not in state['settings']
    assert same_zone(zone_now(app)[0], expected)


# ============================================================ relecture, journaux, statique ===================================

def test_replaying_the_migration_changes_nothing_and_audits_nothing_more(tmp_path):
    app, _ = started(tmp_path, **{'routing__coverage_bbox': json.dumps(LILLE_BOX)})
    first = snapshot(app)
    conn = connect(app)
    conn.execute("DELETE FROM schema_migrations WHERE name = ?", (MIGRATION,))                # le nom est retiré : la migration se rejoue
    conn.commit()
    database.migrate(conn)
    conn.close()
    second = snapshot(app)
    assert second == first                                                                    # lignes, version, migration enregistrée, audit : identiques


def test_a_new_database_has_nothing_to_migrate_and_the_migration_is_registered(tmp_path):
    app = make_app(tmp_path)
    state = snapshot(app)
    assert state['migrated'] and state['settings'] == {} and state['audit'] == []
    assert MIGRATION in [name for name, _ in database.NAMED_MIGRATIONS]
    assert same_zone(zone_now(app)[0], DEFAULT)


def test_no_log_line_of_the_migration_contains_a_coordinate(tmp_path, caplog):
    old_database(tmp_path, **{'routing__coverage_bbox': '[3.123456, 50.654321, 3.234567, 50.765432]'})
    with caplog.at_level(logging.DEBUG):
        make_app(tmp_path)
    for number in ('3.123456', '50.654321', '3.234567', '50.765432'):
        assert number not in caplog.text, number


def test_the_migration_imports_neither_the_settings_registry_nor_the_routing_service():
    """Une évolution future d'EF-6.20 ne doit pas changer le comportement d'une migration publiée (ADR-0012) : copie figée des contrôles."""
    source = inspect.getsource(database._coverage_polygon_only)
    tree = ast.parse(source.strip() if source.startswith('def') else inspect.cleandoc(source))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or '')
            imported |= {f"{node.module}.{alias.name}" for alias in node.names}
    assert imported, "la migration ne contient plus aucun import : le test ne regarde plus rien"
    for name in imported:
        assert not name.startswith(('app.settings', 'app.services.routing')), name
        assert not ({'settings', 'services', 'routing', 'zone', 'registry'} & set(name.lower().split('.'))), name
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert not ({'REGISTRY', 'zone', 'routing', 'SettingsStore'} & names), names
    # le module db.py lui-même ne dépend ni du registre des réglages ni du service de routage (ADR-0012)
    module = ast.parse((Path(__file__).resolve().parent.parent / 'app' / 'db.py').read_text(encoding='utf-8'))
    for node in ast.walk(module):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert not node.module.startswith(('app.settings', 'app.services.routing')), (node.module, node.lineno)
        if isinstance(node, ast.Import):
            assert not any(alias.name.startswith(('app.settings', 'app.services.routing')) for alias in node.names), node.lineno
