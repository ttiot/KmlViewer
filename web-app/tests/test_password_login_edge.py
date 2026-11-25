"""
T10 en profondeur (rôle test), écrit depuis la spec (admin-space EF-1.5 à 1.12, EF-S3, EF-S9, EF-9.2 ; CA-7, 9, 14, 15, 16, 45, 46 ;
M1, M2, M3, r8, r9), pas depuis le code.

 1. énumération de comptes : inconnu / désactivé / SSO / none / hash absent ou corrompu / mauvais mot de passe => même réponse, durées proches
 2. limiteur : 429 homogène, clés normalisées, /64 IPv6, X-Forwarded-For sans effet, AVANT le sémaphore (20 requêtes simultanées, CA-46)
 3. sémaphore saturé (HashBusy) : 429 générique identique pour tous
 4. mfa_pending : cookie sans user_id, expiration serveur, essais jamais remis à zéro, liaison au compte, rejeu, mémoire bornée
 5. cookies forgés ou anciens, fixation de session, attributs du cookie, login-CSRF
 6. redirection ouverte (next)
 7. mot de passe oublié : réponse identique, envoi hors du thread de la requête, destinataire, lien, file saturée
 8. corps hostiles (JSON profond, surrogats, types, tailles, corps sans Content-Length)
 9. audit et journaux : jamais le mot de passe, une ligne par seuil
"""

import io
import json
import re
import statistics
import threading
import time
from datetime import timedelta
from urllib.parse import urlsplit

import pytest
from cryptography.fernet import Fernet

from app import create_app, db as database
from app.audit import Actor
from app.auth import csrf, passwords, tokens
from app.auth.pending import PendingStore
from app.config import TestingConfig
from tests.fake_smtp import FakeSMTP
from tests.pw_harness import freeze_limiters

HOST = 'traces.example.com'
KEY = Fernet.generate_key().decode()
CLI = Actor.cli()
GOOD = 'cheval-agrafe-lune-42'
WRONG = 'cheval-agrafe-lune-43'
HASH = passwords.hash_password(GOOD)
KW = {'base_url': f'https://{HOST}'}
ORIGIN = f'https://{HOST}'


class Prod(TestingConfig):
    TESTING = False
    INBOX_ENABLED = False
    SECRET_KEY = 'k' * 40
    SESSION_COOKIE_SECURE = None


def make_app(tmp_path, **extra):
    app = create_app(Prod, overrides={'DATA_DIR': str(tmp_path / 'data'), 'AUTH_MODE': 'password', 'TRUSTED_HOSTS': [HOST],
                                      'SECRETS_KEY': KEY, 'PUBLIC_URL': ORIGIN, **extra})
    return freeze_limiters(app)                     # horloge figée : aucun verdict ne dépend de l'heure réelle (instabilité 1/5267)


@pytest.fixture
def app(tmp_path):
    return make_app(tmp_path)


@pytest.fixture
def conn(app):
    c = database.connect(app.config['DB_PATH'])
    yield c
    c.close()


_ips = iter(f'192.0.{a}.{b}' for a in range(2, 250) for b in range(1, 255))


def new_ip():
    return next(_ips)


def add(conn, name, *, admin=False, active=True, email=None, method='password', password=HASH, factor=False, token='tok', keep_hash=False):
    conn.execute("INSERT INTO users (sub, username, email, is_admin, is_active, auth_method, password_hash, session_token) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (f'pwd:{name}' if method == 'password' else f'sub:{name}', name, email, int(admin), int(active), method,
                  password if (method == 'password' or keep_hash) else None, token))
    if factor:
        cols = {r['name'] for r in conn.execute("PRAGMA table_info(users)")}
        for column, kind in (('totp_secret', 'BLOB'), ('totp_enabled_at', 'TEXT')):
            if column not in cols:
                conn.execute(f"ALTER TABLE users ADD COLUMN {column} {kind}")
        conn.execute("UPDATE users SET totp_secret = x'00', totp_enabled_at = '2026-01-01T00:00:00Z' WHERE username = ?", (name,))
    conn.commit()
    return conn.execute("SELECT * FROM users WHERE username = ?", (name,)).fetchone()


_token_cache = {}


def csrf_token(app):
    """Jeton CSRF de formulaire public : sans état, valable une heure, identique pour toutes les applications de même SECRET_KEY."""
    if 'v' not in _token_cache:
        html = app.test_client().get('/auth/password/login', **KW).get_data(as_text=True)
        _token_cache['v'] = re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)
    return _token_cache['v']


def post(app, path, payload, *, ip=None, client=None, headers=None, raw=None, content_type='application/json', origin=ORIGIN,
         token=True):
    client = client or app.test_client()
    hdrs = {}
    if token:
        hdrs['X-CSRF-Token'] = token if isinstance(token, str) else csrf_token(app)
    if origin:
        hdrs['Origin'] = origin
    hdrs.update(headers or {})
    body = {'data': raw, 'content_type': content_type} if raw is not None else {'json': payload}
    return client.post(path, **body, headers=hdrs, environ_overrides={'REMOTE_ADDR': ip or new_ip()}, **KW)


def login(app, username, password, **kw):
    extra = kw.pop('extra', {})
    return post(app, '/auth/password/login', {'username': username, 'password': password, **extra}, **kw)


def forgot(app, identifier, **kw):
    return post(app, '/auth/password/forgot', {'identifier': identifier}, **kw)


def signature(response):
    """Tout ce qu'un observateur voit d'une réponse : code, corps, en-têtes (hors date et longueur), présence d'un cookie."""
    headers = {k: v for k, v in response.headers.items() if k.lower() not in ('date', 'content-length', 'set-cookie')}
    return response.status_code, response.get_data(), tuple(sorted(headers.items())), 'Set-Cookie' in response.headers


def raw_cookie(client):
    cookie = client.get_cookie('session', domain=HOST) or client.get_cookie('session')
    return cookie.value if cookie else None


def cookie_payload(app, client):
    """Contenu décodé du cookie de session du client de test (ce que le navigateur renvoie)."""
    cookie = client.get_cookie('session', domain=HOST) or client.get_cookie('session')
    if cookie is None:
        return {}
    serializer = app.session_interface.get_signing_serializer(app)
    return dict(serializer.loads(cookie.value))


# ===== 1. Énumération de comptes ================================================================================================

KINDS = {                       # clé -> (propriétés du compte, mot de passe saisi)
    'wrong': ({}, WRONG), 'unknown': (None, GOOD), 'disabled': ({'active': False}, GOOD), 'disabled_wrong': ({'active': False}, WRONG),
    'sso': ({'method': 'oidc'}, GOOD), 'sso_wrong': ({'method': 'oidc'}, WRONG), 'none': ({'method': 'none'}, GOOD),
    'nohash': ({'password': None}, GOOD), 'garbage': ({'password': 'pas-un-hash'}, GOOD), 'emptyhash': ({'password': ''}, GOOD),
    'truncated': ({'password': HASH[:30]}, GOOD), 'argon2i': ({'password': HASH.replace('$argon2id$', '$argon2i$', 1)}, GOOD),
    'short': ({}, 'x'), 'toolong': ({}, 'x' * 129), 'huge': ({}, 'x' * 1024),
}
SPELLINGS = [lambda n: n, lambda n: n.upper(), lambda n: f'  {n}  ', lambda n: n.translate({ord(c): ord(c) + 0xFEE0 for c in 'abcdefghijklmnopqrstuvwxyz0123456789'}),
             lambda n: n + '\x00', lambda n: n + '\n']


