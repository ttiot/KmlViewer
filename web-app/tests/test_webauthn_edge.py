"""
T12 (clés de sécurité WebAuthn) en profondeur (rôle test), écrit depuis la spec (EF-2.1, EF-2.2, EF-2.4, EF-2.5, EF-2.8, EF-S9, B5, M3, M12),
avec l'authentificateur VIRTUEL de tests/webauthn_authenticator.py (clé EC P-256, attestation `none`) et des réponses FALSIFIÉES à la main.

 1. configuration : RP ID, surcharge, origine attendue, localhost, ports
 2. connexion par clé : défi (usage unique, 2 min, lié au cookie), origine, RP, type, drapeaux, signature, compteur, autre compte
 3. enrôlement (étape enroll) et octroi d'admin (B5) ; profil : ajout, retrait, plafond de 10, noms
 4. EF-2.2 avec des clés ; réinitialisation ; aucune fuite de la clé publique
"""

import json
import re
import struct
import hashlib
import time

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from app.audit import Actor
from app.auth import factors, tokens, webauthn_factor
from tests.pw_harness import (GOOD, HOST, KW, ORIGIN, OTHER, add, call, concurrent, connect, cookie_payload, form_post, login, make_app,
                              new_ip, new_secret, raw_cookie, refresh, session_client)
from tests.webauthn_authenticator import VirtualAuthenticator, b64

CLI = Actor.cli()
RP = HOST


@pytest.fixture
def app(tmp_path):
    return make_app(tmp_path)


@pytest.fixture
def loose_app(tmp_path):
    """Seuils du limiteur au maximum permis : pour les tests qui enchaînent beaucoup de connexions et de vérifications."""
    application = make_app(tmp_path / 'loose')
    c = connect(application)
    application.config['SETTINGS_STORE'].set_many(c, {'ratelimit.password_per_identifier': 20, 'ratelimit.password_per_ip': 100,
                                                      'ratelimit.mfa_per_account': 10}, actor=CLI)
    c.close()
    return application


@pytest.fixture
def conn(app):
    c = connect(app)
    yield c
    c.close()


def add_key(conn, row, authenticator=None, *, counter=0, name='cle'):
    """Facteur actif : une clé (public_key COSE du virtuel) enregistrée directement en base."""
    authenticator = authenticator or VirtualAuthenticator(RP, ORIGIN, counter)
    conn.execute("INSERT INTO webauthn_credentials (user_id, credential_id, public_key, sign_count, transports, name) VALUES (?, ?, ?, ?, 'usb', ?)",
                 (row['id'], authenticator.credential_id, authenticator._cose(), counter, name))
    conn.commit()
    return authenticator


def key_user(app, conn, name='kay', *, admin=True, counter=0):
    row = add(app, conn, name, admin=admin)
    authenticator = add_key(conn, row, counter=counter)
    return row, authenticator


def pending(app, name):
    client = app.test_client()
    r = login(app, name, client=client, ip=new_ip())
    assert r.status_code == 200 and r.get_json()['step'] == 'mfa', r.get_data()
    return client


def options(app, client, **kw):
    return form_post(app, '/auth/password/mfa/webauthn/options', {}, client=client, **kw)


def verify(app, client, credential, **kw):
    return form_post(app, '/auth/password/mfa/webauthn/verify', {'credential': credential}, client=client, **kw)


def logged_in(app, client):
    return 'user_id' in cookie_payload(app, client)


def assertion(auth, options_json, *, counter=None, flags=0x05, rp_id=None, origin=None, ctype='webauthn.get', challenge=None, key=None,
              credential_id=None, cross_origin=False, extra_client=None):
    """Assertion forgée à la main : chaque paramètre peut être faussé indépendamment."""
    opts = json.loads(options_json)
    auth.counter = auth.counter + 1 if counter is None else counter
    client = {'type': ctype, 'challenge': opts['challenge'] if challenge is None else challenge, 'origin': auth.origin if origin is None else origin,
              'crossOrigin': cross_origin}
    client.update(extra_client or {})
    client_json = json.dumps(client).encode()
    auth_data = hashlib.sha256((auth.rp_id if rp_id is None else rp_id).encode()).digest() + bytes([flags]) + struct.pack('>I', auth.counter)
    signature = (key or auth.key).sign(auth_data + hashlib.sha256(client_json).digest(), ec.ECDSA(hashes.SHA256()))
    cid = b64(credential_id or auth.credential_id)
    return {'id': cid, 'rawId': cid, 'type': 'public-key', 'response': {'clientDataJSON': b64(client_json), 'authenticatorData': b64(auth_data),
                                                                          'signature': b64(signature), 'userHandle': None}, 'clientExtensionResults': {}}


# ===== 1. Configuration =========================================================================================================

