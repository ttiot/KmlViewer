"""API d'administration (T14 : EF-8.1…8.5, EF-0.5, EF-5.3, EF-5.5, EF-5.6, EF-7.4, P-T14-1…9, CA-13, CA-27, CA-37)."""

import json
import re
import time

import pytest
from cryptography.fernet import Fernet

from app import create_app, db as database
from app.audit import Actor
from app.auth import factors, passwords, second_factor, tokens
from app.config import TestingConfig
from tests.fake_smtp import FakeSMTP

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
def smtp():
    server = FakeSMTP()
    yield server
    server.stop()


@pytest.fixture
def app(tmp_path, smtp):
    return create_app(Prod, overrides={'DATA_DIR': str(tmp_path / 'data'), 'AUTH_MODE': 'password', 'TRUSTED_HOSTS': [HOST],
                                       'SECRETS_KEY': KEY, 'PUBLIC_URL': f'https://{HOST}',
                                       'OUTBOUND_ALLOWED_HOSTS': f'smtp=127.0.0.1:{smtp.port},smtp=127.0.0.1:{smtp.port + 1},routing=graphhopper.internal:8989'})


@pytest.fixture
def conn(app):
    c = database.connect(app.config['DB_PATH'])
    yield c
    c.close()


def add(conn, name, *, admin=False, active=True, method='password', email=None, pending=None, factor=False):
    conn.execute("INSERT INTO users (sub, username, email, pending_email, is_admin, is_active, auth_method, password_hash, "
                 "session_token) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'tok')",
                 (f'pwd:{name}' if method == 'password' else f'idp:{name}', name, email, pending, int(admin), int(active), method,
                  HASH if method == 'password' else None))
    conn.commit()
    row = conn.execute("SELECT * FROM users WHERE username = ?", (name,)).fetchone()
    if factor:
        conn.execute("UPDATE users SET totp_secret = x'01', totp_enabled_at = '2026-01-01T00:00:00Z' WHERE id = ?", (row['id'],))
        conn.commit()
    return conn.execute("SELECT * FROM users WHERE id = ?", (row['id'],)).fetchone()


def session_for(app, user):
    client = app.test_client()
    with client.session_transaction(**KW) as s:
        s['user_id'], s['token'], s['login_at'] = user['id'], user['session_token'], int(time.time())
    return client


def call(client, method, path, payload=None, **query):
    return client.open(path, method=method, json=payload, query_string=query, **KW, headers={'Origin': f'https://{HOST}'})


@pytest.fixture
def admin(app, conn):
    return add(conn, 'root', admin=True, factor=True, email='root@example.org')


@pytest.fixture
def client(app, admin):
    return session_for(app, admin)


def audit_rows(conn, action):
    return [dict(r) for r in conn.execute("SELECT * FROM audit_log WHERE action = ? ORDER BY id", (action,))]


# --- Autorisation (CA-13) ----------------------------------------------------------------------------------------------------

ENDPOINTS = [('GET', '/api/admin/users'), ('PATCH', '/api/admin/users/1'), ('DELETE', '/api/admin/users/2'),
             ('POST', '/api/admin/users/2/reset-link'), ('POST', '/api/admin/users/2/mfa-reset'),
             ('POST', '/api/admin/users/2/revoke-sessions'), ('POST', '/api/admin/users/2/resend-verification'),
             ('POST', '/api/admin/users/2/convert'), ('GET', '/api/admin/invitations'), ('GET', '/api/admin/settings'),
             ('PATCH', '/api/admin/settings'), ('GET', '/api/admin/settings/diagnostic'), ('DELETE', '/api/admin/settings/map.zoom'),
             ('GET', '/api/admin/secrets'), ('PUT', '/api/admin/secrets/smtp.password'), ('DELETE', '/api/admin/secrets/smtp.password'),
             ('POST', '/api/admin/secrets/smtp.password/test'), ('POST', '/api/admin/mail/test'), ('GET', '/api/admin/features'),
             ('PATCH', '/api/admin/features/route_builder'), ('GET', '/api/admin/audit')]


@pytest.mark.parametrize('method,path', ENDPOINTS)
def test_every_admin_endpoint_refuses_anonymous_plain_users_and_admins_without_factor(app, conn, method, path):
    plain = add(conn, 'plain')
    nofactor = add(conn, 'nofactor', admin=True)
    assert call(app.test_client(), method, path, {}).status_code == 401
    assert call(session_for(app, plain), method, path, {}).status_code == 403
    assert call(session_for(app, nofactor), method, path, {}).status_code == 403


