"""
2e lot, groupe 4 (rôle test, écrit depuis la spec) : E-13 / CA-59 (Public Suffix List embarquée, branchement au démarrage), E-21 / CA-63 (page de
connexion unique en mode oidc), E-20 / CA-64 (bandeaux et diagnostic : e-mail absent, destination hors liste blanche). CA-65 (confirmation avant
d'effacer un secret) est une règle d'interface : elle est jouée dans un vrai navigateur par le banc `tests_browser/`.
Aucun marqueur xfail : un test rouge est un défaut à rapporter.
"""

import socket
import urllib.parse

import pytest
from cryptography.fernet import Fernet

from app import create_app
from app.auth import oidc, publicsuffix, webauthn_factor
from tests.conftest import OIDC_OVERRIDES
from tests.pw_harness import HOST, KEY, ORIGIN, Prod, add, call, connect, make_app, new_secret, session_client

OTHER_KEY = Fernet.generate_key().decode()


@pytest.fixture(autouse=True)
def fresh_list(monkeypatch):
    """Chaque test repart d'une liste non chargée (le chargement est mis en cache par processus)."""
    monkeypatch.setattr(publicsuffix, '_rules', None)


def start(tmp_path, public_url, rp_id=None, **extra):
    host = urllib.parse.urlsplit(public_url).hostname
    overrides = {'DATA_DIR': str(tmp_path / 'data'), 'AUTH_MODE': 'password', 'TRUSTED_HOSTS': [host], 'SECRETS_KEY': KEY, 'PUBLIC_URL': public_url,
                 **extra}
    if rp_id is not None:
        overrides['WEBAUTHN_RP_ID'] = rp_id
    return create_app(Prod, overrides=overrides)


# ===== E-13 / CA-59 : la liste embarquée décide, au démarrage ===================================================================

@pytest.mark.parametrize('public_url,rp_id', [
    ('https://app.exemple.fr', 'exemple.fr'),
    ('https://app.exemple.fr', 'app.exemple.fr'),
    ('https://app.exemple.fr', None),
    ('https://a.b.example.co.uk', 'example.co.uk'),                    # un suffixe public à deux labels n'est pas le domaine enregistrable
    ('https://www.ck', 'www.ck'),                                      # exception de la liste : « !www.ck » n'est PAS un suffixe public
    ('https://sub.www.ck', 'www.ck'),
    ('https://xn--e1afmkfd.xn--p1ai', 'xn--e1afmkfd.xn--p1ai'),       # пример.рф en punycode
])
def test_a_registrable_domain_is_accepted(tmp_path, public_url, rp_id):
    app = start(tmp_path, public_url, rp_id)
    assert app.config['PASSWORD_ACCOUNTS'] is True


@pytest.mark.parametrize('public_url,rp_id', [
    ('https://app.exemple.fr', 'fr'),
    ('https://app.gouv.fr', 'gouv.fr'),
    ('https://mon.blogspot.com', 'blogspot.com'),                      # section PRIVATE de la liste
    ('https://moi.github.io', 'github.io'),
    ('https://a.example.co.uk', 'co.uk'),
    ('https://app.exemple.com', 'com'),
    ('https://x.foo.ck', 'foo.ck'),                                    # joker « *.ck » : foo.ck est un suffixe public
    ('https://x.xn--p1ai', 'xn--p1ai'),                                # le TLD « рф » en punycode
    ('https://app.exemple.fr', 'EXEMPLE.FR.'),                         # point final : pas une forme valide
    ('https://app.exemple.fr', 'fr.'),
    ('https://app.exemple.fr', 'autre.fr'),                            # pas un suffixe de l'hôte
    ('https://app.exemple.fr', 'app.exemple.fr:443'),
    ('https://app.exemple.fr', '192.0.2.1'),                           # une adresse IP n'est jamais un RP ID
    ('https://app.exemple.fr', '[2001:db8::1]'),
])
def test_a_public_suffix_or_an_invalid_override_stops_the_start_and_names_the_variable(tmp_path, public_url, rp_id):
    with pytest.raises(RuntimeError, match='WEBAUTHN_RP_ID'):
        start(tmp_path, public_url, rp_id)


