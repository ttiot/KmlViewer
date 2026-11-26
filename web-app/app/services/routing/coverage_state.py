"""
État de la couverture pilotée par la zone (routing-zone-donnees ZD-2, ZD-3, ZD-4, ZD-10, ZD-13, ADR-0013).

Une ligne en base (`routing_coverage_state`) : zone ACTIVE (graphe et dalles en service), statut unique, demande en cours.
Toutes les fonctions travaillent sur une connexion, DANS une transaction (`write_transaction`), avec une horloge injectée
(`now`, datetime UTC) : aucune ne lit l'heure, l'environnement ni le réseau. La zone demandée (le réglage) est passée par
l'appelant (`requested`). Un seul statut, une seule zone active ; l'acceptation des points lit `accepted_zone`.

Audit : acteur `system`, une entrée par CHANGEMENT (jamais par passage).
"""

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Sequence

from app import audit
from app.db import write_transaction
from app.services.routing import catalog as catalog_module
from app.services.routing import coverage, zone

logger = logging.getLogger(__name__)

STATUSES = ('idle', 'pending', 'building', 'starting', 'failed')
REASONS = ('zone', 'mensuel', 'activation')
FAILURE_CODES = ('catalog', 'cap', 'budget', 'download', 'merge', 'extract', 'import', 'incomplete', 'dem', 'disk', 'timeout',
                 'health', 'other')
CLAIM_EXPIRY = timedelta(hours=2)
STUCK_AFTER = CLAIM_EXPIRY                       # « Tâche bloquée ? » (ZD-12)
GRAPH_NAME = re.compile(r'graph-\d{14}')
MAX_ZONE_JSON_BYTES = 64 * 1024
MAX_NEW_TILES = 2000
_SYSTEM = audit.Actor.system


class CoverageError(ValueError):
    """Argument ou contenu invalide (code de sortie 2) : rien n'est écrit."""


class StaleRequest(Exception):
    """Demande dépassée ou inconnue (code de sortie 3)."""


class PlanRefused(ValueError):
    """Zone refusée à l'écriture (ZD-4) : le message est affichable (400) ; `code` = code d'échec (`catalog` ou `cap`)."""

    def __init__(self, message: str, code: str = 'cap'):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Limits:
    max_uncovered_pct: int
    max_regions: int
    max_source_bytes: int
    max_tiles: int
    settle_s: int

    @classmethod
    def from_config(cls, cfg) -> 'Limits':
        return cls(cfg['ROUTING_MAX_UNCOVERED_PCT'], cfg['ROUTING_MAX_REGIONS'], cfg['ROUTING_MAX_SOURCE_BYTES'],
                   cfg['DEM_MAX_TILES'], cfg['ROUTING_ZONE_SETTLE_S'])


@dataclass(frozen=True)
class Plan:
    """Ce que la zone demande : régions, octets, dalles, emprise d'extraction (ZD-4 à ZD-7)."""

    vertices: list
    extract_bbox: list
    regions: tuple
    tiles: list
    uncovered_pct: float

    @property
    def region_ids(self) -> list:
        return [r.id for r in self.regions]

    @property
    def total_bytes(self) -> int:
        return sum(r.size for r in self.regions)


def enabled(cfg) -> bool:
    return bool(cfg.get('ROUTING_ZONE_BUILD'))


def _iso(now: datetime) -> str:
    return now.strftime('%Y-%m-%dT%H:%M:%SZ')


def _parse(text: Optional[str]) -> Optional[datetime]:
    try:
        return datetime.strptime(text, '%Y-%m-%dT%H:%M:%SZ') if text else None
    except ValueError:
        return None


parse_time = _parse


def _loads(text, default=None):
    try:
        return json.loads(text) if text else default
    except (ValueError, RecursionError):
        return default


