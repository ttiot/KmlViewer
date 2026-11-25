"""
2e lot, groupe 3a (rôle test, écrit depuis la spec) : CA-60 (E-15 : bandeau et renvoi du mail de validation), CA-61 (E-10 : secrets en oidc),
CA-62 (E-12 : collision d'identifiant SSO), CA-66 (E-16 : perte de la file d'envoi), CA-R38 (E-24 : trace à 1 point) et la reproductibilité de
la Public Suffix List embarquée (E-13, CA-59, partie « fichier »). Aucun marqueur xfail : un test rouge est un défaut à rapporter.
Tout ce que ces tests lisent est DANS web-app (la CI ne monte que ce dossier).
"""

import hashlib
import json
import logging
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from app import create_app
from app.audit import Actor
from app.auth import tokens, users
from app.mailer import MailQueue
from tests.conftest import OIDC_OVERRIDES
from tests.pw_harness import GOOD, HOST, ORIGIN, add, call, connect, form_post, make_app, new_secret, refresh, session_client
from tests.test_lot2_g1_edge import configure_smtp, mails, smtp as smtp, text_of  # noqa: F401  (smtp : fixture)
from tests.test_route_builder_api_edge import build_world, POINTS
from tests.routing_fakes import FakeProvider
from tests.pw_harness import KEY, Prod as _Prod

CLI = Actor.cli()
WEB_APP = Path(__file__).resolve().parent.parent


# ===== CA-60 / E-15 : adresse non validée ⇒ bandeau et renvoi du mail (3 par heure) =================================================

@pytest.fixture
def mail_app(tmp_path, smtp):
    app = make_app(tmp_path, OUTBOUND_ALLOWED_HOSTS=f'smtp=127.0.0.1:{smtp.port}')
    configure_smtp(app, smtp.port)
    return app


@pytest.fixture
def conn_of(mail_app):
    c = connect(mail_app)
    yield c
    c.close()


def pending_account(app, conn, name='alice', address='alice@example.org'):
    return add(app, conn, name, pending_email=address)


def shifted(base, delta):
    return lambda: base + delta


def advance(app, seconds):
    """Avance l'horloge de TOUS les limiteurs (ils sont gelés par le harnais)."""
    for limiter in vars(app.extensions['limiters']).values():
        clock = getattr(limiter, '_clock', None)
        if clock is not None and hasattr(limiter, 'hit'):
            limiter._clock = shifted(clock(), seconds)


@pytest.mark.parametrize('path', ['/', '/dashboard', '/profile', '/segments'])
def test_the_banner_with_its_button_is_on_every_page_for_a_pending_address_when_smtp_works(mail_app, conn_of, path):
    user = pending_account(mail_app, conn_of)
    html = call(session_client(mail_app, user), 'get', path).get_data(as_text=True)
    assert 'id="pendingEmailBanner"' in html and 'alice@example.org' in html
    assert 'id="resendVerification"' in html


def test_a_validated_address_has_no_banner(mail_app, conn_of):
    user = add(mail_app, conn_of, 'alice', email='alice@example.org')
    for path in ('/', '/dashboard', '/profile'):
        assert 'pendingEmailBanner' not in call(session_client(mail_app, user), 'get', path).get_data(as_text=True)


def test_without_smtp_the_banner_has_no_button(tmp_path):
    app = make_app(tmp_path)
    conn = connect(app)
    user = pending_account(app, conn)
    for path in ('/', '/dashboard', '/profile'):
        html = call(session_client(app, user), 'get', path).get_data(as_text=True)
        assert 'id="pendingEmailBanner"' in html and 'id="resendVerification"' not in html, path
    conn.close()


def test_the_banner_never_shows_an_unescaped_address(mail_app, conn_of):
    user = pending_account(mail_app, conn_of, address='x@example.org')
    conn_of.execute("UPDATE users SET pending_email = ? WHERE id = ?", ('<script>alert(1)</script>@example.org', user['id']))
    conn_of.commit()
    html = call(session_client(mail_app, refresh(conn_of, 'alice')), 'get', '/profile').get_data(as_text=True)
    assert '<script>alert(1)</script>' not in html


def resend(client, **kw):
    return call(client, 'post', '/api/me/email/resend-verification', {}, **kw)


def verification_links(app, smtp):
    return [re.search(r'(https://\S+#t=\S+)', text_of(m)).group(1) for m in mails(app, smtp)]