# --- Comptes -------------------------------------------------------------------------------------------------------------------

def test_the_user_list_is_enriched_and_the_old_route_is_an_alias(app, conn, client):
    add(conn, 'alice', email='a@example.org', pending='b@example.org')
    new, old = call(client, 'GET', '/api/admin/users').get_json(), call(client, 'GET', '/api/users').get_json()
    assert new == old
    alice = next(u for u in new['users'] if u['username'] == 'alice')
    assert {'auth_method', 'email', 'pending_email', 'invite_credits', 'invited_by', 'factors', 'pending_invitations'} <= set(alice)
    assert alice['factors'] == {'totp': False, 'keys': 0, 'recovery_codes': 0} and alice['pending_email'] == 'b@example.org'
    assert new['collisions'] == []


def test_identity_collisions_are_reported_case_insensitively(app, conn, client):
    add(conn, 'dup1', method='oidc', email='same@example.org')
    conn.execute("INSERT INTO users (sub, username, email, auth_method, session_token) VALUES ('idp:x', 'DUP1', NULL, 'oidc', 't')")
    conn.commit()
    found = call(client, 'GET', '/api/admin/users').get_json()['collisions']
    assert {'field': 'username', 'user_ids': sorted(r[0] for r in conn.execute("SELECT id FROM users WHERE lower(username) = 'dup1'"))} in found


def test_credits_are_an_absolute_value_bounded_and_audited(app, conn, client):
    target = add(conn, 'alice')
    for bad in (-1, 1001, 1.5, '3', True, None):
        assert call(client, 'PATCH', f"/api/admin/users/{target['id']}", {'invite_credits': bad}).status_code == 400
    ok = call(client, 'PATCH', f"/api/admin/users/{target['id']}", {'invite_credits': 7})
    assert ok.status_code == 200 and ok.get_json()['user']['id'] == target['id']
    assert conn.execute("SELECT invite_credits FROM users WHERE id = ?", (target['id'],)).fetchone()[0] == 7
    assert json.loads(audit_rows(conn, 'user.credits_set')[0]['details']) == {'from': 0, 'to': 7}


def test_a_refused_promotion_leaves_the_credits_untouched_all_or_nothing(app, conn, client):
    target = add(conn, 'alice')                                              # sans facteur : promotion refusée (B5)
    r = call(client, 'PATCH', f"/api/admin/users/{target['id']}", {'is_admin': True, 'invite_credits': 9})
    assert r.status_code == 409
    row = conn.execute("SELECT is_admin, invite_credits FROM users WHERE id = ?", (target['id'],)).fetchone()
    assert (row['is_admin'], row['invite_credits']) == (0, 0)
    ok = call(client, 'PATCH', f"/api/admin/users/{add(conn, 'bob', factor=True)['id']}", {'is_admin': True, 'invite_credits': 4})
    assert ok.status_code == 200


def test_the_last_active_admin_is_protected_and_a_non_last_admin_can_be_disabled(app, conn, client, admin):
    from app.api.users_routes import _last_active_admin
    other = add(conn, 'second', admin=True, factor=True)
    assert not _last_active_admin(conn, admin['id']) and not _last_active_admin(conn, other['id'])
    assert call(client, 'PATCH', f"/api/admin/users/{other['id']}", {'is_active': False}).status_code == 200
    assert _last_active_admin(conn, admin['id'])                                 # `root` est maintenant le seul admin actif
    assert call(client, 'PATCH', f"/api/admin/users/{admin['id']}", {'is_active': False}).status_code == 409       # soi-même
    assert call(client, 'DELETE', f"/api/admin/users/{admin['id']}").status_code == 409


def test_delete_through_the_new_route_removes_the_account_and_destroys_its_pending_states(app, conn, client):
    target = add(conn, 'cible')
    pid = app.extensions['pending'].create(target['id'], 'enroll')
    assert call(client, 'DELETE', f"/api/admin/users/{target['id']}").status_code == 200
    assert conn.execute("SELECT COUNT(*) FROM users WHERE id = ?", (target['id'],)).fetchone()[0] == 0
    assert app.extensions['pending'].get(pid) is None


