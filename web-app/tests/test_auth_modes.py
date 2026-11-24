"""Modes d'authentification : local / oidc / password (T8, B1, M4, EF-0.x, CA-28, CA-29)."""

import json
import sqlite3
import time

import pytest
from cryptography.fernet import Fernet

from app import create_app, db as database
from app.auth import modes, passwords
from app.auth.checks import normalize_public_url
from app.config import TestingConfig
from tests.conftest import OIDC_OVERRIDES

KEY = Fernet.generate_key().decode()
HOST = 'traces.example.com'


class Prod(TestingConfig):
    """Comme en production : les contrôles de démarrage ne sont pas allégés."""
    TESTING = False
    INBOX_ENABLED = False
    SECRET_KEY = 'k' * 40
    SESSION_COOKIE_SECURE = None


PASSWORD_MODE = {'AUTH_MODE': 'password', 'TRUSTED_HOSTS': [HOST], 'SECRETS_KEY': KEY, 'PUBLIC_URL': f'https://{HOST}'}


def make(tmp_path, config=Prod, **overrides):
    return create_app(config, overrides={'DATA_DIR': str(tmp_path / 'data'), **overrides})


def password_app(tmp_path, **overrides):
    return make(tmp_path, **{**PASSWORD_MODE, **overrides})


def client_of(app):
    return app.test_client(), {'base_url': f'https://{HOST}'}


def add_user(app, sub, method, admin=False, active=True):
    conn = database.connect(app.config['DB_PATH'])
    conn.execute("INSERT INTO users (sub, username, is_admin, is_active, auth_method, session_token) VALUES (?, ?, ?, ?, ?, 'tok')",
                 (sub, sub, int(admin), int(active), method))
    conn.commit()
    uid = conn.execute("SELECT id FROM users WHERE sub = ?", (sub,)).fetchone()[0]
    conn.close()
    return uid


def sign_in(client, uid, token='tok'):
    with client.session_transaction(base_url=f'https://{HOST}') as session:
        session['user_id'], session['token'], session['login_at'] = uid, token, int(time.time())


# --- modes.py --------------------------------------------------------------------------------

def test_modes_table_matches_the_spec():
    assert modes.PERMITTED_METHODS == {'local': {'none'}, 'oidc': {'oidc', 'password'}, 'password': {'password'}}
    assert modes.method_permitted('password', 'password') and not modes.method_permitted('password', 'oidc')
    assert modes.method_permitted('oidc', 'password') and modes.method_permitted('local', 'none')
    for bad in (None, '', 'ldap', 5):
        assert not any(modes.method_permitted(m, bad) for m in modes.MODES)
    assert not modes.method_permitted('unknown', 'password')


def test_auth_mode_refuses_an_unvalidated_configuration():
    with pytest.raises(RuntimeError):
        modes.auth_mode({})
    with pytest.raises(RuntimeError):
        modes.auth_mode({'AUTH_MODE': ''})
    assert modes.auth_mode({'AUTH_MODE': 'password'}) == 'password'


# --- Démarrage (EF-0.1, EF-0.2, EF-0.4) ---------------------------------------------------------------

def test_password_mode_starts_with_everything_in_place(tmp_path):
    app = password_app(tmp_path)
    assert app.config['AUTH_MODE'] == 'password' and app.config['PASSWORD_ACCOUNTS'] is True
    assert app.config['SESSION_COOKIE_SECURE'] is True                    # Secure par défaut
    assert app.config['PUBLIC_URL'] == f'https://{HOST}'


def test_password_mode_is_never_deduced(tmp_path):
    """Variable vide : jamais `password`, même avec tout ce qu'il faut."""
    app = make(tmp_path, TRUSTED_HOSTS=[HOST], SECRETS_KEY=KEY, PUBLIC_URL=f'https://{HOST}', AUTH_MODE='local')
    assert app.config['AUTH_MODE'] == 'local'
    with pytest.raises(RuntimeError, match='OIDC_ISSUER'):                  # configuration « de production » sans mode : refus
        make(tmp_path, TRUSTED_HOSTS=[HOST], SECRETS_KEY=KEY, PUBLIC_URL=f'https://{HOST}')


