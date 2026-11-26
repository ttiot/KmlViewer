"""
T9-1 et T16 en profondeur (rôle test), écrit depuis la spec (admin-space EF-6.5, EF-6.8, EF-S7, CA-21c, CA-41), pas depuis le code.

A. Le registre : un tableau INDÉPENDANT (tiré d'EF-6.5 et des specs voisines) décrit chaque réglage ; toute entrée ajoutée au registre
   sans ligne ici fait échouer le test (revue obligatoire, EF-6.6) ; bornes, types, valeurs hostiles, env, écriture tout-ou-rien.
B. Fonds de carte : limites exactes, URL hostiles contre la liste `tiles=`, champs mal typés, HTML (T16-1).
C. /api/map-config : valeurs forcées en base, JSON strict, CSP exactement alignée sur ce que reçoit le navigateur.
D. Contrôle d'origine contre PUBLIC_URL (T9-1) : matrice de variantes, méthodes, proxy TLS avec et sans TRUST_PROXY.
E. Les consommateurs suivent la valeur effective (DEM_*, quota, lot de rattrapage).
"""

import json
import math
import re

import pytest
from cryptography.fernet import Fernet

from app import create_app, db as database, outbound
from app.audit import Actor
from app.config import TestingConfig
from app.settings import REGISTRY, SettingsError, effective
from tests.conftest import OIDC_OVERRIDES

CLI = Actor.cli()
HOST = 'traces.example.com'
KEY = Fernet.generate_key().decode()
TILES = 'tiles=tuiles.example.org,tiles=a.t.example.org,tiles=b.t.example.org'


class Prod(TestingConfig):
    TESTING = False
    INBOX_ENABLED = False
    SECRET_KEY = 'k' * 40
    SESSION_COOKIE_SECURE = None


def password_app(tmp_path, **extra):
    return create_app(Prod, overrides={'DATA_DIR': str(tmp_path / 'data'), 'AUTH_MODE': 'password', 'TRUSTED_HOSTS': [HOST],
                                       'SECRETS_KEY': KEY, 'PUBLIC_URL': f'https://{HOST}', **extra})


# ===== A. Le registre, contre un tableau indépendant ============================================================================

# clé -> (type, bas, haut, défaut) ; tiré d'EF-6.5, de gpx-route-builder §5.6 et d'EF-4.1 (smtp), pas du registre.
INT, FLOAT = 'int', 'float'
SPEC = {
    'USER_QUOTA_MB': (INT, 0, 102_400, 2048), 'audit.retention_days': (INT, 30, 3650, 90), 'auth.invite_ttl_hours': (INT, 1, 720, 168),
    'INBOX_SCAN_INTERVAL': (INT, 5, 3600, 30), 'DEM_BATCH_SIZE': (INT, 1, 1000, 100), 'DEM_TIMEOUT': (FLOAT, 1, 120, 15.0),
    'DEM_TOTAL_TIMEOUT': (FLOAT, 10, 600, 90.0), 'DEM_REQUEST_DELAY': (FLOAT, 0, 10, 0.0), 'DEM_BACKFILL_BATCH': (INT, 1, 1000, 25),
    'DEM_BACKFILL_INTERVAL': (INT, 30, 3600, 300), 'map.zoom': (INT, 1, 22, 6), 'map.center_lat': (FLOAT, -85, 85, 46.603354),
    'map.center_lon': (FLOAT, -180, 180, 1.888334),
    'ratelimit.password_per_identifier': (INT, 3, 20, 5), 'ratelimit.password_per_ip': (INT, 10, 100, 20),
    'ratelimit.mfa_per_account': (INT, 3, 10, 5), 'ratelimit.forgot_per_identifier': (INT, 1, 10, 3),
    'ratelimit.forgot_per_ip': (INT, 10, 100, 10), 'ratelimit.token_per_ip': (INT, 5, 30, 10),
    'routing.timeout_s': (FLOAT, 1, 60, 20.0), 'routing.total_budget_s': (FLOAT, 5, 120, 30.0), 'routing.max_calls': (INT, 1, 30, 15),
    'routing.max_waypoints': (INT, 2, 100, 50), 'routing.max_distance_km': (INT, 1, 1000, 300),
    'routing.distance_tolerance_pct': (INT, 1, 100, 15), 'routing.snap_tolerance_m': (INT, 10, 500, 50),
    'routing.overlap_warning_pct': (INT, 1, 100, 25), 'routing.max_concurrent': (INT, 1, 4, 2),
    'routing.health_interval_s': (INT, 15, 600, 60), 'routing.rate_limit_per_user': (INT, 1, 100, 30),
    'routing.overlap_warning_pct_2pts': (INT, 1, 100, 40), 'routing.seed_retries': (INT, 0, 2, 2),
    'smtp.port': (INT, 1, 65535, 587), 'smtp.timeout_s': (INT, 1, 60, 10),
    'routing.zone_margin_km': (INT, 0, 50, 10),          # lot Z2, admin-space EF-6.22
}
OTHER = {                      # clé -> (type, défaut)
    'OIDC_AUTO_ACTIVATE': ('bool', False), 'auth.mfa_policy': ('enum', 'admins'), 'auth.forgot_enabled': ('bool', True),
    'INBOX_ENABLED': ('bool', True), 'INBOX_DEFAULT_USER': ('str', ''), 'APP_TIMEZONE': ('timezone', 'Europe/Paris'),
    'map.basemaps': ('basemaps', None), 'map.default_basemap': ('str', 'osm'), 'smtp.host': ('str', ''),
    'smtp.security': ('enum', 'starttls'), 'smtp.username': ('str', ''), 'smtp.sender': ('email', ''), 'DEM_URL': ('url', ''),
    'routing.base_url': ('url', ''), 'routing.detour_strategy': ('enum', 'hybrid'),
    'routing.coverage_polygon': ('polygon', [[1.35, 48.83], [6.42, 48.83], [6.42, 51.55], [1.35, 51.55]]),   # seul réglage de zone (lot Z1, EF-6.20)
}
NUMERIC = list(SPEC)


def test_the_registry_and_the_independent_spec_table_list_exactly_the_same_settings():
    """Une entrée ajoutée au registre sans ligne ici (donc sans revue des bornes, EF-6.6) ou retirée fait échouer."""
    assert set(REGISTRY) == set(SPEC) | set(OTHER)


@pytest.mark.parametrize('key', NUMERIC)
def test_numeric_setting_matches_the_spec_table(key):
    kind, low, high, default = SPEC[key]
    s = REGISTRY[key]
    assert s.type == kind and (s.min, s.max) == (low, high) and s.default == default, key
    assert s.validate(low) == low and s.validate(high) == high and s.validate(default) == default
    for bad in (low - 1, high + 1, low - 10 ** 6, high + 10 ** 6, 10 ** 400, -10 ** 400, 2 ** 63, -2 ** 63, 2 ** 31):
        with pytest.raises(ValueError):
            s.validate(bad)
    hostile = [None, True, False, '5', ' 5', '5.0', '', b'5', [], [5], {}, {'a': 1}, float('nan'), float('inf'), -float('inf'), 1e308,
               -1e308, object()]
    if kind == INT:
        hostile += [low + 0.5, float(low), 1e30]
    for bad in hostile:
        with pytest.raises(ValueError):
            s.validate(bad)


@pytest.mark.parametrize('key', [k for k in NUMERIC if SPEC[k][0] == FLOAT])
def test_float_settings_accept_integers_and_reject_every_non_finite_form(key):
    _, low, high, _ = SPEC[key]
    s = REGISTRY[key]
    assert s.validate(int(low)) == float(low) and isinstance(s.validate(int(high)), float)
    for bad in (math.nan, math.inf, -math.inf, 10 ** 400, float(high) * 1.0000001, float(low) - 1e-9):
        with pytest.raises(ValueError):
            s.validate(bad)


@pytest.mark.parametrize('key', ['OIDC_AUTO_ACTIVATE', 'auth.forgot_enabled', 'INBOX_ENABLED'])
def test_boolean_settings_are_strict_json_booleans(key):
    s = REGISTRY[key]
    assert (s.validate(True), s.validate(False)) == (True, False)
    for bad in (1, 0, 'true', 'false', 'yes', 'on', '', None, [], [True], {}, 1.0, 'True', 'TRUE'):
        with pytest.raises(ValueError):
            s.validate(bad)


