"""
2e lot, groupe 5 (rôle test, écrit depuis la demande de review) : (1) limiteur des changements d'adresse (POST /api/me/email), (2) un seul lien
`reset` valable à la fois, (3) nit E-3-n1 : un code de secours n'est ni consommé ni notifié quand le 409 « dernier facteur d'un admin » refuse
l'opération. Aucun marqueur xfail : un test rouge est un défaut à rapporter.
"""

import json
import re
import threading

import pytest

from app import db as database
from app.auth import second_factor, tokens
from app.audit import Actor
from tests.pw_harness import GOOD, add, call, connect, form_post, make_app, new_ip, new_secret, refresh, session_client
from tests.test_lot2_g1_edge import configure_smtp, mails, smtp as smtp, text_of, with_recovery, admin_client, recovery_notices  # noqa: F401
from tests.test_lot2_g3a_edge import advance, resend

NEW_ADDRESS = 'nouvelle{n}@example.org'


@pytest.fixture
def app(tmp_path, smtp):
    application = make_app(tmp_path, OUTBOUND_ALLOWED_HOSTS=f'smtp=127.0.0.1:{smtp.port}')
    configure_smtp(application, smtp.port)
    return application


@pytest.fixture
def conn(app):
    c = connect(app)
    yield c
    c.close()


def change(client, n, *, ip=None):
    return call(client, 'post', '/api/me/email', {'password': GOOD, 'email': NEW_ADDRESS.format(n=n)}, ip=ip)


# ===== (1) limiteur des changements d'adresse ===================================================================================

def test_three_changes_in_the_hour_pass_and_the_fourth_is_a_429_with_retry_after(app, conn):
    user = add(app, conn, 'alice', email='alice@example.org')
    client = session_client(app, user)
    for n in range(3):
        assert change(client, n).status_code == 200, n
    fourth = change(client, 3)
    assert fourth.status_code == 429 and int(fourth.headers['Retry-After']) > 0
    assert fourth.get_json()['success'] is False


def test_a_refused_change_modifies_nothing_sends_nothing_and_leaves_no_trace(app, conn, smtp):
    user = add(app, conn, 'alice', email='alice@example.org')
    client = session_client(app, user)
    for n in range(3):
        change(client, n)
    before = refresh(conn, 'alice')
    mails(app, smtp)
    smtp.messages.clear()
    tokens_before = conn.execute("SELECT COUNT(*) FROM auth_tokens").fetchone()[0]
    audit_before = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    refused = change(client, 99)
    assert refused.status_code == 429
    after = refresh(conn, 'alice')
    assert after['pending_email'] == before['pending_email'] == NEW_ADDRESS.format(n=2) and after['email'] == 'alice@example.org'
    assert mails(app, smtp) == []                                                      # ni notification à l'ancienne adresse, ni lien à la nouvelle
    assert conn.execute("SELECT COUNT(*) FROM auth_tokens").fetchone()[0] == tokens_before
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'user.email_change_requested'").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] in (audit_before, audit_before + 1)          # au plus une ligne de refus
    assert after['session_token'] == before['session_token']


def test_the_window_slides_and_the_budget_comes_back_after_an_hour(app, conn):
    user = add(app, conn, 'alice', email='alice@example.org')
    client = session_client(app, user)
    for n in range(3):
        change(client, n)
    assert change(client, 3).status_code == 429
    advance(app, 3601)
    assert change(client, 4).status_code == 200


def test_the_budget_is_per_account(app, conn):
    one, two = add(app, conn, 'alice', email='alice@example.org'), add(app, conn, 'bob', email='bob@example.org')
    for n in range(3):
        change(session_client(app, one), n)
    assert change(session_client(app, one), 3).status_code == 429
    assert change(session_client(app, two), 0).status_code == 200


