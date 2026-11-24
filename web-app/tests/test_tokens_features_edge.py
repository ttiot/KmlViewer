"""
Cas limites de T7a (tokens.py, flask user-invite : EF-0.3, EF-3.3, EF-3.6, EF-S8, CA-5, CA-52), de T5
(features.py : EF-7, CA-22) et des corrections T8-1 à T8-4 (admin_required, TOTP non confirmé, sub `pwd:`,
repli de l'inbox). Complète test_tokens.py, test_features.py et test_auth_modes.py.
"""

import json
import logging
import os
import pathlib
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet
from flask import jsonify, render_template_string

from app import create_app
from app import db as database
from app import features
from app.audit import Actor
from app.auth import factors, tokens, users
from app.config import TestingConfig
from app.features import FEATURES, FeatureError, feature_required
from app.services import inbox_watcher

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
CLI = Actor.cli()
KEY = Fernet.generate_key().decode()
HOST = 'traces.example.com'
FEATURE = 'route_builder'
ADMIN = Actor.user(1, 'admin')


@pytest.fixture
def conn(tmp_path):
    path = str(tmp_path / 't.sqlite3')
    database.init_db(path)
    c = database.connect(path)
    c.executemany("INSERT INTO users (sub, username) VALUES (?, ?)", [(f's{i}', f'u{i}') for i in range(1, 6)])
    c.commit()
    yield c
    c.close()


@pytest.fixture
def path(tmp_path):
    p = str(tmp_path / 'p.sqlite3')
    database.init_db(p)
    c = database.connect(p)
    c.execute("INSERT INTO users (sub, username) VALUES ('s1', 'alice')")
    c.commit()
    c.close()
    return p


class Prod(TestingConfig):
    TESTING = False
    INBOX_ENABLED = False
    SECRET_KEY = 'k' * 40
    SESSION_COOKIE_SECURE = None


PASSWORD_MODE = {'AUTH_MODE': 'password', 'TRUSTED_HOSTS': [HOST], 'SECRETS_KEY': KEY, 'PUBLIC_URL': f'https://{HOST}'}


@pytest.fixture
def app(tmp_path):
    return create_app(Prod, overrides={'DATA_DIR': str(tmp_path / 'data'), **PASSWORD_MODE})


def add_account(app, sub, method='password', email=None, admin=False, token='tok', active=True):
    c = database.connect(app.config['DB_PATH'])
    c.execute("INSERT INTO users (sub, username, email, auth_method, is_admin, is_active, session_token) VALUES (?, ?, ?, ?, ?, ?, ?)",
              (sub, sub, email, method, int(admin), int(active), token))
    c.commit()
    uid = c.execute("SELECT id FROM users WHERE sub = ?", (sub,)).fetchone()[0]
    c.close()
    return uid


def sign_in(client, uid, token='tok'):
    with client.session_transaction(base_url=f'https://{HOST}') as session:
        session['user_id'], session['token'], session['login_at'] = uid, token, int(time.time())


def get(client, url, **kw):
    return client.get(url, base_url=f'https://{HOST}', **kw)


def stored(app):
    c = database.connect(app.config['DB_PATH'])
    try:
        return [dict(r) for r in c.execute("SELECT * FROM auth_tokens ORDER BY id")]
    finally:
        c.close()


def invoke(app, *args, **kw):
    return app.test_cli_runner().invoke(args=['user-invite', *args], **kw)


def everything(conn):
    return json.dumps([dict(r) for r in conn.execute("SELECT * FROM auth_tokens")], default=str) + \
        json.dumps([dict(r) for r in conn.execute("SELECT * FROM audit_log")])


# =================================================== T7a : tokens.py ===================================================

def test_the_new_token_object_does_not_print_the_token(conn):
    new = tokens.create(conn, 'invite', actor=CLI)
    assert new.token not in repr(new) and new.token not in str(new) and new.token not in f'{new}' and new.token not in f'{new!r}'


def test_no_token_in_any_exception_message_or_traceback(conn):
    import traceback
    new = tokens.create(conn, 'invite', actor=CLI)
    for call in (lambda: tokens.create(conn, 'invite', actor=CLI, email=new.token), lambda: tokens.create(conn, 'invite', actor=CLI, note=new.token * 5),
                 lambda: tokens.create(conn, 'invite', actor=CLI, created_by=999), lambda: tokens.consume(conn, new.token, used_by=999)):
        with pytest.raises(Exception) as exc:      # noqa: B017 - on veut tout type d'exception
            call()
        assert new.token not in ''.join(traceback.format_exception(exc.value)) + repr(exc.value) + str(exc.value)
    assert repr(tokens.TokenInfo(1, 'invite', None, None, None, False, 0, None)).count('token') == 0


def test_the_token_is_never_stored_logged_or_audited_through_the_whole_life_cycle(conn, caplog):
    with caplog.at_level(logging.DEBUG):
        created = [tokens.create(conn, kind, actor=CLI, created_by=1, email=f'{kind}@example.org', note='n')
                   for kind in tokens.KINDS if kind != 'bootstrap']
        created.append(tokens.create(conn, 'bootstrap', actor=CLI, is_admin_grant=True, email='b@example.org'))
        for new in created:
            tokens.inspect(conn, new.token)
        infos = [tokens.consume(conn, new.token, used_by=1) for new in created[:3]]
        for new in created[3:]:
            tokens.revoke(conn, new.id, actor=CLI)
        tokens.revoke_pending_for_creator(conn, 1, actor=CLI)
        for info in infos:
            tokens.set_used_by(conn, info.id, 1)
    dump = everything(conn)
    for new in created:
        assert new.token not in dump and new.token not in caplog.text
        assert hash_of(new.token) in dump                                 # seul le hash est conservé