@pytest.mark.parametrize('key,choices', [('auth.mfa_policy', ('admins', 'all')), ('smtp.security', ('starttls', 'tls', 'none')),
                                      ('routing.detour_strategy', ('s1', 's2', 'hybrid'))])
def test_enum_settings_accept_exactly_their_choices(key, choices):
    s = REGISTRY[key]
    assert tuple(s.choices) == choices
    for good in choices:
        assert s.validate(good) == good
    for bad in ('', ' all', 'all ', 'ALL', 'Admins', 'x', None, 1, True, ['all'], {'all': 1}, 'admins\n', 'admins\x00', 'all,admins'):
        with pytest.raises((ValueError, TypeError)):
            s.validate(bad)


@pytest.mark.parametrize('key', ['INBOX_DEFAULT_USER', 'smtp.host', 'smtp.username', 'map.default_basemap'])
def test_text_settings_refuse_control_characters_and_oversize_values(key):
    s = REGISTRY[key]
    for bad in ('a\nb', 'a\rb', 'a\x00b', 'a\tb', 'a\x7fb', 'a‮b', 'a b', 'x' * 1000, 5, None, ['a'], b'a', {'a': 1}):
        with pytest.raises(ValueError):
            s.validate(bad)
    assert s.validate('') == ''
    assert s.validate('x' * s.max_length) == 'x' * s.max_length
    with pytest.raises(ValueError):
        s.validate('x' * (s.max_length + 1))


@pytest.mark.parametrize('key', ['DEM_URL', 'routing.base_url'])
@pytest.mark.parametrize('bad', [
    'ftp://a:21', 'file:///etc/passwd', 'javascript:alert(1)', 'data:text/plain,x', '//a:5000', 'http://', 'http:///x', 'a:5000',
    'http://u:p@a:5000', 'http://:p@a:5000', 'http://u@a:5000', 'http://a:5000#frag', 'http://a b:5000',
    'http://a:5000\n', 'http://a:5000\r\nHost: evil', ' http://a:5000', 'http://a:5000 ', 'http://a:99999', 'http://a:-1', 'http://a:x',
    'http://a:5000/' + 'x' * 600, 5, None, ['http://a'], 'http://[::1', 'http://a\x00b:5000', 'http://a‮:5000',
])
def test_service_urls_refuse_hostile_forms(key, bad):
    with pytest.raises(ValueError):
        REGISTRY[key].validate(bad)


@pytest.mark.parametrize('key', ['DEM_URL', 'routing.base_url'])
def test_service_urls_accept_the_documented_forms_and_drop_the_trailing_slash(key):
    s = REGISTRY[key]
    assert s.validate('') == ''
    for good in ('http://opentopodata:5000', 'https://dem.example.org', 'http://[::1]:5000', 'http://127.0.0.1:8989/path'):
        assert s.validate(good) == good
    assert s.validate('http://opentopodata:5000/') == 'http://opentopodata:5000'


@pytest.mark.parametrize('bad', ['a@@b.co', 'a b@c.co', '<a@b.co>', 'a@b.co,c@d.co', 'a@b.co;c@d.co', '"a"@b.co', 'a@b.co\r\nBcc: x@y.z',
                                 'a@b.co\n', '‮a@b.co', 'x' * 300 + '@b.co', 5, None, ['a@b.co'], 'a@b..co', '@b.co', 'a@', '@', 'a\\@b.co',
                                 'a@b.co ', ' a@b.co'])
def test_email_setting_refuses_lists_headers_and_malformed_addresses(bad):
    with pytest.raises(ValueError):
        REGISTRY['smtp.sender'].validate(bad)


@pytest.mark.parametrize('good,ok', [
    ('Europe/Paris', True), ('UTC', True), ('Etc/GMT+5', True), ('America/Argentina/Buenos_Aires', True), ('Asia/Kolkata', True),
    ('europe/paris', False), ('Europe/Paris ', False), (' Europe/Paris', False), ('Europe//Paris', False), ('/Europe/Paris', False),
    ('Europe/Paris/', False), ('../Europe/Paris', False), ('Europe/../Europe/Paris', False), ('Europe\\Paris', False), ('~/x', False),
    ('Europe/Paris\x00', False), ('Europe/Páris', False), ('Europe/Париж', False), ('a/b/c/d', False), ('UTC+1', False),
    ('zone.tab', False), ('tzdata.zi', False), ('iso3166.tab', False), ('', False), ('.', False), ('..', False), (None, False), (5, False),
    (['UTC'], False), ('x' * 65, False),
])
def test_timezone_setting(good, ok):
    s = REGISTRY['APP_TIMEZONE']
    if ok:
        assert s.validate(good) == good
    else:
        with pytest.raises(ValueError):
            s.validate(good)


@pytest.mark.xfail(strict=True, reason="EF-6.5 : « liste des fuseaux connus » ; 'localtime' (lien vers /etc/localtime), 'Factory' et "
                                       "'posixrules' ne sont pas des fuseaux géographiques mais passent (à arbitrer, mineur)")
@pytest.mark.parametrize('name', ['localtime', 'Factory', 'posixrules'])
def test_timezone_setting_only_takes_geographic_zones(name):
    with pytest.raises(ValueError):
        REGISTRY['APP_TIMEZONE'].validate(name)


def test_the_effective_timezone_is_always_a_usable_zone():
    import zoneinfo
    for zone in ('Europe/Paris', 'Pacific/Auckland', 'Etc/GMT-14', 'UTC'):
        zoneinfo.ZoneInfo(REGISTRY['APP_TIMEZONE'].validate(zone))


@pytest.fixture
def conn_app(tmp_path):
    app = create_app(TestingConfig, overrides={'DATA_DIR': str(tmp_path / 'd'), 'OUTBOUND_ALLOWED_HOSTS': TILES})
    c = database.connect(app.config['DB_PATH'])
    yield app, c
    c.close()


@pytest.mark.parametrize('key', NUMERIC)
def test_a_value_beyond_the_bounds_is_refused_by_the_store_and_nothing_is_written_or_audited(conn_app, key):
    app, conn = conn_app
    _, low, high, _ = SPEC[key]
    store = app.config['SETTINGS_STORE']
    before = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    for bad in (low - 1, high + 1, 10 ** 400):
        with pytest.raises(SettingsError) as exc:
            store.set_many(conn, {key: bad}, actor=CLI)
        assert exc.value.field == key
    assert conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == before
    store.set_many(conn, {key: high}, actor=CLI)
    assert effective(app.config, key) == high


def test_one_invalid_value_in_a_batch_of_every_ec65_setting_writes_nothing(conn_app):
    app, conn = conn_app
    store = app.config['SETTINGS_STORE']
    batch = {k: SPEC[k][2] for k in NUMERIC if k.startswith(('DEM_', 'map.', 'USER_', 'audit.', 'auth.', 'INBOX_'))}
    batch['APP_TIMEZONE'] = 'Pacific/Auckland'
    batch['map.zoom'] = 23                                             # le seul fautif
    with pytest.raises(SettingsError) as exc:
        store.set_many(conn, batch, actor=CLI)
    assert exc.value.field == 'map.zoom'
    assert conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 0
    assert effective(app.config, 'APP_TIMEZONE') == 'Europe/Paris'


