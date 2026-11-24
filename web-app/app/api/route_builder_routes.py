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
from app.services.routing import builder, profiles
from app.services.routing.base import (InvalidResponse, NoRouteFound, RoutingTimeout, RoutingUnavailable)
from app.settings import effective

logger = logging.getLogger(__name__)

MAX_SEED = 2 ** 31 - 1
MAX_NAME = 200


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
    return jsonify({
        'available': state == 'ok', 'state': state, 'activities': profiles.options(),
        'default_activity': profiles.default_for(profile_service.default_activity_type(db, user_id)),
        'min_km': builder.MIN_DISTANCE_KM, 'max_km': effective(cfg, 'routing.max_distance_km'), 'step_km': 0.5,
        'max_waypoints': effective(cfg, 'routing.max_waypoints'),
        'tolerance_pct': effective(cfg, 'routing.distance_tolerance_pct'),
        'coverage_bbox': effective(cfg, 'routing.coverage_bbox')})


def _error(exc) -> ApiError:
    """Erreur du moteur ou du builder -> erreur HTTP à message FIXE (RB-S9)."""
    if isinstance(exc, builder.InvalidBuild):
        return ApiError(str(exc), 400)
    if isinstance(exc, builder.UnreachablePoint):
        return ApiError(f"Le point {exc.number} ne peut pas être approché (hors réseau ou hors de la zone couverte).", 422)
    if isinstance(exc, NoRouteFound):
        return ApiError("Aucun itinéraire trouvé : essaie d'autres points.", 422)
    if isinstance(exc, RoutingTimeout):
        return ApiError("Le service de routage n'a pas répondu à temps.", 504)
    if isinstance(exc, RoutingUnavailable):
        return ApiError("Service de routage indisponible.", 503)
    if isinstance(exc, InvalidResponse):
        return ApiError("Réponse du service de routage invalide.", 502)
    return ApiError("Erreur de routage.", 502)


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
            generation = builder.execute(runtime.provider(), prepared, config)
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
    meta['seed'] = meta.get('last_seed', seed)
    meta['ascent_m'], meta['descent_m'] = _elevation_gain(parsed)
    payload = {**parsed, 'filename': generation.name, 'success': True}
    result_id = runtime.results.put(user_id, generation.gpx, payload, meta)
    meta['result_id'] = result_id
    return jsonify({**payload, 'route_meta': meta})


def _elevation_gain(parsed):
    stats = parsed.get('stats') or parsed.get('elevation') or {}
    ascent = stats.get('ascent_m', stats.get('ascent')) if isinstance(stats, dict) else None
    descent = stats.get('descent_m', stats.get('descent')) if isinstance(stats, dict) else None
    return (ascent, descent)


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
    gpx = builder.rename_gpx(result.gpx, name)                         # nom écrit par l'API XML (échappement), jamais concaténé
    with heavy_slot():
        outcome = ingest_service.ingest_bytes(db, current_app.config, user_id, gpx, f'{name[:100]}.gpx', 'route_builder',
                                              defer_elevation=True, kind='planned')
    if outcome.status == 'error':
        raise ApiError('Enregistrement impossible', 503 if outcome.retryable else 400)
    digest = hashlib.sha256(gpx).hexdigest()
    runtime.results.mark_saved(user_id, data.get('result_id'), {'activity_id': outcome.activity_id, 'user_id': user_id,
                                                                'sha256': digest})
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
        super().__init__('Déjà enregistrée', 409, activity_id=activity_id)
