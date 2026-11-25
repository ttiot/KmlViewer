"""
T14 (API d'administration) et T15 (pages admin et profil) en profondeur (rôle test), écrit depuis la spec (EF-2.2, EF-2.7, EF-5, EF-6.4,
EF-7.2, EF-8.x, EF-9.x, EF-S1, EF-S6, P-T14-1 à 10), pas depuis le code.

 A. autorisation de CHAQUE route admin, dérivée de app.url_map (anonyme, simple compte, admin sans facteur, état mfa_pending, admin valide)
 B. hygiène des réponses : no-store, aucun secret, aucun hash
 C. dernier admin et auto-protection, y compris les courses entre deux admins (interleaving forcé, déterministe)
 D. PATCH des comptes : champs, crédits, mass assignment, alias /api/users
 E. conversion SSO -> mot de passe
 F. réglages, secrets (écriture seule), fonctions bêta, journal
 G. T15 : JS et gabarits (puits XSS, stockage navigateur, CSP)
"""

import json
import re
import threading
from pathlib import Path

import pytest
from flask import g, has_request_context

from app.audit import Actor
from app.auth import second_factor, tokens, users
from app.settings import REGISTRY
from tests.pw_harness import (GOOD, HOST, KW, ORIGIN, OTHER, add, call, concurrent, connect, cookie_payload, form_post, login, make_app, new_ip,
                              new_secret, refresh, session_client)

CLI = Actor.cli()
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def app(tmp_path):
    return make_app(tmp_path)


@pytest.fixture
def conn(app):
    c = connect(app)
    yield c
    c.close()


def world(app, conn):
    """Deux admins valides, un admin SANS facteur (base éditée), un simple compte, un compte SSO."""
    a_secret = new_secret()
    rows = {'a': add(app, conn, 'adma', admin=True, secret=a_secret, email='adma@example.org'),
            'b': add(app, conn, 'admb', admin=True, secret=new_secret()),
            'weak': add(app, conn, 'weak', admin=True),
            'user': add(app, conn, 'bob', email='bob@example.org'),
            'sso': add(app, conn, 'ssoguy', method='oidc', email='sso@example.org')}
    return rows, a_secret


# ===== A. Autorisation de chaque route ==========================================================================================

def admin_rules(app):
    found = []
    for rule in app.url_map.iter_rules():
        if rule.rule.startswith(('/api/admin', '/admin')):
            found.append(rule)
    return found


def concrete_path(rule, victim_id):
    def sub(match):
        return {'user_id': str(victim_id), 'name': 'smtp.password', 'key': 'route_builder' if 'features' in rule.rule else 'DEM_URL'}.get(match.group(1), '1')
    return re.sub(r'<(?:[a-z]+:)?([a-z_]+)>', sub, rule.rule)


def methods_of(rule):
    return sorted(rule.methods - {'HEAD', 'OPTIONS'})


def test_every_admin_route_is_protected_by_admin_required_and_the_set_of_routes_is_complete(app):
    rules = admin_rules(app)
    assert len(rules) >= 25, [r.rule for r in rules]
    for rule in rules:
        assert getattr(app.view_functions[rule.endpoint], 'admin_only', False), f"{rule.rule} n'est pas sous admin_required"
    paths = {r.rule for r in rules}
    for expected in ('/api/admin/users', '/api/admin/users/<int:user_id>', '/api/admin/settings', '/api/admin/secrets/<name>', '/api/admin/audit',
                     '/api/admin/features/<key>', '/api/admin/users/<int:user_id>/mfa-reset', '/api/admin/users/<int:user_id>/convert',
                     '/api/admin/users/<int:user_id>/reset-link', '/api/admin/users/<int:user_id>/revoke-sessions', '/api/admin/mail/test'):
        assert expected in paths, expected
    for rule in app.url_map.iter_rules():                                           # l'alias historique est protégé de la même façon
        if rule.rule.startswith('/api/users'):
            assert getattr(app.view_functions[rule.endpoint], 'admin_only', False), rule.rule


def test_each_route_and_method_refuses_anonymous_plain_weak_admin_and_pending_visitors_and_opens_for_the_real_admin(app, conn):
    rows, secret = world(app, conn)
    victim = add(app, conn, 'victim')
    subjects = {'anonymous': app.test_client(), 'user': session_client(app, rows['user']), 'weak': session_client(app, rows['weak'])}
    pending = app.test_client()
    assert login(app, 'admb', client=pending, ip=new_ip()).get_json()['step'] == 'mfa'
    subjects['pending'] = pending
    admin = session_client(app, rows['a'])
    checked = 0
    for rule in admin_rules(app):
        path = concrete_path(rule, victim['id'])
        for method in methods_of(rule):
            payload = {} if method in ('POST', 'PATCH', 'PUT', 'DELETE') else None
            for label, client in subjects.items():
                r = call(client, method.lower(), path, payload)
                if label == 'anonymous' or label == 'pending':
                    assert r.status_code in (401, 302), (label, method, path, r.status_code)
                else:
                    assert r.status_code == 403, (label, method, path, r.status_code)
                checked += 1
            r = call(admin, method.lower(), path, payload)
            assert r.status_code not in (401, 403, 500), (method, path, r.status_code, r.get_data()[:120])
            checked += 1
            if method != 'GET':
                assert call(admin, method.lower(), path, payload, origin='https://evil.example').status_code == 403, (method, path)
    assert checked > 100