def hash_of(token):
    return tokens.hash_token(token)


def test_the_hash_is_the_only_trace_and_cannot_be_used_as_a_token(conn):
    new = tokens.create(conn, 'invite', actor=CLI)
    digest = tokens.hash_token(new.token)
    assert len(digest) == 64 and tokens.inspect(conn, digest) is None and tokens.consume(conn, digest) is None


@pytest.mark.parametrize('ttl,ok', [(timedelta(hours=1), True), (timedelta(days=30), True), (timedelta(hours=1, seconds=1), True),
                                    (timedelta(hours=1) - timedelta(microseconds=1), False), (timedelta(days=30, microseconds=1), False)])
def test_lifetime_bounds_are_inclusive_to_the_microsecond(conn, ttl, ok):
    if ok:
        assert tokens.create(conn, 'invite', actor=CLI, ttl=ttl, now=NOW).id
    else:
        with pytest.raises(ValueError):
            tokens.create(conn, 'invite', actor=CLI, ttl=ttl)


@pytest.mark.parametrize('ttl', [3600, '1h', None.__class__, float('inf'), 1.5])
def test_a_ttl_that_is_not_a_timedelta_is_refused_cleanly(conn, ttl):
    with pytest.raises((ValueError, TypeError)):
        tokens.create(conn, 'invite', actor=CLI, ttl=ttl)
    assert conn.execute("SELECT COUNT(*) FROM auth_tokens").fetchone()[0] == 0


def test_expiry_is_evaluated_in_utc_whatever_the_timezone_of_now(conn):
    new = tokens.create(conn, 'invite', actor=CLI, now=NOW, ttl=timedelta(hours=2))          # expire à 14:00 UTC
    plus_two = timezone(timedelta(hours=2))
    assert tokens.inspect(conn, new.token, now=datetime(2026, 11, 24, 13, 59, tzinfo=plus_two)) == 'choose_credentials'     # 11:59 UTC
    assert tokens.inspect(conn, new.token, now=datetime(2026, 11, 24, 15, 59, tzinfo=plus_two)) == 'choose_credentials'     # 13:59 UTC
    assert tokens.inspect(conn, new.token, now=datetime(2026, 11, 24, 16, 0, tzinfo=plus_two)) is None                      # 14:00 UTC : échu
    assert tokens.inspect(conn, new.token, now=datetime(2026, 11, 24, 14, 0)) is None                                         # naïf = UTC


def test_a_clock_going_backwards_never_extends_a_token_that_was_used_or_revoked(conn):
    used = tokens.create(conn, 'invite', actor=CLI, now=NOW)
    assert tokens.consume(conn, used.token, now=NOW + timedelta(hours=1))
    assert tokens.inspect(conn, used.token, now=NOW - timedelta(days=365)) is None
    assert tokens.consume(conn, used.token, now=NOW - timedelta(days=365)) is None
    revoked = tokens.create(conn, 'invite', actor=CLI, now=NOW)
    tokens.revoke(conn, revoked.id, actor=CLI, now=NOW)
    assert tokens.inspect(conn, revoked.token, now=NOW - timedelta(days=1)) is None


@pytest.mark.parametrize('bad', ['A' * 43 + '\n', '\n' + 'A' * 43, 'A' * 43 + '\x00', 'A' * 42, 'A' * 44, 'Ａ' * 43, '𝐀' * 43,
                                 'A' * 42 + '=', 'A' * 42 + '+', 'A' * 42 + '/', 'A' * 21 + ' ' + 'A' * 21, 'A' * 10_000, 'A' * 5_000_000,
                                 '%41' * 14, 'A' * 43 + '#t=x', '<script>' + 'A' * 35, "A' OR '1'='1" + 'A' * 30, b'A' * 43, bytearray(b'A' * 43),
                                 ['A' * 43], {'t': 'A' * 43}, 4.3, True, object()], ids=lambda v: repr(v)[:24])
def test_malformed_and_enormous_tokens_get_the_unknown_answer_without_touching_the_database(conn, bad):
    statements = []
    conn.set_trace_callback(statements.append)
    started = time.monotonic()
    assert tokens.inspect(conn, bad) is None and tokens.consume(conn, bad) is None and tokens.hash_token(bad) is None
    assert time.monotonic() - started < 1
    conn.set_trace_callback(None)
    assert statements == []                                                     # forme invalide : aucune requête


def test_a_well_formed_but_unknown_token_costs_one_read_exactly_like_an_expired_or_used_one(conn):
    expired = tokens.create(conn, 'invite', actor=CLI, now=NOW - timedelta(days=30))
    used = tokens.create(conn, 'invite', actor=CLI)
    tokens.consume(conn, used.token)
    probes = [expired.token, used.token, 'B' * 43]
    shapes = []
    for probe in probes:
        statements = []
        conn.set_trace_callback(statements.append)
        assert tokens.inspect(conn, probe, now=NOW) is None
        conn.set_trace_callback(None)
        shapes.append(len(statements))
    assert len(set(shapes)) == 1                                                # même travail : aucun canal auxiliaire entre les cas