def test_three_resends_in_the_hour_send_three_mails_the_fourth_is_a_429_with_retry_after(mail_app, conn_of, smtp):
    user = pending_account(mail_app, conn_of)
    client = session_client(mail_app, user)
    for _ in range(3):
        assert resend(client).status_code == 200
    links = verification_links(mail_app, smtp)
    assert len(links) == 3 and len(set(links)) == 3 and all(m['To'] == 'alice@example.org' for m in map(
        __import__('email').message_from_string, smtp.messages))
    smtp.messages.clear()
    fourth = resend(client)
    assert fourth.status_code == 429 and int(fourth.headers['Retry-After']) > 0
    assert mails(mail_app, smtp) == []                                                   # refusé : aucun mail
    advance(mail_app, 3601)
    assert resend(client).status_code == 200


def test_an_admin_resend_counts_in_the_same_three(mail_app, conn_of, smtp):
    admin = add(mail_app, conn_of, 'chef', admin=True, secret=new_secret())
    user = pending_account(mail_app, conn_of)
    client = session_client(mail_app, user)
    assert resend(client).status_code == 200 and resend(client).status_code == 200
    assert call(session_client(mail_app, admin), 'post', f"/api/admin/users/{user['id']}/resend-verification").status_code == 200
    assert resend(client).status_code == 429
    again = call(session_client(mail_app, admin), 'post', f"/api/admin/users/{user['id']}/resend-verification")
    assert again.status_code == 429 and 'Retry-After' in again.headers


def test_the_budget_is_per_account(mail_app, conn_of):
    one, two = pending_account(mail_app, conn_of), pending_account(mail_app, conn_of, 'bob', 'bob@example.org')
    for _ in range(3):
        assert resend(session_client(mail_app, one)).status_code == 200
    assert resend(session_client(mail_app, one)).status_code == 429
    assert resend(session_client(mail_app, two)).status_code == 200


def test_the_first_mail_of_an_address_change_does_not_count_as_a_resend(mail_app, conn_of, smtp):
    user = add(mail_app, conn_of, 'alice', email='alice@example.org')
    client = session_client(mail_app, user)
    r = call(client, 'post', '/api/me/email', {'password': GOOD, 'email': 'nouvelle@example.org'})
    assert r.status_code == 200 and refresh(conn_of, 'alice')['pending_email'] == 'nouvelle@example.org'
    assert [resend(client).status_code for _ in range(4)] == [200, 200, 200, 429]


def test_only_the_last_link_validates_the_address_with_the_session_of_the_account(mail_app, conn_of, smtp):
    user = pending_account(mail_app, conn_of)
    client = session_client(mail_app, user)
    for _ in range(3):
        resend(client)
    links = verification_links(mail_app, smtp)
    token = [link.split('#t=')[1] for link in links]
    assert tokens.kind_of(conn_of, token[0]) is None and tokens.kind_of(conn_of, token[1]) is None      # précédents : plus rien ne valide
    assert tokens.kind_of(conn_of, token[2]) == 'verify_email'
    assert conn_of.execute("SELECT COUNT(*) FROM auth_tokens WHERE kind = 'verify_email' AND used_at IS NULL AND revoked_at IS NULL").fetchone()[0] == 1
    assert form_post(mail_app, '/auth/token/consume', {'token': token[0]}, client=client).status_code in (404, 403)
    assert refresh(conn_of, 'alice')['email'] is None
    done = form_post(mail_app, '/auth/token/consume', {'token': token[2]}, client=session_client(mail_app, user))
    assert done.status_code == 200
    after = refresh(conn_of, 'alice')
    assert after['email'] == 'alice@example.org' and after['pending_email'] is None


def test_a_refused_resend_does_not_revoke_the_valid_link(mail_app, conn_of, smtp):
    user = pending_account(mail_app, conn_of)
    client = session_client(mail_app, user)
    for _ in range(3):
        resend(client)
    last = verification_links(mail_app, smtp)[-1].split('#t=')[1]
    assert resend(client).status_code == 429
    assert tokens.kind_of(conn_of, last) == 'verify_email'


def test_the_link_of_another_account_session_validates_nothing(mail_app, conn_of, smtp):
    user = pending_account(mail_app, conn_of)
    other = add(mail_app, conn_of, 'mallory')
    resend(session_client(mail_app, user))
    token = verification_links(mail_app, smtp)[0].split('#t=')[1]
    assert form_post(mail_app, '/auth/token/consume', {'token': token}, client=session_client(mail_app, other)).status_code == 404
    assert tokens.kind_of(conn_of, token) == 'verify_email' and refresh(conn_of, 'alice')['email'] is None


