"""
2e lot, groupe 2 (rôle test, écrit depuis la spec) : CA-R35 / CA-R36 (E-6, E-7 : exclusion éditeur / constructeur, effacement des points à toute
déconnexion et à tout 401), CA-R37 (E-8 : 413), CA-R39 (E-26 : nom daté dans APP_TIMEZONE), DEP-1b (cookie Secure exigé avec PUBLIC_URL https,
tous modes), nit E-4 (cible relue dans la transaction). Aucun marqueur xfail : un test rouge est un défaut à rapporter.

Le code JavaScript de common.js est EXÉCUTÉ (Node, bac à sable `vm`, DOM minimal) : pas une recherche de chaîne. Sans `node`, ces tests sont ignorés.
"""

import json
import re
import shutil
import subprocess
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app import create_app, db as database
from app.config import TestingConfig
from tests.conftest import OIDC_OVERRIDES
from tests.pw_harness import add, call, connect, make_app, new_secret, session_client
from tests.test_route_builder_api_edge import build_world

STATIC = Path(__file__).resolve().parent.parent / 'static'
HAS_NODE = shutil.which('node') is not None
needs_node = pytest.mark.skipif(not HAS_NODE, reason="node absent : exécution du JavaScript impossible")
QUOTA_MESSAGE = 'Quota de stockage atteint : supprime des activités'


# ===== CA-R37 / E-8 : quota atteint ⇒ 413 et message clair ======================================================================

@pytest.fixture
def world(tmp_path):
    w = build_world(tmp_path)
    yield w
    w.conn.close()


def fill_storage(world, user, megabytes):
    folder = Path(world.app.config['DATA_DIR']) / 'files' / str(user['id'])
    folder.mkdir(parents=True, exist_ok=True)
    filler = folder / 'filler.bin'
    filler.write_bytes(b'0' * (megabytes * 1024 * 1024 + 1))
    return filler


def test_a_full_quota_is_a_413_with_the_clear_message_nothing_is_created_and_the_result_survives_CA_R37(world):
    world.set(USER_QUOTA_MB=1)
    filler = fill_storage(world, world.alice, 1)
    rid = world.route().get_json()['route_meta']['result_id']
    before = world.conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0]
    r = world.post(world.a, 'save', {'result_id': rid})
    assert r.status_code == 413 and r.get_json()['error'] == QUOTA_MESSAGE
    assert world.conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == before
    assert world.runtime.results.get(world.alice['id'], rid) is not None                       # le résultat reste en mémoire
    assert world.runtime.results.get(world.alice['id'], rid).saved is None
    filler.unlink()
    again = world.post(world.a, 'save', {'result_id': rid})
    assert again.status_code == 200 and world.conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == before + 1


def test_the_413_names_no_path_size_or_internal_detail(world):
    world.set(USER_QUOTA_MB=1)
    fill_storage(world, world.alice, 1)
    rid = world.route().get_json()['route_meta']['result_id']
    body = world.post(world.a, 'save', {'result_id': rid}).get_data(as_text=True)
    assert 'filler' not in body and '/data' not in body and 'Traceback' not in body and str(world.alice['id']) not in body.replace('"success"', '')


def test_the_quota_of_one_user_does_not_block_another(world):
    world.set(USER_QUOTA_MB=1)
    fill_storage(world, world.alice, 1)
    rid = world.route(world.b).get_json()['route_meta']['result_id']
    assert world.post(world.b, 'save', {'result_id': rid}).status_code == 200


def test_other_save_failures_stay_503_or_400_and_never_413(world, monkeypatch):
    from app.services import ingest_service
    rid = world.route().get_json()['route_meta']['result_id']
    original = ingest_service.ingest_bytes
    monkeypatch.setattr(ingest_service, 'ingest_bytes',
                        lambda *a, **k: ingest_service.IngestResult('error', message='Erreur de stockage', retryable=True))
    r = world.post(world.a, 'save', {'result_id': rid})
    assert r.status_code == 503 and r.get_json()['error'] == 'Enregistrement impossible'            # le message interne n'est pas montré
    monkeypatch.setattr(ingest_service, 'ingest_bytes', original)
    assert world.post(world.a, 'save', {'result_id': rid}).status_code == 200


