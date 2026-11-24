"""Invitations, crédits, création de compte par jeton, validation d'adresse (T7b, EF-3, EF-4.3b, CA-1…CA-6, CA-17b, CA-39)."""

import json
import logging
import re
import sqlite3
import threading
from datetime import timedelta

import pytest
from cryptography.fernet import Fernet

from app import create_app, db as database
from app.audit import Actor
from app.auth import csrf, identifiers, invitations, tokens
from app.auth.invitations import AccountError, InvitationError
from app.config import TestingConfig
from app.ratelimit import ip_key

KEY = Fernet.generate_key().decode()
HOST = 'traces.example.com'
CLI = Actor.cli()
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


def web(app):
    client = app.test_client()
    return client, {'base_url': f'https://{HOST}'}


def page_token(client, kw):
    html = client.get('/auth/token', **kw).get_data(as_text=True)
    return re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)


def post(client, kw, path, payload, csrf_token=None, **extra):
    headers = {'X-CSRF-Token': csrf_token or page_token(client, kw), 'Origin': f'https://{HOST}', **extra}
    return client.post(path, json=payload, headers=headers, **kw)


# --- Qui peut inviter, crédits (EF-3.2, EF-3.5) ------------------------------------------------------------------

def test_who_can_invite(conn):
    admin = add_user(conn, 'adm', admin=True)
    member = add_user(conn, 'mem', credits=1, email='mem@example.org')
    no_credit = add_user(conn, 'nocredit', email='n@example.org')
    unverified = add_user(conn, 'unver', credits=3, pending='u@example.org')
    disabled = add_user(conn, 'off', credits=3, email='o@example.org', active=False)
    assert [invitations.can_invite(u) for u in (admin, member, no_credit, unverified, disabled)] == [True, True, False, False, False]


def test_a_non_admin_invitation_consumes_one_credit_and_a_zero_credit_one_is_refused_with_409(conn):
    member = add_user(conn, 'mem', credits=1, email='mem@example.org')
    first = invitations.create(conn, member, 'new@example.org')
    assert fresh(conn, member)['invite_credits'] == 0 and first.token
    with pytest.raises(InvitationError) as exc:
        invitations.create(conn, fresh(conn, member), 'other@example.org')
    assert exc.value.status == 409                                     # CA-2 : plus de crédit => 409


def test_two_concurrent_creations_with_one_credit_succeed_once(app):
    """CA-2 : 1 crédit et deux créations concurrentes => une seule réussit, crédit final 0."""
    setup = database.connect(app.config['DB_PATH'])
    member = add_user(setup, 'mem', credits=1, email='mem@example.org')
    setup.close()
    results, barrier = [], threading.Barrier(6)

    def worker(i):
        c = database.connect(app.config['DB_PATH'])
        try:
            row = c.execute("SELECT * FROM users WHERE id = ?", (member['id'],)).fetchone()
            barrier.wait(10)
            try:
                invitations.create(c, row, f'p{i}@example.org')
                results.append('ok')
            except InvitationError as exc:
                results.append(exc.status)
        finally:
            c.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    check = database.connect(app.config['DB_PATH'])
    assert results.count('ok') == 1 and check.execute("SELECT invite_credits FROM users").fetchone()[0] == 0
    assert check.execute("SELECT COUNT(*) FROM auth_tokens WHERE kind = 'invite'").fetchone()[0] == 1
    check.close()


def test_an_admin_creates_many_invitations_without_touching_credits(conn):
    """CA-3."""
    admin = add_user(conn, 'adm', admin=True, credits=5)
    for i in range(5):
        invitations.create(conn, admin, f'x{i}@example.org')
    assert fresh(conn, admin)['invite_credits'] == 5


def test_a_failed_creation_gives_the_credit_back_with_the_transaction(conn):
    member = add_user(conn, 'mem', credits=2, email='mem@example.org')
    with pytest.raises(InvitationError):
        invitations.create(conn, member, 'pas-un-mail')
    assert fresh(conn, member)['invite_credits'] == 2
    invitations.create(conn, member, 'ok@example.org')
    assert fresh(conn, member)['invite_credits'] == 1


def test_note_and_initial_credits_are_reserved_to_admins_and_an_invitation_never_grants_admin(conn):
    member = add_user(conn, 'mem', credits=3, email='mem@example.org')
    admin = add_user(conn, 'adm', admin=True)
    for kwargs in (dict(note='salut'), dict(initial_credits=2)):
        with pytest.raises(InvitationError) as exc:
            invitations.create(conn, member, 'a@example.org', **kwargs)       # CA-4 : 400 (403 côté API pour les crédits)
        assert exc.value.status == 400
    assert fresh(conn, member)['invite_credits'] == 3
    invitations.create(conn, admin, 'b@example.org', note='Bienvenue', initial_credits=4)
    row = conn.execute("SELECT * FROM auth_tokens WHERE email = 'b@example.org'").fetchone()
    assert (row['kind'], row['is_admin_grant'], row['initial_credits'], row['note']) == ('invite', 0, 4, 'Bienvenue')
    invitations.create(conn, member, 'c@example.org')
    assert conn.execute("SELECT initial_credits FROM auth_tokens WHERE email = 'c@example.org'").fetchone()[0] == 0


def test_the_address_is_stored_canonical(conn):
    admin = add_user(conn, 'adm', admin=True)
    invitations.create(conn, admin, '  Prénom.NOM@Example.ORG ')
    assert conn.execute("SELECT email FROM auth_tokens").fetchone()[0] == 'prénom.nom@example.org'


def test_an_admin_gets_409_for_a_taken_address_a_non_admin_gets_the_same_answer_as_for_a_free_one(conn):
    """EF-3.10, CA-39."""
    admin = add_user(conn, 'adm', admin=True)
    member = add_user(conn, 'mem', credits=5, email='mem@example.org')
    add_user(conn, 'holder', email='taken@example.org')
    add_user(conn, 'waiting', pending='waiting@example.org')
    invitations.create(conn, admin, 'invited@example.org')
    for address in ('taken@example.org', ' TAKEN@example.org', 'waiting@example.org', 'invited@example.org'):
        with pytest.raises(InvitationError) as exc:
            invitations.create(conn, admin, address)
        assert exc.value.status == 409
        assert invitations.create(conn, fresh(conn, member), address).token                   # non-admin : créée normalement
    assert invitations.create(conn, admin, 'free@example.org').token


# --- Liste et révocation (EF-3.7) -------------------------------------------------------------------------------------

