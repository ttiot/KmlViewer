"""Connexion par mot de passe en deux étapes, état mfa_pending, mot de passe oublié (T10 : CA-7, CA-9, CA-14, CA-15, CA-16, CA-45, CA-46)."""

import re
import threading
import time

import pytest
from cryptography.fernet import Fernet

from app import create_app, db as database
from app.audit import Actor
from tests.conftest import force_setting
from app.auth import factors, passwords, tokens
from app.auth.pending import PendingStore
from app.config import TestingConfig
from app.ratelimit import Limiters, Limits
from tests.fake_smtp import FakeSMTP

KEY = Fernet.generate_key().decode()
HOST = 'traces.example.com'
CLI = Actor.cli()
GOOD = 'cheval-agrafe-lune-42'
HASH = passwords.hash_password(GOOD)
KW = {'base_url': f'https://{HOST}'}


class Prod(TestingConfig):
    TESTING = False
    INBOX_ENABLED = False
    SECRET_KEY = 'k' * 40
    SESSION_COOKIE_SECURE = None


def make_app(tmp_path, **extra):
    return create_app(Prod, overrides={'DATA_DIR': str(tmp_path / 'data'), 'AUTH_MODE': 'password', 'TRUSTED_HOSTS': [HOST],
                                       'SECRETS_KEY': KEY, 'PUBLIC_URL': f'https://{HOST}', **extra})


@pytest.fixture
def app(tmp_path):
    return make_app(tmp_path)


@pytest.fixture
def conn(app):
    c = database.connect(app.config['DB_PATH'])
    yield c
    c.close()


def add(conn, name, *, admin=False, active=True, email=None, method='password', password=HASH):
    conn.execute("INSERT INTO users (sub, username, email, is_admin, is_active, auth_method, password_hash, session_token) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?, 'tok')",
                 (f'pwd:{name}' if method == 'password' else name, name, email, int(admin), int(active), method,
                  password if method == 'password' else None))
    conn.commit()
    return conn.execute("SELECT * FROM users WHERE username = ?", (name,)).fetchone()


def csrf_of(client, path='/auth/password/login'):
    html = client.get(path, **KW).get_data(as_text=True)
    return re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)


def login(client, username, password, *, token=None, **extra):
    return client.post('/auth/password/login', json={'username': username, 'password': password, **extra}, **KW,
                       headers={'X-CSRF-Token': token or csrf_of(client), 'Origin': f'https://{HOST}'})


# --- CA-7 : réponse unique et générique ---------------------------------------------------------------------------------

def test_every_failure_has_the_same_status_and_body(app, conn):
    add(conn, 'alice')
    add(conn, 'off', active=False)
    add(conn, 'sso', method='oidc')
    client = app.test_client()
    results = {(r.status_code, r.get_data()) for r in (login(client, 'alice', 'mauvais-mot-de-passe-1'),
                                                         login(client, 'inconnu', GOOD), login(client, 'off', GOOD),
                                                         login(client, 'sso', GOOD), login(client, 'alice', ''))}
    assert len(results) == 1 and next(iter(results))[0] == 401


def test_success_opens_a_full_session_for_a_user_without_factor_and_clears_the_previous_one(app, conn):
    user = add(conn, 'alice')
    client = app.test_client()
    with client.session_transaction(**KW) as s:
        s['injected'] = 'x'
        s['pending_id'] = 'old'
    r = login(client, 'ALICE', GOOD, next='/dashboard')
    assert r.status_code == 200 and r.get_json() == {'success': True, 'next': '/dashboard'}
    with client.session_transaction(**KW) as s:
        assert s['user_id'] == user['id'] and 'injected' not in s and 'pending_id' not in s
    assert client.get('/api/activities', **KW).status_code == 200


def test_the_next_parameter_cannot_leave_the_site(app, conn):
    add(conn, 'alice')
    for bad in ('https://evil.example/', '//evil.example', '/\\evil.example', 'javascript:alert(1)'):
        r = login(app.test_client(), 'alice', GOOD, next=bad)
        assert r.get_json()['next'] == '/', bad


