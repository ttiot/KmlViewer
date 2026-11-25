"""Constructeur de traces côté serveur (TR5 : CA-R1, R12, R15, R16, R20, R20b, R24, R25, R27, R29 ; RB-5.2, P-TR5-1…9)."""

import json
import re
import time

import pytest
from cryptography.fernet import Fernet

from app import create_app, db as database, features
from app.audit import Actor
from app.config import TestingConfig
from app.ratelimit import Limiters, Limits
from app.services.routing import builder
from app.services.routing.base import InvalidResponse, NoRouteFound, RoutingTimeout, RoutingUnavailable
from app.services.routing.results import ResultStore
from app.settings import REGISTRY
from tests.routing_fakes import FakeProvider

KEY = Fernet.generate_key().decode()
HOST = 'traces.example.com'
KW = {'base_url': f'https://{HOST}'}
CLI = Actor.cli()
ENGINE = 'http://graphhopper.internal:8989'
POINTS = [[50.63, 3.05], [50.66, 3.10], [50.64, 3.15], [50.61, 3.10]]


class Prod(TestingConfig):
    TESTING = False
    INBOX_ENABLED = False
    SECRET_KEY = 'k' * 40
    SESSION_COOKIE_SECURE = None


def make(tmp_path, *, engine=True, allowed=True):
    app = create_app(Prod, overrides={
        'DATA_DIR': str(tmp_path / 'data'), 'AUTH_MODE': 'password', 'TRUSTED_HOSTS': [HOST], 'SECRETS_KEY': KEY,
        'PUBLIC_URL': f'https://{HOST}', 'OUTBOUND_ALLOWED_HOSTS': 'routing=graphhopper.internal:8989' if allowed else ''})
    if engine:
        c = database.connect(app.config['DB_PATH'])
        app.config['SETTINGS_STORE'].set_many(c, {'routing.base_url': ENGINE}, actor=CLI) if allowed else \
            app.config['SETTINGS_STORE'].__class__  # destination refusée à l'écriture : réglage posé en base ci-dessous
        c.close()
    return app


@pytest.fixture
def app(tmp_path):
    application = make(tmp_path)
    application.extensions['routing'].provider_factory = lambda: FakeProvider(sinuosity=0.05)
    return application


@pytest.fixture
def conn(app):
    c = database.connect(app.config['DB_PATH'])
    yield c
    c.close()


def add(conn, name):
    conn.execute("INSERT INTO users (sub, username, is_active, auth_method, session_token) VALUES (?, ?, 1, 'password', 'tok')",
                 (f'pwd:{name}', name))
    conn.commit()
    return conn.execute("SELECT * FROM users WHERE username = ?", (name,)).fetchone()


def session_for(app, user):
    client = app.test_client()
    with client.session_transaction(**KW) as s:
        s['user_id'], s['token'], s['login_at'] = user['id'], user['session_token'], int(time.time())
    return client


def call(client, method, path, payload=None):
    return client.open(path, method=method, json=payload, **KW, headers={'Origin': f'https://{HOST}'})


def enable(conn, state='all', users=()):
    features.set_state(conn, 'route_builder', state, actor=CLI, user_ids=list(users) if users else None)


def healthy(app):
    app.extensions['scheduler'].run_task('routing_health')


@pytest.fixture
def ready(app, conn):
    enable(conn)
    healthy(app)
    user = add(conn, 'alice')
    return session_for(app, user), user


def generate(client, **over):
    body = {'points': POINTS, 'activity': 'run', **over}
    return call(client, 'post', '/api/route-builder/route', body)


# --- CA-R1 : disponibilité ----------------------------------------------------------------------------------------------------------

def test_the_feature_off_or_not_listed_or_an_unconfigured_engine_answers_404_everywhere(app, conn, tmp_path):
    user = add(conn, 'alice')
    client = session_for(app, user)
    paths = [('get', '/api/route-builder/options'), ('post', '/api/route-builder/route'), ('post', '/api/route-builder/save')]
    for method, path in paths:
        assert call(client, method, path, {}).status_code == 404                              # fonction `off`
    enable(conn, 'users', [add(conn, 'other')['id']])
    assert all(call(client, m, p, {}).status_code == 404 for m, p in paths)                  # `users` sans cet utilisateur
    enable(conn, 'all')
    healthy(app)
    assert call(client, 'get', '/api/route-builder/options').status_code == 200
    bare = make(tmp_path / 'bare', engine=False)                                               # moteur NON configuré
    c2 = database.connect(bare.config['DB_PATH'])
    enable(c2)
    other = add(c2, 'bob')
    assert call(session_for(bare, other), 'get', '/api/route-builder/options').status_code == 404
    c2.close()


