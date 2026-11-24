"""Jetons à usage unique et amorçage (T7 début, EF-0.3, EF-3.3, EF-3.6, EF-S8, CA-5, CA-52)."""

import json
import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet

from app import create_app, db as database
from app.audit import Actor
from app.auth import tokens
from app.config import TestingConfig

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
CLI = Actor.cli()


@pytest.fixture
def conn(tmp_path):
    path = str(tmp_path / 't.sqlite3')
    database.init_db(path)
    c = database.connect(path)
    c.execute("INSERT INTO users (sub, username) VALUES ('s1', 'alice')")
    c.commit()
    yield c
    c.close()


def rows(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM auth_tokens ORDER BY id")]


def everything(conn):
    """Tout ce que la base et l'audit contiennent, pour y chercher un jeton."""
    return json.dumps([dict(r) for r in conn.execute("SELECT * FROM auth_tokens")], default=str) + \
        json.dumps([dict(r) for r in conn.execute("SELECT * FROM audit_log")])


# --- Création et stockage ---------------------------------------------------------------------

def test_migration_is_named_and_shaped(conn):
    assert conn.execute("SELECT 1 FROM schema_migrations WHERE name = '0012_auth_tokens'").fetchone()
    cols = {r['name'] for r in conn.execute("PRAGMA table_info(auth_tokens)")}
    assert cols == {'id', 'token_hash', 'kind', 'created_by', 'target_user_id', 'email', 'is_admin_grant', 'initial_credits',
                    'note', 'sent_by_mail', 'created_at', 'expires_at', 'used_at', 'used_by', 'revoked_at'}


def test_token_is_256_bits_random_and_only_its_hash_is_stored(conn):
    a, b = (tokens.create(conn, 'invite', actor=CLI, email='x@example.org') for _ in range(2))
    assert a.token != b.token and len(a.token) == 43
    stored = rows(conn)[0]
    assert stored['token_hash'] == tokens.hash_token(a.token) and len(stored['token_hash']) == 64
    assert a.token not in everything(conn) and b.token not in everything(conn)


def test_the_link_carries_the_token_in_the_fragment_only(conn):
    new = tokens.create(conn, 'bootstrap', actor=CLI, is_admin_grant=True)
    url = tokens.link('https://traces.example.com/', new.token)
    assert url == f'https://traces.example.com/auth/token#t={new.token}'
    path_and_query = url.split('#')[0]
    assert new.token not in path_and_query and '?' not in path_and_query
    with pytest.raises(ValueError):
        tokens.link('', new.token)


def test_default_lifetimes_per_kind(conn):
    for kind, delta in (('invite', timedelta(days=7)), ('bootstrap', timedelta(hours=24)), ('reset', timedelta(hours=24)),
                        ('set_password', timedelta(hours=24)), ('verify_email', timedelta(hours=48))):
        new = tokens.create(conn, kind, actor=CLI, now=NOW)
        assert new.expires_at == (NOW + delta).strftime('%Y-%m-%dT%H:%M:%SZ'), kind


@pytest.mark.parametrize('ttl', [timedelta(minutes=59), timedelta(days=31), timedelta(0), timedelta(days=-1)])
def test_lifetime_is_bounded(conn, ttl):
    with pytest.raises(ValueError):
        tokens.create(conn, 'invite', actor=CLI, ttl=ttl)
    assert rows(conn) == []


@pytest.mark.parametrize('kwargs', [
    dict(kind='autre'), dict(kind='invite', is_admin_grant=True), dict(kind='reset', is_admin_grant=True),
    dict(kind='verify_email', is_admin_grant=True), dict(kind='invite', initial_credits=-1),
    dict(kind='invite', initial_credits=1001), dict(kind='invite', initial_credits=True),
    dict(kind='invite', note='x' * 201), dict(kind='invite', note='a\nb'), dict(kind='invite', note=5),
    dict(kind='invite', email='pas-un-mail'), dict(kind='invite', email='a b@c.d'), dict(kind='invite', email='a@b.c\r\nBcc: x@y'),
    dict(kind='invite', email='x' * 251 + '@y.z'), dict(kind='invite', email=5),
])
def test_invalid_tokens_are_refused_and_nothing_is_written(conn, kwargs):
    with pytest.raises(ValueError):
        tokens.create(conn, kwargs.pop('kind'), actor=CLI, **kwargs)
    assert rows(conn) == [] and conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 0


def test_only_a_bootstrap_token_can_give_admin_and_the_database_agrees(conn):
    assert tokens.create(conn, 'bootstrap', actor=CLI, is_admin_grant=True)
    assert rows(conn)[0]['is_admin_grant'] == 1
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO auth_tokens (token_hash, kind, is_admin_grant, created_at, expires_at) "
                     "VALUES ('h', 'invite', 1, 'a', 'b')")