def test_the_413_also_leaves_the_save_slot_free_so_two_saves_after_freeing_make_one_activity(world):
    world.set(USER_QUOTA_MB=1)
    filler = fill_storage(world, world.alice, 1)
    rid = world.route().get_json()['route_meta']['result_id']
    assert world.post(world.a, 'save', {'result_id': rid}).status_code == 413
    assert world.post(world.a, 'save', {'result_id': rid}).status_code == 413                  # toujours 413 : pas un 409 « déjà enregistré »
    filler.unlink()
    codes = sorted(world.post(world.a, 'save', {'result_id': rid}).status_code for _ in range(2))
    assert codes == [200, 409]
    assert world.conn.execute("SELECT COUNT(*) FROM activities WHERE source = 'route_builder'").fetchone()[0] == 1


# ===== CA-R39 / E-26 : nom daté dans APP_TIMEZONE ================================================================================

def freeze_clock(monkeypatch, moment_utc):
    from app.api import route_builder_routes

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment_utc.astimezone(tz) if tz else moment_utc.replace(tzinfo=None)
    monkeypatch.setattr(route_builder_routes, 'datetime', Frozen)


@pytest.mark.parametrize('zone,moment,expected', [
    ('Europe/Paris', datetime(2026, 11, 25, 23, 30, tzinfo=timezone.utc), '26/11/2026'),
    ('Europe/Paris', datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc), '25/11/2026'),
    ('Europe/Paris', datetime(2026, 11, 25, 22, 59, 59, tzinfo=timezone.utc), '25/11/2026'),          # 23 h 59 min 59 s à Paris
    ('Europe/Paris', datetime(2026, 11, 25, 23, 0, 0, tzinfo=timezone.utc), '26/11/2026'),            # minuit pile à Paris
    ('Europe/Paris', datetime(2026, 6, 30, 22, 30, tzinfo=timezone.utc), '01/07/2026'),               # heure d'été : UTC+2
    ('Europe/Paris', datetime(2026, 12, 31, 23, 30, tzinfo=timezone.utc), '01/01/2027'),              # passage d'année
    ('UTC', datetime(2026, 11, 25, 23, 30, tzinfo=timezone.utc), '25/11/2026'),
    ('Pacific/Auckland', datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc), '26/11/2026'),
    ('America/Los_Angeles', datetime(2026, 11, 25, 5, 0, tzinfo=timezone.utc), '24/11/2026'),
])
def test_route_and_save_date_the_default_name_in_the_app_timezone_CA_R39(world, monkeypatch, zone, moment, expected):
    world.set(APP_TIMEZONE=zone)
    freeze_clock(monkeypatch, moment)
    answer = world.route().get_json()
    assert answer['filename'] == f'Trace planifiée du {expected}'
    rid = answer['route_meta']['result_id']
    saved_answer = world.post(world.a, 'save', {'result_id': rid})
    assert saved_answer.status_code == 200
    row = world.conn.execute("SELECT name FROM activities WHERE id = ?", (saved_answer.get_json()['activity_id'],)).fetchone()
    assert row['name'] == f'Trace planifiée du {expected}'


def test_the_gpx_name_inside_the_file_carries_the_same_date(world, monkeypatch):
    world.set(APP_TIMEZONE='Europe/Paris')
    freeze_clock(monkeypatch, datetime(2026, 11, 25, 23, 30, tzinfo=timezone.utc))
    answer = world.route().get_json()
    assert 'Trace planifiée du 26/11/2026' in json.dumps(answer, ensure_ascii=False)
    result = world.runtime.results.get(world.alice['id'], answer['route_meta']['result_id'])
    assert b'Trace planifi' in result.gpx and '26/11/2026'.encode() in result.gpx and b'25/11/2026' not in result.gpx


def test_an_explicit_name_wins_over_the_dated_default(world, monkeypatch):
    freeze_clock(monkeypatch, datetime(2026, 11, 25, 23, 30, tzinfo=timezone.utc))
    rid = world.route().get_json()['route_meta']['result_id']
    r = world.post(world.a, 'save', {'result_id': rid, 'name': 'Ma boucle'})
    assert r.status_code == 200
    assert world.conn.execute("SELECT name FROM activities WHERE id = ?", (r.get_json()['activity_id'],)).fetchone()[0] == 'Ma boucle'


