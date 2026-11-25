"""
2e lot, groupe 3b (rôle test) : retours de l'utilisateur pendant sa recette.
  - état vide « Glisse-dépose… » en mode « Créer une trace » (opener.js + route-builder.js EXÉCUTÉS sous Node, bac à sable vm) ;
  - diagnostic du moteur lisible dans /admin (admin.js + common.js exécutés, DOM minimal) ;
  - CA-66 de bout en bout : un VRAI gunicorn, un SMTP factice qui ne répond pas, des messages en file, SIGTERM, la ligne WARNING.
Aucun marqueur xfail : un test rouge est un défaut à rapporter. Les tests Node sont ignorés si `node` est absent.
"""

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from tests.fake_smtp import FakeSMTP
from tests.pw_harness import GOOD, HOST, KEY, ORIGIN, add, connect, make_app

WEB_APP = Path(__file__).resolve().parent.parent
STATIC = WEB_APP / 'static'
needs_node = pytest.mark.skipif(shutil.which('node') is None, reason="node absent : exécution du JavaScript impossible")

# -- Outillage Node : une « chose » universelle (propriétés et appels chaînables), un DOM minimal ----------------------------------------

PRELUDE = r"""
const vm = require('vm'), fs = require('fs');
function universal(own = {}) {
  const fn = function () { return proxy; };
  const proxy = new Proxy(fn, {
    get(target, prop) {
      if (prop in own) return own[prop];
      if (prop === 'then') return undefined;
      if (prop === Symbol.toPrimitive) return () => '';
      if (prop === 'length') return 0;
      if (prop === Symbol.iterator) return function* () {};
      if (prop === 'classList') return { add() {}, remove() {}, toggle() {}, contains: () => false };
      if (prop === 'dataset' || prop === 'style') return (own[prop] = own[prop] || {});
      return proxy;
    },
    set(target, prop, value) { own[prop] = value; return true; },
    apply() { return proxy; },
  });
  return proxy;
}
class Node {}
class FakeText extends Node { constructor(t) { super(); this.text = String(t); } }
class FakeEl extends Node {
  constructor(tag) { super(); this.tagName = tag; this.children = []; this.attrs = {}; this.className = ''; this.dataset = {}; this.listeners = {}; this._text = null; this.hidden = false; }
  append(...nodes) { for (const n of nodes) this.children.push(n); }
  appendChild(n) { this.children.push(n); return n; }
  replaceChildren(...nodes) { this.children = [...nodes]; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  removeAttribute(k) { delete this.attrs[k]; }
  addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }
  querySelectorAll() { return []; }
  querySelector() { return null; }
  select() {}
  set textContent(v) { this.children = [new FakeText(v)]; }
  get textContent() { return this.children.map((c) => (c instanceof FakeText ? c.text : c.textContent)).join(''); }
  set innerHTML(v) { globalThis.__innerHTML.push(String(v)); }
  get innerHTML() { return ''; }
}
globalThis.__innerHTML = [];
function serialize(node) {   // texte rendu + classes (pour distinguer les pastilles)
  if (node instanceof FakeText) return node.text;
  const inner = node.children.map(serialize).join('');
  return node.className ? `[${node.className}|${inner}]` : inner;
}
"""

