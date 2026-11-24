"""Destinations sortantes : liste blanche par service, tuiles et CSP, secrets liés, refus audités (T18, ADR-0011, CA-36/37/41/42/52)."""

import json
import time

import pytest
from cryptography.fernet import Fernet

from app import create_app, db as database, outbound
from app.audit import Actor
from app.background import Scheduler, default_tasks
from app.config import TestingConfig
from app.outbound import Allowlist, OutboundError, RefusalLog
from app.secrets_store import SecretsStore
from app.services import dem_service
from app.settings import REGISTRY, SettingsError, SettingsStore
from app.settings.registry import Setting

KEY = Fernet.generate_key().decode()
ADMIN = Actor.user(1, 'admin')


# --- Analyse de la variable (CA-42, CA-52) ---------------------------------------------------------------------

def test_parse_accepts_one_entry_per_service_with_ports_and_tiles_without():
    allow = Allowlist.parse('routing=graphhopper:8989, dem=opentopodata:5000,smtp=relay.example.com:587,'
                            'tiles=a.tile.openstreetmap.org,tiles=Tuiles.Example.org')
    assert allow.servers['routing'] == {('graphhopper', 8989)} and allow.servers['dem'] == {('opentopodata', 5000)}
    assert allow.servers['smtp'] == {('relay.example.com', 587)}
    assert allow.explicit_tiles == {'a.tile.openstreetmap.org', 'tuiles.example.org'}
    assert Allowlist.parse('') .servers['dem'] == frozenset() and Allowlist.parse(None).explicit_tiles == frozenset()


@pytest.mark.parametrize('text', [
    'routing=moteur', 'dem=opentopodata', 'smtp=relay.example.com', 'routing=fd00::1:8989', 'routing=fd00::1',
    'routing=[fd00::1]', 'routing=[fd00::zz]:8989', 'routing=h:0', 'routing=h:70000', 'routing=h:port', 'routing=:5000',
    'routing=a b:5000', 'dem=h:5000:6', 'ftp=h:21', 'h:5000', '=h:5000', 'tiles=h:443', 'tiles=', 'tiles=a b',
    'tiles=https://tuiles.org', 'routing=http://moteur:8989', 'tiles=h/p',
])
def test_malformed_entries_refuse_the_startup(text):
    with pytest.raises(OutboundError):
        Allowlist.parse(text)


def test_ipv6_literals_need_brackets():
    """CA-52."""
    with pytest.raises(OutboundError):
        Allowlist.parse('routing=fd00::1:8989')
    assert Allowlist.parse('routing=[fd00::1]:8989').servers['routing'] == {('fd00::1', 8989)}
    assert Allowlist.parse('routing=[FD00:0:0:0:0:0:0:1]:8989').servers['routing'] == {('fd00::1', 8989)}


def _app(tmp_path, **overrides):
    return create_app(TestingConfig, overrides={'DATA_DIR': str(tmp_path / 'data'), **overrides})


def test_the_application_refuses_to_start_with_an_invalid_variable(tmp_path):
    for bad in ('routing=moteur', 'dem=h', 'smtp=h', 'routing=fd00::1:8989', 'inconnu=h:1'):
        with pytest.raises(RuntimeError, match='OUTBOUND_ALLOWED_HOSTS'):
            _app(tmp_path, OUTBOUND_ALLOWED_HOSTS=bad)
    assert _app(tmp_path, OUTBOUND_ALLOWED_HOSTS='routing=[fd00::1]:8989').config['OUTBOUND'].servers['routing']


def test_the_variable_is_not_a_registered_setting():
    assert not any('OUTBOUND' in (s.env or '') or 'OUTBOUND' in k.upper() for k, s in REGISTRY.items())