@pytest.mark.parametrize('public,override,expected', [
    ('https://traces.example.com', '', 'traces.example.com'), ('https://TRACES.Example.COM', '', 'traces.example.com'),
    ('https://traces.example.com', 'example.com', 'example.com'), ('https://traces.example.com', 'EXAMPLE.COM', 'example.com'),
    ('https://traces.example.com', 'traces.example.com', 'traces.example.com'),
    ('https://traces.example.com', 'evil.com', None), ('https://traces.example.com', 'ample.com', None), ('https://traces.example.com', 'xexample.com', None),
    ('https://traces.example.com', 'example.com.evil.com', None), ('https://traces.example.com', 'sub.traces.example.com', None),
    ('https://traces.example.com', '.example.com', None), ('https://traces.example.com', 'example.com.', None),
    ('https://traces.example.com', 'traces.example.com:443', None), ('', '', None), ('not a url', '', None),
])
def test_the_rp_id_is_the_host_or_a_domain_suffix_of_it(public, override, expected):
    assert webauthn_factor.rp_id({'PUBLIC_URL': public, 'WEBAUTHN_RP_ID': override}) == expected


@pytest.mark.parametrize('override', ['com', 'co.uk'])
def test_a_public_suffix_is_not_a_valid_rp_id_override(override):
    host = 'traces.example.co.uk' if override == 'co.uk' else 'traces.example.com'
    assert webauthn_factor.rp_id({'PUBLIC_URL': f'https://{host}', 'WEBAUTHN_RP_ID': override}) is None


@pytest.mark.parametrize('host', ['192.0.2.1', '[2001:db8::1]'])
def test_an_ip_address_cannot_be_a_rp_id(host):
    assert webauthn_factor.available({'PUBLIC_URL': f'https://{host}'}) is False


@pytest.mark.parametrize('cfg,expected', [
    ({'PUBLIC_URL': 'https://traces.example.com'}, 'https://traces.example.com'), ({'PUBLIC_URL': 'https://traces.example.com:443'}, 'https://traces.example.com'),
    ({'PUBLIC_URL': 'https://traces.example.com:8443'}, 'https://traces.example.com:8443'), ({'PUBLIC_URL': 'http://traces.example.com'}, None),
    ({'PUBLIC_URL': 'http://localhost:5000'}, None), ({'PUBLIC_URL': 'http://localhost:5000', 'TESTING': True}, 'http://localhost:5000'),
    ({'PUBLIC_URL': 'http://localhost:5000', 'DEBUG': True}, 'http://localhost:5000'), ({'PUBLIC_URL': 'http://localhost', 'TESTING': True}, 'http://localhost'),
    ({'PUBLIC_URL': 'http://127.0.0.1:5000', 'TESTING': True}, None), ({'PUBLIC_URL': 'http://evil.example', 'TESTING': True}, None),
    ({'PUBLIC_URL': 'ftp://traces.example.com'}, None), ({'PUBLIC_URL': ''}, None),
])
def test_the_expected_origin_is_the_public_url_and_cleartext_only_for_localhost_in_test_or_debug(cfg, expected):
    assert webauthn_factor.expected_origin(cfg) == expected


# ===== 2. Connexion par clé =====================================================================================================

def test_a_valid_assertion_opens_a_full_session_updates_the_counter_and_the_last_use(app, conn):
    row, auth = key_user(app, conn)
    client = pending(app, 'kay')
    opts = options(app, client)
    assert opts.status_code == 200
    body = opts.get_json()
    assert body['rpId'] == RP and len(body['challenge']) >= 40 and [c['id'] for c in body['allowCredentials']] == [b64(auth.credential_id)]
    r = verify(app, client, assertion(auth, opts.get_data(as_text=True)))
    assert r.status_code == 200 and r.get_json()['success'] and logged_in(app, client)
    key = conn.execute("SELECT * FROM webauthn_credentials").fetchone()
    assert key['sign_count'] == 1 and key['last_used_at']
    assert call(client, 'get', '/api/me').status_code == 200


def test_the_challenge_is_single_use_and_a_replayed_assertion_is_refused(app, conn):
    row, auth = key_user(app, conn)
    client = pending(app, 'kay')
    opts = options(app, client).get_data(as_text=True)
    credential = assertion(auth, opts)
    assert verify(app, client, credential).status_code == 200
    again = pending(app, 'kay')
    options(app, again)
    r = verify(app, again, credential)                                        # même assertion, nouveau défi côté serveur
    assert r.status_code == 401 and not logged_in(app, again)


def test_an_assertion_without_asking_for_options_first_is_refused(app, conn):
    row, auth = key_user(app, conn)
    client = pending(app, 'kay')
    fake = assertion(auth, json.dumps({'challenge': b64(b'x' * 32)}))
    assert verify(app, client, fake).status_code == 401 and not logged_in(app, client)


