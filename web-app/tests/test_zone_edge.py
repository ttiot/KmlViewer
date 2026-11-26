"""
Zone couverte en polygone, côté serveur (rôle test) : admin-space EF-6.20 (CA-85), gpx-route-builder RB-1.3b (CA-R49, CA-R51).
Écrit depuis la spec, pas depuis le code : les messages, l'ordre des contrôles, la règle du bord et la méthode de rabattement sont ceux
du texte.

 A. contrôles à l'écriture (validate) : forme, bornes, emprise, polygone simple, ordre des contrôles, valeur stockée
 B. appartenance d'un point : jeu de cas commun (tests_browser/zone_cases.json), bords, sommets, U concave, projection de la carte
 C. zone en vigueur : toujours un polygone (défaut = 4 sommets), valeur forgée, emprise
 D. rabattement d'un point technique (pull_inside) et emprise RB-S7
 E. API : PATCH /api/admin/settings (CA-85), options, contrôle avant tout appel au moteur, détours S1, déterminisme (CA-R49)
 F. diagnostic, journaux sans coordonnée
"""

import json
import logging
import math
import random
import re
from pathlib import Path

import pytest

from app.services.routing import builder, zone
from app.services.routing.builder import BuildConfig
from app.settings import REGISTRY
from tests.routing_fakes import FakeProvider
from tests.test_route_builder_api_edge import BASE, build_world
from tests.pw_harness import call

CASES_FILE = Path(__file__).resolve().parent.parent / 'tests_browser' / 'zone_cases.json'
KEY = 'routing.coverage_polygon'
DEFAULT_RECTANGLE = [1.35, 48.83, 6.42, 51.55]                                      # emprise de la zone par défaut
CORNERS = [[1.35, 48.83], [6.42, 48.83], [6.42, 51.55], [1.35, 51.55]]            # sud-ouest, sud-est, nord-est, nord-ouest

# Polygone de 8 sommets qui suit le nord de la France et la Belgique (CA-85) : [lon, lat]
NORTH_FR_BE = [[2.55, 51.09], [4.25, 51.35], [5.85, 50.75], [6.35, 49.55], [4.85, 48.95], [3.15, 48.85], [1.85, 49.75], [1.60, 50.85]]
# U ouvert au nord : branche gauche, fond, branche droite ; échancrure = lon 3 à 4, lat 50 à 51
U = [[2, 49], [5, 49], [5, 51], [4, 51], [4, 50], [3, 50], [3, 51], [2, 51]]
# Petit U autour de Lille (échancrure = lon 3,1 à 3,3, lat 50,6 à 50,8) : les détours S1 y tiennent avec de courtes distances
SMALL_U = [[3.0, 50.4], [3.4, 50.4], [3.4, 50.8], [3.3, 50.8], [3.3, 50.6], [3.1, 50.6], [3.1, 50.8], [3.0, 50.8]]

MSG_COUNT = "Zone : de 3 à 50 sommets."
MSG_SHAPE = "Zone : chaque sommet est [longitude, latitude]."
MSG_BOUNDS = "Zone : longitude de −180 à 180, latitude de −85,0511 à 85,0511."
MSG_BOX = "Zone : emprise de 2 000 km de côté au plus."


def crossing(first, second):
    return f"Zone : le contour se croise (arêtes {first[0]}–{first[1]} et {second[0]}–{second[1]})."


# ============================================================ A. contrôles à l'écriture ======================================

@pytest.mark.parametrize('value', [NORTH_FR_BE, U, list(reversed(U)), [[0, 0], [1, 0], [0, 1]], SMALL_U, CORNERS])
def test_valid_polygons_are_accepted_in_either_direction(value):
    assert zone.validate(value) == [[float(a), float(b)] for a, b in value]


def test_a_value_is_stored_as_received_with_floats_and_no_rounding():
    vertices = [[1.23456789012345, 50.987654321098], [3, 50], [2.5, 51.123456789012345]]
    kept = zone.validate(vertices)
    assert kept == [[1.23456789012345, 50.987654321098], [3.0, 50.0], [2.5, 51.123456789012345]]
    assert all(isinstance(n, float) for vertex in kept for n in vertex)
    assert zone.validate(tuple(tuple(v) for v in vertices)) == kept


@pytest.mark.parametrize('value, message', [
    ([], MSG_COUNT), ((), MSG_COUNT),                                  # lot Z1 : `[]` n'est plus « aucun polygone », il est refusé
    ([[0, 0], [1, 1]], MSG_COUNT), ([[0, 0]], MSG_COUNT),
    ([[i / 100, 50 + i / 100] for i in range(51)], MSG_COUNT),
    ([[1, 2]] * 10_000, MSG_COUNT),
    (['x'] * 10_000, MSG_COUNT),                                      # le nombre se contrôle AVANT de lire un seul sommet
    ([None] * 51, MSG_COUNT), ([[1, "50"], [2, 50]], MSG_COUNT),
    ('x', MSG_COUNT), (None, MSG_COUNT), (5, MSG_COUNT), ({'a': 1}, MSG_COUNT), (False, MSG_COUNT),
])
def test_the_vertex_count_is_checked_first(value, message):
    with pytest.raises(ValueError) as error:
        zone.validate(value)
    assert str(error.value) == message


