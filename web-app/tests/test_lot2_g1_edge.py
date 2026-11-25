"""
2e lot, groupe 1 (rôle test, écrit depuis la spec, pas depuis le code) : E-1 (EF-1.8, CA-56), E-3 a (EF-2.5, CA-57), E-3 b (P-T11-7, CA-58)
et restart.sh. Aucun marqueur xfail : un test rouge est un défaut à rapporter.
"""

import email
import json
import logging
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import db as database
from app.audit import Actor
from app.auth import second_factor, tokens
from tests.fake_smtp import FakeSMTP
from tests.pw_harness import (GOOD, ORIGIN, OTHER, add, call, cookie_payload, connect, form_post, login, make_app, mfa, refresh, session_client)
from tests.webauthn_authenticator import VirtualAuthenticator
from tests.test_webauthn_edge import RP, add_key, assertion, options as login_options, pending as pending_login

CLI = Actor.cli()
NEW = 'nouveau-mot-de-passe-lune-77'
SPEC_PROOF_OPTIONS = '/api/me/mfa/webauthn/proof/options'
CODE_PATH = Path(__file__).resolve().parent.parent
STAMP = '%Y-%m-%dT%H:%M:%SZ'


@pytest.fixture
def smtp():
    server = FakeSMTP()
    yield server
    server.stop()


def configure_smtp(app, port):
    c = database.connect(app.config['DB_PATH'])
    app.config['SETTINGS_STORE'].set_many(c, {'smtp.host': '127.0.0.1', 'smtp.port': port, 'smtp.security': 'none',
                                              'smtp.sender': 'noreply@example.org', 'smtp.timeout_s': 2}, actor=CLI)
    c.close()


@pytest.fixture
def app(tmp_path, smtp):
    return make_app(tmp_path, OUTBOUND_ALLOWED_HOSTS=f'smtp=127.0.0.1:{smtp.port}')


@pytest.fixture
def mail_app(app, smtp):
    configure_smtp(app, smtp.port)
    return app


@pytest.fixture
def conn(app):
    c = connect(app)
    yield c
    c.close()


def mails(app, smtp):
    """Vide la file d'envoi et rend les messages reçus par le serveur SMTP factice (analysés)."""
    app.extensions['mailer'].queue.drain()
    return [email.message_from_string(m) for m in smtp.messages]


def text_of(message):
    payload = message.get_payload(decode=True)
    return (payload or b'').decode('utf-8', 'replace')


def admin_client(app, conn, name='chef'):
    return session_client(app, add(app, conn, name, admin=True, secret=second_factor.new_secret()))        # un admin garde un second facteur


# ===== E-1 : réinitialisation par un admin (EF-1.8, CA-56) ===========================================================================

def reset_link(client, user_id):
    r = call(client, 'post', f'/api/admin/users/{user_id}/reset-link')
    assert r.status_code == 200, r.get_data()
    return r.get_json()


def token_of(link):
    return link.split('#t=', 1)[1]


def test_the_reset_token_lasts_exactly_24_hours_accepted_at_23h59_and_refused_at_24h01(mail_app, conn):
    admin = admin_client(mail_app, conn)
    target = add(mail_app, conn, 'alice', email='alice@example.org')
    link = reset_link(admin, target['id'])['link']
    row = conn.execute("SELECT created_at, expires_at FROM auth_tokens").fetchone()
    created, expires = (datetime.strptime(row[k], STAMP).replace(tzinfo=timezone.utc) for k in ('created_at', 'expires_at'))
    assert expires - created == timedelta(hours=24)
    token = token_of(link)
    assert tokens.kind_of(conn, token, now=expires - timedelta(minutes=1)) == 'reset'
    assert tokens.kind_of(conn, token, now=expires + timedelta(minutes=1)) is None
    assert tokens.consume(conn, token, kind='reset', now=expires + timedelta(minutes=1)) is None          # refusé : non consommé
    assert tokens.consume(conn, token, kind='reset', now=expires - timedelta(minutes=1)) is not None      # accepté une fois
    assert tokens.consume(conn, token, kind='reset', now=expires - timedelta(minutes=1)) is None          # usage unique


def test_the_link_is_mailed_once_to_the_validated_address_and_shown_to_the_admin(mail_app, smtp, conn):
    admin = admin_client(mail_app, conn)
    target = add(mail_app, conn, 'alice', email='alice@example.org')
    answer = reset_link(admin, target['id'])
    assert answer['sent_by_mail'] is True and answer['link'].startswith(f'{ORIGIN}/auth/token#t=') and 'expires_at' in answer
    received = mails(mail_app, smtp)
    assert len(received) == 1 and received[0]['To'] == 'alice@example.org'
    body = text_of(received[0])
    assert answer['link'] in body                                                  # même lien que celui montré à l'admin
    assert '?' not in answer['link'].split('#t=')[0]                               # le jeton est dans le fragment, jamais dans la requête
    assert conn.execute("SELECT sent_by_mail FROM auth_tokens").fetchone()[0] == 1


