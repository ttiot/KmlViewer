"""
Commande `flask routing-zone` (rôle test) : routing-zone-donnees ZD-1 (code 4), ZD-10 (sous-commandes, sortie, codes, entrée standard
bornée, aucun fil de fond), inventaire n° 29. CA-ZD4 (arguments, `catalog -`, aucun fil), CA-ZD1 (`claim` ⇒ 4 à `=0`), CA-ZD6 (rapport).
Écrit depuis la spec : la commande est appelée comme l'hôte l'appelle (arguments et entrée standard), horloge injectée
(`app.config['ROUTING_ZONE_CLOCK']`), sortie lue sur la sortie standard (UNE ligne JSON) et le code de sortie.

 A. désactivé (`=0`) : code 4, rien n'est lu ni écrit
 B. `catalog -` : succès, taille bornée avant analyse, contenus invalides ⇒ 2 et ancien catalogue gardé
 C. `claim`, `switching`, `done`, `failed` : cycle complet par la commande, codes 0, 2 et 3
 D. `report` : zone.json, `--historic`, budget, dédoublonnage, taille bornée
 E. arguments invalides ⇒ 2 et RIEN n'est écrit
 F. aucun fil de fond, aucune coordonnée dans les journaux, une seule ligne JSON
"""

import json
import logging
import sqlite3
import threading
from datetime import timedelta

import pytest

from app import background
from app.services.routing import catalog as catalog_module
from app.services.routing import coverage_state as coverage
from tests.pw_harness import connect
from tests.routing_zone_support import (DEFAULT_REGIONS, DEFAULT_ZONE, GRAPH, GRAPH2, LILLE_ZONE, audit_mark, catalog_row, feature,
                                        frozen_document, now_utc, raw, rect, routing_audit, state_row, synthetic, zone_app)
from tests.routing_zone_support import CLI as ACTOR

BOUND = 16 * 1024 * 1024
NOW = {'value': None}


def zone_json(polygon=DEFAULT_ZONE, regions=DEFAULT_REGIONS, bbox=None):
    from app.services.routing import coverage as geometry
    return json.dumps({'polygon': polygon, 'extract_bbox': bbox or geometry.extract_bbox(polygon, 10), 'regions': list(regions)}).encode()


class Host:
    """L'hôte vu de l'application : un `invoke` par appel, comme un `docker compose exec -T kml-viewer flask routing-zone …`."""

    def __init__(self, app):
        self.app, self.runner = app, app.test_cli_runner()
        self.now = now_utc()
        app.config['ROUTING_ZONE_CLOCK'] = lambda: self.now

    def run(self, *args, stdin=b''):
        result = self.runner.invoke(args=['routing-zone', *args], input=stdin)
        return result

    def json(self, *args, stdin=b'', code=0):
        result = self.run(*args, stdin=stdin)
        assert result.exit_code == code, (args, result.exit_code, result.stderr, result.output)
        return json.loads(result.stdout) if result.stdout.strip() else None

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def host(tmp_path):
    app = zone_app(tmp_path)
    return Host(app)


@pytest.fixture
def ready(host):
    """Catalogue figé chargé, zone active = zone par défaut (rapportée), zone demandée = Lille (enregistrée « maintenant »)."""
    host.json('catalog', '-', stdin=raw(frozen_document()))
    host.json('report', '--graph', GRAPH, '--zone', '-', stdin=zone_json())
    conn = connect(host.app)
    host.app.config['SETTINGS_STORE'].set_many(conn, {'routing.coverage_polygon': LILLE_ZONE}, actor=ACTOR)
    conn.close()
    host.now = now_utc()
    return host


def snapshot(app):
    conn = connect(app)
    try:
        return state_row(conn), catalog_row(conn), conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    finally:
        conn.close()


# ===================================================== A. désactivé ===========================================================