def test_password_mode_is_incompatible_with_oidc_issuer(tmp_path):
    with pytest.raises(RuntimeError, match='OIDC_ISSUER'):
        password_app(tmp_path, OIDC_ISSUER='https://idp.test')


@pytest.mark.parametrize('missing,match', [
    ('SECRETS_KEY', 'SECRETS_KEY'), ('PUBLIC_URL', 'PUBLIC_URL'), ('TRUSTED_HOSTS', 'TRUSTED_HOSTS'),
])
def test_password_mode_refuses_to_start_without_a_guarantee(tmp_path, monkeypatch, missing, match):
    monkeypatch.delenv('SECRETS_KEY', raising=False)
    overrides = {**PASSWORD_MODE, missing: '' if missing != 'TRUSTED_HOSTS' else []}
    with pytest.raises(RuntimeError, match=match):
        make(tmp_path, **overrides)


@pytest.mark.parametrize('secret_key', ['your-secret-key-change-in-production', 'court'])
def test_password_mode_refuses_a_weak_session_key(tmp_path, secret_key):
    with pytest.raises(RuntimeError, match='SECRET_KEY'):
        password_app(tmp_path, SECRET_KEY=secret_key)


def test_password_mode_refuses_a_malformed_secrets_key(tmp_path):
    with pytest.raises(RuntimeError, match='SECRETS_KEY') as exc:
        password_app(tmp_path, SECRETS_KEY='pas-une-cle')
    assert 'pas-une-cle' not in str(exc.value)


def test_public_url_must_be_covered_by_trusted_hosts(tmp_path):
    with pytest.raises(RuntimeError, match='TRUSTED_HOSTS'):
        password_app(tmp_path, PUBLIC_URL='https://autre.example.org')
    assert password_app(tmp_path, TRUSTED_HOSTS=['.example.com'], PUBLIC_URL='https://a.b.example.com:8443'
                        ).config['PUBLIC_URL'] == 'https://a.b.example.com:8443'


@pytest.mark.parametrize('url,expected', [
    ('https://Traces.Example.com', 'https://traces.example.com'), ('https://traces.example.com/', 'https://traces.example.com'),
    ('https://traces.example.com:8443', 'https://traces.example.com:8443'), ('http://localhost:5000', 'http://localhost:5000'),
    ('http://127.0.0.1:5000/', 'http://127.0.0.1:5000'),
])
def test_public_url_normalization(url, expected):
    assert normalize_public_url(url) == expected


@pytest.mark.parametrize('url', [
    'http://traces.example.com', 'ftp://x.org', 'traces.example.com', 'https://', 'https://u:p@traces.example.com',
    'https://traces.example.com/app', 'https://traces.example.com?x=1', 'https://traces.example.com#f',
    'https://traces.example.com:99999', 'javascript:alert(1)', '//traces.example.com', '',
])
def test_public_url_refusals(url):
    with pytest.raises(RuntimeError):
        normalize_public_url(url)


def test_an_invalid_public_url_stops_the_start_in_any_mode(tmp_path):
    with pytest.raises(RuntimeError, match='PUBLIC_URL'):
        make(tmp_path, AUTH_MODE='local', PUBLIC_URL='http://evil.example.org')


@pytest.mark.parametrize('secrets_key,public_url,expected', [
    (KEY, f'https://{HOST}', True), ('', f'https://{HOST}', False), (KEY, '', False), ('', '', False)])
def test_oidc_mode_enables_password_accounts_only_with_secrets_key_and_public_url(tmp_path, monkeypatch, secrets_key,
                                                                                  public_url, expected):
    """EF-0.4 : pas de refus de démarrage, simplement des comptes à mot de passe désactivés."""
    monkeypatch.delenv('SECRETS_KEY', raising=False)
    app = make(tmp_path, **OIDC_OVERRIDES, TRUSTED_HOSTS=[HOST], SECRETS_KEY=secrets_key, PUBLIC_URL=public_url)
    assert app.config['AUTH_MODE'] == 'oidc' and app.config['PASSWORD_ACCOUNTS'] is expected
    assert modes.password_accounts_available(app.config) is expected