@pytest.mark.parametrize('case', ['none_pending', 'validated_only', 'no_smtp'])
def test_resend_is_a_409_when_there_is_nothing_to_validate_or_no_way_to_send(tmp_path, smtp, case):
    app = make_app(tmp_path, OUTBOUND_ALLOWED_HOSTS=f'smtp=127.0.0.1:{smtp.port}')
    if case != 'no_smtp':
        configure_smtp(app, smtp.port)
    conn = connect(app)
    user = {'none_pending': lambda: add(app, conn, 'alice'),
            'validated_only': lambda: add(app, conn, 'alice', email='alice@example.org'),
            'no_smtp': lambda: pending_account(app, conn)}[case]()
    assert resend(session_client(app, user)).status_code == 409
    assert conn.execute("SELECT COUNT(*) FROM auth_tokens").fetchone()[0] == 0
    conn.close()


def test_resend_without_a_session_is_a_401_and_an_sso_account_gets_a_404(tmp_path):
    app = create_app(_Prod, overrides={'DATA_DIR': str(tmp_path / 'oidc'), 'AUTH_MODE': 'oidc', **OIDC_OVERRIDES, 'TRUSTED_HOSTS': [HOST],
                                       'SECRETS_KEY': KEY, 'PUBLIC_URL': ORIGIN})
    conn = connect(app)
    sso = add(app, conn, 'sso', method='oidc', email='sso@example.org')
    assert resend(app.test_client()).status_code == 401
    r = resend(session_client(app, sso))
    assert r.status_code == 404, r.status_code                                           # décision de coord (25/11) : 404 gardé, spec alignée
    conn.close()


NODE_HARNESS = r"""
const vm = require('vm'), fs = require('fs');
const source = fs.readFileSync(process.argv[2], 'utf8');
const scenario = JSON.parse(process.argv[3]);
const listeners = {};
const calls = [], notices = [];
const button = { disabled: false, textContent: 'Renvoyer', addEventListener(t, f) { this.click = f; } };
class HTMLFormElement {} class Node {} class FormData {}
const sandbox = {
  document: { addEventListener(t, f) { (listeners[t] = listeners[t] || []).push(f); },
              getElementById: (id) => (id === 'resendVerification' ? button : null), querySelector: () => null,
              documentElement: { style: { setProperty() {} } }, createElement: () => ({ append() {}, setAttribute() {}, classList: { add() {} } }),
              createTextNode: (t) => ({ t }) },
  window: { localStorage: {}, location: { pathname: '/', search: '', assign() {} } }, HTMLFormElement, Node, FormData, console,
  ResizeObserver: class { observe() {} }, Object, JSON, Error, Promise, Date, Math, Number, String, Array, Map, Set, setTimeout, clearTimeout, Intl,
  encodeURIComponent,
  fetch: async (path, init) => { calls.push([path, init && init.method]); return { status: scenario.status, ok: scenario.status < 400,
                                                                                    json: async () => (scenario.status < 400 ? { success: true } : { error: 'Trop de renvois : réessaie plus tard.' }) }; },
};
sandbox.window.document = sandbox.document;
vm.createContext(sandbox);
vm.runInContext(source, sandbox);
vm.runInContext("App.notify = (m, k) => { __notices.push([m, k || 'info']); };", Object.assign(sandbox, { __notices: notices }));
(async () => {
  for (const f of (listeners.DOMContentLoaded || [])) f();
  await button.click();
  console.log(JSON.stringify({ calls, notices, disabled: button.disabled, text: button.textContent }));
})();
"""