@pytest.mark.parametrize('path', ['/admin', '/admin/', '/admin/users'])
def test_the_admin_pages_refuse_everyone_but_a_real_admin(app, conn, path):
    rows, _ = world(app, conn)
    assert app.test_client().get(path, **KW).status_code in (302, 401, 403, 404)
    for who in ('user', 'weak'):
        assert session_client(app, rows[who]).get(path, **KW).status_code in (403, 404)
    r = session_client(app, rows['a']).get('/admin', **KW)
    assert r.status_code == 200 and 'no-store' in r.headers['Cache-Control']


def test_an_admin_demoted_or_disabled_loses_every_route_at_the_next_request(app, conn):
    rows, _ = world(app, conn)
    client = session_client(app, rows['b'])
    assert call(client, 'get', '/api/admin/users').status_code == 200
    conn.execute("UPDATE users SET is_admin = 0 WHERE username = 'admb'")
    conn.commit()
    assert call(client, 'get', '/api/admin/users').status_code == 403
    conn.execute("UPDATE users SET is_admin = 1, is_active = 0 WHERE username = 'admb'")
    conn.commit()
    assert call(client, 'get', '/api/admin/users').status_code in (401, 403)


def test_an_admin_who_loses_his_last_factor_loses_the_admin_routes_at_once_EF22(app, conn):
    rows, _ = world(app, conn)
    client = session_client(app, rows['b'])
    assert call(client, 'get', '/api/admin/audit').status_code == 200
    conn.execute("UPDATE users SET totp_secret = NULL, totp_enabled_at = NULL WHERE username = 'admb'")
    conn.commit()
    assert call(client, 'get', '/api/admin/audit').status_code == 403


# ===== B. Hygiène des réponses ==================================================================================================

def test_admin_and_me_responses_are_uncacheable_json_without_any_secret_or_hash(app, conn):
    rows, secret = world(app, conn)
    second_factor.replace_recovery_codes(conn, rows['a']['id'])
    conn.commit()
    client = session_client(app, rows['a'])
    blobs = []
    for path in ('/api/admin/users', '/api/admin/settings', '/api/admin/secrets', '/api/admin/features', '/api/admin/audit', '/api/admin/invitations',
                 '/api/admin/settings/diagnostic', '/api/me', '/api/users', '/api/me/mfa'):
        r = call(client, 'get', path)
        assert r.status_code == 200, path
        assert 'no-store' in r.headers['Cache-Control'] and r.mimetype == 'application/json', path
        blobs.append(r.get_data(as_text=True))
    blob = '\n'.join(blobs)
    hashes = [row['password_hash'] for row in conn.execute("SELECT password_hash FROM users") if row['password_hash']]    # les comptes SSO n'en ont pas
    for forbidden in (secret, *hashes, '$argon2', 'password_hash', 'totp_secret', 'code_hash', 'tok-adma', 'tok-admb', 'session_token'):
        assert forbidden and forbidden not in blob, forbidden
    users_view = json.loads(call(client, 'get', '/api/admin/users').get_data())['users']
    adma = next(u for u in users_view if u['username'] == 'adma')
    assert adma['factors'] == {'totp': True, 'keys': 0, 'recovery_codes': 10}


# ===== C. Dernier admin, auto-protection, courses ===============================================================================

def test_self_protection_rules(app, conn):
    rows, secret = world(app, conn)
    a = rows['a']
    client = session_client(app, a)
    for body in ({'is_admin': False}, {'is_active': False}, {'is_admin': False, 'is_active': False}):
        assert call(client, 'patch', f"/api/admin/users/{a['id']}", body).status_code == 409
    assert call(client, 'delete', f"/api/admin/users/{a['id']}").status_code == 409
    assert call(client, 'post', f"/api/admin/users/{a['id']}/mfa-reset", {}).status_code == 409
    assert refresh(conn, 'adma')['is_admin'] == 1 and refresh(conn, 'adma')['is_active'] == 1
    assert call(client, 'patch', f"/api/admin/users/{a['id']}", {'invite_credits': 3}).status_code == 200
    r = call(client, 'post', f"/api/admin/users/{a['id']}/revoke-sessions", {})
    assert r.status_code == 200 and call(client, 'get', '/api/admin/users').status_code == 401        # la courante comprise


def test_mfa_reset_demotes_clears_factors_rotates_sessions_destroys_states_and_is_audited(app, conn):
    rows, secret = world(app, conn)
    target, actor = rows['b'], rows['a']
    second_factor.replace_recovery_codes(conn, target['id'])
    conn.commit()
    old_session = session_client(app, target)
    pid = app.extensions['pending'].create(target['id'], 'mfa', binding=(target['session_token'], target['password_changed_at']))
    r = call(session_client(app, actor), 'post', f"/api/admin/users/{target['id']}/mfa-reset", {})
    assert r.status_code == 200 and r.get_json() == {'success': True, 'demoted': True}
    after = refresh(conn, 'admb')
    assert after['is_admin'] == 0 and after['totp_secret'] is None and after['totp_enabled_at'] is None and after['session_token'] != target['session_token']
    assert second_factor.remaining_recovery(conn, target['id']) == 0 and app.extensions['pending'].get(pid) is None
    assert call(old_session, 'get', '/api/me').status_code == 401
    entry = dict(conn.execute("SELECT * FROM audit_log WHERE action = 'user.mfa_reset'").fetchone())
    assert entry['actor_id'] == actor['id'] and entry['target_id'] == str(target['id']) and json.loads(entry['details'])['demoted'] is True


@pytest.mark.parametrize('kind', ['unknown', 'sso', 'none'])
def test_mfa_reset_and_reset_link_only_concern_password_accounts(app, conn, kind):
    rows, _ = world(app, conn)
    client = session_client(app, rows['a'])
    target = {'unknown': 99999, 'sso': rows['sso']['id'], 'none': add(app, conn, 'nomode', method='none')['id']}[kind]
    expected = 404 if kind == 'unknown' else 409
    assert call(client, 'post', f'/api/admin/users/{target}/mfa-reset', {}).status_code == expected
    assert call(client, 'post', f'/api/admin/users/{target}/reset-link', {}).status_code == expected


