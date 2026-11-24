"""
Application Flask : factory et câblage des composants.
"""

import logging
import os
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

    # Avant tout câblage : une configuration dangereuse (prod sans authentification, clé de
    # session par défaut…) arrête le démarrage ici
    from app.auth.checks import check_startup_config
    check_startup_config(app)

    # Chemins dérivés du dossier de données
    data_dir = Path(app.config['DATA_DIR']).resolve()
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    app.config['DATA_DIR'] = str(data_dir)
    app.config['DB_PATH'] = app.config.get('DB_PATH') or str(data_dir / 'kmlviewer.sqlite3')

    from app import db
    db.init_db(app.config['DB_PATH'])
    app.teardown_appcontext(db.close_db)

    # Clés des secrets : une valeur chiffrée indéchiffrable arrête le démarrage (EF-5.2)
    from app.secrets_store import init_secrets
    init_secrets(app)

    # L'ordre compte : contrôle d'origine, puis authentification, puis routes
    from app.security import init_security
    init_security(app)

    from app.errors import init_errors
    init_errors(app)

    # Comptes à mot de passe disponibles : la liste de mots de passe courants est chargée maintenant, et son
    # absence arrête le démarrage (jamais de politique silencieusement affaiblie, EF-1.4b)
    if app.config.get('PASSWORD_ACCOUNTS'):
        from app.auth.passwords import CommonPasswordsError, init_common_passwords
        try:
            init_common_passwords()
        except CommonPasswordsError as exc:
            raise RuntimeError(f"{exc}. Démarrage refusé.") from None

    # Après le contrôle d'hôte et d'origine (une requête rejetée ne lit pas la base), avant le garde
    # (qui voit des réglages à jour)
    from app.settings import init_settings
    store = init_settings(app)
    from app.mailer import init_mailer
    init_mailer(app)
    from app.auth.pending import PendingStore
    app.extensions['pending'] = PendingStore()
    from app.ratelimit import Limiters
    app.extensions['limiters'] = Limiters(get=store.get)      # seuils relus à chaque accès (réglages à chaud)

    from app.auth import init_auth
    init_auth(app)

    from app.features import init_features
    init_features(app)

    from app.main import bp as main_bp
    app.register_blueprint(main_bp)

    from app.api import bp as api_bp
    app.register_blueprint(api_bp, url_prefix='/api')

    from app.cli import register_cli
    register_cli(app)

    from app.background import Scheduler
    app.extensions['scheduler'] = Scheduler(app.config)      # toujours créé (tests : tick manuel) ; le thread, selon le contexte
    _start_scheduler(app)
    return app


def _start_scheduler(app) -> None:
    """
    Démarre l'ordonnanceur de fond (inbox, rattrapage d'altitude, purge d'audit, relecture des réglages, sondage du
    moteur) dans tout processus SERVI, quels que soient INBOX_ENABLED et DEM_URL (EF-10.1). Jamais en test, en CLI,
    ni dans le parent du reloader.
    """
    if app.config.get('TESTING') or not app.config.get('SCHEDULER_ENABLED', True):
        return
    # `flask <commande>` : jamais. Seul le serveur de développement `flask run` peut le demander (KMLVIEWER_WATCHER=1,
    # outil de développement : en production c'est gunicorn).
    if os.environ.get('FLASK_RUN_FROM_CLI') == 'true' and os.environ.get('KMLVIEWER_WATCHER') != '1':
        return
    # Serveur de développement : le parent du reloader ne sert aucune requête, seul son
    # enfant lance la tâche. gunicorn n'a pas de reloader : même en configuration de
    # développement, son worker doit la lancer (il annonce sa présence par SERVER_SOFTWARE).
    under_gunicorn = os.environ.get('SERVER_SOFTWARE', '').startswith('gunicorn')
    if app.debug and not under_gunicorn and os.environ.get('WERKZEUG_RUN_MAIN') != 'true':
        return
    app.extensions['scheduler'].start()
    from app.mailer import start_queue
    start_queue(app)                       # même thread de vie que l'ordonnanceur : processus servi seulement (EF-10.5)
    logging.getLogger(__name__).info("Ordonnanceur de fond démarré")