# --- Normalisation ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('url,expected', [
    ('http://DEM.example.org:5000/v1/x', 'http://dem.example.org:5000'), ('https://moteur', 'https://moteur:443'),
    ('http://moteur', 'http://moteur:80'), ('https://moteur:8443/a?b=c', 'https://moteur:8443'),
    ('http://[FD00::1]:8989/', 'http://[fd00::1]:8989'), ('https://moteur./x', 'https://moteur:443'),
])
def test_destination_normalization_uses_the_effective_port(url, expected):
    assert outbound.destination_of_url(url) == expected


@pytest.mark.parametrize('url', [
    '', ' ', 'ftp://h:21', 'file:///etc/passwd', 'http://', 'http://u:p@h:5000', 'http://h:99999', 'http://h :5000',
    'javascript:alert(1)', 'h:5000', None, 5, 'http://ex ample.org', 'http://h\n:5000', 'http://-bad-.org',
    'https://moteur../x', ' http://h:5000', 'http://h:5000\n', 'http://h:5000\u2028', 'http://h:5000\u00a0',
])
def test_destination_refusals(url):
    with pytest.raises(OutboundError):
        outbound.destination_of_url(url)


# --- Un hôte n'est autorisé que pour son service (CA-42) -----------------------------------------------------------

def test_an_entry_authorizes_its_own_service_only_and_the_exact_port():
    allow = Allowlist.parse('dem=h:5000')
    assert allow.allows('dem', 'http://h:5000/v1/x') and allow.allows('dem', 'https://h:5000')
    assert not allow.allows('smtp', 'smtp://h:5000') and not allow.allows('smtp', 'http://h:5000')
    assert not allow.allows('routing', 'http://h:5000')
    assert not allow.allows('dem', 'http://h:5001') and not allow.allows('dem', 'http://other:5000')
    assert not allow.allows('dem', 'http://h') and not allow.allows('tiles', 'http://h:5000')      # 80 ≠ 5000
    assert not allow.allows('dem', 'not a url') and not allow.allows('inconnu', 'http://h:5000')


def test_the_default_scheme_port_counts_as_the_port():
    assert Allowlist.parse('routing=moteur:443').allows('routing', 'https://moteur')
    assert not Allowlist.parse('routing=moteur:443').allows('routing', 'http://moteur')
    assert Allowlist.parse('routing=moteur:80').allows('routing', 'http://moteur')


def test_an_env_destination_is_implicitly_allowed_for_its_own_service_only():
    cfg = {'DEM_URL': 'http://env-dem:5000/v1/x', 'ROUTING_BASE_URL': 'http://env-routing:8989', 'OUTBOUND_ALLOWED_HOSTS': ''}
    assert outbound.allowed(cfg, 'dem', 'http://env-dem:5000')
    assert not outbound.allowed(cfg, 'routing', 'http://env-dem:5000')           # DEM_URL d'env n'autorise pas le routage
    assert not outbound.allowed(cfg, 'smtp', 'smtp://env-dem:5000')
    assert outbound.allowed(cfg, 'routing', 'https://env-routing:8989') is True    # même hôte et port, autre schéma : ok
    assert not outbound.allowed({'OUTBOUND_ALLOWED_HOSTS': ''}, 'dem', 'http://env-dem:5000')


def test_a_bare_config_builds_its_allowlist_from_the_variable():
    assert outbound.allowed({'OUTBOUND_ALLOWED_HOSTS': 'dem=h:5000'}, 'dem', 'http://h:5000')
    assert not outbound.allowed({}, 'dem', 'http://h:5000')


# --- Fonds de carte et CSP (R1, CA-41) ----------------------------------------------------------------------------

def test_default_tile_hosts_are_those_of_the_embedded_basemaps():
    hosts = Allowlist().tile_hosts()
    assert hosts == outbound.DEFAULT_TILE_HOSTS and 'server.arcgisonline.com' in hosts
    assert {h for h in hosts if 'openstreetmap' in h} == {f'{c}.tile.openstreetmap.org' for c in 'abc'}