@pytest.mark.skipif(__import__('shutil').which('node') is None, reason="node absent : exécution du JavaScript impossible")
@pytest.mark.parametrize('status,expected_text', [(200, 'Mail renvoyé'), (429, 'Renvoyer')])
def test_the_resend_button_posts_to_the_resend_route_and_reenables_itself(status, expected_text):
    out = subprocess.run(['node', '-', str(WEB_APP / 'static' / 'js' / 'common.js'), json.dumps({'status': status})], input=NODE_HARNESS,
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    data = json.loads(out.stdout.strip().splitlines()[-1])
    assert data['calls'] == [['/api/me/email/resend-verification', 'POST']] and data['disabled'] is False and data['text'] == expected_text


# ===== CA-61 / E-10 : mode oidc sans SECRETS_KEY ou sans PUBLIC_URL ===========================================================

def oidc_app(tmp_path, *, key=True, url=True, **extra):
    overrides = {'DATA_DIR': str(tmp_path / 'data'), 'AUTH_MODE': 'oidc', **OIDC_OVERRIDES, 'TRUSTED_HOSTS': [HOST], **extra}
    if key:
        overrides['SECRETS_KEY'] = KEY
    if url:
        overrides['PUBLIC_URL'] = ORIGIN
    return create_app(_Prod, overrides=overrides)


def sso_admin(app):
    conn = connect(app)
    admin = add(app, conn, 'chef', admin=True, method='oidc')
    conn.close()
    return session_client(app, admin)


@pytest.mark.parametrize('key,url,missing', [(True, False, ['PUBLIC_URL']), (False, True, ['SECRETS_KEY']), (False, False, ['SECRETS_KEY', 'PUBLIC_URL'])])
def test_secrets_input_and_test_are_404_and_the_admin_names_the_missing_variables(tmp_path, key, url, missing):
    app = oidc_app(tmp_path, key=key, url=url)
    admin = sso_admin(app)
    assert call(admin, 'put', '/api/admin/secrets/smtp.password', {'value': 'secret-value'}).status_code == 404
    assert call(admin, 'post', '/api/admin/secrets/smtp.password/test', {}).status_code == 404
    answer = call(admin, 'get', '/api/admin/secrets')
    assert answer.status_code == 200
    body = answer.get_json()
    assert body['disabled'] is True and sorted(body['missing']) == sorted(missing)
    assert 'secret-value' not in answer.get_data(as_text=True)


@pytest.mark.parametrize('key,url', [(True, False), (False, True)])
def test_password_account_and_invitation_routes_are_404_in_that_state(tmp_path, key, url):
    app = oidc_app(tmp_path, key=key, url=url)
    admin = sso_admin(app)
    anonymous = app.test_client()
    for method, path in (('get', '/auth/password/login'), ('post', '/auth/password/login'), ('get', '/auth/password/forgot'),
                         ('post', '/auth/password/forgot'), ('get', '/auth/token'), ('post', '/auth/token/inspect')):
        r = (form_post(app, path, {}) if method == 'post' else call(anonymous, 'get', path))
        assert r.status_code == 404, (method, path, r.status_code)
    for method, path in (('get', '/api/invitations'), ('post', '/api/invitations'), ('get', '/api/admin/invitations')):
        assert call(admin, method, path, {} if method == 'post' else None).status_code == 404, (method, path)


def test_with_both_variables_the_secret_can_be_entered(tmp_path):
    app = oidc_app(tmp_path, OUTBOUND_ALLOWED_HOSTS='smtp=127.0.0.1:2525')
    configure_smtp(app, 2525)                                                            # la destination (hôte autorisé) précède le secret
    admin = sso_admin(app)
    body = call(admin, 'get', '/api/admin/secrets').get_json()
    assert body['disabled'] is False and body['missing'] == []
    assert call(admin, 'put', '/api/admin/secrets/smtp.password', {'value': 'secret-value'}).status_code == 200


@pytest.mark.parametrize('key,url', [(True, False), (False, True), (False, False)])
def test_the_start_is_never_refused_for_that_reason(tmp_path, key, url):
    assert oidc_app(tmp_path, key=key, url=url).config['AUTH_MODE'] == 'oidc'


def test_password_mode_never_reports_the_secrets_as_disabled(tmp_path):
    app = make_app(tmp_path)
    conn = connect(app)
    admin = add(app, conn, 'chef', admin=True, secret=new_secret())
    body = call(session_client(app, admin), 'get', '/api/admin/secrets').get_json()
    assert body['disabled'] is False and body['missing'] == []
    conn.close()


def test_the_admin_script_shows_the_explanation_with_the_missing_names_and_hides_the_form():
    source = (WEB_APP / 'static' / 'js' / 'admin.js').read_text(encoding='utf-8')
    block = source[source.index('if (data.disabled) {'):][:600]
    assert 'missing' in block and 'return' in block and 'replaceChildren' in block and 'innerHTML' not in block


# ===== CA-62 / E-12 : collision d'identifiant créée par l'IdP =================================================================

@pytest.fixture
def sso_world(tmp_path):
    app = oidc_app(tmp_path)
    conn = connect(app)
    add(app, conn, 'alice', email='alice@example.org')                          # compte à mot de passe
    add(app, conn, 'bob', method='oidc')
    yield app, conn
    conn.close()


def collisions(conn, kind='identifiant'):
    return [json.loads(r[0]) for r in conn.execute("SELECT details FROM audit_log WHERE action = 'identity.collision'") if json.loads(r[0]).get('kind') == kind]


def test_an_idp_identifier_equal_to_a_password_account_is_accepted_with_one_warning_and_one_audit_line(sso_world, caplog):
    app, conn = sso_world
    caplog.set_level(logging.WARNING)
    row = users.upsert_from_claims(conn, {'sub': 'sub:a1', 'preferred_username': 'Alice'})
    assert row['username'] == 'Alice' and row['auth_method'] == 'oidc'                       # connexion acceptée
    lines = collisions(conn)
    assert len(lines) == 1 and row['id'] in lines[0]['accounts'] and len(lines[0]['accounts']) == 2
    assert len([r for r in caplog.records if r.levelno == logging.WARNING and 'ollision' in r.getMessage()]) == 1
    row2 = conn.execute("SELECT * FROM audit_log WHERE action = 'identity.collision'").fetchone()
    assert row2['actor_type'] in ('system', 'cli', 'user')


def test_the_same_identifier_at_the_next_logins_adds_nothing(sso_world, caplog):
    app, conn = sso_world
    users.upsert_from_claims(conn, {'sub': 'sub:a1', 'preferred_username': 'Alice'})
    caplog.set_level(logging.WARNING)
    caplog.clear()
    for _ in range(3):
        users.upsert_from_claims(conn, {'sub': 'sub:a1', 'preferred_username': 'Alice'})
    users.upsert_from_claims(conn, {'sub': 'sub:a1', 'preferred_username': 'ALICE'})                  # autre casse : même identifiant
    assert len(collisions(conn)) == 1 and not [r for r in caplog.records if 'ollision' in r.getMessage()]


def test_a_change_of_identifier_to_another_taken_one_adds_a_line_and_to_a_free_one_adds_none(sso_world):
    app, conn = sso_world
    users.upsert_from_claims(conn, {'sub': 'sub:a1', 'preferred_username': 'Alice'})
    users.upsert_from_claims(conn, {'sub': 'sub:a1', 'preferred_username': 'libre'})
    assert len(collisions(conn)) == 1
    users.upsert_from_claims(conn, {'sub': 'sub:a1', 'preferred_username': 'BOB'})                  # pris par un autre
    assert len(collisions(conn)) == 2


def test_an_identifier_that_is_free_creates_no_collision(sso_world):
    app, conn = sso_world
    users.upsert_from_claims(conn, {'sub': 'sub:z', 'preferred_username': 'personne'})
    assert collisions(conn) == []


def test_an_address_collision_keeps_its_own_kind_and_is_not_counted_as_an_identifier_collision(sso_world):
    app, conn = sso_world
    users.upsert_from_claims(conn, {'sub': 'sub:m', 'preferred_username': 'mallory', 'email': 'alice@example.org', 'email_verified': True})
    assert collisions(conn) == []                                                         # identifiant libre : aucune ligne « identifiant »
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'identity.collision'").fetchone()[0] >= 1    # l'adresse, elle, est tracée


def test_the_password_login_of_the_colliding_account_still_works(sso_world):
    app, conn = sso_world
    users.upsert_from_claims(conn, {'sub': 'sub:a1', 'preferred_username': 'Alice'})
    from tests.pw_harness import login
    assert login(app, 'alice', GOOD).status_code == 200


def test_the_admin_list_reports_the_identifier_collision_case_insensitively(sso_world):
    app, conn = sso_world
    users.upsert_from_claims(conn, {'sub': 'sub:a1', 'preferred_username': 'Alice'})
    admin = session_client(app, add(app, conn, 'chef', admin=True, method='oidc'))
    r = call(admin, 'get', '/api/admin/users')
    assert r.status_code == 200
    names = [u['username'] for u in r.get_json()['users']]
    assert 'Alice' in names and 'alice' in names


def test_no_secret_or_hash_reaches_the_collision_audit(sso_world):
    app, conn = sso_world
    users.upsert_from_claims(conn, {'sub': 'sub:a1', 'preferred_username': 'Alice', 'email': 'a@example.org', 'email_verified': True})
    dump = json.dumps([dict(r) for r in conn.execute("SELECT * FROM audit_log WHERE action = 'identity.collision'")])
    assert 'argon2' not in dump and 'password' not in dump.lower() and 'session' not in dump.lower()


# ===== CA-66 / E-16 : perte de la file d'envoi à l'arrêt ======================================================================

def make_message(subject, to='destinataire@example.org', body='https://secret.example/lien#t=jeton-secret'):
    from email.message import EmailMessage
    message = EmailMessage()
    message['To'], message['Subject'] = to, subject
    message.set_content(body)
    return message


def blocked_queue(waiting):
    """File dont l'envoi est bloqué : 1 message en cours + `waiting` messages en attente."""
    release, started = threading.Event(), threading.Event()

    def send(message):
        started.set()
        release.wait(30)
    queue = MailQueue(send)
    queue.start()
    queue.put(make_message('Premier'))
    assert started.wait(5)
    for subject in waiting:
        assert queue.put(make_message(subject))
    return queue, release


def test_stopping_a_queue_with_unsent_messages_logs_one_warning_with_the_count_and_the_types(caplog):
    queue, release = blocked_queue(['Validation de ton adresse e-mail', 'Validation de ton adresse e-mail', "Un code de secours a été utilisé"])
    caplog.set_level(logging.WARNING)
    queue.stop()
    release.set()
    lines = [r for r in caplog.records if r.levelno == logging.WARNING and r.name == 'app.mailer']
    assert len(lines) == 1
    text = lines[0].getMessage()
    assert '4' in text                                                                     # 1 en cours + 3 en attente
    assert 'Validation de ton adresse e-mail' in text and 'secours' in text


def test_the_line_holds_no_address_link_or_token(caplog):
    queue, release = blocked_queue(['Autre sujet'])
    caplog.set_level(logging.DEBUG)
    queue.stop()
    release.set()
    for record in caplog.records:
        text = record.getMessage()
        assert 'destinataire@example.org' not in text and 'secret.example' not in text and 'jeton-secret' not in text
    assert 'jeton-secret' not in caplog.text


def test_an_empty_queue_logs_nothing_at_stop(caplog):
    queue = MailQueue(lambda m: None)
    queue.start()
    queue.put(make_message('Envoyé'))
    deadline = time.monotonic() + 5
    while queue.pending() and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.2)
    caplog.set_level(logging.WARNING)
    queue.stop()
    assert [r for r in caplog.records if r.name == 'app.mailer'] == []


