"""
Machine à états de la couverture pilotée par la zone (rôle test) : routing-zone-donnees ZD-1, ZD-3, ZD-8, ZD-10, ZD-13 ;
CA-ZD1 (activation), CA-ZD4, CA-ZD6, CA-ZD7. Écrit depuis la spec : statuts, entrées et sorties du tableau de ZD-3, sorties des
sous-commandes de ZD-10. Horloge INJECTÉE (`now`), jamais l'heure réelle ; la CLI a ses tests (test_routing_zone_cli.py), l'API et
les routes les leurs (test_routing_zone_api.py).

 A. ZD-3 : statut selon la zone demandée et la zone active, enregistrement pendant une tâche, zone active et acceptation des points
 B. `claim` : délai de stabilité, charge utile, une seule prise, mensuelle, prise expirée, plafonds
 C. `switching`, `done`, `failed` : demande dépassée, recalcul, échec de la dernière, contenu pris au `claim`
 D. `report` : réalignement une seule fois, zone invalide dédoublonnée, `--historic`, budget
 E. dalles de la tâche (ZD-8) et réveil du MNT au tick suivant
 F. audit (ZD-13) : une entrée par changement, acteur `system`, aucune coordonnée
 G. activation (ZD-1), `ROUTING_ZONE_BUILD=0`, concurrence
"""

import json
import re
import threading
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app import background
from app.audit import Actor
from app.services import ingest_service
from app.services.routing import coverage as geometry
from app.services.routing import coverage_state as coverage
from tests.pw_harness import connect
from tests.routing_zone_support import (DEFAULT_REGIONS, DEFAULT_ZONE, GRAPH, GRAPH2, LILLE_ZONE, T0, after, audit_mark, frozen_document,
                                        load_catalog, reset_state, routing_audit, set_active, state_row, zone_app)

LUX_ZONE = [[5.8, 49.5], [6.2, 49.5], [6.2, 49.9], [5.8, 49.9]]
SETTLE = 300
MARGIN = 10


@pytest.fixture
def env(tmp_path):
    app = zone_app(tmp_path)
    conn = connect(app)
    reset_state(conn)
    load_catalog(conn, app, frozen_document())
    yield SimpleNamespace(app=app, conn=conn, cfg=app.config)
    conn.close()


def ask(env, polygon, now=T0, reason='zone'):
    coverage.register_request(env.conn, env.cfg, polygon, now, reason)


def take(env, requested, now, monthly=False, margin=MARGIN):
    return coverage.claim(env.conn, env.cfg, requested, margin, now, monthly)


def status(env):
    return coverage.read(env.conn)['status']


def active_default(env):
    set_active(env.conn, DEFAULT_ZONE, regions=DEFAULT_REGIONS)
    assert status(env) == 'idle'


def claim_lille(env, base=T0):
    """Zone active = défaut ; zone demandée = Lille, enregistrée à `base`, prise 300 s après."""
    active_default(env)
    ask(env, LILLE_ZONE, base)
    payload = take(env, LILLE_ZONE, after(SETTLE, base))
    assert payload and status(env) == 'building'
    return payload


# ===================================================== A. ZD-3 : statut et zone active ========================================

def test_the_row_is_born_historic_and_idle_and_the_points_follow_the_requested_zone(env):
    state = coverage.read(env.conn)
    assert state['status'] == 'idle' and state['active_polygon'] is None and state['active_regions'] == []
    assert coverage.accepted_zone(env.conn, env.cfg, LILLE_ZONE) == LILLE_ZONE                # « sources historiques » : la zone demandée


def test_the_requested_zone_equal_to_the_active_one_is_idle_any_other_is_pending_with_its_reason(env):
    active_default(env)
    ask(env, DEFAULT_ZONE)
    assert status(env) == 'idle'
    ask(env, LILLE_ZONE)
    state = coverage.read(env.conn)
    assert state['status'] == 'pending' and state['reason'] == 'zone' and state['request_id'] == 2 and state['request_polygon'] == LILLE_ZONE
    ask(env, DEFAULT_ZONE)                                                                    # retour à la zone active
    state = coverage.read(env.conn)
    assert state['status'] == 'idle' and state['request_id'] == 3 and state['reason'] is None


def test_equality_is_on_the_vertex_lists_ints_equal_floats_a_rotated_list_is_another_request(env):
    set_active(env.conn, LILLE_ZONE)
    ask(env, [[3, 50.4], [3.4, 50.4], [3.4, 50.8], [3, 50.8]])                                 # 3 et 3.0 : même sommet
    assert status(env) == 'idle'
    ask(env, LILLE_ZONE[1:] + LILLE_ZONE[:1])                                                 # « listes de sommets égales » (ZD-3), à la lettre
    assert status(env) == 'pending'


def test_a_request_during_building_or_starting_does_not_change_the_status_and_drops_the_one_in_progress(env):
    payload = claim_lille(env)
    ask(env, LUX_ZONE, after(SETTLE + 5))
    state = coverage.read(env.conn)
    assert state['status'] == 'building' and state['request_id'] == payload['request_id'] + 1 and state['claim_id'] == payload['request_id']
    reset_state(env.conn)
    active_default(env)
    ask(env, LILLE_ZONE)
    first = take(env, LILLE_ZONE, after(SETTLE))
    coverage.switching(env.conn, first['request_id'], LILLE_ZONE, after(SETTLE + 10))
    assert status(env) == 'starting'
    ask(env, LUX_ZONE, after(SETTLE + 20))
    assert status(env) == 'starting' and coverage.read(env.conn)['request_id'] == first['request_id'] + 1


def test_the_points_follow_the_active_zone_once_there_is_one_and_the_requested_zone_before(env):
    active_default(env)
    ask(env, LILLE_ZONE)
    assert coverage.accepted_zone(env.conn, env.cfg, LILLE_ZONE) == [[float(a), float(b)] for a, b in DEFAULT_ZONE]
    reset_state(env.conn)
    assert coverage.accepted_zone(env.conn, env.cfg, LILLE_ZONE) == LILLE_ZONE


# ===================================================== B. claim ===============================================================