def test_a_missing_csrf_token_or_origin_is_refused_before_any_check(app, conn):
    add(conn, 'alice')
    client = app.test_client()
    r = client.post('/auth/password/login', json={'username': 'alice', 'password': GOOD}, **KW,
                    headers={'Origin': f'https://{HOST}'})
    assert r.status_code == 400
    r = client.post('/auth/password/login', json={'username': 'alice', 'password': GOOD}, **KW,
                    headers={'X-CSRF-Token': 'forge', 'Origin': f'https://{HOST}'})
    assert r.status_code == 400
    with client.session_transaction(**KW) as s:
        assert 'user_id' not in s


# --- CA-9 : état mfa_pending côté serveur -------------------------------------------------------------------------------------

def test_an_admin_gets_a_pending_state_not_a_session_and_the_cookie_holds_no_user_id(app, conn):
    admin = add(conn, 'root', admin=True)
    client = app.test_client()
    r = login(client, 'root', GOOD)
    assert r.get_json()['success'] and r.get_json()['step'] == 'enroll'              # admin sans facteur (B5) : enrôlement
    with client.session_transaction(**KW) as s:
        assert 'user_id' not in s and 'token' not in s
        assert set(s) == {'pending_id'}
        assert str(admin['id']) != s['pending_id']
    assert client.get('/api/activities', **KW).status_code in (401, 302)


def test_pending_cookie_alone_opens_nothing_and_unknown_ids_are_ignored(app, conn):
    add(conn, 'root', admin=True)
    client = app.test_client()
    with client.session_transaction(**KW) as s:
        s['pending_id'] = 'inventé'
    r = client.get('/auth/password/enroll', **KW)
    assert r.status_code == 302 and '/auth/password/login' in r.headers['Location']


def test_pending_state_expires_after_five_minutes_server_side(tmp_path):
    now = [1000.0]
    store = PendingStore(clock=lambda: now[0])
    pid = store.create(7, 'mfa')
    assert store.get(pid).user_id == 7
    now[0] += 299
    assert store.get(pid) is not None
    now[0] += 2
    assert store.get(pid) is None and store.hit(pid) is None


def test_replaying_a_cookie_does_not_reset_the_attempt_counter():
    store = PendingStore(max_tries=3)
    pid = store.create(7, 'mfa')
    assert [store.hit(pid) for _ in range(3)] == [2, 1, 0]
    assert store.hit(pid) is None and store.get(pid) is None                          # épuisé : détruit, rejouer ne redonne rien
    assert store.consume(pid) is None


def test_the_pending_state_is_destroyed_when_the_limit_is_reached_via_the_route(app, conn):
    add(conn, 'root', admin=True)
    admin_pending = app.extensions['pending']
    client = app.test_client()
    # état d'étape mfa créé directement (les facteurs arrivent avec T11)
    row = conn.execute("SELECT id, session_token, password_changed_at FROM users WHERE username='root'").fetchone()
    pid = admin_pending.create(row['id'], 'mfa', binding=(row['session_token'], row['password_changed_at']))
    with client.session_transaction(**KW) as s:
        s['pending_id'] = pid
    token = csrf_of(client)
    codes = []
    for _ in range(6):
        r = client.post('/auth/password/mfa', json={'code': '000000'}, **KW, headers={'X-CSRF-Token': token, 'Origin': f'https://{HOST}'})
        codes.append(r.status_code)
    assert 401 in codes and codes[-1] in (401, 302) and admin_pending.get(pid) is None      # 5e essai : 401, puis état détruit


def test_a_new_login_replaces_the_pending_state_of_the_cookie(app, conn):
    add(conn, 'root', admin=True)
    client = app.test_client()
    login(client, 'root', GOOD)
    with client.session_transaction(**KW) as s:
        first = s['pending_id']
    login(client, 'root', GOOD)
    with client.session_transaction(**KW) as s:
        assert s['pending_id'] != first


# --- T10-1 : l'état intermédiaire est lié à l'état du compte --------------------------------------------------------------------

def pending_cookie(app, conn, name='root'):
    add(conn, name, admin=True)
    client = app.test_client()
    assert login(client, name, GOOD).get_json()['step'] == 'enroll'
    with client.session_transaction(**KW) as s:
        return client, s['pending_id']


def still_valid(client):
    r = client.get('/auth/password/enroll', **KW)
    return r.status_code == 200                    # la page d'enrôlement s'affiche : l'état est valide