def test_the_change_budget_and_the_resend_budget_are_distinct(app, conn):
    user = add(app, conn, 'alice', email='alice@example.org')
    client = session_client(app, user)
    for n in range(3):
        assert change(client, n).status_code == 200
    assert change(client, 3).status_code == 429
    assert [resend(client).status_code for _ in range(4)] == [200, 200, 200, 429]               # 3 renvois restent possibles, puis le 429 propre
    advance(app, 3601)
    assert resend(client).status_code == 200 and change(client, 5).status_code == 200


def test_using_up_the_resends_does_not_consume_the_change_budget(app, conn):
    user = add(app, conn, 'alice', email='alice@example.org')
    client = session_client(app, user)
    assert change(client, 0).status_code == 200
    assert [resend(client).status_code for _ in range(4)] == [200, 200, 200, 429]
    assert change(client, 1).status_code == 200 and change(client, 2).status_code == 200


def test_a_per_ip_ceiling_stops_many_accounts_behind_one_address(app, conn):
    clients = [session_client(app, add(app, conn, f'user{i}', email=f'user{i}@example.org')) for i in range(40)]
    ip = '198.51.100.77'
    codes = [change(c, i, ip=ip).status_code for i, c in enumerate(clients)]
    assert codes[0] == 200 and 429 in codes, codes
    first_block = codes.index(429)
    assert 3 <= first_block <= 30
    other = change(clients[-1], 0, ip=new_ip())
    assert other.status_code == 200                                                            # une autre adresse n'est pas touchée
    assert all(refresh(conn, f'user{i}')['pending_email'] is None for i in range(first_block, 40) if i != 39)


def test_the_limiter_is_consulted_before_any_password_hashing(app, conn, monkeypatch):
    from app.auth import passwords
    user = add(app, conn, 'alice', email='alice@example.org')
    client = session_client(app, user)
    for n in range(3):
        change(client, n)
    calls = []
    original = passwords.verify_password
    monkeypatch.setattr(passwords, 'verify_password', lambda *a, **k: (calls.append(1), original(*a, **k))[1])
    assert change(client, 3).status_code == 429 and calls == []                                # r9 : pas de hachage pour un compte déjà limité


def test_a_wrong_password_or_a_missing_session_never_consumes_the_budget_of_the_account(app, conn):
    user = add(app, conn, 'alice', email='alice@example.org')
    client = session_client(app, user)
    for _ in range(5):
        assert call(app.test_client(), 'post', '/api/me/email', {'password': GOOD, 'email': 'x@example.org'}).status_code == 401
    assert [change(client, n).status_code for n in range(4)] == [200, 200, 200, 429]


@pytest.mark.parametrize('payload', [{}, {'password': GOOD}, {'password': GOOD, 'email': ''}, {'password': GOOD, 'email': 'pas une adresse'},
                                     {'password': GOOD, 'email': ['a@b.fr']}, {'password': GOOD, 'email': 'a' * 400 + '@example.org'}])
def test_invalid_requests_are_client_errors_and_do_not_use_the_three_slots(app, conn, payload):
    user = add(app, conn, 'alice', email='alice@example.org')
    client = session_client(app, user)
    for _ in range(6):
        assert 400 <= call(client, 'post', '/api/me/email', payload).status_code < 500
    assert [change(client, n).status_code for n in range(3)] == [200, 200, 200]


def test_simultaneous_changes_never_exceed_three(app, conn, monkeypatch):
    from app.auth import passwords
    monkeypatch.setattr(passwords, 'verify_password', lambda stored, password: (True, None))     # sans la barrière de hachage (2 places) : c'est le limiteur qu'on éprouve
    user = add(app, conn, 'alice', email='alice@example.org')
    results = []
    barrier = threading.Barrier(8, timeout=10)

    def job(n):
        client = session_client(app, user)
        barrier.wait()
        results.append(change(client, n).status_code)
    threads = [threading.Thread(target=job, args=(n,)) for n in range(8)]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    assert sorted(results).count(200) <= 3 and 429 in results, results
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'user.email_change_requested'").fetchone()[0] <= 3


