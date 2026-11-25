"""Second facteur TOTP, codes de secours, enrôlement, fin d'amorçage (T11 : EF-0.3, EF-2.2…2.6, EF-5.7, CA-8, CA-9, CA-13, CA-27, P-T11-1…6)."""

import re
import threading
import time

import pyotp
import pytest
from cryptography.fernet import Fernet

from app import create_app, db as database
from app.audit import Actor
from tests.conftest import force_setting
from app.auth import factors, passwords, second_factor, tokens
from app.auth.pending import PendingStore
from app.config import TestingConfig
from app.ratelimit import Limiters, Limits
from app.secrets_store import SecretsStore

KEY = Fernet.generate_key().decode()
HOST = 'traces.example.com'
KW = {'base_url': f'https://{HOST}'}
CLI = Actor.cli()
GOOD = 'cheval-agrafe-lune-42'
HASH = passwords.hash_password(GOOD)


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


def add(conn, name, *, admin=False, active=True):
    conn.execute("INSERT INTO users (sub, username, is_admin, is_active, auth_method, password_hash, session_token) "
                 "VALUES (?, ?, ?, ?, 'password', ?, 'tok')", (f'pwd:{name}', name, int(admin), int(active), HASH))
    conn.commit()
    return conn.execute("SELECT * FROM users WHERE username = ?", (name,)).fetchone()


def csrf_of(client, path):
    html = client.get(path, **KW).get_data(as_text=True)
    return re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)


def post(client, path, payload, csrf=None):
    return client.post(path, json=payload, **KW,
                       headers={'X-CSRF-Token': csrf or csrf_of(client, path), 'Origin': f'https://{HOST}'})


def login(client, name='alice', password=GOOD):
    return post(client, '/auth/password/login', {'username': name, 'password': password}, csrf_of(client, '/auth/password/login'))


def pending_state(app, client):
    with client.session_transaction(**KW) as s:
        return app.extensions['pending'].get(s.get('pending_id'))


def enroll(app, conn, client, user_id, *, grant=False):
    """Pose directement un état d'enrôlement valide ; retourne (secret, état)."""
    row = conn.execute("SELECT session_token, password_changed_at FROM users WHERE id = ?", (user_id,)).fetchone()
    pid = app.extensions['pending'].create(user_id, 'enroll', admin_grant=grant, binding=(row[0], row[1]))
    with client.session_transaction(**KW) as s:
        s['pending_id'] = pid
    assert client.get('/auth/password/enroll', **KW).status_code == 200
    return app.extensions['pending'].get(pid).extra['totp_secret'], app.extensions['pending'].get(pid)


def code_for(secret, offset=0):
    return pyotp.TOTP(secret).at(time.time() + 30 * offset)


def activate(app, conn, user_id, *, grant=False):
    """Enrôle un TOTP pour le compte (par la route) ; retourne (secret, codes de secours)."""
    client = app.test_client()
    secret, _ = enroll(app, conn, client, user_id, grant=grant)
    r = post(client, '/auth/password/enroll', {'code': code_for(secret)})
    assert r.status_code == 200, r.get_data()
    return secret, r.get_json()['recovery_codes']


# --- TOTP : fenêtre, rejeu, chiffrement ----------------------------------------------------------------------------------------

def test_the_window_is_plus_or_minus_one_step():
    secret = second_factor.new_secret()
    now = 1_700_000_000.0
    totp = pyotp.TOTP(secret)
    for offset, ok in ((-2, False), (-1, True), (0, True), (1, True), (2, False)):
        assert (second_factor.matching_step(secret, totp.at(now + 30 * offset), now) is not None) is ok, offset


def test_the_secret_is_160_bits_base32_and_the_uri_is_exactly_the_specified_one():
    secret = second_factor.new_secret()
    assert len(secret) == 32 and set(secret) <= set('ABCDEFGHIJKLMNOPQRSTUVWXYZ234567')
    assert second_factor.provisioning_uri(secret, 'alice') == (
        f'otpauth://totp/KmlViewer:alice?secret={secret}&issuer=KmlViewer&algorithm=SHA1&digits=6&period=30')
    assert second_factor.grouped('ABCDEFGH') == 'ABCD EFGH'
    svg = second_factor.qr_svg(second_factor.provisioning_uri(secret, 'alice'))
    assert svg.startswith('<svg') and 'style' not in svg and '<script' not in svg and 'http' not in svg


@pytest.mark.parametrize('code', ['12345', '1234567', 'abcdef', '12 3456x', '١٢٣٤٥٦', '', None, 123456, ['123456'], '１２３４５６'])
def test_only_six_ascii_digits_are_a_totp_code(code):
    assert second_factor.normalize_totp(code) is None


