"""
Blueprint principal pour les routes de l'interface utilisateur.
"""

from flask import Blueprint

bp = Blueprint('main', __name__)

# Import des routes après la création du blueprint pour éviter les imports circulaires
from . import routes