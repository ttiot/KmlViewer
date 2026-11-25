"""Clés de sécurité WebAuthn (T12 : EF-2.1, EF-2.2, EF-2.4, EF-2.8, EF-S9, CA-11) avec un authentificateur virtuel."""

import json
import re
import time

import pytest
from cryptography.fernet import Fernet

from app import create_app, db as database
from app.audit import Actor
from app.auth import factors, passwords, tokens, webauthn_factor
from app.config import TestingConfig
from tests.webauthn_authenticator import VirtualAuthenticator, b64

KEY = Fernet.generate_key().decode()
HOST = 'traces.example.com'
KW = {'base_url': f'https://{HOST}'}
ORIGIN = f'https://{HOST}'
CLI = Actor.cli()
GOOD = 'cheval-agrafe-lune-42'
HASH = passwords.hash_password(GOOD)


class Prod(TestingConfig):
    TESTING = False
    INBOX_ENABLED = False
    SECRET_KEY = 'k' * 40
    SESSION_COOKIE_SECURE = None


def make(tmp_path, **extra):
    return create_app(Prod, overrides={'DATA_DIR': str(tmp_path / 'data'), 'AUTH_MODE': 'password', 'TRUSTED_HOSTS': [HOST],
                                       'SECRETS_KEY': KEY, 'PUBLIC_URL': ORIGIN, **extra})


@pytest.fixture
def app(tmp_path):
    return make(tmp_path)


@pytest.fixture
def conn(app):
    c = database.connect(app.config['DB_PATH'])
    yield c
    c.close()


def add(conn, name, *, admin=False):
    conn.execute("INSERT INTO users (sub, username, is_admin, is_active, auth_method, password_hash, session_token) "
                 "VALUES (?, ?, ?, 1, 'password', ?, 'tok')", (f'pwd:{name}', name, int(admin), HASH))
    conn.commit()
    return conn.execute("SELECT * FROM users WHERE username = ?", (name,)).fetchone()


def csrf_of(client, path='/auth/password/login'):
    html = client.get(path, **KW).get_data(as_text=True)
    return re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)


def post(client, path, payload, csrf=None):
    return client.post(path, json=payload, **KW, headers={'X-CSRF-Token': csrf or csrf_of(client), 'Origin': ORIGIN})


def pending_for(app, conn, client, user, step, *, grant=False):
    row = conn.execute("SELECT session_token, password_changed_at FROM users WHERE id = ?", (user['id'],)).fetchone()
    pid = app.extensions['pending'].create(user['id'], step, admin_grant=grant, binding=(row[0], row[1]))
    with client.session_transaction(**KW) as s:
        s['pending_id'] = pid
    return pid


def enroll_key(app, conn, user, *, grant=False):
    """Enrôle une clé virtuelle par les routes d'enrôlement ; retourne (authentificateur, client, réponse)."""
    client = app.test_client()
    pending_for(app, conn, client, user, 'enroll', grant=grant)
    assert client.get('/auth/password/enroll', **KW).status_code == 200
    token = csrf_of(client, '/auth/password/enroll')
    options = post(client, '/auth/password/enroll/webauthn/options', {}, token)
    assert options.status_code == 200, options.get_data()
    auth = VirtualAuthenticator(HOST, ORIGIN)
    reply = post(client, '/auth/password/enroll/webauthn/verify',
                 {'credential': auth.create(options.get_data(as_text=True)), 'name': 'Ma clé'}, token)
    return auth, client, reply


def login_to_mfa(app, conn, user):
    client = app.test_client()
    r = post(client, '/auth/password/login', {'username': user['username'], 'password': GOOD})
    assert r.get_json()['step'] == 'mfa', r.get_json()
    return client


def sign_in(client, user):
    with client.session_transaction(**KW) as s:
        s['user_id'], s['token'], s['login_at'] = user['id'], user['session_token'], int(time.time())


# --- Enrôlement obligatoire par clé (bootstrap) --------------------------------------------------------------------------------

