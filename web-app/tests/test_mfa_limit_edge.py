"""
Limiteur du second facteur « n'échecs que » (rôle test) : 5 échecs / 15 min par compte, un succès ne consomme rien, aucun contournement par
alternance de succès ni par concurrence, aucun état partagé entre comptes ; mêmes garanties pour le mot de passe (compté avant le hachage,
restitué au succès). Écrit depuis EF-2.6, EF-S3 et la décision de recette (le limiteur ne compte que les échecs), pas depuis le code.
"""

import pytest

from app.audit import Actor
from app.auth import passwords, second_factor
from tests.pw_harness import (GOOD, KW, OTHER, FakeTime, add, call, code_at, concurrent, connect, cookie_payload, form_post, login, make_app,
                              mfa, new_ip, new_secret, raw_cookie, refresh, session_client)

CLI = Actor.cli()
WINDOW = 15 * 60


class Clock:
    def __init__(self, now=3_601_350.0):
        self.now = now

    def __call__(self):
        return self.now


def drive(app, clock):
    """Horloge des limiteurs pilotée par le test (avancer = laisser passer la fenêtre)."""
    for limiter in vars(app.extensions['limiters']).values():
        if hasattr(limiter, '_clock') and hasattr(limiter, 'hit'):
            limiter._clock = clock
    return app


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def app(tmp_path, clock):
    application = drive(make_app(tmp_path), clock)
    c = connect(application)
    # seuil du mot de passe relevé : ces tests portent sur le second facteur, pas sur le login
    application.config['SETTINGS_STORE'].set_many(c, {'ratelimit.password_per_identifier': 20, 'ratelimit.password_per_ip': 100}, actor=CLI)
    c.close()
    return application


@pytest.fixture
def conn(app):
    c = connect(app)
    yield c
    c.close()


def admin(app, conn, name='adm'):
    secret = new_secret()
    row = add(app, conn, name, admin=True, secret=secret)
    codes = second_factor.replace_recovery_codes(conn, row['id'])
    conn.commit()
    return row, secret, codes


def pending(app, name='adm'):
    client = app.test_client()
    r = login(app, name, client=client, ip=new_ip())
    assert r.status_code == 200 and r.get_json()['step'] == 'mfa', r.get_data()
    return client


def count_verifications(monkeypatch):
    calls = []
    original_totp, original_recovery = second_factor.verify_totp, second_factor.consume_recovery
    monkeypatch.setattr(second_factor, 'verify_totp', lambda *a, **k: (calls.append('totp'), original_totp(*a, **k))[1])
    monkeypatch.setattr(second_factor, 'consume_recovery', lambda *a, **k: (calls.append('recovery'), original_recovery(*a, **k))[1])
    return calls


def test_a_success_consumes_nothing_twelve_successful_logins_are_never_blocked(app, conn, monkeypatch):
    row, secret, codes = admin(app, conn)
    fake = FakeTime()
    monkeypatch.setattr(second_factor, 'time', fake)
    for i in range(12):
        fake.now += 61                                                       # un nouveau pas à chaque connexion (anti-rejeu)
        client = pending(app)
        r = mfa(app, client, code_at(secret, fake.now), ip=new_ip())
        assert r.status_code == 200, (i, r.status_code, r.get_data())


def test_exactly_five_failures_block_the_account_and_the_block_ends_with_the_window(app, conn, clock):
    row, secret, codes = admin(app, conn)
    statuses = []
    for _ in range(5):
        client = pending(app)
        statuses.append(mfa(app, client, '000000', ip=new_ip()).status_code)
    assert statuses == [401] * 5
    client = pending(app)
    blocked = mfa(app, client, code_at(secret), ip=new_ip())
    assert blocked.status_code == 429 and 0 < int(blocked.headers['Retry-After']) <= WINDOW * 2
    clock.now += WINDOW * 2 + 1                                              # la fenêtre glissante est vidée
    client = pending(app)
    assert mfa(app, client, code_at(secret), ip=new_ip()).status_code == 200


