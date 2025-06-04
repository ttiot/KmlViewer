"""
Configuration de l'application Flask.
"""

import os
from pathlib import Path


class Config:
    """Configuration de base de l'application."""
    
    # Configuration Flask
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'your-secret-key-change-in-production'
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16MB max file size
    
    # Configuration des uploads
    UPLOAD_FOLDER = os.environ.get('UPLOAD_FOLDER') or 'uploads'
    ALLOWED_EXTENSIONS = {'kml', 'kmz'}
    
    # Configuration des fichiers d'exemple
    SAMPLE_FILES_DIR = os.environ.get('SAMPLE_FILES_DIR') or '/app/sample_files'
    SAMPLE_FILES_FALLBACK = str(Path(__file__).parent.parent.parent)
    
    @staticmethod
    def init_app(app):
        """Initialisation spécifique à l'application."""
        # Créer le dossier d'upload s'il n'existe pas
        os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)


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
    WTF_CSRF_ENABLED = False
    UPLOAD_FOLDER = 'test_uploads'


config = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
    'testing': TestingConfig,
    'default': DevelopmentConfig
}