"""
Couverture pilotée par la zone, côté API (rôle test) : routing-zone-donnees ZD-1, ZD-3, ZD-4, ZD-12 ; CA-ZD1 (`=0`, WARNING), CA-ZD2
(écriture de la zone, contrôles, acceptation des points), CA-ZD5 (`starting` ⇒ 503), CA-ZD6 (diagnostic), CA-ZD8 (API admin).
Écrit depuis la spec, avec la copie figée du catalogue (tests/fixtures/routing) et des catalogues faits main quand l'aire doit être
connue exactement. Horloge : les enregistrements par l'API datent de l'heure réelle ; les passages de l'hôte sont simulés par des
horloges relatives à cette heure.

 A. `ROUTING_ZONE_BUILD=0` : comportement du lot Z1, table non écrite, API 404, WARNING des variables héritées avec `=1`
 B. écriture de la zone (ZD-4) : catalogue, couverture, plafonds, ordre des contrôles, version inchangée en cas d'échec
 C. zone demandée / zone active : statut, options, acceptation des points (ZD-3)
 D. « Revenir au défaut » (reset) : mêmes contrôles que PATCH
 E. marge (ZD-5), aperçu de confirmation (ZD-4.4)
 F. `starting` : options, 503, résultats déjà calculés (CA-ZD5)
 G. API admin (CA-ZD8), alertes, diagnostic (CA-ZD6)
"""

import json
import logging
import re
from datetime import timedelta

import pytest

from app import features
from app.services.routing import coverage as geometry
from app.services.routing import coverage_state as coverage
from tests.pw_harness import call, connect
from tests.routing_fakes import FakeProvider
from tests.routing_zone_support import (DEFAULT_REGIONS, DEFAULT_ZONE, GRAPH, LILLE_ZONE, NORMANDIE_ZONE, audit_mark, catalog_row, expected_tiles,
                                        frozen_document, load_catalog, now_utc, rect, reset_state, routing_audit, set_active,
                                        state_row, synthetic, zone_app)
from tests.test_route_builder_api_edge import BASE, CLI, ENGINE, POINTS, World

KEY = 'routing.coverage_polygon'
LIEGE = [[50.63, 5.55], [50.66, 5.60], [50.64, 5.65]]                  # (lat, lon) : dans la zone par défaut, hors de la zone de Lille
LILLE_POINTS = POINTS                                                   # autour de Lille
SEA_ZONE = [[-3.0, 50.0], [0.0, 50.0], [0.0, 50.5], [-3.0, 50.5]]
MSG_NO_CATALOG = "Zone : catalogue des données pas encore chargé, réessaie dans quelques minutes."


LOCAL_DEM = 'http://opentopodata:5000/v1/copernicus30'            # le plafond des dalles n'existe qu'avec le MNT local (ZD-1, ZD-4.3)


def zone_world(tmp_path, *, build=True, catalog=frozen_document, active=DEFAULT_ZONE, bbox=(48.0, 0.0, 53.0, 8.0), **config):
    config.setdefault('DEM_URL', LOCAL_DEM)
    app = zone_app(tmp_path, build=build, **config)
    conn = connect(app)
    app.config['SETTINGS_STORE'].set_many(conn, {'routing.base_url': ENGINE}, actor=CLI)
    provider = FakeProvider(sinuosity=0.05, bbox=bbox)
    world = World(app, conn, provider)
    world.runtime.provider_factory = lambda: provider
    features.set_state(conn, 'route_builder', 'all', actor=CLI)
    world.runtime.run_health()
    if build:
        reset_state(conn)
        if catalog:
            load_catalog(conn, app, catalog() if callable(catalog) else catalog)
        if active:
            set_active(conn, active, now_utc(), regions=DEFAULT_REGIONS if active == DEFAULT_ZONE else ('belgium', 'nord-pas-de-calais'),
                       requested=active)
            conn.execute("UPDATE routing_coverage_state SET request_id = 0, request_polygon = NULL, requested_at = NULL")
            conn.commit()
    return world


@pytest.fixture
def world(tmp_path):
    w = zone_world(tmp_path)
    yield w
    w.conn.close()


def patch(world, value, key=KEY, **extra):
    body = {key: value, **extra}
    return call(world.adm, 'patch', '/api/admin/settings', body)


def delete(world, key=KEY):
    return call(world.adm, 'delete', f'/api/admin/settings/{key}')


def options(world, client=None):
    return call(client or world.a, 'get', f'{BASE}/options').get_json()


def route(world, points, **fields):
    return world.route(points=points, **fields)


def state(world):
    return coverage.read(world.conn)


def settings_view(world):
    data = call(world.adm, 'get', '/api/admin/settings').get_json()
    row = next(s for s in data['settings'] if s['key'] == KEY)
    audits = world.conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'setting.update'").fetchone()[0]
    return {'version': data['version'], 'value': row['value'], 'source': row['source'], 'audits': audits,
            'request_id': state(world)['request_id'], 'alerts': data['alerts']}


def vertices(value):
    return [[float(a), float(b)] for a, b in value]


# ===================================================== A. ROUTING_ZONE_BUILD=0 ================================================

def test_with_the_mode_off_a_zone_is_applied_at_once_without_catalog_and_the_state_table_is_never_written(tmp_path):
    w = zone_world(tmp_path, build=False)
    before, mark = state_row(w.conn), audit_mark(w.conn)
    assert patch(w, SEA_ZONE).status_code == 200                                              # aucun contrôle de couverture ni de plafond
    assert vertices(options(w)['coverage_polygon']) == vertices(SEA_ZONE)                     # zone demandée = zone acceptée (lot Z1)
    assert patch(w, LILLE_ZONE).status_code == 200 and vertices(options(w)['coverage_polygon']) == vertices(LILLE_ZONE)
    assert delete(w).status_code == 200 and vertices(options(w)['coverage_polygon']) == vertices(DEFAULT_ZONE)
    assert state_row(w.conn) == before and routing_audit(w.conn, mark) == []
    assert w.route(points=LIEGE).status_code == 200 and w.provider.calls                       # le contrôle des points suit la zone demandée
    w.conn.close()