def setup_kind(conn, kind, name):
    props, password = KINDS[kind]
    if props is not None:
        add(conn, name, **props)
    return password


def test_every_failure_kind_gives_the_same_status_body_headers_and_no_cookie(app, conn):
    """CA-7 : compte inconnu, désactivé (même avec le bon mot de passe), SSO, none, hash absent/corrompu, mauvais mot de passe."""
    sigs, n = set(), 0
    for kind in KINDS:
        for spell in SPELLINGS:
            n += 1
            name = f'compte{n}'
            password = setup_kind(conn, kind, name)
            sigs.add(signature(login(app, spell(name), password)))
    assert len(sigs) == 1, sigs
    status, body, _, cookie = next(iter(sigs))
    assert status == 401 and not cookie
    assert json.loads(body) == {'success': False, 'error': 'Identifiant ou mot de passe incorrect.'}


@pytest.mark.parametrize('username', ['alice', 'ALICE', ' Alice ', 'ａｌｉｃｅ', 'Ａlice'])
def test_case_whitespace_and_compatibility_variants_of_the_identifier_log_in(app, conn, username):
    add(conn, 'alice')
    response = login(app, username, GOOD)
    assert response.status_code == 200 and response.get_json()['success'] is True


def test_an_sso_or_none_account_never_logs_in_by_password_even_with_a_valid_hash_in_base_EF110(app, conn):
    """EF-1.10 : un compte SSO n'a jamais de mot de passe ; ligne incohérente (hash présent) => refus quand même."""
    for name, method in (('ssohash', 'oidc'), ('nonehash', 'none')):
        add(conn, name, method=method, keep_hash=True)
        client = app.test_client()
        assert login(app, name, GOOD, client=client).status_code == 401 and raw_cookie(client) is None


def test_a_disabled_or_sso_account_with_the_right_password_never_gets_a_session(app, conn):
    for name, kw in (('disabled', {'active': False}), ('ssouser', {'method': 'oidc'}), ('nonemode', {'method': 'none'})):
        add(conn, name, **kw)
        client = app.test_client()
        response = login(app, name, GOOD, client=client)
        assert response.status_code == 401 and cookie_payload(app, client) == {}, name


def test_durations_of_the_failure_kinds_are_comparable(app, conn):
    """M1 : un hachage factice est vérifié quand le compte est absent : la durée ne révèle ni l'existence ni l'état du compte."""
    kinds = ['wrong', 'unknown', 'disabled', 'sso', 'none', 'nohash', 'garbage']
    samples = {k: [] for k in kinds}
    for round_ in range(14):
        for kind in kinds:
            name = f'dur{kind}{round_}'
            password = setup_kind(conn, kind, name)
            started = time.perf_counter()
            response = login(app, name, password)
            samples[kind].append(time.perf_counter() - started)
            assert response.status_code == 401
    medians = {k: statistics.median(v[2:]) for k, v in samples.items()}      # les deux premiers tours servent de chauffe
    assert max(medians.values()) / min(medians.values()) < 1.6, medians


def test_an_overlong_password_is_refused_like_a_wrong_one_without_hashing(app, conn, monkeypatch):
    add(conn, 'alice')
    calls = []
    original = passwords._gate.slot
    monkeypatch.setattr(passwords._gate, 'slot', lambda: (calls.append(1), original())[1])
    big = login(app, 'alice', 'é' * 1000)
    assert big.status_code == 401 and calls == []                      # le coût d'un énorme mot de passe est borné


# ===== 2. Limiteur ==============================================================================================================

def test_the_identifier_limit_is_identical_for_existing_and_unknown_identifiers(app, conn):
    """CA-7 / M1 : comportement identique, 429 générique, avec Retry-After de même ordre ; les variantes du même identifiant partagent la clé."""
    add(conn, 'alice')
    seq = {}
    for name in ('alice', 'fantome'):
        seq[name] = []
        variants = [name, name.upper(), f' {name} ', name.title(), name, name.upper(), name, name, name, name]
        for variant in variants:
            r = login(app, variant, WRONG)
            seq[name].append((r.status_code, r.get_data(), r.headers.get('Retry-After')))
    assert [s[0] for s in seq['alice']] == [s[0] for s in seq['fantome']]
    assert seq['alice'][0][0] == 401 and seq['alice'][-1][0] == 429
    limited = [s for s in seq['alice'] if s[0] == 429]
    assert limited and {s[1] for s in limited} == {s[1] for s in seq['fantome'] if s[0] == 429}
    assert all(abs(int(a[2]) - int(b[2])) <= 5 for a, b in zip(limited, [s for s in seq['fantome'] if s[0] == 429]))


def test_the_sixth_attempt_on_the_same_identifier_is_refused_and_the_window_slides(app, conn):
    """CA-7 : seuil 5 par défaut => la 6e est refusée, même depuis des IP différentes."""
    add(conn, 'alice')
    statuses = [login(app, 'alice', WRONG).status_code for _ in range(8)]
    assert statuses[:5] == [401] * 5 and set(statuses[5:]) == {429}
    assert login(app, 'alice', GOOD).status_code == 429               # même avec le bon mot de passe pendant le refus


def test_a_successful_login_while_limited_is_refused_and_a_different_account_is_not_affected(app, conn):
    add(conn, 'alice')
    add(conn, 'bob')
    for _ in range(6):
        login(app, 'alice', WRONG)
    assert login(app, 'bob', GOOD).status_code == 200


def test_the_ip_limit_applies_across_identifiers_and_ignores_x_forwarded_for(app, conn):
    """EF-S3 : par IP aussi ; sans TRUST_PROXY, un X-Forwarded-For forgé ne change pas la clé."""
    ip = '198.51.100.77'
    statuses = []
    for i in range(30):
        spoof = {'X-Forwarded-For': f'203.0.113.{i + 1}', 'X-Real-IP': f'203.0.113.{i + 1}', 'Forwarded': f'for=203.0.113.{i + 1}'}
        statuses.append(login(app, f'inconnu{i}', WRONG, ip=ip, headers=spoof).status_code)
    assert statuses[0] == 401 and statuses[-1] == 429
    first = statuses.index(429)
    assert 10 <= first <= 40, statuses
    assert set(statuses[first:]) == {429}


def test_ipv6_addresses_of_the_same_64_share_a_bucket_and_other_prefixes_do_not(app):
    seen = []
    for i in range(30):
        seen.append(login(app, f'v6{i}', WRONG, ip=f'2001:db8:1:1::{i + 1:x}').status_code)
    assert 429 in seen
    assert login(app, 'autre', WRONG, ip='2001:db8:2:2::1').status_code == 401        # autre /64 : intact
    mapped = [login(app, f'm{i}', WRONG, ip='::ffff:198.51.100.9').status_code for i in range(3)]
    plain = [login(app, f'n{i}', WRONG, ip='198.51.100.9').status_code for i in range(40)]
    assert 429 in plain and mapped[0] == 401