def test_a_code_is_accepted_once_and_older_or_equal_steps_are_refused(app, conn):
    user = add(conn, 'alice')
    secret, _ = activate(app, conn, user['id'])
    store = app.extensions['secrets']
    now = time.time()
    first = code_for(secret, 1)                                           # pas courant + 1 : accepté
    assert second_factor.verify_totp(conn, store, user['id'], first, now=now)
    assert not second_factor.verify_totp(conn, store, user['id'], first, now=now)        # rejeu
    assert not second_factor.verify_totp(conn, store, user['id'], code_for(secret, 0), now=now)     # pas antérieur
    assert second_factor.verify_totp(conn, store, user['id'], code_for(secret, 2), now=now + 30)    # pas postérieur : accepté


def test_two_simultaneous_submissions_of_the_same_code_let_exactly_one_through(app, conn):
    user = add(conn, 'alice')
    secret, _ = activate(app, conn, user['id'])
    store = app.extensions['secrets']
    code, results = code_for(secret, 1), []
    barrier = threading.Barrier(8)

    def attempt():
        c = database.connect(app.config['DB_PATH'])
        barrier.wait()
        results.append(second_factor.verify_totp(c, store, user['id'], code))
        c.close()

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count(True) == 1


def test_the_secret_is_encrypted_at_rest_and_unconfirmed_secrets_are_not_a_factor(app, conn):
    user = add(conn, 'alice')
    client = app.test_client()
    secret, _ = enroll(app, conn, client, user['id'])
    row = conn.execute("SELECT totp_secret, totp_enabled_at FROM users WHERE id = ?", (user['id'],)).fetchone()
    assert row['totp_secret'] is None and not factors.has_second_factor(conn, user['id'])      # jamais en base avant confirmation
    post(client, '/auth/password/enroll', {'code': code_for(secret)})
    row = conn.execute("SELECT totp_secret, totp_enabled_at FROM users WHERE id = ?", (user['id'],)).fetchone()
    assert row['totp_enabled_at'] and secret.encode() not in bytes(row['totp_secret'])
    assert app.extensions['secrets'].decrypt_value(row['totp_secret']).decode() == secret
    assert factors.has_second_factor(conn, user['id'])


# --- Codes de secours -----------------------------------------------------------------------------------------------------------

def test_recovery_codes_are_ten_unique_formatted_hashed_and_single_use(conn):
    user = add(conn, 'alice')
    codes = second_factor.replace_recovery_codes(conn, user['id'])
    conn.commit()
    assert len(codes) == 10 and len(set(codes)) == 10 and all(re.fullmatch(r'([A-Z2-7]{4}-){3}[A-Z2-7]{4}', c) for c in codes)
    stored = [r['code_hash'] for r in conn.execute("SELECT code_hash FROM recovery_codes")]
    assert len(stored) == 10 and not any(c.replace('-', '') in ' '.join(stored) for c in codes)
    assert second_factor.consume_recovery(conn, user['id'], codes[0].lower().replace('-', ' '))   # casse, espaces et tirets ignorés
    assert not second_factor.consume_recovery(conn, user['id'], codes[0])                          # usage unique
    assert second_factor.remaining_recovery(conn, user['id']) == 9
    for bad in ('', 'AAAA', None, 12, codes[1][:-1], codes[1] + 'A', 'ÀÀÀÀ-ÀÀÀÀ-ÀÀÀÀ-ÀÀÀÀ'):
        assert not second_factor.consume_recovery(conn, user['id'], bad)


def test_regenerating_the_codes_invalidates_the_old_ones_and_one_account_cannot_use_another_s(conn):
    a, b = add(conn, 'alice'), add(conn, 'bob')
    old = second_factor.replace_recovery_codes(conn, a['id'])
    new = second_factor.replace_recovery_codes(conn, a['id'])
    conn.commit()
    assert not second_factor.consume_recovery(conn, a['id'], old[0])
    assert not second_factor.consume_recovery(conn, b['id'], new[0])
    assert second_factor.consume_recovery(conn, a['id'], new[0])


def test_the_code_kind_is_told_by_its_shape():
    assert second_factor.kind_of('123456') == 'totp' and second_factor.kind_of('ABCD-EFGH-2345-6723') == 'recovery'
    assert second_factor.kind_of('abc') is None and second_factor.kind_of(None) is None


# --- Enrôlement : bootstrap complet ------------------------------------------------------------------------------------------------

def bootstrap(app, conn, client):
    new = tokens.create(conn, 'bootstrap', actor=CLI, email='first@example.org', is_admin_grant=True)
    r = post(client, '/auth/token/consume', {'token': new.token, 'username': 'founder', 'password': GOOD}, csrf_of(client, '/auth/token'))
    assert r.status_code == 200 and r.get_json()['next'] == '/auth/password/enroll'
    return conn.execute("SELECT * FROM users WHERE username = 'founder'").fetchone()