def test_with_the_mode_off_a_point_outside_the_requested_zone_is_refused_at_once(tmp_path):
    w = zone_world(tmp_path, build=False)
    assert patch(w, LILLE_ZONE).status_code == 200
    r = route(w, LIEGE)
    assert r.status_code == 400 and r.get_json()['error'] == "Le point 1 est hors de la zone couverte." and w.provider.calls == []
    w.conn.close()


@pytest.mark.parametrize('method, path', [
    ('get', '/api/admin/routing/coverage'), ('post', '/api/admin/routing/coverage/retry'), ('post', '/api/admin/routing/coverage/release'),
    ('post', '/api/admin/routing/coverage/revert'), ('post', '/api/admin/routing/coverage/preview')])
def test_with_the_mode_off_the_whole_admin_api_is_404_even_for_an_admin(tmp_path, method, path):
    w = zone_world(tmp_path, build=False)
    assert call(w.adm, method, path, {} if method == 'post' else None).status_code == 404
    w.conn.close()


@pytest.mark.parametrize('variable', ['DEM_BBOX', 'ROUTING_EXTRACT_URLS'])
def test_with_the_mode_on_a_legacy_variable_still_defined_is_ignored_with_a_warning(tmp_path, monkeypatch, caplog, variable):
    monkeypatch.setenv(variable, '41,-6,52,10' if variable == 'DEM_BBOX' else 'https://download.geofabrik.de/europe/belgium-latest.osm.pbf')
    with caplog.at_level(logging.WARNING):
        zone_app(tmp_path)
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and variable in r.getMessage()]
    assert len(warnings) == 1 and '41,-6' not in warnings[0] and 'geofabrik' not in warnings[0]


@pytest.mark.parametrize('build, defined', [(False, True), (True, False)])
def test_no_warning_when_the_mode_is_off_or_the_variables_are_not_defined(tmp_path, monkeypatch, caplog, build, defined):
    for variable in ('DEM_BBOX', 'ROUTING_EXTRACT_URLS'):
        monkeypatch.delenv(variable, raising=False)
        if defined:
            monkeypatch.setenv(variable, 'x')
    with caplog.at_level(logging.WARNING):
        zone_app(tmp_path, build=build)
    assert not [r for r in caplog.records if 'DEM_BBOX' in r.getMessage() or 'ROUTING_EXTRACT_URLS' in r.getMessage()]


def test_the_defaults_of_the_caps_and_the_activation_flag_are_the_spec_values():
    from app.config import Config, _env_int
    assert Config.ROUTING_ZONE_BUILD is False
    assert (Config.ROUTING_MAX_UNCOVERED_PCT, Config.ROUTING_MAX_REGIONS, Config.ROUTING_MAX_SOURCE_BYTES, Config.DEM_MAX_TILES,
            Config.ROUTING_ZONE_SETTLE_S) == (10, 20, 4294967296, 60, 300)
    assert Config.ROUTING_CATALOG_ROOT == 'europe' and Config.ROUTING_ALLOWED_HOSTS == ['download.geofabrik.de']
    assert Config.ROUTING_CATALOG_EXCLUDE == ['alps', 'britain-and-ireland', 'dach', 'great-britain', 'guadeloupe', 'martinique', 'mayotte', 'reunion']
    assert [_env_int('X_UNSET_TEST', 7, 1, 9)] == [7]


@pytest.mark.parametrize('value, expected', [('0', 0), ('50', 50), ('51', 10), ('-1', 10), ('x', 10), ('', 10), (' 25 ', 25), ('5.5', 10)])
def test_the_uncovered_percentage_variable_is_bounded_to_0_50_and_falls_back_to_the_default(monkeypatch, value, expected):
    from app.config import _env_int
    monkeypatch.setenv('ROUTING_MAX_UNCOVERED_PCT', value)
    assert _env_int('ROUTING_MAX_UNCOVERED_PCT', 10, 0, 50) == expected


@pytest.mark.parametrize('name, low, high, default', [('ROUTING_MAX_REGIONS', 1, 50, 20), ('DEM_MAX_TILES', 1, 1000, 60)])
def test_the_cap_variables_never_take_a_dangerous_value(monkeypatch, name, low, high, default):
    from app.config import _env_int
    for value, expected in ((str(low), low), (str(high), high), (str(low - 1), default), (str(high + 1), default), ('abc', default)):
        monkeypatch.setenv(name, value)
        assert _env_int(name, default, low, high) == expected, value


# ===================================================== B. écriture de la zone (ZD-4) ==========================================

def catalog_of_three(size=100_000_000):
    return synthetic(rect('aa', 2, 45, 6, 48, size=size), rect('bb', 6, 45, 10, 48, size=size), rect('cc', 10, 45, 14, 48, size=size))


def refused(world, value, key=KEY):
    """PATCH refusé : 400, et RIEN n'est écrit (version, valeur, audit, numéro de demande) ; renvoie le message affiché."""
    before = settings_view(world)
    r = patch(world, value, key)
    assert r.status_code == 400, (r.status_code, r.get_data(as_text=True)[:200])
    assert settings_view(world) == before
    return r.get_json()['error']


def test_a_zone_without_the_catalog_is_a_400_with_the_spec_message_and_nothing_is_written(tmp_path):
    w = zone_world(tmp_path, catalog=None, active=None)
    assert MSG_NO_CATALOG in refused(w, LILLE_ZONE)
    w.conn.close()


def test_a_zone_with_23_percent_outside_the_data_is_a_400_with_the_percentage(tmp_path):
    w = zone_world(tmp_path, catalog=lambda: synthetic(rect('solo', 2, 45, 12, 48, size=1)), active=None)
    assert "Zone : 23 % de la zone hors des données OSM (mer ou hors d'Europe)." in refused(w, [[2, 45], [15, 45], [15, 48], [2, 48]])
    w.conn.close()


