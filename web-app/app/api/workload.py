"""
Régulation des traitements lourds : parsing d'un fichier, analyse d'une trace entière,
simplification. Chacun peut occuper des centaines de Mo et plusieurs secondes de calcul ;
lancés sur tous les threads à la fois, ils épuiseraient la mémoire du conteneur.
"""

import logging
import threading
from contextlib import contextmanager

from flask import request

from app.errors import ApiError

logger = logging.getLogger(__name__)

# Traitements lourds simultanés, tous utilisateurs confondus. Les autres threads restent
# disponibles pour les pages et les requêtes légères.
HEAVY_SLOTS = 2
# Attente maximale d'une place avant de répondre 503 (le client peut réessayer)
SLOT_WAIT_S = 20.0

_slots = threading.BoundedSemaphore(HEAVY_SLOTS)


@contextmanager
def heavy_slot():
    """
    Réserve une place de traitement lourd pour la durée du bloc. Lève ApiError 503 si aucune
    ne se libère à temps. Ne pas imbriquer : un thread qui attendrait sa propre place se
    bloquerait jusqu'au délai.
    """
    if not _slots.acquire(timeout=SLOT_WAIT_S):
        logger.warning("Traitements lourds saturés : %s %r refusée après %.0f s d'attente",
                       request.method, request.path, SLOT_WAIT_S)
        raise ApiError('Serveur occupé : réessaie dans un instant', 503)
    try:
        yield
    finally:
        _slots.release()
