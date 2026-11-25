"""
Invitations depuis le profil d'un compte SSO (retour utilisateur du 25/11) : la section est visible pour TOUT compte dès que les
invitations sont disponibles (mode `password`, ou mode `oidc` avec SECRETS_KEY ET PUBLIC_URL, EF-0.4, EF-3.2), pas seulement pour un compte
à mot de passe ; sinon elle est absente. Écrits depuis le défaut : un compte SSO avec crédits ne voyait pas la section.
"""

import re

import pytest

from app import create_app
from tests.conftest import OIDC_OVERRIDES
from tests.pw_harness import HOST, KEY, ORIGIN, Prod, add, call, connect, make_app, new_secret, session_client


def oidc_app(tmp_path, *, secrets=True, public=True, name='oidc'):
    overrides = {'DATA_DIR': str(tmp_path / name), 'AUTH_MODE': 'oidc', **OIDC_OVERRIDES, 'TRUSTED_HOSTS': [HOST]}
    if public:
        overrides['PUBLIC_URL'] = ORIGIN
    if secrets:
        overrides['SECRETS_KEY'] = KEY
    return create_app(Prod, overrides=overrides)


def sso(app, name='sso-user', *, credits=1, email='sso@example.org', admin=False):
    conn = connect(app)
    try:
        row = add(app, conn, name, method='oidc', email=email, credits=credits, admin=admin)
    finally:
        conn.close()
    return session_client(app, row), row['id']


def page(client, path='/profile'):
    response = client.get(path, base_url=ORIGIN)
    assert response.status_code == 200, response.get_data(as_text=True)[:300]
    return response.get_data(as_text=True)


def scripts_of(html):
    return re.findall(r'<script[^>]+src="([^"]+)"', html)


def served_scripts(client, html):
    """Contenu de chaque script chargé par la page (ce que le navigateur exécute)."""
    return {src: client.get(src, base_url=ORIGIN).get_data(as_text=True) for src in scripts_of(html)}


def drives_invitations(client, html):
    return any('/api/invitations' in code and 'invForm' in code for code in served_scripts(client, html).values())


def credits(app, user_id):
    conn = connect(app)
    try:
        return conn.execute("SELECT invite_credits FROM users WHERE id = ?", (user_id,)).fetchone()[0]
    finally:
        conn.close()


def pending_invitations(app):
    conn = connect(app)
    try:
        return conn.execute("SELECT COUNT(*) FROM auth_tokens WHERE kind = 'invite'").fetchone()[0]
    finally:
        conn.close()


# --- compte SSO avec un crédit -------------------------------------------------------------------------------------------------------

def test_an_sso_account_with_a_credit_sees_the_invitations_section_the_form_and_a_script_that_drives_it(tmp_path):
    app = oidc_app(tmp_path)
    client, _ = sso(app, credits=1)
    html = page(client)
    assert html.count('id="p-inv"') == 1 and 'id="invCredits"' in html and html.count('id="invForm"') == 1
    assert 'id="invEmail"' in html and 'Inviter' in html and 'id="invList"' in html
    assert drives_invitations(client, html), "aucun script chargé ne lit /api/invitations pour ce compte"


def test_an_sso_account_sees_no_password_account_blocks_with_the_invitations_section(tmp_path):
    app = oidc_app(tmp_path)
    client, _ = sso(app)
    html = page(client)
    for forbidden in ('id="passwordForm"', 'id="emailForm"', 'id="mfaForm"', 'id="mfaSetup"'):
        assert forbidden not in html, forbidden                                      # mot de passe, adresse, second facteur : comptes password
    assert not any('webauthn' in src or 'profile-security' in src for src in scripts_of(html))       # ni WebAuthn ni TOTP chez un compte SSO


def test_an_sso_account_can_create_an_invitation_and_the_credit_goes_down(tmp_path):
    app = oidc_app(tmp_path)
    client, user = sso(app, credits=1)
    listing = call(client, 'get', '/api/invitations').get_json()
    assert listing['invite_credits'] == 1 and listing['can_invite'] is True and listing['is_admin'] is False
    created = call(client, 'post', '/api/invitations', {'email': 'ami@example.org'})
    assert created.status_code == 201, created.get_data(as_text=True)
    assert created.get_json()['link'].startswith(ORIGIN) and credits(app, user) == 0 and pending_invitations(app) == 1
    after = call(client, 'get', '/api/invitations').get_json()
    assert after['invite_credits'] == 0 and after['can_invite'] is False
    assert [i['email'] for i in after['invitations']] == ['ami@example.org']