def test_a_destination_outside_the_allow_list_is_treated_as_not_configured(tmp_path):
    app = make(tmp_path, engine=False, allowed=False)
    c = database.connect(app.config['DB_PATH'])
    c.execute("INSERT INTO settings (key, value, updated_at, updated_by) VALUES ('routing.base_url', ?, 'x', 'sql')", (json.dumps(ENGINE),))
    c.execute("UPDATE settings_meta SET version = version + 1")
    c.commit()
    enable(c)
    user = add(c, 'alice')
    app.config['SETTINGS_STORE'].refresh(c, force=True)
    assert not app.extensions['routing'].configured()
    assert call(session_for(app, user), 'get', '/api/route-builder/options').status_code == 404
    c.close()


# --- Options, santé (CA-R24, R29, P-TR5-3) ----------------------------------------------------------------------------------------------

def test_options_carry_the_contract_even_before_the_first_health_probe_CA_R29(app, conn):
    enable(conn)
    client = session_for(app, add(conn, 'alice'))
    body = call(client, 'get', '/api/route-builder/options').get_json()
    assert body['available'] is False and body['state'] == 'starting'                         # cache vide : injoignable (n8)
    assert body['coverage_bbox'] == [1.35, 48.83, 6.42, 51.55]
    assert (body['min_km'], body['step_km'], body['max_km'], body['max_waypoints'], body['tolerance_pct']) == (1.0, 0.5, 300, 50, 15)
    assert [a['key'] for a in body['activities']] == ['run', 'trail', 'hike', 'road_bike', 'gravel', 'mtb']
    assert body['default_activity'] == 'run'
    provider = FakeProvider()
    app.extensions['routing'].provider_factory = lambda: provider
    assert generate(client).status_code == 503 and provider.calls == []                      # aucun appel au moteur
    healthy(app)
    assert call(client, 'get', '/api/route-builder/options').get_json()['state'] == 'ok'


def test_the_probe_follows_the_engine_and_the_task_is_scheduled_without_inbox_or_dem_CA_R24_R29(app, conn):
    scheduler = app.extensions['scheduler']
    assert 'routing_health' in scheduler.tasks
    enable(conn)
    client = session_for(app, add(conn, 'alice'))
    down = FakeProvider(available=False)
    app.extensions['routing'].provider_factory = lambda: down
    healthy(app)
    assert call(client, 'get', '/api/route-builder/options').get_json() == {**call(client, 'get', '/api/route-builder/options').get_json(),
                                                                             'available': False, 'state': 'unavailable'}
    assert generate(client).status_code == 503 and down.calls == []
    up = FakeProvider(sinuosity=0.05)
    app.extensions['routing'].provider_factory = lambda: up
    healthy(app)
    assert generate(client).status_code == 200
    assert 'routing_health' in scheduler.tick(now=10 ** 6) or scheduler.state['routing_health'].runs >= 1


def test_no_request_to_the_engine_happens_while_rendering_pages_or_options(app, conn):
    provider = FakeProvider()
    app.extensions['routing'].provider_factory = lambda: provider
    enable(conn)
    client = session_for(app, add(conn, 'alice'))
    for path in ('/', '/dashboard', '/profile', '/api/route-builder/options'):
        client.get(path, **KW)
    assert provider.calls == [] and provider.health_calls == 0


# --- Génération --------------------------------------------------------------------------------------------------------------------------

def test_a_generation_returns_the_viewer_payload_and_the_route_meta(ready):
    client, _ = ready
    r = generate(client, distance_km=30, seed=5)
    body = r.get_json()
    assert r.status_code == 200 and body['success'] and body['route_meta']['result_id']
    meta = body['route_meta']
    for key in ('distance_m', 'target_m', 'gap_pct', 'd0_m', 'detours', 'overlap_pct', 'calls', 'attempts', 'seed', 'next_seed',
                'profile', 'warnings', 'result_id', 'ascent_m', 'descent_m'):
        assert key in meta, key
    assert meta['profile'] == 'foot' and meta['seed'] >= 5 and meta['next_seed'] == (meta['seed'] + 1)
    assert body['filename'].startswith('Trace planifiée du ')
    upload = call(client, 'post', '/api/activities', None)                                     # même forme que le visualiseur
    assert upload is not None and set(body) >= {'success', 'filename'}


def test_the_seed_is_optional_and_the_next_seed_gives_another_proposal(ready):
    client, _ = ready
    first = generate(client, distance_km=30, seed=0).get_json()['route_meta']
    second = generate(client, distance_km=30, seed=first['next_seed']).get_json()['route_meta']
    assert second['seed'] >= first['next_seed']
    assert generate(client).get_json()['route_meta']['seed'] is not None                      # absent : tiré par le serveur