def test_the_cap_holds_across_fresh_pending_states_and_a_blocked_attempt_is_never_evaluated(app, conn, monkeypatch):
    row, secret, codes = admin(app, conn)
    calls = count_verifications(monkeypatch)
    for _ in range(3):                                                        # 3 états de connexion, 2 mauvais codes chacun (le 6e est refusé)
        client = pending(app)
        for _ in range(2):
            mfa(app, client, '000000', ip=new_ip())
    assert calls == ['totp'] * 5
    client = pending(app)
    before = len(calls)
    assert mfa(app, client, code_at(secret), ip=new_ip()).status_code == 429
    assert mfa(app, client, codes[0], ip=new_ip()).status_code == 429
    assert len(calls) == before                                              # rien n'est vérifié tant que le compte est bloqué


def test_alternating_successes_does_not_buy_extra_failures(app, conn, monkeypatch):
    row, secret, codes = admin(app, conn)
    calls = count_verifications(monkeypatch)
    verdicts = []
    for i in range(4):
        verdicts.append(mfa(app, pending(app), '000000', ip=new_ip()).status_code)             # échec
        verdicts.append(mfa(app, pending(app), codes[i], ip=new_ip()).status_code)             # succès (code de secours) : ne consomme rien
    verdicts.append(mfa(app, pending(app), '000000', ip=new_ip()).status_code)                 # 5e échec cumulé
    assert verdicts == [401, 200] * 4 + [401]
    assert mfa(app, pending(app), codes[4], ip=new_ip()).status_code == 429                    # le succès n'a rien « remis à zéro » : bloqué
    assert mfa(app, pending(app), '000000', ip=new_ip()).status_code == 429
    assert calls.count('totp') == 5 and calls.count('recovery') == 4


def test_failures_of_one_account_never_block_another_and_the_counts_are_per_account(app, conn):
    a, secret_a, codes_a = admin(app, conn, 'ada')
    b, secret_b, codes_b = admin(app, conn, 'bea')
    for _ in range(5):
        mfa(app, pending(app, 'ada'), '000000', ip='198.51.100.1')
    assert mfa(app, pending(app, 'ada'), code_at(secret_a), ip='198.51.100.1').status_code == 429
    assert mfa(app, pending(app, 'bea'), code_at(secret_b), ip='198.51.100.1').status_code == 200       # même IP, autre compte : intact
    for _ in range(4):
        mfa(app, pending(app, 'bea'), '000000', ip='198.51.100.2')
    assert mfa(app, pending(app, 'bea'), codes_b[0], ip='198.51.100.2').status_code == 200            # 4 échecs : pas encore bloquée
    limiter = app.extensions['limiters'].mfa_account
    assert limiter.peek(a['id'])[0] is False and limiter.peek(b['id'])[0] is True


def test_a_blocked_answer_is_the_same_for_a_good_and_a_bad_code_and_never_changes_the_pending_tries(app, conn):
    row, secret, codes = admin(app, conn)
    for _ in range(5):
        mfa(app, pending(app), '000000', ip=new_ip())
    client = pending(app)
    good, bad = mfa(app, client, code_at(secret), ip=new_ip()), mfa(app, client, '000000', ip=new_ip())
    assert good.status_code == bad.status_code == 429 and good.get_json() == bad.get_json()
    assert app.extensions['pending'].get(cookie_payload(app, client)['pending_id']).tries == 0


@pytest.mark.parametrize('burst', [10, 16])
def test_a_concurrent_burst_cannot_run_more_than_five_verifications(app, conn, monkeypatch, burst):
    import threading
    row, secret, codes = admin(app, conn)
    barrier = threading.Barrier(burst, timeout=2)
    calls = []
    original = second_factor.verify_totp

    def slow(*args, **kwargs):
        calls.append(1)
        try:
            barrier.wait()                                                   # toutes les requêtes ont franchi le contrôle avant qu'une seule échec ne soit compté
        except threading.BrokenBarrierError:
            pass
        return original(*args, **kwargs)
    monkeypatch.setattr(second_factor, 'verify_totp', slow)
    cookies = []
    for _ in range(burst):
        cookies.append(raw_cookie(pending(app)))
    from tests.pw_harness import HOST

    def job(i):
        client = app.test_client()
        client.set_cookie('session', cookies[i], domain=HOST)
        return mfa(app, client, '000000', ip=new_ip()).status_code
    concurrent(burst, job)
    assert len(calls) <= 5, f"{len(calls)} vérifications pour un compte limité à 5 échecs"


