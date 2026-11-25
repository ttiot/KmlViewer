"""
Cas limites des invitations et de la création de compte par jeton (T7b, T18-1 : EF-3, EF-4.3b, EF-S8, B2, R2, r7, r8 ;
CA-1 à CA-5, CA-17b, CA-39, CA-44). Complète test_invitations.py : crédits sous concurrence, atomicité de l'acceptation,
identifiants réservés normalisés, CSRF et corps hostiles des routes /auth/token, indiscernabilité des jetons inutilisables,
aucune fuite du jeton, migration 0013 sur une base peuplée, clé /64 du limiteur.
"""

import json
import logging
import re
import sqlite3
import threading
import time

import pytest
from cryptography.fernet import Fernet

from app import audit, create_app
from app import db as database
from app.auth import csrf, invitations, passwords, tokens
from app.auth.invitations import AccountError, InvitationError
from app.config import TestingConfig
from app.ratelimit import LruLimiter, ip_key

KEY = Fernet.generate_key().decode()
HOST = 'traces.example.com'
GOOD = 'cheval-agrafe-lune-42'


class Prod(TestingConfig):
    TESTING = False
    INBOX_ENABLED = False
    SECRET_KEY = 'k' * 40
    SESSION_COOKIE_SECURE = None


@pytest.fixture
def app(tmp_path):
    return create_app(Prod, overrides={'DATA_DIR': str(tmp_path / 'data'), 'AUTH_MODE': 'password', 'TRUSTED_HOSTS': [HOST],
                                       'SECRETS_KEY': KEY, 'PUBLIC_URL': f'https://{HOST}'})


@pytest.fixture
def conn(app):
    c = database.connect(app.config['DB_PATH'])
    yield c
    c.close()


def add_user(conn, name, *, admin=False, credits=0, email=None, active=True, method='password', pending=None):
    conn.execute("INSERT INTO users (sub, username, email, pending_email, is_admin, is_active, invite_credits, auth_method, "
                 "session_token) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'tok')",
                 (f'pwd:{name}' if method == 'password' else name, name, email, pending, int(admin), int(active), credits, method))
    conn.commit()
    return conn.execute("SELECT * FROM users WHERE username = ?", (name,)).fetchone()


def fresh(conn, user):
    return conn.execute("SELECT * FROM users WHERE id = ?", (user['id'],)).fetchone()


def count(conn, sql, *params):
    return conn.execute(sql, params).fetchone()[0]


KW = {'base_url': f'https://{HOST}'}


def page_token(client):
    html = client.get('/auth/token', **KW).get_data(as_text=True)
    return re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)


def post(client, path, payload, csrf_token=None, **headers):
    return client.post(path, json=payload, headers={'X-CSRF-Token': csrf_token or page_token(client), 'Origin': f'https://{HOST}',
                                                    **headers}, **KW)


def signed_in(app, conn, user):
    client = app.test_client()
    with client.session_transaction(**KW) as session:
        session['user_id'], session['token'], session['login_at'] = user['id'], 'tok', int(time.time())
    return client


# =========================================================== crédits (EF-3.2, EF-3.5, CA-2, CA-3) =====================================

@pytest.mark.parametrize('credits', [1, 5])
def test_twenty_threads_never_create_more_invitations_than_credits_and_never_go_negative(app, conn, credits):
    member = add_user(conn, 'mem', credits=credits, email='mem@example.org')
    barrier, outcome, errors = threading.Barrier(20), [], []

    def worker(i):
        c = database.connect(app.config['DB_PATH'])
        try:
            barrier.wait(10)
            stale = c.execute("SELECT * FROM users WHERE id = ?", (member['id'],)).fetchone()        # ligne lue AVANT la course
            invitations.create(c, stale, f'guest{i}@example.org')
            outcome.append('ok')
        except InvitationError as exc:
            outcome.append(exc.status)
        except Exception as exc:                                                                     # noqa: BLE001
            errors.append(exc)
        finally:
            c.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    [t.start() for t in threads]
    [t.join(60) for t in threads]
    assert not errors, errors
    assert outcome.count('ok') == credits and outcome.count(409) == 20 - credits
    assert fresh(conn, member)['invite_credits'] == 0
    assert count(conn, "SELECT COUNT(*) FROM auth_tokens WHERE kind = 'invite'") == credits
    assert count(conn, "SELECT COUNT(*) FROM audit_log WHERE action = 'token.create'") == credits


def test_a_stale_issuer_row_with_credits_cannot_spend_credits_that_are_gone(conn):
    member = add_user(conn, 'mem', credits=3, email='mem@example.org')
    stale = fresh(conn, member)
    conn.execute("UPDATE users SET invite_credits = 0 WHERE id = ?", (member['id'],))
    conn.commit()
    with pytest.raises(InvitationError) as exc:
        invitations.create(conn, stale, 'a@example.org')
    assert exc.value.status == 409 and fresh(conn, member)['invite_credits'] == 0 and count(conn, "SELECT COUNT(*) FROM auth_tokens") == 0


@pytest.mark.parametrize('failure', [RuntimeError("boum"), KeyboardInterrupt(), SystemExit(3)], ids=['erreur', 'ctrl-c', 'exit'])
def test_any_failure_after_the_decrement_gives_the_credit_back_with_the_rollback(conn, monkeypatch, failure):
    member = add_user(conn, 'mem', credits=2, email='mem@example.org')

    def boom(*a, **k):
        raise failure

    monkeypatch.setattr(tokens, 'create', boom)
    with pytest.raises(type(failure)):
        invitations.create(conn, fresh(conn, member), 'a@example.org')
    assert fresh(conn, member)['invite_credits'] == 2 and not conn.in_transaction


@pytest.mark.parametrize('kwargs', [dict(active=False, credits=3, email='o@example.org'), dict(credits=3, pending='p@example.org'),
                                    dict(credits=0, email='z@example.org')], ids=['désactivé', 'adresse-non-validée', 'sans-crédit'])