def test_inspect_never_exposes_anything_but_a_form_name_for_every_kind(conn):
    allowed = {'choose_credentials', 'set_password', 'verify_email'}
    for kind in tokens.KINDS:
        new = tokens.create(conn, kind, actor=CLI, email='cible@example.org', note='note secrète', initial_credits=7,
                            created_by=1, target_user_id=1, is_admin_grant=(kind == 'bootstrap'))
        answer = tokens.inspect(conn, new.token)
        assert answer in allowed and 'cible' not in answer and 'secrète' not in answer


def test_consume_and_revoke_race_has_exactly_one_winner_every_time(tmp_path):
    path = str(tmp_path / 'race.sqlite3')
    database.init_db(path)
    setup = database.connect(path)
    created = [tokens.create(setup, 'invite', actor=CLI) for _ in range(30)]
    setup.close()
    outcome = {}
    barrier = threading.Barrier(2 * 6)

    def consumer(new):
        c = database.connect(path)
        try:
            barrier.wait(10)
            outcome.setdefault(('consume', new.id), []).append(tokens.consume(c, new.token) is not None)
        finally:
            c.close()

    def revoker(new):
        c = database.connect(path)
        try:
            barrier.wait(10)
            outcome.setdefault(('revoke', new.id), []).append(tokens.revoke(c, new.id, actor=CLI))
        finally:
            c.close()

    for start in range(0, 30, 6):
        batch = created[start:start + 6]
        threads = [threading.Thread(target=consumer, args=(n,)) for n in batch] + [threading.Thread(target=revoker, args=(n,)) for n in batch]
        barrier.reset()
        [t.start() for t in threads]
        [t.join(30) for t in threads]
    check = database.connect(path)
    for new in created:
        consumed, revoked = outcome[('consume', new.id)][0], outcome[('revoke', new.id)][0]
        row = dict(check.execute("SELECT used_at, revoked_at FROM auth_tokens WHERE id = ?", (new.id,)).fetchone())
        assert consumed != revoked, (new.id, consumed, revoked)                  # exactement un gagnant
        assert (row['used_at'] is not None) == consumed and (row['revoked_at'] is not None) == revoked
    check.close()


def test_sixteen_simultaneous_consumptions_across_processes_threads_yield_one_success(tmp_path):
    path = str(tmp_path / 'many.sqlite3')
    database.init_db(path)
    setup = database.connect(path)
    new = tokens.create(setup, 'bootstrap', actor=CLI, is_admin_grant=True)
    setup.close()
    results, barrier = [], threading.Barrier(16)

    def worker():
        c = database.connect(path)
        try:
            barrier.wait(10)
            results.append(tokens.consume(c, new.token))
        finally:
            c.close()

    threads = [threading.Thread(target=worker) for _ in range(16)]
    [t.start() for t in threads]
    [t.join(60) for t in threads]
    winners = [r for r in results if r is not None]
    assert len(results) == 16 and len(winners) == 1 and winners[0].is_admin_grant is True


def test_a_failed_account_creation_after_consumption_leaves_the_token_usable_and_untraced(conn):
    new = tokens.create(conn, 'invite', actor=CLI, email='a@example.org')
    with pytest.raises(sqlite3.IntegrityError), database.write_transaction(conn):
        assert tokens.consume(conn, new.token, used_by=1)
        conn.execute("INSERT INTO users (sub, username) VALUES ('s1', 'doublon')")           # sub déjà pris : échec
    assert tokens.inspect(conn, new.token) == 'choose_credentials'


def test_consuming_with_an_unknown_used_by_is_atomic(conn):
    new = tokens.create(conn, 'invite', actor=CLI)
    with pytest.raises(sqlite3.IntegrityError):
        tokens.consume(conn, new.token, used_by=999)
    assert tokens.inspect(conn, new.token) == 'choose_credentials' and not conn.in_transaction


def test_creating_with_unknown_foreign_keys_writes_neither_token_nor_audit(conn):
    for kwargs in (dict(created_by=999), dict(target_user_id=999)):
        with pytest.raises(sqlite3.IntegrityError):
            tokens.create(conn, 'invite', actor=CLI, **kwargs)
    assert conn.execute("SELECT COUNT(*) FROM auth_tokens").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 0


def test_deleting_the_target_account_takes_its_bootstrap_token_with_it(conn):
    new = tokens.create(conn, 'bootstrap', actor=CLI, is_admin_grant=True, target_user_id=1)
    conn.execute("DELETE FROM users WHERE id = 1")
    conn.commit()
    assert tokens.inspect(conn, new.token) is None and tokens.consume(conn, new.token) is None      # pas de jeton orphelin utilisable


def test_many_tokens_stay_unique_and_hashes_never_collide(conn):
    made = [tokens.create(conn, 'invite', actor=CLI) for _ in range(300)]
    assert len({m.token for m in made}) == 300 and len({r[0] for r in conn.execute("SELECT token_hash FROM auth_tokens")}) == 300
    assert all(len(m.token) == 43 and tokens.hash_token(m.token) for m in made)


