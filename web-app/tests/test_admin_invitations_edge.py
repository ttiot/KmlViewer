"""Onglet « Invitations » de l'administration (rôle test) : EF-3.13, CA-84.

Ce que la spec exige : la liste admin dit qui a émis chaque invitation (`created_by_name`), dans quel état elle est (statut calculé
par le serveur : acceptée > révoquée > expirée > en attente) et quel compte elle a créé (`used_by_name`) ; `None` quand le compte
n'existe plus ; jamais le jeton ni son empreinte ; route réservée aux admins ; vue du profil inchangée ; « Acceptée » partout,
« Utilisée » nulle part comme statut.
"""

import json
import re
from pathlib import Path

import pytest

from app.auth import invitations
from tests.pw_harness import GOOD, add, call, connect, make_app, session_client

WEB_APP = Path(__file__).resolve().parent.parent
PAST = '2020-01-01T00:00:00Z'


@pytest.fixture
def world(tmp_path):
    """alice (non admin, 3 crédits) et un admin ; cinq invitations : en attente x2, révoquée, expirée, acceptée par bob."""
    app = make_app(tmp_path)
    conn = connect(app)
    admin = add(app, conn, 'root', admin=True, email='root@example.org')
    conn.execute("CREATE TABLE IF NOT EXISTS webauthn_credentials (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL)")
    conn.execute("INSERT INTO webauthn_credentials (user_id, credential_id, public_key) VALUES (?, randomblob(16), x'00')", (admin['id'],))
    conn.commit()
    alice = add(app, conn, 'alice', email='alice@example.org', credits=3)
    links = {}
    links['x'] = invitations.create(conn, conn.execute("SELECT * FROM users WHERE id = ?", (alice['id'],)).fetchone(), 'x@exemple.fr')
    links['y'] = invitations.create(conn, admin, 'y@exemple.fr')
    links['revoked'] = invitations.create(conn, admin, 'revoquee@exemple.fr')
    links['expired'] = invitations.create(conn, admin, 'expiree@exemple.fr')
    links['accepted'] = invitations.create(conn, admin, 'acceptee@exemple.fr')
    ids = {name: conn.execute("SELECT id FROM auth_tokens WHERE email = ?", (f"{address}@exemple.fr",)).fetchone()['id']
           for name, address in (('x', 'x'), ('y', 'y'), ('revoked', 'revoquee'), ('expired', 'expiree'), ('accepted', 'acceptee'))}
    assert invitations.revoke(conn, admin, ids['revoked'])
    conn.execute("UPDATE auth_tokens SET expires_at = ? WHERE id = ?", (PAST, ids['expired']))
    conn.commit()
    bob = invitations.accept(conn, links['accepted'].token, 'bob', GOOD)
    assert bob
    return {'app': app, 'conn': conn, 'admin': admin, 'alice': alice, 'bob': bob, 'links': links, 'ids': ids}


def admin_list(world):
    answer = call(session_client(world['app'], world['admin']), 'get', '/api/admin/invitations')
    assert answer.status_code == 200, answer.get_data(as_text=True)
    return {entry['id']: entry for entry in answer.get_json()['invitations']}, answer.get_data(as_text=True)


# ============================================================ API admin (CA-84) =================================================

def test_every_entry_names_its_issuer_and_the_account_it_created(world):
    entries, _ = admin_list(world)
    ids = world['ids']
    assert entries[ids['x']]['created_by_name'] == 'alice'
    assert entries[ids['y']]['created_by_name'] == 'root'
    assert entries[ids['accepted']]['used_by_name'] == 'bob'
    for entry in entries.values():
        assert 'created_by_name' in entry and 'used_by_name' in entry, entry
    assert entries[ids['x']]['used_by_name'] is None                   # pas d'acceptation : aucun compte créé