@pytest.mark.parametrize('args, stdin', [
    (['catalog', '-'], raw(frozen_document())), (['claim'], b''), (['claim', '--monthly'], b''), (['switching', '1'], b''),
    (['done', '1', '--graph', GRAPH, '--data-date', '2026-11-26', '--new-tiles', '-'], b'[]'), (['failed', '1', '--code', 'other'], b''),
    (['report', '--graph', GRAPH, '--zone', '-'], zone_json()), (['report', '--graph', GRAPH, '--historic'], b''),
])
def test_with_the_mode_off_every_subcommand_exits_4_and_reads_and_writes_nothing(tmp_path, args, stdin):
    app = zone_app(tmp_path, build=False)
    before = snapshot(app)
    result = Host(app).run(*args, stdin=stdin)
    assert result.exit_code == 4 and result.stdout.strip() == ''
    assert snapshot(app) == before


def test_an_oversized_input_with_the_mode_off_is_still_a_4_not_a_2(tmp_path):
    result = Host(zone_app(tmp_path, build=False)).run('catalog', '-', stdin=b' ' * (BOUND + 1))
    assert result.exit_code == 4


# ===================================================== B. catalog - ===========================================================

def test_a_valid_catalog_is_loaded_and_the_answer_is_one_json_line(host):
    result = host.run('catalog', '-', stdin=raw(frozen_document()))
    assert result.exit_code == 0 and len(result.stdout.strip().splitlines()) == 1
    assert json.loads(result.stdout) == {'regions': 70}
    conn = connect(host.app)
    assert catalog_row(conn)['sha256'] and conn.execute("SELECT COUNT(*) FROM routing_catalog").fetchone()[0] == 1
    conn.close()


def test_a_catalog_of_exactly_16_mib_is_read_and_one_byte_more_is_refused_without_any_analysis(host, monkeypatch):
    document = raw(synthetic(rect('aa', 0, 0, 2, 2, size=1)))
    assert host.run('catalog', '-', stdin=document + b' ' * (BOUND - len(document))).exit_code == 0
    seen = []
    monkeypatch.setattr(catalog_module, 'parse_bytes', lambda data: seen.append(1))
    monkeypatch.setattr(catalog_module, 'build', lambda *a, **k: seen.append(1))
    before = snapshot(host.app)
    result = host.run('catalog', '-', stdin=document + b' ' * (BOUND - len(document) + 1))
    assert result.exit_code == 2 and seen == [] and snapshot(host.app) == before


def bad_catalogs():
    base = synthetic(rect('aa', 0, 0, 2, 2, size=10), rect('bb', 2, 0, 4, 2, size=20))

    def change(**props):
        doc = json.loads(json.dumps(base))
        doc['features'][2]['properties'].update(props)
        return raw(doc)

    def circle(n):
        import math
        points = [[5 + math.cos(2 * math.pi * i / n), 45 + math.sin(2 * math.pi * i / n)] for i in range(n)]
        doc = json.loads(json.dumps(base))
        doc['features'][2]['geometry'] = {'type': 'Polygon', 'coordinates': [points + [points[0]]]}
        return raw(doc)

    nan = json.loads(json.dumps(base))
    nan['features'][2]['geometry']['coordinates'][0][1] = [float('nan'), 0]
    cycle = synthetic(feature('france', 'europe'), rect('aa', 0, 0, 1, 1, size=1, parent='france'))
    cycle['features'][0]['properties']['parent'] = 'france'
    return {
        'http': change(urls={'pbf': 'http://download.geofabrik.de/europe/bb-latest.osm.pbf'}),
        'autre hôte': change(urls={'pbf': 'https://evil.example/europe/bb-latest.osm.pbf'}),
        'chemin ..': change(urls={'pbf': 'https://download.geofabrik.de/europe/../x/bb.osm.pbf'}),
        'taille négative': change(size=-1), 'taille texte': change(size='10'),
        'contour de 10 001 sommets': circle(10_000), 'coordonnée NaN': raw(nan),
        'cycle': raw(cycle), 'illisible': b'{"features": [', 'liste': b'[]', 'vide': b'', 'utf-8 invalide': b'\xff\xfe\x00',
    }


@pytest.mark.parametrize('label', list(bad_catalogs()))
def test_an_invalid_catalog_exits_2_and_the_previous_one_is_kept(host, label):
    host.json('catalog', '-', stdin=raw(synthetic(rect('aa', 0, 0, 2, 2, size=10), rect('bb', 2, 0, 4, 2, size=20))))
    before = snapshot(host.app)
    result = host.run('catalog', '-', stdin=bad_catalogs()[label])
    assert result.exit_code == 2 and result.stdout.strip() == '', (label, result.stdout)
    assert snapshot(host.app) == before


