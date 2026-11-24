"""E-mail : messages, transport sans repli en clair, file d'envoi, notifications CLI synchrones (T9, EF-4, CA-35, CA-37, CA-49)."""

import email as email_lib
import email.policy
import json
import re
import threading
import time
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app import create_app, db as database, mailer as mailer_module
from app.audit import Actor
from app.auth import invitations, tokens
from app.config import TestingConfig
from app.mailer import MailError, MailQueue, build_message, compose, validate_address
from app.secrets_store import SecretsStore
from app.settings import SettingsError
from tests.fake_smtp import FakeSMTP, closed_port

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
def smtp():
    server = FakeSMTP()
    yield server
    server.stop()


def make_app(tmp_path, port, **extra):
    return create_app(Prod, overrides={
        'DATA_DIR': str(tmp_path / 'data'), 'AUTH_MODE': 'password', 'TRUSTED_HOSTS': [HOST], 'SECRETS_KEY': KEY,
        'PUBLIC_URL': f'https://{HOST}', 'OUTBOUND_ALLOWED_HOSTS': f'smtp=127.0.0.1:{port}', **extra})


def configure(app, port, security='none', timeout=2, **more):
    conn = database.connect(app.config['DB_PATH'])
    app.config['SETTINGS_STORE'].set_many(conn, {'smtp.host': '127.0.0.1', 'smtp.port': port, 'smtp.security': security,
                                                 'smtp.sender': 'noreply@example.org', 'smtp.timeout_s': timeout, **more},
                                          actor=CLI)
    conn.close()


@pytest.fixture
def app(tmp_path, smtp):
    application = make_app(tmp_path, smtp.port)
    configure(application, smtp.port)
    return application


@pytest.fixture
def conn(app):
    c = database.connect(app.config['DB_PATH'])
    yield c
    c.close()


def add_user(conn, name, *, admin=False, email=None, credits=0, pending=None, method='password'):
    conn.execute("INSERT INTO users (sub, username, email, pending_email, is_admin, invite_credits, auth_method, session_token) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?, 'tok')", (f'pwd:{name}', name, email, pending, int(admin), credits, method))
    conn.commit()
    return conn.execute("SELECT * FROM users WHERE username = ?", (name,)).fetchone()


def parsed(raw):
    """Message reçu par le serveur factice, décodé (quoted-printable, en-têtes encodés)."""
    return email_lib.message_from_string(raw, policy=email.policy.default)


def text_of(raw):
    return parsed(raw).get_content()


def audit_actions(conn):
    return [r[0] for r in conn.execute("SELECT action FROM audit_log ORDER BY id")]


# --- Messages ---------------------------------------------------------------------------------------------------------

def test_messages_are_plain_french_text_built_by_emailmessage():
    subject, body = compose('invitation', link='https://x/auth/token#t=abc', inviter='alice', note='Bienvenue !')
    message = build_message('Noreply@Example.org', ' Bob@Example.ORG ', subject, body)
    assert message['To'] == 'bob@example.org' and message['From'] == 'noreply@example.org'
    assert message['Subject'] == '[KmlViewer] Invitation' and message.get_content_type() == 'text/plain'
    text = message.get_content()
    assert 'Message de alice : Bienvenue !' in text and 'https://x/auth/token#t=abc' in text and 'usage unique' in text


def test_no_user_data_in_headers_beyond_the_recipient_address():
    for kind in ('verification', 'reset', 'bootstrap', 'password_changed', 'factor_reset', 'secrets_reset'):
        subject, body = compose(kind, link='https://x/y', username="alice\r\nBcc: evil@example.org")
        message = build_message('a@example.org', 'b@example.org', subject, body)
        assert set(message.keys()) == {'Content-Type', 'MIME-Version', 'Content-Transfer-Encoding', 'From', 'To', 'Subject'}
        assert 'alice' not in message['Subject'] and 'Bcc' not in str(message.keys())


def test_the_verification_message_names_the_account_and_says_to_ignore_it_otherwise():
    _, body = compose('verification', link='https://x/y', username='alice')
    assert '« alice »' in body and "Si tu n'as pas créé ce compte, ignore ce message" in body