def read(conn) -> dict:
    """La ligne d'état, champs JSON décodés ; créée « sources historiques » si absente."""
    row = conn.execute("SELECT * FROM routing_coverage_state WHERE id = 1").fetchone()
    if row is None:
        with write_transaction(conn):
            conn.execute("INSERT OR IGNORE INTO routing_coverage_state (id) VALUES (1)")
        row = conn.execute("SELECT * FROM routing_coverage_state WHERE id = 1").fetchone()
    state = dict(row)
    for key, default in (('active_polygon', None), ('active_extract_bbox', None), ('active_regions', []),
                         ('task_tiles', []), ('request_polygon', None), ('claim_payload', None)):
        state[key] = _loads(state.get(key), default)
    return state


def _set(conn, **fields) -> None:
    columns = ', '.join(f'{k} = ?' for k in fields)
    conn.execute(f"UPDATE routing_coverage_state SET {columns} WHERE id = 1",
                 [json.dumps(v, separators=(',', ':')) if isinstance(v, (list, dict)) else v for v in fields.values()])


def _audit(conn, action: str, **details) -> None:
    audit.record(conn, action, actor=_SYSTEM(), target_type='routing', target_id='coverage', details=details)


def _same_zone(a, b) -> bool:
    if not a or not b or len(a) != len(b):
        return False
    return all(len(p) == 2 and len(q) == 2 and float(p[0]) == float(q[0]) and float(p[1]) == float(q[1]) for p, q in zip(a, b))


# -- Acceptation des points ----------------------------------------------------------------------------------------------

def accepted_zone(conn, cfg, requested: list) -> list:
    """
    Zone qui accepte ou refuse les points (inventaire n° 1 à 4) : la zone ACTIVE ; avant la première bascule (« sources
    historiques »), la zone demandée, comme au lot Z1. `ROUTING_ZONE_BUILD=0` : toujours la zone demandée.
    """
    if not enabled(cfg):
        return requested
    active = read(conn)['active_polygon']
    return active if active else requested


# -- Plan (ZD-4) ----------------------------------------------------------------------------------------------------------

def load_catalog(conn) -> Optional[list]:
    """Régions candidates prêtes pour le calcul, ou None si aucun catalogue n'est chargé."""
    row = conn.execute("SELECT content, sha256 FROM routing_catalog WHERE id = 1").fetchone()
    return catalog_module.prepare(row['content'], row['sha256']) if row else None


def _go(size: int) -> str:
    return f"{size / 1e9:.1f}".replace('.', ',') + ' Go'


def _gio(size: int) -> str:
    value = size / 2 ** 30
    return (f"{value:.0f}" if value == int(value) else f"{value:.1f}".replace('.', ',')) + ' Gio'


def _names(ids: Sequence) -> str:
    return ', '.join(ids[:3]) + (', …' if len(ids) > 3 else '')


def plan_zone(vertices: list, candidates: Optional[list], limits: Limits, margin_km: int) -> Plan:
    """Contrôles de ZD-4 dans l'ordre (catalogue, couverture, plafonds) ; lève PlanRefused avec un message chiffré."""
    if not candidates:
        raise PlanRefused("Zone : catalogue des données pas encore chargé, réessaie dans quelques minutes.", 'catalog')
    resolution = coverage.resolve_regions(vertices, candidates)
    if resolution.uncovered_pct > limits.max_uncovered_pct:
        raise PlanRefused(f"Zone : {resolution.uncovered_pct:.0f} % de la zone hors des données OSM (mer ou hors d'Europe).")
    bbox = coverage.extract_bbox(vertices, margin_km)
    tiles = coverage.dem_tiles(bbox)
    ids = resolution.ids
    if len(ids) > limits.max_regions:
        raise PlanRefused(f"Zone trop grande : {len(ids)} régions ({_names(ids)}) pour {limits.max_regions} au plus.")
    if resolution.total_bytes > limits.max_source_bytes:
        raise PlanRefused(f"Zone trop grande : {_go(resolution.total_bytes)} d'extraits ({len(ids)} régions : {_names(ids)}) "
                          f"pour {_gio(limits.max_source_bytes)} au plus.")
    if len(tiles) > limits.max_tiles:
        raise PlanRefused(f"Zone trop grande : {len(tiles)} dalles d'altitude pour {limits.max_tiles} au plus.")
    return Plan(vertices, bbox, resolution.regions, tiles, resolution.uncovered_pct)