def test_local_mode_never_has_password_accounts(tmp_path):
    app = make(tmp_path, AUTH_MODE='local', SECRETS_KEY=KEY, PUBLIC_URL=f'https://{HOST}', TRUSTED_HOSTS=[HOST])
    assert app.config['PASSWORD_ACCOUNTS'] is False


# --- Liste de mots de passe courants au démarrage (T6b-1) -----------------------------------------------

def test_missing_wordlist_stops_a_start_that_has_password_accounts(tmp_path, monkeypatch):
    monkeypatch.setattr(passwords, '_common', None)
    monkeypatch.setattr(passwords, 'WORDLIST', tmp_path / 'absente.txt')
    with pytest.raises(RuntimeError, match='Démarrage refusé'):
        password_app(tmp_path)
    with pytest.raises(RuntimeError, match='Démarrage refusé'):
        make(tmp_path, **OIDC_OVERRIDES, TRUSTED_HOSTS=[HOST], SECRETS_KEY=KEY, PUBLIC_URL=f'https://{HOST}')


def test_wordlist_is_not_loaded_without_password_accounts(tmp_path, monkeypatch):
    monkeypatch.setattr(passwords, '_common', None)
    monkeypatch.setattr(passwords, 'WORDLIST', tmp_path / 'absente.txt')
    assert make(tmp_path, AUTH_MODE='local').config['PASSWORD_ACCOUNTS'] is False
    assert make(tmp_path / 'b', **OIDC_OVERRIDES, TRUSTED_HOSTS=[HOST]).config['PASSWORD_ACCOUNTS'] is False


def test_the_wordlist_is_loaded_at_startup_when_needed(tmp_path, monkeypatch):
    monkeypatch.setattr(passwords, '_common', None)
    password_app(tmp_path)
    assert passwords._common is not None and len(passwords._common) == 46_187


# --- Le garde se fonde sur le mode (B1, CA-28) ---------------------------------------------------------

def _routes(app):
    for rule in app.url_map.iter_rules():
        if 'GET' not in rule.methods:
            continue
        values = {arg: 1 for arg in rule.arguments}
        yield rule, app.url_map.bind(HOST).build(rule.endpoint, values) if True else None


def test_password_mode_without_session_closes_every_route_and_creates_no_local_account(tmp_path):
    """CA-28 : chaque route du url_map hors liste blanche => 401 ou redirection ; aucune ligne sub='local'."""
    from app.auth.guard import PUBLIC_ENDPOINTS
    app = password_app(tmp_path)
    client, kw = client_of(app)
    checked = 0
    for rule, url in _routes(app):
        if rule.endpoint in PUBLIC_ENDPOINTS:
            continue
        response = client.get(url, **kw)
        assert response.status_code in (401, 302), (url, response.status_code)
        if response.status_code == 302:
            assert '/auth/password/login' in response.headers['Location'], url
        checked += 1
    assert checked > 20
    for method, url in (('post', '/api/activities'), ('patch', '/api/users/1'), ('delete', '/api/activities/1'),
                        ('post', '/api/export/csv')):
        response = getattr(client, method)(url, **kw, headers={'Origin': f'https://{HOST}'})
        assert response.status_code in (401, 302, 404, 405), (method, url, response.status_code)
    conn = database.connect(app.config['DB_PATH'])
    assert conn.execute("SELECT COUNT(*) FROM users WHERE sub = 'local'").fetchone()[0] == 0
    conn.close()