def test_the_bootstrap_ends_with_an_admin_having_one_factor_and_recovery_codes_and_no_session(app, conn):
    client = app.test_client()
    user = bootstrap(app, conn, client)
    assert user['is_admin'] == 0
    page = client.get('/auth/password/enroll', **KW).get_data(as_text=True)
    state = pending_state(app, client)
    assert '<svg' in page and second_factor.grouped(state.extra['totp_secret']) in page and state.admin_grant
    assert conn.execute("SELECT totp_secret FROM users WHERE id = ?", (user['id'],)).fetchone()[0] is None
    wrong = post(client, '/auth/password/enroll', {'code': '000000' if code_for(state.extra['totp_secret']) != '000000' else '111111'})
    assert wrong.status_code == 401 and wrong.get_json()['error'] == 'Code invalide ou déjà utilisé.'
    assert conn.execute("SELECT is_admin FROM users WHERE id = ?", (user['id'],)).fetchone()[0] == 0     # pas d'octroi sans facteur
    done = post(client, '/auth/password/enroll', {'code': code_for(state.extra['totp_secret'])})
    body = done.get_json()
    assert done.status_code == 200 and len(body['recovery_codes']) == 10 and body['next'] == '/auth/password/login'
    row = conn.execute("SELECT is_admin, totp_enabled_at FROM users WHERE id = ?", (user['id'],)).fetchone()
    assert row['is_admin'] == 1 and row['totp_enabled_at']
    actions = [r[0] for r in conn.execute("SELECT action FROM audit_log WHERE target_id = ? ORDER BY id", (str(user['id']),))]
    assert 'user.factor_added' in actions and 'user.admin_grant' in actions
    with client.session_transaction(**KW) as s:
        assert 'user_id' not in s and 'pending_id' not in s                                          # aucune session ouverte (r8)
    assert pending_state(app, client) is None
    assert post(client, '/auth/password/enroll', {'code': '123456'}, csrf_of(client, '/auth/password/login')).status_code in (302, 401)


def test_an_abandoned_enrolment_leaves_a_non_admin_account_without_factor(app, conn):
    client = app.test_client()
    user = bootstrap(app, conn, client)
    app.extensions['pending'].destroy_for_user(user['id'])                                          # échéance / abandon
    assert client.get('/auth/password/enroll', **KW).status_code == 302
    row = conn.execute("SELECT is_admin FROM users WHERE id = ?", (user['id'],)).fetchone()
    assert row['is_admin'] == 0 and not factors.has_second_factor(conn, user['id'])


def test_a_plain_enrolment_without_grant_makes_nobody_admin(app, conn):
    user = add(conn, 'alice')
    activate(app, conn, user['id'])
    assert conn.execute("SELECT is_admin FROM users WHERE id = ?", (user['id'],)).fetchone()[0] == 0


def test_the_secret_stays_the_same_across_page_loads_and_is_never_in_the_cookie(app, conn):
    user = add(conn, 'alice')
    client = app.test_client()
    secret, _ = enroll(app, conn, client, user['id'])
    client.get('/auth/password/enroll', **KW)
    assert pending_state(app, client).extra['totp_secret'] == secret
    with client.session_transaction(**KW) as s:
        assert secret not in repr(dict(s)) and set(s) == {'pending_id'}


def test_the_enrolment_state_lasts_15_minutes_and_the_mfa_state_5():
    now = [0.0]
    store = PendingStore(clock=lambda: now[0])
    enrol, mfa = store.create(1, 'enroll'), store.create(1, 'mfa')
    now[0] = 299
    assert store.get(enrol) and store.get(mfa)
    now[0] = 301
    assert store.get(enrol) and store.get(mfa) is None
    now[0] = 899
    assert store.get(enrol)
    now[0] = 901
    assert store.get(enrol) is None


def test_five_wrong_codes_destroy_the_enrolment_state_and_the_account_limiter_applies(app, conn):
    user = add(conn, 'alice')
    client = app.test_client()
    secret, _ = enroll(app, conn, client, user['id'])
    token = csrf_of(client, '/auth/password/enroll')
    codes = [post(client, '/auth/password/enroll', {'code': '000000'}, token).status_code for _ in range(5)]
    assert codes[:4] == [401] * 4 and codes[4] == 401 and pending_state(app, client) is None