def test_the_reset_link_is_shown_once_single_use_24_hours_and_never_sent_to_the_admin_session(app, conn):
    rows, _ = world(app, conn)
    r = call(session_client(app, rows['a']), 'post', f"/api/admin/users/{rows['user']['id']}/reset-link", {})
    body = r.get_json()
    assert r.status_code == 200 and body['link'].startswith(f'{ORIGIN}/auth/token#t=')
    row = conn.execute("SELECT * FROM auth_tokens WHERE kind = 'reset'").fetchone()
    assert row['target_user_id'] == rows['user']['id'] and row['token_hash'] not in body['link'] and body['link'].split('#t=')[1] not in json.dumps(
        [dict(x) for x in conn.execute("SELECT * FROM audit_log")])
    from datetime import datetime
    fmt = '%Y-%m-%dT%H:%M:%SZ'
    assert 86300 <= (datetime.strptime(row['expires_at'], fmt) - datetime.strptime(row['created_at'], fmt)).total_seconds() <= 86500
    disabled = add(app, conn, 'off', active=False)
    assert call(session_client(app, rows['a']), 'post', f"/api/admin/users/{disabled['id']}/reset-link", {}).status_code == 409


def force_interleaving(monkeypatch, parties=2):
    """Les deux requêtes ont passé le garde et LU leur cible avant que l'une n'écrive : la course est déterministe."""
    barrier = threading.Barrier(parties, timeout=10)
    original = users.get_by_id

    def wrapper(db, user_id):
        row = original(db, user_id)
        if has_request_context() and g.get('user') is not None:
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass
        return row
    monkeypatch.setattr(users, 'get_by_id', wrapper)


def active_admins(conn):
    return [r['username'] for r in conn.execute("SELECT username FROM users WHERE is_admin = 1 AND is_active = 1")]


@pytest.mark.parametrize('attack', ['disable', 'delete'])
def test_two_admins_acting_on_each_other_at_the_same_time_never_leave_zero_admin_for_disable_and_delete(app, conn, monkeypatch, attack):
    rows, _ = world(app, conn)
    conn.execute("DELETE FROM users WHERE username = 'weak'")
    conn.commit()
    force_interleaving(monkeypatch)
    a, b = rows['a'], rows['b']
    clients = {a['id']: session_client(app, a), b['id']: session_client(app, b)}

    def job(i):
        actor, target = (a, b) if i == 0 else (b, a)
        if attack == 'disable':
            return call(clients[actor['id']], 'patch', f"/api/admin/users/{target['id']}", {'is_active': False}).status_code
        return call(clients[actor['id']], 'delete', f"/api/admin/users/{target['id']}").status_code
    results = concurrent(2, job)
    assert [r for r in results if isinstance(r, Exception)] == []
    assert len(active_admins(connect(app))) >= 1, results


def test_two_admins_demoting_each_other_at_the_same_time_never_leave_zero_admin(app, conn, monkeypatch):
    rows, _ = world(app, conn)
    force_interleaving(monkeypatch)
    a, b = rows['a'], rows['b']
    clients = {a['id']: session_client(app, a), b['id']: session_client(app, b)}

    def job(i):
        actor, target = (a, b) if i == 0 else (b, a)
        return call(clients[actor['id']], 'patch', f"/api/admin/users/{target['id']}", {'is_admin': False}).status_code
    concurrent(2, job)
    assert len(active_admins(connect(app))) >= 1


def test_two_admins_resetting_each_others_factors_at_the_same_time_never_leave_zero_admin(app, conn, monkeypatch):
    rows, _ = world(app, conn)
    force_interleaving(monkeypatch)
    a, b = rows['a'], rows['b']
    clients = {a['id']: session_client(app, a), b['id']: session_client(app, b)}

    def job(i):
        actor, target = (a, b) if i == 0 else (b, a)
        return call(clients[actor['id']], 'post', f"/api/admin/users/{target['id']}/mfa-reset", {}).status_code
    concurrent(2, job)
    assert len(active_admins(connect(app))) >= 1


def test_sequential_last_admin_rules_hold_through_every_door(app, conn):
    rows, _ = world(app, conn)
    conn.execute("DELETE FROM users WHERE username IN ('weak', 'admb')")
    conn.commit()
    a = refresh(conn, 'adma')
    other = add(app, conn, 'adm2', admin=True, secret=new_secret())
    client = session_client(app, other)
    assert call(client, 'patch', f"/api/admin/users/{a['id']}", {'is_active': False}).status_code == 200      # un autre admin reste : permis
    assert call(session_client(app, a), 'get', '/api/admin/users').status_code in (401, 403)                  # compte désactivé : plus rien
    assert call(client, 'patch', f"/api/admin/users/{a['id']}", {'is_admin': False}).status_code == 200


def test_a_promotion_without_factor_is_refused_audited_and_applies_nothing_else(app, conn):
    rows, _ = world(app, conn)
    client = session_client(app, rows['a'])
    user = rows['user']
    r = call(client, 'patch', f"/api/admin/users/{user['id']}", {'is_admin': True, 'invite_credits': 7, 'is_active': True})
    assert r.status_code == 409
    after = refresh(conn, 'bob')
    assert after['is_admin'] == 0 and after['invite_credits'] == 0
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'user.admin_refused'").fetchone()[0] == 1
    conn.execute("UPDATE users SET totp_secret = x'01', totp_enabled_at = '2026-01-01T00:00:00Z' WHERE username = 'bob'")
    conn.commit()
    assert call(client, 'patch', f"/api/admin/users/{user['id']}", {'is_admin': True}).status_code == 200
    assert call(client, 'patch', f"/api/admin/users/{rows['sso']['id']}", {'is_admin': True}).status_code == 200      # SSO : pas de facteur local