def test_claim_before_the_settling_delay_gives_nothing_after_it_gives_the_task_and_a_second_claim_gives_nothing(env):
    active_default(env)
    ask(env, LILLE_ZONE, T0)
    assert take(env, LILLE_ZONE, after(SETTLE - 1)) == {} and status(env) == 'pending'
    payload = take(env, LILLE_ZONE, after(SETTLE))                                            # « au moins ROUTING_ZONE_SETTLE_S »
    assert payload and status(env) == 'building'
    assert take(env, LILLE_ZONE, after(SETTLE + 1)) == {} and take(env, LILLE_ZONE, after(SETTLE + 1), monthly=True) == {}
    assert status(env) == 'building'


def test_the_settling_delay_is_the_configured_one(tmp_path):
    app = zone_app(tmp_path, ROUTING_ZONE_SETTLE_S=60)
    conn = connect(app)
    reset_state(conn)
    load_catalog(conn, app, frozen_document())
    set_active(conn, DEFAULT_ZONE)
    coverage.register_request(conn, app.config, LILLE_ZONE, T0)
    assert coverage.claim(conn, app.config, LILLE_ZONE, MARGIN, after(59), False) == {}
    assert coverage.claim(conn, app.config, LILLE_ZONE, MARGIN, after(60), False)
    conn.close()


def test_the_claim_carries_region_identifiers_only_never_a_url_or_a_size(env):
    """ADR-0013 point 3 et §6 : l'application ne renvoie à l'hôte que des identifiants de régions et des nombres ; les URL viennent de la
    copie locale du catalogue de l'hôte, jamais de la sortie de l'application (une application compromise ne désigne pas une URL)."""
    payload = claim_lille(env)
    assert set(payload) == {'request_id', 'reason', 'polygon', 'extract_bbox', 'regions', 'dem_tiles', 'margin_km'}
    assert payload['request_id'] == 1 and payload['reason'] == 'zone' and payload['margin_km'] == MARGIN
    assert payload['polygon'] == LILLE_ZONE and payload['extract_bbox'] == [2.8577, 50.31, 3.5423, 50.89]
    assert payload['regions'] == ['belgium', 'nord-pas-de-calais']
    assert sorted(tuple(t) for t in payload['dem_tiles']) == [(50, 2), (50, 3)]
    text = json.dumps(payload)
    assert 'http' not in text and 'geofabrik' not in text and 'size' not in text and '.pbf' not in text


def test_the_content_taken_at_the_claim_is_the_identifier_list_and_done_declares_it_active(env):
    payload = claim_lille(env)
    coverage.switching(env.conn, payload['request_id'], LILLE_ZONE, after(SETTLE + 10))
    coverage.done(env.conn, payload['request_id'], GRAPH, '2026-11-24', [], LILLE_ZONE, after(SETTLE + 20))
    assert coverage.read(env.conn)['active_regions'] == ['belgium', 'nord-pas-de-calais']
    assert 'http' not in (state_row(env.conn)['claim_payload'] or '')


def test_the_margin_is_read_at_the_claim_not_at_the_registration(env):
    active_default(env)
    ask(env, LILLE_ZONE)
    payload = take(env, LILLE_ZONE, after(SETTLE), margin=30)
    assert payload['margin_km'] == 30 and payload['extract_bbox'] == geometry.extract_bbox(LILLE_ZONE, 30)
    assert payload['extract_bbox'][0] < 2.8577 and payload['extract_bbox'][3] > 50.89


def test_a_claim_with_nothing_to_do_gives_nothing_and_writes_nothing(env):
    active_default(env)
    before, mark = state_row(env.conn), audit_mark(env.conn)
    assert take(env, DEFAULT_ZONE, after(SETTLE)) == {}
    assert state_row(env.conn) == before and routing_audit(env.conn, mark) == []


def test_a_monthly_claim_on_an_idle_install_rebuilds_the_active_zone_at_once_with_the_reason_monthly(env):
    active_default(env)
    payload = take(env, DEFAULT_ZONE, after(5), monthly=True)
    assert payload['reason'] == 'mensuel' and payload['polygon'] == DEFAULT_ZONE and status(env) == 'building'
    assert payload['regions'] == DEFAULT_REGIONS


def test_a_monthly_claim_takes_the_pending_request_but_only_once_it_has_settled(env):
    active_default(env)
    ask(env, LILLE_ZONE, T0)
    assert take(env, LILLE_ZONE, after(SETTLE - 1), monthly=True) == {}                       # une saisie récente n'est jamais prise
    payload = take(env, LILLE_ZONE, after(SETTLE), monthly=True)
    assert payload['reason'] == 'mensuel' and payload['polygon'] == LILLE_ZONE


def test_a_failed_request_is_never_claimed_until_retried_and_the_retry_restarts_the_settling_delay(env):
    payload = claim_lille(env)
    coverage.failed(env.conn, payload['request_id'], 'download', LILLE_ZONE, after(SETTLE + 60))
    assert status(env) == 'failed'
    assert take(env, LILLE_ZONE, after(10 * SETTLE)) == {} and take(env, LILLE_ZONE, after(10 * SETTLE), monthly=True) == {}
    assert status(env) == 'failed'
    retry_at = after(10 * SETTLE)
    assert coverage.retry(env.conn, LILLE_ZONE, retry_at, Actor.cli()) == 'pending' and status(env) == 'pending'
    assert take(env, LILLE_ZONE, retry_at + timedelta(seconds=SETTLE - 1)) == {}
    again = take(env, LILLE_ZONE, retry_at + timedelta(seconds=SETTLE))
    assert again and again['polygon'] == LILLE_ZONE


def test_a_claim_older_than_two_hours_expires_and_is_taken_again_with_one_audit_entry(env):
    payload = claim_lille(env)
    claimed = after(SETTLE)
    assert take(env, LILLE_ZONE, claimed + timedelta(hours=2) - timedelta(seconds=1)) == {} and status(env) == 'building'
    mark = audit_mark(env.conn)
    again = take(env, LILLE_ZONE, claimed + timedelta(hours=2))
    assert again and again['request_id'] == payload['request_id'] and status(env) == 'building'
    assert [a for _, a in routing_audit(env.conn, mark)].count('routing.claim_expired') == 1


