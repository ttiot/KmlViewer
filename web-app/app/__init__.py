"""
Application Flask : factory et câblage des composants.
"""

import logging
import os
from datetime import timedelta
from pathlib import Path

from flask import Flask

from app.config import Config


def create_app(config_class=Config, overrides=None):
    """Factory pattern pour créer l'application Flask."""
    template_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'templates'))
    static_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'static'))

    app = Flask(__name__, template_folder=template_dir, static_folder=static_dir)
    app.config.from_object(config_class)
    app.config.update(overrides or {})
    app.config.setdefault('PERMANENT_SESSION_LIFETIME', timedelta(hours=12))

    # Chemins dérivés du dossier de données
    data_dir = Path(app.config['DATA_DIR']).resolve()
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    app.config['DATA_DIR'] = str(data_dir)
    app.config['DB_PATH'] = app.config.get('DB_PATH') or str(data_dir / 'kmlviewer.sqlite3')

    from app import db
    db.init_db(app.config['DB_PATH'])
    app.teardown_appcontext(db.close_db)

    # L'ordre compte : contrôle d'origine, puis authentification, puis routes
    from app.security import init_security
    init_security(app)

    from app.auth import init_auth
    init_auth(app)

    from app.main import bp as main_bp
    app.register_blueprint(main_bp)

    from app.api import bp as api_bp
    app.register_blueprint(api_bp, url_prefix='/api')

    from app.cli import register_cli
    register_cli(app)

    _start_inbox_watcher(app)
    return app


def _start_inbox_watcher(app) -> None:
    """Démarre la surveillance du dossier, sauf en test, en CLI et dans le parent du reloader."""
    if not app.config.get('INBOX_ENABLED') or app.config.get('TESTING'):
        return
    if os.environ.get('FLASK_RUN_FROM_CLI') == 'true' and os.environ.get('KMLVIEWER_WATCHER') != '1':
        return
    if app.debug and os.environ.get('WERKZEUG_RUN_MAIN') != 'true':
        return
    from app.services.inbox_watcher import InboxWatcher
    InboxWatcher(app.config).start()
    logging.getLogger(__name__).info("Surveillance du dossier d'import démarrée")
