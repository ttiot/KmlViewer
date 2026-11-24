"""
Cas limites des destinations sortantes (T18 : EF-6.6, EF-6.8, B3, R1, ADR-0011 ; CA-34 à CA-37, CA-41, CA-42, CA-52).

Contournements de la liste blanche (userinfo, casse, point final, IPv6, ports, schémas, redirection, formes
décimale / octale / IDN, gabarits de tuiles), aucun appel réseau hors liste (socket simulé), CSP réellement émise,
secret d'env d'un autre service, effacement du secret sous erreur, RefusalLog, « Tester » neutre.
"""

import http.server
import json
import re
import socket
import threading
import time
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app import create_app, outbound
from app import db as database
from app.audit import Actor
from app.background import Scheduler, default_tasks
from app.config import TestingConfig
from app.outbound import Allowlist, OutboundError, RefusalLog
from app.secrets_store import SecretsStore, SecretUnavailable
from app.services import dem_service
from app.settings import SettingsError, SettingsStore

KEY = Fernet.generate_key().decode()
ADMIN = Actor.user(1, 'admin')
ROOT = Path(__file__).resolve().parents[1]
CFG = {'OUTBOUND_ALLOWED_HOSTS': 'dem=allowed.example:5000,routing=[fd00::1]:8989,smtp=relay.example:587,tiles=tiles.example',
       'DEM_URL': '', 'ROUTING_BASE_URL': ''}
IP_CFG = {'OUTBOUND_ALLOWED_HOSTS': 'dem=127.0.0.1:5000', 'DEM_URL': '', 'ROUTING_BASE_URL': ''}


def ok(service, url, cfg=CFG):
    return outbound.allowed(cfg, service, url)


# --- Contournements de la liste blanche : l'hôte et le port exacts, ou rien -------------------------------------------

@pytest.mark.parametrize('url', ['http://allowed.example:5000', 'https://allowed.example:5000/v1/x?a=b#frag', 'HTTP://ALLOWED.EXAMPLE:5000',
                                 'http://Allowed.Example:5000', 'http://allowed.example.:5000', 'http://allowed.example:05000'])
def test_the_listed_destination_is_accepted_whatever_the_case_path_query_or_one_trailing_dot(url):
    assert ok('dem', url) is True


@pytest.mark.parametrize('url', [
    # identifiants dans l'URL : l'hôte réel serait celui d'après l'arobase
    'http://user@allowed.example:5000', 'http://:pw@allowed.example:5000', 'http://allowed.example:5000@evil.test',
    'http://allowed.example@evil.test:5000', 'http://allowed.example:5000\\@evil.test/', 'http://evil.test\\@allowed.example:5000',
    'http://allowed.example%40evil.test:5000', 'http://%61llowed.example:5000',
    # hôtes voisins
    'http://sub.allowed.example:5000', 'http://xallowed.example:5000', 'http://allowed.example.evil.test:5000',
    'http://allowed.example-evil.test:5000', 'http://evil-allowed.example:5000', 'http://allowed.example\\.evil.test:5000/',
    'http://allowed.example%2eevil.test:5000', 'http://allowed.example%00.evil.test:5000',
    # caractères d'apparence identique, IDN
    'http://ａllowed.example:5000', 'http://allowed。example:5000', 'http://allowed.exаmple:5000', 'http://bücher.example:5000',
    'http://xn--allowed-.example:5000', 'http://[allowed.example]:5000',
    # ports
    'http://allowed.example:5001', 'http://allowed.example:0', 'http://allowed.example:65536', 'http://allowed.example:99999',
    'http://allowed.example:-1', 'http://allowed.example:5000.0', 'http://allowed.example:+5000', 'http://allowed.example:５０００',
    'http://allowed.example:5000:5000', 'http://allowed.example', 'https://allowed.example', 'http://allowed.example:', 'http://allowed.example:/x',
    'http://allowed.example:5000000000000000000000',
    # schémas et formes non absolues
    'ftp://allowed.example:5000', 'file://allowed.example:5000/', 'gopher://allowed.example:5000', 'javascript:alert(1)',
    'data:text/html,x', '//allowed.example:5000', 'allowed.example:5000', 'http:allowed.example:5000', 'http:/allowed.example:5000',
    'http:///allowed.example:5000', 'http://:5000', 'http://', 'http', '',
    # séparateurs et caractères de contrôle
    'http://allowed.example:5000\r\nHost: evil.test', 'http://allowed.example:5000\x00', 'http://allowed.example\n:5000',
    'http://allowed .example:5000', 'http://allowed.example:5000/\x7f',
])
def test_every_bypass_attempt_is_refused(url):
    assert ok('dem', url) is False, url


@pytest.mark.parametrize('value', [None, 5, 5.5, b'http://allowed.example:5000', ['http://allowed.example:5000'],
                                   {'url': 'http://allowed.example:5000'}, True, object()])
def test_non_string_destinations_are_refused_without_raising(value):
    assert outbound.allowed(CFG, 'dem', value) is False


def test_a_gigantic_destination_is_refused_quickly():
    started = time.monotonic()
    assert ok('dem', 'http://' + 'a' * 5_000_000 + '.example:5000') is False
    assert ok('dem', 'http://allowed.example:5000/' + 'x' * 5_000_000) in (True, False)
    assert time.monotonic() - started < 5