def test_the_database_wins_over_the_environment_for_each_dem_setting(tmp_path):
    app = create_app(TestingConfig, overrides={'DATA_DIR': str(tmp_path / 'd'), 'DEM_BATCH_SIZE': '7', 'DEM_TIMEOUT': '3',
                                               'DEM_TOTAL_TIMEOUT': '40', 'DEM_REQUEST_DELAY': '0.5', 'DEM_BACKFILL_BATCH': '4'})
    cfg = app.config
    assert [effective(cfg, k) for k in ('DEM_BATCH_SIZE', 'DEM_TIMEOUT', 'DEM_TOTAL_TIMEOUT', 'DEM_REQUEST_DELAY', 'DEM_BACKFILL_BATCH')] \
        == [7, 3.0, 40.0, 0.5, 4]
    conn = database.connect(cfg['DB_PATH'])
    cfg['SETTINGS_STORE'].set_many(conn, {'DEM_BATCH_SIZE': 11, 'DEM_TIMEOUT': 9, 'DEM_TOTAL_TIMEOUT': 99, 'DEM_REQUEST_DELAY': 2,
                                          'DEM_BACKFILL_BATCH': 12}, actor=CLI)
    assert [effective(cfg, k) for k in ('DEM_BATCH_SIZE', 'DEM_TIMEOUT', 'DEM_TOTAL_TIMEOUT', 'DEM_REQUEST_DELAY', 'DEM_BACKFILL_BATCH')] \
        == [11, 9.0, 99.0, 2.0, 12]
    conn.close()


@pytest.mark.parametrize('key,raw', [('DEM_BATCH_SIZE', '5000'), ('DEM_TIMEOUT', 'nan'), ('DEM_TOTAL_TIMEOUT', '1'), ('DEM_REQUEST_DELAY', '-1'),
                                     ('DEM_BACKFILL_BATCH', '0'), ('APP_TIMEZONE', 'Mars/Phobos'), ('DEM_URL', 'ftp://x'),
                                     ('INBOX_ENABLED', 'peut-être'), ('USER_QUOTA_MB', '1e3'), ('APP_TIMEZONE', '../../etc/passwd')])
def test_an_invalid_environment_value_falls_back_to_the_default_and_never_stops_the_app(tmp_path, key, raw):
    env = REGISTRY[key].env
    app = create_app(TestingConfig, overrides={'DATA_DIR': str(tmp_path / 'd'), env: raw})
    assert effective(app.config, key) == REGISTRY[key].default


# ===== B. Fonds de carte ========================================================================================================

def basemap(name='fond', url='https://tuiles.example.org/{z}/{x}/{y}.png', **extra):
    return {'name': name, 'url': url, **extra}


def write_maps(conn, store, maps, default=None):
    changes = {'map.basemaps': maps}
    if default is not None:
        changes['map.default_basemap'] = default
    return store.set_many(conn, changes, actor=CLI)


def test_exact_limits_of_a_basemap_are_accepted(conn_app):
    app, conn = conn_app
    url = 'https://tuiles.example.org/' + 'x' * (500 - len('https://tuiles.example.org/') - len('/{z}/{x}/{y}')) + '/{z}/{x}/{y}'
    assert len(url) == 500
    maps = [basemap(name='n' * 40, url=url, attribution='a' * 200, max_zoom=22, subdomains='abcd')] + \
           [basemap(name=f'f{i}') for i in range(9)]
    assert len(maps) == 10
    write_maps(conn, app.config['SETTINGS_STORE'], maps, 'n' * 40)
    assert len(effective(app.config, 'map.basemaps')) == 10
    s = REGISTRY['map.basemaps']
    assert s.validate([basemap(max_zoom=1)])[0]['max_zoom'] == 1
    assert s.validate([basemap()])[0]['subdomains'] == 'abc' and s.validate([basemap()])[0]['max_zoom'] == 19    # défauts de la spec
    assert s.validate([basemap(url='https://tuiles.example.org/{z}/{x}/{y}{r}.png')])


@pytest.mark.parametrize('value', [
    [basemap(name='   ')], [basemap(name='a\nb')], [basemap(name='a\x00b')], [basemap(name='‮')], [basemap(name=5)],
    [basemap(name=['a'])], [basemap(name=None)], [basemap(url=5)], [basemap(url=None)], [basemap(url=['https://x/{z}/{x}/{y}'])],
    [basemap(attribution=5)], [basemap(attribution=None)], [basemap(attribution='a\x00b')], [basemap(subdomains=5)],
    [basemap(subdomains=['a'])], [basemap(subdomains=None)], [basemap(subdomains='ABC')], [basemap(subdomains='aab')],
    [basemap(subdomains='é')], [basemap(subdomains='a b')], [basemap(subdomains='a.b')], [basemap(subdomains='a-b')],
    [basemap(max_zoom=True)], [basemap(max_zoom=False)], [basemap(max_zoom=5.0)], [basemap(max_zoom='5')], [basemap(max_zoom=None)], [basemap(max_zoom=-1)], [basemap(max_zoom=10 ** 400)],
    [basemap(max_zoom=float('nan'))], [basemap(), basemap(name='fond')], [basemap(name='Fond'), basemap(name='FOND')],
    [basemap(name=' fond'), basemap(name='fond ')],                             # le nom est rogné : doublon
    [basemap(name='STRASSE'), basemap(name='straße')],                          # casefold
    {'name': 'x', 'url': 'https://t/{z}/{x}/{y}'}, 'texte', 5, True, [[]], [None], [()], [basemap(url=None, name='z')],
    [dict(basemap(), **{'__proto__': 1})], [dict(basemap(), URL='x')], [dict(basemap(), Name='y')],
])
def test_more_invalid_basemaps_are_refused(value):
    with pytest.raises((ValueError, TypeError)):
        REGISTRY['map.basemaps'].validate(value)


HOSTILE_URLS = [
    'http://tuiles.example.org/{z}/{x}/{y}.png', 'HTTP://tuiles.example.org/{z}/{x}/{y}.png', 'ftp://tuiles.example.org/{z}/{x}/{y}.png',
    'https://tuiles.example.org:8443/{z}/{x}/{y}.png', 'https://tuiles.example.org:80/{z}/{x}/{y}.png',
    'https://tuiles.example.org:0/{z}/{x}/{y}.png',
    'https://u@tuiles.example.org/{z}/{x}/{y}.png', 'https://:p@tuiles.example.org/{z}/{x}/{y}.png',
    'https://tuiles.example.org@attaquant.example/{z}/{x}/{y}.png', 'https://attaquant.example\\@tuiles.example.org/{z}/{x}/{y}.png',
    'https://attaquant.example#@tuiles.example.org/{z}/{x}/{y}.png', 'https://attaquant.example?@tuiles.example.org/{z}/{x}/{y}.png',
    'https://attaquant.example/@tuiles.example.org/{z}/{x}/{y}.png', 'https://attaquant.example/tuiles.example.org/{z}/{x}/{y}.png',
    'https://tuiles.example.org.attaquant.example/{z}/{x}/{y}.png', 'https://attaquant-tuiles.example.org/{z}/{x}/{y}.png',
    'https://xtuiles.example.org/{z}/{x}/{y}.png', 'https://tuiles.example.org.evil/{z}/{x}/{y}.png',
    'https://sub.tuiles.example.org/{z}/{x}/{y}.png', 'https://example.org/{z}/{x}/{y}.png', 'https://127.0.0.1/{z}/{x}/{y}.png',
    'https://[::1]/{z}/{x}/{y}.png', 'https://localhost/{z}/{x}/{y}.png', 'https://2130706433/{z}/{x}/{y}.png',
    'https://0x7f000001/{z}/{x}/{y}.png', 'https://tuiles.exаmple.org/{z}/{x}/{y}.png',                    # « а » cyrillique
    'https://tuiles。example。org/{z}/{x}/{y}.png', 'https://tuiles.example.org%2f.attaquant.example/{z}/{x}/{y}.png',
    'https://tuiles.example.org%00.attaquant.example/{z}/{x}/{y}.png', 'https://tuiles.example.org\n.attaquant.example/{z}/{x}/{y}.png',
    'https://tuiles.example.org /{z}/{x}/{y}.png', 'https:tuiles.example.org/{z}/{x}/{y}.png', 'https:///tuiles.example.org/{z}/{x}/{y}.png',
    'https:/tuiles.example.org/{z}/{x}/{y}.png', '//tuiles.example.org/{z}/{x}/{y}.png', 'tuiles.example.org/{z}/{x}/{y}.png',
    'https://{z}.example.org/{x}/{y}', 'https://{x}/{z}/{y}', 'https://{y}.tuiles.example.org/{z}/{x}/{y}',
    'https://{s}{s}.t.example.org/{z}/{x}/{y}', 'https://{s}.{s}.t.example.org/{z}/{x}/{y}', 'https://{r}.t.example.org/{z}/{x}/{y}',
    'https://tuiles.example.org/{z}/{x}', 'https://tuiles.example.org/{x}/{y}', 'https://tuiles.example.org/{z}/{y}',
    'https://tuiles.example.org/', 'https://tuiles.example.org', 'https://tuiles.example.org/{Z}/{X}/{Y}.png',
    'javascript:alert(1)//{z}/{x}/{y}', 'data:text/html,{z}{x}{y}',
    'https://tuiles.example.org/{z}/{x}/{y}\n', 'https://tuiles.example.org/{z}/{x}/{y} ', ' https://tuiles.example.org/{z}/{x}/{y}',
    'https://{s}.t.example.org/{z}/{x}/{y}.png',         # sans sous-domaines explicites : abc par défaut, « c » est hors liste
]