def test_the_challenge_expires_after_two_minutes(app, conn):
    row, auth = key_user(app, conn)
    client = pending(app, 'kay')
    clock = [time.monotonic()]
    app.extensions['pending']._clock = lambda: clock[0]
    opts = options(app, client).get_data(as_text=True)
    clock[0] += 121
    assert verify(app, client, assertion(auth, opts)).status_code == 401 and not logged_in(app, client)


def test_a_new_options_request_replaces_the_previous_challenge(app, conn):
    row, auth = key_user(app, conn)
    client = pending(app, 'kay')
    first = options(app, client).get_data(as_text=True)
    second = options(app, client).get_data(as_text=True)
    assert json.loads(first)['challenge'] != json.loads(second)['challenge']
    assert verify(app, client, assertion(auth, first)).status_code == 401
    client2 = pending(app, 'kay')
    options(app, client2)
    assert not logged_in(app, client2)


def test_the_challenge_is_bound_to_the_login_cookie_not_to_the_account(app, conn):
    """Défi demandé sous un état de connexion A : inutilisable avec l'état B du même compte (M3, EF-S9)."""
    row, auth = key_user(app, conn)
    a, b = pending(app, 'kay'), pending(app, 'kay')
    opts_a = options(app, a).get_data(as_text=True)
    r = verify(app, b, assertion(auth, opts_a))
    assert r.status_code == 401 and not logged_in(app, b)
    assert verify(app, a, assertion(auth, opts_a)).status_code == 200 and logged_in(app, a)       # la tentative de B n'a pas consommé le défi de A


FORGERIES = {
    'origine étrangère': dict(origin='https://evil.example'), 'origine http': dict(origin=f'http://{HOST}'), 'origine avec port': dict(origin=f'{ORIGIN}:8443'),
    'origine sous-domaine': dict(origin=f'https://evil.{HOST}'), 'origine vide': dict(origin=''), 'RP étranger': dict(rp_id='evil.example'),
    'RP parent': dict(rp_id='example.com'), 'type create': dict(ctype='webauthn.create'), 'type inconnu': dict(ctype='webauthn.x'),
    'défi faux': dict(challenge=b64(b'a' * 32)), 'défi vide': dict(challenge=''), 'présence absente': dict(flags=0x04), 'aucun drapeau': dict(flags=0x00),
}


@pytest.mark.parametrize('name', list(FORGERIES))
def test_every_forged_assertion_is_a_plain_failure_and_opens_nothing(app, conn, name):
    row, auth = key_user(app, conn)
    client = pending(app, 'kay')
    opts = options(app, client).get_data(as_text=True)
    r = verify(app, client, assertion(auth, opts, **FORGERIES[name]))
    assert r.status_code == 401 and not logged_in(app, client), name
    assert 'webauthn' not in r.get_data(as_text=True).lower() and 'Traceback' not in r.get_data(as_text=True)
    assert conn.execute("SELECT sign_count FROM webauthn_credentials").fetchone()[0] == 0


def test_a_signature_made_with_another_key_or_tampered_is_refused(app, conn):
    row, auth = key_user(app, conn)
    client = pending(app, 'kay')
    opts = options(app, client).get_data(as_text=True)
    other_key = ec.generate_private_key(ec.SECP256R1())
    assert verify(app, client, assertion(auth, opts, key=other_key)).status_code == 401
    client = pending(app, 'kay')
    opts = options(app, client).get_data(as_text=True)
    credential = assertion(auth, opts)
    signature = bytearray(__import__('base64').urlsafe_b64decode(credential['response']['signature'] + '=='))
    signature[10] ^= 0x01
    credential['response']['signature'] = b64(bytes(signature))
    assert verify(app, client, credential).status_code == 401 and not logged_in(app, client)


@pytest.mark.parametrize('mutate', [
    lambda c: c['response'].update(signature=''), lambda c: c['response'].update(signature='AAAA'), lambda c: c['response'].update(authenticatorData=''),
    lambda c: c['response'].update(authenticatorData='AAAA'), lambda c: c['response'].update(clientDataJSON=''), lambda c: c['response'].update(clientDataJSON='e30'),
    lambda c: c['response'].pop('signature'), lambda c: c.pop('response'), lambda c: c.update(rawId='%%%'), lambda c: c.update(rawId=''),
    lambda c: c.update(rawId=None), lambda c: c.update(rawId=5), lambda c: c.update(type='password'), lambda c: c.update(response='texte'),
    lambda c: c.update(response=['x']), lambda c: c['response'].update(signature=5), lambda c: c['response'].update(authenticatorData=None),
])
def test_malformed_credentials_are_plain_failures(app, conn, mutate):
    row, auth = key_user(app, conn)
    client = pending(app, 'kay')
    opts = options(app, client).get_data(as_text=True)
    credential = assertion(auth, opts)
    mutate(credential)
    r = verify(app, client, credential)
    assert r.status_code in (400, 401) and not logged_in(app, client), r.get_data()[:100]