def test_confirmation_is_atomic_two_simultaneous_confirmations_create_one_factor(app, conn):
    user = add(conn, 'alice')
    clients = [app.test_client(), app.test_client()]
    row = conn.execute("SELECT session_token, password_changed_at FROM users WHERE id = ?", (user['id'],)).fetchone()
    pid = app.extensions['pending'].create(user['id'], 'enroll', binding=(row[0], row[1]))
    for c in clients:
        with c.session_transaction(**KW) as s:
            s['pending_id'] = pid
    assert clients[0].get('/auth/password/enroll', **KW).status_code == 200
    secret = app.extensions['pending'].get(pid).extra['totp_secret']
    tokens_ = [csrf_of(c, '/auth/password/login') for c in clients]
    results = []
    barrier = threading.Barrier(2)

    def run(c, t):
        barrier.wait()
        results.append(post(c, '/auth/password/enroll', {'code': code_for(secret)}, t).status_code)

    threads = [threading.Thread(target=run, args=(c, t)) for c, t in zip(clients, tokens_)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count(200) == 1
    assert conn.execute("SELECT COUNT(*) FROM recovery_codes WHERE user_id = ?", (user['id'],)).fetchone()[0] == 10


# --- Connexion : seconde étape ---------------------------------------------------------------------------------------------------

def enrolled(app, conn, name='alice', admin=False):
    user = add(conn, name, admin=admin)
    secret, codes = activate(app, conn, user['id'])
    return user, secret, codes


def test_login_then_totp_opens_a_full_session_and_the_enrolment_code_cannot_be_replayed(app, conn):
    user, secret, _ = enrolled(app, conn)
    client = app.test_client()
    r = login(client)
    assert r.get_json()['step'] == 'mfa' and r.get_json()['next'] == '/auth/password/mfa'
    assert client.get('/auth/password/mfa', **KW).status_code == 200
    token = csrf_of(client, '/auth/password/mfa')
    assert post(client, '/auth/password/mfa', {'code': code_for(secret)}, token).status_code == 401       # même pas que l'enrôlement
    ok = post(client, '/auth/password/mfa', {'code': code_for(secret, 1)}, token)
    assert ok.status_code == 200 and ok.get_json()['next'] == '/'
    with client.session_transaction(**KW) as s:
        assert s['user_id'] == user['id'] and 'pending_id' not in s
    assert client.get('/api/activities', **KW).status_code == 200


def test_a_replayed_cookie_cannot_pass_the_second_step_twice(app, conn):
    user, secret, _ = enrolled(app, conn)
    client = app.test_client()
    login(client)
    with client.session_transaction(**KW) as s:
        saved = s['pending_id']
    token = csrf_of(client, '/auth/password/mfa')
    assert post(client, '/auth/password/mfa', {'code': code_for(secret, 1)}, token).status_code == 200
    other = app.test_client()
    with other.session_transaction(**KW) as s:
        s['pending_id'] = saved
    assert other.get('/auth/password/mfa', **KW).status_code == 302
    assert other.get('/api/activities', **KW).status_code in (401, 302)


def test_five_wrong_codes_end_the_attempt_and_a_replayed_cookie_does_not_reset_the_counter(app, conn):
    user, secret, _ = enrolled(app, conn)
    app.extensions['limiters'] = Limiters(clock=lambda: 0.0)                 # l'enrôlement a déjà consommé une tentative du compte
    client = app.test_client()
    login(client)
    token = csrf_of(client, '/auth/password/mfa')
    answers = [post(client, '/auth/password/mfa', {'code': '000001'}, token) for _ in range(5)]
    assert [a.status_code for a in answers] == [401] * 5 and answers[-1].get_json().get('restart') is True
    assert all(a.get_json()['error'] == 'Code invalide ou déjà utilisé.' for a in answers[:4])
    assert client.get('/auth/password/mfa', **KW).status_code == 302
    assert client.get('/api/activities', **KW).status_code in (401, 302)


def test_the_account_limiter_runs_before_the_verification_and_answers_a_generic_429(app, conn):
    user, secret, _ = enrolled(app, conn)
    app.extensions['limiters'] = Limiters(limits=Limits(mfa_per_account=3, password_per_identifier=20, password_per_ip=100), clock=lambda: 0.0)
    statuses = []
    for _ in range(5):
        client = app.test_client()
        login(client)
        statuses.append(post(client, '/auth/password/mfa', {'code': '000001'}, csrf_of(client, '/auth/password/mfa')).status_code)
    assert statuses == [401, 401, 401, 429, 429]
    client = app.test_client()
    login(client)
    assert post(client, '/auth/password/mfa', {'code': code_for(secret, 1)}, csrf_of(client, '/auth/password/mfa')).status_code == 429


def test_a_recovery_code_logs_in_once_and_is_audited(app, conn):
    user, _, codes = enrolled(app, conn)
    client = app.test_client()
    login(client)
    ok = post(client, '/auth/password/mfa', {'code': codes[0].lower()}, csrf_of(client, '/auth/password/mfa'))
    assert ok.status_code == 200
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'user.recovery_used'").fetchone()[0] == 1
    client = app.test_client()
    login(client)
    assert post(client, '/auth/password/mfa', {'code': codes[0]}, csrf_of(client, '/auth/password/mfa')).status_code == 401


@pytest.mark.parametrize('payload', [{}, {'code': None}, {'code': 123456}, {'code': ['123456']}, {'code': 'x' * 100}, {'code': '\ud800'},
                                     {'code': ''}, {'cod': '123456'}])
def test_malformed_codes_are_plain_failures_never_errors(app, conn, payload):
    enrolled(app, conn)
    client = app.test_client()
    login(client)
    r = client.post('/auth/password/mfa', data=__import__('json').dumps(payload, ensure_ascii=True), content_type='application/json',
                    **KW, headers={'X-CSRF-Token': csrf_of(client, '/auth/password/mfa'), 'Origin': f'https://{HOST}'})
    assert r.status_code in (400, 401)


def test_the_second_step_needs_csrf_and_a_pending_state(app, conn):
    enrolled(app, conn)
    anonymous = app.test_client()
    assert anonymous.get('/auth/password/mfa', **KW).status_code == 302
    assert post(anonymous, '/auth/password/mfa', {'code': '123456'}, csrf_of(anonymous, '/auth/password/login')).status_code == 302
    client = app.test_client()
    login(client)
    assert client.post('/auth/password/mfa', json={'code': '123456'}, **KW, headers={'Origin': f'https://{HOST}'}).status_code == 400


def test_an_admin_without_factor_is_sent_to_enrolment_and_policy_all_applies_to_everyone(app, conn):
    add(conn, 'root', admin=True)
    add(conn, 'alice')
    assert login(app.test_client(), 'root').get_json()['step'] == 'enroll'
    assert login(app.test_client(), 'alice').get_json().get('step') is None
    force_setting(conn, app.config['SETTINGS_STORE'], 'auth.mfa_policy', 'all')
    assert login(app.test_client(), 'alice').get_json()['step'] == 'enroll'


# --- Reprise d'amorçage d'un compte existant (EF-0.3b) --------------------------------------------------------------------------

def test_resuming_a_bootstrap_asks_for_the_current_password_and_keeps_it(app, conn):
    user = add(conn, 'alice')
    new = tokens.create(conn, 'bootstrap', actor=CLI, target_user_id=user['id'], is_admin_grant=True)
    client = app.test_client()
    assert tokens.inspect(conn, new.token) == 'confirm_password'
    csrf = csrf_of(client, '/auth/token')
    bad = post(client, '/auth/token/consume', {'token': new.token, 'password': 'mauvais-mot-de-passe-1'}, csrf)
    assert bad.status_code == 400 and tokens.inspect(conn, new.token) == 'confirm_password'      # jeton intact
    good = post(client, '/auth/token/consume', {'token': new.token, 'password': GOOD}, csrf)
    assert good.status_code == 200 and good.get_json()['next'] == '/auth/password/enroll'
    assert tokens.inspect(conn, new.token) is None
    assert conn.execute("SELECT password_hash FROM users WHERE id = ?", (user['id'],)).fetchone()[0] == user['password_hash']
    state = pending_state(app, client)
    assert state.admin_grant and state.user_id == user['id']
    with client.session_transaction(**KW) as s:
        assert 'user_id' not in s


def test_resuming_is_refused_for_an_account_that_got_a_factor_or_was_disabled_meanwhile(app, conn):
    for change in ("UPDATE users SET is_active = 0", "UPDATE users SET auth_method = 'oidc'"):
        user = add(conn, f'u{abs(hash(change)) % 1000}')
        new = tokens.create(conn, 'bootstrap', actor=CLI, target_user_id=user['id'], is_admin_grant=True)
        conn.execute(change + " WHERE id = ?", (user['id'],))
        conn.commit()
        client = app.test_client()
        r = post(client, '/auth/token/consume', {'token': new.token, 'password': GOOD}, csrf_of(client, '/auth/token'))
        assert r.status_code == 400 and 'next' not in r.get_json()
    holder = add(conn, 'holder')
    activate(app, conn, holder['id'])
    new = tokens.create(conn, 'bootstrap', actor=CLI, target_user_id=holder['id'], is_admin_grant=True)
    client = app.test_client()
    assert post(client, '/auth/token/consume', {'token': new.token, 'password': GOOD}, csrf_of(client, '/auth/token')).status_code == 400


def test_the_resume_form_is_limited_before_any_hashing(app, conn, monkeypatch):
    user = add(conn, 'alice')
    new = tokens.create(conn, 'bootstrap', actor=CLI, target_user_id=user['id'], is_admin_grant=True)
    app.extensions['limiters'] = Limiters(limits=Limits(password_per_identifier=3, password_per_ip=100), clock=lambda: 0.0)
    client = app.test_client()
    csrf = csrf_of(client, '/auth/token')
    assert [post(client, '/auth/token/consume', {'token': new.token, 'password': 'faux-mot-de-passe-12'}, csrf).status_code
            for _ in range(3)] == [400, 400, 400]

    class Gate:
        calls = 0

        def slot(self):
            Gate.calls += 1
            raise passwords.HashBusy()

    monkeypatch.setattr(passwords, '_gate', Gate())
    r = post(client, '/auth/token/consume', {'token': new.token, 'password': GOOD}, csrf)
    assert r.status_code == 429 and Gate.calls == 0


# --- secrets-reset (EF-5.7, CA-27) ----------------------------------------------------------------------------------------------

def test_secrets_reset_demotes_admins_left_without_factor_and_invalidates_their_codes(app, conn):
    admin, _, codes = enrolled(app, conn, 'root', admin=True)
    conn.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (admin['id'],))
    other = add(conn, 'viewer')
    conn.commit()
    counts = app.extensions['secrets'].reset(conn, actor=CLI)
    assert counts.get('demoted') == 1
    row = conn.execute("SELECT is_admin, session_token, totp_secret FROM users WHERE id = ?", (admin['id'],)).fetchone()
    assert row['is_admin'] == 0 and row['totp_secret'] is None and row['session_token'] != 'tok'
    assert conn.execute("SELECT COUNT(*) FROM recovery_codes").fetchone()[0] == 0
    assert not second_factor.consume_recovery(conn, admin['id'], codes[0])
    assert conn.execute("SELECT is_admin FROM users WHERE id = ?", (other['id'],)).fetchone()[0] == 0