@pytest.mark.parametrize('address', ['a@b.c\r\nBcc: x@y.z', 'a@b.c\nX: y', 'a b@c.d', '<a@b.c>', 'a@b.c,d@e.f', 'a@b.c;d@e.f',
                                     '"a"@b.c', 'pas-un-mail', '', None, 5, 'x' * 300 + '@a.b', '@b.c', 'a@', 'a..b@c.d'])
def test_invalid_addresses_are_refused(address):
    with pytest.raises(ValueError):
        validate_address(address)
    with pytest.raises(ValueError):
        build_message('a@example.org', address, 'x', 'y')


def test_every_notification_kind_composes_and_unknown_kinds_do_not():
    for kind in mailer_module.NOTIFICATIONS:
        subject, body = compose(kind)
        assert subject and body.startswith('Bonjour') and 'contacte un administrateur' in body
    with pytest.raises(ValueError):
        compose('inconnu')


# --- Réglages SMTP (EF-4.1, CA-34, CA-35, CA-36) ---------------------------------------------------------------------------

def test_smtp_settings_are_bounded_and_have_safe_defaults(app):
    store = app.config['SETTINGS_STORE']
    defaults = {k: store.setting(k).default for k in ('smtp.host', 'smtp.port', 'smtp.security', 'smtp.username', 'smtp.sender',
                                                      'smtp.timeout_s')}
    assert defaults == {'smtp.host': '', 'smtp.port': 587, 'smtp.security': 'starttls', 'smtp.username': '', 'smtp.sender': '',
                        'smtp.timeout_s': 10}
    conn = database.connect(app.config['DB_PATH'])
    for key, bad in (('smtp.port', 0), ('smtp.port', 65536), ('smtp.security', 'ssl'), ('smtp.timeout_s', 0), ('smtp.timeout_s', 61),
                     ('smtp.sender', 'a@b.c,d@e.f'), ('smtp.host', 'x' * 254), ('smtp.username', 'x' * 255)):
        with pytest.raises(SettingsError):
            store.set_many(conn, {key: bad}, actor=CLI)
    conn.close()


def test_an_smtp_host_outside_the_list_is_refused_on_write_and_the_port_is_part_of_the_destination(app, smtp):
    store = app.config['SETTINGS_STORE']
    conn = database.connect(app.config['DB_PATH'])
    for changes in ({'smtp.host': 'attaquant.example'}, {'smtp.port': smtp.port + 1}, {'smtp.host': '127.0.0.2'}):
        with pytest.raises(SettingsError) as exc:
            store.set_many(conn, changes, actor=CLI)
        assert 'OUTBOUND_ALLOWED_HOSTS' in exc.value.message
    assert store.get('smtp.host') == '127.0.0.1'
    conn.close()


def test_security_none_refuses_a_username_on_write_both_ways(app, smtp):
    """CA-35 : none + identifiant => refusé à l'écriture."""
    store = app.config['SETTINGS_STORE']
    conn = database.connect(app.config['DB_PATH'])
    with pytest.raises(SettingsError) as exc:
        store.set_many(conn, {'smtp.username': 'robot'}, actor=CLI)                 # sécurité courante : none
    assert exc.value.field == 'smtp.username'
    store.set_many(conn, {'smtp.security': 'starttls', 'smtp.username': 'robot'}, actor=CLI)
    with pytest.raises(SettingsError) as exc:
        store.set_many(conn, {'smtp.security': 'none'}, actor=CLI)
    assert exc.value.field == 'smtp.security'
    store.set_many(conn, {'smtp.security': 'none', 'smtp.username': ''}, actor=CLI)    # le lot qui rend l'ensemble cohérent passe
    conn.close()