def test_the_catalog_is_only_read_on_the_standard_input(host):
    assert host.run('catalog', '/etc/passwd').exit_code == 2
    assert host.run('catalog').exit_code == 2
    assert snapshot(host.app)[1] is None


# ===================================================== C. le cycle complet par la commande ====================================

def test_the_whole_task_through_the_command_claim_switching_done(ready):
    host = ready
    assert host.json('claim') == {}                                                           # saisie récente : rien
    host.advance(299)
    assert host.json('claim') == {}
    host.advance(2)
    payload = host.json('claim')
    assert payload['request_id'] >= 1 and payload['reason'] == 'zone' and payload['polygon'] == LILLE_ZONE
    assert payload['regions'] == ['belgium', 'nord-pas-de-calais'] and payload['extract_bbox'] == [2.8577, 50.31, 3.5423, 50.89]
    assert payload['margin_km'] == 10 and sorted(map(tuple, payload['dem_tiles'])) == [(50, 2), (50, 3)]
    assert host.json('claim') == {} and host.json('claim', '--monthly') == {}                 # une seule prise
    rid = str(payload['request_id'])
    host.json('switching', rid)
    host.json('done', rid, '--graph', GRAPH2, '--data-date', '2026-11-24', '--new-tiles', '-', stdin=b'[[50, 3]]')
    conn = connect(host.app)
    state = coverage.read(conn)
    conn.close()
    assert state['status'] == 'idle' and state['active_polygon'] == LILLE_ZONE and state['task_tiles'] == [[50, 3]]
    assert state['active_regions'] == ['belgium', 'nord-pas-de-calais'] and state['dem_wakeup'] == 1


def test_unknown_or_stale_requests_exit_3_and_a_stale_switching_recomputes_the_status(ready):
    host = ready
    host.advance(301)
    payload = host.json('claim')
    conn = connect(host.app)
    host.app.config['SETTINGS_STORE'].set_many(conn, {'routing.coverage_polygon': DEFAULT_ZONE}, actor=ACTOR)        # retour à la zone active
    conn.close()
    assert host.run('switching', str(payload['request_id'])).exit_code == 3
    conn = connect(host.app)
    assert coverage.read(conn)['status'] == 'idle'
    conn.close()
    for args in (['switching', '999'], ['done', '999', '--graph', GRAPH, '--data-date', '2026-11-26', '--new-tiles', '-'],
                 ['failed', '999', '--code', 'other']):
        assert host.run(*args, stdin=b'[]').exit_code == 3, args


def test_failed_then_claim_gives_nothing_until_the_admin_retries(ready):
    host = ready
    host.advance(301)
    rid = str(host.json('claim')['request_id'])
    host.json('failed', rid, '--code', 'budget')
    host.advance(10_000)
    assert host.json('claim') == {} and host.json('claim', '--monthly') == {}
    conn = connect(host.app)
    assert coverage.read(conn)['status'] == 'failed'
    conn.close()


def test_a_monthly_claim_through_the_command_takes_the_active_zone_with_the_reason_monthly(host):
    host.json('catalog', '-', stdin=raw(frozen_document()))
    host.json('report', '--graph', GRAPH, '--zone', '-', stdin=zone_json())
    payload = host.json('claim', '--monthly')
    assert payload['reason'] == 'mensuel' and payload['regions'] == DEFAULT_REGIONS
    rid = str(payload['request_id'])
    host.json('switching', rid)
    conn = connect(host.app)
    assert coverage.read(conn)['status'] == 'starting'
    mark = audit_mark(conn)
    conn.close()
    host.json('done', rid, '--graph', GRAPH2, '--data-date', '2026-11-27', '--new-tiles', '-', stdin=b'[]')
    conn = connect(host.app)
    assert coverage.read(conn)['status'] == 'idle' and 'routing.realigned' not in [a for _, a in routing_audit(conn, mark)]
    conn.close()