@pytest.mark.parametrize('change', [
    "UPDATE users SET session_token = 'autre'", "UPDATE users SET is_active = 0", "UPDATE users SET password_changed_at = '2030-01-01T00:00:00Z'",
    "UPDATE users SET auth_method = 'oidc'", "DELETE FROM users",
])
def test_a_pending_state_does_not_survive_a_change_of_the_account_T10_1(app, conn, change):
    client, pid = pending_cookie(app, conn)
    assert still_valid(client)
    conn.execute(change)
    conn.commit()
    r = client.get('/auth/password/enroll', **KW)
    assert r.status_code == 302 and '/auth/password/login' in r.headers['Location']
    assert app.extensions['pending'].get(pid) is None                            # détruit, pas seulement ignoré


def test_consuming_a_reset_token_destroys_the_pending_states_of_the_account_T10_1(app, conn):
    client, pid = pending_cookie(app, conn)
    user = conn.execute("SELECT id FROM users WHERE username = 'root'").fetchone()
    new = tokens.create(conn, 'reset', actor=CLI, target_user_id=user['id'])
    page = client.get('/auth/token', **KW).get_data(as_text=True)
    csrf = re.search(r'name="csrf-token" content="([^"]+)"', page).group(1)
    r = client.post('/auth/token/consume', json={'token': new.token, 'password': 'un-autre-mot-de-passe-91'}, **KW,
                    headers={'X-CSRF-Token': csrf, 'Origin': f'https://{HOST}'})
    assert r.status_code == 200 and r.get_json()['next'] == '/auth/password/login'          # T10-3 : jamais chez l'IdP
    assert app.extensions['pending'].get(pid) is None and not still_valid(client)


def test_deactivating_or_deleting_the_account_through_the_api_destroys_its_pending_states_T10_1(app, conn, monkeypatch):
    admin = add(conn, 'chef', admin=True)
    monkeypatch.setattr(factors, 'has_second_factor', lambda *a: True)        # un admin password exige un facteur (T11)
    other = add(conn, 'cible')
    store = app.extensions['pending']
    pid = store.create(other['id'], 'enroll')
    client = app.test_client()
    with client.session_transaction(**KW) as s:
        s['user_id'], s['token'], s['login_at'] = admin['id'], 'tok', int(time.time())
    headers = {'Origin': f'https://{HOST}'}
    assert client.patch(f"/api/users/{other['id']}", json={'is_active': False}, headers=headers, **KW).status_code == 200
    assert store.get(pid) is None
    pid = store.create(other['id'], 'enroll')
    assert client.delete(f"/api/users/{other['id']}", headers=headers, **KW).status_code == 200
    assert store.get(pid) is None


def test_the_throttle_audit_has_one_line_per_ip_when_the_ip_limit_refuses_T10_2(app, conn):
    app.extensions['limiters'] = Limiters(limits=Limits(password_per_identifier=20, password_per_ip=10), clock=lambda: 0.0)
    client = app.test_client()
    for i in range(60):
        login(client, f'inventé{i}', 'x' * 14)
    rows = conn.execute("SELECT details FROM audit_log WHERE action = 'auth.rate_limited'").fetchall()
    assert len(rows) == 1


def test_the_saturated_gate_429_carries_a_fixed_retry_after_T10_n1(app, conn, monkeypatch):
    add(conn, 'alice')
    monkeypatch.setattr(passwords, '_gate', CountingGate())
    a, b = login(app.test_client(), 'alice', GOOD), login(app.test_client(), 'inconnu', GOOD)
    assert a.headers['Retry-After'] == b.headers['Retry-After'] == '2'


# --- CA-45 : politique all => enrôlement ---------------------------------------------------------------------------------------

def test_policy_all_sends_a_plain_user_to_enrollment_and_policy_admins_does_not(app, conn):
    add(conn, 'alice')
    assert login(app.test_client(), 'alice', GOOD).get_json().get('step') is None
    force_setting(conn, app.config['SETTINGS_STORE'], 'auth.mfa_policy', 'all')
    client = app.test_client()
    r = login(client, 'alice', GOOD)
    assert r.get_json()['step'] == 'enroll' and r.get_json()['next'] == '/auth/password/enroll'
    with client.session_transaction(**KW) as s:
        assert 'user_id' not in s


# --- CA-14 / CA-46 : limiteur avant hachage ------------------------------------------------------------------------------------

