"""
Prépare la base JETABLE du banc navigateur (lancé dans le conteneur de l'application, avant gunicorn).
Profils (BENCH_PROFILE) :
  main      moteur de routage local, fonction bêta ouverte à tous, carte centrée sur la zone couverte, lignes d'audit lisibles
  settings  état EXACT du CA-72 : en base, seulement `map.zoom` (la variable d'env DEM_TIMEOUT=30 est posée par run.sh)
  registry  comme `settings`, avec le registre de test (réglages `list`, `requires_restart` et `secret`, CA-78)
Mode `local` : le compte « local » est administrateur (aucune authentification, c'est un banc de test).
"""

import os

PROFILE = os.environ.get('BENCH_PROFILE', 'main')
if PROFILE == 'registry':
    import bench_registry  # noqa: F401  (avant create_app : le store lit le registre à sa création)

from app import create_app, db as database  # noqa: E402
from app import audit, features  # noqa: E402
from app.config import config  # noqa: E402

app = create_app(config[os.environ.get('FLASK_ENV', 'production')])
conn = database.connect(app.config['DB_PATH'])
actor = audit.Actor.cli()
store = app.config['SETTINGS_STORE']
if PROFILE == 'main':
    store.set_many(conn, {'routing.base_url': os.environ.get('BENCH_ENGINE_URL', 'http://127.0.0.1:8989'),
                          'map.center_lat': 50.63, 'map.center_lon': 3.05, 'map.zoom': 11}, actor=actor)
    features.set_state(conn, 'route_builder', 'all', actor=actor)
    for name in ('alice', 'bob'):
        audit.record(conn, 'user.create', actor=actor, target_type='user', target_id=name,
                     details={'username': name, 'source': 'banc navigateur'})
else:
    store.set_many(conn, {'map.zoom': 9}, actor=actor)
conn.commit()
conn.close()
print(f"base du banc prête (profil {PROFILE})")