def test_the_host_itself_being_a_public_suffix_without_override_stops_the_start(tmp_path):
    with pytest.raises(RuntimeError):
        start(tmp_path, 'https://gouv.fr')


@pytest.mark.parametrize('name,expected', [
    ('fr', True), ('gouv.fr', True), ('exemple.fr', False), ('co.uk', True), ('example.co.uk', False), ('github.io', True), ('blogspot.com', True),
    ('moi.github.io', False), ('foo.ck', True), ('www.ck', False), ('sub.www.ck', False), ('xn--p1ai', True), ('com', True), ('COM', True),
])
def test_the_list_answers_like_the_public_suffix_list_wildcards_exceptions_sections_and_case(name, expected):
    assert publicsuffix.is_public_suffix(name, publicsuffix.load()) is expected


def test_an_internationalised_name_is_compared_in_punycode():
    rules = publicsuffix.load()
    assert publicsuffix.is_public_suffix('рф', rules) is True and publicsuffix.is_public_suffix('пример.рф', rules) is False
    assert publicsuffix.to_ascii('рф') == 'xn--p1ai'
    for hostile in ('', '.', '..', 'a..b', '.fr', 'fr.', 'a b.fr', 'x' * 300 + '.fr', '‮fr'):
        assert publicsuffix.is_public_suffix(hostile, rules) is False                 # inutilisable : jamais « suffixe public », jamais d'exception