@pytest.mark.parametrize('vertex', [[1, "50"], [True, 50], [1, 50, 3], [float('nan'), 50], [1], [], 'ab', None, 5, {'a': 1}, [1, None],
                                    [float('inf'), 50], [1, -float('inf')], [[1], 50], ["1", "50"], [False, False]])
def test_every_vertex_is_two_finite_numbers(vertex):
    with pytest.raises(ValueError) as error:
        zone.validate([[2, 50], [3, 50], vertex])
    assert str(error.value) == MSG_SHAPE


@pytest.mark.parametrize('vertex', [[10 ** 400, 50], [-10 ** 400, 50], [1, 10 ** 400], [2 ** 1030, 50], [181, 50], [-181, 50], [1, 86],
                                    [1, -86], [1, 85.0512], [1, -85.0512], [180.0001, 50], [1, 90]])
def test_bounds_follow_the_map_and_a_huge_integer_is_never_converted(vertex):
    with pytest.raises(ValueError) as error:
        zone.validate([[2, 50], [3, 50], vertex])
    assert str(error.value) == MSG_BOUNDS


def test_the_projection_limit_itself_is_accepted():
    kept = zone.validate([[0, 85.0511], [1, 85.0511], [1, 84.9]])
    assert kept[0][1] == 85.0511
    assert zone.validate([[0, -85.0511], [1, -85.0511], [1, -84.9]])[0][1] == -85.0511
    assert zone.validate([[-180, 50], [-179, 50], [-179, 51]])[0][0] == -180.0


def test_the_bounds_are_checked_before_the_box_and_the_box_before_the_contour():
    huge_and_crossed = [[0, 0], [0, 19], [1, 0], [1, 19]]                 # emprise ≈ 2 100 km ET contour qui se croise
    with pytest.raises(ValueError) as error:
        zone.validate(huge_and_crossed)
    assert str(error.value) == MSG_BOX
    with pytest.raises(ValueError) as error:
        zone.validate([[200, 0], [0, 19], [1, 0], [1, 19]])
    assert str(error.value) == MSG_BOUNDS
    with pytest.raises(ValueError) as error:
        zone.validate([[200, "x"], [0, 19], [1, 0], [1, 19]])
    assert str(error.value) == MSG_SHAPE


def test_a_box_of_2100_km_is_refused_and_one_just_under_2000_km_is_accepted():
    with pytest.raises(ValueError) as error:
        zone.validate([[0, 0], [0, 19], [1, 10]])
    assert str(error.value) == MSG_BOX
    assert zone.validate([[0, 0], [0, 17.9], [1, 10]])                    # ≈ 1 990 km de côté


def test_the_bow_tie_names_the_first_pair_of_edges_in_order():
    with pytest.raises(ValueError) as error:
        zone.validate([[0, 0], [1, 1], [1, 0], [0, 1]])
    assert str(error.value) == crossing((1, 2), (3, 4))


@pytest.mark.parametrize('value', [
    [[0, 0], [1, 0], [1, 0], [1, 1]],                                     # sommet répété
    [[1, 50], [1, 51], [1, 52]],                                          # alignés (sur un méridien : aussi droits sur la carte)
    [[0, 50], [1, 50], [2, 50]],                                          # alignés (sur un parallèle)
    [[0, 0], [2, 0], [1, 0], [1, 1]],                                     # retour en arrière sur la même ligne
    [[0, 0], [1, 0], [0, 0], [0, 1]],                                     # premier sommet repris au milieu
    [[0, 0], [1, 0], [1, 1], [0, 0]],                                     # premier sommet répété à la fin
    [[0, 0], [2, 0], [2, 2], [1, 0], [0, 2]],                             # une arête touche un sommet non voisin
    [[0, 0], [2, 0], [1, 0.0], [1, 1]],                                   # idem, sommet posé sur l'arête
])
def test_a_polygon_that_is_not_simple_is_refused(value):
    with pytest.raises(ValueError) as error:
        zone.validate(value)
    assert str(error.value).startswith("Zone : le contour se croise (arêtes ")


def test_a_closed_ring_with_the_first_vertex_repeated_is_refused():
    """Le contour se referme seul : répéter le premier sommet à la fin donne une arête nulle, donc un refus."""
    with pytest.raises(ValueError):
        zone.validate([[2, 49], [5, 49], [5, 51], [2, 49]])


@pytest.mark.parametrize('value', [U, list(reversed(U)), U[3:] + U[:3], NORTH_FR_BE])
def test_the_direction_and_the_starting_vertex_do_not_matter(value):
    assert len(zone.validate(value)) == len(value)


def test_the_maximum_of_50_vertices_is_accepted():
    ring = [[round(3 + 0.5 * math.cos(2 * math.pi * i / 50), 6), round(50 + 0.5 * math.sin(2 * math.pi * i / 50), 6)] for i in range(50)]
    assert len(zone.validate(ring)) == 50


# ======================================================= B. appartenance d'un point ==========================================

def load_cases():
    if not CASES_FILE.is_file():
        pytest.skip("jeu de cas absent : la CI ne monte que web-app dans le conteneur")
    return json.loads(CASES_FILE.read_text(encoding='utf-8'))


