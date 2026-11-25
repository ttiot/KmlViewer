"""
Comptes SSO du banc (mode oidc de test, IdP factice jamais joint) : crée deux comptes SSO NON admin avec une adresse, l'un avec
1 crédit d'invitation et l'autre sans, et écrit dans $SSO_OUT/<SSO_NAME>.json leur cookie de session, signé par l'application
(SECRET_KEY fixe du banc, base jetable : jamais un secret). Le navigateur du banc les pose sans passer par l'IdP.
"""

import json
import os
import time

from app import create_app, db as database
from app.auth import users
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
conn.close()
with open(os.path.join(os.environ['SSO_OUT'], f"{os.environ['SSO_NAME']}.json"), 'w', encoding='utf-8') as handle:
    json.dump(cookies, handle)
print("comptes SSO du banc prêts")
