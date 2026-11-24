"""
Routes principales de l'application (pages HTML).
"""

from flask import current_app, render_template

from app.auth import admin_required, current_user_id
from app.db import get_db
from app.main import bp
from app.services import activity_store, activity_types, profile_service


@bp.app_context_processor
def _catalogs():
    """Catalogue des types d'activité (listes déroulantes rendues côté serveur) et version du pied de page."""
    return {'activity_types': activity_types.CATALOG,
            'unclassified': (activity_types.UNCLASSIFIED, activity_types.UNCLASSIFIED_LABEL),
            'app_version': current_app.config['APP_VERSION']}


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


@bp.route('/profile')
def profile():
    """Profil de l'utilisateur courant : mesures facultatives et préférences d'import."""
    db, user_id = get_db(), current_user_id()
    return render_template('profile.html', active_page='profile',
                           profile=profile_service.get(db, user_id),
                           nb_activities=activity_store.count_for_user(db, user_id))


@bp.route('/admin')
@bp.route('/admin/users')
@admin_required
def admin():
    """Administration (onglets repérés par l'ancre : utilisateurs, invitations, fonctions bêta, réglages, secrets, journal)."""
    return render_template('admin.html', active_page='admin', admin_role=current_app.config.get('OIDC_ADMIN_ROLE'))