def reset_mail_body(mail_app, smtp, conn):
    admin = admin_client(mail_app, conn)
    target = add(mail_app, conn, 'alice', email='alice@example.org')
    reset_link(admin, target['id'])
    return text_of(mails(mail_app, smtp)[0])


def test_the_mail_names_the_account_identifier(mail_app, smtp, conn):
    assert 'alice' in reset_mail_body(mail_app, smtp, conn)                        # EF-1.8, CA-56 : « nomme l'identifiant »


def test_the_mail_states_the_duration_of_the_link(mail_app, smtp, conn):
    assert re.search(r'24\s*(h|heures)', reset_mail_body(mail_app, smtp, conn))    # EF-1.8 : « donne ... sa durée »


def test_the_mail_tells_to_warn_the_administrator_when_not_requested(mail_app, smtp, conn):
    assert 'préviens ton administrateur' in reset_mail_body(mail_app, smtp, conn).lower()      # EF-1.8


def test_the_mail_is_never_sent_inside_the_request_thread(mail_app, smtp, conn):
    admin = admin_client(mail_app, conn)
    target = add(mail_app, conn, 'alice', email='alice@example.org')
    reset_link(admin, target['id'])
    assert smtp.messages == []                                                     # mis en file seulement ; l'envoi se fait au drain
    assert len(mails(mail_app, smtp)) == 1


@pytest.mark.parametrize('kind', ['no_smtp', 'pending_address_only', 'no_address'])
def test_no_mail_without_smtp_or_without_a_validated_address_but_the_link_is_still_shown(app, smtp, conn, kind):
    if kind != 'no_smtp':
        configure_smtp(app, smtp.port)
    admin = admin_client(app, conn)
    if kind == 'no_smtp':
        target = add(app, conn, 'alice', email='alice@example.org')
    elif kind == 'pending_address_only':
        target = add(app, conn, 'alice', pending_email='alice@example.org')
    else:
        target = add(app, conn, 'alice')
    answer = reset_link(admin, target['id'])
    assert answer['sent_by_mail'] is False and answer['link'].startswith(ORIGIN)
    assert mails(app, smtp) == []
    assert conn.execute("SELECT sent_by_mail FROM auth_tokens").fetchone()[0] == 0
    assert tokens.kind_of(conn, token_of(answer['link'])) == 'reset'


def test_the_mail_goes_to_the_validated_address_never_to_the_pending_one(mail_app, smtp, conn):
    admin = admin_client(mail_app, conn)
    target = add(mail_app, conn, 'alice', email='alice@example.org', pending_email='nouvelle@example.org')
    reset_link(admin, target['id'])
    received = mails(mail_app, smtp)
    assert [m['To'] for m in received] == ['alice@example.org']


def test_an_unreachable_smtp_does_not_break_the_answer_and_the_link_stays_usable(tmp_path, conn_factory=None):
    from tests.fake_smtp import closed_port
    port = closed_port()
    app = make_app(tmp_path / 'dead', OUTBOUND_ALLOWED_HOSTS=f'smtp=127.0.0.1:{port}')
    configure_smtp(app, port)                                                      # port fermé : l'envoi échoue, jamais la requête
    conn = connect(app)
    admin = admin_client(app, conn)
    target = add(app, conn, 'alice', email='alice@example.org')
    answer = reset_link(admin, target['id'])
    assert answer['link'] and isinstance(answer['sent_by_mail'], bool)
    app.extensions['mailer'].queue.drain()
    assert tokens.kind_of(conn, token_of(answer['link'])) == 'reset'
    conn.close()


@pytest.mark.parametrize('setup,status', [("UPDATE users SET is_active = 0 WHERE username = 'alice'", 409),
                                          ("UPDATE users SET auth_method = 'oidc', password_hash = NULL WHERE username = 'alice'", (404, 409))])
def test_no_link_and_no_mail_for_an_inactive_or_sso_account(mail_app, smtp, conn, setup, status):
    admin = admin_client(mail_app, conn)
    target = add(mail_app, conn, 'alice', email='alice@example.org')
    conn.execute(setup)
    conn.commit()
    r = call(admin, 'post', f"/api/admin/users/{target['id']}/reset-link")
    assert r.status_code in (status if isinstance(status, tuple) else (status,)) and 'link' not in r.get_json()
    assert mails(mail_app, smtp) == [] and conn.execute("SELECT COUNT(*) FROM auth_tokens").fetchone()[0] == 0


