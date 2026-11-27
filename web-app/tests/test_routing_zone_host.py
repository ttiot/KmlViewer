"""
Tâche de l'hôte (rôle test) : `routing/zone_update.py` et `routing/update.sh` selon routing-zone-donnees ZD-8 à ZD-11, CA-ZD9 [hôte].
Le script est lancé EN PROCESSUS (banc : tests/routing_host_support.py) contre la VRAIE commande `flask routing-zone` de l'application,
Docker et le réseau étant simulés. Écrit depuis la spec : les assertions portent sur les fichiers du volume, le journal des appels et
l'état de l'application, jamais sur les détails internes du script.

 A. bout en bout et accord hôte/application (catalogue, claim, zone.json, dalles)
 B. sortie de `claim` non fiable
 C. plafonds recalculés par l'hôte, budget
 D. redirections et téléchargement (même jour, cache, mensuelle, taille réelle)
 E. recadrage, fusion, import
 F. conduite selon les codes de l'application (switching, done, étape en suspens, 1 et 137)
 G. bascule : santé, échec du lien (TZ2c-M1), garde du graphe en service (TZ2c-n1)
 H. dalles (ZD-8), cache des extraits (ZD-11), budget en octets (TZ2c-m1)
 I. verrou, configuration, journaux
"""

import fcntl
import importlib.util
import json
import re
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest

from app.services.routing import coverage as geometry
from tests.pw_harness import connect
from tests.routing_host_support import (OLD_GRAPH, SCRIPT, UPDATE_SH, ENV_NAMES, Clock, FakeHost, Web, yymmdd)
from tests.routing_zone_support import (CLI, DEFAULT_REGIONS, DEFAULT_ZONE, LILLE_ZONE, audit_mark, routing_audit, state_row, zone_app)

pytestmark = pytest.mark.skipif(not SCRIPT.exists(), reason="routing/zone_update.py absent (tests lancés hors du dépôt complet)")

NEW_ZONE = [[3.123457, 50.412346], [3.412345, 50.412346], [3.412345, 50.812347], [3.123457, 50.812347]]     # coordonnées reconnaissables
BIG_ZONE = [[2.0, 49.3], [3.6, 49.3], [3.6, 51.0], [2.0, 51.0]]
NORMANDIE = [[1.35, 48.83], [6.42, 48.83], [6.42, 51.55], [1.35, 51.55], [1.35, 49.75], [-0.5, 49.4], [-0.5, 49.05], [1.35, 49.05]]