@pytest.mark.parametrize('url', HOSTILE_URLS)
def test_a_hostile_basemap_url_is_refused_on_write_and_nothing_is_written(conn_app, url):
    """EF-6.5 / EF-6.8 / CA-41 : chaque hôte effectif dans `tiles=`, schéma https, port 443, sans identifiants."""
    app, conn = conn_app
    store = app.config['SETTINGS_STORE']
    with pytest.raises((SettingsError, ValueError)):
        write_maps(conn, store, [basemap(url=url)], 'fond')
    assert conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 0
    assert 'attaquant' not in json.dumps(effective(app.config, 'map.basemaps'))


@pytest.mark.parametrize('url,subdomains', [
    ('https://tuiles.example.org/{z}/{x}/{y}.png', 'abc'), ('https://tuiles.example.org:443/{z}/{x}/{y}.png', 'abc'),
    ('https://TUILES.example.org/{z}/{x}/{y}.png', 'abc'), ('https://{s}.t.example.org/{z}/{x}/{y}.png', 'ab'),
    ('https://{s}.t.example.org/{z}/{x}/{y}{r}.png?k=1', 'ba'), ('https://tuiles.example.org/{s}/{z}/{x}/{y}.png', 'abcd'),
])
def test_legitimate_basemap_urls_pass_the_tiles_list(conn_app, url, subdomains):
    app, conn = conn_app
    write_maps(conn, app.config['SETTINGS_STORE'], [basemap(url=url, subdomains=subdomains)], 'fond')
    assert effective(app.config, 'map.basemaps')[0]['url'] == url


def test_a_basemap_that_uses_a_subdomain_outside_the_list_is_refused_even_when_the_first_ones_are_allowed(conn_app):
    app, conn = conn_app
    store = app.config['SETTINGS_STORE']
    for subs in ('abc', 'cab', 'abcd', 'c'):
        with pytest.raises(SettingsError):
            write_maps(conn, store, [basemap(url='https://{s}.t.example.org/{z}/{x}/{y}.png', subdomains=subs)], 'fond')


def test_the_tiles_list_default_applies_when_no_tiles_entry_exists(tmp_path):
    """EF-6.8 : sans entrée tiles=, les hôtes des cinq fonds embarqués ; la première entrée tiles= remplace tout."""
    app = create_app(TestingConfig, overrides={'DATA_DIR': str(tmp_path / 'd')})
    conn = database.connect(app.config['DB_PATH'])
    store = app.config['SETTINGS_STORE']
    write_maps(conn, store, [basemap(name='osm', url='https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png')], 'osm')
    write_maps(conn, store, [basemap(name='esri', url='https://server.arcgisonline.com/x/{z}/{y}/{x}')], 'esri')
    with pytest.raises(SettingsError):
        write_maps(conn, store, [basemap(name='n', url='https://tuiles.example.org/{z}/{x}/{y}.png')], 'n')
    conn.close()
    app2 = create_app(TestingConfig, overrides={'DATA_DIR': str(tmp_path / 'e'), 'OUTBOUND_ALLOWED_HOSTS': 'tiles=tuiles.example.org'})
    conn2 = database.connect(app2.config['DB_PATH'])
    with pytest.raises(SettingsError):
        write_maps(conn2, app2.config['SETTINGS_STORE'],
                   [basemap(name='osm', url='https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png')], 'osm')   # remplacée, pas complétée
    conn2.close()


def test_the_default_basemap_reference_is_no_longer_checked_by_the_server_but_stays_an_exact_name(conn_app):
    """Décision 6 (EF-6.12) : le contrôle du fond par défaut est celui de la page d'admin ; le serveur l'écrit tel quel, la
    référence reste une chaîne EXACTE (casse, espaces), et le bandeau d'usage signale un fond absent de la liste."""
    app, conn = conn_app
    store = app.config['SETTINGS_STORE']
    with pytest.raises(SettingsError):                                              # caractères de contrôle : toujours refusés
        write_maps(conn, store, [basemap()], 'fond\n')
    for bad in ('FOND', ' fond', 'fon'):
        write_maps(conn, store, [basemap()], bad)                                   # accepté : plus de 400 côté serveur
        assert effective(app.config, 'map.default_basemap') == bad
        from app.api.map_routes import default_basemap_alert
        assert default_basemap_alert(app.config) is not None, bad                   # mais le bandeau d'usage le signale
    write_maps(conn, store, [basemap(), basemap(name='autre')], 'autre')
    assert default_basemap_alert(app.config) is None
    write_maps(conn, store, [basemap()])                                            # fond par défaut retiré de la liste
    assert effective(app.config, 'map.default_basemap') == 'autre' and default_basemap_alert(app.config) is not None


def test_an_empty_basemap_list_is_handled_end_to_end(conn_app):
    """La spec ne fixe pas de minimum : si la liste vide est acceptée, le serveur doit répondre proprement (basemaps [] / default None)."""
    app, conn = conn_app
    store = app.config['SETTINGS_STORE']
    try:
        store.set_many(conn, {'map.basemaps': [], 'map.default_basemap': ''}, actor=CLI)
    except SettingsError:
        return                                              # refusée : tout aussi cohérent
    cfg = app.test_client().get('/api/map-config')
    assert cfg.status_code == 200 and cfg.get_json()['basemaps'] == [] and cfg.get_json()['default'] is None


@pytest.mark.parametrize('field,value', [
    ('attribution', '<script>alert(1)</script>'), ('attribution', '<img src=x onerror=alert(1)>'),
    ('attribution', '<a href="javascript:alert(1)">x</a>'), ('attribution', '<svg onload=alert(1)>'),
    ('attribution', '<iframe src="https://attaquant.example"></iframe>'), ('name', '<img src=x onerror=alert(1)>'),
    ('name', '"><script>alert(1)</script>'),
])
def test_script_capable_html_is_neutralised_before_it_reaches_the_browser(conn_app, field, value):
    app, conn = conn_app
    try:
        write_maps(conn, app.config['SETTINGS_STORE'], [basemap(**{field: value})], value if field == 'name' else 'fond')
    except (SettingsError, ValueError):
        return                                              # refusé à l'écriture : conforme
    body = app.test_client().get('/api/map-config').get_json()
    raw = json.dumps(body)
    assert not re.search(r'<\s*(script|img|svg|iframe)|javascript:|onerror\s*=|onload\s*=', raw, re.I), raw


@pytest.mark.parametrize('field,value', [('attribution', '<img src=x onerror=alert(1)>'), ('attribution', '<script>alert(1)</script>'),
                                         ('name', '<svg onload=alert(1)>')])
def test_markup_forced_in_the_database_never_reaches_the_browser(conn_app, field, value):
    """T16-1 à l'USAGE : une liste forcée en base (SQL) avec du HTML ne doit pas être renvoyée telle quelle."""
    app, conn = conn_app
    force(conn, app, **{'map.basemaps': json.dumps([basemap(**{field: value})]), 'map.default_basemap': json.dumps(value if field == 'name' else 'fond')})
    response = app.test_client().get('/api/map-config')
    assert response.status_code == 200
    assert not re.search(r'<\s*(script|img|svg|iframe)|onerror\s*=|onload\s*=', json.dumps(response.get_json()), re.I)


