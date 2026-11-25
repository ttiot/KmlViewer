"""
Limiteur du second facteur par compte (admin-space EF-2.6, EF-S3), ATOMIQUE (MFA-1) : un essai est RÉSERVÉ par un `hit` AVANT la
vérification du code (`reserve` : refus si le plafond est atteint) puis RESTITUÉ au succès (`release`), exactement comme pour le
mot de passe. Des requêtes parallèles ne peuvent donc pas vérifier plus de codes que le plafond ; un compte qui se connecte
souvent n'est jamais bloqué par ses propres succès (recette). Un échec garde simplement la réservation.
"""

from flask import current_app

MESSAGE = 'Trop de tentatives, réessaie plus tard.'


def reserve(user_id):
    """Réserve un essai : `None` si obtenu, sinon les secondes avant la prochaine tentative (rien n'est alors réservé)."""
    allowed, retry = current_app.extensions['limiters'].mfa_account.hit(int(user_id))
    return None if allowed else retry


def release(user_id) -> None:
    """Facteur accepté : l'essai réservé est rendu."""
    current_app.extensions['limiters'].mfa_account.refund(int(user_id))