@pytest.fixture(scope='module')
def zu():
    spec = importlib.util.spec_from_file_location('zone_update_under_test', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class World:
    """L'application réelle, l'hôte réel (le script), Docker et Geofabrik simulés, une horloge commune."""

    def __init__(self, tmp_path, monkeypatch, zu):
        self.zu, self.mp, self.tmp = zu, monkeypatch, tmp_path
        self.clock = Clock()
        self.app = zone_app(tmp_path / 'app')
        self.app.config['ROUTING_ZONE_CLOCK'] = self.clock
        self.web = Web(self.clock)
        self.host = FakeHost(tmp_path / 'host', self.clock, self.web, self.app.test_cli_runner())
        project = tmp_path / 'project'
        project.mkdir()
        (project / 'docker-compose.yml').write_text('')
        monkeypatch.chdir(project)
        for name in ENV_NAMES:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setattr(subprocess, 'run', self.host.run)
        monkeypatch.setattr(zu._OPENER, 'open', self.web.open)
        monkeypatch.setattr(zu, 'utcnow', lambda: self.clock.now)
        self.host.seed_graph(OLD_GRAPH, zone={'polygon': DEFAULT_ZONE, 'extract_bbox': geometry.extract_bbox(DEFAULT_ZONE, 10),
                                              'regions': DEFAULT_REGIONS})

    # -- pilotage -------------------------------------------------------------------------------------------------------------
    def env(self, **values):
        for key, value in values.items():
            self.mp.setenv(key, str(value))

    def run(self, **env):
        self.env(**env)
        return self.zu.run_pass(self.zu.Config())

    def advance(self, seconds=0, days=0):
        self.clock.advance(seconds, days)

    def request(self, polygon):
        conn = connect(self.app)
        self.app.config['SETTINGS_STORE'].set_many(conn, {'routing.coverage_polygon': polygon}, actor=CLI)
        conn.close()

    def bootstrap(self, **env):
        """Premier passage : rapport du graphe en service, catalogue chargé (rien à prendre)."""
        assert self.run(**env) == 0
        assert self.host.builds() == []

    def ask(self, polygon=LILLE_ZONE, **env):
        """Zone demandée, délai de stabilité écoulé, un passage."""
        self.request(polygon)
        self.advance(330)  # délai de stabilité (300 s) + marge : l'application date la demande à l'heure réelle, l'horloge du banc est figée au départ
        return self.run(**env)

    def built(self, polygon=LILLE_ZONE, **env):
        self.bootstrap()
        assert self.ask(polygon, **env) == 0
        assert self.state()['status'] == 'idle' and self.state()['active_graph'] == self.host.current(), self.state()
        return self.state()

    def state(self):
        conn = connect(self.app)
        try:
            return state_row(conn)
        finally:
            conn.close()

    def regions(self):
        return json.loads(self.state()['active_regions'])

    def local_catalog(self):
        return self.host.read_json('catalog.json')['regions']

    def mark(self):
        return len(self.host.log)

    def since(self, mark):
        return self.host.log[mark:]

    def pending_claim(self, polygon=LILLE_ZONE, **override):
        """Demande réelle prise par l'application (zone demandée, délai écoulé) : sortie de `claim` et numéro."""
        self.request(polygon)
        self.advance(330)  # délai de stabilité (300 s) + marge : l'application date la demande à l'heure réelle, l'horloge du banc est figée au départ
        code, out = self.host.app_call(['claim'], b'')
        assert code == 0
        return json.loads(out)

    def claim_json(self, **override):
        base = {'request_id': 7, 'reason': 'zone', 'polygon': LILLE_ZONE, 'extract_bbox': [2.8, 50.2, 3.6, 51.0],
                'regions': ['belgium', 'nord-pas-de-calais'], 'dem_tiles': [], 'margin_km': 10}
        base.update(override)
        return json.dumps(base).encode()

    def codes(self, sub):
        return [e[-1] for e in self.host.apps(sub)]


@pytest.fixture
def w(tmp_path, monkeypatch, zu):
    return World(tmp_path, monkeypatch, zu)


def assert_call_order(journal):
    """CA-ZD9 : jamais `done` sans un `switching` à 0 pour le même numéro ; jamais deux `switching` à 0 sans rejeu perdu entre les deux."""
    answered = {}
    for entry in journal:
        if entry[0] != 'app' or entry[1] not in ('switching', 'done'):
            continue
        rid, code = entry[2], entry[-1]
        if entry[1] == 'switching' and code == 0:
            answered[rid] = answered.get(rid, 0) + 1
        if entry[1] == 'done':
            assert answered.get(rid), f"done {rid} sans switching à 0 : {journal}"


def failed_calls(w_):
    return [(e[2], e[4]) for e in w_.host.apps('failed')]


# ===================================================== A. bout en bout et accord hôte / application ============================

def test_a_whole_task_from_the_request_to_the_new_graph_in_service_and_the_application_aligned(w):
    w.bootstrap()
    mark = w.mark()
    assert w.ask(NEW_ZONE) == 0
    journal = w.since(mark)
    state = w.state()
    assert [e[1] for e in journal if e[0] == 'app'] == ['report', 'claim', 'switching', 'done'] and all(e[-1] == 0 for e in journal if e[0] == 'app')
    regions = json.loads(state['active_regions'])
    assert len(regions) >= 2 and state['status'] == 'idle' and state['active_graph'] == w.host.current() != OLD_GRAPH
    assert w.host.extracts() == regions and [e[1] for e in journal if e[0] == 'fetch'] == regions
    extracts = [i for i, e in enumerate(journal) if e[0] == 'extract']
    merge = next(i for i, e in enumerate(journal) if e[0] == 'merge')
    assert len(extracts) == len(regions) and max(extracts) < merge                      # ZD-9.6 : chaque extrait recadré AVANT la fusion
    assert all(journal[i][3] == 'extracts' for i in extracts)                          # jamais d'extraction sur le fusionné
    bbox = json.loads(state['active_extract_bbox'])
    assert all(journal[i][2] == ','.join(f"{n:.4f}" for n in bbox) for i in extracts)
    merged = sorted(p.name for p in (w.host.state / 'merged').iterdir())
    assert len(merged) == 1 and re.fullmatch(r'sources-20\d{6}\.osm\.pbf', merged[0])
    assert w.host.read_json('pending-step.json') is None and w.host.read_json('budget.json')['builds'] == 1
    assert w.host.read_json('last-success.json')['request_id'] == state['request_id']
    zone = w.host.read_json(f"graphs/{w.host.current()}/zone.json")
    assert zone['regions'] == regions and zone['extract_bbox'] == bbox and zone['data_date'] == state['data_date']
    assert sorted(w.host.graphs()) == sorted([OLD_GRAPH, w.host.current()])
    assert not (w.host.state / 'work').exists()


def test_the_report_of_the_new_graph_is_the_one_the_application_already_holds_nothing_changes_and_nothing_is_audited(w):
    w.built(NEW_ZONE)
    conn = connect(w.app)
    mark, before = audit_mark(conn), {k: v for k, v in w.state().items() if not k.startswith('budget_')}
    w.advance(60)
    mark_log = w.mark()
    assert w.run() == 0
    assert [e[1] for e in w.since(mark_log) if e[0] == 'app'] == ['report', 'claim']
    assert routing_audit(conn, mark) == [] and {k: v for k, v in w.state().items() if not k.startswith('budget_')} == before                        # accord des deux contrats (zone.json ⇒ empreinte)
    conn.close()


def test_the_catalog_sent_by_the_host_is_accepted_by_the_application_and_both_hold_the_same_regions(w):
    w.bootstrap()
    conn = connect(w.app)
    stored = json.loads(conn.execute("SELECT content FROM routing_catalog").fetchone()[0])
    conn.close()
    local = w.local_catalog()
    assert sorted(local) == sorted(r['id'] for r in stored) and len(local) == 70 and 'great-britain' not in local
    assert all(v['size'] == 2000 and v['url'].startswith('https://download.geofabrik.de/') for v in local.values())
    assert w.codes('catalog') == [0] and w.host.read_json('catalog-meta.json')['loaded_at']


def test_a_real_claim_of_the_application_is_accepted_by_the_host_with_the_same_regions_bbox_and_tiles(w):
    w.bootstrap()
    payload = w.pending_claim(NORMANDIE)
    config = w.zu.Config()
    claim = w.zu.validate_claim(json.dumps(payload), w.local_catalog(), config)
    assert claim['regions'] == sorted(payload['regions']) and claim['bbox'] == payload['extract_bbox']
    assert claim['tiles'] == payload['dem_tiles'] and len(claim['regions']) == 18


@pytest.mark.parametrize('bbox', [[1.2053, 48.74, 6.5647, 51.64], [-0.5, 48.0, 0.0, 49.0], [2.0, 50.0, 3.0, 51.0], [-5.5, -1.5, 0.2, 0.7],
                                  [179.0, 10.0, 180.0, 11.0], [0.0001, 0.0001, 0.9999, 0.9999], [-180, -90, -179.5, -89.5]])
def test_the_dem_tiles_computed_by_the_host_are_the_ones_of_the_application(zu, bbox):
    assert zu.tiles_for(bbox) == [list(t) for t in geometry.dem_tiles(bbox)]


def test_the_host_accepts_the_same_number_of_polygon_vertices_as_the_application(zu):
    from app.services.routing.zone import MAX_VERTICES
    local = {'belgium': {'url': 'https://download.geofabrik.de/europe/belgium-latest.osm.pbf', 'size': 1}}
    ring = [[3 + 0.01 * i, 50 + 0.01 * (i % 2)] for i in range(MAX_VERTICES)]
    ok = json.dumps({'request_id': 1, 'reason': 'zone', 'polygon': ring, 'extract_bbox': [2.0, 49.0, 4.0, 51.0], 'regions': ['belgium'],
                     'dem_tiles': [], 'margin_km': 10})
    assert zu.validate_claim(ok, local, zu.Config())['regions'] == ['belgium']
    with pytest.raises(zu.BuildFailed):
        zu.validate_claim(json.dumps(dict(json.loads(ok), polygon=ring + [[4, 50]])), local, zu.Config())


# ===================================================== B. sortie de `claim` non fiable ========================================

def hostile(marker):
    t = f"$(touch {marker})"
    return [
        ('regions', ['belgium', t], 'other'), ('regions', [f'belgium;touch {marker}'], 'other'), ('regions', [f'belgium\ntouch {marker}'], 'other'),
        ('regions', ['belgium', 'atlantis'], 'other'), ('regions', ['belgium', 'belgium'], 'other'), ('regions', [], 'other'),
        ('regions', 'belgium', 'other'), ('regions', [['belgium']], 'other'), ('regions', ['../belgium'], 'other'), ('regions', ['Belgium'], 'other'),
        ('reason', t, 'other'), ('reason', 'weekly', 'other'), ('reason', None, 'other'),
        ('margin_km', -1, 'other'), ('margin_km', 51, 'other'), ('margin_km', '10', 'other'), ('margin_km', True, 'other'), ('margin_km', 10.5, 'other'),
        ('extract_bbox', [1, 2, 3, t], 'other'), ('extract_bbox', [3, 2, 1, 4], 'other'), ('extract_bbox', [0, 0, 181, 1], 'other'),
        ('extract_bbox', [0, 0, 1], 'other'), ('extract_bbox', 'x', 'other'), ('extract_bbox', [0, 0, float('nan'), 1], 'other'),
        ('extract_bbox', [0, 0, float('inf'), 1], 'other'), ('extract_bbox', [0, 0, True, 1], 'other'),
        ('extract_bbox', [-179, -80, 179, 80], 'cap'),                                  # emprise du monde : dalles recalculées par l'hôte
        ('polygon', t, 'other'), ('polygon', [[0, 0], [1, 1]], 'other'), ('polygon', [[0, 0], [1, 1], ['a', 2]], 'other'),
        ('polygon', [[0, 0, 0], [1, 1, 1], [2, 2, 2]], 'other'), ('polygon', [[200, 0], [1, 1], [2, 2]], 'other'),
        ('dem_tiles', t, None),                                                           # champ de l'application ignoré : sans effet
    ]


def hostile_ids():
    return [f"{name}={str(value)[:18]}" for name, value, _ in hostile('m')]


def test_the_hostile_claims_are_a_failed_and_never_a_build_a_command_or_a_file(w, tmp_path):
    marker = tmp_path / 'created-by-claim'
    w.bootstrap()
    w.env(DEM_URL=DEM)                                       # le plafond des dalles n'existe qu'avec le MNT local (ZD-1) : cas « emprise du monde »
    cases = hostile(marker)
    assert len(cases) >= 30
    for field, value, expected in cases:
        mark = w.mark()
        w.host.app_script['claim'] = [(0, w.claim_json(**{field: value}))]
        assert w.run() == 0, (field, value)
        journal = w.since(mark)
        if expected is None:
            continue
        assert [e for e in journal if e[0] in ('fetch', 'extract', 'merge', 'import', 'dem')] == [], (field, value)
        assert [(e[2], e[3], e[4]) for e in journal if e[0] == 'app' and e[1] == 'failed'] == [('7', '--code', expected)], (field, value, journal)
        assert [e[1] for e in journal if e[0] == 'app'] == ['report', 'claim', 'failed']
    assert not marker.exists() and w.host.unlisted == [] and w.host.shell_used == []


@pytest.mark.parametrize('request_id', [True, 0, -3, '7', 7.5, None, [7]])
def test_a_claim_without_a_usable_number_ends_the_pass_with_no_build_and_no_failed(w, request_id):
    w.bootstrap()
    w.host.app_script['claim'] = [(0, w.claim_json(request_id=request_id))]
    mark = w.mark()
    assert w.run() == 0
    assert w.host.apps('failed') == [] and [e for e in w.since(mark) if e[0] != 'app'] == []


@pytest.mark.parametrize('garbage', [b'$(touch /tmp/x); rm -rf /', b'[1, 2]', b'"{}"', b'null', b'{"request_id": 3', b'\xff\xfe', b'7'])
def test_a_claim_output_that_is_not_a_json_object_changes_nothing(w, garbage):
    w.bootstrap()
    w.host.app_script['claim'] = [(0, garbage)]
    mark = w.mark()
    assert w.run() == 0
    assert w.host.apps('failed') == [] and w.host.builds() == [] and w.host.unlisted == [] and not any(e[0] == 'up' for e in w.since(mark))


def test_a_valid_claim_control_proceeds_to_the_build(w):
    w.bootstrap()
    w.host.app_script['claim'] = [(0, w.claim_json())]
    assert w.run() == 0
    assert [e[1] for e in w.host.events('fetch')] == ['belgium', 'nord-pas-de-calais']


# ===================================================== C. plafonds de l'hôte, budget ==========================================

def test_the_caps_are_recomputed_by_the_host_with_its_own_environment(w):
    w.bootstrap()
    payload = w.pending_claim(NEW_ZONE)
    n = len(payload['regions'])
    for env, label in (({'ROUTING_MAX_REGIONS': n - 1}, 'régions'), ({'ROUTING_MAX_SOURCE_BYTES': 2000 * n - 1}, 'octets'),
                       ({'DEM_MAX_TILES': len(payload['dem_tiles']) - 1, 'DEM_URL': 'http://opentopodata:5000/v1/copernicus30'}, 'dalles')):
        w.host.app_script['claim'] = [(0, json.dumps(payload).encode())]
        mark = w.mark()
        assert w.run(**env) == 0
        assert [(e[3], e[4]) for e in w.since(mark) if e[0] == 'app' and e[1] == 'failed'] == [('--code', 'cap')], label
        assert [e for e in w.since(mark) if e[0] in ('fetch', 'merge', 'import')] == [], label
        for name in env:
            w.mp.delenv(name)


def seed_budget(w, builds, size=0, day=None):
    w.host.write_json('budget.json', {'day': day or w.clock.now.strftime('%Y-%m-%d'), 'builds': builds, 'bytes': size})


def test_the_seventh_task_of_the_day_is_refused_with_budget_and_nothing_is_downloaded(w):
    w.bootstrap()
    seed_budget(w, 6)
    mark = w.mark()
    assert w.ask() == 0
    assert [(e[3], e[4]) for e in w.since(mark) if e[0] == 'app' and e[1] == 'failed'] == [('--code', 'budget')]
    assert w.host.builds() == [] and w.state()['status'] == 'failed' and w.state()['last_failure_code'] == 'budget'


def test_the_sixth_task_goes_through_and_is_counted_the_counter_restarts_on_a_new_utc_day(w):
    w.bootstrap()
    seed_budget(w, 5)
    assert w.ask() == 0 and w.host.read_json('budget.json')['builds'] == 6 and w.state()['status'] == 'idle'
    seed_budget(w, 6, day=(w.clock.now - timedelta(days=1)).strftime('%Y-%m-%d'))
    assert w.ask(NEW_ZONE) == 0 and w.host.read_json('budget.json') == {'day': w.clock.now.strftime('%Y-%m-%d'), 'builds': 1,
                                                                        'bytes': w.host.read_json('budget.json')['bytes']}


def test_the_byte_budget_of_the_day_refuses_a_task_whose_extracts_do_not_fit(w):
    w.bootstrap()
    seed_budget(w, 0, size=10_000 - 1)
    mark = w.mark()
    assert w.ask(ROUTING_MAX_DOWNLOAD_BYTES_PER_DAY=10_000) == 0
    assert [(e[3], e[4]) for e in w.since(mark) if e[0] == 'app' and e[1] == 'failed'] == [('--code', 'budget')] and w.host.events('fetch') == []


def test_a_refused_monthly_is_not_a_failed_and_is_tried_again_the_next_day(w):
    w.built()
    w.host.write_json('last-success.json', {'at': (w.clock.now - timedelta(days=31)).strftime('%Y-%m-%dT%H:%M:%SZ'), 'request_id': 1})
    seed_budget(w, 6)
    mark = w.mark()
    assert w.run() == 0
    assert [e for e in w.since(mark) if e[0] == 'app' and e[1] == 'claim'] == [('app', 'claim', 0)]            # sans --monthly
    assert w.host.apps('failed') == [] and [e for e in w.since(mark) if e[0] in ('fetch', 'merge', 'import')] == []
    w.advance(days=1)
    mark = w.mark()
    assert w.run() == 0
    assert [e[:3] for e in w.since(mark) if e[0] == 'app' and e[1] == 'claim'] == [('app', 'claim', '--monthly')]


def test_a_monthly_rebuild_downloads_every_extract_again_even_when_the_cache_is_good(w):
    w.built()
    first = len(w.host.events('fetch'))
    w.host.write_json('last-success.json', {'at': (w.clock.now - timedelta(days=31)).strftime('%Y-%m-%dT%H:%M:%SZ'), 'request_id': 1})
    w.advance(60)
    assert w.run() == 0
    assert len(w.host.events('fetch')) == 2 * first and w.state()['status'] == 'idle'


# ===================================================== D. redirections, même jour, cache, taille réelle ========================

def latest(w_, rid):
    return w_.local_catalog()[rid]['url']


def scripted_claim(w_, **fields):
    w_.host.app_script['claim'] = [(0, w_.claim_json(**fields))]


def failed_codes(w_, mark=0):
    return [(e[2], e[4]) for e in w_.since(mark) if e[0] == 'app' and e[1] == 'failed']


REDIRECTS = ['https://evil.example.com/europe/belgium-{d}.osm.pbf', 'http://download.geofabrik.de/europe/belgium-{d}.osm.pbf',
             'https://download.geofabrik.de.evil.com/europe/belgium-{d}.osm.pbf', 'https://download.geofabrik.de@evil.com/europe/belgium-{d}.osm.pbf',
             'https://evil.com/download.geofabrik.de/belgium-{d}.osm.pbf', 'https://download.geofabrik.de/europe/../belgium-{d}.osm.pbf',
             'https://download.geofabrik.de/europe//belgium-{d}.osm.pbf', 'ftp://download.geofabrik.de/belgium-{d}.osm.pbf',
             'file:///etc/passwd', '//evil.com/x-{d}.osm.pbf', 'https://download.geofabrik.de:8443/europe/belgium-{d}.osm.pbf',
             'https://download.geofabrik.de/europe/belgium-{d}.osm.pbf?x=1', 'https://download.geofabrik.de/europe/belgium-{d}.zip']


@pytest.mark.parametrize('target', REDIRECTS)
def test_a_redirect_outside_the_allowed_hosts_or_the_simple_https_urls_is_never_followed_and_nothing_is_downloaded(w, target):
    w.bootstrap()
    day = yymmdd(w.clock.now - timedelta(days=1))
    w.web.override[latest(w, 'belgium')] = ('302', target.format(d=day))
    scripted_claim(w)
    mark = w.mark()
    assert w.run() == 0
    assert failed_codes(w, mark) == [('7', 'download')] and w.host.builds() == []
    assert not [url for _, url in w.web.calls if 'evil' in url or url.startswith(('http://', 'ftp', 'file'))]


def test_the_md5_file_is_subject_to_the_same_redirect_rules(w):
    w.bootstrap()
    day = yymmdd(w.clock.now - timedelta(days=1))
    dated = w.web.dated(latest(w, 'belgium'))
    w.web.override[dated + '.md5'] = ('302', f'https://evil.example.com/belgium-{day}.osm.pbf.md5')
    scripted_claim(w)
    mark = w.mark()
    assert w.run() == 0
    assert failed_codes(w, mark) == [('7', 'download')] and w.host.events('fetch') == [] and not [1 for _, u in w.web.calls if 'evil' in u]


def hops(w_, n):
    """latest -> hop1 -> … -> daté : `n` redirections."""
    chain = [latest(w_, 'belgium')] + [f"https://download.geofabrik.de/europe/hop{i}" for i in range(1, n)] + [w_.web.dated(latest(w_, 'belgium'))]
    for here, there in zip(chain, chain[1:]):
        w_.web.override[here] = ('302', there)
    return chain


def test_at_most_two_redirects_are_followed(w):
    w.bootstrap()
    hops(w, 3)                                                   # latest -> hop1 -> hop2 -> daté : trois sauts
    scripted_claim(w)
    mark = w.mark()
    assert w.run() == 0
    assert failed_codes(w, mark) == [('7', 'download')] and w.host.events('fetch') == []


def test_two_redirects_are_accepted_and_a_relative_location_is_resolved_against_the_current_url(w):
    w.bootstrap()
    dated = w.web.dated(latest(w, 'belgium'))
    w.web.override[latest(w, 'belgium')] = ('302', 'https://download.geofabrik.de/europe/hop1')
    w.web.override['https://download.geofabrik.de/europe/hop1'] = ('302', '/' + dated.split('/', 3)[3])
    scripted_claim(w)
    assert w.run() == 0 and [e[1] for e in w.host.events('fetch')] == ['belgium', 'nord-pas-de-calais']


def test_extracts_published_on_different_days_or_too_old_are_refused_before_any_download(w):
    w.bootstrap()
    today = w.clock.now
    for dates, label in (({'belgium': yymmdd(today - timedelta(days=1)), 'nord-pas-de-calais': yymmdd(today - timedelta(days=2))}, 'jours'),
                         ({'belgium': yymmdd(today - timedelta(days=36)), 'nord-pas-de-calais': yymmdd(today - timedelta(days=36))}, 'âge')):
        w.web.dates = dates
        scripted_claim(w)
        mark = w.mark()
        assert w.run() == 0
        assert failed_codes(w, mark) == [('7', 'download')] and w.host.events('fetch') == [], label
    w.web.dates = {rid: yymmdd(today - timedelta(days=34)) for rid in ('belgium', 'nord-pas-de-calais')}
    scripted_claim(w)
    assert w.run() == 0 and w.host.events('fetch')                                # 34 jours : accepté


def test_with_the_cache_at_the_published_date_nothing_is_downloaded_and_a_missing_extract_alone_is_downloaded(w):
    w.built(LILLE_ZONE)
    regions = w.regions()
    first = list(w.host.events('fetch'))
    assert w.ask(NEW_ZONE) == 0 and w.regions() == regions and w.host.events('fetch') == first          # cache à la date D : aucun
    (w.host.state / 'extracts' / 'belgium.osm.pbf').unlink()
    w.advance(60)
    assert w.ask(LILLE_ZONE) == 0
    assert [e[1] for e in w.host.events('fetch')[len(first):]] == ['belgium']                           # le manquant seul
    assert w.state()['status'] == 'idle'


def test_a_new_region_published_on_the_same_day_is_the_only_one_downloaded(w):
    w.built(LILLE_ZONE)
    before = w.regions()
    first = len(w.host.events('fetch'))
    w.advance(60)
    assert w.ask(BIG_ZONE) == 0
    new = [r for r in w.regions() if r not in before]
    assert new and set(before) < set(w.regions())
    assert sorted(e[1] for e in w.host.events('fetch')[first:]) == sorted(new)


def test_a_cache_at_another_date_than_the_published_one_is_downloaded_again_in_full(w):
    w.built(LILLE_ZONE)
    regions, first = w.regions(), len(w.host.events('fetch'))
    w.advance(days=1)                                                      # Geofabrik a publié un jour de plus
    assert w.ask(NEW_ZONE) == 0
    assert sorted(e[1] for e in w.host.events('fetch')[first:]) == sorted(regions)


def test_an_extract_streaming_more_than_announced_is_cut_and_refused_with_cap_and_leaves_nothing_behind(w):
    w.bootstrap()
    w.web.actual['belgium'] = 12_000_000                                  # HEAD annonce 2000 octets
    scripted_claim(w)
    mark = w.mark()
    assert w.run() == 0
    assert failed_codes(w, mark) == [('7', 'cap')] and [e[1] for e in w.host.events('fetch')] == ['belgium']
    assert not list((w.host.state / 'extracts').glob('*.part')) and w.host.extracts() == []
    assert 1_000_000 <= w.host.read_json('budget.json')['bytes'] <= 1_100_000          # reçus et bornés à taille + 1 Mio : comptés (TZ2c-m1)


def test_the_total_received_is_checked_against_the_cap_even_when_the_catalog_sizes_were_underestimated(w):
    w.web.sizes = {'belgium': 100, 'nord-pas-de-calais': 100}              # tailles du catalogue : 200 au total
    w.bootstrap()
    w.web.sizes = {'belgium': 400, 'nord-pas-de-calais': 400}              # tailles réelles : 800 au total
    scripted_claim(w)
    mark = w.mark()
    assert w.run(ROUTING_MAX_SOURCE_BYTES=700) == 0
    assert failed_codes(w, mark) == [('7', 'cap')] and [e[1] for e in w.host.events('fetch')] == ['belgium']


def test_a_wrong_md5_is_tried_twice_then_refused_and_the_extracts_already_received_stay_in_the_cache(w):
    w.bootstrap()
    w.web.bad_md5 = {'nord-pas-de-calais'}
    scripted_claim(w)
    mark = w.mark()
    assert w.run() == 0
    assert failed_codes(w, mark) == [('7', 'download')] and w.host.builds() == w.host.events('fetch')
    attempts = [json.loads(line)[-1] for line in w.host.curl_log.read_text().splitlines()]
    assert attempts.count(w.web.dated(latest(w, 'nord-pas-de-calais'))) == 2
    assert w.host.extracts() == ['belgium'] and not list((w.host.state / 'extracts').glob('*.part'))
    assert all('-L' not in json.loads(line) and '--proto' in json.loads(line) for line in w.host.curl_log.read_text().splitlines())
    assert w.host.read_json('budget.json')['bytes'] >= 2000


# ===================================================== E. recadrage, fusion, import ===========================================

def test_a_merged_file_one_byte_above_the_default_cap_is_refused_without_import_and_the_cap_itself_is_accepted(w):
    w.bootstrap()
    scripted_claim(w)
    w.host.merged_size = 2_400_000_001
    mark = w.mark()
    assert w.run() == 0
    assert failed_codes(w, mark) == [('7', 'cap')] and w.host.events('import') == [] and list((w.host.state / 'merged').iterdir()) == []
    w.host.merged_size = 2_400_000_000
    scripted_claim(w)
    w.advance(60)
    assert w.run() == 0 and len(w.host.events('import')) == 1


@pytest.mark.parametrize('failure, code', [('extract:belgium', 'extract'), ('merge', 'merge'), ('import', 'import'), ('incomplete', 'incomplete'),
                                           ('import-timeout', 'timeout'), ('build', 'other'), ('init', 'other'), ('state-init', None)])
def test_a_failing_step_ends_with_its_code_and_leaves_the_service_graph_and_the_volume_clean(w, failure, code):
    w.bootstrap()
    if failure == 'incomplete':
        w.host.incomplete = True
    else:
        w.host.fail.add(failure)
    scripted_claim(w)
    mark = w.mark()
    rc = w.run()
    if code is None:                                         # le volume d'état n'est pas initialisable : passage en échec, aucune construction
        assert rc == 1 and w.host.builds() == []
        return
    assert rc == 0 and failed_codes(w, mark) == [('7', code)]
    assert w.host.graphs() == [OLD_GRAPH] and w.host.current() == OLD_GRAPH
    merged = w.host.state / 'merged'
    assert (not merged.exists() or list(merged.iterdir()) == []) and not (w.host.state / 'work' / w.clock.now.strftime('%Y%m%d%H%M%S')).exists()
    assert w.host.read_json('pending-step.json') is None
    if failure == 'import-timeout':
        assert w.host.events('rm-container')


# ===================================================== F. conduite selon les codes de l'application ===========================

def first_app_call(journal):
    return next(e for e in journal if e[0] == 'app')[1]


@pytest.mark.parametrize('code', [2, 1, 137])
def test_a_switching_that_does_not_answer_keeps_everything_fails_nothing_and_is_replayed_before_the_report(w, code):
    w.bootstrap()
    w.host.app_script['switching'] = [code]
    assert w.ask(NEW_ZONE) == 0
    assert w.host.current() == OLD_GRAPH and len(w.host.graphs()) == 2 and w.host.apps('failed') == []
    pending = w.host.read_json('pending-step.json')
    assert pending['stage'] == 'switching' and len(list((w.host.state / 'merged').iterdir())) == 1 and w.state()['status'] == 'building'
    mark = w.mark()
    w.advance(60)
    assert w.run() == 0
    journal = w.since(mark)
    subs = [e[1] for e in journal if e[0] == 'app']
    assert subs[0] == 'switching' and subs.index('switching') < subs.index('report') and 'done' in subs
    assert_call_order(w.host.log)
    assert w.state()['status'] == 'idle' and w.host.current() == w.state()['active_graph'] != OLD_GRAPH and w.host.read_json('pending-step.json') is None


def test_a_switching_whose_answer_is_lost_after_the_application_applied_it_is_replayed_and_the_new_graph_goes_live(w):
    w.bootstrap()
    w.host.app_script['switching'] = [('real_then', 2)]
    assert w.ask(NEW_ZONE) == 0
    assert w.state()['status'] == 'starting' and w.host.current() == OLD_GRAPH and w.host.apps('failed') == []
    w.advance(60)
    assert w.run() == 0
    assert w.codes('switching') == [2, 0] and w.state()['status'] == 'idle' and w.host.current() == w.state()['active_graph']
    assert_call_order(w.host.log)


@pytest.mark.parametrize('code', [3, 4])
def test_a_switching_answered_3_or_4_deletes_the_new_graph_and_the_merged_file_and_keeps_the_extracts(w, code):
    w.bootstrap()
    w.host.app_script['switching'] = [code]
    assert w.ask(NEW_ZONE) == 0
    assert w.host.graphs() == [OLD_GRAPH] and w.host.current() == OLD_GRAPH and w.host.read_json('pending-step.json') is None
    assert list((w.host.state / 'merged').iterdir()) == [] and len(w.host.extracts()) >= 2 and w.host.apps('failed') == []


@pytest.mark.parametrize('code', [2, 1, 137])
def test_a_done_that_does_not_answer_keeps_the_service_graph_fails_nothing_and_is_replayed_before_the_report(w, code):
    w.bootstrap()
    w.host.app_script['done'] = [code]
    assert w.ask(NEW_ZONE) == 0
    new = w.host.current()
    assert new != OLD_GRAPH and w.host.apps('failed') == [] and OLD_GRAPH in w.host.graphs()          # ni retour arrière, ni suppression
    assert w.host.read_json('pending-step.json')['stage'] == 'done' and w.state()['status'] == 'starting'
    mark = w.mark()
    w.advance(60)
    assert w.run() == 0
    subs = [e[1] for e in w.since(mark) if e[0] == 'app']
    assert subs[0] == 'done' and subs.index('done') < subs.index('report')
    assert_call_order(w.host.log)
    assert w.host.read_json('pending-step.json') is None and w.state()['status'] == 'idle' and w.state()['active_graph'] == new
    assert [e for e in w.host.log if e[0] == 'up' and e[1] == 'graphhopper'] != [] and len([e for e in w.host.log if e == ('up', 'graphhopper')]) == 1


def test_a_done_answered_3_closes_the_step_and_the_next_report_realigns_the_application(w):
    w.bootstrap()
    w.host.app_script['done'] = [3]
    assert w.ask(NEW_ZONE) == 0
    new = w.host.current()
    assert w.host.read_json('pending-step.json') is None and w.host.apps('failed') == []
    w.advance(60)
    assert w.run() == 0 and w.state()['active_graph'] == new and w.state()['status'] == 'idle'


def pending_after_lost_switching(w_):
    """Application en `starting` (switching appliqué, réponse perdue), étape `switching` en suspens."""
    w_.bootstrap()
    w_.host.app_script['switching'] = [('real_then', 2)]
    assert w_.ask(NEW_ZONE) == 0
    assert w_.state()['status'] == 'starting' and w_.host.read_json('pending-step.json')['stage'] == 'switching'
    return w_.host.read_json('pending-step.json')['graph']


def test_a_switching_step_older_than_two_hours_is_abandoned_the_new_graph_deleted_and_the_application_freed_from_starting(w):
    new = pending_after_lost_switching(w)
    w.advance(3 * 3600)
    mark = w.mark()
    assert w.run() == 0
    assert new not in w.host.graphs() and w.host.read_json('pending-step.json') is None and w.host.current() == OLD_GRAPH
    assert 'switching' not in [e[1] for e in w.since(mark) if e[0] == 'app']
    assert w.state()['status'] != 'starting', "l'application reste en `starting` (503) alors que l'étape est abandonnée"
    assert w.state()['status'] == 'failed'


def test_a_switching_step_younger_than_two_hours_is_still_replayed(w):
    pending_after_lost_switching(w)
    w.advance(2 * 3600 - 120)
    mark = w.mark()
    assert w.run() == 0
    assert 'switching' in [e[1] for e in w.since(mark) if e[0] == 'app'] and w.state()['status'] == 'idle'


def test_a_done_step_older_than_two_hours_is_abandoned_the_service_graph_is_kept_and_the_report_realigns(w):
    w.bootstrap()
    w.host.app_script['done'] = [2]
    assert w.ask(NEW_ZONE) == 0
    new = w.host.current()
    w.advance(3 * 3600)
    mark = w.mark()
    assert w.run() == 0
    subs = [e[1] for e in w.since(mark) if e[0] == 'app']
    assert 'done' not in subs and 'switching' not in subs and w.host.current() == new and new in w.host.graphs()
    assert w.host.read_json('pending-step.json') is None
    assert w.state()['status'] == 'idle' and w.state()['active_graph'] == new                       # `report` du graphe neuf


@pytest.mark.parametrize('code', [1, 137])
def test_a_report_that_does_not_answer_ends_the_pass_with_nothing_else_called(w, code):
    w.host.app_script['report'] = [code]
    assert w.run() == 0
    assert [e[1] for e in w.host.apps()] == ['report'] and w.host.builds() == []


@pytest.mark.parametrize('code', [1, 137])
def test_a_claim_that_does_not_answer_builds_nothing_calls_no_failed_and_the_request_is_taken_at_the_next_pass(w, code):
    w.bootstrap()
    w.request(NEW_ZONE)
    w.advance(330)
    w.host.app_script['claim'] = [code]
    assert w.run() == 0 and w.host.builds() == [] and w.host.apps('failed') == []
    w.advance(60)
    assert w.run() == 0 and w.state()['status'] == 'idle' and w.host.events('fetch')


@pytest.mark.parametrize('code', [1, 137])
def test_a_catalog_that_does_not_answer_ends_the_pass_without_a_local_copy_nor_a_claim(w, code):
    w.host.app_script['catalog'] = [code]
    assert w.run() == 0
    assert [e[1] for e in w.host.apps()] == ['report', 'catalog'] and w.host.read_json('catalog.json') is None and w.host.apps('failed') == []


def test_a_catalog_refused_by_the_application_keeps_the_previous_local_copy(w):
    w.bootstrap()
    before = w.host.read_json('catalog.json')
    w.advance(days=8)
    w.host.app_script['catalog'] = [2]
    assert w.run() == 0
    assert w.host.read_json('catalog.json') == before and w.host.read_json('catalog-meta.json')['loaded_at'] != w.clock.now.strftime('%Y-%m-%dT%H:%M:%SZ')


def test_a_failed_that_does_not_answer_is_not_retried_in_a_loop(w):
    w.bootstrap()
    scripted_claim(w, regions=['atlantis'])
    w.host.app_script['failed'] = [137]
    mark = w.mark()
    assert w.run() == 0 and len([e for e in w.since(mark) if e[0] == 'app' and e[1] == 'failed']) == 1


# ===================================================== G. bascule : santé, échec du lien, garde ===============================

def test_an_engine_that_does_not_become_healthy_is_rolled_back_deleted_and_reported_as_health(w):
    w.bootstrap()
    w.host.health = 'unhealthy'
    mark = w.mark()
    assert w.ask(NEW_ZONE) == 0
    assert failed_codes(w, mark) == [(str(w.state()['request_id']), 'health')] and w.host.apps('done') == []
    assert w.host.current() == OLD_GRAPH and w.host.graphs() == [OLD_GRAPH] and w.host.read_json('pending-step.json') is None
    assert len([e for e in w.host.log if e == ('up', 'graphhopper')]) == 2 and w.state()['status'] == 'failed'


def test_m1_a_link_switch_that_fails_after_the_switching_frees_the_application_and_is_not_replayed(w):
    """TZ2c-M1 : `graphs/current` devenu un dossier non vide ⇒ failed appelé, graphe neuf supprimé, aucun 503 au passage suivant."""
    w.bootstrap()
    link = w.host.vol / 'graphs' / 'current'
    link.unlink()
    link.mkdir()
    (link / 'keep').write_text('x')
    mark = w.mark()
    assert w.ask(NEW_ZONE) == 0
    request = str(w.state()['request_id'])
    assert w.codes('switching') == [0] and failed_codes(w, mark) == [(request, 'other')]
    assert w.state()['status'] == 'failed' and w.state()['status'] != 'starting'
    assert w.host.graphs() == [OLD_GRAPH] and w.host.read_json('pending-step.json') is None and (link / 'keep').exists()
    assert ('up', 'graphhopper') not in w.host.log and w.host.apps('done') == []
    w.advance(60)
    assert w.run() == 0 and w.codes('switching') == [0]                      # pas de rejeu de `switching`


@pytest.mark.parametrize('code', [3, 4])
def test_a_replayed_switching_answered_3_or_4_never_deletes_the_graph_already_in_service(w, code):
    """TZ2c-n1 : passage tué après la bascule, avant l'étape `done` ; la demande est entre-temps dépassée ou désactivée."""
    w.bootstrap()
    new = 'graph-20261126210000'
    w.host.seed_graph(new, zone=json.loads((w.host.vol / 'graphs' / OLD_GRAPH / 'zone.json').read_text()), current=True)
    w.host.write_json('pending-step.json', {'request_id': 5, 'stage': 'switching', 'graph': new, 'previous': OLD_GRAPH, 'stamp': '20261126210000',
                                            'merged': 'sources-20261125.osm.pbf', 'data_date': '2026-11-25', 'tiles_added': [],
                                            'purged_tiles': [], 'purged_extract_ids': [], 'owned': [],
                                            'claimed_at': w.clock.now.strftime('%Y-%m-%dT%H:%M:%SZ')})
    w.host.app_script['switching'] = [code]
    assert w.run() == 0
    assert (w.host.vol / 'graphs' / new / 'properties').exists() and w.host.current() == new and w.host.read_json('pending-step.json') is None


# ===================================================== H. dalles (ZD-8), cache (ZD-11), budget en octets ========================

DEM = 'http://opentopodata:5000/v1/copernicus30'


def put_tiles(w_, tiles):
    for lat, lon in tiles:
        (w_.host.dem / f"{'N' if lat >= 0 else 'S'}{abs(lat):02d}{'E' if lon >= 0 else 'W'}{abs(lon):03d}.tif").write_bytes(b't')


def tile_files(w_):
    return sorted(p.name for p in w_.host.dem.glob('*.tif'))


def registry(w_):
    return [tuple(t[:2]) for t in (w_.host.read_json('task-tiles.json') or {}).get('tiles', [])]


def done_payload(w_):
    return json.loads([data for sub, data in w_.host.stdin if sub == 'done'][-1])


def test_tiles_present_before_the_task_are_never_deleted_nor_counted_and_the_dem_bbox_is_the_extract_bbox_in_the_expected_order(w):
    before = [(lat, lon) for lat in range(10, 20) for lon in range(100, 110)]
    put_tiles(w, before)
    state = w.built(NEW_ZONE, DEM_URL=DEM)
    bbox = json.loads(state['active_extract_bbox'])
    assert len(tile_files(w)) == 100 + len(registry(w)) and registry(w) and all(t[0] == 50 for t in registry(w))
    assert [e[1] for e in w.host.events('dem')] == [f"{bbox[1]:.4f},{bbox[0]:.4f},{bbox[3]:.4f},{bbox[2]:.4f}"]
    payload = done_payload(w)
    assert sorted(map(tuple, payload['added'])) == sorted(registry(w)) and payload['purged'] == []
    assert ('up', 'opentopodata') in w.host.log


@pytest.mark.parametrize('dem_url', ['', 'https://api.opentopodata.org/v1/copernicus30', 'http://opentopodata.example:5000/v1/copernicus30',
                                      'http://localhost:5000/x', 'opentopodata', 'http://opentopodata.evil.com/x'])
def test_without_the_local_dem_service_no_tile_is_downloaded_counted_or_purged_and_done_gets_an_empty_list(w, dem_url):
    """CA-ZD9 : `DEM_URL` vide ou sur un autre hôte qu'`opentopodata` ⇒ aucun appel à `dem-tiles`, aucune purge, `done` avec une liste vide."""
    tiles = seed_owned(w)                                     # dalles d'un ancien MNT local : jamais touchées sans MNT local
    w.bootstrap()
    before = tile_files(w)
    assert w.ask(NEW_ZONE, DEM_URL=dem_url, DEM_MAX_TILES=2) == 0
    assert w.state()['status'] == 'idle' and w.host.events('dem') == [] and w.host.events('rm-tile') == []
    assert tile_files(w) == before and set(tiles) <= set(registry(w)) and ('up', 'opentopodata') not in w.host.log
    assert done_payload(w) == {'added': [], 'purged': [], 'purged_extracts': 0}
    assert not [e for e in w.host.log if e[0] == 'up' and e[1] != 'graphhopper']


def test_only_the_exact_opentopodata_host_enables_the_dem_step(w):
    w.built(NEW_ZONE, DEM_URL='http://opentopodata:5000/v1/copernicus30')
    assert len(w.host.events('dem')) == 1


def test_a_part_file_older_than_24_hours_is_removed_at_the_end_of_a_task_a_recent_one_is_kept_and_none_counts_as_an_extract(w):
    import os
    w.bootstrap()
    extracts = w.host.state / 'extracts'
    old, recent = extracts / 'stale.osm.pbf.part', extracts / 'fresh.osm.pbf.part'
    old.write_bytes(b'x' * 50)
    recent.write_bytes(b'x' * 50)
    past = (w.clock.now - timedelta(hours=25)).timestamp()
    os.utime(old, (past, past))
    os.utime(recent, None)
    assert w.ask(NEW_ZONE) == 0 and w.state()['status'] == 'idle'
    assert not old.exists() and recent.exists()
    assert 'stale' not in w.host.extracts() and 'fresh' not in w.host.extracts()


def test_a_task_without_new_tile_reports_no_added_tile_and_does_not_restart_the_dem_service(w):
    """TZ2c-n3 : ZD-10, « si des dalles ont été ajoutées »."""
    w.built(NEW_ZONE, DEM_URL=DEM)
    owned = registry(w)
    mark = w.mark()
    w.advance(60)
    assert w.ask(LILLE_ZONE) == 0
    assert registry(w) == owned and done_payload(w)['added'] == [] and ('up', 'opentopodata') not in w.since(mark)


def seed_owned(w_, count=6):
    tiles = [(10, 100 + i) for i in range(count)]
    put_tiles(w_, tiles)
    put_tiles(w_, [(20, 100)])                                                                  # présente avant la tâche : hors du registre
    w_.host.write_json('task-tiles.json', {'tiles': [[lat, lon, f"2026-10-0{i + 1}T00:00:00Z"] for i, (lat, lon) in enumerate(tiles)]})
    return tiles


def test_above_the_cap_only_the_oldest_task_tiles_outside_the_zone_are_purged_after_the_switch(w):
    tiles = seed_owned(w)
    w.built(NEW_ZONE, DEM_URL=DEM, DEM_MAX_TILES=4)
    new = [t for t in registry(w) if t[0] == 50]
    assert len(new) == 2 and sorted(registry(w)) == sorted(tiles[4:] + new)                        # 8 possédées, plafond 4 : les 4 plus anciennes
    assert sorted(map(tuple, done_payload(w)['purged'])) == sorted(tiles[:4])
    assert not any(f"E{100 + i:03d}" in ' '.join(tile_files(w)) and f"N10E{100 + i:03d}.tif" in tile_files(w) for i in range(4))
    assert 'N20E100.tif' in tile_files(w) and 'N10E104.tif' in tile_files(w) and 'N10E105.tif' in tile_files(w) and len(new) == 2


def test_nothing_is_purged_before_the_bascule_is_done_and_the_registry_keeps_every_tile_until_then(w):
    tiles = seed_owned(w)
    w.bootstrap()
    w.host.app_script['switching'] = [2]
    assert w.ask(NEW_ZONE, DEM_URL=DEM, DEM_MAX_TILES=4) == 0
    assert all(f"N10E{100 + i:03d}.tif" in tile_files(w) for i in range(6))
    assert sorted(registry(w)) == sorted(tiles + [t for t in registry(w) if t[0] == 50])
    w.advance(60)
    assert w.run() == 0 and not any(f"N10E{100 + i:03d}.tif" in tile_files(w) for i in range(4))


def test_a_failed_bascule_leaves_the_tiles_planned_for_purge_on_disk_and_in_the_registry(w):
    """TZ2c-m2 : une dalle de la tâche ne devient jamais « présente avant »."""
    tiles = seed_owned(w)
    w.bootstrap()
    w.host.health = 'unhealthy'
    assert w.ask(NEW_ZONE, DEM_URL=DEM, DEM_MAX_TILES=4) == 0
    assert all(f"N10E{100 + i:03d}.tif" in tile_files(w) for i in range(6))
    assert set(tiles) <= set(registry(w))


def test_tiles_received_before_a_dem_failure_are_registered_as_the_tasks(w):
    """TZ2c-m2 : `dem-tiles` en panne à mi-parcours."""
    w.bootstrap()
    w.host.fail.add('dem-partial')
    scripted_claim(w)
    mark = w.mark()
    assert w.run(DEM_URL=DEM) == 0
    assert failed_codes(w, mark) == [('7', 'dem')]
    files = tile_files(w)
    assert len(files) == 1 and len(registry(w)) == 1


def seed_extract(w_, rid, date, size, fetched_at):
    (w_.host.state / 'extracts' / f"{rid}.osm.pbf").write_bytes(b'x' * size)
    w_.host.write_json(f"extracts/{rid}.json", {'id': rid, 'date': date, 'md5': '0' * 32, 'size': size, 'fetched_at': fetched_at})


def cache_ids(w_):
    return sorted(p.name[:-5] for p in (w_.host.state / 'extracts').glob('*.json'))


def test_the_cache_purge_removes_the_old_extracts_outside_the_selection_after_the_done_and_never_the_active_ones(w):
    w.bootstrap()
    old, recent = yymmdd(w.clock.now - timedelta(days=40)), yymmdd(w.clock.now - timedelta(days=5))
    seed_extract(w, 'oldie', old, 10, '2026-10-01T00:00:00Z')
    seed_extract(w, 'recent', recent, 10, '2026-11-20T00:00:00Z')
    w.host.app_script['done'] = [2]
    assert w.ask(NEW_ZONE) == 0
    assert 'oldie' in cache_ids(w)                                                                # pas avant le `done`
    w.advance(60)
    assert w.run() == 0
    assert cache_ids(w) == sorted(['recent'] + w.regions()) and not (w.host.state / 'extracts' / 'oldie.osm.pbf').exists()
    assert done_payload(w)['purged_extracts'] == 1


@pytest.mark.parametrize('cap, left', [(8000, ['b']), (0, [])])
def test_the_cache_above_its_cap_drops_the_oldest_first_and_stops_under_it_the_selection_is_never_touched(w, cap, left):
    w.bootstrap()
    day = yymmdd(w.clock.now - timedelta(days=3))
    seed_extract(w, 'a', day, 3000, '2026-11-01T00:00:00Z')
    seed_extract(w, 'b', day, 3000, '2026-11-10T00:00:00Z')
    assert w.ask(NEW_ZONE, ROUTING_CACHE_MAX_BYTES=cap) == 0                                       # sélection : 2 × 2000 octets
    assert cache_ids(w) == sorted(left + w.regions())


def purge(zu, **kw):
    cfg = zu.Config()
    return zu.purge_plan(cfg, kw.get('regions', []), kw.get('extracts', {}), kw.get('owned', {}), kw.get('bbox', [3.0, 50.0, 3.5, 50.5]),
                         kw.get('now', zu.dt.datetime(2026, 11, 26)))


def test_expired_extracts_go_first_so_an_older_fresh_one_is_kept_when_that_is_enough(zu, monkeypatch):
    """ZD-11 : d'abord les extraits de plus de 35 jours, PUIS les plus anciens tant que le total dépasse le plafond."""
    monkeypatch.setenv('ROUTING_CACHE_MAX_BYTES', '6000')
    extracts = {'fresh-old-download': {'id': 'fresh-old-download', 'date': '261120', 'size': 5000, 'fetched_at': '2026-10-01T00:00:00Z'},
                'expired-new-download': {'id': 'expired-new-download', 'date': '261001', 'size': 5000, 'fetched_at': '2026-11-01T00:00:00Z'}}
    assert purge(zu, extracts=extracts)[1] == ['expired-new-download']


def test_the_tiles_purge_takes_task_tiles_outside_the_zone_oldest_first_never_the_zone_nor_unregistered_ones(zu, monkeypatch):
    monkeypatch.setenv('DEM_MAX_TILES', '60')
    monkeypatch.setenv('DEM_URL', DEM)
    bbox = [3.0, 50.0, 4.0, 51.0]                                         # une dalle : (50, 3)
    owned = {(10, i): f"2026-10-{1 + i % 28:02d}T00:00:00Z" for i in range(69)}
    owned[(50, 3)] = '2020-01-01T00:00:00Z'                                # la plus ancienne, mais dans la zone
    tiles, _ = purge(zu, owned=owned, bbox=bbox)
    assert len(tiles) == 10 and (50, 3) not in tiles and tiles == sorted(tiles, key=lambda t: (owned[t], t))
    assert purge(zu, owned={t: d for t, d in list(owned.items())[:60]}, bbox=bbox)[0] == []                # sous le plafond : rien


@pytest.mark.parametrize('dem_url', ['', 'https://api.opentopodata.org/v1/copernicus30', 'http://opentopodata.example:5000/x'])
def test_without_the_local_dem_service_the_tiles_purge_plans_nothing_whatever_the_registry(zu, monkeypatch, dem_url):
    monkeypatch.setenv('DEM_MAX_TILES', '1')
    if dem_url:
        monkeypatch.setenv('DEM_URL', dem_url)
    owned = {(10, i): f"2026-10-{1 + i:02d}T00:00:00Z" for i in range(30)}
    assert purge(zu, owned=owned, bbox=[3.0, 50.0, 4.0, 51.0])[0] == []


# ===================================================== I. verrou, configuration, journaux =====================================

def project_with_fake_docker(tmp_path):
    project = tmp_path / 'proj'
    (project / 'routing').mkdir(parents=True)
    (project / 'docker-compose.yml').write_text('')
    for source in (UPDATE_SH, SCRIPT):
        (project / 'routing' / source.name).write_text(source.read_text())
        (project / 'routing' / source.name).chmod(0o755)
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    log = tmp_path / 'docker.log'
    (bin_dir / 'docker').write_text(f'#!/bin/sh\necho "$@" >> {log}\nexit 1\n')
    (bin_dir / 'docker').chmod(0o755)
    return project, bin_dir, log


def test_update_sh_exits_0_without_any_docker_call_while_the_lock_is_held_and_runs_the_task_once_it_is_free(tmp_path):
    import os
    from tests.routing_host_support import REAL_RUN
    project, bin_dir, log = project_with_fake_docker(tmp_path)
    lock = tmp_path / 'lock'
    env = {'PATH': f"{bin_dir}:{os.environ['PATH']}", 'HOME': str(tmp_path), 'ROUTING_ZONE_BUILD': '1', 'ROUTING_LOCK': str(lock)}
    with open(lock, 'w') as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        done = REAL_RUN(['bash', 'routing/update.sh'], cwd=project, env=env, capture_output=True, timeout=60)
        assert done.returncode == 0 and not log.exists()
    done = REAL_RUN(['bash', 'routing/update.sh'], cwd=project, env=env, capture_output=True, timeout=60)
    assert done.returncode == 1 and 'image inspect' in log.read_text()                    # tâche lancée (Docker simulé en échec)


def test_update_sh_launched_by_the_timer_with_the_mode_off_does_nothing(tmp_path):
    import os
    from tests.routing_host_support import REAL_RUN
    project, bin_dir, log = project_with_fake_docker(tmp_path)
    env = {'PATH': f"{bin_dir}:{os.environ['PATH']}", 'HOME': str(tmp_path), 'ROUTING_ZONE_BUILD': '0', 'ROUTING_FROM_TIMER': '1',
           'ROUTING_LOCK': str(tmp_path / 'lock')}
    done = REAL_RUN(['bash', 'routing/update.sh'], cwd=project, env=env, capture_output=True, timeout=60)
    assert done.returncode == 0 and not log.exists()


@pytest.mark.parametrize('name, value', [('ROUTING_MAX_REGIONS', '0'), ('ROUTING_MAX_REGIONS', 'abc'), ('ROUTING_MAX_REGIONS', '51'),
                                         ('ROUTING_ALLOWED_HOSTS', 'bad host!;x'), ('ROUTING_ALLOWED_HOSTS', '-bad'), ('ROUTING_KEEP_GRAPHS', '1'),
                                         ('ROUTING_CATALOG_URL', 'https://evil.example.com/index-v1.json'),
                                         ('ROUTING_CATALOG_URL', 'http://download.geofabrik.de/index-v1.json'),
                                         ('ROUTING_CATALOG_ROOT', '../x'), ('ROUTING_CATALOG_EXCLUDE', 'a;b'), ('DEM_MAX_TILES', '1001'),
                                         ('ROUTING_HEALTH_TIMEOUT', '4'), ('ROUTING_MAX_BUILDS_PER_DAY', '-1')])
def test_an_invalid_configuration_exits_2_before_any_docker_call(w, name, value, capsys):
    w.env(**{name: value})
    assert w.zu.main() == 2 and w.host.log == [] and w.host.unlisted == []
    assert 'configuration invalide' in capsys.readouterr().err


def test_the_dotenv_file_is_read_never_executed_and_the_environment_wins(w, tmp_path):
    marker = tmp_path / 'executed'
    (Path.cwd() / '.env').write_text(f"ROUTING_MAX_REGIONS=3\nROUTING_KEEP_GRAPHS='4'\n$(touch {marker})\n`touch {marker}`\nROUTING_MAX_REGIONS=7\n")
    config = w.zu.Config()
    assert (config.max_regions, config.keep_graphs) == (7, 4) and not marker.exists()
    w.env(ROUTING_MAX_REGIONS=9)
    assert w.zu.Config().max_regions == 9


def test_the_logs_carry_no_coordinate_and_no_command_goes_through_a_shell(w, capsys):
    w.bootstrap()
    assert w.ask(NEW_ZONE, DEM_URL=DEM) == 0
    bbox = json.loads(w.state()['active_extract_bbox'])
    seen = capsys.readouterr()
    for text in (seen.out, seen.err):
        for number in ('3.123457', '50.412346', '3.412345', '50.812347', *(f"{n:.4f}" for n in bbox)):
            assert number not in text
    assert w.host.shell_used == [] and w.host.unlisted == []


# ===================================================== J. fonctions pures : catalogue de l'hôte, http =======================

def index(*features):
    return {'features': [{'properties': dict(p), 'geometry': None} for p in features]}


def region(rid, parent, url=True, shp=True):
    props = {'id': rid, 'parent': parent}
    if url:
        props['urls'] = {'pbf': f"https://download.geofabrik.de/europe/{rid}-latest.osm.pbf", **({'shp': 'https://x/y.shp.zip'} if shp else {})}
    return props


HOSTS = ['download.geofabrik.de']


def test_the_host_keeps_the_same_leaves_as_the_application_on_the_frozen_index(zu):
    from tests.routing_zone_support import frozen_document
    out, _, _ = zu.candidates_of(frozen_document(), 'europe', {'alps', 'britain-and-ireland', 'dach', 'great-britain', 'guadeloupe', 'martinique',
                                                              'mayotte', 'reunion'}, HOSTS)
    assert len(out) == 70 and 'great-britain' not in out and 'belgium' in out


@pytest.mark.parametrize('document', [
    index(region('europe', None, url=False), region('a', 'europe'), region('b', 'a'), region('a2', 'b')),                 # contrôle négatif plus bas
])
def test_a_valid_small_index_gives_its_leaves(zu, document):
    document['features'][3]['properties']['id'] = 'c'
    out, _, _ = zu.candidates_of(document, 'europe', set(), HOSTS)
    assert sorted(out) == ['c']


@pytest.mark.parametrize('document, why', [
    (index(region('europe', None, url=False), region('a', 'b'), region('b', 'a')), 'cycle hors racine inoffensif'),
    (index(region('europe', None, url=False), region('a', 'europe'), region('a', 'europe')), 'identifiant double'),
    (index(region('europe', None, url=False), region('A', 'europe')), 'identifiant invalide'),
    (index(region('europe', None, url=False), region('../x', 'europe')), 'identifiant avec chemin'),
    (index(region('europe', None, url=False)), 'aucune candidate'),
    (index(region('a', 'europe')), 'racine absente'),
    (index(region('europe', None, url=False), region('a', 'europe', url=False)), 'candidate sans URL'),
    ({'features': []}, 'vide'), ({'features': 'x'}, 'forme'), ([], 'liste'), ({'features': [1]}, 'élément'),
])
def test_an_invalid_index_is_refused_by_the_host(zu, document, why):
    if why == 'cycle hors racine inoffensif':
        document['features'].append({'properties': region('c', 'europe'), 'geometry': None})
        assert sorted(zu.candidates_of(document, 'europe', set(), HOSTS)[0]) == ['c']                       # le cycle n'est jamais parcouru
        return
    with pytest.raises(ValueError):
        zu.candidates_of(document, 'europe', set(), HOSTS)


@pytest.mark.parametrize('url', ['http://download.geofabrik.de/europe/a-latest.osm.pbf', 'https://evil.example.com/a-latest.osm.pbf',
                                 'https://download.geofabrik.de/../a-latest.osm.pbf', 'https://download.geofabrik.de/a.zip',
                                 'https://download.geofabrik.de/a-latest.osm.pbf?x=1', 'https://user@download.geofabrik.de/a-latest.osm.pbf',
                                 'https://download.geofabrik.de/' + 'a' * 300 + '.osm.pbf'])
def test_a_candidate_url_outside_the_rules_refuses_the_whole_index_so_nothing_reaches_the_application(zu, url):
    document = index(region('europe', None, url=False), region('a', 'europe'))
    document['features'][1]['properties']['urls']['pbf'] = url
    with pytest.raises(ValueError):
        zu.candidates_of(document, 'europe', set(), HOSTS)


def test_a_cycle_under_the_root_refuses_the_index(zu):
    document = index(region('europe', None, url=False), region('a', 'europe', url=False), region('b', 'a', url=False))
    document['features'][1]['properties']['parent'] = 'b'
    document['features'].append({'properties': region('c', 'europe'), 'geometry': None})
    document['features'][2]['properties']['parent'] = 'a'
    out, _, _ = zu.candidates_of(document, 'europe', set(), HOSTS)                       # a et b ne sont pas sous la racine : c seul
    assert sorted(out) == ['c']
    cyclic = index(region('europe', None, url=False), region('a', 'europe', url=False), region('b', 'a', url=False))
    cyclic['features'][0]['properties']['parent'] = 'b'                                    # la racine dans son propre sous-arbre
    with pytest.raises(ValueError):
        zu.candidates_of(cyclic, 'europe', set(), HOSTS)


def test_the_http_helper_checks_the_first_hop_before_any_request_and_returns_the_final_url_and_length(w, zu):
    day = yymmdd(w.clock.now)
    base = 'https://download.geofabrik.de/europe/'
    w.web.override[base + 'x-latest.osm.pbf'] = ('302', f"{base}x-{day}.osm.pbf")
    w.web.override[f"{base}x-{day}.osm.pbf"] = ('200', 1234)
    assert zu.http(base + 'x-latest.osm.pbf', HOSTS, method='HEAD')[:2] == (f"{base}x-{day}.osm.pbf", 1234)
    with pytest.raises(ValueError):
        zu.http('https://evil.example.com/x.json', HOSTS, suffix=None)
    assert not [u for _, u in w.web.calls if 'evil' in u]


# ===================================================== K. mode historique (`ROUTING_ZONE_BUILD=0`) contre les montages réels ===

COMPOSE = UPDATE_SH.parent.parent / 'docker-compose.yml'


@pytest.mark.parametrize('mode', [None, '0'], ids=['unset', 'zero'])
def test_the_historic_update_runs_end_to_end_against_the_real_compose_mounts(tmp_path, mode):
    """
    `update.sh` avec `ROUTING_ZONE_BUILD=0` (la production actuelle) : chaque conteneur n'écrit que dans un volume qu'il monte.
    Le compose simulé lit les VRAIS montages de `docker-compose.yml` : un chemin non monté est un dossier vide en lecture seule.
    """
    import os
    from tests.routing_host_support import FAKE_COMPOSE_CURL, FAKE_COMPOSE_DOCKER, REAL_RUN
    project = tmp_path / 'proj'
    (project / 'routing').mkdir(parents=True)
    (project / 'docker-compose.yml').write_text(COMPOSE.read_text())
    (project / 'routing' / 'update.sh').write_text(UPDATE_SH.read_text())
    root, bin_dir = tmp_path / 'fake', tmp_path / 'bin'
    for folder in (root / 'data', root / 'state', root / 'unmounted', bin_dir):
        folder.mkdir(parents=True)
    (root / 'state').chmod(0o555)                                  # volume neuf : propriétaire root tant que `routing-state-init` n'a pas tourné
    (root / 'unmounted').chmod(0o555)
    for name, text in (('docker', FAKE_COMPOSE_DOCKER), ('curl', FAKE_COMPOSE_CURL), ('sleep', '#!/bin/sh\nexit 0\n')):
        (bin_dir / name).write_text(text)
        (bin_dir / name).chmod(0o755)
    env = {'PATH': f"{bin_dir}:{os.environ['PATH']}", 'HOME': str(tmp_path), 'FAKE_ROOT': str(root), 'ROUTING_LOCK': str(tmp_path / 'lock'),
           'ROUTING_HEALTH_TIMEOUT': '5'}
    if mode is not None:
        env['ROUTING_ZONE_BUILD'] = mode
    try:
        done = REAL_RUN(['bash', 'routing/update.sh'], cwd=project, env=env, capture_output=True, timeout=120, text=True)
        log = (root / 'docker.log').read_text() if (root / 'docker.log').exists() else ''
        assert done.returncode == 0, (done.returncode, done.stdout[-1500:], done.stderr[-1500:], log[-1500:])
        graphs = sorted(p.name for p in (root / 'data' / 'graphs').glob('graph-*'))
        assert len(graphs) == 1 and (root / 'data' / 'graphs' / graphs[0] / 'properties').exists()
        assert os.readlink(root / 'data' / 'graphs' / 'current') == graphs[0]                # bascule faite
        assert sorted(p for p in (root / 'unmounted').rglob('*')) == []                       # rien n'a été écrit hors d'un volume monté
        assert list(root.rglob('hdf-be-*.osm.pbf'))                                           # le fusionné existe quelque part de monté
        services = set(re.findall(r'run --rm -T (?:(?:-e \S+|--name \S+) )*(routing-[a-z-]+)', log))
        assert {'routing-fetch-legacy', 'routing-merge-legacy', 'routing-init', 'routing-import', 'routing-volume'} <= services, services
        assert services.isdisjoint({'routing-state-init', 'routing-fetch', 'routing-merge'}), services       # CA-ZD1 : =0 ne touche pas à l'état
        assert not (root / 'state' / '.inited').exists() and list((root / 'state').rglob('*')) == []        # ni fichier, ni volume d'état initialisé
    finally:
        (root / 'state').chmod(0o755)
        (root / 'unmounted').chmod(0o755)


def compose_block(service):
    match = re.search(r'^  ' + re.escape(service) + r':\n(.*?)(?=^  \S|^\S|\Z)', COMPOSE.read_text(), re.M | re.S)
    assert match, service
    block = re.search(r'^    volumes:\n((?:      - .*\n)+)', match.group(1), re.M)
    return [line.strip()[2:] for line in (block.group(1).splitlines() if block else [])]


def test_the_compose_mounts_keep_the_host_volume_away_from_the_services_and_the_historic_services_on_the_graph_volume(w):
    """CA-ZD9 / ZD-9 : l'état de l'hôte n'est monté ni par le moteur, ni par l'application, ni par `opentopodata` ; import en lecture seule ;
    les services du mode historique montent `routing-data:/data` (leur mode `=0` ne dépend pas du volume d'état)."""
    for service in ('graphhopper', 'kml-viewer', 'opentopodata'):
        assert not [m for m in compose_block(service) if 'routing-state' in m], service
    assert 'routing-state:/state:ro' in compose_block('routing-import') and 'routing-data:/data' in compose_block('routing-import')
    for service in ('routing-fetch-legacy', 'routing-merge-legacy'):
        assert 'routing-data:/data' in compose_block(service) and not [m for m in compose_block(service) if 'routing-state' in m], service
    for service in ('routing-fetch', 'routing-merge'):
        assert compose_block(service) == ['routing-state:/state'], service
    assert compose_block('routing-state-init') == ['routing-state:/state']


# ===================================================== L. volume d'état et capacités (TZ2c-B1), plafond des dalles (TZ2c-m5) ====

def test_the_state_volume_is_initialised_with_the_compose_capabilities_at_every_pass(w):
    """TZ2c-B1 : avec `cap_drop: ALL` + `cap_add: CHOWN`, `chmod` après `chown` échoue (root n'est plus propriétaire, pas de FOWNER)."""
    assert w.host.state_meta == {'uid': 0, 'mode': 0o755}
    assert w.run() == 0 and w.host.state_meta == {'uid': 10002, 'mode': 0o700}
    assert [e[1] for e in w.host.apps()][:1] == ['report']                                      # le passage n'est pas bloqué avant `report`
    assert w.run() == 0 and w.host.state_meta == {'uid': 10002, 'mode': 0o700}                    # idempotent : chaque minute
    assert len(w.host.apps('report')) == 2


def test_the_fake_compose_does_enforce_the_capabilities_so_the_previous_entrypoint_is_caught(w):
    """Contrôle du banc : l'ancien entrypoint (`chown` puis `chmod`) échoue ici comme sous Docker, et le passage se termine en échec."""
    block = re.search(r'^  routing-state-init:\n(.*?)(?=^  \S|^\S|\Z)', w.host.compose_text, re.M | re.S).group(1)
    current = re.search(r'entrypoint: (\[.*\])', block).group(1)
    w.host.compose_text = w.host.compose_text.replace(current, '["sh", "-c", "chown 10002 /state && chmod 700 /state"]')
    assert w.run() == 1 and w.host.apps() == [] and w.host.state_meta['mode'] == 0o755
    w.host.compose_text = w.host.compose_text.replace('cap_add:\n      - CHOWN', 'cap_add:\n      - CHOWN\n      - FOWNER', 1)
    assert w.run() == 0                                                                              # avec FOWNER, l'ancienne forme passe


@pytest.mark.parametrize('dem_url', ['', 'https://api.opentopodata.org/v1/copernicus30', 'http://opentopodata.example:5000/v1/copernicus30'])
def test_without_the_local_dem_service_the_host_takes_a_zone_of_more_than_the_tiles_cap(w, dem_url):
    """TZ2c-m5 : DEM_URL vide (ou hors `opentopodata`) et zone de 61 dalles ou plus ⇒ prise acceptée ; l'application ne plafonne pas non plus."""
    wide = [0.0, 45.0, 8.0, 53.0]
    assert len(w.zu.tiles_for(wide)) >= 61
    w.bootstrap()
    scripted_claim(w, extract_bbox=wide)
    mark = w.mark()
    assert w.run(DEM_URL=dem_url) == 0 if dem_url else w.run() == 0
    assert failed_codes(w, mark) == [] and [e[1] for e in w.host.events('fetch')] == ['belgium', 'nord-pas-de-calais']
    assert w.host.events('dem') == []


def test_with_the_local_dem_service_the_same_zone_is_refused_by_the_host_with_cap(w):
    wide = [0.0, 45.0, 8.0, 53.0]
    w.bootstrap()
    scripted_claim(w, extract_bbox=wide)
    mark = w.mark()
    assert w.run(DEM_URL=DEM) == 0
    assert failed_codes(w, mark) == [('7', 'cap')] and w.host.builds() == []


# ===================================================== M. index réel : entrées hors du sous-arbre (E1) ========================

def index_bytes(mutate=None):
    from tests.routing_zone_support import frozen_document
    document = frozen_document()
    if mutate:
        mutate(document['features'])
    return json.dumps(document).encode()


def test_the_host_accepts_the_index_with_the_ids_outside_europe_and_ignores_them(zu):
    from tests.routing_zone_support import frozen_document, outside_europe
    document = frozen_document()
    outside = outside_europe(document)
    assert len([r for r in outside if r.startswith('us/')]) == 51 and 'africa' in outside
    out, nodes, subtree = zu.candidates_of(document, 'europe', {'alps', 'britain-and-ireland', 'dach', 'great-britain', 'guadeloupe',
                                                                'martinique', 'mayotte', 'reunion'}, HOSTS)
    assert len(out) == 70 and not set(out) & outside and not subtree & outside


def test_an_entry_outside_the_subtree_is_ignored_by_the_host_whatever_is_wrong_with_it(zu):
    from tests.routing_zone_support import frozen_document, outside_europe
    document = frozen_document()
    outside = outside_europe(document)
    for feature in document['features']:
        if feature['properties']['id'] in outside:
            feature['properties'].update(urls={'pbf': 'http://evil.example/x'}, size='x')
    document['features'].append({'properties': {'id': 'us/alabama', 'parent': 'us'}, 'geometry': None})
    document['features'].append({'properties': {'id': 'Bad ID/%', 'parent': 'nowhere'}, 'geometry': None})
    out, _, _ = zu.candidates_of(document, 'europe', set(), HOSTS)
    assert not set(out) & outside and 'belgium' in out


@pytest.mark.parametrize('mutate', [
    lambda f: f.append({'properties': {'id': 'france/us', 'parent': 'france', 'urls': {'pbf': 'https://download.geofabrik.de/europe/a-latest.osm.pbf'}},
                        'geometry': None}),
    lambda f: f.append({'properties': {'id': 'Upper', 'parent': 'france', 'urls': {'pbf': 'https://download.geofabrik.de/europe/a-latest.osm.pbf'}},
                        'geometry': None}),
    lambda f: next(x for x in f if x['properties']['id'] == 'lorraine')['properties'].update(urls={'pbf': 'http://download.geofabrik.de/x-latest.osm.pbf'}),
    lambda f: f.append(next(x for x in f if x['properties']['id'] == 'lorraine')),
], ids=['slash-id-inside', 'uppercase-inside', 'http-url-of-a-candidate', 'duplicate-inside'])
def test_an_invalid_entry_inside_the_subtree_is_still_refused_by_the_host(zu, mutate):
    from tests.routing_zone_support import frozen_document
    document = frozen_document()
    mutate(document['features'])
    with pytest.raises(ValueError):
        zu.candidates_of(document, 'europe', set(), HOSTS)


def test_the_catalog_of_the_real_shaped_index_goes_through_end_to_end_without_the_outside_entries_reaching_the_application(w):
    w.bootstrap()
    sent = json.loads([data for sub, data in w.host.stdin if sub == 'catalog'][0])
    ids = [f['properties']['id'] for f in sent['features']]
    assert len(w.local_catalog()) == 70 and w.codes('catalog') == [0]
    assert not [i for i in ids if i.startswith('us') or i in ('africa', 'asia', 'russia', 'north-america')] and 'europe' in ids


def test_a_refused_index_is_logged_never_a_silent_success_nothing_is_sent_and_it_is_retried_only_after_an_hour(w, capsys):
    """E1 : une entrée invalide DANS le sous-arbre ⇒ journal « catalogue non rafraîchi », aucun `catalog -`, aucune copie locale."""
    w.web.index_override = index_bytes(lambda f: f.append({'properties': {'id': 'france/us', 'parent': 'france',
                                                                           'urls': {'pbf': 'https://download.geofabrik.de/europe/a-latest.osm.pbf'}},
                                                            'geometry': None}))
    assert w.run() == 0
    out = capsys.readouterr().out
    assert 'catalogue non rafraîchi' in out and 'ValueError' in out and 'catalogue chargé' not in out
    assert w.host.apps('catalog') == [] and w.host.read_json('catalog.json') is None and w.host.read_json('catalog-meta.json')['attempt_at']
    w.advance(600)
    assert w.run() == 0 and w.host.apps('catalog') == []
    assert 'catalogue non rafraîchi' not in capsys.readouterr().out                    # tentative espacée d'une heure : pas de bruit chaque minute
    w.advance(3600)
    w.web.index_override = None                                                         # l'index est corrigé : accepté à la tentative suivante
    assert w.run() == 0 and w.codes('catalog') == [0] and len(w.local_catalog()) == 70


def test_an_index_refused_by_the_application_is_logged_and_the_previous_local_copy_is_kept(w, capsys):
    w.bootstrap()
    before = w.host.read_json('catalog.json')
    capsys.readouterr()
    w.advance(days=8)
    w.host.app_script['catalog'] = [2]
    assert w.run() == 0
    assert 'catalogue non rafraîchi' in capsys.readouterr().out and w.host.read_json('catalog.json') == before


@pytest.mark.parametrize('parent', ['A/B', 'unknown-parent', None, 'us/alabama', ''])
def test_an_entry_whose_parent_is_not_in_the_subtree_is_ignored_by_the_host_too(zu, parent):
    from tests.routing_zone_support import frozen_document
    document = frozen_document()
    document['features'].append({'properties': {'id': 'orphan-region', 'parent': parent,
                                                'urls': {'pbf': 'https://download.geofabrik.de/europe/orphan-region-latest.osm.pbf'}}, 'geometry': None})
    assert 'orphan-region' not in zu.candidates_of(document, 'europe', set(), HOSTS)[0]


@pytest.mark.parametrize('where', ['first', 'last'])
@pytest.mark.parametrize('parent', ['nowhere', 'us', None, 'A/B'])
@pytest.mark.parametrize('real_id', ['france', 'belgium', 'lorraine'])
def test_a_duplicate_of_an_entry_of_the_subtree_is_refused_by_the_host_wherever_it_is_placed_E1_M1(zu, real_id, parent, where):
    import copy
    from tests.routing_zone_support import frozen_document
    document = frozen_document()
    twin = copy.deepcopy(next(x for x in document['features'] if x['properties']['id'] == real_id))
    twin['properties']['parent'] = parent
    document['features'].insert(0 if where == 'first' else len(document['features']), twin)
    with pytest.raises(ValueError):
        zu.candidates_of(document, 'europe', set(), HOSTS)


def test_a_hijacked_index_is_never_loaded_the_previous_copy_stays_and_the_refusal_is_logged_E1_M1(w, capsys):
    import copy
    w.bootstrap()
    before = w.host.read_json('catalog.json')
    capsys.readouterr()

    def hijack(features):
        twin = copy.deepcopy(next(x for x in features if x['properties']['id'] == 'france'))
        twin['properties']['parent'] = 'nowhere'
        features.insert(0, twin)

    w.web.index_override = index_bytes(hijack)
    w.advance(days=8)
    assert w.run() == 0
    assert 'catalogue non rafraîchi' in capsys.readouterr().out and w.host.read_json('catalog.json') == before and len(before['regions']) == 70
    assert w.codes('catalog') == [0]                                                             # un seul `catalog -`, celui du premier passage