# ============================================== T7a : flask user-invite ================================================

def test_the_link_is_printed_exactly_once_and_nowhere_else(app, caplog):
    with caplog.at_level(logging.DEBUG):
        result = invoke(app, '--admin', 'first@example.org')
    assert result.exit_code == 0
    token = next(line for line in result.output.splitlines() if '/auth/token#t=' in line).split('#t=')[1]
    assert (result.output + (result.stderr or '')).count(token) == 1
    assert token not in caplog.text
    c = database.connect(app.config['DB_PATH'])
    assert token not in everything(c)
    c.close()
    assert '?' not in next(line for line in result.output.splitlines() if '/auth/token#t=' in line).split('#')[0]


def test_the_link_is_built_from_public_url_never_from_the_request_or_the_host(app):
    result = invoke(app, '--admin', 'first@example.org')
    assert f'https://{HOST}/auth/token#t=' in result.output and 'localhost' not in result.output


@pytest.mark.parametrize('variant', [' taken@example.org', 'TAKEN@EXAMPLE.ORG ', 'taken@example.org\t', '\ttaken@example.org'])
def test_an_address_carried_by_an_account_is_refused_whatever_its_spelling(app, variant):
    add_account(app, 'pwd:1', email='taken@example.org')
    assert invoke(app, '--admin', variant).exit_code != 0 and stored(app) == []


def test_the_exact_spelling_variant_with_case_only_is_refused(app):
    add_account(app, 'pwd:1', email='taken@example.org')
    for variant in ('TAKEN@EXAMPLE.ORG', 'Taken@Example.Org'):
        assert invoke(app, '--admin', variant).exit_code != 0
    assert stored(app) == []


def test_an_address_carried_by_an_sso_or_inactive_account_is_refused_too(app):
    add_account(app, 'idp-1', method='oidc', email='sso@example.org')
    add_account(app, 'pwd:off', email='off@example.org', active=False)
    for address in ('sso@example.org', 'off@example.org'):
        assert invoke(app, '--admin', address).exit_code != 0
    assert stored(app) == []


@pytest.mark.parametrize('address', ['a@b', '@', 'a@', 'a b@c.d', 'a@b.c\nBcc: x@y.z', '"a"@b.c', 'x' * 300 + '@a.b', '<a@b.c>', 'a@b.c,d@e.f'])
def test_user_invite_rejects_degenerate_addresses(app, address):
    result = invoke(app, '--admin', address)
    if result.exit_code == 0:
        assert len(stored(app)) == 1 and '@' in stored(app)[0]['email'] and '\n' not in stored(app)[0]['email']
    else:
        assert stored(app) == []


def test_a_pending_invitation_for_the_same_address_blocks_another_until_it_is_gone(app):
    """EF-3.10 : pas deux invitations valables pour une adresse ; une expirée ou révoquée ne bloque plus."""
    first = invoke(app, '--admin', 'again@example.org')
    assert first.exit_code == 0
    for spelling in ('again@example.org', ' AGAIN@Example.ORG\t', 'ａｇａｉｎ@example.org'):
        refused = invoke(app, '--admin', spelling)
        assert refused.exit_code != 0 and 'en attente' in refused.output, spelling
    assert len(stored(app)) == 1
    c = database.connect(app.config['DB_PATH'])
    c.execute("UPDATE auth_tokens SET expires_at = '2000-01-01T00:00:00Z'")
    c.commit()
    c.close()
    assert invoke(app, '--admin', 'again@example.org').exit_code == 0
    rows = stored(app)
    assert len(rows) == 2 and rows[0]['token_hash'] != rows[1]['token_hash']
    c = database.connect(app.config['DB_PATH'])
    c.execute("UPDATE auth_tokens SET revoked_at = '2026-01-01T00:00:00Z' WHERE id = ?", (rows[1]['id'],))
    c.commit()
    c.close()
    assert invoke(app, '--admin', 'again@example.org').exit_code == 0               # une révoquée ne bloque pas non plus


def test_account_option_rules_in_every_combination(app):
    no_email = add_account(app, 'pwd:noemail')
    with_email = add_account(app, 'pwd:withmail', email='kept@example.org')
    assert invoke(app, '--admin', '--account', 'inconnu').exit_code != 0
    add_account(app, 'plain', email=None)
    assert invoke(app, '--admin', '--account', 'plain').exit_code == 0
    assert invoke(app, '--admin', '--account', 'KEPT@example.org').exit_code == 0       # par e-mail, sans tenir compte de la casse
    rows = {r['target_user_id']: r for r in stored(app)}
    assert rows[with_email]['email'] == 'kept@example.org' and rows[with_email]['kind'] == 'bootstrap' and rows[with_email]['is_admin_grant'] == 1
    assert no_email not in rows and len(rows) == 2


def test_account_option_prefers_the_accounts_address_over_the_argument(app):
    uid = add_account(app, 'resumer', email='stored@example.org')
    assert invoke(app, '--admin', '--account', 'resumer', 'other@example.org').exit_code == 0
    assert [r['email'] for r in stored(app) if r['target_user_id'] == uid] == ['stored@example.org']


