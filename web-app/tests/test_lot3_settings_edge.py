"""
Refonte de la page Réglages (admin-space §5.6b, 3ᵉ image) : tests ÉCRITS PAR `test` depuis les critères d'acceptation, sans lire le
code de dev pour décider de ce qu'il faut vérifier.

  CA-67  registre : SECTIONS, unités, champs de l'API
  CA-68  repli (`fallback`), variable d'env invalide, secrets masqués, rétablissement d'une destination
  CA-69  PATCH tout ou rien, `field` et `index`
  CA-70  contrôle de version (409 `settings_version_conflict`), concurrence
  CA-71  `restart_pending`
  CA-79  rendu : ni innerHTML, ni script ou style en ligne, ni boîte du navigateur ; un éditeur par type
  CA-80  banc navigateur : README, empreintes, image épinglée, hors CI
CA-72 à CA-78 sont des comportements de navigateur : ils sont joués par `web-app/tests_browser/` (scénario (c)).
"""

import configparser
import json
import pathlib
import re
import subprocess
import sys
import threading

import pytest

from app import create_app, outbound
from app.audit import Actor
from app.settings import REGISTRY, SECTIONS, SettingsStore
from app.settings.registry import DEFAULT_BASEMAPS, Setting
from tests.pw_harness import HOST, KEY, ORIGIN, Prod, add, call, connect, new_secret, session_client

WEB_APP = pathlib.Path(__file__).resolve().parent.parent
CLI = Actor.cli()
OUTBOUND = 'smtp=127.0.0.1:2525,routing=graphhopper.internal:8989,routing=other.internal:8989'
EXPECTED_SECTIONS = ('Sécurité', 'E-mail', 'Carte', 'Traces planifiées', 'Altitude', 'Dossier surveillé', 'Stockage', 'Général')
EXPECTED_COUNTS = {'Sécurité': 11, 'E-mail': 6, 'Carte': 5, 'Traces planifiées': 16, 'Altitude': 7, 'Dossier surveillé': 3,
                   'Stockage': 1, 'Général': 1}


def basemap(name, url='https://a.tiles.exemple.fr/{z}/{x}/{y}.png', **extra):
    return {'name': name, 'url': url, 'subdomains': 'abc', 'attribution': '© Exemple', 'max_zoom': 19, **extra}


@pytest.fixture
def stack(tmp_path, monkeypatch):
    """Fabrique d'application (mode password) : `new(extra=[Setting…], **overrides)` -> (app, conn, client admin)."""
    opened = []

    def new(extra=(), directory=None, **overrides):
        for setting in extra:
            monkeypatch.setitem(REGISTRY, setting.key, setting)
        settings = {'OUTBOUND_ALLOWED_HOSTS': OUTBOUND, **overrides}
        app = create_app(Prod, overrides={'DATA_DIR': str(directory or tmp_path / 'data'), 'AUTH_MODE': 'password',
                                          'TRUSTED_HOSTS': [HOST], 'SECRETS_KEY': KEY, 'PUBLIC_URL': ORIGIN, **settings})
        conn = connect(app)
        opened.append(conn)
        try:
            admin = add(app, conn, 'root', admin=True, secret=new_secret(), email='root@example.org')
        except Exception:                                    # base déjà peuplée (second démarrage sur la même base)
            admin = conn.execute("SELECT * FROM users WHERE username = 'root'").fetchone()
        return app, conn, session_client(app, admin)

    yield new
    for conn in opened:
        conn.close()


def get_settings(client):
    response = call(client, 'get', '/api/admin/settings')
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()


def by_key(body):
    return {s['key']: s for s in body['settings']}


def audit_count(conn, action):
    return conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = ?", (action,)).fetchone()[0]


def patch(client, settings, **extra):
    return call(client, 'patch', '/api/admin/settings', {'settings': settings, **extra})


# --- CA-67 : registre et contrat de l'API --------------------------------------------------------------------------------------

def test_the_registry_lists_the_eight_sections_in_the_order_of_EF_6_9():
    assert tuple(SECTIONS) == EXPECTED_SECTIONS


def test_every_setting_belongs_to_a_declared_section_and_the_visible_counts_match_the_spec():
    assert {s.section for s in REGISTRY.values()} <= set(SECTIONS)
    counts = {}
    for setting in REGISTRY.values():
        if not setting.secret:
            counts[setting.section] = counts.get(setting.section, 0) + 1
    assert counts == EXPECTED_COUNTS, counts                                  # CA-72 : compteurs de la page


@pytest.mark.parametrize('section', ['Inconnue', '', 'securite', 'Sécurité '])
def test_a_setting_whose_section_is_not_declared_is_refused_at_construction(section):
    with pytest.raises(ValueError):
        Setting('test.bool', 'bool', True, section, 'Un réglage')
    # une section déclarée passe
    SettingsStore(registry={'test.ok': Setting('test.ok', 'bool', True, 'Général', 'Un réglage')})


def test_a_registry_built_from_a_valid_setting_is_accepted_by_the_store_and_the_page_has_no_tab_for_an_empty_section():
    store = SettingsStore(registry={'test.ok': Setting('test.ok', 'bool', True, 'Général', 'Un réglage')})
    assert store.get('test.ok') is True