def test_an_sso_account_cannot_invite_twice_with_one_credit(tmp_path):
    app = oidc_app(tmp_path)
    client, user = sso(app, credits=1)
    assert call(client, 'post', '/api/invitations', {'email': 'un@example.org'}).status_code == 201
    second = call(client, 'post', '/api/invitations', {'email': 'deux@example.org'})
    assert second.status_code == 409 and credits(app, user) == 0 and pending_invitations(app) == 1


# --- compte SSO sans crédit ----------------------------------------------------------------------------------------------------------

def test_an_sso_account_without_credit_sees_the_section_but_cannot_invite(tmp_path):
    app = oidc_app(tmp_path)
    client, user = sso(app, credits=0)
    html = page(client)
    assert html.count('id="p-inv"') == 1 and 'id="invCredits"' in html
    assert re.search(r'<form id="invForm"[^>]*\bhidden\b', html), "le formulaire doit être masqué dès le rendu pour un compte sans crédit"
    assert drives_invitations(client, html)                                           # le texte des crédits est écrit par ce script
    listing = call(client, 'get', '/api/invitations').get_json()
    assert listing['invite_credits'] == 0 and listing['can_invite'] is False
    refused = call(client, 'post', '/api/invitations', {'email': 'ami@example.org'})
    assert refused.status_code == 409 and credits(app, user) == 0 and pending_invitations(app) == 0


def test_an_sso_account_without_a_validated_address_cannot_invite_even_with_credits(tmp_path):
    app = oidc_app(tmp_path)
    client, user = sso(app, credits=3, email=None)
    html = page(client)
    assert html.count('id="p-inv"') == 1                                              # la section est là : elle dit pourquoi pas de formulaire
    assert call(client, 'get', '/api/invitations').get_json()['can_invite'] is False
    assert call(client, 'post', '/api/invitations', {'email': 'ami@example.org'}).status_code == 403
    assert credits(app, user) == 3


def test_an_sso_admin_sees_the_section_with_unlimited_invitations(tmp_path):
    app = oidc_app(tmp_path)
    client, _ = sso(app, credits=0, admin=True)
    assert page(client).count('id="p-inv"') == 1
    listing = call(client, 'get', '/api/invitations').get_json()
    assert listing['is_admin'] is True and listing['can_invite'] is True


# --- invitations indisponibles --------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('secrets,public', [(False, True), (True, False), (False, False)], ids=['sans SECRETS_KEY', 'sans PUBLIC_URL', 'sans les deux'])
def test_without_the_conditions_of_ef_0_4_the_section_is_absent_and_the_api_answers_404(tmp_path, secrets, public):
    try:
        app = oidc_app(tmp_path, secrets=secrets, public=public)
    except RuntimeError:
        pytest.skip("configuration refusée au démarrage : aucune instance à tester")
    client, _ = sso(app, credits=2)
    html = page(client)
    assert 'id="p-inv"' not in html and 'id="invForm"' not in html and 'id="invCredits"' not in html
    assert not drives_invitations(client, html)
    assert call(client, 'get', '/api/invitations').status_code == 404
    assert call(client, 'post', '/api/invitations', {'email': 'ami@example.org'}).status_code == 404


def test_in_local_mode_there_is_no_invitations_section(app, client, local_user_id):
    html = client.get('/profile').get_data(as_text=True)
    assert 'id="p-inv"' not in html and 'id="invForm"' not in html
    assert client.get('/api/invitations').status_code == 404


# --- compte à mot de passe : aucune régression, une seule section --------------------------------------------------------------------

def test_a_password_account_still_has_exactly_one_invitations_section_and_the_password_blocks(tmp_path):
    app = make_app(tmp_path)
    conn = connect(app)
    row = add(app, conn, 'pw-user', email='pw@example.org', credits=1, secret=new_secret())
    conn.close()
    client = session_client(app, row)
    html = page(client)
    assert html.count('id="p-inv"') == 1 and html.count('id="invForm"') == 1 and html.count('id="invCredits"') == 1
    assert 'id="passwordForm"' in html and 'id="mfaForm"' in html
    assert drives_invitations(client, html)
    scripts = [s for s in scripts_of(html)]
    assert len(scripts) == len(set(scripts))                                           # aucun script chargé deux fois (pas de double écouteur)
    handlers = sum(code.count("getElementById('invForm')") + code.count('$(\'invForm\')') for code in served_scripts(client, html).values())
    assert handlers <= 2, "le formulaire ne doit pas être branché par deux scripts"
    assert call(client, 'post', '/api/invitations', {'email': 'ami@example.org'}).status_code == 201


def test_an_sso_account_loads_no_script_twice(tmp_path):
    app = oidc_app(tmp_path)
    client, _ = sso(app)
    scripts = scripts_of(page(client))
    assert len(scripts) == len(set(scripts))