@pytest.mark.parametrize('url', ['http://[fd00::1]:8989', 'http://[FD00::1]:8989', 'http://[fd00:0:0:0:0:0:0:1]:8989', 'http://[fd00::0001]:8989'])
def test_ipv6_in_any_spelling_matches_its_canonical_form(url):
    assert ok('routing', url) is True


@pytest.mark.parametrize('url', ['http://fd00::1:8989', 'http://[fd00::1]:8990', 'http://[fd00::1]', 'http://[fd00::1%25eth0]:8989',
                                 'http://[fd00::1%eth0]:8989', 'http://[fd00::2]:8989', 'http://[::ffff:fd00:1]:8989', 'http://[fd00::1:8989',
                                 'http://fd00::1]:8989', 'http://[fd00::1]:8989@evil.test', 'http://[fd00::1]@[::1]:8989',
                                 'http://[fd00::1]:8989\\@[::1]', 'http://[[fd00::1]]:8989', 'http://[]:8989', 'http://[fd00::g]:8989'])
def test_ipv6_bypass_attempts_are_refused(url):
    assert ok('routing', url) is False


@pytest.mark.parametrize('url', ['http://127.1:5000', 'http://0x7f.0.0.1:5000', 'http://0x7f000001:5000', 'http://2130706433:5000',
                                 'http://017700000001:5000', 'http://0177.0.0.1:5000', 'http://127.000.000.001:5000',
                                 'http://[::ffff:127.0.0.1]:5000', 'http://[::ffff:7f00:1]:5000', 'http://localhost:5000', 'http://0.0.0.0:5000',
                                 'http://127.0.0.2:5000', 'http://127.0.0.1%00:5000', 'http://127.0.0.1:5000@127.0.0.2', 'http://0:5000'])
def test_alternative_spellings_of_an_ip_are_never_treated_as_the_listed_ip(url):
    """Comparaison sur le NOM : toute autre écriture (décimale, octale, hexadécimale, mappée IPv6) est refusée."""
    assert ok('dem', url, IP_CFG) is False


def test_the_listed_ip_is_accepted_only_in_its_canonical_ipv4_form():
    assert ok('dem', 'http://127.0.0.1:5000', IP_CFG) is True and ok('dem', 'https://127.0.0.1:5000/x', IP_CFG) is True


@pytest.mark.parametrize('url', ['http://.allowed.example:5000', 'http://allowed.example..:5000', 'http://allowed.example...:5000', 'http://allowed.example...:5000/',
                                 'http://..allowed.example:5000', 'http://allowed..example:5000'])
def test_malformed_trailing_or_leading_dots_are_refused(url):
    assert ok('dem', url) is False


@pytest.mark.parametrize('url', ['http://allowed.example:5000 ', '\thttp://allowed.example:5000', ' http://allowed.example:5000\n',
                                 'http://allowed.example:5000\u2028', '\u00a0http://allowed.example:5000', ' http://allowed.example:5000'])
def test_whitespace_around_the_url_is_not_silently_trimmed(url):
    assert ok('dem', url) is False


@pytest.mark.parametrize('service,url', [
    ('smtp', 'smtp://allowed.example:5000'), ('routing', 'http://allowed.example:5000'), ('tiles', 'https://tiles.example:443'),
    ('dem', 'smtp://relay.example:587'), ('dem', 'http://relay.example:587'), ('routing', 'http://[fd00::1]:8989/x') if False else ('smtp', 'smtp://[fd00::1]:8989'),
    ('inconnu', 'http://allowed.example:5000'), ('', 'http://allowed.example:5000'), (None, 'http://allowed.example:5000'),
])
def test_an_entry_never_authorizes_another_service(service, url):
    assert ok(service, url) is False


def test_an_smtp_destination_needs_the_exact_host_and_port_and_the_smtp_form():
    assert ok('smtp', 'smtp://relay.example:587') is True
    for bad in ('smtp://relay.example:25', 'smtp://relay.example', 'smtp://relay.example:587/x', 'smtp://user@relay.example:587',
                'smtp://relay.example.evil.test:587', 'smtp://RELAY.example:587:1', 'smtps://relay.example:587', 'smtp://:587'):
        assert ok('smtp', bad) is False, bad


# --- Analyse de la variable : ce qui est refusé ou normalisé -----------------------------------------------------------

@pytest.mark.parametrize('text', ['dem=h:0', 'dem=h:65536', 'dem=h:-1', 'dem=h:+5', 'dem=h:5.0', 'dem=h:', 'dem=:80', 'dem=h', 'dem=h:80:90',
                                  'dem=u@h:80', 'dem=http://h:80', 'dem=h/x:80', 'dem=h .evil:80', 'dem=h:80 x', 'tiles=h:443', 'tiles=*.example.com',
                                  'tiles=bücher.example', 'tiles=h/x', 'tiles=https://h', 'tiles=', 'tiles=h..', 'tiles=.h', 'smtp=[::1]', 'smtp=[::1:25',
                                  'smtp=::1:25', 'routing=[fd00::1]:8989x', 'routing=[fd00::g]:8989', 'routing=[zz]:1', 'routing=[fd00::1%eth0]:80',
                                  '=h:80', 'dem', 'dem=h:80;smtp=h:25', 'dem=h:80\nsmtp=h:25', 'DEM2=h:80', 'dem==h:80', 'dem=h:80,,dem'])
