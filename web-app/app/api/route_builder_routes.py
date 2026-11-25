"""
Constructeur de traces (gpx-route-builder §5.7, RB-1.x, RB-4.x, RB-5.x, RB-S1…S10 ; TR5) : `options`, `route`, `save`.

Toutes les routes sont sous `feature_required('route_builder')` (404 si la fonction n'est pas ouverte à l'utilisateur, jamais
403) ET exigent un moteur configuré dont la destination est autorisée (sinon 404 aussi). Ordre d'une génération : validation
(400, aucun appel au moteur, ne compte pas) -> places (429 immédiat) -> limiteur horaire à deux compteurs (429) -> moteur ->
parse final sous `heavy_slot()` -> résultat gardé en mémoire (lié à l'utilisateur, `result_id` de 192 bits). Erreurs du moteur :
budget épuisé sans trace 504, indisponible 503, réponse invalide 502, point non approchable 422 avec son numéro d'ORIGINE ;
aucun message ne contient l'URL, la réponse du moteur ni de coordonnée (RB-S9).
"""

import hashlib
import logging
import secrets as secrets_module
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import current_app, jsonify

from app.api import bp
from app.api.payload import json_body
from app.api.uploads import parse_gpx_cached
from app.api.workload import heavy_slot
from app.auth import current_user_id
from app.db import get_db
from app.errors import ApiError
from app.features import feature_required
from app.services import ingest_service, profile_service
from app.services.routing import builder, profiles, zone
from app.services.routing.base import (InvalidResponse, NoRouteFound, RoutingTimeout, RoutingUnavailable)
from app.settings import effective

logger = logging.getLogger(__name__)

MAX_SEED = 2 ** 31 - 1
MAX_NAME = 200
MAX_SEEN = 20


def _runtime():
    runtime = current_app.extensions.get('routing')
    if runtime is None or not runtime.configured():
        # moteur non configuré ou hors liste blanche : comme une fonction absente
        raise ApiError('Introuvable', 404)
    return runtime


def route_builder_route(rule, **options):
    """Route sous la fonction bêta ET le moteur configuré."""
    def decorator(view):
        def wrapper(*args, **kwargs):
            _runtime()
            return view(*args, **kwargs)
        wrapper.__name__ = view.__name__
        return bp.route(rule, **options)(feature_required('route_builder')(wrapper))
    return decorator


@route_builder_route('/route-builder/options')
def route_builder_options():
    runtime, cfg = _runtime(), current_app.config
    db, user_id = get_db(), current_user_id()
    state = runtime.state()
    area = zone.effective_zone(lambda key: effective(cfg, key))          # polygone, sinon les 4 angles du rectangle (RB-1.3b)
    return jsonify({
        'available': state == 'ok', 'state': state, 'activities': profiles.options(),
        'default_activity': profiles.default_for(profile_service.default_activity_type(db, user_id)),
        'min_km': builder.MIN_DISTANCE_KM, 'max_km': effective(cfg, 'routing.max_distance_km'), 'step_km': 0.5,
        'max_waypoints': effective(cfg, 'routing.max_waypoints'),
        'tolerance_pct': effective(cfg, 'routing.distance_tolerance_pct'),
        'coverage_bbox': zone.envelope_lonlat(area) if area else None, 'coverage_polygon': area})


def _error(exc) -> ApiError:
    """Erreur du moteur ou du builder -> erreur HTTP à message FIXE (RB-S9)."""
    if isinstance(exc, builder.InvalidBuild):
        return ApiError(str(exc), 400)
    if isinstance(exc, builder.UnreachablePoint):
        # Le point est DANS la zone couverte (sinon 400 avant tout appel) mais le moteur ne l'accroche à aucune voie (tolérance)
        return ApiError(f"Le point {exc.number} est trop loin d'une voie praticable : rapproche-le d'un chemin.", 422)
    if isinstance(exc, NoRouteFound):
        return ApiError("Aucune trace possible avec ces points : essaie d'autres points.", 422)     # CA-R19
    if isinstance(exc, RoutingTimeout):
        return ApiError("Le service de routage n'a pas répondu à temps.", 504)
    if isinstance(exc, RoutingUnavailable):
        return ApiError("Service de routage indisponible.", 503)
    if isinstance(exc, InvalidResponse):
        return ApiError("Réponse du service de routage invalide.", 502)
    return ApiError("Erreur de routage.", 502)


