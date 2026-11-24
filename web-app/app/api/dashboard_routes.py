"""API du tableau de bord."""

from flask import current_app, jsonify, request

from app.api import bp
from app.settings import effective
from app.api.payload import type_filter
from app.auth import current_user_id
from app.db import get_db
from app.errors import ApiError
from app.services import dashboard_service


def _year_arg():
    """Paramètre `year` : année à afficher, absente pour laisser le service choisir."""
    raw = request.args.get('year')
    if raw is None or raw == '':
        return None
    if not (raw.isascii() and raw.isdigit()) or not dashboard_service.MIN_YEAR <= int(raw) <= dashboard_service.MAX_YEAR:
        raise ApiError('Année invalide')
    return int(raw)


@bp.route('/dashboard')
def dashboard():
    period = request.args.get('period', 'week')
    if period not in dashboard_service.PERIODS:
        raise ApiError(f"Période invalide ({', '.join(dashboard_service.PERIODS)})")
    activity_type = type_filter()
    db, user_id = get_db(), current_user_id()
    stats = dashboard_service.period_stats(db, user_id, period, effective(current_app.config, 'APP_TIMEZONE'),
                                           activity_type, year=_year_arg())
    return jsonify({'stats': stats, 'records': dashboard_service.records(db, user_id, activity_type)})