def test_a_zone_under_the_10_percent_threshold_is_accepted_and_one_above_is_not(tmp_path):
    w = zone_world(tmp_path, catalog=lambda: synthetic(rect('solo', 2, 45, 6, 48, size=1)), active=None)
    assert patch(w, [[2, 45], [6.35, 45], [6.35, 48], [2, 48]]).status_code == 200            # 0,35 / 4,35 ≈ 8 % hors des données
    assert "Zone : 13 %" in refused(w, [[2, 45], [6.6, 45], [6.6, 48], [2, 48]])               # 0,6 / 4,6 ≈ 13 %
    w.conn.close()


def test_the_default_zone_with_its_4_percent_of_sea_is_accepted_by_the_default_threshold(world):
    assert patch(world, LILLE_ZONE).status_code == 200
    assert patch(world, DEFAULT_ZONE).status_code == 200


def test_the_threshold_is_the_configured_one(tmp_path):
    w = zone_world(tmp_path, ROUTING_MAX_UNCOVERED_PCT=2)
    assert "Zone : 4 % de la zone hors" in refused(w, DEFAULT_ZONE)                           # 4,06 % > 2 %
    w.conn.close()


def test_too_many_regions_is_a_numbered_400(tmp_path):
    w = zone_world(tmp_path, catalog=catalog_of_three, active=None, ROUTING_MAX_REGIONS=2)
    error = refused(w, [[2, 45], [14, 45], [14, 48], [2, 48]])
    assert 'Zone trop grande' in error and re.search(r'\b3\b', error) and re.search(r'\b2\b', error)
    w.conn.close()


def test_too_many_bytes_is_a_numbered_400_in_go_and_gio(tmp_path):
    w = zone_world(tmp_path, catalog=lambda: catalog_of_three(1_500_000_000), active=None)
    error = refused(w, [[2, 45], [14, 45], [14, 48], [2, 48]])
    assert re.search(r"Zone trop grande : 4,5 Go d'extraits \(3 régions : .+\) pour 4 Gio au plus\.$", error), error
    w.conn.close()


def test_the_bytes_cap_is_the_configured_one_and_the_limit_itself_is_accepted(tmp_path):
    two_regions = [[2, 45], [10, 45], [10, 48], [2, 48]]                                       # aa + bb : 2 × 100 Mo, 50 dalles
    w = zone_world(tmp_path, catalog=catalog_of_three, active=None, ROUTING_MAX_SOURCE_BYTES=200_000_000)
    assert patch(w, two_regions).status_code == 200
    w.conn.close()
    w = zone_world(tmp_path / 'b', catalog=catalog_of_three, active=None, ROUTING_MAX_SOURCE_BYTES=200_000_000 - 1)
    assert 'Zone trop grande' in refused(w, two_regions)
    w.conn.close()


def test_too_many_tiles_is_a_numbered_400_against_the_default_of_60(tmp_path):
    zone_value = [[2, 45], [14, 45], [14, 48], [2, 48]]
    tiles = len(expected_tiles(geometry.extract_bbox(zone_value, 10)))
    assert tiles > 60
    w = zone_world(tmp_path, catalog=catalog_of_three, active=None)
    error = refused(w, zone_value)
    assert 'Zone trop grande' in error and re.search(rf'\b{tiles}\b', error) and re.search(r'\b60\b', error)
    w.conn.close()


@pytest.mark.parametrize('dem_url, refused_for_tiles', [(LOCAL_DEM, True), ('', False), ('https://api.opentopodata.org/v1/copernicus30', False),
                                                        ('http://opentopodata.example:5000/v1/copernicus30', False),
                                                        ('http://opentopodata.example.com:5000/x', False), ('http://localhost:5000/x', False)])
def test_the_tiles_cap_only_exists_with_the_local_dem_service(tmp_path, dem_url, refused_for_tiles):
    """CA-ZD3 : `DEM_MAX_TILES` = 10 et zone de 24 dalles."""
    w = zone_world(tmp_path, active=LILLE_ZONE, DEM_URL=dem_url, DEM_MAX_TILES=10)
    assert len(expected_tiles(geometry.extract_bbox(DEFAULT_ZONE, 10))) == 24
    response = patch(w, DEFAULT_ZONE)
    if refused_for_tiles:
        error = response.get_json()['error']
        assert response.status_code == 400 and re.search(r'\b24\b', error) and re.search(r'\b10\b', error) and 'dalles' in error
    else:
        assert response.status_code == 200, response.get_json()
    w.conn.close()