def vertices_of(data, name):
    return data['zones'][name]['polygon']                                 # lot Z1 : plus de zone « rectangle », le défaut est un polygone de 4 sommets


def case_ids():
    if not CASES_FILE.is_file():
        return []
    return [(c['zone'], c['name']) for c in json.loads(CASES_FILE.read_text(encoding='utf-8'))['cases']]


def test_the_shared_case_file_is_large_enough_and_well_formed():
    data = load_cases()
    assert len(data['cases']) >= 30, len(data['cases'])
    names = [(c['zone'], c['name']) for c in data['cases']]
    assert len(set(names)) == len(names), "noms de cas en double"
    for case in data['cases']:
        assert set(case) == {'zone', 'name', 'point', 'expected'} and case['zone'] in data['zones'], case
        assert len(case['point']) == 2 and isinstance(case['expected'], bool), case
    for name in data['zones']:
        zone.validate(vertices_of(data, name))                            # chaque zone du fichier est une zone valide
    covered = {c['zone'] for c in data['cases']}
    assert covered == set(data['zones'])
    assert any('diagonale' in c['name'] for c in data['cases'])


@pytest.mark.parametrize('zone_name, case_name', case_ids())
def test_every_shared_case_gives_the_expected_answer(zone_name, case_name):
    data = load_cases()
    case = next(c for c in data['cases'] if (c['zone'], c['name']) == (zone_name, case_name))
    assert zone.contains(vertices_of(data, zone_name), case['point'][0], case['point'][1]) is case['expected'], case