class CountingGate:
    def __init__(self):
        self.calls = 0

    def slot(self):
        self.calls += 1
        raise passwords.HashBusy()


def test_a_limited_request_never_reaches_the_hash_gate(app, conn, monkeypatch):
    add(conn, 'alice')
    now = [0.0]
    app.extensions['limiters'] = Limiters(limits=Limits(password_per_identifier=3, password_per_ip=100), clock=lambda: now[0])
    client = app.test_client()
    assert [login(client, 'alice', 'x' * 14).status_code for _ in range(3)] == [401, 401, 401]
    gate = CountingGate()
    monkeypatch.setattr(passwords, '_gate', gate)
    r = login(client, 'alice', GOOD)
    assert r.status_code == 429 and 'Retry-After' in r.headers and gate.calls == 0       # CA-46
    other = login(client, 'ALICE ', GOOD)                                                # même identifiant normalisé
    assert other.status_code == 429 and gate.calls == 0


def test_the_limit_applies_to_unknown_identifiers_like_known_ones_and_expires_with_the_window(app, conn):
    now = [0.0]
    app.extensions['limiters'] = Limiters(limits=Limits(password_per_identifier=3, password_per_ip=100), clock=lambda: now[0])
    add(conn, 'alice')
    client = app.test_client()
    for name in ('alice', 'fantome'):
        codes = [login(client, name, 'x' * 14).status_code for _ in range(4)]
        assert codes == [401, 401, 401, 429], name
    now[0] += 15 * 60 + 1
    assert login(client, 'alice', GOOD).status_code == 200                                # CA-7 : fenêtre glissante


def test_a_saturated_hash_gate_answers_the_same_429_for_everyone(app, conn, monkeypatch):
    add(conn, 'alice')
    monkeypatch.setattr(passwords, '_gate', CountingGate())
    a, b = login(app.test_client(), 'alice', GOOD), login(app.test_client(), 'inconnu', GOOD)
    assert (a.status_code, a.get_data()) == (b.status_code, b.get_data()) and a.status_code == 429


def test_the_throttle_audit_has_one_line_per_identifier_and_ip_not_one_per_failure(app, conn):
    app.extensions['limiters'] = Limiters(limits=Limits(password_per_identifier=3, password_per_ip=100), clock=lambda: 0.0)
    client = app.test_client()
    for _ in range(8):
        login(client, 'alice', 'x' * 14)
    rows = conn.execute("SELECT details FROM audit_log WHERE action = 'auth.rate_limited'").fetchall()
    assert len(rows) == 1


def test_the_pages_and_endpoints_do_not_exist_outside_password_mode(tmp_path):
    other = create_app(TestingConfig, overrides={'DATA_DIR': str(tmp_path / 'local')})
    client = other.test_client()
    assert client.get('/auth/password/login').status_code == 404
    assert client.post('/auth/password/login', json={'username': 'a', 'password': 'b'}).status_code in (400, 404)
    assert client.post('/auth/password/forgot', json={'identifier': 'a'}).status_code in (400, 404)


# --- CA-15 : mot de passe oublié -----------------------------------------------------------------------------------------------

@pytest.fixture
def smtp():
    server = FakeSMTP()
    yield server
    server.stop()


@pytest.fixture
def mail_app(tmp_path, smtp):
    application = make_app(tmp_path / 'mail', OUTBOUND_ALLOWED_HOSTS=f'smtp=127.0.0.1:{smtp.port}')
    c = database.connect(application.config['DB_PATH'])
    application.config['SETTINGS_STORE'].set_many(c, {'smtp.host': '127.0.0.1', 'smtp.port': smtp.port, 'smtp.security': 'none',
                                                      'smtp.sender': 'noreply@example.org', 'smtp.timeout_s': 2}, actor=CLI)
    c.close()
    return application


def forgot(client, identifier):
    return client.post('/auth/password/forgot', json={'identifier': identifier}, **KW,
                       headers={'X-CSRF-Token': csrf_of(client, '/auth/password/forgot'), 'Origin': f'https://{HOST}'})


