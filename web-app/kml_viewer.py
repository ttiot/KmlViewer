#!/usr/bin/env python3
"""
Point d'entrée de l'application.

Production : `gunicorn kml_viewer:app` (voir Dockerfile).
Développement : `FLASK_ENV=development python kml_viewer.py`.
"""

import logging
import os

from app import create_app
from app.config import config

logging.basicConfig(level=os.environ.get('LOG_LEVEL', 'INFO').upper(),
                    format='%(asctime)s %(levelname)s %(name)s: %(message)s')

# Production par défaut : le mode debug (console Werkzeug) ne s'active que sur demande explicite
config_name = os.environ.get('FLASK_ENV', 'production')
app = create_app(config.get(config_name, config['production']))

if __name__ == '__main__':
    app.run(host=os.environ.get('FLASK_HOST', '127.0.0.1'), port=5000,
            debug=config_name == 'development')
