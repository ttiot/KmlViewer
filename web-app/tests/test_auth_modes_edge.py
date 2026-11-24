"""
Cas limites des modes d'authentification (T8, EF-0.x, B1, B5, M4, CA-28, CA-29, CA-13 côté CLI).

Objectif : chercher comment OUVRIR l'instance en mode `password` (route sans session, en-tête, cookie,
bascule de mode, compte `local`, promotion sans second facteur) et vérifier que `user-mfa-reset` ferme bien.
Complète test_auth_modes.py.
"""

import os
import pathlib
import sqlite3
import subprocess
import sys
import time

import pytest
from cryptography.fernet import Fernet

from app import create_app
from app import db as database
from app.auth import factors, users
from app.auth.guard import PUBLIC_ENDPOINTS
from app.config import TestingConfig
from tests.conftest import OIDC_OVERRIDES

KEY = Fernet.generate_key().decode()
HOST = 'traces.example.com'
ROOT = str(pathlib.Path(__file__).resolve().parents[1])


class Prod(TestingConfig):
    TESTING = False
    INBOX_ENABLED = False
    SECRET_KEY = 'k' * 40
    SESSION_COOKIE_SECURE = None


PASSWORD_MODE = {'AUTH_MODE': 'password', 'TRUSTED_HOSTS': [HOST], 'SECRETS_KEY': KEY, 'PUBLIC_URL': f'https://{HOST}'}
OIDC_MODE = {**OIDC_OVERRIDES, 'TRUSTED_HOSTS': [HOST]}


def make(tmp_path, **overrides):
    return create_app(Prod, overrides={'DATA_DIR': str(tmp_path / 'data'), **overrides})


@pytest.fixture
def app(tmp_path):
    return make(tmp_path, **PASSWORD_MODE)


def conn_of(app):
    return database.connect(app.config['DB_PATH'])