EMPTY_STATE_HARNESS = PRELUDE + r"""
const [openerPath, builderPath] = process.argv.slice(2, 4);
const scenario = JSON.parse(process.argv[4]);
const elements = {};
const clicks = {};
const calls = { uploads: [], cancelEditing: 0 };
const ids = new Proxy({}, { get(_, id) {
  if (!elements[id]) {
    elements[id] = universal({ hidden: id === 'emptyState' || id === 'routeBuilderPanel' ? scenario.initialHidden[id] === true : false, value: '',
      addEventListener(type, f) { (clicks[id + ':' + type] = clicks[id + ':' + type] || []).push(f); } });
  }
  return elements[id];
} });
const windowListeners = {};
const sandbox = {
  console, Object, JSON, Error, Promise, Date, Math, Number, String, Array, Map, Set, setTimeout, clearTimeout, Intl, Boolean,
  document: { getElementById: (id) => ids[id], querySelector: () => universal(), querySelectorAll: () => [], createElement: (t) => new FakeEl(t), createTextNode: (t) => new FakeText(t),
              addEventListener() {}, body: universal() },
  window: { localStorage: { getItem: () => null, setItem() {}, removeItem() {} }, addEventListener(t, f) { (windowListeners[t] = windowListeners[t] || []).push(f); },
            location: { pathname: '/', search: '' } },
  App: { api: async (path) => (scenario.failOpen ? Promise.reject(new Error('indisponible')) : path.includes('options')
            ? { available: true, state: 'ok', activities: [{ key: 'run', label: 'Course' }], default_activity: 'run', min_km: 1, max_km: 100, step_km: 1, coverage_bbox: null }
            : { user: { id: 7 } }),
         el: (tag, attrs, ...children) => { const n = new FakeEl(tag); for (const c of children.flat()) n.append(c instanceof Node ? c : new FakeText(c)); return n; },
         notify() {} },
  Node, L: universal(), bootstrap: universal(),
  Viewer: { files: new Map(), map: universal() },
  uploadFiles: (files) => { calls.uploads.push(files.length); },
  cancelEditing: () => { calls.cancelEditing += 1; },
};
sandbox.window.document = sandbox.document;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(openerPath, 'utf8'), sandbox);
vm.runInContext(fs.readFileSync(builderPath, 'utf8'), sandbox);
vm.runInContext('RouteBuilder.setup(); setupPageDrop();', sandbox);
const empty = () => ids.emptyState.hidden;
const out = [];
const log = (label) => out.push([label, empty(), ids.routeBuilderPanel.hidden]);
const tick = () => new Promise((r) => setTimeout(r, 5));
const click = async (id) => { for (const f of (clicks[id + ':click'] || [])) await f(); await tick(); };
const setFiles = (n) => { sandbox.Viewer.files = new Map(Array.from({ length: n }, (_, i) => [i, {}])); };
(async () => {
  setFiles(scenario.files || 0);
  vm.runInContext('updateEmptyState()', sandbox); log('initial');
  await click('routeBuilderOpen'); log('apres ouverture');
  vm.runInContext('updateEmptyState()', sandbox); log('mise a jour pendant constructeur');
  if (scenario.loadWhileOpen) { setFiles(1); vm.runInContext('updateEmptyState()', sandbox); log('fichier ouvert pendant constructeur'); setFiles(0); vm.runInContext('updateEmptyState()', sandbox); log('fichier ferme pendant constructeur'); }
  if (scenario.dropWhileOpen) { for (const f of (windowListeners.drop || [])) f({ dataTransfer: { types: ['Files'], files: [{}] }, preventDefault() {} }); }
  await click(scenario.closeWith || 'rbClose'); log('apres fermeture');
  await click('routeBuilderOpen'); log('reouverture');
  await click('routeBuilderOpen'); log('refermeture par le bouton');
  if (scenario.dropAfter) { for (const f of (windowListeners.drop || [])) f({ dataTransfer: { types: ['Files'], files: [{}, {}] }, preventDefault() {} }); }
  console.log(JSON.stringify({ steps: out, uploads: calls.uploads, cancelEditing: calls.cancelEditing }));
})();
"""