def test_every_int_and_float_declares_a_unit_and_no_label_ends_with_a_unit_in_brackets():
    numeric = [s for s in REGISTRY.values() if s.type in ('int', 'float')]
    assert numeric
    for setting in numeric:
        assert setting.unit is not None, setting.key                             # la chaîne vide est permise
        assert isinstance(setting.unit, str), setting.key
    for setting in REGISTRY.values():
        tail = re.search(r'\(([^()]*)\)\s*$', setting.label)
        if tail:
            inside = tail.group(1).strip()
            assert inside != setting.unit and inside not in {'s', 'ms', 'Mo', 'Go', 'km', 'm', '%', 'h', 'jours', '/ h', 'min'}, \
                (setting.key, setting.label)
    with pytest.raises(ValueError):                                              # un int sans `unit` ne se construit pas
        Setting('test.n', 'int', 1, 'Général', 'Un nombre', min=0, max=5)
    with pytest.raises(ValueError):
        Setting('test.f', 'float', 1.0, 'Général', 'Un nombre', min=0, max=5)
    Setting('test.n', 'int', 1, 'Général', 'Un nombre', min=0, max=5, unit='')   # chaîne vide : permise


def test_the_api_returns_the_sections_and_the_new_fields_for_every_setting(stack):
    app, conn, client = stack()
    body = get_settings(client)
    assert body['sections'] == list(EXPECTED_SECTIONS)
    assert isinstance(body['version'], int)
    views = by_key(body)
    assert set(views) == set(REGISTRY)
    for key, setting in REGISTRY.items():
        view = views[key]
        for field in ('unit', 'fallback', 'fallback_source', 'env_invalid', 'restart_pending'):
            assert field in view, (key, field)
        assert view['unit'] == setting.unit
        assert view['section'] == setting.section
        assert view['env_invalid'] is False and view['restart_pending'] is False
        assert view['fallback_source'] in ('env', 'défaut')
    assert views['DEM_TIMEOUT']['unit'] == 's' and views['routing.max_distance_km']['unit'] == 'km'
    assert views['map.zoom']['fallback'] == 6 and views['map.zoom']['fallback_source'] == 'défaut'


def test_the_basemaps_setting_carries_the_default_tile_hosts_when_no_tiles_entry_is_configured(stack):
    app, conn, client = stack()
    views = by_key(get_settings(client))
    assert views['map.basemaps']['allowlist'] == sorted(outbound.DEFAULT_TILE_HOSTS)
    assert views['map.zoom']['allowlist'] is None and views['DEM_TIMEOUT']['allowlist'] is None


@pytest.mark.parametrize('hosts,expected', [
    ('tiles=a.tiles.exemple.fr', ['a.tiles.exemple.fr']),
    ('tiles=b.tiles.exemple.fr,tiles=a.tiles.exemple.fr', ['a.tiles.exemple.fr', 'b.tiles.exemple.fr']),
])
def test_the_basemaps_setting_carries_exactly_the_configured_tile_hosts(stack, hosts, expected):
    app, conn, client = stack(OUTBOUND_ALLOWED_HOSTS=f'{OUTBOUND},{hosts}')
    assert by_key(get_settings(client))['map.basemaps']['allowlist'] == expected          # remplace la liste par défaut


def test_the_settings_listing_is_refused_without_admin_rights(stack):
    app, conn, client = stack()
    plain = add(app, conn, 'plain')
    assert call(session_client(app, plain), 'get', '/api/admin/settings').status_code == 403
    assert call(app.test_client(), 'get', '/api/admin/settings').status_code == 401
    assert call(session_client(app, plain), 'patch', '/api/admin/settings', {'settings': {'map.zoom': 7}}).status_code == 403
    assert call(session_client(app, plain), 'delete', '/api/admin/settings/map.zoom').status_code == 403


# --- CA-68 : repli, variable invalide, secrets, rétablissement -----------------------------------------------------------------

def test_a_value_in_base_over_a_valid_env_variable_reports_the_env_value_as_fallback(stack):
    # la variable est posée dans la configuration de l'app seulement (jamais os.environ) : `env_overridden` en découle aussi
    app, conn, client = stack(DEM_TIMEOUT='30')
    view = by_key(get_settings(client))['DEM_TIMEOUT']
    assert (view['source'], view['value']) == ('env', 30.0) and view['fallback_source'] == 'env'
    assert patch(client, {'DEM_TIMEOUT': 45}).status_code == 200
    view = by_key(get_settings(client))['DEM_TIMEOUT']
    assert view['source'] == 'base' and view['value'] == 45.0
    assert view.get('env_overridden') is True
    assert view['fallback'] == 30.0 and view['fallback_source'] == 'env' and view['env_invalid'] is False
    before = audit_count(conn, 'setting.reset')
    deleted = call(client, 'delete', '/api/admin/settings/DEM_TIMEOUT').get_json()
    assert deleted['reset'] is True
    view = by_key(get_settings(client))['DEM_TIMEOUT']
    assert view['value'] == 30.0 and view['source'] == 'env'
    assert audit_count(conn, 'setting.reset') == before + 1