def preview(conn, cfg, vertices: list, margin_km: int) -> dict:
    """Aperçu pour la confirmation (ZD-4.4) : régions ajoutées et retirées, octets à télécharger, dalles nouvelles."""
    plan = plan_zone(vertices, load_catalog(conn), Limits.from_config(cfg), margin_km)
    state = read(conn)
    before = set(state['active_regions'] or [])
    added = [r for r in plan.regions if r.id not in before]
    tiles_before = {tuple(t) for t in (state['task_tiles'] or [])}
    return {'regions': plan.region_ids, 'added': [r.id for r in added], 'removed': sorted(before - set(plan.region_ids)),
            # « octets hors cache » : l'application ignore le cache de l'hôte, elle donne les octets des régions AJOUTÉES
            'download_bytes': sum(r.size for r in added), 'total_bytes': plan.total_bytes,
            'new_tiles': len([t for t in plan.tiles if tuple(t) not in tiles_before]), 'tiles': len(plan.tiles),
            'extract_bbox': plan.extract_bbox, 'settle_s': cfg['ROUTING_ZONE_SETTLE_S']}


# -- Statut ---------------------------------------------------------------------------------------------------------------

def _recalc(conn, requested: list, now: datetime) -> str:
    """Après `switching` en code 3, `failed` d'une demande dépassée, `done` : `idle` si demandée = active, sinon `pending`."""
    state = read(conn)
    status = 'idle' if _same_zone(requested, state['active_polygon']) else 'pending'
    _set(conn, status=status, reason=None if status == 'idle' else state['reason'] or 'zone', status_at=_iso(now),
         claim_id=None, claimed_at=None, claim_payload=None)
    return status


def register_request(conn, cfg, requested: list, now: datetime, reason: str = 'zone') -> None:
    """
    Zone enregistrée (écriture du réglage, « Revenir au défaut » ou à la zone active) : nouvelle demande. Pendant `building`
    ou `starting`, le statut ne change pas, la demande en cours devient dépassée (le numéro suffit). Sans effet si
    `ROUTING_ZONE_BUILD=0`.
    """
    if not enabled(cfg):
        return
    with write_transaction(conn):
        state = read(conn)
        _set(conn, request_id=state['request_id'] + 1, request_polygon=requested, request_reason=reason, requested_at=_iso(now))
        if state['status'] in ('building', 'starting'):
            return
        if _same_zone(requested, state['active_polygon']):
            _set(conn, status='idle', reason=None, status_at=_iso(now), claim_id=None, claimed_at=None, claim_payload=None)
        else:
            _set(conn, status='pending', reason=reason, status_at=_iso(now), claim_id=None, claimed_at=None, claim_payload=None,
                 last_failure_code=None if state['status'] == 'failed' else state['last_failure_code'])


def ensure_activation(conn, cfg, requested: list, now: datetime) -> bool:
    """
    Premier démarrage avec `ROUTING_ZONE_BUILD=1` (ZD-1) : zone active « sources historiques » ou absente et aucune demande ⇒
    demande pour la zone demandée, motif `activation`, `pending`. Vrai si une demande a été créée.
    """
    if not enabled(cfg):
        return False
    with write_transaction(conn):                     # plusieurs processus au démarrage : une seule demande
        state = read(conn)
        if state['active_polygon'] is not None or state['request_id'] > 0 or state['status'] != 'idle':
            return False
        register_request(conn, cfg, requested, now, reason='activation')
        _audit(conn, 'routing.request', reason='activation')
    return True


# -- Sous-commandes de l'hôte (ZD-10) -------------------------------------------------------------------------------------