def test_a_never_started_queue_logs_nothing_and_stop_twice_is_harmless(caplog):
    queue = MailQueue(lambda m: None)
    caplog.set_level(logging.WARNING)
    queue.stop()
    queue.stop()
    assert caplog.records == []


def test_deferred_jobs_are_counted_with_a_generic_type_and_no_detail(caplog):
    release, started = threading.Event(), threading.Event()
    queue = MailQueue(lambda m: None)
    queue.start()
    queue.put(lambda: (started.set(), release.wait(30)))
    assert started.wait(5)
    queue.put(lambda: None)
    queue.put(lambda: None)
    caplog.set_level(logging.WARNING)
    queue.stop()
    release.set()
    lines = [r.getMessage() for r in caplog.records if r.name == 'app.mailer']
    assert len(lines) == 1 and '3' in lines[0] and 'lambda' not in lines[0] and '<function' not in lines[0]


def test_two_stops_in_a_row_log_the_loss_once(caplog):
    queue, release = blocked_queue(['Sujet'])
    caplog.set_level(logging.WARNING)
    queue.stop()
    queue.stop()
    release.set()
    assert len([r for r in caplog.records if r.name == 'app.mailer']) == 1


def test_the_application_registers_the_queue_stop_for_the_process_shutdown(tmp_path, smtp):
    """L'arrêt du processus doit appeler stop() (sinon la ligne de perte ne sortirait jamais en production)."""
    import atexit
    from unittest import mock
    with mock.patch.object(atexit, 'register') as register:
        app = make_app(tmp_path, OUTBOUND_ALLOWED_HOSTS=f'smtp=127.0.0.1:{smtp.port}')
    mail_queue = app.extensions['mailer'].queue
    registered = [c.args[0] for c in register.call_args_list if c.args]
    assert any(getattr(f, '__self__', None) in (mail_queue, app.extensions['mailer']) and f.__name__ == 'stop' for f in registered), \
        "aucun crochet d'arrêt (atexit) n'appelle la file d'envoi : la ligne de perte ne sortirait jamais"