def test_with_trust_proxy_the_forwarded_address_is_the_key(tmp_path):
    app = make_app(tmp_path, TRUST_PROXY=1)
    statuses = [login(app, f'p{i}', WRONG, ip='10.0.0.1', headers={'X-Forwarded-For': '203.0.113.5', 'X-Forwarded-Proto': 'https'}).status_code
                for i in range(35)]
    assert 429 in statuses
    assert login(app, 'z', WRONG, ip='10.0.0.1', headers={'X-Forwarded-For': '203.0.113.6', 'X-Forwarded-Proto': 'https'}).status_code == 401


def concurrent(n, job):
    results, barrier = [None] * n, threading.Barrier(n)

    def run(i):
        barrier.wait()
        results[i] = job(i)
    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    return results


def test_twenty_simultaneous_requests_over_the_identifier_limit_never_touch_the_hash_gate(app, conn, monkeypatch):
    """CA-46 (r9) : 20 requêtes simultanées => toutes en 429, AUCUNE acquisition du sémaphore."""
    add(conn, 'alice')
    for _ in range(6):
        login(app, 'alice', WRONG)
    acquisitions = []
    original = passwords._gate.slot
    monkeypatch.setattr(passwords._gate, 'slot', lambda: (acquisitions.append(1), original())[1])
    results = concurrent(20, lambda i: login(app, 'alice', GOOD, ip=f'203.0.113.{i + 1}').status_code)
    assert results == [429] * 20 and acquisitions == []
    results = concurrent(20, lambda i: login(app, 'ALICE ', WRONG, ip=f'203.0.114.{i + 1}').status_code)
    assert results == [429] * 20 and acquisitions == []


def test_twenty_simultaneous_requests_over_the_ip_limit_never_touch_the_hash_gate(app, conn, monkeypatch):
    for i in range(40):
        login(app, f'warm{i}', WRONG, ip='198.51.100.200')
    acquisitions = []
    original = passwords._gate.slot
    monkeypatch.setattr(passwords._gate, 'slot', lambda: (acquisitions.append(1), original())[1])
    results = concurrent(20, lambda i: login(app, f'neuf{i}', GOOD, ip='198.51.100.200').status_code)
    assert results == [429] * 20 and acquisitions == []


def test_a_limited_request_does_not_consume_the_identifier_bucket_when_the_ip_refuses(app, conn):
    """Un refus par IP ne consomme pas l'identifiant (sinon une IP hostile verrouillerait un compte)."""
    add(conn, 'alice')
    for i in range(40):
        login(app, f'bruit{i}', WRONG, ip='198.51.100.201')
    for _ in range(20):
        assert login(app, 'alice', WRONG, ip='198.51.100.201').status_code == 429
    assert login(app, 'alice', GOOD, ip=new_ip()).status_code == 200


def test_a_successful_login_does_not_count_against_the_limit(app, conn):
    """Décision de recette : le comptage se fait AVANT le hachage (r9) mais un SUCCÈS rend son accès : jamais de blocage par ses succès."""
    add(conn, 'alice')
    statuses = [login(app, 'alice', GOOD).status_code for _ in range(12)]
    assert statuses == [200] * 12


# ===== 3. Sémaphore de hachage saturé ===========================================================================================

def test_a_saturated_hash_gate_answers_the_same_429_for_every_identifier(app, conn, monkeypatch):
    add(conn, 'alice')
    gate = passwords.HashGate(slots=1, wait_s=0.05)
    monkeypatch.setattr(passwords, '_gate', gate)
    held, release = threading.Event(), threading.Event()

    def hold():
        with gate.slot():
            held.set()
            release.wait(10)
    holder = threading.Thread(target=hold)
    holder.start()
    held.wait(5)
    try:
        sigs = {signature(login(app, name, GOOD)) for name in ('alice', 'inconnu', 'ALICE', 'x' * 100)}
        retry = {login(app, name, GOOD).headers.get('Retry-After') for name in ('alice2', 'inconnu2')}
    finally:
        release.set()
        holder.join(5)
    assert len(sigs) == 1 and next(iter(sigs))[0] == 429 and next(iter(sigs))[3] is False
    assert retry == {'2'}
    assert login(app, 'alice', GOOD).status_code in (200, 429)       # la place est rendue


def test_the_gate_is_released_after_a_failure_an_exception_and_a_busy_refusal(app, conn):
    gate = passwords.HashGate(slots=2, wait_s=0.2)
    for _ in range(5):
        with pytest.raises(RuntimeError):
            with gate.slot():
                raise RuntimeError
    with gate.slot(), gate.slot():
        pass


# ===== 4. mfa_pending ===========================================================================================================

def start_pending(app, conn, name='adm', *, factor=True, **kw):
    add(conn, name, admin=True, factor=factor, **kw)
    client = app.test_client()
    response = login(app, name, GOOD, client=client)
    assert response.status_code == 200 and response.get_json()['success'], response.get_data()
    return client, response.get_json()


def test_the_pending_cookie_carries_only_an_opaque_identifier_never_the_account(app, conn):
    """EF-1.6, CA-9 : clé distincte `pending_id`, ≥ 128 bits, aucun user_id ni jeton de session."""
    client, body = start_pending(app, conn)
    assert body['step'] == 'mfa'
    payload = cookie_payload(app, client)
    assert set(payload) == {'pending_id'}
    assert len(payload['pending_id']) >= 22 and re.fullmatch(r'[A-Za-z0-9_-]+', payload['pending_id'])
    raw = raw_cookie(client)
    assert raw and 'user_id' not in raw


def test_pending_identifiers_are_unique_and_unguessable(app):
    store = PendingStore()
    ids = {store.create(1, 'mfa') for _ in range(2000)}
    assert len(ids) == 2000 and all(len(i) >= 43 for i in ids)


def test_a_pending_state_opens_no_protected_route(app, conn):
    """CA-9 : toutes les routes protégées répondent comme sans session."""
    client, _ = start_pending(app, conn)
    anonymous = app.test_client()
    for method, path in (('get', '/'), ('get', '/api/activities'), ('get', '/api/me'), ('get', '/api/map-config'),
                         ('get', '/api/dashboard'), ('post', '/api/export/csv'), ('get', '/admin/'), ('get', '/api/invitations')):
        a = getattr(client, method)(path, **KW)
        b = getattr(anonymous, method)(path, **KW)
        assert (a.status_code, a.headers.get('Location')) == (b.status_code, b.headers.get('Location')), path
        assert a.status_code in (401, 302, 403, 404, 405)


def test_the_pending_state_expires_server_side_whatever_the_cookie_says(app, conn):
    clock = [1000.0]
    app.extensions['pending']._clock = lambda: clock[0]
    client, _ = start_pending(app, conn)
    assert client.get('/auth/password/mfa', **KW).status_code == 200                    # page du second facteur (T11)
    clock[0] += 299
    assert client.get('/auth/password/mfa', **KW).status_code == 200
    clock[0] += 2
    response = client.get('/auth/password/mfa', **KW)
    assert response.status_code == 302 and '/auth/password/login' in response.headers['Location']
    post_ = post(app, '/auth/password/mfa', {'code': '000000'}, client=client)
    assert post_.status_code in (302, 401, 400)
    assert app.extensions['pending'].size() == 0