# ===================================================== D. report ==============================================================

def test_the_same_report_ten_times_realigns_once_and_audits_once(host):
    host.json('report', '--graph', GRAPH, '--zone', '-', stdin=zone_json())
    conn = connect(host.app)
    mark = audit_mark(conn)
    conn.close()
    answers = [host.json('report', '--graph', GRAPH2, '--zone', '-', stdin=zone_json(LILLE_ZONE, ['belgium'])) for _ in range(10)]
    assert [a['realigned'] for a in answers] == [True] + [False] * 9
    conn = connect(host.app)
    assert routing_audit(conn, mark) == [('system', 'routing.realigned')]
    assert coverage.read(conn)['active_polygon'] == LILLE_ZONE
    conn.close()


def test_the_budget_is_reported_with_the_zone(host):
    host.json('report', '--graph', GRAPH, '--zone', '-', '--budget', '3,5000000000', stdin=zone_json())
    conn = connect(host.app)
    state = coverage.read(conn)
    conn.close()
    assert (state['budget_builds'], state['budget_bytes']) == (3, 5_000_000_000)


def test_a_historic_report_makes_the_points_follow_the_requested_zone(host):
    host.json('report', '--graph', GRAPH, '--zone', '-', stdin=zone_json())
    assert host.json('report', '--graph', GRAPH2, '--historic') == {'realigned': True}
    assert host.json('report', '--graph', GRAPH2, '--historic') == {'realigned': False}
    conn = connect(host.app)
    assert coverage.read(conn)['active_polygon'] is None
    conn.close()


@pytest.mark.parametrize('content', [b'', b'{', b'[]', b'{"polygon": 5}', b'\xff\xfe', zone_json([[0, 0], [1, 1], [1, 0], [0, 1]], [], [0, 0, 1, 1])])
def test_an_invalid_zone_json_exits_2_is_ignored_and_raises_one_alert_ten_times_over(host, content):
    host.json('report', '--graph', GRAPH, '--zone', '-', stdin=zone_json())
    conn = connect(host.app)
    active, mark = coverage.read(conn)['active_polygon'], audit_mark(conn)
    conn.close()
    for _ in range(10):
        assert host.run('report', '--graph', GRAPH2, '--zone', '-', stdin=content).exit_code == 2
    conn = connect(host.app)
    assert coverage.read(conn)['active_polygon'] == active and routing_audit(conn, mark) == []
    assert [a['code'] for a in coverage.alerts(conn, host.app.config)] == ['coverage_report_invalid']
    conn.close()
    host.json('report', '--graph', GRAPH, '--zone', '-', stdin=zone_json())                   # réparé, identique au dernier rapport accepté
    conn = connect(host.app)
    assert coverage.alerts(conn, host.app.config) == []
    conn.close()


def test_a_zone_json_over_64_kib_is_refused_without_any_analysis(host, monkeypatch):
    seen = []
    monkeypatch.setattr(coverage, 'parse_zone_json', lambda data: seen.append(1))
    before = snapshot(host.app)
    assert host.run('report', '--graph', GRAPH, '--zone', '-', stdin=b' ' * (64 * 1024 + 1)).exit_code == 2
    assert seen == [] and snapshot(host.app) == before


# ===================================================== E. arguments invalides =================================================