def test_changing_the_host_or_the_port_clears_the_smtp_password_in_the_same_transaction(app, smtp, tmp_path):
    """CA-34."""
    store = app.config['SETTINGS_STORE']
    conn = database.connect(app.config['DB_PATH'])
    secrets = app.extensions['secrets']
    other = FakeSMTP()
    try:
        app2 = make_app(tmp_path / 'b', smtp.port, OUTBOUND_ALLOWED_HOSTS=f'smtp=127.0.0.1:{smtp.port},smtp=127.0.0.1:{other.port}')
        store2, secrets2 = app2.config['SETTINGS_STORE'], app2.extensions['secrets']
        conn2 = database.connect(app2.config['DB_PATH'])
        store2.set_many(conn2, {'smtp.host': '127.0.0.1', 'smtp.port': smtp.port}, actor=CLI)
        secrets2.set(conn2, 'smtp.password', 'mot-de-passe-smtp', f'smtp://127.0.0.1:{smtp.port}', actor=CLI)
        store2.set_many(conn2, {'smtp.sender': 'a@example.org', 'smtp.timeout_s': 5}, actor=CLI)         # sans effet sur la destination
        assert secrets2.status(conn2, 'smtp.password')['configured']
        store2.set_many(conn2, {'smtp.port': other.port}, actor=CLI)
        assert not secrets2.status(conn2, 'smtp.password')['configured']
        assert 'secret.clear_on_destination_change' in audit_actions(conn2)
        conn2.close()
    finally:
        other.stop()
        conn.close()
    assert secrets is not None and store is not None


def test_resetting_the_smtp_settings_clears_the_secret_too(app, smtp, conn):
    app.extensions['secrets'].set(conn, 'smtp.password', 'mot-de-passe-smtp', f'smtp://127.0.0.1:{smtp.port}', actor=CLI)
    app.config['SETTINGS_STORE'].reset(conn, 'smtp.host', actor=CLI)
    assert not app.extensions['secrets'].status(conn, 'smtp.password')['configured']


# --- Transport : jamais de clair, jamais d'authentification avec none (CA-35) ------------------------------------------------

def test_sending_with_none_delivers_without_any_authentication(app, smtp):
    app.extensions['mailer'].send('dest@example.org', 'Sujet', 'Corps du message')
    assert smtp.verbs()[:1] == ['EHLO'] and 'AUTH' not in smtp.verbs() and 'MAIL' in smtp.verbs() and 'DATA' in smtp.verbs()
    assert 'Corps du message' in text_of(smtp.messages[0]) and parsed(smtp.messages[0])['To'] == 'dest@example.org'


def test_none_with_an_inconsistent_state_in_the_database_refuses_to_send_and_never_connects(app, smtp, conn):
    conn.execute("INSERT INTO settings (key, value, updated_at, updated_by) VALUES ('smtp.username', '\"robot\"', 'x', 'sql')")
    conn.execute("UPDATE settings_meta SET version = version + 1")
    conn.commit()
    app.config['SETTINGS_STORE'].refresh(conn, force=True)
    with pytest.raises(MailError):
        app.extensions['mailer'].send('dest@example.org', 'S', 'C')
    assert smtp.connections == 0


def test_a_stored_smtp_password_with_none_also_refuses(app, smtp, conn):
    app.extensions['secrets'].set(conn, 'smtp.password', 'mot-de-passe-smtp', f'smtp://127.0.0.1:{smtp.port}', actor=CLI)
    with pytest.raises(MailError):
        app.extensions['mailer'].send('dest@example.org', 'S', 'C')
    assert smtp.connections == 0


def test_starttls_that_the_server_cannot_do_fails_and_nothing_is_ever_sent_in_clear(app, smtp, conn):
    """EF-4.1 : jamais de repli en clair."""
    app.config['SETTINGS_STORE'].set_many(conn, {'smtp.security': 'starttls'}, actor=CLI)
    with pytest.raises(MailError):
        app.extensions['mailer'].send('dest@example.org', 'S', 'Corps secret')
    assert 'MAIL' not in smtp.verbs() and 'DATA' not in smtp.verbs() and 'AUTH' not in smtp.verbs()
    assert smtp.messages == []
    smtp.advertise_starttls = True                              # annoncé mais refusé (454) : même résultat
    with pytest.raises(MailError):
        app.extensions['mailer'].send('dest@example.org', 'S', 'Corps secret')
    assert 'STARTTLS' in smtp.verbs() and 'MAIL' not in smtp.verbs() and smtp.messages == []


def test_tls_against_a_plain_server_fails_without_sending_anything(app, smtp, conn):
    app.config['SETTINGS_STORE'].set_many(conn, {'smtp.security': 'tls'}, actor=CLI)
    with pytest.raises(MailError):
        app.extensions['mailer'].send('dest@example.org', 'S', 'Corps secret')
    assert 'MAIL' not in smtp.verbs() and smtp.messages == []