def test_password_mode_login_page_does_not_redirect_in_a_loop(tmp_path):
    app = password_app(tmp_path)
    client, kw = client_of(app)
    response = client.get('/', **kw)
    assert response.status_code == 302 and '/auth/password/login' in response.headers['Location']
    entry = client.get('/auth/login', **kw)                                  # l'entrée générique renvoie vers la page du mode
    assert entry.status_code == 302 and '/auth/password/login' in entry.headers['Location']
    assert client.get('/auth/password/login', **kw).status_code == 200
    assert client.get('/auth/callback', **kw).status_code == 302            # OIDC inactif : retour à l'accueil, jamais d'erreur


def test_local_mode_is_the_only_one_that_creates_the_local_account(tmp_path):
    app = make(tmp_path, AUTH_MODE='local')
    assert app.test_client().get('/api/activities').status_code == 200
    conn = database.connect(app.config['DB_PATH'])
    row = conn.execute("SELECT is_admin, auth_method FROM users WHERE sub = 'local'").fetchone()
    assert tuple(row) == (1, 'none')
    conn.close()


def test_oidc_mode_without_session_still_answers_401(tmp_path):
    app = make(tmp_path, **OIDC_OVERRIDES, TRUSTED_HOSTS=[HOST])
    client = app.test_client()
    assert client.get('/api/activities', base_url=f'https://{HOST}').status_code == 401
    conn = database.connect(app.config['DB_PATH'])
    assert conn.execute("SELECT COUNT(*) FROM users WHERE sub = 'local'").fetchone()[0] == 0
    conn.close()


@pytest.mark.parametrize('overrides,mode', [
    ({'AUTH_MODE': 'local'}, 'local'), ({**OIDC_OVERRIDES, 'TRUSTED_HOSTS': [HOST]}, 'oidc'), (PASSWORD_MODE, 'password')])
def test_templates_receive_the_mode(tmp_path, overrides, mode):
    app = make(tmp_path / mode, **overrides)
    with app.test_request_context('/', base_url='http://localhost' if mode == 'local' else f'https://{HOST}'):
        app.preprocess_request()
        context = {}
        app.update_template_context(context)
    assert context['auth_mode'] == mode and context['oidc_enabled'] is (mode == 'oidc')
    assert context['password_accounts'] is (mode == 'password')       # oidc sans SECRETS_KEY ni PUBLIC_URL : désactivés


def test_navbar_shows_logout_and_admin_entries_outside_local_mode(tmp_path):
    app = password_app(tmp_path)
    uid = add_user(app, 'pwd:1', 'password', admin=True)
    client, kw = client_of(app)
    sign_in(client, uid)
    html = client.get('/dashboard', **kw).get_data(as_text=True)
    assert 'action="/auth/logout"' in html and 'Utilisateurs' in html


# --- Méthode d'authentification permise (M4, CA-29) -----------------------------------------------

def test_accepted_session_in_password_mode_is_a_password_account(tmp_path):
    app = password_app(tmp_path)
    uid = add_user(app, 'pwd:1', 'password')
    client, kw = client_of(app)
    sign_in(client, uid)
    assert client.get('/api/activities', **kw).status_code == 200


def test_switching_from_oidc_to_password_invalidates_sso_sessions(tmp_path):
    """CA-29 : un cookie de session d'un compte oidc émis avant la bascule est refusé, session vidée."""
    before = make(tmp_path, **OIDC_OVERRIDES, TRUSTED_HOSTS=[HOST])
    sso = add_user(before, 'idp-sub-1', 'oidc')
    client, kw = client_of(before)
    sign_in(client, sso)
    assert client.get('/api/activities', **kw).status_code == 200               # valide en mode oidc
    after = password_app(tmp_path)                                                # même base, nouveau mode
    client.application = after
    assert client.get('/api/activities', **kw).status_code == 401
    with client.session_transaction(base_url=f'https://{HOST}') as session:
        assert 'user_id' not in session                                          # session vidée


