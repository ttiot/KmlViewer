"""
Catalogue des données OSM (rôle test) : routing-zone-donnees ZD-2 (table `routing_catalog`), ZD-6 (candidates), ZD-10 (`catalog -`,
format d'entrée), §6 (document tiers : contrôles avant GEOS). Écrit depuis la spec ; la CLI a ses propres tests
(test_routing_zone_cli.py), ici le contrôle lui-même et son effet sur la base.

 A. contenu stocké : feuilles du sous-arbre, exclusions, tri, forme, empreinte
 B. refus : taille, JSON, forme, identifiants, URL (https, hôte, chemin, extension), tailles, contours, sommets, bornes
 C. contours : invalide corrigé, trou, MultiPolygon, plafonds de sommets par région et au total
 D. base : remplacement en une transaction, ancien catalogue gardé, même empreinte, audit, rechargement par empreinte
"""

import copy
import json
import math
import sqlite3
import threading
from datetime import timedelta

import pytest

from app.services.routing import catalog, coverage_state as coverage
from tests.routing_zone_support import (T0, audit_mark, catalog_row, feature, frozen_document, raw, rect, reset_state,
                                        routing_audit, synthetic, zone_app)
from tests.pw_harness import connect

HOSTS = ['download.geofabrik.de']
EXCLUDE = ['alps', 'britain-and-ireland', 'dach', 'great-britain', 'guadeloupe', 'martinique', 'mayotte', 'reunion']


def build(document, **kw):
    kw.setdefault('root', 'europe')
    kw.setdefault('exclude', EXCLUDE)
    kw.setdefault('allowed_hosts', HOSTS)
    return catalog.build(document, **kw)


def simple_document():
    return synthetic(rect('aa', 0, 0, 2, 2, size=10), rect('bb', 2, 0, 4, 2, size=20))


# ===================================================== A. contenu stocké ======================================================

def test_only_the_leaves_of_the_subtree_are_kept_exclusions_removed_sorted_by_identifier():
    document = synthetic(
        feature('france', 'europe'), rect('lorraine', 5, 48, 8, 50, size=3, parent='france'), rect('alsace', 7, 47, 8, 49, size=4, parent='france'),
        rect('great-britain', -5, 50, 2, 58, size=99), rect('belgium', 2, 49, 6, 51.5, size=5),
        rect('mayotte', 45, -13, 46, -12, size=7, parent='france'),
        feature('asia', None, url=False), rect('japan', 130, 30, 140, 40, size=9, parent='asia'))
    regions = build(document)
    assert [r['id'] for r in regions] == ['alsace', 'belgium', 'lorraine']
    assert all(set(r) == {'id', 'url', 'size', 'geometry'} for r in regions)
    assert {r['id']: r['size'] for r in regions} == {'alsace': 4, 'belgium': 5, 'lorraine': 3}
    assert regions[0]['url'] == 'https://download.geofabrik.de/europe/alsace-latest.osm.pbf'


def test_a_custom_root_and_a_custom_exclusion_list_are_applied():
    document = synthetic(feature('france', 'europe'), rect('lorraine', 5, 48, 8, 50, size=3, parent='france'),
                         rect('alsace', 7, 47, 8, 49, size=4, parent='france'), rect('belgium', 2, 49, 6, 51.5, size=5))
    assert [r['id'] for r in build(document, root='france')] == ['alsace', 'lorraine']
    assert [r['id'] for r in build(document, exclude=['alsace'])] == ['belgium', 'lorraine']
    with pytest.raises(catalog.CatalogError):
        build(document, root='inconnue')


def test_the_stored_text_and_the_fingerprint_depend_on_the_content_only_not_on_the_input_order():
    document = simple_document()
    swapped = copy.deepcopy(document)
    swapped['features'].reverse()
    first, second = catalog.serialize(build(document)), catalog.serialize(build(swapped))
    assert first == second and len(first[1]) == 64
    smaller = simple_document()
    smaller['features'][1]['properties']['size'] = 11
    assert catalog.serialize(build(smaller))[1] != first[1]