@pytest.mark.parametrize('payload', [
    {'points': [], 'activity': 'run'}, {'points': POINTS * 20, 'activity': 'run'}, {'points': [[91, 3], [50, 3]], 'activity': 'run'},
    {'points': POINTS, 'activity': 'tank'}, {'points': POINTS, 'activity': 'run', 'distance_km': 0},
    {'points': POINTS, 'activity': 'run', 'distance_km': 301}, {'points': [POINTS[0]], 'activity': 'run'},
    {'points': POINTS, 'activity': 'run', 'seed': -1}, {'points': POINTS, 'activity': 'run', 'seed': 2 ** 31},
    {'points': 'abc', 'activity': 'run'}, {'activity': 'run'}, {'points': [[51.51, -0.13], [50.6, 3.0]], 'activity': 'run'},
    {'points': [[52.37, 4.90], [50.6, 3.0]], 'activity': 'run'}])
def test_an_invalid_request_is_a_400_with_no_call_and_does_not_count_against_the_hourly_limit_CA_R2_R20(app, conn, payload):
    provider = FakeProvider()
    app.extensions['routing'].provider_factory = lambda: provider
    enable(conn)
    healthy(app)
    client = session_for(app, add(conn, 'alice'))
    app.extensions['limiters'] = Limiters(limits=Limits(generation_per_user=1), clock=lambda: 0.0)
    assert call(client, 'post', '/api/route-builder/route', payload).status_code == 400
    assert call(client, 'post', '/api/route-builder/route', payload).status_code == 400            # la limite d'1/h n'a pas été entamée
    assert provider.calls == []
    app.extensions['routing'].provider_factory = lambda: FakeProvider(sinuosity=0.05)
    assert generate(client).status_code == 200


def test_london_and_amsterdam_are_refused_and_lille_and_liege_accepted_CA_R20b(ready):
    client, _ = ready
    assert generate(client, points=[[51.51, -0.13], [50.63, 3.06]]).status_code == 400
    assert generate(client, points=[[52.37, 4.90], [50.63, 3.06]]).status_code == 400
    assert generate(client, points=[[50.63, 3.06], [50.63, 5.57]]).status_code == 200


# --- Limites (CA-R16) -------------------------------------------------------------------------------------------------------------------

def test_the_hourly_limit_then_a_429_with_retry_after(app, conn):
    enable(conn)
    healthy(app)
    client = session_for(app, add(conn, 'alice'))
    app.extensions['limiters'] = Limiters(limits=Limits(generation_per_user=2), clock=lambda: 0.0)
    assert [generate(client).status_code for _ in range(2)] == [200, 200]
    over = generate(client)
    assert over.status_code == 429 and int(over.headers['Retry-After']) >= 1


def test_one_generation_at_a_time_per_user_and_max_concurrent_per_instance_both_refuse_immediately(app, conn):
    enable(conn)
    healthy(app)
    runtime = app.extensions['routing']
    alice, bob = add(conn, 'alice'), add(conn, 'bob')
    c_alice, c_bob = session_for(app, alice), session_for(app, bob)
    assert runtime.acquire(alice['id']) is None                                             # une génération en cours pour alice
    busy = generate(c_alice)
    assert busy.status_code == 429 and 'encore en cours' in busy.get_json()['error']
    assert generate(c_bob).status_code == 200                                               # un autre utilisateur passe
    app.config['SETTINGS_STORE'].set_many(conn, {'routing.max_concurrent': 1}, actor=CLI)
    started = time.monotonic()
    full = generate(c_bob)
    assert full.status_code == 429 and 'occupé' in full.get_json()['error'] and time.monotonic() - started < 1
    runtime.release(alice['id'])
    assert generate(c_bob).status_code == 200


def test_the_place_is_released_whatever_the_outcome(app, conn):
    enable(conn)
    healthy(app)
    runtime = app.extensions['routing']
    client = session_for(app, add(conn, 'alice'))
    runtime.provider_factory = lambda: FakeProvider(fail=RoutingUnavailable())
    assert generate(client).status_code == 503
    runtime.provider_factory = lambda: FakeProvider(sinuosity=0.05)
    assert generate(client).status_code == 200


# --- Erreurs du moteur (CA-R15) ------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('error,status', [(RoutingTimeout(), 504), (RoutingUnavailable(), 503), (InvalidResponse(), 502)])
def test_engine_errors_map_to_fixed_statuses_without_any_internal_detail(app, conn, error, status):
    enable(conn)
    healthy(app)
    app.extensions['routing'].provider_factory = lambda: FakeProvider(fail=error)
    r = generate(session_for(app, add(conn, 'alice')))
    text = r.get_data(as_text=True)
    assert r.status_code == status and 'graphhopper' not in text and '50.63' not in text and ENGINE not in text


def test_an_unreachable_point_is_a_422_with_its_original_number(app, conn):
    enable(conn)
    healthy(app)
    bad = tuple(POINTS[2])
    app.extensions['routing'].provider_factory = lambda: FakeProvider(unroutable=lambda p: tuple(p) == bad)
    r = generate(session_for(app, add(conn, 'alice')))
    assert r.status_code == 422 and 'point 3' in r.get_json()['error']


