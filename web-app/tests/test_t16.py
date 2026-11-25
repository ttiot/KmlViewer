"""Réglages EF-6.5 au registre, fonds de carte (R1, CA-41), contrôle d'origine derrière un proxy (T16, T9-1)."""

import json

import pytest
from cryptography.fernet import Fernet

from app import create_app, db as database
from app.audit import Actor
from app.config import TestingConfig
from app.settings import REGISTRY, SettingsError, effective
from tests.conftest import OIDC_OVERRIDES

CLI = Actor.cli()
HOST = 'traces.example.com'
KEY = Fernet.generate_key().decode()


class Prod(TestingConfig):
    TESTING = False
    INBOX_ENABLED = False
    SECRET_KEY = 'k' * 40
    SESSION_COOKIE_SECURE = None


def password_app(tmp_path, **extra):
    return create_app(Prod, overrides={'DATA_DIR': str(tmp_path / 'data'), 'AUTH_MODE': 'password', 'TRUSTED_HOSTS': [HOST],
                                       'SECRETS_KEY': KEY, 'PUBLIC_URL': f'https://{HOST}', **extra})


# --- Le registre couvre EF-6.5 avec ses bornes ----------------------------------------------------------------------------

EXPECTED_BOUNDS = {
    'USER_QUOTA_MB': (0, 102_400), 'audit.retention_days': (30, 3650), 'auth.invite_ttl_hours': (1, 720),
    'INBOX_SCAN_INTERVAL': (5, 3600), 'DEM_BATCH_SIZE': (1, 1000), 'DEM_TIMEOUT': (1, 120), 'DEM_TOTAL_TIMEOUT': (10, 600),
    'DEM_REQUEST_DELAY': (0, 10), 'DEM_BACKFILL_BATCH': (1, 1000), 'DEM_BACKFILL_INTERVAL': (30, 3600), 'map.zoom': (1, 22),
    'routing.timeout_s': (1, 60), 'routing.total_budget_s': (5, 120), 'routing.max_calls': (1, 30), 'routing.max_waypoints': (2, 100),
    'routing.max_distance_km': (1, 1000), 'routing.distance_tolerance_pct': (1, 100), 'routing.snap_tolerance_m': (10, 500),
    'routing.overlap_warning_pct': (1, 100), 'routing.max_concurrent': (1, 4), 'routing.health_interval_s': (15, 600),
    'routing.rate_limit_per_user': (1, 100), 'map.center_lat': (-85, 85), 'map.center_lon': (-180, 180),
}


@pytest.mark.parametrize('key,bounds', EXPECTED_BOUNDS.items())
def test_each_setting_has_the_documented_bounds_and_refuses_values_beyond_them(key, bounds):
    setting = REGISTRY[key]
    assert (setting.min, setting.max) == bounds and bounds[0] <= setting.default <= bounds[1]
    for bad in (bounds[0] - 1, bounds[1] + 1, 10 ** 400, None, 'x'):
        with pytest.raises(ValueError):
            setting.validate(bad)
    for good in bounds:
        assert setting.validate(good) == good


def test_every_ec65_key_is_present_and_enums_and_bools_are_typed():
    for key in ('OIDC_AUTO_ACTIVATE', 'auth.mfa_policy', 'auth.forgot_enabled', 'INBOX_ENABLED', 'INBOX_DEFAULT_USER', 'APP_TIMEZONE',
                'map.basemaps', 'map.default_basemap', 'smtp.host', 'DEM_URL', 'routing.base_url'):
        assert key in REGISTRY
    mfa = REGISTRY['auth.mfa_policy']
    assert mfa.choices == ('admins', 'all') and mfa.default == 'admins'
    for bad in ('none', 'off', '', None, 'ALL', ['all']):
        with pytest.raises(ValueError):
            mfa.validate(bad)                                           # jamais en dessous de « admins »
    assert REGISTRY['OIDC_AUTO_ACTIVATE'].default is False and REGISTRY['auth.forgot_enabled'].default is True