def test_a_destination_outside_the_list_is_refused_at_send_time_audited_and_never_contacted(tmp_path, smtp):
    app = make_app(tmp_path, smtp.port)
    configure(app, smtp.port)
    conn = database.connect(app.config['DB_PATH'])
    conn.execute("UPDATE settings SET value = '\"127.0.0.9\"' WHERE key = 'smtp.host'")        # forcé en base, hors liste
    conn.execute("UPDATE settings_meta SET version = version + 1")
    conn.commit()
    app.config['SETTINGS_STORE'].refresh(conn, force=True)
    with pytest.raises(MailError):
        app.extensions['mailer'].send('dest@example.org', 'S', 'C')
    assert smtp.connections == 0 and 'outbound.refused' in audit_actions(conn)
    assert app.extensions['mailer'].configured is False
    conn.close()


def test_a_missing_or_foreign_smtp_password_refuses_when_a_username_is_set(app, smtp, conn):
    store = app.config['SETTINGS_STORE']
    store.set_many(conn, {'smtp.security': 'starttls', 'smtp.username': 'robot'}, actor=CLI)
    with pytest.raises(MailError):
        app.extensions['mailer'].send('dest@example.org', 'S', 'C')                  # mot de passe absent
    app.extensions['secrets'].set(conn, 'smtp.password', 'mot-de-passe-smtp', 'smtp://autre-hote:587', actor=CLI)
    with pytest.raises(MailError):
        app.extensions['mailer'].send('dest@example.org', 'S', 'C')                  # lié à une autre destination
    assert smtp.connections == 0


def test_not_configured_means_not_sent(tmp_path, smtp):
    app = make_app(tmp_path, smtp.port)
    mailer = app.extensions['mailer']
    assert mailer.configured is False
    with pytest.raises(MailError):
        mailer.send('a@example.org', 'S', 'C')
    assert mailer.enqueue('a@example.org', 'S', 'C') == 'not_configured' and smtp.connections == 0


def test_errors_never_leak_smtp_details_or_the_body(app, smtp, caplog):
    import logging
    smtp.mode = 'hang'
    with caplog.at_level(logging.DEBUG), pytest.raises(MailError) as exc:
        app.extensions['mailer'].send('dest@example.org', 'S', 'Lien secret https://x/auth/token#t=SECRET')
    assert str(exc.value) == "L'e-mail n'a pas pu être envoyé"
    assert 'SECRET' not in caplog.text and 'auth/token' not in caplog.text


# --- File d'envoi (EF-4.8) --------------------------------------------------------------------------------------------------

def test_enqueue_is_deferred_bounded_and_drained(app, smtp):
    mailer = app.extensions['mailer']
    assert mailer.enqueue('a@example.org', 'S', 'C1') == 'queued' and smtp.connections == 0
    assert mailer.enqueue('pas-un-mail', 'S', 'C') == 'invalid'
    assert mailer.queue.pending() == 1 and mailer.queue.drain() == 1
    assert len(smtp.messages) == 1 and mailer.queue.pending() == 0


def test_the_queue_is_bounded_and_a_full_queue_drops_and_logs(caplog):
    sent = []
    queue_ = MailQueue(sent.append, max_size=3)
    results = [queue_.put(f'm{i}') for i in range(5)]
    assert results == [True, True, True, False, False] and queue_.dropped == 2
    assert queue_.drain() == 3 and sent == ['m0', 'm1', 'm2']


def test_a_failing_message_never_kills_the_worker(caplog):
    done = threading.Event()
    seen = []

    def send(message):
        seen.append(message)
        if message == 'boom':
            raise RuntimeError("panne avec lien https://x/auth/token#t=SECRET")
        done.set()

    queue_ = MailQueue(send)
    queue_.start()
    queue_.put('boom')
    queue_.put('ok')
    assert done.wait(5) and seen == ['boom', 'ok']
    queue_.stop()
    assert 'SECRET' not in caplog.text


def test_the_default_queue_size_is_one_thousand():
    assert mailer_module.QUEUE_SIZE == 1000