def test_forgot_answers_identically_for_every_kind_of_account_and_sends_one_mail_to_the_eligible_one(mail_app, smtp):
    c = database.connect(mail_app.config['DB_PATH'])
    add(c, 'alice', email='alice@example.org')
    add(c, 'unvalidated')
    add(c, 'sso', method='oidc', email='sso@example.org')
    add(c, 'off', active=False, email='off@example.org')
    c.close()
    queue = mail_app.extensions['mailer'].queue
    client = mail_app.test_client()
    answers = {(r.status_code, r.get_data()) for r in (forgot(client, name) for name in
                                                      ('alice', 'unvalidated', 'sso', 'off', 'inconnu', 'alice@example.org'))}
    assert len(answers) == 1 and next(iter(answers))[0] == 200
    queue.drain()
    assert len(smtp.messages) == 2                                  # « alice » puis « alice@example.org » : un message par demande
    assert all('/auth/token#' in m for m in smtp.messages)


def test_forgot_work_runs_only_in_the_queue_thread_never_in_the_request(mail_app, smtp, monkeypatch):
    c = database.connect(mail_app.config['DB_PATH'])
    add(c, 'alice', email='alice@example.org')
    c.close()
    mailer = mail_app.extensions['mailer']
    threads = []
    real = mailer.send

    def spy(*args, **kwargs):
        threads.append(threading.get_ident())
        return real(*args, **kwargs)

    monkeypatch.setattr(mailer, 'send', spy)
    mailer.queue.start()
    try:
        client = mail_app.test_client()
        forgot(client, 'alice')
        forgot(client, 'inconnu')
        assert mailer.queue.join(5)
    finally:
        mailer.queue.stop()
    assert len(threads) == 1 and threads[0] != threading.get_ident()


def test_forgot_response_time_does_not_depend_on_the_account(mail_app, monkeypatch):
    """Rien de la recherche ne tourne dans la requête : une file qui n'est pas vidée ne change pas la réponse."""
    c = database.connect(mail_app.config['DB_PATH'])
    add(c, 'alice', email='alice@example.org')
    c.close()
    client = mail_app.test_client()
    forgot(client, 'warm')
    t = {}
    for name in ('alice', 'inconnu'):
        start = time.perf_counter()
        forgot(client, name)
        t[name] = time.perf_counter() - start
    assert mail_app.extensions['mailer'].queue.pending() >= 2                  # rien n'a été traité dans la requête
    assert abs(t['alice'] - t['inconnu']) < 0.25


def test_forgot_token_is_a_reset_token_for_the_account_valid_one_hour_and_single_use(mail_app):
    c = database.connect(mail_app.config['DB_PATH'])
    user = add(c, 'alice', email='alice@example.org')
    forgot(mail_app.test_client(), 'alice')
    mail_app.extensions['mailer'].queue.drain()
    row = c.execute("SELECT kind, target_user_id, expires_at, created_at FROM auth_tokens").fetchall()
    assert len(row) == 1 and row[0]['kind'] == 'reset' and row[0]['target_user_id'] == user['id']
    c.close()


def test_forgot_is_unavailable_without_smtp_or_when_disabled(app, mail_app):
    assert app.test_client().post('/auth/password/forgot', json={'identifier': 'a'}, **KW).status_code == 404
    c = database.connect(mail_app.config['DB_PATH'])
    mail_app.config['SETTINGS_STORE'].set_many(c, {'auth.forgot_enabled': False}, actor=CLI)
    c.close()
    assert mail_app.test_client().get('/auth/password/forgot', **KW).status_code == 404


def test_forgot_is_rate_limited_and_the_limit_answer_is_generic(mail_app):
    mail_app.extensions['limiters'] = Limiters(limits=Limits(forgot_per_identifier=3, forgot_per_ip=100), clock=lambda: 0.0)
    client = mail_app.test_client()
    codes = [forgot(client, 'alice').status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    assert forgot(client, 'autre').status_code == 200


# --- CA-16 : la page de connexion ne révèle rien -------------------------------------------------------------------------------

def test_login_page_exposes_forgot_link_only_when_available(app, mail_app):
    assert 'password/forgot' not in app.test_client().get('/auth/password/login', **KW).get_data(as_text=True)
    assert 'password/forgot' in mail_app.test_client().get('/auth/password/login', **KW).get_data(as_text=True)


def test_every_public_response_carries_no_store_and_security_headers(app):
    r = app.test_client().get('/auth/password/login', **KW)
    assert r.status_code == 200 and 'no-store' in r.headers.get('Cache-Control', '') and 'Content-Security-Policy' in r.headers