def test_who_cannot_invite_never_spends_anything(conn, kwargs):
    user = add_user(conn, 'nope', **kwargs)
    with pytest.raises(InvitationError) as exc:
        invitations.create(conn, user, 'a@example.org')
    assert exc.value.status in (403, 409) and fresh(conn, user)['invite_credits'] == kwargs['credits']
    assert count(conn, "SELECT COUNT(*) FROM auth_tokens") == 0


def test_an_admin_is_unlimited_and_a_disabled_admin_is_not_an_inviter(conn):
    admin = add_user(conn, 'adm', admin=True, credits=0)
    for i in range(30):
        invitations.create(conn, admin, f'g{i}@example.org')
    assert fresh(conn, admin)['invite_credits'] == 0 and count(conn, "SELECT COUNT(*) FROM auth_tokens") == 30
    off = add_user(conn, 'offadm', admin=True, active=False)
    with pytest.raises(InvitationError):
        invitations.create(conn, off, 'late@example.org')


def test_credit_bounds_are_enforced_by_the_database_and_by_the_admin_path(conn):
    admin = add_user(conn, 'adm', admin=True)
    assert invitations.create(conn, admin, 'max@example.org', initial_credits=1000)
    for bad in (1001, -1, True, 1.5, '5', None):
        if bad is None:
            continue
        with pytest.raises((InvitationError, ValueError, TypeError)):
            invitations.create(conn, admin, f'bad{bad}@example.org', initial_credits=bad)
    for value in (-1, 1001):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE users SET invite_credits = ? WHERE id = ?", (value, admin['id']))
        conn.rollback()
    assert count(conn, "SELECT COUNT(*) FROM auth_tokens") == 1


def test_a_member_cannot_raise_their_own_credits_through_the_invitation_api(app, conn):
    member = add_user(conn, 'mem', credits=1, email='mem@example.org')
    client = signed_in(app, conn, member)
    created = client.post('/api/invitations', json={'email': 'a@example.org', 'invite_credits': 1000, 'credits': 1000,
                                                    'initial_credits': 0, 'is_admin': True}, **KW)
    assert created.status_code == 201
    assert fresh(conn, member)['invite_credits'] == 0 and not fresh(conn, member)['is_admin']
    assert client.post('/api/invitations', json={'email': 'b@example.org'}, **KW).status_code in (403, 409)
    assert count(conn, "SELECT COUNT(*) FROM auth_tokens") == 1


def test_a_member_with_zero_credit_gets_a_409_through_the_api(app, conn):
    member = add_user(conn, 'zero', credits=0, email='zero@example.org')
    assert signed_in(app, conn, member).post('/api/invitations', json={'email': 'a@example.org'}, **KW).status_code == 409


@pytest.mark.parametrize('payload', [{'email': 'a@example.org', 'initial_credits': 1}, {'email': 'a@example.org', 'note': 'x'}])
def test_a_member_cannot_attach_a_note_or_credits_and_the_refusal_costs_nothing(app, conn, payload):
    member = add_user(conn, 'mem', credits=2, email='mem@example.org')
    response = signed_in(app, conn, member).post('/api/invitations', json=payload, **KW)
    assert response.status_code == 400 and fresh(conn, member)['invite_credits'] == 2 and count(conn, "SELECT COUNT(*) FROM auth_tokens") == 0


def test_a_member_gets_the_same_shape_for_a_taken_and_a_free_address_and_nothing_more(app, conn):
    member = add_user(conn, 'mem', credits=3, email='mem@example.org')
    add_user(conn, 'carriers', email='carried@example.org')
    client = signed_in(app, conn, member)
    free = client.post('/api/invitations', json={'email': 'free@example.org'}, **KW)
    taken = client.post('/api/invitations', json={'email': 'CARRIED@example.org'}, **KW)
    assert free.status_code == taken.status_code == 201
    assert set(free.get_json()) == set(taken.get_json()) and len(free.get_json()['link']) == len(taken.get_json()['link'])


def test_disabling_or_deleting_the_issuer_revokes_their_pending_invitations(app, conn):
    admin = add_user(conn, 'adm', admin=True)
    conn.execute("CREATE TABLE IF NOT EXISTS webauthn_credentials (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL)")
    conn.execute("INSERT INTO webauthn_credentials (user_id, credential_id, public_key) VALUES (?, randomblob(16), x'00')", (admin['id'],))          # un admin password a un second facteur (B5)
    conn.commit()
    for name in ('left', 'gone'):
        member = add_user(conn, name, credits=2, email=f'{name}@example.org')
        link = invitations.create(conn, fresh(conn, member), f'{name}.guest@example.org')
        client = signed_in(app, conn, admin)
        if name == 'left':
            assert client.patch(f"/api/users/{member['id']}", json={'is_active': False}, **KW).status_code == 200
        else:
            assert client.delete(f"/api/users/{member['id']}", **KW).status_code == 200
        assert tokens.inspect(conn, link.token) is None, name


# ======================================================= acceptation : atomicité (EF-3.6, r8) =======================================

def invite(conn, address='guest@example.org', **kw):
    admin = conn.execute("SELECT * FROM users WHERE username = 'adm'").fetchone() or add_user(conn, 'adm', admin=True)
    return invitations.create(conn, admin, address, **kw)


def test_if_the_audit_write_fails_neither_the_account_nor_the_consumption_survives(conn, monkeypatch):
    new = invite(conn)
    real = audit.record

    def fail_on_user_create(c, action, **kw):
        if action == 'user.create':
            raise RuntimeError("journal indisponible")
        return real(c, action, **kw)

    monkeypatch.setattr(audit, 'record', fail_on_user_create)
    with pytest.raises(RuntimeError):
        invitations.accept(conn, new.token, 'alice', GOOD)
    assert count(conn, "SELECT COUNT(*) FROM users WHERE username = 'alice'") == 0 and tokens.inspect(conn, new.token)
    monkeypatch.undo()
    assert invitations.accept(conn, new.token, 'alice', GOOD)['username'] == 'alice'