def test_malformed_entries_stop_the_start(text):
    with pytest.raises(OutboundError):
        Allowlist.parse(text)


def test_the_variable_is_read_case_insensitively_for_the_service_and_tolerates_blanks_and_duplicates():
    parsed = Allowlist.parse(' DEM = h:80 , dem=h:80 , Smtp=[::1]:25 , ,TILES=Tiles.Example ')
    assert parsed.servers['dem'] == {('h', 80)} and parsed.servers['smtp'] == {('::1', 25)} and parsed.explicit_tiles == {'tiles.example'}


def test_leading_zeros_and_unicode_digits_in_a_port_never_change_its_value():
    for text in ('dem=h:00080', 'dem=h:８０'):
        try:
            assert Allowlist.parse(text).servers['dem'] == {('h', 80)}
        except OutboundError:
            pass                                                                       # refusé : tout aussi sûr


def test_a_huge_variable_is_parsed_in_bounded_time():
    text = ','.join(f'dem=h{i}.example:{1 + i % 65000}' for i in range(20_000))
    started = time.monotonic()
    assert len(Allowlist.parse(text).servers['dem']) > 10_000
    assert time.monotonic() - started < 5


def test_no_entry_means_only_the_env_destinations_and_the_default_tiles():
    empty = {'OUTBOUND_ALLOWED_HOSTS': '', 'DEM_URL': 'http://envdem:5000/v1', 'ROUTING_BASE_URL': ''}
    assert outbound.allowed(empty, 'dem', 'http://envdem:5000') is True
    for service in ('routing', 'smtp'):
        assert outbound.allowed(empty, service, 'http://envdem:5000') is False
    assert Allowlist.parse('').tile_hosts() == outbound.DEFAULT_TILE_HOSTS


# --- Secret et destination d'env : un service ne prête pas son env à un autre -----------------------------------------------

def test_an_env_destination_authorizes_its_own_service_only_even_on_the_same_host_and_port():
    cfg = {'OUTBOUND_ALLOWED_HOSTS': '', 'DEM_URL': 'http://shared:5000/v1', 'ROUTING_BASE_URL': 'http://moteur:8989'}
    assert outbound.allowed(cfg, 'dem', 'http://shared:5000') and outbound.allowed(cfg, 'routing', 'http://moteur:8989')
    assert not outbound.allowed(cfg, 'routing', 'http://shared:5000') and not outbound.allowed(cfg, 'dem', 'http://moteur:8989')
    assert not outbound.allowed(cfg, 'smtp', 'smtp://shared:5000') and not outbound.allowed(cfg, 'smtp', 'smtp://moteur:8989')


def test_a_malformed_env_destination_authorizes_nothing_not_even_itself():
    for bad in ('pas une url', 'ftp://h:1', 'http://u:p@h:80', 'http://h:99999'):
        cfg = {'OUTBOUND_ALLOWED_HOSTS': '', 'DEM_URL': bad, 'ROUTING_BASE_URL': ''}
        assert outbound.allowed(cfg, 'dem', 'http://h:80') is False


def test_an_env_secret_is_only_given_to_the_env_destination_of_the_same_service(tmp_path):
    path = str(tmp_path / 's.sqlite3')
    database.init_db(path)
    conn = database.connect(path)
    secrets = SecretsStore(KEY)
    env_dem, env_routing = outbound.destination_of_url('http://dem:5000'), outbound.destination_of_url('http://moteur:8989')
    assert secrets.effective(conn, 'routing.api_key', env_routing, env_secret='cle-env-123456789', env_destination=env_routing) == 'cle-env-123456789'
    assert secrets.effective(conn, 'routing.api_key', env_dem, env_secret='cle-env-123456789', env_destination=env_routing) is None
    assert secrets.effective(conn, 'routing.api_key', 'smtp://dem:5000', env_secret='cle-env-123456789', env_destination=env_dem) is None
    conn.close()


# --- Changement de destination : le secret part avec, même sous erreur ------------------------------------------------------

@pytest.fixture
def stack(tmp_path):
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
    store.set_many(conn, {'routing.base_url': 'http://moteur:8989'}, actor=ADMIN)
    store.secrets.set(conn, 'routing.api_key', 'cle-de-routage-123456', 'http://moteur:8989', actor=ADMIN)
    store.secrets.set(conn, 'smtp.password', 'mot-de-passe-smtp', 'smtp://relais:587', actor=ADMIN)
    yield conn, store, cfg
    conn.close()


def secret_names(conn):
    return sorted(r['name'] for r in conn.execute("SELECT name FROM secrets"))