def test_email_is_normalized_and_the_note_is_kept(conn):
    tokens.create(conn, 'invite', actor=CLI, email='  Bob@Example.ORG ', note="Bienvenue", initial_credits=3)
    stored = rows(conn)[0]
    assert (stored['email'], stored['note'], stored['initial_credits']) == ('bob@example.org', 'Bienvenue', 3)


def test_creation_is_audited_without_the_token_and_with_the_actor(conn):
    new = tokens.create(conn, 'bootstrap', actor=CLI, is_admin_grant=True, email='a@b.c', ip='203.0.113.1')
    entry = dict(conn.execute("SELECT * FROM audit_log").fetchone())
    assert (entry['action'], entry['actor_type'], entry['actor_id'], entry['target_id'], entry['ip']) == \
        ('token.create', 'cli', None, str(new.id), '203.0.113.1')
    assert json.loads(entry['details'])['admin_grant'] is True and new.token not in json.dumps(entry)
    assert 'a@b.c' not in json.dumps(entry)


# --- inspect : même réponse pour tout jeton non valable (EF-3.6, CA-5) ------------------------------------

def test_inspect_gives_only_the_form_type(conn):
    expected = {'invite': 'choose_credentials', 'bootstrap': 'choose_credentials', 'reset': 'set_password',
                'set_password': 'set_password', 'verify_email': 'verify_email'}
    for kind, form in expected.items():
        new = tokens.create(conn, kind, actor=CLI, email='secret-address@example.org', note='note privée')
        assert tokens.inspect(conn, new.token) == form
    assert isinstance(tokens.inspect(conn, new.token), str)          # une chaîne : aucune adresse, émetteur, note ni échéance


def test_inspect_does_not_consume(conn):
    new = tokens.create(conn, 'invite', actor=CLI)
    for _ in range(3):
        assert tokens.inspect(conn, new.token) == 'choose_credentials'
    assert rows(conn)[0]['used_at'] is None
    assert tokens.consume(conn, new.token, kind='invite')


def test_unknown_expired_used_revoked_and_malformed_tokens_are_indistinguishable(conn):
    expired = tokens.create(conn, 'invite', actor=CLI, now=NOW - timedelta(days=8))
    used = tokens.create(conn, 'invite', actor=CLI)
    assert tokens.consume(conn, used.token, kind='invite')
    revoked = tokens.create(conn, 'invite', actor=CLI)
    assert tokens.revoke(conn, revoked.id, actor=CLI)
    bad = [expired.token, used.token, revoked.token, 'A' * 43, 'x', '', None, 5, b'bytes', 'é' * 43, ' ' + 'A' * 42,
           expired.token + 'A', "' OR 1=1 --"]
    assert {repr(tokens.inspect(conn, t, now=NOW)) for t in bad} == {'None'}
    assert {repr(tokens.consume(conn, t, now=NOW, kind='invite')) for t in bad} == {'None'}


def test_expiry_boundary_is_exclusive(conn):
    new = tokens.create(conn, 'invite', actor=CLI, now=NOW, ttl=timedelta(hours=2))
    assert tokens.inspect(conn, new.token, now=NOW + timedelta(hours=2) - timedelta(seconds=1)) == 'choose_credentials'
    assert tokens.inspect(conn, new.token, now=NOW + timedelta(hours=2)) is None


# --- consume : atomique, usage unique ----------------------------------------------------------------

def test_consume_returns_what_the_token_carries_once(conn):
    new = tokens.create(conn, 'bootstrap', actor=CLI, email='boot@example.org', is_admin_grant=True, initial_credits=5,
                        note='n', created_by=1, target_user_id=1)
    info = tokens.consume(conn, new.token, used_by=1, kind='bootstrap')
    assert info == tokens.TokenInfo(new.id, 'bootstrap', 1, 1, 'boot@example.org', True, 5, 'n')
    stored = rows(conn)[0]
    assert stored['used_at'] and stored['used_by'] == 1
    assert tokens.consume(conn, new.token, kind='invite') is None and tokens.inspect(conn, new.token) is None


