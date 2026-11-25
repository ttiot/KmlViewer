"""
Comptes SSO du banc (mode oidc de test, IdP factice jamais joint) : crée deux comptes SSO NON admin avec une adresse, l'un avec
1 crédit d'invitation et l'autre sans, et écrit dans $SSO_OUT/<SSO_NAME>.json leur cookie de session, signé par l'application
(SECRET_KEY fixe du banc, base jetable : jamais un secret). Le navigateur du banc les pose sans passer par l'IdP.
"""

import json
import os
import time

from app import create_app, db as database
from app.auth import invitations, users
from app.config import config

app = create_app(config[os.environ.get('FLASK_ENV', 'production')])
conn = database.connect(app.config['DB_PATH'])
serializer = app.session_interface.get_signing_serializer(app)
cookies = {}
for name, credits in (('sso-credit', 1), ('sso-nocredit', 0)):
    row = users.upsert_from_claims(conn, {'sub': f'idp-{name}', 'preferred_username': name, 'email': f'{name}@example.org',
                                          'email_verified': True})
    conn.execute("UPDATE users SET invite_credits = ? WHERE id = ?", (credits, row['id']))
    conn.commit()
    cookies[name] = serializer.dumps({'user_id': row['id'], 'token': users.session_token(conn, row['id']),
                                      'login_at': int(time.time()), '_permanent': True})
if os.environ['SSO_NAME'] == 'oidc':
    # Onglet Invitations de l'administration (EF-3.13, CA-84) : un admin SSO, un émetteur, des invitations dans tous les états
    PASSWORD = 'cheval-agrafe-lune-42'
    admin = users.upsert_from_claims(conn, {'sub': 'idp-sso-admin', 'preferred_username': 'sso-admin',
                                            'email': 'sso-admin@example.org', 'email_verified': True})
    conn.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (admin['id'],))

    def member(name, credits=0):
        row = users.upsert_from_claims(conn, {'sub': f'idp-{name}', 'preferred_username': name, 'email': f'{name}@example.org',
                                              'email_verified': True})
        conn.execute("UPDATE users SET invite_credits = ? WHERE id = ?", (credits, row['id']))
        conn.commit()
        return conn.execute("SELECT * FROM users WHERE id = ?", (row['id'],)).fetchone()

    def cookie_of(row):
        return serializer.dumps({'user_id': row['id'], 'token': users.session_token(conn, row['id']),
                                 'login_at': int(time.time()), '_permanent': True})

    admin = conn.execute("SELECT * FROM users WHERE id = ?", (admin['id'],)).fetchone()
    issuer = member('sso-issuer', 3)
    invite_x = invitations.create(conn, issuer, 'x@exemple.fr')                         # en attente, émise par sso-issuer
    invitations.create(conn, admin, 'y@exemple.fr')                                    # en attente, émise par l'admin
    invitations.create(conn, admin, 'revoquee@exemple.fr')                             # révoquée
    invitations.create(conn, admin, 'expiree@exemple.fr')                              # expirée (date forcée)
    for address, account in (('acceptee@exemple.fr', 'bob'), ('supprimee@exemple.fr', 'carol')):
        assert invitations.accept(conn, invitations.create(conn, admin, address).token, account, PASSWORD)
    dave = invitations.create(conn, issuer, 'dave@exemple.fr')                         # acceptée, visible du profil de l'émetteur
    assert invitations.accept(conn, dave.token, 'dave', PASSWORD)
    ids = {r['email']: r['id'] for r in conn.execute("SELECT id, email FROM auth_tokens WHERE kind = 'invite'")}
    assert invitations.revoke(conn, admin, ids['revoquee@exemple.fr'])
    conn.execute("UPDATE auth_tokens SET expires_at = '2020-01-01T00:00:00Z' WHERE id = ?", (ids['expiree@exemple.fr'],))
    conn.execute("UPDATE auth_tokens SET revoked_at = '2026-01-01T00:00:00Z' WHERE id = ?",
                 (ids['acceptee@exemple.fr'],))                                         # acceptée PUIS révoquée
    gone = member('sso-gone', 1)
    invitations.create(conn, gone, 'orpheline@exemple.fr')
    conn.execute("DELETE FROM users WHERE id = ?", (gone['id'],))                      # émetteur supprimé : created_by à NULL
    conn.execute("DELETE FROM users WHERE username = 'carol'")                         # compte créé supprimé : used_by à NULL
    markup = member('sso-markup', 1)
    invitations.create(conn, markup, 'balise@exemple.fr')
    conn.execute("UPDATE users SET username = '<b>x</b>' WHERE id = ?", (markup['id'],))
    conn.commit()
    cookies['sso-admin'] = cookie_of(admin)
    cookies['sso-issuer'] = cookie_of(issuer)
    assert invite_x
conn.close()
with open(os.path.join(os.environ['SSO_OUT'], f"{os.environ['SSO_NAME']}.json"), 'w', encoding='utf-8') as handle:
    json.dump(cookies, handle)
print("comptes SSO du banc prêts")
