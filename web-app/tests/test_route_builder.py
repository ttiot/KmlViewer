"""Détours et orchestration d'une génération (TR2) sur provider factice : CA-R2 à R11, R15, R18, R26, R30 à R33 (ADR-0010)."""

import math
import xml.etree.ElementTree as ET
from datetime import datetime

import pytest

from app.services import geo
from app.services.gpx_parser import GPXParser
from app.services.routing import builder, detours, quality
from app.services.routing.base import (InvalidResponse, NoRouteFound, RouteResult, RoutingTimeout, RoutingUnavailable)
from app.services.routing.builder import BuildConfig, InvalidBuild, UnreachablePoint, generate
from tests.routing_fakes import FakeProvider, length_m

POINTS = [(50.63, 3.05), (50.66, 3.10), (50.64, 3.15), (50.61, 3.10)]        # ~20 km de boucle de base
CFG = BuildConfig()
NS = {'g': 'http://www.topografix.com/GPX/1/1'}


def d0_of(points=POINTS, sinuosity=0.05):
    return generate(FakeProvider(sinuosity=sinuosity), points, 'run', None, 0, CFG).meta['distance_m']


class Counting(FakeProvider):
    """Compte les appels et expose les points techniques reçus."""


# --- CA-R2 : validation, aucun appel ------------------------------------------------------------------------------------------

@pytest.mark.parametrize('points,activity,distance', [
    ([], 'run', 10), ([(50.0 + i / 1000, 3.0) for i in range(51)], 'run', 10), ([(91, 3), (50, 3)], 'run', None),
    ([(float('nan'), 3), (50, 3)], 'run', None), ([(50, 3), (50.1, 3.1)], 'bike', None), ([(50, 3), (50.1, 3.1)], None, None),
    ([(50, 3), (50.1, 3.1)], 'run', 0), ([(50, 3), (50.1, 3.1)], 'run', 301), ([(50, 3), (50.1, 3.1)], 'run', float('inf')),
    ([(50, 3), (50.1, 3.1)], 'run', True), ([(50, 3), (50.1, 3.1)], 'run', '10'), ([(50, 3)], 'run', None),
    ('abc', 'run', 5), ([('a', 'b'), (1, 2)], 'run', None), ([(50, 3), (50.1, 3.1)], 'run', 0.9),
    ([(10, 3), (60, 3)], 'run', 5),                                                                   # diagonale > distance max
])
def test_invalid_input_is_refused_before_any_provider_call(points, activity, distance):
    provider = FakeProvider()
    with pytest.raises(InvalidBuild):
        generate(provider, points, activity, distance, 0, CFG)
    assert provider.calls == []


@pytest.mark.parametrize('seed', [-1, 2 ** 31, 1.5, '1', None, True])
def test_invalid_seed_is_refused_before_any_call(seed):
    provider = FakeProvider()
    with pytest.raises(InvalidBuild):
        generate(provider, POINTS, 'run', None, seed, CFG)
    assert provider.calls == []


def test_a_point_outside_the_configured_coverage_is_refused_without_any_call():
    cfg = BuildConfig(coverage_bbox=(49.0, 2.0, 51.6, 6.5), coverage_polygon=((2.0, 49.0), (6.5, 49.0), (6.5, 51.6), (2.0, 51.6)))
    provider = FakeProvider()
    for outside in ((51.51, -0.13), (52.37, 4.90)):
        with pytest.raises(InvalidBuild):
            generate(provider, [(50.63, 3.06), outside], 'run', None, 0, cfg)
    assert provider.calls == []
    generate(provider, [(50.63, 3.06), (50.63, 5.57)], 'run', None, 0, cfg)             # Lille, Liège : acceptés


# --- CA-R3 : 1 point => round_trip ; >= 2 points => route P1…Pn,P1 -----------------------------------------------------------

def test_one_point_calls_round_trip_and_several_points_call_route_first_with_the_closing_point():
    p = FakeProvider()
    generate(p, [POINTS[0]], 'run', 10, 0, CFG)
    assert p.calls[0][0] == 'round_trip'
    p = FakeProvider()
    generate(p, POINTS, 'run', None, 0, CFG)
    assert [c[0] for c in p.calls] == ['route'] and list(p.calls[0][1]) == POINTS + [POINTS[0]]