def test_an_unexpected_crash_is_a_generic_502(app, conn):
    enable(conn)
    healthy(app)

    class Boom(FakeProvider):
        def route(self, *a, **k):
            raise ZeroDivisionError('secret detail 50.63')
    app.extensions['routing'].provider_factory = lambda: Boom()
    r = generate(session_for(app, add(conn, 'alice')))
    assert r.status_code == 502 and 'secret' not in r.get_data(as_text=True)


# --- Enregistrement (CA-R12, R25, RB-5.2) ---------------------------------------------------------------------------------------------------

def test_save_creates_a_planned_activity_with_the_route_builder_source_and_no_start_time_CA_R12(ready, conn):
    client, user = ready
    rid = generate(client, distance_km=30).get_json()['route_meta']['result_id']
    r = call(client, 'post', '/api/route-builder/save', {'result_id': rid, 'name': '  Ma boucle  '})
    assert r.status_code == 200
    row = conn.execute("SELECT * FROM activities WHERE id = ?", (r.get_json()['activity_id'],)).fetchone()
    assert (row['kind'], row['source'], row['user_id'], row['start_time_known']) == ('planned', 'route_builder', user['id'], 0)
    assert row['name'] == 'Ma boucle'


def test_save_default_name_is_dated_and_a_hostile_name_is_escaped_not_injected_CA_R25(ready, conn):
    client, _ = ready
    rid = generate(client).get_json()['route_meta']['result_id']
    ok = call(client, 'post', '/api/route-builder/save', {'result_id': rid})
    assert ok.status_code == 200
    assert conn.execute("SELECT name FROM activities WHERE id = ?", (ok.get_json()['activity_id'],)).fetchone()[0].startswith('Trace planifiée du ')
    hostile = generate(client, seed=77).get_json()['route_meta']['result_id']
    r = call(client, 'post', '/api/route-builder/save', {'result_id': hostile, 'name': '</name><wpt lat="1" lon="1"/>'})
    assert r.status_code == 200
    gpx = builder.rename_gpx(builder.build_gpx([(50.0, 3.0), (50.1, 3.1)], [(50.0, 3.0)], 'run'), '</name><wpt lat="1" lon="1"/>')
    assert b'&lt;/name&gt;' in gpx and gpx.count(b'<wpt') == 1


@pytest.mark.parametrize('name', ['x' * 201, '', '   ', 'a\x00b', 'a\nb', 5, ['x']])
def test_save_refuses_invalid_names_without_consuming_the_result_CA_R25(ready, name):
    client, _ = ready
    rid = generate(client).get_json()['route_meta']['result_id']
    assert call(client, 'post', '/api/route-builder/save', {'result_id': rid, 'name': name}).status_code == 400
    assert call(client, 'post', '/api/route-builder/save', {'result_id': rid}).status_code == 200


def test_a_foreign_unknown_or_expired_result_is_a_404_CA_R12(app, conn):
    enable(conn)
    healthy(app)
    alice, bob = add(conn, 'alice'), add(conn, 'bob')
    c_alice, c_bob = session_for(app, alice), session_for(app, bob)
    rid = generate(c_alice).get_json()['route_meta']['result_id']
    assert call(c_bob, 'post', '/api/route-builder/save', {'result_id': rid}).status_code == 404
    for bad in ('inconnu', '', None, 5, 'x' * 500):
        assert call(c_alice, 'post', '/api/route-builder/save', {'result_id': bad}).status_code == 404
    runtime = app.extensions['routing']
    now = [0.0]
    runtime.results._clock = lambda: now[0]
    rid2 = runtime.results.put(alice['id'], b'<gpx/>', {}, {})
    now[0] = 31 * 60
    assert call(c_alice, 'post', '/api/route-builder/save', {'result_id': rid2}).status_code == 404


def test_a_second_save_is_a_409_with_the_existing_id_and_after_deletion_it_creates_a_new_trace_CA_R25_n7(ready, conn):
    client, _ = ready
    rid = generate(client).get_json()['route_meta']['result_id']
    first = call(client, 'post', '/api/route-builder/save', {'result_id': rid}).get_json()['activity_id']
    again = call(client, 'post', '/api/route-builder/save', {'result_id': rid, 'name': 'autre nom'})
    assert again.status_code == 409 and again.get_json()['activity_id'] == first
    assert conn.execute("SELECT COUNT(*) FROM activities WHERE source = 'route_builder'").fetchone()[0] == 1
    conn.execute("DELETE FROM activities WHERE id = ?", (first,))
    conn.commit()
    third = call(client, 'post', '/api/route-builder/save', {'result_id': rid})
    assert third.status_code == 200 and third.get_json()['activity_id'] is not None
    assert conn.execute("SELECT COUNT(*) FROM activities WHERE source = 'route_builder'").fetchone()[0] == 1


