"""
Calculs géométriques de la couverture pilotée par la zone (rôle test) : routing-zone-donnees ZD-5, ZD-6, ZD-7, CA-ZD3 et SHP-1
(dépendance `shapely` / GEOS figée, ADR-0013 point 3). Écrit depuis la spec : les valeurs attendues sont écrites en dur (emprise de
la zone par défaut, nombre de dalles, régions de la copie figée du catalogue), jamais recalculées par le code testé.

 A. emprise d'extraction (ZD-5) : exemple de la spec, marge, bornes, arrondi vers l'extérieur, arêtes contenues
 B. dalles MNT (ZD-7) : le carré coupe l'emprise, le contact de bord ne compte pas, longitudes négatives
 C. résolution des régions sur le polygone (ZD-6) : U, agrégat, plus petite région, liseré 0,01 %, petite région, ordre des sommets
 D. copie figée du catalogue : la zone par défaut, l'exemple Normandie, une zone de 2 000 km en moins d'une seconde
 E. SHP-1 : shapely 2.2.0 / GEOS 3.14.1, requirements.in, verrou à empreintes, Dockerfile en `--only-binary shapely`
"""

import math
import random
import re
import time
from pathlib import Path

import pytest
import shapely

from app.config import Config
from app.services.routing import catalog, coverage, zone
from tests.routing_zone_support import (DEFAULT_REGIONS, DEFAULT_ZONE, NORMANDIE_ZONE, expected_tiles, frozen_document, rect,
                                        synthetic)

ROOT = Path(__file__).resolve().parent.parent
HOSTS = ['download.geofabrik.de']


def prepared(*features, document=None):
    """Régions prêtes pour le calcul (contours en Web Mercator), comme les relit l'application depuis la base."""
    document = document or synthetic(*features)
    regions = catalog.build(document, root='europe', exclude=Config.ROUTING_CATALOG_EXCLUDE, allowed_hosts=HOSTS)
    text, digest = catalog.serialize(regions)
    return catalog.prepare(text, digest)


def box(lon0, lat0, lon1, lat1):
    return [[lon0, lat0], [lon1, lat0], [lon1, lat1], [lon0, lat1]]


@pytest.fixture(scope='module')
def frozen():
    return prepared(document=frozen_document())


# ===================================================== A. emprise d'extraction (ZD-5) =========================================

def test_the_extraction_box_of_the_default_zone_is_exactly_the_spec_value_with_24_tiles():
    bbox = coverage.extract_bbox(DEFAULT_ZONE, 10)
    assert bbox == [1.2053, 48.74, 6.5647, 51.64]
    assert len(coverage.dem_tiles(bbox)) == 24
    assert set(coverage.dem_tiles(bbox)) == {(lat, lon) for lat in range(48, 52) for lon in range(1, 7)}


def test_the_longitude_margin_uses_the_largest_latitude_of_the_zone_before_the_margin():
    """φ = 51,55 (pas 51,64) : Δlon = m / (111,1949 · cos φ). Un φ pris après la marge donnerait 6,5647 − 6,42 plus petit."""
    delta = 10 / (111.1949 * math.cos(math.radians(51.55)))
    bbox = coverage.extract_bbox(DEFAULT_ZONE, 10)
    assert bbox[2] == math.ceil((6.42 + delta) * 1e4) / 1e4 and bbox[0] == math.floor((1.35 - delta) * 1e4) / 1e4


@pytest.mark.parametrize('margin', [0, 1, 10, 25, 50])
def test_the_margin_widens_the_envelope_and_zero_is_the_envelope_itself(margin):
    bbox = coverage.extract_bbox(DEFAULT_ZONE, margin)
    if margin == 0:
        assert bbox == [1.35, 48.83, 6.42, 51.55]
    else:
        assert bbox[0] < 1.35 and bbox[1] < 48.83 and bbox[2] > 6.42 and bbox[3] > 51.55
    d_lat = margin / 111.1949
    assert bbox[3] - 51.55 >= d_lat - 1e-9 and bbox[3] - 51.55 <= d_lat + 1.01e-4         # arrondi vers l'extérieur : au plus 1e-4 de plus