INVALID = {
    'catalog autre source': ['catalog', 'fichier.json'],
    'switching sans numéro': ['switching'], 'switching texte': ['switching', 'abc'], 'switching zéro': ['switching', '0'],
    'switching négatif': ['switching', '--', '-3'], 'switching décimal': ['switching', '1.5'],
    'done graphe court': ['done', '1', '--graph', 'graph-123', '--data-date', '2026-11-26', '--new-tiles', '-'],
    'done graphe long': ['done', '1', '--graph', 'graph-202611261200000', '--data-date', '2026-11-26', '--new-tiles', '-'],
    'done graphe chemin': ['done', '1', '--graph', '../graph-20261126120000', '--data-date', '2026-11-26', '--new-tiles', '-'],
    'done graphe commande': ['done', '1', '--graph', 'graph-20261126120000;rm', '--data-date', '2026-11-26', '--new-tiles', '-'],
    'done date impossible': ['done', '1', '--graph', GRAPH, '--data-date', '2026-13-40', '--new-tiles', '-'],
    'done date mal formée': ['done', '1', '--graph', GRAPH, '--data-date', '26/11/2026', '--new-tiles', '-'],
    'done date manquante': ['done', '1', '--graph', GRAPH, '--new-tiles', '-'],
    'done graphe manquant': ['done', '1', '--data-date', '2026-11-26', '--new-tiles', '-'],
    'done dalles hors entrée': ['done', '1', '--graph', GRAPH, '--data-date', '2026-11-26', '--new-tiles', 'x.json'],
    'failed code inconnu': ['failed', '1', '--code', 'nope'], 'failed code vide': ['failed', '1', '--code', ''],
    'failed code commande': ['failed', '1', '--code', 'cap;ls'], 'failed sans code': ['failed', '1'],
    'report sans source': ['report', '--graph', GRAPH], 'report deux sources': ['report', '--graph', GRAPH, '--zone', '-', '--historic'],
    'report zone hors entrée': ['report', '--graph', GRAPH, '--zone', 'zone.json'], 'report sans graphe': ['report', '--historic'],
    'report graphe invalide': ['report', '--graph', 'graph-1', '--historic'],
    'budget point-virgule': ['report', '--graph', GRAPH, '--historic', '--budget', '1;2'],
    'budget texte': ['report', '--graph', GRAPH, '--historic', '--budget', 'a,b'],
    'budget négatif': ['report', '--graph', GRAPH, '--historic', '--budget', '-1,2'],
    'budget trois valeurs': ['report', '--graph', GRAPH, '--historic', '--budget', '1,2,3'],
    'budget NaN': ['report', '--graph', GRAPH, '--historic', '--budget', 'nan,1'],
    'budget incomplet': ['report', '--graph', GRAPH, '--historic', '--budget', '1,'],
    'budget démesuré': ['report', '--graph', GRAPH, '--historic', '--budget', '1,' + '9' * 16],
    'sous-commande inconnue': ['purge'], 'option inconnue': ['claim', '--force'],
}


@pytest.mark.parametrize('label', list(INVALID))
def test_an_invalid_argument_exits_2_and_writes_nothing(ready, label):
    host = ready
    host.advance(301)
    payload = host.json('claim')
    before = snapshot(host.app)
    result = host.run(*[a.replace('1', str(payload['request_id'])) if a == '1' else a for a in INVALID[label]], stdin=b'[]')
    assert result.exit_code == 2, (label, result.exit_code, result.stderr)
    assert result.stdout.strip() == '' and snapshot(host.app) == before


@pytest.mark.parametrize('tiles', [b'x', b'{', b'[[1]]', b'[[1,2,3]]', b'[[true,2]]', b'[[91,2]]', b'[[1,181]]', b'[[1.5,2]]', b'"x"', b'[' * 5000,
                                   b'[[1,2]]' + b' ' * (64 * 1024)])
def test_invalid_new_tiles_exit_2_and_write_nothing(ready, tiles):
    host = ready
    host.advance(301)
    rid = str(host.json('claim')['request_id'])
    host.json('switching', rid)
    before = snapshot(host.app)
    result = host.run('done', rid, '--graph', GRAPH2, '--data-date', '2026-11-26', '--new-tiles', '-', stdin=tiles)
    assert result.exit_code == 2 and snapshot(host.app) == before


# ===================================================== F. fils, journaux, sortie ==============================================

def test_no_subcommand_starts_a_thread_and_none_starts_the_scheduler(ready, monkeypatch):
    started = []
    monkeypatch.setattr(background.Scheduler, 'start', lambda self: started.append('scheduler'))
    before = set(threading.enumerate())
    host = ready
    host.advance(301)
    payload = host.json('claim')
    rid = str(payload['request_id'])
    host.json('catalog', '-', stdin=raw(frozen_document()))
    host.json('switching', rid)
    host.json('done', rid, '--graph', GRAPH2, '--data-date', '2026-11-24', '--new-tiles', '-', stdin=b'[]')
    host.json('report', '--graph', GRAPH2, '--zone', '-', stdin=zone_json(LILLE_ZONE, ['belgium']))
    host.json('failed', '999', '--code', 'other', code=3)
    assert set(threading.enumerate()) == before and started == []