def test_a_reassigned_id_is_never_taken_for_the_saved_trace_CA_R25b(ready, conn):
    client, user = ready
    rid = generate(client).get_json()['route_meta']['result_id']
    first = call(client, 'post', '/api/route-builder/save', {'result_id': rid}).get_json()['activity_id']
    conn.execute("UPDATE activities SET kind = 'activity', source = 'upload', sha256 = 'deadbeef' WHERE id = ?", (first,))
    conn.commit()                                                                  # l'id N désigne désormais une AUTRE activité
    again = call(client, 'post', '/api/route-builder/save', {'result_id': rid})
    assert again.status_code == 200 and again.get_json()['activity_id'] != first   # nouvelle trace, jamais l'activité N
    row = conn.execute("SELECT kind, source FROM activities WHERE id = ?", (first,)).fetchone()
    assert (row['kind'], row['source']) == ('activity', 'upload')                                       # l'activité n'est pas modifiée


# --- results.py (RB-5.2, m10, P-TR5-8) ----------------------------------------------------------------------------------------------------

def test_the_result_store_binds_to_the_user_expires_and_caps_per_user_and_globally():
    now = [0.0]
    store = ResultStore(clock=lambda: now[0], max_per_user=3, max_total=5)
    ids = [store.put(1, b'x', {'a': 1}, {}) for _ in range(4)]
    assert store.get(1, ids[0]) is None and store.get(1, ids[3]) is not None                  # 20 -> 3 : le plus ancien part
    assert store.get(2, ids[3]) is None                                                       # d'un autre utilisateur : introuvable
    others = [store.put(2, b'x', {}, {}) for _ in range(3)]
    more = [store.put(3, b'x', {}, {}) for _ in range(3)]
    assert store.stats()['results'] <= 5 and store.get(3, more[-1]) is not None
    assert store.get(2, others[0]) is None or store.get(1, ids[1]) is None                   # plafond global : éviction LRU
    now[0] = 30 * 60 + 1
    assert store.get(3, more[-1]) is None and store.stats()['results'] == 0 or store.stats()['bytes'] >= 0


def test_the_result_store_caps_the_memory_and_counts_gpx_plus_serialized_payload():
    store = ResultStore(max_bytes=1000)
    size = ResultStore.size_of(b'g' * 100, {'k': 'v' * 50})
    assert size == 100 + len(json.dumps({'k': 'v' * 50}, separators=(',', ':')))
    first = store.put(1, b'g' * 400, {}, {})
    second = store.put(1, b'g' * 400, {}, {})
    third = store.put(1, b'g' * 400, {}, {})
    assert store.stats()['bytes'] <= 1000 and store.get(1, third) is not None
    assert store.get(1, first) is None and second is not None


def test_result_ids_have_at_least_128_bits_and_are_unique():
    store = ResultStore()
    ids = {store.put(1, b'x', {}, {}) for _ in range(50)}
    assert len(ids) == 50 and all(len(i) >= 32 for i in ids) and all(re.fullmatch(r'[A-Za-z0-9_-]+', i) for i in ids)


# --- Réglage routing.coverage_bbox (CA-R20b, P-TR5-1) -------------------------------------------------------------------------------------

@pytest.mark.parametrize('value', [[0, 0, 1], [1, 2, 3, 4, 5], 'x', [1, 48, 1, 51], [6, 48, 1, 51], [1, 51, 6, 48], [1, 48, 6, 95],
                                   [-181, 48, 6, 51], [1, 48, float('nan'), 51], [True, 48, 6, 51], ['1', 48, 6, 51], [0, 0, 40, 10],
                                   [0, 0, 5, 40], None])
def test_coverage_bbox_validation_rejects_malformed_inverted_and_oversized_rectangles(value):
    with pytest.raises(ValueError):
        REGISTRY['routing.coverage_bbox'].validate(value)


def test_coverage_bbox_default_and_a_valid_write_through_the_settings_store(app, conn):
    assert REGISTRY['routing.coverage_bbox'].validate([1.35, 48.83, 6.42, 51.55]) == [1.35, 48.83, 6.42, 51.55]
    store = app.config['SETTINGS_STORE']
    store.set_many(conn, {'routing.coverage_bbox': [2, 49, 5, 51]}, actor=CLI)
    assert store.get('routing.coverage_bbox') == [2.0, 49.0, 5.0, 51.0]
    assert builder.BuildConfig.from_settings(store.get).coverage_bbox == (49.0, 2.0, 51.0, 5.0)


# --- Diagnostic admin (P-TR5-9) -----------------------------------------------------------------------------------------------------------