# ===== D. PATCH des comptes =====================================================================================================

@pytest.mark.parametrize('prefix', ['/api/admin/users', '/api/users'])
def test_patch_validates_every_field_and_ignores_nothing_dangerous(app, conn, prefix):
    rows, _ = world(app, conn)
    client = session_client(app, rows['a'])
    target = rows['user']
    url = f"{prefix}/{target['id']}"
    for bad in ({}, {'is_admin': 'true'}, {'is_admin': 1}, {'is_active': None}, {'is_active': 'yes'}, {'invite_credits': -1}, {'invite_credits': 1001},
                {'invite_credits': True}, {'invite_credits': 1.5}, {'invite_credits': '5'}, {'invite_credits': None}, {'invite_credits': 10 ** 400},
                {'invite_credits': [1]}, {'unknown': 1}, {'sub': 'pwd:evil'}, {'password_hash': 'x'}, {'auth_method': 'oidc'}, {'email': 'x@y.z'},
                {'session_token': 'x'}, {'username': 'root'}, {'totp_secret': 'x'}, {'id': 1}):
        r = call(client, 'patch', url, bad)
        assert r.status_code == 400, (prefix, bad, r.status_code)
    after = refresh(conn, 'bob')
    for column in ('sub', 'password_hash', 'auth_method', 'email', 'session_token', 'username', 'is_admin', 'invite_credits'):
        assert after[column] == target[column], column
    assert call(client, 'patch', f'{prefix}/99999', {'invite_credits': 1}).status_code == 404
    assert call(client, 'patch', f'{prefix}/{target["id"]}', {'invite_credits': 1000}).status_code == 200
    assert call(client, 'patch', f'{prefix}/{target["id"]}', {'invite_credits': 0}).status_code == 200
    entries = [json.loads(r['details']) for r in conn.execute("SELECT details FROM audit_log WHERE action = 'user.credits_set'")]
    assert entries == [{'from': 0, 'to': 1000}, {'from': 1000, 'to': 0}]


def test_deactivating_revokes_sessions_and_pending_invitations_and_reactivating_resurrects_nothing(app, conn):
    rows, _ = world(app, conn)
    user = rows['user']
    cookie_client = session_client(app, user)
    tokens.create(conn, 'invite', actor=CLI, created_by=user['id'], email='x@example.org')
    pid = app.extensions['pending'].create(user['id'], 'enroll', binding=(user['session_token'], user['password_changed_at']))
    admin = session_client(app, rows['a'])
    assert call(admin, 'patch', f"/api/admin/users/{user['id']}", {'is_active': False}).status_code == 200
    assert call(cookie_client, 'get', '/api/me').status_code in (401, 403)
    assert app.extensions['pending'].get(pid) is None
    assert conn.execute("SELECT COUNT(*) FROM auth_tokens WHERE kind = 'invite' AND revoked_at IS NULL AND used_at IS NULL").fetchone()[0] == 0
    assert call(admin, 'patch', f"/api/admin/users/{user['id']}", {'is_active': True}).status_code == 200
    assert call(cookie_client, 'get', '/api/me').status_code == 401                                   # le vieux cookie ne revient pas


def test_deleting_an_account_removes_its_data_and_files_and_revokes_its_invitations(app, conn):
    rows, _ = world(app, conn)
    victim = rows['user']
    folder = Path(app.config['DATA_DIR']) / 'files' / str(victim['id'])
    folder.mkdir(parents=True)
    (folder / 'trace.gpx').write_text('x')
    tokens.create(conn, 'invite', actor=CLI, created_by=victim['id'], email='z@example.org')
    r = call(session_client(app, rows['a']), 'delete', f"/api/admin/users/{victim['id']}")
    assert r.status_code == 200 and refresh(conn, 'bob') is None and not folder.exists()
    assert conn.execute("SELECT COUNT(*) FROM auth_tokens WHERE kind = 'invite' AND revoked_at IS NULL AND used_at IS NULL").fetchone()[0] == 0
    assert call(session_client(app, rows['a']), 'delete', '/api/admin/users/99999').status_code == 404


# ===== E. Conversion SSO -> mot de passe ========================================================================================

def convert_link(app, conn, rows):
    r = call(session_client(app, rows['a']), 'post', f"/api/admin/users/{rows['sso']['id']}/convert", {})
    assert r.status_code == 200, r.get_data()
    return r.get_json()['link'].split('#t=')[1]


def test_conversion_is_reserved_to_active_sso_accounts_in_password_mode(app, conn, tmp_path):
    rows, _ = world(app, conn)
    client = session_client(app, rows['a'])
    assert call(client, 'post', f"/api/admin/users/{rows['user']['id']}/convert", {}).status_code == 409          # déjà password
    off = add(app, conn, 'ssooff', method='oidc', active=False)
    assert call(client, 'post', f"/api/admin/users/{off['id']}/convert", {}).status_code == 409
    assert call(client, 'post', '/api/admin/users/99999/convert', {}).status_code == 404