def test_the_stuck_task_button_only_works_after_two_hours_then_recomputes_the_status(env):
    claim_lille(env)
    claimed = after(SETTLE)
    with pytest.raises(coverage.CoverageError):
        coverage.release_stuck(env.conn, LILLE_ZONE, claimed + timedelta(hours=2) - timedelta(seconds=1), Actor.cli())
    assert status(env) == 'building'
    mark = audit_mark(env.conn)
    assert coverage.release_stuck(env.conn, LILLE_ZONE, claimed + timedelta(hours=2), Actor.cli()) == 'pending'
    assert status(env) == 'pending' and len(routing_audit(env.conn, mark)) == 1
    with pytest.raises(coverage.CoverageError):                                               # plus rien à débloquer
        coverage.release_stuck(env.conn, LILLE_ZONE, claimed + timedelta(hours=5), Actor.cli())


def test_a_request_over_the_caps_fails_with_the_code_cap_at_the_claim_and_raises_the_alert(tmp_path):
    app = zone_app(tmp_path, ROUTING_MAX_REGIONS=3)
    conn = connect(app)
    reset_state(conn)
    load_catalog(conn, app, frozen_document())
    coverage.register_request(conn, app.config, DEFAULT_ZONE, T0, reason='activation')         # 17 régions pour 3 au plus
    assert coverage.claim(conn, app.config, DEFAULT_ZONE, MARGIN, after(SETTLE), False) == {}
    state = coverage.read(conn)
    assert state['status'] == 'failed' and state['last_failure_code'] == 'cap'
    assert [a['message'] for a in coverage.alerts(conn, app.config)] == [
        "La zone dépasse les plafonds : réduis-la ou relève les plafonds (exploitation)."]
    assert coverage.claim(conn, app.config, DEFAULT_ZONE, MARGIN, after(2 * SETTLE), False) == {}       # jamais reprise sans « Réessayer »
    conn.close()


def test_a_claim_without_a_catalog_waits_it_does_not_fail(tmp_path):
    app = zone_app(tmp_path)
    conn = connect(app)
    reset_state(conn)
    coverage.register_request(conn, app.config, DEFAULT_ZONE, T0)
    assert coverage.claim(conn, app.config, DEFAULT_ZONE, MARGIN, after(SETTLE), False) == {}
    assert coverage.read(conn)['status'] == 'pending'
    conn.close()


# ===================================================== C. switching, done, failed =============================================

def test_the_normal_cycle_goes_building_starting_idle_and_the_new_zone_becomes_the_active_one(env):
    payload = claim_lille(env)
    rid = payload['request_id']
    coverage.switching(env.conn, rid, LILLE_ZONE, after(SETTLE + 100))
    assert status(env) == 'starting'
    assert coverage.done(env.conn, rid, GRAPH, '2026-11-24', [[50, 2], [50, 3]], LILLE_ZONE, after(SETTLE + 400)) == {'status': 'idle'}
    state = coverage.read(env.conn)
    assert state['status'] == 'idle' and state['reason'] is None and state['active_polygon'] == LILLE_ZONE
    assert state['active_regions'] == ['belgium', 'nord-pas-de-calais'] and state['active_graph'] == GRAPH
    assert state['active_extract_bbox'] == [2.8577, 50.31, 3.5423, 50.89] and state['data_date'] == '2026-11-24'
    assert state['task_tiles'] == [[50, 2], [50, 3]] and state['claim_id'] is None and state['last_failure_code'] is None
    assert coverage.accepted_zone(env.conn, env.cfg, LILLE_ZONE) == LILLE_ZONE


def test_done_takes_the_content_recorded_at_the_claim_not_a_new_calculation(env):
    """Un catalogue ou une marge changés entre-temps ne modifient pas ce qui est déclaré actif (ZD-2)."""
    from tests.routing_zone_support import rect, synthetic
    payload = claim_lille(env)
    load_catalog(env.conn, env.app, synthetic(rect('only-one', 0, 0, 10, 10, size=1)), after(SETTLE + 5))
    coverage.switching(env.conn, payload['request_id'], LILLE_ZONE, after(SETTLE + 10))
    coverage.done(env.conn, payload['request_id'], GRAPH, '2026-11-24', [], LILLE_ZONE, after(SETTLE + 20))
    state = coverage.read(env.conn)
    assert state['active_regions'] == ['belgium', 'nord-pas-de-calais'] and state['active_extract_bbox'] == payload['extract_bbox']
    assert state['active_polygon'] == LILLE_ZONE


def test_a_new_zone_during_the_task_makes_it_stale_at_switching_then_the_status_is_recomputed_to_pending(env):
    payload = claim_lille(env)
    ask(env, LUX_ZONE, after(SETTLE + 30))
    mark = audit_mark(env.conn)
    with pytest.raises(coverage.StaleRequest):
        coverage.switching(env.conn, payload['request_id'], LUX_ZONE, after(SETTLE + 60))
    state = coverage.read(env.conn)
    assert state['status'] == 'pending' and state['claim_id'] is None and state['active_polygon'] == [[float(a), float(b)] for a, b in DEFAULT_ZONE]
    assert [a for _, a in routing_audit(env.conn, mark)] == ['routing.request_dropped']       # « demande dépassée jetée » : une entrée
    with pytest.raises(coverage.StaleRequest):                                                # la tâche jetée n'a plus de numéro valide
        coverage.done(env.conn, payload['request_id'], GRAPH, '2026-11-24', [], LUX_ZONE, after(SETTLE + 70))
    assert state_row(env.conn)['active_graph'] == GRAPH


def test_going_back_to_the_active_zone_during_the_task_gives_idle_at_switching(env):
    payload = claim_lille(env)
    ask(env, DEFAULT_ZONE, after(SETTLE + 30))
    assert status(env) == 'building'
    with pytest.raises(coverage.StaleRequest):
        coverage.switching(env.conn, payload['request_id'], DEFAULT_ZONE, after(SETTLE + 60))
    assert status(env) == 'idle'


def test_the_failure_of_a_stale_request_recomputes_and_never_blocks_the_most_recent_one(env):
    payload = claim_lille(env)
    ask(env, LUX_ZONE, after(SETTLE + 30))
    assert coverage.failed(env.conn, payload['request_id'], 'import', LUX_ZONE, after(SETTLE + 60)) == {'status': 'pending'}
    state = coverage.read(env.conn)
    assert state['status'] == 'pending' and state['last_failure_code'] is None
    nxt = take(env, LUX_ZONE, after(SETTLE + 30 + SETTLE))
    assert nxt and nxt['polygon'] == LUX_ZONE and nxt['request_id'] == payload['request_id'] + 1