# -- Chaque chemin compte ses échecs, bloque au 6e, et ne compte pas ses succès ---------------------------------------------------

def test_every_profile_and_account_path_shares_the_same_five_failure_budget(app, conn, monkeypatch):
    row, secret, codes = admin(app, conn, 'kay')
    row = refresh(conn, 'kay')
    client = session_client(app, row)
    calls = count_verifications(monkeypatch)
    # 5 échecs répartis sur 5 chemins différents
    assert call(client, 'delete', '/api/me/mfa/totp', {'password': GOOD, 'code': '000000'}).status_code == 403
    assert call(client, 'post', '/api/me/mfa/recovery-codes', {'password': GOOD, 'code': '000000'}).status_code == 403
    assert call(client, 'post', '/api/me/password', {'current_password': GOOD, 'new_password': OTHER, 'code': '000000'}).status_code == 403
    assert call(client, 'post', '/api/me/email', {'password': GOOD, 'email': 'x@example.org', 'code': '000000'}).status_code == 403
    assert mfa(app, pending(app, 'kay'), '000000', ip=new_ip()).status_code == 401
    before = len(calls)
    for path_call in (lambda: call(client, 'delete', '/api/me/mfa/totp', {'password': GOOD, 'code': code_at(secret)}),
                      lambda: call(client, 'post', '/api/me/mfa/recovery-codes', {'password': GOOD, 'code': code_at(secret)}),
                      lambda: call(client, 'post', '/api/me/password', {'current_password': GOOD, 'new_password': OTHER, 'code': codes[0]}),
                      lambda: call(client, 'post', '/api/me/email', {'password': GOOD, 'email': 'y@example.org', 'code': codes[1]})):
        assert path_call().status_code == 429
    assert mfa(app, pending(app, 'kay'), code_at(secret), ip=new_ip()).status_code == 429
    assert len(calls) == before


def test_successful_profile_operations_never_consume_the_budget(app, conn, monkeypatch):
    row, secret, codes = admin(app, conn, 'kay')
    fake = FakeTime()
    monkeypatch.setattr(second_factor, 'time', fake)
    for i in range(8):
        fake.now += 61
        client = session_client(app, refresh(conn, 'kay'))
        r = call(client, 'post', '/api/me/mfa/recovery-codes', {'password': GOOD, 'code': code_at(secret, fake.now)})
        assert r.status_code == 200, (i, r.status_code, r.get_data())


def test_the_enrollment_confirmation_counts_failures_only(app, conn):
    from app.auth import tokens
    created = tokens.create(conn, 'bootstrap', actor=CLI, email='chef@example.org', is_admin_grant=True)
    client = app.test_client()
    form_post(app, '/auth/token/consume', {'token': created.token, 'username': 'chef', 'password': GOOD}, client=client)
    import re
    html = client.get('/auth/password/enroll', **KW).get_data(as_text=True)
    secret = re.sub(r'\s', '', re.search(r'(?:[A-Z2-7]{4}\s){7}[A-Z2-7]{4}', html).group(0))
    results = [form_post(app, '/auth/password/enroll', {'code': '000000'}, client=client).status_code for _ in range(4)]
    assert results == [401] * 4
    ok = form_post(app, '/auth/password/enroll', {'code': code_at(secret)}, client=client)
    assert ok.status_code == 200                                              # 4 échecs puis le bon code : accepté (5e essai de l'état)


def test_a_valid_webauthn_login_counts_nothing_and_a_failed_one_counts(app, conn):
    from tests.test_webauthn_edge import add_key, assertion, options, verify
    row = add(app, conn, 'kay', admin=True)
    auth = add_key(conn, row)
    for _ in range(8):                                                        # 8 succès : jamais bloqué
        client = pending(app, 'kay')
        opts = options(app, client).get_data(as_text=True)
        assert verify(app, client, assertion(auth, opts)).status_code == 200
    for _ in range(5):
        client = pending(app, 'kay')
        opts = options(app, client).get_data(as_text=True)
        assert verify(app, client, assertion(auth, opts, origin='https://evil.example')).status_code == 401
    client = pending(app, 'kay')
    opts = options(app, client).get_data(as_text=True)
    assert verify(app, client, assertion(auth, opts)).status_code == 429


