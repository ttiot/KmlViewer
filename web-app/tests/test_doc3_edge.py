"""
Doc-3 en profondeur (rôle test) : `flask user-invite --admin --account <login> --reset-password` (CA-54, EF-0.3b) : jeton, politique de mot de
passe, sessions révoquées, enrôlement puis octroi d'admin, comptes non éligibles, état changé entre l'émission et la consommation.
"""

import json
import re

import pytest

from tests.pw_harness import (GOOD, KW, OTHER, add, call, code_at, cookie_payload, connect, form_post, login, make_app, new_ip, refresh,
                              session_client)

NEW = 'nouveau-mot-de-passe-lune-77'


@pytest.fixture
def app(tmp_path):
    return make_app(tmp_path)


@pytest.fixture
def conn(app):
    c = connect(app)
    yield c
    c.close()


def invite(app, *args):
    result = app.test_cli_runner().invoke(args=['user-invite', '--admin', *args])
    return result, (re.search(r'/auth/token#t=([A-Za-z0-9_-]+)', result.output) or [None, None])[1]


def enroll_secret(client):
    html = client.get('/auth/password/enroll', **KW).get_data(as_text=True)
    return re.sub(r'\s', '', re.search(r'(?:[A-Z2-7]{4}\s){7}[A-Z2-7]{4}', html).group(0))


def test_the_reset_link_asks_for_a_new_password_not_the_current_one_and_ends_in_enrollment_then_admin(app, conn):
    user = add(app, conn, 'chef')
    stale = session_client(app, user)
    result, token = invite(app, '--account', 'chef', '--reset-password')
    assert result.exit_code == 0 and token, result.output
    assert form_post(app, '/auth/token/inspect', {'token': token}).get_json()['form'] == 'set_password'
    for weak in ('court', 'chef', 'password1234', 'x' * 129, ''):
        r = form_post(app, '/auth/token/consume', {'token': token, 'password': weak})
        assert r.status_code == 400, weak
    assert conn.execute("SELECT used_at FROM auth_tokens").fetchone()[0] is None                     # politique refusée : jeton non consommé
    client = app.test_client()
    r = form_post(app, '/auth/token/consume', {'token': token, 'password': NEW}, client=client)
    assert r.status_code == 200 and r.get_json()['next'].endswith('/auth/password/enroll'), r.get_data()
    after = refresh(conn, 'chef')
    assert after['password_hash'] != user['password_hash'] and after['password_changed_at'] and not after['is_admin']
    assert after['session_token'] != user['session_token'] and call(stale, 'get', '/api/me').status_code == 401        # sessions révoquées
    assert login(app, 'chef', GOOD).status_code == 401                                                # l'ancien mot de passe est mort
    assert set(cookie_payload(app, client)) == {'pending_id'}                                         # aucune session complète (r8)
    secret = enroll_secret(client)
    done = form_post(app, '/auth/password/enroll', {'code': code_at(secret)}, client=client)
    assert done.status_code == 200 and len(done.get_json()['recovery_codes']) == 10 and refresh(conn, 'chef')['is_admin'] == 1
    assert form_post(app, '/auth/token/inspect', {'token': token}).status_code == 404               # usage unique
    assert login(app, 'chef', NEW).get_json()['step'] == 'mfa'


def test_the_reset_keeps_the_account_non_admin_until_a_factor_is_enrolled_and_an_abandoned_enrollment_changes_nothing_more(app, conn):
    add(app, conn, 'chef')
    result, token = invite(app, '--account', 'chef', '--reset-password')
    client = app.test_client()
    assert form_post(app, '/auth/token/consume', {'token': token, 'password': NEW}, client=client).status_code == 200
    assert not refresh(conn, 'chef')['is_admin']
    app.extensions['pending']._clock = lambda: 10 ** 9                                                # état d'enrôlement expiré
    assert client.get('/auth/password/enroll', **KW).status_code == 302
    after = refresh(conn, 'chef')
    assert not after['is_admin'] and login(app, 'chef', NEW).status_code == 200                       # le nouveau mot de passe reste valable


def test_the_plain_resume_still_asks_for_the_current_password_and_keeps_the_hash(app, conn):
    user = add(app, conn, 'chef')
    result, token = invite(app, '--account', 'chef')
    assert form_post(app, '/auth/token/inspect', {'token': token}).get_json()['form'] == 'confirm_password'
    assert form_post(app, '/auth/token/consume', {'token': token, 'password': NEW}).status_code == 400
    ok = form_post(app, '/auth/token/consume', {'token': token, 'password': GOOD})
    assert ok.status_code == 200 and refresh(conn, 'chef')['password_hash'] == user['password_hash']


def test_the_two_variants_do_not_cross(app, conn):
    """Un lien « reprise » ne remplace jamais le mot de passe ; un lien « oubli » n'est pas un lien qui demande l'ancien."""
    user = add(app, conn, 'chef')
    _, resume = invite(app, '--account', 'chef')
    r = form_post(app, '/auth/token/consume', {'token': resume, 'password': NEW})
    assert r.status_code == 400 and refresh(conn, 'chef')['password_hash'] == user['password_hash']
    conn.execute("UPDATE auth_tokens SET revoked_at = '2026-01-01T00:00:00Z'")
    conn.commit()
    _, reset = invite(app, '--account', 'chef', '--reset-password')
    assert form_post(app, '/auth/token/inspect', {'token': reset}).get_json()['form'] == 'set_password'
    assert form_post(app, '/auth/token/consume', {'token': reset, 'password': GOOD}).status_code in (200, 400)