# --- CA-R4 : fermeture et ordre, avant et après détours ----------------------------------------------------------------------

@pytest.mark.parametrize('factor', [1.0, 1.3, 1.6, 2.0, 3.0])
@pytest.mark.parametrize('seed', [0, 1, 2])
def test_the_trace_is_closed_and_passes_by_every_point_in_order(factor, seed):
    d0 = d0_of()
    gen = generate(FakeProvider(sinuosity=0.05), POINTS, 'run', d0 * factor / 1000, seed, CFG)
    assert quality.closure_m(gen.points) <= 1.0
    assert quality.ordered_pass(gen.points, POINTS, 50.0)


# --- CA-R5 : D0 dans la tolérance, ou trop long -------------------------------------------------------------------------------

def test_base_loop_within_tolerance_makes_one_call_and_no_detour():
    d0 = d0_of()
    p = FakeProvider(sinuosity=0.05)
    gen = generate(p, POINTS, 'run', d0 * 1.05 / 1000, 0, CFG)
    assert len(p.calls) == 1 and gen.meta['detours'] == 0 and gen.warnings == []


def test_base_loop_longer_than_the_target_is_returned_with_the_minimum_distance_warning():
    d0 = d0_of()
    p = FakeProvider(sinuosity=0.05)
    gen = generate(p, POINTS, 'run', d0 * 0.5 / 1000, 0, CFG)
    assert len(p.calls) == 1 and gen.meta['detours'] == 0
    [warning] = [w for w in gen.warnings if w['code'] == 'min_distance']
    assert f"{d0 / 1000:.1f}" in warning['message']
    assert not any(w['code'] == 'target_gap' for w in gen.warnings)


def test_without_a_target_the_base_loop_is_returned():
    p = FakeProvider()
    gen = generate(p, POINTS, 'run', None, 0, CFG)
    assert len(p.calls) == 1 and gen.meta['target_m'] is None and gen.meta['gap_pct'] is None


# --- CA-R6 : ajustement ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('factor', [1.3, 1.6, 2.0, 3.0])
def test_detours_converge_into_the_tolerance_within_four_iterations(factor):
    d0 = d0_of()
    target = d0 * factor
    gen = generate(FakeProvider(sinuosity=0.05), POINTS, 'run', target / 1000, 3, CFG)
    assert abs(gen.meta['distance_m'] - target) <= 0.15 * target and gen.meta['detours'] > 0 and gen.meta['iterations'] <= 4
    assert not any(w['code'] == 'target_gap' for w in gen.warnings)


class Stubborn(FakeProvider):
    """Ignore les points techniques : les détours ne changent rien (la boucle ne s'allonge jamais)."""

    def __init__(self, user_points, **kw):
        super().__init__(**kw)
        self.user = set(user_points)

    def route(self, waypoints, profile, deadline):
        kept = [p for p in waypoints if tuple(p) in self.user]
        return super().route(kept, profile, deadline)


def test_a_provider_that_never_converges_gives_the_best_trace_and_a_gap_warning():
    d0 = d0_of()
    p = Stubborn(POINTS, sinuosity=0.05)
    gen = generate(p, POINTS, 'run', d0 * 2 / 1000, 0, BuildConfig(detour_strategy='s1'))
    assert gen.meta['distance_m'] == pytest.approx(d0, rel=0.01)
    assert any(w['code'] == 'target_gap' for w in gen.warnings)


# --- CA-R7 : budget ----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('max_calls', [1, 2, 3, 5, 8])
def test_never_more_than_max_calls(max_calls):
    d0 = d0_of()
    p = Stubborn(POINTS, sinuosity=0.05)
    try:
        generate(p, POINTS, 'run', d0 * 3 / 1000, 0, BuildConfig(max_calls=max_calls))
    except (RoutingTimeout, InvalidResponse):
        pass
    assert len(p.calls) <= max_calls


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_never_more_than_the_total_time_budget_with_a_slow_provider():
    clock = Clock()
    p = FakeProvider(sinuosity=0.05, delay_s=4.0, sleep=clock.sleep, clock=clock)
    d0 = d0_of()
    gen = generate(p, POINTS, 'run', d0 * 3 / 1000, 0, BuildConfig(total_budget_s=10.0), clock=clock)
    assert clock() - 1000.0 <= 10.0 + 1e-6 and len(p.calls) <= 3 and gen.points


