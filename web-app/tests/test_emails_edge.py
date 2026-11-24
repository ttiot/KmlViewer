"""
Forme canonique des adresses (T7a-n2, EF-4.10, §13 n°32) et migration 0014_emails_canonical sur une base peuplée ;
bootstrap sans enrôlement (T7b : 501 partout, jeton intact). Complète test_mailer.py, test_tokens.py, test_invitations.py.
"""

import json
import random
import re
import sqlite3
import time

import pytest
from cryptography.fernet import Fernet

from app import audit, create_app
from app import db as database
from app.auth import invitations, tokens
from app.auth.emails import canonical_email, canonical_or_none
from app.config import TestingConfig
from app.mailer import validate_address

KEY = Fernet.generate_key().decode()
HOST = 'traces.example.com'
GOOD = 'cheval-agrafe-lune-42'


# --- canonical_email : NFKC + casefold + blancs, rien d'autre ---------------------------------------------------------------

@pytest.mark.parametrize('raw,expected', [
    ('Alice@Example.ORG', 'alice@example.org'), ('  a@b.c  ', 'a@b.c'), ('　a@b.c ', 'a@b.c'), ('a@b.c\n', 'a@b.c'), ('\ta@b.c\r\n', 'a@b.c'),
    ('Ａ@Ｂ.com', 'a@b.com'), ('a＠b.c', 'a@b.c'), ('ß@Straße.de', 'ss@strasse.de'), ('STRASSE@x.de', 'strasse@x.de'),
    ('ﬁ@ﬃ.org', 'fi@ffi.org'), ('ǅ@x.org', 'dž@x.org'), ('İ@x.org', 'i̇@x.org'), ('Σ@x.org', 'σ@x.org'), ('ς@x.org', 'σ@x.org'),
    ('é@x.org', 'é@x.org'), ('é@x.org', 'é@x.org'), ('a+Tag@B.c', 'a+tag@b.c'), ('ⓐ@x.org', 'a@x.org'), ('①@x.org', '1@x.org'),
    ('a@b.c', 'a@b.c'),
])
def test_canonical_form(raw, expected):
    assert canonical_email(raw) == expected


def test_composed_and_decomposed_accents_and_width_variants_collapse_to_one_address():
    forms = ['éric@exemple.fr', 'éric@exemple.fr', 'ÉRIC@EXEMPLE.FR', 'Ｅ́ＲＩＣ@EXEMPLE.fr', ' éric@exemple.fr ']
    assert len({canonical_email(f) for f in forms}) == 1


@pytest.mark.parametrize('lookalike', ['а@x.org', 'a@х.org', 'ａ@x.org'.replace('ａ', 'α'), 'a@x.оrg'])
def test_cross_script_homoglyphs_are_NOT_folded_and_stay_distinct_addresses(lookalike):
    """Spec EF-4.10 : NFKC + casefold, pas de conversion IDNA ni de repli des confusables : la collision éventuelle est connue et acceptée."""
    assert canonical_email(lookalike) != canonical_email('a@x.org')


def test_canonical_form_is_idempotent_on_a_large_random_sample():
    rng = random.Random(7)
    alphabet = ['a', 'B', 'ß', 'İ', 'ﬃ', 'Σ', 'ς', 'é', 'é', 'Ａ', '①', 'ǅ', '1', '.', '-', '_', '+', 'ı', 'ſ', 'ǰ', 'ΐ', 'ᾳ', 'ẞ']
    for _ in range(3000):
        local = ''.join(rng.choice(alphabet) for _ in range(rng.randint(1, 8)))
        domain = ''.join(rng.choice(alphabet) for _ in range(rng.randint(1, 8)))
        address = f'{local}@{domain}'
        canon = canonical_or_none(address)
        if canon is not None:
            assert canonical_email(canon) == canon, repr(address)


@pytest.mark.parametrize('bad', ['', ' ', '\n', 'sans-arobase', None, 5, b'a@b.c', ['a@b.c'], 'a b@c.d', 'a@b c.d', 'a@b.c\r\nBcc: x@y.z', 'a@b.c\x00',
                                 'a@b.c x', 'a@b.c\u0085x', 'x' * 255 + '@b.c', 'ﬃ' * 100 + '@b.c', 'a@b.c' + ' ' * 600])