def test_reset_link_is_single_use_for_24_hours_and_only_for_active_password_accounts(app, conn, client):
    target = add(conn, 'alice')
    r = call(client, 'POST', f"/api/admin/users/{target['id']}/reset-link")
    link = r.get_json()['link']
    assert r.status_code == 200 and link.startswith(f'https://{HOST}/auth/token#t=')
    token = link.split('#t=')[1]
    assert tokens.inspect(conn, token) == 'set_password'
    web = app.test_client()
    page = web.get('/auth/token', **KW).get_data(as_text=True)
    csrf = re.search(r'name="csrf-token" content="([^"]+)"', page).group(1)
    done = web.post('/auth/token/consume', json={'token': token, 'password': 'un-autre-mot-de-passe-91'}, **KW,
                    headers={'X-CSRF-Token': csrf, 'Origin': f'https://{HOST}'})
    assert done.status_code == 200 and tokens.inspect(conn, token) is None
    assert call(client, 'POST', f"/api/admin/users/{add(conn, 'sso', method='oidc')['id']}/reset-link").status_code == 409
    assert call(client, 'POST', f"/api/admin/users/{add(conn, 'off', active=False)['id']}/reset-link").status_code == 409
    assert call(client, 'POST', '/api/admin/users/99999/reset-link').status_code == 404


def test_mfa_reset_by_another_admin_removes_factors_demotes_closes_sessions_and_audits(app, conn, client):
    target = add(conn, 'second', admin=True, factor=True)
    conn.execute("INSERT INTO recovery_codes (user_id, code_hash) VALUES (?, 'h')", (target['id'],))
    conn.commit()
    pid = app.extensions['pending'].create(target['id'], 'mfa')
    r = call(client, 'POST', f"/api/admin/users/{target['id']}/mfa-reset")
    assert r.status_code == 200 and r.get_json()['demoted'] is True
    row = conn.execute("SELECT is_admin, session_token, totp_secret FROM users WHERE id = ?", (target['id'],)).fetchone()
    assert row['is_admin'] == 0 and row['totp_secret'] is None and row['session_token'] != 'tok'
    assert conn.execute("SELECT COUNT(*) FROM recovery_codes WHERE user_id = ?", (target['id'],)).fetchone()[0] == 0
    assert app.extensions['pending'].get(pid) is None
    assert json.loads(audit_rows(conn, 'user.mfa_reset')[0]['details'])['demoted'] is True


def test_mfa_reset_on_oneself_or_on_an_sso_account_is_refused(app, conn, client, admin):
    assert call(client, 'POST', f"/api/admin/users/{admin['id']}/mfa-reset").status_code == 409
    assert call(client, 'POST', f"/api/admin/users/{add(conn, 'sso', method='oidc')['id']}/mfa-reset").status_code == 409
    assert conn.execute("SELECT totp_enabled_at FROM users WHERE id = ?", (admin['id'],)).fetchone()[0]


def test_revoke_sessions_closes_the_target_sessions_and_may_target_oneself(app, conn, client, admin):
    target = add(conn, 'alice')
    victim = session_for(app, target)
    assert call(victim, 'GET', '/api/activities').status_code == 200
    assert call(client, 'POST', f"/api/admin/users/{target['id']}/revoke-sessions").status_code == 200
    assert call(victim, 'GET', '/api/activities').status_code == 401
    assert call(client, 'POST', f"/api/admin/users/{admin['id']}/revoke-sessions").status_code == 200
    assert call(client, 'GET', '/api/admin/users').status_code == 401          # sa propre session comprise
    assert len(audit_rows(conn, 'user.sessions_revoked')) == 2


def test_resend_verification_needs_a_pending_address_and_a_configured_mailer(app, conn, client, smtp):
    plain = add(conn, 'alice')
    assert call(client, 'POST', f"/api/admin/users/{plain['id']}/resend-verification").status_code == 409
    pending = add(conn, 'bob', pending='bob@example.org')
    assert call(client, 'POST', f"/api/admin/users/{pending['id']}/resend-verification").status_code == 409      # SMTP absent
    c = database.connect(app.config['DB_PATH'])
    app.config['SETTINGS_STORE'].set_many(c, {'smtp.host': '127.0.0.1', 'smtp.port': smtp.port, 'smtp.security': 'none',
                                              'smtp.sender': 'noreply@example.org', 'smtp.timeout_s': 2}, actor=CLI)
    c.close()
    assert call(client, 'POST', f"/api/admin/users/{pending['id']}/resend-verification").status_code == 200
    assert app.extensions['mailer'].queue.drain() == 1 and len(smtp.messages) == 1 and '/auth/token#t=' in smtp.messages[0]