def test_if_the_clearing_fails_the_destination_does_not_change(stack, monkeypatch):
    conn, store, _ = stack

    def boom(*a, **k):
        raise RuntimeError("table des secrets verrouillée")

    monkeypatch.setattr(store.secrets, 'clear_linked', boom)
    with pytest.raises(RuntimeError):
        store.set_many(conn, {'routing.base_url': 'http://autre:8989'}, actor=ADMIN)
    assert store.get('routing.base_url') == 'http://moteur:8989' and secret_names(conn) == ['routing.api_key', 'smtp.password']
    row = conn.execute("SELECT bound_to FROM secrets WHERE name = 'routing.api_key'").fetchone()
    assert row['bound_to'] == 'http://moteur:8989' and not conn.in_transaction


def test_an_error_after_the_clearing_undoes_the_clearing_and_the_change(stack):
    conn, store, _ = stack
    with pytest.raises(RuntimeError), database.write_transaction(conn):
        store.set_many(conn, {'routing.base_url': 'http://autre:8989'}, actor=ADMIN)
        assert secret_names(conn) == ['smtp.password']                              # dans la transaction : déjà effacé
        raise RuntimeError("échec plus tard dans la même transaction")
    assert secret_names(conn) == ['routing.api_key', 'smtp.password']
    assert store.get('routing.base_url') == 'http://moteur:8989'


def test_a_refused_or_invalid_write_touches_no_secret(stack):
    conn, store, _ = stack
    for bad in ('http://attaquant:8989', 'ftp://moteur:8989', 'http://u:p@moteur:8989', 'moteur:8989', 'http://moteur:8989 /x'):
        with pytest.raises(SettingsError):
            store.set_many(conn, {'routing.base_url': bad}, actor=ADMIN)
    assert secret_names(conn) == ['routing.api_key', 'smtp.password'] and store.get('routing.base_url') == 'http://moteur:8989'


def test_the_cleared_secret_is_never_given_to_either_destination_afterwards(stack):
    conn, store, _ = stack
    store.set_many(conn, {'routing.base_url': 'http://autre:8989'}, actor=ADMIN)
    for destination in ('http://moteur:8989', 'http://autre:8989'):
        with pytest.raises(SecretUnavailable):
            store.secrets.get(conn, 'routing.api_key', destination)


def test_a_destination_forced_by_raw_sql_does_not_receive_the_secret_bound_to_the_old_one(stack):
    """Contournement de l'API par SQL : la liaison `bound_to` protège quand même (CA-34)."""
    conn, store, _ = stack
    conn.execute("UPDATE settings SET value = ? WHERE key = 'routing.base_url'", (json.dumps('http://attaquant:8989'),))
    conn.execute("UPDATE settings_meta SET version = version + 1")
    conn.commit()
    store.refresh(conn, force=True)
    assert store.get('routing.base_url') == 'http://attaquant:8989'
    with pytest.raises(SecretUnavailable):
        store.secrets.effective(conn, 'routing.api_key', outbound.destination_of_url(store.get('routing.base_url')))
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'secret.refused'").fetchone()[0] >= 1


def test_a_change_that_keeps_the_effective_destination_keeps_the_secret_whatever_the_spelling(stack):
    conn, store, _ = stack
    for spelling in ('http://MOTEUR:8989/v1', 'http://moteur.:8989', 'http://moteur:8989?x=1'):
        store.set_many(conn, {'routing.base_url': spelling}, actor=ADMIN)
        assert 'routing.api_key' in secret_names(conn), spelling


def test_clearing_works_without_the_encryption_key(stack):
    conn, store, _ = stack
    store.secrets = SecretsStore('')                                                  # clé perdue : CLI de secours
    store.set_many(conn, {'routing.base_url': 'http://autre:8989'}, actor=ADMIN)
    assert secret_names(conn) == ['smtp.password']


# --- Aucun appel réseau hors liste (socket simulé) ---------------------------------------------------------------------------

@pytest.fixture
def sockets(monkeypatch):
    """Enregistre toute tentative de connexion et de résolution, et refuse de la faire."""
    seen = {'connect': [], 'resolve': []}

    def connect(self, address, *a):
        seen['connect'].append(address)
        raise ConnectionRefusedError("bloqué par le test")

    def resolve(host, *a, **k):
        seen['resolve'].append(host)
        raise socket.gaierror("bloqué par le test")

    monkeypatch.setattr(socket.socket, 'connect', connect)
    monkeypatch.setattr(socket.socket, 'connect_ex', lambda self, address: seen['connect'].append(address) or 111)
    monkeypatch.setattr(socket, 'getaddrinfo', resolve)
    return seen


def forced_app(tmp_path, url, **overrides):
    app = create_app(TestingConfig, overrides={'DATA_DIR': str(tmp_path / 'data'), **overrides})
    conn = database.connect(app.config['DB_PATH'])
    conn.execute("INSERT INTO settings (key, value, updated_at, updated_by) VALUES ('DEM_URL', ?, 'x', 'sql')", (json.dumps(url),))
    conn.execute("UPDATE settings_meta SET version = version + 1")
    conn.commit()
    app.config['SETTINGS_STORE'].refresh(conn, force=True)
    conn.close()
    return app


@pytest.mark.parametrize('url', ['http://attaquant.example:5000/v1/x', 'http://allowed.example:5001/v1/x', 'http://user@allowed.example:5000/v1',
                                 'http://allowed.example:5000@attaquant.example/v1', 'ftp://allowed.example:5000', 'http://169.254.169.254/latest',
                                 'http://127.0.0.1:5000/v1', 'http://[::1]:5000/v1', 'http://localhost:5000'])
