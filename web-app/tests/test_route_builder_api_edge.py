"""
TR5 en profondeur (rôle test) : /api/route-builder/options | route | save, résultats en mémoire, limites, santé, couverture.
Écrit depuis gpx-route-builder (RB-1.x, RB-4.x, RB-5.x, RB-S1 à S9, §15 P-TR5-1 à 9, CA-R1 à R34), pas depuis le code.

 A. fonction bêta, moteur configuré, authentification, CSRF
 B. options et état de santé (starting / ok / unavailable)
 C. validation avant tout appel au moteur, limiteur non consommé par un refus
 D. génération : forme de la réponse, graines, avertissements, altitude
 E. places (par utilisateur, instance), limiteur horaire, budget, erreurs du moteur, aucune fuite
 F. résultats : propriété, éviction, expiration, mémoire
 G. enregistrement : dédoublonnage composé, nom, kind=planned hors statistiques, MNT
 H. diagnostic, tâche de santé, registre routing.coverage_polygon (seul réglage de zone depuis le lot Z1)
"""

import io
import json
import logging
import re
import threading
import time
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app import features
from app.audit import Actor
from app.services.routing.base import (InvalidResponse, NoRouteFound, RouteResult, RoutingTimeout, RoutingUnavailable)
from app.services.routing import zone
from app.services.routing.results import ResultStore
from app.settings import REGISTRY
from tests.pw_harness import KW, add, call, concurrent, connect, make_app, new_secret, session_client
from tests.routing_fakes import FakeProvider

CLI = Actor.cli()
ENGINE = 'http://engine.test:8989'
POINTS = [[50.63, 3.05], [50.66, 3.10], [50.64, 3.15]]
BASE = '/api/route-builder'
DEFAULT_POLYGON = REGISTRY['routing.coverage_polygon'].default        # la valeur par défaut du registre (voir le test de conformité à la spec)
DEFAULT_COVERAGE = zone.envelope_lonlat(DEFAULT_POLYGON)              # son emprise : [1.35, 48.83, 6.42, 51.55]


class World:
    def __init__(self, app, conn, provider):
        self.app, self.conn, self.provider = app, conn, provider
        self.runtime = app.extensions['routing']
        self.alice = add(app, conn, 'alice')
        self.bob = add(app, conn, 'bob')
        self.admin = add(app, conn, 'adm', admin=True, secret=new_secret())
        self.a, self.b = session_client(app, self.alice), session_client(app, self.bob)
        self.adm = session_client(app, self.admin)

    def post(self, client, path, payload=None, **kw):
        return call(client, 'post', f'{BASE}/{path}', {} if payload is None else payload, **kw)

    def route(self, client=None, **fields):
        body = {'points': POINTS, 'activity': 'run', 'distance_km': 25, 'seed': 1}
        body.update(fields)
        return self.post(client or self.a, 'route', body)

    def set(self, **changes):
        self.app.config['SETTINGS_STORE'].set_many(self.conn, changes, actor=CLI)


def build_world(tmp_path, provider=None, *, state='ok', feature='all'):
    app = make_app(tmp_path, OUTBOUND_ALLOWED_HOSTS='routing=engine.test:8989')
    conn = connect(app)
    app.config['SETTINGS_STORE'].set_many(conn, {'routing.base_url': ENGINE}, actor=CLI)
    provider = provider or FakeProvider(sinuosity=0.05)
    world = World(app, conn, provider)
    world.runtime.provider_factory = lambda: provider
    features.set_state(conn, 'route_builder', feature, actor=CLI)
    if state == 'ok':
        world.runtime.run_health()
    return world


@pytest.fixture
def world(tmp_path):
    w = build_world(tmp_path)
    yield w
    w.conn.close()


# ===== A. Fonction bêta, moteur, authentification ===============================================================================

def test_the_three_routes_are_404_when_the_feature_is_off_or_the_user_is_not_listed_never_403(tmp_path):
    for state, listed, expected in (('off', True, 404), ('users', False, 404), ('users', True, 200), ('all', False, 200)):
        w = build_world(tmp_path / f'{state}{listed}', feature='off')
        ids = [w.alice['id']] if listed else [w.bob['id']]
        features.set_state(w.conn, 'route_builder', state, actor=CLI, user_ids=ids)
        assert call(w.a, 'get', f'{BASE}/options').status_code == expected, (state, listed)
        assert w.route().status_code == expected, (state, listed)
        assert w.post(w.a, 'save', {'result_id': 'x' * 32}).status_code == 404                  # 404 : fonction fermée ou résultat inconnu
        if expected == 404:
            assert not w.provider.calls
        w.conn.close()


def test_without_a_configured_or_allowed_engine_the_feature_is_404_and_nothing_goes_out(tmp_path):
    w = build_world(tmp_path)
    w.conn.execute("DELETE FROM settings WHERE key = 'routing.base_url'")
    w.conn.execute("UPDATE settings_meta SET version = version + 1")
    w.conn.commit()
    w.app.config['SETTINGS_STORE'].refresh(w.conn, force=True)
    assert call(w.a, 'get', f'{BASE}/options').status_code == 404 and w.route().status_code == 404
    w.conn.execute("INSERT INTO settings (key, value, updated_at, updated_by) VALUES ('routing.base_url', '\"http://evil.test:9\"', 'x', 'sql')")
    w.conn.execute("UPDATE settings_meta SET version = version + 1")
    w.conn.commit()
    w.app.config['SETTINGS_STORE'].refresh(w.conn, force=True)
    assert call(w.a, 'get', f'{BASE}/options').status_code == 404 and w.route().status_code == 404       # CA-R23 : forcé hors liste
    assert w.provider.calls == []
    w.conn.close()


def test_anonymous_visitors_and_other_methods_are_refused(world):
    anonymous = world.app.test_client()
    assert call(anonymous, 'get', f'{BASE}/options').status_code == 401
    assert world.post(anonymous, 'route', {'points': POINTS, 'activity': 'run', 'distance_km': 25}).status_code == 401
    assert world.post(anonymous, 'save', {'result_id': 'x'}).status_code == 401
    for method, path in (('get', 'route'), ('get', 'save'), ('put', 'route'), ('delete', 'route'), ('post', 'options'), ('patch', 'save')):
        assert call(world.a, method, f'{BASE}/{path}', {}).status_code in (404, 405), (method, path)
    assert world.provider.calls == []