@pytest.mark.parametrize('value,ok', [('Europe/Paris', True), ('UTC', True), ('America/Argentina/Buenos_Aires', True),
                                      ('Europe/Nope', False), ('../etc/passwd', False), ('/etc/localtime', False), ('', False),
                                      ('Europe/Paris\n', False), ('x' * 65, False), (5, False), (None, False)])
def test_timezone_setting(value, ok):
    setting = REGISTRY['APP_TIMEZONE']
    if ok:
        assert setting.validate(value) == value
    else:
        with pytest.raises(ValueError):
            setting.validate(value)


def test_migrated_consumers_read_the_effective_value(app, db):
    store = app.config['SETTINGS_STORE']
    store.set_many(db, {'APP_TIMEZONE': 'Asia/Tokyo', 'DEM_TIMEOUT': 7.5, 'INBOX_DEFAULT_USER': 'bob', 'DEM_BACKFILL_BATCH': 3},
                   actor=CLI)
    for key, expected in (('APP_TIMEZONE', 'Asia/Tokyo'), ('DEM_TIMEOUT', 7.5), ('INBOX_DEFAULT_USER', 'bob'),
                          ('DEM_BACKFILL_BATCH', 3)):
        assert effective(app.config, key) == expected
    assert effective(app.config, 'OIDC_AUTO_ACTIVATE') is True              # TestingConfig (couche « env »)
    assert app.config['SETTINGS_STORE'].source('DEM_TIMEOUT') == 'base'


def test_the_dashboard_follows_the_timezone_setting(app, db):
    app.config['SETTINGS_STORE'].set_many(db, {'APP_TIMEZONE': 'Pacific/Auckland'}, actor=CLI)
    assert effective(app.config, 'APP_TIMEZONE') == 'Pacific/Auckland'


def test_unset_environment_variables_show_up_as_defaults_not_env(app):
    store = app.config['SETTINGS_STORE']
    for key in ('DEM_BATCH_SIZE', 'DEM_TIMEOUT', 'DEM_TOTAL_TIMEOUT', 'DEM_REQUEST_DELAY', 'DEM_BACKFILL_BATCH', 'APP_TIMEZONE'):
        assert store.source(key) == 'défaut', key


def test_invitation_lifetime_follows_the_setting(tmp_path):
    from app.auth import invitations
    app = password_app(tmp_path)
    conn = database.connect(app.config['DB_PATH'])
    conn.execute("INSERT INTO users (sub, username, is_admin, auth_method) VALUES ('pwd:a', 'adm', 1, 'password')")
    conn.commit()
    app.config['SETTINGS_STORE'].set_many(conn, {'auth.invite_ttl_hours': 2}, actor=CLI)
    from tests.test_auth_modes import _enable_totp
    _enable_totp(app, 1)
    client = app.test_client()
    kw = {'base_url': f'https://{HOST}'}
    with client.session_transaction(**kw) as s:
        s['user_id'], s['token'], s['login_at'] = 1, '', 4102444800
    conn.execute("UPDATE users SET session_token = 'tok' WHERE id = 1")
    conn.commit()
    with client.session_transaction(**kw) as s:
        s['token'] = 'tok'
        s['login_at'] = int(__import__('time').time())
    body = client.post('/api/invitations', json={'email': 'a@example.org'}, headers={'Origin': f'https://{HOST}'}, **kw).get_json()
    created = conn.execute("SELECT created_at, expires_at FROM auth_tokens").fetchone()
    from datetime import datetime
    fmt = '%Y-%m-%dT%H:%M:%SZ'
    assert datetime.strptime(created[1], fmt) - datetime.strptime(created[0], fmt) == __import__('datetime').timedelta(hours=2)
    assert body['expires_at'] == created[1] and invitations is not None