def test_account_option_refuses_ambiguous_logins_and_local_style_names(app):
    add_account(app, 'a1', email=None)
    add_account(app, 'idp-a', method='oidc')
    c = database.connect(app.config['DB_PATH'])
    c.execute("UPDATE users SET username = 'dup' WHERE sub IN ('a1', 'idp-a')")
    c.commit()
    c.close()
    assert invoke(app, '--admin', '--account', 'dup').exit_code != 0                    # deux comptes : aucun jeton
    for name in ('local', 'LOCAL', 'system', 'cli'):
        assert invoke(app, '--admin', '--account', name).exit_code != 0
    assert stored(app) == []


def test_account_option_refuses_an_account_that_already_has_an_active_factor_even_an_inactive_one(app):
    uid = add_account(app, 'withfactor', active=False)
    c = database.connect(app.config['DB_PATH'])
    c.execute("CREATE TABLE IF NOT EXISTS webauthn_credentials (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL)")
    c.execute("INSERT INTO webauthn_credentials (user_id) VALUES (?)", (uid,))
    c.commit()
    c.close()
    assert invoke(app, '--admin', '--account', 'withfactor').exit_code != 0 and stored(app) == []


def test_user_invite_refuses_account_and_a_pending_unconfirmed_totp_is_not_a_factor(app):
    """CA-52 + T8-2 : un TOTP enrôlé mais non confirmé n'empêche pas de reprendre l'amorçage."""
    uid = add_account(app, 'pending')
    c = database.connect(app.config['DB_PATH'])
    c.execute("ALTER TABLE users ADD COLUMN totp_secret BLOB")
    c.execute("ALTER TABLE users ADD COLUMN totp_enabled_at TEXT")
    c.execute("UPDATE users SET totp_secret = x'01', totp_enabled_at = NULL WHERE id = ?", (uid,))
    c.commit()
    c.close()
    assert invoke(app, '--admin', '--account', 'pending').exit_code == 0


def test_user_invite_never_logs_or_prints_the_address_beyond_its_own_confirmation(app, caplog):
    with caplog.at_level(logging.DEBUG):
        invoke(app, '--admin', 'private.person@example.org')
    assert 'private.person' not in caplog.text


# ============================================== T5 : features.py (CA-22) ================================================

def _feature_app(tmp_path):
    app = create_app(TestingConfig, overrides={'DATA_DIR': str(tmp_path / 'data'), 'AUTH_MODE': 'local'})

    @app.route('/api/_beta')
    @feature_required(FEATURE)
    def beta_endpoint():
        return jsonify({'ok': True})

    @app.route('/_beta_html')
    def beta_html():
        return render_template_string('{% if feature_enabled("route_builder") %}VISIBLE-ENTRY{% else %}nothing{% endif %}')

    return app


@pytest.fixture
def fapp(tmp_path):
    return _feature_app(tmp_path)


def local_conn(fapp):
    return database.connect(fapp.config['DB_PATH'])


def local_user_id(fapp):
    fapp.test_client().get('/api/activities')
    c = local_conn(fapp)
    try:
        return c.execute("SELECT id FROM users WHERE sub = 'local'").fetchone()[0]
    finally:
        c.close()


def test_a_disabled_feature_is_a_404_identical_to_an_unknown_route_and_absent_from_the_html(fapp):
    client = fapp.test_client()
    unknown = client.get('/api/this-route-does-not-exist')
    disabled = client.get('/api/_beta')
    assert disabled.status_code == unknown.status_code == 404
    assert disabled.get_data() == unknown.get_data() and disabled.headers['Content-Type'] == unknown.headers['Content-Type']
    html = client.get('/_beta_html').get_data(as_text=True)
    assert 'VISIBLE-ENTRY' not in html


def test_ca22_off_users_without_the_user_users_with_the_user_and_all(fapp):
    uid = local_user_id(fapp)
    client, c = fapp.test_client(), local_conn(fapp)
    other = c.execute("INSERT INTO users (sub, username) VALUES ('s9', 'other')").lastrowid
    c.commit()

    def state():
        return client.get('/api/_beta').status_code, 'VISIBLE-ENTRY' in client.get('/_beta_html').get_data(as_text=True)

    assert state() == (404, False)                                                           # off
    features.set_state(c, FEATURE, 'users', actor=ADMIN, user_ids=[other])
    assert state() == (404, False)                                                           # users, sans lui
    features.set_state(c, FEATURE, 'users', actor=ADMIN, user_ids=[other, uid])
    assert state() == (200, True)                                                            # users, avec lui
    features.set_state(c, FEATURE, 'off', actor=ADMIN)
    assert state() == (404, False)                                                           # off : la liste est conservée mais fermée
    features.set_state(c, FEATURE, 'all', actor=ADMIN)
    assert state() == (200, True)                                                            # all
    features.set_state(c, FEATURE, 'users', actor=ADMIN)                                     # retour à users : la liste conservée
    assert state() == (200, True) and features.get_state(c, FEATURE)['users'] == sorted([other, uid])
    c.close()


def test_the_decision_is_taken_at_every_request_so_another_process_applies_at_once(fapp):
    client, c = fapp.test_client(), local_conn(fapp)
    assert client.get('/api/_beta').status_code == 404
    features.set_state(c, FEATURE, 'all', actor=ADMIN)                                       # « autre processus » : autre connexion
    assert client.get('/api/_beta').status_code == 200
    features.set_state(c, FEATURE, 'off', actor=ADMIN)
    assert client.get('/api/_beta').status_code == 404
    c.close()