def test_the_other_fields_of_the_published_index_are_ignored_and_never_stored():
    document = simple_document()
    document['features'][1]['properties'].update({'name': 'x' * 50, 'urls': {'pbf': 'https://download.geofabrik.de/europe/aa-latest.osm.pbf',
                                                                            'shp': 'https://evil.example/x.zip', 'poly': 'ftp://nope'}})
    text, _ = catalog.serialize(build(document))
    assert 'evil.example' not in text and 'ftp:' not in text and 'xxxx' not in text


def test_the_frozen_copy_of_the_real_index_is_accepted_and_gives_70_candidates_within_the_size_bounds():
    stored = build(frozen_document())
    assert len(stored) == 70
    text, _ = catalog.serialize(stored)
    assert len(text) < 1_000_000 and not {'great-britain', 'alps', 'dach', 'britain-and-ireland'} & {r['id'] for r in stored}


# ===================================================== B. refus ===============================================================

def with_props(**changes):
    document = simple_document()
    document['features'][1]['properties'].update(changes)
    return document


def with_geometry(geometry):
    document = simple_document()
    document['features'][1]['geometry'] = geometry
    return document


@pytest.mark.parametrize('label, document', [
    ('liste vide', {'features': []}), ('pas de features', {}), ('liste', []), ('chaîne', 'x'), ('features pas une liste', {'features': {}}),
    ('feature pas un objet', {'features': [1]}), ('propriétés absentes', {'features': [{'geometry': None}]}),
    ('identifiant manquant', synthetic({'properties': {'parent': 'europe', 'size': 1}, 'geometry': None})),
    ('identifiant en double', synthetic(rect('aa', 0, 0, 1, 1, size=1), rect('aa', 1, 0, 2, 1, size=1))),
    ('identifiant à majuscule', with_props(id='AA')), ('identifiant avec /', with_props(id='a/b')), ('identifiant avec ..', with_props(id='..')),
    ('identifiant avec espace', with_props(id='a b')), ('identifiant trop long', with_props(id='a' * 65)),
    ('parent invalide', with_props(parent='A/B')),
    ('url http', with_props(urls={'pbf': 'http://download.geofabrik.de/europe/bb-latest.osm.pbf'})),
    ('url autre hôte', with_props(urls={'pbf': 'https://evil.example/europe/bb-latest.osm.pbf'})),
    ('url hôte préfixé', with_props(urls={'pbf': 'https://download.geofabrik.de.evil.example/europe/bb-latest.osm.pbf'})),
    ('url identifiants', with_props(urls={'pbf': 'https://download.geofabrik.de@evil.example/x.osm.pbf'})),
    ('url port', with_props(urls={'pbf': 'https://download.geofabrik.de:8443/europe/bb-latest.osm.pbf'})),
    ('url chemin ..', with_props(urls={'pbf': 'https://download.geofabrik.de/europe/../etc/bb.osm.pbf'})),
    ('url chemin //', with_props(urls={'pbf': 'https://download.geofabrik.de//europe/bb.osm.pbf'})),
    ('url mauvaise extension', with_props(urls={'pbf': 'https://download.geofabrik.de/europe/bb-latest.zip'})),
    ('url requête', with_props(urls={'pbf': 'https://download.geofabrik.de/europe/bb.osm.pbf?x=1'})),
    ('url retour ligne', with_props(urls={'pbf': 'https://download.geofabrik.de/europe/bb.osm.pbf\n'})),
    ('url espace', with_props(urls={'pbf': 'https://download.geofabrik.de/europe/b b.osm.pbf'})),
    ('url ; commande', with_props(urls={'pbf': 'https://download.geofabrik.de/europe/b;rm.osm.pbf'})),
    ('url $()', with_props(urls={'pbf': 'https://download.geofabrik.de/europe/$(touch)x.osm.pbf'})),
    ('url absente', with_props(urls={})), ('url pas une chaîne', with_props(urls={'pbf': 5})), ('urls pas un objet', with_props(urls='x')),
    ('taille négative', with_props(size=-1)), ('taille absente', with_props(size=None)), ('taille chaîne', with_props(size='5')),
    ('taille décimale', with_props(size=1.5)), ('taille booléenne', with_props(size=True)), ('taille NaN', with_props(size=float('nan'))),
    ('contour absent', with_geometry(None)), ('contour Point', with_geometry({'type': 'Point', 'coordinates': [1, 1]})),
    ('contour sans coordonnées', with_geometry({'type': 'Polygon'})),
    ('coordonnées pas une liste', with_geometry({'type': 'Polygon', 'coordinates': 'x'})),
    ('sommet à trois valeurs', with_geometry({'type': 'Polygon', 'coordinates': [[[0, 0, 5], [1, 0, 5], [1, 1, 5], [0, 0, 5]]]})),
    ('sommet texte', with_geometry({'type': 'Polygon', 'coordinates': [[['a', 0], [1, 0], [1, 1], [0, 0]]]})),
    ('sommet booléen', with_geometry({'type': 'Polygon', 'coordinates': [[[True, 0], [1, 0], [1, 1], [0, 0]]]})),
    ('longitude 181', with_geometry({'type': 'Polygon', 'coordinates': [[[181, 0], [182, 0], [182, 1], [181, 0]]]})),
    ('latitude 91', with_geometry({'type': 'Polygon', 'coordinates': [[[0, 91], [1, 91], [1, 92], [0, 91]]]})),
    ('coordonnée NaN', with_geometry({'type': 'Polygon', 'coordinates': [[[float('nan'), 0], [1, 0], [1, 1], [float('nan'), 0]]]})),
    ('coordonnée infinie', with_geometry({'type': 'Polygon', 'coordinates': [[[float('inf'), 0], [1, 0], [1, 1], [0, 0]]]})),
    ('anneau vide', with_geometry({'type': 'Polygon', 'coordinates': [[]]})),
    ('polygone sans anneau', with_geometry({'type': 'Polygon', 'coordinates': []})),
    ('contour sans surface', with_geometry({'type': 'Polygon', 'coordinates': [[[0, 0], [1, 0], [2, 0], [0, 0]]]})),
])
def test_an_invalid_document_is_refused_with_a_catalog_error_and_nothing_else(label, document):
    with pytest.raises(catalog.CatalogError):
        build(document)