def test_account_creation_does_not_wait_for_the_mail_and_queues_the_verification(app, smtp, conn):
    """EF-4.3 / EF-4.8 : réponse indépendante de l'envoi ; message de validation en file, lien sur PUBLIC_URL."""
    smtp.mode = 'hang'                                          # un serveur muet ne ralentit pas la requête
    admin = add_user(conn, 'adm', admin=True, email='adm@example.org')
    new = invitations.create(conn, admin, 'invitee@example.org')
    client = app.test_client()
    kw = {'base_url': f'https://{HOST}'}
    csrf_token = re.search(r'name="csrf-token" content="([^"]+)"', client.get('/auth/token', **kw).get_data(as_text=True)).group(1)
    started = time.monotonic()
    response = client.post('/auth/token/consume', json={'token': new.token, 'username': 'alice', 'password': GOOD},
                           headers={'X-CSRF-Token': csrf_token, 'Origin': f'https://{HOST}'}, **kw)
    assert response.status_code == 200 and time.monotonic() - started < 1.5
    queue_ = app.extensions['mailer'].queue
    assert queue_.pending() == 1
    verify = conn.execute("SELECT * FROM auth_tokens WHERE kind = 'verify_email'").fetchone()
    alice = conn.execute("SELECT id, pending_email FROM users WHERE username = 'alice'").fetchone()
    assert verify['target_user_id'] == alice['id'] and verify['email'] == 'invitee@example.org' == alice['pending_email']


def test_the_verification_mail_content_after_draining(app, smtp, conn):
    admin = add_user(conn, 'adm', admin=True, email='adm@example.org')
    new = invitations.create(conn, admin, 'invitee@example.org')
    client = app.test_client()
    kw = {'base_url': f'https://{HOST}'}
    csrf_token = re.search(r'name="csrf-token" content="([^"]+)"', client.get('/auth/token', **kw).get_data(as_text=True)).group(1)
    client.post('/auth/token/consume', json={'token': new.token, 'username': 'alice', 'password': GOOD},
                headers={'X-CSRF-Token': csrf_token, 'Origin': f'https://{HOST}'}, **kw)
    assert app.extensions['mailer'].queue.drain() == 1
    text = text_of(smtp.messages[0])
    assert parsed(smtp.messages[0])['To'] == 'invitee@example.org' and '« alice »' in text
    assert f'https://{HOST}/auth/token#t=' in text
    token = re.search(r'#t=([A-Za-z0-9_-]{43})', text).group(1)
    assert tokens.inspect(conn, token) == 'verify_email'


def test_without_smtp_no_verification_token_is_created_and_the_address_stays_pending(tmp_path, smtp):
    app = make_app(tmp_path, smtp.port)
    conn = database.connect(app.config['DB_PATH'])
    admin = add_user(conn, 'adm', admin=True)
    new = invitations.create(conn, admin, 'invitee@example.org')
    client = app.test_client()
    kw = {'base_url': f'https://{HOST}'}
    csrf_token = re.search(r'name="csrf-token" content="([^"]+)"', client.get('/auth/token', **kw).get_data(as_text=True)).group(1)
    assert client.post('/auth/token/consume', json={'token': new.token, 'username': 'alice', 'password': GOOD},
                       headers={'X-CSRF-Token': csrf_token, 'Origin': f'https://{HOST}'}, **kw).status_code == 200
    assert conn.execute("SELECT COUNT(*) FROM auth_tokens WHERE kind = 'verify_email'").fetchone()[0] == 0
    assert conn.execute("SELECT pending_email, email FROM users WHERE username = 'alice'").fetchone()[:] == ('invitee@example.org', None)
    conn.close()


# --- Invitation : envoi synchrone (EF-4.8) ------------------------------------------------------------------------------------

def session_client(app, conn, user):
    client = app.test_client()
    kw = {'base_url': f'https://{HOST}'}
    with client.session_transaction(**kw) as s:
        s['user_id'], s['token'], s['login_at'] = user['id'], 'tok', int(time.time())
    return client, {**kw, 'headers': {'Origin': f'https://{HOST}'}}


def test_invitation_is_mailed_synchronously_and_the_link_is_still_shown(app, smtp, conn):
    admin = add_user(conn, 'adm', admin=True, email='adm@example.org')
    client, kw = session_client(app, conn, admin)
    body = client.post('/api/invitations', json={'email': 'Friend@Example.org', 'note': 'Salut'}, **kw).get_json()
    assert body['sent_by_mail'] is True and body['link'].startswith(f'https://{HOST}/auth/token#t=')
    assert len(smtp.messages) == 1 and parsed(smtp.messages[0])['To'] == 'friend@example.org'
    assert 'Message de adm : Salut' in text_of(smtp.messages[0]) and body['link'].split('#t=')[1] in text_of(smtp.messages[0])
    assert conn.execute("SELECT sent_by_mail FROM auth_tokens").fetchone()[0] == 1