def test_unusable_addresses_raise_value_error_and_the_lenient_form_returns_none(bad):
    with pytest.raises(ValueError):
        canonical_email(bad)
    assert canonical_or_none(bad) is None


def test_the_length_limit_is_measured_after_normalisation_and_is_exact():
    ok = 'a' * 249 + '@b.co'                                  # 254
    assert len(ok) == 254 and canonical_email(ok) == ok
    with pytest.raises(ValueError):
        canonical_email('a' * 250 + '@b.co')                    # 255
    with pytest.raises(ValueError):
        canonical_email('ﬃ' * 85 + '@b.co')                    # 85 caractères bruts, 255+ après NFKC
    assert canonical_email('ﬃ' * 83 + '@b.c') == 'ffi' * 83 + '@b.c' and len(canonical_email('ﬃ' * 83 + '@b.c')) <= 254


@pytest.mark.parametrize('address', ['＜a＠b.c＞', 'a，b@c.d', 'a；b@c.d', 'a＂b@c.d', 'a＼b@c.d', 'a＜b＞@c.d', 'a＠b.c，d＠e.f'])
def test_fullwidth_separators_become_ascii_and_are_then_refused_by_the_mail_validation(address):
    assert any(c in canonical_email(address) for c in '<>,;"\\')
    with pytest.raises(ValueError):
        validate_address(address)