def set_dem_url_in_database(w, url):
    """Réglage `DEM_URL` posé dans l'onglet Altitude (en base), sans passer par la liste blanche de destinations."""
    w.conn.execute("INSERT INTO settings (key, value, updated_at, updated_by) VALUES ('DEM_URL', ?, '2026-11-26T00:00:00Z', 'admin') "
                   "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (json.dumps(url),))
    w.conn.commit()
    w.app.config['SETTINGS_STORE'].refresh(w.conn, force=True)


@pytest.mark.parametrize('env_url, database_url, capped', [('', LOCAL_DEM, False), (LOCAL_DEM, 'https://api.opentopodata.org/v1/copernicus30', True),
                                                            (LOCAL_DEM, '', True), ('', '', False)])
def test_the_application_and_the_host_follow_the_same_variable_the_environment_not_the_altitude_tab(tmp_path, env_url, database_url, capped):
    """TZ2c-m5 : « MNT géré par la tâche » se lit dans l'environnement seul des deux côtés (l'hôte ne lit pas la base)."""
    w = zone_world(tmp_path, active=LILLE_ZONE, DEM_URL=env_url, DEM_MAX_TILES=10)
    set_dem_url_in_database(w, database_url)
    response = patch(w, DEFAULT_ZONE)                                                                # 24 dalles
    assert (response.status_code == 400) is capped, response.get_json()
    w.conn.close()


def test_the_checks_come_in_the_spec_order_catalog_then_coverage_then_caps(tmp_path):
    big = [[2, 45], [20, 45], [20, 48], [2, 48]]                                              # 12 % hors des données ET trop de dalles
    w = zone_world(tmp_path, catalog=lambda: synthetic(rect('solo', 2, 45, 18, 48, size=1)), active=None, DEM_MAX_TILES=3)
    assert 'hors des données' in refused(w, big)                                              # la couverture avant les plafonds
    w.conn.close()
    w = zone_world(tmp_path / 'b', catalog=None, active=None, DEM_MAX_TILES=3)
    assert MSG_NO_CATALOG in refused(w, big)                                                  # le catalogue avant tout
    w.conn.close()


@pytest.mark.parametrize('value, message', [([], 'Zone : de 3 à 50 sommets.'), ([[1, 2], [3, 4]], 'Zone : de 3 à 50 sommets.'),
                                            ([[0, 0], [1, 1], [1, 0], [0, 1]], 'Zone : ')])
def test_the_ef_6_20_controls_come_first_even_without_a_catalog(tmp_path, value, message):
    w = zone_world(tmp_path, catalog=None, active=None)
    assert message in refused(w, value) and MSG_NO_CATALOG not in refused(w, value)
    w.conn.close()


def test_an_accepted_zone_writes_the_setting_and_creates_the_request_in_the_same_write(world):
    before = settings_view(world)
    r = patch(world, LILLE_ZONE)
    assert r.status_code == 200 and r.get_json()['version'] == before['version'] + 1
    after = settings_view(world)
    assert vertices(after['value']) == vertices(LILLE_ZONE) and after['audits'] == before['audits'] + 1 and after['request_id'] == 1
    s = state(world)
    assert s['status'] == 'pending' and s['reason'] == 'zone' and s['request_polygon'] == vertices(LILLE_ZONE)


# ===================================================== C. zone demandée / zone active =========================================

def test_a_new_zone_is_pending_the_options_keep_the_active_zone_and_a_point_of_the_active_zone_is_still_accepted(world):
    assert patch(world, LILLE_ZONE).status_code == 200
    assert state(world)['status'] == 'pending'
    shown = options(world)
    assert vertices(shown['coverage_polygon']) == vertices(DEFAULT_ZONE) and shown['coverage_bbox'] == [1.35, 48.83, 6.42, 51.55]
    assert shown['state'] == 'ok' and shown['available'] is True
    answer = route(world, LIEGE)                                                              # hors de la zone demandée, dans la zone active
    assert answer.status_code == 200 and world.provider.calls


def test_a_point_of_the_requested_zone_outside_the_active_zone_is_refused_before_any_engine_call(tmp_path):
    w = zone_world(tmp_path, active=LILLE_ZONE)
    assert patch(w, DEFAULT_ZONE).status_code == 200 and state(w)['status'] == 'pending'
    assert vertices(options(w)['coverage_polygon']) == vertices(LILLE_ZONE)
    r = route(w, LIEGE)
    assert r.status_code == 400 and r.get_json()['error'] == "Le point 1 est hors de la zone couverte." and w.provider.calls == []
    assert route(w, LILLE_POINTS).status_code == 200
    w.conn.close()


def test_the_technical_points_are_pulled_back_into_the_active_zone_not_the_requested_one(tmp_path):
    w = zone_world(tmp_path, active=LILLE_ZONE)
    assert patch(w, DEFAULT_ZONE).status_code == 200
    assert route(w, LILLE_POINTS).status_code == 200
    for _, *arguments in w.provider.calls:
        for point in [p for a in arguments if isinstance(a, list) for p in a if isinstance(p, tuple)]:
            assert 50.4 - 1e-6 <= point[0] <= 50.8 + 1e-6 and 3.0 - 1e-6 <= point[1] <= 3.4 + 1e-6, point
    w.conn.close()


def test_writing_the_active_zone_again_is_idle(world):
    patch(world, LILLE_ZONE)
    assert state(world)['status'] == 'pending'
    assert patch(world, DEFAULT_ZONE).status_code == 200
    assert state(world)['status'] == 'idle' and state(world)['reason'] is None


def test_before_the_first_switch_the_historic_state_accepts_on_the_requested_zone_like_the_z1_lot(tmp_path):
    w = zone_world(tmp_path, active=None)
    assert patch(w, LILLE_ZONE).status_code == 200
    assert vertices(options(w)['coverage_polygon']) == vertices(LILLE_ZONE)
    assert route(w, LIEGE).status_code == 400 and route(w, LILLE_POINTS).status_code == 200
    w.conn.close()


def test_after_the_switch_the_new_zone_is_the_one_in_force(world):
    patch(world, LILLE_ZONE)
    asked = now_utc() + timedelta(seconds=301)
    payload = coverage.claim(world.conn, world.app.config, LILLE_ZONE, 10, asked, False)
    coverage.switching(world.conn, payload['request_id'], LILLE_ZONE, asked + timedelta(seconds=10))
    coverage.done(world.conn, payload['request_id'], 'graph-20261127000000', '2026-11-26', [], LILLE_ZONE, asked + timedelta(seconds=60))
    assert state(world)['status'] == 'idle' and vertices(options(world)['coverage_polygon']) == vertices(LILLE_ZONE)
    assert route(world, LIEGE).status_code == 400 and route(world, LILLE_POINTS).status_code == 200


def test_a_regeneration_after_the_switch_repasses_the_check_but_a_save_of_an_already_computed_result_stays_allowed(world):
    first = route(world, LIEGE)
    assert first.status_code == 200
    result_id = first.get_json()['route_meta']['result_id']
    set_active(world.conn, LILLE_ZONE, now_utc(), graph='graph-20261127000000', regions=['belgium'], requested=LILLE_ZONE)
    assert route(world, LIEGE).status_code == 400
    assert world.post(world.a, 'save', {'result_id': result_id}).status_code == 200


# ===================================================== D. « Revenir au défaut » ================================================

def test_the_reset_to_default_creates_the_request_for_the_default_zone(tmp_path):
    w = zone_world(tmp_path, active=LILLE_ZONE)
    patch(w, [[3.0, 50.4], [3.3, 50.4], [3.3, 50.7], [3.0, 50.7]])
    assert delete(w).status_code == 200
    s = state(w)
    assert s['status'] == 'pending' and s['request_polygon'] == vertices(DEFAULT_ZONE)
    w.conn.close()


def test_the_reset_to_the_default_that_is_the_active_zone_gives_idle(world):
    patch(world, LILLE_ZONE)
    assert delete(world).status_code == 200 and state(world)['status'] == 'idle'


def test_a_reset_refused_by_the_zone_checks_is_a_400_with_the_same_message_as_the_patch_and_changes_nothing(tmp_path):
    w = zone_world(tmp_path, active=LILLE_ZONE, ROUTING_MAX_REGIONS=3)                        # la zone par défaut : 17 régions
    assert patch(w, LILLE_ZONE).status_code == 200
    patched = refused(w, DEFAULT_ZONE)
    before = settings_view(w)
    r = delete(w)
    assert r.status_code == 400 and r.get_json()['error'] == patched, r.get_json()
    assert settings_view(w) == before and vertices(options(w)['coverage_polygon']) == vertices(LILLE_ZONE)
    w.conn.close()


def test_a_reset_without_a_catalog_is_refused_with_the_catalog_message(tmp_path):
    w = zone_world(tmp_path, catalog=None, active=None)
    w.conn.execute("INSERT INTO settings (key, value, updated_at, updated_by) VALUES (?, ?, 'x', 'sql')", (KEY, json.dumps(LILLE_ZONE)))
    w.conn.execute("UPDATE settings_meta SET version = version + 1")
    w.conn.commit()
    w.app.config['SETTINGS_STORE'].refresh(w.conn, force=True)
    r = delete(w)
    assert r.status_code == 400 and MSG_NO_CATALOG in r.get_json()['error']
    assert w.conn.execute("SELECT 1 FROM settings WHERE key = ?", (KEY,)).fetchone()
    w.conn.close()


def test_resetting_every_setting_goes_through_the_same_zone_checks(tmp_path):
    w = zone_world(tmp_path, active=LILLE_ZONE, ROUTING_MAX_REGIONS=3)
    patch(w, LILLE_ZONE)
    store = w.app.config['SETTINGS_STORE']
    with pytest.raises(ValueError):
        store.reset(w.conn, None, actor=CLI)
    assert w.conn.execute("SELECT 1 FROM settings WHERE key = ?", (KEY,)).fetchone()
    w.conn.close()


@pytest.mark.parametrize('catalog', [frozen_document, None], ids=['caps-too-low', 'no-catalog-yet'])
def test_resetting_every_setting_with_a_zone_never_modified_succeeds_whatever_the_caps_or_the_catalog(tmp_path, catalog):
    """TZ2a-m5 : la zone n'a aucune ligne en base, le retour au défaut ne la change pas : aucun contrôle ZD-4 n'a lieu d'être."""
    w = zone_world(tmp_path, catalog=catalog, ROUTING_MAX_REGIONS=3)
    store = w.app.config['SETTINGS_STORE']
    store.set_many(w.conn, {'routing.max_concurrent': 3}, actor=CLI)
    assert not w.conn.execute("SELECT 1 FROM settings WHERE key = ?", (KEY,)).fetchone()
    before = state_row(w.conn)
    rows = w.conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0]
    assert rows >= 2 and store.reset(w.conn, None, actor=CLI) == rows
    assert not w.conn.execute("SELECT 1 FROM settings").fetchone()
    assert state_row(w.conn) == before                                   # la zone ne change pas : aucune demande
    w.conn.close()