def test_the_box_is_clipped_to_the_projection_and_to_the_antimeridian():
    high = coverage.extract_bbox([[170.0, 84.0], [179.9, 84.0], [179.9, 85.0], [170.0, 85.0]], 50)
    assert high[3] == 85.0511 and high[2] == 180.0
    low = coverage.extract_bbox([[-179.9, -85.0], [-170.0, -85.0], [-170.0, -84.0], [-179.9, -84.0]], 50)
    assert low[1] == -85.0511 and low[0] == -180.0
    assert all(-180 <= v <= 180 for v in high + low) and high[1] < high[3] and low[1] < low[3]


def test_every_value_has_at_most_four_decimals_and_is_rounded_outwards():
    rng = random.Random(7)
    for _ in range(200):
        lon, lat = rng.uniform(-60, 60), rng.uniform(-60, 60)
        w, h = rng.uniform(0.05, 5), rng.uniform(0.05, 5)
        vertices = box(lon, lat, lon + w, lat + h)
        bbox = coverage.extract_bbox(vertices, rng.randint(0, 50))
        for value in bbox:
            assert abs(value * 1e4 - round(value * 1e4)) < 1e-6, bbox
        assert bbox[0] <= lon and bbox[1] <= lat and bbox[2] >= lon + w and bbox[3] >= lat + h


def test_the_box_contains_every_point_of_the_traced_edges():
    """Les arêtes sont droites dans la projection de la carte : tout point d'une arête est dans l'emprise d'extraction."""
    rng = random.Random(11)
    shapes = [DEFAULT_ZONE, NORMANDIE_ZONE, [[2, 49], [5, 49], [5, 51], [4, 51], [4, 50], [3, 50], [3, 51], [2, 51]],
              [[3.0, 50.4], [3.4, 50.4], [3.0, 50.8]]]
    for vertices in shapes:
        bbox = coverage.extract_bbox(vertices, rng.randint(0, 50))
        projected = [zone.project(lon, lat) for lon, lat in vertices]
        for a, b in zip(projected, projected[1:] + projected[:1]):
            for step in range(101):
                t = step / 100
                lon = a[0] + (b[0] - a[0]) * t
                lat = math.degrees(2 * math.atan(math.exp(math.radians(a[1] + (b[1] - a[1]) * t))) - math.pi / 2)   # inverse de zone.project
                assert bbox[0] <= lon <= bbox[2] and bbox[1] <= lat <= bbox[3], (vertices[0], lon, lat, bbox)


# ===================================================== B. dalles MNT (ZD-7) ===================================================

def test_a_tile_counts_when_its_square_cuts_the_box_and_a_shared_edge_does_not():
    assert coverage.dem_tiles([2.0, 48.0, 3.0, 49.0]) == [(48, 2)]                         # un carré exact : une dalle, pas neuf
    assert set(coverage.dem_tiles([1.5, 48.5, 3.5, 49.5])) == {(48, 1), (48, 2), (48, 3), (49, 1), (49, 2), (49, 3)}
    assert coverage.dem_tiles([2.0, 48.0, 3.0, 49.0001]) == [(48, 2), (49, 2)]            # un dépassement d'un cheveu : une dalle de plus


def test_the_tile_is_named_by_its_south_west_corner_including_negative_longitudes_and_the_southern_hemisphere():
    assert set(coverage.dem_tiles([-0.6447, 48.74, 0.5, 49.2])) == {(48, -1), (48, 0), (49, -1), (49, 0)}
    assert set(coverage.dem_tiles([-0.5, -33.5, 0.5, -32.5])) == {(-34, -1), (-34, 0), (-33, -1), (-33, 0)}