def test_a_session_less_visitor_in_password_mode_gets_401_or_login_never_the_feature(tmp_path):
    app = create_app(Prod, overrides={'DATA_DIR': str(tmp_path / 'd'), **PASSWORD_MODE})

    @app.route('/api/_beta')
    @feature_required(FEATURE)
    def beta_endpoint():
        return jsonify({'ok': True})

    c = database.connect(app.config['DB_PATH'])
    features.set_state(c, FEATURE, 'all', actor=ADMIN)
    c.close()
    assert get(app.test_client(), '/api/_beta').status_code == 401                         # le garde passe avant : pas de fuite par le 404


def test_an_account_removed_from_the_list_or_deleted_loses_access(fapp):
    uid = local_user_id(fapp)
    c = local_conn(fapp)
    other = c.execute("INSERT INTO users (sub, username) VALUES ('s9', 'other')").lastrowid
    c.commit()
    features.set_state(c, FEATURE, 'users', actor=ADMIN, user_ids=[uid, other])
    features.set_state(c, FEATURE, 'users', actor=ADMIN, user_ids=[other])
    assert fapp.test_client().get('/api/_beta').status_code == 404
    c.execute("DELETE FROM users WHERE id = ?", (other,))
    c.commit()
    assert features.get_state(c, FEATURE)['users'] == []                                     # pas d'orphelin dans la liste
    c.close()


@pytest.mark.parametrize('ids', [[1, 1], [999], [0], [-1], ['1'], [1.0], [True], [None], [1, 'x'], list(range(1, 1002)), [2 ** 70]], ids=repr)
def test_a_bad_list_writes_nothing_not_even_the_state(conn, ids):
    features.set_state(conn, FEATURE, 'users', actor=ADMIN, user_ids=[1, 2])
    before, audits = features.get_state(conn, FEATURE), conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    with pytest.raises((FeatureError, OverflowError)):
        features.set_state(conn, FEATURE, 'all', actor=ADMIN, user_ids=ids)
    assert features.get_state(conn, FEATURE) == before
    assert conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == audits and not conn.in_transaction


def test_list_bounds_and_replacement_semantics(conn):
    conn.executemany("INSERT INTO users (sub, username) VALUES (?, ?)", [(f'b{i}', f'b{i}') for i in range(1100)])
    conn.commit()
    ids = [r[0] for r in conn.execute("SELECT id FROM users ORDER BY id LIMIT 1000")]
    assert len(features.set_state(conn, FEATURE, 'users', actor=ADMIN, user_ids=ids)['users']) == 1000              # borne incluse
    more = [r[0] for r in conn.execute("SELECT id FROM users ORDER BY id LIMIT 1001")]
    with pytest.raises(FeatureError):
        features.set_state(conn, FEATURE, 'users', actor=ADMIN, user_ids=more)
    assert features.set_state(conn, FEATURE, 'users', actor=ADMIN, user_ids=[3, 1, 2])['users'] == [1, 2, 3]       # triée
    assert features.set_state(conn, FEATURE, 'off', actor=ADMIN)['users'] == [1, 2, 3]                              # conservée sans `user_ids`
    assert features.set_state(conn, FEATURE, 'users', actor=ADMIN, user_ids=[])['users'] == []                      # vide : efface


@pytest.mark.parametrize('state', ['', 'ON', 'On', 'beta', None, 1, True, ['all'], 'all ', 'off\n'])
def test_unknown_states_and_features_are_refused(conn, state):
    with pytest.raises(FeatureError):
        features.set_state(conn, FEATURE, state, actor=ADMIN)
    for key in ('inconnue', '', None, 5, "route_builder'; DROP TABLE feature_flags;--"):
        with pytest.raises(FeatureError):
            features.set_state(conn, key, 'all', actor=ADMIN)
        assert features.is_enabled(conn, key, 1) is False
    assert conn.execute("SELECT COUNT(*) FROM feature_flags").fetchone()[0] == 0


def test_anonymous_and_unknown_accounts_never_get_a_feature(conn):
    features.set_state(conn, FEATURE, 'all', actor=ADMIN)
    assert features.is_enabled(conn, FEATURE, None) is False
    assert features.is_enabled(conn, 'nope', 1) is False


def test_every_write_is_audited_with_counts_only_and_by_the_right_actor(conn):
    features.set_state(conn, FEATURE, 'users', actor=Actor.user(3, 'carol'), user_ids=[1, 2], ip='203.0.113.9')
    features.set_state(conn, FEATURE, 'off', actor=CLI)
    entries = [dict(r) for r in conn.execute("SELECT * FROM audit_log WHERE action = 'feature.update' ORDER BY id")]
    assert [(e['actor_type'], e['actor_id']) for e in entries] == [('user', 3), ('cli', None)]
    first = json.loads(entries[0]['details'])
    assert first == {'old': 'off', 'new': 'users', 'listed_users': 2, 'list_changed': True, 'added': [1, 2], 'removed': []}
    assert entries[0]['ip'] == '203.0.113.9'
    assert json.loads(entries[1]['details'])['list_changed'] is False