@pytest.mark.parametrize('credential', [None, 5, True, [], {}, 'x', '{', '{"rawId": 1}', 'a' * 40_000, {'a': {'b': [1] * 5000}}, [[[[]]]] * 100])
def test_hostile_credential_values_never_cause_a_server_error(app, conn, credential):
    row, auth = key_user(app, conn)
    client = pending(app, 'kay')
    options(app, client)
    r = form_post(app, '/auth/password/mfa/webauthn/verify', {'credential': credential}, client=client, raw=json.dumps({'credential': credential}))
    assert r.status_code in (400, 401, 413), (r.status_code, r.get_data()[:100])
    assert not logged_in(app, client)


def test_a_key_of_another_account_does_not_open_this_one(app, conn):
    """Même navigateur, même clé physique : la clé d'un compte ne sert qu'à ce compte (credential_id lié à user_id)."""
    row, auth = key_user(app, conn, 'kay')
    other, other_auth = key_user(app, conn, 'lou')
    client = pending(app, 'kay')
    opts = options(app, client).get_data(as_text=True)
    r = verify(app, client, assertion(other_auth, opts))
    assert r.status_code == 401 and not logged_in(app, client)
    assert conn.execute("SELECT sign_count FROM webauthn_credentials").fetchall()[0][0] == 0


def test_the_counter_must_move_forward_and_a_regression_is_audited_then_a_good_assertion_still_works(loose_app):
    app = loose_app
    conn = connect(app)
    row, auth = key_user(app, conn, counter=5)
    for counter, ok in ((6, True), (6, False), (5, False), (1, False), (7, True), (7, False), (2 ** 32 - 1, True), (3, False)):
        client = pending(app, 'kay')
        opts = options(app, client).get_data(as_text=True)
        r = verify(app, client, assertion(auth, opts, counter=counter))
        assert (r.status_code == 200) == ok, (counter, r.status_code)
        assert logged_in(app, client) == ok
    rows = conn.execute("SELECT * FROM audit_log WHERE action = 'webauthn.counter_regression'").fetchall()
    assert len(rows) == 5 and all(r['actor_type'] == 'user' and r['target_id'] == str(row['id']) for r in rows)
    assert json.loads(rows[0]['details']) == {'key': 1}
    assert conn.execute("SELECT sign_count FROM webauthn_credentials").fetchone()[0] == 2 ** 32 - 1
    conn.close()


def test_a_key_without_counter_stays_usable_with_a_zero_counter(loose_app):
    app = loose_app
    conn = connect(app)
    row, auth = key_user(app, conn, counter=0)
    for _ in range(3):
        client = pending(app, 'kay')
        opts = options(app, client).get_data(as_text=True)
        assert verify(app, client, assertion(auth, opts, counter=0)).status_code == 200
    conn.close()


def test_five_failures_destroy_the_state_and_the_limiter_counts_per_account(app, conn):
    row, auth = key_user(app, conn)
    client = pending(app, 'kay')
    statuses = []
    for _ in range(5):
        opts = options(app, client)
        if opts.status_code != 200:
            statuses.append(('options', opts.status_code))
            break
        statuses.append(verify(app, client, assertion(auth, opts.get_data(as_text=True), origin='https://evil.example')).status_code)
    assert statuses.count(401) == 5 and app.extensions['pending'].size() == 0
    assert options(app, client).status_code == 401                                # plus d'état : plus de défi
    fresh = app.test_client()
    assert login(app, 'kay', client=fresh, ip=new_ip()).get_json()['step'] == 'mfa'
    opts = options(app, fresh)
    r = verify(app, fresh, assertion(auth, opts.get_data(as_text=True)))
    assert r.status_code == 429 and not logged_in(app, fresh)                    # 5 échecs du compte dans la fenêtre : même la bonne clé attend


def test_two_simultaneous_assertions_with_the_same_challenge_open_one_session_only(app, conn):
    row, auth = key_user(app, conn)
    client = pending(app, 'kay')
    opts = options(app, client).get_data(as_text=True)
    credential = assertion(auth, opts)
    cookie = raw_cookie(client)

    def job(i):
        c = app.test_client()
        c.set_cookie('session', cookie, domain=HOST)
        r = verify(app, c, credential)
        return r.status_code, logged_in(app, c)
    results = concurrent(4, job)
    assert [r for r in results if isinstance(r, Exception)] == []
    assert sum(1 for status, opened in results if status == 200 and opened) == 1


def test_the_cookie_of_an_opened_login_cannot_be_replayed_for_a_second_session(app, conn):
    row, auth = key_user(app, conn)
    client = pending(app, 'kay')
    cookie = raw_cookie(client)
    opts = options(app, client).get_data(as_text=True)
    assert verify(app, client, assertion(auth, opts)).status_code == 200
    replay = app.test_client()
    replay.set_cookie('session', cookie, domain=HOST)
    assert options(app, replay).status_code == 401 and verify(app, replay, assertion(auth, opts)).status_code == 401