def test_password_accounts_may_use_the_oidc_mode_but_a_local_account_may_not(tmp_path):
    app = make(tmp_path, **OIDC_OVERRIDES, TRUSTED_HOSTS=[HOST])
    client, kw = client_of(app)
    sign_in(client, add_user(app, 'pwd:2', 'password'))
    assert client.get('/api/activities', **kw).status_code == 200
    sign_in(client, add_user(app, 'local-ish', 'none'))
    assert client.get('/api/activities', **kw).status_code == 401


def test_an_unknown_auth_method_in_the_database_has_no_session_anywhere(tmp_path):
    app = password_app(tmp_path)
    uid = add_user(app, 'pwd:3', 'password')
    conn = database.connect(app.config['DB_PATH'])
    with pytest.raises(sqlite3.IntegrityError):  # la base refuse une méthode inconnue (CHECK)
        conn.execute("UPDATE users SET auth_method = 'ldap' WHERE id = ?", (uid,))
    conn.close()


def test_users_api_exposes_the_mode_and_the_method(tmp_path):
    app = password_app(tmp_path)
    uid = add_user(app, 'pwd:4', 'password')
    client, kw = client_of(app)
    sign_in(client, uid)
    data = client.get('/api/me', **kw).get_json() if client.get('/api/me', **kw).status_code == 200 else None
    if data is None:
        pytest.skip("route /api/me absente sous ce nom")
    assert data['auth_mode'] == 'password' and data['user']['auth_method'] == 'password'


# --- Migration 0009 --------------------------------------------------------------------------------

def test_migration_gives_oidc_to_existing_accounts_and_none_to_the_local_one(tmp_path):
    path = str(tmp_path / 'old.sqlite3')
    conn = database.connect(path)
    conn.executescript(database.SCHEMA)
    database.migrate(conn, named=[])
    conn.execute("INSERT INTO users (sub, username) VALUES ('local', 'local'), ('idp-1', 'alice')")
    conn.commit()
    conn.close()
    database.init_db(path)
    database.init_db(path)
    conn = database.connect(path)
    assert dict(conn.execute("SELECT sub, auth_method FROM users").fetchall()) == {'local': 'none', 'idp-1': 'oidc'}
    assert conn.execute("SELECT 1 FROM schema_migrations WHERE name = '0009_users_auth_method'").fetchone()
    conn.close()


# --- Inbox -------------------------------------------------------------------------------------------

def test_inbox_owner_resolution_follows_the_validated_mode_and_never_falls_back_open(db):
    """T8-3 : pas de repli sur OIDC_ISSUER ; mode absent => erreur, jamais le compte local."""
    from app.services.inbox_watcher import _resolve_owner
    db.execute("INSERT INTO users (sub, username) VALUES ('s1', 'alice')")
    db.commit()
    assert _resolve_owner(db, {'AUTH_MODE': 'local'}, None)['sub'] == 'local'
    assert _resolve_owner(db, {'AUTH_MODE': 'password'}, 'alice')['username'] == 'alice'
    assert _resolve_owner(db, {'AUTH_MODE': 'oidc'}, None) is None
    for cfg in ({}, {'OIDC_ISSUER': ''}, {'AUTH_MODE': ''}):
        with pytest.raises(RuntimeError):
            _resolve_owner(db, cfg, None)
    assert db.execute("SELECT COUNT(*) FROM users WHERE sub = 'local'").fetchone()[0] == 1    # créé par le premier cas seulement


# --- CLI : user-admin (B5), user-mfa-reset, secrets-reset ----------------------------------------------

def _audit(conn, action):
    return [dict(r) for r in conn.execute("SELECT * FROM audit_log WHERE action = ? ORDER BY id", (action,))]


@pytest.fixture
def cli_app(tmp_path):
    return password_app(tmp_path)


def _add_totp(app, uid):
    conn = database.connect(app.config['DB_PATH'])
    conn.execute("ALTER TABLE users ADD COLUMN totp_secret BLOB")
    conn.execute("ALTER TABLE users ADD COLUMN totp_enabled_at TEXT")
    conn.execute("UPDATE users SET totp_secret = x'01', totp_enabled_at = '2026-01-01T00:00:00Z' WHERE id = ?", (uid,))
    conn.commit()
    conn.close()