def test_list_shows_own_invitations_with_status_and_never_a_token_or_hash(conn):
    admin = add_user(conn, 'adm', admin=True)
    member = add_user(conn, 'mem', credits=3, email='mem@example.org')
    mine = invitations.create(conn, member, 'a@example.org')
    invitations.create(conn, admin, 'someone-else@example.org')
    expired = invitations.create(conn, fresh(conn, member), 'old@example.org', ttl=timedelta(hours=1),
                                 now=tokens.datetime.now(tokens.timezone.utc) - timedelta(days=2))
    listed = invitations.list_for(conn, fresh(conn, member))
    assert {i['email']: i['status'] for i in listed} == {'a@example.org': 'pending', 'old@example.org': 'expired'}
    dump = json.dumps(listed)
    assert mine.token not in dump and expired.token not in dump and 'token_hash' not in dump
    assert len(invitations.list_for(conn, admin, everyone=True)) == 3
    with pytest.raises(InvitationError):
        invitations.list_for(conn, member, everyone=True)


def test_revocation_by_the_owner_or_an_admin_only_and_the_credit_is_not_returned(conn):
    admin = add_user(conn, 'adm', admin=True)
    member = add_user(conn, 'mem', credits=2, email='mem@example.org')
    other = add_user(conn, 'oth', credits=2, email='oth@example.org')
    mine = invitations.create(conn, member, 'a@example.org')
    theirs = invitations.create(conn, other, 'b@example.org')
    assert invitations.revoke(conn, fresh(conn, member), theirs.id) is False                  # pas la sienne
    assert invitations.revoke(conn, fresh(conn, member), mine.id) is True
    assert invitations.revoke(conn, fresh(conn, member), mine.id) is False
    assert fresh(conn, member)['invite_credits'] == 1                                         # non rendu
    assert invitations.revoke(conn, admin, theirs.id) is True
    for bad in (0, -1, 2 ** 63, True, '1', None):
        assert invitations.revoke(conn, admin, bad) is False


def test_a_revoked_invitation_cannot_be_used(conn):
    admin = add_user(conn, 'adm', admin=True)
    new = invitations.create(conn, admin, 'a@example.org')
    invitations.revoke(conn, admin, new.id)
    assert invitations.accept(conn, new.token, 'alice', GOOD) is None


# --- Création du compte à la consommation (EF-1.1 à 1.3, EF-3.11, B2, r8) ----------------------------------------------------

def test_accepting_an_invitation_creates_an_active_password_account_with_a_pending_address(conn):
    admin = add_user(conn, 'adm', admin=True)
    new = invitations.create(conn, admin, 'Alice@Example.org', initial_credits=2)
    account = invitations.accept(conn, new.token, ' Alice ', GOOD, ip='203.0.113.7')
    assert account['username'] == 'alice'
    row = conn.execute("SELECT * FROM users WHERE id = ?", (account['id'],)).fetchone()
    assert row['auth_method'] == 'password' and re.fullmatch(r'pwd:[0-9a-f-]{36}', row['sub'])
    assert (row['email'], row['pending_email'], row['email_verified_at']) == (None, 'alice@example.org', None)   # jamais validée d'office
    assert (row['is_admin'], row['is_active'], row['invite_credits'], row['invited_by']) == (0, 1, 2, admin['id'])
    assert row['password_hash'].startswith('$argon2id$') and GOOD not in json.dumps(dict(row), default=str)
    assert row['session_token'] and row['password_changed_at']
    used = conn.execute("SELECT used_at, used_by FROM auth_tokens WHERE id = ?", (new.id,)).fetchone()
    assert used['used_at'] and used['used_by'] == account['id']
    assert tokens.inspect(conn, new.token) is None                                           # usage unique


def test_a_non_admin_inviter_leaves_the_new_account_with_zero_credits(conn):
    member = add_user(conn, 'mem', credits=1, email='mem@example.org')
    new = invitations.create(conn, member, 'a@example.org')
    account = invitations.accept(conn, new.token, 'bob', GOOD)
    assert conn.execute("SELECT invite_credits, invited_by FROM users WHERE id = ?", (account['id'],)).fetchone()[:] == (0, member['id'])


def test_creation_is_audited_without_the_password_or_the_token(conn):
    admin = add_user(conn, 'adm', admin=True)
    new = invitations.create(conn, admin, 'a@example.org')
    invitations.accept(conn, new.token, 'alice', GOOD)
    dump = json.dumps([dict(r) for r in conn.execute("SELECT * FROM audit_log")])
    assert GOOD not in dump and new.token not in dump and 'user.create' in dump


def test_accepting_never_opens_a_session(conn):
    """r8 : la consommation ne rend aucune session ; le compte devra passer par la connexion."""
    admin = add_user(conn, 'adm', admin=True)
    account = invitations.accept(conn, invitations.create(conn, admin, 'a@example.org').token, 'alice', GOOD)
    assert set(account) == {'id', 'username'}


@pytest.mark.parametrize('username', ['cli', 'System', 'LOCAL', 'admin', 'Root', 'ａｄｍｉｎ'])
def test_reserved_identifiers_are_refused_with_the_neutral_message_and_the_link_stays_usable(conn, username):
    """EF-1.2b branchée : réservés refusés comme un identifiant pris."""
    admin = add_user(conn, 'adm', admin=True)
    new = invitations.create(conn, admin, 'a@example.org')
    with pytest.raises(AccountError) as exc:
        invitations.accept(conn, new.token, username, GOOD)
    assert (exc.value.code, exc.value.status, exc.value.message) == ('unavailable', 409, identifiers.UNAVAILABLE_MESSAGE)
    assert tokens.inspect(conn, new.token) == 'choose_credentials'


@pytest.mark.parametrize('username', ['', 'ab', 'x' * 65, 'a@b', 'a b', 'é-é-é', None, 5])
def test_bad_identifier_format_refuses_without_consuming(conn, username):
    admin = add_user(conn, 'adm', admin=True)
    new = invitations.create(conn, admin, 'a@example.org')
    with pytest.raises(AccountError) as exc:
        invitations.accept(conn, new.token, username, GOOD)
    assert exc.value.code == 'invalid_identifier' and tokens.inspect(conn, new.token)