def test_the_webauthn_routes_need_a_pending_state_csrf_and_a_same_origin(app, conn):
    row, auth = key_user(app, conn)
    anonymous = app.test_client()
    assert options(app, anonymous).status_code == 401 and verify(app, anonymous, {}).status_code == 401
    client = pending(app, 'kay')
    assert options(app, client, token=False).status_code in (400, 403) and options(app, client, origin='https://evil.example').status_code == 403
    opts = options(app, client).get_data(as_text=True)
    credential = assertion(auth, opts)
    assert verify(app, client, credential, token=False).status_code in (400, 403)
    assert verify(app, client, credential, origin='https://evil.example').status_code == 403
    assert verify(app, client, credential).status_code == 200                        # ces refus ne consomment ni le défi ni un essai


def test_an_account_without_keys_gets_no_challenge_and_a_session_state_is_not_enough(app, conn):
    add(app, conn, 'tot', admin=True, secret=new_secret())
    client = pending(app, 'tot')
    assert options(app, client).status_code == 400
    plain = add(app, conn, 'bob')
    c = session_client(app, plain)
    assert form_post(app, '/auth/password/mfa/webauthn/options', {}, client=c).status_code == 401


def test_the_pending_state_dies_with_a_security_change_even_with_a_valid_assertion(app, conn):
    row, auth = key_user(app, conn)
    client = pending(app, 'kay')
    opts = options(app, client).get_data(as_text=True)
    conn.execute("UPDATE users SET session_token = 'rotated' WHERE id = ?", (row['id'],))
    conn.commit()
    r = verify(app, client, assertion(auth, opts))
    assert r.status_code == 401 and not logged_in(app, client)


# ===== 3. Enrôlement et profil ==================================================================================================

def bootstrap(app, conn, name='chef'):
    created = tokens.create(conn, 'bootstrap', actor=CLI, email=f'{name}@example.org', is_admin_grant=True)
    client = app.test_client()
    r = form_post(app, '/auth/token/consume', {'token': created.token, 'username': name, 'password': GOOD}, client=client)
    assert r.status_code == 200 and r.get_json()['next'].endswith('/auth/password/enroll')
    return client


def enroll_options(app, client, **kw):
    return form_post(app, '/auth/password/enroll/webauthn/options', {}, client=client, **kw)


def enroll_verify(app, client, credential, name=None, **kw):
    payload = {'credential': credential, **({'name': name} if name is not None else {})}
    return form_post(app, '/auth/password/enroll/webauthn/verify', payload, client=client, raw=json.dumps(payload), **kw)


def test_the_first_key_of_a_bootstrap_account_grants_admin_in_the_same_step_with_recovery_codes(app, conn):
    client = bootstrap(app, conn)
    assert not refresh(conn, 'chef')['is_admin']
    auth = VirtualAuthenticator(RP, ORIGIN)
    opts = enroll_options(app, client)
    assert opts.status_code == 200 and json.loads(opts.get_data())['rp']['id'] == RP
    assert json.loads(opts.get_data()).get('attestation', 'none') == 'none'
    r = enroll_verify(app, client, auth.create(opts.get_data(as_text=True)), name='Ma clé')
    assert r.status_code == 200 and len(r.get_json()['recovery_codes']) == 10 and r.get_json()['next'].endswith('/auth/password/login')
    user = refresh(conn, 'chef')
    assert user['is_admin'] == 1 and factors.has_second_factor(conn, user['id'])
    key = conn.execute("SELECT * FROM webauthn_credentials").fetchone()
    assert key['name'] == 'Ma clé' and key['credential_id'] == auth.credential_id and key['user_id'] == user['id']
    assert 'user_id' not in cookie_payload(app, client)
    actions = [r['action'] for r in conn.execute("SELECT action FROM audit_log ORDER BY id")]
    assert actions.index('user.factor_added') < actions.index('user.admin_grant')
    session2 = app.test_client()
    assert login(app, 'chef', client=session2, ip=new_ip()).get_json()['step'] == 'mfa'
    o = options(app, session2)
    assert verify(app, session2, assertion(auth, o.get_data(as_text=True), counter=1)).status_code == 200