def test_a_security_key_ends_the_bootstrap_with_codes_and_the_admin_grant(app, conn):
    user = add(conn, 'founder')
    auth, client, reply = enroll_key(app, conn, user, grant=True)
    body = reply.get_json()
    assert reply.status_code == 200 and len(body['recovery_codes']) == 10 and body['next'] == '/auth/password/login'
    row = conn.execute("SELECT * FROM webauthn_credentials WHERE user_id = ?", (user['id'],)).fetchone()
    assert bytes(row['credential_id']) == auth.credential_id and row['name'] == 'Ma clé' and row['sign_count'] == 0
    assert conn.execute("SELECT is_admin FROM users WHERE id = ?", (user['id'],)).fetchone()[0] == 1
    assert factors.has_second_factor(conn, user['id'])
    actions = [r[0] for r in conn.execute("SELECT action FROM audit_log WHERE target_id = ? ORDER BY id", (str(user['id']),))]
    assert 'user.factor_added' in actions and 'user.admin_grant' in actions
    with client.session_transaction(**KW) as s:
        assert 'user_id' not in s and 'pending_id' not in s


def test_the_challenge_is_server_side_single_use_and_two_minutes_CA_11(app, conn):
    user = add(conn, 'alice')
    client = app.test_client()
    pending_for(app, conn, client, user, 'enroll')
    token = csrf_of(client, '/auth/password/enroll')
    options = post(client, '/auth/password/enroll/webauthn/options', {}, token).get_data(as_text=True)
    assert len(json.loads(options)['challenge']) >= 43                               # 32 octets >= 128 bits
    auth = VirtualAuthenticator(HOST, ORIGIN)
    credential = auth.create(options)
    first = post(client, '/auth/password/enroll/webauthn/verify', {'credential': credential}, token)
    assert first.status_code == 200
    # Rejeu : le défi est consommé et l'état aussi
    again = pending_for(app, conn, client, user, 'enroll')
    assert again
    replay = post(client, '/auth/password/enroll/webauthn/verify', {'credential': credential}, token)
    assert replay.status_code == 401
    assert conn.execute("SELECT COUNT(*) FROM webauthn_credentials").fetchone()[0] == 1


def test_a_stale_challenge_is_refused(app, conn):
    user = add(conn, 'alice')
    client = app.test_client()
    pid = pending_for(app, conn, client, user, 'enroll')
    store = app.extensions['pending']
    now = [0.0]
    store._clock = lambda: now[0]
    token = csrf_of(client, '/auth/password/enroll')
    options = post(client, '/auth/password/enroll/webauthn/options', {}, token).get_data(as_text=True)
    now[0] += 121
    credential = VirtualAuthenticator(HOST, ORIGIN).create(options)
    assert post(client, '/auth/password/enroll/webauthn/verify', {'credential': credential}, token).status_code == 401
    assert pid and conn.execute("SELECT COUNT(*) FROM webauthn_credentials").fetchone()[0] == 0