def test_a_flask_command_process_never_starts_the_background_scheduler(tmp_path, monkeypatch):
    """Inventaire n° 29 : `flask routing-zone` est lancé chaque minute ; `_start_scheduler` ne doit rien démarrer hors service."""
    from tests.pw_harness import make_app
    started = []
    monkeypatch.setattr(background.Scheduler, 'start', lambda self: started.append(1))
    monkeypatch.setattr('app.mailer.start_queue', lambda app: started.append(2))
    monkeypatch.setenv('FLASK_RUN_FROM_CLI', 'true')
    monkeypatch.delenv('KMLVIEWER_WATCHER', raising=False)
    make_app(tmp_path / 'cli', ROUTING_ZONE_BUILD=True, SCHEDULER_ENABLED=True)
    assert started == []
    monkeypatch.delenv('FLASK_RUN_FROM_CLI')
    monkeypatch.setenv('SERVER_SOFTWARE', 'gunicorn/23')
    make_app(tmp_path / 'served', ROUTING_ZONE_BUILD=True, SCHEDULER_ENABLED=True)
    assert started == [1, 2]                                                                  # contrôle : un processus servi, lui, le démarre


def test_stdout_is_exactly_one_json_line_and_logs_stay_on_stderr_without_a_coordinate(ready, caplog):
    host = ready
    host.advance(301)
    result = host.run('claim')
    assert result.exit_code == 0 and len(result.stdout.strip().splitlines()) == 1 and json.loads(result.stdout)
    bad = host.run('report', '--graph', GRAPH2, '--zone', '-',
                   stdin=zone_json([[5.123456, 49.654321], [5.2, 49.7], [5.2, 49.654321], [5.123456, 49.7]], [], [0, 0, 1, 1]))
    assert bad.exit_code == 2 and bad.stdout.strip() == ''
    for text in (bad.stderr, caplog.text):
        assert '5.123456' not in text and '49.654321' not in text and '5.2' not in text.replace('5.2.', '')
    assert not [r for r in caplog.records if '5.123456' in r.getMessage()]


def test_the_command_group_without_arguments_shows_help_and_changes_nothing(host):
    before = snapshot(host.app)
    result = host.runner.invoke(args=['routing-zone'])
    assert result.exit_code in (0, 2) and 'claim' in result.output and snapshot(host.app) == before        # click ≥ 8.2 : aide + code 2


# ===================================================== G. delta TZ2-a (relecture) =============================================

SECRET = '5.123456,49.654321'
INTERNAL = {
    'catalog': (['catalog', '-'], 'load_catalog_document', raw(frozen_document())),
    'claim': (['claim'], 'claim', b''),
    'switching': (['switching', '1'], 'switching', b''),
    'done': (['done', '1', '--graph', GRAPH2, '--data-date', '2026-11-24', '--new-tiles', '-'], 'done', b'[]'),
    'failed': (['failed', '1', '--code', 'other'], 'failed', b''),
    'report': (['report', '--graph', GRAPH2, '--zone', '-'], 'report', None),
}


@pytest.mark.parametrize('command', sorted(INTERNAL))
@pytest.mark.parametrize('error', [RuntimeError, ValueError, sqlite3.OperationalError], ids=lambda e: e.__name__)
def test_an_exception_outside_the_contract_exits_2_without_a_trace_nor_the_message_and_writes_nothing(ready, monkeypatch, command, error):
    """TZ2a-n4 : une panne imprévue (GEOS, base verrouillée) ne montre ni pile ni coordonnée, et sort en 2."""
    args, target, stdin = INTERNAL[command]
    stdin = zone_json() if stdin is None else stdin

    def boom(*a, **k):
        raise error(f'database is locked {SECRET}')

    monkeypatch.setattr(coverage, target, boom)
    before = snapshot(ready.app)
    result = ready.run(*args, stdin=stdin)
    assert result.exit_code == 2 and isinstance(result.exception, SystemExit), (result.exit_code, result.exception)
    assert result.stdout.strip() == ''
    for text in (result.stderr, result.output):
        assert 'Traceback' not in text and SECRET not in text and '5.123456' not in text and '49.654321' not in text
    assert error.__name__ in result.stderr                                        # le type seul, pour l'exploitation
    assert snapshot(ready.app) == before