def test_a_budget_exhausted_before_any_trace_raises_a_timeout():
    clock = Clock()
    p = FakeProvider(delay_s=20.0, sleep=clock.sleep, clock=clock)
    with pytest.raises(RoutingTimeout):
        generate(p, POINTS, 'run', None, 0, BuildConfig(total_budget_s=5.0), clock=clock)


# --- CA-R8 : reproductibilité ------------------------------------------------------------------------------------------------

def test_same_seed_same_inputs_same_trace_and_different_seeds_give_different_traces():
    d0 = d0_of()
    run = lambda seed: generate(FakeProvider(sinuosity=0.05), POINTS, 'run', d0 * 1.6 / 1000, seed, CFG).points   # noqa: E731
    assert run(5) == run(5)
    assert len({tuple(run(s)) for s in range(10)}) >= 2


# --- CA-R9 : points techniques -----------------------------------------------------------------------------------------------

def test_technical_points_are_not_exported_as_waypoints_and_stay_inside_the_expanded_extent():
    d0 = d0_of()
    target = d0 * 2
    p = FakeProvider(sinuosity=0.05)
    gen = generate(p, POINTS, 'run', target / 1000, 1, CFG)
    root = ET.fromstring(gen.gpx)
    wpts = [(float(w.get('lat')), float(w.get('lon'))) for w in root.findall('g:wpt', NS)]
    assert wpts == [(round(a, 6), round(b, 6)) for a, b in POINTS]
    box = builder._technical_bbox(POINTS, target / 4, None)
    sent = [pt for call in p.calls if call[0] == 'route' for pt in call[1]]
    technical = [pt for pt in sent if pt not in POINTS]
    assert technical and all(box[0] <= lat <= box[2] and box[1] <= lon <= box[3] for lat, lon in technical)


def test_technical_points_are_clamped_into_the_expanded_extent_even_for_a_huge_target():
    p = FakeProvider(sinuosity=0.05)
    generate(p, POINTS, 'run', 250, 0, CFG)
    box = builder._technical_bbox(POINTS, 250_000 / 4, None)
    for call in p.calls:
        if call[0] == 'route':
            assert all(box[0] <= lat <= box[2] and box[1] <= lon <= box[3] for lat, lon in call[1])


# --- CA-R10 : recouvrement ---------------------------------------------------------------------------------------------------

def out_and_back(km):
    """Aller-retour pur sur ~km kilomètres, au pas de 25 m (cas construit : recouvrement ~50 %)."""
    n = int(km * 1000 / 25)
    line = [(50.0 + i * 25 / 111_194.9, 3.0) for i in range(n)]
    return line + line[-2::-1]


def test_overlap_rate_of_an_out_and_back_is_about_one_half_and_of_a_clean_loop_is_near_zero():
    assert quality.overlap_rate(out_and_back(5)) == pytest.approx(0.5, abs=0.06)
    loop = generate(FakeProvider(sinuosity=0.05), POINTS, 'run', None, 0, CFG).points
    assert quality.overlap_rate(loop) < 0.05


class OutAndBackProvider(FakeProvider):
    def route(self, waypoints, profile, deadline):
        trace = out_and_back(4)
        return RouteResult(points=trace, distance_m=length_m(trace))


def test_an_overlapping_trace_triggers_the_warning_above_the_threshold():
    pts = [(50.0, 3.0), (50.0 + 2.0 * 1000 / 111_194.9 * 2, 3.0), (50.0 + 1000 / 111_194.9, 3.0)]
    gen = generate(OutAndBackProvider(), pts, 'run', None, 0, CFG)
    assert any(w['code'] == 'overlap' for w in gen.warnings) and gen.meta['overlap_pct'] > 25


# --- CA-R32 : seuil différencié ---------------------------------------------------------------------------------------------

