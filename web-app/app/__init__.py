"""
Application Flask modernisée avec architecture modulaire.
Phase 1 du plan d'améliorations - Modernisation de l'infrastructure.
"""

from flask import Flask
from app.config import Config
import os


def create_app(config_class=Config):
    """Factory pattern pour créer l'application Flask."""
    # Définir le chemin vers les templates et static
    template_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'templates'))
    static_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'static'))
    
    app = Flask(__name__,
                template_folder=template_dir,
                static_folder=static_dir)
    app.config.from_object(config_class)
    
    # Enregistrer les blueprints
    from app.main import bp as main_bp
    app.register_blueprint(main_bp)
    
    from app.api import bp as api_bp
    app.register_blueprint(api_bp, url_prefix='/api')
    
    return app