# ===== CA-R38 / E-24 : une trace à 1 point est contrôlée comme à 2 points ======================================================

class LoopProvider(FakeProvider):
    """
    Boucle native CIRCULAIRE dont le point le plus proche de P1 est à `gap_m` mètres (cercle tangent, centre au nord) ; `cut` : part de la
    boucle supprimée à la fin (la boucle ne se referme plus). `gaps` : une valeur par essai (la dernière est répétée).
    """

    def __init__(self, gaps=(0.0,), cut=0.0, **kw):
        super().__init__(**kw)
        self.gaps, self.cut = list(gaps), cut

    def round_trip(self, start, profile, distance_m, seed, deadline):
        import math
        from app.services.routing.base import RouteResult
        from tests.routing_fakes import length_m
        gap = self.gaps.pop(0) if len(self.gaps) > 1 else self.gaps[0]
        self._enter('round_trip', deadline, start, profile, distance_m, seed)
        radius = distance_m / (2 * math.pi)
        per_degree = 111_194.9
        centre_lat = start[0] + (gap + radius) / per_degree
        cos_lat = math.cos(math.radians(start[0]))
        count = 240
        ring = [(centre_lat - math.cos(2 * math.pi * k / count) * radius / per_degree,
                 start[1] + math.sin(2 * math.pi * k / count) * radius / (per_degree * cos_lat)) for k in range(count + 1)]
        ring[-1] = ring[0]
        if self.cut:
            ring = ring[:int(len(ring) * (1 - self.cut))]
        return RouteResult(points=ring, distance_m=length_m(ring))