class FixedOverlap(FakeProvider):
    """Trace dont le recouvrement est ~`rate` : une boucle plus un aller-retour dont la part vaut `rate`."""

    def __init__(self, rate):
        super().__init__()
        self.rate = rate

    def route(self, waypoints, profile, deadline):
        total = 10_000.0
        back = total * self.rate                                      # longueur repassée = rate × total
        loop = FakeProvider().route([(50.0, 3.0), (50.03, 3.0), (50.03, 3.04), (50.0, 3.0)], 'foot', deadline).points
        spur = [(50.0 + i * 25 / 111_194.9, 3.0 - 0.05) for i in range(int(back / 2 / 25))]
        trace = list(loop) + [(50.0, 3.0 - 0.05)] + spur + spur[-2::-1] + [(50.0, 3.0)]        # refermée sur le départ
        return RouteResult(points=trace, distance_m=length_m(trace))


@pytest.mark.parametrize('points,rate,warns', [(2, 0.35, False), (2, 0.45, True), (5, 0.30, True), (5, 0.15, False)])
def test_overlap_threshold_depends_on_the_number_of_points(points, rate, warns, monkeypatch):
    pts = [(50.0 + i * 0.001, 3.0) for i in range(points)]
    monkeypatch.setattr(quality, 'overlap_rate', lambda trace: rate)
    gen = generate(FixedOverlap(rate), pts, 'run', None, 0, CFG)
    assert any(w['code'] == 'overlap' for w in gen.warnings) is warns


# --- CA-R11 : GPX ------------------------------------------------------------------------------------------------------------

def test_the_gpx_is_valid_1_1_with_one_track_user_waypoints_stored_type_and_no_time():
    d0 = d0_of()
    gen = generate(FakeProvider(sinuosity=0.05), POINTS, 'gravel', d0 * 1.5 / 1000, 2, CFG, today=datetime(2026, 11, 24))
    root = ET.fromstring(gen.gpx)
    assert root.tag == '{http://www.topografix.com/GPX/1/1}gpx' and root.get('version') == '1.1'
    assert len(root.findall('g:trk', NS)) == 1 and len(root.findall('g:trk/g:trkseg', NS)) == 1
    assert root.find('g:trk/g:type', NS).text == 'road_bike'                                    # gravel : type stocké road_bike
    assert root.find('g:trk/g:name', NS).text == 'Trace planifiée du 24/11/2026'
    assert [w.find('g:name', NS).text for w in root.findall('g:wpt', NS)] == ['P1', 'P2', 'P3', 'P4']
    assert not list(root.iter('{http://www.topografix.com/GPX/1/1}time'))
    assert len(root.findall('g:trk/g:trkseg/g:trkpt', NS)) == len(gen.points)


def test_the_gpx_is_readable_by_the_application_parser():
    gen = generate(FakeProvider(sinuosity=0.05), POINTS, 'run', None, 0, CFG)
    parsed = GPXParser.parse_gpx_coordinates(gen.gpx.decode('utf-8'))
    assert parsed and len(parsed['coordinates']) >= len(gen.points) - 1 if 'coordinates' in parsed else parsed


def test_the_track_name_is_written_through_the_xml_api_never_concatenated():
    gpx = builder.build_gpx([(50.0, 3.0), (50.1, 3.1)], [(50.0, 3.0)], 'run', today=None)
    assert ET.fromstring(gpx).find('g:trk/g:name', NS).text.startswith('Trace planifiée du ')
    assert builder.build_gpx([(50.0, 3.0), (50.1, 3.1)], [(50.0, 3.0)], '</type><x/>')       # type inconnu : échappé, pas injecté
    root = ET.fromstring(builder.build_gpx([(50.0, 3.0), (50.1, 3.1)], [(50.0, 3.0)], '</type><x/>'))
    assert root.find('g:trk/g:x', NS) is None


def test_engine_elevation_is_kept_only_when_complete():
    pts = [(50.0, 3.0), (50.1, 3.1)]
    assert b'<ele>12.5</ele>' in builder.build_gpx(pts, pts[:1], 'run', elevations=[12.5, 13.0])
    assert b'<ele>' not in builder.build_gpx(pts, pts[:1], 'run', elevations=[12.5])
    assert b'<ele>' not in builder.build_gpx(pts, pts[:1], 'run')


# --- CA-R15 : erreurs du moteur ---------------------------------------------------------------------------------------------

