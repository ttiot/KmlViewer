"""
Prépare la base JETABLE du banc navigateur (lancé dans le conteneur de l'application, avant gunicorn).
Profils (BENCH_PROFILE) :
  main      moteur de routage local, fonction bêta ouverte à tous, carte centrée sur la zone couverte, lignes d'audit lisibles
  settings  état EXACT du CA-72 : en base, seulement `map.zoom` (la variable d'env DEM_TIMEOUT=30 est posée par run.sh)
  registry  comme `settings`, avec le registre de test (réglages `list`, `requires_restart` et `secret`, CA-78)
  coverage  mini-carte « Couverture en service » (CA-86, lot Z3) : ROUTING_ZONE_BUILD=1 (run.sh), trois fonds de carte
            (un autorisé, un dont un sous-domaine est hors de `tiles=`, un d'un hôte hors liste),
            zone active (pentagone) ≠ zone demandée (rectangle par défaut)
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
elif PROFILE == 'coverage':
    store.tiles_allowed = lambda hosts: True               # fonds hors liste aussi : l'état que l'éditeur refuserait
    store.set_many(conn, {'map.zoom': 9, 'map.default_basemap': 'banc', 'map.basemaps': [
        {'name': 'banc', 'url': 'https://{s}.tiles.exemple.fr/banc/{z}/{x}/{y}.png', 'subdomains': 'abc',
         'attribution': '© Fond du banc', 'max_zoom': 18},
        {'name': 'partiel', 'url': 'https://{s}.tiles.exemple.fr/partiel/{z}/{x}/{y}.png', 'subdomains': 'abcd',
         'attribution': '© Fond partiel', 'max_zoom': 18},
        {'name': 'etranger', 'url': 'https://tuiles.autre-hote.invalid/{z}/{x}/{y}.png', 'subdomains': 'abc',
         'attribution': '© Fond étranger', 'max_zoom': 18}]}, actor=actor)
    # Zone active = pentagone ; zone demandée = `routing.coverage_polygon` au défaut (rectangle) : les deux zones diffèrent
    conn.execute("UPDATE routing_coverage_state SET active_polygon = ?, status = 'pending', reason = 'zone', "
                 "status_at = datetime('now'), request_id = 1, requested_at = datetime('now') WHERE id = 1",
                 ('[[2,49],[5,49],[6,50.5],[3.5,52],[1,50.5]]',))
else:
    store.set_many(conn, {'map.zoom': 9}, actor=actor)
conn.commit()
conn.close()
print(f"base du banc prête (profil {PROFILE})")