def test_a_non_admin_and_an_anonymous_caller_get_no_link_and_no_mail(mail_app, smtp, conn):
    target = add(mail_app, conn, 'alice', email='alice@example.org')
    other = add(mail_app, conn, 'bob')
    assert call(session_client(mail_app, other), 'post', f"/api/admin/users/{target['id']}/reset-link").status_code == 403
    assert call(mail_app.test_client(), 'post', f"/api/admin/users/{target['id']}/reset-link").status_code in (401, 403)
    assert call(session_client(mail_app, other), 'post', '/api/admin/users/999/reset-link').status_code in (403, 404)
    assert mails(mail_app, smtp) == [] and conn.execute("SELECT COUNT(*) FROM auth_tokens").fetchone()[0] == 0


def test_the_consumption_revokes_the_sessions_opens_none_keeps_the_second_factor_and_is_single_use(mail_app, smtp, conn):
    admin = admin_client(mail_app, conn)
    secret = second_factor.new_secret()
    target = add(mail_app, conn, 'alice', email='alice@example.org', secret=secret)
    stale = session_client(mail_app, target)
    link = reset_link(admin, target['id'])['link']
    token = token_of(link)
    before = refresh(conn, 'alice')
    client = mail_app.test_client()
    r = form_post(mail_app, '/auth/token/consume', {'token': token, 'password': NEW}, client=client)
    assert r.status_code == 200, r.get_data()
    after = refresh(conn, 'alice')
    assert after['password_hash'] != before['password_hash'] and after['session_token'] != before['session_token']
    assert call(stale, 'get', '/api/me').status_code == 401                                    # CA-14 : sessions antérieures refusées
    assert 'user_id' not in cookie_payload(mail_app, client)                                   # CA-45 : aucune session ouverte
    assert after['totp_secret'] == before['totp_secret'] and after['totp_enabled_at']          # le second facteur est conservé
    assert form_post(mail_app, '/auth/token/consume', {'token': token, 'password': OTHER}).status_code == 404
    assert login(mail_app, 'alice', NEW).get_json()['step'] == 'mfa'                           # le second facteur reste exigé
    assert login(mail_app, 'alice', GOOD).status_code == 401


def test_neither_the_token_nor_the_password_reaches_the_audit_or_the_logs(mail_app, smtp, conn, caplog):
    caplog.set_level(logging.DEBUG)
    admin = admin_client(mail_app, conn)
    target = add(mail_app, conn, 'alice', email='alice@example.org')
    link = reset_link(admin, target['id'])['link']
    token = token_of(link)
    form_post(mail_app, '/auth/token/consume', {'token': token, 'password': NEW})
    mails(mail_app, smtp)
    audit_dump = json.dumps([dict(r) for r in conn.execute("SELECT * FROM audit_log")])
    assert 'token.create' in audit_dump                                                         # l'émission est tracée
    for secret in (token, NEW, GOOD):
        assert secret not in audit_dump and secret not in caplog.text
    assert conn.execute("SELECT token_hash FROM auth_tokens").fetchone()[0] != token           # stocké haché


def test_two_links_for_the_same_account_stay_independent_and_each_mails_once(mail_app, smtp, conn):
    admin = admin_client(mail_app, conn)
    target = add(mail_app, conn, 'alice', email='alice@example.org')
    first = reset_link(admin, target['id'])['link']
    second = reset_link(admin, target['id'])['link']
    assert first != second and len(mails(mail_app, smtp)) == 2


# ===== E-3 a : code de secours utilisé depuis le profil ⇒ notification (EF-2.5, CA-57) ==============================================

def with_recovery(app, conn, name='alice', *, admin=False, email='alice@example.org', totp=True, keys=0):
    secret = second_factor.new_secret() if totp else None
    row = add(app, conn, name, admin=admin, email=email, secret=secret)
    for i in range(keys):
        add_key(conn, row, VirtualAuthenticator(RP, ORIGIN), name=f'cle{i}')
    codes = second_factor.replace_recovery_codes(conn, row['id'])
    conn.commit()
    return refresh(conn, name), secret, codes


def recovery_notices(smtp, app):
    return [m for m in mails(app, smtp) if 'secours' in (m['Subject'] or '').lower() or 'secours' in text_of(m).lower()]


PROFILE_PATHS = {
    'password': lambda c, code: call(c, 'post', '/api/me/password', {'current_password': GOOD, 'new_password': NEW, 'code': code}),
    'email': lambda c, code: call(c, 'post', '/api/me/email', {'password': GOOD, 'email': 'nouvelle@example.org', 'code': code}),
    'totp_remove': lambda c, code: call(c, 'delete', '/api/me/mfa/totp', {'password': GOOD, 'code': code}),
}