def test_the_first_tiles_entry_replaces_the_default_list():
    allow = Allowlist.parse('tiles=tuiles.example.org')
    assert allow.tile_hosts() == {'tuiles.example.org'}
    assert not (allow.tile_hosts() & outbound.DEFAULT_TILE_HOSTS)


def test_every_embedded_basemap_is_covered_by_the_default_list():
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / 'static' / 'js'
    urls = set()
    for name in ('viewer/map.js', 'segments.js'):
        urls |= set(re.findall(r"tileLayer\('([^']+)'", (root / name).read_text(encoding='utf-8')))
    assert len(urls) >= 5
    for url in urls:
        outbound.check_tile_template(Allowlist(), url, 'abc')


def test_tile_templates_expand_subdomains_and_are_checked_against_the_tiles_entries():
    allow = Allowlist.parse('tiles=a.t.example.org,tiles=b.t.example.org')
    assert outbound.check_tile_template(allow, 'https://{s}.t.example.org/{z}/{x}/{y}.png', 'ab') == \
        {'a.t.example.org', 'b.t.example.org'}
    with pytest.raises(OutboundError, match='non autorisé'):
        outbound.check_tile_template(allow, 'https://{s}.t.example.org/{z}/{x}/{y}.png', 'abc')     # c.t... hors liste
    with pytest.raises(OutboundError, match='non autorisé'):
        outbound.check_tile_template(allow, 'https://attaquant.example/{z}/{x}/{y}.png')


@pytest.mark.parametrize('template,subdomains', [
    ('http://a.t.example.org/{z}/{x}/{y}.png', 'abc'), ('https://u:p@a.t.example.org/{z}/{x}/{y}.png', 'abc'),
    ('https://a.t.example.org/{z}/{x}.png', 'abc'), ('https://a.t.example.org:8443/{z}/{x}/{y}.png', 'abc'),
    ('https://{s}.t.example.org/{z}/{x}/{y}.png', ''), ('https://{s}.t.example.org/{z}/{x}/{y}.png', 'abcde'),
    ('https://{s}.t.example.org/{z}/{x}/{y}.png', 'a1'), ('https://{s}.t.example.org/{z}/{x}/{y}.png', 'aa'),
    ('https://t.example.org/' + 'x' * 500 + '/{z}/{x}/{y}.png', 'abc'), ('', 'abc'), (None, 'abc'),
    ('https://a.t.example.org/{z}/{x}/{y} .png', 'abc'),
])
def test_invalid_tile_templates(template, subdomains):
    with pytest.raises(OutboundError):
        outbound.tile_hosts_of_template(template, subdomains)


def csp_of(app):
    return app.test_client().get('/api/health').headers['Content-Security-Policy']


def img_src(csp):
    return next(d for d in csp.split('; ') if d.startswith('img-src')).split()[1:]


def test_csp_img_src_lists_exactly_the_default_tile_hosts_and_no_longer_https(tmp_path):
    sources = img_src(csp_of(_app(tmp_path)))
    assert sources[:3] == ["'self'", 'data:', 'blob:'] and 'https:' not in sources
    assert sorted(sources[3:]) == sorted(f'https://{h}' for h in outbound.DEFAULT_TILE_HOSTS)


def test_csp_img_src_follows_the_tiles_entries(tmp_path):
    sources = img_src(csp_of(_app(tmp_path, OUTBOUND_ALLOWED_HOSTS='tiles=tuiles.example.org,tiles=autres.example.org')))
    assert sources == ["'self'", 'data:', 'blob:', 'https://autres.example.org', 'https://tuiles.example.org']


def test_the_rest_of_the_csp_is_unchanged(tmp_path):
    csp = csp_of(_app(tmp_path))
    for directive in ("default-src 'self'", "script-src 'self'", "connect-src 'self'", "object-src 'none'",
                      "frame-ancestors 'none'", "form-action 'self'"):
        assert directive in csp