def test_statuses_follow_accepted_then_revoked_then_expired_then_pending(world):
    entries, _ = admin_list(world)
    ids = world['ids']
    assert {name: entries[ids[name]]['status'] for name in ids} == {
        'x': 'pending', 'y': 'pending', 'revoked': 'revoked', 'expired': 'expired', 'accepted': 'used'}
    conn = world['conn']
    conn.execute("UPDATE auth_tokens SET revoked_at = '2026-01-01T00:00:00Z' WHERE id = ?", (ids['accepted'],))     # acceptée PUIS révoquée
    conn.execute("UPDATE auth_tokens SET revoked_at = '2026-01-01T00:00:00Z' WHERE id = ?", (ids['expired'],))      # expirée ET révoquée
    conn.commit()
    entries, _ = admin_list(world)
    assert entries[ids['accepted']]['status'] == 'used'                  # reste « Acceptée »
    assert entries[ids['expired']]['status'] == 'revoked'                # révoquée l'emporte sur expirée


def test_no_token_and_no_fingerprint_ever_leave_the_server(world):
    entries, raw = admin_list(world)
    for entry in entries.values():
        assert not [key for key in entry if 'token' in key.lower() or 'hash' in key.lower()], sorted(entry)
    for link in world['links'].values():
        assert link.token not in raw
    digest_columns = [r['name'] for r in world['conn'].execute("PRAGMA table_info(auth_tokens)") if 'hash' in r['name'].lower()]
    for column in digest_columns:
        for row in world['conn'].execute(f"SELECT {column} AS v FROM auth_tokens WHERE {column} IS NOT NULL"):
            value = row['v'].hex() if isinstance(row['v'], bytes) else str(row['v'])
            assert value not in raw, column


def test_a_deleted_issuer_reads_none_and_the_pending_invitations_are_revoked(world):
    client = session_client(world['app'], world['admin'])
    assert call(client, 'delete', f"/api/users/{world['alice']['id']}").status_code == 200
    entries, _ = admin_list(world)
    entry = entries[world['ids']['x']]
    assert entry['created_by_name'] is None and entry['created_by'] is None
    assert entry['status'] == 'revoked'                                  # EF-3.7
    assert entries[world['ids']['y']]['created_by_name'] == 'root'       # les autres sont intactes


def test_a_deleted_created_account_reads_none_and_the_invitation_stays_accepted(world):
    client = session_client(world['app'], world['admin'])
    assert call(client, 'delete', f"/api/users/{world['bob']['id']}").status_code == 200
    entries, _ = admin_list(world)
    entry = entries[world['ids']['accepted']]
    assert entry['used_by_name'] is None and entry['status'] == 'used', entry


def test_names_fall_back_from_identifier_to_display_name_to_address_to_the_id(world):
    conn = world['conn']
    made = {}
    for label, username, display, email in (('plain', 'ident', 'Nom affiché', 'a@exemple.fr'), ('display', None, 'Nom affiché', 'b@exemple.fr'),
                                            ('address', None, None, 'c@exemple.fr'), ('bare', None, None, None)):
        cursor = conn.execute("INSERT INTO users (sub, username, display_name, email, is_admin, is_active, auth_method, session_token, "
                              "invite_credits) VALUES (?, ?, ?, ?, 0, 1, 'oidc', ?, 0)",
                              (f'sub:{label}', username, display, email, f'tok-{label}'))
        made[label] = cursor.lastrowid
        conn.execute("INSERT INTO auth_tokens (kind, token_hash, email, created_by, created_at, expires_at) "
                     "VALUES ('invite', ?, ?, ?, '2026-01-01T00:00:00Z', '2099-01-01T00:00:00Z')",
                     (f'hash-{label}'.encode().ljust(32, b'0'), f'{label}.invite@exemple.fr', cursor.lastrowid))
    conn.commit()
    entries, _ = admin_list(world)
    by_email = {e['email']: e for e in entries.values()}
    assert by_email['plain.invite@exemple.fr']['created_by_name'] == 'ident'
    assert by_email['display.invite@exemple.fr']['created_by_name'] == 'Nom affiché'
    assert by_email['address.invite@exemple.fr']['created_by_name'] == 'c@exemple.fr'
    assert by_email['bare.invite@exemple.fr']['created_by_name'] == f"#{made['bare']}"