def test_the_tile_list_is_exactly_the_independent_enumeration():
    rng = random.Random(3)
    for _ in range(100):
        lon, lat = rng.uniform(-170, 170), rng.uniform(-80, 80)
        bbox = [lon, lat, lon + rng.uniform(0.01, 9), lat + rng.uniform(0.01, 9)]
        assert set(coverage.dem_tiles(bbox)) == expected_tiles(bbox)
        assert len(coverage.dem_tiles(bbox)) == len(set(coverage.dem_tiles(bbox)))             # aucune dalle en double


def test_the_normandie_example_of_the_spec_has_the_extraction_box_and_32_tiles():
    bbox = coverage.extract_bbox(NORMANDIE_ZONE, 10)
    assert bbox == [-0.6447, 48.74, 6.5647, 51.64]
    assert len(coverage.dem_tiles(bbox)) == 32 and len(coverage.dem_tiles(coverage.extract_bbox(DEFAULT_ZONE, 10))) == 24


# ===================================================== C. résolution sur le polygone (ZD-6) ===================================

def ids(vertices, regions):
    return coverage.resolve_regions(vertices, regions).ids


def test_a_u_shaped_zone_does_not_keep_the_region_inside_its_notch():
    """Zone en U dont l'échancrure (lon 3 à 4, lat 50 à 51) couvre la région C : résolution sur le POLYGONE, pas sur son emprise."""
    u = [[2, 49], [5, 49], [5, 51], [4, 51], [4, 50], [3, 50], [3, 51], [2, 51]]
    regions = prepared(rect('left', 1.5, 48.5, 3.0, 51.5, size=1), rect('right', 4.0, 48.5, 5.5, 51.5, size=1),
                       rect('bottom', 1.5, 48.5, 5.5, 50.0, size=1), rect('notch', 3.0, 50.0, 4.0, 51.0, size=1))
    resolution = coverage.resolve_regions(u, regions)
    assert 'notch' not in resolution.ids and resolution.ids == ['bottom', 'left', 'right']
    assert resolution.uncovered_pct < 0.5


def test_an_aggregate_in_the_exclusion_list_is_never_kept_so_the_channel_is_outside_the_data():
    """Relevé du §3 : sans exclusion, `great-britain` gagne les points en mer de la Manche ; avec la liste par défaut, la Manche est
    hors des données et seules les feuilles d'un côté ou de l'autre sont retenues."""
    channel = box(-0.5, 50.0, 0.5, 50.4)
    excluded = prepared(document=frozen_document())
    assert not {'great-britain', 'britain-and-ireland', 'alps', 'dach'} & {r.id for r in excluded}
    assert coverage.resolve_regions(channel, excluded).ids == [] and coverage.resolve_regions(channel, excluded).uncovered_pct > 99
    stored = catalog.build(frozen_document(), root='europe', exclude=[], allowed_hosts=HOSTS)
    text, digest = catalog.serialize(stored)
    unexcluded = catalog.prepare(text, digest)
    assert {'great-britain', 'britain-and-ireland'} <= set(coverage.resolve_regions(channel, unexcluded).ids)


def test_the_kent_coast_keeps_the_small_leaves_and_never_an_aggregate(frozen):
    resolved = ids(box(0.8, 50.9, 1.6, 51.4), frozen)
    assert 'kent' in resolved and not {'great-britain', 'britain-and-ireland'} & set(resolved)


def test_a_piece_covered_by_two_regions_goes_to_the_smaller_one():
    regions = prepared(rect('big', 0, 0, 10, 10, size=1), rect('small', 4, 4, 6, 6, size=1))
    assert ids(box(4.5, 4.5, 5.5, 5.5), regions) == ['small']                              # tout dans les deux : la petite seule
    assert ids(box(3, 3, 7, 7), regions) == ['big', 'small']                               # la couronne n'est couverte que par la grande