def naive_contains(vertices, lat, lon):
    """Même règle, mais en longitude et latitude brutes : ce que la spec interdit (l'arête droite sur la carte n'est pas droite ici)."""
    inside = False
    for i in range(len(vertices)):
        (x1, y1), (x2, y2) = vertices[i], vertices[(i + 1) % len(vertices)]
        if (y1 > lat) != (y2 > lat) and lon < x1 + (lat - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def test_the_diagonal_cases_tell_the_map_projection_from_raw_longitude_and_latitude():
    data = load_cases()
    diagonal = [c for c in data['cases'] if 'diagonale' in c['name'] or c['zone'] in ('triangle_nord_ouest', 'triangle_sud_est')]
    assert diagonal
    disagreeing = [c['name'] for c in diagonal
                   if naive_contains(vertices_of(data, c['zone']), *c['point']) is not c['expected']
                   and not any(token in c['name'] for token in ('sommet', 'sur l\'arête'))]
    assert len(disagreeing) >= 3, f"le jeu ne distingue pas la carte de lon/lat brut : {disagreeing}"


def test_the_long_diagonal_edge_is_at_least_200_km():
    data = load_cases()
    a, b = vertices_of(data, 'triangle_nord_ouest')[:2]
    assert zone.box_too_large(a[0], a[1], b[0], b[1]) is False
    from app.services.geo import haversine
    assert haversine(a[1], a[0], b[1], b[0]) >= 200_000


@pytest.mark.parametrize('vertices', [U, NORTH_FR_BE, SMALL_U, CORNERS])
def test_every_vertex_and_every_edge_midpoint_is_inside_and_a_point_just_outside_an_axis_edge_is_not(vertices):
    for lon, lat in vertices:
        assert zone.contains(vertices, lat, lon) is True, ('sommet', lon, lat)
    n = len(vertices)
    for i in range(n):
        (lon1, lat1), (lon2, lat2) = vertices[i], vertices[(i + 1) % n]
        if lat1 == lat2 or lon1 == lon2:                                  # arête sur un parallèle ou un méridien : milieu exact
            lat, lon = (lat1 + lat2) / 2, (lon1 + lon2) / 2
        else:                                                             # arête en biais : milieu sur la carte
            my = (zone.project(lon1, lat1)[1] + zone.project(lon2, lat2)[1]) / 2
            lat, lon = math.degrees(2 * math.atan(math.exp(math.radians(my))) - math.pi / 2), (lon1 + lon2) / 2
        assert zone.contains(vertices, lat, lon) is True, ('milieu d arête', i)


def test_the_default_zone_is_exactly_the_old_rectangle_edges_and_corners_included():
    box = DEFAULT_RECTANGLE
    area = zone.effective_zone(getter())
    assert area == CORNERS
    rng = random.Random(7)
    for _ in range(500):
        lat, lon = rng.uniform(48.0, 52.5), rng.uniform(0.5, 7.5)
        assert zone.contains(area, lat, lon) is (box[1] <= lat <= box[3] and box[0] <= lon <= box[2])
    for lat in (box[1], box[3]):
        for lon in (box[0], box[2], 3.0):
            assert zone.contains(area, lat, lon) is True
    for lat, lon in ((box[1] - 1e-9, 3), (box[3] + 1e-9, 3), (3, box[0] - 1e-9)):
        assert zone.contains(area, lat, lon) is False


def test_a_point_outside_the_envelope_is_outside_without_any_other_calculation(monkeypatch):
    def forbidden(*args):
        raise AssertionError("aucun calcul de géométrie hors de l'emprise")
    monkeypatch.setattr(zone, 'project', forbidden)
    assert zone.contains(U, 52.0, 3.5) is False and zone.contains(U, 50.5, 9.0) is False


def test_the_point_in_the_notch_is_outside_while_inside_the_envelope():
    assert zone.envelope(U) == (49, 2, 51, 5)
    assert zone.contains(U, 50.5, 3.5) is False
    assert zone.contains(U, 50.5, 2.5) is True and zone.contains(U, 50.5, 4.5) is True


def test_the_tolerance_at_the_edge_is_about_1e_9_in_projected_coordinates():
    assert zone.contains(U, 50.5, 3.0 + 1e-12) is True                    # à 10⁻¹² près d'une arête : dedans
    assert zone.contains(U, 50.5, 3.0 + 1e-6) is False                    # à 10⁻⁶ : dehors (dans l'échancrure)
    assert zone.contains(U, 50.5, 3.0 - 1e-6) is True


def test_the_result_does_not_depend_on_the_direction_of_the_contour():
    rng = random.Random(3)
    points = [(rng.uniform(48.5, 51.5), rng.uniform(1.5, 5.5)) for _ in range(300)]
    forward, backward = U, list(reversed(U))
    assert [zone.contains(forward, *p) for p in points] == [zone.contains(backward, *p) for p in points]


def test_a_horizontal_ray_through_a_vertex_does_not_flip_the_answer():
    diamond = [[3, 49], [4, 50], [3, 51], [2, 50]]
    assert zone.contains(diamond, 50, 3.0) and zone.contains(diamond, 50, 2.5) and zone.contains(diamond, 50, 3.9)
    assert not zone.contains(diamond, 50, 1.9) and not zone.contains(diamond, 50, 4.1)
    star = [[0, 0], [2, 1], [4, 0], [4, 2], [2, 1.5], [0, 2]]             # un sommet à la hauteur d'un autre
    for lat in (0.0, 1.0, 1.5, 2.0):
        assert isinstance(zone.contains(star, lat, 1.0), bool)


# ===================================================== C. zone en vigueur ====================================================

def getter(**values):
    return lambda key: values[key] if key in values else REGISTRY[key].default


def test_by_default_the_zone_is_the_four_corners_south_west_first_and_the_registry_says_so():
    assert zone.effective_zone(getter()) == CORNERS == [list(v) for v in zone.DEFAULT_ZONE]
    assert REGISTRY[KEY].default == CORNERS and 'routing.coverage_bbox' not in REGISTRY
    assert zone.validate(REGISTRY[KEY].default) == CORNERS                    # le défaut passe ses propres contrôles


def test_a_polygon_is_the_zone_and_the_removed_rectangle_setting_is_ignored_even_if_a_getter_still_has_it():
    assert zone.effective_zone(getter(**{KEY: U})) == [[float(a), float(b)] for a, b in U]
    far = {'routing.coverage_bbox': [-5.0, 40.0, 5.0, 50.0]}
    assert zone.effective_zone(getter(**far)) == CORNERS                      # le réglage supprimé n'a plus aucun effet
    assert zone.effective_zone(getter(**far, **{KEY: U})) == [[float(a), float(b)] for a, b in U]


@pytest.mark.parametrize('forged', ['x', None, [], [[0, 0], [1, 1]], [[0, 0], [1, 1], [1, 0], [0, 1]], 5, {'a': 1}, [[1, 2, 3]] * 5, [[200, 0]] * 3,
                                    [['a', 'b']] * 3])
def test_a_forged_polygon_means_the_default_zone_never_no_zone(forged):
    assert zone.effective_zone(getter(**{KEY: forged})) == CORNERS
    cfg = BuildConfig.from_settings(getter(**{KEY: forged}))
    assert cfg.coverage_polygon == tuple(tuple(v) for v in CORNERS) and cfg.coverage_bbox == (48.83, 1.35, 51.55, 6.42)
    assert cfg.zone_vertices() == CORNERS


def test_the_envelope_of_the_zone_in_force_is_given_both_ways():
    assert zone.envelope(U) == (49, 2, 51, 5) and zone.envelope_lonlat(U) == [2, 49, 5, 51]
    assert zone.envelope_lonlat(zone.effective_zone(getter())) == DEFAULT_RECTANGLE
    assert zone.envelope_lonlat(zone.effective_zone(getter(**{KEY: NORTH_FR_BE}))) == [1.6, 48.85, 6.35, 51.35]


def test_the_build_config_carries_the_polygon_and_its_envelope():
    cfg = BuildConfig.from_settings(getter(**{KEY: SMALL_U}))
    assert cfg.coverage_bbox == (50.4, 3.0, 50.8, 3.4) and len(cfg.coverage_polygon) == 8
    assert cfg.zone_vertices() == [[float(a), float(b)] for a, b in SMALL_U]


# ============================================== D. rabattement des points techniques =========================================

def exit_point(vertices, origin, target):
    """Dernier point dans la zone du segment origine → cible, par une dichotomie fine (60 étapes) : la référence."""
    low, high = origin, target
    for _ in range(60):
        middle = ((low[0] + high[0]) / 2, (low[1] + high[1]) / 2)
        low, high = (middle, high) if zone.contains(vertices, *middle) else (low, middle)
    return low


def test_a_target_inside_the_zone_is_kept_as_it_is():
    assert zone.pull_inside(U, (50.5, 2.5), (50.2, 4.5)) == (50.2, 4.5)


def test_a_target_in_the_notch_is_brought_back_inside_close_to_the_edge():
    origin, target = (50.5, 2.5), (50.5, 3.6)                              # la cible est dans l'échancrure
    pulled = zone.pull_inside(U, origin, target)
    assert pulled is not None and zone.contains(U, *pulled)
    reference = exit_point(U, origin, target)
    assert abs(pulled[1] - reference[1]) <= (target[1] - origin[1]) / 2 ** 12 + 1e-12      # 12 étapes : un 4096ᵉ du segment
    assert pulled[1] <= 3.0 + 1e-9


def test_the_pulling_back_is_deterministic_and_stays_on_the_segment():
    origin, target = (50.1, 2.2), (50.9, 4.4)
    first = zone.pull_inside(U, origin, target)
    assert first == zone.pull_inside(U, origin, target)
    if first != target:
        cross = (first[0] - origin[0]) * (target[1] - origin[1]) - (first[1] - origin[1]) * (target[0] - origin[0])
        assert abs(cross) < 1e-9
        assert zone.contains(U, *first)


def test_an_origin_outside_the_zone_gives_no_technical_point_when_the_point_must_be_pulled_back():
    """RB-1.3b : « si le milieu de la corde est lui-même hors de la zone, ce tronçon n'a pas de point technique » : ce cas vaut
    pour un point à ramener (cible hors zone). Une cible déjà dans la zone n'a rien à ramener (question de spec signalée à coord)."""
    assert zone.pull_inside(U, (50.5, 3.5), (50.5, 3.7)) is None            # origine et cible dans l'échancrure
    assert zone.pull_inside(U, (52.0, 3.5), (50.5, 3.6)) is None            # origine hors de l'emprise, cible dans l'échancrure
    assert zone.pull_inside(U, (50.5, 3.5), (50.5, 2.5)) == (50.5, 2.5)     # cible dans la zone : gardée


def test_the_pulled_back_point_is_always_inside_for_many_segments():
    rng = random.Random(11)
    for _ in range(300):
        origin = (rng.uniform(49.05, 49.95), rng.uniform(2.05, 4.95))       # le fond du U : dans la zone
        target = (rng.uniform(48.0, 52.5), rng.uniform(1.0, 6.0))
        pulled = zone.pull_inside(U, origin, target)
        assert pulled is not None and zone.contains(U, *pulled), (origin, target, pulled)


def test_the_technical_box_is_the_users_box_widened_then_clipped_to_the_envelope_of_the_zone():
    points = [(50.5, 3.05), (50.5, 3.35)]
    envelope = zone.envelope(SMALL_U)
    box = builder._technical_bbox(points, 20_000, envelope)
    assert box[0] >= envelope[0] and box[1] >= envelope[1] and box[2] <= envelope[2] and box[3] <= envelope[3]
    assert box[0] < box[2] and box[1] < box[3]                                # jamais vide
    assert box == pytest.approx((50.4, 3.0, 50.68, 3.4), abs=0.002) or box[0] == envelope[0]
    tiny = builder._technical_bbox(points, 100, envelope)
    assert tiny[0] > envelope[0] and tiny[2] < envelope[2]                    # petite marge : l'emprise des points seule
    on_edge = builder._technical_bbox([(50.4, 3.0)], 0.0, envelope)
    assert on_edge[0] <= on_edge[2] and on_edge[1] <= on_edge[3]


# ===================================================== E. API : CA-85 et CA-R49 ===============================================

@pytest.fixture
def world(tmp_path):
    w = build_world(tmp_path)
    yield w
    w.conn.close()


def patch(world, value, **extra):
    body = {KEY: value, **extra}
    return call(world.adm, 'patch', '/api/admin/settings', body, raw=json.dumps(body))


def snapshot(world):
    data = call(world.adm, 'get', '/api/admin/settings').get_json()
    row = next(s for s in data['settings'] if s['key'] == KEY)
    audits = world.conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'setting.update'").fetchone()[0]
    return {'version': data['version'], 'value': row['value'], 'source': row['source'], 'audits': audits}


def options(world, client=None):
    return call(client or world.a, 'get', f'{BASE}/options').get_json()


def test_the_polygon_is_the_four_default_corners_with_the_default_origin(world):
    state = snapshot(world)
    assert state['value'] == CORNERS and state['source'] == 'défaut'
    assert KEY in REGISTRY and REGISTRY[KEY].type == 'polygon' and REGISTRY[KEY].section == 'Traces planifiées'
    assert REGISTRY[KEY].default == CORNERS and REGISTRY[KEY].label == "Zone couverte (polygone)" and not REGISTRY[KEY].env


def test_the_rectangle_setting_is_gone_from_the_list_and_its_patch_is_an_unknown_setting(world):
    listed = call(world.adm, 'get', '/api/admin/settings').get_json()
    assert all(s['key'] != 'routing.coverage_bbox' for s in listed['settings'])
    before = snapshot(world)
    r = call(world.adm, 'patch', '/api/admin/settings', {'routing.coverage_bbox': [3.0, 50.6, 3.2, 50.7]})
    assert r.status_code == 400 and 'Réglage inconnu' in r.get_json()['error'], r.get_data(as_text=True)
    assert snapshot(world) == before


def test_a_polygon_of_eight_vertices_is_written_read_back_identical_with_a_new_version_and_an_audit_entry(world):
    before = snapshot(world)
    r = patch(world, NORTH_FR_BE)
    assert r.status_code == 200, r.get_data(as_text=True)
    after = snapshot(world)
    assert after['value'] == NORTH_FR_BE and after['source'] == 'base'
    assert after['version'] == before['version'] + 1 and after['audits'] == before['audits'] + 1


@pytest.mark.parametrize('value', [U, list(reversed(U))])
def test_a_concave_polygon_and_the_other_direction_are_accepted(world, value):
    assert patch(world, value).status_code == 200
    assert snapshot(world)['value'] == [[float(a), float(b)] for a, b in value]


def test_reset_to_default_gives_the_four_corners_and_the_default_origin(world):
    assert patch(world, U).status_code == 200
    r = call(world.adm, 'delete', f'/api/admin/settings/{KEY}')
    assert r.status_code == 200 and r.get_json()['reset'] is True, r.get_data(as_text=True)
    state = snapshot(world)
    assert state['value'] == CORNERS and state['source'] == 'défaut'
    assert options(world)['coverage_polygon'] == CORNERS


BAD = [
    ('liste vide', [], MSG_COUNT), ('2 sommets', [[0, 0], [1, 1]], MSG_COUNT), ('51 sommets', [[i / 100, 50 + i / 100] for i in range(51)], MSG_COUNT),
    ('10 000 sommets', [[1, 2]] * 10_000, MSG_COUNT),
    ('texte', [[1, "50"], [2, 50], [3, 51]], MSG_SHAPE), ('booléen', [[True, 50], [2, 50], [3, 51]], MSG_SHAPE),
    ('3 nombres', [[1, 50, 3], [2, 50], [3, 51]], MSG_SHAPE), ('NaN', [[float('nan'), 50], [2, 50], [3, 51]], MSG_SHAPE),
    ('longitude 10**400', [[10 ** 400, 50], [2, 50], [3, 51]], MSG_BOUNDS), ('latitude 86', [[1, 86], [2, 50], [3, 51]], MSG_BOUNDS),
    ('longitude 181', [[181, 50], [2, 50], [3, 51]], MSG_BOUNDS), ('emprise 2 100 km', [[0, 0], [0, 19], [1, 10]], MSG_BOX),
    ('nœud papillon', [[0, 0], [1, 1], [1, 0], [0, 1]], crossing((1, 2), (3, 4))),
    ('sommet répété', [[0, 0], [1, 0], [1, 0], [1, 1]], "Zone : le contour se croise"),
    ('alignés', [[1, 50], [1, 51], [1, 52]], "Zone : le contour se croise"),
    ('retour en arrière', [[0, 0], [2, 0], [1, 0], [1, 1]], "Zone : le contour se croise"),
    ('pas une liste', 'x', MSG_COUNT), ('nul', None, MSG_COUNT), ('objet', {'a': 1}, MSG_COUNT),
]


@pytest.mark.parametrize('label, value, message', BAD, ids=[b[0] for b in BAD])
def test_every_invalid_polygon_is_a_400_with_the_spec_message_and_nothing_is_written(world, label, value, message):
    assert patch(world, NORTH_FR_BE).status_code == 200
    before = snapshot(world)
    r = patch(world, value)
    assert r.status_code == 400, (label, r.status_code, r.get_data(as_text=True)[:200])
    body = r.get_json()
    assert body['field'] == KEY and message in body['error'], body
    assert snapshot(world) == before                                      # version, valeur et audit inchangés


def test_a_huge_polygon_body_is_a_400_never_a_500(world):
    for value in ([[1, 2]] * 100_000, [[0, 0]] * 3 + [[10 ** 400, 0]], [[2 ** 2000, 1]] * 4):
        assert patch(world, value).status_code == 400


def test_a_stale_version_is_refused_and_writes_nothing(world):
    before = snapshot(world)
    body = {'settings': {KEY: U}, 'version': before['version'] - 1}
    r = call(world.adm, 'patch', '/api/admin/settings', body)
    assert r.status_code == 409 and r.get_json()['code'] == 'settings_version_conflict'
    assert snapshot(world) == before


def forge(world, text):
    world.conn.execute("INSERT OR REPLACE INTO settings (key, value, updated_at, updated_by) VALUES (?, ?, 'x', 'sql')", (KEY, text))
    world.conn.execute("UPDATE settings_meta SET version = version + 1")
    world.conn.commit()
    world.app.config['SETTINGS_STORE'].refresh(world.conn, force=True)


@pytest.mark.parametrize('forged', ['"x"', 'null', '[]', '[[0,0],[1,1]]', '[[0,0],[1,1],[1,0],[0,1]]', '5', '[[1,2,3],[1,2,3],[1,2,3]]'])
def test_a_forged_polygon_in_the_database_reads_back_as_the_default_zone_and_it_applies(world, forged):
    forge(world, forged)
    assert snapshot(world)['value'] == CORNERS
    assert options(world)['coverage_polygon'] == CORNERS and options(world)['coverage_bbox'] == DEFAULT_RECTANGLE
    assert world.route(points=[[51.5072, -0.1276], [51.51, -0.12]], distance_km=None).status_code == 400          # Londres refusé
    assert world.route(points=[[50.63, 3.05], [50.66, 3.10]], distance_km=None).status_code == 200                # Lille accepté


def test_options_by_default_give_the_four_corners_and_their_envelope(world):
    body = options(world)
    assert body['coverage_polygon'] == CORNERS and body['coverage_bbox'] == DEFAULT_RECTANGLE


def test_options_with_a_polygon_give_it_and_its_envelope(world):
    assert patch(world, U).status_code == 200
    body = options(world)
    assert body['coverage_polygon'] == [[float(a), float(b)] for a, b in U] and body['coverage_bbox'] == [2.0, 49.0, 5.0, 51.0]


def test_options_follow_a_four_vertex_polygon_the_way_they_followed_a_rectangle(world):
    rectangle = [[3.0, 50.6], [3.2, 50.6], [3.2, 50.7], [3.0, 50.7]]
    world.set(**{KEY: rectangle})
    body = options(world)
    assert body['coverage_bbox'] == [3.0, 50.6, 3.2, 50.7] and body['coverage_polygon'] == rectangle
    world.set(**{KEY: SMALL_U})
    assert options(world)['coverage_bbox'] == [3.0, 50.4, 3.4, 50.8]


def test_a_point_in_the_notch_is_a_400_before_any_call_to_the_engine(world):
    world.set(**{KEY: U})
    r = world.route(points=[[50.5, 2.5], [50.5, 3.5]], distance_km=None)
    assert r.status_code == 400 and r.get_json()['error'] == "Le point 2 est hors de la zone couverte."
    assert world.provider.calls == []
    r = world.route(points=[[50.5, 3.5], [50.5, 2.5], [50.5, 3.6]], distance_km=None)
    assert r.get_json()['error'] == "Le point 1 est hors de la zone couverte." and world.provider.calls == []     # premier point fautif


def test_points_in_both_branches_a_vertex_and_an_edge_midpoint_are_accepted(world):
    world.set(**{KEY: U})
    for points in ([[50.5, 2.5], [50.5, 4.5]], [[49.0, 2.0], [49.5, 2.2]], [[49.0, 3.5], [49.2, 3.6]], [[50.5, 3.0], [50.6, 2.5]]):
        r = world.route(points=points, distance_km=None)
        assert r.status_code == 200, (points, r.get_data(as_text=True)[:150])


def test_the_point_numbers_are_the_original_ones_even_after_deduplication(world):
    world.set(**{KEY: U})
    r = world.route(points=[[50.5, 2.5], [50.5, 2.5], [50.5, 3.5]], distance_km=None)
    assert r.status_code == 400 and "Le point 3 est hors de la zone couverte." == r.get_json()['error']


def test_the_default_zone_behaves_exactly_as_the_old_rectangle_did(world):
    assert world.route(points=[[50.63, 3.05], [50.66, 3.10]], distance_km=None).status_code == 200
    r = world.route(points=[[50.63, 3.05], [52.37, 4.90]], distance_km=None)                  # Amsterdam
    assert r.status_code == 400 and r.get_json()['error'] == "Le point 2 est hors de la zone couverte."


def waypoints_sent(provider):
    return [p for call_ in provider.calls if call_[0] == 'route' for p in call_[1]]


S1_POINTS = [[50.7, 3.05], [50.7, 3.35], [50.45, 3.2]]


def widened(points, distance_km):
    """Emprise RB-S7 : les points de l'utilisateur élargis de T / 4 (T en mètres), calculée ici sans le code du serveur."""
    margin = distance_km * 1000 / 4
    lats, lons = [p[0] for p in points], [p[1] for p in points]
    dlat = margin / 111_194.9
    dlon = margin / (111_194.9 * math.cos(math.radians(sum(lats) / len(lats))))
    return (min(lats) - dlat, min(lons) - dlon, max(lats) + dlat, max(lons) + dlon)


@pytest.mark.parametrize('distance_km, seed', [(110, 1), (130, 2), (130, 4), (160, 1), (160, 3)])
def test_every_point_sent_to_the_engine_technical_points_included_is_inside_the_zone_and_the_rb_s7_box(tmp_path, distance_km, seed):
    world = build_world(tmp_path)
    try:
        world.set(**{KEY: SMALL_U, 'routing.detour_strategy': 's1'})
        r = world.route(points=S1_POINTS, distance_km=distance_km, seed=seed)
        assert r.status_code == 200, r.get_data(as_text=True)[:200]
        sent = waypoints_sent(world.provider)
        technical = [p for p in sent if [round(p[0], 6), round(p[1], 6)] not in S1_POINTS and tuple(p) not in {tuple(q) for q in S1_POINTS}]
        assert technical, "la distance doit forcer au moins un détour S1"
        box = widened(S1_POINTS, distance_km)
        for lat, lon in sent:
            assert zone.contains(SMALL_U, lat, lon), ('point envoyé hors de la zone', lat, lon)
        for lat, lon in technical:
            assert box[0] - 1e-6 <= lat <= box[2] + 1e-6 and box[1] - 1e-6 <= lon <= box[3] + 1e-6, ('hors emprise RB-S7', lat, lon)
    finally:
        world.conn.close()


def test_without_the_polygon_the_same_request_does_send_technical_points_into_the_notch(tmp_path):
    """Le test précédent a un sens : avec la zone par défaut (le rectangle), les mêmes détours placent des points dans l'échancrure."""
    world = build_world(tmp_path)
    try:
        world.set(**{'routing.detour_strategy': 's1'})
        assert world.route(points=S1_POINTS, distance_km=110, seed=1).status_code == 200
        assert [p for p in waypoints_sent(world.provider) if not zone.contains(SMALL_U, *p)]
    finally:
        world.conn.close()


def test_the_same_seed_twice_gives_the_same_trace_with_a_polygon(tmp_path):
    traces = []
    for run in range(2):
        world = build_world(tmp_path / f'run{run}')
        try:
            world.set(**{KEY: SMALL_U, 'routing.detour_strategy': 's1'})
            r = world.route(points=S1_POINTS, distance_km=130, seed=4)
            assert r.status_code == 200
            traces.append((json.dumps(r.get_json()['points']), waypoints_sent(world.provider)))
        finally:
            world.conn.close()
    assert traces[0] == traces[1]


def test_a_single_point_loop_sends_nothing_outside_the_zone(tmp_path):
    """Un seul point : la boucle vient du moteur (aller-retour), il n'y a pas de point technique S1 ; l'unique point est contrôlé."""
    world = build_world(tmp_path)
    try:
        world.set(**{KEY: SMALL_U})
        assert world.route(points=[[50.45, 3.05]], distance_km=12, seed=3).status_code == 200
        assert [c[0] for c in world.provider.calls] == ['round_trip']
        calls = len(world.provider.calls)
        assert world.route(points=[[50.7, 3.2]], distance_km=12, seed=3).status_code == 400            # dans l'échancrure
        assert len(world.provider.calls) == calls
    finally:
        world.conn.close()


def test_the_zone_controls_the_points_sent_not_the_returned_trace(tmp_path):
    """Une trace peut sortir de la zone entre deux points : seuls les points envoyés sont contrôlés (RB-1.3b)."""
    world = build_world(tmp_path, FakeProvider(sinuosity=0.4))
    try:
        world.set(**{KEY: SMALL_U})
        r = world.route(points=[[50.7, 3.05], [50.7, 3.35]], distance_km=None)               # tronçon qui traverse l'échancrure
        assert r.status_code == 200
        line = next(f for f in r.get_json()['features'] if f.get('type') == 'polyline')['coordinates']
        assert any(not zone.contains(SMALL_U, c[0], c[1]) for c in line), "la trace doit sortir de la zone entre les deux points"
        assert all(zone.contains(SMALL_U, p[0], p[1]) for p in waypoints_sent(world.provider))
    finally:
        world.conn.close()


# ============================================================ F. diagnostic et journaux ========================================

def view(world):
    return next(f for f in call(world.adm, 'get', '/api/admin/features').get_json()['features'] if f['key'] == 'route_builder')['diagnostic']


def test_the_diagnostic_compares_the_envelope_of_the_zone_in_force_with_the_engine_rectangle(tmp_path):
    provider = FakeProvider(sinuosity=0.05, bbox=(48.0, 0.0, 53.0, 8.0))
    world = build_world(tmp_path, provider)
    try:
        assert view(world)['coverage_contained'] is True
        world.set(**{KEY: SMALL_U})
        assert view(world)['coverage_contained'] is True
        world.set(**{KEY: [[-5, 40], [5, 40], [0, 50]]})                  # l'emprise [-5, 40, 5, 50] dépasse celle du moteur
        assert view(world)['coverage_contained'] is False
        world.set(**{KEY: [[3.0, 50.6], [3.2, 50.6], [3.2, 50.7], [3.0, 50.7]]})        # ancien rectangle personnalisé, devenu 4 sommets
        assert view(world)['coverage_contained'] is True
    finally:
        world.conn.close()


def test_no_log_line_contains_a_coordinate(tmp_path, caplog):
    world = build_world(tmp_path)
    try:
        world.set(**{KEY: SMALL_U, 'routing.detour_strategy': 's1'})
        secret_points = [[50.712345, 3.054321], [50.723456, 3.345678], [50.456789, 3.212345]]
        with caplog.at_level(logging.DEBUG):
            assert world.route(points=secret_points, distance_km=130, seed=2).status_code == 200
            refused = world.route(points=[[50.712345, 3.054321], [50.7, 3.2123456]], distance_km=None)       # dans l'échancrure
            assert refused.status_code == 400
            assert patch(world, [[0, 0], [1, 1], [1, 0], [0, 1]]).status_code == 400
        text = caplog.text
        numbers = ['50.712345', '3.054321', '50.723456', '3.345678', '50.456789', '3.212345', '3.2123456']
        assert not [n for n in numbers if n in text], [n for n in numbers if n in text]
        assert re.search(r'\b50\.7\d{2,}', text) is None
    finally:
        world.conn.close()