# --- Fonds de carte (R1, CA-41) -----------------------------------------------------------------------------------------

def basemap(name='fond', url='https://tuiles.example.org/{z}/{x}/{y}.png', **extra):
    return {'name': name, 'url': url, **extra}


@pytest.fixture
def tiles_app(tmp_path):
    return create_app(TestingConfig, overrides={'DATA_DIR': str(tmp_path / 'd'),
                                                'OUTBOUND_ALLOWED_HOSTS': 'tiles=tuiles.example.org,tiles=a.t.example.org,tiles=b.t.example.org'})


def test_the_default_basemaps_are_the_five_embedded_ones(app):
    names = [b['name'] for b in effective(app.config, 'map.basemaps')]
    assert names == ['osm', 'satellite', 'terrain', 'cartodb', 'toner']
    config = app.test_client().get('/api/map-config').get_json()
    assert [b['name'] for b in config['basemaps']] == names and config['default'] == 'osm'     # CA-41 : comme avant
    assert config['center'] == [46.603354, 1.888334] and config['zoom'] == 6


@pytest.mark.parametrize('value', [
    [basemap(url='http://tuiles.example.org/{z}/{x}/{y}.png')], [basemap(url='https://tuiles.example.org/{z}/{x}.png')],
    [basemap(url='https://u:p@tuiles.example.org/{z}/{x}/{y}.png')], [basemap(url='https://tuiles.example.org:8443/{z}/{x}/{y}.png')],
    [basemap(url='https://tuiles.example.org/' + 'x' * 500 + '/{z}/{x}/{y}.png')], [basemap(name='')], [basemap(name='x' * 41)],
    [basemap(), basemap(name='FOND')], [basemap(max_zoom=0)], [basemap(max_zoom=23)], [basemap(max_zoom=True)],
    [basemap(subdomains='')], [basemap(subdomains='abcde')], [basemap(subdomains='a1')], [basemap(attribution='x' * 201)],
    [basemap(extra='x')], [{'name': 'sans-url'}], ['pas-un-dict'], 'pas-une-liste', [basemap(name=f'f{i}') for i in range(11)], None,
])
def test_invalid_basemaps_are_refused_by_the_registry(value):
    with pytest.raises(ValueError):
        REGISTRY['map.basemaps'].validate(value)


@pytest.mark.parametrize('field', ['name', 'attribution'])
@pytest.mark.parametrize('text', ['<a href="https://x.example">Se reconnecter</a>', 'a<b', 'a>b', 'a&amp;b', 'a"b', "a'b",
                                  '<img src=x onerror=alert(1)>'])
def test_basemap_name_and_attribution_are_plain_text_T16_1(field, text):
    """Leaflet insère l'attribution en innerHTML : aucun balisage accepté (T16-1). Le rendu côté front en textContent = T15."""
    with pytest.raises(ValueError):
        REGISTRY['map.basemaps'].validate([basemap(**{field: text})])


def test_the_embedded_basemaps_contain_no_markup():
    for item in REGISTRY['map.basemaps'].default:
        assert not any(c in item['name'] + item['attribution'] for c in '<>&"\'')
        REGISTRY['map.basemaps'].validate([item])


def test_a_basemap_with_a_host_outside_tiles_is_refused_on_write_and_nothing_is_written(tiles_app):
    """CA-41."""
    conn = database.connect(tiles_app.config['DB_PATH'])
    store = tiles_app.config['SETTINGS_STORE']
    for bad in ([basemap(url='https://attaquant.example/{z}/{x}/{y}.png')],
                [basemap(url='https://{s}.t.example.org/{z}/{x}/{y}.png', subdomains='abc')]):        # c.t.example.org hors liste
        with pytest.raises(SettingsError) as exc:
            store.set_many(conn, {'map.basemaps': bad, 'map.default_basemap': 'fond'}, actor=CLI)
        assert exc.value.field == 'map.basemaps' and 'tiles=' in exc.value.message
    assert conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 0
    store.set_many(conn, {'map.basemaps': [basemap(url='https://{s}.t.example.org/{z}/{x}/{y}.png', subdomains='ab')],
                          'map.default_basemap': 'fond'}, actor=CLI)
    assert [b['name'] for b in effective(tiles_app.config, 'map.basemaps')] == ['fond']
    conn.close()


