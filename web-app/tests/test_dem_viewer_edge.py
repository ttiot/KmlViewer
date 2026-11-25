"""
Altitude corrigée (MNT) et visualiseur : une trace planifiée enregistrée puis corrigée par la tâche de fond doit montrer D+, D−, profil et
altitude sous le curseur à l'ouverture (retour utilisateur du 25/11, diagnostic de `test`). Écrits depuis le défaut, sans lire le correctif :

  - /api/activities/<id>/parsed (ce que reçoit la page) porte l'altitude corrigée quand elev_source = dem, point pour point ;
  - les analyses alimentées par ce GeoJSON (trajectoire, avancée) voient donc l'altitude, et donnent le dénivelé de la bibliothèque ;
  - l'analyse avancée ENREGISTRÉE n'est jamais un instantané périmé après la correction ;
  - rien ne change pour une activité sans correction (altitude du fichier, pas de couverture MNT, altitude MNT désalignée).
"""

import re

import numpy as np
import pytest

from app.services import activity_store, dem_service, ingest_service
from tests.conftest import line, make_gpx
from tests.test_dem import CFG, _fake_dem

HILLS = lambda lat, lon: 300.0 + 120.0 * np.sin((lat - 45.0) * 900.0) + 40.0 * np.sin(lon * 700.0)    # noqa: E731  (relief net)


def without_elevation(gpx: str) -> str:
    """Le fichier d'un moteur de routage sans altitude : aucun <ele>."""
    return re.sub(r'<ele>[^<]*</ele>', '', gpx)


def two_traces_gpx() -> str:
    """Deux traces (deux <trk>) : le MNT est aligné sur la CONCATÉNATION, les ruptures sont dans 'breaks'."""
    def trk(name, pts):
        rows = ''.join(f"<trkpt lat='{la:.6f}' lon='{lo:.6f}'></trkpt>" for la, lo, _ in pts)
        return f"<trk><name>{name}</name><trkseg>{rows}</trkseg></trk>"
    return ("<?xml version='1.0' encoding='UTF-8'?><gpx xmlns='http://www.topografix.com/GPX/1/1'>"
            + trk('A', line(120, lat0=45.0, lon0=6.0)) + trk('B', line(90, lat0=45.1, lon0=6.1)) + "</gpx>")


@pytest.fixture
def cfg(app):
    return {**app.config, **CFG}


def save(db, cfg, user_id, gpx, name='trace.gpx', defer=True, kind='planned'):
    outcome = ingest_service.ingest_bytes(db, cfg, user_id, gpx.encode(), name, 'route_builder', defer_elevation=defer, kind=kind)
    assert outcome.status == 'created', outcome
    return outcome.activity_id


def parsed(client, activity_id):
    response = client.get(f'/api/activities/{activity_id}/parsed')
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()


def lines_of(data):
    return [f for f in data['features'] if f.get('type') in ('polyline', 'linestring', 'track')]


def line_altitudes(data):
    return [c[2] for feature in lines_of(data) for c in feature['coordinates']]


def point_altitudes(data):
    return [p['altitude'] for p in data['points']]


def correct(db, cfg, monkeypatch, elevation=HILLS):
    _fake_dem(monkeypatch, elevation=elevation)
    assert ingest_service.backfill_elevation(db, cfg) >= 1


def stored_dem(db, user_id, activity_id):
    row = activity_store.get(db, user_id, activity_id)
    return row, activity_store.unpack_points(row['points']).get('ele_dem')


def analysis_body(data):
    features = [f for f in data['features'] if f.get('type') in ('polyline', 'linestring', 'track', 'multitrack')]
    return {'features': features, 'points': data['points']}


# --- /parsed porte l'altitude corrigée --------------------------------------------------------------------------------------------

def test_before_the_correction_the_viewer_gets_the_file_as_it_is(app, db, client, local_user_id, cfg):
    activity = save(db, cfg, local_user_id, without_elevation(make_gpx(line(150), with_time=False)))
    data = parsed(client, activity)
    assert set(line_altitudes(data)) == {0} and set(point_altitudes(data)) == {0}            # aucune altitude dans le fichier
    row = activity_store.get(db, local_user_id, activity)
    assert row['elev_source'] == 'gps' and row['dem_status'] is None                         # correction différée, pas encore faite