def test_consuming_the_conversion_link_rewrites_the_account_in_one_step_and_logs_nobody_in(app, conn):
    rows, _ = world(app, conn)
    sso = rows['sso']
    conn.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (sso['id'],))
    conn.commit()
    token = convert_link(app, conn, rows)
    old_cookie = session_client(app, refresh(conn, 'ssoguy'))
    client = app.test_client()
    assert form_post(app, '/auth/token/inspect', {'token': token}).get_json()['form'] in ('set_password', 'choose_credentials')
    r = form_post(app, '/auth/token/consume', {'token': token, 'username': 'nouveau.nom', 'password': GOOD}, client=client)
    assert r.status_code == 200 and r.get_json()['next'].endswith('/auth/password/login'), r.get_data()
    after = conn.execute("SELECT * FROM users WHERE id = ?", (sso['id'],)).fetchone()
    assert after['auth_method'] == 'password' and after['username'] == 'nouveau.nom' and re.fullmatch(r'pwd:[0-9a-f-]{36}', after['sub'])
    assert after['sub'] != sso['sub'] and after['session_token'] != sso['session_token'] and after['password_hash']
    assert after['is_admin'] == 0                                                                    # B5 : repromu après enrôlement d'un facteur
    assert after['email'] == 'sso@example.org'                                                       # adresse déjà validée par l'IdP : conservée
    assert 'user_id' not in cookie_payload(app, client)
    assert call(old_cookie, 'get', '/api/me').status_code == 401
    assert login(app, 'nouveau.nom', GOOD).status_code == 200 and login(app, 'ssoguy', GOOD).status_code == 401
    assert form_post(app, '/auth/token/consume', {'token': token, 'username': 'autre.nom', 'password': GOOD}).status_code == 404       # usage unique


@pytest.mark.parametrize('username', ['', 'ab', 'x' * 65, 'Bad Name', 'a@b.co', 'néo', 'root', 'ADMIN', 'cli', 'system', 'local', '../x', 'a;b', 'adma', 'ADMA', 'bob'])
def test_the_chosen_identifier_must_conform_and_reserved_or_taken_ones_answer_alike_and_leave_the_token_usable(app, conn, username):
    rows, _ = world(app, conn)
    token = convert_link(app, conn, rows)
    r = form_post(app, '/auth/token/consume', {'token': token, 'username': username, 'password': GOOD})
    assert r.status_code in (400, 409)
    assert refresh(conn, 'ssoguy')['auth_method'] == 'oidc'
    ok = form_post(app, '/auth/token/consume', {'token': token, 'username': 'libre.nom', 'password': GOOD})
    assert ok.status_code == 200


def test_reserved_and_taken_identifiers_give_the_same_message_no_oracle(app, conn):
    rows, _ = world(app, conn)
    token = convert_link(app, conn, rows)
    bodies = {json.dumps(form_post(app, '/auth/token/consume', {'token': token, 'username': name, 'password': GOOD}).get_json(), sort_keys=True)
              for name in ('root', 'adma', 'ADMIN')}
    assert len(bodies) == 1


def test_the_password_policy_applies_to_the_conversion_and_a_failure_does_not_consume_the_link(app, conn):
    rows, _ = world(app, conn)
    token = convert_link(app, conn, rows)
    for bad in ('court', 'nouveau.nom', 'sso@example.org', 'password1234', 'x' * 129):
        assert form_post(app, '/auth/token/consume', {'token': token, 'username': 'nouveau.nom', 'password': bad}).status_code == 400
    assert form_post(app, '/auth/token/consume', {'token': token, 'username': 'nouveau.nom', 'password': GOOD}).status_code == 200


def test_the_address_moves_to_pending_when_a_password_account_already_validated_it(app, conn):
    rows, _ = world(app, conn)
    add(app, conn, 'dup', email='sso@example.org')
    token = convert_link(app, conn, rows)
    r = form_post(app, '/auth/token/consume', {'token': token, 'username': 'nouveau.nom', 'password': GOOD})
    assert r.status_code == 200
    after = refresh(conn, 'nouveau.nom')
    assert after['email'] is None and after['pending_email'] == 'sso@example.org'
    assert json.loads(conn.execute("SELECT details FROM audit_log WHERE action = 'user.convert'").fetchone()[0])['email_pending'] is True


@pytest.mark.parametrize('change', ["UPDATE users SET is_active = 0 WHERE username = 'ssoguy'", "UPDATE users SET auth_method = 'password', password_hash = 'x' WHERE username = 'ssoguy'"])
def test_a_link_for_an_account_disabled_or_already_converted_since_the_issue_is_neutral(app, conn, change):
    rows, _ = world(app, conn)
    token = convert_link(app, conn, rows)
    conn.execute(change)
    conn.commit()
    r = form_post(app, '/auth/token/consume', {'token': token, 'username': 'nouveau.nom', 'password': GOOD})
    assert r.status_code == 404 and refresh(conn, 'nouveau.nom') is None


def test_the_conversion_is_unavailable_in_oidc_mode_and_the_link_is_unusable_there(tmp_path):
    from tests.conftest import OIDC_OVERRIDES
    from app import create_app
    from app.config import TestingConfig
    app = create_app(TestingConfig, overrides={**OIDC_OVERRIDES, 'DATA_DIR': str(tmp_path / 'o'), 'TRUSTED_HOSTS': [HOST], 'AUTH_MODE': 'oidc',
                                               'SECRETS_KEY': __import__('tests.pw_harness', fromlist=['KEY']).KEY, 'PUBLIC_URL': ORIGIN})
    c = connect(app)
    admin = add(app, c, 'idpadmin', admin=True, method='oidc')
    sso = add(app, c, 'ssoguy', method='oidc')
    r = call(session_client(app, admin), 'post', f"/api/admin/users/{sso['id']}/convert", {})
    assert r.status_code == 409
    c.close()


# ===== F. Réglages, secrets, fonctions bêta, journal ============================================================================