def test_the_store_encrypts_and_decrypts_totp_values_and_never_leaks_the_detail():
    store = SecretsStore(KEY)
    token = store.encrypt_value(b'ABC')
    assert b'ABC' not in token and store.decrypt_value(token) == b'ABC'
    other = SecretsStore(Fernet.generate_key().decode())
    with pytest.raises(Exception) as exc:
        other.decrypt_value(token)
    assert 'ABC' not in str(exc.value)


# --- Gestion depuis le profil (P-T11-7) -----------------------------------------------------------------------------------------

def session_client(app, conn, user, *, mfa_secret=None):
    """Client avec une session complète du compte."""
    client = app.test_client()
    with client.session_transaction(**KW) as s:
        s['user_id'], s['token'], s['login_at'] = user['id'], user['session_token'], int(time.time())
    return client


def api(client, method, path, payload=None):
    return client.open(path, method=method, json=payload if payload is not None else {}, **KW, headers={'Origin': f'https://{HOST}'})


def test_profile_setup_requires_the_current_password_and_confirmation_activates_totp(app, conn):
    user = add(conn, 'alice')
    client = session_client(app, conn, user)
    assert api(client, 'GET', '/api/me/mfa').get_json() == {'success': True, 'totp': False, 'keys': 0, 'key_list': [], 'webauthn_available': True,
                                                                       'recovery_codes_remaining': 0}
    assert api(client, 'POST', '/api/me/mfa/totp/setup', {}).status_code == 400
    assert api(client, 'POST', '/api/me/mfa/totp/setup', {'password': 'mauvais-mot-de-passe-1'}).status_code == 403
    setup = api(client, 'POST', '/api/me/mfa/totp/setup', {'password': GOOD}).get_json()
    assert '<svg' in setup['qr_svg'] and setup['uri'].startswith('otpauth://totp/KmlViewer:alice?secret=')
    assert conn.execute("SELECT totp_secret FROM users WHERE id = ?", (user['id'],)).fetchone()[0] is None      # pas en base avant confirmation
    secret = setup['uri'].split('secret=')[1].split('&')[0]
    assert api(client, 'POST', '/api/me/mfa/totp/confirm', {'code': '000000'}).status_code == 403
    done = api(client, 'POST', '/api/me/mfa/totp/confirm', {'code': code_for(secret)})
    assert done.status_code == 200 and len(done.get_json()['recovery_codes']) == 10
    assert api(client, 'GET', '/api/me/mfa').get_json()['totp'] is True
    assert api(client, 'POST', '/api/me/mfa/totp/setup', {'password': GOOD}).status_code == 409           # déjà actif