@pytest.mark.parametrize('path', list(PROFILE_PATHS))
def test_a_recovery_code_used_at_the_profile_sends_one_notice_and_a_wrong_one_none(mail_app, smtp, conn, path):
    row, secret, codes = with_recovery(mail_app, conn)
    client = session_client(mail_app, row)
    bad = PROFILE_PATHS[path](client, 'ZZZZZZZZZZZZZZZZ')
    assert bad.status_code in (400, 403) and recovery_notices(smtp, mail_app) == []                  # refusé ⇒ aucune notification
    smtp.messages.clear()
    ok = PROFILE_PATHS[path](client, codes[0])
    assert ok.status_code == 200, (path, ok.get_data())
    notices = recovery_notices(smtp, mail_app)
    assert [m['To'] for m in notices] == ['alice@example.org'] and codes[0] not in ''.join(text_of(m) for m in notices)


def test_a_recovery_code_used_to_remove_a_key_sends_one_notice(mail_app, smtp, conn):
    row, _, codes = with_recovery(mail_app, conn, totp=False, keys=2)
    key_id = conn.execute("SELECT id FROM webauthn_credentials ORDER BY id").fetchone()[0]
    r = call(session_client(mail_app, row), 'delete', f'/api/me/mfa/webauthn/{key_id}', {'password': GOOD, 'code': codes[0]})
    assert r.status_code == 200
    assert [m['To'] for m in recovery_notices(smtp, mail_app)] == ['alice@example.org']


def test_a_recovery_code_used_at_login_sends_one_notice(mail_app, smtp, conn):
    row, _, codes = with_recovery(mail_app, conn)
    client = mail_app.test_client()
    assert login(mail_app, 'alice', client=client).get_json()['step'] == 'mfa'
    assert mfa(mail_app, client, codes[0]).status_code == 200
    assert [m['To'] for m in recovery_notices(smtp, mail_app)] == ['alice@example.org']


def test_a_totp_code_sends_no_recovery_notice_anywhere(mail_app, smtp, conn):
    from tests.pw_harness import code_at
    row, secret, _ = with_recovery(mail_app, conn)
    r = call(session_client(mail_app, row), 'post', '/api/me/password', {'current_password': GOOD, 'new_password': NEW, 'code': code_at(secret)})
    assert r.status_code == 200 and recovery_notices(smtp, mail_app) == []


@pytest.mark.parametrize('path', ['password', 'email'])
def test_an_account_without_a_validated_address_gets_no_recovery_mail_but_the_action_works(mail_app, smtp, conn, path):
    row, _, codes = with_recovery(mail_app, conn, email=None)
    r = PROFILE_PATHS[path](session_client(mail_app, row), codes[0])
    assert r.status_code == 200
    assert [m for m in mails(mail_app, smtp) if 'secours' in text_of(m).lower()] == []
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'user.recovery_used'").fetchone()[0] == 1       # audit gardé, mail absent


def test_a_recovery_code_stays_single_use_from_the_profile(mail_app, smtp, conn):
    row, _, codes = with_recovery(mail_app, conn)
    client = session_client(mail_app, row)
    assert PROFILE_PATHS['password'](client, codes[0]).status_code == 200
    row = refresh(conn, 'alice')
    again = call(session_client(mail_app, row), 'post', '/api/me/password', {'current_password': NEW, 'new_password': OTHER, 'code': codes[0]})
    assert again.status_code in (400, 403) and len(recovery_notices(smtp, mail_app)) == 1


# ===== E-3 b : assertion WebAuthn au profil (P-T11-7, CA-58) =========================================================================

def proof_options(client, password=GOOD, **kw):
    return call(client, 'post', SPEC_PROOF_OPTIONS, {'password': password}, **kw)


def proof_of(client, auth, **forged):
    r = proof_options(client)
    assert r.status_code == 200, r.get_data()
    return assertion(auth, r.get_data(as_text=True), **forged)


def keys_only_account(app, conn, name='bob', n=2, *, admin=False):
    row = add(app, conn, name, admin=admin, email=f'{name}@example.org')
    auths = [add_key(conn, row, VirtualAuthenticator(RP, ORIGIN), name=f'cle{i}') for i in range(n)]
    codes = second_factor.replace_recovery_codes(conn, row['id'])
    conn.commit()
    return refresh(conn, name), auths, codes


def key_ids(conn, user_id):
    return [r[0] for r in conn.execute("SELECT id FROM webauthn_credentials WHERE user_id = ? ORDER BY id", (user_id,))]


def snapshot(conn, user_id):
    return (key_ids(conn, user_id), second_factor.remaining_recovery(conn, user_id),
            conn.execute("SELECT password_hash, session_token FROM users WHERE id = ?", (user_id,)).fetchone()[:])


