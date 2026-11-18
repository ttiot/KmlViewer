"""
Configuration de l'application Flask.
"""

import os
from pathlib import Path

DEFAULT_SECRET_KEY = 'your-secret-key-change-in-production'


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or value == '':
        return default
    return value.strip().lower() in {'1', 'true', 'yes', 'on'}


class Config:
    """Configuration de base de l'application."""

    # Configuration Flask
    SECRET_KEY = os.environ.get('SECRET_KEY') or DEFAULT_SECRET_KEY
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16MB max file size
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    SESSION_COOKIE_SECURE = _env_bool('SESSION_COOKIE_SECURE', False)
    # Nombre de reverse proxies de confiance devant l'appli (X-Forwarded-*)
    TRUST_PROXY = int(os.environ.get('TRUST_PROXY') or 0)

    # Configuration des fichiers d'exemple
    SAMPLE_FILES_DIR = os.environ.get('SAMPLE_FILES_DIR') or '/app/sample_files'
    SAMPLE_FILES_FALLBACK = str(Path(__file__).parent.parent.parent)

    # Stockage persistant : base SQLite + fichiers d'origine
    DATA_DIR = os.environ.get('DATA_DIR') or str(Path(__file__).parent.parent / 'data')
    DB_PATH = os.environ.get('DB_PATH') or ''  # défaut : DATA_DIR/kmlviewer.sqlite3

    # Dossier surveillé
    INBOX_ENABLED = _env_bool('INBOX_ENABLED', True)
    INBOX_DIR = os.environ.get('INBOX_DIR') or ''  # défaut : DATA_DIR/inbox
    INBOX_SCAN_INTERVAL = int(os.environ.get('INBOX_SCAN_INTERVAL') or 30)
    # Propriétaire des fichiers déposés à la racine de l'inbox (username ou email)
    INBOX_DEFAULT_USER = os.environ.get('INBOX_DEFAULT_USER') or ''
    # Activités corrigées par cycle de la tâche de fond (rattrapage d'altitude)
    DEM_BACKFILL_BATCH = int(os.environ.get('DEM_BACKFILL_BATCH') or 25)

    # OIDC (FusionAuth ou autre). Sans OIDC_ISSUER : mode mono-utilisateur local.
    OIDC_ISSUER = (os.environ.get('OIDC_ISSUER') or '').rstrip('/')
    OIDC_CLIENT_ID = os.environ.get('OIDC_CLIENT_ID') or ''
    OIDC_CLIENT_SECRET = os.environ.get('OIDC_CLIENT_SECRET') or ''
    OIDC_SCOPES = os.environ.get('OIDC_SCOPES') or 'openid email profile'
    # Si défini, le flag admin suit ce rôle (claim "roles") à chaque connexion
    OIDC_ADMIN_ROLE = os.environ.get('OIDC_ADMIN_ROLE') or ''
    # False : les nouveaux comptes attendent l'activation par un admin
    OIDC_AUTO_ACTIVATE = _env_bool('OIDC_AUTO_ACTIVATE', True)

    # Correction d'altitude : URL d'un dataset OpenTopoData,
    # ex. http://opentopodata:5000/v1/copernicus30 (vide = désactivé)
    DEM_URL = (os.environ.get('DEM_URL') or '').rstrip('/')
    DEM_BATCH_SIZE = int(os.environ.get('DEM_BATCH_SIZE') or 100)
    DEM_TIMEOUT = float(os.environ.get('DEM_TIMEOUT') or 15)
    # Pause entre deux requêtes (1 s requis sur l'API publique opentopodata.org)
    DEM_REQUEST_DELAY = float(os.environ.get('DEM_REQUEST_DELAY') or 0)

    # Fuseau utilisé pour regrouper les activités par semaine/mois/année
    APP_TIMEZONE = os.environ.get('APP_TIMEZONE') or 'Europe/Paris'

    # Version de l'application, injectée au build de l'image (ARG APP_VERSION)
    APP_VERSION = os.environ.get('APP_VERSION') or 'dev'


class DevelopmentConfig(Config):
    """Configuration pour le développement."""
    DEBUG = True
    FLASK_ENV = 'development'


class ProductionConfig(Config):
    """Configuration pour la production."""
    DEBUG = False
    FLASK_ENV = 'production'


class TestingConfig(Config):
    """Configuration pour les tests."""
    TESTING = True
    INBOX_ENABLED = False
    OIDC_ISSUER = ''
    DEM_URL = ''


config = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
    'testing': TestingConfig,
    'default': DevelopmentConfig
}