def test_a_candidate_without_a_size_makes_the_whole_catalog_invalid_but_a_non_candidate_may_lack_one():
    document = simple_document()
    del document['features'][1]['properties']['size']
    with pytest.raises(catalog.CatalogError):
        build(document)
    document = synthetic(rect('aa', 0, 0, 2, 2, size=10), rect('great-britain', -5, 50, 2, 58, size=None))      # exclue : taille sans objet
    assert [r['id'] for r in build(document)] == ['aa']


def test_a_catalog_without_any_candidate_is_refused():
    with pytest.raises(catalog.CatalogError):
        build(synthetic(rect('great-britain', -5, 50, 2, 58, size=1)))


def test_the_size_is_bounded_before_any_analysis_and_unreadable_json_is_a_catalog_error():
    with pytest.raises(catalog.CatalogError):
        catalog.parse_bytes(b'{}' + b' ' * (16 * 1024 * 1024 - 1))                     # 16 Mio + 1 octet d'un JSON VALIDE : refusé sur la taille seule
    assert catalog.parse_bytes(b'{}' + b' ' * (16 * 1024 * 1024 - 2)) == {}               # 16 Mio exactement : lu
    for bad in (b'', b'{', b'\xff\xfe', b'[' * 100_000, b'{"a":' * 50_000):
        with pytest.raises(catalog.CatalogError):
            catalog.parse_bytes(bad)


# ===================================================== C. contours ============================================================

def circle(n, cx=5.0, cy=45.0, r=1.0, closed=True):
    points = [[cx + r * math.cos(2 * math.pi * i / n), cy + r * math.sin(2 * math.pi * i / n)] for i in range(n)]
    return points + [points[0]] if closed else points


def test_a_bow_tie_contour_is_repaired_or_the_region_dropped_never_stored_invalid():
    bow = {'type': 'Polygon', 'coordinates': [[[0, 0], [2, 2], [2, 0], [0, 2], [0, 0]]]}
    stored = build(with_geometry(bow))
    import shapely.geometry
    for region in stored:
        assert shapely.geometry.shape(region['geometry']).is_valid, region['id']