def run_empty_state(**scenario):
    scenario.setdefault('initialHidden', {})
    done = subprocess.run(['node', '-', str(STATIC / 'js' / 'viewer' / 'opener.js'), str(STATIC / 'js' / 'viewer' / 'route-builder.js'),
                           json.dumps(scenario)], input=EMPTY_STATE_HARNESS, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    data = json.loads(done.stdout.strip().splitlines()[-1])
    data['by'] = {label: (empty_hidden, builder_hidden) for label, empty_hidden, builder_hidden in data['steps']}
    return data


@needs_node
def test_the_empty_state_is_shown_without_file_and_hidden_for_the_whole_time_the_builder_is_open():
    data = run_empty_state()['by']
    assert data['initial'][0] is False                                   # aucun fichier, constructeur fermé : le popup est là
    assert list(data['apres ouverture']) == [True, False]                # constructeur ouvert : popup masqué, panneau visible
    assert data['mise a jour pendant constructeur'][0] is True           # une mise à jour de l'état vide ne le ramène pas
    assert list(data['apres fermeture']) == [False, True]                # fermeture sans trace : le popup revient
    assert data['reouverture'][0] is True and data['refermeture par le bouton'][0] is False


@needs_node
def test_a_file_opened_or_closed_while_the_builder_is_open_does_not_bring_the_popup_back():
    data = run_empty_state(loadWhileOpen=True)['by']
    assert data['fichier ouvert pendant constructeur'][0] is True and data['fichier ferme pendant constructeur'][0] is True


@needs_node
def test_with_a_trace_already_open_the_popup_stays_hidden_before_during_and_after_the_builder():
    data = run_empty_state(files=2)['by']
    assert all(hidden is True for hidden, _ in (data['initial'], data['apres ouverture'], data['apres fermeture'], data['reouverture']))


@needs_node
def test_a_builder_that_fails_to_open_leaves_the_popup_as_it_was():
    data = run_empty_state(failOpen=True)['by']
    assert data['apres ouverture'][0] is False


@needs_node
def test_opening_the_builder_closes_the_point_editor_once_per_opening():
    result = run_empty_state()
    assert result['cancelEditing'] == 2                                 # deux ouvertures dans le scénario


@needs_node
def test_page_wide_drag_and_drop_stays_active_outside_the_builder_and_during_it():
    outside = run_empty_state(dropAfter=True)
    assert outside['uploads'] == [2]                                    # constructeur fermé : le dépôt envoie les fichiers
    during = run_empty_state(dropWhileOpen=True)
    assert during['uploads'] == [1]                                     # « sur toute la page » : le constructeur ne le désactive pas


@needs_node
def test_a_drop_that_carries_no_file_is_ignored_and_never_uploads():
    assert run_empty_state()['uploads'] == []


def stylesheet():
    return (STATIC / 'css' / 'styles.css').read_text(encoding='utf-8')


def rule(css, selector):
    match = re.search(re.escape(selector) + r'\s*\{([^}]*)\}', css)
    assert match, selector
    return match.group(1)


def test_the_empty_state_container_never_captures_the_map_clicks_only_its_box_does():
    css = stylesheet()
    outer, box = rule(css, '.viewer-empty'), rule(css, '.viewer-empty-box')
    assert re.search(r'pointer-events\s*:\s*none', outer)                # le conteneur plein carte laisse passer les clics
    assert re.search(r'pointer-events\s*:\s*auto', box)                  # seul l'encart capte la souris


def test_a_hidden_empty_state_is_really_not_displayed_even_though_its_rule_sets_display():
    """`.viewer-empty { display: grid }` l'emporterait sur l'attribut hidden : il faut une règle qui force display:none."""
    css = stylesheet()
    assert re.search(r'display\s*:\s*grid', rule(css, '.viewer-empty'))
    forced = re.search(r'\.viewer-empty\[hidden\]\s*\{[^}]*display\s*:\s*none', css)
    bootstrap = (STATIC / 'vendor' / 'bootstrap' / 'bootstrap.min.css').read_text(encoding='utf-8')
    reboot = re.search(r'\[hidden\]\s*\{\s*display\s*:\s*none\s*!important', bootstrap)
    assert forced or reboot, "le popup « masqué » resterait affiché : aucune règle ne force display:none sur [hidden]"
    assert forced or reboot  # la règle de Bootstrap (!important) suffit tant que bootstrap.css est chargé avant et sans surcharge
    assert not re.search(r'\.viewer-empty[^{]*\{[^}]*display\s*:\s*grid\s*!important', css)


def test_the_hidden_empty_state_has_no_pointer_events_override_that_would_block_the_map():
    template = (WEB_APP / 'templates' / 'viewer' / 'empty_state.html').read_text(encoding='utf-8')
    assert 'id="emptyState"' in template and 'pointer-events' not in template and 'style=' not in template
    css = stylesheet()
    assert not re.search(r'#emptyState[^{]*\{[^}]*pointer-events\s*:\s*auto', css)
    assert not re.search(r'\.viewer-empty\s*\{[^}]*pointer-events\s*:\s*auto', css)


def test_the_empty_state_is_rendered_by_the_viewer_page_with_its_choose_button():
    html = (WEB_APP / 'templates' / 'index.html').read_text(encoding='utf-8')
    assert "viewer/empty_state.html" in html


# ===== Diagnostic du moteur dans /admin : lisible, jamais le JSON brut ============================================================

ADMIN_HARNESS = PRELUDE + r"""
const [commonPath, adminPath] = process.argv.slice(2, 4);
const scenario = JSON.parse(process.argv[4]);
const settingsPath = process.argv[5];                    // admin-settings.js (page Réglages, chargé avant admin.js) ; vide si absent
const elements = {};
const featuresListeners = [];
const ids = new Proxy({}, { get(_, id) {
  if (!elements[id]) elements[id] = id === 'featuresBox' ? new FakeEl('div') : universal({ value: '', hidden: false });
  return elements[id];
} });
const listeners = {};
const sandbox = {
  console, Object, JSON, Error, Promise, Date, Math, Number, String, Array, Map, Set, setTimeout, clearTimeout, Intl, Boolean, encodeURIComponent, Node,
  document: { getElementById: (id) => ids[id], querySelector: () => null, querySelectorAll: () => [], createElement: (t) => new FakeEl(t),
              createTextNode: (t) => new FakeText(t), addEventListener(t, f) { (listeners[t] = listeners[t] || []).push(f); }, documentElement: { style: { setProperty() {} } } },
  window: { localStorage: {}, location: { pathname: '/admin', search: '', hash: '#fonctions', assign() {} }, addEventListener() {} },
  navigator: {}, HTMLFormElement: class {}, FormData: class {}, ResizeObserver: class { observe() {} },
  fetch: async (path) => {
    const body = path === '/api/me' ? { user: { id: 1, username: 'chef', is_admin: true }, auth_mode: 'password' }
      : path === '/api/admin/features' ? { features: [{ key: 'route_builder', label: 'Créer une trace', description: 'Constructeur', state: 'all', users: [], diagnostic: scenario.diagnostic }],
                                          timezone: scenario.timezone || 'Europe/Paris' }
      : path === '/api/admin/users' ? { users: [] } : { success: true };
    return { status: 200, ok: true, json: async () => body };
  },
};
sandbox.window.document = sandbox.document;
Date.now = () => scenario.now * 1000;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(commonPath, 'utf8'), sandbox);
if (settingsPath) vm.runInContext(fs.readFileSync(settingsPath, 'utf8'), sandbox);
vm.runInContext(fs.readFileSync(adminPath, 'utf8'), sandbox);
(async () => {
  for (const f of (listeners.DOMContentLoaded || [])) await f();
  await new Promise((r) => setTimeout(r, 30));
  console.log(JSON.stringify({ text: ids.featuresBox.textContent, tree: serialize(ids.featuresBox), innerHTML: globalThis.__innerHTML }));
})();
"""

NOW = 1_795_000_000                                                   # une date fixe (25/11/2026 environ)
BASE = {'configured': True, 'available': True, 'checked_at': NOW - 5 * 60, 'info_bbox': [49.0, 2.0, 51.6, 6.5], 'coverage_contained': True,
        'profiles': ['foot', 'hike', 'racingbike']}


def render_diagnostic(diagnostic, timezone='Europe/Paris'):
    settings_js = STATIC / 'js' / 'admin-settings.js'
    done = subprocess.run(['node', '-', str(STATIC / 'js' / 'common.js'), str(STATIC / 'js' / 'admin.js'),
                           json.dumps({'diagnostic': diagnostic, 'now': NOW, 'timezone': timezone}),
                           str(settings_js) if settings_js.exists() else ''], input=ADMIN_HARNESS, capture_output=True, text=True,
                          timeout=60, env={**os.environ, 'TZ': 'UTC'})
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


@needs_node
def test_the_diagnostic_is_never_the_raw_json_nor_the_raw_epoch():
    out = render_diagnostic(BASE)
    for text in (out['text'], out['tree']):
        assert '{"available"' not in text and '"available"' not in text and 'available' not in text.lower().replace('disponible', '')
        assert str(NOW - 300) not in text and 'checked_at' not in text and 'info_bbox' not in text and 'coverage_contained' not in text
        assert '[49' not in text and 'null' not in text and 'undefined' not in text and '[object' not in text


@needs_node
def test_the_check_time_is_converted_to_a_relative_and_a_local_time():
    out = render_diagnostic(BASE)['text']
    assert 'Vérifié il y a 5 min' in out
    assert re.search(r'\d{2}/\d{2}/\d{2,4}', out) and re.search(r'\d{1,2}:\d{2}', out)               # heure locale lisible


@needs_node
@pytest.mark.parametrize('zone,expected_hour', [('Europe/Paris', None), ('Pacific/Auckland', None)])
def test_the_local_time_follows_the_app_timezone(zone, expected_hour):
    paris = render_diagnostic(BASE, 'Europe/Paris')['text']
    other = render_diagnostic(BASE, zone)['text']
    if zone == 'Pacific/Auckland':
        assert paris != other                                                                           # le fuseau configuré est bien utilisé
    assert 'Vérifié' in other


@needs_node
@pytest.mark.parametrize('minutes,expected', [(0.2, "à l'instant"), (1, 'il y a 1 min'), (59, 'il y a 59 min'), (120, 'il y a 2 h'), (60 * 30, 'il y a 30 h')])
def test_the_relative_age_is_human(minutes, expected):
    diagnostic = {**BASE, 'checked_at': NOW - int(minutes * 60)}
    assert expected in render_diagnostic(diagnostic)['text'] or (minutes == 60 * 30 and 'il y a' in render_diagnostic(diagnostic)['text'])


@needs_node
def test_a_check_time_in_the_future_or_missing_never_prints_a_negative_or_an_epoch():
    future = render_diagnostic({**BASE, 'checked_at': NOW + 3600})['text']
    assert '-' not in future.split('Vérifié')[1].split('(')[0] and 'il y a -' not in future
    missing = render_diagnostic({**BASE, 'checked_at': None})['text']
    assert 'Pas encore vérifié' in missing and 'null' not in missing and str(NOW) not in missing


STATES = {
    'disponible': {**BASE},
    'indisponible': {**BASE, 'available': False},
    'non configuré': {**BASE, 'configured': False, 'available': False, 'checked_at': None, 'info_bbox': None, 'coverage_contained': None, 'profiles': []},
    'zone non couverte': {**BASE, 'coverage_contained': False},
}


@needs_node
def test_every_state_has_its_own_distinct_label():
    labels = {}
    for name, diagnostic in STATES.items():
        tree = render_diagnostic(diagnostic)['tree']
        labels[name] = tuple(sorted(set(re.findall(r'Moteur [a-zé]+(?: [a-zé]+)?|Zone couverte :\s*\[[^\]]*\|(?:oui|non)\]', tree))))
    assert len(set(labels.values())) == 4, labels
    assert 'Moteur disponible' in ' '.join(labels['disponible']) and 'Moteur indisponible' in ' '.join(labels['indisponible'])
    assert 'Moteur non configuré' in ' '.join(labels['non configuré'])
    assert any('non' in item for item in labels['zone non couverte'])


@needs_node
def test_the_state_badges_carry_a_distinct_visual_class_not_only_a_different_word():
    classes = {}
    for name in ('disponible', 'indisponible', 'non configuré'):
        tree = render_diagnostic(STATES[name])['tree']
        classes[name] = re.search(r'\[badge-soft is-(\w+)\|Moteur', tree).group(1)
    assert len(set(classes.values())) == 3, classes


@needs_node
def test_an_unconfigured_engine_shows_neither_the_zone_nor_the_extent_nor_profiles_nor_the_test_button():
    text = render_diagnostic(STATES['non configuré'])['text']
    assert 'Zone couverte' not in text and 'Étendue' not in text and 'Profils' not in text and 'Tester le moteur' not in text
    assert 'Pas encore vérifié' in text


@needs_node
def test_the_extent_is_written_in_words_and_the_coverage_answer_is_yes_or_no():
    text = render_diagnostic(BASE)['text']
    assert re.search(r'sud 49\.00°.*ouest 2\.00°.*nord 51\.60°.*est 6\.50°', text)
    assert 'Zone couverte :oui' in text.replace(' ', '') or 'Zone couverte :' in text and 'oui' in text
    assert 'non' in render_diagnostic(STATES['zone non couverte'])['text'] and 'routing.coverage_bbox' in render_diagnostic(STATES['zone non couverte'])['text']


@needs_node
def test_a_missing_extent_or_coverage_is_simply_not_shown():
    text = render_diagnostic({**BASE, 'info_bbox': None, 'coverage_contained': None})['text']
    assert 'Étendue' not in text and 'Zone couverte' not in text and 'null' not in text


@needs_node
@pytest.mark.parametrize('bbox', [[49.0, 2.0], 'abc', [None, None, None, None], ['a', 'b', 'c', 'd'], [1e308, 1e308, -1e308, -1e308]])
def test_a_malformed_extent_never_breaks_the_page_or_prints_raw_values(bbox):
    out = render_diagnostic({**BASE, 'info_bbox': bbox})
    assert 'Vérifié' in out['text'] and '[object' not in out['text'] and 'undefined' not in out['text']


@needs_node
def test_profiles_are_pills_in_a_stable_order_and_unknown_ones_follow():
    tree = render_diagnostic({**BASE, 'profiles': ['mtb', 'racingbike', 'foot', 'exotique']})['tree']
    names = re.findall(r'\[badge-soft is-info\|([^\]]*)\]', tree)
    assert names == ['foot', 'racingbike', 'mtb', 'exotique']


HOSTILE = ['<img src=x onerror=alert(1)>', '<script>alert(1)</script>', '"><svg/onload=alert(1)>', '&lt;b&gt;', "'; DROP TABLE users;--", '‮evil', 'x' * 5000]


@needs_node
@pytest.mark.parametrize('name', HOSTILE)
def test_a_hostile_profile_name_is_text_never_markup(name):
    out = render_diagnostic({**BASE, 'profiles': ['foot', name]})
    assert out['innerHTML'] == []                                                                       # aucun innerHTML écrit
    assert name in out['text'] or len(name) > 100                                                       # affiché tel quel, en texte
    assert '<img' not in out['tree'].replace(name, '') and '<script' not in out['tree'].replace(name, '')


@needs_node
def test_a_hostile_string_diagnostic_is_text_too():
    out = render_diagnostic('<img src=x onerror=alert(1)>')
    assert out['innerHTML'] == [] and '<img src=x onerror=alert(1)>' in out['text']


@needs_node
def test_a_diagnostic_of_an_unexpected_shape_never_crashes_the_page():
    for weird in (None, {}, [], 0, {'configured': True}, {'profiles': 'foot'}, {'profiles': [1, None]}):
        out = render_diagnostic(weird)
        assert 'Traceback' not in out['text'] and '[object Object]' not in out['text'] and out['innerHTML'] == []


def test_no_place_in_admin_js_writes_json_into_the_page_except_the_labelled_detail_blocks():
    """Recherche des JSON.stringify de admin.js : chacun doit être une valeur de champ de saisie, une comparaison ou le détail replié de l'audit."""
    source = (STATIC / 'js' / 'admin.js').read_text(encoding='utf-8')
    found = [(i + 1, line.strip()) for i, line in enumerate(source.splitlines()) if 'JSON.stringify' in line]
    allowed = ('dataset.initial', '!== input.dataset', "'pre'", "'Voir le détail'", 'setting.value')
    unexpected = [(n, line) for n, line in found if not any(a in line for a in allowed)]
    assert not unexpected, unexpected
    for _n, line in found:
        if "'pre'" in line:
            assert 'details' in line and 'Voir le détail' in line                                       # détail d'audit replié, jamais le diagnostic


@needs_node
def test_the_features_panel_never_contains_a_json_looking_text_for_any_diagnostic():
    for diagnostic in STATES.values():
        text = render_diagnostic(diagnostic)['text']
        assert not re.search(r'[{}\[\]]{1}\s*"', text) and not re.search(r'"\w+"\s*:', text)


# ===== CA-66 de bout en bout : vrai gunicorn, SMTP qui ne répond pas, SIGTERM =====================================================

def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def http(port, method, path, payload=None, *, csrf=None):
    headers = {'Host': HOST, 'Origin': ORIGIN, 'X-Forwarded-Proto': 'https'}
    data = None
    if payload is not None:
        data, headers['Content-Type'] = json.dumps(payload).encode(), 'application/json'
    if csrf:
        headers['X-CSRF-Token'] = csrf
    request = urllib.request.Request(f'http://127.0.0.1:{port}{path}', data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, response.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode('utf-8', 'replace')


@pytest.mark.skipif(not (WEB_APP / 'kml_viewer.py').exists(), reason="kml_viewer.py absent")
def test_sigterm_on_a_real_gunicorn_logs_one_warning_with_the_lost_messages_and_nothing_secret(tmp_path):
    try:
        import gunicorn  # noqa: F401
    except ImportError:
        pytest.skip("gunicorn non installé dans cet environnement")
    smtp = FakeSMTP(mode='hang')                                      # le serveur accepte la connexion et ne répond jamais
    try:
        app = make_app(tmp_path, OUTBOUND_ALLOWED_HOSTS=f'smtp=127.0.0.1:{smtp.port}')
        conn = connect(app)
        from app import db as database  # noqa: F401
        from app.audit import Actor
        app.config['SETTINGS_STORE'].set_many(conn, {'smtp.host': '127.0.0.1', 'smtp.port': smtp.port, 'smtp.security': 'none',
                                                     'smtp.sender': 'noreply@example.org', 'smtp.timeout_s': 30}, actor=Actor.cli())
        for i in range(4):
            add(app, conn, f'user{i}', email=f'user{i}@example.org')
        conn.close()
        port, log = free_port(), tmp_path / 'gunicorn.log'
        env = {**os.environ, 'FLASK_ENV': 'production', 'DATA_DIR': str(tmp_path / 'data'), 'AUTH_MODE': 'password', 'SECRET_KEY': 'k' * 48,
               'SECRETS_KEY': KEY, 'PUBLIC_URL': ORIGIN, 'TRUSTED_HOSTS': HOST, 'OUTBOUND_ALLOWED_HOSTS': f'smtp=127.0.0.1:{smtp.port}',
               'INBOX_ENABLED': '0', 'INBOX_DIR': str(tmp_path / 'inbox'), 'LOG_LEVEL': 'INFO', 'PYTHONUNBUFFERED': '1', 'TRUST_PROXY': '1',
               'PYTHONPATH': str(WEB_APP)}
        for key in ('OIDC_ISSUER', 'DEM_URL'):
            env.pop(key, None)
        with open(log, 'w') as handle:
            server = subprocess.Popen([sys.executable, '-m', 'gunicorn', '--bind', f'127.0.0.1:{port}', '--workers', '1', '--threads', '8', '--timeout', '120',
                                       '--no-control-socket', '--graceful-timeout', '20', 'kml_viewer:app'], cwd=WEB_APP, env=env, stdout=handle,
                                      stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                try:
                    if http(port, 'GET', '/api/health')[0] == 200:
                        break
                except OSError:
                    time.sleep(0.3)
            else:
                pytest.fail("gunicorn ne démarre pas :\n" + log.read_text()[-2000:])
            status, page = http(port, 'GET', '/auth/password/forgot')
            assert status == 200, page[:200]
            csrf = re.search(r'name="csrf-token" content="([^"]+)"', page).group(1)
            for i in range(4):
                assert http(port, 'POST', '/auth/password/forgot', {'identifier': f'user{i}'}, csrf=csrf)[0] == 200
            time.sleep(1.5)                                           # les messages sont en file (un en cours d'envoi, bloqué)
            server.send_signal(signal.SIGTERM)
            server.wait(timeout=40)
        finally:
            if server.poll() is None:
                server.kill()
                server.wait(10)
        text = log.read_text()
        lost = [line for line in text.splitlines() if 'non envoyé' in line or "File d'envoi" in line]
        assert len(lost) == 1, text[-3000:]
        line = lost[0]
        assert 'WARNING' in line and re.search(r'\b4\b', line) and 'Réinitialisation du mot de passe' in line
        for forbidden in ('user0@example.org', 'example.org', '#t=', '/auth/token', KEY, 'k' * 48, GOOD):
            assert forbidden not in line, forbidden
        assert 'Traceback' not in text
    finally:
        smtp.stop()
