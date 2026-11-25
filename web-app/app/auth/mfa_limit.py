"""
Limiteur du second facteur par compte (admin-space EF-2.6, EF-S3). Ne compte QUE les ÉCHECS : un code juste ne consomme rien et un
compte qui se connecte souvent n'est jamais bloqué par ses propres succès (recette). Le blocage est lu AVANT la vérification
(`blocked`), l'échec est compté APRÈS (`failed`).
"""

from flask import current_app

MESSAGE = 'Trop de tentatives, réessaie plus tard.'


def blocked(user_id):
    """`None` si le compte peut tenter un facteur, sinon les secondes avant la prochaine tentative (sans rien compter)."""
    allowed, retry = current_app.extensions['limiters'].mfa_account.peek(int(user_id))
    return None if allowed else retry


def failed(user_id) -> None:
    """Un facteur refusé : compté."""
    current_app.extensions['limiters'].mfa_account.hit(int(user_id))