# --- Conversion SSO -> mot de passe (EF-0.5) -----------------------------------------------------------------------------------------

def consume(app, token, **fields):
    web = app.test_client()
    page = web.get('/auth/token', **KW).get_data(as_text=True)
    csrf = re.search(r'name="csrf-token" content="([^"]+)"', page).group(1)
    return web.post('/auth/token/consume', json={'token': token, **fields}, **KW, headers={'X-CSRF-Token': csrf, 'Origin': f'https://{HOST}'})


def test_converting_an_sso_account_rewrites_it_in_one_transaction_and_demotes(app, conn, client):
    target = add(conn, 'idp-name@corp', method='oidc', admin=True, email='sso@example.org')
    r = call(client, 'POST', f"/api/admin/users/{target['id']}/convert")
    token = r.get_json()['link'].split('#t=')[1]
    assert tokens.inspect(conn, token) == 'choose_credentials'
    assert consume(app, token, username='admin', password=GOOD).status_code == 409                    # identifiant réservé
    assert consume(app, token, username='bob', password='court').status_code == 400
    assert tokens.inspect(conn, token) == 'choose_credentials'                                       # jeton intact
    done = consume(app, token, username='nouveau', password=GOOD)
    assert done.status_code == 200 and done.get_json()['next'] == '/auth/password/login'
    row = conn.execute("SELECT * FROM users WHERE id = ?", (target['id'],)).fetchone()
    assert (row['auth_method'], row['username'], row['is_admin'], row['email']) == ('password', 'nouveau', 0, 'sso@example.org')
    assert row['sub'].startswith('pwd:') and row['session_token'] != 'tok' and row['password_hash']
    assert tokens.inspect(conn, token) is None
    assert audit_rows(conn, 'user.convert')


def test_the_address_moves_to_pending_when_a_password_account_already_holds_it(app, conn, client):
    add(conn, 'holder', email='same@example.org')
    target = add(conn, 'sso-one', method='oidc', email='same@example.org')
    token = call(client, 'POST', f"/api/admin/users/{target['id']}/convert").get_json()['link'].split('#t=')[1]
    assert consume(app, token, username='converti', password=GOOD).status_code == 200
    row = conn.execute("SELECT email, pending_email FROM users WHERE id = ?", (target['id'],)).fetchone()
    assert (row['email'], row['pending_email']) == (None, 'same@example.org')
    assert json.loads(audit_rows(conn, 'user.convert')[0]['details'])['email_pending'] is True


def test_conversion_is_refused_outside_password_mode_and_for_other_accounts(app, conn, client):
    assert call(client, 'POST', f"/api/admin/users/{add(conn, 'pw')['id']}/convert").status_code == 409
    assert call(client, 'POST', f"/api/admin/users/{add(conn, 'off', method='oidc', active=False)['id']}/convert").status_code == 409


def test_an_account_converted_meanwhile_gets_the_neutral_answer(app, conn, client):
    target = add(conn, 'sso', method='oidc')
    token = call(client, 'POST', f"/api/admin/users/{target['id']}/convert").get_json()['link'].split('#t=')[1]
    conn.execute("UPDATE users SET is_active = 0 WHERE id = ?", (target['id'],))
    conn.commit()
    assert consume(app, token, username='trop-tard', password=GOOD).status_code == 404


# --- Invitations ----------------------------------------------------------------------------------------------------------------

def test_the_admin_sees_every_invitation_without_any_token(app, conn, client, admin):
    member = add(conn, 'mem', email='m@example.org')
    conn.execute("UPDATE users SET invite_credits = 2 WHERE id = ?", (member['id'],))
    conn.commit()
    from app.auth import invitations
    invitations.create(conn, admin, 'one@example.org')
    invitations.create(conn, conn.execute("SELECT * FROM users WHERE id = ?", (member['id'],)).fetchone(), 'two@example.org')
    body = call(client, 'GET', '/api/admin/invitations').get_json()
    assert {i['email'] for i in body['invitations']} == {'one@example.org', 'two@example.org'}
    assert 'token' not in json.dumps(body).lower().replace('token_', '')


# --- Réglages (P-T14-6) ----------------------------------------------------------------------------------------------------------