def test_the_stored_and_the_compared_form_is_the_same_everywhere(tmp_path):
    """Plus de lower() SQL sur les adresses : seul le repli Python (casefold) sait que ß = ss."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1] / 'app'
    offenders = []
    for path in root.rglob('*.py'):
        for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
            if re.search(r"lower\(\s*(?:u\.|a\.)?(?:pending_)?email", line, re.I):
                offenders.append(f'{path.name}:{number}')
    assert offenders == [], offenders


def test_the_invitation_pipeline_compares_the_canonical_form_end_to_end(tmp_path):
    app = create_app(TestingConfig, overrides={'DATA_DIR': str(tmp_path / 'd'), 'AUTH_MODE': 'password', 'TRUSTED_HOSTS': [HOST],
                                               'SECRETS_KEY': KEY, 'PUBLIC_URL': f'https://{HOST}', 'TESTING': False, 'SECRET_KEY': 'k' * 40,
                                               'SESSION_COOKIE_SECURE': True, 'INBOX_ENABLED': False})
    conn = database.connect(app.config['DB_PATH'])
    conn.execute("INSERT INTO users (sub, username, email, is_admin, auth_method, session_token) VALUES ('pwd:a', 'adm', 'strasse@exemple.fr', 1, 'password', 't')")
    conn.commit()
    admin = conn.execute("SELECT * FROM users").fetchone()
    with pytest.raises(invitations.InvitationError) as exc:
        invitations.create(conn, admin, 'STRAßE@EXEMPLE.fr')                                                   # ß = ss : même adresse
    assert exc.value.status == 409
    assert invitations.create(conn, admin, 'ＮＯＵＶＥＬ@Exemple.fr')
    with pytest.raises(invitations.InvitationError):
        invitations.create(conn, admin, 'nouvel@exemple.fr')                                                   # visée par une invitation en attente
    conn.close()


# --- Migration 0014 sur une base peuplée ------------------------------------------------------------------------------------------

def _database_before_0014(tmp_path):
    path = str(tmp_path / 'old.sqlite3')
    c = database.connect(path)
    c.executescript(database.SCHEMA)
    names = [name for name, _ in database.NAMED_MIGRATIONS]
    database.migrate(c, named=[m for m in database.NAMED_MIGRATIONS if names.index(m[0]) < names.index('0014_emails_canonical')])
    return path, c


def _add(c, sub, username, email=None, pending=None, method='oidc'):
    c.execute("INSERT INTO users (sub, username, email, pending_email, auth_method) VALUES (?, ?, ?, ?, ?)", (sub, username, email, pending, method))


def _rows(c):
    return {r['username']: (r['email'], r['pending_email']) for r in c.execute("SELECT username, email, pending_email FROM users")}


def test_a_populated_database_gets_canonical_addresses_and_loses_nothing(tmp_path):
    path, c = _database_before_0014(tmp_path)
    _add(c, 'idp-1', 'sso-case', 'Alice@Example.ORG')
    _add(c, 'idp-2', 'sso-width', 'Ｂｏｂ@Example.org', 'ＰＥＮＤ@Example.org')
    _add(c, 'idp-3', 'sso-eszett', 'Straße@Exemple.de')
    _add(c, 'pwd:1', 'pwd-ligature', 'ﬁnal@x.org', None, 'password')
    _add(c, 'pwd:2', 'pwd-pending', None, '  Pending@X.org ', 'password')
    _add(c, 'idp-4', 'sso-null', None, None)
    _add(c, 'idp-5', 'sso-already', 'deja@canonique.org')
    c.commit()
    before = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    c.close()
    database.init_db(path)
    c = database.connect(path)
    rows = _rows(c)
    assert rows['sso-case'] == ('alice@example.org', None) and rows['sso-width'] == ('bob@example.org', 'pend@example.org')
    assert rows['sso-eszett'][0] == 'strasse@exemple.de' and rows['pwd-ligature'][0] == 'final@x.org'
    assert rows['pwd-pending'] == (None, 'pending@x.org') and rows['sso-null'] == (None, None) and rows['sso-already'][0] == 'deja@canonique.org'
    assert c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == before
    assert c.execute("SELECT 1 FROM schema_migrations WHERE name = '0014_emails_canonical'").fetchone()
    c.close()


def test_the_migration_is_idempotent_and_a_second_pass_changes_and_audits_nothing(tmp_path):
    path, c = _database_before_0014(tmp_path)
    _add(c, 'idp-1', 'one', 'A@x.org')
    _add(c, 'idp-2', 'two', 'a@x.org')                                                          # collision SSO après canonisation
    c.commit()
    c.close()
    database.init_db(path)
    c = database.connect(path)
    snapshot = _rows(c)
    audits = c.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'identity.collision'").fetchone()[0]
    c.execute("DELETE FROM schema_migrations WHERE name = '0014_emails_canonical'")
    c.commit()
    c.close()
    database.init_db(path)
    c = database.connect(path)
    assert _rows(c) == snapshot
    assert c.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'identity.collision'").fetchone()[0] in (audits, audits + 1)       # une collision persistante est re-signalée
    c.close()


def test_collisions_never_fail_the_migration_and_are_audited_without_the_addresses(tmp_path, caplog):
    path, c = _database_before_0014(tmp_path)
    _add(c, 'idp-1', 'sso-one', 'Same@X.org')
    _add(c, 'idp-2', 'sso-two', 'SAME@x.org')                                                    # deux SSO : tolérés (EF-1.2)
    _add(c, 'pwd:1', 'pwd-one', 'Dup@X.org', None, 'password')
    _add(c, 'pwd:2', 'pwd-two', 'dup@x.org', None, 'password')                                  # deux password : l'index unique partiel refusera la réécriture
    c.commit()
    c.close()
    import logging
    with caplog.at_level(logging.WARNING):
        database.init_db(path)                                                                   # ne doit pas lever
    c = database.connect(path)
    rows = _rows(c)
    assert rows['sso-one'][0] == rows['sso-two'][0] == 'same@x.org'
    assert {rows['pwd-one'][0], rows['pwd-two'][0]} == {'Dup@X.org', 'dup@x.org'} or sorted([rows['pwd-one'][0], rows['pwd-two'][0]]) in (['dup@x.org', 'dup@x.org'], ['Dup@X.org', 'dup@x.org'])
    entry = c.execute("SELECT actor_type, details FROM audit_log WHERE action = 'identity.collision'").fetchone()
    assert entry['actor_type'] == 'system' and 'x.org' not in entry['details'] and caplog.text.count('collision') >= 1 and 'x.org' not in caplog.text
    unique = c.execute("SELECT COUNT(*) FROM (SELECT email FROM users WHERE auth_method = 'password' AND email IS NOT NULL GROUP BY email HAVING COUNT(*) > 1)").fetchone()[0]
    assert unique == 0                                                                           # l'invariant d'unicité des comptes password tient
    c.close()


@pytest.mark.parametrize('stored', ['pas-une-adresse', '', '   ', 'a b@c.d', 'x' * 300 + '@b.c', 'a@b.c\x00', 'a@b.c\r\nBcc: x@y.z'])
def test_unusable_stored_values_are_left_untouched_not_erased(tmp_path, stored):
    path, c = _database_before_0014(tmp_path)
    c.execute("INSERT INTO users (sub, username, email) VALUES ('idp-1', 'weird', ?)", (stored,))
    c.commit()
    c.close()
    database.init_db(path)
    c = database.connect(path)
    assert c.execute("SELECT email FROM users WHERE username = 'weird'").fetchone()[0] == stored
    c.close()


def test_the_migration_is_fast_on_a_large_table_and_keeps_user_version(tmp_path):
    path, c = _database_before_0014(tmp_path)
    c.executemany("INSERT INTO users (sub, username, email) VALUES (?, ?, ?)", [(f'idp-{i}', f'user{i}', f'User{i}@Example.ORG') for i in range(20_000)])
    c.commit()
    version = c.execute("PRAGMA user_version").fetchone()[0]
    c.close()
    started = time.monotonic()
    database.init_db(path)
    assert time.monotonic() - started < 20
    c = database.connect(path)
    assert c.execute("PRAGMA user_version").fetchone()[0] == version
    assert c.execute("SELECT COUNT(*) FROM users WHERE email != lower(email)").fetchone()[0] == 0
    assert c.execute("SELECT email FROM users WHERE username = 'user19999'").fetchone()[0] == 'user19999@example.org'
    c.close()


def test_a_fresh_database_has_the_migration_registered_and_nothing_to_rewrite(tmp_path):
    path = str(tmp_path / 'fresh.sqlite3')
    database.init_db(path)
    c = database.connect(path)
    assert c.execute("SELECT 1 FROM schema_migrations WHERE name = '0014_emails_canonical'").fetchone()
    assert c.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'identity.collision'").fetchone()[0] == 0
    c.close()


def test_after_the_migration_a_password_account_can_no_longer_take_the_address_of_another_in_another_case(tmp_path):
    path, c = _database_before_0014(tmp_path)
    _add(c, 'pwd:1', 'first', 'Owner@X.org', None, 'password')
    c.commit()
    c.close()
    database.init_db(path)
    c = database.connect(path)
    assert c.execute("SELECT email FROM users WHERE username = 'first'").fetchone()[0] == 'owner@x.org'
    with pytest.raises(sqlite3.IntegrityError):
        _add(c, 'pwd:2', 'second', 'owner@x.org', None, 'password')
    c.close()


# --- Bootstrap sans enrôlement : 501 partout, jeton intact (T7b) ------------------------------------------------------------------

class Prod(TestingConfig):
    TESTING = False
    INBOX_ENABLED = False
    SECRET_KEY = 'k' * 40
    SESSION_COOKIE_SECURE = None


@pytest.fixture
def app(tmp_path):
    return create_app(Prod, overrides={'DATA_DIR': str(tmp_path / 'data'), 'AUTH_MODE': 'password', 'TRUSTED_HOSTS': [HOST],
                                       'SECRETS_KEY': KEY, 'PUBLIC_URL': f'https://{HOST}'})


@pytest.fixture
def conn(app):
    c = database.connect(app.config['DB_PATH'])
    yield c
    c.close()


def _post(client, path, payload):
    html = client.get('/auth/token', base_url=f'https://{HOST}').get_data(as_text=True)
    csrf_token = re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)
    return client.post(path, json=payload, headers={'X-CSRF-Token': csrf_token, 'Origin': f'https://{HOST}'}, base_url=f'https://{HOST}')


@pytest.mark.parametrize('with_target', [False, True])
def test_a_bootstrap_token_never_grants_admin_by_itself(app, conn, with_target):
    """EF-0.3 : la consommation d'un jeton d'amorçage ne donne JAMAIS l'admin ; seul l'enrôlement d'un facteur le fait (T11)."""
    target = None
    if with_target:
        conn.execute("INSERT INTO users (sub, username, auth_method, session_token) VALUES ('pwd:t', 'target', 'password', 't')")
        conn.commit()
        target = conn.execute("SELECT id FROM users WHERE username = 'target'").fetchone()[0]
    new = tokens.create(conn, 'bootstrap', actor=audit.Actor.cli(), email='boot@example.org', is_admin_grant=True, target_user_id=target)
    client = app.test_client()
    for payload in ({'token': new.token, 'username': 'a', 'password': 'x'}, {'token': new.token}):
        response = _post(client, '/auth/token/consume', payload)
        assert response.status_code in (400, 404), (payload, response.status_code)       # mot de passe faible / champ manquant : refus
        assert 'next' not in response.get_json()
    response = _post(client, '/auth/token/consume', {'token': new.token, 'username': 'founder', 'password': GOOD})
    if not with_target:
        assert response.status_code == 200 and response.get_json()['next'] == '/auth/password/enroll'
    else:
        assert response.status_code == 400                                               # reprise : mot de passe ACTUEL du compte
    assert conn.execute("SELECT COUNT(*) FROM users WHERE is_admin = 1").fetchone()[0] == 0