def test_a_taken_identifier_is_refused_case_insensitively_among_all_accounts_and_keeps_the_link(conn):
    admin = add_user(conn, 'adm', admin=True)
    add_user(conn, 'Taken-Sso', method='oidc')                       # compte SSO : compte aussi dans la comparaison
    add_user(conn, 'alice')
    for i, taken in enumerate(('alice', 'ALICE', 'taken-sso')):
        new = invitations.create(conn, admin, f'invite{i}@example.org')
        with pytest.raises(AccountError) as exc:
            invitations.accept(conn, new.token, taken, GOOD)
        assert exc.value.code == 'unavailable'
        assert tokens.inspect(conn, new.token) == 'choose_credentials'
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 3


def test_policy_failures_do_not_consume_the_link(conn):
    from app.auth.passwords import PolicyError
    admin = add_user(conn, 'adm', admin=True)
    new = invitations.create(conn, admin, 'mail.perso@example.org')
    for bad in ('court', 'password1234', 'alice-alice-alice', 'mail.perso@example.org', 'ＭＡＩＬ.perso@example.org'):
        with pytest.raises(PolicyError):
            invitations.accept(conn, new.token, 'alice-alice-alice', bad)
    assert tokens.inspect(conn, new.token) == 'choose_credentials'
    assert invitations.accept(conn, new.token, 'alice', GOOD)


def test_an_unusable_link_yields_none_whatever_it_is(conn):
    admin = add_user(conn, 'adm', admin=True)
    expired = invitations.create(conn, admin, 'e@example.org', ttl=timedelta(hours=1),
                                 now=tokens.datetime.now(tokens.timezone.utc) - timedelta(days=3))
    used = invitations.create(conn, admin, 'u@example.org')
    invitations.accept(conn, used.token, 'first', GOOD)
    reset = tokens.create(conn, 'reset', actor=CLI, target_user_id=admin['id'])
    for t in (expired.token, used.token, 'A' * 43, 'x', None, reset.token):
        assert invitations.accept(conn, t, 'alice', GOOD) is None


def test_a_concurrent_identifier_race_creates_one_account_and_leaves_the_loser_s_link_intact(app):
    setup = database.connect(app.config['DB_PATH'])
    admin = add_user(setup, 'adm', admin=True)
    links = [invitations.create(setup, admin, f'race{i}@example.org').token for i in range(6)]
    setup.close()
    results, barrier = [], threading.Barrier(6)

    def worker(token):
        c = database.connect(app.config['DB_PATH'])
        try:
            barrier.wait(10)
            try:
                results.append(('ok', invitations.accept(c, token, 'samename', GOOD)))
            except AccountError as exc:
                results.append((exc.code, token))
        finally:
            c.close()

    threads = [threading.Thread(target=worker, args=(t,)) for t in links]
    [t.start() for t in threads]
    [t.join(60) for t in threads]
    check = database.connect(app.config['DB_PATH'])
    assert [r[0] for r in results].count('ok') == 1 and [r[0] for r in results].count('unavailable') == 5
    assert check.execute("SELECT COUNT(*) FROM users WHERE username = 'samename'").fetchone()[0] == 1
    losers = [r[1] for r in results if r[0] == 'unavailable']
    assert all(tokens.inspect(check, t) == 'choose_credentials' for t in losers)
    check.close()


def test_a_bootstrap_token_is_not_consumed_and_creates_no_account_until_the_enrolment_exists(conn):
    """T7b-1 (EF-0.3) : l'octroi d'admin ne peut pas être perdu ; tant que l'enrôlement n'existe pas, 501 et jeton intact."""
    before = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    fresh_token = tokens.create(conn, 'bootstrap', actor=CLI, email='first@example.org', is_admin_grant=True)
    existing = add_user(conn, 'resume')
    resume = tokens.create(conn, 'bootstrap', actor=CLI, target_user_id=existing['id'], is_admin_grant=True)
    for new in (fresh_token, resume):
        with pytest.raises(AccountError) as exc:
            invitations.accept(conn, new.token, 'first', GOOD)
        assert exc.value.status == 501 and exc.value.code == 'enroll_unavailable'
        assert tokens.inspect(conn, new.token) == 'choose_credentials'
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == before + 1            # seulement `resume`, ajouté par le test
    assert [r[0] for r in conn.execute("SELECT used_at FROM auth_tokens WHERE kind = 'bootstrap'")] == [None, None]


def test_over_http_a_bootstrap_link_answers_501_and_stays_usable(app, conn):
    new = tokens.create(conn, 'bootstrap', actor=CLI, email='first@example.org', is_admin_grant=True)
    client, kw = web(app)
    response = post(client, kw, '/auth/token/consume', {'token': new.token, 'username': 'first', 'password': GOOD})
    assert response.status_code == 501 and response.get_json()['code'] == 'enroll_unavailable'
    assert tokens.inspect(conn, new.token) == 'choose_credentials'
    assert conn.execute("SELECT COUNT(*) FROM users WHERE username = 'first'").fetchone()[0] == 0


# --- Validation d'adresse (EF-4.3, EF-4.3b, CA-17b) ------------------------------------------------------------------------

def make_pending(conn, name='alice', address='alice@example.org', other=None):
    user = add_user(conn, name, pending=address)
    new = tokens.create(conn, 'verify_email', actor=CLI, target_user_id=user['id'], email=address)
    return user, new


def test_verify_email_with_the_target_session_validates_the_address(conn):
    user, new = make_pending(conn)
    assert invitations.verify_email(conn, user, new.token) == 'ok'
    row = fresh(conn, user)
    assert (row['email'], row['pending_email']) == ('alice@example.org', None) and row['email_verified_at']
    assert invitations.verify_email(conn, user, new.token) == 'invalid'                       # usage unique


def test_another_account_cannot_consume_it_and_the_link_stays_reusable(conn):
    """R2 / CA-17b : mauvais compte => non consommé."""
    victim, new = make_pending(conn)
    attacker = add_user(conn, 'mallory')
    assert invitations.verify_email(conn, attacker, new.token) == 'invalid'
    assert fresh(conn, victim)['email'] is None and fresh(conn, attacker)['email'] is None
    assert tokens.inspect(conn, new.token) == 'verify_email'
    assert invitations.verify_email(conn, victim, new.token) == 'ok'


def test_an_address_already_validated_on_another_account_is_refused_neutrally_and_the_pending_one_is_cleared(conn):
    add_user(conn, 'holder', email='alice@example.org')
    user, new = make_pending(conn)
    assert invitations.verify_email(conn, user, new.token) == 'refused'
    row = fresh(conn, user)
    assert (row['email'], row['pending_email']) == (None, None)
    assert [r['action'] for r in conn.execute("SELECT action FROM audit_log WHERE action LIKE 'identity.%'")] == ['identity.verify_refused']