def test_the_proof_options_route_is_the_one_written_in_the_spec(app, conn):
    row, auths, _ = keys_only_account(app, conn)
    r = call(session_client(app, row), 'post', SPEC_PROOF_OPTIONS, {'password': GOOD})
    assert r.status_code == 200, r.status_code
    opts = r.get_json()
    assert len(opts['challenge']) >= 22                                           # ≥ 128 bits en base64url


def test_the_proof_options_need_the_password_a_key_and_a_session(app, conn):
    row, auths, _ = keys_only_account(app, conn)
    client = session_client(app, row)
    assert proof_options(client, OTHER).status_code == 403                        # mot de passe faux : aucun défi
    assert proof_options(client, '').status_code in (400, 403)
    assert proof_options(app.test_client()).status_code in (401, 403)
    nokey = add(app, conn, 'nokey')
    assert proof_options(session_client(app, nokey)).status_code in (400, 409)    # aucune clé : pas de défi d'assertion


def test_the_challenge_is_fresh_unpredictable_and_replaced_by_a_new_request(app, conn):
    row, auths, _ = keys_only_account(app, conn)
    client = session_client(app, row)
    first, second = (proof_options(client).get_json()['challenge'] for _ in range(2))
    assert first != second
    stale = assertion(auths[0], json.dumps({'challenge': first}))
    key_id = key_ids(conn, row['id'])[1]
    assert call(client, 'delete', f'/api/me/mfa/webauthn/{key_id}', {'password': GOOD, 'assertion': stale}).status_code != 200


def test_an_assertion_removes_a_key_and_regenerates_the_codes_without_burning_a_recovery_code(app, conn):
    row, auths, codes = keys_only_account(app, conn)
    ids = key_ids(conn, row['id'])
    client = session_client(app, row)
    remaining = second_factor.remaining_recovery(conn, row['id'])
    r = call(client, 'delete', f'/api/me/mfa/webauthn/{ids[1]}', {'password': GOOD, 'assertion': proof_of(client, auths[0])})
    assert r.status_code == 200, r.get_data()
    assert key_ids(conn, row['id']) == [ids[0]] and second_factor.remaining_recovery(conn, row['id']) == remaining     # aucun code consommé
    client = session_client(app, refresh(conn, 'bob'))
    new = call(client, 'post', '/api/me/mfa/recovery-codes', {'password': GOOD, 'assertion': proof_of(client, auths[0])})
    assert new.status_code == 200 and len(new.get_json()['recovery_codes']) == 10
    assert not set(new.get_json()['recovery_codes']) & set(codes)
    assert second_factor.remaining_recovery(conn, row['id']) == 10
    assert second_factor.consume_recovery(conn, row['id'], codes[0]) is False                       # anciens codes refusés


def test_the_challenge_is_consumed_by_the_action_that_presents_it(app, conn):
    row, auths, _ = keys_only_account(app, conn, n=3)
    ids = key_ids(conn, row['id'])
    client = session_client(app, row)
    proof = proof_of(client, auths[0])
    assert call(client, 'delete', f'/api/me/mfa/webauthn/{ids[2]}', {'password': GOOD, 'assertion': proof}).status_code == 200
    client = session_client(app, refresh(conn, 'bob'))
    assert call(client, 'delete', f'/api/me/mfa/webauthn/{ids[1]}', {'password': GOOD, 'assertion': proof}).status_code != 200   # rejouée
    assert key_ids(conn, row['id']) == ids[:2]


def test_an_assertion_may_come_from_the_key_being_removed(app, conn):
    row, auths, _ = keys_only_account(app, conn)
    ids = key_ids(conn, row['id'])
    client = session_client(app, row)
    r = call(client, 'delete', f'/api/me/mfa/webauthn/{ids[1]}', {'password': GOOD, 'assertion': proof_of(client, auths[1])})
    assert r.status_code == 200 and key_ids(conn, row['id']) == [ids[0]]


def test_the_last_key_of_an_admin_is_kept_even_with_a_valid_assertion(app, conn):
    row, auths, _ = keys_only_account(app, conn, 'chef', n=1, admin=True)
    ids = key_ids(conn, row['id'])
    client = session_client(app, row)
    r = call(client, 'delete', f'/api/me/mfa/webauthn/{ids[0]}', {'password': GOOD, 'assertion': proof_of(client, auths[0])})
    assert r.status_code == 409 and key_ids(conn, row['id']) == ids and refresh(conn, 'chef')['is_admin'] == 1


def test_the_last_key_of_a_non_admin_can_go_and_the_recovery_codes_die_with_the_last_factor(app, conn):
    row, auths, _ = keys_only_account(app, conn, n=1)
    ids = key_ids(conn, row['id'])
    client = session_client(app, row)
    r = call(client, 'delete', f'/api/me/mfa/webauthn/{ids[0]}', {'password': GOOD, 'assertion': proof_of(client, auths[0])})
    assert r.status_code == 200 and key_ids(conn, row['id']) == [] and second_factor.remaining_recovery(conn, row['id']) == 0