@pytest.fixture
def one_point_world(tmp_path):
    made = []

    def build(provider):
        w = build_world(tmp_path / f'w{len(made)}', provider)
        made.append(w)
        return w
    yield build
    for w in made:
        w.conn.close()


def route_one(w, **fields):
    body = {'points': POINTS[:1], 'activity': 'run', 'distance_km': 10, 'seed': 1}
    body.update(fields)
    return w.post(w.a, 'route', body)


FAR = "Le point 1 est trop loin d'une voie praticable : rapproche-le d'un chemin."
NONE = "Aucune trace possible avec ces points : essaie d'autres points."


def test_a_loop_starting_more_than_the_snap_tolerance_from_the_point_is_refused_with_the_far_message(one_point_world):
    w = one_point_world(LoopProvider(gaps=[120.0]))
    r = route_one(w)
    assert r.status_code == 422 and r.get_json()['error'] == FAR
    assert len([c for c in w.provider.calls if c[0] == 'round_trip']) >= 1


def test_a_loop_that_does_not_close_is_refused_with_the_no_trace_message(one_point_world):
    w = one_point_world(LoopProvider(cut=0.08))
    r = route_one(w)
    assert r.status_code == 422 and r.get_json()['error'] == NONE


def test_a_loop_starting_at_30_m_and_closed_is_accepted_with_its_metrics(one_point_world):
    w = one_point_world(LoopProvider(gaps=[30.0]))
    r = route_one(w)
    assert r.status_code == 200, r.get_data()
    meta = r.get_json()['route_meta']
    assert meta['closure_m'] is not None and meta['closure_m'] <= 50 and meta['overlap_pct'] is not None


@pytest.mark.parametrize('shift,accepted', [(0.0, True), (49.0, True), (51.0, False), (150.0, False)])
def test_the_tolerance_boundary_is_the_snap_tolerance_of_50_m(one_point_world, shift, accepted):
    w = one_point_world(LoopProvider(gaps=[shift]))
    assert (route_one(w).status_code == 200) is accepted


def test_the_next_seed_is_tried_when_the_first_loop_is_invalid(one_point_world):
    w = one_point_world(LoopProvider(gaps=[200.0, 10.0]))
    r = route_one(w)
    assert r.status_code == 200 and len([c for c in w.provider.calls if c[0] == 'round_trip']) == 2


def test_every_seed_invalid_gives_the_far_message_and_stays_within_the_call_budget(one_point_world):
    w = one_point_world(LoopProvider(gaps=[200.0]))
    r = route_one(w)
    assert r.status_code == 422 and r.get_json()['error'] == FAR
    assert len(w.provider.calls) <= 15                                                   # budget d'appels (15 par génération)


def test_a_refused_one_point_trace_stores_no_result_and_the_message_names_no_internal_detail(one_point_world):
    w = one_point_world(LoopProvider(gaps=[300.0]))
    r = route_one(w)
    body = r.get_data(as_text=True)
    assert r.status_code == 422 and 'Traceback' not in body and 'round_trip' not in body and 'seed' not in body.lower()


def test_two_points_keep_their_own_behaviour(one_point_world):
    w = one_point_world(FakeProvider(sinuosity=0.05))
    r = w.post(w.a, 'route', {'points': POINTS[:2], 'activity': 'run', 'distance_km': 10, 'seed': 1})
    assert r.status_code == 200


# ===== E-13 : reproductibilité de la Public Suffix List embarquée ============================================================

PSL_DIR = WEB_APP / 'app' / 'auth' / 'publicsuffix_data'
PSL_SCRIPT = WEB_APP / 'scripts' / 'build_public_suffix_list.py'


def licence_text():
    source = (PSL_DIR / 'public_suffix_list.SOURCE').read_text(encoding='utf-8')
    return source.split('AVIS DE LICENCE (MPL-2.0)\n-------------------------\n', 1)[1]


def run_script(tmp_path, *args):
    return subprocess.run([sys.executable, '-I', str(PSL_SCRIPT), *args, '--out-dir', str(tmp_path / 'out')], capture_output=True, text=True,
                          timeout=120)