def test_deleting_the_zone_key_without_a_value_in_the_database_is_a_200_reset_false_even_with_low_caps(tmp_path):
    w = zone_world(tmp_path, ROUTING_MAX_REGIONS=3)
    response = delete(w)
    assert response.status_code == 200 and response.get_json()['reset'] is False
    w.conn.close()


def run_settings_reset(app, monkeypatch, *args):
    notified = []
    monkeypatch.setattr('app.cli._notify', lambda *a, **k: notified.append(a[2]) or [])
    result = app.test_cli_runner().invoke(args=['settings-reset', *args])
    return result, notified


def test_the_settings_reset_command_with_a_zone_never_modified_succeeds_and_notifies_after_the_reset(tmp_path, monkeypatch):
    w = zone_world(tmp_path, ROUTING_MAX_REGIONS=3)
    w.app.config['SETTINGS_STORE'].set_many(w.conn, {'routing.max_concurrent': 3}, actor=CLI)
    rows = w.conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0]
    w.conn.close()
    result, notified = run_settings_reset(w.app, monkeypatch, '--all')
    assert result.exit_code == 0 and f'{rows} valeur(s)' in result.output, (result.output, result.exception)
    assert notified == ['smtp_reset']
    conn = connect(w.app)
    assert not conn.execute("SELECT 1 FROM settings").fetchone()
    conn.close()


def test_the_settings_reset_command_refused_by_the_zone_exits_1_without_a_trace_without_notification_and_changes_nothing(tmp_path, monkeypatch):
    w = zone_world(tmp_path, active=LILLE_ZONE, ROUTING_MAX_REGIONS=3)
    patch(w, LILLE_ZONE)
    patch(w, 3, 'routing.max_concurrent')
    before = w.conn.execute("SELECT key, value FROM settings ORDER BY key").fetchall()
    w.conn.close()
    result, notified = run_settings_reset(w.app, monkeypatch, '--all')
    assert result.exit_code == 1 and 'Traceback' not in result.output and isinstance(result.exception, SystemExit), (result.output, result.exception)
    assert re.search(r'17 régions', result.output) and notified == []             # avant toute notification : rien n'a changé
    conn = connect(w.app)
    assert conn.execute("SELECT key, value FROM settings ORDER BY key").fetchall() == before
    conn.close()