# --- Réglages de destination : écriture refusée, secrets liés effacés (CA-34, CA-36) -------------------------------------

@pytest.fixture
def stack(tmp_path):
    """Base + store de réglages + magasin de secrets branchés comme dans l'application."""
    path = str(tmp_path / 'o.sqlite3')
    database.init_db(path)
    conn = database.connect(path)
    conn.execute("INSERT INTO users (sub, username) VALUES ('s', 'admin')")
    conn.commit()
    cfg = {'OUTBOUND_ALLOWED_HOSTS': 'routing=moteur:8989,routing=autre:8989,dem=dem:5000', 'DEM_URL': '', 'ROUTING_BASE_URL': ''}
    store = SettingsStore(env=cfg.get)
    store.destination_allowed = lambda service, destination: outbound.allowed(cfg, service, destination)
    store.secrets = SecretsStore(KEY)
    store.refresh(conn, force=True)
    yield conn, store, cfg
    conn.close()


def secret_rows(conn):
    return [r['name'] for r in conn.execute("SELECT name FROM secrets")]


def test_a_destination_outside_the_list_is_refused_on_write_and_nothing_is_written(stack):
    conn, store, _ = stack
    for key, value in (('routing.base_url', 'http://attaquant.example:8989'), ('DEM_URL', 'http://dem:5001/v1'),
                       ('DEM_URL', 'http://autre-hote:5000'), ('routing.base_url', 'http://dem:5000')):
        with pytest.raises(SettingsError) as exc:
            store.set_many(conn, {key: value}, actor=ADMIN)
        assert exc.value.field == key and 'OUTBOUND_ALLOWED_HOSTS' in exc.value.message
    assert conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 0
    assert store.get('routing.base_url') == ''


def test_an_allowed_destination_is_written_and_a_batch_is_all_or_nothing(stack):
    conn, store, _ = stack
    store.set_many(conn, {'routing.base_url': 'http://moteur:8989/', 'DEM_URL': 'http://dem:5000/v1/glo30'}, actor=ADMIN)
    assert store.get('routing.base_url') == 'http://moteur:8989' and store.get('DEM_URL') == 'http://dem:5000/v1/glo30'
    with pytest.raises(SettingsError):
        store.set_many(conn, {'routing.base_url': 'http://autre:8989', 'DEM_URL': 'http://mauvais:5000'}, actor=ADMIN)
    assert store.get('routing.base_url') == 'http://moteur:8989'


def test_the_env_destination_may_be_written_even_without_an_entry(stack):
    conn, store, cfg = stack
    cfg['DEM_URL'] = 'http://env-dem:5000/v1/x'
    store.set_many(conn, {'DEM_URL': 'http://env-dem:5000/v1/y'}, actor=ADMIN)
    assert store.get('DEM_URL') == 'http://env-dem:5000/v1/y'


@pytest.mark.parametrize('bad', ['ftp://dem:5000', 'http://u:p@dem:5000', 'dem:5000', 'http://dem:5000 /x', 'http://dem:5000#f',
                                 'x' * 501, 5, None, ['http://dem:5000']])
def test_url_settings_are_validated_by_the_registry(stack, bad):
    conn, store, _ = stack
    with pytest.raises(SettingsError):
        store.set_many(conn, {'DEM_URL': bad}, actor=ADMIN)


def test_changing_the_destination_clears_the_linked_secret_in_the_same_transaction(stack):
    """CA-34 : changer routing.base_url efface routing.api_key (audit)."""
    conn, store, _ = stack
    store.set_many(conn, {'routing.base_url': 'http://moteur:8989'}, actor=ADMIN)
    store.secrets.set(conn, 'routing.api_key', 'cle-de-routage-123456', outbound.destination_of_url('http://moteur:8989'), actor=ADMIN)
    store.secrets.set(conn, 'smtp.password', 'mot-de-passe-smtp', 'smtp://relais:587', actor=ADMIN)
    store.set_many(conn, {'routing.base_url': 'http://autre:8989'}, actor=ADMIN)
    assert secret_rows(conn) == ['smtp.password']                              # seul le secret lié est effacé
    events = [r['action'] for r in conn.execute("SELECT action FROM audit_log WHERE action LIKE 'secret.%' ORDER BY id")]
    assert events[-1] == 'secret.clear_on_destination_change'
    assert 'cle-de-routage' not in json.dumps([dict(r) for r in conn.execute("SELECT * FROM audit_log")])