def test_ordinary_attributions_with_links_and_entities_stay_possible(conn_app):
    """Contrepartie du test précédent : une correction ne doit pas casser l'attribution OSM habituelle (texte brut ou lien sûr)."""
    app, conn = conn_app
    write_maps(conn, app.config['SETTINGS_STORE'], [basemap(attribution='© OpenStreetMap contributors')], 'fond')
    assert '© OpenStreetMap' in app.test_client().get('/api/map-config').get_json()['basemaps'][0]['attribution']


# ===== C. /api/map-config =======================================================================================================

def force(conn, app, **rows):
    for key, raw in rows.items():
        conn.execute("INSERT OR REPLACE INTO settings (key, value, updated_at, updated_by) VALUES (?, ?, 'x', 'sql')", (key, raw))
    conn.execute("UPDATE settings_meta SET version = version + 1")
    conn.commit()
    app.config['SETTINGS_STORE'].refresh(conn, force=True)


def strict_json(response):
    """Le JSON reçu par le navigateur doit être du JSON strict (pas de NaN/Infinity)."""
    def refuse(token):
        raise AssertionError(f"jeton JSON non standard : {token}")
    return json.loads(response.get_data(as_text=True), parse_constant=refuse)


@pytest.mark.parametrize('lat', ['NaN', 'Infinity', '-Infinity', '1e999', '"x"', 'true', 'null', '[1]', '{}', '86', '-86', '9' * 400])
def test_a_forged_center_never_produces_non_standard_json_or_an_out_of_bounds_value(conn_app, lat):
    app, conn = conn_app
    force(conn, app, **{'map.center_lat': lat})
    response = app.test_client().get('/api/map-config')
    assert response.status_code == 200
    center = strict_json(response)['center']
    assert -85 <= center[0] <= 85 and -180 <= center[1] <= 180 and all(isinstance(v, (int, float)) and math.isfinite(v) for v in center)


@pytest.mark.parametrize('raw', ['0', '23', '"6"', 'null', 'true', '6.5', '1e99', 'NaN', '-1', '[]'])
def test_a_forged_zoom_falls_back_to_a_valid_integer(conn_app, raw):
    app, conn = conn_app
    force(conn, app, **{'map.zoom': raw})
    zoom = strict_json(app.test_client().get('/api/map-config'))['zoom']
    assert isinstance(zoom, int) and not isinstance(zoom, bool) and 1 <= zoom <= 22


@pytest.mark.parametrize('forged', [
    'not json', '{}', '"texte"', '5', 'null', '[1, 2]', '[{"name": "x"}]', '[{"name": "x", "url": "javascript:alert(1)"}]',
    json.dumps([basemap(name=f'f{i}') for i in range(11)]), json.dumps([basemap(name='x' * 5000)]),
    json.dumps([basemap(url='http://tuiles.example.org/{z}/{x}/{y}')]), json.dumps([basemap(max_zoom=99)]),
    json.dumps([basemap(attribution='x' * 10_000)]), '[' * 5000 + ']' * 5000, '[{"name": "a", "url": "https://tuiles.example.org/{z}/{x}/{y}", "x": 1}]',
])
def test_a_malformed_basemap_list_forced_in_the_database_never_breaks_the_endpoint_or_leaks(conn_app, forged):
    app, conn = conn_app
    force(conn, app, **{'map.basemaps': forged})
    response = app.test_client().get('/api/map-config')
    assert response.status_code == 200, response.get_data(as_text=True)[:200]
    body = strict_json(response)
    for entry in body['basemaps']:
        assert entry['url'].startswith('https://') and len(entry['name']) <= 40 and len(entry.get('attribution', '')) <= 200
        assert entry['max_zoom'] <= 22
    assert 'javascript:' not in json.dumps(body)
    assert (body['default'] is None) == (not body['basemaps'])


def test_every_host_of_a_forced_basemap_must_be_allowed_not_only_the_first(conn_app):
    """Forcé en base : {s} = abc, seuls a et b sont autorisés => le fond est retiré en entier (CA-41), pas tronqué."""
    app, conn = conn_app
    force(conn, app, **{'map.basemaps': json.dumps([basemap('ok'), basemap('mixte', url='https://{s}.t.example.org/{z}/{x}/{y}.png',
                                                                               subdomains='abc')]),
                        'map.default_basemap': '"mixte"'})
    body = app.test_client().get('/api/map-config').get_json()
    assert [b['name'] for b in body['basemaps']] == ['ok'] and body['default'] == 'ok'


def test_when_every_basemap_is_refused_the_answer_is_empty_but_still_valid(conn_app):
    app, conn = conn_app
    force(conn, app, **{'map.basemaps': json.dumps([basemap('x', url='https://attaquant.example/{z}/{x}/{y}')]),
                        'map.default_basemap': '"x"'})
    body = strict_json(app.test_client().get('/api/map-config'))
    assert body['basemaps'] == [] and body['default'] is None and body['zoom'] == 6 and body['center']


def test_the_audit_of_a_refused_basemap_is_deduplicated_per_destination_and_carries_no_user_data(conn_app):
    app, conn = conn_app
    force(conn, app, **{'map.basemaps': json.dumps([basemap('ok'), basemap('a', url='https://un.example/{z}/{x}/{y}')])})
    client = app.test_client()
    for _ in range(5):
        client.get('/api/map-config', environ_overrides={'REMOTE_ADDR': '203.0.113.9'}, headers={'User-Agent': 'UA-secret'})
    rows = [dict(r) for r in conn.execute("SELECT * FROM audit_log WHERE action = 'outbound.refused'")]
    assert len(rows) == 1
    assert 'un.example' in rows[0]['details'] and '203.0.113.9' not in json.dumps(rows[0]) and 'UA-secret' not in json.dumps(rows[0])
    force(conn, app, **{'map.basemaps': json.dumps([basemap('ok'), basemap('b', url='https://deux.example/{z}/{x}/{y}')])})
    client.get('/api/map-config')
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'outbound.refused'").fetchone()[0] == 2     # nouvelle destination


def test_the_endpoint_is_read_only_uncacheable_and_json(conn_app):
    app, _ = conn_app
    client = app.test_client()
    ok = client.get('/api/map-config')
    assert ok.status_code == 200 and ok.mimetype == 'application/json' and 'no-store' in ok.headers['Cache-Control']
    for method in ('post', 'put', 'patch', 'delete'):
        assert getattr(client, method)('/api/map-config', json={}).status_code in (403, 405)
    assert client.head('/api/map-config').status_code == 200


def img_src(response):
    csp = response.headers['Content-Security-Policy']
    directive = next(d for d in (x.strip() for x in csp.split(';')) if d.startswith('img-src'))
    return directive.split()[1:]


def hosts_of(entry):
    return outbound.tile_hosts_of_template(entry['url'], entry.get('subdomains', 'abc'))


def tokens_of(hosts):
    return {f'https://{h}' if isinstance(h, str) else f'https://{h[0]}' for h in hosts}


def test_the_csp_lists_exactly_the_hosts_of_the_basemaps_the_browser_receives(conn_app):
    """Correspondance réelle : chaque hôte effectif d'un fond renvoyé est dans img-src ; img-src n'a aucun hôte en plus ni « https: »."""
    app, conn = conn_app
    write_maps(conn, app.config['SETTINGS_STORE'],
               [basemap('a', url='https://{s}.t.example.org/{z}/{x}/{y}.png', subdomains='ab'), basemap('b')], 'a')
    response = app.test_client().get('/api/map-config')
    sources = img_src(response)
    assert 'https:' not in sources and "'self'" in sources and 'data:' in sources and 'blob:' in sources
    needed = set()
    for entry in response.get_json()['basemaps']:
        needed |= {f'https://{h}' for h in (h if isinstance(h, str) else h[0] for h in hosts_of(entry))}
    assert needed <= set(sources)
    assert {s for s in sources if s.startswith('https://')} == {'https://tuiles.example.org', 'https://a.t.example.org',
                                                                'https://b.t.example.org'}