def test_resetting_another_setting_does_not_run_the_zone_checks(tmp_path):
    w = zone_world(tmp_path, active=LILLE_ZONE, ROUTING_MAX_REGIONS=3)
    patch(w, LILLE_ZONE)
    patch(w, 3, 'routing.max_concurrent')
    assert delete(w, 'routing.max_concurrent').status_code == 200
    w.conn.close()


# ===================================================== E. marge et aperçu =====================================================

def test_a_margin_change_alone_creates_no_request_and_the_next_claim_uses_it(world):
    before = state_row(world.conn)
    assert patch(world, 20, 'routing.zone_margin_km').status_code == 200
    assert state_row(world.conn) == before and state(world)['status'] == 'idle'
    patch(world, LILLE_ZONE)
    asked = now_utc() + timedelta(seconds=301)
    payload = coverage.claim(world.conn, world.app.config, LILLE_ZONE, 20, asked, False)
    assert payload['margin_km'] == 20 and payload['extract_bbox'] == geometry.extract_bbox(LILLE_ZONE, 20)


@pytest.mark.parametrize('value', [-1, 51, 10.5, '10', None, True, [10]])
def test_the_margin_is_an_integer_from_0_to_50(world, value):
    before = settings_view(world)
    assert patch(world, value, 'routing.zone_margin_km').status_code == 400
    assert settings_view(world) == before


@pytest.mark.parametrize('value', [0, 10, 50])
def test_the_margin_bounds_are_accepted(world, value):
    assert patch(world, value, 'routing.zone_margin_km').status_code == 200


def preview(world, value, client=None):
    return call(client or world.adm, 'post', '/api/admin/routing/coverage/preview', {'polygon': value})


def test_the_preview_lists_the_added_and_removed_regions_the_bytes_to_download_and_writes_nothing(world):
    before, mark = state_row(world.conn), audit_mark(world.conn)
    version = settings_view(world)
    r = preview(world, NORMANDIE_ZONE)
    assert r.status_code == 200
    data = r.get_json()['preview']
    assert data['added'] == ['basse-normandie'] and data['removed'] == [] and 'basse-normandie' in data['regions']
    size = next(f['properties']['size'] for f in frozen_document()['features'] if f['properties']['id'] == 'basse-normandie')
    assert data['download_bytes'] == size
    assert data['extract_bbox'] == [-0.6447, 48.74, 6.5647, 51.64] and data['settle_s'] == 300
    small = preview(world, LILLE_ZONE).get_json()['preview']
    assert small['regions'] == ['belgium', 'nord-pas-de-calais'] and small['added'] == [] and small['download_bytes'] == 0
    assert small['removed'] == sorted(set(DEFAULT_REGIONS) - {'belgium', 'nord-pas-de-calais'})
    assert state_row(world.conn) == before and routing_audit(world.conn, mark) == [] and settings_view(world) == version


@pytest.mark.parametrize('value', [[], [[0, 0], [1, 1], [1, 0], [0, 1]], SEA_ZONE, 'x', None])
def test_the_preview_of_a_zone_the_write_would_refuse_is_the_same_400(world, value):
    r = preview(world, value)
    assert r.status_code == 400 and r.get_json()['error']


def test_the_preview_is_admin_only_and_same_origin(world):
    assert preview(world, LILLE_ZONE, world.a).status_code == 403
    assert preview(world, LILLE_ZONE, world.app.test_client()).status_code == 401
    assert call(world.adm, 'post', '/api/admin/routing/coverage/preview', {'polygon': LILLE_ZONE}, origin='https://evil.example').status_code == 403


# ===================================================== F. starting (CA-ZD5) ===================================================

def go_starting(world, polygon=LILLE_ZONE, monthly=False):
    if not monthly:
        patch(world, polygon)
    asked = now_utc() + timedelta(seconds=301)
    payload = coverage.claim(world.conn, world.app.config, polygon, 10, asked, monthly)
    assert payload and state(world)['status'] == 'building'
    coverage.switching(world.conn, payload['request_id'], polygon, asked + timedelta(seconds=10))
    assert state(world)['status'] == 'starting'
    return payload, asked


@pytest.mark.parametrize('monthly', [False, True], ids=['zone', 'mensuel'])
def test_while_starting_the_options_say_so_and_the_route_is_a_503_without_any_engine_call(world, monthly):
    go_starting(world, DEFAULT_ZONE if monthly else LILLE_ZONE, monthly)
    shown = options(world)
    assert shown['available'] is False and shown['state'] == 'starting'
    r = route(world, LIEGE)
    assert r.status_code == 503 and r.get_json()['error'] == "Service en cours de redémarrage : réessaie dans une minute."
    assert world.provider.calls == []


def test_when_the_task_is_done_the_service_is_available_again(world):
    payload, asked = go_starting(world)
    coverage.done(world.conn, payload['request_id'], 'graph-20261127000000', '2026-11-26', [], LILLE_ZONE, asked + timedelta(seconds=90))
    assert options(world)['available'] is True and options(world)['state'] == 'ok'
    assert route(world, LILLE_POINTS).status_code == 200


def test_building_and_pending_do_not_stop_the_service(world):
    patch(world, LILLE_ZONE)
    assert state(world)['status'] == 'pending' and options(world)['available'] is True and route(world, LIEGE).status_code == 200
    coverage.claim(world.conn, world.app.config, LILLE_ZONE, 10, now_utc() + timedelta(seconds=301), False)
    assert state(world)['status'] == 'building' and options(world)['available'] is True and route(world, LIEGE).status_code == 200


def test_a_failure_during_the_switch_gives_the_service_back(world):
    payload, asked = go_starting(world)
    coverage.failed(world.conn, payload['request_id'], 'health', LILLE_ZONE, asked + timedelta(seconds=120))
    assert options(world)['state'] == 'ok' and route(world, LIEGE).status_code == 200


def test_a_result_computed_before_the_restart_can_still_be_saved_during_it(world):
    result_id = route(world, LIEGE).get_json()['route_meta']['result_id']
    go_starting(world)
    assert world.post(world.a, 'save', {'result_id': result_id}).status_code == 200