def test_the_embedded_list_matches_the_source_file_sha256_and_size():
    source = (PSL_DIR / 'public_suffix_list.SOURCE').read_text(encoding='utf-8')
    data = (PSL_DIR / 'public_suffix_list.dat').read_bytes()
    declared = re.search(r'SHA-256\s*:\s*([0-9a-f]{64})\s+\((\d+) octets\)', source)
    assert declared and hashlib.sha256(data).hexdigest() == declared.group(1) and len(data) == int(declared.group(2))
    assert b'===BEGIN ICANN DOMAINS===' in data and b'===BEGIN PRIVATE DOMAINS===' in data       # les deux sections


def test_regenerating_from_the_shipped_inputs_gives_identical_bytes_without_any_network(tmp_path):
    (tmp_path / 'licence.txt').write_bytes(licence_text().encode('utf-8'))
    result = run_script(tmp_path, str(PSL_DIR / 'public_suffix_list.dat'), str(tmp_path / 'licence.txt'))
    assert result.returncode == 0, result.stderr
    assert (tmp_path / 'out' / 'public_suffix_list.dat').read_bytes() == (PSL_DIR / 'public_suffix_list.dat').read_bytes()
    assert (tmp_path / 'out' / 'public_suffix_list.SOURCE').read_bytes() == (PSL_DIR / 'public_suffix_list.SOURCE').read_bytes()


def test_the_licence_notice_in_source_has_the_sha256_declared_by_the_script():
    sha = re.search(r'LICENSE_SHA256 = "([0-9a-f]{64})"', PSL_SCRIPT.read_text(encoding='utf-8')).group(1)
    assert hashlib.sha256(licence_text().encode('utf-8')).hexdigest() == sha
    assert 'Mozilla Public License Version 2.0' in licence_text()


@pytest.mark.parametrize('mutation', ['flip', 'append', 'empty', 'truncate'])
def test_a_different_source_stops_the_script_and_writes_nothing(tmp_path, mutation):
    data = (PSL_DIR / 'public_suffix_list.dat').read_bytes()
    changed = {'flip': data[:100] + bytes([data[100] ^ 1]) + data[101:], 'append': data + b'\nevil.example\n', 'empty': b'', 'truncate': data[:-10]}[mutation]
    (tmp_path / 'list.dat').write_bytes(changed)
    (tmp_path / 'licence.txt').write_bytes(licence_text().encode('utf-8'))
    result = run_script(tmp_path, str(tmp_path / 'list.dat'), str(tmp_path / 'licence.txt'))
    assert result.returncode == 1 and 'SHA-256' in result.stderr
    assert not (tmp_path / 'out').exists()


def test_a_different_licence_file_stops_the_script_and_writes_nothing(tmp_path):
    (tmp_path / 'licence.txt').write_bytes(licence_text().encode('utf-8') + b'x')
    result = run_script(tmp_path, str(PSL_DIR / 'public_suffix_list.dat'), str(tmp_path / 'licence.txt'))
    assert result.returncode == 1 and not (tmp_path / 'out').exists()


def test_a_missing_input_file_is_a_clean_failure(tmp_path):
    result = run_script(tmp_path, str(tmp_path / 'absente.dat'), str(tmp_path / 'absente.txt'))
    assert result.returncode == 1 and 'Traceback' not in result.stderr


def test_the_script_never_opens_the_network_when_given_both_files(tmp_path):
    (tmp_path / 'licence.txt').write_bytes(licence_text().encode('utf-8'))
    guard = tmp_path / 'sitecustomize.py'
    guard.write_text("import socket\n\ndef _no(*a, **k):\n    raise SystemExit('RESEAU INTERDIT')\nsocket.socket.connect = _no\nsocket.create_connection = _no\n", encoding='utf-8')
    done = subprocess.run([sys.executable, '-c', f"import sys; sys.path.insert(0, {str(tmp_path)!r}); import sitecustomize; import runpy; sys.argv = ['x', {str(PSL_DIR / 'public_suffix_list.dat')!r}, {str(tmp_path / 'licence.txt')!r}, '--out-dir', {str(tmp_path / 'out')!r}]; runpy.run_path({str(PSL_SCRIPT)!r}, run_name='__main__')"],
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0 and 'RESEAU INTERDIT' not in done.stderr + done.stdout


def test_the_script_is_standard_library_only():
    source = PSL_SCRIPT.read_text(encoding='utf-8')
    imports = set(re.findall(r'^(?:from|import)\s+(\w+)', source, re.M))
    assert imports <= {'hashlib', 'sys', 'urllib', 'pathlib', '__future__'}, imports