def test_a_link_for_an_address_that_changed_since_is_invalid(conn):
    user, new = make_pending(conn)
    conn.execute("UPDATE users SET pending_email = 'other@example.org' WHERE id = ?", (user['id'],))
    conn.commit()
    assert invitations.verify_email(conn, user, new.token) == 'invalid'


def test_two_concurrent_validations_of_the_same_address_succeed_once(app):
    """CA-44 : une seule réussit ; l'autre reçoit le refus neutre."""
    setup = database.connect(app.config['DB_PATH'])
    a, ta = make_pending(setup, 'ua', 'same@example.org')
    b, tb = make_pending(setup, 'ub', 'same@example.org')
    setup.close()
    results, barrier = [], threading.Barrier(2)

    def worker(user_id, token):
        c = database.connect(app.config['DB_PATH'])
        try:
            row = c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            barrier.wait(10)
            results.append(invitations.verify_email(c, row, token))
        finally:
            c.close()

    threads = [threading.Thread(target=worker, args=(a['id'], ta.token)), threading.Thread(target=worker, args=(b['id'], tb.token))]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    assert sorted(results) == ['ok', 'refused']
    check = database.connect(app.config['DB_PATH'])
    assert check.execute("SELECT COUNT(*) FROM users WHERE email = 'same@example.org'").fetchone()[0] == 1
    check.close()


# --- Réinitialisation du mot de passe par jeton (EF-1.8, EF-1.12) --------------------------------------------------------------

def test_reset_sets_the_password_closes_sessions_keeps_factors_and_opens_no_session(conn):
    user = add_user(conn, 'alice')
    new = tokens.create(conn, 'reset', actor=CLI, target_user_id=user['id'])
    assert invitations.reset_password(conn, new.token, GOOD) == user['id']
    row = fresh(conn, user)
    assert row['password_hash'].startswith('$argon2id$') and row['session_token'] != 'tok'
    assert invitations.reset_password(conn, new.token, GOOD) is None


def test_reset_refuses_a_weak_password_without_consuming_and_ignores_other_kinds(conn):
    from app.auth.passwords import PolicyError
    user = add_user(conn, 'alice')
    reset = tokens.create(conn, 'reset', actor=CLI, target_user_id=user['id'])
    with pytest.raises(PolicyError):
        invitations.reset_password(conn, reset.token, 'alice')
    assert tokens.inspect(conn, reset.token) == 'set_password'
    invite = tokens.create(conn, 'invite', actor=CLI)
    assert invitations.reset_password(conn, invite.token, GOOD) is None


# --- Routes publiques /auth/token* (EF-S8, B4, r5, r8, CA-5) ------------------------------------------------------------------

def test_the_token_page_is_static_and_carries_no_token_data(app, conn):
    admin = add_user(conn, 'adm', admin=True)
    new = invitations.create(conn, admin, 'a@example.org')
    client, kw = web(app)
    for url in ('/auth/token', f'/auth/token#t={new.token}', '/auth/token?t=' + new.token):
        response = client.get(url, **kw)
        text = response.get_data(as_text=True)
        assert response.status_code == 200 and new.token not in text and 'a@example.org' not in text
    assert tokens.inspect(conn, new.token)                                                  # aucun GET ne consomme
    assert response.headers['Cache-Control'] == 'no-store' and 'src="http' not in text
    assert '/static/js/token.js' in text and 'https://' not in text.replace(f'https://{HOST}', '')


def test_the_token_page_javascript_reads_the_fragment_and_clears_it():
    js = open('static/js/token.js', encoding='utf-8').read()
    assert 'location.hash' in js and "history.replaceState(null, ''" in js and 'X-CSRF-Token' in js
    assert 'innerHTML' not in js and 'eval(' not in js                                      # données via textContent seulement


def test_inspect_gives_the_form_type_only_and_the_same_answer_for_every_unusable_token(app, conn):
    admin = add_user(conn, 'adm', admin=True)
    good = invitations.create(conn, admin, 'secret@example.org', note='note privée')
    used = invitations.create(conn, admin, 'u@example.org')
    invitations.accept(conn, used.token, 'first', GOOD)
    revoked = invitations.create(conn, admin, 'r@example.org')
    invitations.revoke(conn, admin, revoked.id)
    client, kw = web(app)
    ok = post(client, kw, '/auth/token/inspect', {'token': good.token})
    assert ok.status_code == 200 and ok.get_json() == {'success': True, 'form': 'choose_credentials'}
    assert 'secret@example.org' not in ok.get_data(as_text=True) and 'note' not in ok.get_data(as_text=True)
    bodies = set()
    for token in (used.token, revoked.token, 'A' * 43, 'x', '', None, 5, ['a']):
        response = post(client, kw, '/auth/token/inspect', {'token': token})
        bodies.add((response.status_code, response.get_data(as_text=True)))
    assert len(bodies) == 1 and next(iter(bodies))[0] == 404


def test_consume_creates_the_account_and_redirects_to_the_login_without_a_session(app, conn):
    admin = add_user(conn, 'adm', admin=True)
    new = invitations.create(conn, admin, 'a@example.org')
    client, kw = web(app)
    response = post(client, kw, '/auth/token/consume', {'token': new.token, 'username': 'alice', 'password': GOOD})
    assert response.status_code == 200 and response.get_json() == {'success': True, 'next': '/auth/password/login'}
    assert 'Set-Cookie' not in response.headers or 'session=' not in response.headers.get('Set-Cookie', '')
    assert client.get('/api/activities', **kw).status_code == 401                                 # aucune session
    assert conn.execute("SELECT username FROM users WHERE username = 'alice'").fetchone()
    again = post(client, kw, '/auth/token/consume', {'token': new.token, 'username': 'bob', 'password': GOOD})
    assert again.status_code == 404