def test_with_the_mode_off_the_status_never_stops_the_service(tmp_path):
    w = zone_world(tmp_path, build=False)
    w.conn.execute("UPDATE routing_coverage_state SET status = 'starting'")
    w.conn.commit()
    assert options(w)['available'] is True and route(w, LILLE_POINTS).status_code == 200
    w.conn.close()


# ===================================================== G. API admin (CA-ZD8) ==================================================

def view(world, client=None):
    return call(client or world.adm, 'get', '/api/admin/routing/coverage')


def test_the_coverage_view_is_admin_only(world):
    assert view(world, world.a).status_code == 403 and view(world, world.app.test_client()).status_code == 401
    assert view(world).status_code == 200


def test_the_view_shows_status_zones_regions_date_tiles_usage_and_budget(world):
    world.conn.execute("UPDATE routing_coverage_state SET data_date = '2026-11-24', task_tiles = '[[50,3],[49,3]]'")
    world.conn.commit()
    coverage.report(world.conn, GRAPH, {'polygon': vertices(DEFAULT_ZONE), 'extract_bbox': geometry.extract_bbox(DEFAULT_ZONE, 10),
                                         'regions': DEFAULT_REGIONS}, False, (2, 3_000_000_000), DEFAULT_ZONE, now_utc())
    data = view(world).get_json()['coverage']
    assert data['status'] == 'idle' and data['reason'] is None and data['historic'] is False
    assert vertices(data['requested_zone']) == vertices(DEFAULT_ZONE) and vertices(data['active_zone']) == vertices(DEFAULT_ZONE)
    assert data['active_regions'] == DEFAULT_REGIONS and data['data_date'] == '2026-11-24' and data['dem_tiles'] == 2
    usage = data['usage']
    assert (usage['regions'], usage['max_regions'], usage['max_source_bytes'], usage['max_tiles']) == (17, 20, 4 * 2 ** 30, 60)
    assert usage['tiles'] == 24 and usage['bytes'] == pytest.approx(3.28e9, rel=0.01)       # « 3,3 Go / 4 Gio · 17 régions / 20 · 24 dalles / 60 »
    assert data['budget'] == {'day': now_utc().strftime('%Y-%m-%d'), 'builds': 2, 'bytes': 3_000_000_000}
    assert data['settle_s'] == 300 and data['stuck'] is False


def test_the_view_follows_every_status_and_reason(world):
    patch(world, LILLE_ZONE)
    data = view(world).get_json()['coverage']
    assert (data['status'], data['reason']) == ('pending', 'zone') and vertices(data['requested_zone']) == vertices(LILLE_ZONE)
    assert vertices(data['active_zone']) == vertices(DEFAULT_ZONE) and data['requested_at']
    payload = coverage.claim(world.conn, world.app.config, LILLE_ZONE, 10, now_utc() + timedelta(seconds=301), True)
    data = view(world).get_json()['coverage']
    assert (data['status'], data['reason']) == ('building', 'mensuel') and data['claimed_at']
    coverage.switching(world.conn, payload['request_id'], LILLE_ZONE, now_utc() + timedelta(seconds=330))
    assert view(world).get_json()['coverage']['status'] == 'starting'
    coverage.failed(world.conn, payload['request_id'], 'budget', LILLE_ZONE, now_utc() + timedelta(seconds=340))
    data = view(world).get_json()['coverage']
    assert (data['status'], data['failure_code']) == ('failed', 'budget')


def test_the_view_of_a_historic_install_has_no_active_zone(tmp_path):
    w = zone_world(tmp_path, active=None)
    data = view(w).get_json()['coverage']
    assert data['historic'] is True and data['active_zone'] is None and data['active_regions'] == [] and data['usage']['tiles'] is None
    w.conn.close()


def test_a_building_task_older_than_two_hours_is_flagged_stuck(world):
    patch(world, LILLE_ZONE)
    coverage.claim(world.conn, world.app.config, LILLE_ZONE, 10, now_utc() + timedelta(seconds=301), False)
    assert view(world).get_json()['coverage']['stuck'] is False
    old = (now_utc() - timedelta(hours=2, minutes=1)).strftime('%Y-%m-%dT%H:%M:%SZ')
    world.conn.execute("UPDATE routing_coverage_state SET claimed_at = ?", (old,))
    world.conn.commit()
    assert view(world).get_json()['coverage']['stuck'] is True


def post(world, action, client=None, **kw):
    return call(client or world.adm, 'post', f'/api/admin/routing/coverage/{action}', {}, **kw)


def failed_world(world, code='download'):
    patch(world, LILLE_ZONE)
    payload = coverage.claim(world.conn, world.app.config, LILLE_ZONE, 10, now_utc() + timedelta(seconds=301), False)
    coverage.failed(world.conn, payload['request_id'], code, LILLE_ZONE, now_utc() + timedelta(seconds=320))
    assert state(world)['status'] == 'failed'


def test_retry_puts_a_failed_task_back_to_pending_and_is_audited_with_the_admin_account(world):
    failed_world(world)
    mark = audit_mark(world.conn)
    r = post(world, 'retry')
    assert r.status_code == 200 and r.get_json() == {'success': True, 'status': 'pending'} and state(world)['status'] == 'pending'
    rows = world.conn.execute("SELECT actor_type, actor_id, action FROM audit_log WHERE id > ? AND action LIKE 'routing.%'", (mark,)).fetchall()
    assert [(r['actor_type'], r['action']) for r in rows] == [('user', 'routing.retry')] and rows[0]['actor_id'] == world.admin['id']
    assert state(world)['request_polygon'] == vertices(LILLE_ZONE)                            # la demande est inchangée


@pytest.mark.parametrize('action', ['retry', 'release'])
def test_retry_and_release_are_refused_when_there_is_nothing_to_do_and_write_nothing(world, action):
    before, mark = state_row(world.conn), audit_mark(world.conn)
    assert post(world, action).status_code == 409
    assert state_row(world.conn) == before and routing_audit(world.conn, mark) == []