def test_no_socket_is_opened_for_a_destination_outside_the_list(tmp_path, sockets, url):
    app = forced_app(tmp_path, url, OUTBOUND_ALLOWED_HOSTS='dem=allowed.example:5000')
    with pytest.raises(dem_service.DemError):
        dem_service.fetch_elevations([45.0, 45.001], [6.0, 6.0], app.config)
    assert dem_service.is_enabled(app.config) is False
    assert sockets == {'connect': [], 'resolve': []}


def test_the_background_task_and_the_correction_path_open_no_socket_outside_the_list(tmp_path, sockets):
    app = forced_app(tmp_path, 'http://attaquant.example:5000/v1/x')

    class Lock:
        held = True

        def try_acquire(self):
            return True

        def release(self):
            pass

    scheduler = Scheduler(app.config, default_tasks(), lock=Lock(), clock=lambda: 1.0)
    for now in (1.0, 500.0, 1000.0):
        scheduler.tick(now)
    with pytest.raises(dem_service.DemError):
        dem_service.corrected_profile([45.0, 45.001, 45.002], [6.0, 6.0, 6.0], app.config)
    assert sockets == {'connect': [], 'resolve': []}


def test_control_the_mock_works_an_allowed_destination_does_try_to_connect(tmp_path, sockets):
    app = forced_app(tmp_path, 'http://allowed.example:5000/v1/x', OUTBOUND_ALLOWED_HOSTS='dem=allowed.example:5000')
    with pytest.raises(dem_service.DemError):
        dem_service.fetch_elevations([45.0, 45.001], [6.0, 6.0], app.config)
    assert sockets['resolve'] and set(sockets['resolve']) == {'allowed.example'}               # le contrôle simulé voit bien l'appel autorisé


class Recorder(http.server.BaseHTTPRequestHandler):
    hits = []
    target = None

    def do_GET(self):  # noqa: N802
        type(self).hits.append(self.path)
        if type(self).target:
            self.send_response(302)
            self.send_header('Location', type(self).target)
            self.end_headers()
        else:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{}')

    def log_message(self, *args):
        pass