@pytest.mark.parametrize('name', ['origine étrangère', 'RP étranger', 'défi faux', 'type get'])
def test_a_forged_registration_enrols_nothing_and_grants_nothing(app, conn, name):
    client = bootstrap(app, conn)
    auth = VirtualAuthenticator(RP, ORIGIN)
    opts = enroll_options(app, client).get_data(as_text=True)
    kwargs = {'origine étrangère': dict(origin='https://evil.example'), 'RP étranger': dict(rp_id='evil.example'),
              'défi faux': dict(challenge=b64(b'z' * 32)), 'type get': {}}[name]
    credential = auth.create(opts, **kwargs)
    if name == 'type get':
        client_data = json.loads(__import__('base64').urlsafe_b64decode(credential['response']['clientDataJSON'] + '=='))
        client_data['type'] = 'webauthn.get'
        credential['response']['clientDataJSON'] = b64(json.dumps(client_data).encode())
    r = enroll_verify(app, client, credential)
    assert r.status_code in (400, 401)
    user = refresh(conn, 'chef')
    assert not user['is_admin'] and conn.execute("SELECT COUNT(*) FROM webauthn_credentials").fetchone()[0] == 0


def test_an_enrollment_challenge_is_single_use(app, conn):
    client = bootstrap(app, conn)
    auth = VirtualAuthenticator(RP, ORIGIN)
    opts = enroll_options(app, client).get_data(as_text=True)
    credential = auth.create(opts)
    assert enroll_verify(app, client, credential).status_code == 200
    again = bootstrap(app, conn, 'chef2')
    enroll_options(app, again)
    assert enroll_verify(app, again, credential).status_code in (400, 401)           # même réponse, autre défi
    assert not refresh(conn, 'chef2')['is_admin']


def test_the_same_physical_key_cannot_be_registered_twice(app, conn):
    first = bootstrap(app, conn, 'chef1')
    auth = VirtualAuthenticator(RP, ORIGIN)
    assert enroll_verify(app, first, auth.create(enroll_options(app, first).get_data(as_text=True))).status_code == 200
    second = bootstrap(app, conn, 'chef2')
    r = enroll_verify(app, second, auth.create(enroll_options(app, second).get_data(as_text=True)))
    assert r.status_code in (400, 401, 409) and not refresh(conn, 'chef2')['is_admin']
    assert conn.execute("SELECT COUNT(*) FROM webauthn_credentials").fetchone()[0] == 1


@pytest.mark.parametrize('name', ['', '   ', 'x' * 61, 'a\nb', 'a\x00b', 'a‮b', 5, ['x'], {'a': 1}])
def test_invalid_key_names_enrol_nothing(app, conn, name):
    client = bootstrap(app, conn)
    auth = VirtualAuthenticator(RP, ORIGIN)
    opts = enroll_options(app, client).get_data(as_text=True)
    r = enroll_verify(app, client, auth.create(opts), name=name)
    if name in ('', '   '):
        assert r.status_code in (200, 400, 401)
    else:
        assert r.status_code in (400, 401), name
        assert conn.execute("SELECT COUNT(*) FROM webauthn_credentials").fetchone()[0] == 0


def test_hostile_registration_values_are_client_errors(app, conn):
    client = bootstrap(app, conn)
    enroll_options(app, client)
    for credential in (None, 5, [], {}, 'x', {'id': 1}, 'a' * 40_000, {'response': {'attestationObject': 'AAAA', 'clientDataJSON': 'AAAA'}}):
        payload = {'credential': credential}
        r = form_post(app, '/auth/password/enroll/webauthn/verify', payload, client=client, raw=json.dumps(payload))
        assert r.status_code in (400, 401, 413), (credential if not isinstance(credential, str) else credential[:20], r.status_code)
        if r.status_code == 401 and r.get_json().get('restart'):
            break
    assert not refresh(conn, 'chef')['is_admin']


def test_profile_registration_needs_the_current_password_and_binds_the_challenge_to_the_session(app, conn):
    user = add(app, conn, 'bob', admin=False)
    client, other_session = session_client(app, user), session_client(app, user)
    assert call(client, 'post', '/api/me/mfa/webauthn/options', {}).status_code == 400
    assert call(client, 'post', '/api/me/mfa/webauthn/options', {'password': OTHER}).status_code == 403
    opts = call(client, 'post', '/api/me/mfa/webauthn/options', {'password': GOOD})
    assert opts.status_code == 200
    auth = VirtualAuthenticator(RP, ORIGIN)
    credential = auth.create(opts.get_data(as_text=True))
    thief = session_client(app, add(app, conn, 'eve'))
    assert call(thief, 'post', '/api/me/mfa/webauthn/verify', {'credential': credential}).status_code in (403, 409)      # défi d'un autre compte
    r = call(client, 'post', '/api/me/mfa/webauthn/verify', {'credential': credential, 'name': 'Clé du profil'})
    assert r.status_code == 200 and len(r.get_json()['recovery_codes']) == 10
    assert not refresh(conn, 'bob')['is_admin'] and factors.has_second_factor(conn, user['id'])
    assert call(client, 'get', '/api/me').status_code == 200                                    # la session COURANTE est réémise (T11-1)
    assert call(other_session, 'get', '/api/me').status_code == 401                             # les AUTRES sessions du compte tombent
    assert call(client, 'post', '/api/me/mfa/webauthn/verify', {'credential': credential}).status_code in (401, 403, 409)