def test_confirming_without_a_setup_or_from_another_session_is_refused(app, conn):
    user = add(conn, 'alice')
    first = session_client(app, conn, user)
    assert api(first, 'POST', '/api/me/mfa/totp/confirm', {'code': '123456'}).status_code == 409
    secret = api(first, 'POST', '/api/me/mfa/totp/setup', {'password': GOOD}).get_json()['uri'].split('secret=')[1].split('&')[0]
    conn.execute("UPDATE users SET session_token = 'autre' WHERE id = ?", (user['id'],))
    conn.commit()
    other = session_client(app, conn, conn.execute("SELECT * FROM users WHERE id = ?", (user['id'],)).fetchone())
    assert api(other, 'POST', '/api/me/mfa/totp/confirm', {'code': code_for(secret)}).status_code == 409   # lié au jeton de session


def test_removing_totp_needs_password_and_a_valid_code_and_an_admin_keeps_one_factor(app, conn):
    user = add(conn, 'alice')
    secret, _ = activate(app, conn, user['id'])
    client = session_client(app, conn, user)
    assert api(client, 'DELETE', '/api/me/mfa/totp', {'password': GOOD}).status_code == 403                    # code manquant
    assert api(client, 'DELETE', '/api/me/mfa/totp', {'password': 'mauvais-mot-de-passe-1', 'code': code_for(secret, 1)}).status_code == 403
    ok = api(client, 'DELETE', '/api/me/mfa/totp', {'password': GOOD, 'code': code_for(secret, 1)})
    assert ok.status_code == 200 and not factors.has_second_factor(conn, user['id'])
    assert conn.execute("SELECT COUNT(*) FROM recovery_codes WHERE user_id = ?", (user['id'],)).fetchone()[0] == 0   # codes invalidés (r6)
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'user.factor_removed'").fetchone()[0] == 1
    admin = add(conn, 'root')
    asecret, _ = activate(app, conn, admin['id'], grant=True)
    conn.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (admin['id'],))
    conn.commit()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (admin['id'],)).fetchone()
    refused = api(session_client(app, conn, row), 'DELETE', '/api/me/mfa/totp', {'password': GOOD, 'code': code_for(asecret, 1)})
    assert refused.status_code == 409 and factors.has_second_factor(conn, admin['id'])