def test_an_invalid_env_variable_is_flagged_and_the_fallback_is_the_default(stack):
    app, conn, client = stack(DEM_TIMEOUT='env-invalide-x7q')
    view = by_key(get_settings(client))['DEM_TIMEOUT']
    assert view['env_invalid'] is True and view['source'] == 'défaut' and view['value'] == 15.0
    assert patch(client, {'DEM_TIMEOUT': 45}).status_code == 200
    text = call(client, 'get', '/api/admin/settings').get_data(as_text=True)
    view = by_key(json.loads(text))['DEM_TIMEOUT']
    assert view['fallback'] == 15.0 and view['fallback_source'] == 'défaut' and view['env_invalid'] is True
    # jamais la valeur de la variable (EF-6.3)
    assert 'env-invalide-x7q' not in text


def test_a_setting_in_base_without_env_variable_falls_back_to_the_default(stack):
    app, conn, client = stack()
    assert patch(client, {'map.zoom': 9}).status_code == 200
    view = by_key(get_settings(client))['map.zoom']
    assert view['source'] == 'base' and view['value'] == 9 and view['fallback'] == 6 and view['fallback_source'] == 'défaut'
    assert 'env_overridden' not in view or view['env_overridden'] is False


def test_a_secret_setting_never_exposes_a_value_nor_a_fallback_nor_the_env_value(stack):
    secret = Setting('test.secret', 'str', '', 'Général', 'Un secret de test', max_length=40, env='TEST_SECRET_ENV', secret=True)
    app, conn, client = stack(extra=[secret], TEST_SECRET_ENV='valeur-jamais-visible-9fk2')
    text = call(client, 'get', '/api/admin/settings').get_data(as_text=True)
    view = by_key(json.loads(text))['test.secret']
    assert view['secret'] is True and view['value'] is None and view['fallback'] is None
    assert 'valeur-jamais-visible-9fk2' not in text
    store = app.config['SETTINGS_STORE']
    store.set_many(conn, {'test.secret': 'valeur-en-base-77hd'}, actor=CLI)
    store.refresh(conn, force=True)
    text = call(client, 'get', '/api/admin/settings').get_data(as_text=True)
    view = by_key(json.loads(text))['test.secret']
    assert view['value'] is None and view['fallback'] is None
    assert 'valeur-en-base-77hd' not in text and 'valeur-jamais-visible-9fk2' not in text
    assert 'valeur-en-base-77hd' not in text
    one = call(client, 'get', '/api/admin/settings/test.secret').get_data(as_text=True)
    assert 'valeur-en-base-77hd' not in one and 'valeur-jamais-visible-9fk2' not in one


def test_resetting_a_destination_to_another_env_destination_clears_and_traces_the_linked_secret(stack):
    app, conn, client = stack(ROUTING_BASE_URL='http://graphhopper.internal:8989')
    assert patch(client, {'routing.base_url': 'http://other.internal:8989'}).status_code == 200
    put = call(client, 'put', '/api/admin/secrets/routing.api_key', {'value': 'cle-de-routage-123456'})
    assert put.status_code == 200, put.get_data(as_text=True)
    secrets = {s['name']: s['configured'] for s in call(client, 'get', '/api/admin/secrets').get_json()['secrets']}
    assert secrets['routing.api_key'] is True
    cleared = audit_count(conn, 'secret.clear_on_destination_change')
    assert call(client, 'delete', '/api/admin/settings/routing.base_url').status_code == 200
    secrets = {s['name']: s['configured'] for s in call(client, 'get', '/api/admin/secrets').get_json()['secrets']}
    assert secrets['routing.api_key'] is False                                    # la destination a changé : le secret est effacé
    assert audit_count(conn, 'secret.clear_on_destination_change') == cleared + 1
    dump = json.dumps([dict(r) for r in conn.execute("SELECT * FROM audit_log")])
    assert 'cle-de-routage-123456' not in dump


def test_resetting_a_destination_to_the_same_destination_keeps_the_secret(stack):
    app, conn, client = stack(ROUTING_BASE_URL='http://graphhopper.internal:8989')
    assert patch(client, {'routing.base_url': 'http://graphhopper.internal:8989/v2'}).status_code == 200      # même hôte et port
    assert call(client, 'put', '/api/admin/secrets/routing.api_key', {'value': 'cle-de-routage-123456'}).status_code == 200
    assert call(client, 'delete', '/api/admin/settings/routing.base_url').status_code == 200
    secrets = {s['name']: s['configured'] for s in call(client, 'get', '/api/admin/secrets').get_json()['secrets']}
    assert secrets['routing.api_key'] is True


# --- CA-69 : PATCH tout ou rien, field et index --------------------------------------------------------------------------------