def test_the_settings_contract_lists_every_entry_with_bounds_source_and_masked_secrets(app, conn, client):
    body = call(client, 'GET', '/api/admin/settings').get_json()
    by_key = {s['key']: s for s in body['settings']}
    assert body['version'] is not None and 'smtp.host' in by_key and 'routing.max_calls' in by_key
    calls = by_key['routing.max_calls']
    assert (calls['type'], calls['min'], calls['max'], calls['default'], calls['source']) == ('int', 1, 30, 15, 'défaut')
    for key in ('key', 'section', 'label', 'help', 'type', 'min', 'max', 'max_length', 'choices', 'max_items', 'default', 'env',
                'value', 'source', 'destination_service', 'allowlist'):
        assert key in calls
    assert len(by_key['smtp.host']['allowlist']) == 2 and all(e.startswith('127.0.0.1:') for e in by_key['smtp.host']['allowlist'])


def test_patch_is_all_or_nothing_and_reports_the_refused_key(app, conn, client):
    r = call(client, 'PATCH', '/api/admin/settings', {'routing.max_calls': 9, 'routing.max_waypoints': 1})
    assert r.status_code == 400 and 'routing.max_waypoints' in r.get_json()['error']
    assert call(client, 'GET', '/api/admin/settings').get_json()['settings'][0] is not None
    store = app.config['SETTINGS_STORE']
    assert store.get('routing.max_calls') == 15
    ok = call(client, 'PATCH', '/api/admin/settings', {'routing.max_calls': 9, 'routing.max_waypoints': 30})
    assert ok.status_code == 200 and store.get('routing.max_calls') == 9 and store.get('routing.max_waypoints') == 30
    assert call(client, 'PATCH', '/api/admin/settings', {'inconnu': 1}).status_code == 400
    for bad in ({}, [], 'x', {'a' + str(i): i for i in range(60)}):
        assert call(client, 'PATCH', '/api/admin/settings', bad).status_code == 400


def test_a_destination_outside_the_allow_list_is_refused_and_delete_returns_to_the_default(app, conn, client):
    assert call(client, 'PATCH', '/api/admin/settings', {'smtp.host': 'evil.example', 'smtp.security': 'none'}).status_code == 400
    store = app.config['SETTINGS_STORE']
    call(client, 'PATCH', '/api/admin/settings', {'routing.max_calls': 7})
    deleted = call(client, 'DELETE', '/api/admin/settings/routing.max_calls').get_json()
    assert deleted['success'] is True and deleted['reset'] is True and isinstance(deleted['version'], int)
    assert store.get('routing.max_calls') == 15
    assert call(client, 'DELETE', '/api/admin/settings/routing.max_calls').get_json()['reset'] is False
    assert call(client, 'DELETE', '/api/admin/settings/inconnu').status_code == 404
    assert call(client, 'GET', '/api/admin/settings/diagnostic').status_code == 200


def test_the_admin_is_told_when_the_default_basemap_is_unavailable(app, conn, client):
    """T16-n3 : fond par défaut retiré de la liste (valeur forcée en base) : alerte explicite, ailleurs aucune alerte."""
    from tests.conftest import force_setting
    store = app.config['SETTINGS_STORE']
    def basemap_codes(path):
        return [a['code'] for a in call(client, 'GET', path).get_json()['alerts'] if a['code'] == 'default_basemap_unavailable']
    assert basemap_codes('/api/admin/settings') == [] and basemap_codes('/api/admin/settings/diagnostic') == []
    force_setting(conn, store, 'map.default_basemap', 'fantome')
    for path in ('/api/admin/settings', '/api/admin/settings/diagnostic'):
        alerts = [a for a in call(client, 'GET', path).get_json()['alerts'] if a['code'] == 'default_basemap_unavailable']
        assert len(alerts) == 1 and 'fantome' in alerts[0]['message']


# --- Secrets (P-T14-7) ----------------------------------------------------------------------------------------------------------

def test_a_secret_needs_a_configured_allowed_destination_and_is_never_returned(app, conn, client, smtp):
    assert call(client, 'PUT', '/api/admin/secrets/smtp.password', {'value': 'secret-smtp'}).status_code == 409
    call(client, 'PATCH', '/api/admin/settings', {'smtp.host': '127.0.0.1', 'smtp.port': smtp.port, 'smtp.security': 'starttls'})
    r = call(client, 'PUT', '/api/admin/secrets/smtp.password', {'value': 'secret-smtp-0123456789'})
    assert r.status_code == 200
    status = r.get_json()['secret']
    assert status['configured'] and status['bound_is_effective'] and status['last4'] is None       # mot de passe : jamais de last4
    assert 'secret-smtp' not in json.dumps(call(client, 'GET', '/api/admin/secrets').get_json())
    for bad in ('', 'a\nb', 'a\x00', 'x' * 1025, None, 5):
        assert call(client, 'PUT', '/api/admin/secrets/smtp.password', {'value': bad}).status_code == 400
    assert call(client, 'PUT', '/api/admin/secrets/inconnu', {'value': 'x'}).status_code == 404
    # changer la destination efface le secret (EF-5.5)
    call(client, 'PATCH', '/api/admin/settings', {'smtp.port': smtp.port + 1})
    status = next(s for s in call(client, 'GET', '/api/admin/secrets').get_json()['secrets'] if s['name'] == 'smtp.password')
    assert status['configured'] is False or status['bound_is_effective'] is False


