"""API du tableau de bord."""

from flask import current_app, jsonify, request

from app.api import bp
from app.auth import current_user_id
from app.db import get_db
from app.services import dashboard_service


@bp.route('/dashboard')
def dashboard():
    period = request.args.get('period', 'week')
    activity_type = request.args.get('type') or None
    db, user_id = get_db(), current_user_id()
    try:
        stats = dashboard_service.period_stats(db, user_id, period,
                                               current_app.config['APP_TIMEZONE'], activity_type)
    except ValueError as exc:
        return jsonify({'success': False, 'error': str(exc)}), 400
    return jsonify({'stats': stats, 'records': dashboard_service.records(db, user_id, activity_type)})