def load_catalog_document(conn, raw: bytes, cfg, now: datetime) -> dict:
    """`catalog -` : catalogue contrôlé puis remplacé en une transaction ; invalide ⇒ CoverageError, l'ancien est gardé."""
    try:
        regions = catalog_module.build(catalog_module.parse_bytes(raw), root=cfg['ROUTING_CATALOG_ROOT'],
                                       exclude=cfg['ROUTING_CATALOG_EXCLUDE'], allowed_hosts=cfg['ROUTING_ALLOWED_HOSTS'])
    except catalog_module.CatalogError as exc:
        raise CoverageError(str(exc)) from None
    text, digest = catalog_module.serialize(regions)
    with write_transaction(conn):
        before = conn.execute("SELECT sha256 FROM routing_catalog WHERE id = 1").fetchone()
        _set(conn, catalog_at=_iso(now))
        if before is None or before['sha256'] != digest:       # même empreinte : contenu non réécrit (ZD-2), seule la date change
            conn.execute("INSERT INTO routing_catalog (id, content, sha256, loaded_at) VALUES (1, ?, ?, ?) "
                         "ON CONFLICT(id) DO UPDATE SET content = excluded.content, sha256 = excluded.sha256, "
                         "loaded_at = excluded.loaded_at", (text, digest, _iso(now)))
            _audit(conn, 'routing.catalog_loaded', regions=len(regions))
    return {'regions': len(regions)}


def claim(conn, cfg, requested: list, margin_km: int, now: datetime, monthly: bool = False) -> dict:
    """
    `claim [--monthly]` : prend la demande selon ZD-9.3 ⇒ `building`, sortie `{request_id, reason, polygon, extract_bbox, regions,
    dem_tiles, margin_km}` ; sinon `{}`. Jamais une demande `failed`. Une prise de plus de 2 h expire (reprise).
    """
    limits = Limits.from_config(cfg)
    with write_transaction(conn):
        state = read(conn)
        claimed_at = _parse(state['claimed_at'])
        if state['status'] == 'building' and claimed_at and now - claimed_at >= CLAIM_EXPIRY:
            _audit(conn, 'routing.claim_expired', request_id=state['claim_id'])
            _set(conn, status='pending', claim_id=None, claimed_at=None, claim_payload=None, status_at=_iso(now))
            state = read(conn)
        if state['status'] not in ('idle', 'pending'):
            return {}
        reason = None
        if state['status'] == 'pending':
            asked = _parse(state['requested_at'])
            if asked is None or (now - asked).total_seconds() < limits.settle_s:
                return {}                              # une saisie récente n'est jamais prise, mensuelle comprise
            polygon, reason = state['request_polygon'] or requested, 'mensuel' if monthly else (state['reason'] or 'zone')
        elif monthly:
            polygon, reason = state['active_polygon'] or requested, 'mensuel'
        else:
            return {}
        try:
            plan = plan_zone(polygon, load_catalog(conn), limits, margin_km)
        except PlanRefused as exc:
            if state['status'] == 'pending' or monthly:
                if exc.code == 'cap':
                    _set(conn, status='failed', reason=state['reason'], status_at=_iso(now), last_failure_code='cap')
                    _audit(conn, 'routing.failed', failure='cap', request_id=state['request_id'])
            return {}
        request_id = state['request_id'] + 1 if state['status'] == 'idle' else state['request_id']
        payload = {'request_id': request_id, 'reason': reason, 'polygon': plan.vertices, 'extract_bbox': plan.extract_bbox,
                   'regions': plan.region_ids,                    # identifiants seulement : l'hôte lit ses URL dans sa copie
                   'dem_tiles': [list(t) for t in plan.tiles], 'margin_km': margin_km}
        _set(conn, status='building', reason=reason, status_at=_iso(now), claim_id=request_id, claimed_at=_iso(now),
             claim_payload=payload, request_id=request_id,
             request_polygon=state['request_polygon'] or polygon, requested_at=state['requested_at'] or _iso(now))
        _audit(conn, 'routing.claim', request_id=request_id, reason=reason, regions=len(plan.regions))
        return payload