def test_consume_errors_are_precise_for_the_form_and_never_consume(app, conn):
    admin = add_user(conn, 'adm', admin=True)
    add_user(conn, 'taken')
    new = invitations.create(conn, admin, 'a@example.org')
    client, kw = web(app)
    weak = post(client, kw, '/auth/token/consume', {'token': new.token, 'username': 'alice', 'password': 'court'})
    assert weak.status_code == 400 and weak.get_json()['code'] == 'too_short'
    common = post(client, kw, '/auth/token/consume', {'token': new.token, 'username': 'alice', 'password': 'password1234'})
    assert common.get_json()['code'] == 'too_common'
    clash = post(client, kw, '/auth/token/consume', {'token': new.token, 'username': 'TAKEN', 'password': GOOD})
    reserved = post(client, kw, '/auth/token/consume', {'token': new.token, 'username': 'root', 'password': GOOD})
    assert clash.status_code == reserved.status_code == 409 and clash.get_json() == reserved.get_json()   # même réponse
    assert post(client, kw, '/auth/token/consume', {'token': new.token, 'username': 'alice'}).status_code == 400
    assert tokens.inspect(conn, new.token) == 'choose_credentials'


def test_a_surrogate_password_is_a_clean_refusal(app, conn):
    admin = add_user(conn, 'adm', admin=True)
    new = invitations.create(conn, admin, 'a@example.org')
    client, kw = web(app)
    response = client.post('/auth/token/consume', data='{"token": "%s", "username": "alice", "password": "aaaaaaaaaaa\\ud800"}' % new.token,
                           content_type='application/json', headers={'X-CSRF-Token': page_token(client, kw),
                                                                      'Origin': f'https://{HOST}'}, **kw)
    assert response.status_code == 400 and tokens.inspect(conn, new.token)


def test_verify_email_over_http_needs_the_session_of_the_target_account(app, conn):
    """R2 : sans session ou avec une autre session, rien n'est consommé ; avec la bonne, l'adresse est validée."""
    user, new = make_pending(conn)
    client, kw = web(app)
    anonymous = post(client, kw, '/auth/token/consume', {'token': new.token})
    assert anonymous.status_code == 403 and anonymous.get_json()['login_required'] is True and 'alice' not in anonymous.get_data(as_text=True)
    other = add_user(conn, 'mallory')
    with client.session_transaction(**kw) as s:
        s['user_id'], s['token'], s['login_at'] = other['id'], 'tok', int(__import__('time').time())
    assert post(client, kw, '/auth/token/consume', {'token': new.token}).status_code == 404
    assert tokens.inspect(conn, new.token) == 'verify_email'
    with client.session_transaction(**kw) as s:
        s['user_id'] = user['id']
    done = post(client, kw, '/auth/token/consume', {'token': new.token})
    assert done.status_code == 200 and fresh(conn, user)['email'] == 'alice@example.org'


def test_consume_requires_a_valid_csrf_token_and_a_json_object(app, conn):
    admin = add_user(conn, 'adm', admin=True)
    new = invitations.create(conn, admin, 'a@example.org')
    client, kw = web(app)
    good = {'token': new.token, 'username': 'alice', 'password': GOOD}
    headers = {'Origin': f'https://{HOST}'}
    assert client.post('/auth/token/consume', json=good, headers=headers, **kw).status_code == 403              # sans CSRF
    assert client.post('/auth/token/consume', json=good, headers={**headers, 'X-CSRF-Token': 'forgé'}, **kw).status_code == 403
    assert post(client, kw, '/auth/token/consume', ['pas', 'un', 'objet']).status_code == 400
    assert client.post('/auth/token/consume', data='pas du json', content_type='application/json',
                       headers={**headers, 'X-CSRF-Token': page_token(client, kw)}, **kw).status_code == 400
    assert tokens.inspect(conn, new.token)
    assert client.post('/auth/token/consume', json=good, headers={'X-CSRF-Token': page_token(client, kw),
                                                                 'Origin': 'https://evil.example'}, **kw).status_code == 403


def test_csrf_tokens_are_signed_timed_and_stateless(app):
    with app.test_request_context():
        token = csrf.issue()
        assert csrf.verify(token) and not csrf.verify(token + 'x') and not csrf.verify('') and not csrf.verify(None)
        assert not csrf.verify(token[:-3]) and not csrf.verify('a' * 600)
        assert csrf.issue() != token                                              # nonce aléatoire


def test_routes_do_not_exist_without_password_accounts(tmp_path):
    local = create_app(TestingConfig, overrides={'DATA_DIR': str(tmp_path / 'l'), 'AUTH_MODE': 'local'})
    client = local.test_client()
    assert client.get('/auth/token').status_code == 404
    assert client.post('/auth/token/inspect', json={'token': 'A' * 43}).status_code in (404, 403)
    assert client.get('/api/invitations').status_code == 404


def test_the_token_endpoints_are_rate_limited_per_ip_before_any_work(app, conn):
    client, kw = web(app)
    csrf_token = page_token(client, kw)
    statuses = [post(client, kw, '/auth/token/inspect', {'token': 'A' * 43}, csrf_token).status_code for _ in range(14)]
    assert statuses[:10] == [404] * 10 and set(statuses[10:]) == {429}
    response = post(client, kw, '/auth/token/inspect', {'token': 'A' * 43}, csrf_token)
    assert response.status_code == 429 and int(response.headers['Retry-After']) >= 1


def test_the_limit_follows_the_runtime_setting(app, conn):
    from app.audit import Actor as A
    store = app.config['SETTINGS_STORE']
    store.set_many(conn, {'ratelimit.token_per_ip': 5}, actor=A.cli())
    client, kw = web(app)
    csrf_token = page_token(client, kw)
    statuses = [post(client, kw, '/auth/token/inspect', {'token': 'A' * 43}, csrf_token).status_code for _ in range(7)]
    assert statuses == [404] * 5 + [429] * 2


def test_no_token_ever_reaches_the_application_or_access_log(app, conn, caplog):
    admin = add_user(conn, 'adm', admin=True)
    new = invitations.create(conn, admin, 'a@example.org')
    client, kw = web(app)
    with caplog.at_level(logging.DEBUG):
        post(client, kw, '/auth/token/inspect', {'token': new.token})
        post(client, kw, '/auth/token/consume', {'token': new.token, 'username': 'alice', 'password': GOOD})
        post(client, kw, '/auth/token/consume', {'token': new.token, 'username': 'alice', 'password': GOOD})
    assert new.token not in caplog.text and GOOD not in caplog.text
    dump = json.dumps([dict(r) for r in conn.execute("SELECT * FROM audit_log")]) + json.dumps(
        [dict(r) for r in conn.execute("SELECT * FROM auth_tokens")], default=str)
    assert new.token not in dump


# --- /api/invitations ---------------------------------------------------------------------------------------------------------