def test_save_without_a_name_uses_the_timezone_at_save_time_not_at_route_time(world, monkeypatch):
    world.set(APP_TIMEZONE='Europe/Paris')
    freeze_clock(monkeypatch, datetime(2026, 11, 25, 22, 30, tzinfo=timezone.utc))
    rid = world.route().get_json()['route_meta']['result_id']
    freeze_clock(monkeypatch, datetime(2026, 11, 25, 23, 30, tzinfo=timezone.utc))
    r = world.post(world.a, 'save', {'result_id': rid})
    name = world.conn.execute("SELECT name FROM activities WHERE id = ?", (r.get_json()['activity_id'],)).fetchone()[0]
    assert name in ('Trace planifiée du 25/11/2026', 'Trace planifiée du 26/11/2026')               # les deux lectures sont admissibles, jamais autre chose


# ===== DEP-1b : cookie Secure exigé quand PUBLIC_URL est en https, dans tout mode ================================================

KEY = Fernet.generate_key().decode()
HOST = 'traces.example.com'


class Prod(TestingConfig):
    TESTING = False
    INBOX_ENABLED = False
    SECRET_KEY = 'k' * 40
    SESSION_COOKIE_SECURE = None


def start(tmp_path, **overrides):
    return create_app(Prod, overrides={'DATA_DIR': str(tmp_path / 'data'), **overrides})


MODES = {
    'password': {'AUTH_MODE': 'password', 'TRUSTED_HOSTS': [HOST], 'SECRETS_KEY': KEY, 'PUBLIC_URL': f'https://{HOST}'},
    'oidc+password': {'AUTH_MODE': 'oidc', **OIDC_OVERRIDES, 'TRUSTED_HOSTS': [HOST], 'SECRETS_KEY': KEY, 'PUBLIC_URL': f'https://{HOST}'},
    'oidc seul avec PUBLIC_URL': {'AUTH_MODE': 'oidc', **OIDC_OVERRIDES, 'TRUSTED_HOSTS': [HOST], 'PUBLIC_URL': f'https://{HOST}'},
}


@pytest.mark.parametrize('mode', list(MODES))
@pytest.mark.parametrize('value', [False, 0, ''])
def test_a_non_secure_session_cookie_is_refused_at_startup_with_an_https_public_url(tmp_path, mode, value):
    with pytest.raises(RuntimeError, match='SESSION_COOKIE_SECURE'):
        start(tmp_path, **MODES[mode], SESSION_COOKIE_SECURE=value)


@pytest.mark.parametrize('mode', list(MODES))
@pytest.mark.parametrize('text', ['0', 'false', 'no', 'False'])
def test_the_environment_variable_set_to_off_is_refused_with_an_https_public_url(tmp_path, monkeypatch, mode, text):
    from app import config as configuration
    monkeypatch.setenv('SESSION_COOKIE_SECURE', text)

    class FromEnvironment(Prod):
        SESSION_COOKIE_SECURE = configuration._env_bool('SESSION_COOKIE_SECURE', None)
    with pytest.raises(RuntimeError, match='SESSION_COOKIE_SECURE'):
        create_app(FromEnvironment, overrides={'DATA_DIR': str(tmp_path / 'data'), **MODES[mode]})


@pytest.mark.parametrize('mode', list(MODES))
@pytest.mark.parametrize('value', [None, True])
def test_the_session_cookie_is_secure_by_default_and_accepted_explicitly(tmp_path, mode, value):
    app = start(tmp_path / str(value), **MODES[mode], SESSION_COOKIE_SECURE=value)
    assert app.config['SESSION_COOKIE_SECURE'] is True


def test_oidc_without_public_url_keeps_the_legacy_non_secure_possibility(tmp_path):
    """Sans PUBLIC_URL il n'y a pas d'https déclaré : pas de refus (EF-0.4 : le démarrage n'est pas refusé pour ce motif)."""
    app = start(tmp_path, AUTH_MODE='oidc', **OIDC_OVERRIDES, TRUSTED_HOSTS=[HOST], SESSION_COOKIE_SECURE=False)
    assert app.config['SESSION_COOKIE_SECURE'] is False and not app.config['PASSWORD_ACCOUNTS']


@pytest.mark.parametrize('mode,extra', [('password', {'AUTH_MODE': 'password', 'TRUSTED_HOSTS': ['localhost'], 'SECRETS_KEY': KEY}),
                                        ('oidc', {'AUTH_MODE': 'oidc', **OIDC_OVERRIDES, 'TRUSTED_HOSTS': ['localhost'], 'SECRETS_KEY': KEY})])