def test_state_and_list_change_together_for_concurrent_readers(path):
    stop, seen, errors = threading.Event(), set(), []
    c = database.connect(path)
    c.executemany("INSERT INTO users (sub, username) VALUES (?, ?)", [('s2', 'b'), ('s3', 'c')])
    c.commit()

    def reader():
        r = database.connect(path)
        try:
            while not stop.is_set():
                state = features.get_state(r, FEATURE)
                seen.add((state['state'], tuple(state['users'])))
        except Exception as exc:
            errors.append(exc)
        finally:
            r.close()

    threads = [threading.Thread(target=reader) for _ in range(3)]
    [t.start() for t in threads]
    for i in range(60):
        features.set_state(c, FEATURE, 'users' if i % 2 else 'all', actor=ADMIN, user_ids=[1, 2] if i % 2 else [3])
    stop.set()
    [t.join(30) for t in threads]
    c.close()
    assert not errors, errors
    assert seen <= {('off', ()), ('users', (1, 2)), ('all', (3,))}                             # jamais un état mêlé à la liste de l'autre


def test_concurrent_writers_never_corrupt_the_flag_and_each_write_is_audited(path):
    errors = []

    def writer(n):
        c = database.connect(path)
        try:
            for i in range(10):
                features.set_state(c, FEATURE, ('off', 'all', 'users')[(n + i) % 3], actor=ADMIN)
        except Exception as exc:
            errors.append(exc)
        finally:
            c.close()

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(5)]
    [t.start() for t in threads]
    [t.join(60) for t in threads]
    c = database.connect(path)
    assert not errors, errors
    assert c.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'feature.update'").fetchone()[0] == 50
    assert c.execute("SELECT COUNT(*) FROM feature_flags").fetchone()[0] == 1
    c.close()


def test_a_flag_row_for_a_feature_removed_from_the_registry_is_inert(conn):
    conn.execute("INSERT INTO feature_flags (key, state, updated_at, updated_by) VALUES ('retiree', 'all', 'a', 'b')")
    conn.commit()
    assert features.is_enabled(conn, 'retiree', 1) is False
    with pytest.raises(FeatureError):
        features.get_state(conn, 'retiree')


def test_the_registry_holds_the_first_client_and_decorating_an_unknown_feature_fails_at_import():
    assert FEATURE in FEATURES
    with pytest.raises(FeatureError):
        feature_required('inconnue')


# ===================================== T8-1..4 : corrections ======================================================

def test_t8_1_every_admin_route_refuses_non_admins_and_factorless_password_admins(app):
    admin_rules = [rule for rule in app.url_map.iter_rules() if getattr(app.view_functions[rule.endpoint], 'admin_only', False)]
    assert len(admin_rules) >= 4
    plain = add_account(app, 'pwd:plain', admin=False, token='plain')
    factorless = add_account(app, 'pwd:nofactor', admin=True, token='nofactor')
    pending = add_account(app, 'pwd:pending', admin=True, token='pending')
    c = database.connect(app.config['DB_PATH'])
    for column in ('totp_secret BLOB', 'totp_enabled_at TEXT'):
        c.execute(f"ALTER TABLE users ADD COLUMN {column}")
    c.execute("UPDATE users SET totp_secret = x'01', totp_enabled_at = NULL WHERE id = ?", (pending,))        # enrôlé, non confirmé
    c.commit()
    c.close()
    for uid, token in ((plain, 'plain'), (factorless, 'nofactor'), (pending, 'pending')):
        client = app.test_client()
        sign_in(client, uid, token)
        for rule in admin_rules:
            url = rule.rule
            import re
            url = re.sub(r'<[^>]+>', '1', url)
            for method in rule.methods & {'GET', 'POST', 'PATCH', 'DELETE'}:
                response = client.open(url, method=method, base_url=f'https://{HOST}', json={},
                                       headers={'Origin': f'https://{HOST}'})
                assert response.status_code == 403, (uid, rule.rule, method, response.status_code)


def test_t8_1_routes_under_admin_prefixes_are_all_admin_only(app):
    for rule in app.url_map.iter_rules():
        if rule.rule.startswith(('/admin', '/api/admin')):
            assert getattr(app.view_functions[rule.endpoint], 'admin_only', False), rule.rule
    for rule in app.url_map.iter_rules():
        if rule.rule in ('/api/users', '/api/stats') or rule.rule.startswith('/api/users/'):
            assert getattr(app.view_functions[rule.endpoint], 'admin_only', False), rule.rule


def test_t8_1_an_admin_with_an_active_webauthn_key_passes_and_a_sso_admin_in_oidc_mode_passes(app, tmp_path):
    uid = add_account(app, 'pwd:ok', admin=True, token='ok')
    c = database.connect(app.config['DB_PATH'])
    c.execute("CREATE TABLE IF NOT EXISTS webauthn_credentials (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL)")
    c.execute("INSERT INTO webauthn_credentials (user_id) VALUES (?)", (uid,))
    c.commit()
    c.close()
    client = app.test_client()
    sign_in(client, uid, 'ok')
    assert get(client, '/api/users').status_code == 200
    oidc = create_app(Prod, overrides={'DATA_DIR': str(tmp_path / 'o'), 'OIDC_ISSUER': 'https://idp.test', 'OIDC_CLIENT_ID': 'k',
                                       'OIDC_CLIENT_SECRET': 's', 'TRUSTED_HOSTS': [HOST]})
    sso = add_account(oidc, 'idp-admin', method='oidc', admin=True, token='sso')
    sso_client = oidc.test_client()
    sign_in(sso_client, sso, 'sso')
    assert get(sso_client, '/api/users').status_code == 200


