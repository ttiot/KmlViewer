import pytest

from app import create_app
from app.auth import oidc, users
from app.auth.guard import safe_next
from app.config import TestingConfig
from tests.conftest import OIDC_OVERRIDES, login_as


def test_everything_requires_login(oidc_app):
    client = oidc_app.test_client()
    protected = [r for r in oidc_app.url_map.iter_rules()
                 if r.endpoint not in ('static', 'api.health_check') and not r.endpoint.startswith('auth.')]
    assert len(protected) > 30
    for rule in protected:
        path = rule.rule.replace('<int:', '<').replace('<filename>', 'x.gpx')
        for arg in rule.arguments:
            path = path.replace(f'<{arg}>', '1')
        for method in rule.methods - {'HEAD', 'OPTIONS'}:
            response = client.open(path, method=method)
            expected = 401 if path.startswith('/api/') else 302
            assert response.status_code == expected, (method, path)
    assert client.get('/api/health').status_code == 200
    assert '/auth/login' in client.get('/dashboard').headers['Location']


def test_no_account_becomes_admin_by_logging_in_first(db):
    first = users.upsert_from_claims(db, {'sub': 'a', 'email': 'a@x.io', 'email_verified': True, 'name': 'A'})
    second = users.upsert_from_claims(db, {'sub': 'b', 'preferred_username': 'bee'})
    assert not first['is_admin'] and not second['is_admin'] and second['is_active']
    assert second['username'] == 'bee' and first['username'] == 'a@x.io'
    # Les claims ne peuvent pas retirer un admin géré dans l'appli
    db.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (first['id'],))
    db.commit()
    assert users.upsert_from_claims(db, {'sub': 'a', 'roles': []})['is_admin']


def test_admin_role_follows_idp_and_pending_activation(db):
    kwargs = {'admin_role': 'admin', 'auto_activate': False}
    plain = users.upsert_from_claims(db, {'sub': 'a'}, **kwargs)
    boss = users.upsert_from_claims(db, {'sub': 'b', 'roles': ['user', 'admin']}, **kwargs)
    assert not plain['is_admin'] and not plain['is_active']
    assert boss['is_admin'] and boss['is_active']
    assert not users.upsert_from_claims(db, {'sub': 'b', 'roles': ['user']}, **kwargs)['is_admin']


def test_sub_is_mandatory_and_local_is_reserved(db):
    for claims in ({}, {'sub': ''}, {'sub': 'local'}):
        with pytest.raises(ValueError):
            users.upsert_from_claims(db, claims)


def test_safe_next(app):
    with app.test_request_context():
        assert safe_next('/dashboard?x=1') == '/dashboard?x=1'
        for evil in ('https://evil.io', '//evil.io', '/\\evil.io', 'javascript:alert(1)', ''):
            assert safe_next(evil) == '/'


def test_callback_creates_session(oidc_app, monkeypatch):
    token = {'userinfo': {'sub': 'fa-123', 'email': 'j@x.io', 'preferred_username': 'jm'}}
    monkeypatch.setattr(oidc.oauth.oidc, 'authorize_access_token', lambda: token)
    client = oidc_app.test_client()
    with client.session_transaction() as session:
        session['next'] = '/dashboard'
    response = client.get('/auth/callback?code=x&state=y')
    assert response.status_code == 302 and response.headers['Location'] == '/dashboard'
    me = client.get('/api/me').get_json()
    assert me['user']['username'] == 'jm' and not me['user']['is_admin']
    cookie = response.headers['Set-Cookie']
    assert 'HttpOnly' in cookie and 'SameSite=Lax' in cookie


def test_callback_failure_gives_no_session_and_no_detail(oidc_app, monkeypatch):
    def boom():
        raise RuntimeError('secret-interne')
    monkeypatch.setattr(oidc.oauth.oidc, 'authorize_access_token', boom)
    client = oidc_app.test_client()
    response = client.get('/auth/callback?code=x&state=y')
    assert response.status_code == 401 and b'secret-interne' not in response.data
    assert client.get('/api/me').status_code == 401


def test_inactive_user_is_blocked(oidc_app):
    admin, _ = login_as(oidc_app, 'admin', admin=True)
    bob, bob_id = login_as(oidc_app, 'bob')
    assert admin.patch(f'/api/users/{bob_id}', json={'is_active': False}).status_code == 200
    # La désactivation ferme les sessions ouvertes ; une nouvelle connexion tombe sur le refus
    assert bob.get('/api/activities').status_code == 401
    bob, _ = login_as(oidc_app, 'bob')
    assert bob.get('/api/activities').status_code == 403
    assert bob.get('/dashboard').status_code == 403


