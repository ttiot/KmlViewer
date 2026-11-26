"""
Catalogue des données OSM (routing-zone-donnees ZD-6, ZD-10 `catalog -`, §6). Document TIERS (index Geofabrik complété des
tailles par l'hôte) : taille bornée AVANT toute analyse, forme, hôte, chemin, extension, sous-arbre et exclusions contrôlés ;
ses contours vont à GEOS : au plus 10 000 sommets par région et 500 000 au total, coordonnées finies et dans les bornes.

Entrée : `{"features": [{"properties": {"id", "parent", "urls": {"pbf"}, "size"}, "geometry": {Polygon | MultiPolygon}}]}`.
Sortie stockée : uniquement les FEUILLES candidates (sous-arbre `root`, moins `exclude`), contours corrigés (`make_valid`) et
simplifiés, au format `[{"id", "url", "size", "geometry"}]`. Module sans base ni env : l'appelant fournit les paramètres.
"""

import hashlib
import json
import math
import re
from typing import Sequence

from shapely import make_valid
from shapely.geometry import MultiPolygon, Polygon, mapping, shape

from app.services.routing import zone
from app.services.routing.coverage import Region

MAX_CATALOG_BYTES = 16 * 1024 * 1024
MAX_VERTICES_PER_REGION = 10_000
MAX_VERTICES_TOTAL = 500_000
MAX_REGION_BYTES = 2 ** 40                      # 1 Tio : borne de bon sens d'une taille d'extrait
SIMPLIFY_DEGREES = 0.005                        # ≈ 500 m : les contours Geofabrik débordent déjà des frontières
_ID = re.compile(r'[a-z0-9][a-z0-9_-]{0,63}')
_PATH = re.compile(r'/[A-Za-z0-9._/-]{1,200}')


class CatalogError(ValueError):
    """Catalogue refusé : le message est affichable, l'ancien catalogue est gardé."""


def parse_bytes(raw: bytes) -> object:
    """JSON du catalogue, taille bornée AVANT l'analyse."""
    if len(raw) > MAX_CATALOG_BYTES:
        raise CatalogError("catalogue trop volumineux")
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):
        raise CatalogError("catalogue illisible") from None


def _finite_number(value) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return not (isinstance(value, float) and not math.isfinite(value))


def _vertices_of(coordinates) -> int:
    """Nombre de sommets d'un Polygon / MultiPolygon, en vérifiant la forme ; lève CatalogError."""
    count = 0
    try:
        for ring in coordinates:
            for point in ring:
                if len(point) != 2 or not all(_finite_number(n) for n in point):
                    raise CatalogError("contour invalide")
                if not (-180 <= point[0] <= 180 and -90 <= point[1] <= 90):
                    raise CatalogError("contour hors des bornes")
                count += 1
    except (TypeError, ValueError):
        raise CatalogError("contour invalide") from None
    return count


def _geometry(raw, budget: int):
    """(géométrie shapely valide et simplifiée en lon/lat, nombre de sommets lus)."""
    if not isinstance(raw, dict) or raw.get('type') not in ('Polygon', 'MultiPolygon'):
        raise CatalogError("contour : Polygon ou MultiPolygon attendu")
    coordinates = raw.get('coordinates')
    if not isinstance(coordinates, list):
        raise CatalogError("contour invalide")
    polygons = [coordinates] if raw['type'] == 'Polygon' else coordinates
    count = 0
    for polygon in polygons:
        if not isinstance(polygon, list):
            raise CatalogError("contour invalide")
        count += _vertices_of(polygon)
        if count > min(MAX_VERTICES_PER_REGION, budget):
            raise CatalogError("contour trop détaillé")
    try:
        geometry = shape(raw)
    except (ValueError, TypeError, AttributeError):
        raise CatalogError("contour invalide") from None
    if not geometry.is_valid:
        geometry = make_valid(geometry)
    parts = []
    for member in getattr(geometry, 'geoms', [geometry]):          # make_valid peut renvoyer une collection
        parts.extend(getattr(member, 'geoms', [member]) if isinstance(member, (Polygon, MultiPolygon)) else [])
    parts = [p for p in parts if isinstance(p, Polygon) and not p.is_empty]
    if not parts:
        raise CatalogError("contour sans surface")
    geometry = MultiPolygon(parts)
    simple = geometry.simplify(SIMPLIFY_DEGREES, preserve_topology=True)
    return (simple if not simple.is_empty and simple.is_valid else geometry), count


def _check_url(url, allowed_hosts: Sequence) -> str:
    if not isinstance(url, str) or len(url) > 300 or not url.startswith('https://'):
        raise CatalogError("URL : https obligatoire")
    host, _, path = url[len('https://'):].partition('/')
    path = '/' + path
    if host not in allowed_hosts:
        raise CatalogError("URL : hôte non autorisé")
    if not _PATH.fullmatch(path) or '..' in path or '//' in path or not path.endswith('.osm.pbf'):
        raise CatalogError("URL : chemin invalide")
    return url