def session_client(app, conn, user):
    client, kw = web(app)
    with client.session_transaction(**kw) as s:
        s['user_id'], s['token'], s['login_at'] = user['id'], 'tok', int(__import__('time').time())
    return client, {**kw, 'headers': {'Origin': f'https://{HOST}'}}


def test_api_create_list_revoke_flow_and_the_link_is_shown_once(app, conn):
    admin = add_user(conn, 'adm', admin=True)
    client, kw = session_client(app, conn, admin)
    created = client.post('/api/invitations', json={'email': 'Friend@Example.org', 'note': 'Salut', 'initial_credits': 2}, **kw)
    assert created.status_code == 201
    body = created.get_json()
    assert body['link'].startswith(f'https://{HOST}/auth/token#t=') and body['sent_by_mail'] is False
    token = body['link'].split('#t=')[1]
    assert '?' not in body['link'].split('#')[0]
    listed = client.get('/api/invitations', **kw).get_json()
    assert listed['can_invite'] is True and listed['invitations'][0]['email'] == 'friend@example.org'
    assert token not in json.dumps(listed) and body['link'] not in json.dumps(listed)
    assert client.delete(f"/api/invitations/{body['id']}", **kw).status_code == 200
    assert client.delete(f"/api/invitations/{body['id']}", **kw).status_code == 404
    assert tokens.inspect(conn, token) is None


def test_api_link_is_built_on_public_url_never_on_the_host_header(app, conn):
    admin = add_user(conn, 'adm', admin=True)
    client, kw = session_client(app, conn, admin)
    kw = {**kw, 'headers': {**kw['headers'], 'X-Forwarded-Host': 'evil.example', 'Host': HOST}}
    link = client.post('/api/invitations', json={'email': 'a@example.org'}, **kw).get_json()['link']
    assert link.startswith(f'https://{HOST}/') and 'evil' not in link


def test_api_non_admin_rules(app, conn):
    """CA-4 : un non-admin qui envoie invite_credits => refusé ; note refusée ; plus de crédit => refusé."""
    member = add_user(conn, 'mem', credits=1, email='mem@example.org')
    client, kw = session_client(app, conn, member)
    assert client.post('/api/invitations', json={'email': 'a@example.org', 'initial_credits': 5}, **kw).status_code == 400
    assert client.post('/api/invitations', json={'email': 'a@example.org', 'note': 'x'}, **kw).status_code == 400
    assert client.post('/api/invitations', json={'email': 'a@example.org'}, **kw).status_code == 201
    assert client.post('/api/invitations', json={'email': 'b@example.org'}, **kw).status_code == 409        # CA-2
    assert fresh(conn, member)['invite_credits'] == 0


@pytest.mark.parametrize('payload', [{}, {'email': 5}, {'email': None}, {'email': 'a@b.c', 'note': 5},
                                     {'email': 'a@b.c', 'initial_credits': '3'}, {'email': 'a@b.c', 'initial_credits': True},
                                     {'email': 'a@b.c\nBcc: x@y.z'}, {'email': 'a b@c.d'}, {'email': 'a@b.c', 'initial_credits': 1001}])
def test_api_bad_input(app, conn, payload):
    admin = add_user(conn, 'adm', admin=True)
    client, kw = session_client(app, conn, admin)
    assert client.post('/api/invitations', json=payload, **kw).status_code == 400
    assert conn.execute("SELECT COUNT(*) FROM auth_tokens").fetchone()[0] == 0


def test_api_taken_address_answers_409_to_an_admin_and_201_to_a_member(app, conn):
    admin = add_user(conn, 'adm', admin=True)
    member = add_user(conn, 'mem', credits=3, email='mem@example.org')
    add_user(conn, 'holder', email='held@example.org')
    a_client, a_kw = session_client(app, conn, admin)
    m_client, m_kw = session_client(app, conn, member)
    assert a_client.post('/api/invitations', json={'email': 'held@example.org'}, **a_kw).status_code == 409
    free = m_client.post('/api/invitations', json={'email': 'free@example.org'}, **m_kw)
    taken = m_client.post('/api/invitations', json={'email': 'held@example.org'}, **m_kw)
    assert free.status_code == taken.status_code == 201 and set(free.get_json()) == set(taken.get_json())


def test_api_needs_a_session_and_cannot_revoke_someone_else_s(app, conn):
    admin = add_user(conn, 'adm', admin=True)
    member = add_user(conn, 'mem', credits=3, email='mem@example.org')
    a_client, a_kw = session_client(app, conn, admin)
    theirs = a_client.post('/api/invitations', json={'email': 'a@example.org'}, **a_kw).get_json()['id']
    m_client, m_kw = session_client(app, conn, member)
    assert m_client.delete(f'/api/invitations/{theirs}', **m_kw).status_code == 404
    anonymous, kw = web(app)
    assert anonymous.get('/api/invitations', **kw).status_code == 401
    assert anonymous.post('/api/invitations', json={'email': 'a@b.c'}, headers={'Origin': f'https://{HOST}'}, **kw).status_code == 401


# --- Clé IP IPv6 /64 (T6-1) ---------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('address,key', [
    ('203.0.113.9', '203.0.113.9'), ('2001:db8:1:2:3:4:5:6', '2001:db8:1:2::/64'), ('2001:db8:1:2:ffff:ffff:ffff:ffff', '2001:db8:1:2::/64'),
    ('::ffff:198.51.100.7', '198.51.100.7'), ('::1', '::/64'), ('2001:DB8::1', '2001:db8::/64'), (' 10.0.0.1 ', '10.0.0.1'),
    ('pas-une-ip', 'invalid'), ('', 'invalid'), (None, 'invalid'),
])
def test_ip_key_groups_ipv6_by_64_and_maps_ipv4(address, key):
    assert ip_key(address) == key


def test_a_whole_ipv6_prefix_shares_one_counter():
    from app.ratelimit import LruLimiter
    limiter = LruLimiter(3, 900, clock=lambda: 1000.0)
    results = [limiter.hit(ip_key(f'2001:db8:1:2::{i:x}'))[0] for i in range(6)]
    assert results == [True, True, True, False, False, False]
    assert limiter.hit(ip_key('2001:db8:1:3::1'))[0] is True                   # un autre /64 a son compteur


# --- Migration 0013 -----------------------------------------------------------------------------------------------------------------