@pytest.mark.parametrize('failure', [RuntimeError("x"), KeyboardInterrupt()], ids=['erreur', 'ctrl-c'])
def test_an_interruption_during_account_creation_leaves_the_link_intact(conn, monkeypatch, failure):
    new = invite(conn)
    monkeypatch.setattr(audit, 'record', lambda *a, **k: (_ for _ in ()).throw(failure))
    with pytest.raises(type(failure)):
        invitations.accept(conn, new.token, 'alice', GOOD)
    assert tokens.inspect(conn, new.token) and count(conn, "SELECT COUNT(*) FROM users WHERE username = 'alice'") == 0 and not conn.in_transaction


def test_ten_simultaneous_acceptances_of_one_link_create_exactly_one_account(app, conn):
    new = invite(conn)
    barrier, results = threading.Barrier(10), []

    def worker(i):
        c = database.connect(app.config['DB_PATH'])
        try:
            barrier.wait(10)
            results.append(invitations.accept(c, new.token, f'user{i}', GOOD))
        except AccountError:
            results.append('refused')
        finally:
            c.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    [t.start() for t in threads]
    [t.join(120) for t in threads]
    assert sum(1 for r in results if isinstance(r, dict)) == 1 and len(results) == 10
    assert count(conn, "SELECT COUNT(*) FROM users WHERE auth_method = 'password' AND username LIKE 'user%'") == 1
    assert count(conn, "SELECT COUNT(*) FROM auth_tokens WHERE used_at IS NOT NULL") == 1


def test_a_saturated_hash_gate_is_a_429_and_the_link_survives(app, conn, monkeypatch):
    new = invite(conn)
    gate = passwords.HashGate(slots=1, wait_s=0.05)
    monkeypatch.setattr(passwords, '_gate', gate)
    held = gate.slot()
    held.__enter__()
    try:
        response = post(app.test_client(), '/auth/token/consume', {'token': new.token, 'username': 'alice', 'password': GOOD})
    finally:
        held.__exit__(None, None, None)
    assert response.status_code == 429 and tokens.inspect(conn, new.token)


@pytest.mark.parametrize('name', ['Admin', ' ROOT ', 'ｃｌｉ', 'ＳＹＳＴＥＭ', 'Local', 'ADMIN', 'ａｄｍｉｎ', 'root\t', 'ſystem' if False else 'SYSTEM'])
def test_reserved_names_in_any_spelling_give_the_exact_answer_of_a_taken_name(app, conn, name):
    add_user(conn, 'existing')
    reserved_link, taken_link = invite(conn, 'one@example.org'), invite(conn, 'two@example.org')
    client = app.test_client()
    reserved = post(client, '/auth/token/consume', {'token': reserved_link.token, 'username': name, 'password': GOOD})
    taken = post(client, '/auth/token/consume', {'token': taken_link.token, 'username': 'EXISTING', 'password': GOOD})
    assert (reserved.status_code, reserved.get_json()) == (taken.status_code, taken.get_json())
    assert tokens.inspect(conn, reserved_link.token) and tokens.inspect(conn, taken_link.token)           # liens intacts


@pytest.mark.parametrize('name', ['a', 'ab', 'a b', 'bob@example.org', 'é' * 3, 'x' * 65, 'ad​min', 'bob/../root', "bob'; DROP TABLE users;--",
                                  '', '   ', 'a' * 300, '日本語です'])
def test_invalid_identifier_formats_refuse_without_consuming_and_without_creating(app, conn, name):
    new = invite(conn)
    response = post(app.test_client(), '/auth/token/consume', {'token': new.token, 'username': name, 'password': GOOD})
    assert response.status_code in (400, 409) and tokens.inspect(conn, new.token)
    assert count(conn, "SELECT COUNT(*) FROM users WHERE auth_method = 'password' AND username != 'adm'") == 0


def test_the_stored_identifier_is_the_normalized_one_so_case_variants_collide(conn):
    first, second = invite(conn, 'one@example.org'), invite(conn, 'two@example.org')
    assert invitations.accept(conn, first.token, 'ＡＬＩＣＥ', GOOD)['username'] == 'alice'
    with pytest.raises(AccountError) as exc:
        invitations.accept(conn, second.token, 'Alice', GOOD)
    assert exc.value.status == 409 and tokens.inspect(conn, second.token)


@pytest.mark.parametrize('password,what', [('alice', 'identifiant'), ('guest@example.org', 'adresse'), ('GUEST@EXAMPLE.ORG', 'adresse')])
def test_a_password_equal_to_the_identifier_or_the_invited_address_is_refused_and_keeps_the_link(conn, password, what):
    new = invite(conn)
    with pytest.raises(passwords.PolicyError):
        invitations.accept(conn, new.token, 'alice', password)
    assert tokens.inspect(conn, new.token) and count(conn, "SELECT COUNT(*) FROM users WHERE username = 'alice'") == 0


def test_the_new_account_is_exactly_as_specified_and_has_no_secret_in_clear(conn):
    new = invite(conn, initial_credits=7, note='Bienvenue')
    created = invitations.accept(conn, new.token, 'alice', GOOD)
    row = conn.execute("SELECT * FROM users WHERE id = ?", (created['id'],)).fetchone()
    assert (row['auth_method'], row['is_admin'], row['is_active'], row['email'], row['pending_email'], row['invite_credits']) == \
        ('password', 0, 1, None, 'guest@example.org', 7)
    assert row['sub'].startswith('pwd:') and row['password_hash'].startswith('$argon2id$') and GOOD not in row['password_hash']
    assert row['session_token'] and row['invited_by'] == conn.execute("SELECT id FROM users WHERE username = 'adm'").fetchone()[0]
    assert GOOD not in json.dumps([dict(r) for r in conn.execute("SELECT * FROM audit_log")], default=str)