def _server(handler):
    httpd = http.server.HTTPServer(('127.0.0.1', 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


@pytest.mark.parametrize('status', [301, 302, 303, 307, 308])
def test_a_redirection_from_an_allowed_destination_is_never_followed(tmp_path, status):
    class Hop(Recorder):
        hits = []

    class Target(Recorder):
        hits = []
        target = None

    final = _server(Target)
    start = _server(type('Start', (Recorder,), {'hits': [], 'target': f'http://127.0.0.1:{final.server_port}/v1/stolen'}))
    try:
        if status != 302:
            original = Recorder.do_GET

            def do_GET(self):  # noqa: N802
                self.send_response(status)
                self.send_header('Location', f'http://127.0.0.1:{final.server_port}/v1/stolen')
                self.end_headers()
            start.RequestHandlerClass.do_GET = do_GET
            Recorder.do_GET = original
        app = create_app(TestingConfig, overrides={'DATA_DIR': str(tmp_path / 'data'),
                                                   'DEM_URL': f'http://127.0.0.1:{start.server_port}/v1/x'})
        assert dem_service.is_enabled(app.config)
        with pytest.raises(dem_service.DemError):
            dem_service.fetch_elevations([45.0, 45.001], [6.0, 6.0], app.config)
        assert Target.hits == []                                                  # la cible de la redirection n'a jamais été jointe
    finally:
        final.shutdown()
        start.shutdown()


# --- Fonds de carte : gabarits et CSP ---------------------------------------------------------------------------------------

TILES = Allowlist.parse('tiles=a.tiles.example,tiles=b.tiles.example,tiles=c.tiles.example,tiles=single.example')


@pytest.mark.parametrize('template,subs', [
    ('https://{s}.tiles.example/{z}/{x}/{y}.png', 'abc'), ('https://single.example/{z}/{x}/{y}.png', 'abc'),
    ('https://single.example/{z}/{x}/{y}{r}.png?k={s}', 'abc'), ('HTTPS://Single.Example/{z}/{x}/{y}.png', 'abc'),
])
def test_valid_templates_are_accepted(template, subs):
    assert outbound.check_tile_template(TILES, template, subs)


@pytest.mark.parametrize('template,subs', [
    ('https://{s}.tiles.example/{z}/{x}/{y}.png', 'abcd'),                       # d absent de la liste : l'hôte effectif d.tiles.example est refusé
    ('https://{s}.tiles.example/{z}/{x}/{y}.png', 'ab' + 'z'),                   # z.tiles.example
    ('https://{s}/{z}/{x}/{y}.png', 'abc'), ('https://{s}{s}.tiles.example/{z}/{x}/{y}.png', 'abc'),
    ('https://tiles.example/{z}/{x}/{y}.png', 'abc'), ('https://evil.example/{z}/{x}/{y}.png?h=single.example', 'abc'),
    ('https://evil.example/single.example/{z}/{x}/{y}.png', 'abc'), ('https://single.example.evil.test/{z}/{x}/{y}.png', 'abc'),
    ('https://user@single.example/{z}/{x}/{y}.png', 'abc'), ('https://single.example@evil.test/{z}/{x}/{y}.png', 'abc'),
    ('https://single.example\\@evil.test/{z}/{x}/{y}.png', 'abc'), ('http://single.example/{z}/{x}/{y}.png', 'abc'),
    ('https://single.example:8443/{z}/{x}/{y}.png', 'abc'), ('https://single.example:443/{z}/{x}/{y}.png', 'abc') if False else
    ('https://single.example:444/{z}/{x}/{y}.png', 'abc'),
    ('https://[::1]/{z}/{x}/{y}.png', 'abc'), ('https://127.0.0.1/{z}/{x}/{y}.png', 'abc'), ('https://bücher.example/{z}/{x}/{y}.png', 'abc'),
    ('https://{x}.single.example/{z}/{x}/{y}.png', 'abc'), ('https://{z}{r}.tiles.example/{z}/{x}/{y}.png', 'abc'),
    ('https://{attribution}/{z}/{x}/{y}.png', 'abc'), ('https://{foo}.single.example/{z}/{x}/{y}.png', 'abc'),
    ('https://single.example/{z}/{x}.png', 'abc'), ('https://single.example/{z}/{y}.png', 'abc'), ('https://single.example/{x}/{y}.png', 'abc'),
    ('//single.example/{z}/{x}/{y}.png', 'abc'), ('single.example/{z}/{x}/{y}.png', 'abc'), ('', 'abc'), (None, 'abc'),
    ('https://single.example/{z}/{x}/{y}\n.png', 'abc'), ('https://single.example/{z}/{x}/{y} .png', 'abc'),
    ('https://single.example/{z}/{x}/{y}.png' + 'x' * 500, 'abc'),
])
def test_template_bypass_attempts_are_refused(template, subs):
    with pytest.raises(OutboundError):
        outbound.check_tile_template(TILES, template, subs)


@pytest.mark.parametrize('subs', ['', 'abcde', 'aa', 'A', '1', 'ab c', 'a,b', 'é', None, 5, ['a'], 'ab\n'])
def test_invalid_subdomain_sets_are_refused_even_when_the_template_has_no_placeholder(subs):
    with pytest.raises(OutboundError):
        outbound.tile_hosts_of_template('https://single.example/{z}/{x}/{y}.png', subs)


def test_template_length_bound_is_exact():
    base = 'https://single.example/{z}/{x}/{y}.png?'
    assert outbound.tile_hosts_of_template(base + 'x' * (500 - len(base)))
    with pytest.raises(OutboundError):
        outbound.tile_hosts_of_template(base + 'x' * (501 - len(base)))


def test_csp_sources_are_hosts_only_and_cannot_carry_a_directive_separator():
    for host in ('a.tiles.example', 'single.example'):
        assert re.fullmatch(r"https://[a-z0-9.-]+", f"https://{host}")
    for text in ("tiles=a.example;script-src *", "tiles=a.example 'unsafe-inline'", "tiles=a.example,tiles=b.example\nx", 'tiles=a.example"'):
        with pytest.raises(OutboundError):
            Allowlist.parse(text)
    for source in outbound.img_src_hosts(Allowlist.parse('tiles=a.example,tiles=b.example')):
        assert re.fullmatch(r"https://[a-z0-9.-]+", source)


def csp_of(response):
    headers = response.headers.getlist('Content-Security-Policy')
    assert len(headers) == 1, headers
    return {part.split()[0]: part.split()[1:] for part in (p.strip() for p in headers[0].split(';')) if part}


@pytest.mark.parametrize('variable,expected_extra', [
    ('', sorted(f'https://{h}' for h in outbound.DEFAULT_TILE_HOSTS)),
    ('tiles=a.tiles.example,tiles=b.tiles.example', ['https://a.tiles.example', 'https://b.tiles.example']),
    ('dem=h:5000,routing=m:8989,smtp=s:25', sorted(f'https://{h}' for h in outbound.DEFAULT_TILE_HOSTS)),     # pas de tiles= : valeurs par défaut
])
def test_the_csp_really_sent_lists_exactly_the_tile_hosts(tmp_path, variable, expected_extra):
    app = create_app(TestingConfig, overrides={'DATA_DIR': str(tmp_path / 'd'), 'OUTBOUND_ALLOWED_HOSTS': variable})
    client = app.test_client()
    for url in ('/', '/api/health', '/static/favicon.svg', '/api/this-does-not-exist', '/dashboard'):
        response = client.get(url)
        policy = csp_of(response)
        assert policy['img-src'] == ["'self'", 'data:', 'blob:', *expected_extra], (url, policy['img-src'])
        flat = ' '.join(sum(policy.values(), []))
        assert 'https:' not in flat.split() and '*' not in flat.split() and 'http:' not in flat.split()
        assert policy['default-src'] == ["'self'"] and policy['script-src'] == ["'self'"] and policy['object-src'] == ["'none'"]
        assert policy['frame-ancestors'] == ["'none'"] and 'Content-Security-Policy-Report-Only' not in response.headers


def test_the_csp_is_also_sent_with_errors_redirects_and_json_responses(tmp_path):
    from cryptography.fernet import Fernet as F

    class Prod(TestingConfig):
        TESTING = False
        INBOX_ENABLED = False
        SECRET_KEY = 'k' * 40
        SESSION_COOKIE_SECURE = None

    app = create_app(Prod, overrides={'DATA_DIR': str(tmp_path / 'd'), 'AUTH_MODE': 'password', 'TRUSTED_HOSTS': ['traces.example.com'],
                                      'SECRETS_KEY': F.generate_key().decode(), 'PUBLIC_URL': 'https://traces.example.com',
                                      'OUTBOUND_ALLOWED_HOSTS': 'tiles=a.tiles.example'})
    client = app.test_client()
    seen = set()
    for url in ('/', '/api/activities', '/auth/login', '/nonexistent', '/api/health', '/static/favicon.svg', '/static/absent.js'):
        response = client.get(url, base_url='https://traces.example.com')
        seen.add(response.status_code)
        assert csp_of(response)['img-src'] == ["'self'", 'data:', 'blob:', 'https://a.tiles.example'], (url, response.status_code)
    assert {302, 401, 404} <= seen


def test_the_csp_is_computed_once_at_startup_and_does_not_follow_later_env_changes(tmp_path, monkeypatch):
    app = create_app(TestingConfig, overrides={'DATA_DIR': str(tmp_path / 'd'), 'OUTBOUND_ALLOWED_HOSTS': 'tiles=a.tiles.example'})
    app.config['OUTBOUND_ALLOWED_HOSTS'] = 'tiles=evil.example'
    assert csp_of(app.test_client().get('/'))['img-src'][-1] == 'https://a.tiles.example'


def _embedded_templates():
    pattern = re.compile(r"tileLayer\('([^']+)'")
    urls = set()
    for path in (ROOT / 'static' / 'js').rglob('*.js'):
        if 'vendor' in path.parts:
            continue
        urls |= set(pattern.findall(path.read_text(encoding='utf-8')))
    return urls


def test_every_embedded_basemap_still_loads_under_the_default_csp_and_keeps_its_subdomains():
    urls = _embedded_templates()
    assert len(urls) >= 5
    hosts = set()
    for url in urls:
        hosts |= outbound.check_tile_template(Allowlist(), url, 'abc')          # Leaflet : sous-domaines « abc » par défaut
    assert hosts <= outbound.DEFAULT_TILE_HOSTS
    sources = outbound.img_src_hosts(Allowlist())
    assert all(f'https://{h}' in sources for h in hosts)


def test_no_template_or_script_loads_an_image_from_a_host_outside_the_default_csp():
    allowed = {h for h in outbound.DEFAULT_TILE_HOSTS}
    pattern = re.compile(r"""(?:src|href|url)\s*[=(]\s*['"]?(https?://[^'")\s>]+)""", re.I)
    offenders = []
    for path in [*(ROOT / 'templates').rglob('*.html'), *(ROOT / 'static' / 'css').rglob('*.css'),
                 *(p for p in (ROOT / 'static' / 'js').rglob('*.js') if 'vendor' not in p.parts)]:
        for url in pattern.findall(path.read_text(encoding='utf-8', errors='ignore')):
            host = re.match(r'https?://([^/:]+)', url).group(1)
            if host not in allowed and 'w3.org' not in host:
                offenders.append((path.name, url))
    assert offenders == []


# --- RefusalLog ------------------------------------------------------------------------------------------------------------------

def test_refusal_log_window_boundary_is_exact_and_keys_are_independent():
    clock = [0.0]
    log = RefusalLog(window_s=100, clock=lambda: clock[0])
    assert log.should_record('dem', 'http://x:1') is True
    assert log.should_record('dem', 'http://x:1') is False and log.should_record('routing', 'http://x:1') is True
    assert log.should_record('dem', 'http://x:2') is True
    clock[0] = 99.999
    assert log.should_record('dem', 'http://x:1') is False
    clock[0] = 100.0
    assert log.should_record('dem', 'http://x:1') is True and log.should_record('dem', 'http://x:1') is False


def test_refusal_log_memory_stays_bounded_under_a_flood_of_distinct_destinations():
    log = RefusalLog(window_s=3600, max_entries=256)
    for i in range(100_000):
        log.should_record('dem', f'http://flood-{i}.example:80')
    assert len(log._seen) == 256


def test_refusal_log_is_exactly_once_under_concurrency():
    log = RefusalLog(window_s=3600)
    barrier, results = threading.Barrier(16), []

    def hit():
        barrier.wait(10)
        results.append(log.should_record('dem', 'http://same:80'))

    threads = [threading.Thread(target=hit) for _ in range(16)]
    [t.start() for t in threads]
    [t.join(10) for t in threads]
    assert results.count(True) == 1


def test_each_refusal_row_is_system_scoped_cleaned_and_free_of_the_secret_part_of_the_url(tmp_path):
    path = str(tmp_path / 'r.sqlite3')
    database.init_db(path)
    conn = database.connect(path)
    log = RefusalLog()
    outbound.refuse(conn, 'dem', 'http://user:motdepasse@evil.example:80/\x1b[31mpath', log=log)
    row = dict(conn.execute("SELECT * FROM audit_log WHERE action = 'outbound.refused'").fetchone())
    assert (row['actor_type'], row['actor_id'], row['target_id']) == ('system', None, 'dem')
    assert '\x1b' not in row['details'] and len(row['details']) < 400
    conn.close()


def test_a_refusal_whose_audit_row_was_rolled_back_is_audited_again_at_the_next_attempt(tmp_path):
    path = str(tmp_path / 'r.sqlite3')
    database.init_db(path)
    conn = database.connect(path)
    log = RefusalLog(window_s=3600)
    with pytest.raises(RuntimeError), database.write_transaction(conn):
        outbound.refuse(conn, 'dem', 'http://evil.example:80', log=log)
        raise RuntimeError("l'opération englobante échoue")
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'outbound.refused'").fetchone()[0] == 0
    outbound.refuse(conn, 'dem', 'http://evil.example:80', log=log)
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'outbound.refused'").fetchone()[0] == 1
    conn.close()


def test_guard_raises_the_generic_refusal_and_never_the_destination(tmp_path):
    path = str(tmp_path / 'g.sqlite3')
    database.init_db(path)
    conn = database.connect(path)
    with pytest.raises(outbound.DestinationRefused) as exc:
        outbound.guard(conn, CFG, 'dem', 'http://secret-internal-host:9999/v1?token=abc')
    assert 'secret-internal' not in str(exc.value) and 'token' not in str(exc.value) and exc.value.__cause__ is None
    conn.close()


# --- « Tester » neutre : durée constante ------------------------------------------------------------------------------------------

PROBES = {
    'ok-immediat': lambda t: True, 'ko-immediat': lambda t: False, 'exception': lambda t: (_ for _ in ()).throw(ConnectionRefusedError("refus")),
    'jamais': lambda t: time.sleep(5), 'tard-mais-ok': lambda t: (time.sleep(t * 1.5), True)[1], 'valeur-bizarre': lambda t: 'oui',
    'none': lambda t: None, 'exception-message': lambda t: (_ for _ in ()).throw(RuntimeError("503 http://interne:8989")),
}


@pytest.mark.parametrize('timeout', [0.2, 0.6])
def test_every_kind_of_probe_takes_the_service_timeout_within_ten_percent(timeout):
    durations = {}
    for name, probe in PROBES.items():
        started = time.monotonic()
        result = outbound.run_neutral_test(probe, timeout)
        durations[name] = time.monotonic() - started
        assert set(result) == {'ok'} and isinstance(result['ok'], bool), name
    assert all(timeout * 0.95 <= d <= timeout * 1.10 + 0.05 for d in durations.values()), durations
    assert max(durations.values()) - min(durations.values()) <= timeout * 0.10 + 0.05, durations


def test_only_a_fast_true_answer_is_ok_a_late_success_is_ko():
    assert outbound.run_neutral_test(PROBES['ok-immediat'], 0.1) == {'ok': True}
    assert outbound.run_neutral_test(PROBES['tard-mais-ok'], 0.1) == {'ok': False}
    assert outbound.run_neutral_test(PROBES['valeur-bizarre'], 0.1) == {'ok': True}
    assert outbound.run_neutral_test(PROBES['none'], 0.1) == {'ok': False}


def test_a_probe_that_never_answers_does_not_keep_the_caller_waiting_and_leaves_only_a_daemon_thread():
    started = time.monotonic()
    result = outbound.run_neutral_test(lambda t: time.sleep(30), 0.2)
    assert result == {'ok': False} and time.monotonic() - started < 1
    assert all(t.daemon for t in threading.enumerate() if t.name == 'neutral-test')


def test_a_base_exception_in_the_probe_is_a_plain_ko():
    assert outbound.run_neutral_test(lambda t: (_ for _ in ()).throw(SystemExit(1)), 0.1) == {'ok': False}


def test_parallel_tests_do_not_disturb_each_other_or_the_duration():
    results, durations = [], []

    def one(probe):
        started = time.monotonic()
        results.append(outbound.run_neutral_test(probe, 0.3))
        durations.append(time.monotonic() - started)

    threads = [threading.Thread(target=one, args=(p,)) for p in list(PROBES.values()) * 3]
    [t.start() for t in threads]
    [t.join(10) for t in threads]
    assert len(results) == 24 and all(set(r) == {'ok'} for r in results)
    assert all(0.27 <= d <= 0.45 for d in durations), sorted(durations)


@pytest.mark.parametrize('bad', [float('nan'), float('inf'), None, 'x'])
def test_an_out_of_spec_timeout_raises_instead_of_answering_something_misleading(bad):
    with pytest.raises((ValueError, TypeError, OverflowError)):
        outbound.run_neutral_test(lambda t: True, bad, sleep=lambda s: None)


def test_the_injected_clock_makes_the_remainder_exact_for_every_probe_outcome():
    for probe in (lambda t: True, lambda t: False, lambda t: (_ for _ in ()).throw(OSError("x"))):
        slept = []
        outbound.run_neutral_test(probe, 4.0, clock=lambda: 10.0, sleep=slept.append)
        assert slept == [4.0]
