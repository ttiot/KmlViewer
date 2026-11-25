"""
Prépare la base JETABLE du banc navigateur (lancé dans le conteneur de l'application, avant gunicorn) : moteur de routage local,
fonction bêta ouverte à tous, carte centrée sur la zone couverte, quelques lignes d'audit lisibles.
Mode `local` : le compte « local » est administrateur (aucune authentification, c'est un banc de test).
"""

import os

from app import create_app, db as database
from app import audit, features
from app.config import config

app = create_app(config[os.environ.get('FLASK_ENV', 'production')])
conn = database.connect(app.config['DB_PATH'])
actor = audit.Actor.cli()
store = app.config['SETTINGS_STORE']
store.set_many(conn, {'routing.base_url': os.environ.get('BENCH_ENGINE_URL', 'http://127.0.0.1:8989'),
                      'map.center_lat': 50.63, 'map.center_lon': 3.05, 'map.zoom': 11}, actor=actor)
features.set_state(conn, 'route_builder', 'all', actor=actor)
for name in ('alice', 'bob'):
    audit.record(conn, 'user.create', actor=actor, target_type='user', target_id=name,
                 details={'username': name, 'source': 'banc navigateur'})
conn.commit()
conn.close()
print("base du banc prête")
