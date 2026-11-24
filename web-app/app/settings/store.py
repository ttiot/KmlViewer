"""
Valeur effective des réglages : base > environnement > défaut (ADR-0008), version en base (M10).

  - `get(key)` ne touche jamais la base : il lit un cache. `refresh(conn)` relit UNE ligne
    (`settings_meta.version`) et ne recharge le cache que si elle a changé : à appeler à chaque
    requête et à chaque tour de l'ordonnanceur (EF-6.4). Un changement fait par un autre processus
    (CLI, autre worker) est donc vu à la requête suivante, sans redémarrage.
  - Toute écriture (`set_many`, `reset`) valide d'abord, puis écrit, incrémente la version et
    journalise dans UNE transaction. Un lot est tout ou rien.
  - Valeur d'env invalide, ou valeur en base devenue invalide : ignorée, signalée (nom seul, jamais
    la valeur), défaut appliqué ; jamais de refus de démarrage pour un réglage du registre.
"""

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Mapping, Optional

from app import audit
from app.db import write_transaction
from app.settings.registry import REGISTRY, Setting

logger = logging.getLogger(__name__)

MASK = '[masqué]'


class SettingsError(ValueError):
    """Écriture refusée : `field` est la clé en cause (400 avec le champ)."""

    def __init__(self, key: str, message: str):
        super().__init__(f"{key} : {message}")
        self.field, self.message = key, message


@dataclass(frozen=True)
class _State:
    version: Optional[int] = None
    base: Mapping = field(default_factory=dict)      # clé -> valeur validée
    rows: Mapping = field(default_factory=dict)      # clé -> (updated_at, updated_by)