def test_a_multipolygon_and_a_polygon_with_a_hole_are_accepted():
    multi = {'type': 'MultiPolygon', 'coordinates': [[[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]], [[[3, 0], [4, 0], [4, 1], [3, 1], [3, 0]]]]}
    holed = {'type': 'Polygon', 'coordinates': [[[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]], [[4, 4], [4, 6], [6, 6], [6, 4], [4, 4]]]}
    assert [r['id'] for r in build(with_geometry(multi))] == ['aa', 'bb']
    assert [r['id'] for r in build(with_geometry(holed))] == ['aa', 'bb']


def test_a_region_of_10000_vertices_is_accepted_and_10001_is_refused_before_geos():
    assert [r['id'] for r in build(with_geometry({'type': 'Polygon', 'coordinates': [circle(9999)]}))] == ['aa', 'bb']       # 10 000 sommets fermés
    with pytest.raises(catalog.CatalogError):
        build(with_geometry({'type': 'Polygon', 'coordinates': [circle(10000)]}))                                           # 10 001


def test_the_vertex_count_is_summed_over_every_ring_and_every_part():
    rings = {'type': 'Polygon', 'coordinates': [circle(6000, r=2.0), circle(6000, r=1.0)]}
    parts = {'type': 'MultiPolygon', 'coordinates': [[circle(6000, cx=0.0)], [circle(6000, cx=10.0)]]}
    for geometry in (rings, parts):
        with pytest.raises(catalog.CatalogError):
            build(with_geometry(geometry))


def test_the_total_of_vertices_is_bounded_to_500000_for_the_whole_catalog():
    ok = synthetic(*[feature(f'r{i:02d}', 'europe', {'type': 'Polygon', 'coordinates': [circle(9_000, cx=i * 0.5)]}, size=1) for i in range(55)])
    build(ok)                                                                              # 55 × 9 001 = 495 055 sommets
    too_many = synthetic(*[feature(f'r{i:02d}', 'europe', {'type': 'Polygon', 'coordinates': [circle(9_000, cx=i * 0.5)]}, size=1)
                           for i in range(56)])
    with pytest.raises(catalog.CatalogError):
        build(too_many)                                                                    # 56 × 9 001 = 504 056 > 500 000


# ===================================================== D. base ================================================================

@pytest.fixture
def world(tmp_path):
    app = zone_app(tmp_path)
    conn = connect(app)
    reset_state(conn)
    yield app, conn
    conn.close()


def load(app, conn, document, now=T0):
    return coverage.load_catalog_document(conn, raw(document), app.config, now)


def test_a_catalog_is_stored_with_its_fingerprint_and_its_date_and_audited_once_by_the_system(world):
    app, conn = world
    mark = audit_mark(conn)
    assert load(app, conn, simple_document()) == {'regions': 2}
    row = catalog_row(conn)
    text, digest = catalog.serialize(build(simple_document()))
    assert row['content'] == text and row['sha256'] == digest and row['loaded_at'].startswith('2026-11-26T12:00:00')
    assert routing_audit(conn, mark) == [('system', 'routing.catalog_loaded')]
    assert conn.execute("SELECT COUNT(*) FROM routing_catalog").fetchone()[0] == 1


def test_the_same_content_is_not_audited_again_and_a_changed_one_replaces_the_old_in_place(world):
    app, conn = world
    load(app, conn, simple_document())
    mark = audit_mark(conn)
    load(app, conn, simple_document())
    assert routing_audit(conn, mark) == [] and catalog_row(conn)['content'] == catalog.serialize(build(simple_document()))[0]
    changed = synthetic(rect('aa', 0, 0, 2, 2, size=10), rect('bb', 2, 0, 4, 2, size=21), rect('cc', 4, 0, 6, 2, size=1))
    assert load(app, conn, changed) == {'regions': 3}
    assert routing_audit(conn, mark) == [('system', 'routing.catalog_loaded')]
    assert conn.execute("SELECT COUNT(*) FROM routing_catalog").fetchone()[0] == 1
    assert [r['id'] for r in json.loads(catalog_row(conn)['content'])] == ['aa', 'bb', 'cc']