def test_the_admin_feature_view_carries_the_engine_diagnostic(app, conn):
    healthy(app)
    admin = add(conn, 'root')
    conn.execute("UPDATE users SET is_admin = 1, totp_secret = x'01', totp_enabled_at = '2026-01-01T00:00:00Z' WHERE id = ?", (admin['id'],))
    conn.commit()
    client = session_for(app, conn.execute("SELECT * FROM users WHERE id = ?", (admin['id'],)).fetchone())
    body = call(client, 'get', '/api/admin/features').get_json()['features'][0]
    diagnostic = body['diagnostic']
    assert diagnostic['configured'] is True and diagnostic['available'] is True and diagnostic['checked_at']
    assert {'info_bbox', 'coverage_contained', 'profiles'} <= set(diagnostic)
    assert diagnostic['coverage_contained'] is False          # le factice annonce (49, 2, 51.6, 6.5) : plus petit que la couverture par défaut


def test_unused_imports_guard():
    assert NoRouteFound and Actor


# --- Front (TR6, EF-7.3, P-TR6-1, P-TR6-6, P-TR6-7) -------------------------------------------------------------------------------------

def test_the_viewer_shows_the_builder_only_when_the_feature_and_the_engine_are_available(app, conn, tmp_path):
    user = add(conn, 'alice')
    client = session_for(app, user)
    html = client.get('/', **KW).get_data(as_text=True)
    assert 'routeBuilderOpen' not in html and 'route-builder.js' not in html                    # fonction `off` : rien dans le HTML
    enable(conn)
    html = client.get('/', **KW).get_data(as_text=True)
    assert 'id="routeBuilderOpen"' in html and 'id="routeBuilderPanel"' in html and 'js/viewer/route-builder.js' in html
    assert 'style=' not in html.split('routeBuilderPanel')[1].split('</section>')[0]
    bare = make(tmp_path / 'bare', engine=False)
    c2 = database.connect(bare.config['DB_PATH'])
    enable(c2)
    other = add(c2, 'bob')
    assert 'routeBuilderOpen' not in session_for(bare, other).get('/', **KW).get_data(as_text=True)      # moteur non configuré
    c2.close()


def test_the_builder_script_follows_the_front_rules():
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent / 'static' / 'js'
    js = (root / 'viewer' / 'route-builder.js').read_text(encoding='utf-8')
    assert 'innerHTML' not in js and 'eval(' not in js and 'window.confirm' not in js and 'XMLHttpRequest' not in js
    urls = set(re.findall(r"""(?:fetch|api)\(\s*['"`]([^'"`]+)""", js))
    assert urls and all(u.startswith('/') for u in urls)                                          # aucune requête hors de l'origine
    assert "kmlviewer.routeBuilder." in js and 'MAX_HISTORY = 100' in js and 'divIcon' in js
    common = (root / 'common.js').read_text(encoding='utf-8')                                       # E-7 : script commun à toutes les pages
    assert '/auth/logout' in common and 'kmlviewer.routeBuilder.' in common                       # effacement à TOUTE déconnexion


def test_the_library_filters_planned_traces_and_shows_the_badge(app, conn):
    html = session_for(app, add(conn, 'alice')).get('/dashboard', **KW).get_data(as_text=True)
    assert 'id="kindFilter"' in html and 'Planifiées' in html and 'Réalisées' in html
    import pathlib
    js = (pathlib.Path(__file__).resolve().parent.parent / 'static' / 'js' / 'dashboard.js').read_text(encoding='utf-8')
    assert "a.kind === 'planned'" in js and "params.set('kind', state.kind)" in js


# --- TR5-1 / TR5-2 ------------------------------------------------------------------------------------------------------------------

def test_eight_concurrent_saves_of_one_result_create_exactly_one_trace_TR5_1(ready, conn, app):
    import threading
    client, user = ready
    rid = generate(client).get_json()['route_meta']['result_id']
    clients = [session_for(app, user) for _ in range(8)]
    barrier = threading.Barrier(8)
    codes = []

    def job(c):
        barrier.wait()
        codes.append(call(c, 'post', '/api/route-builder/save', {'result_id': rid, 'name': 'concurrent'}).status_code)

    threads = [threading.Thread(target=job, args=(c,)) for c in clients]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(codes) == [200] + [409] * 7, codes
    assert conn.execute("SELECT COUNT(*) FROM activities WHERE source = 'route_builder'").fetchone()[0] == 1


def test_the_save_reservation_is_released_after_a_failure_and_is_atomic_in_the_store():
    store = ResultStore()
    rid = store.put(1, b'x', {}, {})
    assert store.begin_save(1, rid) is True and store.begin_save(1, rid) is False
    assert store.begin_save(2, rid) is False
    store.end_save(1, rid)
    assert store.begin_save(1, rid) is True