def forged_cases(app, conn):
    """(étiquette, fabrique de preuve) : chaque preuve est invalide pour une raison différente."""
    row, auths, _ = keys_only_account(app, conn)
    other, other_auths, _ = keys_only_account(app, conn, 'carl', n=1)
    client = session_client(app, row)
    ids = key_ids(conn, row['id'])

    def good_options(c=client):
        return proof_options(c).get_data(as_text=True)
    return row, client, ids, {
        'mauvaise origine': lambda: assertion(auths[0], good_options(), origin='https://evil.example'),
        'mauvais RP ID': lambda: assertion(auths[0], good_options(), rp_id='evil.example'),
        'mauvais type': lambda: assertion(auths[0], good_options(), ctype='webauthn.create'),
        'mauvais défi': lambda: assertion(auths[0], good_options(), challenge='AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'),
        'signature d une autre clé': lambda: assertion(auths[0], good_options(), key=other_auths[0].key),
        'clé d un autre compte': lambda: assertion(other_auths[0], good_options()),
        'user presence absente': lambda: assertion(auths[0], good_options(), flags=0x00),
        'compteur en arrière': lambda: assertion(auths[0], good_options(), counter=0),
        'défi de connexion': lambda: assertion(auths[0], login_options(app, pending_login(app, 'bob')).get_data(as_text=True)),
        'défi d enregistrement du profil': lambda: assertion(
            auths[0], call(client, 'post', '/api/me/mfa/webauthn/options', {'password': GOOD}).get_data(as_text=True)),
    }


@pytest.mark.parametrize('label', ['mauvaise origine', 'mauvais RP ID', 'mauvais type', 'mauvais défi', 'signature d une autre clé',
                                   'clé d un autre compte', 'user presence absente', 'compteur en arrière', 'défi de connexion',
                                   'défi d enregistrement du profil'])
def test_a_forged_or_misplaced_assertion_changes_nothing(app, conn, label):
    row, client, ids, cases = forged_cases(app, conn)
    if label == 'compteur en arrière':
        conn.execute("UPDATE webauthn_credentials SET sign_count = 50 WHERE user_id = ?", (row['id'],))
        conn.commit()
    before = snapshot(conn, row['id'])
    proof = cases[label]()
    r = call(client, 'delete', f'/api/me/mfa/webauthn/{ids[1]}', {'password': GOOD, 'assertion': proof})
    assert 400 <= r.status_code < 500 and r.status_code != 200, (label, r.status_code)
    assert snapshot(conn, row['id']) == before
    again = call(client, 'post', '/api/me/mfa/recovery-codes', {'password': GOOD, 'assertion': proof})
    assert again.status_code != 200 and snapshot(conn, row['id']) == before


def test_a_refused_assertion_is_a_403(app, conn):
    row, client, ids, cases = forged_cases(app, conn)
    r = call(client, 'delete', f'/api/me/mfa/webauthn/{ids[1]}', {'password': GOOD, 'assertion': cases['mauvais RP ID']()})
    assert r.status_code == 403, r.status_code                                     # décision de coord (25/11) : 403 gardé, spec alignée


def test_a_challenge_of_another_session_or_another_account_is_refused(app, conn):
    row, auths, _ = keys_only_account(app, conn)
    other, other_auths, _ = keys_only_account(app, conn, 'carl')
    ids = key_ids(conn, row['id'])
    client = session_client(app, row)
    proof_before_rotation = proof_of(client, auths[0])
    conn.execute("UPDATE users SET session_token = 'rotated' WHERE id = ?", (row['id'],))                    # autre session (jeton renouvelé)
    conn.commit()
    client = session_client(app, refresh(conn, 'bob'))
    assert call(client, 'delete', f'/api/me/mfa/webauthn/{ids[1]}', {'password': GOOD, 'assertion': proof_before_rotation}).status_code != 200
    carl = session_client(app, other)                                                                         # défi demandé par un autre compte
    carl_proof = proof_of(carl, other_auths[0])
    assert call(client, 'delete', f'/api/me/mfa/webauthn/{ids[1]}', {'password': GOOD, 'assertion': carl_proof}).status_code != 200
    assert key_ids(conn, row['id']) == ids


def test_an_expired_challenge_is_refused(app, conn):
    row, auths, _ = keys_only_account(app, conn)
    ids = key_ids(conn, row['id'])
    client = session_client(app, row)
    proof = proof_of(client, auths[0])
    pending_store = app.extensions['pending']
    real = pending_store._clock
    pending_store._clock = lambda: real() + 125                                                               # > 2 min
    r = call(client, 'delete', f'/api/me/mfa/webauthn/{ids[1]}', {'password': GOOD, 'assertion': proof})
    assert r.status_code != 200 and key_ids(conn, row['id']) == ids