def test_a_replayed_switching_on_a_request_already_starting_exits_0_and_changes_nothing(ready):
    """TZ2a-n6 : l'hôte rejoue `switching` (coupure après la bascule) : 0, jamais 3 (qui lui ferait supprimer le graphe neuf)."""
    host = ready
    host.advance(301)
    rid = str(host.json('claim')['request_id'])
    host.json('switching', rid)
    assert state_row(connect(host.app))['status'] == 'starting'
    before = snapshot(host.app)
    replay = host.run('switching', rid)
    assert replay.exit_code == 0, replay.stderr
    assert snapshot(host.app) == before                                           # statut `starting` inchangé, aucune entrée d'audit en plus
    assert host.run('switching', rid).exit_code == 0                              # et autant de fois que nécessaire
    host.json('done', rid, '--graph', GRAPH2, '--data-date', '2026-11-24', '--new-tiles', '-', stdin=b'[]')
    assert state_row(connect(host.app))['status'] == 'idle' and state_row(connect(host.app))['active_graph'] == GRAPH2


@pytest.mark.parametrize('environment, expected', [({'FLASK_RUN_FROM_CLI': 'true'}, False),
                                                   ({'FLASK_RUN_FROM_CLI': 'true', 'KMLVIEWER_WATCHER': '1'}, True),
                                                   ({}, True)],
                         ids=['flask-command', 'flask-run-dev-server', 'served-process'])
def test_under_a_flask_command_there_is_no_legacy_warning_and_no_activation_only_a_served_process_does(tmp_path, monkeypatch, caplog, environment,
                                                                                                    expected):
    """TZ2a-n2 : `flask routing-zone` tourne chaque minute ; ni WARNING DEM_BBOX ni `ensure_activation` (même règle que l'ordonnanceur)."""
    from tests.pw_harness import make_app
    calls = []
    original = coverage.ensure_activation
    monkeypatch.setattr(coverage, 'ensure_activation', lambda *a, **k: calls.append(1) or original(*a, **k))
    for name in ('FLASK_RUN_FROM_CLI', 'KMLVIEWER_WATCHER'):
        monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv('DEM_BBOX', '41,-6,52,10')
    monkeypatch.setenv('ROUTING_EXTRACT_URLS', 'https://download.geofabrik.de/europe/belgium-latest.osm.pbf')
    with caplog.at_level(logging.WARNING):
        app = make_app(tmp_path, ROUTING_ZONE_BUILD=True, OUTBOUND_ALLOWED_HOSTS='routing=engine.test:8989')
    warned = [r for r in caplog.records if 'DEM_BBOX' in r.getMessage() or 'ROUTING_EXTRACT_URLS' in r.getMessage()]
    assert bool(warned) is expected and bool(calls) is expected, (warned, calls)
    assert coverage.enabled(app.config)                                              # le mode reste actif : seule l'activation au démarrage saute


def test_the_real_shaped_index_with_entries_outside_europe_is_loaded_and_an_invalid_entry_inside_is_a_visible_refusal(host):
    """E1 : exit 0 avec les `us/*` ; un identifiant invalide DANS le sous-arbre ⇒ exit 2, message sur stderr, ancien catalogue gardé."""
    assert host.json('catalog', '-', stdin=raw(frozen_document())) == {'regions': 70}
    before = snapshot(host.app)
    document = frozen_document()
    document['features'].append({'properties': {'id': 'france/us', 'parent': 'france', 'urls': {'pbf': 'https://download.geofabrik.de/europe/a-latest.osm.pbf'},
                                                'size': 1}, 'geometry': None})
    result = host.run('catalog', '-', stdin=raw(document))
    assert result.exit_code == 2 and result.stdout.strip() == '' and result.stderr.strip().startswith('invalide')
    assert snapshot(host.app) == before