def test_engine_errors_without_any_trace_are_raised_as_is():
    for error, expected in ((RoutingUnavailable(), RoutingUnavailable), (InvalidResponse(), InvalidResponse),
                            (RoutingTimeout(), RoutingTimeout)):
        with pytest.raises(expected):
            generate(FakeProvider(fail=error), POINTS, 'run', None, 0, CFG)


def test_an_engine_failure_during_the_detours_keeps_the_base_trace_with_a_gap_warning():
    d0 = d0_of()
    p = FakeProvider(sinuosity=0.05, fail=[None] + [RoutingUnavailable() for _ in range(30)])
    gen = generate(p, POINTS, 'run', d0 * 2 / 1000, 0, CFG)
    assert gen.meta['distance_m'] == pytest.approx(d0, rel=0.01) and any(w['code'] == 'target_gap' for w in gen.warnings)


def test_an_unreachable_point_is_reported_with_its_original_number():
    bad = POINTS[2]
    p = FakeProvider(unroutable=lambda pt: tuple(pt) == bad)
    with pytest.raises(UnreachablePoint) as exc:
        generate(p, POINTS, 'run', None, 0, CFG)
    assert exc.value.number == 3 and isinstance(exc.value, NoRouteFound)


def test_a_point_far_from_the_network_is_unreachable_even_if_the_engine_snaps_it():
    class Snaps(FakeProvider):
        def route(self, waypoints, profile, deadline):
            return super().route([(lat + 0.01, lon) if (lat, lon) == POINTS[1] else (lat, lon) for lat, lon in waypoints], profile,
                                 deadline)
    with pytest.raises(UnreachablePoint) as exc:
        generate(Snaps(), POINTS, 'run', None, 0, CFG)
    assert exc.value.number == 2


# --- CA-R26 : dédoublonnage et numéros d'origine ---------------------------------------------------------------------------

def shifted(point, meters):
    return (point[0] + meters / 111_194.9, point[1])


def test_a_second_point_within_5_m_is_dropped_and_numbers_stay_those_of_the_original_request():
    sent = [POINTS[0], shifted(POINTS[0], 3), POINTS[1], POINTS[2], POINTS[3]]
    gen = generate(FakeProvider(), sent, 'run', None, 0, CFG)
    assert gen.meta['point_numbers'] == [1, 3, 4, 5] and gen.meta['points'] == 4
    bad = POINTS[2]
    with pytest.raises(UnreachablePoint) as exc:
        generate(FakeProvider(unroutable=lambda pt: tuple(pt) == bad), sent, 'run', None, 0, CFG)
    assert exc.value.number == 4


def test_two_coincident_points_without_a_distance_are_refused_and_with_one_become_a_one_point_loop():
    with pytest.raises(InvalidBuild, match='Distance requise'):
        generate(FakeProvider(), [POINTS[0], shifted(POINTS[0], 3)], 'run', None, 0, CFG)
    p = FakeProvider()
    gen = generate(p, [POINTS[0], shifted(POINTS[0], 3)], 'run', 10, 0, CFG)
    assert p.calls[0][0] == 'round_trip' and gen.meta['points'] == 1


def test_a_last_point_within_5_m_of_the_first_is_dropped_with_a_warning():
    sent = POINTS + [shifted(POINTS[0], 3)]
    gen = generate(FakeProvider(), sent, 'run', None, 0, CFG)
    assert gen.meta['point_numbers'] == [1, 2, 3, 4]
    assert [w['code'] for w in gen.warnings] == ['closing_point_removed'] and '5' in gen.warnings[0]['message']


# --- Hybride, essais de graine (CA-R30, R31, R33) ----------------------------------------------------------------------------

class PetalOnly(Stubborn):
    """S1 est sans effet (points techniques ignorés) ; seul un pétale allonge la boucle. `lengths[seed]` : longueur rendue."""

    def __init__(self, user_points, lengths=None, **kw):
        super().__init__(user_points, **kw)
        self.lengths = lengths

    def round_trip(self, start, profile, distance_m, seed, deadline):
        asked = self.lengths.get(seed, distance_m) if self.lengths else distance_m
        return super().round_trip(start, profile, asked, seed, deadline)