def test_after_the_correction_every_point_served_to_the_viewer_carries_the_corrected_altitude(app, db, client, local_user_id, cfg,
                                                                                                monkeypatch):
    activity = save(db, cfg, local_user_id, without_elevation(make_gpx(line(300, dlat=0.0004), with_time=False)))
    correct(db, cfg, monkeypatch)
    row, ele_dem = stored_dem(db, local_user_id, activity)
    assert row['elev_source'] == 'dem' and ele_dem is not None and len(ele_dem) == 300
    data = parsed(client, activity)
    served = line_altitudes(data)
    assert len(served) == 300
    assert np.allclose(served, ele_dem, atol=0.06), "la polyligne ne porte pas l'altitude MNT stockée"
    assert len(set(served)) > 100                                                              # un vrai relief, pas un palier
    marks = point_altitudes(data)
    assert np.allclose(marks[-300:] if len(marks) > 300 else marks, ele_dem, atol=0.06), "les points de mesure non plus"
    assert all(abs(p['parsed_info']['altitude_m'] - p['altitude']) < 0.06 for p in data['points'])      # même valeur partout


def test_the_reopened_trace_shows_the_same_altitudes_on_every_reopening(app, db, client, local_user_id, cfg, monkeypatch):
    activity = save(db, cfg, local_user_id, without_elevation(make_gpx(line(200), with_time=False)))
    correct(db, cfg, monkeypatch)
    first, second = parsed(client, activity), parsed(client, activity)
    assert line_altitudes(first) == line_altitudes(second) and point_altitudes(first) == point_altitudes(second)


def test_the_corrected_altitude_follows_each_trace_of_a_multi_trace_file(app, db, client, local_user_id, cfg, monkeypatch):
    activity = save(db, cfg, local_user_id, two_traces_gpx(), kind='activity')
    correct(db, cfg, monkeypatch)
    _, ele_dem = stored_dem(db, local_user_id, activity)
    assert len(ele_dem) == 210
    data = parsed(client, activity)
    served = [c[2] for feature in lines_of(data) for c in feature['coordinates']]
    assert len(served) == 210 and np.allclose(served, ele_dem, atol=0.06), "altitude MNT mal alignée sur les deux traces"


def test_a_stored_dem_profile_that_is_not_aligned_is_ignored_and_never_breaks_the_page(app, db, client, local_user_id, cfg):
    activity = save(db, cfg, local_user_id, make_gpx(line(100, ele=lambda i: 50.0 + i), with_time=False))
    row = activity_store.get(db, local_user_id, activity)
    pts = activity_store.unpack_points(row['points'])
    short = list(np.linspace(500, 600, 40))                                                    # 40 valeurs pour 100 points
    activity_store.update_elevation(db, local_user_id, activity, points=activity_store.pack_points(
        pts['lat'], pts['lon'], pts['ele'], pts['t'], short, pts.get('breaks') or []), ascent_m=100.0, descent_m=0.0, elev_source='dem')
    data = parsed(client, activity)                                                            # 200, pas de 500
    assert line_altitudes(data) == [50.0 + i for i in range(100)]                              # l'altitude du fichier est conservée


def test_without_correction_the_altitude_of_the_file_is_served_untouched(app, db, client, local_user_id, cfg):
    activity = save(db, cfg, local_user_id, make_gpx(line(120, ele=lambda i: 200.0 + (i % 17)), with_time=True), kind='activity')
    data = parsed(client, activity)
    assert line_altitudes(data) == [200.0 + (i % 17) for i in range(120)]
    assert activity_store.get(db, local_user_id, activity)['elev_source'] == 'gps'


def test_a_trace_outside_the_dem_coverage_keeps_the_altitude_of_the_file(app, db, client, local_user_id, cfg, monkeypatch):
    activity = save(db, cfg, local_user_id, make_gpx(line(80, ele=lambda i: 10.0 + i), with_time=False))

    def uncovered(self, url, params=None, **kwargs):
        results = [{'elevation': None} for _ in params['locations'].split('|')]
        return type('R', (), {'raise_for_status': lambda s: None, 'json': lambda s: {'status': 'OK', 'results': results}})()

    monkeypatch.setattr(dem_service.requests.Session, 'get', uncovered)
    ingest_service.backfill_elevation(db, cfg)
    row = activity_store.get(db, local_user_id, activity)
    assert row['elev_source'] == 'gps' and row['dem_status'] == 'no_coverage'
    assert line_altitudes(parsed(client, activity)) == [10.0 + i for i in range(80)]