def test_http_localhost_may_keep_a_non_secure_cookie_for_development(tmp_path, mode, extra):
    app = start(tmp_path, **extra, PUBLIC_URL='http://localhost:5000', SESSION_COOKIE_SECURE=False)
    assert app.config['SESSION_COOKIE_SECURE'] is False


def test_the_error_message_names_the_variable_and_the_way_out(tmp_path):
    with pytest.raises(RuntimeError) as caught:
        start(tmp_path, **MODES['oidc+password'], SESSION_COOKIE_SECURE=False)
    assert 'SESSION_COOKIE_SECURE' in str(caught.value) and 'https' in str(caught.value) and KEY not in str(caught.value)


def test_local_mode_is_not_blocked_by_the_rule(tmp_path):
    app = start(tmp_path, AUTH_MODE='local')
    assert app.config['AUTH_MODE'] == 'local'


# ===== nit E-4 : la cible est relue DANS la transaction ============================================================================

@pytest.fixture
def admin_app(tmp_path):
    app = make_app(tmp_path)
    conn = connect(app)
    admin = add(app, conn, 'chef', admin=True, secret=new_secret())
    yield app, conn, session_client(app, admin)
    conn.close()


@contextmanager
def interfere_before_transaction(app, action):
    """Exécute `action(connexion)` (une autre connexion, validée) juste AVANT l'ouverture de la transaction de la route."""
    from app.api import users_routes
    real, done = users_routes.write_transaction, []

    @contextmanager
    def wrapper(db):
        if not done:
            done.append(1)
            other = database.connect(app.config['DB_PATH'])
            try:
                action(other)
                other.commit()
            finally:
                other.close()
        with real(db) as value:
            yield value
    users_routes.write_transaction = wrapper
    try:
        yield
    finally:
        users_routes.write_transaction = real


def audit_actions(conn):
    return [r[0] for r in conn.execute("SELECT action FROM audit_log ORDER BY id")]


def test_a_target_deleted_between_the_first_read_and_the_transaction_is_a_clean_404_with_no_audit(admin_app):
    app, conn, client = admin_app
    target = add(app, conn, 'alice')
    with interfere_before_transaction(app, lambda c: c.execute("DELETE FROM users WHERE id = ?", (target['id'],))):
        r = call(client, 'patch', f"/api/admin/users/{target['id']}", {'is_active': False})
    assert r.status_code == 404 and 'Traceback' not in r.get_data(as_text=True)
    assert not [a for a in audit_actions(conn) if a in ('user.deactivated', 'user.activated')]


def test_the_audit_before_and_after_reflect_the_state_inside_the_transaction_not_the_stale_read(admin_app):
    """Désactivé par quelqu'un d'autre entre-temps : la requête « désactiver » ne change rien et ne trace donc pas une transition fausse."""
    app, conn, client = admin_app
    target = add(app, conn, 'alice')
    with interfere_before_transaction(app, lambda c: c.execute("UPDATE users SET is_active = 0 WHERE id = ?", (target['id'],))):
        r = call(client, 'patch', f"/api/admin/users/{target['id']}", {'is_active': False})
    assert r.status_code == 200
    assert audit_actions(conn).count('user.deactivated') == 0


def test_the_reverse_race_reactivated_meanwhile_is_traced_from_the_real_state(admin_app):
    app, conn, client = admin_app
    target = add(app, conn, 'alice', active=False)
    with interfere_before_transaction(app, lambda c: c.execute("UPDATE users SET is_active = 1 WHERE id = ?", (target['id'],))):
        r = call(client, 'patch', f"/api/admin/users/{target['id']}", {'is_active': True})
    assert r.status_code == 200 and audit_actions(conn).count('user.activated') == 0


def test_a_target_deleted_before_the_delete_transaction_is_a_clean_404(admin_app):
    app, conn, client = admin_app
    target = add(app, conn, 'alice')
    with interfere_before_transaction(app, lambda c: c.execute("DELETE FROM users WHERE id = ?", (target['id'],))):
        r = call(client, 'delete', f"/api/admin/users/{target['id']}")
    assert r.status_code == 404 and 'user.deleted' not in audit_actions(conn)