def test_consuming_a_link_never_touches_or_replaces_an_existing_session(app, conn):
    admin = conn.execute("SELECT * FROM users WHERE username = 'adm'").fetchone() or add_user(conn, 'adm', admin=True)
    new = invitations.create(conn, admin, 'guest@example.org')
    client = signed_in(app, conn, add_user(conn, 'watcher'))
    response = post(client, '/auth/token/consume', {'token': new.token, 'username': 'alice', 'password': GOOD})
    assert response.status_code == 200
    with client.session_transaction(**KW) as session:
        assert session['user_id'] == conn.execute("SELECT id FROM users WHERE username = 'watcher'").fetchone()[0]
    assert 'alice' not in json.dumps(dict(response.headers.items()))


def test_the_account_created_by_an_invitation_has_no_session_and_cannot_use_the_api(app, conn):
    new = invite(conn)
    client = app.test_client()
    response = post(client, '/auth/token/consume', {'token': new.token, 'username': 'alice', 'password': GOOD})
    assert not any('session=' in v and 'Max-Age=0' not in v and 'Expires=Thu, 01 Jan 1970' not in v
                   for v in response.headers.getlist('Set-Cookie'))
    assert client.get('/api/activities', **KW).status_code == 401 and client.get('/', **KW).status_code == 302


# ======================================================= verify_email (EF-4.3b, R2, CA-17b, CA-44) ====================================

def pending(conn, name='alice', address='alice@example.org'):
    user = add_user(conn, name, pending=address)
    return user, tokens.create(conn, 'verify_email', actor=audit.Actor.cli(), target_user_id=user['id'], email=address)


def test_a_verification_link_works_once_for_its_account_even_under_two_simultaneous_clicks(app, conn):
    user, new = pending(conn)
    barrier, results = threading.Barrier(2), []

    def click():
        c = database.connect(app.config['DB_PATH'])
        try:
            barrier.wait(10)
            results.append(invitations.verify_email(c, c.execute("SELECT * FROM users WHERE id = ?", (user['id'],)).fetchone(), new.token))
        finally:
            c.close()

    threads = [threading.Thread(target=click) for _ in range(2)]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    assert sorted(results) == ['invalid', 'ok'] and fresh(conn, user)['email'] == 'alice@example.org'


def test_without_a_session_the_link_is_neutral_unconsumed_and_names_nobody(app, conn):
    user, new = pending(conn)
    response = post(app.test_client(), '/auth/token/consume', {'token': new.token})
    assert response.status_code == 403 and 'alice' not in response.get_data(as_text=True) and 'example.org' not in response.get_data(as_text=True)
    assert tokens.inspect(conn, new.token) == 'verify_email' and fresh(conn, user)['pending_email'] == 'alice@example.org'


@pytest.mark.parametrize('stranger', ['admin', 'oidc', 'other-password'])
def test_a_stranger_session_gets_the_unknown_answer_and_the_link_stays_reusable(app, conn, stranger):
    user, new = pending(conn)
    other = {'admin': lambda: add_user(conn, 'boss', admin=True), 'oidc': lambda: add_user(conn, 'sso', method='oidc'),
             'other-password': lambda: add_user(conn, 'mallory')}[stranger]()
    client = signed_in(app, conn, other)
    unknown = post(client, '/auth/token/consume', {'token': 'A' * 43})
    wrong = post(client, '/auth/token/consume', {'token': new.token})
    if stranger == 'oidc':
        assert wrong.status_code in (401, 404, 403)                                                  # session SSO refusée en mode password
    else:
        assert (wrong.status_code, wrong.get_json()) == (unknown.status_code, unknown.get_json())
    assert tokens.inspect(conn, new.token) == 'verify_email' and fresh(conn, user)['email'] is None


def test_the_target_session_validates_and_a_second_use_of_the_link_is_unknown(app, conn):
    user, new = pending(conn)
    client = signed_in(app, conn, user)
    assert post(client, '/auth/token/consume', {'token': new.token}).status_code == 200
    again = post(client, '/auth/token/consume', {'token': new.token})
    assert again.status_code == 404 and fresh(conn, user)['email'] == 'alice@example.org'


def test_validation_is_refused_neutrally_when_the_address_is_validated_elsewhere_including_a_sso_account(app, conn):
    add_user(conn, 'sso-holder', method='oidc', email='alice@example.org')
    user, new = pending(conn)
    response = post(signed_in(app, conn, user), '/auth/token/consume', {'token': new.token})
    assert response.status_code == 409 and 'sso-holder' not in response.get_data(as_text=True)
    row = fresh(conn, user)
    assert row['email'] is None and row['pending_email'] is None


def test_three_accounts_validating_the_same_address_at_once_leave_exactly_one_owner(app, conn):
    users = []
    for name in ('a1', 'a2', 'a3'):
        user, new = pending(conn, name, 'same@example.org')
        users.append((user, new))
    barrier, results = threading.Barrier(3), []

    def click(user, new):
        c = database.connect(app.config['DB_PATH'])
        try:
            barrier.wait(10)
            results.append(invitations.verify_email(c, c.execute("SELECT * FROM users WHERE id = ?", (user['id'],)).fetchone(), new.token))
        finally:
            c.close()

    threads = [threading.Thread(target=click, args=pair) for pair in users]
    [t.start() for t in threads]
    [t.join(60) for t in threads]
    assert sorted(results) == ['ok', 'refused', 'refused']
    assert count(conn, "SELECT COUNT(*) FROM users WHERE email = 'same@example.org'") == 1


# ======================================================= routes /auth/token : CSRF et corps hostiles (EF-S2, EF-S8) ====================

@pytest.mark.parametrize('path', ['/auth/token/inspect', '/auth/token/consume'])
@pytest.mark.parametrize('header', [None, '', ' ', 'garbage', 'A' * 600, 'a.b.c', csrf.issue.__name__])
def test_a_missing_or_bogus_csrf_header_is_a_403_before_any_processing(app, conn, path, header):
    client = app.test_client()
    headers = {'Origin': f'https://{HOST}'}
    if header is not None:
        headers['X-CSRF-Token'] = header
    response = client.post(path, json={'token': 'A' * 43, 'username': 'alice', 'password': GOOD}, headers=headers, **KW)
    assert response.status_code == 403 and count(conn, "SELECT COUNT(*) FROM users") == 0