# --- les analyses alimentées par ce GeoJSON voient l'altitude ----------------------------------------------------------------------

def test_the_base_and_advanced_analyses_of_the_served_data_see_the_corrected_elevation(app, db, client, local_user_id, cfg,
                                                                                         monkeypatch):
    activity = save(db, cfg, local_user_id, without_elevation(make_gpx(line(400, dlat=0.0004), with_time=False)))
    before = client.post('/api/analysis/trajectory', json=analysis_body(parsed(client, activity))).get_json()['analysis']
    assert before['basic_stats']['has_elevation_data'] is False and not before['elevation']
    correct(db, cfg, monkeypatch)
    row = activity_store.get(db, local_user_id, activity)
    body = analysis_body(parsed(client, activity))
    after = client.post('/api/analysis/trajectory', json=body).get_json()['analysis']
    assert after['basic_stats']['has_elevation_data'] is True
    elevation = after['elevation']
    assert elevation['total_ascent'] > 0 and elevation['total_descent'] > 0, elevation          # le grand chiffre du panneau
    assert elevation['min_elevation'] < elevation['max_elevation']                                # « altitude de x à y m »
    assert elevation['total_ascent'] == pytest.approx(row['ascent_m'], rel=0.05, abs=2), "panneau ≠ bibliothèque"
    advanced = client.post('/api/analysis/advanced', json=body).get_json()['analysis']['summary']
    assert advanced['has_elevation'] is True and advanced['ascent_m'] > 0
    assert advanced['ascent_m'] == pytest.approx(row['ascent_m'], rel=0.05, abs=2)


# --- l'analyse avancée enregistrée n'est jamais un instantané périmé ---------------------------------------------------------------

def stored_summary(client, activity):
    body = client.get(f'/api/activities/{activity}/analysis').get_json()
    return body, ((body.get('analysis') or {}).get('summary'))


def fresh_or_absent(client, activity, row):
    """Après la correction : soit plus d'analyse enregistrée, soit une analyse qui voit l'altitude et donne le dénivelé de la bibliothèque."""
    body, summary = stored_summary(client, activity)
    if body['analysis'] is None:
        return 'absente'
    assert summary['has_elevation'] is True, "analyse enregistrée avant la correction restée périmée (aucune altitude)"
    assert summary['ascent_m'] == pytest.approx(row['ascent_m'], rel=0.05, abs=2)
    assert summary['elevation_source'] == 'dem'
    return 'recalculée'


def test_the_saved_advanced_analysis_is_invalidated_or_recomputed_when_the_background_correction_ends(app, db, client, local_user_id,
                                                                                                      cfg, monkeypatch):
    activity = save(db, cfg, local_user_id, without_elevation(make_gpx(line(300, dlat=0.0004), with_time=False)))
    first = client.post(f'/api/activities/{activity}/analysis')
    assert first.status_code == 200 and first.get_json()['analysis']['summary']['has_elevation'] is False
    assert client.get(f'/api/activities/{activity}/analysis').get_json()['analysis'] is not None
    correct(db, cfg, monkeypatch)
    fresh_or_absent(client, activity, activity_store.get(db, local_user_id, activity))
    again = client.post(f'/api/activities/{activity}/analysis').get_json()['analysis']['summary']     # relancée : l'altitude est vue
    assert again['has_elevation'] is True and again['elevation_source'] == 'dem' and again['ascent_m'] > 0


def test_the_manual_correction_endpoint_also_leaves_no_stale_saved_analysis(app, db, client, local_user_id, cfg, monkeypatch):
    activity = save(db, cfg, local_user_id, without_elevation(make_gpx(line(250, dlat=0.0004), with_time=False)))
    client.post(f'/api/activities/{activity}/analysis')
    _fake_dem(monkeypatch, elevation=HILLS)
    app.config.update(CFG)
    response = client.post(f'/api/activities/{activity}/elevation')
    assert response.status_code == 200, response.get_data(as_text=True)
    fresh_or_absent(client, activity, activity_store.get(db, local_user_id, activity))