def build(document, *, root: str, exclude: Sequence, allowed_hosts: Sequence) -> list:
    """
    Catalogue contrôlé (document déjà décodé) -> `[{"id", "url", "size", "geometry"}]` trié par identifiant : les feuilles du
    sous-arbre `root` moins `exclude`. Lève CatalogError sans rien écrire.
    """
    features = document.get('features') if isinstance(document, dict) else None
    if not isinstance(features, list) or not features or len(features) > 5000:
        raise CatalogError("catalogue : liste de régions attendue")
    # Forme contrôlée sur TOUT le document ; identifiants (gabarit, unicité) et parents seulement sur le sous-arbre de `root` :
    # l'index réel porte des entrées hors Europe (`us/alabama`…) qui n'ont pas à passer le gabarit et sont simplement ignorées.
    nodes, dups, total = {}, set(), 0
    links = []                                          # (id, parent) de TOUTES les occurrences : l'ordre n'y change rien
    for feature in features:
        props = feature.get('properties') if isinstance(feature, dict) else None
        if not isinstance(props, dict):
            raise CatalogError("région invalide")
        rid, parent = props.get('id'), props.get('parent')
        if not isinstance(rid, str) or len(rid) > 128:
            raise CatalogError("identifiant de région invalide ou en double")
        parent = parent if isinstance(parent, str) else None
        links.append((rid, parent))
        if rid in nodes:
            dups.add(rid)
            continue
        nodes[rid] = (parent, props, feature.get('geometry'))
    children = {}
    for rid, parent in links:
        children.setdefault(parent, []).append(rid)
    if root not in nodes:
        raise CatalogError("racine du catalogue absente")
    wanted, stack, seen = [], [root], {root}
    while stack:
        current = stack.pop()
        for child in children.get(current, []):
            if child in seen:                           # cycle (dont un retour à la racine) : charge non bornée
                raise CatalogError("catalogue : arborescence invalide (cycle)")
            seen.add(child)
            (stack if child in children else wanted).append(child)
    for rid in seen:                                    # contrôles d'identité, sur le sous-arbre seulement
        if not _ID.fullmatch(rid) or rid in dups:
            raise CatalogError("identifiant de région invalide ou en double")
        parent = nodes[rid][0]
        if rid != root and (parent is None or not _ID.fullmatch(parent)):
            raise CatalogError("parent de région invalide")
    out = []
    for rid in sorted(set(wanted) - set(exclude)):
        _, props, geometry = nodes[rid]
        size = props.get('size')
        if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= MAX_REGION_BYTES:
            raise CatalogError("taille d'extrait invalide")
        urls = props.get('urls')
        url = _check_url(urls.get('pbf') if isinstance(urls, dict) else None, allowed_hosts)
        shaped, count = _geometry(geometry, MAX_VERTICES_TOTAL - total)
        total += count
        if total > MAX_VERTICES_TOTAL:
            raise CatalogError("catalogue trop détaillé")
        out.append({'id': rid, 'url': url, 'size': size, 'geometry': mapping(shaped)})
    if not out:
        raise CatalogError("catalogue sans région candidate")
    return out


def serialize(regions: Sequence) -> tuple:
    """(texte JSON compact, sha256) du catalogue stocké."""
    text = json.dumps(regions, separators=(',', ':'), sort_keys=True)
    return text, hashlib.sha256(text.encode()).hexdigest()


_PREPARED: dict = {}


def prepare(text: str, digest: str) -> list:
    """Régions prêtes pour le calcul (contours en Web Mercator), gardées en mémoire du processus par empreinte (ZD-6)."""
    cached = _PREPARED.get(digest)
    if cached is not None:
        return cached
    def ring(coords):
        return [zone.project(lon, max(-zone.MAX_LAT, min(zone.MAX_LAT, lat))) for lon, lat in coords]

    regions = []
    for item in json.loads(text):
        geometry = shape(item['geometry'])
        projected = MultiPolygon([Polygon(ring(poly.exterior.coords), [ring(r.coords) for r in poly.interiors])
                                  for poly in getattr(geometry, 'geoms', [geometry])])
        if not projected.is_valid:
            projected = make_valid(projected)
        if projected.geom_type not in ('Polygon', 'MultiPolygon'):      # make_valid peut réduire un contour à une ligne
            polys = [g for g in getattr(projected, 'geoms', []) if g.geom_type in ('Polygon', 'MultiPolygon')]
            if not polys:
                continue
            projected = MultiPolygon([q for g in polys for q in getattr(g, 'geoms', [g])])
        regions.append(Region(item['id'], item['url'], item['size'], projected))
    _PREPARED.clear()                                    # un seul catalogue vivant
    _PREPARED[digest] = regions
    return regions