def test_t8_1_removing_the_factor_closes_admin_routes_at_the_next_request(app):
    uid = add_account(app, 'pwd:lose', admin=True, token='lose')
    c = database.connect(app.config['DB_PATH'])
    c.execute("CREATE TABLE IF NOT EXISTS webauthn_credentials (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL)")
    c.execute("INSERT INTO webauthn_credentials (user_id) VALUES (?)", (uid,))
    c.commit()
    client = app.test_client()
    sign_in(client, uid, 'lose')
    assert get(client, '/api/users').status_code == 200
    factors.clear_factors(c, uid)
    c.commit()
    c.close()
    assert get(client, '/api/users').status_code == 403


@pytest.mark.parametrize('sub', ['pwd:', 'pwd:abc', 'pwd:3f2b8c1e-0000-4000-8000-000000000000', 'pwd:local', 'local', '', None, 5, ['pwd:x'], {'sub': 'x'}])
def test_t8_3_an_idp_cannot_claim_a_password_account_nor_the_local_one(app, sub):
    uid = add_account(app, 'pwd:3f2b8c1e-0000-4000-8000-000000000000', admin=True, email='victim@example.org')
    c = database.connect(app.config['DB_PATH'])
    before = dict(c.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone())
    with pytest.raises(ValueError):
        users.upsert_from_claims(c, {'sub': sub, 'email': 'victim@example.org', 'email_verified': True, 'preferred_username': 'victim'})
    assert dict(c.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()) == before
    assert c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
    c.close()


@pytest.mark.parametrize('sub', ['PWD:abc', ' pwd:abc', 'pwd :abc', 'xpwd:abc', 'idp|pwd:abc', 'pwd​:abc'])
def test_t8_3_lookalike_subjects_stay_separate_sso_accounts_and_never_touch_a_password_account(app, sub):
    uid = add_account(app, 'pwd:abc', admin=True)
    c = database.connect(app.config['DB_PATH'])
    row = users.upsert_from_claims(c, {'sub': sub, 'preferred_username': 'x'})
    assert row['id'] != uid and row['auth_method'] == 'oidc' and row['is_admin'] == 0
    assert c.execute("SELECT is_admin, auth_method, session_token FROM users WHERE id = ?", (uid,)).fetchone()[:] == (1, 'password', 'tok')
    c.close()


def test_t8_4_the_inbox_never_creates_the_local_account_without_a_validated_mode(app, tmp_path):
    inbox = pathlib.Path(app.config['DATA_DIR']) / 'inbox'
    (inbox / 'local').mkdir(parents=True)
    (inbox / 'x').mkdir()
    for path in (inbox / 'local' / 'a.gpx', inbox / 'x' / 'b.gpx', inbox / 'c.gpx'):
        path.write_bytes(b'<gpx/>')
        os.utime(path, (time.time() - 60, time.time() - 60))
    c = database.connect(app.config['DB_PATH'])
    for cfg in ({'DATA_DIR': app.config['DATA_DIR']}, {'DATA_DIR': app.config['DATA_DIR'], 'OIDC_ISSUER': ''},
                {'DATA_DIR': app.config['DATA_DIR'], 'AUTH_MODE': ''}, {**dict(app.config), 'AUTH_MODE': 'password'},
                {**dict(app.config), 'AUTH_MODE': 'oidc', 'INBOX_DEFAULT_USER': 'local'}):
        try:
            inbox_watcher.scan_once(c, cfg)
        except RuntimeError:
            pass                                                                      # « absent => erreur, jamais de repli ouvert »
    assert c.execute("SELECT COUNT(*) FROM users WHERE sub = 'local'").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 0
    c.close()


def test_t8_4_the_inbox_still_serves_the_local_mode_when_it_is_explicit(tmp_path):
    local = create_app(TestingConfig, overrides={'DATA_DIR': str(tmp_path / 'l'), 'AUTH_MODE': 'local'})
    c = database.connect(local.config['DB_PATH'])
    owner = inbox_watcher._resolve_owner(c, dict(local.config), None)
    assert owner is not None and owner['sub'] == 'local'
    with pytest.raises(RuntimeError):
        inbox_watcher._resolve_owner(c, {'DATA_DIR': local.config['DATA_DIR']}, None)
    c.close()


def test_consumption_at_the_exact_expiry_second_is_refused_and_one_second_before_is_accepted(conn):
    early = tokens.create(conn, 'invite', actor=CLI, now=NOW, ttl=timedelta(hours=2))
    on_time = tokens.create(conn, 'invite', actor=CLI, now=NOW, ttl=timedelta(hours=2))
    assert tokens.consume(conn, early.token, now=NOW + timedelta(hours=2)) is None                      # échéance : refusé
    assert tokens.consume(conn, on_time.token, now=NOW + timedelta(hours=2) - timedelta(seconds=1)) is not None