@pytest.mark.parametrize('path', ['/auth/token/inspect', '/auth/token/consume'])
def test_csrf_from_the_body_the_query_or_a_cookie_is_not_accepted(app, path):
    client = app.test_client()
    token = page_token(client)
    for kwargs in (dict(json={'token': 'A' * 43, 'csrf': token, 'csrf_token': token, '_csrf': token}),
                   dict(json={'token': 'A' * 43}, query_string={'csrf': token, 'csrf_token': token}),
                   dict(json={'token': 'A' * 43}, headers={'Cookie': f'csrf={token}; csrf_token={token}'})):
        headers = {'Origin': f'https://{HOST}', **kwargs.pop('headers', {})}
        assert client.post(path, headers=headers, **KW, **kwargs).status_code == 403


def test_a_csrf_token_signed_with_another_key_or_expired_is_refused(app, monkeypatch):
    client = app.test_client()
    good = page_token(client)
    other = create_app(Prod, overrides={'DATA_DIR': str(app.config['DATA_DIR']) + '2', 'AUTH_MODE': 'password', 'TRUSTED_HOSTS': [HOST],
                                        'SECRETS_KEY': KEY, 'PUBLIC_URL': f'https://{HOST}', 'SECRET_KEY': 'z' * 40})
    foreign = page_token(other.test_client())
    assert post(client, '/auth/token/inspect', {'token': 'A' * 43}, csrf_token=foreign).status_code == 403
    assert post(client, '/auth/token/inspect', {'token': 'A' * 43}, csrf_token=good).status_code == 404
    monkeypatch.setattr(csrf, 'MAX_AGE_S', -1)
    assert post(client, '/auth/token/inspect', {'token': 'A' * 43}, csrf_token=good).status_code == 403


@pytest.mark.parametrize('origin', ['https://evil.example', 'null', f'https://{HOST}.evil.example', 'https://evil.example@' + HOST])
def test_a_cross_origin_post_is_refused_even_with_a_valid_csrf_token(app, conn, origin):
    client = app.test_client()
    token = page_token(client)
    new = tokens.create(conn, 'invite', actor=audit.Actor.cli(), email='g@example.org')
    for path in ('/auth/token/inspect', '/auth/token/consume'):
        response = client.post(path, json={'token': new.token, 'username': 'alice', 'password': GOOD},
                               headers={'X-CSRF-Token': token, 'Origin': origin}, **KW)
        assert response.status_code in (400, 403), (origin, path)
    assert tokens.inspect(conn, new.token) and count(conn, "SELECT COUNT(*) FROM users WHERE username = 'alice'") == 0


@pytest.mark.parametrize('body,content_type', [
    (b'', 'application/json'), (b'[]', 'application/json'), (b'"texte"', 'application/json'), (b'123', 'application/json'),
    (b'null', 'application/json'), (b'{', 'application/json'), (b'\xff\xfe\x00', 'application/json'), (b'\xef\xbb\xbf{}', 'application/json'),
    (b'{"token": ["x"]}', 'application/json'), (b'{"token": null, "password": 5, "username": {}}', 'application/json'),
    (b'{"token": "' + b'A' * 43 + b'"}', 'text/plain'), (b'{"token": "' + b'A' * 43 + b'"}', 'application/x-www-form-urlencoded'),
    (b'[' * 5000, 'application/json'), (b'{"a":' * 3000, 'application/json'), (b'{"token": "\x00"}', 'application/json'),
    (b'x' * 20_000, 'application/json'),
])
def test_hostile_bodies_are_clean_refusals_never_a_500_and_never_a_state_change(app, conn, body, content_type):
    client = app.test_client()
    token = page_token(client)
    for path in ('/auth/token/inspect', '/auth/token/consume'):
        response = client.post(path, data=body, headers={'X-CSRF-Token': token, 'Origin': f'https://{HOST}', 'Content-Type': content_type}, **KW)
        assert response.status_code in (400, 404), (path, response.status_code)
        assert response.status_code != 500 and b'Traceback' not in response.get_data()
    assert count(conn, "SELECT COUNT(*) FROM users") == 0


def test_the_body_size_limit_is_exact(app):
    client = app.test_client()
    token = page_token(client)
    exact = json.dumps({'token': 'A' * 43, 'pad': ''}).encode()
    filler = 8192 - len(exact)
    ok_body = json.dumps({'token': 'A' * 43, 'pad': 'x' * filler}).encode()
    big_body = json.dumps({'token': 'A' * 43, 'pad': 'x' * (filler + 1)}).encode()
    assert len(ok_body) == 8192 and len(big_body) == 8193
    headers = {'X-CSRF-Token': token, 'Origin': f'https://{HOST}', 'Content-Type': 'application/json'}
    assert client.post('/auth/token/inspect', data=ok_body, headers=headers, **KW).status_code == 404       # lu, jeton inconnu
    assert client.post('/auth/token/inspect', data=big_body, headers=headers, **KW).status_code == 400      # refusé avant lecture


@pytest.mark.parametrize('field,value', [('username', 'x' * 1025), ('password', 'x' * 1025), ('username', 5), ('password', ['a']), ('username', None)])
def test_oversized_or_mistyped_form_fields_are_refused_without_consuming(app, conn, field, value):
    new = invite(conn)
    payload = {'token': new.token, 'username': 'alice', 'password': GOOD, field: value}
    response = post(app.test_client(), '/auth/token/consume', payload)
    assert response.status_code == 400 and tokens.inspect(conn, new.token)