def test_the_secret_is_kept_when_the_destination_does_not_change(stack):
    conn, store, _ = stack
    store.set_many(conn, {'routing.base_url': 'http://moteur:8989/v1'}, actor=ADMIN)
    store.secrets.set(conn, 'routing.api_key', 'cle-de-routage-123456', 'http://moteur:8989', actor=ADMIN)
    store.set_many(conn, {'routing.base_url': 'http://moteur:8989/v2'}, actor=ADMIN)            # même hôte et port : même destination
    assert secret_rows(conn) == ['routing.api_key']
    store.set_many(conn, {'routing.base_url': 'https://moteur:8989'}, actor=ADMIN)               # autre schéma : autre destination
    assert secret_rows(conn) == []


def test_the_change_and_the_clearing_are_one_transaction(stack, monkeypatch):
    conn, store, _ = stack
    store.set_many(conn, {'routing.base_url': 'http://moteur:8989'}, actor=ADMIN)
    store.secrets.set(conn, 'routing.api_key', 'cle-de-routage-123456', 'http://moteur:8989', actor=ADMIN)
    monkeypatch.setattr('app.settings.store.audit.record_setting_change', lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    with pytest.raises(RuntimeError):
        store.set_many(conn, {'routing.base_url': 'http://autre:8989'}, actor=ADMIN)
    assert secret_rows(conn) == ['routing.api_key'] and store.get('routing.base_url') == 'http://moteur:8989'


def test_reset_to_another_effective_destination_clears_the_secret_too(stack):
    """EF-5.5 : settings-reset compris."""
    conn, store, cfg = stack
    cfg['ROUTING_BASE_URL'] = 'http://env-moteur:8989'
    store.set_many(conn, {'routing.base_url': 'http://moteur:8989'}, actor=ADMIN)
    store.secrets.set(conn, 'routing.api_key', 'cle-de-routage-123456', 'http://moteur:8989', actor=ADMIN)
    store.reset(conn, 'routing.base_url', actor=Actor.cli())                  # retombe sur l'env : autre destination
    assert secret_rows(conn) == [] and store.get('routing.base_url') == 'http://env-moteur:8989'


def test_reset_without_effective_change_keeps_the_secret(stack):
    conn, store, _ = stack
    store.set_many(conn, {'routing.base_url': 'http://moteur:8989'}, actor=ADMIN)
    store.secrets.set(conn, 'routing.api_key', 'cle-de-routage-123456', 'http://moteur:8989', actor=ADMIN)
    store.set_many(conn, {'DEM_URL': 'http://dem:5000'}, actor=ADMIN)
    store.reset(conn, 'DEM_URL', actor=Actor.cli())                           # DEM_URL n'a aucun secret lié
    assert secret_rows(conn) == ['routing.api_key']


def test_clearing_needs_no_key_so_the_rescue_cli_can_do_it(stack, tmp_path):
    conn, store, _ = stack
    store.set_many(conn, {'routing.base_url': 'http://moteur:8989'}, actor=ADMIN)
    store.secrets.set(conn, 'routing.api_key', 'cle-de-routage-123456', 'http://moteur:8989', actor=ADMIN)
    store.secrets.degraded = 1                                                # CLI de secours : clé perdue
    store.set_many(conn, {'routing.base_url': 'http://autre:8989'}, actor=Actor.cli())
    assert secret_rows(conn) == []


# --- Usage : destination forcée en base hors liste (CA-36) ---------------------------------------------------------------

def forced_app(tmp_path, url, **overrides):
    app = _app(tmp_path, **overrides)
    conn = database.connect(app.config['DB_PATH'])
    conn.execute("INSERT INTO settings (key, value, updated_at, updated_by) VALUES ('DEM_URL', ?, 'x', 'sql')", (json.dumps(url),))
    conn.execute("UPDATE settings_meta SET version = version + 1")
    conn.commit()
    conn.close()
    app.config['SETTINGS_STORE'].refresh(database.connect(app.config['DB_PATH']), force=True)
    return app


def test_a_url_forced_in_the_database_outside_the_list_disables_the_function_and_makes_no_call(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(dem_service.requests.Session, 'get', lambda self, url, **kw: calls.append(url))
    app = forced_app(tmp_path, 'http://attaquant.example:5000/v1/x')
    assert dem_service.dem_url(app.config) == 'http://attaquant.example:5000/v1/x'
    assert dem_service.is_enabled(app.config) is False
    with pytest.raises(dem_service.DemError):
        dem_service.fetch_elevations([45.0, 45.001], [6.0, 6.0], app.config)
    assert calls == []                                                          # aucune requête sortante


def test_the_same_url_is_used_when_the_entry_exists_or_comes_from_the_env(tmp_path):
    assert dem_service.is_enabled(forced_app(tmp_path / 'a', 'http://h:5000/v1', OUTBOUND_ALLOWED_HOSTS='dem=h:5000').config)
    app = _app(tmp_path / 'b', DEM_URL='http://env:5000/v1')
    assert dem_service.is_enabled(app.config)                                   # env : autorisée pour dem sans entrée


def test_refusals_at_use_are_audited_once_per_window_even_when_the_background_task_repeats(tmp_path, monkeypatch):
    """Dédoublonnage : un sondage de fond répété ne doit pas inonder le journal."""
    monkeypatch.setattr(outbound, '_refusals', RefusalLog(window_s=3600))
    app = forced_app(tmp_path, 'http://attaquant.example:5000/v1/x')
    clock, scheduler = [1000.0], None

    class Lock:
        held = True
        def try_acquire(self): return True
        def release(self): pass

    scheduler = Scheduler(app.config, default_tasks(), clock=lambda: clock[0], lock=Lock())
    for _ in range(40):
        clock[0] += 400
        scheduler.tick()
    conn = database.connect(app.config['DB_PATH'])
    rows = [dict(r) for r in conn.execute("SELECT * FROM audit_log WHERE action = 'outbound.refused'")]
    assert len(rows) <= 5 and rows                                           # 40 passes : une ligne par heure au plus
    assert (rows[0]['actor_type'], rows[0]['target_id']) == ('system', 'dem')
    assert json.loads(rows[0]['details']) == {'destination': 'http://attaquant.example:5000'}


def test_refusal_log_windows_and_memory_bound():
    clock = [0.0]
    log = RefusalLog(window_s=10, max_entries=3, clock=lambda: clock[0])
    assert log.should_record('dem', 'a') and not log.should_record('dem', 'a')
    assert log.should_record('dem', 'b') and log.should_record('routing', 'a')
    clock[0] = 11
    assert log.should_record('dem', 'a')                                      # fenêtre écoulée
    for i in range(50):
        log.should_record('dem', f'x{i}')
    assert len(log._seen) <= 3                                               # mémoire bornée


def test_refuse_truncates_and_cleans_the_destination_it_writes(tmp_path):
    path = str(tmp_path / 'r.sqlite3')
    database.init_db(path)
    conn = database.connect(path)
    outbound.refuse(conn, 'dem', 'http://h\n:5000/' + 'x' * 500, log=RefusalLog())
    shown = json.loads(conn.execute("SELECT details FROM audit_log").fetchone()[0])['destination']
    assert '\n' not in shown and len(shown) <= 200
    conn.close()


def test_guard_raises_and_audits(tmp_path):
    path = str(tmp_path / 'g.sqlite3')
    database.init_db(path)
    conn = database.connect(path)
    cfg = {'OUTBOUND_ALLOWED_HOSTS': 'dem=h:5000'}
    outbound.guard(conn, cfg, 'dem', 'http://h:5000/x')
    with pytest.raises(outbound.DestinationRefused) as exc:
        outbound.guard(conn, cfg, 'dem', 'http://autre:5000')
    assert str(exc.value) == 'Destination non autorisée' and 'autre' not in str(exc.value)
    conn.close()


# --- « Tester » neutre (CA-37, r4) -----------------------------------------------------------------------------------------

def timed(probe, timeout):
    started = time.monotonic()
    result = outbound.run_neutral_test(probe, timeout)
    return result, time.monotonic() - started


@pytest.mark.parametrize('probe,ok', [
    (lambda t: True, True), (lambda t: False, False), (lambda t: (_ for _ in ()).throw(ConnectionRefusedError("refused")), False),
    (lambda t: time.sleep(3), False),
])
def test_neutral_test_has_the_same_shape_and_the_same_duration_whatever_the_probe_does(probe, ok):
    result, elapsed = timed(probe, 0.5)
    assert result == {'ok': ok} and set(result) == {'ok'}
    assert 0.45 <= elapsed <= 0.65                                              # durée = délai du service, à 10-20 % près


def test_neutral_test_never_leaks_a_message_or_a_code():
    result, _ = timed(lambda t: (_ for _ in ()).throw(RuntimeError("503 Service Unavailable http://interne:8989")), 0.1)
    assert json.dumps(result) == '{"ok": false}'


def test_neutral_test_with_injected_clock_sleeps_exactly_the_remainder():
    slept = []
    clock = [0.0]
    result = outbound.run_neutral_test(lambda t: True, 5.0, clock=lambda: clock[0], sleep=slept.append)
    assert result == {'ok': True} and slept == [5.0]


# --- Types url / email du registre (T3-2) --------------------------------------------------------------------------------

def test_url_and_email_entries_need_their_bounds():
    for kwargs in (dict(type='url', max_length=500), dict(type='url', schemes=('https',)), dict(type='email')):
        with pytest.raises(ValueError):
            Setting('x.u', section='s', label='l', default='', **kwargs)
    Setting('x.u', 'url', '', 's', 'l', max_length=100, schemes=('https',))
    Setting('x.e', 'email', '', 's', 'l', max_length=254)


@pytest.mark.parametrize('value,ok', [
    ('', True), ('a@example.org', True), ('prenom.nom+tag@sous.example.org', True), ('a@b', True),
    ('a b@c.d', False), ('a@b@c', False), ('<a@b.c>', False), ('a@b.c,d@e.f', False), ('a@b.c;d@e.f', False),
    ('"a"@b.c', False), ('a@b.c\nBcc: x@y.z', False), ('@b.c', False), ('a@', False), ('a..b@c.d', False),
    ('x' * 65 + '@b.c', False), ('a@' + 'x' * 254, False), (5, False), (None, False),
])
def test_email_settings(value, ok):
    setting = Setting('x.e', 'email', '', 's', 'l', max_length=400)
    if ok:
        assert setting.validate(value) == value
    else:
        with pytest.raises(ValueError):
            setting.validate(value)


def test_url_setting_validation_details():
    setting = Setting('x.u', 'url', '', 's', 'l', max_length=60, schemes=('https',))
    assert setting.validate('https://h/p/') == 'https://h/p' and setting.validate('') == ''
    for bad in ('http://h', 'https://', 'https://u@h', 'https://h:99999', 'https://h#f', ' https://h', 'https://h x', 'x' * 61):
        with pytest.raises(ValueError):
            setting.validate(bad)