def _previous_fingerprints(runtime, user_id, previous_id) -> frozenset:
    """Empreintes des propositions déjà vues, lues dans le résultat précédent de CET utilisateur (inconnu : ensemble vide)."""
    previous = runtime.results.get(user_id, previous_id) if isinstance(previous_id, str) else None
    if previous is None:
        return frozenset()
    return frozenset(previous.meta.get('seen') or ([previous.meta['fingerprint']] if previous.meta.get('fingerprint') else []))


def _request_fields(data):
    seed = data.get('seed')
    if seed is None:
        seed = secrets_module.randbelow(MAX_SEED + 1)             # facultatif : tiré par le serveur (P-TR5-4)
    return data.get('points'), data.get('activity'), data.get('distance_km'), seed


@route_builder_route('/route-builder/route', methods=['POST'])
def route_builder_route_generate():
    runtime = _runtime()
    data = json_body()
    raw_points, activity, distance_km, seed = _request_fields(data)
    config = runtime.build_config()
    try:
        prepared = builder.prepare(raw_points, activity, distance_km, seed, config)            # 400 : ne compte pas, aucun appel
    except builder.InvalidBuild as exc:
        raise _error(exc) from None
    state = runtime.state()
    # RB-1.2 : cache vide = injoignable
    if state != 'ok':
        raise ApiError("Service de routage en cours de démarrage." if state == 'starting'
                       else "Service de routage momentanément indisponible.", 503)
    user_id = current_user_id()
    avoid = _previous_fingerprints(runtime, user_id, data.get('previous_result_id'))   # « Autre proposition » (RB-4.5, RB-4.12)
    refused = runtime.acquire(user_id)
    if refused == 'user':
        raise ApiError("Une génération est encore en cours, réessaie dans quelques secondes.", 429)
    if refused:
        raise ApiError("Service occupé, réessaie dans un instant.", 429)
    try:
        allowed, retry = current_app.extensions['limiters'].generation_user.hit(user_id)
        if not allowed:
            response_error = ApiError("Trop de générations : réessaie plus tard.", 429)
            response_error.retry_after = retry
            raise response_error
        try:
            generation = builder.execute(runtime.provider(), prepared, config, avoid=avoid,
                                         today=datetime.now(ZoneInfo(effective(current_app.config, 'APP_TIMEZONE'))))   # E-26
        except (builder.InvalidBuild, RoutingTimeout, RoutingUnavailable, InvalidResponse, NoRouteFound) as exc:
            raise _error(exc) from None
        except Exception:  # noqa: BLE001 - jamais de détail interne
            logger.exception("Génération de trace : erreur inattendue")
            raise ApiError("Erreur de routage.", 502) from None
    finally:
        runtime.release(user_id)
    with heavy_slot():                                                                            # parse final seulement (RB-S5)
        parsed = parse_gpx_cached(generation.gpx.decode('utf-8'))
    if not parsed.get('success'):
        raise ApiError("Réponse du service de routage invalide.", 502)
    meta = dict(generation.meta)
    meta['warnings'] = list(generation.warnings)
    meta['next_seed'] = (meta.get('last_seed', seed) + 1) % (MAX_SEED + 1)
    meta['seed'] = meta.get('seed_used', meta.get('last_seed', seed))         # graine de la trace RETENUE (P-TR5-4)
    seen = list(avoid)[-MAX_SEEN:] + [meta['fingerprint']] if meta.get('fingerprint') else list(avoid)
    meta['seen'] = list(dict.fromkeys(seen))[-MAX_SEEN:]                       # propositions déjà vues de cette série
    payload = {**parsed, 'filename': generation.name, 'success': True}
    result_id = runtime.results.put(user_id, generation.gpx, payload, meta)
    meta['result_id'] = result_id
    public = {k: v for k, v in meta.items() if k != 'seen'}                 # la série vue reste côté serveur
    return jsonify({**payload, 'route_meta': public})