def test_a_patch_with_one_bad_value_writes_nothing_and_names_the_field(stack):
    app, conn, client = stack()
    before = audit_count(conn, 'setting.update')
    version = get_settings(client)['version']
    response = call(client, 'patch', '/api/admin/settings', {'map.zoom': 30, 'map.center_lat': 45})
    body = response.get_json()
    assert response.status_code == 400 and body['field'] == 'map.zoom' and 'index' not in body
    store = app.config['SETTINGS_STORE']
    store.refresh(conn, force=True)
    assert store.get('map.zoom') == 6 and store.get('map.center_lat') == 46.603354           # aucune des deux clés écrite
    assert audit_count(conn, 'setting.update') == before and get_settings(client)['version'] == version
    response = patch(client, {'map.zoom': 30, 'map.center_lat': 45})                          # forme avec `settings` : idem
    assert response.status_code == 400 and response.get_json()['field'] == 'map.zoom'


def test_a_basemap_without_the_y_placeholder_is_refused_with_the_field_and_the_index(stack):
    app, conn, client = stack(OUTBOUND_ALLOWED_HOSTS=f'{OUTBOUND},tiles=a.tiles.exemple.fr')
    good = [basemap('un'), basemap('deux'), basemap('trois', url='https://a.tiles.exemple.fr/{z}/{x}/tile.png')]
    before = audit_count(conn, 'setting.update')
    response = patch(client, {'map.basemaps': good, 'map.default_basemap': 'un'})
    body = response.get_json()
    assert response.status_code == 400 and body['field'] == 'map.basemaps' and body['index'] == 2, body
    assert audit_count(conn, 'setting.update') == before
    store = app.config['SETTINGS_STORE']
    store.refresh(conn, force=True)
    assert store.get('map.basemaps') == [dict(b) for b in DEFAULT_BASEMAPS]                    # rien n'est écrit


def test_a_basemap_with_a_host_outside_tiles_is_refused_with_the_field_the_index_and_the_host(stack):
    app, conn, client = stack(OUTBOUND_ALLOWED_HOSTS=f'{OUTBOUND},tiles=a.tiles.exemple.fr')
    maps = [basemap('un'), basemap('deux', url='https://{s}.tiles.exemple.fr/{z}/{x}/{y}.png', subdomains='ab')]
    response = patch(client, {'map.basemaps': maps})
    body = response.get_json()
    assert response.status_code == 400 and body['field'] == 'map.basemaps' and body['index'] == 1, body
    assert 'tiles=' in body['error'] or 'OUTBOUND_ALLOWED_HOSTS' in body['error']
    # le fond ou l'hôte en cause est nommé
    assert 'deux' in body['error'] or 'b.tiles.exemple.fr' in body['error']


def test_a_basemap_whose_markup_text_is_in_the_attribution_is_refused_by_the_server_CA_79(stack):
    app, conn, client = stack(OUTBOUND_ALLOWED_HOSTS=f'{OUTBOUND},tiles=a.tiles.exemple.fr')
    for attribution in ('<img src=x onerror=alert(1)>', 'A & B', 'dit "oui"', "l'autre", '<b>x</b>'):
        response = patch(client, {'map.basemaps': [basemap('un', attribution=attribution)], 'map.default_basemap': 'un'})
        assert response.status_code == 400 and response.get_json()['field'] == 'map.basemaps', attribution
        assert response.get_json()['index'] == 0


def test_a_default_basemap_missing_from_the_list_is_no_longer_a_server_error_decision_6(stack):
    app, conn, client = stack(OUTBOUND_ALLOWED_HOSTS=f'{OUTBOUND},tiles=a.tiles.exemple.fr')
    response = patch(client, {'map.basemaps': [basemap('un')], 'map.default_basemap': 'osm'})
    assert response.status_code == 200
    codes = [a['code'] for a in get_settings(client)['alerts']]
    assert 'default_basemap_unavailable' in codes                                              # le bandeau d'usage subsiste


def test_a_refused_mfa_policy_is_a_409_with_the_field_and_not_the_version_code(stack):
    app, conn, client = stack()
    add(app, conn, 'sans-facteur')                                                              # actif, aucun second facteur
    response = patch(client, {'auth.mfa_policy': 'all'})
    body = response.get_json()
    assert response.status_code == 409 and body['field'] == 'auth.mfa_policy', body
    assert body.get('code') != 'settings_version_conflict'


# --- CA-70 : contrôle de version -----------------------------------------------------------------------------------------------

def test_a_patch_with_the_current_version_succeeds_and_returns_the_next_version(stack):
    app, conn, client = stack()
    version = get_settings(client)['version']
    response = patch(client, {'map.zoom': 8}, version=version)
    assert response.status_code == 200
    assert response.get_json()['version'] == version + 1
    assert get_settings(client)['version'] == version + 1
    again = patch(client, {'map.zoom': 9}, version=version + 1)                                 # deux enregistrements successifs
    assert again.status_code == 200 and again.get_json()['version'] == version + 2


def test_a_patch_with_a_stale_version_is_a_409_that_writes_and_audits_nothing(stack):
    app, conn, client = stack()
    version = get_settings(client)['version']
    assert patch(client, {'map.zoom': 8}, version=version).status_code == 200
    changes, current = audit_count(conn, 'setting.update'), get_settings(client)['version']
    response = patch(client, {'map.zoom': 12, 'map.center_lat': 45.0}, version=version)
    body = response.get_json()
    assert response.status_code == 409
    assert body['code'] == 'settings_version_conflict' and body['version'] == current
    assert body['error'] == 'Un autre admin a modifié ces réglages : recharge la page avant d\'enregistrer.'
    store = app.config['SETTINGS_STORE']
    store.refresh(conn, force=True)
    assert store.get('map.zoom') == 8 and store.get('map.center_lat') == 46.603354
    assert audit_count(conn, 'setting.update') == changes and get_settings(client)['version'] == current