def test_a_default_basemap_outside_the_list_is_no_longer_refused_by_the_server(tiles_app):
    """EF-6.12 (refonte Réglages) : le fond par défaut inexistant est contrôlé par la page d'admin ; le serveur l'accepte
    (le bandeau d'usage `default_basemap_alert` et le repli de /api/map-config s'appliquent)."""
    conn = database.connect(tiles_app.config['DB_PATH'])
    store = tiles_app.config['SETTINGS_STORE']
    store.set_many(conn, {'map.basemaps': [basemap()], 'map.default_basemap': 'osm'}, actor=CLI)
    assert store.get('map.default_basemap') == 'osm'
    store.set_many(conn, {'map.default_basemap': 'fond'}, actor=CLI)
    assert store.get('map.default_basemap') == 'fond'
    conn.close()


def test_a_basemap_forced_in_the_database_outside_the_list_is_not_sent_to_the_browser_and_is_audited(tiles_app):
    conn = database.connect(tiles_app.config['DB_PATH'])
    forced = [basemap('ok'), basemap('evil', url='https://attaquant.example/{z}/{x}/{y}.png')]
    conn.execute("INSERT INTO settings (key, value, updated_at, updated_by) VALUES ('map.basemaps', ?, 'x', 'sql'), "
                 "('map.default_basemap', '\"evil\"', 'x', 'sql')", (json.dumps(forced),))
    conn.execute("UPDATE settings_meta SET version = version + 1")
    conn.commit()
    tiles_app.config['SETTINGS_STORE'].refresh(conn, force=True)
    config = tiles_app.test_client().get('/api/map-config').get_json()
    assert [b['name'] for b in config['basemaps']] == ['ok'] and config['default'] == 'ok'      # le fond par défaut retombe sur un fond permis
    assert 'attaquant' not in json.dumps(config)
    lines = [dict(r) for r in conn.execute("SELECT * FROM audit_log WHERE action = 'outbound.refused'")]
    assert len(lines) == 1 and lines[0]['target_id'] == 'tiles' and 'attaquant.example' in lines[0]['details']
    tiles_app.test_client().get('/api/map-config')
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'outbound.refused'").fetchone()[0] == 1   # dédoublonné
    conn.close()


def test_the_csp_matches_the_basemaps_the_browser_receives(tiles_app):
    response = tiles_app.test_client().get('/api/map-config')
    csp = response.headers['Content-Security-Policy']
    for basemap_ in response.get_json()['basemaps']:
        assert basemap_ is not None
    assert 'https://tuiles.example.org' in csp and 'https://a.t.example.org' in csp and 'https:;' not in csp


def test_map_settings_are_bounded(tiles_app):
    conn = database.connect(tiles_app.config['DB_PATH'])
    store = tiles_app.config['SETTINGS_STORE']
    for changes in ({'map.zoom': 0}, {'map.zoom': 23}, {'map.center_lat': 86}, {'map.center_lon': 181}, {'map.center_lat': float('nan')}):
        with pytest.raises(SettingsError):
            store.set_many(conn, changes, actor=CLI)
    store.set_many(conn, {'map.zoom': 12, 'map.center_lat': 50.63, 'map.center_lon': 3.06}, actor=CLI)
    assert tiles_app.test_client().get('/api/map-config').get_json()['zoom'] == 12
    conn.close()


