"""
Réglages administrables : registre borné (registry.py) et valeur effective (store.py).

Règle (admin-space §4) : aucun module métier ne lit `os.environ` ni la table des réglages ; il passe
par `effective(cfg, clé)` ou par le store.
"""

import logging

from app.settings.registry import REGISTRY, SECTIONS
from app.settings.store import SettingsConflict, SettingsError, SettingsStore

__all__ = ['REGISTRY', 'SECTIONS', 'SettingsConflict', 'SettingsError', 'SettingsStore', 'STORE_KEY', 'effective',
           'init_settings']

logger = logging.getLogger(__name__)

STORE_KEY = 'SETTINGS_STORE'       # clé de app.config sous laquelle le store est publié


def effective(cfg, key: str, db=None):
    """
    Valeur effective d'un réglage pour un code qui ne connaît que `cfg` (app.config ou copie).
    Avec une connexion `db`, la version en base est relue d'abord (une ligne) : le fil de fond et
    la CLI voient ainsi un changement fait ailleurs. Sans store dans `cfg` (outil isolé, test
    unitaire), repli sur la valeur de `cfg` validée, puis sur le défaut.
    """
    store = cfg.get(STORE_KEY)
    if store is not None:
        if db is not None:
            try:
                store.refresh(db)
            except Exception:           # base momentanément illisible : on garde le cache
                logger.warning("Version des réglages illisible, cache conservé", exc_info=True)
        return store.get(key, cfg)
    setting = REGISTRY[key]
    raw = cfg.get(setting.env) if setting.env else None
    if raw not in (None, ''):
        try:
            return setting.parse_env(raw)
        except ValueError:
            pass
    return setting.default


def init_settings(app) -> SettingsStore:
    """Crée le store de l'application, le charge, et le fait relire à chaque requête (EF-6.4)."""
    from flask import request

    from app.db import connect, get_db

    # La couche « env » est app.config : elle porte déjà les variables d'environnement lues par
    # Config, et les surcharges de test ; None = variable non renseignée
    from app import outbound
    store = SettingsStore(env=lambda name: app.config.get(name))
    store.destination_allowed = lambda service, destination: outbound.allowed(app.config, service, destination)
    store.secrets = app.extensions.get('secrets')
    store.tiles_allowed = lambda hosts: hosts <= outbound.allowlist_of(app.config).tile_hosts()
    app.config[STORE_KEY] = store
    app.extensions['settings'] = store

    conn = connect(app.config['DB_PATH'])
    try:
        store.refresh(conn, force=True)
    finally:
        conn.close()
    store.log_overrides()

    def refresh_per_request():
        if request.endpoint == 'static':
            return
        try:
            store.refresh(get_db())
        except Exception:               # ne jamais faire échouer une requête pour une relecture de réglages
            logger.warning("Relecture des réglages impossible, cache conservé", exc_info=True)

    app.before_request(refresh_per_request)
    return store