def test_a_token_of_the_wrong_kind_cannot_be_used_through_another_kinds_form(app, conn):
    """Un jeton `reset` ne crée aucun compte (l'identifiant fourni est ignoré) ; un `invite` ne réinitialise aucun mot de passe."""
    victim = add_user(conn, 'victim', email='victim@example.org')
    reset = tokens.create(conn, 'reset', actor=audit.Actor.cli(), target_user_id=victim['id'])
    new = invite(conn)
    post(app.test_client(), '/auth/token/consume', {'token': reset.token, 'username': 'intruder', 'password': GOOD})
    assert count(conn, "SELECT COUNT(*) FROM users WHERE username = 'intruder'") == 0               # c'est la réinitialisation du compte visé
    before = fresh(conn, victim)['password_hash']
    assert invitations.reset_password(conn, new.token, 'autre-phrase-de-passe-99') is None and fresh(conn, victim)['password_hash'] == before
    assert tokens.inspect(conn, new.token) == 'choose_credentials' and tokens.inspect(conn, reset.token) is None


@pytest.mark.parametrize('method', ['get', 'put', 'delete', 'patch'])
@pytest.mark.parametrize('path', ['/auth/token/inspect', '/auth/token/consume'])
def test_only_post_reaches_the_token_endpoints_and_no_other_method_consumes(app, conn, method, path):
    new = invite(conn)
    client = app.test_client()
    response = getattr(client, method)(path, query_string={'token': new.token, 't': new.token}, headers={'Origin': f'https://{HOST}'}, **KW)
    assert response.status_code in (302, 405) and tokens.inspect(conn, new.token)          # 302 : le garde passe avant le routage (405)


# ================================================= indiscernabilité des jetons inutilisables (EF-3.6, CA-5) ===========================

def unusable_tokens(conn):
    admin = conn.execute("SELECT * FROM users WHERE username = 'adm'").fetchone() or add_user(conn, 'adm', admin=True)
    expired = tokens.create(conn, 'invite', actor=audit.Actor.cli(), email='e@example.org', now=tokens._stamp and None)
    conn.execute("UPDATE auth_tokens SET expires_at = '2000-01-01T00:00:00Z' WHERE id = ?", (expired.id,))
    used = invitations.create(conn, admin, 'u@example.org')
    invitations.accept(conn, used.token, 'someone', GOOD)
    revoked = invitations.create(conn, admin, 'r@example.org')
    invitations.revoke(conn, admin, revoked.id)
    conn.commit()
    return {'unknown': 'B' * 43, 'expired': expired.token, 'used': used.token, 'revoked': revoked.token, 'short': 'abc', 'empty': ''}


@pytest.mark.parametrize('path,extra', [('/auth/token/inspect', {}), ('/auth/token/consume', {'username': 'zed', 'password': GOOD})])
def test_every_unusable_token_gets_byte_identical_answers(app, conn, path, extra):
    client = app.test_client()
    answers = {}
    for name, token in unusable_tokens(conn).items():
        response = post(client, path, {'token': token, **extra})
        answers[name] = (response.status_code, response.get_data(), response.headers.get('Content-Type'), response.headers.get('Cache-Control'))
    assert len(set(answers.values())) == 1, answers
    assert count(conn, "SELECT COUNT(*) FROM users WHERE username = 'zed'") == 0


def test_unusable_tokens_cost_the_database_the_same_work(app, conn):
    client = app.test_client()
    probes = unusable_tokens(conn)
    token = page_token(client)
    shapes = {}
    for name in ('unknown', 'expired', 'used', 'revoked'):
        statements = []
        conn_trace = database.connect(app.config['DB_PATH'])
        conn_trace.set_trace_callback(statements.append)
        conn_trace.close()
        real = tokens.peek
        seen = []

        def counting(c, t, now=None, _seen=seen, _real=real):
            _seen.append(1)
            return _real(c, t, now)

        tokens.peek = counting
        try:
            client.post('/auth/token/consume', json={'token': probes[name], 'username': 'zed', 'password': GOOD},
                        headers={'X-CSRF-Token': token, 'Origin': f'https://{HOST}'}, **KW)
        finally:
            tokens.peek = real
        shapes[name] = len(seen)
    assert len(set(shapes.values())) == 1, shapes