def test_a_delete_between_the_load_and_the_patch_makes_the_patch_a_409(stack):
    app, conn, client = stack()
    assert patch(client, {'map.zoom': 8}).status_code == 200
    loaded = get_settings(client)['version']
    deleted = call(client, 'delete', '/api/admin/settings/map.zoom')
    # DELETE renvoie la nouvelle version
    assert deleted.get_json()['version'] == loaded + 1
    response = patch(client, {'map.center_lat': 45.0}, version=loaded)
    assert response.status_code == 409 and response.get_json()['code'] == 'settings_version_conflict'
    assert response.get_json()['version'] == loaded + 1


def test_a_cli_settings_reset_between_the_load_and_the_patch_makes_the_patch_a_409(stack):
    app, conn, client = stack()
    assert patch(client, {'map.zoom': 8}).status_code == 200
    loaded = get_settings(client)['version']
    result = app.test_cli_runner().invoke(args=['settings-reset', 'map.zoom'])
    assert result.exit_code == 0, result.output
    response = patch(client, {'map.center_lat': 45.0}, version=loaded)
    assert response.status_code == 409 and response.get_json()['code'] == 'settings_version_conflict'


def test_a_write_by_the_store_between_the_load_and_the_patch_makes_the_patch_a_409(stack):
    app, conn, client = stack()
    loaded = get_settings(client)['version']
    app.config['SETTINGS_STORE'].set_many(conn, {'map.zoom': 11}, actor=CLI)                     # un autre processus (CLI, admin)
    response = patch(client, {'map.center_lat': 45.0}, version=loaded)
    assert response.status_code == 409 and response.get_json()['code'] == 'settings_version_conflict'
    assert get_settings(client)['settings'] is not None


def test_a_patch_without_version_is_accepted_without_control_in_both_forms(stack):
    app, conn, client = stack()
    assert patch(client, {'map.zoom': 7}).status_code == 200                                    # {settings: …} sans version
    assert call(client, 'patch', '/api/admin/settings', {'map.zoom': 8}).status_code == 200      # forme plate historique
    stale = get_settings(client)['version'] - 1
    # avec une version périmée : refusé
    assert patch(client, {'map.zoom': 9}, version=stale).status_code == 409
    assert patch(client, {'map.zoom': 9}).status_code == 200                                    # sans version : jamais refusé


@pytest.mark.parametrize('bad', [True, '3', 1.5, [1], {'a': 1}, -1.0])
def test_a_version_that_is_not_an_integer_is_a_400_and_writes_nothing(stack, bad):
    app, conn, client = stack()
    changes = audit_count(conn, 'setting.update')
    response = patch(client, {'map.zoom': 8}, version=bad)
    assert response.status_code == 400, (bad, response.get_data(as_text=True))
    assert audit_count(conn, 'setting.update') == changes
    store = app.config['SETTINGS_STORE']
    store.refresh(conn, force=True)
    assert store.get('map.zoom') == 6


