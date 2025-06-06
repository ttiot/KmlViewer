"""
Blueprint API pour les routes REST.
"""

from flask import Blueprint

bp = Blueprint('api', __name__)

# Import des routes après la création du blueprint
from . import routes
from . import analysis_routes
from . import editor_routes