def test_a_sliver_under_0_01_percent_does_not_keep_its_region_and_counts_as_outside_the_data():
    zone_box = box(0, 0, 10, 10)
    sliver = prepared(rect('main', 0, 0, 10, 10, size=1), rect('neighbour', 9.9995, 0, 12, 10, size=1))       # 0,005 % de la zone
    assert ids(zone_box, sliver) == ['main']
    kept = prepared(rect('main', 0, 0, 10, 10, size=1), rect('neighbour', 9.998, 0, 12, 10, size=1))          # 0,02 % : retenu
    assert ids(zone_box, kept) == ['main', 'neighbour']
    alone = coverage.resolve_regions(zone_box, prepared(rect('neighbour', 9.9995, 0, 12, 10, size=1)))
    assert alone.ids == [] and alone.uncovered_pct == pytest.approx(100.0)                 # écarté : compte « hors des données »


def test_a_10_km2_region_inside_a_50_by_50_km_zone_is_kept_wherever_it_sits_no_grid_effect():
    """Zone de ≈ 50 km × 50 km autour de (4, 50), région de ≈ 3,2 km × 3,2 km : retenue quelle que soit sa position (pas d'échantillonnage)."""
    zone_box = box(3.65, 49.78, 4.35, 50.22)
    rng = random.Random(5)
    for _ in range(40):
        lon, lat = rng.uniform(3.7, 4.25), rng.uniform(49.82, 50.16)
        tiny = rect('tiny', lon, lat, lon + 0.045, lat + 0.029, size=1)
        regions = prepared(rect('around', 3.0, 49.0, 5.0, 51.0, size=1), tiny)
        assert 'tiny' in ids(zone_box, regions), (lon, lat)


def test_the_same_zone_gives_the_same_regions_whatever_the_vertex_order(frozen):
    reference = ids(DEFAULT_ZONE, frozen)
    for shift in range(4):
        rotated = DEFAULT_ZONE[shift:] + DEFAULT_ZONE[:shift]
        assert ids(rotated, frozen) == reference and ids(rotated[::-1], frozen) == reference
    assert reference == sorted(reference)                                                  # résultat trié par identifiant


def test_a_region_with_a_hole_does_not_cover_a_zone_inside_the_hole():
    regions = prepared(rect('ring', 0, 0, 10, 10, size=1, hole=(4, 4, 6, 6)))
    resolution = coverage.resolve_regions(box(4.5, 4.5, 5.5, 5.5), regions)
    assert resolution.ids == [] and resolution.uncovered_pct == pytest.approx(100.0)
    assert ids(box(3, 3, 7, 7), regions) == ['ring']


def test_a_zone_with_no_area_is_refused_not_resolved():
    with pytest.raises(ValueError):
        coverage.resolve_regions([[1, 1], [2, 1], [3, 1]], prepared(rect('big', 0, 0, 10, 10, size=1)))


# ===================================================== D. copie figée du catalogue ============================================

def test_the_default_zone_resolves_to_the_listed_17_regions_within_the_default_caps(frozen):
    resolution = coverage.resolve_regions(DEFAULT_ZONE, frozen)
    assert resolution.ids == DEFAULT_REGIONS and len(resolution.ids) == 17
    assert resolution.total_bytes == pytest.approx(3.28e9, rel=0.01)                       # §3 : 3,28 Go
    assert resolution.uncovered_pct == pytest.approx(4.0, abs=0.5)                          # §3 : 4,0 % de mer
    assert len(resolution.ids) <= Config.ROUTING_MAX_REGIONS and resolution.total_bytes <= Config.ROUTING_MAX_SOURCE_BYTES
    assert resolution.uncovered_pct <= Config.ROUTING_MAX_UNCOVERED_PCT
    assert len(coverage.dem_tiles(coverage.extract_bbox(DEFAULT_ZONE, 10))) <= Config.DEM_MAX_TILES