def test_a_foreign_origin_is_refused_before_anything_happens(world):
    assert world.post(world.a, 'route', {'points': POINTS, 'activity': 'run', 'distance_km': 25}, origin='https://evil.example').status_code == 403
    assert world.post(world.a, 'save', {'result_id': 'x'}, origin='https://evil.example').status_code == 403
    assert world.provider.calls == []
    first = world.route()
    rid = first.get_json()['route_meta']['result_id']
    assert world.post(world.a, 'save', {'result_id': rid}, origin='null').status_code == 403
    assert world.post(world.a, 'save', {'result_id': rid}).status_code == 200


def test_the_bodies_must_be_json_objects_and_are_bounded(world):
    for raw, ctype in (('x=1', 'application/x-www-form-urlencoded'), ('{"a":1}', 'text/plain'), ('[1,2]', 'application/json'), ('"x"', 'application/json'),
                       ('null', 'application/json'), ('{', 'application/json'), ('[' * 9000, 'application/json'), ('{"a":' * 5000 + '1' + '}' * 5000, 'application/json')):
        r = call(world.a, 'post', f'{BASE}/route', raw=raw, content_type=ctype)
        assert r.status_code == 400, (raw[:20], r.status_code)
    huge = json.dumps({'points': [[50.0, 3.0]] * 200_000, 'activity': 'run', 'distance_km': 5})
    r = call(world.a, 'post', f'{BASE}/route', raw=huge)
    assert r.status_code in (400, 413)
    assert world.provider.calls == []


# ===== B. Options et santé ======================================================================================================

def test_options_describe_the_form_and_follow_the_registry(world):
    body = call(world.a, 'get', f'{BASE}/options').get_json()
    assert body['available'] is True and body['state'] == 'ok'
    assert [a['key'] for a in body['activities']] == ['run', 'trail', 'hike', 'road_bike', 'gravel', 'mtb']
    assert body['min_km'] == 1 and body['max_km'] == 300 and body['step_km'] == 0.5 and body['max_waypoints'] == 50 and body['tolerance_pct'] == 15
    assert body['coverage_bbox'] == [1.35, 48.83, 6.42, 51.55] and body['coverage_polygon'] == DEFAULT_POLYGON and body['default_activity'] == 'run'
    world.set(**{'routing.max_distance_km': 120, 'routing.max_waypoints': 7})
    again = call(world.a, 'get', f'{BASE}/options').get_json()
    assert again['max_km'] == 120 and again['max_waypoints'] == 7
    assert 'engine.test' not in json.dumps(body) and ENGINE not in json.dumps(again)


def test_a_cold_cache_is_starting_options_stay_visible_and_route_is_503_without_touching_the_engine_CA_R29(tmp_path):
    w = build_world(tmp_path, state='cold')
    options = call(w.a, 'get', f'{BASE}/options')
    assert options.status_code == 200 and options.get_json()['available'] is False and options.get_json()['state'] == 'starting'
    assert options.get_json()['coverage_bbox'] and options.get_json()['coverage_polygon']
    r = w.route()
    assert r.status_code == 503 and w.provider.calls == []
    w.runtime.run_health()
    assert call(w.a, 'get', f'{BASE}/options').get_json()['state'] == 'ok' and w.route().status_code == 200
    w.conn.close()


def test_an_engine_that_stops_makes_route_503_while_the_function_stays_visible_CA_R24(tmp_path):
    provider = FakeProvider(sinuosity=0.05)
    w = build_world(tmp_path, provider)
    assert w.route().status_code == 200
    provider.available = False
    w.app.extensions['scheduler'].run_task('routing_health')                       # sans thread
    options = call(w.a, 'get', f'{BASE}/options')
    assert options.status_code == 200 and options.get_json()['available'] is False and options.get_json()['state'] == 'unavailable'
    calls = len(provider.calls)
    r = w.route()
    assert r.status_code == 503 and len(provider.calls) == calls
    provider.available = True
    w.app.extensions['scheduler'].run_task('routing_health')
    assert w.route().status_code == 200
    w.conn.close()


def test_the_health_probe_is_never_triggered_by_a_page_request(world):
    before = world.provider.health_calls
    for _ in range(5):
        call(world.a, 'get', f'{BASE}/options')
    assert world.provider.health_calls == before


def test_the_routing_health_task_is_registered_and_only_enabled_with_a_configured_engine(world):
    task = world.app.extensions['scheduler'].tasks['routing_health']
    assert task.enabled(world.app.config) is True
    world.conn.execute("DELETE FROM settings WHERE key = 'routing.base_url'")
    world.conn.execute("UPDATE settings_meta SET version = version + 1")
    world.conn.commit()
    world.app.config['SETTINGS_STORE'].refresh(world.conn, force=True)
    assert task.enabled(world.app.config) is False
    assert world.runtime.run_health() == {'configured': False} and world.runtime.state() == 'starting'


# ===== C. Validation ============================================================================================================