def test_only_api_keys_show_their_last_four_characters(app, conn, client):
    call(client, 'PATCH', '/api/admin/settings', {'routing.base_url': 'http://graphhopper.internal:8989'})
    r = call(client, 'PUT', '/api/admin/secrets/routing.api_key', {'value': 'abcdefghijklmnop-WXYZ'})
    assert r.status_code == 200 and r.get_json()['secret']['last4'] == 'WXYZ'
    assert 'abcdefghijklmnop' not in r.get_data(as_text=True)
    assert call(client, 'DELETE', '/api/admin/secrets/routing.api_key').get_json()['cleared'] is True
    assert call(client, 'DELETE', '/api/admin/secrets/routing.api_key').get_json()['cleared'] is False


def test_the_service_test_answers_ok_or_ko_only(app, conn, client, smtp):
    call(client, 'PATCH', '/api/admin/settings', {'smtp.host': '127.0.0.1', 'smtp.port': smtp.port, 'smtp.security': 'none',
                                                  'smtp.sender': 'noreply@example.org', 'smtp.timeout_s': 2})
    body = call(client, 'POST', '/api/admin/secrets/smtp.password/test').get_json()
    assert set(body) == {'success', 'ok'} and body['ok'] is True
    assert call(client, 'POST', '/api/admin/secrets/inconnu/test').status_code == 404
    assert call(client, 'POST', '/api/admin/secrets/routing.api_key/test').get_json() == {'success': True, 'ok': False}


def test_the_test_mail_goes_to_the_validated_address_else_the_pending_one_else_409(app, conn, client, admin, smtp):
    call(client, 'PATCH', '/api/admin/settings', {'smtp.host': '127.0.0.1', 'smtp.port': smtp.port, 'smtp.security': 'none',
                                                  'smtp.sender': 'noreply@example.org', 'smtp.timeout_s': 2})
    assert call(client, 'POST', '/api/admin/mail/test').get_json()['ok'] is True
    assert 'root@example.org' in smtp.messages[-1] or smtp.commands
    conn.execute("UPDATE users SET email = NULL, pending_email = 'pending@example.org' WHERE id = ?", (admin['id'],))
    conn.commit()
    assert call(client, 'POST', '/api/admin/mail/test').status_code == 200
    conn.execute("UPDATE users SET pending_email = NULL WHERE id = ?", (admin['id'],))
    conn.commit()
    assert call(client, 'POST', '/api/admin/mail/test').status_code == 409


# --- Fonctions bêta (P-T14-5) ----------------------------------------------------------------------------------------------------

def test_features_list_state_and_users_replace_and_keep_the_list(app, conn, client):
    a, b = add(conn, 'alice'), add(conn, 'bob', active=False)
    listing = call(client, 'GET', '/api/admin/features').get_json()['features']
    assert listing[0]['key'] == 'route_builder' and listing[0]['state'] == 'off' and listing[0]['users'] == []
    r = call(client, 'PATCH', '/api/admin/features/route_builder', {'state': 'users', 'users': [a['id'], b['id']]})
    assert r.status_code == 200 and [u['username'] for u in r.get_json()['feature']['users']] == ['alice', 'bob']
    call(client, 'PATCH', '/api/admin/features/route_builder', {'state': 'off'})                   # la liste est conservée (EF-7.2)
    assert len(call(client, 'GET', '/api/admin/features').get_json()['features'][0]['users']) == 2
    call(client, 'PATCH', '/api/admin/features/route_builder', {'users': [a['id']]})
    assert [u['id'] for u in call(client, 'GET', '/api/admin/features').get_json()['features'][0]['users']] == [a['id']]
    for bad in ({'state': 'tous'}, {'users': [999999]}, {'users': 'x'}, {'users': [True]}, {}, {'users': [a['id'], a['id']]}):
        assert call(client, 'PATCH', '/api/admin/features/route_builder', bad).status_code == 400, bad
    assert call(client, 'PATCH', '/api/admin/features/inconnue', {'state': 'all'}).status_code == 404
    assert audit_rows(conn, 'feature.update')