def test_a_wrong_password_is_refused_before_the_assertion_is_verified(app, conn, monkeypatch):
    from app.auth import webauthn_factor
    row, auths, _ = keys_only_account(app, conn)
    ids = key_ids(conn, row['id'])
    client = session_client(app, row)
    proof = proof_of(client, auths[0])
    calls = []
    original = webauthn_factor.verify_authentication
    monkeypatch.setattr(webauthn_factor, 'verify_authentication', lambda *a, **k: (calls.append(1), original(*a, **k))[1])
    for path, method, payload in ((f'/api/me/mfa/webauthn/{ids[1]}', 'delete', {'password': OTHER, 'assertion': proof}),
                                  ('/api/me/mfa/recovery-codes', 'post', {'password': OTHER, 'assertion': proof})):
        assert call(client, method, path, payload).status_code == 403
    assert calls == [] and key_ids(conn, row['id']) == ids
    ok = call(client, 'delete', f'/api/me/mfa/webauthn/{ids[1]}', {'password': GOOD, 'assertion': proof})
    assert ok.status_code == 200 and calls == [1]                                                             # le défi n'avait pas été consommé


def test_five_failed_assertions_block_the_sixth_even_a_valid_one(app, conn):
    row, auths, _ = keys_only_account(app, conn)
    ids = key_ids(conn, row['id'])
    client = session_client(app, row)
    for _ in range(5):
        bad = proof_of(client, auths[0], rp_id='evil.example')
        assert call(client, 'delete', f'/api/me/mfa/webauthn/{ids[1]}', {'password': GOOD, 'assertion': bad}).status_code in (400, 401, 403)
    good = proof_of(client, auths[0]) if proof_options(client).status_code == 200 else None
    r = call(client, 'delete', f'/api/me/mfa/webauthn/{ids[1]}', {'password': GOOD, 'assertion': good})
    assert r.status_code == 429 and key_ids(conn, row['id']) == ids


def test_the_assertion_failures_share_the_budget_with_the_other_second_factor_paths(app, conn):
    row, auths, codes = keys_only_account(app, conn)
    ids = key_ids(conn, row['id'])
    client = session_client(app, row)
    for _ in range(3):
        call(client, 'delete', f'/api/me/mfa/webauthn/{ids[1]}', {'password': GOOD, 'code': 'ZZZZZZZZZZZZZZZZ'})
    for _ in range(2):
        bad = proof_of(client, auths[0], rp_id='evil.example')
        call(client, 'delete', f'/api/me/mfa/webauthn/{ids[1]}', {'password': GOOD, 'assertion': bad})
    r = call(client, 'delete', f'/api/me/mfa/webauthn/{ids[1]}', {'password': GOOD, 'code': codes[0]})
    assert r.status_code == 429


def test_a_successful_assertion_gives_the_attempt_back(app, conn):
    row, auths, _ = keys_only_account(app, conn, n=3)
    key_ids(conn, row['id'])
    for i in range(7):                                                                                          # 7 succès > 5 échecs permis
        client = session_client(app, refresh(conn, 'bob'))
        r = call(client, 'post', '/api/me/mfa/recovery-codes', {'password': GOOD, 'assertion': proof_of(client, auths[0])})
        assert r.status_code == 200, (i, r.status_code)


def test_an_assertion_proves_nothing_else_password_address_and_totp_removal_keep_their_own_proof(mail_app, smtp, conn):
    secret = second_factor.new_secret()
    row = add(mail_app, conn, 'bob', email='bob@example.org', secret=secret)
    auth = add_key(conn, row, VirtualAuthenticator(RP, ORIGIN), name='cle')
    second_factor.replace_recovery_codes(conn, row['id'])
    conn.commit()
    client = session_client(mail_app, refresh(conn, 'bob'))
    before = refresh(conn, 'bob')
    r = call(client, 'post', '/api/me/password', {'current_password': GOOD, 'new_password': NEW, 'assertion': proof_of(client, auth)})
    assert r.status_code in (400, 401, 403) and refresh(conn, 'bob')['password_hash'] == before['password_hash']
    r = call(client, 'post', '/api/me/email', {'password': GOOD, 'email': 'nouvelle@example.org', 'assertion': proof_of(client, auth)})
    assert r.status_code in (400, 401, 403) and refresh(conn, 'bob')['pending_email'] is None
    r = call(client, 'delete', '/api/me/mfa/totp', {'password': GOOD, 'assertion': proof_of(client, auth)})
    assert r.status_code in (400, 401, 403) and refresh(conn, 'bob')['totp_enabled_at']
    assert mails(mail_app, smtp) == [] or all('nouvelle@example.org' not in m['To'] for m in mails(mail_app, smtp))