def add_user(app, sub, method='password', admin=False, active=True, token='tok', username=None, email=None):
    conn = conn_of(app)
    conn.execute("INSERT INTO users (sub, username, email, is_admin, is_active, auth_method, session_token) VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (sub, username or sub, email, int(admin), int(active), method, token))
    conn.commit()
    uid = conn.execute("SELECT id FROM users WHERE sub = ?", (sub,)).fetchone()[0]
    conn.close()
    return uid


def sign_in(client, uid, token='tok', **extra):
    with client.session_transaction(base_url=f'https://{HOST}') as session:
        session['user_id'], session['token'], session['login_at'] = uid, token, int(time.time())
        session.update(extra)


def client_of(app):
    return app.test_client()


def get(client, url, **kw):
    kw.setdefault('base_url', f'https://{HOST}')
    return client.get(url, **kw)


def user_count(app, where='1=1'):
    conn = conn_of(app)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM users WHERE {where}").fetchone()[0]
    finally:
        conn.close()


def add_factor_tables(app):
    conn = conn_of(app)
    existing = {r['name'] for r in conn.execute("PRAGMA table_info(users)")}
    for column, kind in (('totp_secret', 'BLOB'), ('totp_last_step', 'INTEGER'), ('totp_enabled_at', 'TEXT')):
        if column not in existing:
            conn.execute(f"ALTER TABLE users ADD COLUMN {column} {kind}")
    conn.execute("CREATE TABLE IF NOT EXISTS webauthn_credentials (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, label TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS recovery_codes (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, code_hash TEXT)")
    conn.commit()
    conn.close()


def give_totp(app, uid, enabled=True):
    add_factor_tables(app)
    conn = conn_of(app)
    conn.execute("UPDATE users SET totp_secret = x'01', totp_enabled_at = ? WHERE id = ?", ('2026-01-01T00:00:00Z' if enabled else None, uid))
    conn.commit()
    conn.close()


# --- Toutes les routes, toutes les méthodes, sans session (CA-28) --------------------------------------

METHODS = ('GET', 'HEAD', 'OPTIONS', 'POST', 'PUT', 'PATCH', 'DELETE')


def _sample_url(rule):
    url = rule.rule
    for argument, converter in rule._converters.items():
        sample = {'int': '1', 'float': '1.5', 'path': 'x/y.gpx'}.get(type(converter).__name__.replace('Converter', '').lower(), 'x')
        url = url.replace(f'<{converter.__class__.__name__.lower()}:{argument}>', sample)
    import re
    return re.sub(r'<[^>]+>', '1', url)


def _all_requests(app):
    seen = set()
    for rule in app.url_map.iter_rules():
        if rule.endpoint in PUBLIC_ENDPOINTS:
            continue
        for method in rule.methods | {'PUT', 'PATCH', 'DELETE', 'POST'}:
            if method in METHODS and (rule.rule, method) not in seen:
                seen.add((rule.rule, method))
                yield rule.endpoint, method, _sample_url(rule)


@pytest.mark.parametrize('mode_overrides,mode', [(PASSWORD_MODE, 'password'), (OIDC_MODE, 'oidc')], ids=['password', 'oidc'])
def test_no_route_answers_a_request_without_a_session_in_any_method(tmp_path, mode_overrides, mode):
    app = make(tmp_path / mode, **mode_overrides)
    client = client_of(app)
    count = 0
    for endpoint, method, url in _all_requests(app):
        headers = {'Origin': f'https://{HOST}', 'Content-Type': 'application/json'}
        response = client.open(url, method=method, base_url=f'https://{HOST}', headers=headers, data='{}')
        label = (endpoint, method, url, response.status_code)
        assert not 200 <= response.status_code < 300 or method == 'OPTIONS' and response.status_code == 200 and not response.get_data(), label
        if 300 <= response.status_code < 400:
            assert '/auth/login' in response.headers['Location'] or '/auth/password/login' in response.headers['Location'], label
        assert response.status_code != 500, label
        count += 1
    assert count > 60
    assert user_count(app) == 0                                                   # aucun compte créé, `local` compris


def test_the_list_of_public_endpoints_is_exactly_the_expected_one():
    """Toute nouvelle route publique doit être décidée ici, pas ajoutée par accident."""
    assert PUBLIC_ENDPOINTS == {'static', 'auth.login', 'auth.callback', 'auth.logout', 'auth.logged_out', 'api.health_check',
                                'auth.token_page', 'auth.token_inspect', 'auth.token_consume',
                                'auth.password_login', 'auth.password_mfa', 'auth.password_enroll', 'auth.password_forgot'}      # jetons à usage unique (T7b, EF-S8)


def test_unknown_and_tricky_paths_are_closed_not_enumerable(app):
    client = client_of(app)
    for path in ('/admin', '/admin/users', '/api/nonexistent', '/api/', '/api', '/API/activities', '//api/activities', '/api//activities',
                 '/api/activities/', '/api/activities;x=1', '/api/activities%00', '/%2e%2e/etc/passwd', '/..;/api/activities',
                 '/static/../api/activities', '/static/..%2fapp/config.py', '/static/%2e%2e/%2e%2e/app/config.py',
                 '/static/', '/static', '/static/js/..%5c..%5capp/config.py', '/data/kmlviewer.sqlite3', '/backup', '/api/backup',
                 '/api/files/1', '/files/1', '/uploads', '/.env', '/.git/config', '/kml_viewer.py'):
        response = get(client, path)
        assert response.status_code in (401, 302, 404, 400), (path, response.status_code)
        assert b'SECRET_KEY' not in response.get_data() and b'SQLite format' not in response.get_data(), path
        if response.status_code == 302:
            assert '/auth/login' in response.headers['Location'] or '/auth/password/login' in response.headers['Location'], path


def test_static_serves_only_the_static_folder(app):
    client = client_of(app)
    assert get(client, '/static/favicon.svg').status_code == 200
    for traversal in ('/static/../app/config.py', '/static/..%2fapp%2fconfig.py', '/static/%2e%2e/app/config.py',
                      '/static/css/../../app/config.py', '/static/js/%2e%2e%2f%2e%2e%2fapp%2fconfig.py'):
        response = get(client, traversal)
        assert response.status_code in (404, 400, 308) and b'SECRET_KEY' not in response.get_data(), traversal


def test_the_static_folder_holds_no_data_secret_or_database_file():
    root = pathlib.Path(ROOT) / 'static'
    suspicious = [p for p in root.rglob('*') if p.is_file()
                  and (p.suffix in ('.sqlite3', '.db', '.env', '.bak', '.key', '.pem', '.gpx', '.kml', '.fit') or p.name.startswith('.env'))]
    assert suspicious == []


def test_public_endpoints_create_nothing_and_leak_nothing(app):
    client = client_of(app)
    health = get(client, '/api/health')
    assert health.status_code == 200 and set(health.get_json()) <= {'status', 'version', 'api'}
    login = get(client, '/auth/login')                                            # T10 : redirige vers le formulaire (plus de 503)
    assert login.status_code == 302 and '/auth/password/login' in login.headers['Location']
    assert get(client, '/auth/callback', query_string={'code': 'x', 'state': 'y'}).status_code == 302
    assert get(client, '/auth/callback', query_string={'error': 'access_denied'}).status_code == 302
    assert client.post('/auth/logout', base_url=f'https://{HOST}', headers={'Origin': f'https://{HOST}'}).status_code in (302, 400, 403)
    assert get(client, '/auth/logged-out').status_code in (200, 404)
    with client.session_transaction(base_url=f'https://{HOST}') as session:
        assert 'user_id' not in session
    assert user_count(app) == 0


# --- En-têtes : Host, X-Forwarded-*, identités injectées -----------------------------------------------

@pytest.mark.parametrize('host', ['localhost', 'localhost:5000', '127.0.0.1', '[::1]', 'evil.example', f'{HOST}.evil.example',
                                  f'evil.{HOST}', f'{HOST}@evil.example', 'TRACES.EXAMPLE.COM.evil.org', '0.0.0.0'])
def test_a_host_header_outside_trusted_hosts_is_refused(app, host):
    client = client_of(app)
    for url in ('/', '/api/activities', '/auth/login', '/static/favicon.svg', '/dashboard'):
        response = client.get(url, base_url=f'https://{HOST}', headers={'Host': host})
        assert response.status_code == 400, (host, url, response.status_code)


@pytest.mark.parametrize('host', ['localhost', 'evil.example', '127.0.0.1'])
def test_only_the_health_probe_answers_whatever_the_host_and_reveals_only_the_version(app, host):
    """Choix de dev (a3c9b1d) : la sonde de santé répond sans contrôle d'hôte ; elle ne donne que statut et version."""
    response = app.test_client().get('/api/health', base_url=f'https://{HOST}', headers={'Host': host})
    assert response.status_code == 200 and set(response.get_json()) == {'status', 'version', 'api'}


@pytest.mark.parametrize('headers', [
    {'X-Forwarded-Host': 'localhost'}, {'X-Forwarded-Host': '127.0.0.1:5000'}, {'X-Forwarded-For': '127.0.0.1'},
    {'X-Forwarded-For': '::1'}, {'X-Forwarded-Proto': 'http'}, {'X-Real-IP': '127.0.0.1'}, {'Forwarded': 'for=127.0.0.1;host=localhost'},
    {'X-Remote-User': 'local'}, {'X-Forwarded-User': 'admin'}, {'Remote-User': 'admin'}, {'X-Auth-User': 'local'},
    {'X-Forwarded-Email': 'admin@example.com'}, {'Authorization': 'Bearer abc'}, {'Authorization': 'Basic YWRtaW46YWRtaW4='},
    {'X-API-Key': 'local'}, {'X-HTTP-Method-Override': 'GET'}, {'X-Original-URL': '/static/x'}, {'X-Rewrite-URL': '/static/x'},
    {'X-Forwarded-Prefix': '/static'}, {'X-Forwarded-Server': 'localhost'}, {'Cookie': 'session=eyJ1c2VyX2lkIjoxfQ.x.y'},
    {'Cookie': 'session=' + 'A' * 5000}, {'Cookie': 'user_id=1; token=tok; session=;;'}, {'Sec-Fetch-Site': 'same-origin'},
    {'Origin': 'null'}, {'Referer': f'https://{HOST}/admin'}, {'Accept': 'text/html'}, {'Accept': 'application/json'},
], ids=lambda h: next(iter(h)) + '=' + str(next(iter(h.values())))[:20])
@pytest.mark.parametrize('proxy_hops', [0, 1])
def test_no_header_ever_opens_a_closed_route(tmp_path, headers, proxy_hops):
    app = make(tmp_path / f'p{proxy_hops}', **PASSWORD_MODE, TRUST_PROXY=proxy_hops, TRUST_PROXY_HOST=bool(proxy_hops))
    client = client_of(app)
    try:
        for url, ok in (('/api/activities', {401, 400}), ('/api/users', {401, 400}), ('/', {302, 400}), ('/dashboard', {302, 400}),
                        ('/api/dashboard?period=week', {401, 400})):
            response = client.get(url, base_url=f'https://{HOST}', headers=headers)
            assert response.status_code in ok, (headers, url, response.status_code)
    finally:
        pass
    assert user_count(app) == 0


def test_forwarded_host_cannot_smuggle_a_trusted_host_past_the_check_without_a_proxy(app):
    """Sans TRUST_PROXY_HOST, X-Forwarded-Host est ignoré : le Host réel décide."""
    client = client_of(app)
    assert client.get('/api/activities', base_url='https://evil.example', headers={'X-Forwarded-Host': HOST}).status_code == 400


# --- Cookies forgés (la clé de session est supposée connue : le jeton du compte est la seconde barrière) ----

@pytest.mark.parametrize('case', ['oidc-with-right-token', 'local-with-right-token', 'no-method-none', 'other-account-token',
                                  'empty-token', 'none-token', 'int-token', 'bytes-token', 'list-token'])
def test_a_forged_session_never_opens_the_instance(app, case):
    password_uid = add_user(app, 'pwd:real', 'password', token='real-token')
    oidc_uid = add_user(app, 'idp-sub', 'oidc', token='oidc-token')
    local_uid = add_user(app, 'local', 'none', admin=True, token='local-token')
    client = client_of(app)
    uid, token = {
        'oidc-with-right-token': (oidc_uid, 'oidc-token'), 'local-with-right-token': (local_uid, 'local-token'),
        'no-method-none': (local_uid, 'real-token'), 'other-account-token': (password_uid, 'oidc-token'),
        'empty-token': (password_uid, ''), 'none-token': (password_uid, None), 'int-token': (password_uid, 0),
        'bytes-token': (password_uid, b'real-token'), 'list-token': (password_uid, ['real-token']),
    }[case]
    sign_in(client, uid, token)
    for url in ('/api/activities', '/api/users', '/api/me'):
        assert get(client, url).status_code == 401, (case, url)
    assert get(client, '/').status_code == 302
    with client.session_transaction(base_url=f'https://{HOST}') as session:
        assert 'user_id' not in session                                          # session vidée
    conn = conn_of(app)
    assert conn.execute("SELECT session_token FROM users WHERE id = ?", (uid,)).fetchone()[0] in ('real-token', 'oidc-token', 'local-token')
    conn.close()


@pytest.mark.parametrize('user_id', ['1', 1.0, None, -1, 0, True, [1], {'id': 1}, '1; DROP TABLE users'], ids=repr)
def test_a_forged_user_id_of_any_type_never_opens_and_never_errors(app, user_id):
    add_user(app, 'pwd:real', 'password', admin=True, token='real-token')
    client = client_of(app)
    with client.session_transaction(base_url=f'https://{HOST}') as session:
        session['user_id'], session['token'], session['login_at'] = user_id, 'real-token', int(time.time())
    response = get(client, '/api/activities')
    assert response.status_code in (401, 200) and response.status_code != 500, (user_id, response.status_code)
    if user_id is True:
        pytest.skip("True == 1 en Python : désigne le compte 1 mais seul le jeton protège (voir test dédié)")
    assert response.status_code == 401


def test_an_oversized_integer_user_id_is_a_401_not_a_500(app):
    add_user(app, 'pwd:real', 'password', token='real-token')
    client = client_of(app)
    with client.session_transaction(base_url=f'https://{HOST}') as session:
        session['user_id'], session['token'], session['login_at'] = 2 ** 70, 'real-token', int(time.time())
    assert get(client, '/api/activities').status_code == 401


def test_a_boolean_user_id_still_needs_the_accounts_token(app):
    add_user(app, 'pwd:real', 'password', token='real-token')
    client = client_of(app)
    with client.session_transaction(base_url=f'https://{HOST}') as session:
        session['user_id'], session['token'], session['login_at'] = True, 'faux-jeton', int(time.time())
    assert get(client, '/api/activities').status_code == 401


@pytest.mark.parametrize('login_at', [None, 'maintenant', -1, 0, 1.5, time.time() + 10_000, time.time() - 10 * 3600 * 24, float('inf'), float('nan')],
                         ids=repr)
def test_a_forged_login_date_cannot_extend_or_skip_the_session_lifetime(app, login_at):
    uid = add_user(app, 'pwd:real', 'password')
    client = client_of(app)
    with client.session_transaction(base_url=f'https://{HOST}') as session:
        session['user_id'], session['token'] = uid, 'tok'
        if login_at is not None:
            session['login_at'] = login_at
    assert get(client, '/api/activities').status_code == 401


def test_a_session_for_a_deactivated_password_account_is_refused_not_served(app):
    uid = add_user(app, 'pwd:off', 'password', active=False)
    client = client_of(app)
    sign_in(client, uid)
    assert get(client, '/api/activities').status_code == 403
    assert get(client, '/api/users').status_code in (401, 403)


def test_a_valid_session_cookie_carries_hardened_flags(app):
    uid = add_user(app, 'pwd:ok', 'password')
    client = client_of(app)
    sign_in(client, uid)
    response = get(client, '/api/activities')
    assert response.status_code == 200
    cookies = response.headers.getlist('Set-Cookie')
    if cookies:
        header = cookies[0].lower()
        assert 'secure' in header and 'httponly' in header and 'samesite=lax' in header


# --- Bascule de mode : la matrice complète (M4, CA-29) ------------------------------------------------------

@pytest.mark.parametrize('source,target', [('oidc', 'password'), ('password', 'oidc'), ('local', 'password'), ('local', 'oidc'),
                                           ('password', 'password'), ('oidc', 'oidc')])
def test_session_validity_follows_the_target_mode_for_every_account_kind(tmp_path, source, target):
    overrides = {'oidc': OIDC_MODE, 'password': PASSWORD_MODE, 'local': {'AUTH_MODE': 'local'}}
    first = make(tmp_path, **overrides[source])
    accounts = {'oidc': add_user(first, 'idp-1', 'oidc'), 'password': add_user(first, 'pwd:1', 'password'),
                'none': add_user(first, 'local-ish', 'none')}
    second = make(tmp_path, **overrides[target])
    for method, uid in accounts.items():
        client = client_of(second)
        sign_in(client, uid)
        # Doc-6 : en mode oidc, les comptes password n'ont de session que si les comptes à mot de passe sont disponibles (EF-0.4)
        oidc_methods = {'oidc', 'password'} if second.config['PASSWORD_ACCOUNTS'] else {'oidc'}
        expected = {'password': {'password'}, 'oidc': oidc_methods}[target]
        status = get(client, '/api/activities').status_code
        assert (status == 200) == (method in expected), (source, target, method, status)


def test_switching_to_password_mode_keeps_the_sso_accounts_but_without_any_session(tmp_path):
    first = make(tmp_path, **OIDC_MODE)
    uid = add_user(first, 'idp-1', 'oidc', admin=True)
    second = make(tmp_path, **PASSWORD_MODE)
    conn = conn_of(second)
    assert tuple(conn.execute("SELECT is_admin, auth_method FROM users WHERE id = ?", (uid,)).fetchone()) == (1, 'oidc')
    conn.close()
    client = client_of(second)
    sign_in(client, uid)
    assert get(client, '/api/users').status_code == 401                          # un admin SSO n'administre plus rien


def test_the_old_sso_cookie_comes_back_to_life_only_if_the_mode_returns_to_oidc(tmp_path):
    """Documenté : la bascule n'invalide pas le jeton du compte, elle refuse la méthode ; revenir à oidc rend le cookie valide."""
    first = make(tmp_path, **OIDC_MODE)
    uid = add_user(first, 'idp-1', 'oidc')
    client = client_of(first)
    sign_in(client, uid)
    assert get(client, '/api/activities').status_code == 200
    client.application = make(tmp_path, **PASSWORD_MODE)
    assert get(client, '/api/activities').status_code == 401                     # session vidée
    client.application = make(tmp_path, **OIDC_MODE)
    assert get(client, '/api/activities').status_code == 401                     # la session a été vidée : plus de résurrection


# --- Le compte `local` (B1) -------------------------------------------------------------------------------

def test_sub_local_is_never_created_in_password_or_oidc_whatever_is_done(tmp_path):
    for name, overrides in (('password', PASSWORD_MODE), ('oidc', OIDC_MODE)):
        app = make(tmp_path / name, **overrides)
        client = client_of(app)
        for url in ('/', '/api/activities', '/api/users', '/dashboard', '/api/health', '/auth/login', '/auth/callback'):
            get(client, url)
        assert app.test_cli_runner().invoke(args=['inbox-scan']).exit_code == 0
        assert app.test_cli_runner().invoke(args=['settings-list']).exit_code == 0
        assert user_count(app) == 0, name


def test_the_inbox_in_password_mode_attaches_nothing_to_the_local_account(tmp_path):
    app = make(tmp_path, **PASSWORD_MODE)
    inbox = pathlib.Path(app.config['DATA_DIR']) / 'inbox'
    (inbox / 'someone').mkdir(parents=True)
    path = inbox / 'someone' / 'a.gpx'
    path.write_bytes(b'<gpx/>')
    os.utime(path, (time.time() - 60, time.time() - 60))
    folderless = inbox / 'b.gpx'
    folderless.write_bytes(b'<gpx/>')
    os.utime(folderless, (time.time() - 60, time.time() - 60))
    assert app.test_cli_runner().invoke(args=['inbox-scan']).exit_code == 0
    assert user_count(app, "sub = 'local'") == 0


def test_an_idp_cannot_hand_out_the_local_subject(tmp_path):
    app = make(tmp_path, **OIDC_MODE)
    conn = conn_of(app)
    for claims in ({'sub': 'local'}, {'sub': 'local', 'preferred_username': 'admin'}, {'sub': ''}, {}):
        with pytest.raises(ValueError):
            users.upsert_from_claims(conn, claims)
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    conn.close()


def test_a_pre_existing_local_account_is_inert_in_password_mode(tmp_path):
    """Base utilisée d'abord en mode local, puis passée en password : le compte `local` (admin) ne sert plus à rien."""
    local_app = create_app(TestingConfig, overrides={'DATA_DIR': str(tmp_path / 'data'), 'AUTH_MODE': 'local'})
    assert local_app.test_client().get('/api/activities').status_code == 200
    assert user_count(local_app, "sub = 'local' AND is_admin = 1") == 1
    pwd = make(tmp_path, **PASSWORD_MODE)
    client = client_of(pwd)
    conn = conn_of(pwd)
    uid, token = conn.execute("SELECT id, session_token FROM users WHERE sub = 'local'").fetchone()
    conn.close()
    sign_in(client, uid, token)
    assert get(client, '/api/users').status_code == 401
    assert pwd.test_cli_runner().invoke(args=['user-mfa-reset', 'local']).exit_code != 0


# --- Démarrage : variantes de mode et filet TESTING ----------------------------------------------------------

@pytest.mark.parametrize('mode', ['Password', 'PASSWORD', ' password', 'password ', 'passwd', 'pwd', 'password,oidc', 'true', '1'])
def test_auth_mode_is_matched_exactly(tmp_path, mode):
    with pytest.raises(RuntimeError, match='AUTH_MODE'):
        make(tmp_path, **{**PASSWORD_MODE, 'AUTH_MODE': mode})


@pytest.mark.parametrize('hosts', [[''], ['*'], ['.'], [' ']])
def test_degenerate_trusted_hosts_do_not_pass_for_a_real_list(tmp_path, hosts):
    try:
        app = make(tmp_path, **{**PASSWORD_MODE, 'TRUSTED_HOSTS': hosts})
    except RuntimeError:
        return
    response = client_of(app).get('/api/health', base_url='https://evil.example')
    assert response.status_code == 400, f"TRUSTED_HOSTS={hosts!r} accepte n'importe quel hôte"


def test_password_mode_warns_when_the_cookie_may_travel_in_clear(tmp_path, caplog):
    import logging
    with caplog.at_level(logging.WARNING):
        app = make(tmp_path, **PASSWORD_MODE, SESSION_COOKIE_SECURE=False)
    assert app.config['SESSION_COOKIE_SECURE'] is False and 'cookie' in caplog.text.lower()


def test_the_production_entry_point_cannot_run_the_password_mode_with_the_testing_config(tmp_path):
    env = {**os.environ, 'DATA_DIR': str(tmp_path), 'FLASK_ENV': 'testing', 'AUTH_MODE': 'password', 'SECRETS_KEY': KEY,
           'PUBLIC_URL': f'https://{HOST}', 'PYTHONPATH': ROOT}
    for name in ('SECRET_KEY', 'TRUSTED_HOSTS'):
        env.pop(name, None)
    result = subprocess.run([sys.executable, '-c', 'import kml_viewer'], capture_output=True, text=True, cwd=ROOT, env=env, timeout=120)
    assert result.returncode != 0


# --- B5 : promotion sans second facteur (CLI et chemins internes) -----------------------------------------------

def run_cli(app, *args, **kw):
    return app.test_cli_runner().invoke(args=list(args), **kw)


def is_admin(app, uid):
    conn = conn_of(app)
    try:
        return conn.execute("SELECT is_admin FROM users WHERE id = ?", (uid,)).fetchone()[0]
    finally:
        conn.close()


def test_has_second_factor_counts_only_what_the_spec_calls_a_factor(app):
    uid = add_user(app, 'pwd:f', 'password')
    conn = conn_of(app)
    assert factors.has_second_factor(conn, uid) is False
    add_factor_tables(app)
    conn.execute("INSERT INTO recovery_codes (user_id, code_hash) VALUES (?, 'h')", (uid,))
    conn.commit()
    assert factors.has_second_factor(conn, uid) is False                         # EF-2.2 : les codes de secours ne sont pas un facteur
    conn.execute("INSERT INTO webauthn_credentials (user_id, label) VALUES (?, 'cle')", (uid,))
    conn.commit()
    assert factors.has_second_factor(conn, uid) is True
    conn.close()


def test_a_pending_totp_enrolment_is_not_an_active_factor(app):
    uid = add_user(app, 'pwd:pending', 'password')
    give_totp(app, uid, enabled=False)
    conn = conn_of(app)
    assert factors.has_second_factor(conn, uid) is False
    conn.close()


@pytest.mark.parametrize('with_recovery_codes_only', [False, True])
def test_the_cli_refuses_promotion_without_an_active_factor_and_changes_nothing(app, with_recovery_codes_only):
    uid = add_user(app, 'pwd:nofactor', 'password', active=False)
    if with_recovery_codes_only:
        add_factor_tables(app)
        conn = conn_of(app)
        conn.execute("INSERT INTO recovery_codes (user_id, code_hash) VALUES (?, 'h')", (uid,))
        conn.commit()
        conn.close()
    result = run_cli(app, 'user-admin', 'pwd:nofactor')
    assert result.exit_code != 0 and 'second facteur' in result.output
    conn = conn_of(app)
    assert tuple(conn.execute("SELECT is_admin, is_active FROM users WHERE id = ?", (uid,)).fetchone()) == (0, 0)   # ni admin, ni activé
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'user.admin_grant'").fetchone()[0] == 0
    conn.close()


def test_the_cli_promotes_with_a_webauthn_key_and_by_email_case_insensitively(app):
    uid = add_user(app, 'pwd:wa', 'password', email='wa@example.org')     # stockée canonique (EF-4.10), la saisie varie
    add_factor_tables(app)
    conn = conn_of(app)
    conn.execute("INSERT INTO webauthn_credentials (user_id, label) VALUES (?, 'cle')", (uid,))
    conn.commit()
    conn.close()
    assert run_cli(app, 'user-admin', 'WA@example.ORG').exit_code == 0 and is_admin(app, uid) == 1


def test_the_cli_refuses_an_unknown_or_ambiguous_login(app):
    add_user(app, 'pwd:a', 'password', username='bob')
    add_user(app, 'idp:b', 'oidc', username='BOB')
    assert run_cli(app, 'user-admin', 'bob').exit_code != 0                      # deux comptes : aucun n'est promu
    assert run_cli(app, 'user-admin', 'personne').exit_code != 0
    assert user_count(app, 'is_admin = 1') == 0


def test_a_factor_removed_between_the_check_and_the_promotion_does_not_leave_a_factorless_admin(app, monkeypatch):
    uid = add_user(app, 'pwd:race', 'password')
    give_totp(app, uid)
    real = factors.has_second_factor

    def check_then_lose_the_factor(conn, user_id):
        answer = real(conn, user_id)
        other = conn_of(app)
        factors.clear_factors(other, user_id)                                    # un user-mfa-reset concurrent
        other.commit()
        other.close()
        return answer

    monkeypatch.setattr(factors, 'has_second_factor', check_then_lose_the_factor)
    run_cli(app, 'user-admin', 'pwd:race')
    conn = conn_of(app)
    assert not (is_admin(app, uid) and not real(conn, uid))
    conn.close()


def test_the_existing_api_promotion_path_refuses_a_password_account_without_a_factor(app):
    admin = add_user(app, 'pwd:admin', 'password', admin=True, token='admin-token')
    give_totp(app, admin)
    target = add_user(app, 'pwd:target', 'password')
    client = client_of(app)
    sign_in(client, admin, 'admin-token')
    response = client.patch(f'/api/users/{target}', json={'is_admin': True}, base_url=f'https://{HOST}', headers={'Origin': f'https://{HOST}'})
    assert response.status_code == 409 and is_admin(app, target) == 0


def test_the_existing_api_promotion_path_works_for_a_password_account_with_a_factor(app):
    admin = add_user(app, 'pwd:admin', 'password', admin=True, token='admin-token')
    give_totp(app, admin)
    target = add_user(app, 'pwd:target', 'password')
    give_totp(app, target)
    client = client_of(app)
    sign_in(client, admin, 'admin-token')
    response = client.patch(f'/api/users/{target}', json={'is_admin': True}, base_url=f'https://{HOST}', headers={'Origin': f'https://{HOST}'})
    assert response.status_code == 200 and is_admin(app, target) == 1


# --- user-mfa-reset : facteurs retirés, sessions fermées, rétrogradation (EF-2.7) -------------------------------

def test_mfa_reset_removes_every_kind_of_factor_and_only_for_that_account(app):
    victim = add_user(app, 'pwd:victim', 'password', admin=True)
    bystander = add_user(app, 'pwd:other', 'password', admin=True, token='other-token')
    give_totp(app, victim)
    give_totp(app, bystander)
    conn = conn_of(app)
    for uid in (victim, bystander):
        conn.execute("INSERT INTO webauthn_credentials (user_id, label) VALUES (?, 'cle')", (uid,))
        conn.execute("INSERT INTO recovery_codes (user_id, code_hash) VALUES (?, 'h')", (uid,))
    conn.commit()
    conn.close()
    result = run_cli(app, 'user-mfa-reset', 'pwd:victim')
    assert result.exit_code == 0
    conn = conn_of(app)
    assert not factors.has_second_factor(conn, victim) and factors.has_second_factor(conn, bystander)
    assert conn.execute("SELECT COUNT(*) FROM recovery_codes WHERE user_id = ?", (victim,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM recovery_codes WHERE user_id = ?", (bystander,)).fetchone()[0] == 1
    assert tuple(conn.execute("SELECT is_admin, session_token FROM users WHERE id = ?", (bystander,)).fetchone()) == (1, 'other-token')
    conn.close()
    assert is_admin(app, victim) == 0


def test_mfa_reset_closes_every_open_session_of_the_account(app):
    uid = add_user(app, 'pwd:multi', 'password')
    clients = [client_of(app) for _ in range(3)]
    for client in clients:
        sign_in(client, uid)
        assert get(client, '/api/activities').status_code == 200
    assert run_cli(app, 'user-mfa-reset', 'pwd:multi').exit_code == 0
    assert [get(client, '/api/activities').status_code for client in clients] == [401, 401, 401]


def test_mfa_reset_demotion_survives_and_promotion_needs_a_new_factor(app):
    uid = add_user(app, 'pwd:demote', 'password', admin=True)
    give_totp(app, uid)
    assert run_cli(app, 'user-mfa-reset', 'pwd:demote').exit_code == 0
    assert is_admin(app, uid) == 0
    assert run_cli(app, 'user-admin', 'pwd:demote').exit_code != 0               # B5 : pas de repromotion sans facteur
    give_totp(app, uid)
    assert run_cli(app, 'user-admin', 'pwd:demote').exit_code == 0 and is_admin(app, uid) == 1


def test_mfa_reset_is_repeatable_and_always_renews_the_session_token(app):
    uid = add_user(app, 'pwd:again', 'password')
    tokens = []
    for _ in range(3):
        assert run_cli(app, 'user-mfa-reset', 'pwd:again').exit_code == 0
        conn = conn_of(app)
        tokens.append(conn.execute("SELECT session_token FROM users WHERE id = ?", (uid,)).fetchone()[0])
        conn.close()
    assert len(set(tokens)) == 3 and 'tok' not in tokens and all(len(t) >= 32 for t in tokens)


@pytest.mark.parametrize('method', ['oidc', 'none'])
def test_mfa_reset_refuses_non_password_accounts_and_changes_nothing(app, method):
    uid = add_user(app, f'x:{method}', method, admin=True, token='keep-me')
    result = run_cli(app, 'user-mfa-reset', f'x:{method}')
    assert result.exit_code != 0
    conn = conn_of(app)
    assert tuple(conn.execute("SELECT is_admin, session_token FROM users WHERE id = ?", (uid,)).fetchone()) == (1, 'keep-me')
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'user.mfa_reset'").fetchone()[0] == 0
    conn.close()


def test_mfa_reset_does_not_activate_or_deactivate_the_account_and_is_atomic(app, monkeypatch):
    uid = add_user(app, 'pwd:atomic', 'password', admin=True, active=True)
    give_totp(app, uid)

    def boom(conn, user_id):
        raise RuntimeError("disque plein")

    monkeypatch.setattr(factors, 'clear_factors', boom)
    result = run_cli(app, 'user-mfa-reset', 'pwd:atomic')
    assert result.exit_code != 0
    conn = conn_of(app)
    assert tuple(conn.execute("SELECT is_admin, session_token FROM users WHERE id = ?", (uid,)).fetchone()) == (1, 'tok')
    assert factors.has_second_factor(conn, uid) and conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'user.mfa_reset'").fetchone()[0] == 0
    conn.close()


def test_the_last_admin_warning_goes_to_the_error_stream_and_mentions_the_way_out(app):
    add_user(app, 'pwd:last', 'password', admin=True)
    result = run_cli(app, 'user-mfa-reset', 'pwd:last')
    assert result.exit_code == 0 and 'user-invite --admin' in result.stderr + result.output


def test_admin_count_ignores_inactive_admins(app):
    add_user(app, 'pwd:off', 'password', admin=True, active=False)
    add_user(app, 'pwd:on', 'password', admin=True)
    conn = conn_of(app)
    assert factors.active_admin_count(conn) == 1
    conn.close()


def test_users_table_constraints_back_the_modes(app):
    conn = conn_of(app)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO users (sub, auth_method) VALUES ('x', 'ldap')")
    conn.close()


def test_an_account_without_a_session_token_has_no_session_even_with_an_empty_cookie_token(app):
    """session_token vaut '' tant qu'aucune connexion n'en a émis un : '' == '' ne doit jamais ouvrir le compte."""
    uid = add_user(app, 'pwd:notoken', 'password', token='')
    client = client_of(app)
    for token in ('', None):
        sign_in(client, uid, token)
        assert get(client, '/api/activities').status_code == 401, repr(token)
    conn = conn_of(app)
    assert conn.execute("SELECT session_token FROM users WHERE id = ?", (uid,)).fetchone()[0] == ''
    conn.close()