def test_the_failure_of_the_latest_request_is_failed_with_its_code_and_the_active_content_is_unchanged(env):
    payload = claim_lille(env)
    before = {k: v for k, v in coverage.read(env.conn).items() if k.startswith('active_') or k in ('data_date', 'task_tiles')}
    assert coverage.failed(env.conn, payload['request_id'], 'budget', LILLE_ZONE, after(SETTLE + 60)) == {'status': 'failed'}
    state = coverage.read(env.conn)
    assert state['status'] == 'failed' and state['last_failure_code'] == 'budget' and state['claim_id'] is None
    assert {k: v for k, v in state.items() if k in before} == before
    assert [a['code'] for a in coverage.alerts(env.conn, env.cfg)] == ['coverage_failed']
    assert 'budget' in coverage.alerts(env.conn, env.cfg)[0]['message']


def test_a_health_failure_after_the_switch_goes_back_to_failed_and_keeps_the_previous_coverage(env):
    payload = claim_lille(env)
    coverage.switching(env.conn, payload['request_id'], LILLE_ZONE, after(SETTLE + 10))
    assert coverage.failed(env.conn, payload['request_id'], 'health', LILLE_ZONE, after(SETTLE + 50)) == {'status': 'failed'}
    state = coverage.read(env.conn)
    assert state['status'] == 'failed' and state['active_polygon'] == [[float(a), float(b)] for a, b in DEFAULT_ZONE]
    assert state['active_graph'] == GRAPH


def test_the_monthly_cycle_goes_through_starting_and_done_leaves_idle_without_any_realignment_entry(env):
    active_default(env)
    mark = audit_mark(env.conn)
    payload = take(env, DEFAULT_ZONE, after(5), monthly=True)
    coverage.switching(env.conn, payload['request_id'], DEFAULT_ZONE, after(60))
    assert status(env) == 'starting' and coverage.read(env.conn)['reason'] == 'mensuel'
    coverage.done(env.conn, payload['request_id'], GRAPH2, '2026-11-27', [], DEFAULT_ZONE, after(600))
    state = coverage.read(env.conn)
    assert state['status'] == 'idle' and state['active_graph'] == GRAPH2
    assert 'routing.realigned' not in [a for _, a in routing_audit(env.conn, mark)]
    # le rapport suivant de l'hôte (même zone, nouveau graphe) ne réaligne rien non plus
    mark = audit_mark(env.conn)
    set_active(env.conn, DEFAULT_ZONE, after(660), graph=GRAPH2, regions=DEFAULT_REGIONS)
    assert 'routing.realigned' not in [a for _, a in routing_audit(env.conn, mark)]


@pytest.mark.parametrize('call', [
    lambda c, rid: coverage.switching(c, rid, DEFAULT_ZONE, T0),
    lambda c, rid: coverage.done(c, rid, GRAPH, '2026-11-24', [], DEFAULT_ZONE, T0),
    lambda c, rid: coverage.failed(c, rid, 'other', DEFAULT_ZONE, T0),
], ids=['switching', 'done', 'failed'])
def test_an_unknown_request_number_is_stale_and_writes_nothing(env, call):
    claim_lille(env)
    before, mark = state_row(env.conn), audit_mark(env.conn)
    with pytest.raises(coverage.StaleRequest):
        call(env.conn, 999)
    assert state_row(env.conn) == before and routing_audit(env.conn, mark) == []


@pytest.mark.parametrize('call', [
    lambda c: coverage.switching(c, 0, DEFAULT_ZONE, T0), lambda c: coverage.switching(c, -3, DEFAULT_ZONE, T0),
    lambda c: coverage.switching(c, True, DEFAULT_ZONE, T0), lambda c: coverage.switching(c, '1', DEFAULT_ZONE, T0),
    lambda c: coverage.done(c, 1, 'graph-123', '2026-11-24', [], DEFAULT_ZONE, T0),
    lambda c: coverage.done(c, 1, 'graph-20261126120000x', '2026-11-24', [], DEFAULT_ZONE, T0),
    lambda c: coverage.done(c, 1, 'graph-2026112612000a', '2026-11-24', [], DEFAULT_ZONE, T0),
    lambda c: coverage.done(c, 1, '../graph-20261126120000', '2026-11-24', [], DEFAULT_ZONE, T0),
    lambda c: coverage.done(c, 1, GRAPH, '2026-13-40', [], DEFAULT_ZONE, T0),
    lambda c: coverage.done(c, 1, GRAPH, '26/11/2026', [], DEFAULT_ZONE, T0),
    lambda c: coverage.done(c, 1, GRAPH, None, [], DEFAULT_ZONE, T0),
    lambda c: coverage.done(c, 1, GRAPH, '2026-11-24', 'x', DEFAULT_ZONE, T0),
    lambda c: coverage.done(c, 1, GRAPH, '2026-11-24', [[1]], DEFAULT_ZONE, T0),
    lambda c: coverage.done(c, 1, GRAPH, '2026-11-24', [[1, 2, 3]], DEFAULT_ZONE, T0),
    lambda c: coverage.done(c, 1, GRAPH, '2026-11-24', [[True, 2]], DEFAULT_ZONE, T0),
    lambda c: coverage.done(c, 1, GRAPH, '2026-11-24', [[91, 2]], DEFAULT_ZONE, T0),
    lambda c: coverage.done(c, 1, GRAPH, '2026-11-24', [[1, 181]], DEFAULT_ZONE, T0),
    lambda c: coverage.done(c, 1, GRAPH, '2026-11-24', [[1.5, 2]], DEFAULT_ZONE, T0),
    lambda c: coverage.done(c, 1, GRAPH, '2026-11-24', [[1, 2]] * 2001, DEFAULT_ZONE, T0),
    lambda c: coverage.failed(c, 1, 'inconnu', DEFAULT_ZONE, T0), lambda c: coverage.failed(c, 1, '', DEFAULT_ZONE, T0),
    lambda c: coverage.failed(c, 1, 'cap; rm -rf /', DEFAULT_ZONE, T0), lambda c: coverage.failed(c, 1, None, DEFAULT_ZONE, T0),
], ids=lambda f: None)
def test_invalid_arguments_are_refused_before_anything_is_written(env, call):
    claim_lille(env)
    before, mark = state_row(env.conn), audit_mark(env.conn)
    with pytest.raises(coverage.CoverageError):
        call(env.conn)
    assert state_row(env.conn) == before and routing_audit(env.conn, mark) == []