def test_map_config_needs_a_session_outside_local_mode(tmp_path):
    app = create_app(TestingConfig, overrides={**OIDC_OVERRIDES, 'DATA_DIR': str(tmp_path / 'o'), 'TRUSTED_HOSTS': [HOST]})
    assert app.test_client().get('/api/map-config', base_url=f'https://{HOST}').status_code == 401


# --- Contrôle d'origine derrière un proxy TLS (T9-1) -----------------------------------------------------------------------

def origin_status(app, origin, base_url, path='/auth/token/inspect'):
    client = app.test_client()
    html = client.get('/auth/token', base_url=base_url).get_data(as_text=True)
    import re
    csrf_token = re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)
    headers = {'X-CSRF-Token': csrf_token}
    if origin:
        headers['Origin'] = origin
    return client.post(path, json={'token': 'A' * 43}, headers=headers, base_url=base_url).status_code


def test_behind_a_tls_proxy_without_trust_proxy_the_browser_origin_is_accepted(tmp_path):
    """T9-1 : l'application voit « http », le navigateur envoie « https » : ne PAS refuser tous les POST."""
    app = password_app(tmp_path)                                         # TRUST_PROXY = 0 (défaut)
    seen_http = f'http://{HOST}'
    assert origin_status(app, f'https://{HOST}', seen_http) == 404                          # accepté (jeton inconnu => 404, pas 403)
    assert origin_status(app, f'http://{HOST}', seen_http) == 403                           # schéma différent de PUBLIC_URL
    assert origin_status(app, 'https://evil.example', seen_http) == 403
    assert origin_status(app, f'https://{HOST}:8443', seen_http) == 403                     # autre port
    assert origin_status(app, f'https://evil@{HOST}', seen_http) == 403                     # identifiants dans l'origine
    assert origin_status(app, 'null', seen_http) == 403


def test_with_trust_proxy_the_same_rule_holds(tmp_path):
    app = password_app(tmp_path, TRUST_PROXY=1)
    client = app.test_client()
    import re
    html = client.get('/auth/token', base_url=f'https://{HOST}', headers={'X-Forwarded-Proto': 'https'}).get_data(as_text=True)
    csrf_token = re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)
    ok = client.post('/auth/token/inspect', json={'token': 'A' * 43}, base_url=f'http://{HOST}',
                     headers={'X-CSRF-Token': csrf_token, 'Origin': f'https://{HOST}', 'X-Forwarded-Proto': 'https',
                              'X-Forwarded-For': '203.0.113.5'})
    assert ok.status_code == 404
    bad = client.post('/auth/token/inspect', json={'token': 'A' * 43}, base_url=f'http://{HOST}',
                      headers={'X-CSRF-Token': csrf_token, 'Origin': 'https://evil.example', 'X-Forwarded-Proto': 'https'})
    assert bad.status_code == 403


def test_the_origin_is_compared_to_public_url_default_ports_and_case(tmp_path):
    app = password_app(tmp_path, PUBLIC_URL=f'https://{HOST}:443')
    assert origin_status(app, f'https://{HOST.upper()}', f'http://{HOST}') == 404
    assert origin_status(app, f'https://{HOST}:443', f'http://{HOST}') == 404


def test_a_request_without_origin_keeps_the_sec_fetch_site_rule(tmp_path):
    app = password_app(tmp_path)
    assert origin_status(app, None, f'http://{HOST}') == 404


def test_without_public_url_the_previous_behaviour_stays_and_the_scheme_is_not_compared(tmp_path):
    local = create_app(TestingConfig, overrides={'DATA_DIR': str(tmp_path / 'l'), 'AUTH_MODE': 'local'})
    client = local.test_client()
    for origin, expected in (('https://localhost', 200), ('http://localhost', 200), ('https://evil.example', 403),
                             ('http://localhost:1234', 403)):
        response = client.post('/api/export/csv', json={'features': []}, headers={'Origin': origin}, base_url='http://localhost')
        assert (response.status_code == 403) == (expected == 403), origin