def test_a_catalog_of_the_same_fingerprint_is_not_rewritten_content_fingerprint_and_date_stay_as_loaded(world):
    """ZD-2 : « un catalogue de même empreinte que l'actuel n'est pas réécrit » (TZ2a-n3)."""
    app, conn = world
    load(app, conn, simple_document(), T0)
    first = catalog_row(conn)
    mark = audit_mark(conn)
    assert load(app, conn, simple_document(), T0 + timedelta(hours=5)) == {'regions': 2}       # accepté : la commande répond comme avant
    assert catalog_row(conn) == first and routing_audit(conn, mark) == []
    reordered = copy.deepcopy(simple_document())
    reordered['features'].reverse()                                    # même contenu, autre ordre d'entrée : même empreinte
    load(app, conn, reordered, T0 + timedelta(hours=6))
    assert catalog_row(conn) == first
    changed = synthetic(rect('aa', 0, 0, 2, 2, size=10), rect('bb', 2, 0, 4, 2, size=21))
    load(app, conn, changed, T0 + timedelta(hours=7))                  # contrôle : un contenu différent, lui, est bien réécrit, date comprise
    assert catalog_row(conn)['sha256'] != first['sha256'] and catalog_row(conn)['loaded_at'] > first['loaded_at']


@pytest.mark.parametrize('bad', [b'', b'{', b'[]', b'{"features":[]}', b' ' * (16 * 1024 * 1024 + 1)])
def test_an_invalid_catalog_keeps_the_previous_one_and_writes_nothing(world, bad):
    app, conn = world
    load(app, conn, simple_document())
    before, mark = catalog_row(conn), audit_mark(conn)
    state_before = dict(conn.execute("SELECT * FROM routing_coverage_state").fetchone())
    with pytest.raises(coverage.CoverageError):
        coverage.load_catalog_document(conn, bad, app.config, T0)
    assert catalog_row(conn) == before and routing_audit(conn, mark) == []
    assert dict(conn.execute("SELECT * FROM routing_coverage_state").fetchone()) == state_before


def test_an_invalid_catalog_when_none_was_loaded_leaves_the_table_empty(world):
    app, conn = world
    with pytest.raises(coverage.CoverageError):
        coverage.load_catalog_document(conn, b'{"features": [{"properties": {"id": "a", "parent": null}}]}', app.config, T0)
    assert catalog_row(conn) is None and coverage.load_catalog(conn) is None


def test_the_table_holds_one_row_only_and_refuses_an_empty_content(world):
    _, conn = world
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO routing_catalog (id, content, sha256, loaded_at) VALUES (2, '[]', 'x', 'y')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO routing_catalog (id, content, sha256, loaded_at) VALUES (1, NULL, 'x', 'y')")
    conn.rollback()


def test_a_process_reloads_its_prepared_contours_when_the_fingerprint_in_the_database_changes(world, monkeypatch):
    """Les processus web voient le catalogue chargé par la commande de l'hôte, sans redémarrage (ZD-2)."""
    app, conn = world
    web = connect(app)                                                                     # « un autre processus » : sa propre connexion
    try:
        load(app, conn, synthetic(rect('aa', 0, 0, 4, 4, size=10)))
        assert [r.id for r in coverage.load_catalog(web)] == ['aa']
        load(app, conn, synthetic(rect('aa', 0, 0, 4, 4, size=10), rect('zz', 10, 10, 12, 12, size=5)))
        assert [r.id for r in coverage.load_catalog(web)] == ['aa', 'zz']                  # empreinte changée : rechargé
        calls = []
        real = catalog.shape
        monkeypatch.setattr(catalog, 'shape', lambda *a, **k: calls.append(1) or real(*a, **k))
        coverage.load_catalog(web)
        coverage.load_catalog(web)
        assert calls == []                                                                 # même empreinte : contours gardés en mémoire
    finally:
        web.close()


def test_the_catalog_replacement_is_atomic_for_a_concurrent_reader(world):
    """Un lecteur ne voit jamais une table vide ni un contenu dont l'empreinte ne correspond pas (remplacement en une transaction)."""
    app, conn = world
    documents = [synthetic(rect('aa', 0, 0, 2, 2, size=n)) for n in (1, 2)]
    load(app, conn, documents[0])
    stop, bad = threading.Event(), []

    def reader():
        own = connect(app)
        try:
            while not stop.is_set():
                row = own.execute("SELECT content, sha256 FROM routing_catalog WHERE id = 1").fetchone()
                if row is None or catalog.serialize(json.loads(row['content']))[1] != row['sha256']:
                    bad.append(row)
        finally:
            own.close()

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        for i in range(60):
            coverage.load_catalog_document(conn, raw(documents[i % 2]), app.config, T0)
    finally:
        stop.set()
        thread.join()
    assert bad == []