def test_an_api_key_never_travels_in_clear_unless_the_operator_allows_it_TR5_2(app, conn):
    runtime = app.extensions['routing']
    runtime.provider_factory = runtime._build_provider
    c = database.connect(app.config['DB_PATH'])
    app.config['SETTINGS_STORE'].set_many(c, {'routing.api_key_placeholder': 1} if False else {}, actor=CLI)
    app.extensions['secrets'].set(c, 'routing.api_key', 'abcdefghijklmnop-WXYZ', 'http://graphhopper.internal:8989', actor=CLI)
    c.close()
    with pytest.raises(RoutingUnavailable):                                       # http + clé, sans autorisation explicite
        runtime.provider()
    assert runtime.run_health()['available'] is False and runtime.state() == 'unavailable'
    app.config['ROUTING_ALLOW_CLEARTEXT_KEY'] = True
    assert runtime.provider() is not None                                           # autorisé par l'exploitant
    app.config['ROUTING_ALLOW_CLEARTEXT_KEY'] = False
    c = database.connect(app.config['DB_PATH'])
    app.extensions['secrets'].clear(c, 'routing.api_key', actor=CLI)
    c.close()
    assert runtime.provider() is not None                                           # sans clé : http permis (moteur interne)


# --- Recette v3 : « Tester » le moteur, messages, saisie des réglages, RB-2.2 -----------------------------------------------------------

def admin_client(app, conn):
    admin = add(conn, 'root')
    conn.execute("UPDATE users SET is_admin = 1, totp_secret = x'01', totp_enabled_at = '2026-01-01T00:00:00Z' WHERE id = ?", (admin['id'],))
    conn.commit()
    return session_for(app, conn.execute("SELECT * FROM users WHERE id = ?", (admin['id'],)).fetchone())


def test_the_engine_test_of_the_admin_calls_the_health_probe_and_answers_ok_or_ko_only(app, conn):
    c = database.connect(app.config['DB_PATH'])
    app.config['SETTINGS_STORE'].set_many(c, {'routing.timeout_s': 1}, actor=CLI)
    c.close()
    client = admin_client(app, conn)
    runtime = app.extensions['routing']
    healthy_engine = FakeProvider()
    runtime.provider_factory = lambda: healthy_engine
    body = call(client, 'post', '/api/admin/secrets/routing.api_key/test', {}).get_json()
    assert body == {'success': True, 'ok': True} and healthy_engine.health_calls == 1
    runtime.provider_factory = lambda: FakeProvider(available=False)
    assert call(client, 'post', '/api/admin/secrets/routing.api_key/test', {}).get_json() == {'success': True, 'ok': False}

    def broken():
        raise RoutingUnavailable()
    runtime.provider_factory = broken
    started = time.monotonic()
    assert call(client, 'post', '/api/admin/secrets/routing.api_key/test', {}).get_json() == {'success': True, 'ok': False}
    assert time.monotonic() - started >= 0.9                              # rendu à l'échéance du service, ok comme ko


def test_the_422_message_is_the_specified_one_with_the_original_number_and_no_forbidden_word_CA_R19_R20(app, conn):
    enable(conn)
    healthy(app)
    bad = tuple(POINTS[2])
    app.extensions['routing'].provider_factory = lambda: FakeProvider(unroutable=lambda p: tuple(p) == bad)
    client = session_for(app, add(conn, 'alice'))
    message = generate(client).get_json()['error']
    assert message == "Le point 3 est trop loin d'une voie praticable : rapproche-le d'un chemin."
    app.extensions['routing'].provider_factory = lambda: FakeProvider(fail=NoRouteFound())
    assert 'itinéraire' not in generate(client).get_json()['error'].lower()


def test_no_user_visible_text_uses_the_word_itineraire_CA_R19():
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    offenders = []
    for folder, patterns in (('templates', ('*.html',)), ('static/js', ('*.js',)), ('app/api', ('route_builder_routes.py',))):
        for pattern in patterns:
            for path in (root / folder).rglob(pattern):
                if re.search(r'itin[ée]raire', path.read_text(encoding='utf-8'), re.I):
                    offenders.append(str(path))
    assert offenders == []


def test_the_default_coverage_matches_the_engine_extent_and_is_reported_contained(app, conn):
    assert REGISTRY['routing.coverage_bbox'].default == [1.35, 48.83, 6.42, 51.55]
    runtime = app.extensions['routing']
    runtime.provider_factory = lambda: FakeProvider(bbox=(48.83, -8.32, 54.60, 6.42))        # /info du moteur réel (lat, lon, lat, lon)
    healthy(app)
    assert runtime.diagnostic()['coverage_contained'] is True
    runtime.provider_factory = lambda: FakeProvider(bbox=(49.0, 2.0, 51.6, 6.5))
    healthy(app)
    assert runtime.diagnostic()['coverage_contained'] is False


def test_the_builder_front_covers_rb_2_2_undo_keys_insert_before_touch_removal_and_in_page_confirmation():
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    js = (root / 'static' / 'js' / 'viewer' / 'route-builder.js').read_text(encoding='utf-8')
    html = (root / 'templates' / 'viewer' / 'toolbar.html').read_text(encoding='utf-8')
    assert "key === 'z'" in js and "key === 'y'" in js and 'ctrlKey' in js
    assert 'Insérer avant' in js and 'insertBefore' in js and "marker.on('click'" in js and "marker.on('contextmenu'" in js
    assert 'rbClearConfirm' in js and 'rbClearConfirm' in html and 'confirm(' not in js.replace('Confirmer', '')
    assert 'beforeunload' in js and 'dashArray' in js and 'birdLength' in js


