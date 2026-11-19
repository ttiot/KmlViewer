"""API du tableau de bord."""

from flask import current_app, jsonify, request

from app.api import bp
from app.auth import current_user_id
from app.db import get_db
from app.errors import ApiError
from app.services import dashboard_service


@bp.route('/dashboard')
def dashboard():
    period = request.args.get('period', 'week')
    if period not in dashboard_service.PERIODS:
        raise ApiError(f"Période invalide ({', '.join(dashboard_service.PERIODS)})")
    activity_type = request.args.get('type') or None
    db, user_id = get_db(), current_user_id()
    stats = dashboard_service.period_stats(db, user_id, period,
                                           current_app.config['APP_TIMEZONE'], activity_type)
    return jsonify({'stats': stats, 'records': dashboard_service.records(db, user_id, activity_type)})