def _clean_name(value, fallback: str) -> str:
    if value is None:
        return fallback
    if not isinstance(value, str):
        raise ApiError('Nom invalide', 400)
    name = value.strip()
    if not 1 <= len(name) <= MAX_NAME or any(not c.isprintable() for c in name):
        raise ApiError(f'Nom : 1 à {MAX_NAME} caractères, sans caractère de contrôle', 400)
    return name


@route_builder_route('/route-builder/save', methods=['POST'])
def route_builder_save():
    runtime = _runtime()
    data = json_body()
    db, user_id = get_db(), current_user_id()
    result = runtime.results.get(user_id, data.get('result_id'))
    if result is None:
        raise ApiError('Résultat introuvable ou expiré', 404)          # inconnu, expiré, évincé ou d'un autre : même réponse
    zone = ZoneInfo(effective(current_app.config, 'APP_TIMEZONE'))
    fallback = f"Trace planifiée du {datetime.now(zone).strftime('%d/%m/%Y')}"
    name = _clean_name(data.get('name'), fallback)
    if result.saved is not None and _still_exists(db, result.saved):
        raise _Conflict(result.saved['activity_id'])
    rid = data.get('result_id')
    if not runtime.results.begin_save(user_id, rid):               # réservation atomique : un seul enregistrement à la fois
        raise _Conflict(result.saved['activity_id'] if result.saved else None)
    try:
        fresh = runtime.results.get(user_id, rid)                  # relu SOUS la réservation (un concurrent a pu finir)
        if fresh is not None and fresh.saved is not None and _still_exists(db, fresh.saved):
            raise _Conflict(fresh.saved['activity_id'])
        gpx = builder.rename_gpx(result.gpx, name)                 # nom écrit par l'API XML (échappement), jamais concaténé
        with heavy_slot():
            outcome = ingest_service.ingest_bytes(db, current_app.config, user_id, gpx, f'{name[:100]}.gpx', 'route_builder',
                                                  defer_elevation=True, kind='planned')
        if outcome.status == 'error':
            if outcome.quota:
                raise ApiError(outcome.message, 413)                  # E-8 : quota atteint, message clair (pas 503)
            raise ApiError('Enregistrement impossible', 503 if outcome.retryable else 400)
        digest = hashlib.sha256(gpx).hexdigest()
        runtime.results.mark_saved(user_id, rid, {'activity_id': outcome.activity_id, 'user_id': user_id, 'sha256': digest})
    finally:
        runtime.results.end_save(user_id, rid)
    if outcome.status == 'duplicate':
        raise _Conflict(outcome.activity_id)
    scheduler = current_app.extensions.get('scheduler')
    if scheduler is not None:
        scheduler.wake('dem_backfill')                                 # altitude MNT à l'enregistrement (RB-4.8, P-TR5-7)
    return jsonify({'success': True, 'activity_id': outcome.activity_id})


def _still_exists(db, reference) -> bool:
    """La référence COMPOSÉE désigne toujours la trace créée (n7 : l'id seul ne prouve jamais son existence)."""
    row = db.execute("SELECT 1 FROM activities WHERE id = ? AND user_id = ? AND sha256 = ? AND kind = 'planned' "
                     "AND source = 'route_builder'",
                     (reference['activity_id'], reference['user_id'], reference['sha256'])).fetchone()
    return row is not None


class _Conflict(ApiError):
    def __init__(self, activity_id):
        super().__init__('Déjà enregistrée', 409, **({'activity_id': activity_id} if activity_id else {}))
