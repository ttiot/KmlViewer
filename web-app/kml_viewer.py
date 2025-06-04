#!/usr/bin/env python3
"""
Point d'entrée principal de l'application KML Viewer modernisée.
Phase 1 du plan d'améliorations - Architecture modulaire avec Flask 3.0.
"""

import os
from app import create_app
from app.config import config

# Déterminer l'environnement
config_name = os.environ.get('FLASK_ENV', 'development')
app = create_app(config.get(config_name, config['default']))

if __name__ == '__main__':
    # Configuration pour le développement
    debug_mode = config_name == 'development'
    app.run(host='0.0.0.0', port=5000, debug=debug_mode)