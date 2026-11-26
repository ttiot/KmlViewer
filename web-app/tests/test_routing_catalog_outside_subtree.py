"""E1 (ops, index Geofabrik réel) : les entrées hors du sous-arbre racine (ids avec « / », `us/alabama`…) sont ignorées, pas refusées ;
un identifiant invalide ou double DANS le sous-arbre reste refusé, côté application (catalog.build) comme côté hôte (candidates_of)."""
import copy
import importlib.util
import json
from pathlib import Path

import pytest

from app.services.routing import catalog

ROOT = Path(__file__).resolve().parents[2]
HOSTS = ['download.geofabrik.de']
INDEX = json.loads((Path(__file__).parent / 'fixtures/routing/geofabrik-index-frozen.json').read_text())


@pytest.fixture(scope='module')
def zu():
    spec = importlib.util.spec_from_file_location('zone_update_e1', ROOT / 'routing/zone_update.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def realistic():
    document = copy.deepcopy(INDEX)
    document['features'] += [{'properties': {'id': 'north-america', 'name': 'North America'}, 'geometry': None}] + [
        {'properties': {'id': f'us/{name}', 'parent': 'north-america',
                        'urls': {'pbf': f'https://download.geofabrik.de/north-america/us/{name}-latest.osm.pbf'}}, 'geometry': None}
        for name in ('alabama', 'alaska', 'wyoming')]
    return document


def feature(rid, parent):
    return {'properties': {'id': rid, 'parent': parent, 'urls': {'pbf': f'https://download.geofabrik.de/europe/{rid}-latest.osm.pbf'}},
            'geometry': None}


def test_entries_outside_the_root_subtree_are_ignored_by_the_host(zu):
    reference = zu.candidates_of(copy.deepcopy(INDEX), 'europe', [], HOSTS)[0]
    candidates = zu.candidates_of(realistic(), 'europe', [], HOSTS)[0]
    assert candidates == reference and not any('/' in rid for rid in candidates)


def test_entries_outside_the_root_subtree_are_ignored_by_the_application():
    reference = catalog.build(copy.deepcopy(INDEX), root='europe', exclude=[], allowed_hosts=HOSTS)
    assert catalog.build(realistic(), root='europe', exclude=[], allowed_hosts=HOSTS) == reference


@pytest.mark.parametrize('rid', ['a/b', 'Bad', 'x' * 70])
def test_an_invalid_identifier_inside_the_subtree_is_still_refused(zu, rid):
    document = realistic()
    document['features'].append(feature(rid, 'france'))
    with pytest.raises(ValueError):
        zu.candidates_of(document, 'europe', [], HOSTS)
    with pytest.raises(catalog.CatalogError):
        catalog.build(document, root='europe', exclude=[], allowed_hosts=HOSTS)


def test_a_duplicate_identifier_inside_the_subtree_is_still_refused(zu):
    document = realistic()
    document['features'].append(copy.deepcopy(next(f for f in INDEX['features'] if f['properties']['id'] == 'belgium')))
    with pytest.raises(ValueError):
        zu.candidates_of(document, 'europe', [], HOSTS)
    with pytest.raises(catalog.CatalogError):
        catalog.build(document, root='europe', exclude=[], allowed_hosts=HOSTS)


def test_a_duplicate_outside_the_subtree_is_ignored(zu):
    document = realistic()
    document['features'].append(copy.deepcopy(document['features'][-1]))
    assert zu.candidates_of(document, 'europe', [], HOSTS)[0]
    assert catalog.build(document, root='europe', exclude=[], allowed_hosts=HOSTS)


def test_a_cycle_outside_is_ignored_and_a_non_string_identifier_refused(zu):
    document = realistic()
    document['features'].append(feature('loop', 'loop'))
    assert catalog.build(document, root='europe', exclude=[], allowed_hosts=HOSTS)          # hors sous-arbre : ignoré
    document = realistic()
    document['features'].append({'properties': {'id': 42, 'parent': 'france'}, 'geometry': None})
    with pytest.raises(ValueError):
        zu.candidates_of(document, 'europe', [], HOSTS)
    with pytest.raises(catalog.CatalogError):
        catalog.build(document, root='europe', exclude=[], allowed_hosts=HOSTS)


@pytest.mark.parametrize('rid', ['france', 'belgium'])
def test_a_hostile_duplicate_placed_first_with_a_parent_outside_the_subtree_is_refused(zu, rid):
    """E1-M1 : l'ordre du document ne change rien ; un doublon rattaché hors du sous-arbre ne retire pas la branche en silence."""
    document = realistic()
    document['features'].insert(0, {'properties': {'id': rid, 'parent': 'nowhere'}, 'geometry': None})
    with pytest.raises(ValueError):
        zu.candidates_of(document, 'europe', [], HOSTS)
    with pytest.raises(catalog.CatalogError):
        catalog.build(document, root='europe', exclude=[], allowed_hosts=HOSTS)