def test_hybrid_falls_back_to_a_petal_when_lateral_points_do_not_reach_the_tolerance_CA_R30():
    d0 = d0_of()
    p = PetalOnly(POINTS, sinuosity=0.05)
    gen = generate(p, POINTS, 'run', d0 * 1.6 / 1000, 0, BuildConfig(detour_strategy='hybrid', seed_retries=0))
    names = [c[0] for c in p.calls]
    assert 'round_trip' in names and len(p.calls) <= 15
    assert abs(gen.meta['distance_m'] - d0 * 1.6) <= 0.15 * d0 * 1.6 and gen.meta['strategy'] == 'hybrid'
    s1_only = PetalOnly(POINTS, sinuosity=0.05)
    generate(s1_only, POINTS, 'run', d0 * 1.6 / 1000, 0, BuildConfig(detour_strategy='s1', seed_retries=0))
    assert 'round_trip' not in [c[0] for c in s1_only.calls]


def test_hybrid_without_native_round_trip_reduces_to_lateral_points():
    class NoLoop(FakeProvider):
        supports_round_trip = False
    d0 = d0_of()
    p = NoLoop(sinuosity=0.05)
    generate(p, POINTS, 'run', d0 * 1.6 / 1000, 0, CFG)
    assert 'round_trip' not in [c[0] for c in p.calls]


def test_seed_retries_stop_at_the_first_in_tolerance_trace_and_report_the_attempts_CA_R31():
    d0 = d0_of()
    target = d0 * 1.6
    wanted = target - d0
    # 2 pétales par essai, de graines (s, s+1) : trop courts aux essais 0 et 1, suffisants à l'essai 2 (graines 2 et 3)
    p = PetalOnly(POINTS, lengths={0: wanted * 0.05, 1: wanted * 0.05, 2: wanted * 0.05, 3: wanted * 0.9}, sinuosity=0.05)
    gen = generate(p, POINTS, 'run', target / 1000, 0, BuildConfig(seed_retries=2, max_calls=30))
    assert gen.meta['attempts'] == 3 and gen.meta['last_seed'] == 2
    assert abs(gen.meta['distance_m'] - target) <= 0.15 * target


def test_no_extra_seed_attempt_when_the_remaining_budget_is_too_small_CA_R31():
    d0 = d0_of()
    p = PetalOnly(POINTS, sinuosity=0.05, lengths={0: 10.0, 1: 10.0, 2: 10.0})
    gen = generate(p, POINTS, 'run', d0 * 1.6 / 1000, 0, BuildConfig(seed_retries=2, max_calls=5))
    assert gen.meta['attempts'] == 1 and len(p.calls) <= 5


def test_seed_retries_zero_means_a_single_attempt():
    d0 = d0_of()
    p = PetalOnly(POINTS, sinuosity=0.05, lengths={0: 10.0})
    gen = generate(p, POINTS, 'run', d0 * 1.6 / 1000, 0, BuildConfig(seed_retries=0))
    assert gen.meta['attempts'] == 1


def test_two_points_off_target_after_every_attempt_get_the_suggestion_to_add_a_point_CA_R33():
    pts = POINTS[:2]
    d0 = d0_of(pts)
    p = Stubborn(pts, sinuosity=0.0)
    gen = generate(p, pts, 'run', d0 * 2 / 1000, 0, BuildConfig(detour_strategy='s1'))
    assert any(w['code'] == 'target_gap' for w in gen.warnings)
    [warning] = [w for w in gen.warnings if w['code'] == 'add_waypoint']
    assert 'Ajoute un point de passage' in warning['message']
    five = Stubborn(POINTS, sinuosity=0.0)
    gen = generate(five, POINTS, 'run', d0_of() * 2 / 1000, 0, BuildConfig(detour_strategy='s1'))
    assert any(w['code'] == 'target_gap' for w in gen.warnings) and not any(w['code'] == 'add_waypoint' for w in gen.warnings)


def test_one_point_retries_seeds_until_in_tolerance_and_keeps_the_best():
    class Seeded(FakeProvider):
        def round_trip(self, start, profile, distance_m, seed, deadline):
            return super().round_trip(start, profile, distance_m * (1.0 if seed == 2 else 0.5), seed, deadline)

    p = Seeded()
    gen = generate(p, [POINTS[0]], 'run', 10, 0, CFG)
    assert gen.meta['attempts'] == 3 and abs(gen.meta['distance_m'] - 10_000) <= 1500
    p = Seeded()
    gen = generate(p, [POINTS[0]], 'run', 10, 0, BuildConfig(seed_retries=0))
    assert gen.meta['attempts'] == 1 and any(w['code'] == 'target_gap' for w in gen.warnings)