def test_a_code_without_an_assertion_still_works_for_the_same_actions(app, conn):
    from tests.pw_harness import code_at
    secret = second_factor.new_secret()
    row = add(app, conn, 'bob', secret=secret)
    add_key(conn, row, VirtualAuthenticator(RP, ORIGIN), name='cle')
    second_factor.replace_recovery_codes(conn, row['id'])
    conn.commit()
    client = session_client(app, refresh(conn, 'bob'))
    new = call(client, 'post', '/api/me/mfa/recovery-codes', {'password': GOOD, 'code': code_at(secret)})
    assert new.status_code == 200 and len(new.get_json()['recovery_codes']) == 10


@pytest.mark.parametrize('assertion_value', [None, '', 'x', 0, [], {}, {'id': 1}, {'response': 'x'}, {'id': 'a' * 5000}, 10 ** 30, True])
def test_hostile_assertion_values_are_client_errors_never_a_server_error_and_change_nothing(app, conn, assertion_value):
    row, auths, _ = keys_only_account(app, conn)
    ids = key_ids(conn, row['id'])
    client = session_client(app, row)
    proof_options(client)
    r = call(client, 'delete', f'/api/me/mfa/webauthn/{ids[1]}', {'password': GOOD, 'assertion': assertion_value})
    assert 400 <= r.status_code < 500 and key_ids(conn, row['id']) == ids
    r = call(client, 'post', '/api/me/mfa/recovery-codes', {'password': GOOD, 'assertion': assertion_value})
    assert 400 <= r.status_code < 500


def test_an_assertion_is_not_accepted_without_the_session_or_for_another_account_key_id(app, conn):
    row, auths, _ = keys_only_account(app, conn)
    other, other_auths, _ = keys_only_account(app, conn, 'carl')
    client = session_client(app, row)
    foreign = key_ids(conn, other['id'])[0]
    r = call(client, 'delete', f'/api/me/mfa/webauthn/{foreign}', {'password': GOOD, 'assertion': proof_of(client, auths[0])})
    assert r.status_code in (404, 403) and key_ids(conn, other['id']) == [foreign, key_ids(conn, other['id'])[1]]
    assert call(app.test_client(), 'delete', f'/api/me/mfa/webauthn/{foreign}', {'password': GOOD, 'assertion': {}}).status_code in (401, 403)


def test_the_removal_by_assertion_is_audited_notified_and_rotates_the_other_sessions(mail_app, smtp, conn):
    row, auths, _ = keys_only_account(mail_app, conn)
    ids = key_ids(conn, row['id'])
    client = session_client(mail_app, row)
    other_session = session_client(mail_app, row)
    r = call(client, 'delete', f'/api/me/mfa/webauthn/{ids[1]}', {'password': GOOD, 'assertion': proof_of(client, auths[0])})
    assert r.status_code == 200
    assert call(other_session, 'get', '/api/me').status_code == 401                                          # CA-14 : autres sessions fermées
    assert call(client, 'get', '/api/me').status_code == 200                                                 # la session courante est réémise
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'user.factor_removed'").fetchone()[0] == 1
    assert len(mails(mail_app, smtp)) >= 1                                                                   # notification de retrait de facteur
    dump = json.dumps([dict(x) for x in conn.execute("SELECT * FROM audit_log")])
    assert 'assertion' not in dump.lower() or 'signature' not in dump.lower()


# ===== restart.sh : relit le fichier .env (recréation des conteneurs) ===========================================================

def restart_script():
    """restart.sh est à la racine du dépôt, hors web-app : la CI ne monte que web-app, le test se saute alors (jamais un échec)."""
    script = CODE_PATH.parent / 'restart.sh'
    if not script.exists():
        pytest.skip("restart.sh absent (hors web-app)")
    return script


def test_restart_sh_recreates_the_containers_so_that_env_is_reread():
    script = restart_script()
    source = script.read_text(encoding='utf-8')
    assert subprocess.run(['bash', '-n', str(script)], capture_output=True).returncode == 0                  # syntaxe valide
    code = '\n'.join(line for line in source.splitlines() if not line.lstrip().startswith('#'))
    assert re.search(r'run_compose\s+up\s+-d\s+--force-recreate', code)
    assert not re.search(r'run_compose\s+restart\b', code) and not re.search(r'\b(docker[- ]compose)\s+restart\b', code)
    assert 'set -e' in code
    assert script.stat().st_mode & 0o111                                                                      # exécutable


def test_restart_sh_has_no_other_destructive_command():
    code = restart_script().read_text(encoding='utf-8')
    assert not re.search(r'\b(down\b|rm\s+-|--volumes|-v\b|prune)', re.sub(r'#.*', '', code))                # jamais de volume supprimé