def test_a_saved_analysis_of_another_activity_is_left_alone_by_a_correction(app, db, client, local_user_id, cfg, monkeypatch):
    other = save(db, cfg, local_user_id, make_gpx(line(120, ele=lambda i: 100.0 + (i % 7)), with_time=False), name='autre.gpx',
                 kind='activity')
    kept = client.post(f'/api/activities/{other}/analysis').get_json()
    target = save(db, cfg, local_user_id, without_elevation(make_gpx(line(300, dlat=0.0004), with_time=False)), name='cible.gpx')
    # `other` n'a pas de correction en attente (altitude du fichier) ; seule `target` est corrigée
    db.execute("UPDATE activities SET dem_status = 'no_coverage' WHERE id = ?", (other,))
    db.commit()
    correct(db, cfg, monkeypatch)
    now = client.get(f'/api/activities/{other}/analysis').get_json()
    assert now['analysis'] is not None and now['computed_at'] == kept['computed_at'], "l'analyse d'une AUTRE activité a été touchée"
    assert activity_store.get(db, local_user_id, target)['elev_source'] == 'dem'


# --- garde de course (EA-5, CA-A6) : un calcul d'analyse en cours pendant la correction n'est pas enregistré --------------------------

def suspend_analysis(monkeypatch, app, cfg, during):
    """
    Crochet injecté APRÈS la lecture des points par `analysis_store.compute` : `during()` s'exécute pendant que le calcul est
    « suspendu » (une autre connexion, comme la tâche de fond), puis le calcul reprend avec les données lues avant.
    """
    from app.services import analysis_store
    original = analysis_store.advanced_analysis.analyze
    fired = []

    def suspended(*args, **kwargs):
        if not fired:
            fired.append(True)
            during()
        return original(*args, **kwargs)

    monkeypatch.setattr(analysis_store.advanced_analysis, 'analyze', suspended)
    return fired


def test_an_analysis_computed_while_the_correction_lands_is_returned_but_not_stored_CA_A6(app, db, client, local_user_id, cfg, monkeypatch):
    from app import db as database
    activity = save(db, cfg, local_user_id, without_elevation(make_gpx(line(300, dlat=0.0004), with_time=False)))

    def correction_during_the_suspension():
        other = database.connect(app.config['DB_PATH'])
        try:
            _fake_dem(monkeypatch, elevation=HILLS)
            assert ingest_service.backfill_elevation(other, cfg) == 1
        finally:
            other.close()

    fired = suspend_analysis(monkeypatch, app, cfg, correction_during_the_suspension)
    response = client.post(f'/api/activities/{activity}/analysis')
    assert fired and response.status_code == 200
    assert response.get_json()['analysis']['summary']['has_elevation'] is False           # rendu à l'appelant, calculé AVANT la correction
    assert activity_store.get(db, local_user_id, activity)['elev_source'] == 'dem'          # la correction a bien eu lieu pendant le calcul
    assert client.get(f'/api/activities/{activity}/analysis').get_json()['analysis'] is None, "instantané périmé enregistré"
    again = client.post(f'/api/activities/{activity}/analysis').get_json()['analysis']['summary']      # la demande suivante recalcule
    assert again['has_elevation'] is True and again['elevation_source'] == 'dem'
    assert client.get(f'/api/activities/{activity}/analysis').get_json()['analysis'] is not None


def test_an_analysis_with_no_correction_during_the_suspension_is_stored_CA_A6(app, db, client, local_user_id, cfg, monkeypatch):
    activity = save(db, cfg, local_user_id, without_elevation(make_gpx(line(300, dlat=0.0004), with_time=False)))
    fired = suspend_analysis(monkeypatch, app, cfg, lambda: None)
    response = client.post(f'/api/activities/{activity}/analysis')
    assert fired and response.status_code == 200
    stored = client.get(f'/api/activities/{activity}/analysis').get_json()
    assert stored['analysis'] is not None and stored['computed_at'] == response.get_json()['computed_at']


def test_a_correction_that_ended_before_the_calculation_started_leaves_the_result_stored_CA_A6(app, db, client, local_user_id, cfg,
                                                                                                monkeypatch):
    activity = save(db, cfg, local_user_id, without_elevation(make_gpx(line(300, dlat=0.0004), with_time=False)))
    correct(db, cfg, monkeypatch)
    fired = suspend_analysis(monkeypatch, app, cfg, lambda: None)
    summary = client.post(f'/api/activities/{activity}/analysis').get_json()['analysis']['summary']
    assert fired and summary['has_elevation'] is True and summary['elevation_source'] == 'dem'
    stored = client.get(f'/api/activities/{activity}/analysis').get_json()['analysis']
    assert stored is not None and stored['summary']['has_elevation'] is True