# ===================================================== E. arborescence : cycles (TZ2a-M1) =====================================

CYCLE_CHILD = """
import json, resource, sys
resource.setrlimit(resource.RLIMIT_AS, (1_500_000_000, 1_500_000_000))              # un parcours sans fin échoue en MemoryError, pas en OOM hôte
sys.path.insert(0, sys.argv[1])
from app.services.routing import catalog
doc = json.load(sys.stdin)
try:
    out = catalog.build(doc, root='europe', exclude=[], allowed_hosts=['download.geofabrik.de'])
except catalog.CatalogError as exc:
    print('CatalogError', exc)
except MemoryError:
    print('MemoryError')
else:
    print('OK', len(out))
"""


def build_in_a_child(document, timeout=20):
    """`catalog.build` dans un processus à mémoire bornée et à durée bornée : un cycle ne doit ni boucler ni gonfler (document tiers)."""
    import subprocess
    import sys
    from pathlib import Path
    root = str(Path(__file__).resolve().parent.parent)
    try:
        done = subprocess.run([sys.executable, '-I', '-c', CYCLE_CHILD, root], input=json.dumps(document), capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        return 'Timeout'
    return done.stdout.strip() or done.stderr.strip()[-200:]


def tree(parents):
    """Catalogue dont les parents sont ceux donnés ({id: parent}) ; chaque id est une feuille valide avec un contour."""
    features = [rect(rid, 0, 0, 1, 1, size=1, parent=parent) for rid, parent in parents.items() if rid != 'europe']
    if 'europe' in parents:
        features.insert(0, feature('europe', parents['europe'], url=False))
    return {'type': 'FeatureCollection', 'features': features}


@pytest.mark.parametrize('label, parents', [
    ('cycle qui passe par la racine', {'europe': 'france', 'france': 'europe', 'lorraine': 'france'}),
    ('cycle direct hors de la racine', {'europe': None, 'a': 'b', 'b': 'a', 'c': 'europe'}),
    ('auto-parenté', {'europe': None, 'a': 'a', 'c': 'europe'}),
    ('cycle hors du sous-arbre', {'europe': None, 'france': 'europe', 'x': 'y', 'y': 'x', 'lorraine': 'france'}),
    ('racine parente d\'elle-même', {'europe': 'europe', 'lorraine': 'europe'}),
])
def test_a_catalog_whose_parents_form_a_cycle_is_a_catalog_error_never_a_loop_or_a_memory_growth(label, parents):
    outcome = build_in_a_child(tree(parents))
    assert outcome in ('OK 1', 'OK 2', 'OK 3') or outcome.startswith('CatalogError'), (label, outcome)
    assert outcome != 'MemoryError' and outcome != 'Timeout', (label, outcome)
    if label in ('cycle qui passe par la racine', 'racine parente d\'elle-même'):
        assert outcome.startswith('CatalogError'), (label, outcome)                      # le cycle atteint le sous-arbre : refusé


def test_a_cycle_through_the_subtree_keeps_the_previous_catalog_and_writes_nothing(world):
    app, conn = world
    load(app, conn, simple_document())
    before, mark = catalog_row(conn), audit_mark(conn)
    bad = tree({'europe': 'france', 'france': 'europe', 'lorraine': 'france'})
    with pytest.raises(coverage.CoverageError):
        coverage.load_catalog_document(conn, raw(bad), app.config, T0)
    assert catalog_row(conn) == before and routing_audit(conn, mark) == []


def test_a_wide_flat_catalog_is_not_mistaken_for_a_cycle_and_a_deep_chain_is_accepted():
    flat = tree({'europe': None, **{f'r{i}': 'europe' for i in range(300)}})
    assert len(build(flat)) == 300
    chain = {'europe': None, 'n0': 'europe'}
    chain.update({f'n{i}': f'n{i - 1}' for i in range(1, 40)})
    chain['leaf'] = 'n39'
    assert [r['id'] for r in build(tree(chain))] == ['leaf']