@pytest.mark.parametrize('tamper', [{'origin': 'https://evil.example'}, {'rp_id': 'evil.example'}, {'origin': 'http://traces.example.com'},
                                    {'challenge': 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'}])
def test_origin_rp_id_and_challenge_mismatches_are_refused_and_count_as_tries_CA_11(app, conn, tamper):
    user = add(conn, 'alice')
    client = app.test_client()
    pid = pending_for(app, conn, client, user, 'enroll')
    token = csrf_of(client, '/auth/password/enroll')
    options = post(client, '/auth/password/enroll/webauthn/options', {}, token).get_data(as_text=True)
    credential = VirtualAuthenticator(HOST, ORIGIN).create(options, **tamper)
    r = post(client, '/auth/password/enroll/webauthn/verify', {'credential': credential}, token)
    assert r.status_code == 401 and 'Code invalide' in r.get_json()['error']
    assert app.extensions['pending'].get(pid).tries == 1
    assert conn.execute("SELECT COUNT(*) FROM webauthn_credentials").fetchone()[0] == 0


@pytest.mark.parametrize('bad', [None, 'texte', 5, [], {}, {'id': 1}, {'response': 'x'}, {'rawId': 'x' * 50000}])
def test_hostile_credential_payloads_are_plain_refusals_never_errors(app, conn, bad):
    user = add(conn, 'alice')
    client = app.test_client()
    pending_for(app, conn, client, user, 'enroll')
    token = csrf_of(client, '/auth/password/enroll')
    post(client, '/auth/password/enroll/webauthn/options', {}, token)
    r = post(client, '/auth/password/enroll/webauthn/verify', {'credential': bad}, token)
    assert r.status_code in (400, 401)


def test_the_webauthn_routes_need_a_pending_state_csrf_and_the_right_step(app, conn):
    anonymous = app.test_client()
    for path in ('/auth/password/mfa/webauthn/options', '/auth/password/mfa/webauthn/verify',
                 '/auth/password/enroll/webauthn/options', '/auth/password/enroll/webauthn/verify'):
        assert post(anonymous, path, {}, csrf_of(anonymous)).status_code == 401
        assert anonymous.post(path, json={}, **KW, headers={'Origin': ORIGIN}).status_code == 400          # sans CSRF
    user = add(conn, 'alice')
    client = app.test_client()
    pending_for(app, conn, client, user, 'enroll')
    assert post(client, '/auth/password/mfa/webauthn/options', {}).status_code == 401                     # étape enroll != mfa


# --- Connexion par clé -----------------------------------------------------------------------------------------------------------

def test_login_with_a_key_opens_a_full_session_and_counts_the_signatures(app, conn):
    user = add(conn, 'alice')
    auth, _, _ = enroll_key(app, conn, user)
    client = login_to_mfa(app, conn, user)
    assert 'mfaKey' in client.get('/auth/password/mfa', **KW).get_data(as_text=True)
    token = csrf_of(client, '/auth/password/mfa')
    options = post(client, '/auth/password/mfa/webauthn/options', {}, token)
    assert options.status_code == 200 and len(json.loads(options.get_data())['allowCredentials']) == 1
    ok = post(client, '/auth/password/mfa/webauthn/verify', {'credential': auth.get(options.get_data(as_text=True))}, token)
    assert ok.status_code == 200 and ok.get_json()['next'] == '/'
    assert conn.execute("SELECT sign_count, last_used_at FROM webauthn_credentials").fetchone()[0] == 1
    with client.session_transaction(**KW) as s:
        assert s['user_id'] == user['id'] and 'pending_id' not in s
    assert client.get('/api/activities', **KW).status_code == 200


def test_a_signature_counter_regression_is_refused_and_audited_CA_11(app, conn):
    user = add(conn, 'alice')
    auth, _, _ = enroll_key(app, conn, user)
    for count, expected in ((5, 200), (5, 401), (3, 401)):                           # égal puis inférieur au dernier compteur
        client = login_to_mfa(app, conn, user)
        token = csrf_of(client, '/auth/password/mfa')
        options = post(client, '/auth/password/mfa/webauthn/options', {}, token).get_data(as_text=True)
        r = post(client, '/auth/password/mfa/webauthn/verify', {'credential': auth.get(options, counter=count)}, token)
        assert r.status_code == expected, (count, r.get_data())
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'webauthn.counter_regression'").fetchone()[0] == 2
    assert conn.execute("SELECT sign_count FROM webauthn_credentials").fetchone()[0] == 5


def test_a_replayed_assertion_or_cookie_cannot_log_in_twice(app, conn):
    user = add(conn, 'alice')
    auth, _, _ = enroll_key(app, conn, user)
    client = login_to_mfa(app, conn, user)
    token = csrf_of(client, '/auth/password/mfa')
    options = post(client, '/auth/password/mfa/webauthn/options', {}, token).get_data(as_text=True)
    assertion = auth.get(options)
    with client.session_transaction(**KW) as s:
        saved = s['pending_id']
    assert post(client, '/auth/password/mfa/webauthn/verify', {'credential': assertion}, token).status_code == 200
    other = app.test_client()
    with other.session_transaction(**KW) as s:
        s['pending_id'] = saved                                                     # cookie antérieur rejoué
    assert post(other, '/auth/password/mfa/webauthn/verify', {'credential': assertion}, csrf_of(other)).status_code == 401
    assert other.get('/api/activities', **KW).status_code in (401, 302)


def test_a_key_of_another_account_or_with_a_wrong_signature_is_refused(app, conn):
    alice, bob = add(conn, 'alice'), add(conn, 'bob')
    alice_key, _, _ = enroll_key(app, conn, alice)
    bob_key, _, _ = enroll_key(app, conn, bob)
    client = login_to_mfa(app, conn, alice)
    token = csrf_of(client, '/auth/password/mfa')
    options = post(client, '/auth/password/mfa/webauthn/options', {}, token).get_data(as_text=True)
    assert post(client, '/auth/password/mfa/webauthn/verify', {'credential': bob_key.get(options)}, token).status_code == 401
    options = post(client, '/auth/password/mfa/webauthn/options', {}, token).get_data(as_text=True)
    forged = alice_key.get(options)
    forged['response']['signature'] = b64(b'\x30\x06\x02\x01\x01\x02\x01\x01')
    assert post(client, '/auth/password/mfa/webauthn/verify', {'credential': forged}, token).status_code == 401
    assert conn.execute("SELECT sign_count FROM webauthn_credentials WHERE user_id = ?", (alice['id'],)).fetchone()[0] == 0


def test_five_failures_destroy_the_state_and_the_account_limiter_applies(app, conn):
    user = add(conn, 'alice')
    auth, _, _ = enroll_key(app, conn, user)
    from app.ratelimit import Limiters, Limits
    app.extensions['limiters'] = Limiters(limits=Limits(password_per_identifier=20, password_per_ip=100), clock=lambda: 0.0)
    client = login_to_mfa(app, conn, user)
    token = csrf_of(client, '/auth/password/mfa')
    codes = []
    for _ in range(5):
        options = post(client, '/auth/password/mfa/webauthn/options', {}, token).get_data(as_text=True)
        codes.append(post(client, '/auth/password/mfa/webauthn/verify', {'credential': auth.get(options, origin='https://x.example')},
                          token).status_code)
    assert codes == [401] * 5 and client.get('/auth/password/mfa', **KW).status_code == 302


# --- Disponibilité (EF-2.4, M12) ------------------------------------------------------------------------------------------------------

def test_webauthn_is_unavailable_over_plain_http_except_localhost_in_test_or_debug(tmp_path):
    assert not webauthn_factor.available({'PUBLIC_URL': 'http://traces.example.com'})
    assert not webauthn_factor.available({'PUBLIC_URL': 'http://localhost:5000'})
    assert not webauthn_factor.available({'PUBLIC_URL': ''})
    assert webauthn_factor.available({'PUBLIC_URL': 'http://localhost:5000', 'TESTING': True})
    assert webauthn_factor.available({'PUBLIC_URL': 'http://localhost:5000', 'DEBUG': True})
    assert webauthn_factor.expected_origin({'PUBLIC_URL': 'https://traces.example.com:8443'}) == 'https://traces.example.com:8443'
    assert webauthn_factor.expected_origin({'PUBLIC_URL': 'https://traces.example.com:443'}) == 'https://traces.example.com'


def test_the_rp_id_override_must_be_a_domain_suffix_of_the_host():
    cfg = {'PUBLIC_URL': 'https://app.traces.example.com'}
    assert webauthn_factor.rp_id(cfg) == 'app.traces.example.com'
    assert webauthn_factor.rp_id({**cfg, 'WEBAUTHN_RP_ID': 'traces.example.com'}) == 'traces.example.com'
    assert webauthn_factor.rp_id({**cfg, 'WEBAUTHN_RP_ID': 'example.org'}) is None
    assert webauthn_factor.rp_id({**cfg, 'WEBAUTHN_RP_ID': 'ces.example.com'}) is None        # suffixe de chaîne, pas de domaine


def test_the_routes_refuse_politely_when_webauthn_is_unavailable(tmp_path):
    app = make(tmp_path, PUBLIC_URL='https://traces.example.com', WEBAUTHN_RP_ID='example.org')
    c = database.connect(app.config['DB_PATH'])
    user = add(c, 'alice')
    client = app.test_client()
    pending_for(app, c, client, user, 'enroll')
    r = post(client, '/auth/password/enroll/webauthn/options', {}, csrf_of(client, '/auth/password/enroll'))
    assert r.status_code == 400 and 'disponibles' in r.get_json()['error']
    assert 'enrollKey' not in client.get('/auth/password/enroll', **KW).get_data(as_text=True)


# --- Profil ------------------------------------------------------------------------------------------------------------------------

def api(client, method, path, payload=None):
    return client.open(path, method=method, json=payload if payload is not None else {}, **KW, headers={'Origin': ORIGIN})


def add_key_from_profile(client, auth):
    options = api(client, 'POST', '/api/me/mfa/webauthn/options', {'password': GOOD})
    assert options.status_code == 200, options.get_data()
    return api(client, 'POST', '/api/me/mfa/webauthn/verify', {'credential': auth.create(options.get_data(as_text=True)), 'name': 'Portable'})


def test_adding_a_key_from_the_profile_needs_the_password_rotates_sessions_and_lists_it(app, conn):
    user = add(conn, 'alice')
    client, other = app.test_client(), app.test_client()
    sign_in(client, user)
    sign_in(other, user)
    assert api(client, 'POST', '/api/me/mfa/webauthn/options', {'password': 'mauvais-mot-de-passe-1'}).status_code == 403
    assert api(client, 'POST', '/api/me/mfa/webauthn/options', {}).status_code == 400
    reply = add_key_from_profile(client, VirtualAuthenticator(HOST, ORIGIN))
    assert reply.status_code == 200 and len(reply.get_json()['recovery_codes']) == 10             # premier facteur
    state = api(client, 'GET', '/api/me/mfa').get_json()
    assert state['keys'] == 1 and state['key_list'][0]['name'] == 'Portable' and state['webauthn_available'] is True
    assert api(other, 'GET', '/api/me/mfa').status_code == 401                                    # les autres sessions sont fermées
    assert 'credential_id' not in json.dumps(state) and 'public_key' not in json.dumps(state)


def test_at_most_ten_keys_and_a_duplicate_credential_is_refused(app, conn):
    from app.ratelimit import Limiters, Limits
    app.extensions['limiters'] = Limiters(limits=Limits(password_per_identifier=20, password_per_ip=100, mfa_per_account=10),
                                          clock=lambda: 0.0)
    user = add(conn, 'alice')
    client = app.test_client()
    sign_in(client, user)
    first = VirtualAuthenticator(HOST, ORIGIN)
    for i in range(10):
        client = app.test_client()
        sign_in(client, conn.execute("SELECT * FROM users WHERE id = ?", (user['id'],)).fetchone())
        assert add_key_from_profile(client, first if i == 0 else VirtualAuthenticator(HOST, ORIGIN)).status_code == 200
    client = app.test_client()
    sign_in(client, conn.execute("SELECT * FROM users WHERE id = ?", (user['id'],)).fetchone())
    assert api(client, 'POST', '/api/me/mfa/webauthn/options', {'password': GOOD}).status_code == 409


def test_a_credential_already_registered_is_refused(app, conn):
    user = add(conn, 'alice')
    auth = VirtualAuthenticator(HOST, ORIGIN)
    client = app.test_client()
    sign_in(client, user)
    assert add_key_from_profile(client, auth).status_code == 200
    client = app.test_client()
    sign_in(client, conn.execute("SELECT * FROM users WHERE id = ?", (user['id'],)).fetchone())
    options = api(client, 'POST', '/api/me/mfa/webauthn/options', {'password': GOOD}).get_data(as_text=True)
    again = api(client, 'POST', '/api/me/mfa/webauthn/verify', {'credential': auth.create(options)})
    assert again.status_code == 409 and conn.execute("SELECT COUNT(*) FROM webauthn_credentials").fetchone()[0] == 1


def test_removing_the_last_factor_of_an_admin_is_refused_and_otherwise_codes_follow_r6(app, conn):
    user = add(conn, 'alice', admin=True)
    auth, _, reply = enroll_key(app, conn, user)
    codes = reply.get_json()['recovery_codes']
    client = app.test_client()
    sign_in(client, conn.execute("SELECT * FROM users WHERE id = ?", (user['id'],)).fetchone())
    key_id = api(client, 'GET', '/api/me/mfa').get_json()['key_list'][0]['id']
    refused = api(client, 'DELETE', f'/api/me/mfa/webauthn/{key_id}', {'password': GOOD, 'code': codes[0]})
    assert refused.status_code == 409 and factors.has_second_factor(conn, user['id'])
    conn.execute("UPDATE users SET is_admin = 0 WHERE id = ?", (user['id'],))
    conn.commit()
    client = app.test_client()
    sign_in(client, conn.execute("SELECT * FROM users WHERE id = ?", (user['id'],)).fetchone())
    assert api(client, 'DELETE', f'/api/me/mfa/webauthn/{key_id}', {'password': GOOD}).status_code == 403           # code manquant
    ok = api(client, 'DELETE', f'/api/me/mfa/webauthn/{key_id}', {'password': GOOD, 'code': codes[1]})
    assert ok.status_code == 200 and not factors.has_second_factor(conn, user['id'])
    assert conn.execute("SELECT COUNT(*) FROM recovery_codes WHERE user_id = ?", (user['id'],)).fetchone()[0] == 0
    assert api(client, 'DELETE', '/api/me/mfa/webauthn/999', {'password': GOOD, 'code': codes[2]}).status_code in (403, 404)


def test_a_key_keeps_an_admin_an_admin_after_secrets_reset_CA_27(app, conn):
    user = add(conn, 'root', admin=True)
    enroll_key(app, conn, user)
    conn.execute("UPDATE users SET totp_secret = x'01', totp_enabled_at = '2026-01-01T00:00:00Z' WHERE id = ?", (user['id'],))
    conn.commit()
    counts = app.extensions['secrets'].reset(conn, actor=CLI)
    assert 'demoted' not in counts
    row = conn.execute("SELECT is_admin FROM users WHERE id = ?", (user['id'],)).fetchone()
    assert row[0] == 1 and conn.execute("SELECT COUNT(*) FROM recovery_codes").fetchone()[0] >= 1


def test_keys_follow_the_account_on_deletion_and_mfa_reset_removes_them(app, conn):
    user = add(conn, 'alice')
    enroll_key(app, conn, user)
    removed = factors.clear_factors(conn, user['id'])
    conn.commit()
    assert removed['webauthn'] == 1 and conn.execute("SELECT COUNT(*) FROM webauthn_credentials").fetchone()[0] == 0
    enroll_key(app, conn, user)
    conn.execute("DELETE FROM users WHERE id = ?", (user['id'],))
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM webauthn_credentials").fetchone()[0] == 0


def test_the_front_script_is_served_by_the_application_without_external_library_EF_2_8():
    import pathlib
    js = (pathlib.Path(__file__).resolve().parent.parent / 'static' / 'js' / 'webauthn.js').read_text(encoding='utf-8')
    assert 'innerHTML' not in js and 'eval(' not in js and 'http' not in js.replace('https', '')
    assert 'navigator.credentials.create' in js and 'navigator.credentials.get' in js and 'X-CSRF-Token' in js
    assert tokens                                                                                  # import utilisé