def test_consumption_is_rolled_back_with_the_callers_transaction(conn):
    """Compte non créé => jeton non consommé."""
    new = tokens.create(conn, 'invite', actor=CLI)
    with pytest.raises(RuntimeError), database.write_transaction(conn):
        assert tokens.consume(conn, new.token, kind='invite')
        raise RuntimeError("création du compte échouée")
    assert rows(conn)[0]['used_at'] is None and tokens.inspect(conn, new.token)


def test_two_concurrent_consumptions_yield_exactly_one_success(tmp_path):
    path = str(tmp_path / 'race.sqlite3')
    database.init_db(path)
    setup = database.connect(path)
    new = tokens.create(setup, 'invite', actor=CLI)
    setup.close()
    results, barrier = [], threading.Barrier(8)

    def worker():
        c = database.connect(path)
        try:
            barrier.wait(10)
            results.append(tokens.consume(c, new.token, kind='invite'))
        finally:
            c.close()

    threads = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    assert sum(r is not None for r in results) == 1 and len(results) == 8


def test_set_used_by_completes_the_consumption_record(conn):
    new = tokens.create(conn, 'invite', actor=CLI)
    info = tokens.consume(conn, new.token, kind='invite')
    tokens.set_used_by(conn, info.id, 1)
    assert rows(conn)[0]['used_by'] == 1
    other = tokens.create(conn, 'invite', actor=CLI)
    tokens.set_used_by(conn, other.id, 1)                             # non consommé : rien ne change
    assert rows(conn)[1]['used_by'] is None


# --- Révocation (EF-3.7) --------------------------------------------------------------------------

def test_revoke_only_acts_on_an_unused_token_and_is_audited(conn):
    new = tokens.create(conn, 'invite', actor=CLI)
    assert tokens.revoke(conn, new.id, actor=Actor.user(1, 'alice')) is True
    assert tokens.revoke(conn, new.id, actor=CLI) is False and tokens.inspect(conn, new.token) is None
    used = tokens.create(conn, 'invite', actor=CLI)
    tokens.consume(conn, used.token, kind='invite')
    assert tokens.revoke(conn, used.id, actor=CLI) is False
    assert [r['action'] for r in conn.execute("SELECT action FROM audit_log WHERE action = 'token.revoke'")] == ['token.revoke']
    assert new.token not in everything(conn)


def test_a_disabled_or_deleted_issuer_loses_its_pending_invitations(conn):
    mine = [tokens.create(conn, 'invite', actor=CLI, created_by=1) for _ in range(2)]
    other = tokens.create(conn, 'invite', actor=CLI, created_by=None)
    used = tokens.create(conn, 'invite', actor=CLI, created_by=1)
    tokens.consume(conn, used.token, kind='invite')
    assert tokens.revoke_pending_for_creator(conn, 1, actor=CLI) == 2
    assert [tokens.inspect(conn, t.token) for t in mine] == [None, None] and tokens.inspect(conn, other.token)


def test_deleting_the_issuer_keeps_the_token_row_without_an_owner(conn):
    new = tokens.create(conn, 'invite', actor=CLI, created_by=1)
    conn.execute("DELETE FROM users WHERE id = 1")
    conn.commit()
    assert rows(conn)[0]['created_by'] is None and tokens.inspect(conn, new.token)


def test_constant_time_match_helper(conn):
    new = tokens.create(conn, 'invite', actor=CLI)
    stored = rows(conn)[0]['token_hash']
    assert tokens.matches(stored, new.token) and not tokens.matches(stored, 'A' * 43) and not tokens.matches(stored, None)


def test_no_token_ever_reaches_a_log(conn, caplog):
    with caplog.at_level(logging.DEBUG):
        new = tokens.create(conn, 'invite', actor=CLI)
        tokens.inspect(conn, new.token)
        info = tokens.consume(conn, new.token, kind='invite')
        tokens.revoke(conn, info.id, actor=CLI)
        tokens.inspect(conn, 'A' * 43)
    assert new.token not in caplog.text


# --- flask user-invite --admin [--account] (EF-0.3, CA-52) -------------------------------------------

KEY = Fernet.generate_key().decode()
HOST = 'traces.example.com'