def test_the_bootstrap_answer_leaks_neither_the_token_nor_the_address_nor_the_password(app, conn):
    plain = tokens.create(conn, 'bootstrap', actor=audit.Actor.cli(), email='secret.boot@example.org', is_admin_grant=True)
    client = app.test_client()
    response = _post(client, '/auth/token/consume', {'token': plain.token, 'username': 'founder', 'password': GOOD})
    text = response.get_data(as_text=True) + json.dumps(dict(response.headers.items()))
    assert response.status_code == 200 and plain.token not in text and 'secret.boot' not in text and GOOD not in text


def test_an_unknown_or_used_bootstrap_like_token_still_gets_the_neutral_404(app, conn):
    client = app.test_client()
    assert _post(client, '/auth/token/consume', {'token': 'A' * 43, 'username': 'founder', 'password': GOOD}).status_code == 404


def test_user_invite_still_issues_the_bootstrap_token_and_a_normal_invitation_still_works(app, conn):
    result = app.test_cli_runner().invoke(args=['user-invite', '--admin', 'boot@example.org'])
    assert result.exit_code == 0 and '/auth/token#t=' in result.output
    conn.execute("INSERT INTO users (sub, username, email, is_admin, auth_method, session_token) VALUES ('pwd:a', 'adm', 'a@example.org', 1, 'password', 't')")
    conn.commit()
    admin = conn.execute("SELECT * FROM users WHERE username = 'adm'").fetchone()
    invite = invitations.create(conn, admin, 'guest@example.org')
    assert invitations.accept(conn, invite.token, 'guest', GOOD)['username'] == 'guest'


@pytest.mark.parametrize('address', ['@', 'a@', '@b.c', 'a@@b.c', 'a@b..c', '.@.'])
def test_an_invitation_to_a_syntactically_empty_address_is_refused_before_anything_is_spent(conn, address):
    conn.execute("INSERT INTO users (sub, username, email, invite_credits, auth_method, session_token) VALUES ('pwd:m', 'member', 'm@example.org', 2, 'password', 't')")
    conn.commit()
    member = conn.execute("SELECT * FROM users WHERE username = 'member'").fetchone()
    with pytest.raises(invitations.InvitationError) as exc:
        invitations.create(conn, member, address)
    assert exc.value.status == 400
    assert conn.execute("SELECT invite_credits FROM users WHERE id = ?", (member['id'],)).fetchone()[0] == 2