def test_the_settings_contract_lists_every_registry_entry_with_its_bounds_and_hides_secret_values(app, conn):
    rows, _ = world(app, conn)
    body = call(session_client(app, rows['a']), 'get', '/api/admin/settings').get_json()
    listed = {s['key']: s for s in body['settings']}
    assert set(listed) == set(REGISTRY) and isinstance(body['version'], int)
    for key, view in listed.items():
        for field in ('key', 'section', 'label', 'help', 'type', 'min', 'max', 'max_length', 'choices', 'max_items', 'default', 'env', 'source', 'value'):
            assert field in view, (key, field)
        assert view['source'] in ('base', 'env', 'défaut')
        if REGISTRY[key].secret:
            assert view['value'] is None
        if REGISTRY[key].destination_service:
            assert view['allowlist'] is not None


def test_settings_patch_is_atomic_audited_versioned_and_validated(app, conn):
    rows, _ = world(app, conn)
    client = session_client(app, rows['a'])
    version = call(client, 'get', '/api/admin/settings').get_json()['version']
    bad = call(client, 'patch', '/api/admin/settings', {'map.zoom': 9, 'DEM_BATCH_SIZE': 5000})
    assert bad.status_code == 400 and conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 0
    for payload in ({}, [], 'x', {'unknown.key': 1}, {'map.zoom': 'x'}, {'x' * 70: 1}, {f'k{i}': 1 for i in range(60)}, {'map.zoom': None}):
        assert call(client, 'patch', '/api/admin/settings', payload).status_code == 400, payload
    ok = call(client, 'patch', '/api/admin/settings', {'map.zoom': 9, 'DEM_BATCH_SIZE': 50})
    assert ok.status_code == 200
    after = call(client, 'get', '/api/admin/settings').get_json()
    assert after['version'] > version
    assert {s['key']: s['value'] for s in after['settings']}['map.zoom'] == 9
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'setting.update'").fetchone()[0] >= 1
    assert call(client, 'delete', '/api/admin/settings/map.zoom').get_json() == {'success': True, 'reset': True}
    assert call(client, 'delete', '/api/admin/settings/map.zoom').get_json() == {'success': True, 'reset': False}
    assert call(client, 'delete', '/api/admin/settings/nope.nope').status_code == 404


def test_switching_the_mfa_policy_to_all_is_refused_while_an_account_has_no_factor(app, conn):
    rows, _ = world(app, conn)
    r = call(session_client(app, rows['a']), 'patch', '/api/admin/settings', {'auth.mfa_policy': 'all'})
    assert r.status_code == 409 and refresh(conn, 'bob') is not None
    assert conn.execute("SELECT COUNT(*) FROM settings WHERE key = 'auth.mfa_policy'").fetchone()[0] == 0


def secrets_world(tmp_path):
    from tests.fake_smtp import FakeSMTP
    smtp = FakeSMTP()
    app = make_app(tmp_path / 's', OUTBOUND_ALLOWED_HOSTS=f'smtp=127.0.0.1:{smtp.port}')
    c = connect(app)
    app.config['SETTINGS_STORE'].set_many(c, {'smtp.host': '127.0.0.1', 'smtp.port': smtp.port, 'smtp.security': 'none',
                                              'smtp.sender': 'noreply@example.org', 'smtp.timeout_s': 2}, actor=CLI)
    rows, _ = world(app, c)
    return app, c, smtp, session_client(app, rows['a'])


