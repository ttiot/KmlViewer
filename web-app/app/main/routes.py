"""
Routes principales de l'application (pages HTML).
"""

from flask import current_app, render_template

from app.auth import admin_required
from app.main import bp


@bp.route('/')
def index():
    """Visualiseur de traces."""
    return render_template('index.html', active_page='viewer')


@bp.route('/dashboard')
def dashboard():
    """Tableau de bord et bibliothèque d'activités."""
    return render_template('dashboard.html', active_page='dashboard')


@bp.route('/segments')
def segments():
    """Tronçons personnels."""
    return render_template('segments.html', active_page='segments')


@bp.route('/admin/users')
@admin_required
def users():
    """Administration des comptes."""
    return render_template('users.html', active_page='users',
                           admin_role=current_app.config.get('OIDC_ADMIN_ROLE'))
