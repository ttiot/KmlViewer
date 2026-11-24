"""Compte personnel : mot de passe et adresse (T13 : EF-1.7, EF-4.6, EF-4.10, EF-1.2 collision SSO)."""

import time

import pytest
from cryptography.fernet import Fernet

from app import create_app, db as database
from app.audit import Actor
from app.auth import emails, passwords, second_factor, users
from app.config import TestingConfig
from tests.fake_smtp import FakeSMTP

KEY = Fernet.generate_key().decode()
HOST = 'traces.example.com'
KW = {'base_url': f'https://{HOST}'}
GOOD = 'cheval-agrafe-lune-42'
NEW = 'un-autre-mot-de-passe-91'
HASH = passwords.hash_password(GOOD)


class Prod(TestingConfig):
    TESTING = False
    INBOX_ENABLED = False
    SECRET_KEY = 'k' * 40
    SESSION_COOKIE_SECURE = None


@pytest.fixture
def smtp():
    server = FakeSMTP()
    yield server
    server.stop()


@pytest.fixture
def app(tmp_path, smtp):
    application = create_app(Prod, overrides={'DATA_DIR': str(tmp_path / 'data'), 'AUTH_MODE': 'password', 'TRUSTED_HOSTS': [HOST],
                                              'SECRETS_KEY': KEY, 'PUBLIC_URL': f'https://{HOST}',
                                              'OUTBOUND_ALLOWED_HOSTS': f'smtp=127.0.0.1:{smtp.port}'})
    c = database.connect(application.config['DB_PATH'])
    application.config['SETTINGS_STORE'].set_many(c, {'smtp.host': '127.0.0.1', 'smtp.port': smtp.port, 'smtp.security': 'none',
                                                      'smtp.sender': 'noreply@example.org', 'smtp.timeout_s': 2}, actor=Actor.cli())
    c.close()
    return application


@pytest.fixture
def conn(app):
    c = database.connect(app.config['DB_PATH'])
    yield c
    c.close()


def add(conn, name, *, email=None):
    conn.execute("INSERT INTO users (sub, username, email, is_active, auth_method, password_hash, session_token) "
                 "VALUES (?, ?, ?, 1, 'password', ?, 'tok')", (f'pwd:{name}', name, email, HASH))
    conn.commit()
    return conn.execute("SELECT * FROM users WHERE username = ?", (name,)).fetchone()


def session_for(app, user):
    client = app.test_client()
    with client.session_transaction(**KW) as s:
        s['user_id'], s['token'], s['login_at'] = user['id'], user['session_token'], int(time.time())
    return client


def call(client, path, payload):
    return client.post(path, json=payload, **KW, headers={'Origin': f'https://{HOST}'})


def with_totp(app, conn, user):
    """Active un TOTP (par le magasin) ; retourne le secret."""
    secret = second_factor.new_secret()
    conn.execute("UPDATE users SET totp_secret = ?, totp_enabled_at = '2026-01-01T00:00:00Z' WHERE id = ?",
                 (app.extensions['secrets'].encrypt_value(secret.encode()), user['id']))
    conn.commit()
    return secret


def code(secret, offset=1):
    import pyotp
    return pyotp.TOTP(secret).at(time.time() + 30 * offset)


# --- Mot de passe (EF-1.7) -------------------------------------------------------------------------------------------------

def test_password_change_needs_the_current_password_and_revokes_the_other_sessions(app, conn, smtp):
    user = add(conn, 'alice', email='alice@example.org')
    current, other = session_for(app, user), session_for(app, user)
    assert call(current, '/api/me/password', {'current_password': 'mauvais-mot-de-passe-1', 'new_password': NEW}).status_code == 403
    assert call(current, '/api/me/password', {'current_password': GOOD, 'new_password': 'court'}).status_code == 400
    assert call(current, '/api/me/password', {'current_password': GOOD}).status_code == 400
    ok = call(current, '/api/me/password', {'current_password': GOOD, 'new_password': NEW})
    assert ok.status_code == 200
    assert current.get('/api/activities', **KW).status_code == 200                  # la session courante est réémise
    assert other.get('/api/activities', **KW).status_code == 401                    # les autres sont fermées
    row = conn.execute("SELECT password_hash, password_changed_at FROM users WHERE id = ?", (user['id'],)).fetchone()
    assert passwords.verify_password(row['password_hash'], NEW)[0] and row['password_changed_at']
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'user.password_changed'").fetchone()[0] == 1
    assert app.extensions['mailer'].queue.drain() == 1 and 'Ton mot de passe a' in smtp.messages[0]


def test_password_change_kills_the_pending_states_of_the_account(app, conn):
    user = add(conn, 'alice')
    pid = app.extensions['pending'].create(user['id'], 'mfa')
    assert call(session_for(app, user), '/api/me/password', {'current_password': GOOD, 'new_password': NEW}).status_code == 200
    assert app.extensions['pending'].get(pid) is None