class SettingsStore:
    def __init__(self, registry: Optional[Mapping[str, Setting]] = None, env: Optional[Callable] = None):
        self._registry = REGISTRY if registry is None else registry
        self._env = env or os.environ.get        # nom de variable -> texte (ou valeur typée) ou None
        self._state = _State()
        self._lock = threading.Lock()
        self._warned = set()

    # -- lecture ---------------------------------------------------------------------------------
    def setting(self, key: str) -> Setting:
        try:
            return self._registry[key]
        except KeyError:
            raise KeyError(f"Réglage inconnu : {key!r}") from None

    def get(self, key: str):
        setting = self.setting(key)
        state = self._state
        if key in state.base:
            return state.base[key]
        value, _ = self._from_env(setting)
        return setting.default if value is None else value

    def _from_env(self, setting: Setting):
        """(valeur validée ou None, invalide?) pour la variable d'env du réglage."""
        raw = self._env(setting.env) if setting.env else None
        if raw is None or raw == '':
            return None, False
        try:
            return setting.parse_env(raw), False
        except ValueError:
            token = (setting.key, 'env')
            if token not in self._warned:
                self._warned.add(token)
                logger.warning("Variable d'environnement %s invalide : ignorée, valeur par défaut appliquée", setting.env)
            return None, True

    def source(self, key: str) -> str:
        setting = self.setting(key)
        if key in self._state.base:
            return 'base'
        return 'env' if self._from_env(setting)[0] is not None else 'défaut'

    # -- cache et version ---------------------------------------------------------------------
    def refresh(self, conn, force: bool = False) -> bool:
        """Recharge le cache si la version en base a changé. Retourne True s'il a été rechargé."""
        row = conn.execute("SELECT version FROM settings_meta WHERE id = 1").fetchone()
        version = row['version'] if row else 0
        with self._lock:
            if not force and version == self._state.version:
                return False
            base, rows = {}, {}
            for r in conn.execute("SELECT key, value, updated_at, updated_by FROM settings"):
                setting = self._registry.get(r['key'])
                if setting is None:
                    self._warn_once((r['key'], 'inconnu'), "Réglage %r en base inconnu de cette version : ignoré", r['key'])
                    continue
                try:
                    base[r['key']] = setting.validate(json.loads(r['value']))
                    rows[r['key']] = (r['updated_at'], r['updated_by'])
                except (ValueError, RecursionError):    # JSON illisible ou trop imbriqué, ou hors bornes resserrées
                    self._warn_once((r['key'], 'base'), "Réglage %r en base invalide : ignoré, repli sur l'env ou le défaut",
                                    r['key'])
            self._state = _State(version, base, rows)
        return True

    def _warn_once(self, token, message, *args):
        if token not in self._warned:
            self._warned.add(token)
            logger.warning(message, *args)

    # -- écriture ------------------------------------------------------------------------------
    def set_many(self, conn, changes: Mapping, *, actor: audit.Actor,
                 ip: Optional[str] = None) -> None:
        """Lot tout ou rien. Lève SettingsError(clé, message) sans rien écrire si une valeur est refusée."""
        if not changes:
            return
        clean = {}
        for key, value in changes.items():
            setting = self._registry.get(key)
            if setting is None:
                raise SettingsError(str(key)[:64], "Réglage inconnu")
            try:
                clean[key] = setting.validate(value)
            except ValueError as exc:
                raise SettingsError(key, str(exc)) from None
        now = _now()
        with write_transaction(conn):
            for key, value in clean.items():
                setting = self._registry[key]
                old = self._effective_in(conn, setting)       # lu en base SOUS la transaction (T3-1), pas dans le cache
                conn.execute(
                    "INSERT INTO settings (key, value, updated_at, updated_by) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at, "
                    "updated_by = excluded.updated_by", (key, json.dumps(value), now, _updated_by(actor)))
                audit.record_setting_change(conn, key, old, value, secret=setting.secret, actor=actor, ip=ip)
            self._bump(conn)
        self._after_write(conn)

    def reset(self, conn, key: Optional[str] = None, *, actor: audit.Actor,
              ip: Optional[str] = None) -> int:
        """Supprime la ligne en base (ou toutes) : l'env, sinon le défaut, redevient effectif. Retourne le nombre supprimé."""
        if key is not None:
            self.setting(key)
        with write_transaction(conn):
            query = "SELECT key, value FROM settings" + ("" if key is None else " WHERE key = ?")
            previous = {}
            for row in conn.execute(query, () if key is None else (key,)).fetchall():
                setting = self._registry.get(row['key'])
                previous[row['key']] = MASK if setting is not None and setting.secret else _loads(row['value'])
            conn.execute("DELETE FROM settings" + ("" if key is None else " WHERE key = ?"), () if key is None else (key,))
            keys = list(previous)
            if keys:
                self._bump(conn)
                audit.record(conn, 'setting.reset', actor=actor, target_type='setting', target_id=key or '*', ip=ip,
                             details={'keys': sorted(keys), 'old': previous})
        self._after_write(conn)
        return len(keys)

    def _effective_in(self, conn, setting: Setting):
        """Valeur effective telle que la base la porte MAINTENANT (même connexion, donc même transaction)."""
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (setting.key,)).fetchone()
        if row is not None:
            try:
                return setting.validate(json.loads(row['value']))
            except (ValueError, RecursionError):
                pass            # valeur en base devenue invalide : elle est ignorée, comme à la lecture
        value, _ = self._from_env(setting)
        return setting.default if value is None else value

    @staticmethod
    def _bump(conn):
        conn.execute("INSERT INTO settings_meta (id, version) VALUES (1, 1) "
                     "ON CONFLICT(id) DO UPDATE SET version = version + 1")

    def _after_write(self, conn):
        # Dans la transaction d'un appelant, rien n'est encore validé : ne pas mettre en cache
        # ce qui pourrait être annulé ; la prochaine requête verra la version en base.
        if not conn.in_transaction:
            self.refresh(conn, force=True)

    # -- diagnostic ----------------------------------------------------------------------------
    def diagnostic(self) -> list:
        """Une entrée par réglage : valeur, source, surcharge d'une variable d'env (valeur masquée si secret)."""
        out = []
        for key, setting in self._registry.items():
            state = self._state
            env_value, env_invalid = self._from_env(setting)
            raw_set = bool(setting.env) and self._env(setting.env) not in (None, '')
            overridden = key in state.base and raw_set
            value = self.get(key)
            updated = state.rows.get(key)
            out.append({
                'key': key, 'label': setting.label, 'section': setting.section, 'type': setting.type,
                'value': MASK if setting.secret else value, 'default': MASK if setting.secret else setting.default,
                'source': self.source(key), 'env': setting.env,
                'env_overridden': setting.env if overridden else None,
                'env_ignored_value': (MASK if setting.secret else env_value) if overridden else None,
                'env_invalid': env_invalid, 'requires_restart': setting.requires_restart,
                'min': setting.min, 'max': setting.max, 'max_length': setting.max_length,
                'choices': list(setting.choices) if setting.choices else None, 'max_items': setting.max_items,
                'updated_at': updated[0] if updated else None, 'updated_by': updated[1] if updated else None,
            })
        return out

    def log_overrides(self) -> list:
        """Un WARNING par variable d'env surchargée par la base (nom seul). Retourne les noms."""
        names = [d['env_overridden'] for d in self.diagnostic() if d['env_overridden']]
        for name in names:
            logger.warning("Variable d'environnement %s surchargée par un réglage en base (l'admin prime)", name)
        return names


def _loads(text):
    try:
        return json.loads(text)
    except (ValueError, RecursionError):
        return None


def _updated_by(actor) -> str:
    """Texte affiché dans `settings.updated_by` : le libellé du compte, ou `cli` / `system` (jamais un libellé libre)."""
    return actor.label if actor.type == 'user' else actor.type


def _now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