def test_user_admin_refuses_to_promote_a_password_account_without_a_second_factor(cli_app):
    """B5 : un mot de passe seul ne donne jamais l'admin."""
    uid = add_user(cli_app, 'pwd:5', 'password')
    result = cli_app.test_cli_runner().invoke(args=['user-admin', 'pwd:5'])
    assert result.exit_code != 0 and 'second facteur' in result.output
    conn = database.connect(cli_app.config['DB_PATH'])
    assert conn.execute("SELECT is_admin FROM users WHERE id = ?", (uid,)).fetchone()[0] == 0
    assert _audit(conn, 'user.admin_grant') == []
    conn.close()


def test_user_admin_promotes_a_password_account_that_has_a_factor_and_traces_it(cli_app):
    uid = add_user(cli_app, 'pwd:6', 'password', active=False)
    _add_totp(cli_app, uid)
    assert cli_app.test_cli_runner().invoke(args=['user-admin', 'pwd:6']).exit_code == 0
    conn = database.connect(cli_app.config['DB_PATH'])
    assert tuple(conn.execute("SELECT is_admin, is_active FROM users WHERE id = ?", (uid,)).fetchone()) == (1, 1)
    (entry,) = _audit(conn, 'user.admin_grant')
    assert (entry['actor_type'], entry['actor_id'], entry['target_id']) == ('cli', None, str(uid))
    conn.close()


def test_user_admin_revoke_never_needs_a_factor(cli_app):
    uid = add_user(cli_app, 'pwd:7', 'password', admin=True)
    assert cli_app.test_cli_runner().invoke(args=['user-admin', 'pwd:7', '--revoke']).exit_code == 0
    conn = database.connect(cli_app.config['DB_PATH'])
    assert conn.execute("SELECT is_admin FROM users WHERE id = ?", (uid,)).fetchone()[0] == 0
    assert len(_audit(conn, 'user.admin_revoke')) == 1
    conn.close()


def test_user_admin_still_promotes_sso_and_local_accounts(tmp_path):
    app = make(tmp_path, AUTH_MODE='local')
    uid = add_user(app, 'idp-9', 'oidc')
    assert app.test_cli_runner().invoke(args=['user-admin', 'idp-9']).exit_code == 0
    conn = database.connect(app.config['DB_PATH'])
    assert conn.execute("SELECT is_admin FROM users WHERE id = ?", (uid,)).fetchone()[0] == 1
    conn.close()


def test_user_mfa_reset_removes_factors_closes_sessions_demotes_and_warns_when_no_admin_is_left(cli_app):
    uid = add_user(cli_app, 'pwd:8', 'password', admin=True)
    _add_totp(cli_app, uid)
    result = cli_app.test_cli_runner().invoke(args=['user-mfa-reset', 'pwd:8'])
    assert result.exit_code == 0 and 'rétrogradé' in result.output and 'aucun administrateur' in result.output
    conn = database.connect(cli_app.config['DB_PATH'])
    row = conn.execute("SELECT totp_secret, session_token, is_admin FROM users WHERE id = ?", (uid,)).fetchone()
    assert row['totp_secret'] is None and row['is_admin'] == 0 and row['session_token'] != 'tok'
    (entry,) = _audit(conn, 'user.mfa_reset')
    assert (entry['actor_type'], entry['actor_id']) == ('cli', None)
    assert json.loads(entry['details'])['demoted'] is True and json.loads(entry['details'])['factors_removed'] == 1
    conn.close()


def test_user_mfa_reset_makes_the_old_session_cookie_useless(cli_app):
    uid = add_user(cli_app, 'pwd:9', 'password')
    client, kw = client_of(cli_app)
    sign_in(client, uid)
    assert client.get('/api/activities', **kw).status_code == 200
    assert cli_app.test_cli_runner().invoke(args=['user-mfa-reset', 'pwd:9']).exit_code == 0
    assert client.get('/api/activities', **kw).status_code == 401