def test_profile_registration_replay_and_session_change_kill_the_challenge(app, conn):
    user = add(app, conn, 'bob', admin=False)
    client = session_client(app, user)
    opts = call(client, 'post', '/api/me/mfa/webauthn/options', {'password': GOOD}).get_data(as_text=True)
    conn.execute("UPDATE users SET session_token = 'rotated' WHERE id = ?", (user['id'],))
    conn.commit()
    fresh = session_client(app, refresh(conn, 'bob'))
    auth = VirtualAuthenticator(RP, ORIGIN)
    assert call(fresh, 'post', '/api/me/mfa/webauthn/verify', {'credential': auth.create(opts)}).status_code in (401, 403, 409)
    assert not factors.has_second_factor(conn, user['id'])


def fill_keys(conn, row, n):
    for i in range(n):
        add_key(conn, row, VirtualAuthenticator(RP, ORIGIN), name=f'k{i}')


def test_the_profile_refuses_an_eleventh_key(app, conn):
    user = add(app, conn, 'bob')
    fill_keys(conn, user, 10)
    client = session_client(app, user)
    assert call(client, 'post', '/api/me/mfa/webauthn/options', {'password': GOOD}).status_code == 409
    with pytest.raises(webauthn_factor.WebAuthnError):
        webauthn_factor.add_credential(conn, user['id'], b'z' * 32, b'k', 0, None, 'onze')
    assert conn.execute("SELECT COUNT(*) FROM webauthn_credentials").fetchone()[0] == 10


def test_the_mfa_state_lists_keys_by_name_and_never_exposes_the_key_material(app, conn):
    user = add(app, conn, 'bob')
    auth = add_key(conn, user, name='Ma clé <b>')
    response = call(session_client(app, user), 'get', '/api/me/mfa')
    body = response.get_data(as_text=True)
    names = [k.get('name') for k in response.get_json().get('key_list', [])]
    assert names == ['Ma clé <b>'] and b64(auth.credential_id) not in body and b64(auth._cose()) not in body
    for forbidden in ('public_key', 'credential_id', 'sign_count_secret'):
        assert forbidden not in body
    admin = add(app, conn, 'adm', admin=True, secret=new_secret())
    listing = call(session_client(app, admin), 'get', '/api/admin/users').get_data(as_text=True)
    assert b64(auth.credential_id) not in listing and 'public_key' not in listing


# ===== 4. EF-2.2, réinitialisation, retrait =====================================================================================

def key_only_admin(app, conn, name='kay'):
    row = add(app, conn, name, admin=True)
    auth = add_key(conn, row)
    second_factor_codes = __import__('app.auth.second_factor', fromlist=['x']).replace_recovery_codes(conn, row['id'])
    conn.commit()
    return row, auth, second_factor_codes


def test_an_admin_with_only_a_key_keeps_the_admin_routes_and_loses_them_without_it(app, conn):
    row, auth, _codes = key_only_admin(app, conn)
    client = session_client(app, row)
    assert call(client, 'get', '/api/admin/users').status_code == 200
    conn.execute("DELETE FROM webauthn_credentials")
    conn.commit()
    assert call(client, 'get', '/api/admin/users').status_code == 403


def test_the_last_key_of_an_admin_cannot_be_removed_but_a_second_one_can(app, conn):
    row, auth, codes = key_only_admin(app, conn)
    client = session_client(app, row)
    key_id = conn.execute("SELECT id FROM webauthn_credentials").fetchone()[0]
    assert key_id
    r = call(client, 'delete', f'/api/me/mfa/webauthn/{key_id}', {'password': GOOD, 'code': codes[0]})
    assert r.status_code == 409 and conn.execute("SELECT COUNT(*) FROM webauthn_credentials").fetchone()[0] == 1
    add_key(conn, row, VirtualAuthenticator(RP, ORIGIN), name='deux')
    client, other_session = session_client(app, refresh(conn, 'kay')), session_client(app, refresh(conn, 'kay'))
    r = call(client, 'delete', f'/api/me/mfa/webauthn/{key_id}', {'password': GOOD, 'code': codes[1]})
    assert r.status_code == 200 and conn.execute("SELECT COUNT(*) FROM webauthn_credentials").fetchone()[0] == 1
    assert call(client, 'get', '/api/me').status_code == 200 and call(other_session, 'get', '/api/me').status_code == 401     # courante réémise, autres tombées