# --- Stratégies : registre (CA-R18) ------------------------------------------------------------------------------------------

def test_a_new_strategy_is_added_without_touching_the_builder(monkeypatch):
    class Noop(detours.DetourStrategy):
        name = 'noop'

        def run(self, ctx, start=None):
            return detours.Outcome(list(ctx.base_points), ctx.base_distance_m)

    monkeypatch.setitem(detours.STRATEGIES, 'noop', Noop)
    d0 = d0_of()
    gen = generate(FakeProvider(sinuosity=0.05), POINTS, 'run', d0 * 2 / 1000, 0, BuildConfig(detour_strategy='noop'))
    assert gen.meta['strategy'] == 'noop'
    with pytest.raises(ValueError):
        detours.get_strategy('inconnue')


def test_candidates_that_break_the_order_or_the_closure_are_discarded():
    class Reverses(FakeProvider):
        """Rend, pour tout appel avec points techniques, une trace qui ne passe pas par P2."""

        def route(self, waypoints, profile, deadline):
            if len(waypoints) > len(POINTS) + 1:
                return super().route([waypoints[0], waypoints[-1]], profile, deadline)
            return super().route(waypoints, profile, deadline)

    d0 = d0_of()
    gen = generate(Reverses(sinuosity=0.05), POINTS, 'run', d0 * 2 / 1000, 0, BuildConfig(detour_strategy='s1', seed_retries=0))
    assert quality.ordered_pass(gen.points, POINTS, 50.0) and quality.closure_m(gen.points) <= 1.0


def test_unroutable_technical_points_are_moved_before_giving_up():
    d0 = d0_of()
    box_points = set(POINTS)
    p = FakeProvider(sinuosity=0.05, unroutable=lambda pt: tuple(pt) not in box_points and pt[0] > 50.655)
    gen = generate(p, POINTS, 'run', d0 * 1.5 / 1000, 1, CFG)
    assert gen.points and quality.ordered_pass(gen.points, POINTS, 50.0)


def test_config_from_settings_reads_the_registry_keys():
    values = {'routing.max_waypoints': 10, 'routing.max_distance_km': 100, 'routing.distance_tolerance_pct': 20,
              'routing.snap_tolerance_m': 60, 'routing.overlap_warning_pct': 30, 'routing.overlap_warning_pct_2pts': 45,
              'routing.max_calls': 9, 'routing.total_budget_s': 12.5, 'routing.detour_strategy': 's1', 'routing.seed_retries': 1}
    cfg = BuildConfig.from_settings(lambda key: values[key])
    assert (cfg.max_waypoints, cfg.tolerance_pct, cfg.overlap_warning_pct_2pts, cfg.detour_strategy, cfg.coverage_bbox) == \
        (10, 20.0, 45.0, 's1', (48.83, 1.35, 51.55, 6.42))          # sans la clé de zone : la zone par défaut (lot Z1), jamais « aucune zone »
    assert len(cfg.coverage_polygon) == 4
    assert math.isclose(cfg.total_budget_s, 12.5)


def test_haversine_length_is_used_for_distance_and_matches_the_geometry():
    gen = generate(FakeProvider(sinuosity=0.05), POINTS, 'run', None, 0, CFG)
    arr = list(gen.points)
    assert gen.meta['distance_m'] == pytest.approx(sum(float(geo.haversine(a[0], a[1], b[0], b[1])) for a, b in zip(arr, arr[1:])))


# --- TR2-1 / TR2-2 : coût et bornes sur trace hostile ---------------------------------------------------------------------------

def test_the_overlap_of_a_hostile_trace_is_computed_in_linear_time():
    import time as real_time
    base = 50.0
    trace = [(base + (i % 2) * 1e-6, 3.0 + (i % 3) * 1e-6) for i in range(60_000)]         # tout dans quelques mètres
    trace += [(base + 0.05, 3.0 + i * 1e-4) for i in range(60_000)]
    started = real_time.monotonic()
    quality.overlap_rate(trace)
    assert real_time.monotonic() - started < 5.0