BAD_BODIES = [
    {'points': [], 'activity': 'run', 'distance_km': 10}, {'points': POINTS * 20, 'activity': 'run', 'distance_km': 10},
    {'points': [[91, 3], [50, 3]], 'activity': 'run', 'distance_km': 10}, {'points': [[50, 181], [50, 3]], 'activity': 'run', 'distance_km': 10},
    {'points': [['a', 'b'], [50, 3]], 'activity': 'run', 'distance_km': 10}, {'points': [[None, None], [50, 3]], 'activity': 'run', 'distance_km': 10},
    {'points': [[50], [50, 3]], 'activity': 'run', 'distance_km': 10}, {'points': [[50, 3, 9], [50.1, 3.1]], 'activity': 'run', 'distance_km': 10},
    {'points': 'abc', 'activity': 'run', 'distance_km': 10}, {'points': {'a': [50, 3]}, 'activity': 'run', 'distance_km': 10}, {'points': None, 'activity': 'run', 'distance_km': 10},
    {'points': [[True, False], [50, 3]], 'activity': 'run', 'distance_km': 10}, {'points': [[50.63, 3.05]], 'activity': 'run'},
    {'points': [[50.63, 3.05]], 'activity': 'run', 'distance_km': None}, {'points': POINTS, 'distance_km': 10}, {'points': POINTS, 'activity': None, 'distance_km': 10},
    {'points': POINTS, 'activity': 'bike', 'distance_km': 10}, {'points': POINTS, 'activity': 'RUN', 'distance_km': 10}, {'points': POINTS, 'activity': ['run'], 'distance_km': 10},
    {'points': POINTS, 'activity': 'run', 'distance_km': 0}, {'points': POINTS, 'activity': 'run', 'distance_km': 0.9}, {'points': POINTS, 'activity': 'run', 'distance_km': 301},
    {'points': POINTS, 'activity': 'run', 'distance_km': -5}, {'points': POINTS, 'activity': 'run', 'distance_km': '10'}, {'points': POINTS, 'activity': 'run', 'distance_km': True},
    {'points': POINTS, 'activity': 'run', 'distance_km': [10]}, {'points': POINTS, 'activity': 'run', 'distance_km': 10 ** 30},
    {'points': POINTS, 'activity': 'run', 'distance_km': 10, 'seed': -1}, {'points': POINTS, 'activity': 'run', 'distance_km': 10, 'seed': 2 ** 31},
    {'points': POINTS, 'activity': 'run', 'distance_km': 10, 'seed': 1.5}, {'points': POINTS, 'activity': 'run', 'distance_km': 10, 'seed': '1'},
    {'points': POINTS, 'activity': 'run', 'distance_km': 10, 'seed': True}, {'points': POINTS, 'activity': 'run', 'distance_km': 10, 'seed': [1]},
    {'points': [[10.0, 3.0], [60.0, 3.0]], 'activity': 'run', 'distance_km': 10},           # diagonale > distance maximale
    {'points': [[52.37, 4.90], [52.38, 4.91]], 'activity': 'run', 'distance_km': 10},        # hors couverture (Amsterdam)
    {'points': [[51.51, -0.13], [51.50, -0.12]], 'activity': 'run', 'distance_km': 10},      # hors couverture (Londres)
    {'points': [[50.63, 3.05], [50.63, 3.05]], 'activity': 'run'},                            # deux points confondus sans distance
]


@pytest.mark.parametrize('body', BAD_BODIES)
def test_every_invalid_request_is_a_400_without_any_engine_call_and_is_not_counted(world, body):
    r = world.post(world.a, 'route', body, raw=json.dumps(body))
    assert r.status_code == 400, (body, r.status_code, r.get_data()[:100])
    assert world.provider.calls == []
    assert world.runtime._active_total == 0


@pytest.mark.parametrize('value', [10 ** 400, -10 ** 400, 2 ** 1030])
def test_a_huge_integer_distance_is_a_400_never_a_500(world, value):
    body = {'points': POINTS, 'activity': 'run', 'distance_km': value}
    r = world.post(world.a, 'route', body, raw=json.dumps(body))
    assert r.status_code == 400 and world.provider.calls == []


def test_forty_invalid_requests_do_not_consume_the_hourly_limit_or_hold_a_place(world):
    for _ in range(40):
        assert world.route(distance_km=0).status_code == 400
    for i in range(5):
        assert world.route(seed=i).status_code == 200


def test_a_point_in_the_coverage_but_without_data_is_a_422_with_its_original_number(tmp_path):
    provider = FakeProvider(sinuosity=0.05, unroutable=lambda p: abs(p[0] - 50.66) < 1e-6)
    w = build_world(tmp_path, provider)
    r = w.route(points=[[50.63, 3.05], [50.63, 3.05], [50.66, 3.10], [50.64, 3.15]])           # doublon retiré : le fautif garde son numéro d'origine
    assert r.status_code == 422 and '3' in r.get_json()['error'] and '50.66' not in r.get_data(as_text=True)
    w.conn.close()


def test_extra_fields_are_ignored_and_the_seed_is_optional(world):
    r = world.route(unknown='x', seed=None, profile='evil', url='http://evil.test')
    assert r.status_code == 200 and isinstance(r.get_json()['route_meta']['seed'], int)
    assert 'evil' not in json.dumps(world.provider.calls, default=str)


# ===== D. Génération ============================================================================================================

def test_the_response_has_the_viewer_payload_plus_route_meta_with_stable_codes(world):
    r = world.route()
    body = r.get_json()
    assert r.status_code == 200 and body['success'] is True
    meta = body['route_meta']
    for key in ('seed', 'next_seed', 'attempts', 'ascent_m', 'descent_m', 'warnings', 'result_id', 'distance_m', 'target_m', 'calls', 'profile'):
        assert key in meta, key
    assert meta['seed'] == 1 and meta['next_seed'] == 2 and meta['attempts'] >= 1 and meta['calls'] <= 15
    assert all(set(w) == {'code', 'message'} for w in meta['warnings'])
    assert {w['code'] for w in meta['warnings']} <= {'min_distance', 'target_gap', 'overlap', 'add_waypoint', 'closing_point_removed', 'engine'}
    assert re.fullmatch(r'[A-Za-z0-9_-]{32}', meta['result_id'])
    assert 'no-store' in r.headers['Cache-Control']


def test_the_payload_has_the_same_shape_as_an_upload_of_the_same_gpx_CA_R11(world):
    body = world.route().get_json()
    gpx = world.runtime.results.get(world.alice['id'], body['route_meta']['result_id']).gpx
    uploaded = world.a.post('/api/upload', data={'file': (io.BytesIO(gpx), 'x.gpx')}, headers={'Origin': 'https://traces.example.com'}, **KW).get_json()
    payload = {k: v for k, v in body.items() if k != 'route_meta'}
    assert set(uploaded) - {'filename'} <= set(payload)
    for key in set(uploaded) - {'filename'}:
        assert payload[key] == uploaded[key], key


def test_next_seed_wraps_and_the_same_seed_gives_the_same_trace_through_the_api(world):
    a, b = world.route(seed=7).get_json(), world.route(client=world.b, seed=7).get_json()
    assert {k: v for k, v in a.items() if k != 'route_meta'} == {k: v for k, v in b.items() if k != 'route_meta'}
    assert world.route(seed=2 ** 31 - 1).get_json()['route_meta']['next_seed'] in (0, 2 ** 31 - 1 + 1 - (2 ** 31))
    different = world.route(client=world.b, seed=8).get_json()
    assert different['route_meta']['seed'] == 8