@pytest.mark.parametrize('code', ['catalog', 'cap', 'budget', 'download', 'merge', 'extract', 'import', 'incomplete', 'dem', 'disk', 'timeout',
                                  'health', 'other'])
def test_every_failure_code_of_the_spec_is_accepted(env, code):
    payload = claim_lille(env)
    assert coverage.failed(env.conn, payload['request_id'], code, LILLE_ZONE, after(SETTLE + 5)) == {'status': 'failed'}
    assert coverage.read(env.conn)['last_failure_code'] == code


# ===================================================== D. report ==============================================================

def zone_info(polygon, regions=('belgium',)):
    return {'polygon': [[float(a), float(b)] for a, b in polygon], 'extract_bbox': geometry.extract_bbox(polygon, MARGIN), 'regions': list(regions)}


def test_a_reported_zone_different_from_the_active_one_realigns_with_one_audit_entry_and_ten_identical_reports_add_none(env):
    active_default(env)
    mark = audit_mark(env.conn)
    for n in range(10):
        result = coverage.report(env.conn, GRAPH2, zone_info(LILLE_ZONE), False, None, DEFAULT_ZONE, after(n))
        assert result == {'realigned': n == 0}, n
    state = coverage.read(env.conn)
    assert state['active_polygon'] == LILLE_ZONE and state['active_graph'] == GRAPH2 and state['active_regions'] == ['belgium']
    assert routing_audit(env.conn, mark) == [('system', 'routing.realigned')]
    assert state['status'] == 'pending'                                                       # demandée = défaut ≠ active : recalcul (ZD-3)


def test_a_report_of_the_zone_already_active_changes_nothing_and_audits_nothing(env):
    active_default(env)
    before, mark = state_row(env.conn), audit_mark(env.conn)
    assert coverage.report(env.conn, GRAPH, zone_info(DEFAULT_ZONE, DEFAULT_REGIONS), False, None, DEFAULT_ZONE, after(5)) == {'realigned': False}
    assert routing_audit(env.conn, mark) == []
    assert {k: v for k, v in state_row(env.conn).items() if k not in ('last_report_hash',)} == {
        k: v for k, v in before.items() if k not in ('last_report_hash',)}


def test_the_report_of_a_manual_rollback_realigns_once_and_the_status_follows(env):
    payload = claim_lille(env)
    coverage.switching(env.conn, payload['request_id'], LILLE_ZONE, after(SETTLE + 10))
    mark = audit_mark(env.conn)
    # ops a remis l'ancien graphe à la main : l'hôte rapporte l'ancienne zone pendant `starting`
    assert coverage.report(env.conn, GRAPH2, zone_info(DEFAULT_ZONE), False, None, LILLE_ZONE, after(SETTLE + 100)) == {'realigned': True}
    state = coverage.read(env.conn)
    assert state['active_polygon'] == [[float(a), float(b)] for a, b in DEFAULT_ZONE] and state['status'] == 'pending'
    assert routing_audit(env.conn, mark) == [('system', 'routing.realigned')]


def test_historic_means_the_points_follow_the_requested_zone_and_repeating_it_adds_no_entry(env):
    active_default(env)
    mark = audit_mark(env.conn)
    assert coverage.report(env.conn, GRAPH2, None, True, None, LILLE_ZONE, after(1)) == {'realigned': True}
    state = coverage.read(env.conn)
    assert state['active_polygon'] is None and state['active_regions'] == [] and state['task_tiles'] == []
    assert coverage.accepted_zone(env.conn, env.cfg, LILLE_ZONE) == LILLE_ZONE
    for n in range(10):
        assert coverage.report(env.conn, GRAPH2, None, True, None, LILLE_ZONE, after(2 + n)) == {'realigned': False}
    assert routing_audit(env.conn, mark) == [('system', 'routing.realigned')]


def test_a_historic_report_on_a_state_that_is_already_historic_realigns_nothing(env):
    mark = audit_mark(env.conn)
    assert coverage.report(env.conn, GRAPH, None, True, None, DEFAULT_ZONE, after(1)) == {'realigned': False}
    assert routing_audit(env.conn, mark) == []


@pytest.mark.parametrize('content', [b'', b'{', b'{"polygon": 5}', b'[]', b'\xff'])
def test_an_invalid_report_is_flagged_once_per_content_with_one_alert_and_a_valid_report_clears_it(env, content):
    active_default(env)
    first = [coverage.report_invalid(env.conn, content) for _ in range(10)]
    assert first == [True] + [False] * 9                                                      # un WARNING par contenu, tant qu'il ne change pas
    assert [a['code'] for a in coverage.alerts(env.conn, env.cfg)] == ['coverage_report_invalid']
    assert coverage.report_invalid(env.conn, content + b' ') is True                          # un autre contenu invalide : signalé à nouveau
    assert [a['code'] for a in coverage.alerts(env.conn, env.cfg)] == ['coverage_report_invalid']          # toujours UNE alerte
    coverage.report(env.conn, GRAPH2, zone_info(DEFAULT_ZONE, DEFAULT_REGIONS), False, None, DEFAULT_ZONE, after(5))
    assert coverage.alerts(env.conn, env.cfg) == []                                           # cause disparue : alerte effacée


def test_the_invalid_report_alert_is_cleared_by_the_next_valid_report_even_when_it_equals_the_last_accepted_one(env):
    """Cas ordinaire : un `zone.json` corrompu un passage, réparé au suivant (identique au dernier rapport accepté)."""
    active_default(env)
    coverage.report(env.conn, GRAPH, zone_info(DEFAULT_ZONE, DEFAULT_REGIONS), False, None, DEFAULT_ZONE, after(1))
    assert coverage.report_invalid(env.conn, b'{"polygon": 5}') is True
    assert [a['code'] for a in coverage.alerts(env.conn, env.cfg)] == ['coverage_report_invalid']
    mark = audit_mark(env.conn)
    assert coverage.report(env.conn, GRAPH, zone_info(DEFAULT_ZONE, DEFAULT_REGIONS), False, None, DEFAULT_ZONE, after(2)) == {'realigned': False}
    assert coverage.alerts(env.conn, env.cfg) == [] and routing_audit(env.conn, mark) == []
    assert coverage.report_invalid(env.conn, b'{"polygon": 5}') is True                       # la même panne plus tard est signalée de nouveau