def test_user_admin_api(oidc_app):
    admin, admin_id = login_as(oidc_app, 'admin', admin=True)
    bob, bob_id = login_as(oidc_app, 'bob')
    assert bob.get('/api/users').status_code == 403
    assert bob.patch(f'/api/users/{bob_id}', json={'is_admin': True}).status_code == 403
    assert bob.get('/admin/users').status_code == 403
    assert len(admin.get('/api/users').get_json()['users']) == 2
    assert admin.patch(f'/api/users/{admin_id}', json={'is_admin': False}).status_code == 409
    assert admin.delete(f'/api/users/{admin_id}').status_code == 409
    assert admin.patch(f'/api/users/{bob_id}', json={'is_admin': 'yes'}).status_code == 400
    assert admin.patch(f'/api/users/{bob_id}', json={'sub': 'x'}).status_code == 400
    assert admin.delete(f'/api/users/{bob_id}').status_code == 200
    assert bob.get('/api/activities').status_code == 401  # session orpheline invalidée


def test_cross_origin_writes_are_refused(client):
    assert client.post('/api/segments', json={}, headers={'Origin': 'https://evil.io'}).status_code == 403
    assert client.post('/api/segments', json={}, headers={'Sec-Fetch-Site': 'cross-site'}).status_code == 403
    assert client.post('/api/segments', json={}, headers={'Origin': 'http://localhost'}).status_code == 400
    assert client.get('/api/segments', headers={'Origin': 'https://evil.io'}).status_code == 200


def test_security_headers(client):
    headers = client.get('/').headers
    assert headers['X-Content-Type-Options'] == 'nosniff' and headers['X-Frame-Options'] == 'DENY'
    assert "frame-ancestors 'none'" in headers['Content-Security-Policy']
    assert client.get('/api/activities').headers['Cache-Control'] == 'no-store'


def test_oidc_refuses_default_secret_key(tmp_path):
    class Prod(TestingConfig):
        TESTING = False
    with pytest.raises(RuntimeError, match='SECRET_KEY'):
        create_app(Prod, overrides={'DATA_DIR': str(tmp_path), 'INBOX_ENABLED': False, **OIDC_OVERRIDES})
    with pytest.raises(RuntimeError, match='incomplète'):
        create_app(Prod, overrides={'DATA_DIR': str(tmp_path), 'OIDC_ISSUER': 'https://idp.test'})


def _login_with(oidc_app, monkeypatch, token, userinfo, access_claims=None):
    oidc_app.config['OIDC_ADMIN_ROLE'] = 'admin'
    monkeypatch.setattr(oidc.oauth.oidc, 'authorize_access_token', lambda: token)
    monkeypatch.setattr(oidc.oauth.oidc, 'userinfo', lambda token: userinfo)
    monkeypatch.setattr(oidc, '_verified_access_token_claims', lambda token, sub: access_claims or {})
    client = oidc_app.test_client()
    client.get('/auth/callback?code=x&state=y')
    return client.get('/api/me').get_json()['user']


def test_roles_can_come_from_userinfo(oidc_app, monkeypatch):
    token = {'userinfo': {'sub': 'u1', 'email': 'a@x.io'}}
    assert _login_with(oidc_app, monkeypatch, token, {'sub': 'u1', 'roles': ['admin']})['is_admin']


def test_userinfo_for_another_subject_is_ignored(oidc_app, monkeypatch):
    token = {'userinfo': {'sub': 'u1', 'email': 'a@x.io'}}
    assert not _login_with(oidc_app, monkeypatch, token, {'sub': 'intrus', 'roles': ['admin']})['is_admin']


def test_roles_can_come_from_verified_access_token(oidc_app, monkeypatch):
    token = {'userinfo': {'sub': 'u1', 'email': 'a@x.io'}}
    assert _login_with(oidc_app, monkeypatch, token, {'sub': 'u1'}, {'roles': ['admin']})['is_admin']


def test_unsigned_access_token_gives_no_claims(oidc_app, monkeypatch):
    import base64
    import json
    b64 = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b'=').decode()  # noqa: E731
    forged = f"{b64({'alg': 'none'})}.{b64({'sub': 'u1', 'aud': 'kmlviewer', 'roles': ['admin'], 'exp': 9999999999})}."
    monkeypatch.setattr(oidc.oauth.oidc, 'fetch_jwk_set', lambda force=False: {'keys': []})
    with oidc_app.test_request_context():
        assert oidc._verified_access_token_claims({'access_token': forged}, 'u1') == {}