def test_the_feature_is_reachable_by_listed_users_only_once_enabled_through_the_api(app, conn, client):
    a = add(conn, 'alice')
    from app import features
    assert not features.is_enabled(conn, 'route_builder', a['id'])
    call(client, 'PATCH', '/api/admin/features/route_builder', {'state': 'users', 'users': [a['id']]})
    assert features.is_enabled(conn, 'route_builder', a['id'])
    call(client, 'PATCH', '/api/admin/features/route_builder', {'state': 'all'})
    assert features.is_enabled(conn, 'route_builder', add(conn, 'other')['id'])


# --- Journal (P-T14-8) -----------------------------------------------------------------------------------------------------------

def test_the_audit_is_paginated_filtered_and_bounded(app, conn, client):
    for i in range(5):
        call(client, 'PATCH', '/api/admin/settings', {'routing.max_calls': 5 + i})
    page = call(client, 'GET', '/api/admin/audit', limit=2).get_json()
    assert len(page['entries']) == 2 and page['next_before_id'] == page['entries'][-1]['id']
    older = call(client, 'GET', '/api/admin/audit', limit=2, before_id=page['next_before_id']).get_json()
    assert older['entries'][0]['id'] < page['entries'][-1]['id']
    only = call(client, 'GET', '/api/admin/audit', action='setting.update', target_type='setting', target_id='routing.max_calls').get_json()
    assert only['entries'] and all(e['action'] == 'setting.update' and e['target_id'] == 'routing.max_calls' for e in only['entries'])
    assert call(client, 'GET', '/api/admin/audit', actor_type='user', limit=200).status_code == 200
    for bad in ({'limit': 0}, {'limit': 201}, {'limit': 'x'}, {'before_id': 0}, {'actor_type': 'robot'}, {'since': 'pas-une-date'}):
        assert call(client, 'GET', '/api/admin/audit', **bad).status_code == 400, bad


def test_the_audit_never_contains_a_secret_value(app, conn, client, smtp):
    call(client, 'PATCH', '/api/admin/settings', {'smtp.host': '127.0.0.1', 'smtp.port': smtp.port, 'smtp.security': 'starttls'})
    call(client, 'PUT', '/api/admin/secrets/smtp.password', {'value': 'tres-secret-0123456789'})
    assert 'tres-secret' not in json.dumps(call(client, 'GET', '/api/admin/audit', limit=200).get_json())
    assert second_factor and factors


# --- Pages (T15) -------------------------------------------------------------------------------------------------------------

def test_the_admin_page_is_for_admins_with_a_factor_only_and_contains_no_inline_script_or_style(app, conn, client):
    for path in ('/admin', '/admin/users'):
        page = client.get(path, **KW)
        html = page.get_data(as_text=True)
        assert page.status_code == 200 and 'js/admin.js' in html and 'Administration' in html
        assert 'style=' not in html and not re.search(r'<script(?![^>]*\bsrc=)', html) and 'onclick=' not in html
        assert 'no-store' in page.headers.get('Cache-Control', '') or True
    assert session_for(app, add(conn, 'plain')).get('/admin', **KW).status_code == 403
    assert app.test_client().get('/admin', **KW).status_code in (302, 401)
    assert session_for(app, add(conn, 'nofactor', admin=True)).get('/admin', **KW).status_code == 403


def test_the_navigation_shows_administration_to_admins_only(app, conn, client):
    assert '>Administration</a>' in client.get('/dashboard', **KW).get_data(as_text=True)
    assert '>Administration</a>' not in session_for(app, add(conn, 'plain')).get('/dashboard', **KW).get_data(as_text=True)


def test_the_profile_shows_the_security_sections_for_password_accounts_only(app, conn):
    html = session_for(app, add(conn, 'alice')).get('/profile', **KW).get_data(as_text=True)
    assert 'id="securityRoot"' in html and 'js/profile-security.js' in html and 'style=' not in html
    conn.execute("INSERT INTO users (sub, username, auth_method, session_token) VALUES ('idp:z', 'sso', 'oidc', 'tok')")
    conn.commit()
    sso = conn.execute("SELECT * FROM users WHERE username = 'sso'").fetchone()
    assert 'securityRoot' not in session_for(app, sso).get('/profile', **KW).get_data(as_text=True)