def test_release_frees_a_task_stuck_for_two_hours_and_not_before(world):
    patch(world, LILLE_ZONE)
    coverage.claim(world.conn, world.app.config, LILLE_ZONE, 10, now_utc() + timedelta(seconds=301), False)
    assert post(world, 'release').status_code == 409 and state(world)['status'] == 'building'
    world.conn.execute("UPDATE routing_coverage_state SET claimed_at = ?",
                       ((now_utc() - timedelta(hours=2, minutes=1)).strftime('%Y-%m-%dT%H:%M:%SZ'),))
    world.conn.commit()
    r = post(world, 'release')
    assert r.status_code == 200 and r.get_json()['status'] == 'pending' and state(world)['status'] == 'pending'


@pytest.mark.parametrize('action', ['retry', 'release', 'revert'])
def test_the_actions_are_admin_only_post_only_and_same_origin(world, action):
    failed_world(world)
    before = state_row(world.conn)
    assert post(world, action, world.a).status_code == 403 and post(world, action, world.app.test_client()).status_code == 401
    assert post(world, action, origin='https://evil.example').status_code == 403
    assert call(world.adm, 'get', f'/api/admin/routing/coverage/{action}').status_code == 405
    assert state_row(world.conn) == before


def test_revert_to_the_active_zone_sets_the_setting_back_goes_idle_and_is_audited_by_the_admin(world):
    patch(world, LILLE_ZONE)
    mark = audit_mark(world.conn)
    r = post(world, 'revert')
    assert r.status_code == 200 and r.get_json()['status'] == 'idle'
    assert state(world)['status'] == 'idle' and vertices(settings_view(world)['value']) == vertices(DEFAULT_ZONE)
    rows = world.conn.execute("SELECT actor_type, actor_id, action, target_id FROM audit_log WHERE id > ? AND action = 'setting.update'",
                              (mark,)).fetchall()
    assert [(r['actor_type'], r['target_id']) for r in rows] == [('user', KEY)] and rows[0]['actor_id'] == world.admin['id']


def test_revert_from_the_failed_state_goes_idle_too(world):
    failed_world(world)
    assert post(world, 'revert').status_code == 200 and state(world)['status'] == 'idle'
    assert coverage.alerts(world.conn, world.app.config) == []


def test_revert_has_no_active_zone_to_go_back_to_on_a_historic_install(tmp_path):
    w = zone_world(tmp_path, active=None)
    patch(w, LILLE_ZONE)
    before = settings_view(w)
    assert post(w, 'revert').status_code == 409 and settings_view(w) == before
    w.conn.close()


def test_the_alert_of_a_failed_task_is_on_the_settings_page_one_per_cause_and_cleared_with_the_cause(world):
    failed_world(world, 'download')
    alerts = settings_view(world)['alerts']
    assert [a['code'] for a in alerts if a['code'].startswith('coverage')] == ['coverage_failed']
    assert 'download' in next(a for a in alerts if a['code'] == 'coverage_failed')['message']
    assert post(world, 'retry').status_code == 200
    assert not [a for a in settings_view(world)['alerts'] if a['code'].startswith('coverage')]


def test_the_cap_failure_alert_is_the_spec_text(world):
    failed_world(world, 'cap')
    assert next(a for a in settings_view(world)['alerts'] if a['code'] == 'coverage_failed')['message'] == (
        "La zone dépasse les plafonds : réduis-la ou relève les plafonds (exploitation).")


def test_the_view_and_the_alerts_carry_no_secret_and_no_engine_address(world):
    failed_world(world)
    text = json.dumps(view(world).get_json()) + json.dumps(settings_view(world)['alerts'])
    assert 'engine.test' not in text and ENGINE not in text


# ===================================================== diagnostic (inventaire n° 17, CA-ZD6) ==================================

def diagnostic(world):
    return next(f for f in call(world.adm, 'get', '/api/admin/features').get_json()['features'] if f['key'] == 'route_builder')['diagnostic']


def test_the_diagnostic_checks_the_engine_extent_against_the_active_zone_not_the_requested_one(tmp_path):
    w = zone_world(tmp_path, active=LILLE_ZONE, bbox=(50.0, 2.5, 51.0, 4.0))
    assert patch(w, DEFAULT_ZONE).status_code == 200                                          # demandée : bien plus grande que l'étendue du moteur
    shown = diagnostic(w)
    assert shown['zone_build'] is True and shown['coverage_contained'] is True
    w.conn.close()


def test_the_diagnostic_flags_an_engine_that_does_not_contain_the_active_zone(tmp_path):
    w = zone_world(tmp_path, active=DEFAULT_ZONE, bbox=(50.0, 2.5, 51.0, 4.0))
    shown = diagnostic(w)
    assert shown['zone_build'] is True and shown['coverage_contained'] is False
    w.conn.close()


def test_the_front_message_for_that_case_is_the_divergence_message():
    from pathlib import Path
    source = (Path(__file__).resolve().parent.parent / 'static' / 'js' / 'admin.js').read_text(encoding='utf-8')
    assert 'graphe et zone active divergent' in source and 'd.zone_build' in source


def test_without_the_mode_the_diagnostic_keeps_the_z1_meaning(tmp_path):
    w = zone_world(tmp_path, build=False, bbox=(48.0, 0.0, 53.0, 8.0))
    shown = diagnostic(w)
    assert shown['zone_build'] is False and shown['coverage_contained'] is True
    assert patch(w, [[-5.0, 40.0], [5.0, 40.0], [0.0, 50.0]]).status_code == 200
    assert diagnostic(w)['coverage_contained'] is False
    w.conn.close()


def test_the_catalog_row_and_the_state_are_never_exposed_by_the_public_options(world):
    text = json.dumps(options(world))
    assert 'geofabrik' not in text and 'regions' not in text and 'sha256' not in text
    assert catalog_row(world.conn) is not None