def test_two_simultaneous_deactivations_trace_exactly_one_transition(admin_app):
    app, conn, client = admin_app
    target = add(app, conn, 'alice')
    barrier = threading.Barrier(2, timeout=10)
    results = []

    def job():
        own = connect(app)
        try:
            c = session_client(app, own.execute("SELECT * FROM users WHERE username = 'chef'").fetchone())
        finally:
            own.close()
        barrier.wait()
        results.append(call(c, 'patch', f"/api/admin/users/{target['id']}", {'is_active': False}).status_code)
    threads = [threading.Thread(target=job) for _ in range(2)]
    [t.start() for t in threads]
    [t.join(20) for t in threads]
    assert sorted(results) == [200, 200]
    assert audit_actions(conn).count('user.deactivated') == 1


# ===== CA-R36 / E-7 : effacement des points à TOUTE déconnexion et à tout 401 (code JavaScript exécuté) ====================================

HARNESS = r"""
const vm = require('vm'), fs = require('fs');
const source = fs.readFileSync(process.argv[2], 'utf8');
const scenario = JSON.parse(process.argv[3]);
const listeners = {};
class HTMLFormElement { constructor(action) { this.action = action; } getAttribute(n) { return n === 'action' ? this.action : null; } }
class Node {}
class FormData {}
function makeStorage(initial, throwing) {
  const store = {};
  Object.defineProperty(store, 'removeItem', { enumerable: false, value: (k) => { if (throwing) throw new Error('indisponible'); delete store[k]; } });
  Object.assign(store, initial);
  return store;
}
const storage = makeStorage(scenario.storage, scenario.throwing);
const location = { pathname: '/segments', search: '?a=1', assigned: null, assign(u) { this.assigned = u; } };
const sandbox = {
  document: { addEventListener(t, f) { (listeners[t] = listeners[t] || []).push(f); }, getElementById: () => null, querySelector: () => null,
              documentElement: { style: { setProperty() {} } }, createElement: () => ({}), createTextNode: (t) => ({ t }) },
  window: { localStorage: storage, location }, HTMLFormElement, Node, FormData, console,
  ResizeObserver: class { observe() {} }, fetch: async () => ({ status: scenario.status || 200, ok: (scenario.status || 200) < 400, json: async () => ({}) }),
  encodeURIComponent, Object, JSON, Error, Promise, Date, Math, Number, String, Array, Map, Set, setTimeout, clearTimeout, Intl,
};
sandbox.window.document = sandbox.document;
vm.createContext(sandbox);
vm.runInContext(source, sandbox);
(async () => {
  let thrown = null;
  if (scenario.submit !== undefined) {
    for (const f of (listeners.submit || [])) f({ target: scenario.submit === null ? {} : new HTMLFormElement(scenario.submit) });
  }
  if (scenario.api) { try { await vm.runInContext("App.api('/api/x')", sandbox); } catch (e) { thrown = e.message; } }
  console.log(JSON.stringify({ storage: Object.keys(storage).sort(), assigned: location.assigned, thrown }));
})();
"""

BUILDER_KEYS = {'kmlviewer.routeBuilder.points.7': '[[1,2]]', 'kmlviewer.routeBuilder.state.7': '{"activity":"run","distance":"12"}',
                'kmlviewer.routeBuilder.points.9': '[[3,4]]'}
UNRELATED = {'kmlviewer.theme': 'dark', 'other': 'x', 'kmlviewer.prefs': '{}'}