def test_regenerating_the_recovery_codes_needs_password_and_code_and_invalidates_the_old_ones(app, conn):
    user = add(conn, 'alice')
    secret, old = activate(app, conn, user['id'])
    client = session_client(app, conn, user)
    assert api(client, 'POST', '/api/me/mfa/recovery-codes', {'password': GOOD}).status_code == 403
    new = api(client, 'POST', '/api/me/mfa/recovery-codes', {'password': GOOD, 'code': code_for(secret, 1)}).get_json()['recovery_codes']
    assert len(new) == 10 and set(new).isdisjoint(old)
    assert not second_factor.consume_recovery(conn, user['id'], old[0]) and second_factor.consume_recovery(conn, user['id'], new[0])


def test_an_sso_account_has_nothing_to_manage_and_anonymous_requests_are_refused(app, conn):
    conn.execute("INSERT INTO users (sub, username, auth_method, session_token) VALUES ('idp', 'sso', 'oidc', 'tok')")
    conn.commit()
    row = conn.execute("SELECT * FROM users WHERE username = 'sso'").fetchone()
    client = app.test_client()
    with client.session_transaction(**KW) as s:
        s['user_id'], s['token'], s['login_at'] = row['id'], 'tok', int(time.time())
    assert api(client, 'GET', '/api/me/mfa').status_code in (401, 404)
    assert api(app.test_client(), 'GET', '/api/me/mfa').status_code == 401
    for path in ('/api/me/mfa/totp/setup', '/api/me/mfa/totp/confirm', '/api/me/mfa/recovery-codes'):
        assert api(app.test_client(), 'POST', path, {'password': GOOD}).status_code == 401


def test_the_password_check_of_the_profile_is_limited_before_hashing(app, conn, monkeypatch):
    user = add(conn, 'alice')
    app.extensions['limiters'] = Limiters(limits=Limits(password_per_identifier=3, password_per_ip=100), clock=lambda: 0.0)
    client = session_client(app, conn, user)
    assert [api(client, 'POST', '/api/me/mfa/totp/setup', {'password': 'mauvais-mot-de-passe-1'}).status_code for _ in range(3)] == [403] * 3

    class Gate:
        calls = 0

        def slot(self):
            Gate.calls += 1
            raise passwords.HashBusy()

    monkeypatch.setattr(passwords, '_gate', Gate())
    assert api(client, 'POST', '/api/me/mfa/totp/setup', {'password': GOOD}).status_code == 429 and Gate.calls == 0


# --- CA-13b : auth.mfa_policy = all refusé tant qu'un compte actif n'a pas de facteur ---------------------------------------------

def test_the_policy_all_is_refused_while_an_active_password_account_has_no_factor_CA_13b(app, conn):
    from app.settings import SettingsError
    store = app.config['SETTINGS_STORE']
    add(conn, 'alice')
    add(conn, 'bob')
    add(conn, 'off', active=False)                                              # un compte inactif ne compte pas
    with pytest.raises(SettingsError) as exc:
        store.set_many(conn, {'auth.mfa_policy': 'all'}, actor=CLI)
    assert '2 compte(s)' in str(exc.value) and exc.value.field == 'auth.mfa_policy'
    assert store.get('auth.mfa_policy') == 'admins'
    row = conn.execute("SELECT details FROM audit_log WHERE action = 'setting.refused'").fetchone()
    assert row and '"accounts": 2' in row[0]


def test_a_batch_containing_the_refused_policy_is_rejected_as_a_whole(app, conn):
    from app.settings import SettingsError
    store = app.config['SETTINGS_STORE']
    add(conn, 'alice')
    with pytest.raises(SettingsError):
        store.set_many(conn, {'routing.max_calls': 9, 'auth.mfa_policy': 'all'}, actor=CLI)
    assert store.get('routing.max_calls') == 15 and store.get('auth.mfa_policy') == 'admins'
    assert conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 0


def test_the_policy_all_is_accepted_once_every_active_account_has_a_factor_and_recovery_codes_do_not_count(app, conn):
    from app.settings import SettingsError
    store = app.config['SETTINGS_STORE']
    alice = add(conn, 'alice')
    second_factor.replace_recovery_codes(conn, alice['id'])
    conn.commit()
    with pytest.raises(SettingsError):                                          # des codes de secours seuls ne sont pas un facteur
        store.set_many(conn, {'auth.mfa_policy': 'all'}, actor=CLI)
    activate(app, conn, alice['id'])
    store.set_many(conn, {'auth.mfa_policy': 'all'}, actor=CLI)
    assert store.get('auth.mfa_policy') == 'all'
    store.set_many(conn, {'auth.mfa_policy': 'admins'}, actor=CLI)               # le retour est toujours permis