def test_two_parallel_patches_carrying_the_same_version_give_one_200_and_one_409_never_two_writes(stack):
    app, conn, client = stack()
    for attempt in range(6):
        version = get_settings(client)['version']
        changes = audit_count(conn, 'setting.update')
        barrier, replies = threading.Barrier(2), []
        clients = [session_client(app, conn.execute("SELECT * FROM users WHERE username = 'root'").fetchone()) for _ in range(2)]

        def run(index, barrier, replies, clients, version):
            barrier.wait()
            replies.append((index, call(clients[index], 'patch', '/api/admin/settings',
                                        {'settings': {'map.zoom': 10 + index}, 'version': version}).status_code))

        threads = [threading.Thread(target=run, args=(i, barrier, replies, clients, version)) for i in range(2)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert sorted(code for _, code in replies) == [200, 409], (attempt, replies)
        assert audit_count(conn, 'setting.update') == changes + 1                                # une seule écriture
        assert get_settings(client)['version'] == version + 1
        winner = next(i for i, code in replies if code == 200)
        app.config['SETTINGS_STORE'].refresh(conn, force=True)
        assert app.config['SETTINGS_STORE'].get('map.zoom') == 10 + winner


def test_the_page_controls_a_patch_of_a_setting_changed_by_a_reset_is_not_hidden_by_the_cache(stack):
    """Le 409 compare la version EN BASE (dans la transaction), pas celle du cache du processus."""
    app, conn, client = stack()
    loaded = get_settings(client)['version']
    other = connect(app)
    try:
        other.execute("UPDATE settings_meta SET version = version + 1 WHERE id = 1")
        other.commit()
    finally:
        other.close()
    response = patch(client, {'map.zoom': 8}, version=loaded)
    assert response.status_code == 409 and response.get_json()['version'] == loaded + 1


def slip_a_third_party_write(app, key, value):
    """
    Glisse une écriture tierce ENTRE la validation de l'écriture de l'admin et le rafraîchissement du cache (L3-n1). Elle passe
    par une AUTRE connexion en SQL direct, comme un autre processus (CLI) : jamais par le store, dont l'état par fil d'exécution
    serait écrasé par un test qui écrit depuis le même fil.
    """
    store = app.config['SETTINGS_STORE']
    original = store._after_write
    fired = []

    def wrapper(conn):
        if not fired:
            fired.append(True)
            other = connect(app)
            try:
                other.execute("INSERT INTO settings (key, value, updated_at, updated_by) VALUES (?, ?, '2026-11-25T00:00:00Z', 'cli') "
                              "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, json.dumps(value)))
                other.execute("UPDATE settings_meta SET version = version + 1 WHERE id = 1")
                other.commit()
            finally:
                other.close()
        return original(conn)

    store._after_write = wrapper
    return fired


def test_patch_returns_the_version_of_its_own_write_even_when_a_third_party_write_slips_in_L3_n1(stack):
    app, conn, client = stack()
    version = get_settings(client)['version']
    fired = slip_a_third_party_write(app, 'map.center_lat', 44.0)
    response = patch(client, {'map.zoom': 8}, version=version)
    assert response.status_code == 200 and fired
    assert response.get_json()['version'] == version + 1, response.get_json()           # la sienne, pas version + 2
    assert get_settings(client)['version'] == version + 2                                # l'écriture tierce est bien passée
    stale = patch(client, {'map.zoom': 9}, version=response.get_json()['version'])
    assert stale.status_code == 409 and stale.get_json()['version'] == version + 2       # le client qui repart de sa version est prévenu


def test_delete_returns_the_version_of_its_own_write_even_when_a_third_party_write_slips_in_L3_n1(stack):
    app, conn, client = stack()
    assert patch(client, {'map.zoom': 8}).status_code == 200
    version = get_settings(client)['version']
    fired = slip_a_third_party_write(app, 'map.center_lat', 44.0)
    response = call(client, 'delete', '/api/admin/settings/map.zoom')
    assert response.status_code == 200 and fired
    assert response.get_json()['version'] == version + 1, response.get_json()
    assert get_settings(client)['version'] == version + 2


# --- CA-71 : restart_pending ---------------------------------------------------------------------------------------------------

def restart_setting():
    return Setting('test.restart', 'int', 5, 'Général', 'Un réglage à redémarrage', min=1, max=50, unit='', requires_restart=True)


def test_the_v1_registry_has_no_setting_that_requires_a_restart():
    assert [s.key for s in REGISTRY.values() if s.requires_restart] == []


def test_restart_pending_is_false_at_startup_true_after_a_patch_and_false_in_a_new_process(stack, tmp_path):
    directory = tmp_path / 'shared'
    app, conn, client = stack(extra=[restart_setting()], directory=directory)
    views = by_key(get_settings(client))
    assert views['test.restart']['restart_pending'] is False and views['test.restart']['requires_restart'] is True
    assert patch(client, {'test.restart': 9}).status_code == 200
    views = by_key(get_settings(client))
    assert views['test.restart']['restart_pending'] is True and views['test.restart']['value'] == 9
    assert all(v['restart_pending'] is False for k, v in views.items() if k != 'test.restart')   # jamais pour un autre réglage
    app2, conn2, client2 = stack(extra=[restart_setting()], directory=directory)                   # nouveau processus, même base
    assert by_key(get_settings(client2))['test.restart']['restart_pending'] is False
    assert by_key(get_settings(client2))['test.restart']['value'] == 9


def test_restart_pending_comes_back_to_false_when_the_value_returns_to_the_startup_one(stack):
    app, conn, client = stack(extra=[restart_setting()])
    patch(client, {'test.restart': 9})
    assert by_key(get_settings(client))['test.restart']['restart_pending'] is True
    patch(client, {'test.restart': 5})
    assert by_key(get_settings(client))['test.restart']['restart_pending'] is False


def test_a_setting_without_the_attribute_is_never_restart_pending(stack):
    app, conn, client = stack()
    assert patch(client, {'map.zoom': 9, 'DEM_TIMEOUT': 40}).status_code == 200
    assert not any(v['restart_pending'] for v in get_settings(client)['settings'])


# --- CA-79 : rendu, sécurité inchangée -----------------------------------------------------------------------------------------

ADMIN_JS = WEB_APP / 'static' / 'js' / 'admin-settings.js'
ADMIN_HTML = WEB_APP / 'templates' / 'admin.html'


def code_without_comments(text):
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.S)
    return re.sub(r'(?m)(^|[^:\'"`])//.*$', r'\1', text)


@pytest.mark.parametrize('path', [ADMIN_JS, WEB_APP / 'static' / 'js' / 'admin.js'], ids=lambda p: p.name)
def test_the_admin_scripts_never_write_markup_nor_open_a_browser_dialog(path):
    code = code_without_comments(path.read_text(encoding='utf-8'))
    for banned in ('innerHTML', 'outerHTML', 'insertAdjacentHTML', 'document.write', 'eval(', 'new Function', 'srcdoc',
                   'createContextualFragment', 'DOMParser'):
        assert banned not in code, (path.name, banned)
    assert not re.search(r'(?<![\w.])(?:window\.)?(?:confirm|alert|prompt)\s*\(', code), path.name
    assert not re.search(r"""setAttribute\(\s*['"]style['"]""", code) and 'cssText' not in code, path.name
    assert not re.search(r"""\bstyle\s*:\s*['"`]""", code), path.name                          # attribut style passé à App.el


def test_the_admin_page_has_no_inline_script_style_or_event_handler():
    html = ADMIN_HTML.read_text(encoding='utf-8')
    for script in re.findall(r'<script\b[^>]*>', html):
        assert 'src=' in script, script                                                         # aucun script en ligne
    assert not re.search(r'<script\b[^>]*>\s*[^<\s]', html) and '<style' not in html
    assert not re.search(r'\sstyle\s*=', html) and not re.search(r'\son[a-z]+\s*=', html)
    assert 'javascript:' not in html


def test_the_admin_page_is_served_with_a_csp_that_forbids_inline_script_and_style(stack):
    app, conn, client = stack()
    response = call(client, 'get', '/admin')
    assert response.status_code == 200
    csp = response.headers.get('Content-Security-Policy', '')
    directives = {d.split()[0]: d.split()[1:] for d in csp.split(';') if d.strip()}
    assert "'unsafe-inline'" not in directives.get('script-src', []) and "'unsafe-eval'" not in directives.get('script-src', [])
    assert directives.get('script-src') == ["'self'"]                                       # CSP des scripts inchangée
    body = response.get_data(as_text=True)
    assert 'js/admin-settings.js' in body and 'leaflet' in body
    assert response.headers.get('Cache-Control', '').find('no-store') >= 0


def test_the_img_src_csp_of_the_admin_page_still_lists_only_the_allowed_tile_hosts(stack):
    app, conn, client = stack(OUTBOUND_ALLOWED_HOSTS=f'{OUTBOUND},tiles=a.tiles.exemple.fr')
    csp = call(client, 'get', '/admin').headers.get('Content-Security-Policy', '')
    img = next(d for d in csp.split(';') if d.strip().startswith('img-src'))
    assert 'a.tiles.exemple.fr' in img and 'openstreetmap' not in img and '*' not in img.replace("'self'", '')


def control_map():
    js = ADMIN_JS.read_text(encoding='utf-8')
    block = re.search(r'CONTROL_FOR_TYPE = \{(.*?)\};', js, re.S)
    assert block, "CONTROL_FOR_TYPE absent de admin-settings.js"
    return js, dict(re.findall(r"(\w+): '([\w-]+)'", block.group(1)))


def test_every_registry_type_has_an_editor_and_none_goes_through_a_json_area_outside_the_advanced_pane():
    from app.settings.registry import TYPES
    js, mapping = control_map()
    assert set(TYPES) <= set(mapping), set(TYPES) - set(mapping)
    assert {s.type for s in REGISTRY.values()} <= set(mapping)
    assert not ({'json', 'textarea'} & set(mapping.values()))                                   # aucun type ne passe par du JSON
    assert mapping['bool'] == 'switch' and mapping['enum'] == 'select' and mapping['int'] == mapping['float'] == 'number'
    assert {mapping['str'], mapping['url'], mapping['email'], mapping['timezone']} == {'text'}
    assert mapping['list'] == 'chips' and mapping['basemaps'] == 'basemap-cards' and mapping['polygon']
    assert 'rectangle' not in mapping and 'rectangle' not in TYPES and 'rectangle-fields' not in js          # lot Z1 : le type et son éditeur sont supprimés
    code = code_without_comments(js)
    areas = [m.start() for m in re.finditer(r"""el\(\s*['"]textarea['"]""", code)]
    assert len(areas) == 1, "une seule zone de texte (le volet « Avancé »)"
    start = code.index('function advancedPane')
    end = code.index('\n    // ', start) if '\n    // ' in code[start:] else len(code)
    assert start < areas[0] < start + (end - start if end > start else len(code))
    for kind in ('basemaps', 'polygon'):
        # le volet ne sert qu'à ces deux types (lot Z1 : le rectangle n'existe plus)
        assert f"setting.type === '{kind}'" in code or kind in code


def test_the_settings_page_sends_the_loaded_version_with_every_patch_and_never_without():
    code = code_without_comments(ADMIN_JS.read_text(encoding='utf-8'))
    patches = re.findall(r"method:\s*'PATCH'[^)]*", code)
    assert patches and all('version' in p for p in patches), patches


def test_the_page_never_writes_a_setting_value_in_the_url_or_the_browser_storage():
    code = code_without_comments(ADMIN_JS.read_text(encoding='utf-8'))
    assert 'localStorage' not in code and 'sessionStorage' not in code and 'indexedDB' not in code and 'document.cookie' not in code
    for call_ in re.findall(r'(?:history\.(?:replaceState|pushState)|location\.(?:hash|href|search)\s*=)[^;]*;', code):
        assert 'S.search' not in call_ and 'current(' not in call_ and 'edits' not in call_, call_
    assert 'beforeunload' in code or 'onbeforeunload' in code                                    # avertissement natif (EF-6.14)


# --- CA-80 : banc de test navigateur -------------------------------------------------------------------------------------------

BENCH = WEB_APP / 'tests_browser'
IMAGE_RE = re.compile(r'mcr\.microsoft\.com/playwright/python:v(\d+\.\d+\.\d+)-[a-z]+@sha256:[0-9a-f]{64}')


def test_the_bench_readme_gives_one_command_and_references_the_image_by_version_and_digest():
    readme = (BENCH / 'README.md').read_text(encoding='utf-8')
    fences = re.findall(r'```(?:bash|sh)?\n(.*?)```', readme, re.S)
    commands = [line for block in fences for line in block.splitlines() if line.strip() and not line.strip().startswith('#')]
    assert len(commands) == 1 and commands[0].strip().endswith('run.sh'), commands                # une seule commande documentée
    assert IMAGE_RE.search(readme), "l'image doit être citée avec sa version ET son digest sha256"
    assert 'latest' not in readme.lower().replace('lastest', '')


def test_the_playwright_image_is_pinned_by_version_and_digest_everywhere():
    found = set()
    for path in sorted(BENCH.iterdir()):
        if not path.is_file():
            continue
        text = path.read_text(encoding='utf-8', errors='ignore')
        assert ':latest' not in text, path.name
        if path.name.startswith('Dockerfile'):
            froms = re.findall(r'(?m)^FROM\s+(\S+)', text)
            assert froms and all('@sha256:' in image for image in froms), (path.name, froms)    # tout FROM est épinglé par digest
        found |= {m.group(0) for m in IMAGE_RE.finditer(text)}
    # une seule version et un seul digest
    assert len(found) == 1, found


def test_the_bench_python_requirements_are_hashed_and_match_the_image_version():
    requirements = BENCH / 'requirements.txt'
    assert requirements.exists(), "tests_browser/requirements.txt (épinglé avec empreintes) est exigé par EF-6.17"
    text = requirements.read_text(encoding='utf-8')
    pinned = re.findall(r'(?m)^([A-Za-z0-9_.\-]+)==([^\s\\;]+)', text)
    assert pinned
    for name, _ in pinned:
        block = re.search(rf'(?ms)^{re.escape(name)}==.*?(?=^\S|\Z)', text).group(0)
        assert '--hash=sha256:' in block, f"{name} sans empreinte"
    assert not re.search(r'(?m)^[A-Za-z0-9_.\-]+\s*(>=|<=|~=|>|<|$)', text), "dépendance non épinglée"
    version = dict(pinned).get('playwright')
    assert version, "playwright absent de requirements.txt"
    image_versions = {m for m in IMAGE_RE.findall((BENCH / 'Dockerfile.playwright').read_text(encoding='utf-8'))}
    assert image_versions == {version}, (image_versions, version)                                   # même version que l'image
    assert '--require-hashes' in (BENCH / 'Dockerfile.playwright').read_text(encoding='utf-8')
    assert 'requirements.txt' in (BENCH / 'Dockerfile.playwright').read_text(encoding='utf-8')


def test_pytest_does_not_collect_the_bench_and_the_bench_is_outside_the_dev_requirements():
    parser = configparser.ConfigParser()
    parser.read(WEB_APP / 'pytest.ini')
    section = parser['pytest']
    assert 'tests_browser' in section.get('norecursedirs', '') or section.get('testpaths', '').split() == ['tests']
    assert 'tests_browser' not in section.get('testpaths', '')
    assert 'playwright' not in (WEB_APP / 'requirements-dev.txt').read_text(encoding='utf-8').lower()
    assert 'playwright' not in (WEB_APP / 'requirements.txt').read_text(encoding='utf-8').lower()


def test_a_collect_only_run_from_web_app_as_in_the_ci_collects_nothing_from_the_bench():
    result = subprocess.run([sys.executable, '-m', 'pytest', '--collect-only', '-q', '-p', 'no:cacheprovider'],
                            cwd=WEB_APP, capture_output=True, text=True, timeout=600)
    assert result.returncode == 0, result.stdout[-500:] + result.stderr[-500:]
    assert 'tests_browser' not in result.stdout and 'tests_browser' not in result.stderr
    # le test de ce fichier est bien collecté
    assert 'test_lot3_settings_edge.py' in result.stdout


def test_the_ci_workflow_mentions_neither_the_bench_nor_playwright():
    workflows = WEB_APP.parent / '.github' / 'workflows'
    if not workflows.is_dir():
        pytest.skip("workflows absents (hors web-app)")
    text = '\n'.join(p.read_text(encoding='utf-8') for p in workflows.glob('*.y*ml')).lower()
    assert 'tests_browser' not in text and 'playwright' not in text


def test_the_bench_blocks_and_records_every_request_to_a_foreign_host():
    scenarios = (BENCH / 'scenarios.py').read_text(encoding='utf-8')
    assert '.route(' in scenarios, "toute requête vers un autre hôte doit être interceptée (page.route)"
    assert 'abort' in scenarios and 'blocked' in scenarios.lower() or 'bloqu' in scenarios.lower()
    assert 'intercepted' in scenarios.lower() or 'intercept' in scenarios.lower()
