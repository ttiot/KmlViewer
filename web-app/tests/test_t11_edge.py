"""
T11 (second facteur) et comptes (T13) en profondeur (rôle test), écrit depuis la spec (EF-0.3, EF-0.3b, EF-1.6, EF-1.7, EF-2.2, EF-2.3,
EF-2.5 à EF-2.7, EF-4.6, EF-4.7, P-T11-1 à 7, B5, M3), pas depuis le code.

 1. TOTP : normalisation, fenêtre ±1, anti-rejeu ATOMIQUE (concurrent), secret chiffré au repos, secret illisible
 2. codes de secours : forme, empreintes seules, usage unique atomique, régénération, liaison au compte
 3. connexion (étape mfa) : rejeu, essais, limiteur par compte, corps hostiles, CSRF, concurrence, session produite
 4. enrôlement (étape enroll) et octroi d'admin B5 à la fin ; reprise d'amorçage (--account, confirm_password)
 5. /api/me/mfa : mot de passe exigé, secret en mémoire, retrait, dernier facteur d'un admin
 6. /api/me/password et /api/me/email
 7. notifications de sécurité
"""

import json
import logging
import re
import time

import pytest

from app.audit import Actor
from app.auth import factors, passwords, second_factor, tokens
from tests.fake_smtp import FakeSMTP
from tests.pw_harness import (GOOD, HOST, KW, OTHER, FakeTime, add, call, code_at, concurrent, connect, cookie_payload, form_post, login,
                              make_app, mfa, new_ip, new_secret, raw_cookie, refresh, session_client)

CLI = Actor.cli()


@pytest.fixture
def app(tmp_path):
    return make_app(tmp_path)


@pytest.fixture
def conn(app):
    c = connect(app)
    yield c
    c.close()


@pytest.fixture
def store(app):
    return app.extensions['secrets']


# ===== 1. TOTP ==================================================================================================================

@pytest.mark.parametrize('code,expected', [
    ('123456', '123456'), ('123 456', '123456'), (' 123456 ', '123456'), ('12 34 56', '123456'),
    ('12345', None), ('1234567', None), ('', None), ('abcdef', None), ('12345a', None), ('１２３４５６', None), ('١٢٣٤٥٦', None),
    ('123456\n', None), ('\t123456', None), ('123-456', None), ('+12345', None), ('1e5000', None), ('0x1234', None),
    (123456, None), (None, None), (True, None), (['123456'], None), ({'a': 1}, None), (b'123456', None), (12.0, None),
])
def test_totp_code_normalisation_accepts_only_six_ascii_digits(code, expected):
    assert second_factor.normalize_totp(code) == expected