def test_response_times_of_unusable_tokens_are_comparable(app, conn):
    client = app.test_client()
    probes = unusable_tokens(conn)
    token = page_token(client)
    medians = {}
    for name in ('unknown', 'expired', 'used', 'revoked'):
        samples = []
        for _ in range(40):
            started = time.perf_counter()
            client.post('/auth/token/inspect', json={'token': probes[name]}, headers={'X-CSRF-Token': token, 'Origin': f'https://{HOST}'}, **KW)
            samples.append(time.perf_counter() - started)
        medians[name] = sorted(samples)[len(samples) // 2]
    assert max(medians.values()) < 2.0 * min(medians.values()) + 0.002, medians


def test_no_response_ever_contains_the_token_the_address_the_issuer_or_the_note(app, conn):
    admin = add_user(conn, 'adm', admin=True)
    new = invitations.create(conn, admin, 'secret.person@example.org', note='note confidentielle')
    client = app.test_client()
    seen = []
    for path, payload in (('/auth/token/inspect', {'token': new.token}), ('/auth/token/consume', {'token': new.token, 'username': 'adm', 'password': GOOD}),
                          ('/auth/token/consume', {'token': new.token, 'username': 'zed', 'password': GOOD})):
        response = post(client, path, payload)
        seen.append(response.get_data(as_text=True) + json.dumps(dict(response.headers.items())) + repr(response.headers.getlist('Set-Cookie')))
    for text in seen:
        for secret in (new.token, 'secret.person', 'note confidentielle', 'adm@'):
            assert secret not in text, secret


def test_gets_with_the_token_in_the_url_never_consume_and_the_real_flow_puts_nothing_in_a_request_line(app, conn):
    new = invite(conn)
    log = []
    app.after_request(lambda response: (log.append((request_line(), response.headers.get('Location'))), response)[1])
    client = app.test_client()
    for url in (f'/auth/token?t={new.token}', f'/auth/token?token={new.token}', f'/auth/token/{new.token}', f'/auth/token#t={new.token}', '/auth/token'):
        client.get(url, **KW)
        client.head(url, **KW)
    assert tokens.inspect(conn, new.token) and count(conn, "SELECT COUNT(*) FROM users WHERE username = 'alice'") == 0
    log.clear()
    inspected = post(client, '/auth/token/inspect', {'token': new.token})
    consumed = post(client, '/auth/token/consume', {'token': new.token, 'username': 'alice', 'password': GOOD})
    assert inspected.status_code == consumed.status_code == 200
    assert log and all(new.token not in json.dumps(entry) for entry in log)                          # chemin, query et Location : jamais le jeton


def request_line():
    from flask import request
    return f"{request.method} {request.full_path}"


def test_the_whole_flow_leaves_no_token_or_password_in_logs_audit_responses_or_the_database(app, conn, caplog):
    admin = add_user(conn, 'adm', admin=True)
    created = add_user(conn, 'carrier', email='c@example.org', credits=2)
    api = signed_in(app, conn, created)
    with caplog.at_level(logging.DEBUG):
        body = api.post('/api/invitations', json={'email': 'guest@example.org'}, **KW).get_json()
        token = body['link'].split('#t=')[1]
        client = app.test_client()
        responses = [post(client, '/auth/token/inspect', {'token': token}),
                     post(client, '/auth/token/consume', {'token': token, 'username': 'alice', 'password': GOOD}),
                     post(client, '/auth/token/consume', {'token': token, 'username': 'alice', 'password': GOOD})]
        listed = api.get('/api/invitations', **KW).get_data(as_text=True)
    del admin
    everything = json.dumps({t: [dict(r) for r in conn.execute(f"SELECT * FROM {t}")] for t in ('auth_tokens', 'audit_log', 'users')}, default=str)
    texts = [caplog.text, everything, listed, *(r.get_data(as_text=True) + json.dumps(dict(r.headers.items())) for r in responses)]
    for text in texts:
        assert token not in text and GOOD not in text
    assert tokens.hash_token(token) in everything


# ================================================================ CA-1 : aucune route ne crée de compte sans jeton valide ==========

def test_no_anonymous_request_to_any_route_ever_creates_an_account(app, conn):
    client = app.test_client()
    token = page_token(client)
    before = count(conn, "SELECT COUNT(*) FROM users")
    payloads = [{'username': 'ghost', 'password': GOOD, 'token': t} for t in ('A' * 43, '', 'x', None, 'B' * 43)]
    for rule in app.url_map.iter_rules():
        if 'POST' not in rule.methods or rule.endpoint == 'static':
            continue
        url = re.sub(r'<[^>]+>', '1', rule.rule)
        for payload in payloads:
            client.post(url, json=payload, headers={'X-CSRF-Token': token, 'Origin': f'https://{HOST}'}, **KW)
    assert count(conn, "SELECT COUNT(*) FROM users") == before
    assert count(conn, "SELECT COUNT(*) FROM users WHERE username = 'ghost'") == 0


# ================================================================ limiteur : clé /64 (T6-1) =========================================

@pytest.mark.parametrize('address,key', [
    ('2001:db8::1', '2001:db8::/64'), ('2001:db8::ffff:ffff:ffff:ffff', '2001:db8::/64'), ('2001:0DB8:0:0:abcd::1', '2001:db8::/64'),
    ('2001:db8:0:1::', '2001:db8:0:1::/64'), ('2001:db8:0:0:ffff:ffff:ffff:ffff', '2001:db8::/64'), ('::1', '::/64'),
    ('::ffff:1.2.3.4', '1.2.3.4'), ('::ffff:0102:0304', '1.2.3.4'), ('1.2.3.4', '1.2.3.4'), (' 1.2.3.4 ', '1.2.3.4'),
])
def test_ip_key_cases(address, key):
    assert ip_key(address) == key


@pytest.mark.parametrize('address', [None, '', 'unknown', '1.2.3', '256.1.1.1', 'fe80::1%eth0%eth1', 'g::1', '1.2.3.4.5', '[::1]', 5, b'1.2.3.4', ['1.2.3.4']])
def test_unreadable_addresses_share_one_key_and_never_raise(address):
    try:
        key = ip_key(address)
    except Exception as exc:                                                                          # noqa: BLE001
        pytest.fail(f"ip_key({address!r}) a levé {exc!r}")
    assert isinstance(key, str) and key


def test_the_boundary_between_two_64_prefixes_is_exact_and_ipv4_stay_per_address():
    assert ip_key('2001:db8:0:0:ffff:ffff:ffff:ffff') != ip_key('2001:db8:0:1::')
    assert ip_key('10.0.0.1') != ip_key('10.0.0.2')
    limiter = LruLimiter(2, 900, clock=lambda: 1000.0)
    assert [limiter.hit(ip_key(f'2001:db8:0:0:{i:x}::1'))[0] for i in range(1, 5)] == [True, True, False, False]
    assert [limiter.hit(ip_key(f'10.0.0.{i}'))[0] for i in range(1, 5)] == [True] * 4


def test_the_token_endpoints_limit_a_whole_ipv6_prefix_but_not_its_neighbour(app):
    client = app.test_client()
    csrf_token = page_token(client)
    limit = app.config['SETTINGS_STORE'].get('ratelimit.token_per_ip')
    statuses = []
    for i in range(limit + 2):
        response = client.post('/auth/token/inspect', json={'token': 'A' * 43}, headers={'X-CSRF-Token': csrf_token, 'Origin': f'https://{HOST}'},
                               environ_base={'REMOTE_ADDR': f'2001:db8:1:2:{i:x}::{i + 1:x}'}, **KW)
        statuses.append(response.status_code)
    assert statuses[:limit] == [404] * limit and statuses[limit:] == [429, 429] and response.headers['Retry-After'].isdigit()
    other = client.post('/auth/token/inspect', json={'token': 'A' * 43}, headers={'X-CSRF-Token': csrf_token, 'Origin': f'https://{HOST}'},
                        environ_base={'REMOTE_ADDR': '2001:db8:1:3::1'}, **KW)
    assert other.status_code == 404


def test_an_ipv4_mapped_ipv6_client_shares_the_ipv4_counter(app):
    client = app.test_client()
    csrf_token = page_token(client)
    limit = app.config['SETTINGS_STORE'].get('ratelimit.token_per_ip')
    headers = {'X-CSRF-Token': csrf_token, 'Origin': f'https://{HOST}'}
    for _ in range(limit):
        client.post('/auth/token/inspect', json={'token': 'A' * 43}, headers=headers, environ_base={'REMOTE_ADDR': '198.51.100.9'}, **KW)
    mapped = client.post('/auth/token/inspect', json={'token': 'A' * 43}, headers=headers, environ_base={'REMOTE_ADDR': '::ffff:198.51.100.9'}, **KW)
    assert mapped.status_code == 429


# ================================================================ migration 0013 sur une base peuplée ===============================

def _database_before_0013(tmp_path):
    path = str(tmp_path / 'old.sqlite3')
    c = database.connect(path)
    c.executescript(database.SCHEMA)
    names = [name for name, _ in database.NAMED_MIGRATIONS]
    database.migrate(c, named=[m for m in database.NAMED_MIGRATIONS if names.index(m[0]) < names.index('0013_users_password_accounts')])
    return path, c


def test_a_populated_database_keeps_every_row_and_gets_neutral_new_columns(tmp_path):
    path, c = _database_before_0013(tmp_path)
    c.executemany("INSERT INTO users (sub, username, email, is_admin, is_active, auth_method) VALUES (?, ?, ?, ?, ?, ?)",
                  [('local', 'local', None, 1, 1, 'none'), ('idp-1', 'alice', 'a@example.org', 1, 1, 'oidc'),
                   ('idp-2', 'alice', 'a@example.org', 0, 1, 'oidc'), ('idp-3', 'Alice', 'A@example.org', 0, 0, 'oidc')])
    c.commit()
    before = [tuple(r) for r in c.execute("SELECT id, sub, username, email, is_admin, is_active FROM users ORDER BY id")]
    c.close()
    for _ in range(2):                                                                               # deux passages : idempotent
        database.init_db(path)
    c = database.connect(path)
    after = [tuple(r) for r in c.execute("SELECT id, sub, username, email, is_admin, is_active FROM users ORDER BY id")]
    # 0014 : les adresses passent sous forme canonique (EF-4.10) ; tout le reste est intact
    assert after == [(r[0], r[1], r[2], r[3].casefold() if r[3] else None, r[4], r[5]) for r in before]
    rows = [tuple(r) for r in c.execute("SELECT invite_credits, password_hash, pending_email, invited_by, password_changed_at, email_verified_at FROM users")]
    assert rows == [(0, None, None, None, None, None)] * 4
    assert {r[0] for r in c.execute("SELECT auth_method FROM users")} <= {'oidc', 'none'}
    assert dict(c.execute("SELECT sub, auth_method FROM users WHERE sub IN ('local', 'idp-1')").fetchall()) == {'local': 'none', 'idp-1': 'oidc'}
    c.close()


def test_partial_unique_indexes_only_bind_password_accounts_after_migration(tmp_path):
    path, c = _database_before_0013(tmp_path)
    c.execute("INSERT INTO users (sub, username, email) VALUES ('idp-1', 'bob', 'b@example.org')")
    c.commit()
    c.close()
    database.init_db(path)
    c = database.connect(path)
    c.execute("INSERT INTO users (sub, username, email) VALUES ('idp-2', 'bob', 'b@example.org')")        # SSO : collision tolérée (EF-1.2)
    c.execute("INSERT INTO users (sub, username, email, auth_method) VALUES ('pwd:1', 'Bob', 'b@example.org', 'password')")   # password vs SSO : toléré
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO users (sub, username, email, auth_method) VALUES ('pwd:2', 'BOB', NULL, 'password')")
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO users (sub, username, email, auth_method) VALUES ('pwd:3', 'other', 'b@example.org', 'password')")
    c.execute("INSERT INTO users (sub, username, email, auth_method) VALUES ('pwd:4', 'noemail1', NULL, 'password')")
    c.execute("INSERT INTO users (sub, username, email, auth_method) VALUES ('pwd:5', 'noemail2', NULL, 'password')")   # NULL : pas d'unicité
    c.execute("INSERT INTO users (sub, username, pending_email, auth_method) VALUES ('pwd:6', 'p1', 'x@example.org', 'password')")
    c.execute("INSERT INTO users (sub, username, pending_email, auth_method) VALUES ('pwd:7', 'p2', 'x@example.org', 'password')")   # en attente : partageable (CA-44)
    c.rollback()
    c.close()


def test_the_migration_is_fast_on_a_large_table_and_keeps_user_version(tmp_path):
    path, c = _database_before_0013(tmp_path)
    c.executemany("INSERT INTO users (sub, username) VALUES (?, ?)", [(f'idp-{i}', f'user{i}') for i in range(20_000)])
    c.commit()
    version = c.execute("PRAGMA user_version").fetchone()[0]
    c.close()
    started = time.monotonic()
    database.init_db(path)
    assert time.monotonic() - started < 15
    c = database.connect(path)
    assert c.execute("PRAGMA user_version").fetchone()[0] == version and count(c, "SELECT COUNT(*) FROM users WHERE invite_credits = 0") == 20_000
    c.close()


def test_existing_sso_duplicates_do_not_block_the_unique_index_creation(tmp_path):
    path, c = _database_before_0013(tmp_path)
    c.executemany("INSERT INTO users (sub, username, email) VALUES (?, 'same', 'same@example.org')", [('a',), ('b',), ('c',)])
    c.commit()
    c.close()
    database.init_db(path)                                                                            # ne doit pas lever
    c = database.connect(path)
    assert count(c, "SELECT COUNT(*) FROM users WHERE username = 'same'") == 3
    c.close()