@pytest.mark.parametrize('document', [
    {'polygon': [[0, 0], [1, 1], [1, 0], [0, 1]], 'extract_bbox': [0, 0, 1, 1], 'regions': []},                        # polygone non simple
    {'polygon': [[0, 0], [1, 0]], 'extract_bbox': [0, 0, 1, 1], 'regions': []},                                        # 2 sommets
    {'polygon': [[1.35, 48.83], [6.42, 48.83], [6.42, 51.55]], 'extract_bbox': [6, 48, 1, 51], 'regions': []},          # emprise inversée
    {'polygon': [[1.35, 48.83], [6.42, 48.83], [6.42, 51.55]], 'extract_bbox': [1, 48, 7, 'x'], 'regions': []},
    {'polygon': [[1.35, 48.83], [6.42, 48.83], [6.42, 51.55]], 'extract_bbox': [1, 48, 7], 'regions': []},
    {'polygon': [[1.35, 48.83], [6.42, 48.83], [6.42, 51.55]], 'extract_bbox': [1, 48, 700, 52], 'regions': []},
    {'polygon': [[1.35, 48.83], [6.42, 48.83], [6.42, 51.55]], 'extract_bbox': [1, 48, 7, 52], 'regions': ['../etc']},
    {'polygon': [[1.35, 48.83], [6.42, 48.83], [6.42, 51.55]], 'extract_bbox': [1, 48, 7, 52], 'regions': ['a b']},
    {'polygon': [[1.35, 48.83], [6.42, 48.83], [6.42, 51.55]], 'extract_bbox': [1, 48, 7, 52], 'regions': 'belgium'},
    {'polygon': [[1.35, 48.83], [6.42, 48.83], [6.42, 51.55]], 'extract_bbox': [1, 48, 7, 52], 'regions': ['r%d' % i for i in range(51)]},
    {'polygon': [[1.35, 48.83], [6.42, 48.83], [6.42, 51.55]], 'regions': []},
    {'extract_bbox': [1, 48, 7, 52], 'regions': []},
])
def test_the_zone_json_reported_by_the_host_is_controlled_like_any_zone(document):
    with pytest.raises(coverage.CoverageError):
        coverage.parse_zone_json(json.dumps(document).encode())


def test_the_zone_json_is_bounded_before_any_analysis_and_a_valid_one_is_normalized():
    with pytest.raises(coverage.CoverageError):
        coverage.parse_zone_json(b' ' * (64 * 1024 + 1))
    ok = coverage.parse_zone_json(json.dumps({'polygon': DEFAULT_ZONE, 'extract_bbox': [1.2053, 48.74, 6.5647, 51.64],
                                              'regions': DEFAULT_REGIONS}).encode() + b' ' * 1000)
    assert ok['polygon'] == [[float(a), float(b)] for a, b in DEFAULT_ZONE] and ok['regions'] == DEFAULT_REGIONS


@pytest.mark.parametrize('graph', ['graph-123', 'graph-20261126120000x', 'GRAPH-20261126120000', '', None, 'graph-2026112612000/'])
def test_a_report_with_an_invalid_graph_name_is_refused_and_writes_nothing(env, graph):
    before = state_row(env.conn)
    with pytest.raises(coverage.CoverageError):
        coverage.report(env.conn, graph, zone_info(DEFAULT_ZONE), False, None, DEFAULT_ZONE, T0)
    assert state_row(env.conn) == before


@pytest.mark.parametrize('budget', [(-1, 0), (0, -5), (1.5, 2), ('1', 2), (True, 1), (1,), (1, 2, 3), (float('nan'), 1)])
def test_an_invalid_budget_is_refused_and_writes_nothing(env, budget):
    before = state_row(env.conn)
    with pytest.raises(coverage.CoverageError):
        coverage.report(env.conn, GRAPH, zone_info(DEFAULT_ZONE), False, budget, DEFAULT_ZONE, T0)
    assert state_row(env.conn) == before


def test_the_budget_counters_of_the_day_are_the_ones_the_host_reports(env):
    coverage.report(env.conn, GRAPH, zone_info(DEFAULT_ZONE), False, (3, 5_000_000_000), DEFAULT_ZONE, after(5))
    state = coverage.read(env.conn)
    assert (state['budget_day'], state['budget_builds'], state['budget_bytes']) == ('2026-11-26', 3, 5_000_000_000)
    coverage.report(env.conn, GRAPH, zone_info(DEFAULT_ZONE), False, (0, 0), DEFAULT_ZONE, after(86400))
    state = coverage.read(env.conn)
    assert (state['budget_day'], state['budget_builds'], state['budget_bytes']) == ('2026-11-27', 0, 0)


# ===================================================== E. dalles de la tâche et réveil du MNT =================================

def run_to_done(env, new_tiles, base=T0):
    payload = claim_lille(env, base)
    coverage.switching(env.conn, payload['request_id'], LILLE_ZONE, after(SETTLE + 10, base))
    return coverage.done(env.conn, payload['request_id'], GRAPH, '2026-11-24', new_tiles, LILLE_ZONE, after(SETTLE + 60, base))


def test_the_task_only_tracks_the_tiles_it_downloaded_never_the_ones_that_were_there_before(env):
    """ZD-8 : l'état ne connaît que `new-tiles`. Cent dalles présentes avant la tâche ne sont ni comptées ni listées."""
    run_to_done(env, [[50, 3]])
    state = coverage.read(env.conn)
    assert state['task_tiles'] == [[50, 3]]
    assert coverage.read(env.conn)['task_tiles'] == [[50, 3]]
    assert len(state['task_tiles']) < len(geometry.dem_tiles(state['active_extract_bbox'])) + 1


def test_a_later_task_adds_its_tiles_without_duplicates_and_forgets_the_purged_ones(env):
    run_to_done(env, [[50, 3], [50, 2]])
    payload = take(env, LILLE_ZONE, after(5000), monthly=True)
    coverage.switching(env.conn, payload['request_id'], LILLE_ZONE, after(5010))
    coverage.done(env.conn, payload['request_id'], GRAPH2, '2026-12-01', {'added': [[50, 3], [51, 3]], 'purged': [[50, 2]]}, LILLE_ZONE, after(5020))
    assert sorted(coverage.read(env.conn)['task_tiles']) == [[50, 3], [51, 3]]