@pytest.mark.parametrize('setup,label', [
    pytest.param("UPDATE users SET is_active = 0 WHERE username = 'chef'", 'désactivé', marks=pytest.mark.xfail(
        strict=True, reason="T-Doc3-A (EF-0.3b : « compte password ACTIF sans facteur seulement ; sinon refus ») : la CLI émet un lien (code 0) pour un "
                            "compte DÉSACTIVÉ ; il sera refusé à la consommation (réponse neutre), mais l'opérateur croit avoir un lien utilisable")),
    ("UPDATE users SET auth_method = 'oidc', password_hash = NULL, sub = 'sub:chef' WHERE username = 'chef'", 'SSO'),
    ("UPDATE users SET totp_secret = x'01', totp_enabled_at = '2026-01-01T00:00:00Z' WHERE username = 'chef'", 'avec facteur'),
])
def test_an_ineligible_account_gets_no_link(app, conn, setup, label):
    add(app, conn, 'chef')
    conn.execute(setup)
    conn.commit()
    result, token = invite(app, '--account', 'chef', '--reset-password')
    assert result.exit_code != 0 and token is None, label
    assert conn.execute("SELECT COUNT(*) FROM auth_tokens").fetchone()[0] == 0


@pytest.mark.parametrize('args', [('--account', 'inconnu', '--reset-password'), ('--reset-password',), ('--account', '', '--reset-password'),
                                  ('--account', 'chef;DROP', '--reset-password')])
def test_invalid_invocations_create_nothing(app, conn, args):
    add(app, conn, 'chef')
    result, token = invite(app, *args)
    assert result.exit_code != 0 and token is None
    assert conn.execute("SELECT COUNT(*) FROM auth_tokens").fetchone()[0] == 0


@pytest.mark.parametrize('change', ["UPDATE users SET is_active = 0 WHERE username = 'chef'",
                                    "UPDATE users SET totp_secret = x'01', totp_enabled_at = '2026-01-01T00:00:00Z' WHERE username = 'chef'",
                                    "UPDATE users SET auth_method = 'oidc', password_hash = NULL WHERE username = 'chef'"])
def test_an_account_that_changed_between_the_issue_and_the_use_makes_the_link_neutral_and_the_password_stays(app, conn, change):
    user = add(app, conn, 'chef')
    _, token = invite(app, '--account', 'chef', '--reset-password')
    conn.execute(change)
    conn.commit()
    r = form_post(app, '/auth/token/consume', {'token': token, 'password': NEW})
    assert r.status_code in (400, 404), r.get_data()
    after = refresh(conn, 'chef')
    assert after['password_hash'] in (user['password_hash'], None) and not after['is_admin']


def test_a_password_changed_by_the_user_between_issue_and_use_is_not_overwritten_silently(app, conn):
    """Le lien a été émis pour un compte qui a, entre-temps, changé son mot de passe : l'écraser serait un détournement par un vieux lien."""
    user = add(app, conn, 'chef')
    _, token = invite(app, '--account', 'chef', '--reset-password')
    changed = call(session_client(app, user), 'post', '/api/me/password', {'current_password': GOOD, 'new_password': OTHER})
    assert changed.status_code == 200
    mid = refresh(conn, 'chef')
    r = form_post(app, '/auth/token/consume', {'token': token, 'password': NEW})
    after = refresh(conn, 'chef')
    # deux comportements acceptables : refus neutre (mot de passe de l'utilisateur conservé) ou remplacement tracé et ses sessions révoquées
    assert (r.status_code in (400, 404) and after['password_hash'] == mid['password_hash']) or (r.status_code == 200 and after['session_token'] != mid['session_token'])


def test_two_reset_links_for_the_same_account_are_both_single_use_and_the_second_does_not_resurrect_the_first(app, conn):
    add(app, conn, 'chef')
    _, first = invite(app, '--account', 'chef', '--reset-password')
    result, second = invite(app, '--account', 'chef', '--reset-password')
    assert first and (second or result.exit_code != 0)
    r = form_post(app, '/auth/token/consume', {'token': first, 'password': NEW})
    assert r.status_code == 200
    if second:
        again = form_post(app, '/auth/token/consume', {'token': second, 'password': OTHER})
        assert again.status_code in (200, 400, 404)
        assert form_post(app, '/auth/token/consume', {'token': first, 'password': OTHER}).status_code == 404


def test_the_cli_audit_line_has_the_cli_actor_and_no_token_nor_password(app, conn):
    add(app, conn, 'chef')
    _, token = invite(app, '--account', 'chef', '--reset-password')
    form_post(app, '/auth/token/consume', {'token': token, 'password': NEW})
    rows = [dict(r) for r in conn.execute("SELECT * FROM audit_log ORDER BY id")]
    assert rows and rows[0]['actor_type'] == 'cli' and rows[0]['actor_id'] is None
    blob = json.dumps(rows)
    for forbidden in (token, NEW, GOOD, 'argon2'):
        assert forbidden not in blob


def test_the_reset_link_is_limited_like_every_other_token_route(app, conn):
    add(app, conn, 'chef')
    _, token = invite(app, '--account', 'chef', '--reset-password')
    codes = [form_post(app, '/auth/token/consume', {'token': token, 'password': 'court'}, ip='198.51.100.7').status_code for _ in range(16)]
    assert codes[0] == 400 and 429 in codes
    assert form_post(app, '/auth/token/consume', {'token': token, 'password': NEW}, ip=new_ip()).status_code == 200