def test_the_validated_address_is_never_replaced_by_a_change_request(app, conn):
    user = add(app, conn, 'alice', email='alice@example.org')
    client = session_client(app, user)
    for n in range(5):
        change(client, n)
    assert refresh(conn, 'alice')['email'] == 'alice@example.org'


# ===== (2) un seul lien reset valable à la fois ==============================================================================

def valid_resets(conn, user_id):
    return conn.execute("SELECT id FROM auth_tokens WHERE kind = 'reset' AND target_user_id = ? AND used_at IS NULL AND revoked_at IS NULL "
                        "AND expires_at > strftime('%Y-%m-%dT%H:%M:%SZ', 'now')", (user_id,)).fetchall()


def admin_reset(admin, user_id):
    r = call(admin, 'post', f'/api/admin/users/{user_id}/reset-link')
    assert r.status_code == 200, r.get_data()
    return r.get_json()['link'].split('#t=')[1]


def test_issuing_a_second_admin_link_revokes_the_first(app, conn):
    admin = admin_client(app, conn)
    target = add(app, conn, 'alice', email='alice@example.org')
    first = admin_reset(admin, target['id'])
    second = admin_reset(admin, target['id'])
    assert tokens.kind_of(conn, first) is None and tokens.kind_of(conn, second) == 'reset'
    assert len(valid_resets(conn, target['id'])) == 1
    assert form_post(app, '/auth/token/consume', {'token': first, 'password': 'nouveau-mot-de-passe-lune-77'}).status_code == 404
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'token.revoke'").fetchone()[0] >= 1       # la révocation est tracée


def test_a_link_from_the_forgotten_password_flow_and_an_admin_link_exclude_each_other(app, conn, smtp):
    admin = admin_client(app, conn)
    target = add(app, conn, 'alice', email='alice@example.org')
    from_admin = admin_reset(admin, target['id'])
    assert form_post(app, '/auth/password/forgot', {'identifier': 'alice'}).status_code == 200
    mails(app, smtp)
    forgot_links = [m.group(1) for m in (re.search(r'#t=(\S+)', text_of(message)) for message in map(
        __import__('email').message_from_string, smtp.messages)) if m]
    assert forgot_links, "aucun lien de mot de passe oublié reçu"
    assert tokens.kind_of(conn, from_admin) is None                                          # le lien de l'admin est révoqué par l'émission « oublié »
    assert len(valid_resets(conn, target['id'])) == 1
    again = admin_reset(admin, target['id'])
    assert tokens.kind_of(conn, forgot_links[-1]) is None and tokens.kind_of(conn, again) == 'reset'
    assert len(valid_resets(conn, target['id'])) == 1


def test_using_one_link_revokes_every_other_pending_link_of_the_account(app, conn):
    """Défense en profondeur : même deux jetons créés sans passer par la route d'émission (course, ancien code) : l'usage en laisse un seul."""
    target = add(app, conn, 'alice', email='alice@example.org')
    first = tokens.create(conn, 'reset', actor=Actor.cli(), target_user_id=target['id'], email='alice@example.org')
    second = tokens.create(conn, 'reset', actor=Actor.cli(), target_user_id=target['id'], email='alice@example.org')
    assert form_post(app, '/auth/token/consume', {'token': first.token, 'password': 'nouveau-mot-de-passe-lune-77'}).status_code == 200
    assert tokens.kind_of(conn, second.token) is None and len(valid_resets(conn, target['id'])) == 0
    assert form_post(app, '/auth/token/consume', {'token': second.token, 'password': 'autre-mot-de-passe-lune-88'}).status_code == 404


def test_the_links_of_other_accounts_are_untouched(app, conn):
    admin = admin_client(app, conn)
    alice, bob = add(app, conn, 'alice', email='alice@example.org'), add(app, conn, 'bob', email='bob@example.org')
    alice_link, bob_link = admin_reset(admin, alice['id']), admin_reset(admin, bob['id'])
    admin_reset(admin, alice['id'])
    assert tokens.kind_of(conn, bob_link) == 'reset' and tokens.kind_of(conn, alice_link) is None
    assert form_post(app, '/auth/token/consume', {'token': bob_link, 'password': 'nouveau-mot-de-passe-lune-77'}).status_code == 200
    assert len(valid_resets(conn, alice['id'])) == 1