@pytest.fixture
def app(tmp_path):
    class Prod(TestingConfig):
        TESTING = False
        INBOX_ENABLED = False
        SECRET_KEY = 'k' * 40
        SESSION_COOKIE_SECURE = None

    return create_app(Prod, overrides={'DATA_DIR': str(tmp_path / 'data'), 'AUTH_MODE': 'password', 'TRUSTED_HOSTS': [HOST],
                                       'SECRETS_KEY': KEY, 'PUBLIC_URL': f'https://{HOST}'})


def add_account(app, sub, method='password', email=None):
    c = database.connect(app.config['DB_PATH'])
    c.execute("INSERT INTO users (sub, username, email, auth_method) VALUES (?, ?, ?, ?)", (sub, sub, email, method))
    c.commit()
    uid = c.execute("SELECT id FROM users WHERE sub = ?", (sub,)).fetchone()[0]
    c.close()
    return uid


def invoke(app, *args):
    return app.test_cli_runner().invoke(args=['user-invite', *args])


def stored(app):
    c = database.connect(app.config['DB_PATH'])
    try:
        return [dict(r) for r in c.execute("SELECT * FROM auth_tokens ORDER BY id")]
    finally:
        c.close()


def test_user_invite_admin_creates_a_bootstrap_token_and_prints_the_link_once(app):
    result = invoke(app, '--admin', 'First@Example.org')
    assert result.exit_code == 0
    link = next(line for line in result.output.splitlines() if line.startswith(f'https://{HOST}/auth/token#t='))
    token = link.split('#t=')[1]
    (row,) = stored(app)
    assert (row['kind'], row['email'], row['is_admin_grant'], row['target_user_id'], row['created_by']) == \
        ('bootstrap', 'first@example.org', 1, None, None)
    assert row['token_hash'] == tokens.hash_token(token)
    assert token not in json.dumps(row)
    c = database.connect(app.config['DB_PATH'])
    entry = dict(c.execute("SELECT * FROM audit_log WHERE action = 'token.create'").fetchone())
    c.close()
    assert entry['actor_type'] == 'cli' and token not in json.dumps(entry)
    created = datetime.strptime(row['created_at'], '%Y-%m-%dT%H:%M:%SZ')
    assert datetime.strptime(row['expires_at'], '%Y-%m-%dT%H:%M:%SZ') - created == timedelta(hours=24)


def test_user_invite_requires_the_admin_flag_and_an_address(app):
    assert invoke(app, 'a@b.c').exit_code != 0
    assert invoke(app, '--admin').exit_code != 0
    assert invoke(app, '--admin', 'pas-un-mail').exit_code != 0
    assert stored(app) == []


def test_user_invite_refuses_an_address_already_carried_by_an_account(app):
    add_account(app, 'pwd:1', email='taken@example.org')
    result = invoke(app, '--admin', 'TAKEN@example.org')
    assert result.exit_code != 0 and stored(app) == []


def test_user_invite_account_resumes_the_bootstrap_of_a_password_account_without_factor(app):
    uid = add_account(app, 'pwd:2', email='resume@example.org')
    result = invoke(app, '--admin', '--account', 'pwd:2')
    assert result.exit_code == 0
    (row,) = stored(app)
    assert (row['target_user_id'], row['kind'], row['is_admin_grant'], row['email']) == (uid, 'bootstrap', 1, 'resume@example.org')


def test_user_invite_account_refuses_sso_accounts_accounts_with_a_factor_and_ambiguous_logins(app):
    add_account(app, 'idp-1', method='oidc')
    with_factor = add_account(app, 'pwd:3')
    c = database.connect(app.config['DB_PATH'])
    c.execute("ALTER TABLE users ADD COLUMN totp_secret BLOB")
    c.execute("ALTER TABLE users ADD COLUMN totp_enabled_at TEXT")
    c.execute("UPDATE users SET totp_secret = x'01', totp_enabled_at = '2026-01-01T00:00:00Z' WHERE id = ?", (with_factor,))
    c.commit()
    c.close()
    for account in ('idp-1', 'pwd:3', 'inconnu'):
        assert invoke(app, '--admin', '--account', account).exit_code != 0
    assert stored(app) == []