def test_with_default_settings_the_five_embedded_basemaps_are_all_covered_by_the_csp(tmp_path):
    """CA-41 « comme avant » : si un hôte d'un fond par défaut manquait à la CSP, la carte afficherait des tuiles vides."""
    app = create_app(TestingConfig, overrides={'DATA_DIR': str(tmp_path / 'd')})
    response = app.test_client().get('/api/map-config')
    body = response.get_json()
    assert len(body['basemaps']) == 5
    sources = set(img_src(response))
    for entry in body['basemaps']:
        for h in hosts_of(entry):
            host = h if isinstance(h, str) else h[0]
            assert f'https://{host}' in sources, (entry['name'], host)


def test_the_csp_of_html_pages_equals_the_csp_of_the_api(conn_app):
    app, _ = conn_app
    client = app.test_client()
    assert img_src(client.get('/')) == img_src(client.get('/api/map-config'))


def test_a_tiles_list_without_the_embedded_hosts_empties_the_default_map_and_audits_it_once(tmp_path):
    """EF-6.8 : la première entrée tiles= remplace la liste par défaut ; les fonds embarqués sont retirés (observation de conception)."""
    app = create_app(TestingConfig, overrides={'DATA_DIR': str(tmp_path / 'd'), 'OUTBOUND_ALLOWED_HOSTS': 'tiles=tuiles.example.org'})
    conn = database.connect(app.config['DB_PATH'])
    client = app.test_client()
    body = client.get('/api/map-config').get_json()
    client.get('/api/map-config')
    assert body['basemaps'] == [] and body['default'] is None
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'outbound.refused'").fetchone()[0] <= 5
    conn.close()


def test_map_config_is_not_served_to_an_anonymous_visitor_in_oidc_mode(tmp_path):
    app = create_app(TestingConfig, overrides={**OIDC_OVERRIDES, 'DATA_DIR': str(tmp_path / 'o'), 'TRUSTED_HOSTS': [HOST]})
    for path in ('/api/map-config', '/api/map-config?x=1', '/api/map-config/', '/api//map-config'):
        assert app.test_client().get(path, base_url=f'https://{HOST}').status_code in (401, 404, 308), path


# ===== D. Contrôle d'origine contre PUBLIC_URL (T9-1) ===========================================================================

REFUSED_BODY = "Origine de la requête refusée"


_ips = iter(f'198.{a}.{b}.{c}' for a in range(18, 20) for b in range(0, 256) for c in range(1, 255))


def next_ip():
    """Chaque requête vient d'une IP distincte : le limiteur de jetons (par IP) ne doit pas fausser le verdict d'origine."""
    return next(_ips)


def _text(response):
    """Corps décodé (flask échappe les accents en \\uXXXX dans le JSON)."""
    try:
        data = response.get_json(silent=True)
    except Exception:
        data = None
    return json.dumps(data, ensure_ascii=False) if data is not None else response.get_data(as_text=True)


def post(app, origins, base_url, path='/auth/token/inspect', method='post', extra=None, csrf_origin=None):
    """Requête modifiante avec un ou plusieurs en-têtes Origin ; renvoie (statut, corps JSON ou texte)."""
    client = app.test_client()
    html = client.get('/auth/token', base_url=base_url).get_data(as_text=True)
    token = re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)
    ip = next_ip()
    headers = [('X-CSRF-Token', token)] + [('Origin', o) for o in ([] if origins is None else [origins] if isinstance(origins, str) else origins)]
    headers += list((extra or {}).items())
    if not any(k.lower() == 'x-forwarded-for' for k, _ in headers):
        headers.append(('X-Forwarded-For', ip))
    response = getattr(client, method)(path, json={'token': 'A' * 43}, headers=headers, base_url=base_url,
                                       environ_overrides={'REMOTE_ADDR': ip})
    return response.status_code, _text(response)


def is_refused_by_origin(result):
    return result[0] == 403 and REFUSED_BODY in result[1]


@pytest.fixture(scope='module')
def https_app(tmp_path_factory):
    return password_app(tmp_path_factory.mktemp('https'))


@pytest.fixture(scope='module')
def https_proxy_app(tmp_path_factory):
    return password_app(tmp_path_factory.mktemp('hp'), TRUST_PROXY=1)


ACCEPTED = [f'https://{HOST}', f'https://{HOST.upper()}', f'https://{HOST}:443', f'https://{HOST.title()}', f'HTTPS://{HOST}']
REFUSED = [
    f'http://{HOST}', f'http://{HOST}:443', f'https://{HOST}:80', f'https://{HOST}:8443', f'https://{HOST}:444', f'http://{HOST}:80',
    'https://evil.example', f'https://{HOST}.evil.example', f'https://evil.{HOST}', f'https://x{HOST}', f'https://evil.example/{HOST}',
    f'https://{HOST}@evil.example', f'https://evil@{HOST}', f'https://evil:pw@{HOST}', f'https://:@{HOST}', f'https://evil.example\\@{HOST}',
    f'https://evil.example#@{HOST}', f'https://evil.example?@{HOST}', f'https://{HOST}\\@evil.example', 'null', 'NULL', 'undefined',
    f'ftp://{HOST}', f'file://{HOST}', f'wss://{HOST}', f'//{HOST}', HOST, f'https:{HOST}', f'https:///{HOST}', 'https://', 'https:', ':',
    f'https://{HOST}:abc', f'https://{HOST}:99999', f'https://{HOST}:-1', f'https://[{HOST}]', 'https://[::1]',
    'https://127.0.0.1', 'https://localhost', f'https://{HOST}%2f@evil.example', f'https://{HOST}\x00',
    f'https://{HOST} ', f'https://{HOST}, https://evil.example', f'https://evil.example, https://{HOST}',
    f'https://{HOST} https://evil.example', 'https://ехаmple.com', f'https://{HOST}。evil.example',
]


@pytest.mark.parametrize('origin', ACCEPTED)
def test_own_origin_variants_are_accepted(https_app, origin):
    status, _ = post(https_app, origin, f'http://{HOST}')            # l'application voit « http » (proxy TLS sans TRUST_PROXY)
    assert status == 404, origin                                      # jeton inconnu : l'origine est passée


@pytest.mark.parametrize('origin', REFUSED)
def test_every_foreign_or_malformed_origin_is_refused(https_app, origin):
    assert is_refused_by_origin(post(https_app, origin, f'http://{HOST}')), repr(origin)


@pytest.mark.parametrize('origin', ACCEPTED[:3])
def test_the_scheme_seen_by_the_application_does_not_change_the_verdict(https_app, origin):
    for seen in (f'http://{HOST}', f'https://{HOST}'):
        assert post(https_app, origin, seen)[0] == 404


@pytest.mark.parametrize('origin', REFUSED[:12] + ['null'])
def test_the_same_verdicts_hold_with_trust_proxy(https_proxy_app, origin):
    xf = {'X-Forwarded-Proto': 'https'}
    assert is_refused_by_origin(post(https_proxy_app, origin, f'http://{HOST}', extra=xf))
    assert post(https_proxy_app, f'https://{HOST}', f'http://{HOST}', extra=xf)[0] == 404


@pytest.mark.parametrize('extra', [
    {'X-Forwarded-Proto': 'http'}, {'X-Forwarded-Proto': 'https'}, {'X-Forwarded-Host': 'evil.example'}, {'X-Forwarded-Host': HOST},
    {'X-Forwarded-Port': '8443'}, {'Forwarded': 'host=evil.example;proto=https'}, {'X-Forwarded-For': '127.0.0.1'},
    {'X-Original-Host': 'evil.example'}, {'X-Host': 'evil.example'}, {'X-Forwarded-Server': 'evil.example'},
])
@pytest.mark.parametrize('app_fixture', ['https_app', 'https_proxy_app'])
def test_forwarded_headers_cannot_make_a_foreign_origin_pass_nor_refuse_the_own_one(request, app_fixture, extra):
    """L'attaquant ne choisit pas les en-têtes de X-Forwarded-* d'un navigateur victime ; mais même présents, PUBLIC_URL fait foi."""
    app = request.getfixturevalue(app_fixture)
    assert is_refused_by_origin(post(app, 'https://evil.example', f'http://{HOST}', extra=extra))
    assert is_refused_by_origin(post(app, f'https://{HOST}:8443', f'http://{HOST}', extra=extra))
    status = post(app, f'https://{HOST}', f'http://{HOST}', extra=extra)[0]
    assert status in (404, 400), extra        # 400 : hôte non fiable (TRUST_PROXY_HOST), jamais 403 d'origine
    assert not is_refused_by_origin(post(app, f'https://{HOST}', f'http://{HOST}', extra=extra))