def _check_id(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise CoverageError("identifiant de demande invalide")
    return value


def switching(conn, request_id: int, requested: list, now: datetime) -> None:
    """`switching ID` : demande encore la dernière ⇒ `starting` ; sinon recalcul et StaleRequest (sortie 3)."""
    _check_id(request_id)
    with write_transaction(conn):
        state = read(conn)
        if state['claim_id'] == request_id and state['status'] == 'starting':
            return                                       # rejoué : déjà basculé, le graphe neuf ne doit PAS être supprimé
        if state['claim_id'] == request_id and state['status'] == 'building':
            if state['request_id'] == request_id:
                _set(conn, status='starting', status_at=_iso(now))
                _audit(conn, 'routing.switching', request_id=request_id)
                return
            _audit(conn, 'routing.request_dropped', request_id=request_id)
            _recalc(conn, requested, now)
    raise StaleRequest()                         # hors de la transaction : le recalcul ci-dessus est validé


def _tiles(value) -> list:
    if not isinstance(value, list) or len(value) > MAX_NEW_TILES:
        raise CoverageError("dalles : liste attendue")
    out = []
    for tile in value:
        if (not isinstance(tile, list) or len(tile) != 2 or any(isinstance(n, bool) or not isinstance(n, int) for n in tile)
                or not (-90 <= tile[0] <= 90 and -180 <= tile[1] <= 180)):
            raise CoverageError("dalle invalide")
        out.append(tile)
    return out


def done(conn, request_id: int, graph: str, data_date: str, new_tiles, requested: list, now: datetime) -> dict:
    """
    `done ID --graph NOM --data-date AAAA-MM-JJ --new-tiles -` : zone, emprise, régions et dalles de la tâche = celles de la
    demande (prises au `claim`) ; recalcul ; des dalles ajoutées ⇒ marqueur de réveil du MNT. `new_tiles` : liste de dalles
    ajoutées ou `{"added": [...], "purged": [...], "purged_extracts": N}` (les purges sont auditées, en nombres).
    """
    _check_id(request_id)
    if not isinstance(graph, str) or not GRAPH_NAME.fullmatch(graph):
        raise CoverageError("nom de graphe invalide")
    try:
        datetime.strptime(data_date, '%Y-%m-%d')
    except (TypeError, ValueError):
        raise CoverageError("date invalide") from None
    added = _tiles(new_tiles['added'] if isinstance(new_tiles, dict) and 'added' in new_tiles else new_tiles)
    purged = _tiles(new_tiles.get('purged', [])) if isinstance(new_tiles, dict) else []
    purged_extracts = new_tiles.get('purged_extracts', 0) if isinstance(new_tiles, dict) else 0
    if isinstance(purged_extracts, bool) or not isinstance(purged_extracts, int) or not 0 <= purged_extracts <= 10_000:
        raise CoverageError("extraits purgés : entier attendu")
    with write_transaction(conn):
        state = read(conn)
        payload = state['claim_payload']
        if state['claim_id'] != request_id or state['status'] not in ('building', 'starting') or not payload:
            raise StaleRequest()
        gone = {tuple(t) for t in purged}
        tiles = [t for t in state['task_tiles'] if tuple(t) not in gone]
        known = {tuple(t) for t in tiles}
        tiles += [t for t in added if tuple(t) not in known]
        _set(conn, active_polygon=payload['polygon'], active_extract_bbox=payload['extract_bbox'],
             active_regions=list(payload['regions']), active_graph=graph, data_date=data_date, task_tiles=tiles,
             last_failure_code=None, dem_wakeup=1 if added else state['dem_wakeup'],
             last_report_hash=_report_hash(payload['polygon'], graph))
        _audit(conn, 'routing.done', request_id=request_id, regions=list(payload['regions']), tiles_added=len(added))
        if purged or purged_extracts:                    # ZD-13 : une entrée par changement, des nombres seulement (aucun chemin)
            _audit(conn, 'routing.purged', request_id=request_id, tiles=len(purged), extracts=purged_extracts)
        status = _recalc(conn, requested, now)
    return {'status': status}


def failed(conn, request_id: int, code: str, requested: list, now: datetime) -> dict:
    """`failed ID --code CODE` : dernière demande ⇒ `failed` ; dépassée ⇒ recalcul. L'actif ne change jamais."""
    _check_id(request_id)
    if code not in FAILURE_CODES:
        raise CoverageError("code d'échec inconnu")
    with write_transaction(conn):
        state = read(conn)
        if state['claim_id'] != request_id or state['status'] not in ('building', 'starting'):
            raise StaleRequest()
        if state['request_id'] == request_id:
            _set(conn, status='failed', status_at=_iso(now), last_failure_code=code, claim_id=None, claimed_at=None,
                 claim_payload=None)
            _audit(conn, 'routing.failed', request_id=request_id, failure=code)       # `*code` serait masqué par l'audit
            return {'status': 'failed'}
        _audit(conn, 'routing.request_dropped', request_id=request_id, failure=code)
        return {'status': _recalc(conn, requested, now)}


def _report_hash(polygon, graph) -> str:
    return hashlib.sha256(json.dumps([polygon, graph], separators=(',', ':')).encode()).hexdigest()


def parse_zone_json(raw: bytes) -> dict:
    """`zone.json` rapporté par l'hôte : taille bornée AVANT toute analyse, zone contrôlée par EF-6.20 ; CoverageError sinon."""
    if len(raw) > MAX_ZONE_JSON_BYTES:
        raise CoverageError("zone.json trop volumineux")
    try:
        data = json.loads(raw)
        vertices = zone.validate(data['polygon'])
        bbox = data['extract_bbox']
        if (not isinstance(bbox, list) or len(bbox) != 4 or not all(isinstance(n, (int, float)) and not isinstance(n, bool)
                                                                    and abs(n) <= 180 for n in bbox)
                or not (bbox[0] < bbox[2] and bbox[1] < bbox[3])):
            raise ValueError("emprise")
        regions = data['regions']
        valid_id = re.compile(r'[a-z0-9_-]{1,64}')
        if not isinstance(regions, list) or len(regions) > 50 or not all(isinstance(r, str) and valid_id.fullmatch(r)
                                                                          for r in regions):
            raise ValueError("régions")
        return {'polygon': vertices, 'extract_bbox': [float(n) for n in bbox], 'regions': regions}
    except (ValueError, KeyError, TypeError, RecursionError):
        raise CoverageError("zone.json invalide") from None


def report(conn, graph: str, zone_info: Optional[dict], historic: bool, budget: Optional[tuple], requested: list,
           now: datetime) -> dict:
    """
    `report` : zone du graphe en service (ou « sources historiques »). Différente de l'active ⇒ réalignement et audit, UNE
    fois par changement ; invalide ⇒ ignorée (le WARNING et l'alerte sont dédoublonnés : une par contenu). Compteurs du budget
    du jour rapportés par l'hôte : `budget = (tâches, octets)`.
    """
    if not isinstance(graph, str) or not GRAPH_NAME.fullmatch(graph):
        raise CoverageError("nom de graphe invalide")
    if budget is not None and (len(budget) != 2 or any(isinstance(n, bool) or not isinstance(n, int) or n < 0 for n in budget)):
        raise CoverageError("budget invalide")
    with write_transaction(conn):
        state = read(conn)
        if budget is not None:
            day = now.strftime('%Y-%m-%d')
            _set(conn, budget_day=day, budget_builds=budget[0], budget_bytes=budget[1])
        if state['invalid_report_hash']:                  # un rapport VALIDE : la cause de l'alerte a disparu (ZD-12)
            _set(conn, invalid_report_hash=None)
        digest = _report_hash(None if historic else zone_info['polygon'], graph)
        if digest == state['last_report_hash']:
            return {'realigned': False}
        realigned = False
        if historic:
            if state['active_polygon'] is not None:
                _set(conn, active_polygon=None, active_extract_bbox=None, active_regions=[], active_graph=graph, task_tiles=[])
                realigned = True
        elif not _same_zone(zone_info['polygon'], state['active_polygon']) or state['active_graph'] != graph:
            _set(conn, active_polygon=zone_info['polygon'], active_extract_bbox=zone_info['extract_bbox'],
                 active_regions=zone_info['regions'], active_graph=graph)
            realigned = True
        _set(conn, last_report_hash=digest, invalid_report_hash=None)
        if realigned:
            _audit(conn, 'routing.realigned', graph=graph, historic=bool(historic))
            if state['status'] in ('idle', 'pending', 'starting'):
                _recalc(conn, requested, now)
    return {'realigned': realigned}


def report_invalid(conn, content: bytes) -> bool:
    """Contenu rapporté invalide : vrai si c'est la PREMIÈRE fois (WARNING et alerte dédoublonnés par contenu)."""
    digest = hashlib.sha256(content).hexdigest()
    with write_transaction(conn):
        if read(conn)['invalid_report_hash'] == digest:
            return False
        _set(conn, invalid_report_hash=digest)
    return True


def alerts(conn, cfg) -> list:
    """
    Bandeaux de l'admin (ZD-12) : UNE alerte par cause, tirée de l'état (jamais accumulée) et donc effacée quand la cause
    disparaît. Ni coordonnée, ni valeur de réglage.
    """
    if not enabled(cfg):
        return []
    state, out = read(conn), []
    if state['status'] == 'failed':
        code = state['last_failure_code'] or 'other'
        message = ("La zone dépasse les plafonds : réduis-la ou relève les plafonds (exploitation)." if code == 'cap'
                   else f"La mise à jour de la couverture a échoué (code {code}) : la couverture actuelle reste en service.")
        out.append({'code': 'coverage_failed', 'message': message})
    if state['invalid_report_hash']:
        out.append({'code': 'coverage_report_invalid',
                    'message': "La zone rapportée par l'hôte est invalide : elle est ignorée, la zone active est inchangée."})
    return out


def consume_dem_wakeup(conn) -> int:
    """
    Réveil du MNT (ZD-10 `done`, inventaire n° 15) : si le marqueur est posé, les activités `no_coverage` sont REMISES EN
    FILE (`dem_status` NULL) et le marqueur est effacé, dans la même transaction. Lu par la tâche `dem_backfill` à son tick
    suivant ; retourne le nombre d'activités remises en file.
    """
    with write_transaction(conn):
        if not read(conn)['dem_wakeup']:
            return 0
        count = conn.execute("UPDATE activities SET dem_status = NULL WHERE dem_status = 'no_coverage'").rowcount
        _set(conn, dem_wakeup=0)
    return count


# -- Actions de l'administrateur (ZD-12) ----------------------------------------------------------------------------------

def retry(conn, requested: list, now: datetime, actor: audit.Actor) -> str:
    """« Réessayer » : `failed` ⇒ `pending` (demande inchangée, délai de stabilité relancé). Autre statut : CoverageError."""
    with write_transaction(conn):
        state = read(conn)
        if state['status'] != 'failed':
            raise CoverageError("rien à réessayer")
        _set(conn, status='pending', reason=state['reason'] or 'zone', status_at=_iso(now), requested_at=_iso(now),
             last_failure_code=None)
        audit.record(conn, 'routing.retry', actor=actor, target_type='routing', target_id='coverage')
    return 'pending'


def release_stuck(conn, requested: list, now: datetime, actor: audit.Actor) -> str:
    """« Tâche bloquée ? » : `building` depuis plus de 2 h ⇒ prise abandonnée, `pending` ou `idle` (recalcul)."""
    with write_transaction(conn):
        state = read(conn)
        claimed_at = _parse(state['claimed_at'])
        if state['status'] != 'building' or not claimed_at or now - claimed_at < STUCK_AFTER:
            raise CoverageError("aucune tâche bloquée")
        status = _recalc(conn, requested, now)
        audit.record(conn, 'routing.release', actor=actor, target_type='routing', target_id='coverage')
    return status