def test_migration_adds_the_columns_and_partial_unique_indexes(conn):
    cols = {r['name'] for r in conn.execute("PRAGMA table_info(users)")}
    assert {'password_hash', 'password_changed_at', 'pending_email', 'email_verified_at', 'invite_credits', 'invited_by'} <= cols
    assert conn.execute("SELECT 1 FROM schema_migrations WHERE name = '0013_users_password_accounts'").fetchone()
    add_user(conn, 'Alice', email='a@example.org')
    with pytest.raises(sqlite3.IntegrityError):
        add_user(conn, 'alice')                                                     # identifiant : unique (casse ignorée) parmi les password
    with pytest.raises(sqlite3.IntegrityError):
        add_user(conn, 'bob', email='a@example.org')                                # adresse validée : unique parmi les password
    add_user(conn, 'sso-user', method='oidc', email='a@example.org')              # pas d'index unique hors comptes password
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE users SET invite_credits = 1001 WHERE username = 'Alice'")


def test_existing_accounts_get_zero_credits_and_no_password(tmp_path):
    path = str(tmp_path / 'old.sqlite3')
    c = database.connect(path)
    c.executescript(database.SCHEMA)
    database.migrate(c, named=[])
    c.execute("INSERT INTO users (sub, username) VALUES ('idp-1', 'alice')")
    c.commit()
    c.close()
    database.init_db(path)
    c = database.connect(path)
    row = c.execute("SELECT invite_credits, password_hash, pending_email, invited_by FROM users").fetchone()
    assert tuple(row) == (0, None, None, None)
    c.close()


# --- consume avec kind : jamais d'appel sans type en production ---------------------------------------------------------------------

def test_every_production_call_of_consume_passes_a_kind():
    import ast
    import pathlib
    root = pathlib.Path('app')
    for path in root.rglob('*.py'):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'consume' \
                    and getattr(node.func.value, 'id', '') == 'tokens':
                assert any(k.arg == 'kind' for k in node.keywords), f'{path}:{node.lineno}'


def test_verify_email_consumption_always_carries_the_target_account():
    import pathlib
    source = pathlib.Path('app/auth/invitations.py').read_text(encoding='utf-8')
    assert "kind='verify_email', target_user_id=user['id']" in source


# --- PATCH /api/users : refus B5 tracé malgré la transaction englobante (T18-1) ----------------------------------------------------------

def test_a_refused_promotion_through_the_api_leaves_its_audit_line_and_changes_nothing_else(app, conn):
    admin = add_user(conn, 'adm', admin=True)
    from tests.test_auth_modes import _enable_totp
    _enable_totp(app, admin['id'])
    target = add_user(conn, 'nofactor')
    client, kw = session_client(app, conn, admin)
    response = client.patch(f"/api/users/{target['id']}", json={'is_admin': True, 'is_active': False}, **kw)
    assert response.status_code == 409
    row = fresh(conn, target)
    assert (row['is_admin'], row['is_active']) == (0, 1)                           # rien d'autre n'a été appliqué
    lines = [dict(r) for r in conn.execute("SELECT * FROM audit_log WHERE action = 'user.admin_refused'")]
    assert len(lines) == 1 and lines[0]['target_id'] == str(target['id']) and lines[0]['actor_id'] == admin['id']


def test_status_codes_409_for_no_credit_403_for_not_allowed(conn):
    nocredit = add_user(conn, 'nocredit', email='n@example.org')
    unverified = add_user(conn, 'unver', credits=3, pending='u@example.org')
    disabled = add_user(conn, 'off', credits=3, email='o@example.org', active=False)
    codes = {}
    for user in (nocredit, unverified, disabled):
        with pytest.raises(InvitationError) as exc:
            invitations.create(conn, user, 'x@example.org')
        codes[user['username']] = exc.value.status
    assert codes == {'nocredit': 409, 'unver': 403, 'off': 403}


def test_deactivating_or_deleting_an_issuer_revokes_its_pending_invitations_without_giving_credits_back(app, conn):
    """EF-3.7 : chemins de production (PATCH et DELETE /api/users)."""
    admin = add_user(conn, 'adm', admin=True)
    a, b = add_user(conn, 'issuer-a', credits=3, email='a@example.org'), add_user(conn, 'issuer-b', credits=3, email='b@example.org')
    mine = [invitations.create(conn, fresh(conn, a), f'a{i}@example.org') for i in range(2)]
    theirs = invitations.create(conn, fresh(conn, b), 'b0@example.org')
    used = invitations.create(conn, fresh(conn, a), 'a-used@example.org')
    invitations.accept(conn, used.token, 'someone', GOOD)
    unrelated = invitations.create(conn, admin, 'free@example.org')
    from tests.test_auth_modes import _enable_totp
    _enable_totp(app, admin['id'])
    client, kw = session_client(app, conn, admin)
    assert client.patch(f"/api/users/{a['id']}", json={'is_active': False}, **kw).status_code == 200
    assert [tokens.inspect(conn, t.token) for t in mine] == [None, None]
    assert tokens.inspect(conn, theirs.token) and tokens.inspect(conn, unrelated.token)
    assert fresh(conn, a)['invite_credits'] == 0                                           # 3 - 3 consommés : jamais rendus
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'token.revoke'").fetchone()[0] == 2
    assert client.delete(f"/api/users/{b['id']}", **kw).status_code == 200
    assert tokens.inspect(conn, theirs.token) is None and tokens.inspect(conn, unrelated.token)
    assert conn.execute("SELECT revoked_at FROM auth_tokens WHERE email = 'b0@example.org'").fetchone()[0]
    assert conn.execute("SELECT used_at FROM auth_tokens WHERE email = 'a-used@example.org'").fetchone()[0]       # utilisée : intacte


# --- T7b-2 : adresses canoniques pour TOUS les comptes (EF-4.10, §13 n°32) ---------------------------------------------------

def test_canonical_email_is_nfkc_casefold_and_stripped():
    from app.auth.emails import canonical_email, canonical_or_none
    assert canonical_email('  Foo@Example.COM \t') == 'foo@example.com'
    assert canonical_email('ＦＯＯ@ＥＸＡＭＰＬＥ.com') == 'foo@example.com'                      # NFKC : pleine chasse
    assert canonical_email('Straße@Example.org') == 'strasse@example.org'                      # casefold, pas lower()
    assert canonical_email('É@Example.org') == 'é@example.org'
    for bad in ('', ' ', 'pas-un-mail', None, 5, 'a b@c.d', 'x' * 600):
        assert canonical_or_none(bad) is None


def test_the_tokens_module_reexports_the_single_canonical_function():
    from app.auth import emails
    assert tokens.canonical_email is emails.canonical_email