def test_a_failed_send_says_so_and_the_issuer_keeps_the_link(app, smtp, conn):
    smtp.mode = 'hang'
    admin = add_user(conn, 'adm', admin=True, email='adm@example.org')
    client, kw = session_client(app, conn, admin)
    body = client.post('/api/invitations', json={'email': 'friend@example.org'}, **kw).get_json()
    assert body['sent_by_mail'] is False and body['link'] and body['id']
    assert conn.execute("SELECT sent_by_mail FROM auth_tokens").fetchone()[0] == 0


def test_a_failed_send_does_not_return_the_credit_either(app, smtp, conn):
    smtp.mode = 'hang'
    member = add_user(conn, 'mem', credits=2, email='mem@example.org')
    client, kw = session_client(app, conn, member)
    assert client.post('/api/invitations', json={'email': 'a@example.org'}, **kw).status_code == 201
    assert conn.execute("SELECT invite_credits FROM users WHERE id = ?", (member['id'],)).fetchone()[0] == 1


# --- E-mail de test neutre (EF-4.2, CA-37) -----------------------------------------------------------------------------------------

def timed_test(app):
    started = time.monotonic()
    result = app.extensions['mailer'].send_test('admin@example.org')
    return result, time.monotonic() - started


def test_the_test_mail_answers_ok_or_ko_only_and_always_after_the_smtp_timeout(app, smtp, conn):
    app.config['SETTINGS_STORE'].set_many(conn, {'smtp.timeout_s': 1}, actor=CLI)
    outcomes = []
    outcomes.append(('immediat', *timed_test(app)))
    smtp.mode = 'hang'
    outcomes.append(('muet', *timed_test(app)))
    dead = closed_port()
    refused = make_app(Path(app.config['DATA_DIR']).parent / 'refused', dead)
    configure(refused, dead, timeout=1)
    outcomes.append(('refuse', *timed_test(refused)))
    for name, result, elapsed in outcomes:
        assert set(result) == {'ok'} and isinstance(result['ok'], bool), name
        assert 0.9 <= elapsed <= 1.3, (name, elapsed)                           # durée = délai du service, à 10-30 % près
    assert outcomes[0][1] == {'ok': True} and outcomes[1][1] == {'ok': False} and outcomes[2][1] == {'ok': False}


# --- Commandes CLI : envoi synchrone, code de retour (EF-4.8, v5-3, CA-49) ------------------------------------------------------------

def invoke(app, *args):
    return app.test_cli_runner().invoke(args=list(args))


def test_user_invite_mails_the_bootstrap_link_synchronously(app, smtp):
    result = invoke(app, 'user-invite', '--admin', 'first@example.org')
    assert result.exit_code == 0 and 'aussi parti par e-mail' in result.output
    assert len(smtp.messages) == 1 and parsed(smtp.messages[0])['To'] == 'first@example.org'
    link = next(line for line in result.output.splitlines() if line.startswith('https://'))
    assert link.split('#t=')[1] in text_of(smtp.messages[0])


def test_user_invite_with_a_dead_smtp_still_creates_the_token_prints_the_link_and_exits_non_zero(app, smtp, conn):
    """CA-49 : action faite, code non nul, message nommant la notification non partie."""
    smtp.mode = 'hang'
    result = invoke(app, 'user-invite', '--admin', 'first@example.org')
    assert result.exit_code == 1 and "n'est PAS partie" in result.output and 'amorçage' in result.output
    assert 'https://' in result.output
    assert conn.execute("SELECT COUNT(*) FROM auth_tokens WHERE kind = 'bootstrap'").fetchone()[0] == 1


def test_user_mfa_reset_notifies_the_holder_before_returning(app, smtp, conn):
    add_user(conn, 'adm', admin=True, email='adm@example.org')
    add_user(conn, 'alice', email='alice@example.org')
    result = invoke(app, 'user-mfa-reset', 'alice')
    assert result.exit_code == 0 and len(smtp.messages) == 1
    assert parsed(smtp.messages[0])['To'] == 'alice@example.org' and 'réinitialisés' in text_of(smtp.messages[0])