def test_attempts_are_counted_on_the_server_and_a_replayed_cookie_never_resets_them(app, conn):
    client, _ = start_pending(app, conn)
    saved = raw_cookie(client)
    codes = []
    for _ in range(4):
        codes.append(post(app, '/auth/password/mfa', {'code': '000000'}, client=client).status_code)
    # rejeu d'un ANCIEN cookie (copie prise avant les essais) dans un autre client : même compteur
    other = app.test_client()
    other.set_cookie('session', saved, domain=HOST)
    codes.append(post(app, '/auth/password/mfa', {'code': '000000'}, client=other).status_code)         # 5e essai : plafond
    assert app.extensions['pending'].size() == 0
    after = post(app, '/auth/password/mfa', {'code': '000000'}, client=client)
    assert after.status_code in (302, 401) and (after.get_json() or {}).get('restart', True)
    again = app.test_client()
    again.set_cookie('session', saved, domain=HOST)
    assert again.get('/auth/password/mfa', **KW).status_code == 302                                  # plus aucun état


def test_an_attempt_without_csrf_token_or_with_a_foreign_origin_is_not_counted(app, conn):
    client, _ = start_pending(app, conn)
    for _ in range(10):
        assert post(app, '/auth/password/mfa', {'code': '1'}, client=client, token=False).status_code in (400, 403)
        assert post(app, '/auth/password/mfa', {'code': '1'}, client=client, origin='https://evil.example').status_code == 403
    pid = cookie_payload(app, client)['pending_id']
    assert app.extensions['pending'].get(pid).tries == 0


def test_the_state_is_bound_to_the_account_and_dies_with_any_security_change(app, conn):
    for change in ("UPDATE users SET session_token = 'autre'", "UPDATE users SET password_changed_at = '2030-01-01T00:00:00Z'",
                   "UPDATE users SET is_active = 0", "UPDATE users SET auth_method = 'oidc'", "DELETE FROM users"):
        add_name = f'adm{abs(hash(change)) % 10000}'
        client, _ = start_pending(app, conn, add_name)
        conn.execute(change)
        conn.commit()
        response = client.get('/auth/password/mfa', **KW)
        assert response.status_code == 302 and '/auth/password/login' in response.headers['Location'], change
        conn.execute("DELETE FROM users")
        conn.commit()
        app.extensions['pending']._states.clear()


def test_a_second_login_replaces_the_pending_state_and_the_old_id_is_dead(app, conn):
    client, _ = start_pending(app, conn)
    first = cookie_payload(app, client)['pending_id']
    assert login(app, 'adm', GOOD, client=client).status_code == 200
    second = cookie_payload(app, client)['pending_id']
    assert first != second
    stale = app.test_client()
    stale.set_cookie('session', app.session_interface.get_signing_serializer(app).dumps({'pending_id': first}), domain=HOST)
    assert stale.get('/auth/password/mfa', **KW).status_code in (200, 302, 501)
    # l'identifiant de la 1re connexion n'est plus accepté pour une action
    assert post(app, '/auth/password/mfa', {'code': '1'}, client=stale).status_code in (302, 401, 501)


def test_an_enrollment_state_does_not_open_the_mfa_route_and_conversely(app, conn):
    client, body = start_pending(app, conn, 'noenroll', factor=False)
    assert body['step'] == 'enroll'
    assert client.get('/auth/password/mfa', **KW).status_code == 302
    assert client.get('/auth/password/enroll', **KW).status_code == 200
    c2, b2 = start_pending(app, conn, 'withmfa')
    assert b2['step'] == 'mfa' and c2.get('/auth/password/enroll', **KW).status_code == 302


def test_pending_routes_without_a_state_redirect_to_login_and_unknown_subroutes_are_not_public(app):
    client = app.test_client()
    for path in ('/auth/password/mfa', '/auth/password/enroll'):
        r = client.get(path, **KW)
        assert r.status_code == 302 and '/auth/password/login' in r.headers['Location']
    for path in ('/auth/password/mfa/totp', '/auth/password/enroll/totp', '/auth/password/enroll/webauthn', '/auth/password/other'):
        assert client.get(path, **KW).status_code in (302, 401, 404)