def test_other_kinds_of_links_of_the_same_account_are_not_revoked_by_a_reset(app, conn):
    admin = admin_client(app, conn)
    target = add(app, conn, 'alice', pending_email='alice@example.org')
    verify = tokens.create(conn, 'verify_email', actor=Actor.cli(), target_user_id=target['id'], email='alice@example.org')
    admin_reset(admin, target['id'])
    assert tokens.kind_of(conn, verify.token) == 'verify_email'


def test_simultaneous_emissions_leave_exactly_one_valid_link(app, conn):
    admin_row = add(app, conn, 'chef', admin=True, secret=new_secret())
    target = add(app, conn, 'alice', email='alice@example.org')
    barrier = threading.Barrier(6, timeout=10)
    links = []

    def job():
        client = session_client(app, admin_row)
        barrier.wait()
        r = call(client, 'post', f"/api/admin/users/{target['id']}/reset-link")
        if r.status_code == 200:
            links.append(r.get_json()['link'].split('#t=')[1])
    threads = [threading.Thread(target=job) for _ in range(6)]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    c = database.connect(app.config['DB_PATH'])
    try:
        assert len(links) >= 2 and len(valid_resets(c, target['id'])) == 1
        survivors = [link for link in links if tokens.kind_of(c, link)]
        assert len(survivors) == 1
    finally:
        c.close()


def test_a_consumed_link_cannot_be_replaced_back_by_an_older_one(app, conn):
    admin = admin_client(app, conn)
    target = add(app, conn, 'alice', email='alice@example.org')
    old = admin_reset(admin, target['id'])
    new = admin_reset(admin, target['id'])
    assert form_post(app, '/auth/token/consume', {'token': new, 'password': 'nouveau-mot-de-passe-lune-77'}).status_code == 200
    assert form_post(app, '/auth/token/consume', {'token': old, 'password': 'autre-mot-de-passe-lune-88'}).status_code == 404
    assert tokens.kind_of(conn, old) is None and tokens.kind_of(conn, new) is None


def test_the_mail_sent_with_a_replaced_link_stays_harmless(app, conn, smtp):
    admin = admin_client(app, conn)
    target = add(app, conn, 'alice', email='alice@example.org')
    admin_reset(admin, target['id'])
    admin_reset(admin, target['id'])
    received = mails(app, smtp)
    old_token = re.search(r'#t=(\S+)', text_of(received[0])).group(1)
    assert len(received) == 2 and tokens.kind_of(conn, old_token) is None


# ===== (3) E-3-n1 : le 409 « dernier facteur d'un admin » ne brûle ni ne notifie le code de secours ==============================

def admin_with_one_factor(app, conn, *, kind):
    """Admin dont l'unique facteur est un TOTP (kind='totp') ou une clé (kind='key') ; codes de secours générés."""
    from tests.test_webauthn_edge import add_key
    from tests.test_webauthn_edge import RP
    from tests.pw_harness import ORIGIN
    from tests.webauthn_authenticator import VirtualAuthenticator
    secret = new_secret() if kind == 'totp' else None
    row = add(app, conn, 'chef', admin=True, email='chef@example.org', secret=secret)
    if kind == 'key':
        add_key(conn, row, VirtualAuthenticator(RP, ORIGIN), name='cle')
    codes = second_factor.replace_recovery_codes(conn, row['id'])
    conn.commit()
    return refresh(conn, 'chef'), codes