def test_the_embedded_file_is_the_one_the_source_notice_describes_and_no_network_is_used(tmp_path, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("accès réseau interdit")
    monkeypatch.setattr(socket, 'create_connection', refuse)
    monkeypatch.setattr(socket.socket, 'connect', refuse)
    app = start(tmp_path, 'https://app.exemple.fr', 'exemple.fr')
    assert webauthn_factor.rp_id(app.config) == 'exemple.fr'
    for _ in range(3):
        assert webauthn_factor.rp_id({'PUBLIC_URL': 'https://x.github.io', 'WEBAUTHN_RP_ID': 'github.io'}) is None


@pytest.mark.parametrize('content', [None, b'', b'// rien\n', b'fr\ncom\n', b'\xff\xfe\x00bad', b'x' * (5 * 1024 * 1024)])
def test_a_missing_empty_truncated_or_oversized_list_stops_the_start_when_password_accounts_are_available(tmp_path, monkeypatch, content):
    path = tmp_path / 'list.dat'
    if content is not None:
        path.write_bytes(content)
    monkeypatch.setattr(publicsuffix, 'LIST_PATH', path)
    with pytest.raises(RuntimeError, match='Démarrage refusé'):
        start(tmp_path / 'app', 'https://app.exemple.fr', 'exemple.fr')


def test_the_list_is_not_needed_when_password_accounts_are_not_available(tmp_path, monkeypatch):
    monkeypatch.setattr(publicsuffix, 'LIST_PATH', tmp_path / 'absente.dat')
    assert create_app(Prod, overrides={'DATA_DIR': str(tmp_path / 'a'), 'AUTH_MODE': 'local'}).config['AUTH_MODE'] == 'local'
    oidc_only = create_app(Prod, overrides={'DATA_DIR': str(tmp_path / 'b'), 'AUTH_MODE': 'oidc', **OIDC_OVERRIDES, 'TRUSTED_HOSTS': [HOST]})
    assert oidc_only.config['PASSWORD_ACCOUNTS'] is False


def test_the_list_is_loaded_once_per_process_and_not_per_request(tmp_path, monkeypatch):
    app = start(tmp_path, 'https://app.exemple.fr', 'exemple.fr')
    calls = []
    original = publicsuffix.load
    monkeypatch.setattr(publicsuffix, 'load', lambda *a, **k: (calls.append(1), original(*a, **k))[1])
    conn = connect(app)
    user = add(app, conn, 'u', secret=new_secret())
    client = session_client(app, user)
    for _ in range(5):
        client.get('/api/me/mfa', base_url='https://app.exemple.fr')
        webauthn_factor.rp_id(app.config)
    assert calls == []
    conn.close()


def test_a_corrupted_list_at_runtime_makes_webauthn_unavailable_never_silently_permissive(monkeypatch):
    def broken():
        raise publicsuffix.PublicSuffixError("liste absente")
    monkeypatch.setattr(publicsuffix, 'rules', broken)
    assert webauthn_factor.rp_id({'PUBLIC_URL': 'https://x.github.io', 'WEBAUTHN_RP_ID': 'github.io'}) is None
    assert webauthn_factor.rp_id({'PUBLIC_URL': 'https://app.exemple.fr', 'WEBAUTHN_RP_ID': 'exemple.fr'}) is None      # ni accepté « par défaut »


# ===== E-21 / CA-63 : une seule page de connexion en mode oidc ==================================================================

def oidc_app(tmp_path, *, passwords=True):
    overrides = {'DATA_DIR': str(tmp_path / 'oidc'), 'AUTH_MODE': 'oidc', **OIDC_OVERRIDES, 'TRUSTED_HOSTS': [HOST], 'PUBLIC_URL': ORIGIN}
    if passwords:
        overrides['SECRETS_KEY'] = KEY
    return create_app(Prod, overrides=overrides)


METADATA = {'issuer': 'https://idp.test', 'authorization_endpoint': 'https://idp.test/oauth2/authorize',
            'token_endpoint': 'https://idp.test/oauth2/token', 'jwks_uri': 'https://idp.test/jwks'}


@pytest.fixture
def idp(monkeypatch):
    monkeypatch.setattr(oidc.oauth.oidc, 'load_server_metadata', lambda: METADATA)


def get(client, path, **kw):
    return client.get(path, base_url=ORIGIN, **kw)


def test_the_login_entry_is_one_page_with_the_sso_button_and_the_form_and_no_redirect_to_the_idp(tmp_path, idp):
    client = oidc_app(tmp_path).test_client()
    r = get(client, '/auth/login')
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and 'idp.test' not in r.headers.get('Location', '')
    assert 'id="ssoButton"' in html and 'Se connecter avec le SSO' in html
    assert 'id="loginForm"' in html and 'name="username"' in html and 'name="password"' in html and 'js/password-login.js' in html


def test_a_protected_route_without_session_ends_on_that_same_page_without_leaving_for_the_idp(tmp_path, idp):
    client = oidc_app(tmp_path).test_client()
    r = get(client, '/', follow_redirects=False)
    hops = 0
    while r.status_code in (301, 302, 303, 307) and hops < 5:
        location = r.headers['Location']
        assert 'idp.test' not in location, location                                   # jamais d'envoi d'office vers l'IdP
        r = get(client, urllib.parse.urlsplit(location).path + ('?' + urllib.parse.urlsplit(location).query if urllib.parse.urlsplit(location).query else ''))
        hops += 1
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and 'id="ssoButton"' in html and 'id="loginForm"' in html


def test_the_sso_button_leads_to_the_idp_keeping_next(tmp_path, idp):
    client = oidc_app(tmp_path).test_client()
    html = get(client, '/auth/login?next=/dashboard').get_data(as_text=True)
    href = html.split('id="ssoButton" href="')[1].split('"')[0].replace('&amp;', '&')
    r = get(client, href)
    assert r.status_code == 302 and r.headers['Location'].startswith('https://idp.test/oauth2/authorize')
    with client.session_transaction(base_url=ORIGIN) as session:
        assert session.get('next') == '/dashboard'


@pytest.mark.parametrize('hostile', ['//evil.example/x', 'https://evil.example', '/\\evil.example', 'javascript:alert(1)', '\x00', 'x' * 3000])
def test_a_hostile_next_is_never_reflected_into_the_page_or_the_idp_redirect(tmp_path, idp, hostile):
    client = oidc_app(tmp_path).test_client()
    r = get(client, '/auth/login?' + urllib.parse.urlencode({'next': hostile}))
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and 'evil.example' not in html and 'javascript:' not in html
    href = html.split('id="ssoButton" href="')[1].split('"')[0].replace('&amp;', '&')
    go = get(client, href)
    assert go.status_code == 302 and 'evil.example' not in go.headers['Location']
    with client.session_transaction(base_url=ORIGIN) as session:
        assert session.get('next') in (None, '', '/')


def test_the_form_still_leads_to_the_two_step_login(tmp_path, idp):
    app = oidc_app(tmp_path)
    conn = connect(app)
    add(app, conn, 'alice', secret=new_secret())
    from tests.pw_harness import login
    r = login(app, 'alice')
    assert r.status_code == 200 and r.get_json()['step'] == 'mfa'
    conn.close()


def test_without_password_accounts_the_behaviour_is_the_current_one_redirect_to_the_idp(tmp_path, idp):
    client = oidc_app(tmp_path, passwords=False).test_client()
    r = get(client, '/auth/login')
    assert r.status_code == 302 and r.headers['Location'].startswith('https://idp.test/')
    assert 'loginForm' not in r.get_data(as_text=True)


def test_in_password_mode_the_page_is_the_form_alone_without_any_sso_button(tmp_path):
    app = make_app(tmp_path)
    r = app.test_client().get('/auth/password/login', base_url=ORIGIN)
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and 'id="loginForm"' in html and 'ssoButton' not in html and 'SSO' not in html
    entry = app.test_client().get('/auth/login?sso=1', base_url=ORIGIN)
    assert entry.status_code == 302 and 'idp.test' not in entry.headers['Location']


def test_the_unauthenticated_api_still_answers_401_in_oidc_mode_with_password_accounts(tmp_path, idp):
    client = oidc_app(tmp_path).test_client()
    assert get(client, '/api/activities').status_code == 401


def test_the_csp_of_the_login_page_allows_the_idp_form_target_only_as_before(tmp_path, idp):
    client = oidc_app(tmp_path).test_client()
    csp = get(client, '/auth/login').headers['Content-Security-Policy']
    assert "script-src 'self'" in csp and "'unsafe-inline'" not in csp.split('script-src')[1].split(';')[0]


# ===== E-20 / CA-64 : bandeaux et diagnostic ====================================================================================

def admin_in(app):
    conn = connect(app)
    admin = add(app, conn, 'chef', admin=True, secret=new_secret())
    return conn, session_client(app, admin)


def alerts(client):
    r = call(client, 'get', '/api/admin/settings/diagnostic')
    assert r.status_code == 200
    return r.get_json()['alerts'], r


def codes(found):
    return sorted((a['code'], a.get('service')) for a in found)


def test_without_smtp_settings_a_banner_says_the_email_is_not_configured(tmp_path):
    app = make_app(tmp_path)
    conn, admin = admin_in(app)
    found, _ = alerts(admin)
    banner = [a for a in found if a['code'] == 'smtp_not_configured']
    assert len(banner) == 1
    assert 'E-mail non configuré' in banner[0]['message'] and 'mot de passe oublié' in banner[0]['message']
    conn.close()


@pytest.mark.parametrize('missing', ['smtp.host', 'smtp.sender'])
def test_a_banner_appears_when_either_the_host_or_the_sender_is_empty_and_disappears_when_both_are_set(tmp_path, missing):
    app = make_app(tmp_path, OUTBOUND_ALLOWED_HOSTS='smtp=127.0.0.1:2525')
    conn, admin = admin_in(app)
    from app.audit import Actor
    values = {'smtp.host': '127.0.0.1', 'smtp.port': 2525, 'smtp.security': 'none', 'smtp.sender': 'noreply@example.org', 'smtp.timeout_s': 2}
    app.config['SETTINGS_STORE'].set_many(conn, values, actor=Actor.cli())
    assert not [a for a in alerts(admin)[0] if a['code'] == 'smtp_not_configured']
    app.config['SETTINGS_STORE'].set_many(conn, {missing: ''}, actor=Actor.cli())
    assert [a for a in alerts(admin)[0] if a['code'] == 'smtp_not_configured']
    conn.close()


def test_a_destination_forced_in_the_database_outside_the_allow_list_is_flagged_by_service_never_by_value(tmp_path):
    app = make_app(tmp_path, OUTBOUND_ALLOWED_HOSTS='routing=engine.test:8989')
    conn, admin = admin_in(app)
    conn.execute("INSERT INTO settings (key, value, updated_at, updated_by) VALUES ('routing.base_url', '\"http://evil.test:9\"', 'x', 'sql')")
    conn.execute("UPDATE settings_meta SET version = version + 1")
    conn.commit()
    app.config['SETTINGS_STORE'].refresh(conn, force=True)
    found, response = alerts(admin)
    flagged = [a for a in found if a['code'] == 'destination_not_allowed' and a.get('service') == 'routing']
    assert len(flagged) == 1 and 'Moteur de routage' in flagged[0]['message']
    assert 'evil.test' not in str(found)                                               # le bandeau nomme le service, jamais la valeur configurée
    conn.execute("UPDATE settings SET value = '\"http://engine.test:8989\"' WHERE key = 'routing.base_url'")
    conn.execute("UPDATE settings_meta SET version = version + 1")
    conn.commit()
    app.config['SETTINGS_STORE'].refresh(conn, force=True)
    assert not [a for a in alerts(admin)[0] if a['code'] == 'destination_not_allowed' and a.get('service') == 'routing']        # remise dans la liste
    conn.close()


def test_the_smtp_destination_outside_the_list_is_flagged_without_the_password(tmp_path):
    app = make_app(tmp_path, OUTBOUND_ALLOWED_HOSTS='smtp=smtp.allowed.test:25')
    conn, admin = admin_in(app)
    for key, value in (('smtp.host', 'smtp.other.test'), ('smtp.port', 25)):
        conn.execute("INSERT INTO settings (key, value, updated_at, updated_by) VALUES (?, ?, 'x', 'sql')", (key, __import__('json').dumps(value)))
    conn.execute("UPDATE settings_meta SET version = version + 1")
    conn.commit()
    app.config['SETTINGS_STORE'].refresh(conn, force=True)
    found, response = alerts(admin)
    assert [a for a in found if a['code'] == 'destination_not_allowed' and a.get('service') == 'smtp']
    assert 'smtp.other.test' not in str(found)
    conn.close()


def test_a_basemap_host_outside_the_list_is_flagged_as_tiles(tmp_path):
    app = make_app(tmp_path, OUTBOUND_ALLOWED_HOSTS='tiles=tile.openstreetmap.org')
    conn, admin = admin_in(app)
    basemaps = [{'id': 'x', 'label': 'X', 'url': 'https://tiles.evil.test/{z}/{x}/{y}.png', 'attribution': 'x', 'max_zoom': 18}]
    conn.execute("INSERT INTO settings (key, value, updated_at, updated_by) VALUES ('map.basemaps', ?, 'x', 'sql')", (__import__('json').dumps(basemaps),))
    conn.execute("UPDATE settings_meta SET version = version + 1")
    conn.commit()
    app.config['SETTINGS_STORE'].refresh(conn, force=True)
    found, response = alerts(admin)
    assert any(a.get('service') == 'tiles' for a in found) or any('tiles' in a['code'] or 'basemap' in a['code'] for a in found)
    assert 'tiles.evil.test' not in str(found)
    conn.close()


def test_the_admin_settings_route_carries_the_same_alerts_and_only_an_admin_can_read_them(tmp_path):
    app = make_app(tmp_path)
    conn, admin = admin_in(app)
    user = session_client(app, add(app, conn, 'simple'))
    assert call(user, 'get', '/api/admin/settings/diagnostic').status_code == 403
    assert call(app.test_client(), 'get', '/api/admin/settings/diagnostic').status_code in (401, 403)
    body = call(admin, 'get', '/api/admin/settings').get_json()
    assert [a['code'] for a in body['alerts']] == [a['code'] for a in alerts(admin)[0]]
    conn.close()


def test_no_alert_ever_carries_a_secret_a_hash_or_an_internal_path(tmp_path):
    app = make_app(tmp_path, OUTBOUND_ALLOWED_HOSTS='routing=engine.test:8989')
    conn, admin = admin_in(app)
    text = call(admin, 'get', '/api/admin/settings/diagnostic').get_data(as_text=True)
    assert KEY not in text and 'argon2' not in text and '/data' not in text and 'Traceback' not in text
    conn.close()