def test_the_normandie_example_adds_basse_normandie_and_stays_within_the_default_caps(frozen):
    resolution = coverage.resolve_regions(NORMANDIE_ZONE, frozen)
    assert 'basse-normandie' in resolution.ids and 17 <= len(resolution.ids) <= 18
    assert set(DEFAULT_REGIONS) - {'saarland'} <= set(resolution.ids)
    assert 3.3e9 <= resolution.total_bytes <= 3.6e9                                         # ≈ 3,4 Go (§3)
    assert resolution.total_bytes <= Config.ROUTING_MAX_SOURCE_BYTES and len(resolution.ids) <= Config.ROUTING_MAX_REGIONS
    assert len(coverage.dem_tiles(coverage.extract_bbox(NORMANDIE_ZONE, 10))) <= Config.DEM_MAX_TILES


def test_a_zone_of_2000_km_is_resolved_in_less_than_one_second_with_prepared_contours(frozen):
    big = box(-11.0, 40.0, 9.0, 57.0)
    assert zone.validate(big)                                                                # c'est bien une zone admise par EF-6.20
    coverage.resolve_regions(big, frozen)                                                    # échauffement : GEOS prépare ses index
    started = time.perf_counter()
    resolution = coverage.resolve_regions(big, frozen)
    assert time.perf_counter() - started <= 1.0
    assert 'belgium' in resolution.ids and len(resolution.ids) > 17


def test_the_calculation_does_not_depend_on_the_catalog_order(frozen):
    shuffled = list(frozen)
    random.Random(1).shuffle(shuffled)
    assert ids(NORMANDIE_ZONE, shuffled) == ids(NORMANDIE_ZONE, frozen)


# ===================================================== E. SHP-1 : shapely et GEOS figés =======================================

def test_shapely_and_geos_are_the_pinned_versions():
    assert shapely.__version__ == "2.2.0"
    assert shapely.geos_version == (3, 14, 1)
    assert coverage.version() == ("2.2.0", (3, 14, 1))


def test_requirements_in_pins_shapely_exactly():
    assert re.search(r'^shapely==2\.2\.0\s*(#.*)?$', (ROOT / 'requirements.in').read_text(), re.M)


def test_the_lock_pins_shapely_with_hashes_and_every_requirement_has_hashes():
    text = (ROOT / 'requirements.txt').read_text()
    block = re.search(r'^shapely==2\.2\.0 \\\n((?:\s+--hash=sha256:[0-9a-f]{64}(?: \\)?\n)+)', text, re.M)
    assert block, "shapely==2.2.0 doit être épinglé avec ses empreintes dans requirements.txt"
    hashes = re.findall(r'--hash=sha256:([0-9a-f]{64})', block.group(1))
    assert len(hashes) == len(set(hashes)) and len(hashes) >= 4                                # plusieurs roues (amd64, arm64, Python)
    for requirement in re.findall(r'^([A-Za-z0-9_.-]+==[^\s\\]+) \\$', text, re.M):
        assert re.search(rf'^{re.escape(requirement)} \\\n\s+--hash=sha256:', text, re.M), requirement


def test_the_dockerfile_installs_shapely_from_wheels_only_with_hashes_required():
    lines = [line for line in (ROOT / 'Dockerfile').read_text().splitlines() if 'pip install' in line and not line.lstrip().startswith('#')]
    assert lines, "aucune ligne `pip install` dans le Dockerfile"
    for line in lines:
        assert '--require-hashes' in line and re.search(r'--only-binary[ =]shapely\b', line), line
        assert '--no-binary' not in line, line
    assert not any('gcc' in line and 'shapely' in line for line in (ROOT / 'Dockerfile').read_text().splitlines())


def test_shapely_is_a_direct_dependency_of_the_catalog_and_coverage_modules_only():
    """GEOS est du code natif : seuls le catalogue et la couverture l'importent (surface réduite, ADR-0013)."""
    importers = sorted(str(path.relative_to(ROOT)) for path in (ROOT / 'app').rglob('*.py')
                       if re.search(r'^\s*(import shapely|from shapely)', path.read_text(encoding='utf-8'), re.M))
    assert importers == ['app/services/routing/catalog.py', 'app/services/routing/coverage.py']