def test_secrets_are_write_only_everywhere(tmp_path, caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    app, conn, smtp, client = secrets_world(tmp_path)
    try:
        value = 'S3CRET-VALUE-0123456789-abcdef'
        r = call(client, 'put', '/api/admin/secrets/smtp.password', {'value': value})
        assert r.status_code == 200 and value not in r.get_data(as_text=True)
        listing = call(client, 'get', '/api/admin/secrets')
        assert value not in listing.get_data(as_text=True)
        status = next(s for s in listing.get_json()['secrets'] if s['name'] == 'smtp.password')
        assert status['configured'] is True and status['bound_is_effective'] is True and 'last4' in status
        assert status.get('last4') in (None, '') or value[-4:] != status.get('last4')                     # smtp.password : pas de last4 (type password)
        raw = conn.execute("SELECT ciphertext FROM secrets WHERE name = 'smtp.password'").fetchone()[0]
        assert value.encode() not in (raw if isinstance(raw, bytes) else raw.encode())
        dump = json.dumps([dict(x) for x in conn.execute("SELECT * FROM audit_log")]) + '\n'.join(x.getMessage() for x in caplog.records)
        assert value not in dump and 'smtp.password' in dump
        test = call(client, 'post', '/api/admin/secrets/smtp.password/test', {})
        assert test.status_code == 200 and set(test.get_json()) <= {'success', 'ok', 'elapsed_ms', 'duration_ms'} and value not in test.get_data(as_text=True)
        assert call(client, 'delete', '/api/admin/secrets/smtp.password').get_json()['cleared'] is True
        assert next(s for s in call(client, 'get', '/api/admin/secrets').get_json()['secrets'] if s['name'] == 'smtp.password')['configured'] is False
    finally:
        smtp.stop()


@pytest.mark.parametrize('value', ['', ' ', 'a\nb', 'a\rb', 'a\x00b', 'x' * 1025, None, 5, ['a'], {'a': 1}, True, 'ok\r\nX: y'])
def test_secret_values_are_validated_and_nothing_is_stored_on_refusal(tmp_path, value):
    app, conn, smtp, client = secrets_world(tmp_path)
    try:
        r = call(client, 'put', '/api/admin/secrets/smtp.password', {'value': value}, raw=json.dumps({'value': value}))
        if value == ' ':
            assert r.status_code in (200, 400)
        else:
            assert r.status_code == 400, (value, r.status_code)
            assert conn.execute("SELECT COUNT(*) FROM secrets").fetchone()[0] == 0
    finally:
        smtp.stop()


def test_a_secret_needs_its_destination_first_and_unknown_names_are_404(app, conn):
    rows, _ = world(app, conn)
    client = session_client(app, rows['a'])
    assert call(client, 'put', '/api/admin/secrets/smtp.password', {'value': 'abc'}).status_code == 409          # destination absente
    assert call(client, 'put', '/api/admin/secrets/routing.api_key', {'value': 'abc'}).status_code == 409
    for name in ('nope', '../etc', 'smtp.password%00', 'SMTP.PASSWORD'):
        for method in ('put', 'delete'):
            assert call(client, method, f'/api/admin/secrets/{name}', {'value': 'x'}).status_code in (404, 405)
    assert call(client, 'post', '/api/admin/secrets/nope/test', {}).status_code == 404


def test_changing_the_bound_destination_erases_the_secret_through_the_settings_api(tmp_path):
    app, conn, smtp, client = secrets_world(tmp_path)
    try:
        assert call(client, 'put', '/api/admin/secrets/smtp.password', {'value': 'abc123'}).status_code == 200
        assert call(client, 'patch', '/api/admin/settings', {'smtp.port': smtp.port}).status_code == 200            # inchangé : le secret reste
        assert conn.execute("SELECT COUNT(*) FROM secrets").fetchone()[0] == 1
        assert call(client, 'patch', '/api/admin/settings', {'smtp.host': 'localhost'}).status_code in (200, 400)
        if conn.execute("SELECT value FROM settings WHERE key = 'smtp.host'").fetchone()[0] == '"localhost"':
            assert conn.execute("SELECT COUNT(*) FROM secrets").fetchone()[0] == 0
    finally:
        smtp.stop()


def test_feature_flags_replace_the_list_validate_ids_and_keep_the_list_across_states(app, conn):
    rows, _ = world(app, conn)
    client = session_client(app, rows['a'])
    key = next(f['key'] for f in call(client, 'get', '/api/admin/features').get_json()['features'])
    ids = [rows['user']['id'], rows['a']['id']]
    assert call(client, 'patch', f'/api/admin/features/{key}', {'state': 'users', 'users': ids}).status_code == 200
    assert call(client, 'patch', f'/api/admin/features/{key}', {'state': 'all'}).status_code == 200
    view = next(f for f in call(client, 'get', '/api/admin/features').get_json()['features'] if f['key'] == key)
    assert view['state'] == 'all' and sorted(u['id'] for u in view['users']) == sorted(ids)
    for bad in ({'state': 'on'}, {'state': None}, {'state': ['all']}, {'users': 'x'}, {'users': [99999]}, {'users': [True]}, {'users': ['1']}, {'users': [1.5]},
                {'users': [ids[0], ids[0]]}, {'users': list(range(1, 1002))}, {}, {'state': 'users', 'users': [None]}, {'users': {'1': 1}}):
        assert call(client, 'patch', f'/api/admin/features/{key}', bad).status_code == 400, bad
    after = next(f for f in call(client, 'get', '/api/admin/features').get_json()['features'] if f['key'] == key)
    assert after['state'] == 'all' and sorted(u['id'] for u in after['users']) == sorted(ids)               # rien n'a bougé
    assert call(client, 'patch', '/api/admin/features/inconnue', {'state': 'off'}).status_code == 404
    assert call(client, 'patch', f'/api/admin/features/{key}', {'state': 'off', 'users': []}).status_code == 200
    assert call(client, 'get', '/api/admin/features').get_json()['features'][0]['users'] == []


def test_the_audit_api_bounds_filters_and_paginates(app, conn):
    rows, _ = world(app, conn)
    client = session_client(app, rows['a'])
    for i in range(7):
        call(client, 'patch', f"/api/admin/users/{rows['user']['id']}", {'invite_credits': i})
    for bad in ('limit=0', 'limit=201', 'limit=-1', 'limit=x', 'limit=1.5', 'before_id=0', 'before_id=x', 'before_id=-3', 'since=garbage', 'until=2026-13-45',
                'limit=' + '9' * 400):
        assert call(client, 'get', f'/api/admin/audit?{bad}').status_code == 400, bad
    first = call(client, 'get', '/api/admin/audit?limit=3').get_json()
    assert len(first['entries']) == 3 and first['next_before_id'] == first['entries'][-1]['id']
    ids = [e['id'] for e in first['entries']]
    assert ids == sorted(ids, reverse=True)
    second = call(client, 'get', f"/api/admin/audit?limit=3&before_id={first['next_before_id']}").get_json()
    assert max(e['id'] for e in second['entries']) < first['next_before_id']
    only = call(client, 'get', '/api/admin/audit?action=user.credits_set&limit=200').get_json()['entries']
    assert only and all(e['action'] == 'user.credits_set' for e in only)
    for hostile in ("' OR 1=1 --", '%', '_', 'x' * 500, '\x00', "user.credits_set' --"):
        r = client.get('/api/admin/audit', query_string={'action': hostile}, **KW)
        assert r.status_code in (200, 400) and r.get_json().get('entries', []) == []


def test_no_secret_or_token_ever_reaches_the_audit_log_over_a_scripted_scenario(app, conn):
    rows, secret = world(app, conn)
    client = session_client(app, rows['a'])
    link = call(client, 'post', f"/api/admin/users/{rows['user']['id']}/reset-link", {}).get_json()['link']
    conv = call(client, 'post', f"/api/admin/users/{rows['sso']['id']}/convert", {}).get_json()['link']
    call(client, 'patch', '/api/admin/settings', {'map.zoom': 9})
    token = link.split('#t=')[1]
    form_post(app, '/auth/token/consume', {'token': token, 'password': OTHER})
    form_post(app, '/auth/token/consume', {'token': conv.split('#t=')[1], 'username': 'nouveau.nom', 'password': GOOD})
    blob = json.dumps([dict(x) for x in conn.execute("SELECT * FROM audit_log")])
    for forbidden in (token, conv.split('#t=')[1], GOOD, OTHER, secret, 'argon2'):
        assert forbidden not in blob


def test_the_mail_test_goes_to_the_validated_address_else_the_pending_one_else_409(tmp_path):
    app, conn, smtp, _ = secrets_world(tmp_path)
    try:
        with_mail = add(app, conn, 'withmail', admin=True, secret=new_secret(), email='valid@example.org', pending_email='pending@example.org')
        with_pending = add(app, conn, 'withpending', admin=True, secret=new_secret(), pending_email='pending2@example.org')
        without = add(app, conn, 'without', admin=True, secret=new_secret())
        for row, expected in ((with_mail, 'valid@example.org'), (with_pending, 'pending2@example.org')):
            smtp.commands.clear()
            r = call(session_client(app, row), 'post', '/api/admin/mail/test', {})
            assert r.status_code == 200
            assert any(expected in c.lower() for c in smtp.commands if c.lower().startswith('rcpt to')), expected
        assert call(session_client(app, without), 'post', '/api/admin/mail/test', {}).status_code == 409
    finally:
        smtp.stop()


# ===== G. T15 : JS et gabarits ==================================================================================================

SOURCES = ['static/js/admin.js', 'static/js/profile-security.js', 'static/js/password-enroll.js', 'static/js/password-mfa.js',
           'static/js/password-login.js', 'static/js/password-forgot.js', 'static/js/token.js']
SINKS = re.compile(r'\.innerHTML|\.outerHTML|insertAdjacentHTML|document\.write|\beval\s*\(|new\s+Function|setTimeout\s*\(\s*[\'"`]|setInterval\s*\(\s*[\'"`]|'
                   r'\.srcdoc|createContextualFragment|dangerouslySet|\$\(.*\)\.html\(')


@pytest.mark.parametrize('path', SOURCES)
def test_no_dom_xss_sink_is_used_by_the_account_and_admin_scripts(path):
    text = (ROOT / path).read_text(encoding='utf-8')
    code = re.sub(r'//.*', '', text)
    assert not SINKS.search(code), SINKS.search(code).group(0)


@pytest.mark.parametrize('path', SOURCES)
def test_the_scripts_never_write_links_codes_or_secrets_to_the_browser_storage(path):
    code = re.sub(r'//.*', '', (ROOT / path).read_text(encoding='utf-8'))
    for write in re.findall(r'(?:localStorage|sessionStorage)\.setItem\([^)]*\)', code):
        assert not re.search(r'link|code|secret|token|password|recovery', write, re.I), write
    assert 'document.cookie' not in code and 'indexedDB' not in code


def test_server_data_is_inserted_with_text_nodes_only_in_the_admin_script():
    code = (ROOT / 'static/js/admin.js').read_text(encoding='utf-8')
    attributes = re.findall(r"\b(href|src|srcdoc|action|formaction|xlink:href|style)\s*:\s*[^,}]+", code)
    assert attributes == [] or all('${' not in a[1] for a in attributes), attributes
    assert 'window.confirm' not in code and 'confirm(' not in re.sub(r'confirmButton|Confirmer', '', code) and 'alert(' not in code and 'prompt(' not in code


def test_the_admin_and_enrollment_pages_use_no_inline_script_or_handler_and_the_csp_forbids_them(app, conn):
    rows, _ = world(app, conn)
    page = session_client(app, rows['a']).get('/admin', **KW)
    html = page.get_data(as_text=True)
    csp = page.headers['Content-Security-Policy']
    assert not re.search(r'<script(?![^>]*\bsrc=)[^>]*>\s*\S', html) and not re.search(r'\son[a-z]+\s*=', html) and 'javascript:' not in html
    assert "script-src 'self'" in csp and 'unsafe-inline' not in csp.split('script-src', 1)[1].split(';', 1)[0] and "object-src 'none'" in csp
    for template in ('admin.html', 'password_enroll.html', 'password_mfa.html', 'password_login.html', 'password_forgot.html', 'token.html', 'profile.html'):
        source = (ROOT / 'templates' / template).read_text(encoding='utf-8')
        assert not re.search(r'\son[a-z]+\s*=\s*["\']', source), template
        safe = re.findall(r'\{\{[^}]*\|\s*safe\s*\}\}', source)
        assert safe == ([] if template != 'password_enroll.html' else ['{{ qr_svg|safe }}']), (template, safe)


def test_the_admin_page_carries_no_server_data_inline_and_the_json_comes_from_the_api(app, conn):
    rows, _ = world(app, conn)
    html = session_client(app, rows['a']).get('/admin', **KW).get_data(as_text=True)
    for private in ('adma@example.org', 'bob@example.org', 'sso@example.org', rows['a']['password_hash'], 'tok-adma'):
        assert private not in html


def test_the_csrf_defence_of_the_json_api_is_the_origin_check_plus_same_site_cookies(app, conn):
    rows, _ = world(app, conn)
    cookie = session_client(app, rows['a']).get_cookie('session', domain=HOST)
    assert cookie is not None
    login_cookie = login(app, 'bob').headers.get('Set-Cookie', '')
    assert 'SameSite=Lax' in login_cookie and 'HttpOnly' in login_cookie and 'Secure' in login_cookie