# -- Le mot de passe : compté avant le hachage, restitué au succès ----------------------------------------------------------------

@pytest.fixture
def strict_app(tmp_path, clock):
    return drive(make_app(tmp_path / 'strict'), clock)                       # seuils par défaut : 5 par identifiant, 20 par IP


def test_twenty_successful_logins_never_block_and_five_failures_still_do(strict_app, clock):
    app = strict_app
    c = connect(app)
    add(app, c, 'bob')
    for _ in range(20):
        assert login(app, 'bob', ip='198.51.100.9').status_code == 200
    codes = [login(app, 'bob', OTHER, ip='198.51.100.9').status_code for _ in range(7)]
    assert codes == [401] * 5 + [429] * 2
    assert login(app, 'bob', GOOD, ip='198.51.100.10').status_code == 429                    # même avec le bon mot de passe pendant le blocage
    clock.now += WINDOW * 2 + 1
    assert login(app, 'bob', GOOD, ip='198.51.100.10').status_code == 200
    c.close()


def test_alternating_a_success_does_not_reset_the_failures_of_the_password_limiter(strict_app):
    app = strict_app
    c = connect(app)
    add(app, c, 'bob')
    seq = []
    for i in range(5):
        seq.append(login(app, 'bob', OTHER, ip=new_ip()).status_code)
        seq.append(login(app, 'bob', GOOD, ip=new_ip()).status_code if i < 4 else 0)
    assert seq[:8] == [401, 200] * 4 and seq[8] == 401
    assert login(app, 'bob', GOOD, ip=new_ip()).status_code == 429                              # 5 échecs cumulés : bloqué malgré 4 succès
    c.close()


def test_twenty_simultaneous_wrong_passwords_run_at_most_five_hashes(strict_app, monkeypatch):
    app = strict_app
    c = connect(app)
    add(app, c, 'bob')
    hashed = []
    original = passwords.verify_password
    monkeypatch.setattr(passwords, 'verify_password', lambda *a, **k: (hashed.append(1), original(*a, **k))[1])
    results = concurrent(20, lambda i: login(app, 'bob', OTHER, ip=f'203.0.113.{i + 1}').status_code)
    assert sorted(set(results)) == [401, 429] and results.count(401) == 5 and len(hashed) == 5
    c.close()


def test_a_correct_password_restores_exactly_its_own_slot_for_the_ip_limiter_too(strict_app):
    app = strict_app
    c = connect(app)
    add(app, c, 'bob')
    limiters = app.extensions['limiters']
    ip = '198.51.100.77'
    for _ in range(10):
        assert login(app, 'bob', ip=ip).status_code == 200
    from app.ratelimit import ip_key
    assert limiters.password_ip.peek(ip_key(ip))[0] is True
    for i in range(20):
        assert login(app, f'inconnu{i}', OTHER, ip=ip).status_code in (401, 429)
    assert limiters.password_ip.peek(ip_key(ip))[0] is False                                    # 20 échecs : l'IP est bloquée
    c.close()


def test_the_limiter_state_is_per_application_and_resets_with_a_new_one(tmp_path, clock):
    a = drive(make_app(tmp_path / 'a'), clock)
    b = drive(make_app(tmp_path / 'b'), Clock())
    ca, cb = connect(a), connect(b)
    ra, secret_a, _ = admin(a, ca)
    rb, secret_b, _ = admin(b, cb)
    for _ in range(5):
        mfa(a, pending(a), '000000', ip=new_ip())
    assert mfa(a, pending(a), code_at(secret_a), ip=new_ip()).status_code == 429
    assert mfa(b, pending(b), code_at(secret_b), ip=new_ip()).status_code == 200
    ca.close()
    cb.close()
    assert ra['id'] == rb['id']                                              # même identifiant en base, deux applications : deux compteurs