def test_the_active_historic_state_has_no_task_tiles(env):
    run_to_done(env, [[50, 3]])
    coverage.report(env.conn, GRAPH2, None, True, None, LILLE_ZONE, after(9000))
    assert coverage.read(env.conn)['task_tiles'] == []


def add_activity(conn, user, n, dem_status):
    return conn.execute("INSERT INTO activities (user_id, name, filename, sha256, source, start_time, points, dem_status) "
                        "VALUES (?, ?, ?, ?, 'test', '2026-11-01T08:00:00Z', x'00', ?)",
                        (user, f'a{n}', f'a{n}.gpx', f'{n:064d}', dem_status)).lastrowid


class _Lock:
    def try_acquire(self):
        return True

    def release(self):
        pass


@pytest.fixture
def scheduler_world(tmp_path, monkeypatch):
    app = zone_app(tmp_path, DEM_URL='http://dem.test:5000/v1/copernicus30')
    conn = connect(app)
    reset_state(conn)
    load_catalog(conn, app, frozen_document())
    seen = []
    monkeypatch.setattr(ingest_service, 'backfill_elevation',
                        lambda db, cfg, limit=None: seen.append(sorted(r[0] for r in db.execute(
                            "SELECT id FROM activities WHERE dem_status = 'no_coverage'"))) or 0)
    scheduler = background.Scheduler(app.config, tasks=[t for t in background.default_tasks() if t.name == 'dem_backfill'], lock=_Lock())
    user = conn.execute("INSERT INTO users (sub, username) VALUES ('u', 'u')").lastrowid
    ids = [add_activity(conn, user, n, 'no_coverage' if n < 2 else 'ok') for n in range(3)]
    conn.commit()
    yield SimpleNamespace(app=app, conn=conn, cfg=app.config, scheduler=scheduler, seen=seen, ids=ids)
    conn.close()


def dem_status(world):
    return {r['id']: r['dem_status'] for r in world.conn.execute("SELECT id, dem_status FROM activities")}


def test_a_done_with_new_tiles_puts_a_wake_marker_read_by_the_dem_task_at_the_next_tick_which_requeues_the_no_coverage_activities(scheduler_world):
    world = scheduler_world
    run_to_done(world, [[50, 3]])
    assert coverage.read(world.conn)['dem_wakeup'] == 1
    assert world.scheduler.tick(0.0) == ['dem_backfill']
    assert world.seen == [[]]                                                                  # remises en file AVANT le rattrapage
    statuses = dem_status(world)
    assert statuses[world.ids[0]] is None and statuses[world.ids[1]] is None and statuses[world.ids[2]] == 'ok'
    assert coverage.read(world.conn)['dem_wakeup'] == 0


def test_the_marker_is_consumed_once_a_later_no_coverage_activity_is_left_alone(scheduler_world):
    world = scheduler_world
    run_to_done(world, [[50, 3]])
    world.scheduler.tick(0.0)
    world.conn.execute("UPDATE activities SET dem_status = 'no_coverage' WHERE id = ?", (world.ids[0],))
    world.conn.commit()
    world.scheduler.tick(100000.0)
    assert dem_status(world)[world.ids[0]] == 'no_coverage'                                    # pas de marqueur : rien n'est retenté


def test_a_done_without_new_tiles_sets_no_marker_and_requeues_nothing(scheduler_world):
    world = scheduler_world
    run_to_done(world, [])
    assert coverage.read(world.conn)['dem_wakeup'] == 0
    world.scheduler.tick(0.0)
    assert dem_status(world)[world.ids[0]] == 'no_coverage'


def test_the_done_itself_only_sets_the_marker_the_dem_task_has_not_run_before_the_tick(scheduler_world):
    world = scheduler_world
    run_to_done(world, [[50, 3]])
    assert world.seen == [] and coverage.read(world.conn)['dem_wakeup'] == 1


def test_the_wake_marker_is_ignored_when_the_zone_mode_is_off(tmp_path):
    app = zone_app(tmp_path, build=False, DEM_URL='http://dem.test:5000/v1/copernicus30')
    conn = connect(app)
    conn.execute("UPDATE routing_coverage_state SET dem_wakeup = 1")
    user = conn.execute("INSERT INTO users (sub, username) VALUES ('u', 'u')").lastrowid
    add_activity(conn, user, 0, 'no_coverage')
    conn.commit()
    scheduler = background.Scheduler(app.config, tasks=[t for t in background.default_tasks() if t.name == 'dem_backfill'], lock=_Lock())
    scheduler.tick(0.0)
    assert conn.execute("SELECT dem_status FROM activities").fetchone()[0] == 'no_coverage'
    assert conn.execute("SELECT dem_wakeup FROM routing_coverage_state").fetchone()[0] == 1
    conn.close()


# ===================================================== F. audit ===============================================================

def test_a_whole_task_writes_one_entry_per_change_all_by_the_system_and_never_one_per_pass(env):
    active_default(env)
    ask(env, LILLE_ZONE, T0)
    mark = audit_mark(env.conn)
    for n in range(5):                                                                        # passages de l'hôte avant le délai : rien
        assert take(env, LILLE_ZONE, after(n * 10)) == {}
    payload = take(env, LILLE_ZONE, after(SETTLE))
    for n in range(5):                                                                        # passages pendant la tâche : rien
        assert take(env, LILLE_ZONE, after(SETTLE + 10 + n)) == {}
    coverage.switching(env.conn, payload['request_id'], LILLE_ZONE, after(SETTLE + 100))
    coverage.done(env.conn, payload['request_id'], GRAPH, '2026-11-24', [[50, 3]], LILLE_ZONE, after(SETTLE + 200))
    entries = routing_audit(env.conn, mark)
    assert len(entries) == 3 and {actor for actor, _ in entries} == {'system'}
    assert all(action.startswith('routing.') for _, action in entries) and len({a for _, a in entries}) == 3


def test_the_failure_audit_entry_carries_the_failure_code(env):
    """ZD-13 : « échec (code) ». L'audit masque par NOM de clé : une clé `code` serait masquée à tort (« [masqué] »)."""
    payload = claim_lille(env)
    mark = audit_mark(env.conn)
    coverage.failed(env.conn, payload['request_id'], 'budget', LILLE_ZONE, after(SETTLE + 5))
    failure = env.conn.execute("SELECT actor_type, details FROM audit_log WHERE id > ? AND action LIKE 'routing.%'", (mark,)).fetchone()
    assert failure['actor_type'] == 'system' and 'budget' in failure['details'], failure['details']