def test_password_change_requires_the_second_factor_when_there_is_one(app, conn):
    user = add(conn, 'alice')
    secret = with_totp(app, conn, user)
    client = session_for(app, user)
    assert call(client, '/api/me/password', {'current_password': GOOD, 'new_password': NEW}).status_code == 403
    assert call(client, '/api/me/password', {'current_password': GOOD, 'new_password': NEW, 'code': '000000'}).status_code == 403
    ok = call(client, '/api/me/password', {'current_password': GOOD, 'new_password': NEW, 'code': code(secret)})
    assert ok.status_code == 200
    assert call(client, '/api/me/password', {'current_password': NEW, 'new_password': GOOD + 'x', 'code': code(secret)}).status_code == 403


def test_a_wrong_current_password_is_limited_before_any_hashing(app, conn, monkeypatch):
    from app.ratelimit import Limiters, Limits
    user = add(conn, 'alice')
    app.extensions['limiters'] = Limiters(limits=Limits(password_per_identifier=3, password_per_ip=100), clock=lambda: 0.0)
    client = session_for(app, user)
    assert [call(client, '/api/me/password', {'current_password': 'mauvais-mot-de-passe-1', 'new_password': NEW}).status_code
            for _ in range(3)] == [403] * 3

    class Gate:
        calls = 0

        def slot(self):
            Gate.calls += 1
            raise passwords.HashBusy()

    monkeypatch.setattr(passwords, '_gate', Gate())
    assert call(client, '/api/me/password', {'current_password': GOOD, 'new_password': NEW}).status_code == 429 and Gate.calls == 0


def test_sso_accounts_and_anonymous_requests_cannot_use_the_personal_routes(app, conn):
    conn.execute("INSERT INTO users (sub, username, auth_method, session_token) VALUES ('idp:x', 'sso', 'oidc', 'tok')")
    conn.commit()
    sso = conn.execute("SELECT * FROM users WHERE username = 'sso'").fetchone()
    for path in ('/api/me/password', '/api/me/email'):
        assert call(app.test_client(), path, {}).status_code == 401
        assert call(session_for(app, sso), path, {'current_password': GOOD}).status_code in (401, 404)


# --- Adresse (EF-4.6) -----------------------------------------------------------------------------------------------------

def test_changing_the_address_keeps_the_old_one_active_notifies_it_and_mails_the_new_one(app, conn, smtp):
    from app.ratelimit import Limiters, Limits
    app.extensions['limiters'] = Limiters(limits=Limits(password_per_identifier=20, password_per_ip=100), clock=lambda: 0.0)
    user = add(conn, 'alice', email='old@example.org')
    client = session_for(app, user)
    assert call(client, '/api/me/email', {'password': 'mauvais-mot-de-passe-1', 'email': 'new@example.org'}).status_code == 403
    for bad in ('', 'a', 'a@@b.c', '<x@y.z>', None, 5, 'old@example.org', 'a@b..c'):
        assert call(client, '/api/me/email', {'password': GOOD, 'email': bad}).status_code in (400, 409), bad
    ok = call(client, '/api/me/email', {'password': GOOD, 'email': 'NEW@Example.ORG'})
    assert ok.status_code == 200 and ok.get_json()['pending_email'] == 'new@example.org'            # forme canonique
    row = conn.execute("SELECT email, pending_email FROM users WHERE id = ?", (user['id'],)).fetchone()
    assert (row['email'], row['pending_email']) == ('old@example.org', 'new@example.org')
    app.extensions['mailer'].queue.drain()
    joined = '\n'.join(smtp.messages)
    assert 'old@example.org' in joined and 'new@example.org' in joined and '/auth/token#t=' in joined


def test_the_answer_is_the_same_whether_the_address_is_free_or_taken(app, conn):
    add(conn, 'holder', email='taken@example.org')
    user = add(conn, 'alice')
    client = session_for(app, user)
    free = call(client, '/api/me/email', {'password': GOOD, 'email': 'free@example.org'})
    taken = call(client, '/api/me/email', {'password': GOOD, 'email': 'taken@example.org'})
    assert (free.status_code, set(free.get_json())) == (taken.status_code, set(taken.get_json()))


def test_changing_the_address_requires_the_second_factor_when_there_is_one(app, conn):
    user = add(conn, 'alice')
    secret = with_totp(app, conn, user)
    client = session_for(app, user)
    assert call(client, '/api/me/email', {'password': GOOD, 'email': 'x@example.org'}).status_code == 403
    assert call(client, '/api/me/email', {'password': GOOD, 'email': 'x@example.org', 'code': code(secret)}).status_code == 200


# --- SSO : adresses canoniques (EF-4.10) ------------------------------------------------------------------------------------------

def test_canonical_addresses_are_the_stored_form(app, conn):
    row = users.upsert_from_claims(conn, {'sub': 'idp-2', 'preferred_username': 'x', 'email': ' Ｍａｘ@Example.ORG ', 'email_verified': True})
    assert row['email'] == emails.canonical_email(' Ｍａｘ@Example.ORG ') == 'max@example.org'