def test_the_api_answers_409_with_the_count(app, conn):
    add(conn, 'alice')
    admin = add(conn, 'root', admin=True)
    conn.execute("UPDATE users SET totp_secret = x'01', totp_enabled_at = '2026-01-01T00:00:00Z' WHERE id = ?", (admin['id'],))
    conn.commit()
    client = app.test_client()
    with client.session_transaction(**KW) as s:
        s['user_id'], s['token'], s['login_at'] = admin['id'], 'tok', int(time.time())
    r = client.patch('/api/admin/settings', json={'auth.mfa_policy': 'all'}, **KW, headers={'Origin': f'https://{HOST}'})
    assert r.status_code == 409 and '1 compte(s)' in r.get_json()['error']


# --- T11-1 / CA-14 : un changement de facteur ferme les autres sessions -------------------------------------------------------------

def test_adding_or_removing_a_totp_closes_the_other_sessions_and_keeps_the_current_one(app, conn):
    user = add(conn, 'alice')
    current, other = session_client(app, conn, user), session_client(app, conn, user)
    setup = api(current, 'POST', '/api/me/mfa/totp/setup', {'password': GOOD}).get_json()
    secret = setup['uri'].split('secret=')[1].split('&')[0]
    assert api(current, 'POST', '/api/me/mfa/totp/confirm', {'code': code_for(secret)}).status_code == 200
    assert api(current, 'GET', '/api/me/mfa').status_code == 200                       # la session courante est réémise
    assert api(other, 'GET', '/api/me/mfa').status_code == 401                         # les autres sont fermées
    third = session_client(app, conn, conn.execute("SELECT * FROM users WHERE id = ?", (user['id'],)).fetchone())
    ok = api(current, 'DELETE', '/api/me/mfa/totp', {'password': GOOD, 'code': code_for(secret, 1)})
    assert ok.status_code == 200
    assert api(current, 'GET', '/api/me/mfa').status_code == 200 and api(third, 'GET', '/api/me/mfa').status_code == 401


# --- Doc-3 / CA-54 : user-invite --admin --account <login> --reset-password -------------------------------------------------------

def _invite_link(app, *args):
    result = app.test_cli_runner().invoke(args=['user-invite', '--admin', *args])
    assert result.exit_code == 0, result.output
    return next(line for line in result.output.splitlines() if '/auth/token#t=' in line).split('#t=')[1].strip()


def test_reset_password_resume_replaces_the_forgotten_password_then_enrols_and_grants_admin_CA_54(app, conn):
    user = add(conn, 'alice')
    token = _invite_link(app, '--account', 'alice', '--reset-password')
    assert tokens.inspect(conn, token) == 'set_password'
    client = app.test_client()
    csrf = csrf_of(client, '/auth/token')
    assert post(client, '/auth/token/consume', {'token': token, 'password': 'court'}, csrf).status_code == 400    # politique : jeton intact
    assert tokens.inspect(conn, token) == 'set_password'
    old_session = session_client(app, conn, user)
    ok = post(client, '/auth/token/consume', {'token': token, 'password': 'un-autre-mot-de-passe-91'}, csrf)
    assert ok.status_code == 200 and ok.get_json()['next'] == '/auth/password/enroll'
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user['id'],)).fetchone()
    assert passwords.verify_password(row['password_hash'], 'un-autre-mot-de-passe-91')[0]
    assert not passwords.verify_password(row['password_hash'], GOOD)[0] and row['is_admin'] == 0
    assert api(old_session, 'GET', '/api/me/mfa').status_code == 401                       # sessions révoquées
    state = pending_state(app, client)
    assert state.admin_grant and state.user_id == user['id'] and tokens.inspect(conn, token) is None
    secret = state.extra.get('totp_secret') or (client.get('/auth/password/enroll', **KW) and pending_state(app, client).extra['totp_secret'])
    done = post(client, '/auth/password/enroll', {'code': code_for(secret)})
    assert done.status_code == 200 and conn.execute("SELECT is_admin FROM users WHERE id = ?", (user['id'],)).fetchone()[0] == 1


def test_reset_password_needs_an_account_and_an_eligible_one(app, conn):
    assert app.test_cli_runner().invoke(args=['user-invite', '--admin', 'x@example.org', '--reset-password']).exit_code != 0
    holder = add(conn, 'holder')
    activate(app, conn, holder['id'])
    assert app.test_cli_runner().invoke(args=['user-invite', '--admin', '--account', 'holder', '--reset-password']).exit_code != 0
    plain = add(conn, 'plain')
    token = _invite_link(app, '--account', 'plain')                                          # sans --reset-password : mot de passe actuel
    assert tokens.inspect(conn, token) == 'confirm_password' and plain