@pytest.mark.parametrize('method', ['post', 'put', 'patch', 'delete'])
def test_every_unsafe_method_is_checked_and_a_good_origin_reaches_the_router(https_app, method):
    bad = post(https_app, 'https://evil.example', f'http://{HOST}', method=method)
    assert is_refused_by_origin(bad), method
    good = post(https_app, f'https://{HOST}', f'http://{HOST}', method=method)
    assert not is_refused_by_origin(good) and good[0] in (302, 401, 403, 404, 405), (method, good)    # le garde/routeur prend le relais


@pytest.mark.parametrize('method', ['get', 'head', 'options'])
def test_safe_methods_are_never_blocked_by_the_origin_check(https_app, method):
    client = https_app.test_client()
    response = getattr(client, method)('/auth/token', headers={'Origin': 'https://evil.example'}, base_url=f'http://{HOST}')
    assert response.status_code in (200, 204) and REFUSED_BODY not in response.get_data(as_text=True)


def test_multiple_origin_headers_are_not_a_bypass(https_app):
    """Deux en-têtes Origin distincts : aucun ordre ne doit permettre à l'attaquant de faire passer sa valeur."""
    for pair in ([f'https://{HOST}', 'https://evil.example'], ['https://evil.example', f'https://{HOST}']):
        assert is_refused_by_origin(post(https_app, pair, f'http://{HOST}')), pair


@pytest.mark.parametrize('site,expected_refused', [('cross-site', True), ('same-site', True), ('same-origin', False), ('none', False),
                                                   ('', False), ('unknown', False)])
def test_without_origin_the_sec_fetch_site_rule_applies(https_app, site, expected_refused):
    extra = {'Sec-Fetch-Site': site} if site else {}
    result = post(https_app, None, f'http://{HOST}', extra=extra)
    assert is_refused_by_origin(result) == expected_refused, site


def test_a_valid_origin_wins_over_a_hostile_sec_fetch_site_only_when_it_is_really_ours(https_app):
    """Un Origin étranger avec Sec-Fetch-Site: same-origin reste refusé (l'attaquant ne contrôle pas ces en-têtes, mais l'ordre compte)."""
    assert is_refused_by_origin(post(https_app, 'https://evil.example', f'http://{HOST}', extra={'Sec-Fetch-Site': 'same-origin'}))


def test_the_check_runs_before_authentication_csrf_and_the_router(https_app):
    """Une origine étrangère donne 403 d'origine même pour un jeton CSRF invalide ou une route inexistante."""
    client = https_app.test_client()
    for path in ('/auth/token/inspect', '/auth/token/consume', '/api/invitations', '/nimporte/quoi', '/admin/x'):
        response = client.post(path, json={}, headers={'Origin': 'https://evil.example'}, base_url=f'http://{HOST}')
        assert response.status_code == 403 and REFUSED_BODY in _text(response), path


@pytest.mark.parametrize('public,seen,good,bad', [
    ('https://traces.example.com:8443', 'http://traces.example.com:8443', ['https://traces.example.com:8443'],
     ['https://traces.example.com', 'https://traces.example.com:443', 'http://traces.example.com:8443', 'https://traces.example.com:8444']),
    ('https://traces.example.com:443', 'http://traces.example.com', ['https://traces.example.com', 'https://traces.example.com:443'],
     ['http://traces.example.com', 'https://traces.example.com:80']),
])
def test_other_public_urls(tmp_path, public, seen, good, bad):
    app = password_app(tmp_path, PUBLIC_URL=public)
    for origin in good:
        assert post(app, origin, seen)[0] == 404, origin
    for origin in bad:
        assert is_refused_by_origin(post(app, origin, seen)), origin


@pytest.mark.parametrize('public,good,bad', [
    ('http://localhost:5000', ['http://localhost:5000', 'http://LOCALHOST:5000'], ['https://localhost:5000', 'http://localhost', 'http://127.0.0.1:5000']),
    ('http://127.0.0.1:5000', ['http://127.0.0.1:5000'], ['http://localhost:5000', 'https://127.0.0.1:5000']),
    ('http://[::1]:5000', ['http://[::1]:5000'], ['http://[::2]:5000', 'http://[::1]:5001', 'http://localhost:5000', 'https://[::1]:5000',
                                                  'http://[::1]', 'http://[0:0:0:0:0:0:0:2]:5000']),
    ('http://localhost', ['http://localhost', 'http://localhost:80'], ['https://localhost', 'http://localhost:8080']),
    ('https://[2001:db8::1]', ['https://[2001:db8::1]', 'https://[2001:db8::1]:443', 'https://[2001:DB8::1]'],
     ['http://[2001:db8::1]', 'https://[2001:db8::2]', 'https://[2001:db8::1]:8443']),
])
def test_origin_matching_for_local_and_ipv6_literal_public_urls(https_app, public, good, bad):
    """Unité : `_origin_matches` contre une PUBLIC_URL donnée (les littéraux IPv6 ne démarrent pas en mode password : voir plus bas)."""
    from app import security
    original = https_app.config['PUBLIC_URL']
    https_app.config['PUBLIC_URL'] = public
    try:
        for origin in good:
            with https_app.test_request_context('/', method='POST', headers={'Origin': origin}):
                assert security._origin_matches(origin) is True, origin
        for origin in bad:
            with https_app.test_request_context('/', method='POST', headers={'Origin': origin}):
                assert security._origin_matches(origin) is False, origin
    finally:
        https_app.config['PUBLIC_URL'] = original


@pytest.mark.xfail(strict=True, reason="nit : normalize_public_url accepte http://[::1]:5000 (localhost, EF-S7) mais host_is_trusted de werkzeug ne "
                                       "reconnaît jamais un littéral IPv6 dans TRUSTED_HOSTS : le démarrage échoue avec « PUBLIC_URL n'est pas "
                                       "couverte par TRUSTED_HOSTS » quelle que soit la liste (échec fermé, message trompeur)")
@pytest.mark.parametrize('trusted', [['[::1]'], ['::1'], ['[::1]', '::1', 'localhost']])
def test_an_ipv6_literal_public_url_can_start_in_password_mode(tmp_path, trusted):
    password_app(tmp_path, PUBLIC_URL='http://[::1]:5000', TRUSTED_HOSTS=trusted)


def test_public_url_is_normalised_so_a_spelling_difference_cannot_lock_everybody_out(tmp_path):
    app = password_app(tmp_path, PUBLIC_URL=f'https://{HOST.upper()}/')
    assert post(app, f'https://{HOST}', f'http://{HOST}')[0] == 404


@pytest.mark.xfail(strict=True, reason="défense en profondeur (nit) : si PUBLIC_URL devenait inutilisable (None), _origin_tuple(origin) == "
                                       "_origin_tuple(public) vaut None == None => toute origine inutilisable (« null ») serait acceptée. "
                                       "Inatteignable aujourd'hui : PUBLIC_URL est validée au démarrage")
def test_an_unusable_public_url_must_never_make_a_null_origin_pass(https_app):
    from app import security
    original = https_app.config['PUBLIC_URL']
    https_app.config['PUBLIC_URL'] = 'ftp://??'
    try:
        with https_app.test_request_context('/', method='POST', headers={'Origin': 'null'}):
            assert security._origin_matches('null') is False
    finally:
        https_app.config['PUBLIC_URL'] = original


def test_without_public_url_the_host_header_comparison_is_unchanged(tmp_path):
    app = create_app(TestingConfig, overrides={'DATA_DIR': str(tmp_path / 'l'), 'AUTH_MODE': 'local'})
    client = app.test_client()

    def status(origin, host='localhost'):
        return client.post('/api/export/csv', json={'features': []}, headers={'Origin': origin}, base_url=f'http://{host}').status_code
    assert status('http://localhost') == 200 and status('https://localhost') == 200
    for bad in ('https://evil.example', 'http://localhost:1234', 'null', 'http://localhost.evil.example', 'http://evil@localhost',
                'http://localhost@evil.example', 'ftp://evil.example'):
        assert status(bad) == 403, bad