def test_an_sso_login_stores_the_canonical_address_and_a_password_account_cannot_validate_it_under_another_case(conn):
    """Le scénario de la review : SSO « Foo@Example.com », compte password validant « foo@example.com »."""
    from app.auth import users
    sso = users.upsert_from_claims(conn, {'sub': 'idp-1', 'preferred_username': 'foo', 'email': 'Foo@Example.COM',
                                         'email_verified': True})
    assert sso['email'] == 'foo@example.com'
    user, new = make_pending(conn, 'pwd-user', 'foo@example.com')
    assert invitations.verify_email(conn, user, new.token) == 'refused'                       # adresse déjà portée


def test_sso_login_with_an_unverified_or_unusable_address_keeps_none(conn):
    from app.auth import users
    assert users.upsert_from_claims(conn, {'sub': 'a', 'email': 'X@Y.Z'})['email'] is None
    assert users.upsert_from_claims(conn, {'sub': 'b', 'email': 'pas-un-mail', 'email_verified': True})['email'] is None


def test_an_sso_address_taken_from_another_account_is_tolerated_but_flagged(conn, caplog):
    from app.auth import users
    add_user(conn, 'holder', email='shared@example.org')
    with caplog.at_level(logging.WARNING):
        users.upsert_from_claims(conn, {'sub': 'idp-9', 'email': 'SHARED@example.org', 'email_verified': True})
        users.upsert_from_claims(conn, {'sub': 'idp-9', 'email': 'SHARED@example.org', 'email_verified': True})     # inchangée : pas de doublon
    assert conn.execute("SELECT COUNT(*) FROM users WHERE email = 'shared@example.org'").fetchone()[0] == 2
    lines = [dict(r) for r in conn.execute("SELECT * FROM audit_log WHERE action = 'identity.collision'")]
    assert len(lines) == 1 and 'Collision' in caplog.text and 'shared' not in caplog.text


def test_match_login_compares_canonical_addresses_without_sql_lower(conn):
    from app.auth import users
    row = add_user(conn, 'u1', email='é@example.org')                                           # « É » : lower() SQL ne le replie pas
    for spelling in ('é@example.org', 'É@Example.ORG', ' É@EXAMPLE.org ', 'ｅ́@example.org'.replace('ｅ', 'e')):
        assert [r['id'] for r in users.match_login(conn, spelling)] == [row['id']], spelling
    assert users.match_login(conn, 'pas@trouve.org') == [] and users.match_login(conn, 'a b@c.d') == []
    assert [r['id'] for r in users.match_login(conn, 'U1')] == [row['id']]                      # identifiant : insensible à la casse
    add_user(conn, 'other', email=None)
    assert users.match_login(conn, 'other@example.org') == []


def test_the_inbox_finds_an_owner_by_canonical_address(conn):
    from app.services.inbox_watcher import _resolve_owner
    owner = add_user(conn, 'inboxer', email='dossier@example.org')
    cfg = {'AUTH_MODE': 'password'}
    for folder in ('dossier@example.org', 'DOSSIER@Example.ORG', ' Dossier@example.org'):
        assert _resolve_owner(conn, cfg, folder)['id'] == owner['id'], folder


def test_migration_0014_canonicalizes_existing_addresses_sso_included_and_is_idempotent(tmp_path):
    path = str(tmp_path / 'canon.sqlite3')
    c = database.connect(path)
    c.executescript(database.SCHEMA)
    names = [n for n, _ in database.NAMED_MIGRATIONS]
    database.migrate(c, named=[m for m in database.NAMED_MIGRATIONS if names.index(m[0]) < names.index('0014_emails_canonical')])
    c.executemany("INSERT INTO users (sub, username, email, pending_email, auth_method) VALUES (?, ?, ?, ?, ?)",
                  [('idp-1', 'a', 'Foo@Example.COM', None, 'oidc'), ('idp-2', 'b', None, ' Bar@EXAMPLE.org ', 'oidc'),
                   ('idp-3', 'c', 'Ünï@Example.org', None, 'oidc'), ('idp-4', 'd', 'déjà-canonique@example.org', None, 'oidc'),
                   ('idp-5', 'e', 'pas un mail', None, 'oidc')])
    c.commit()
    c.close()
    database.init_db(path)
    database.init_db(path)
    c = database.connect(path)
    got = {r['username']: (r['email'], r['pending_email']) for r in c.execute("SELECT username, email, pending_email FROM users")}
    assert got == {'a': ('foo@example.com', None), 'b': (None, 'bar@example.org'), 'c': ('ünï@example.org', None),
                   'd': ('déjà-canonique@example.org', None), 'e': ('pas un mail', None)}      # inutilisable : laissée telle quelle
    assert c.execute("SELECT 1 FROM schema_migrations WHERE name = '0014_emails_canonical'").fetchone()
    c.close()


def test_migration_0014_never_fails_on_collisions_and_flags_them(tmp_path, caplog):
    path = str(tmp_path / 'collide.sqlite3')
    c = database.connect(path)
    c.executescript(database.SCHEMA)
    names = [n for n, _ in database.NAMED_MIGRATIONS]
    database.migrate(c, named=[m for m in database.NAMED_MIGRATIONS if names.index(m[0]) < names.index('0014_emails_canonical')])
    c.executemany("INSERT INTO users (sub, username, email, auth_method) VALUES (?, ?, ?, ?)",
                  [('idp-1', 'a', 'Dup@Example.org', 'oidc'), ('idp-2', 'b', 'dup@example.org', 'oidc'),
                   ('pwd:1', 'p', 'Pwd@Example.org', 'password'), ('idp-3', 'c', 'PWD@Example.org', 'oidc')])
    c.commit()
    c.close()
    with caplog.at_level(logging.WARNING):
        database.init_db(path)                                                              # ne lève pas
    c = database.connect(path)
    assert {r[0] for r in c.execute("SELECT email FROM users WHERE username IN ('a', 'b')")} == {'dup@example.org'}
    line = c.execute("SELECT details FROM audit_log WHERE action = 'identity.collision'").fetchone()
    assert line and 'accounts' in line[0] and 'collision' in caplog.text.lower()
    assert c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 4                        # aucune ligne perdue
    c.close()


def test_no_production_query_compares_addresses_with_sql_lower():
    import pathlib
    for path in pathlib.Path('app').rglob('*.py'):
        text = path.read_text(encoding='utf-8')
        assert 'lower(email)' not in text and 'lower(pending_email)' not in text, path