def test_the_success_audit_entry_carries_the_regions_and_the_number_of_added_tiles(env):
    payload = claim_lille(env)
    coverage.switching(env.conn, payload['request_id'], LILLE_ZONE, after(SETTLE + 10))
    mark = audit_mark(env.conn)
    coverage.done(env.conn, payload['request_id'], GRAPH, '2026-11-24', [[50, 3], [50, 2]], LILLE_ZONE, after(SETTLE + 20))
    success = json.loads(env.conn.execute("SELECT details FROM audit_log WHERE id > ? AND action LIKE 'routing.%'", (mark,)).fetchone()['details'])
    assert 'belgium' in json.dumps(success) and 'nord-pas-de-calais' in json.dumps(success) and 2 in success.values()


def test_no_routing_audit_entry_and_no_alert_contains_a_coordinate(env):
    payload = claim_lille(env)
    coverage.switching(env.conn, payload['request_id'], LILLE_ZONE, after(SETTLE + 10))
    coverage.done(env.conn, payload['request_id'], GRAPH, '2026-11-24', [[50, 3]], LILLE_ZONE, after(SETTLE + 20))
    coverage.report(env.conn, GRAPH2, zone_info(LUX_ZONE), False, None, LILLE_ZONE, after(SETTLE + 30))
    coverage.report_invalid(env.conn, b'{"polygon": [[5.123456, 49.654321]]}')
    texts = [r['details'] or '' for r in env.conn.execute("SELECT details FROM audit_log WHERE action LIKE 'routing.%'")]
    texts += [a['message'] for a in coverage.alerts(env.conn, env.cfg)]
    for text in texts:
        assert not re.search(r'\d+\.\d{3,}', text), text
        assert '5.8' not in text and '49.5' not in text and '3.4' not in text


def test_the_admin_actions_are_audited_with_the_account_that_made_them(env):
    payload = claim_lille(env)
    coverage.failed(env.conn, payload['request_id'], 'other', LILLE_ZONE, after(SETTLE + 5))
    mark = audit_mark(env.conn)
    admin = Actor.cli()
    coverage.retry(env.conn, LILLE_ZONE, after(SETTLE + 10), admin)
    rows = env.conn.execute("SELECT actor_type, action FROM audit_log WHERE id > ?", (mark,)).fetchall()
    assert [(r['actor_type'], r['action']) for r in rows] == [(admin.type, 'routing.retry')]
    with pytest.raises(coverage.CoverageError):
        coverage.retry(env.conn, LILLE_ZONE, after(SETTLE + 20), admin)                        # `pending` : rien à réessayer


# ===================================================== G. activation, désactivation, concurrence ==============================

def test_the_first_start_with_the_mode_on_creates_an_activation_request_for_the_requested_zone(tmp_path):
    app = zone_app(tmp_path)
    conn = connect(app)
    state = coverage.read(conn)
    assert state['status'] == 'pending' and state['reason'] == 'activation' and state['request_id'] == 1
    assert state['request_polygon'] == [[float(a), float(b)] for a, b in DEFAULT_ZONE] and state['active_polygon'] is None
    assert coverage.accepted_zone(conn, app.config, DEFAULT_ZONE) == DEFAULT_ZONE             # « sources historiques » : zone demandée
    conn.close()


def test_a_second_start_does_not_create_another_request(tmp_path):
    zone_app(tmp_path)
    app = zone_app(tmp_path)
    conn = connect(app)
    assert coverage.read(conn)['request_id'] == 1
    conn.close()


def test_an_existing_instance_switched_from_off_to_on_follows_the_same_path(tmp_path):
    off = connect(zone_app(tmp_path, build=False))
    before = state_row(off)
    assert before['status'] == 'idle' and before['request_id'] == 0 and before['active_polygon'] is None
    off.close()
    on = connect(zone_app(tmp_path))
    state = coverage.read(on)
    assert state['status'] == 'pending' and state['reason'] == 'activation' and state['request_id'] == 1
    on.close()


def test_a_start_with_an_active_zone_creates_no_activation_request(tmp_path):
    app = zone_app(tmp_path)
    conn = connect(app)
    reset_state(conn)
    set_active(conn, DEFAULT_ZONE, regions=DEFAULT_REGIONS)
    before = state_row(conn)
    conn.close()
    conn = connect(zone_app(tmp_path))
    assert state_row(conn) == before
    conn.close()


def test_the_mode_off_writes_nothing_and_the_points_follow_the_requested_zone(tmp_path):
    app = zone_app(tmp_path, build=False)
    conn = connect(app)
    before, mark = state_row(conn), audit_mark(conn)
    coverage.register_request(conn, app.config, LILLE_ZONE, T0)
    assert coverage.ensure_activation(conn, app.config, LILLE_ZONE, T0) is False
    assert coverage.accepted_zone(conn, app.config, LILLE_ZONE) == LILLE_ZONE and coverage.alerts(conn, app.config) == []
    assert state_row(conn) == before and routing_audit(conn, mark) == []
    conn.close()


def test_ten_processes_starting_together_create_one_activation_request(tmp_path):
    app = zone_app(tmp_path, build=False)
    seed = connect(app)
    seed.close()
    app.config['ROUTING_ZONE_BUILD'] = True
    barrier, results = threading.Barrier(10), []

    def start():
        conn = connect(app)
        try:
            barrier.wait()
            results.append(coverage.ensure_activation(conn, app.config, DEFAULT_ZONE, T0))
        finally:
            conn.close()

    threads = [threading.Thread(target=start) for _ in range(10)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results) == [False] * 9 + [True]
    check = connect(app)
    assert coverage.read(check)['request_id'] == 1
    check.close()


def test_ten_hosts_claiming_together_get_one_task(env):
    active_default(env)
    ask(env, LILLE_ZONE, T0)
    barrier, results = threading.Barrier(10), []

    def host():
        conn = connect(env.app)
        try:
            barrier.wait()
            results.append(coverage.claim(conn, env.cfg, LILLE_ZONE, MARGIN, after(SETTLE), False))
        finally:
            conn.close()

    threads = [threading.Thread(target=host) for _ in range(10)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len([r for r in results if r]) == 1 and len(results) == 10
    assert status(env) == 'building'