def run_js(**scenario):
    scenario.setdefault('storage', {**BUILDER_KEYS, **UNRELATED})
    done = subprocess.run(['node', '-', str(STATIC / 'js' / 'common.js'), json.dumps(scenario)], input=HARNESS, capture_output=True, text=True,
                          timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


@needs_node
@pytest.mark.parametrize('action', ['/auth/logout', 'https://traces.example.com/auth/logout', '/kml/auth/logout'])
def test_any_logout_form_clears_every_route_builder_key_and_only_those_CA_R36(action):
    out = run_js(submit=action)
    assert out['storage'] == sorted(UNRELATED)


@needs_node
@pytest.mark.parametrize('action', ['/auth/login', '/search', '/auth/logout/other', '', '/api/logout'])
def test_a_form_that_is_not_a_logout_keeps_the_points(action):
    assert run_js(submit=action)['storage'] == sorted({**BUILDER_KEYS, **UNRELATED})


@needs_node
def test_an_event_target_that_is_not_a_form_changes_nothing_and_never_throws():
    assert run_js(submit=None)['storage'] == sorted({**BUILDER_KEYS, **UNRELATED})


@needs_node
def test_an_unavailable_storage_never_breaks_the_logout():
    out = run_js(submit='/auth/logout', throwing=True)
    assert out['thrown'] is None


@needs_node
def test_a_401_clears_the_points_and_goes_back_to_the_login_keeping_the_page():
    out = run_js(api=True, status=401)
    assert out['storage'] == sorted(UNRELATED) and out['thrown'] == 'Session expirée'
    assert out['assigned'] == '/auth/login?next=' + '%2Fsegments%3Fa%3D1'


@needs_node
@pytest.mark.parametrize('status', [200, 403, 404, 500])
def test_other_statuses_keep_the_points(status):
    assert run_js(api=True, status=status)['storage'] == sorted({**BUILDER_KEYS, **UNRELATED})


@pytest.mark.parametrize('path,admin', [('/', False), ('/dashboard', False), ('/segments', False), ('/profile', False), ('/admin', True)])
def test_every_authenticated_page_ships_common_js_and_a_post_logout_form_CA_R36(tmp_path, path, admin):
    """Le script commun ET le formulaire de déconnexion sont sur CHAQUE page (visualiseur, tableau de bord, tronçons, profil, administration),
    y compris pour un compte auquel la fonction bêta n'est pas ouverte : c'est ce qui fait que le script commun efface les points."""
    import re
    app = make_app(tmp_path)
    conn = connect(app)
    user = add(app, conn, 'u', admin=admin, secret=new_secret() if admin else None)
    html = call(session_client(app, user), 'get', path).get_data(as_text=True)
    assert re.search(r'<script src="[^"]*js/common\.js"', html), path
    assert re.search(r'<form[^>]*method="post"[^>]*action="[^"]*/auth/logout"', html), path
    scripts = re.findall(r'<script src="[^"]*js/([\w./-]+)"', html)
    if path != '/':
        assert 'viewer/route-builder.js' not in scripts                                   # le constructeur n'est pas chargé : seule la page commune efface
    conn.close()


def test_route_builder_js_no_longer_owns_the_logout_wipe_and_does_not_double_register_it():
    source = (STATIC / 'js' / 'viewer' / 'route-builder.js').read_text(encoding='utf-8')
    assert source.count('/auth/logout') == 0                                              # un seul propriétaire de l'effacement (common.js)
    assert 'kmlviewer.routeBuilder.' in (STATIC / 'js' / 'common.js').read_text(encoding='utf-8')


# ===== CA-R35 / E-6 : exclusion mutuelle éditeur de points / constructeur (code JavaScript exécuté) =====================================

def test_the_stored_value_holds_only_points_activity_and_distance():
    """RB-2.6 : ce que le constructeur écrit dans le localStorage (lecture du code d'écriture, aucune autre donnée)."""
    source = (STATIC / 'js' / 'viewer' / 'route-builder.js').read_text(encoding='utf-8')
    start = source.index('function save')
    block = source[start:start + 900]
    assert 'points' in block and 'activity' in block and 'distance' in block
    for forbidden in ('token', 'password', 'email', 'username', 'result_id', 'session'):
        assert forbidden not in block.lower()


@needs_node
def test_the_editor_and_the_builder_close_each_other_in_the_source_order_E6():
    """Garde-fou de structure (le comportement complet exige un navigateur : voir la recette de l'écart E-6)."""
    editor = (STATIC / 'js' / 'viewer' / 'editor.js').read_text(encoding='utf-8')
    builder = (STATIC / 'js' / 'viewer' / 'route-builder.js').read_text(encoding='utf-8')
    assert 'RouteBuilder.close()' in editor and 'cancelEditing()' in builder
    assert re.search(r'return \{ setup, close\b', builder)
    # l'éditeur ferme le constructeur AVANT d'activer son mode ; le constructeur ferme l'éditeur AVANT de s'activer
    assert editor.index('RouteBuilder.close()') < editor.index('Editor.mode = mode')
    assert builder.index('cancelEditing()') < builder.index('state.active = true')