def test_ascent_and_descent_come_from_the_trace_altitudes(tmp_path):
    class Hilly(FakeProvider):
        def route(self, waypoints, profile, deadline):
            r = super().route(waypoints, profile, deadline)
            n = len(r.points)
            return RouteResult(points=r.points, distance_m=r.distance_m,
                               elevations_m=tuple(100.0 + 30.0 * ((i // 40) % 2) for i in range(n)))
    w = build_world(tmp_path, Hilly(sinuosity=0.05))
    meta = w.route(distance_km=None).get_json()['route_meta']
    assert isinstance(meta['ascent_m'], (int, float)) and meta['ascent_m'] > 0 and meta['descent_m'] > 0
    w.conn.close()


def test_warnings_are_surfaced_with_their_codes(world):
    short = world.route(distance_km=5).get_json()['route_meta']['warnings']                      # cible trop courte pour passer par les points
    assert any(w['code'] == 'min_distance' for w in short)
    assert all(w['code'] != 'min_distance' for w in world.route(client=world.b, distance_km=25, seed=4).get_json()['route_meta']['warnings'])


def test_engine_warnings_reach_the_front_with_the_engine_code_and_stay_clean(tmp_path):
    class Warns(FakeProvider):
        def route(self, waypoints, profile, deadline):
            r = super().route(waypoints, profile, deadline)
            return RouteResult(points=r.points, distance_m=r.distance_m, warnings=('<script>alert(1)</script>\x00 tronçon hors réseau',))
    w = build_world(tmp_path, Warns(sinuosity=0.05))
    warnings = w.route(distance_km=None).get_json()['route_meta']['warnings']
    engine = [x for x in warnings if x['code'] == 'engine']
    assert engine and all('\x00' not in x['message'] and len(x['message']) <= 200 for x in engine)
    w.conn.close()


# ===== E. Places, limiteur, budget, erreurs =====================================================================================

class Blocking(FakeProvider):
    """Bloque `route` jusqu'à l'ouverture de la porte : maintient des générations « en cours »."""

    def __init__(self):
        super().__init__(sinuosity=0.05)
        self.gate = threading.Event()
        self.entered = threading.Semaphore(0)

    def route(self, waypoints, profile, deadline):
        self.entered.release()
        self.gate.wait(15)
        return super().route(waypoints, profile, deadline)


def test_one_generation_per_user_and_two_for_the_instance_answer_429_immediately(tmp_path):
    provider = Blocking()
    w = build_world(tmp_path, provider)
    carol = add(w.app, w.conn, 'carol')
    clients = [w.a, w.b, session_client(w.app, carol)]
    results = {}

    def run(name, client):
        results[name] = w.route(client)
    threads = [threading.Thread(target=run, args=('a1', clients[0])), threading.Thread(target=run, args=('b1', clients[1]))]
    [t.start() for t in threads]
    assert provider.entered.acquire(timeout=10) and provider.entered.acquire(timeout=10)
    started = time.monotonic()
    same_user = w.route(clients[0])
    third = w.route(clients[2])
    assert time.monotonic() - started < 2.0                                      # refus IMMÉDIAT, jamais en file
    assert same_user.status_code == 429 and 'encore en cours' in same_user.get_json()['error']
    assert third.status_code == 429 and 'occupé' in third.get_json()['error']
    provider.gate.set()
    [t.join(20) for t in threads]
    assert results['a1'].status_code == 200 and results['b1'].status_code == 200
    assert w.route(clients[2]).status_code == 200 and w.runtime._active_total == 0 and not w.runtime._active_users
    w.conn.close()


@pytest.mark.parametrize('exc,status', [(RoutingTimeout(), 504), (RoutingUnavailable(), 503), (InvalidResponse(), 502), (NoRouteFound(1), 422),
                                        (NoRouteFound(), 422), (RuntimeError('boom http://engine.test:8989/route?point=50.63,3.05&key=SECRET'), 502),
                                        (ValueError('x'), 502), (MemoryError(), 502)])
def test_engine_failures_map_to_fixed_status_codes_release_the_places_and_leak_nothing(tmp_path, caplog, exc, status):
    caplog.set_level(logging.DEBUG)
    w = build_world(tmp_path, FakeProvider(sinuosity=0.05, fail=exc))
    r = w.route()
    assert r.status_code == status, r.get_data()
    text = r.get_data(as_text=True)
    for forbidden in ('engine.test', '8989', 'SECRET', '50.63', '3.05', 'boom', 'Traceback', 'key='):
        assert forbidden not in text, forbidden
    assert w.runtime._active_total == 0 and not w.runtime._active_users
    own = '\n'.join(rec.getMessage() for rec in caplog.records if rec.name.startswith('app'))
    assert 'SECRET' not in own and '50.63' not in own and 'key=' not in own
    w.provider.fail = None
    assert w.route().status_code == 200
    w.conn.close()


def test_the_hourly_limit_counts_accepted_generations_only_and_answers_429_with_retry_after(world):
    world.set(**{'routing.rate_limit_per_user': 3})
    for _ in range(40):
        assert world.route(distance_km=0).status_code == 400                      # refusé en 400 : ne compte pas
    assert [world.route(seed=i).status_code for i in range(3)] == [200, 200, 200]
    r = world.route(seed=9)
    assert r.status_code == 429 and int(r.headers['Retry-After']) > 0 and len(world.provider.calls) > 0
    calls = len(world.provider.calls)
    assert world.route(seed=10).status_code == 429 and len(world.provider.calls) == calls
    assert world.route(client=world.b).status_code == 200                           # un autre utilisateur n'est pas touché
    assert world.runtime._active_total == 0


def test_the_hourly_limit_defaults_to_thirty_and_a_failed_generation_still_counts(tmp_path):
    w = build_world(tmp_path, FakeProvider(sinuosity=0.05, fail=RoutingUnavailable()))
    codes = [w.route(seed=i).status_code for i in range(31)]
    assert codes[:30] == [503] * 30 and codes[30] == 429
    w.conn.close()


def test_the_call_budget_follows_the_setting_and_never_exceeds_it(world):
    world.set(**{'routing.max_calls': 3, 'routing.seed_retries': 2})
    for factor in (5, 25, 80, 150):
        before = len(world.provider.calls)
        r = world.route(distance_km=factor, seed=factor)
        assert r.status_code in (200, 504, 503)
        assert len(world.provider.calls) - before <= 3
        world.runtime.results.clear_user(world.alice['id'])


def test_a_response_never_contains_the_engine_address_or_a_key(tmp_path):
    w = build_world(tmp_path)
    blob = ''
    for client in (w.a, w.adm):
        blob += call(client, 'get', f'{BASE}/options').get_data(as_text=True) + w.route(client).get_data(as_text=True)
    for forbidden in ('engine.test', '8989', 'http://', 'api_key', 'key='):
        assert forbidden not in blob.replace('http://www.topografix.com', ''), forbidden
    w.conn.close()


# ===== F. Résultats =============================================================================================================

def test_a_user_can_neither_read_nor_save_the_result_of_another_and_every_failure_looks_the_same(world):
    rid = world.route().get_json()['route_meta']['result_id']
    foreign = world.post(world.b, 'save', {'result_id': rid})
    unknown = world.post(world.b, 'save', {'result_id': 'A' * 32})
    assert foreign.status_code == unknown.status_code == 404 and foreign.get_data() == unknown.get_data()
    assert world.conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 0
    assert world.post(world.a, 'save', {'result_id': rid}).status_code == 200
    expired = world.route().get_json()['route_meta']['result_id']
    world.runtime.results.clear_user(world.alice['id'])
    gone = world.post(world.a, 'save', {'result_id': expired})
    assert gone.status_code == 404 and gone.get_data() == unknown.get_data()


@pytest.mark.parametrize('result_id', [None, 5, 1.5, True, [], {}, ['a' * 32], {'id': 'x'}, '', ' ', 'a', 'a' * 19, 'a' * 65, 'a' * 5000, '../../etc/passwd', "' OR 1=1 --",
                                       '\x00' * 32, 'é' * 32, '%s' * 16, '\ud800' * 25])
def test_hostile_result_ids_are_client_errors_never_a_crash(world, result_id):
    payload = {'result_id': result_id}
    r = world.post(world.a, 'save', payload, raw=json.dumps(payload))
    assert r.status_code in (400, 404), (result_id, r.status_code)
    assert world.conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 0


def accounted(store):
    return sum(v.size for v in store._items.values())


def test_the_result_store_enforces_every_cap_with_lru_eviction_and_exact_accounting():
    clock = [0.0]
    store = ResultStore(max_per_user=3, max_total=5, max_bytes=2000, ttl_s=100, clock=lambda: clock[0])
    gpx, payload = b'x' * 100, {'a': 'y' * 50}
    size = store.size_of(gpx, payload)
    assert size == 100 + len(json.dumps(payload, separators=(',', ':')))
    ids = [store.put(1, gpx, payload, {}) for _ in range(5)]
    assert len(store._items) == 3 and store.get(1, ids[0]) is None and store.get(1, ids[4]) is not None       # 3 par utilisateur
    assert store.stats() == {'results': 3, 'bytes': 3 * size}
    others = [store.put(u, gpx, payload, {}) for u in (2, 3, 4, 5)]
    assert store.stats()['results'] == 5 and store.get(5, others[-1]) is not None and accounted(store) == store.stats()['bytes']
    assert store.get(1, ids[2]) is None or store.stats()['results'] == 5                                      # plafond global : le plus ancien part
    first = store.put(6, b'z' * 900, {'b': 'c'}, {})
    second = store.put(6, b'z' * 900, {'b': 'c'}, {})
    third = store.put(6, b'z' * 900, {'b': 'c'}, {})
    assert store.stats()['bytes'] <= 2000 + 1 and accounted(store) == store.stats()['bytes']
    assert store.get(6, third) is not None and (store.get(6, first) is None or store.get(6, second) is not None)
    clock[0] += 101
    assert store.put(7, gpx, payload, {}) and store.stats()['results'] == 1 and accounted(store) == store.stats()['bytes']     # expirés purgés
    huge = store.put(8, b'h' * 5000, {}, {})
    assert store.get(8, huge) is not None and accounted(store) == store.stats()['bytes']                      # un résultat énorme reste seul


def test_reading_a_result_makes_it_the_most_recently_used():
    store = ResultStore(max_per_user=100, max_total=3, max_bytes=10 ** 9)
    a, b, c = (store.put(1, b'g', {}, {}) for _ in range(3))
    assert store.get(1, a) is not None                                    # a devient le plus récent
    d = store.put(1, b'g', {}, {})
    assert store.get(1, b) is None and store.get(1, a) is not None and store.get(1, c) is not None and store.get(1, d) is not None


def test_the_result_store_ids_are_long_unique_and_bound_to_the_user():
    store = ResultStore(max_per_user=1000, max_total=1000, max_bytes=10 ** 9)
    ids = {store.put(1, b'g', {}, {}) for _ in range(300)}
    assert len(ids) == 300 and all(len(i) >= 30 for i in ids)
    one = next(iter(ids))
    assert store.get(2, one) is None and store.get(1, one) is not None and store.mark_saved(2, one, {}) is False
    assert store.mark_saved(1, one, {'activity_id': 1}) is True and store.get(1, one).saved == {'activity_id': 1}
    assert store.clear_user(2) == 0 and store.clear_user(1) == 300
    assert store.stats() == {'results': 0, 'bytes': 0}


def test_concurrent_puts_keep_the_accounting_exact():
    store = ResultStore(max_per_user=1000, max_total=1000, max_bytes=10 ** 9)
    concurrent(16, lambda i: [store.put(i % 4, b'g' * 10, {'i': i}, {}) for _ in range(60)])
    assert store.stats()['results'] == len(store._items) and store.stats()['bytes'] == sum(v.size for v in store._items.values())


def test_the_global_cap_applies_through_the_api(world):
    world.runtime.results.max_total = 4
    world.runtime.results.max_per_user = 20
    first = world.route(seed=1).get_json()['route_meta']['result_id']
    for seed in range(2, 8):
        world.route(client=world.b if seed % 2 else world.a, seed=seed)
    assert world.runtime.results.stats()['results'] <= 4
    assert world.post(world.a, 'save', {'result_id': first}).status_code == 404                               # le plus ancien est évincé
    world.runtime.results.max_per_user = 2
    ids = [world.route(seed=20 + i).get_json()['route_meta']['result_id'] for i in range(4)]
    assert world.post(world.a, 'save', {'result_id': ids[0]}).status_code == 404 and world.post(world.a, 'save', {'result_id': ids[3]}).status_code == 200


# ===== G. Enregistrement ========================================================================================================

def saved(world, **fields):
    rid = world.route().get_json()['route_meta']['result_id']
    return rid, world.post(world.a, 'save', {'result_id': rid, **fields})


def test_save_creates_a_planned_trace_with_a_dated_default_name_in_the_app_timezone(world):
    world.set(APP_TIMEZONE='Pacific/Auckland')
    rid, r = saved(world)
    assert r.status_code == 200
    row = world.conn.execute("SELECT * FROM activities WHERE id = ?", (r.get_json()['activity_id'],)).fetchone()
    assert (row['kind'], row['source'], row['start_time_known'], row['user_id']) == ('planned', 'route_builder', 0, world.alice['id'])
    assert row['has_time'] == 0
    today = datetime.now(ZoneInfo('Pacific/Auckland')).strftime('%d/%m/%Y')
    assert row['name'] == f'Trace planifiée du {today}'
    assert world.conn.execute("SELECT sha256 FROM activities WHERE id = ?", (row['id'],)).fetchone()[0]


def test_a_second_save_is_a_409_with_the_activity_id_even_with_another_name(world):
    rid, first = saved(world)
    activity = first.get_json()['activity_id']
    for name in (None, 'Autre nom', 'x' * 200):
        payload = {'result_id': rid} if name is None else {'result_id': rid, 'name': name}
        r = world.post(world.a, 'save', payload)
        assert r.status_code == 409 and r.get_json()['activity_id'] == activity
    assert world.conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 1


def test_after_deletion_the_result_can_be_saved_again_even_when_the_id_was_reused_CA_R25b(world):
    rid, first = saved(world)
    n = first.get_json()['activity_id']
    assert call(world.a, 'delete', f'/api/activities/{n}').status_code == 200
    other = world.route(seed=99).get_json()['route_meta']['result_id']
    uploaded = world.a.post('/api/activities', data={'file': (io.BytesIO(world.runtime.results.get(world.alice['id'], other).gpx), 'autre.gpx')},
                            headers={'Origin': 'https://traces.example.com'}, **KW)
    again = world.post(world.a, 'save', {'result_id': rid})
    assert again.status_code == 200 and again.get_json()['activity_id'] != 0
    assert uploaded.status_code in (200, 201, 400, 409, 415)


@pytest.mark.parametrize('name', ['', '   ', 'a' * 201, 'a\nb', 'a\x00b', 'a\tb', 'a‮b', 5, ['x'], {'a': 1}, True, 'x\r\ny'])
def test_invalid_names_are_400_and_save_nothing(world, name):
    rid = world.route().get_json()['route_meta']['result_id']
    payload = {'result_id': rid, 'name': name}
    r = world.post(world.a, 'save', payload, raw=json.dumps(payload))
    assert r.status_code == 400
    assert world.conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 0
    assert world.post(world.a, 'save', {'result_id': rid}).status_code == 200


@pytest.mark.parametrize('name', ['</name><wpt lat="0" lon="0"/>', '<script>alert(1)</script>', '"><x y="', 'a & b < c', "O'Brien", '../../etc/passwd', 'Ünï©ödé ✓', 'a' * 200,
                                  '  espaces  ', '<![CDATA[x]]>', '&#x0;', '${jndi:ldap://x}'])
def test_hostile_but_printable_names_are_stored_as_text_and_the_gpx_stays_valid(world, name):
    rid, r = saved(world, name=name)
    assert r.status_code == 200
    row = world.conn.execute("SELECT name, filename FROM activities WHERE id = ?", (r.get_json()['activity_id'],)).fetchone()
    assert row['name'] == name.strip()
    assert '/' not in row['filename'] or row['filename'].endswith('.gpx')
    assert world.conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 1
    import os
    inside = []
    for root, _dirs, names in os.walk(world.app.config['DATA_DIR']):
        inside += [os.path.join(root, n) for n in names]
    assert all(p.startswith(str(world.app.config['DATA_DIR'])) for p in inside)
    assert not os.path.exists('/etc/passwd.gpx')


def test_a_planned_trace_changes_no_statistic_record_segment_or_counter_CA_R13(world):
    before = call(world.a, 'get', '/api/dashboard').get_json()
    users_before = call(world.adm, 'get', '/api/admin/users').get_json()['users']
    rid, r = saved(world)
    assert r.status_code == 200
    after = call(world.a, 'get', '/api/dashboard').get_json()
    assert after == before
    users_after = call(world.adm, 'get', '/api/admin/users').get_json()['users']
    alice_before = next(u for u in users_before if u['username'] == 'alice')
    alice_after = next(u for u in users_after if u['username'] == 'alice')
    assert alice_after['nb_activities'] == alice_before['nb_activities'] and alice_after['nb_planned'] == alice_before['nb_planned'] + 1
    listing = call(world.a, 'get', '/api/activities').get_json()
    rows = listing['activities'] if isinstance(listing, dict) and 'activities' in listing else listing
    assert any(a.get('kind') == 'planned' for a in rows)
    row = world.conn.execute("SELECT best_efforts FROM activities WHERE id = ?", (r.get_json()['activity_id'],)).fetchone()
    assert row['best_efforts'] in ('{}', None, '')


def test_the_elevation_is_scheduled_at_save_time_through_the_dem_task(world):
    class Recorder:
        def __init__(self):
            self.woken = []

        def wake(self, name):
            self.woken.append(name)
    recorder = Recorder()
    world.app.extensions['scheduler_real'] = world.app.extensions.get('scheduler')
    world.app.extensions['scheduler'] = recorder
    rid, r = saved(world)
    assert r.status_code == 200 and recorder.woken == ['dem_backfill']
    row = world.conn.execute("SELECT dem_status FROM activities WHERE id = ?", (r.get_json()['activity_id'],)).fetchone()
    assert row['dem_status'] in ('pending', None, '')
    world.app.extensions['scheduler'] = world.app.extensions['scheduler_real']


def test_the_stored_quota_is_respected_and_the_failure_is_a_clean_error(world):
    world.set(USER_QUOTA_MB=0)
    rid, r = saved(world)
    assert r.status_code == 200
    world.set(USER_QUOTA_MB=1)
    world.conn.execute("UPDATE activities SET name = name")
    for i in range(3):
        big = world.route(seed=300 + i).get_json()['route_meta']['result_id']
        r = world.post(world.a, 'save', {'result_id': big, 'name': f'n{i}'})
        assert r.status_code in (200, 400, 503) and 'Traceback' not in r.get_data(as_text=True)


def test_deleting_an_account_does_not_leave_its_results_savable(world):
    rid = world.route(client=world.b).get_json()['route_meta']['result_id']
    assert call(world.adm, 'delete', f"/api/admin/users/{world.bob['id']}").status_code == 200
    assert world.post(world.b, 'save', {'result_id': rid}).status_code in (401, 404)


# ===== H. Diagnostic et registre ================================================================================================

def test_the_admin_diagnostic_reports_the_engine_state_and_the_coverage_containment(tmp_path):
    provider = FakeProvider(sinuosity=0.05, bbox=(48.0, 0.0, 53.0, 8.0))
    w = build_world(tmp_path, provider)
    view = next(f for f in call(w.adm, 'get', '/api/admin/features').get_json()['features'] if f['key'] == 'route_builder')
    diagnostic = view['diagnostic']
    assert diagnostic['configured'] is True and diagnostic['available'] is True and diagnostic['checked_at']
    assert diagnostic['info_bbox'] == [48.0, 0.0, 53.0, 8.0] and diagnostic['coverage_contained'] is True
    assert diagnostic['profiles'] == list(provider.profiles)
    w.set(**{'routing.coverage_polygon': [[-5.0, 40.0], [5.0, 40.0], [0.0, 50.0]]})
    view = next(f for f in call(w.adm, 'get', '/api/admin/features').get_json()['features'] if f['key'] == 'route_builder')
    assert view['diagnostic']['coverage_contained'] is False
    provider.available = False
    w.runtime.run_health()
    view = next(f for f in call(w.adm, 'get', '/api/admin/features').get_json()['features'] if f['key'] == 'route_builder')
    assert view['diagnostic']['available'] is False
    assert 'engine.test' not in json.dumps(view)
    w.conn.close()


@pytest.mark.parametrize('value', [DEFAULT_POLYGON, [[0, 0], [1, 0], [0, 1]], [[1, 48], [2, 48], [2, 49], [1, 49]], [[0, -10], [5, -10], [5, 0], [0, 0]],
                                   [[-5, 0], [5, 0], [5, 10], [-5, 10]]])
def test_valid_coverage_polygons_are_accepted_and_stored_as_floats(world, value):
    r = call(world.adm, 'patch', '/api/admin/settings', {'routing.coverage_polygon': value})
    assert r.status_code == 200, r.get_data()
    body = call(world.a, 'get', f'{BASE}/options').get_json()
    assert body['coverage_polygon'] == [[float(a), float(b)] for a, b in value] and all(isinstance(n, float) for v in body['coverage_polygon'] for n in v)
    assert body['coverage_bbox'] == zone.envelope_lonlat(value)


@pytest.mark.parametrize('value', [[], [[1, 2]], [[1, 2], [3, 4]], 'x', None, 5, {'a': 1}, [[1, 2, 3]] * 3, [[1, None]] * 3, [['1', '2']] * 3,
                                   [[True, 1], [2, 2], [3, 1]], [[float('nan'), 0], [1, 1], [1, 0]], [[0, 0], [float('inf'), 1], [1, 0]],
                                   [[-181, 0], [0, 1], [1, 0]], [[0, 0], [181, 1], [1, 0]], [[0, -86], [1, 0], [1, 1]], [[0, 0], [1, 86], [1, 1]],
                                   [[-170, -80], [170, -80], [0, 80]], [[0, 0], [30, 0], [0, 1]], [[0, 0], [1, 30], [1, 0]], [[0, 0], [1, 1], [1, 0], [0, 1]]])
def test_invalid_coverage_polygons_are_refused_at_write_and_nothing_changes(world, value):
    before = call(world.a, 'get', f'{BASE}/options').get_json()
    r = call(world.adm, 'patch', '/api/admin/settings', {'routing.coverage_polygon': value}, raw=json.dumps({'routing.coverage_polygon': value}))
    assert r.status_code == 400, (value, r.status_code)
    after = call(world.a, 'get', f'{BASE}/options').get_json()
    assert (after['coverage_polygon'], after['coverage_bbox']) == (before['coverage_polygon'], before['coverage_bbox'])


@pytest.mark.parametrize('value', [[[0, 0], [10 ** 400, 1], [1, 0]], [[-10 ** 400, 0], [1, 1], [1, 0]], [[0, 2 ** 1030], [1, 1], [1, 0]]])
def test_a_huge_integer_in_a_polygon_is_a_400_never_a_500(world, value):
    r = call(world.adm, 'patch', '/api/admin/settings', {'routing.coverage_polygon': value}, raw=json.dumps({'routing.coverage_polygon': value}))
    assert r.status_code == 400


def test_the_removed_rectangle_setting_is_unknown_to_the_settings_api(world):
    """Lot Z1 (CA-85) : plus de ligne `routing.coverage_bbox` ; son PATCH est refusé comme tout réglage inconnu, la version ne bouge pas."""
    assert 'routing.coverage_bbox' not in REGISTRY
    listed = call(world.adm, 'get', '/api/admin/settings').get_json()
    assert all(s['key'] != 'routing.coverage_bbox' for s in listed['settings'])
    r = call(world.adm, 'patch', '/api/admin/settings', {'routing.coverage_bbox': [3.0, 50.6, 3.2, 50.7]})
    assert r.status_code == 400 and 'Réglage inconnu' in r.get_json()['error'], r.get_data(as_text=True)
    assert call(world.adm, 'get', '/api/admin/settings').get_json()['version'] == listed['version']


def test_the_default_coverage_is_the_one_written_in_the_spec():
    """Lit la valeur dans le tableau §5.6 de la spec (ligne routing.coverage_polygon), pas une copie en dur : spec et registre ne divergent plus."""
    path = Path(__file__).resolve().parents[2] / 'docs' / 'specs' / 'gpx-route-builder.md'
    if not path.is_file():
        pytest.skip("spec absente : la CI ne monte que web-app dans le conteneur (le test protège en local et en développement)")
    spec = path.read_text(encoding='utf-8')
    row = next(line for line in spec.splitlines() if line.startswith('| `routing.coverage_polygon`'))
    written = [json.loads(m) for m in re.findall(r'\[\[[-0-9., \[\]]+\]\]', row)]
    assert written and written[-1] == DEFAULT_POLYGON, (written, DEFAULT_POLYGON)
    assert 'routing.coverage_bbox' not in ''.join(line for line in spec.splitlines() if line.startswith('| `routing.'))      # plus de ligne du rectangle


def test_a_forged_coverage_in_the_database_falls_back_to_the_default_not_to_no_limit(world):
    for forged in ('"x"', '[1,2,3]', '[]', '[[0,0],[1,1]]', '[[0,0],[1,1],[1,0],[0,1]]', 'null', '[[NaN,0],[1,1],[1,0]]'):
        world.conn.execute("INSERT OR REPLACE INTO settings (key, value, updated_at, updated_by) VALUES ('routing.coverage_polygon', ?, 'x', 'sql')", (forged,))
        world.conn.execute("UPDATE settings_meta SET version = version + 1")
        world.conn.commit()
        world.app.config['SETTINGS_STORE'].refresh(world.conn, force=True)
        options = call(world.a, 'get', f'{BASE}/options').get_json()
        assert options['coverage_polygon'] == DEFAULT_POLYGON and options['coverage_bbox'] == [1.35, 48.83, 6.42, 51.55], forged
        assert world.route(points=[[52.37, 4.90], [52.38, 4.91]], distance_km=None).status_code == 400
    assert world.provider.calls == []


def test_the_configured_coverage_blocks_points_before_any_call_and_follows_changes(world):
    world.set(**{'routing.coverage_polygon': [[3.0, 50.6], [3.2, 50.6], [3.2, 50.7], [3.0, 50.7]]})
    assert world.route(points=[[50.63, 3.05], [50.66, 3.10]], distance_km=None).status_code == 200
    calls = len(world.provider.calls)
    assert world.route(points=[[50.63, 3.05], [50.66, 3.30]], distance_km=None).status_code == 400
    assert len(world.provider.calls) == calls


# ===== I. « Autre proposition » (RB-4.5) et messages d'erreur distincts (décisions de recette) =================================

def another(world, previous, client=None, **fields):
    meta = previous.get_json()['route_meta']
    return world.route(client, seed=meta['next_seed'], previous_result_id=meta['result_id'], **fields)


def geometry(response):
    return json.dumps(response.get_json()['points'], sort_keys=True)


def test_a_chain_of_other_proposals_never_repeats_the_previous_one_and_mostly_gives_new_ones(world):
    world.set(**{'routing.rate_limit_per_user': 100})
    current = world.route(seed=3)
    assert current.status_code == 200
    seen, previous_geometry = [geometry(current)], geometry(current)
    for _ in range(11):
        current = another(world, current)
        assert current.status_code == 200
        assert geometry(current) != previous_geometry, "« Autre proposition » a rendu la trace précédente"
        previous_geometry = geometry(current)
        seen.append(previous_geometry)
        world.runtime.results.max_per_user = 50
    assert len(set(seen)) >= 8, f"seulement {len(set(seen))} traces distinctes sur 12 propositions"


def test_when_no_variant_exists_the_answer_says_so_with_a_stable_code_and_the_same_trace(tmp_path):
    w = build_world(tmp_path, FakeProvider(sinuosity=0.0))
    base = w.route(distance_km=None).get_json()['route_meta']['distance_m'] / 1000           # sans cible : la boucle de base, aucune variante
    first = w.route(distance_km=round(base, 1), seed=5)
    assert first.status_code == 200 and first.get_json()['route_meta']['detours'] == 0
    second = another(w, first, distance_km=round(base, 1))
    codes = [x['code'] for x in second.get_json()['route_meta']['warnings']]
    assert second.status_code == 200 and 'no_alternative' in codes and geometry(second) == geometry(first)
    w.conn.close()


@pytest.mark.parametrize('previous', [None, 5, 1.5, True, [], {}, '', 'x', 'a' * 100, '../../etc/passwd', "' OR 1=1 --", '\x00' * 32, '\ud800' * 30])
def test_a_hostile_previous_result_id_is_ignored_and_never_an_error(world, previous):
    body = {'points': POINTS, 'activity': 'run', 'distance_km': 25, 'seed': 4, 'previous_result_id': previous}
    r = world.post(world.a, 'route', body, raw=json.dumps(body))
    assert r.status_code == 200, (previous, r.status_code, r.get_data()[:100])
    plain = world.route(seed=4)
    assert geometry(r) == geometry(plain)


def test_the_previous_result_of_another_user_has_no_influence_and_no_oracle(world):
    mine = world.route(client=world.b, seed=6)
    bob_id = mine.get_json()['route_meta']['result_id']
    with_foreign = world.route(client=world.a, seed=6, previous_result_id=bob_id)
    without = world.route(client=world.a, seed=6)
    assert geometry(with_foreign) == geometry(without)                                       # la série de Bob n'écarte rien chez Alice
    assert [w['code'] for w in with_foreign.get_json()['route_meta']['warnings']] == [w['code'] for w in without.get_json()['route_meta']['warnings']]


def test_the_server_side_series_is_never_sent_back_to_the_browser_and_is_bounded(world):
    world.set(**{'routing.rate_limit_per_user': 100})
    world.runtime.results.max_per_user = 50
    current = world.route(seed=1)
    for _ in range(25):
        current = another(world, current)
    meta = current.get_json()['route_meta']
    assert 'seen' not in meta
    stored = world.runtime.results.get(world.alice['id'], meta['result_id']).meta
    assert len(stored['seen']) <= 20 and all(isinstance(x, str) and len(x) <= 40 for x in stored['seen'])


def test_a_point_outside_the_coverage_is_a_400_with_its_number_and_never_the_unreachable_wording(world):
    r = world.route(points=[[50.63, 3.05], [52.37, 4.90], [50.64, 3.15]])
    assert r.status_code == 400 and 'point 2' in r.get_json()['error'] and 'zone couverte' in r.get_json()['error']
    assert 'voie praticable' not in r.get_json()['error'] and world.provider.calls == []


def test_a_point_in_the_coverage_that_the_engine_cannot_snap_is_a_422_with_its_number_and_the_other_wording(tmp_path):
    class Elsewhere(FakeProvider):
        def route(self, waypoints, profile, deadline):
            self._enter('route', deadline, waypoints, profile)
            return RouteResult(points=[(50.0, 5.0), (50.01, 5.01), (50.0, 5.0)], distance_m=2500.0)
    w = build_world(tmp_path, Elsewhere(sinuosity=0.05))
    r = w.route(points=[[50.63, 3.05], [50.63, 3.05], [50.66, 3.10]])
    text = r.get_json()['error']
    assert r.status_code == 422 and 'voie praticable' in text and 'zone couverte' not in text and re.search(r'point \d', text)
    assert '50.6' not in text and 'engine.test' not in text
    unroutable = build_world(tmp_path / 'u', FakeProvider(sinuosity=0.05, unroutable=lambda p: abs(p[0] - 50.66) < 1e-6))
    r2 = unroutable.route(points=[[50.63, 3.05], [50.66, 3.10], [50.64, 3.15]])
    assert r2.status_code == 422 and 'point 2' in r2.get_json()['error'] and 'zone couverte' not in r2.get_json()['error']
    w.conn.close()
    unroutable.conn.close()
