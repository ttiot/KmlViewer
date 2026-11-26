"""
API d'administration de la couverture pilotée par la zone (routing-zone-donnees ZD-12, ZD-4.4). Réservée aux admins (403 sinon),
404 si `ROUTING_ZONE_BUILD=0`. Les actions de l'admin sont auditées avec son compte ; celles de l'hôte (acteur `system`) le sont
par `coverage_state`.
"""

from datetime import datetime, timezone

from flask import current_app, g, jsonify, request

from app import audit
from app.api import bp
from app.api.payload import json_body
from app.auth import admin_required
from app.db import get_db
from app.errors import ApiError
from app.services.routing import coverage as geometry
from app.services.routing import coverage_state as coverage
from app.services.routing import zone
from app.settings import SettingsConflict, SettingsError, effective


def _enabled():
    if not coverage.enabled(current_app.config):
        raise ApiError('Fonction inconnue', 404)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _requested() -> list:
    return zone.effective_zone(lambda key: effective(current_app.config, key, get_db()))


def _margin() -> int:
    return int(effective(current_app.config, 'routing.zone_margin_km', get_db()))


def _usage(conn, state) -> dict:
    """Consommation des plafonds par la zone ACTIVE (« 1,1 Go / 4 Gio · 3 régions / 20 · 24 dalles / 60 »)."""
    limits = coverage.Limits.from_config(current_app.config)
    usage = {'max_regions': limits.max_regions, 'max_source_bytes': limits.max_source_bytes, 'max_tiles': limits.max_tiles,
             'regions': len(state['active_regions'] or []), 'bytes': None, 'tiles': None}
    candidates = coverage.load_catalog(conn)
    if candidates and state['active_regions']:
        sizes = {c.id: c.size for c in candidates}
        usage['bytes'] = sum(sizes.get(r, 0) for r in state['active_regions'])
    if state['active_extract_bbox']:
        usage['tiles'] = len(geometry.dem_tiles(state['active_extract_bbox']))
    return usage


def _view(conn) -> dict:
    state = coverage.read(conn)
    stuck = coverage.expired(state, _now(), coverage.STUCK_AFTER)
    return {
        'status': state['status'], 'reason': state['reason'], 'status_at': state['status_at'],
        'requested_at': state['requested_at'], 'claimed_at': state['claimed_at'], 'stuck': stuck,
        'settle_s': current_app.config['ROUTING_ZONE_SETTLE_S'], 'failure_code': state['last_failure_code'],
        'requested_zone': _requested(), 'active_zone': state['active_polygon'], 'historic': state['active_polygon'] is None,
        'active_regions': state['active_regions'], 'data_date': state['data_date'], 'active_graph': state['active_graph'],
        'dem_tiles': len(state['task_tiles'] or []), 'catalog_at': state['catalog_at'], 'usage': _usage(conn, state),
        'budget': {'day': state['budget_day'], 'builds': state['budget_builds'], 'bytes': state['budget_bytes']},
    }


@bp.route('/admin/routing/coverage')
@admin_required
def admin_routing_coverage():
    _enabled()
    return jsonify({'success': True, 'coverage': _view(get_db())})


@bp.route('/admin/routing/coverage/preview', methods=['POST'])
@admin_required
def admin_routing_coverage_preview():
    """Aperçu pour la confirmation (ZD-4.4) : régions ajoutées et retirées, octets à télécharger, dalles nouvelles."""
    _enabled()
    data = json_body()
    try:
        vertices = zone.validate(data.get('polygon'))
        return jsonify({'success': True, 'preview': coverage.preview(get_db(), current_app.config, vertices, _margin())})
    except ValueError as exc:                                       # EF-6.20 ou plafonds (PlanRefused) : même 400 que l'écriture
        raise ApiError(f"routing.coverage_polygon : {exc}", 400, field='routing.coverage_polygon') from None


def _action(operation):
    _enabled()
    try:
        status = operation(get_db(), _requested(), _now(), audit.Actor.from_user(g.user))
    except coverage.CoverageError as exc:
        raise ApiError(str(exc), 409) from None
    return jsonify({'success': True, 'status': status})


@bp.route('/admin/routing/coverage/retry', methods=['POST'])
@admin_required
def admin_routing_coverage_retry():
    """« Réessayer » : `failed` ⇒ `pending`."""
    return _action(coverage.retry)


@bp.route('/admin/routing/coverage/release', methods=['POST'])
@admin_required
def admin_routing_coverage_release():
    """« Tâche bloquée ? » : `building` depuis plus de 2 h ⇒ prise abandonnée."""
    return _action(coverage.release_stuck)


@bp.route('/admin/routing/coverage/revert', methods=['POST'])
@admin_required
def admin_routing_coverage_revert():
    """« Revenir à la zone active » : le réglage reprend la zone active ; `idle`, audit de l'admin."""
    _enabled()
    active = coverage.read(get_db())['active_polygon']
    if not active:
        raise ApiError("Aucune zone active à retrouver (sources historiques).", 409)
    store = current_app.config['SETTINGS_STORE']
    try:
        store.set_many(get_db(), {'routing.coverage_polygon': active}, actor=audit.Actor.from_user(g.user),
                       ip=request.remote_addr)
    except (SettingsError, SettingsConflict) as exc:
        raise ApiError(str(exc), 409) from None
    return jsonify({'success': True, 'status': coverage.read(get_db())['status']})