def test_the_window_is_exactly_plus_or_minus_one_step_at_every_boundary():
    secret = new_secret()
    base = 1_800_000_000 - (1_800_000_000 % 30)
    for delta in (0.0, 0.001, 14.9, 29.999):
        now = base + delta
        for offset, ok in ((-2, False), (-1, True), (0, True), (1, True), (2, False)):
            step = second_factor.matching_step(secret, code_at(secret, base, offset), now)
            assert (step is not None) == ok, (delta, offset)
            if ok:
                assert step == int(base // 30) + offset


def test_the_same_code_cannot_be_used_twice_and_an_earlier_step_cannot_follow_a_later_one(app, conn, store):
    secret = new_secret()
    user = add(app, conn, 'bob', secret=secret)
    now = time.time()
    current, previous = code_at(secret, now), code_at(secret, now, -1)
    assert second_factor.verify_totp(conn, store, user['id'], current, now=now) is True
    assert second_factor.verify_totp(conn, store, user['id'], current, now=now) is False       # rejeu
    assert second_factor.verify_totp(conn, store, user['id'], previous, now=now) is False      # pas antérieur au dernier accepté
    assert second_factor.verify_totp(conn, store, user['id'], code_at(secret, now, 1), now=now) is True
    assert refresh(conn, 'bob')['totp_last_step'] == int(now // 30) + 1


def test_a_future_code_blocks_the_current_one_by_design_but_only_until_the_step_catches_up(app, conn, store):
    secret = new_secret()
    user = add(app, conn, 'bob', secret=secret)
    now = time.time()
    assert second_factor.verify_totp(conn, store, user['id'], code_at(secret, now, 1), now=now) is True
    assert second_factor.verify_totp(conn, store, user['id'], code_at(secret, now, 0), now=now) is False
    assert second_factor.verify_totp(conn, store, user['id'], code_at(secret, now + 30, 1), now=now + 30) is True


@pytest.mark.parametrize('round_', range(8))
def test_concurrent_submissions_of_the_same_code_let_exactly_one_through(app, conn, store, round_):
    secret = new_secret()
    user = add(app, conn, 'bob', secret=secret)
    now = time.time()
    code = code_at(secret, now)

    def job(i):
        c = connect(app)
        try:
            return second_factor.verify_totp(c, store, user['id'], code, now=now)
        finally:
            c.close()
    results = concurrent(12, job)
    assert [r for r in results if isinstance(r, Exception)] == []
    assert results.count(True) == 1


def test_the_secret_is_encrypted_at_rest_and_never_in_clear_in_the_database_file(app, conn, store):
    secret = new_secret()
    user = add(app, conn, 'bob', secret=secret)
    raw = conn.execute("SELECT totp_secret FROM users WHERE id = ?", (user['id'],)).fetchone()[0]
    assert isinstance(raw, bytes) and secret.encode() not in raw and raw != secret.encode()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    with open(app.config['DB_PATH'], 'rb') as handle:
        assert secret.encode() not in handle.read()
    assert store.decrypt_value(raw).decode() == secret


def test_an_unreadable_secret_is_a_refusal_never_an_error_or_a_success(app, conn, store, tmp_path):
    secret = new_secret()
    user = add(app, conn, 'bob', secret=secret)
    now = time.time()
    code = code_at(secret, now)
    conn.execute("UPDATE users SET totp_secret = ? WHERE id = ?", (b'garbage', user['id']))
    assert second_factor.verify_totp(conn, store, user['id'], code, now=now) is False
    conn.execute("UPDATE users SET totp_secret = ? WHERE id = ?", (store.encrypt_value(b'\xff\xfe'), user['id']))
    assert second_factor.verify_totp(conn, store, user['id'], code, now=now) is False
    other = make_app(tmp_path / 'autre', SECRETS_KEY=__import__('cryptography.fernet', fromlist=['Fernet']).Fernet.generate_key().decode())
    conn.execute("UPDATE users SET totp_secret = ? WHERE id = ?",
                 (app.extensions['secrets'].encrypt_value(secret.encode()), user['id']))
    assert second_factor.verify_totp(conn, other.extensions['secrets'], user['id'], code, now=now) is False       # autre clé
    assert second_factor.verify_totp(conn, store, 987654, code, now=now) is False                                  # compte inconnu


def test_a_secret_that_is_set_but_not_confirmed_is_not_a_factor(app, conn, store):
    secret = new_secret()
    user = add(app, conn, 'bob', secret=secret)
    conn.execute("UPDATE users SET totp_enabled_at = NULL WHERE id = ?", (user['id'],))
    conn.commit()
    assert not factors.has_second_factor(conn, user['id'])
    assert second_factor.verify_totp(conn, store, user['id'], code_at(secret)) is False


def test_the_provisioning_uri_and_qr_follow_the_spec_and_carry_nothing_dangerous():
    secret = new_secret()
    uri = second_factor.provisioning_uri(secret, 'a.b-c_d')
    assert uri == (f'otpauth://totp/KmlViewer:a.b-c_d?secret={secret}&issuer=KmlViewer&algorithm=SHA1&digits=6&period=30')
    assert len(secret) == 32 and set(secret) <= set('ABCDEFGHIJKLMNOPQRSTUVWXYZ234567')
    hostile = second_factor.provisioning_uri(secret, '"><script>x</script>&secret=EVIL')
    assert '<' not in hostile and '"' not in hostile and hostile.count('secret=') == 1
    svg = second_factor.qr_svg(hostile)
    assert svg.lstrip().startswith('<svg') and not re.search(r'<script|onload|onerror|foreignObject|javascript:|xlink:href|<image|<use', svg, re.I)
    assert secret not in svg and 'EVIL' not in svg


def test_secrets_are_unique_and_long_enough():
    assert len({new_secret() for _ in range(500)}) == 500


# ===== 2. Codes de secours ======================================================================================================

def test_recovery_codes_have_the_documented_form_and_only_their_hashes_are_stored(app, conn):
    user = add(app, conn, 'bob', secret=new_secret())
    codes = second_factor.replace_recovery_codes(conn, user['id'])
    conn.commit()
    assert len(codes) == 10 and len(set(codes)) == 10
    assert all(re.fullmatch(r'[A-Z2-7]{4}(-[A-Z2-7]{4}){3}', c) for c in codes)
    rows = conn.execute("SELECT code_hash, used_at FROM recovery_codes WHERE user_id = ?", (user['id'],)).fetchall()
    assert len(rows) == 10 and all(re.fullmatch(r'[0-9a-f]{64}', r['code_hash']) and r['used_at'] is None for r in rows)
    dump = json.dumps([tuple(r) for r in conn.execute("SELECT * FROM recovery_codes")])
    assert not any(c.replace('-', '') in dump or c in dump for c in codes)


@pytest.mark.parametrize('variant', [lambda c: c, lambda c: c.replace('-', ''), lambda c: c.lower(), lambda c: c.replace('-', ' '),
                                     lambda c: ' ' + c.lower() + ' ', lambda c: c.replace('-', '\t')])
def test_a_recovery_code_is_accepted_in_any_formatting_once(app, conn, variant):
    user = add(app, conn, 'bob', secret=new_secret())
    code = second_factor.replace_recovery_codes(conn, user['id'])[0]
    conn.commit()
    assert second_factor.consume_recovery(conn, user['id'], variant(code)) is True
    assert second_factor.consume_recovery(conn, user['id'], code) is False
    assert second_factor.remaining_recovery(conn, user['id']) == 9


@pytest.mark.parametrize('code', ['', 'AAAA-AAAA-AAAA-AAA', 'AAAA-AAAA-AAAA-AAAAA', 'AAAA-AAAA-AAAA-AAA1', 'AAAA-AAAA-AAAA-AAA0', 'ＡＡＡＡ-AAAA-AAAA-AAAA',
                                  None, 5, ['A'], {'a': 1}, b'AAAA', True, 'AAAA-AAAA-AAAA-AAA\n', '%', "' OR 1=1 --"])
def test_malformed_recovery_codes_are_plain_refusals(app, conn, code):
    user = add(app, conn, 'bob', secret=new_secret())
    second_factor.replace_recovery_codes(conn, user['id'])
    conn.commit()
    assert second_factor.consume_recovery(conn, user['id'], code) is False
    assert second_factor.remaining_recovery(conn, user['id']) == 10


def test_a_recovery_code_belongs_to_its_account_only(app, conn):
    a, b = add(app, conn, 'alice', secret=new_secret()), add(app, conn, 'bob', secret=new_secret())
    code = second_factor.replace_recovery_codes(conn, a['id'])[0]
    conn.commit()
    assert second_factor.consume_recovery(conn, b['id'], code) is False
    assert second_factor.consume_recovery(conn, a['id'], code) is True


@pytest.mark.parametrize('round_', range(6))
def test_concurrent_use_of_one_recovery_code_lets_exactly_one_through(app, conn, round_):
    user = add(app, conn, 'bob', secret=new_secret())
    code = second_factor.replace_recovery_codes(conn, user['id'])[0]
    conn.commit()

    def job(i):
        c = connect(app)
        try:
            return second_factor.consume_recovery(c, user['id'], code)
        finally:
            c.close()
    results = concurrent(12, job)
    assert [r for r in results if isinstance(r, Exception)] == [] and results.count(True) == 1


def test_regenerating_invalidates_every_old_code(app, conn):
    user = add(app, conn, 'bob', secret=new_secret())
    old = second_factor.replace_recovery_codes(conn, user['id'])
    new = second_factor.replace_recovery_codes(conn, user['id'])
    conn.commit()
    assert set(old).isdisjoint(new)
    assert all(second_factor.consume_recovery(conn, user['id'], c) is False for c in old)
    assert second_factor.consume_recovery(conn, user['id'], new[0]) is True


def test_a_recovery_code_is_not_a_second_factor_and_clear_factors_invalidates_them(app, conn):
    user = add(app, conn, 'bob')
    second_factor.replace_recovery_codes(conn, user['id'])
    conn.commit()
    assert not factors.has_second_factor(conn, user['id'])
    add_secret = add(app, conn, 'carol', secret=new_secret())
    second_factor.replace_recovery_codes(conn, add_secret['id'])
    removed = factors.clear_factors(conn, add_secret['id'])
    conn.commit()
    assert removed['totp'] == 1 and removed['recovery_codes'] == 10 and not factors.has_second_factor(conn, add_secret['id'])
    assert second_factor.remaining_recovery(conn, add_secret['id']) == 0


@pytest.mark.parametrize('code,kind', [('123456', 'totp'), ('12 34 56', 'totp'), ('AAAA-BBBB-CCCC-DDDD', 'recovery'), ('aaaabbbbccccdddd', 'recovery'),
                                       ('2222222222222222', 'recovery'), ('12345', None), ('', None), (None, None), ('１２３４５６', None)])
def test_kind_of_distinguishes_by_shape(code, kind):
    assert second_factor.kind_of(code) == kind


# ===== 3. Connexion : étape mfa =================================================================================================

def admin_with_factor(app, conn, name='adm', **kw):
    secret = new_secret()
    row = add(app, conn, name, admin=True, secret=secret, **kw)
    return row, secret


def pending_client(app, conn, name='adm', **kw):
    row, secret = admin_with_factor(app, conn, name, **kw)
    client = app.test_client()
    response = login(app, name, client=client)
    assert response.status_code == 200 and response.get_json()['step'] == 'mfa', response.get_data()
    return client, row, secret


def test_a_valid_totp_opens_a_full_session_and_the_same_code_cannot_open_a_second_one(app, conn):
    client, row, secret = pending_client(app, conn)
    code = code_at(secret)
    response = mfa(app, client, code)
    assert response.status_code == 200 and response.get_json()['success'] is True
    payload = cookie_payload(app, client)
    assert set(payload) == {'user_id', 'token', 'login_at', '_permanent'} and payload['user_id'] == row['id']
    assert client.get('/api/me', **KW).status_code == 200
    second = app.test_client()                                               # nouvelle connexion, MÊME code : rejeu
    login(app, 'adm', client=second)
    refused = mfa(app, second, code)
    assert refused.status_code == 401 and raw_cookie(second) is not None and 'user_id' not in cookie_payload(app, second)


def test_the_next_code_is_accepted_after_the_step_moves_on(app, conn, monkeypatch):
    client, row, secret = pending_client(app, conn)
    clock = FakeTime()
    monkeypatch.setattr(second_factor, 'time', clock)
    assert mfa(app, client, code_at(secret, clock.now)).status_code == 200
    second = app.test_client()
    clock.now += 61                                                        # le pas courant dépasse le dernier accepté + fenêtre
    login(app, 'adm', client=second, ip=new_ip())
    assert mfa(app, second, code_at(secret, clock.now)).status_code == 200


def test_five_wrong_codes_destroy_the_state_and_the_unified_message_does_not_distinguish_the_cause(app, conn):
    client, row, secret = pending_client(app, conn)
    answers = []
    for _ in range(5):
        r = mfa(app, client, '000000')
        answers.append((r.status_code, r.get_json()))
    assert [a[0] for a in answers] == [401] * 5
    assert answers[0][1]['error'] == 'Code invalide ou déjà utilisé.' and answers[-1][1].get('restart') is True
    assert app.extensions['pending'].size() == 0
    assert mfa(app, client, code_at(secret)).status_code in (302, 401) and 'user_id' not in cookie_payload(app, client)


def test_wrong_and_replayed_and_malformed_codes_answer_the_same(app, conn):
    client, row, secret = pending_client(app, conn)
    used = app.test_client()
    login(app, 'adm', client=used)
    mfa(app, used, code_at(secret))
    sigs = set()
    for code in ('000000', code_at(secret), 'garbage', 'AAAA-AAAA-AAAA-AAAA'):                       # 1 essai déjà consommé par `used` (limiteur : 5)
        r = mfa(app, client, code)
        sigs.add((r.status_code, r.get_data()))
    assert len(sigs) == 1


def test_the_account_limiter_caps_total_guesses_across_fresh_pending_states(app, conn, monkeypatch):
    """EF-S3 / EF-2.6 : l'attaquant qui connaît le mot de passe ne repart pas à zéro en se reconnectant."""
    row, secret = admin_with_factor(app, conn)
    calls = []
    original = second_factor.verify_totp
    monkeypatch.setattr(second_factor, 'verify_totp', lambda *a, **k: (calls.append(1), original(*a, **k))[1])
    statuses = []
    for _ in range(30):
        client = app.test_client()
        r = login(app, 'adm', client=client, ip=new_ip())
        if r.status_code != 200:
            statuses.append(('login', r.status_code))
            continue
        for _ in range(5):
            r = mfa(app, client, '000000', ip=new_ip())
            statuses.append(('mfa', r.status_code))
            if r.status_code != 401 or r.get_json().get('restart'):
                break
    assert len(calls) <= 10, f"{len(calls)} vérifications de code pour un seul compte"
    assert ('mfa', 429) in statuses or ('login', 429) in statuses


def test_a_limited_answer_is_generic_never_evaluates_the_code_and_does_not_consume_pending_tries(app, conn, monkeypatch):
    row, secret = admin_with_factor(app, conn)
    first = app.test_client()
    login(app, 'adm', client=first)
    for _ in range(5):
        mfa(app, first, '000000')                                        # le limiteur par compte atteint son seuil (5)
    client = app.test_client()
    login(app, 'adm', client=client, ip=new_ip())
    calls = []
    original = second_factor.verify_totp
    monkeypatch.setattr(second_factor, 'verify_totp', lambda *a, **k: (calls.append(1), original(*a, **k))[1])
    wrong, right = mfa(app, client, '000000'), mfa(app, client, code_at(secret))
    assert wrong.status_code == right.status_code == 429 and wrong.get_json() == right.get_json() and calls == []
    assert wrong.headers.get('Retry-After') and 'user_id' not in cookie_payload(app, client)
    pid = cookie_payload(app, client)['pending_id']
    assert app.extensions['pending'].get(pid).tries == 0


@pytest.mark.parametrize('code', [123456, None, True, ['123456'], {'code': '123456'}, 1.5, 'x' * 65, 'x' * 10_000, '\x00' * 6, '\ud800'])
def test_hostile_code_values_are_plain_refusals_never_a_server_error(app, conn, code):
    client, row, secret = pending_client(app, conn)
    payload = {'code': code}
    r = form_post(app, '/auth/password/mfa', payload, client=client, raw=json.dumps(payload))
    assert r.status_code in (400, 401), (r.status_code, r.get_data()[:100])
    assert 'user_id' not in cookie_payload(app, client)


def test_a_missing_csrf_token_or_a_foreign_origin_is_refused_and_not_counted(app, conn):
    client, row, secret = pending_client(app, conn)
    for _ in range(6):
        assert mfa(app, client, code_at(secret), token=False).status_code in (400, 403)
        assert mfa(app, client, code_at(secret), origin='https://evil.example').status_code == 403
    pid = cookie_payload(app, client)['pending_id']
    assert app.extensions['pending'].get(pid).tries == 0
    assert mfa(app, client, code_at(secret)).status_code == 200


def test_two_simultaneous_logins_with_the_same_code_and_cookie_open_one_session_only(app, conn):
    client, row, secret = pending_client(app, conn)
    cookie = raw_cookie(client)
    code = code_at(secret)

    def job(i):
        c = app.test_client()
        c.set_cookie('session', cookie, domain=HOST)
        r = mfa(app, c, code)
        return r.status_code, 'user_id' in cookie_payload(app, c)
    results = concurrent(8, job)
    assert [r for r in results if isinstance(r, Exception)] == []
    assert sum(1 for status, opened in results if status == 200 and opened) == 1


def test_a_recovery_code_logs_in_once_is_audited_and_two_different_codes_cannot_both_open_sessions(app, conn):
    client, row, secret = pending_client(app, conn)
    codes = second_factor.replace_recovery_codes(conn, row['id'])
    conn.commit()
    r = mfa(app, client, codes[0].lower().replace('-', ' '))
    assert r.status_code == 200 and cookie_payload(app, client)['user_id'] == row['id']
    entries = [dict(e) for e in conn.execute("SELECT * FROM audit_log WHERE action = 'user.recovery_used'")]
    assert len(entries) == 1 and json.loads(entries[0]['details']) == {'remaining': 9} and entries[0]['actor_type'] == 'user'
    assert codes[0] not in json.dumps(entries) and codes[0].replace('-', '') not in json.dumps(entries)
    second = app.test_client()
    login(app, 'adm', client=second)
    assert mfa(app, second, codes[0]).status_code == 401                          # déjà utilisé
    cookie = raw_cookie(second)

    def job(i):
        c = app.test_client()
        c.set_cookie('session', cookie, domain=HOST)
        return mfa(app, c, codes[1 + i]).status_code, 'user_id' in cookie_payload(app, c)
    results = concurrent(4, job)
    assert sum(1 for status, opened in results if status == 200 and opened) == 1


def test_the_session_after_the_second_factor_honours_a_sanitised_next(app, conn):
    row, secret = admin_with_factor(app, conn)
    client = app.test_client()
    r = form_post(app, '/auth/password/login', {'username': 'adm', 'password': GOOD, 'next': '//evil.example/x'}, client=client)
    assert r.get_json()['step'] == 'mfa'
    done = mfa(app, client, code_at(secret))
    assert done.get_json()['next'] == '/'
    other = app.test_client()
    form_post(app, '/auth/password/login', {'username': 'adm', 'password': GOOD, 'next': '/api/activities?page=2'}, client=other, ip=new_ip())
    pid = cookie_payload(app, other)['pending_id']
    assert app.extensions['pending'].get(pid).next_url == '/api/activities?page=2'


def test_the_mfa_page_exists_only_with_a_matching_pending_state(app, conn):
    anonymous = app.test_client()
    r = anonymous.get('/auth/password/mfa', **KW)
    assert r.status_code == 302 and '/auth/password/login' in r.headers['Location']
    client, row, secret = pending_client(app, conn)
    page = client.get('/auth/password/mfa', **KW)
    assert page.status_code == 200 and 'csrf-token' in page.get_data(as_text=True) and 'no-store' in page.headers['Cache-Control']
    assert client.get('/auth/password/enroll', **KW).status_code == 302


def test_a_pending_state_dies_when_the_account_changes_even_between_page_and_code(app, conn):
    client, row, secret = pending_client(app, conn)
    conn.execute("UPDATE users SET session_token = 'rotated' WHERE id = ?", (row['id'],))
    conn.commit()
    r = mfa(app, client, code_at(secret))
    assert r.status_code in (302, 401) and 'user_id' not in cookie_payload(app, client)


# ===== 4. Enrôlement et octroi d'admin (B5) =====================================================================================

def bootstrap_client(app, conn, name='chef', *, existing=None):
    """Consomme un jeton d'amorçage ; retourne (client avec état d'enrôlement, id du compte)."""
    if existing is None:
        created = tokens.create(conn, 'bootstrap', actor=CLI, email=f'{name}@example.org', is_admin_grant=True)
        client = app.test_client()
        r = form_post(app, '/auth/token/consume', {'token': created.token, 'username': name, 'password': GOOD}, client=client)
    else:
        created = tokens.create(conn, 'bootstrap', actor=CLI, target_user_id=existing['id'], is_admin_grant=True)
        client = app.test_client()
        r = form_post(app, '/auth/token/consume', {'token': created.token, 'password': GOOD}, client=client)
    assert r.status_code == 200 and r.get_json()['next'].endswith('/auth/password/enroll'), r.get_data()
    return client, created


def enroll_secret(client):
    html = client.get('/auth/password/enroll', **KW).get_data(as_text=True)
    return re.sub(r'\s', '', re.search(r'(?:[A-Z2-7]{4}\s){7}[A-Z2-7]{4}', html).group(0))


def test_bootstrap_makes_a_non_admin_account_in_enrollment_then_the_first_factor_grants_admin_in_one_step(app, conn, store):
    client, created = bootstrap_client(app, conn)
    user = refresh(conn, 'chef')
    assert not user['is_admin'] and not factors.has_second_factor(conn, user['id'])                # EF-0.3 : non admin tant que sans facteur
    assert set(cookie_payload(app, client)) == {'pending_id'}
    secret = enroll_secret(client)
    assert refresh(conn, 'chef')['totp_secret'] is None                                            # P-T11-1 : jamais en base avant confirmation
    assert enroll_secret(client) == secret                                                         # rechargement : même secret
    assert mfa_enroll(app, client, '000000').status_code == 401
    r = mfa_enroll(app, client, code_at(secret))
    body = r.get_json()
    assert r.status_code == 200 and body['next'].endswith('/auth/password/login') and len(body['recovery_codes']) == 10
    user = refresh(conn, 'chef')
    assert user['is_admin'] == 1 and user['totp_enabled_at'] and user['totp_last_step'] is not None
    assert store.decrypt_value(user['totp_secret']).decode() == secret
    assert 'user_id' not in cookie_payload(app, client) and 'pending_id' not in cookie_payload(app, client)    # r8 : aucune session
    actions = [r['action'] for r in conn.execute("SELECT action FROM audit_log ORDER BY id")]
    assert actions.index('user.factor_added') < actions.index('user.admin_grant')
    assert client.get('/auth/password/enroll', **KW).status_code == 302                            # état détruit


def mfa_enroll(app, client, code, **kw):
    return form_post(app, '/auth/password/enroll', {'code': code}, client=client, **kw)


def test_the_enrollment_code_cannot_be_replayed_at_the_next_login_but_the_next_one_works(app, conn, monkeypatch):
    client, _ = bootstrap_client(app, conn)
    secret = enroll_secret(client)
    clock = FakeTime()
    monkeypatch.setattr(second_factor, 'time', clock)
    code = code_at(secret, clock.now)
    assert mfa_enroll(app, client, code).status_code == 200
    second = app.test_client()
    r = login(app, 'chef', client=second)
    assert r.get_json()['step'] == 'mfa'
    assert mfa(app, second, code).status_code == 401
    clock.now += 31
    third = app.test_client()
    login(app, 'chef', client=third, ip=new_ip())
    assert mfa(app, third, code_at(secret, clock.now)).status_code == 200


def test_an_abandoned_enrollment_leaves_a_non_admin_account_without_factor_and_a_consumed_token(app, conn):
    client, created = bootstrap_client(app, conn)
    clock = [time.monotonic()]
    app.extensions['pending']._clock = lambda: clock[0] + 3600
    r = client.get('/auth/password/enroll', **KW)
    assert r.status_code == 302                                                                     # état expiré
    user = refresh(conn, 'chef')
    assert not user['is_admin'] and not factors.has_second_factor(conn, user['id'])
    again = form_post(app, '/auth/token/consume', {'token': created.token, 'username': 'root2', 'password': GOOD})
    assert again.status_code == 404                                                                 # jeton consommé : neutre


def test_two_simultaneous_confirmations_enrol_once_with_one_set_of_codes(app, conn):
    client, _ = bootstrap_client(app, conn)
    secret = enroll_secret(client)
    cookie = raw_cookie(client)
    code = code_at(secret)

    def job(i):
        c = app.test_client()
        c.set_cookie('session', cookie, domain=HOST)
        r = mfa_enroll(app, c, code)
        return r.status_code, (r.get_json() or {}).get('recovery_codes')
    results = concurrent(8, job)
    assert [r for r in results if isinstance(r, Exception)] == []
    winners = [r for r in results if r[0] == 200]
    assert len(winners) == 1
    user = refresh(conn, 'chef')
    assert conn.execute("SELECT COUNT(*) FROM recovery_codes WHERE user_id = ?", (user['id'],)).fetchone()[0] == 10
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'user.admin_grant'").fetchone()[0] == 1


@pytest.mark.parametrize('code', ['', '12345', '1234567', 'abcdef', None, 123456, ['1'], '٠١٢٣٤٥'])
def test_an_invalid_enrollment_code_enrols_nothing_and_grants_nothing(app, conn, code):
    client, _ = bootstrap_client(app, conn)
    enroll_secret(client)
    payload = {'code': code}
    r = form_post(app, '/auth/password/enroll', payload, client=client, raw=json.dumps(payload))
    assert r.status_code in (400, 401)
    user = refresh(conn, 'chef')
    assert not user['is_admin'] and user['totp_secret'] is None


def test_the_enrollment_dies_after_five_wrong_codes_and_admin_is_never_granted(app, conn):
    client, _ = bootstrap_client(app, conn)
    secret = enroll_secret(client)
    for _ in range(5):
        mfa_enroll(app, client, '000000')
    assert mfa_enroll(app, client, code_at(secret)).status_code in (302, 401)
    assert not refresh(conn, 'chef')['is_admin']


def test_enrollment_requires_csrf_and_a_same_origin(app, conn):
    client, _ = bootstrap_client(app, conn)
    secret = enroll_secret(client)
    assert mfa_enroll(app, client, code_at(secret), token=False).status_code in (400, 403)
    assert mfa_enroll(app, client, code_at(secret), origin='https://evil.example').status_code == 403
    assert not refresh(conn, 'chef')['is_admin']
    assert mfa_enroll(app, client, code_at(secret)).status_code == 200


def test_the_enrollment_page_hides_nothing_dangerous_and_the_qr_is_inline_svg(app, conn):
    client, _ = bootstrap_client(app, conn)
    html = client.get('/auth/password/enroll', **KW).get_data(as_text=True)
    assert '<svg' in html and 'otpauth' not in html.split('<svg', 1)[1].split('</svg>', 1)[0]
    assert 'no-store' in client.get('/auth/password/enroll', **KW).headers['Cache-Control']
    assert not re.search(r'<script(?![^>]*\bsrc=)', html)


def test_resuming_a_bootstrap_for_an_existing_account_asks_for_the_current_password_and_keeps_the_hash(app, conn):
    """EF-0.3b : le lien seul ne suffit pas ; mauvais mot de passe => jeton non consommé ; bon => hash conservé, enrôlement, admin à la fin."""
    user = add(app, conn, 'alice')
    created = tokens.create(conn, 'bootstrap', actor=CLI, target_user_id=user['id'], is_admin_grant=True)
    inspect = form_post(app, '/auth/token/inspect', {'token': created.token})
    assert inspect.get_json()['form'] == 'confirm_password'
    no_password = form_post(app, '/auth/token/consume', {'token': created.token})
    assert no_password.status_code == 400
    wrong = form_post(app, '/auth/token/consume', {'token': created.token, 'password': OTHER})
    assert wrong.status_code == 400 and 'mot de passe' in wrong.get_json()['error'].lower()
    assert conn.execute("SELECT used_at FROM auth_tokens WHERE id = 1").fetchone()[0] is None             # jeton non consommé
    client = app.test_client()
    ok = form_post(app, '/auth/token/consume', {'token': created.token, 'password': GOOD}, client=client)
    assert ok.status_code == 200 and ok.get_json()['next'].endswith('/auth/password/enroll')
    assert refresh(conn, 'alice')['password_hash'] == user['password_hash'] and not refresh(conn, 'alice')['is_admin']
    secret = enroll_secret(client)
    assert mfa_enroll(app, client, code_at(secret)).status_code == 200 and refresh(conn, 'alice')['is_admin'] == 1


def test_a_stolen_bootstrap_link_cannot_attach_a_factor_to_an_existing_account_without_its_password(app, conn):
    user = add(app, conn, 'alice')
    created = tokens.create(conn, 'bootstrap', actor=CLI, target_user_id=user['id'], is_admin_grant=True)
    for password in (OTHER, 'x', GOOD + ' ', GOOD.upper(), ''):
        r = form_post(app, '/auth/token/consume', {'token': created.token, 'password': password})
        assert r.status_code in (400, 429)
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'user.bootstrap_resumed'").fetchone()[0] == 0


def test_the_password_check_of_a_resumed_bootstrap_is_rate_limited_per_target_account_before_hashing(app, conn, monkeypatch):
    user = add(app, conn, 'alice')
    created = tokens.create(conn, 'bootstrap', actor=CLI, target_user_id=user['id'], is_admin_grant=True)
    hashed = []
    original = passwords.verify_password
    monkeypatch.setattr(passwords, 'verify_password', lambda *a, **k: (hashed.append(1), original(*a, **k))[1])
    statuses = [form_post(app, '/auth/token/consume', {'token': created.token, 'password': OTHER}, ip=new_ip()).status_code for _ in range(12)]
    assert 429 in statuses and len(hashed) <= 10


@pytest.mark.parametrize('change', ["UPDATE users SET is_active = 0", "UPDATE users SET auth_method = 'oidc', password_hash = NULL"])
def test_a_resumed_bootstrap_is_refused_for_an_account_disabled_or_converted_since_the_issue(app, conn, change):
    user = add(app, conn, 'alice')
    created = tokens.create(conn, 'bootstrap', actor=CLI, target_user_id=user['id'], is_admin_grant=True)
    conn.execute(change)
    conn.commit()
    r = form_post(app, '/auth/token/consume', {'token': created.token, 'password': GOOD})
    assert r.status_code in (400, 404) and not refresh(conn, 'alice')['is_admin']


def test_a_resumed_bootstrap_is_refused_for_an_account_that_gained_a_factor_since(app, conn):
    user = add(app, conn, 'alice')
    created = tokens.create(conn, 'bootstrap', actor=CLI, target_user_id=user['id'], is_admin_grant=True)
    conn.execute("UPDATE users SET totp_secret = x'01', totp_enabled_at = '2026-01-01T00:00:00Z' WHERE id = ?", (user['id'],))
    conn.commit()
    r = form_post(app, '/auth/token/consume', {'token': created.token, 'password': GOOD})
    assert r.status_code in (400, 404)
    assert not refresh(conn, 'alice')['is_admin']


def test_a_forced_enrollment_under_the_all_policy_never_grants_admin_but_gives_codes(app, conn):
    app.config['SETTINGS_STORE'].set_many(conn, {'auth.mfa_policy': 'all'}, actor=CLI)
    row = add(app, conn, 'bob')
    client = app.test_client()
    assert login(app, 'bob', client=client).get_json()['step'] == 'enroll'
    secret = enroll_secret(client)
    r = mfa_enroll(app, client, code_at(secret))
    assert r.status_code == 200 and len(r.get_json()['recovery_codes']) == 10
    after = refresh(conn, 'bob')
    assert after['is_admin'] == 0 and after['totp_enabled_at'] and 'user_id' not in cookie_payload(app, client)
    again = app.test_client()
    assert login(app, 'bob', client=again, ip=new_ip()).get_json()['step'] == 'mfa'
    assert row['id'] == after['id']


def test_only_a_bootstrap_token_ever_grants_admin(app, conn):
    for kind in ('invite', 'reset'):
        with pytest.raises(ValueError):
            tokens.create(conn, kind, actor=CLI, email='a@example.org', is_admin_grant=True)
    created = tokens.create(conn, 'invite', actor=CLI, email='inv@example.org')
    client = app.test_client()
    r = form_post(app, '/auth/token/consume', {'token': created.token, 'username': 'invitee', 'password': GOOD}, client=client)
    assert r.status_code == 200 and r.get_json()['next'].endswith('/auth/password/login')
    assert not refresh(conn, 'invitee')['is_admin'] and 'pending_id' not in cookie_payload(app, client)


# ===== 5. /api/me/mfa ===========================================================================================================

def test_setup_needs_the_current_password_and_keeps_the_secret_in_memory_only(app, conn):
    user = add(app, conn, 'bob')
    client = session_client(app, user)
    assert call(client, 'post', '/api/me/mfa/totp/setup', {}).status_code == 400
    assert call(client, 'post', '/api/me/mfa/totp/setup', {'password': OTHER}).status_code == 403
    r = call(client, 'post', '/api/me/mfa/totp/setup', {'password': GOOD})
    assert r.status_code == 200
    body = r.get_json()
    secret = body['secret'].replace(' ', '')
    assert refresh(conn, 'bob')['totp_secret'] is None and not factors.has_second_factor(conn, user['id'])
    assert body['uri'].startswith('otpauth://totp/KmlViewer:bob?secret=' + secret)
    assert call(client, 'post', '/api/me/mfa/totp/confirm', {'code': '000000'}).status_code == 403
    done = call(client, 'post', '/api/me/mfa/totp/confirm', {'code': code_at(secret)})
    assert done.status_code == 200 and len(done.get_json()['recovery_codes']) == 10
    assert factors.has_second_factor(conn, user['id']) and not refresh(conn, 'bob')['is_admin']          # le profil n'accorde jamais l'admin
    assert call(client, 'post', '/api/me/mfa/totp/confirm', {'code': code_at(secret)}).status_code in (403, 409)


def test_the_profile_enrollment_is_bound_to_the_session_and_dies_with_a_session_change(app, conn):
    user = add(app, conn, 'bob')
    client = session_client(app, user)
    secret = call(client, 'post', '/api/me/mfa/totp/setup', {'password': GOOD}).get_json()['secret'].replace(' ', '')
    conn.execute("UPDATE users SET session_token = 'rotated' WHERE id = ?", (user['id'],))
    conn.commit()
    fresh = session_client(app, refresh(conn, 'bob'))
    assert call(fresh, 'post', '/api/me/mfa/totp/confirm', {'code': code_at(secret)}).status_code in (403, 409)
    assert not factors.has_second_factor(conn, user['id'])


def test_another_account_cannot_confirm_someone_elses_enrollment(app, conn):
    a, b = add(app, conn, 'alice'), add(app, conn, 'bob')
    ca, cb = session_client(app, a), session_client(app, b)
    secret = call(ca, 'post', '/api/me/mfa/totp/setup', {'password': GOOD}).get_json()['secret'].replace(' ', '')
    assert call(cb, 'post', '/api/me/mfa/totp/confirm', {'code': code_at(secret)}).status_code in (403, 409)
    assert not factors.has_second_factor(conn, b['id'])


def enrolled_profile(app, conn, name='bob', admin=False):
    secret = new_secret()
    row = add(app, conn, name, admin=admin, secret=secret)
    second_factor.replace_recovery_codes(conn, row['id'])
    conn.commit()
    return row, secret, session_client(app, row)


def test_removing_the_totp_needs_the_password_and_a_valid_code_and_invalidates_recovery_codes(app, conn):
    row, secret, client = enrolled_profile(app, conn)
    assert call(client, 'delete', '/api/me/mfa/totp', {'password': GOOD}).status_code == 403
    assert call(client, 'delete', '/api/me/mfa/totp', {'password': GOOD, 'code': '000000'}).status_code == 403
    assert call(client, 'delete', '/api/me/mfa/totp', {'password': OTHER, 'code': code_at(secret)}).status_code == 403
    assert factors.has_second_factor(conn, row['id'])
    ok = call(client, 'delete', '/api/me/mfa/totp', {'password': GOOD, 'code': code_at(secret)})
    assert ok.status_code == 200 and not factors.has_second_factor(conn, row['id'])
    assert second_factor.remaining_recovery(conn, row['id']) == 0


def test_the_last_factor_of_an_admin_cannot_be_removed_EF22(app, conn):
    row, secret, client = enrolled_profile(app, conn, 'adm', admin=True)
    r = call(client, 'delete', '/api/me/mfa/totp', {'password': GOOD, 'code': code_at(secret)})
    assert r.status_code == 409 and factors.has_second_factor(conn, row['id']) and refresh(conn, 'adm')['is_admin'] == 1
    assert second_factor.remaining_recovery(conn, row['id']) == 10


def test_a_recovery_code_does_not_authorise_removing_the_factor_nor_regenerating(app, conn):
    row, secret, client = enrolled_profile(app, conn)
    code = conn.execute("SELECT 1").fetchone() and second_factor.replace_recovery_codes(conn, row['id'])[0]
    conn.commit()
    assert call(client, 'delete', '/api/me/mfa/totp', {'password': GOOD, 'code': code}).status_code == 403
    assert call(client, 'post', '/api/me/mfa/recovery-codes', {'password': GOOD, 'code': code}).status_code == 403


def test_regenerating_the_codes_needs_password_and_totp_and_the_old_ones_die(app, conn):
    row, secret, client = enrolled_profile(app, conn)
    old = second_factor.replace_recovery_codes(conn, row['id'])
    conn.commit()
    assert call(client, 'post', '/api/me/mfa/recovery-codes', {'password': GOOD}).status_code in (400, 403)
    r = call(client, 'post', '/api/me/mfa/recovery-codes', {'password': GOOD, 'code': code_at(secret)})
    assert r.status_code == 200
    new = r.get_json()['recovery_codes']
    assert len(new) == 10 and set(new).isdisjoint(old)
    assert all(second_factor.consume_recovery(conn, row['id'], c) is False for c in old)
    assert 'recovery_codes_remaining' in call(client, 'get', '/api/me/mfa').get_json()


def test_the_mfa_state_never_returns_the_secret_or_codes(app, conn):
    row, secret, client = enrolled_profile(app, conn)
    body = call(client, 'get', '/api/me/mfa').get_data(as_text=True)
    assert secret not in body and 'recovery' in body and 'KmlViewer' not in body
    assert json.loads(body) == {'success': True, 'totp': True, 'keys': 0, 'key_list': [], 'webauthn_available': True,
                                    'recovery_codes_remaining': 10}


def test_the_mfa_routes_are_for_password_accounts_with_a_full_session_only(app, conn):
    oidc = add(app, conn, 'sso', method='oidc')
    pw = add(app, conn, 'bob')
    for client, paths in ((session_client(app, oidc), ('/api/me/mfa',)), (app.test_client(), ('/api/me/mfa', '/api/me/password'))):
        for path in paths:
            assert call(client, 'get' if 'mfa' in path else 'post', path, None if 'mfa' in path else {}).status_code in (401, 404, 405)
    assert call(session_client(app, pw), 'get', '/api/me/mfa').status_code == 200
    pending = app.test_client()
    login(app, 'bob', client=pending)                                       # compte sans facteur : session complète
    add_admin = add(app, conn, 'adm2', admin=True, secret=new_secret())
    c = app.test_client()
    assert login(app, 'adm2', client=c).get_json()['step'] == 'mfa'
    assert call(c, 'get', '/api/me/mfa').status_code == 401                 # un état mfa_pending n'est pas une session
    assert add_admin['id']


def test_mfa_changes_need_a_same_origin_request(app, conn):
    row, secret, client = enrolled_profile(app, conn)
    for method, path, payload in (('post', '/api/me/mfa/totp/setup', {'password': GOOD}), ('delete', '/api/me/mfa/totp', {'password': GOOD, 'code': '1'}),
                                  ('post', '/api/me/mfa/recovery-codes', {'password': GOOD, 'code': '1'})):
        assert call(client, method, path, payload, origin='https://evil.example').status_code == 403


def test_the_password_checks_of_the_profile_are_rate_limited_before_hashing(app, conn, monkeypatch):
    row, secret, client = enrolled_profile(app, conn)
    hashed = []
    original = passwords.verify_password
    monkeypatch.setattr(passwords, 'verify_password', lambda *a, **k: (hashed.append(1), original(*a, **k))[1])
    statuses = [call(client, 'post', '/api/me/mfa/totp/setup', {'password': OTHER}, ip=new_ip()).status_code for _ in range(12)]
    assert statuses[0] in (403, 409) and 429 in statuses and len(hashed) <= 8


# ===== 6. /api/me/password et /api/me/email =====================================================================================

def pw_world(app, conn):
    secret = new_secret()
    row = add(app, conn, 'bob', secret=secret, email='bob@example.org')
    return row, secret, session_client(app, row)


def test_changing_the_password_needs_the_current_one_and_a_factor(app, conn):
    row, secret, client = pw_world(app, conn)
    base = {'current_password': GOOD, 'new_password': OTHER}
    assert call(client, 'post', '/api/me/password', {'new_password': OTHER}).status_code == 400
    assert call(client, 'post', '/api/me/password', {**base, 'current_password': 'faux'}).status_code == 403
    assert call(client, 'post', '/api/me/password', base).status_code == 403                      # facteur manquant
    assert call(client, 'post', '/api/me/password', {**base, 'code': '000000'}).status_code == 403
    assert refresh(conn, 'bob')['password_hash'] == row['password_hash']
    assert call(client, 'post', '/api/me/password', {**base, 'code': code_at(secret)}).status_code == 200


@pytest.mark.parametrize('bad', ['court', 'bob', 'bob@example.org', 'BOB@EXAMPLE.ORG', 'password1234', 'x' * 129, '', None, 5, ['a'], '\ud800'])
def test_the_new_password_obeys_the_policy(tmp_path, bad):
    app = make_app(tmp_path)
    conn = connect(app)
    row, secret, client = pw_world(app, conn)
    payload = {'current_password': GOOD, 'new_password': bad, 'code': code_at(secret)}
    r = call(client, 'post', '/api/me/password', payload, raw=json.dumps(payload))
    assert r.status_code == 400, bad
    assert refresh(conn, 'bob')['password_hash'] == row['password_hash']


def test_a_password_change_revokes_other_sessions_keeps_the_current_one_and_kills_pending_states(app, conn):
    row = add(app, conn, 'bob', email='bob@example.org')
    current, other = session_client(app, row), session_client(app, row)
    pid = app.extensions['pending'].create(row['id'], 'mfa', binding=(row['session_token'], row['password_changed_at']))
    r = call(current, 'post', '/api/me/password', {'current_password': GOOD, 'new_password': OTHER})
    assert r.status_code == 200
    assert call(current, 'get', '/api/me').status_code == 200                       # la courante est réémise
    assert call(other, 'get', '/api/me').status_code == 401                         # les autres tombent
    assert app.extensions['pending'].get(pid) is None
    assert login(app, 'bob', GOOD).status_code == 401 and login(app, 'bob', OTHER).status_code == 200
    dump = json.dumps([dict(x) for x in conn.execute("SELECT * FROM audit_log")])
    assert GOOD not in dump and OTHER not in dump
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'user.password_changed'").fetchone()[0] == 1


def test_a_password_change_with_a_recovery_code_burns_it_and_with_totp_burns_the_step(app, conn):
    secret = new_secret()
    row = add(app, conn, 'bob', secret=secret)
    codes = second_factor.replace_recovery_codes(conn, row['id'])
    conn.commit()
    client = session_client(app, row)
    r = call(client, 'post', '/api/me/password', {'current_password': GOOD, 'new_password': OTHER, 'code': codes[0]})
    assert r.status_code == 200 and second_factor.remaining_recovery(conn, row['id']) == 9
    again = session_client(app, refresh(conn, 'bob'))
    assert call(again, 'post', '/api/me/password', {'current_password': OTHER, 'new_password': GOOD, 'code': codes[0]}).status_code == 403


def test_password_changes_are_refused_for_other_account_kinds_and_for_anonymous_visitors(app, conn):
    sso = session_client(app, add(app, conn, 'sso', method='oidc'))
    assert call(sso, 'post', '/api/me/password', {'current_password': 'x', 'new_password': OTHER}).status_code in (401, 403, 404)
    assert call(app.test_client(), 'post', '/api/me/password', {'current_password': GOOD, 'new_password': OTHER}).status_code == 401
    bob = session_client(app, add(app, conn, 'bob'))
    assert call(bob, 'post', '/api/me/password', {'current_password': GOOD, 'new_password': OTHER}, origin='https://evil.example').status_code == 403


@pytest.mark.parametrize('payload', [None, [], 'x', 5, {'current_password': None, 'new_password': None}, {'current_password': ['a'], 'new_password': {'b': 1}},
                                     {'current_password': GOOD}, {'new_password': OTHER}, {'current_password': 'a' * 5000, 'new_password': OTHER}])
def test_hostile_bodies_on_password_change_are_client_errors(app, conn, payload):
    client = session_client(app, add(app, conn, 'bob'))
    r = call(client, 'post', '/api/me/password', payload, raw=json.dumps(payload))
    assert r.status_code in (400, 403, 413), r.status_code
    assert login(app, 'bob', GOOD).status_code == 200


def mail_world(tmp_path):
    smtp = FakeSMTP()
    app = make_app(tmp_path / 'mail', OUTBOUND_ALLOWED_HOSTS=f'smtp=127.0.0.1:{smtp.port}')
    c = connect(app)
    app.config['SETTINGS_STORE'].set_many(c, {'smtp.host': '127.0.0.1', 'smtp.port': smtp.port, 'smtp.security': 'none',
                                              'smtp.sender': 'noreply@example.org', 'smtp.timeout_s': 2}, actor=CLI)
    return app, c, smtp


def test_changing_the_email_puts_the_new_address_in_pending_notifies_the_old_one_and_mails_the_link_to_the_new_one(tmp_path):
    app, conn, smtp = mail_world(tmp_path)
    try:
        row = add(app, conn, 'bob', email='old@example.org')
        client = session_client(app, row)
        r = call(client, 'post', '/api/me/email', {'password': GOOD, 'email': 'New@Example.ORG'})
        assert r.status_code == 200 and r.get_json()['pending_email'] == 'new@example.org'
        app.extensions['mailer'].queue.drain()
        after = refresh(conn, 'bob')
        assert after['email'] == 'old@example.org' and after['pending_email'] == 'new@example.org'      # l'ancienne reste active
        recipients = [c.lower() for c in smtp.commands if c.lower().startswith('rcpt to')]
        assert any('old@example.org' in x for x in recipients) and any('new@example.org' in x for x in recipients)
        assert any('/auth/token#' in m for m in smtp.messages) and len(recipients) == 2
        assert conn.execute("SELECT COUNT(*) FROM auth_tokens WHERE kind = 'verify_email' AND email = 'new@example.org'").fetchone()[0] == 1
    finally:
        smtp.stop()


def test_changing_the_email_is_neutral_when_the_address_is_taken_and_never_reveals_it(app, conn):
    add(app, conn, 'alice', email='taken@example.org')
    bob = add(app, conn, 'bob', email='bob@example.org')
    client = session_client(app, bob)
    free = call(client, 'post', '/api/me/email', {'password': GOOD, 'email': 'free@example.org'})
    taken = call(client, 'post', '/api/me/email', {'password': GOOD, 'email': 'taken@example.org'})
    assert free.status_code == taken.status_code == 200
    assert set(free.get_json()) == set(taken.get_json())
    assert 'alice' not in taken.get_data(as_text=True)


@pytest.mark.parametrize('email', ['', '@', 'a@', '@b.co', 'a@@b.co', 'a b@c.co', 'a@b..co', 'a@b.co\r\nBcc: x@y.z', 'a@b.co,c@d.co', '<a@b.co>', '"a"@b.co',
                                   'x' * 300 + '@b.co', None, 5, ['a@b.co'], '‮a@b.co'])
def test_invalid_new_addresses_are_refused_without_changing_anything(app, conn, email):
    bob = add(app, conn, 'bob', email='bob@example.org')
    client = session_client(app, bob)
    r = call(client, 'post', '/api/me/email', {'password': GOOD, 'email': email}, raw=json.dumps({'password': GOOD, 'email': email}))
    assert r.status_code in (400, 409)
    after = refresh(conn, 'bob')
    assert after['pending_email'] is None and after['email'] == 'bob@example.org'


@pytest.mark.parametrize('typed', ['a@b.co ', ' A@B.CO', 'ａ＠b.co'])
def test_an_address_is_stored_in_its_canonical_form(app, conn, typed):
    bob = add(app, conn, 'bob', email='bob@example.org')
    r = call(session_client(app, bob), 'post', '/api/me/email', {'password': GOOD, 'email': typed})
    assert r.status_code == 200 and refresh(conn, 'bob')['pending_email'] == 'a@b.co'


def test_the_email_change_needs_password_and_factor_and_a_new_address(app, conn):
    secret = new_secret()
    bob = add(app, conn, 'bob', email='bob@example.org', secret=secret)
    client = session_client(app, bob)
    base = {'password': GOOD, 'email': 'new@example.org'}
    assert call(client, 'post', '/api/me/email', {**base, 'password': OTHER}).status_code == 403
    assert call(client, 'post', '/api/me/email', base).status_code == 403
    assert call(client, 'post', '/api/me/email', {**base, 'code': code_at(secret)}).status_code == 200
    again = session_client(app, refresh(conn, 'bob'))
    assert call(again, 'post', '/api/me/email', {'password': GOOD, 'email': 'BOB@example.org', 'code': code_at(secret, offset=1)}).status_code == 409
    assert call(session_client(app, bob), 'post', '/api/me/email', base, origin='https://evil.example').status_code == 403


@pytest.mark.parametrize('round_', range(4))
def test_two_simultaneous_profile_confirmations_enrol_once(app, conn, round_):
    user = add(app, conn, 'bob')
    client = session_client(app, user)
    secret = call(client, 'post', '/api/me/mfa/totp/setup', {'password': GOOD}).get_json()['secret'].replace(' ', '')
    cookie = raw_cookie(client)
    code = code_at(secret)

    def job(i):
        c = app.test_client()
        c.set_cookie('session', cookie, domain=HOST)
        r = call(c, 'post', '/api/me/mfa/totp/confirm', {'code': code})
        return r.status_code
    results = concurrent(6, job)
    assert [r for r in results if isinstance(r, Exception)] == [] and results.count(200) == 1
    assert conn.execute("SELECT COUNT(*) FROM recovery_codes WHERE user_id = ?", (user['id'],)).fetchone()[0] == 10
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'user.factor_added'").fetchone()[0] == 1


# ===== 6b. Notifications de sécurité (EF-4.7, P-T11-6) ==========================================================================

def recipients(smtp):
    return [c.lower() for c in smtp.commands if c.lower().startswith('rcpt to')]


def test_security_notices_go_to_the_validated_address_only_and_never_carry_a_secret(tmp_path):
    app, conn, smtp = mail_world(tmp_path)
    try:
        secret = new_secret()
        row = add(app, conn, 'bob', secret=secret, email='bob@example.org', pending_email='pending@example.org')
        codes = second_factor.replace_recovery_codes(conn, row['id'])
        conn.commit()
        client = app.test_client()
        login(app, 'bob', client=client)
        assert mfa(app, client, codes[0]).status_code == 200                                    # code de secours : notification
        call(session_client(app, refresh(conn, 'bob')), 'post', '/api/me/password', {'current_password': GOOD, 'new_password': OTHER, 'code': codes[1]})
        app.extensions['mailer'].queue.drain()
        sent = recipients(smtp)
        assert len(sent) >= 2 and all('bob@example.org' in x for x in sent) and not any('pending@' in x for x in sent)
        blob = '\n'.join(smtp.messages)
        for forbidden in (secret, GOOD, OTHER, *codes, *[c.replace('-', '') for c in codes]):
            assert forbidden not in blob
    finally:
        smtp.stop()


def test_an_account_without_a_validated_address_gets_no_mail_and_the_action_still_works(tmp_path, caplog):
    app, conn, smtp = mail_world(tmp_path)
    try:
        row = add(app, conn, 'bob', pending_email='pending@example.org')
        with caplog.at_level(logging.INFO):
            r = call(session_client(app, row), 'post', '/api/me/password', {'current_password': GOOD, 'new_password': OTHER})
        app.extensions['mailer'].queue.drain()
        assert r.status_code == 200 and recipients(smtp) == []
        assert 'aucune adresse validée' in caplog.text
    finally:
        smtp.stop()


# ===== 7. Journaux : aucun secret ===============================================================================================

def test_no_log_line_and_no_audit_row_contains_a_secret_a_code_or_a_recovery_code(app, conn, caplog):
    caplog.set_level(logging.DEBUG)
    client, created = bootstrap_client(app, conn)
    secret = enroll_secret(client)
    code = code_at(secret)
    body = mfa_enroll(app, client, code).get_json()
    login(app, 'chef', client=app.test_client())
    c2 = app.test_client()
    login(app, 'chef', client=c2, ip=new_ip())
    mfa(app, c2, body['recovery_codes'][0])
    mfa(app, c2, '123456')
    blob = '\n'.join(r.getMessage() for r in caplog.records) + json.dumps([dict(r) for r in conn.execute("SELECT * FROM audit_log")])
    for forbidden in (secret, code, GOOD, created.token, *[c.replace('-', '') for c in body['recovery_codes']], *body['recovery_codes']):
        assert forbidden not in blob, forbidden[:6]