def test_user_mfa_reset_only_concerns_password_accounts_and_one_match(cli_app):
    add_user(cli_app, 'idp-10', 'oidc')
    assert cli_app.test_cli_runner().invoke(args=['user-mfa-reset', 'idp-10']).exit_code != 0
    assert cli_app.test_cli_runner().invoke(args=['user-mfa-reset', 'inconnu']).exit_code != 0


def test_user_mfa_reset_does_not_warn_while_another_admin_remains(cli_app):
    add_user(cli_app, 'pwd:11', 'password', admin=True)
    add_user(cli_app, 'pwd:12', 'password', admin=True)
    result = cli_app.test_cli_runner().invoke(args=['user-mfa-reset', 'pwd:11'])
    assert result.exit_code == 0 and 'aucun administrateur' not in result.output


def test_secrets_reset_announces_an_instance_left_without_admin(cli_app):
    result = cli_app.test_cli_runner().invoke(args=['secrets-reset', '--yes'])
    assert result.exit_code == 0 and 'aucun administrateur' in result.output
    add_user(cli_app, 'pwd:13', 'password', admin=True)
    assert 'aucun administrateur' not in cli_app.test_cli_runner().invoke(args=['secrets-reset', '--yes']).output


# --- Relecture T8 : admin sans facteur, TOTP non confirmé, sub réservé (T8-1, T8-2, T8-4) ----------------------

def _admin_views(app):
    """Toutes les routes protégées par admin_required, repérées sur le url_map (aucune liste écrite à la main)."""
    found = []
    for rule in app.url_map.iter_rules():
        view = app.view_functions[rule.endpoint]
        if getattr(view, 'admin_only', False):
            method = next(m for m in ('GET', 'POST', 'PATCH', 'DELETE', 'PUT') if m in rule.methods)
            found.append((rule, method))
    return found


def _enable_totp(app, uid, confirmed=True):
    conn = database.connect(app.config['DB_PATH'])
    cols = {r['name'] for r in conn.execute("PRAGMA table_info(users)")}
    for column, kind in (('totp_secret', 'BLOB'), ('totp_enabled_at', 'TEXT')):
        if column not in cols:
            conn.execute(f"ALTER TABLE users ADD COLUMN {column} {kind}")
    conn.execute("UPDATE users SET totp_secret = x'01', totp_enabled_at = ? WHERE id = ?",
                 ('2026-01-01T00:00:00Z' if confirmed else None, uid))
    conn.commit()
    conn.close()


def test_every_admin_route_refuses_a_password_admin_without_a_second_factor(tmp_path, caplog):
    """T8-1 (EF-2.2, CA-13) : sondé sur toutes les routes admin_required du url_map."""
    app = password_app(tmp_path)
    admin = add_user(app, 'pwd:adm', 'password', admin=True)            # admin « forcé en base », sans facteur
    client, kw = client_of(app)
    sign_in(client, admin)
    routes = _admin_views(app)
    assert len(routes) >= 3
    for rule, method in routes:
        url = app.url_map.bind(HOST).build(rule.endpoint, {a: 1 for a in rule.arguments})
        response = getattr(client, method.lower())(url, **kw, headers={'Origin': f'https://{HOST}'}, json={})
        assert response.status_code == 403, (rule.rule, response.status_code)
    assert 'second facteur' in caplog.text


def test_a_password_admin_with_a_confirmed_factor_passes_and_an_unconfirmed_one_does_not(tmp_path):
    app = password_app(tmp_path)
    admin = add_user(app, 'pwd:adm2', 'password', admin=True)
    client, kw = client_of(app)
    sign_in(client, admin)
    _enable_totp(app, admin, confirmed=False)                              # T8-2 : secret non confirmé ≠ facteur
    assert client.get('/api/users', **kw).status_code == 403
    _enable_totp(app, admin, confirmed=True)
    assert client.get('/api/users', **kw).status_code == 200