def test_pending_memory_is_bounded_and_consumption_is_atomic():
    store = PendingStore(max_entries=100)
    ids = [store.create(i, 'mfa') for i in range(250)]
    assert store.size() == 100 and store.get(ids[0]) is None and store.get(ids[-1]) is not None
    pid = store.create(1, 'mfa')
    wins = []
    threads = [threading.Thread(target=lambda: wins.append(store.consume(pid))) for _ in range(40)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sum(w is not None for w in wins) == 1 and store.get(pid) is None


def test_the_destroy_for_user_call_removes_every_state_of_an_account():
    store = PendingStore()
    mine = [store.create(7, 'mfa') for _ in range(5)]
    other = store.create(8, 'mfa')
    assert store.destroy_for_user(7) == 5 and all(store.get(i) is None for i in mine) and store.get(other) is not None


@pytest.mark.parametrize('pid', [None, '', 0, 1.5, [], {}, True, 'x' * 101, 'a', '../etc', 'ü' * 40, b'bytes'])
def test_hostile_pending_identifiers_are_ignored_not_fatal(pid):
    store = PendingStore()
    assert store.get(pid) is None and store.hit(pid) is None and store.consume(pid) is None
    store.destroy(pid)


# ===== 5. Cookies, fixation, CSRF ===============================================================================================

def test_a_successful_login_discards_everything_the_previous_session_held(app, conn):
    add(conn, 'alice')
    client = app.test_client()
    serializer = app.session_interface.get_signing_serializer(app)
    client.set_cookie('session', serializer.dumps({'planted': 'x', 'pending_id': 'zzz', 'csrf': 'a', 'oidc_state': 's', 'next': '//evil'}),
                      domain=HOST)
    assert login(app, 'alice', GOOD, client=client).status_code == 200
    payload = cookie_payload(app, client)
    assert set(payload) == {'user_id', 'token', 'login_at', '_permanent'} and payload['login_at'] > 1000


def test_a_login_as_another_account_replaces_the_session_without_residue(app, conn):
    a, b = add(conn, 'alice', token='ta'), add(conn, 'bob', token='tb')
    client = app.test_client()
    login(app, 'alice', GOOD, client=client)
    assert cookie_payload(app, client)['user_id'] == a['id']
    login(app, 'bob', GOOD, client=client)
    payload = cookie_payload(app, client)
    assert payload['user_id'] == b['id'] and payload['token'] == 'tb'


def test_a_pending_login_discards_a_previous_full_session(app, conn):
    add(conn, 'alice')
    client = app.test_client()
    login(app, 'alice', GOOD, client=client)
    add(conn, 'adm', admin=True, factor=True)
    login(app, 'adm', GOOD, client=client)
    assert set(cookie_payload(app, client)) == {'pending_id'}
    assert client.get('/api/me', **KW).status_code == 401


def test_a_failed_login_never_creates_or_changes_a_session(app, conn):
    add(conn, 'alice')
    client = app.test_client()
    for _ in range(3):
        login(app, 'alice', WRONG, client=client)
    assert raw_cookie(client) is None


def test_the_session_cookie_has_the_hardening_attributes(app, conn):
    add(conn, 'alice')
    response = login(app, 'alice', GOOD)
    cookie = response.headers['Set-Cookie']
    assert 'HttpOnly' in cookie and 'SameSite=Lax' in cookie and 'Secure' in cookie and 'Expires=' in cookie and 'Path=/' in cookie


def test_a_cookie_issued_before_a_password_or_factor_change_is_refused_CA14(app, conn):
    add(conn, 'alice')
    client = app.test_client()
    login(app, 'alice', GOOD, client=client)
    assert client.get('/api/me', **KW).status_code == 200
    conn.execute("UPDATE users SET session_token = 'rotated' WHERE username = 'alice'")
    conn.commit()
    assert client.get('/api/me', **KW).status_code == 401


def test_consuming_a_reset_token_kills_older_sessions_and_opens_none_CA14_CA45(app, conn):
    user = add(conn, 'alice')
    old = app.test_client()
    login(app, 'alice', GOOD, client=old)
    assert old.get('/api/me', **KW).status_code == 200
    created = tokens.create(conn, 'reset', actor=CLI, target_user_id=user['id'], email=None, ttl=timedelta(hours=1))
    client = app.test_client()
    response = post(app, '/auth/token/consume', {'token': created.token, 'password': 'un-autre-mot-de-passe-9'}, client=client)
    assert response.status_code == 200 and response.get_json()['next'].endswith('/auth/password/login'), response.get_data()
    assert 'user_id' not in cookie_payload(app, client)                       # CA-45 : aucune session complète
    assert old.get('/api/me', **KW).status_code == 401                        # CA-14 : l'ancien cookie meurt
    assert login(app, 'alice', GOOD).status_code == 401                       # l'ancien mot de passe aussi
    assert login(app, 'alice', 'un-autre-mot-de-passe-9').status_code == 200


@pytest.mark.parametrize('policy,step', [('admins', None), ('all', 'enroll')])
def test_consuming_an_invitation_opens_no_session_and_the_next_login_follows_the_policy_CA45(app, conn, policy, step):
    app.config['SETTINGS_STORE'].set_many(conn, {'auth.mfa_policy': policy}, actor=CLI)
    created = tokens.create(conn, 'invite', actor=CLI, email='new@example.org')
    client = app.test_client()
    response = post(app, '/auth/token/consume', {'token': created.token, 'username': 'nouveau', 'password': GOOD}, client=client)
    assert response.status_code == 200 and response.get_json()['next'].endswith('/auth/password/login'), response.get_data()
    assert not {'user_id', 'token', 'login_at'} & set(cookie_payload(app, client))
    assert client.get('/api/me', **KW).status_code == 401
    second = login(app, 'nouveau', GOOD, client=client)
    assert second.status_code == 200
    assert second.get_json().get('step') == step
    assert ('user_id' in cookie_payload(app, client)) == (step is None)


def test_a_forged_or_foreign_cookie_is_ignored(app, conn):
    add(conn, 'alice')
    from itsdangerous import URLSafeTimedSerializer
    from flask.sessions import session_json_serializer
    for secret in ('x' * 40, 'k' * 40):
        s = URLSafeTimedSerializer(secret, salt='cookie-session', serializer=session_json_serializer,
                                   signer_kwargs={'key_derivation': 'hmac', 'digest_method': __import__('hashlib').sha1})
        client = app.test_client()
        client.set_cookie('session', s.dumps({'user_id': 1, 'token': 'tok', 'login_at': int(time.time())}), domain=HOST)
        status = client.get('/api/me', **KW).status_code
        assert status == (200 if secret == 'k' * 40 else 401)           # signé avec la bonne clé : c'est une session ; sinon rien
    for junk in ('', 'abc', 'a.b.c', 'e30.' + 'A' * 5 + '.' + 'B' * 27, '\x00', 'x' * 5000):
        client = app.test_client()
        client.set_cookie('session', junk, domain=HOST)
        assert client.get('/api/me', **KW).status_code == 401 and client.get('/auth/password/mfa', **KW).status_code == 302


def test_a_full_session_cookie_older_than_its_lifetime_is_refused(app, conn):
    add(conn, 'alice')
    client = app.test_client()
    client.set_cookie('session', app.session_interface.get_signing_serializer(app).dumps(
        {'user_id': 1, 'token': 'tok', 'login_at': int(time.time()) - 13 * 3600}), domain=HOST)
    assert client.get('/api/me', **KW).status_code == 401


def test_login_csrf_every_missing_or_bad_token_or_origin_is_refused_and_opens_nothing(app, conn):
    """EF-S2 : POST same-origin + jeton de formulaire ; un site tiers ne peut pas connecter la victime à SON compte."""
    add(conn, 'attaquant')
    good = csrf_token(app)
    for kwargs in ({'token': False}, {'token': 'x'}, {'token': good[:-2] + 'zz'}, {'token': good + 'a'}, {'token': 'a' * 501},
                   {'headers': {'X-CSRF-Token': ''}}, {'origin': 'https://evil.example'}, {'origin': 'null'}):
        client = app.test_client()
        response = login(app, 'attaquant', GOOD, client=client, **kwargs)
        assert response.status_code in (400, 403), kwargs
        assert raw_cookie(client) is None, kwargs
    assert login(app, 'attaquant', GOOD).status_code == 200


def test_an_expired_form_token_is_refused(app, conn, monkeypatch):
    add(conn, 'alice')
    monkeypatch.setattr(csrf, 'MAX_AGE_S', 0)
    time.sleep(1.1)
    assert login(app, 'alice', GOOD).status_code == 400


def test_a_token_signed_with_another_secret_or_salt_is_refused(app, conn):
    add(conn, 'alice')
    from itsdangerous import URLSafeTimedSerializer
    for secret, salt in (('autre-cle' * 5, 'public-form-csrf'), ('k' * 40, 'autre-sel'), ('k' * 40, 'cookie-session')):
        forged = URLSafeTimedSerializer(secret, salt=salt).dumps('abc')
        assert login(app, 'alice', GOOD, token=forged).status_code == 400


def test_the_login_is_not_a_json_get_oracle_and_other_content_types_are_refused(app, conn):
    """Un formulaire HTML tiers ne peut envoyer que du x-www-form-urlencoded, multipart ou text/plain."""
    add(conn, 'alice')
    body = json.dumps({'username': 'alice', 'password': GOOD})
    for ctype in ('text/plain', 'application/x-www-form-urlencoded', 'multipart/form-data; boundary=x', ''):
        client = app.test_client()
        response = post(app, '/auth/password/login', None, client=client, raw=body, content_type=ctype)
        assert response.status_code == 400 and raw_cookie(client) is None, ctype
    form = app.test_client().post('/auth/password/login', data={'username': 'alice', 'password': GOOD}, **KW,
                                  headers={'X-CSRF-Token': csrf_token(app), 'Origin': ORIGIN},
                                  environ_overrides={'REMOTE_ADDR': new_ip()})
    assert form.status_code == 400


@pytest.mark.parametrize('method', ['put', 'patch', 'delete'])
def test_other_methods_on_login_open_nothing(app, conn, method):
    add(conn, 'alice')
    client = app.test_client()
    response = getattr(client, method)('/auth/password/login', json={'username': 'alice', 'password': GOOD}, **KW,
                                       headers={'X-CSRF-Token': csrf_token(app), 'Origin': ORIGIN})
    assert response.status_code in (302, 400, 403, 405) and raw_cookie(client) is None
    if response.status_code == 302:
        assert '/auth/password/login' in response.headers['Location']


# ===== 6. Redirection ouverte ===================================================================================================

NEXTS = ['//evil.example', '///evil.example', '/\\evil.example', '\\\\evil.example', '\\/evil.example', '/\t/evil.example', '/\n/evil.example',
         ' //evil.example', 'https://evil.example', 'http://evil.example', 'HTTPS://EVIL.EXAMPLE', 'javascript:alert(1)', 'data:text/html,x',
         'evil.example', 'http:evil.example', 'https:evil.example', '/%2f%2fevil.example', '/%5cevil.example', '/\r\nSet-Cookie: x=1',
         '//evil.example/%2e%2e', '/..//evil.example', '/.//evil.example', '/@evil.example', 'mailto:a@b.c', 'file:///etc/passwd', '／／evil.example',
         '/\u202e//evil.example', 'x' * 5000, '/' + 'a' * 5000, '%2f%2fevil.example', '/%00//evil.example', '\x00//evil.example']


def assert_same_site(target):
    parts = urlsplit(target)
    assert target.startswith('/') and not target.startswith('//') and '\\' not in target and not parts.netloc and not parts.scheme, target
    assert not any(ord(c) < 32 or c == '\x7f' for c in target), repr(target)
    assert len(target) <= 2048


@pytest.mark.parametrize('target', NEXTS)
def test_the_next_field_can_never_leave_the_site(app, conn, target):
    add(conn, 'alice')
    response = login(app, 'alice', GOOD, extra={'next': target})
    assert response.status_code == 200
    assert_same_site(response.get_json()['next'])


@pytest.mark.parametrize('target', NEXTS[:12])
def test_the_next_field_is_sanitised_before_being_stored_in_the_pending_state(app, conn, target):
    add(conn, 'adm', admin=True, factor=True)
    client = app.test_client()
    assert login(app, 'adm', GOOD, client=client, extra={'next': target}).get_json()['step'] == 'mfa'
    pid = cookie_payload(app, client)['pending_id']
    stored = app.extensions['pending'].get(pid).next_url
    assert_same_site(stored)


@pytest.mark.parametrize('target', ['//evil.example', '"><script>alert(1)</script>', "'+alert(1)+'", '/x"onmouseover="alert(1)', '{{7*7}}',
                                    '</script><script>alert(1)</script>', '/ok?a=1&b=<b>'])
def test_the_next_query_parameter_is_never_reflected_unescaped_in_the_login_page(app, target):
    response = app.test_client().get('/auth/password/login', query_string={'next': target}, **KW)
    html = response.get_data(as_text=True)
    assert response.status_code == 200 and '<script>alert(1)' not in html and 'onmouseover="alert' not in html and '{{7*7}}' not in html
    for found in re.findall(r'data-next="([^"]*)"|name="next"[^>]*value="([^"]*)"', html):
        assert_same_site(next(v for v in found if v) or '/')


@pytest.mark.parametrize('value', [1, 1.5, True, None, ['/x'], {'a': '/x'}, '', 0])
def test_a_non_string_next_is_ignored(app, conn, value):
    add(conn, 'alice')
    response = login(app, 'alice', GOOD, extra={'next': value})
    assert response.status_code == 200 and response.get_json()['next'] == '/'


def test_a_legitimate_relative_next_is_kept(app, conn):
    add(conn, 'alice')
    assert login(app, 'alice', GOOD, extra={'next': '/api/activities?page=2'}).get_json()['next'] == '/api/activities?page=2'


# ===== 7. Mot de passe oublié ===================================================================================================

@pytest.fixture
def smtp():
    server = FakeSMTP()
    yield server
    server.stop()


@pytest.fixture
def mail_app(tmp_path, smtp):
    application = make_app(tmp_path / 'mail', OUTBOUND_ALLOWED_HOSTS=f'smtp=127.0.0.1:{smtp.port}')
    c = database.connect(application.config['DB_PATH'])
    application.config['SETTINGS_STORE'].set_many(c, {'smtp.host': '127.0.0.1', 'smtp.port': smtp.port, 'smtp.security': 'none',
                                                      'smtp.sender': 'noreply@example.org', 'smtp.timeout_s': 2}, actor=CLI)
    c.close()
    return application


def seed_forgot(mail_app):
    c = database.connect(mail_app.config['DB_PATH'])
    add(c, 'alice', email='alice@example.org')
    add(c, 'pending', email=None)
    c.execute("UPDATE users SET pending_email = 'pending@example.org' WHERE username = 'pending'")
    add(c, 'sso', method='oidc', email='sso@example.org')
    add(c, 'off', active=False, email='off@example.org')
    add(c, 'none', method='none', email='none@example.org')
    c.commit()
    c.close()


IDENTIFIERS = ['alice', 'ALICE', 'alice@example.org', ' Alice@Example.ORG ', 'pending', 'pending@example.org', 'sso', 'sso@example.org', 'off',
               'off@example.org', 'none', 'inconnu', 'inconnu@example.org', '@', 'a@', '@b', '%', '_', "'", '"; DROP TABLE users;--',
               'alice\r\nBcc: evil@example.org', 'x' * 1024, 'ａｌｉｃｅ', 'ALİCE', '\u202ealice']


def test_forgot_gives_the_same_answer_for_every_kind_of_input_CA15(mail_app, smtp):
    seed_forgot(mail_app)
    sigs = {signature(forgot(mail_app, ident)) for ident in IDENTIFIERS}
    assert len(sigs) == 1, sigs
    status, body, _, cookie = next(iter(sigs))
    assert status == 200 and not cookie and json.loads(body)['success'] is True


def test_forgot_sends_only_to_the_stored_validated_address_never_to_the_typed_one(mail_app, smtp):
    seed_forgot(mail_app)
    for ident in ('alice', ' Alice@Example.ORG ', 'ALICE', 'pending', 'sso@example.org', 'off', 'none'):
        forgot(mail_app, ident)
    mail_app.extensions['mailer'].queue.drain()
    recipients = [c for c in smtp.commands if c.lower().startswith('rcpt to')]
    assert len(recipients) == 3 and all('alice@example.org' in r.lower() for r in recipients)       # username, ALICE, adresse
    assert not any(x in ' '.join(recipients).lower() for x in ('pending@', 'sso@', 'off@', 'none@', 'evil'))


def test_forgot_creates_exactly_one_reset_token_per_eligible_request_and_none_otherwise(mail_app, smtp):
    seed_forgot(mail_app)
    c = database.connect(mail_app.config['DB_PATH'])
    for ident in ('pending', 'sso', 'off', 'none', 'inconnu', 'alice\r\nBcc: evil@example.org'):
        forgot(mail_app, ident)
    mail_app.extensions['mailer'].queue.drain()
    assert c.execute("SELECT COUNT(*) FROM auth_tokens").fetchone()[0] == 0 and smtp.messages == []
    forgot(mail_app, 'alice')
    mail_app.extensions['mailer'].queue.drain()
    rows = c.execute("SELECT kind, target_user_id, email FROM auth_tokens").fetchall()
    assert len(rows) == 1 and rows[0]['kind'] == 'reset' and len(smtp.messages) == 1
    c.close()


def test_forgot_link_uses_public_url_never_the_request_headers(tmp_path, smtp):
    app = make_app(tmp_path, OUTBOUND_ALLOWED_HOSTS=f'smtp=127.0.0.1:{smtp.port}', TRUST_PROXY=0)
    c = database.connect(app.config['DB_PATH'])
    app.config['SETTINGS_STORE'].set_many(c, {'smtp.host': '127.0.0.1', 'smtp.port': smtp.port, 'smtp.security': 'none',
                                              'smtp.sender': 'noreply@example.org'}, actor=CLI)
    add(c, 'alice', email='alice@example.org')
    c.close()
    forgot(app, 'alice', headers={'X-Forwarded-Host': 'evil.example', 'X-Forwarded-Proto': 'http', 'X-Original-Host': 'evil.example',
                                  'Forwarded': 'host=evil.example'})
    app.extensions['mailer'].queue.drain()
    body = smtp.messages[0]
    assert f'{ORIGIN}/auth/token#t=' in body and 'evil.example' not in body


def test_forgot_never_runs_its_work_in_the_request_thread_even_for_an_unknown_account(mail_app, monkeypatch):
    seed_forgot(mail_app)
    mailer = mail_app.extensions['mailer']
    seen = []
    original = mailer.send
    monkeypatch.setattr(mailer, 'send', lambda *a, **k: (seen.append(threading.get_ident()), original(*a, **k))[1])
    original_create = tokens.create
    monkeypatch.setattr(tokens, 'create', lambda *a, **k: (seen.append(('token', threading.get_ident())), original_create(*a, **k))[1])
    for ident in ('alice', 'inconnu', 'sso'):
        forgot(mail_app, ident)
    assert seen == []                                           # rien dans la requête : tout est en file
    mailer.queue.drain()
    assert seen                                                  # le travail existe bien, mais seulement côté file


def test_forgot_response_time_does_not_depend_on_the_account_nor_on_a_dead_smtp(tmp_path):
    dead = FakeSMTP(mode='hang')
    try:
        app = make_app(tmp_path, OUTBOUND_ALLOWED_HOSTS=f'smtp=127.0.0.1:{dead.port}')
        c = database.connect(app.config['DB_PATH'])
        app.config['SETTINGS_STORE'].set_many(c, {'smtp.host': '127.0.0.1', 'smtp.port': dead.port, 'smtp.security': 'none',
                                                  'smtp.sender': 'noreply@example.org', 'smtp.timeout_s': 5}, actor=CLI)
        add(c, 'alice', email='alice@example.org')
        c.close()
        durations = {'eligible': [], 'unknown': []}
        for i in range(8):
            for kind, ident in (('eligible', 'alice@example.org'), ('unknown', f'inconnu{i}@example.org')):
                ip = new_ip()
                t0 = time.perf_counter()
                forgot(app, ident, ip=ip)
                durations[kind].append(time.perf_counter() - t0)
        assert max(max(v) for v in durations.values()) < 1.0               # jamais l'attente d'un SMTP muet (timeout 5 s)
        a, b = (statistics.median(durations[k]) for k in ('eligible', 'unknown'))
        assert abs(a - b) < max(0.05, 3 * min(a, b)), durations
    finally:
        dead.stop()


def test_forgot_limits_by_identifier_for_existing_and_unknown_alike_and_by_ip(mail_app):
    seed_forgot(mail_app)
    runs = {}
    for ident in ('alice', 'fantome'):
        runs[ident] = [forgot(mail_app, ident).status_code for _ in range(6)]
    assert runs['alice'] == runs['fantome'] and runs['alice'][0] == 200 and runs['alice'][-1] == 429
    ip = '198.51.100.99'
    codes = [forgot(mail_app, f'x{i}@example.org', ip=ip).status_code for i in range(30)]
    assert codes[0] == 200 and 429 in codes
    r = forgot(mail_app, 'alice', ip=new_ip())
    assert r.status_code in (200, 429)


def test_forgot_limit_refusals_enqueue_nothing(mail_app, monkeypatch):
    seed_forgot(mail_app)
    submitted = []
    mailer = mail_app.extensions['mailer']
    monkeypatch.setattr(mailer, 'submit', lambda job: (submitted.append(job), True)[1])
    codes = [forgot(mail_app, 'alice').status_code for _ in range(8)]
    assert codes.count(200) == len(submitted) and 429 in codes


def test_a_full_mail_queue_changes_nothing_in_the_forgot_answer(mail_app, monkeypatch):
    seed_forgot(mail_app)
    mailer = mail_app.extensions['mailer']
    monkeypatch.setattr(mailer, 'submit', lambda job: False)             # file pleine : travail abandonné
    ok = signature(forgot(mail_app, 'alice'))
    other = signature(forgot(mail_app, 'inconnu'))
    assert ok == other and ok[0] == 200


def test_a_thousand_forgot_requests_from_many_ips_never_error_and_the_queue_stays_bounded(mail_app):
    seed_forgot(mail_app)
    queue = mail_app.extensions['mailer'].queue
    codes = {forgot(mail_app, f'inconnu{i}@example.org', ip=new_ip()).status_code for i in range(1100)}
    assert codes == {200}
    assert queue._queue.qsize() <= 1000


def test_forgot_and_login_pages_exist_only_when_the_feature_is_on(app, mail_app):
    assert app.test_client().get('/auth/password/forgot', **KW).status_code == 404              # sans SMTP
    assert post(app, '/auth/password/forgot', {'identifier': 'a'}).status_code == 404
    assert mail_app.test_client().get('/auth/password/forgot', **KW).status_code == 200
    c = database.connect(mail_app.config['DB_PATH'])
    mail_app.config['SETTINGS_STORE'].set_many(c, {'auth.forgot_enabled': False}, actor=CLI)
    c.close()
    assert mail_app.test_client().get('/auth/password/forgot', **KW).status_code == 404
    assert post(mail_app, '/auth/password/forgot', {'identifier': 'a'}).status_code == 404


# ===== 8. Corps hostiles ========================================================================================================

ENDPOINTS = ['/auth/password/login', '/auth/password/forgot']


@pytest.mark.parametrize('path', ENDPOINTS + ['/auth/token/inspect', '/auth/token/consume'])
def test_a_deeply_nested_json_body_is_a_400_never_a_500(mail_app, path):
    for depth in (500, 2000, 8000):
        body = '[' * depth + ']' * depth
        r = post(mail_app, path, None, raw=body)
        assert r.status_code == 400, (path, depth, r.status_code)
        r = post(mail_app, path, None, raw='{"a":' * depth + '1' + '}' * depth)
        assert r.status_code == 400, (path, depth, r.status_code)


@pytest.mark.parametrize('path', ENDPOINTS)
def test_a_body_without_content_length_is_bounded_too(mail_app, path):
    """Corps en transfert fragmenté (pas de Content-Length) : la lecture doit s'arrêter, pas parser des mégaoctets avant authentification."""
    big = json.dumps({'username': 'a', 'identifier': 'a', 'password': 'b' * (3 * 1024 * 1024)}).encode()
    client = mail_app.test_client()
    t0 = time.perf_counter()
    chunked = {'REMOTE_ADDR': new_ip(), 'HTTP_TRANSFER_ENCODING': 'chunked', 'wsgi.input_terminated': True, 'CONTENT_LENGTH': ''}
    r = client.post(path, input_stream=io.BytesIO(big), content_type='application/json', **KW,
                    headers={'X-CSRF-Token': csrf_token(mail_app), 'Origin': ORIGIN}, environ_overrides=chunked)
    assert r.status_code in (400, 413), r.status_code
    assert time.perf_counter() - t0 < 2.0


SURROGATES = ['\ud800', 'a\ud800b', '\udfff' * 5]


@pytest.mark.parametrize('path,field', [('/auth/password/login', 'username'), ('/auth/password/forgot', 'identifier')])
@pytest.mark.parametrize('value', SURROGATES + ['alice\x00', '\x00' * 10, '\u202e', 'a' * 1024, 'a' * 1025, 'é' * 1024, '\U0001F600' * 300,
                                   '%s', '%(x)s', '{0}', '${jndi:ldap://x}', '\r\n', '\t', 'ǅ', 'İ' * 100, 'ﬁ' * 100, 'İı'])
def test_hostile_text_values_never_cause_a_server_error(mail_app, path, field, value):
    seed_forgot(mail_app)
    payload = {'username': value, 'password': GOOD} if field == 'username' else {'identifier': value}
    r = post(mail_app, path, payload, raw=json.dumps(payload, ensure_ascii=True))
    assert r.status_code in (200, 400, 401, 429), (r.status_code, r.get_data()[:200])


@pytest.mark.parametrize('password', ['\ud800', 'a\ud800', '\x00' * 5, 'é' * 129, 'x' * 1025, '\u202e' * 200, 'ﬁ' * 100])
def test_hostile_password_values_are_plain_failures(app, conn, password):
    add(conn, 'alice')
    r = login(app, 'alice', password)
    assert r.status_code == 401 and r.get_json()['error'] == 'Identifiant ou mot de passe incorrect.'


@pytest.mark.parametrize('payload', [[], [1, 2], 'texte', 5, None, True, {}, {'username': None, 'password': None}, {'username': 1, 'password': 2},
                                     {'username': ['a'], 'password': ['b']}, {'username': {'a': 1}, 'password': {'b': 2}},
                                     {'username': True, 'password': True}, {'username': '', 'password': ''}, {'username': 'alice'},
                                     {'password': GOOD}, {'USERNAME': 'alice', 'PASSWORD': GOOD}, {'username': 'alice', 'password': GOOD, 'x': 'y' * 100}])
def test_wrongly_shaped_payloads_are_refused_without_a_session(app, conn, payload):
    add(conn, 'alice')
    client = app.test_client()
    r = post(app, '/auth/password/login', payload, client=client)
    if isinstance(payload, dict) and payload.get('username') == 'alice' and payload.get('password') == GOOD:
        assert r.status_code == 200                                    # champ en trop : ignoré
    else:
        assert r.status_code in (400, 401) and raw_cookie(client) is None


def test_duplicate_json_keys_use_the_last_value_consistently(app, conn):
    add(conn, 'alice')
    raw = '{"username": "bob", "username": "alice", "password": "%s"}' % GOOD
    assert post(app, '/auth/password/login', None, raw=raw).status_code == 200


def test_oversized_declared_body_is_refused_before_any_work(app, conn, monkeypatch):
    add(conn, 'alice')
    calls = []
    original = passwords._gate.slot
    monkeypatch.setattr(passwords._gate, 'slot', lambda: (calls.append(1), original())[1])
    body = json.dumps({'username': 'alice', 'password': GOOD, 'pad': 'x' * 9000})
    assert post(app, '/auth/password/login', None, raw=body).status_code == 400 and calls == []


# ===== 9. Audit et journaux =====================================================================================================

def test_no_log_line_and_no_audit_row_ever_contains_a_password(app, conn, caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    add(conn, 'alice')
    secret_pw = 'S3cr3t-mot-de-passe-à-ne-jamais-voir'
    for _ in range(8):
        login(app, 'alice', secret_pw)
    login(app, 'alice', GOOD)
    login(app, 'inconnu', secret_pw)
    login(app, secret_pw, secret_pw)                              # mot de passe saisi comme identifiant : fuite possible dans l'audit
    dump = ' '.join(r.getMessage() for r in caplog.records) + json.dumps([dict(r) for r in conn.execute("SELECT * FROM audit_log")])
    assert GOOD not in dump and WRONG not in dump
    leaked = [r for r in conn.execute("SELECT * FROM audit_log") if secret_pw in json.dumps(dict(r))]
    assert len(leaked) <= 1                                       # au plus le « mauvais champ » tapé comme identifiant, jamais le mot de passe seul


def test_the_throttle_audit_has_one_line_per_identifier_and_ip(app, conn):
    """EF-9.2 : une ligne par couple (identifiant normalisé, IP) quand le seuil est atteint, avec le nombre d'échecs de la fenêtre."""
    add(conn, 'alice')
    for _ in range(12):
        login(app, 'alice', WRONG, ip='198.51.100.50')
    rows = [dict(r) for r in conn.execute("SELECT * FROM audit_log WHERE action = 'auth.rate_limited'")]
    assert len(rows) == 1 and rows[0]['actor_type'] == 'system' and rows[0]['ip'] == '198.51.100.50'
    details = json.loads(rows[0]['details'])
    assert details['identifier'] == 'alice' and WRONG not in rows[0]['details']


def test_the_throttle_audit_count_is_present(app, conn):
    add(conn, 'alice')
    for _ in range(12):
        login(app, 'alice', WRONG, ip='198.51.100.51')
    details = json.loads(conn.execute("SELECT details FROM audit_log WHERE action = 'auth.rate_limited'").fetchone()['details'])
    assert 'count' in details or 'failures' in details or 'attempts' in details


def test_two_hundred_invented_identifiers_from_one_ip_write_a_bounded_number_of_audit_lines(app, conn):
    for i in range(200):
        login(app, f'invente{i}', WRONG, ip='198.51.100.60')
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'auth.rate_limited'").fetchone()[0] <= 3


def test_audited_identifier_is_truncated_and_cannot_forge_a_line(app, conn):
    hostile = 'x\n{"actor_type":"user"}' + 'y' * 500
    for _ in range(8):
        login(app, hostile, WRONG, ip='198.51.100.61')
    for r in conn.execute("SELECT * FROM audit_log WHERE action = 'auth.rate_limited'"):
        assert r['actor_type'] == 'system' and r['actor_id'] is None and len(r['details']) < 400


def test_successful_and_failed_logins_never_audit_the_account_as_actor_without_a_user_row(app, conn):
    add(conn, 'alice')
    login(app, 'alice', GOOD)
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE actor_type = 'user' AND actor_id IS NULL").fetchone()[0] == 0