def test_an_identifier_with_markup_is_served_as_is_for_the_page_to_render_as_text(world):
    conn = world['conn']
    conn.execute("UPDATE users SET username = '<b>x</b>' WHERE id = ?", (world['alice']['id'],))
    conn.commit()
    entries, _ = admin_list(world)
    assert entries[world['ids']['x']]['created_by_name'] == '<b>x</b>'


def test_the_list_is_reserved_to_admins(world):
    member = session_client(world['app'], world['alice'])
    answer = call(member, 'get', '/api/admin/invitations')
    assert answer.status_code == 403
    assert 'x@exemple.fr' not in answer.get_data(as_text=True)
    assert call(world['app'].test_client(), 'get', '/api/admin/invitations').status_code in (401, 403)


# ======================================================== vue du profil inchangée (EF-3.12) =====================================

def test_the_profile_list_keeps_its_shape_and_only_shows_the_issuers_invitations(world):
    answer = call(session_client(world['app'], world['alice']), 'get', '/api/invitations')
    assert answer.status_code == 200
    body = answer.get_json()
    assert [e['email'] for e in body['invitations']] == ['x@exemple.fr']
    assert 'created_by_name' not in body['invitations'][0] and 'used_by_name' not in body['invitations'][0]
    assert body['invitations'][0]['status'] == 'pending'


def test_the_issuer_sees_an_accepted_invitation_with_the_same_status_code(world):
    conn = world['conn']
    admin = world['admin']
    bobs_link = invitations.create(conn, conn.execute("SELECT * FROM users WHERE id = ?", (world['alice']['id'],)).fetchone(), 'z@exemple.fr')
    assert invitations.accept(conn, bobs_link.token, 'carol', GOOD)
    body = call(session_client(world['app'], world['alice']), 'get', '/api/invitations').get_json()
    by_email = {e['email']: e['status'] for e in body['invitations']}
    assert by_email == {'x@exemple.fr': 'pending', 'z@exemple.fr': 'used'}
    assert admin['id']


# ========================================================== vocabulaire : « Acceptée » partout ==================================

PAGES = ('static/js/admin.js', 'static/js/profile-invitations.js', 'static/js/profile-security.js', 'templates/admin.html',
         'templates/profile.html')


def status_labels(text):
    """Libellés de statut d'un fichier : une chaîne qui EST un des mots (pas « utilisée le … » d'une clé de sécurité)."""
    return [m.group(1) for m in re.finditer(r"""['"`](En attente|Utilisée|Acceptée|Expirée|Révoquée)['"`]""", text, re.I)]


@pytest.mark.parametrize('path', PAGES)
def test_no_page_labels_a_status_utilisee(path):
    target = WEB_APP / path
    if not target.exists():
        pytest.skip(f'{path} absent')
    text = target.read_text(encoding='utf-8')
    offenders = [label for label in status_labels(text) if label.lower().startswith('utilis')]
    assert offenders == [], f"{path} : « Utilisée » comme statut : {offenders}"


@pytest.mark.parametrize('path', ('static/js/admin.js', 'static/js/profile-invitations.js'))
def test_the_four_statuses_use_the_same_four_words_in_the_admin_and_the_profile(path):
    text = (WEB_APP / path).read_text(encoding='utf-8')
    for word in ('En attente', 'Acceptée', 'Expirée', 'Révoquée'):
        assert re.search(re.escape(word), text, re.I), f'{path} : « {word} » absent'


def test_the_status_mapping_is_not_duplicated_with_a_different_vocabulary():
    words = {}
    for path in ('static/js/admin.js', 'static/js/profile-invitations.js'):
        text = (WEB_APP / path).read_text(encoding='utf-8')
        found = re.findall(r"""used\s*:\s*['"`]([^'"`]+)['"`]""", text)
        words[path] = {w.lower() for w in found}
    assert all(values <= {'acceptée'} for values in words.values()), json.dumps({k: sorted(v) for k, v in words.items()})