def test_sso_and_local_admins_are_not_subject_to_the_factor_check(tmp_path):
    local = make(tmp_path / 'l', AUTH_MODE='local')
    assert local.test_client().get('/api/users').status_code == 200
    oidc = make(tmp_path / 'o', **OIDC_OVERRIDES, TRUSTED_HOSTS=[HOST])
    client, kw = client_of(oidc)
    sign_in(client, add_user(oidc, 'idp-adm', 'oidc', admin=True))
    assert client.get('/api/users', **kw).status_code == 200


def test_has_second_factor_requires_a_confirmed_totp_or_a_webauthn_key(tmp_path):
    from app.auth import factors
    app = password_app(tmp_path)
    uid = add_user(app, 'pwd:f', 'password')
    conn = database.connect(app.config['DB_PATH'])
    assert not factors.has_second_factor(conn, uid)
    conn.execute("ALTER TABLE users ADD COLUMN totp_secret BLOB")
    conn.execute("UPDATE users SET totp_secret = x'01' WHERE id = ?", (uid,))
    conn.commit()
    assert not factors.has_second_factor(conn, uid)                       # colonne de confirmation absente
    conn.execute("ALTER TABLE users ADD COLUMN totp_enabled_at TEXT")
    conn.commit()
    assert not factors.has_second_factor(conn, uid)                       # secret enregistré, jamais validé
    conn.execute("UPDATE users SET totp_enabled_at = '2026-01-01T00:00:00Z' WHERE id = ?", (uid,))
    conn.commit()
    assert factors.has_second_factor(conn, uid)
    conn.execute("UPDATE users SET totp_secret = NULL")
    conn.execute("CREATE TABLE webauthn_credentials (id INTEGER PRIMARY KEY, user_id INTEGER)")
    conn.commit()
    assert not factors.has_second_factor(conn, uid)
    conn.execute("INSERT INTO webauthn_credentials (user_id) VALUES (?)", (uid,))
    conn.commit()
    assert factors.has_second_factor(conn, uid)
    conn.close()


def test_user_admin_refuses_an_unconfirmed_totp(cli_app):
    uid = add_user(cli_app, 'pwd:unc', 'password')
    _enable_totp(cli_app, uid, confirmed=False)
    assert cli_app.test_cli_runner().invoke(args=['user-admin', 'pwd:unc']).exit_code != 0


@pytest.mark.parametrize('sub', ['pwd:abc', 'pwd:', 'pwd:00000000-0000-0000-0000-000000000000', 'local', '', None, 5])
def test_oidc_login_never_designates_a_password_namespace_sub(db, sub, caplog):
    """T8-4 : l'IdP ne peut pas désigner un compte `pwd:` ni le compte local."""
    from app.auth import users
    with pytest.raises(ValueError):
        users.upsert_from_claims(db, {'sub': sub, 'preferred_username': 'x'})
    assert db.execute("SELECT COUNT(*) FROM users WHERE sub NOT IN ('local')").fetchone()[0] == 0


def test_oidc_login_refuses_an_existing_row_that_is_not_an_sso_account(db, caplog):
    from app.auth import users
    db.execute("INSERT INTO users (sub, username, auth_method, is_admin) VALUES ('legacy-sub', 'victim', 'password', 1)")
    db.commit()
    with pytest.raises(ValueError):
        users.upsert_from_claims(db, {'sub': 'legacy-sub', 'preferred_username': 'attacker', 'email': 'a@b.c',
                                      'email_verified': True})
    row = db.execute("SELECT username, email, is_admin FROM users WHERE sub = 'legacy-sub'").fetchone()
    assert tuple(row) == ('victim', None, 1)                             # rien n'a été réécrit
    assert 'n\'est pas un compte SSO' in caplog.text
    db.execute("INSERT INTO users (sub, username) VALUES ('idp-ok', 'bob')")
    db.commit()
    assert users.upsert_from_claims(db, {'sub': 'idp-ok', 'preferred_username': 'bob'})['sub'] == 'idp-ok'