def test_user_invite_is_unavailable_without_password_accounts(tmp_path):
    local = create_app(TestingConfig, overrides={'DATA_DIR': str(tmp_path / 'l'), 'AUTH_MODE': 'local'})
    result = invoke(local, '--admin', 'a@b.c')
    assert result.exit_code != 0 and 'mot de passe' in result.output


def test_two_invitations_give_two_distinct_valid_tokens(app):
    first = invoke(app, '--admin', 'one@example.org').output
    second = invoke(app, '--admin', 'two@example.org').output
    assert first != second and len(stored(app)) == 2


# --- Contraintes de consommation (R2, CA-17b, T7a-1) --------------------------------------------------------

def test_consume_with_a_wrong_kind_or_wrong_target_consumes_nothing_and_the_token_stays_usable(conn):
    conn.execute("INSERT INTO users (sub, username) VALUES ('s2', 'bob')")
    conn.commit()
    new = tokens.create(conn, 'verify_email', actor=CLI, target_user_id=1, email='a@b.c')
    for wrong in (dict(kind='invite'), dict(kind='reset'), dict(target_user_id=2), dict(kind='verify_email', target_user_id=2),
                  dict(kind='invite', target_user_id=1)):
        assert tokens.consume(conn, new.token, **{'kind': 'verify_email', **wrong}) is None, wrong
        assert rows(conn)[0]['used_at'] is None and tokens.inspect(conn, new.token) == 'verify_email'
    info = tokens.consume(conn, new.token, kind='verify_email', target_user_id=1)
    assert info and info.kind == 'verify_email' and info.target_user_id == 1
    assert tokens.consume(conn, new.token, kind='verify_email', target_user_id=1) is None       # usage unique


def test_constraints_are_optional_and_validated(conn):
    plain = tokens.create(conn, 'invite', actor=CLI)
    assert tokens.consume(conn, plain.token, kind='invite')                                     # type seul
    untargeted = tokens.create(conn, 'invite', actor=CLI)
    assert tokens.consume(conn, untargeted.token, target_user_id=1, kind='invite') is None                      # exige un compte visé
    assert tokens.consume(conn, untargeted.token, kind='inconnu') is None
    for bad in (0, -1, True, 2 ** 63, '1', 1.0):
        assert tokens.consume(conn, untargeted.token, target_user_id=bad, kind='invite') is None, bad
    assert tokens.consume(conn, untargeted.token, kind='invite')                                               # toujours consommable sans contrainte


def test_a_constrained_consumption_is_still_atomic_under_concurrency(tmp_path):
    path = str(tmp_path / 'race2.sqlite3')
    database.init_db(path)
    setup = database.connect(path)
    setup.execute("INSERT INTO users (sub, username) VALUES ('s1', 'a'), ('s2', 'b')")
    setup.commit()
    new = tokens.create(setup, 'verify_email', actor=CLI, target_user_id=1)
    setup.close()
    results, barrier = [], threading.Barrier(8)

    def worker(target):
        c = database.connect(path)
        try:
            barrier.wait(10)
            results.append((target, tokens.consume(c, new.token, kind='verify_email', target_user_id=target)))
        finally:
            c.close()

    threads = [threading.Thread(target=worker, args=(1 if i % 2 else 2,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    winners = [target for target, info in results if info]
    assert winners == [1]                                                                       # un seul, et c'est le bon compte


# --- B5 : chemin commun CLI/API (CA-13) -----------------------------------------------------------------

def test_set_admin_is_the_shared_rule_for_cli_and_api(tmp_path):
    from app.auth import users
    path = str(tmp_path / 'adm.sqlite3')
    database.init_db(path)
    c = database.connect(path)
    c.execute("INSERT INTO users (sub, username, auth_method) VALUES ('pwd:1', 'p', 'password'), ('idp', 'o', 'oidc')")
    c.commit()
    with pytest.raises(users.AdminPromotionRefused) as exc:
        users.set_admin(c, 1, True, actor=CLI)
    assert str(exc.value) == users.ADMIN_NEEDS_FACTOR
    assert c.execute("SELECT is_admin FROM users WHERE id = 1").fetchone()[0] == 0
    assert c.execute("SELECT action FROM audit_log").fetchone()[0] == 'user.admin_refused'
    assert users.set_admin(c, 2, True, actor=CLI)['is_admin'] == 1
    with pytest.raises(LookupError):
        users.set_admin(c, 99, True, actor=CLI)
    c.close()