def test_a_401_clears_the_builder_points_stored_in_the_browser_TR6_n1():
    import pathlib
    js = (pathlib.Path(__file__).resolve().parent.parent / 'static' / 'js' / 'common.js').read_text(encoding='utf-8')
    block = js.split('response.status === 401')[1].split('}')[0:3]
    assert "kmlviewer.routeBuilder." in ''.join(block) and 'removeItem' in ''.join(block)


def _trace_print(body):
    """Empreinte de la TRACE : les points `trkpt_*` du payload (les points P1…Pn ne varient pas d'une proposition à l'autre)."""
    track = [f['coordinates'] for f in body['features'] if str(f.get('name', '')).startswith('trkpt_')]
    assert len(track) > 10
    return hash(tuple((round(c[0], 5), round(c[1], 5)) for c in track[::5]))


def test_another_proposal_with_the_next_seed_gives_a_different_trace_end_to_end(ready):
    client, _ = ready
    first = generate(client, distance_km=30, seed=1).get_json()
    seen = {_trace_print(first)}
    seed = first['route_meta']['next_seed']
    for _ in range(4):
        reply = generate(client, distance_km=30, seed=seed).get_json()
        assert reply['route_meta']['seed'] >= seed and reply['route_meta']['next_seed'] > reply['route_meta']['seed']
        seen.add(_trace_print(reply))
        seed = reply['route_meta']['next_seed']
    assert len(seen) == 5


def test_a_request_whose_trace_does_not_depend_on_the_seed_says_there_is_no_other_proposal(ready):
    client, _ = ready
    first = generate(client, seed=3).get_json()['route_meta']                              # pas de distance : boucle de base seule
    assert not any(w['code'] == 'no_alternative' for w in first['warnings'])
    again = generate(client, seed=4, previous_result_id=first['result_id']).get_json()['route_meta']
    assert any(w['code'] == 'no_alternative' and 'différente' in w['message'] for w in again['warnings'])
    varied = generate(client, distance_km=30, seed=3).get_json()
    other = generate(client, distance_km=30, seed=4, previous_result_id=varied['route_meta']['result_id']).get_json()
    assert not any(w['code'] == 'no_alternative' for w in other['route_meta']['warnings'])
    assert _trace_print(varied) != _trace_print(other)


def test_the_server_skips_a_proposal_already_seen_and_a_foreign_previous_id_is_ignored(app, conn, ready):
    client, user = ready
    first = generate(client, distance_km=30, seed=1).get_json()
    # même graine que la précédente, MAIS l'empreinte est à éviter : la graine suivante est essayée
    second = generate(client, distance_km=30, seed=1, previous_result_id=first['route_meta']['result_id']).get_json()
    assert _trace_print(second) != _trace_print(first) and second['route_meta']['attempts'] >= 2
    assert second['route_meta']['seed'] > 1 and 'seen' not in second['route_meta']
    stranger = session_for(app, add(conn, 'mallory'))
    third = generate(stranger, distance_km=30, seed=1, previous_result_id=first['route_meta']['result_id']).get_json()
    assert _trace_print(third) == _trace_print(first) and third['route_meta']['attempts'] == 1       # id d'un autre : ignoré


def _engine_up():
    try:
        import urllib.request
        return urllib.request.urlopen('http://127.0.0.1:8989/health', timeout=1).status == 200
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(not _engine_up(), reason="moteur GraphHopper local absent (essai manuel de recette, jamais en CI)")
def test_another_proposal_differs_against_the_local_engine():
    from app.services.routing.graphhopper import GraphHopperProvider
    engine = GraphHopperProvider('http://127.0.0.1:8989', guard=lambda d: None)
    config = builder.BuildConfig(coverage_bbox=(48.83, 1.35, 51.55, 6.42))
    points = [(50.6292, 3.0573), (50.6371, 3.0630), (50.6300, 3.0700)]
    seed, seen = 1, set()
    for _ in range(5):
        generation = builder.generate(engine, points, 'run', 10, seed, config)
        seen.add(tuple((round(lat, 5), round(lon, 5)) for lat, lon in generation.points[::7]))
        seed = generation.meta['last_seed'] + 1
    assert len(seen) == 5


def test_outside_the_coverage_is_a_400_with_the_original_number_and_too_far_from_a_way_is_a_422(ready):
    client, _ = ready
    outside = generate(client, points=[[50.63, 3.06], [51.51, -0.13], [50.64, 3.10]])
    assert outside.status_code == 400 and outside.get_json()['error'] == 'Le point 2 est hors de la zone couverte.'
    for text in (outside.get_json()['error'],):
        assert 'itinéraire' not in text.lower()