# ===== E. Les consommateurs suivent la valeur effective =========================================================================

class FakeResponse:
    status_code = 200

    def __init__(self, n):
        self.n = n

    def raise_for_status(self):
        pass

    def json(self):
        return {'status': 'OK', 'results': [{'elevation': 100.0 + i} for i in range(self.n)]}


@pytest.fixture
def dem_world(monkeypatch, tmp_path):
    """Faux service d'altitude + horloge/sommeil simulés ; l'URL est autorisée pour `dem`."""
    from app.services import dem_service
    app = create_app(TestingConfig, overrides={'DATA_DIR': str(tmp_path / 'd'), 'DEM_URL': 'http://dem.example:5000',
                                               'OUTBOUND_ALLOWED_HOSTS': 'dem=dem.example:5000'})
    state = {'now': 1000.0, 'calls': [], 'sleeps': [], 'cost': 0.0}

    def get(self, url, params=None, timeout=None, **kw):
        n = len(params['locations'].split('|'))
        state['calls'].append({'n': n, 'timeout': timeout})
        state['now'] += state['cost']
        return FakeResponse(n)

    monkeypatch.setattr(dem_service.requests.Session, 'get', get)
    monkeypatch.setattr(dem_service.time, 'monotonic', lambda: state['now'])
    monkeypatch.setattr(dem_service.time, 'sleep', lambda s: (state['sleeps'].append(s), state.__setitem__('now', state['now'] + s)))
    conn = database.connect(app.config['DB_PATH'])
    yield app, conn, state, dem_service
    conn.close()


def points(n):
    return [45.0 + i * 1e-4 for i in range(n)], [6.0] * n


@pytest.mark.parametrize('batch,expected', [(1, [1] * 7), (3, [3, 3, 1]), (7, [7]), (1000, [7])])
def test_dem_batch_size_follows_the_effective_value(dem_world, batch, expected):
    app, conn, state, dem = dem_world
    app.config['SETTINGS_STORE'].set_many(conn, {'DEM_BATCH_SIZE': batch}, actor=CLI)
    assert len(dem.fetch_elevations(*points(7), app.config)) == 7
    assert [c['n'] for c in state['calls']] == expected


def test_dem_timeout_follows_the_effective_value_and_is_capped_by_the_remaining_budget(dem_world):
    app, conn, state, dem = dem_world
    store = app.config['SETTINGS_STORE']
    store.set_many(conn, {'DEM_TIMEOUT': 2.5, 'DEM_TOTAL_TIMEOUT': 600}, actor=CLI)
    dem.fetch_elevations(*points(3), app.config)
    assert state['calls'][-1]['timeout'] == 2.5
    store.set_many(conn, {'DEM_TIMEOUT': 120, 'DEM_TOTAL_TIMEOUT': 10}, actor=CLI)         # le budget restant borne le délai d'un appel
    dem.fetch_elevations(*points(3), app.config)
    assert state['calls'][-1]['timeout'] <= 10.0


@pytest.mark.parametrize('delay', [0, 0.5, 3])
def test_dem_request_delay_follows_the_effective_value(dem_world, delay):
    app, conn, state, dem = dem_world
    store = app.config['SETTINGS_STORE']
    store.set_many(conn, {'DEM_BATCH_SIZE': 2, 'DEM_REQUEST_DELAY': delay, 'DEM_TOTAL_TIMEOUT': 600}, actor=CLI)
    dem.fetch_elevations(*points(6), app.config)                                            # 3 lots => 2 pauses
    assert state['sleeps'] == ([delay] * 2 if delay else [])


def test_dem_total_timeout_follows_the_effective_value(dem_world):
    """Chaque appel « coûte » 6 s : avec 10 s le deuxième lot passe, le troisième dépasse le budget ; avec 100 s tout passe."""
    app, conn, state, dem = dem_world
    store = app.config['SETTINGS_STORE']
    state['cost'] = 6.0
    store.set_many(conn, {'DEM_BATCH_SIZE': 1, 'DEM_TOTAL_TIMEOUT': 10}, actor=CLI)
    with pytest.raises(dem.DemError):
        dem.fetch_elevations(*points(5), app.config)
    assert len(state['calls']) == 2
    state['calls'].clear()
    store.set_many(conn, {'DEM_TOTAL_TIMEOUT': 100}, actor=CLI)
    assert len(dem.fetch_elevations(*points(5), app.config)) == 5 and len(state['calls']) == 5


def test_a_dem_value_changed_by_another_process_is_picked_up_by_the_next_refresh(dem_world):
    app, conn, state, dem = dem_world
    other = database.connect(app.config['DB_PATH'])
    app.config['SETTINGS_STORE'].set_many(other, {'DEM_BATCH_SIZE': 2}, actor=CLI)
    other.close()
    dem.fetch_elevations(*points(4), app.config)                       # sans refresh : l'ancien cache est utilisé
    app.config['SETTINGS_STORE'].refresh(conn, force=True)
    state['calls'].clear()
    dem.fetch_elevations(*points(4), app.config)
    assert [c['n'] for c in state['calls']] == [2, 2]


def test_user_quota_follows_the_effective_value(tmp_path):
    from app.services import ingest_service
    from tests.conftest import line, make_gpx
    app = create_app(TestingConfig, overrides={'DATA_DIR': str(tmp_path / 'd')})
    conn = database.connect(app.config['DB_PATH'])
    uid = conn.execute("INSERT INTO users (sub, username) VALUES ('s', 'u')").lastrowid
    conn.commit()
    store = app.config['SETTINGS_STORE']

    def ingest(name, n):
        return ingest_service.ingest_bytes(conn, app.config, uid, make_gpx(line(n), name=name).encode(), f'{name}.gpx', 'upload')
    assert ingest('petit', 5).status == 'created'
    big = 9000
    store.set_many(conn, {'USER_QUOTA_MB': 1}, actor=CLI)
    first = ingest('gros1', big)
    second = ingest('gros2', big + 1)
    assert first.status == 'created' and second.status == 'error' and 'Quota' in second.message       # 1 Mo dépassé au 2e fichier
    store.set_many(conn, {'USER_QUOTA_MB': 0}, actor=CLI)                                             # 0 = illimité (EF-6.5)
    assert ingest('gros2', big + 1).status == 'created'
    conn.close()


def test_the_backfill_batch_follows_the_effective_value(tmp_path, monkeypatch):
    from app.services import ingest_service
    app = create_app(TestingConfig, overrides={'DATA_DIR': str(tmp_path / 'd'), 'DEM_URL': 'http://dem.example:5000',
                                               'OUTBOUND_ALLOWED_HOSTS': 'dem=dem.example:5000'})
    conn = database.connect(app.config['DB_PATH'])
    seen = []
    monkeypatch.setattr(ingest_service.activity_store, 'pending_dem', lambda db, limit, last=(): (seen.append(limit), [])[1])
    app.config['SETTINGS_STORE'].set_many(conn, {'DEM_BACKFILL_BATCH': 3}, actor=CLI)
    ingest_service.backfill_elevation(conn, app.config)
    app.config['SETTINGS_STORE'].set_many(conn, {'DEM_BACKFILL_BATCH': 17}, actor=CLI)
    ingest_service.backfill_elevation(conn, app.config)
    assert seen == [3, 17]
    conn.close()


def test_the_invitation_lifetime_and_the_mfa_policy_are_read_through_the_registry():
    """Pas de constante parallèle : les consommateurs EF-6.5 passent par `effective` (garde-fou contre une régression silencieuse)."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent / 'app'
    expectations = {'api/invitations_routes.py': 'auth.invite_ttl_hours', 'auth/password_login.py': 'auth.mfa_policy',
                    'auth/oidc.py': 'OIDC_AUTO_ACTIVATE', 'services/dem_service.py': 'DEM_TOTAL_TIMEOUT',
                    'services/ingest_service.py': 'USER_QUOTA_MB', 'background.py': 'audit.retention_days'}
    for path, key in expectations.items():
        text = (root / path).read_text(encoding='utf-8')
        assert re.search(rf"effective\([^)]*'{re.escape(key)}'", text), (path, key)