def test_removing_the_last_totp_of_an_admin_with_a_recovery_code_is_a_409_that_burns_nothing(app, conn, smtp):
    row, codes = admin_with_one_factor(app, conn, kind='totp')
    client = session_client(app, row)
    remaining = second_factor.remaining_recovery(conn, row['id'])
    r = call(client, 'delete', '/api/me/mfa/totp', {'password': GOOD, 'code': codes[0]})
    assert r.status_code == 409
    assert second_factor.remaining_recovery(conn, row['id']) == remaining                  # code de secours NON consommé
    assert recovery_notices(smtp, app) == []                                                # ni notifié
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'user.recovery_used'").fetchone()[0] == 0
    assert refresh(conn, 'chef')['totp_enabled_at'] and refresh(conn, 'chef')['is_admin'] == 1


def test_the_same_recovery_code_still_works_afterwards_for_a_legitimate_action(app, conn, smtp):
    row, codes = admin_with_one_factor(app, conn, kind='totp')
    client = session_client(app, row)
    assert call(client, 'delete', '/api/me/mfa/totp', {'password': GOOD, 'code': codes[0]}).status_code == 409
    ok = call(client, 'post', '/api/me/password', {'current_password': GOOD, 'new_password': 'nouveau-mot-de-passe-lune-77', 'code': codes[0]})
    assert ok.status_code == 200
    assert second_factor.remaining_recovery(conn, row['id']) == 9
    assert len(recovery_notices(smtp, app)) == 1                                            # la notification n'a lieu que pour l'usage réel


def test_removing_the_last_key_of_an_admin_with_a_recovery_code_is_a_409_that_burns_nothing(app, conn, smtp):
    row, codes = admin_with_one_factor(app, conn, kind='key')
    client = session_client(app, row)
    key_id = conn.execute("SELECT id FROM webauthn_credentials").fetchone()[0]
    r = call(client, 'delete', f'/api/me/mfa/webauthn/{key_id}', {'password': GOOD, 'code': codes[0]})
    assert r.status_code == 409
    assert second_factor.remaining_recovery(conn, row['id']) == 10 and recovery_notices(smtp, app) == []
    assert conn.execute("SELECT COUNT(*) FROM webauthn_credentials").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'user.recovery_used'").fetchone()[0] == 0


def test_a_wrong_code_on_that_path_stays_a_403_and_counts_for_the_limiter(app, conn):
    row, _codes = admin_with_one_factor(app, conn, kind='totp')
    client = session_client(app, row)
    for _ in range(5):
        assert call(client, 'delete', '/api/me/mfa/totp', {'password': GOOD, 'code': 'ZZZZZZZZZZZZZZZZ'}).status_code == 403
    assert call(client, 'delete', '/api/me/mfa/totp', {'password': GOOD, 'code': 'ZZZZZZZZZZZZZZZZ'}).status_code == 429


def test_a_successful_removal_by_a_non_admin_still_consumes_and_notifies_exactly_once(app, conn, smtp):
    row, secret, codes = with_recovery(app, conn)                                          # non admin, TOTP + codes, adresse validée
    r = call(session_client(app, row), 'delete', '/api/me/mfa/totp', {'password': GOOD, 'code': codes[0]})
    assert r.status_code == 200
    assert len(recovery_notices(smtp, app)) == 1
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'user.recovery_used'").fetchone()[0] == 1


def test_a_non_admin_with_two_factors_removes_one_with_a_recovery_code_and_it_is_consumed_once(app, conn, smtp):
    row, secret, codes = with_recovery(app, conn, keys=1)
    r = call(session_client(app, row), 'delete', '/api/me/mfa/totp', {'password': GOOD, 'code': codes[0]})
    assert r.status_code == 200
    assert second_factor.remaining_recovery(conn, row['id']) == 9
    assert second_factor.consume_recovery(conn, row['id'], codes[0]) is False              # usage unique


def test_the_409_refusal_carries_no_secret_and_a_stable_message(app, conn):
    row, codes = admin_with_one_factor(app, conn, kind='totp')
    r = call(session_client(app, row), 'delete', '/api/me/mfa/totp', {'password': GOOD, 'code': codes[0]})
    body = json.dumps(r.get_json())
    assert r.status_code == 409 and codes[0] not in body and 'argon2' not in body and 'administrateur' in body.lower()
