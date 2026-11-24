"""
Seconds facteurs d'un compte (lecture et remise à zéro). Les facteurs eux-mêmes (TOTP, WebAuthn, codes de
secours) arrivent avec T11 et T12 : ce module se contente de ce qui existe, colonne par colonne et table par
table, pour que `user-admin` (B5) et `user-mfa-reset` fonctionnent dès maintenant et couvrent les facteurs au
fur et à mesure qu'ils sont créés.
"""

import sqlite3


def _columns(conn: sqlite3.Connection, table: str) -> set:
    return {r['name'] for r in conn.execute(f"PRAGMA table_info({table})")}


def has_second_factor(conn: sqlite3.Connection, user_id: int) -> bool:
    """
    Vrai si le compte a au moins un facteur ACTIF : un TOTP confirmé (`totp_enabled_at` renseigné : un secret
    enregistré mais jamais validé par un code n'est pas un facteur, ADR-0004) ou une clé WebAuthn.
    """
    users_cols = _columns(conn, 'users')
    if {'totp_secret', 'totp_enabled_at'} <= users_cols and conn.execute(
            "SELECT 1 FROM users WHERE id = ? AND totp_secret IS NOT NULL AND totp_enabled_at IS NOT NULL",
            (user_id,)).fetchone():
        return True
    if 'user_id' in _columns(conn, 'webauthn_credentials') and conn.execute(
            "SELECT 1 FROM webauthn_credentials WHERE user_id = ?", (user_id,)).fetchone():
        return True
    return False


def clear_factors(conn: sqlite3.Connection, user_id: int) -> dict:
    """
    Retire tous les facteurs du compte (TOTP, clés WebAuthn, codes de secours) à l'intérieur de la transaction
    de l'appelant. Retourne ce qui a été retiré ; ce qui n'existe pas encore n'est pas une erreur.
    """
    removed = {'totp': 0, 'webauthn': 0, 'recovery_codes': 0}
    users_cols = _columns(conn, 'users')
    if 'totp_secret' in users_cols:
        removed['totp'] = conn.execute("UPDATE users SET totp_secret = NULL WHERE id = ? AND totp_secret IS NOT NULL",
                                       (user_id,)).rowcount
        for column in ('totp_last_step', 'totp_enabled_at'):
            if column in users_cols:
                conn.execute(f"UPDATE users SET {column} = NULL WHERE id = ?", (user_id,))
    for table, key in (('webauthn_credentials', 'webauthn'), ('recovery_codes', 'recovery_codes')):
        if 'user_id' in _columns(conn, table):
            removed[key] = conn.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,)).rowcount
    return removed


def active_admin_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM users WHERE is_admin = 1 AND is_active = 1").fetchone()[0]
