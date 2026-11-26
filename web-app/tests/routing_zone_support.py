"""
Outillage commun des tests du lot Z2 (routing-zone-donnees : catalogue, couverture, état, CLI `flask routing-zone`, API admin).

- `frozen_document()` : copie FIGÉE de l'index Geofabrik (index-v1.json du 2026-11-26) réduite aux feuilles d'Europe de l'Ouest
  (74 feuilles proches de la zone par défaut, ancêtres sans contour), contours simplifiés à 0,01°, `size` = Content-Length réel des
  fichiers `-latest.osm.pbf` à la même date. C'est le « catalogue de test figé (contours et tailles) » de CA-ZD2 / ZD-6.
- `synthetic(...)` : catalogue fait main (rectangles) quand le test doit connaître la géométrie exacte (aires, pourcentages).
- Horloges : jamais l'heure réelle dans une assertion de délai ; les tests de l'état passent `now` explicitement.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.audit import Actor
from app.services.routing import coverage as geometry
from app.services.routing import coverage_state as coverage
from app.services.routing import zone
from tests.pw_harness import connect, make_app

FROZEN = Path(__file__).resolve().parent / 'fixtures' / 'routing' / 'geofabrik-index-frozen.json'
HOST = 'https://download.geofabrik.de'
CLI = Actor.cli()
T0 = datetime(2026, 11, 26, 12, 0, 0)
GRAPH = 'graph-20261126120000'
GRAPH2 = 'graph-20261127120000'

DEFAULT_ZONE = [[1.35, 48.83], [6.42, 48.83], [6.42, 51.55], [1.35, 51.55]]
LILLE_ZONE = [[3.0, 50.4], [3.4, 50.4], [3.4, 50.8], [3.0, 50.8]]
NORMANDIE_ZONE = [[1.35, 48.83], [6.42, 48.83], [6.42, 51.55], [1.35, 51.55], [1.35, 49.75], [-0.5, 49.4], [-0.5, 49.05], [1.35, 49.05]]

# Les 17 régions de la zone par défaut contre la copie figée (calcul exact, ZD-6) : identiques aux 17 régions de l'échantillonnage du §3.
DEFAULT_REGIONS = ['belgium', 'centre', 'champagne-ardenne', 'duesseldorf-regbez', 'haute-normandie', 'ile-de-france', 'kent',
                   'koeln-regbez', 'limburg', 'lorraine', 'luxembourg', 'noord-brabant', 'nord-pas-de-calais', 'picardie',
                   'rheinland-pfalz', 'saarland', 'zeeland']


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def frozen_document() -> dict:
    return json.loads(FROZEN.read_text(encoding='utf-8'))


def raw(document) -> bytes:
    return json.dumps(document, separators=(',', ':')).encode()


def ring(lon0, lat0, lon1, lat1):
    return [[lon0, lat0], [lon1, lat0], [lon1, lat1], [lon0, lat1], [lon0, lat0]]


def feature(rid, parent, geometry=None, *, size=None, url=True):
    props = {'id': rid, 'parent': parent}
    if url:
        props['urls'] = {'pbf': f'{HOST}/europe/{rid}-latest.osm.pbf'}
    if size is not None:
        props['size'] = size
    return {'type': 'Feature', 'properties': props, 'geometry': geometry}


def rect(rid, lon0, lat0, lon1, lat1, *, size=100_000_000, parent='europe', hole=None):
    """Région rectangulaire (lon/lat) ; `hole` = (lon0, lat0, lon1, lat1) d'un trou."""
    rings = [ring(lon0, lat0, lon1, lat1)]
    if hole:
        rings.append(ring(*hole)[::-1])
    return feature(rid, parent, {'type': 'Polygon', 'coordinates': rings}, size=size)


def synthetic(*features, root='europe') -> dict:
    return {'type': 'FeatureCollection', 'features': [feature(root, None, url=False), *features]}


def zone_app(tmp_path, *, build=True, **extra):
    """Application complète (mode `password`) avec `ROUTING_ZONE_BUILD`, moteur autorisé."""
    return make_app(tmp_path, ROUTING_ZONE_BUILD=build, OUTBOUND_ALLOWED_HOSTS='routing=engine.test:8989', **extra)


def load_catalog(conn, app, document, now=T0):
    return coverage.load_catalog_document(conn, raw(document), app.config, now)


def reset_state(conn):
    """État « sources historiques », statut `idle`, aucune demande : le point de départ d'un test de la machine à états."""
    conn.execute("DELETE FROM routing_coverage_state")
    conn.execute("INSERT INTO routing_coverage_state (id) VALUES (1)")
    conn.commit()


def set_active(conn, polygon, now=T0, *, graph=GRAPH, regions=('belgium',), requested=None):
    """Zone active posée comme le fait l'hôte : par `report` (zone.json du graphe en service)."""
    info = {'polygon': [[float(a), float(b)] for a, b in polygon], 'extract_bbox': geometry.extract_bbox(polygon, 10),
            'regions': list(regions)}
    return coverage.report(conn, graph, info, False, None, requested if requested is not None else polygon, now)


def after(seconds, base=T0):
    return base + timedelta(seconds=seconds)


def routing_audit(conn, since=0):
    """[(acteur, action)] des entrées `routing.*` après l'identifiant `since`, dans l'ordre."""
    return [(r['actor_type'], r['action']) for r in conn.execute(
        "SELECT actor_type, action FROM audit_log WHERE id > ? AND action LIKE 'routing.%' ORDER BY id", (since,))]


def audit_mark(conn) -> int:
    return conn.execute("SELECT COALESCE(MAX(id), 0) FROM audit_log").fetchone()[0]


def state_row(conn) -> dict:
    return dict(conn.execute("SELECT * FROM routing_coverage_state WHERE id = 1").fetchone())


def catalog_row(conn):
    row = conn.execute("SELECT * FROM routing_catalog WHERE id = 1").fetchone()
    return dict(row) if row else None


def conn_of(app):
    return connect(app)


def expected_tiles(bbox):
    """Dalles 1° × 1° dont le carré coupe l'emprise (ZD-7), écrites sans le code testé."""
    lon_min, lat_min, lon_max, lat_max = bbox
    out = set()
    lat = int(lat_min // 1)
    while lat < lat_max:
        lon = int(lon_min // 1)
        while lon < lon_max:
            out.add((lat, lon))
            lon += 1
        lat += 1
    return out


def envelope(vertices):
    return zone.envelope_lonlat(vertices)