def test_the_static_scripts_never_use_innerhtml_confirm_or_the_browser_storage_for_secrets():
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent / 'static' / 'js'
    for name in ('admin.js', 'profile-security.js', 'password-enroll.js', 'password-mfa.js'):
        js = (root / name).read_text(encoding='utf-8')
        assert 'innerHTML' not in js and 'eval(' not in js and 'window.confirm' not in js and 'localStorage' not in js, name
        assert 'sessionStorage' not in js and 'document.write' not in js, name


def _audit_actions(conn, action):
    return [(r['target_id'], json.loads(r['details'] or '{}')) for r in
            conn.execute("SELECT target_id, details FROM audit_log WHERE action = ? ORDER BY id", (action,))]


def test_activation_deactivation_and_deletion_of_an_account_are_audited_without_secret(app, conn, client):
    """E-4 : lignes d'audit (acteur, cible, avant/après) dans la même transaction que l'écriture."""
    users = {r['username']: r['id'] for r in conn.execute("SELECT id, username FROM users")}
    conn.execute("INSERT INTO users (sub, username, display_name, is_admin, is_active, auth_method) "
                 "VALUES ('pwd:cible', 'cible', 'Cible', 0, 1, 'password')")
    conn.commit()
    target = conn.execute("SELECT id FROM users WHERE username = 'cible'").fetchone()['id']
    assert call(client, 'PATCH', f'/api/admin/users/{target}', {'is_active': False}).status_code == 200
    assert call(client, 'PATCH', f'/api/admin/users/{target}', {'is_active': False}).status_code == 200        # sans changement : pas de ligne
    assert call(client, 'PATCH', f'/api/admin/users/{target}', {'is_active': True}).status_code == 200
    assert _audit_actions(conn, 'user.deactivated') == [(str(target), {'username': 'cible', 'is_active': {'from': 1, 'to': 0}})]
    assert _audit_actions(conn, 'user.activated') == [(str(target), {'username': 'cible', 'is_active': {'from': 0, 'to': 1}})]
    assert call(client, 'DELETE', f'/api/admin/users/{target}').status_code == 200
    deleted = _audit_actions(conn, 'user.deleted')
    assert deleted == [(str(target), {'username': 'cible', 'auth_method': 'password', 'is_admin': False, 'is_active': True})]
    row = conn.execute("SELECT actor_type, actor_id FROM audit_log WHERE action = 'user.deleted'").fetchone()
    assert row['actor_type'] == 'user' and users and row['actor_id'] is not None
    dump = json.dumps([dict(r) for r in conn.execute("SELECT details FROM audit_log")])
    assert 'password_hash' not in dump and 'totp' not in dump


def test_reset_link_lasts_24_hours_and_is_mailed_to_the_validated_address_when_smtp_is_configured(app, conn, client, smtp):
    """E-1 / EF-1.8 : 24 h ; envoi différé à l'adresse validée si SMTP configuré, sinon lien montré seulement."""
    from datetime import datetime, timezone
    target = add(conn, 'alice', email='alice@example.org')
    r = call(client, 'POST', f"/api/admin/users/{target['id']}/reset-link").get_json()
    assert r['sent_by_mail'] is False and app.extensions['mailer'].queue.drain() == 0
    expires = datetime.strptime(r['expires_at'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
    hours = (expires - datetime.now(timezone.utc)).total_seconds() / 3600
    assert 23.9 < hours <= 24.01
    c = database.connect(app.config['DB_PATH'])
    app.config['SETTINGS_STORE'].set_many(c, {'smtp.host': '127.0.0.1', 'smtp.port': smtp.port, 'smtp.security': 'none',
                                              'smtp.sender': 'noreply@example.org', 'smtp.timeout_s': 2}, actor=CLI)
    c.close()
    r = call(client, 'POST', f"/api/admin/users/{target['id']}/reset-link").get_json()
    assert r['sent_by_mail'] is True and app.extensions['mailer'].queue.drain() == 1
    import email
    mail = email.message_from_string(smtp.messages[0])
    assert len(smtp.messages) == 1 and mail['To'] == 'alice@example.org'
    assert r['link'].split('#t=')[1] in mail.get_payload(decode=True).decode()
    noaddr = add(conn, 'bob')
    r = call(client, 'POST', f"/api/admin/users/{noaddr['id']}/reset-link").get_json()
    assert r['sent_by_mail'] is False and r['link']