def test_a_grafted_trace_never_exceeds_the_file_point_limit():
    from app.services.file_formats import MAX_FILE_POINTS
    points = [(50.0 + i * 1e-7, 3.0) for i in range(MAX_FILE_POINTS + 5000)]
    out = quality.decimate(points, MAX_FILE_POINTS)
    assert len(out) == MAX_FILE_POINTS and out[0] == points[0] and out[-1] == points[-1]
    assert quality.decimate(points[:5], 10) == points[:5]


# --- « Autre proposition » (RB-4.5, RB-4.12, P-TR5-4) --------------------------------------------------------------------------------

def _fingerprint(points):
    return hash(tuple((round(lat, 5), round(lon, 5)) for lat, lon in points[::5]))


def test_consecutive_seeds_give_distinct_traces_so_another_proposal_is_never_the_same_trace():
    d0 = d0_of()
    traces = [generate(FakeProvider(sinuosity=0.05), POINTS, 'run', d0 * 1.6 / 1000, seed, CFG) for seed in range(1, 9)]
    assert len({_fingerprint(g.points) for g in traces}) == 8
    again = generate(FakeProvider(sinuosity=0.05), POINTS, 'run', d0 * 1.6 / 1000, 3, CFG)
    assert _fingerprint(again.points) == _fingerprint(traces[2].points)             # même graine : même trace (RB-4.4)


def test_two_points_also_vary_with_the_seed():
    pts = POINTS[:2]
    d0 = d0_of(pts)
    prints = {_fingerprint(generate(FakeProvider(sinuosity=0.05), pts, 'run', d0 * 1.8 / 1000, seed, CFG).points) for seed in range(1, 9)}
    assert len(prints) >= 6


def test_next_seed_follows_the_last_tried_seed_and_the_retained_seed_is_reported():
    d0 = d0_of()
    p = PetalOnly(POINTS, lengths={0: 10.0, 1: 10.0, 2: 10.0}, sinuosity=0.05)
    gen = generate(p, POINTS, 'run', d0 * 1.6 / 1000, 0, BuildConfig(seed_retries=2, max_calls=30))
    assert gen.meta['last_seed'] == 2 and gen.meta['seed_used'] in (0, 1, 2)
    assert gen.meta['seed_dependent'] is True
    base = generate(FakeProvider(sinuosity=0.05), POINTS, 'run', None, 4, CFG)
    assert base.meta['seed_dependent'] is False and base.meta['strategy'] == 'base'


def test_avoid_skips_a_known_geometry_and_warns_when_nothing_else_exists():
    d0 = d0_of()
    first = generate(FakeProvider(sinuosity=0.05), POINTS, 'run', d0 * 1.6 / 1000, 1, CFG)
    avoid = frozenset({first.meta['fingerprint']})
    prepared = builder.prepare(POINTS, 'run', d0 * 1.6 / 1000, 1, CFG)
    other = builder.execute(FakeProvider(sinuosity=0.05), prepared, CFG, avoid=avoid)
    assert other.meta['fingerprint'] not in avoid and other.meta['attempts'] >= 2 and other.meta['seed_used'] > 1
    assert not any(w['code'] == 'no_alternative' for w in other.warnings)
    base = generate(FakeProvider(sinuosity=0.05), POINTS, 'run', None, 1, CFG)                  # boucle de base : aucune variante
    again = builder.execute(FakeProvider(sinuosity=0.05), builder.prepare(POINTS, 'run', None, 2, CFG), CFG,
                            avoid=frozenset({base.meta['fingerprint']}))
    assert any(w['code'] == 'no_alternative' for w in again.warnings) and again.meta['fingerprint'] == base.meta['fingerprint']


def test_fingerprints_follow_the_geometry_only():
    a = [(50.0 + i * 1e-4, 3.0) for i in range(40)]
    assert quality.fingerprint(a) == quality.fingerprint(list(a))
    assert quality.fingerprint(a) != quality.fingerprint(a[::-1]) and quality.fingerprint(a) != quality.fingerprint(a[:-1])