def test_user_mfa_reset_with_a_dead_smtp_is_done_and_traced_but_exits_non_zero(app, smtp, conn):
    smtp.mode = 'hang'
    add_user(conn, 'adm', admin=True, email='adm@example.org')
    target = add_user(conn, 'alice', email='alice@example.org', admin=True)
    result = invoke(app, 'user-mfa-reset', 'alice')
    assert result.exit_code == 1 and "n'est PAS partie" in result.output and 'facteurs réinitialisés' in result.output
    assert conn.execute("SELECT is_admin FROM users WHERE id = ?", (target['id'],)).fetchone()[0] == 0       # action faite
    assert 'user.mfa_reset' in audit_actions(conn)


def test_user_mfa_reset_without_a_validated_address_or_without_smtp_is_not_an_error(app, smtp, conn, tmp_path):
    add_user(conn, 'noaddr')
    result = invoke(app, 'user-mfa-reset', 'noaddr')
    assert result.exit_code == 0 and 'Aucune adresse validée' in result.output and smtp.connections == 0
    plain = make_app(tmp_path / 'p', smtp.port)
    c = database.connect(plain.config['DB_PATH'])
    add_user(c, 'bob', email='bob@example.org')
    c.close()
    out = invoke(plain, 'user-mfa-reset', 'bob')
    assert out.exit_code == 0 and 'E-mail non configuré' in out.output


def test_secrets_reset_notifies_the_admins_before_erasing_the_secrets(app, smtp, conn):
    add_user(conn, 'adm1', admin=True, email='a1@example.org')
    add_user(conn, 'adm2', admin=True, email='a2@example.org')
    add_user(conn, 'plain', email='p@example.org')
    app.extensions['secrets'].set(conn, 'routing.api_key', 'cle-de-routage-123456', 'http://moteur:8989', actor=CLI)
    result = invoke(app, 'secrets-reset', '--yes')
    assert result.exit_code == 0 and len(smtp.messages) == 2
    assert {parsed(m)['To'] for m in smtp.messages} == {'a1@example.org', 'a2@example.org'}
    assert not app.extensions['secrets'].status(conn, 'routing.api_key')['configured']


def test_secrets_reset_with_a_dead_smtp_erases_anyway_and_exits_non_zero(app, smtp, conn):
    smtp.mode = 'hang'
    add_user(conn, 'adm1', admin=True, email='a1@example.org')
    app.extensions['secrets'].set(conn, 'routing.api_key', 'cle-de-routage-123456', 'http://moteur:8989', actor=CLI)
    result = invoke(app, 'secrets-reset', '--yes')
    assert result.exit_code == 1 and "n'est PAS partie" in result.output
    assert not app.extensions['secrets'].status(conn, 'routing.api_key')['configured']


def test_settings_reset_notifies_only_when_it_touches_smtp(app, smtp, conn):
    add_user(conn, 'adm1', admin=True, email='a1@example.org')
    app.config['SETTINGS_STORE'].set_many(conn, {'USER_QUOTA_MB': 5}, actor=CLI)
    assert invoke(app, 'settings-reset', 'USER_QUOTA_MB').exit_code == 0 and smtp.messages == []
    result = invoke(app, 'settings-reset', 'smtp.sender')
    assert result.exit_code == 0 and len(smtp.messages) == 1 and 'réinitialis' in text_of(smtp.messages[0])


def test_commands_do_not_start_a_queue_thread(app):
    assert app.extensions['mailer'].queue._thread is None


def test_secrets_never_reach_the_audit_through_the_mail_paths(app, smtp, conn):
    add_user(conn, 'adm1', admin=True, email='a1@example.org')
    app.extensions['secrets'].set(conn, 'smtp.password', 'mot-de-passe-smtp-secret', f'smtp://127.0.0.1:{smtp.port}', actor=CLI)
    invoke(app, 'secrets-reset', '--yes')
    dump = json.dumps([dict(r) for r in conn.execute('SELECT * FROM audit_log')])
    assert 'mot-de-passe-smtp-secret' not in dump
    assert SecretsStore(KEY) is not None