def test_removing_a_key_needs_password_and_a_second_proof_and_never_touches_another_account(app, conn):
    row, auth, codes = key_only_admin(app, conn)
    add_key(conn, row, VirtualAuthenticator(RP, ORIGIN), name='deux')
    victim = add(app, conn, 'bob')
    victim_auth = add_key(conn, victim)
    victim_key = conn.execute("SELECT id FROM webauthn_credentials WHERE user_id = ?", (victim['id'],)).fetchone()[0]
    mine = conn.execute("SELECT id FROM webauthn_credentials WHERE user_id = ?", (row['id'],)).fetchone()[0]
    client = session_client(app, row)
    assert call(client, 'delete', f'/api/me/mfa/webauthn/{mine}', {'password': GOOD}).status_code == 403
    assert call(client, 'delete', f'/api/me/mfa/webauthn/{mine}', {'password': OTHER, 'code': codes[2]}).status_code == 403
    assert call(client, 'delete', f'/api/me/mfa/webauthn/{mine}', {'password': GOOD, 'code': '000000'}).status_code == 403
    r = call(client, 'delete', f'/api/me/mfa/webauthn/{victim_key}', {'password': GOOD, 'code': codes[3]})
    assert r.status_code == 404 and conn.execute("SELECT COUNT(*) FROM webauthn_credentials WHERE user_id = ?", (victim['id'],)).fetchone()[0] == 1
    assert victim_auth.credential_id
    assert call(client, 'delete', '/api/me/mfa/webauthn/99999', {'password': GOOD, 'code': codes[4]}).status_code == 404
    assert call(client, 'delete', f'/api/me/mfa/webauthn/{mine}', {'password': GOOD, 'code': codes[5]}, origin='https://evil.example').status_code == 403


def test_recovery_codes_die_with_the_last_key_of_a_non_admin_and_survive_while_a_factor_remains(app, conn):
    from app.auth import second_factor
    row = add(app, conn, 'bob')
    add_key(conn, row, name='une')
    second = add_key(conn, row, VirtualAuthenticator(RP, ORIGIN), name='deux')
    codes = second_factor.replace_recovery_codes(conn, row['id'])
    conn.commit()
    ids = [r[0] for r in conn.execute("SELECT id FROM webauthn_credentials ORDER BY id")]
    client = session_client(app, row)
    assert call(client, 'delete', f'/api/me/mfa/webauthn/{ids[0]}', {'password': GOOD, 'code': codes[0]}).status_code == 200
    assert second_factor.remaining_recovery(conn, row['id']) == 9                                   # un facteur reste : codes conservés
    client = session_client(app, refresh(conn, 'bob'))
    assert call(client, 'delete', f'/api/me/mfa/webauthn/{ids[1]}', {'password': GOOD, 'code': codes[1]}).status_code == 200
    assert second_factor.remaining_recovery(conn, row['id']) == 0 and not factors.has_second_factor(conn, row['id']) and second.credential_id


def test_mfa_reset_by_another_admin_removes_the_keys_and_the_audit_has_no_key_material(app, conn):
    target, auth, _codes = key_only_admin(app, conn, 'kay')
    other = add(app, conn, 'adm2', admin=True, secret=new_secret())
    r = call(session_client(app, other), 'post', f"/api/admin/users/{target['id']}/mfa-reset", {})
    assert r.status_code == 200 and r.get_json()['demoted'] is True
    assert conn.execute("SELECT COUNT(*) FROM webauthn_credentials").fetchone()[0] == 0 and not refresh(conn, 'kay')['is_admin']
    dump = json.dumps([dict(x) for x in conn.execute("SELECT * FROM audit_log")])
    assert b64(auth.credential_id) not in dump and 'public_key' not in dump


def test_the_login_with_a_key_does_not_consume_a_totp_step_and_both_factors_work_side_by_side(app, conn):
    from tests.pw_harness import code_at
    secret = new_secret()
    row = add(app, conn, 'kay', admin=True, secret=secret)
    auth = add_key(conn, row)
    client = pending(app, 'kay')
    opts = options(app, client).get_data(as_text=True)
    assert verify(app, client, assertion(auth, opts)).status_code == 200
    other = pending(app, 'kay')
    from tests.pw_harness import mfa
    assert mfa(app, other, code_at(secret)).status_code == 200


def test_the_enroll_and_login_pages_ship_the_webauthn_script_without_inline_code(app, conn):
    row, auth = key_user(app, conn)
    page = pending(app, 'kay').get('/auth/password/mfa', **KW).get_data(as_text=True)
    assert 'webauthn' in page.lower() and not re.search(r'<script(?![^>]*\bsrc=)[^>]*>\s*\S', page) and not re.search(r'\son[a-z]+\s*=', page)
    source = (__import__('pathlib').Path(__file__).resolve().parent.parent / 'static' / 'js' / 'webauthn.js').read_text(encoding='utf-8')
    code = re.sub(r'//.*', '', source)
    assert not re.search(r'\.innerHTML|insertAdjacentHTML|document\.write|\beval\s*\(|new\s+Function|localStorage|sessionStorage', code)
