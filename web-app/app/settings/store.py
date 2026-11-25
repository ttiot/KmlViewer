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
from app import outbound
from app.settings.registry import REGISTRY, Setting

logger = logging.getLogger(__name__)

MASK = '[masqué]'
SMTP_DESTINATION_KEYS = ('smtp.host', 'smtp.port')


class SettingsError(ValueError):
    """Écriture refusée : `field` est la clé en cause (400 avec le champ)."""

    def __init__(self, key: str, message: str, index: Optional[int] = None):
        super().__init__(f"{key} : {message}")
        self.field, self.message, self.index = key, message, index      # index : rang du fond de carte en cause (EF-6.13)


class SettingsConflict(Exception):
    """EF-6.13 : la version des réglages a changé depuis le chargement de la page ; rien n'est écrit. `version` = la courante."""

    def __init__(self, version):
        super().__init__("Un autre admin a modifié ces réglages : recharge la page avant d'enregistrer.")
        self.version = version


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
        self._written = threading.local()       # version de sa PROPRE écriture, lue dans la transaction après _bump (L3-n1)
        self._warned = set()
        self._startup = None
        # Destinations sortantes (EF-6.8, EF-5.5), branchées par init_settings : `destination_allowed(service, destination)`
        # valide une écriture (400 sinon) ; `secrets` (SecretsStore) efface les secrets liés quand la destination change
        self.destination_allowed = None
        self.tiles_allowed = None             # callable(ensemble d'hôtes) -> bool : fonds de carte contre `tiles=` (R1)
        self.secrets = None

    @property
    def last_write_version(self) -> Optional[int]:
        """Version posée par la dernière écriture de CE fil (set_many ou reset), pas celle d'une écriture concurrente."""
        return getattr(self._written, 'version', None)

    def env_value(self, name: str):
        """Valeur brute d'une variable d'environnement vue par le store (la config de l'app, pas os.environ)."""
        return self._env(name)

    # -- lecture ---------------------------------------------------------------------------------
    @property
    def version(self) -> Optional[int]:
        """Version des réglages en cache (EF-6.4, M10) : change à chaque écriture."""
        return self._state.version

    def setting(self, key: str) -> Setting:
        try:
            return self._registry[key]
        except KeyError:
            raise KeyError(f"Réglage inconnu : {key!r}") from None

    def get(self, key: str, env: Optional[Mapping] = None):
        """
        Valeur effective. `env` (facultatif) : mapping qui tient lieu de couche « environnement » (la copie de
        configuration d'un appelant) ; par défaut, la couche du store (app.config dans l'application).
        """
        setting = self.setting(key)
        state = self._state
        if key in state.base:
            return state.base[key]
        value, _ = self._from_env(setting, env)
        return setting.default if value is None else value

    def _from_env(self, setting: Setting, env: Optional[Mapping] = None):
        """(valeur validée ou None, invalide?) pour la variable d'env du réglage."""
        raw = (env.get(setting.env) if env is not None else self._env(setting.env)) if setting.env else None
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

    def fallback(self, key: str):
        """(valeur effective SANS la ligne en base, origine `env` ou `défaut`) : ce que « Rétablir » donnerait (EF-6.11)."""
        setting = self.setting(key)
        value, _invalid = self._from_env(setting)
        return (setting.default, 'défaut') if value is None else (value, 'env')

    def env_invalid(self, key: str) -> bool:
        return self._from_env(self.setting(key))[1]

    def restart_pending(self, key: str) -> bool:
        """EF-6.15 : réglage `requires_restart` dont la valeur effective diffère de celle lue au démarrage du processus."""
        setting = self.setting(key)
        return bool(setting.requires_restart and key in self._startup and self.get(key) != self._startup[key])

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
            if self._startup is None:                      # instantané des valeurs lues au démarrage (restart_pending)
                self._startup = {k: self.get(k) for k, s in self._registry.items() if s.requires_restart}
        return True

    def _warn_once(self, token, message, *args):
        if token not in self._warned:
            self._warned.add(token)
            logger.warning(message, *args)

    # -- écriture ------------------------------------------------------------------------------
    def set_many(self, conn, changes: Mapping, *, actor: audit.Actor,
                 ip: Optional[str] = None, expected_version: Optional[int] = None) -> None:
        """
        Lot tout ou rien. Lève SettingsError(clé, message) sans rien écrire si une valeur est refusée. `expected_version` (la page
        d'admin l'envoie toujours) est comparée à la version en base DANS la transaction d'écriture : SettingsConflict si elle a
        bougé.
        """
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
                raise SettingsError(key, str(exc), getattr(exc, 'index', None)) from None
            self._check_destination(setting, clean[key])
        self._check_smtp(clean)
        self._check_map(clean)
        now = _now()
        with write_transaction(conn):
            if expected_version is not None:
                row = conn.execute("SELECT version FROM settings_meta WHERE id = 1").fetchone()
                current = row['version'] if row else 0
                if current != expected_version:
                    raise SettingsConflict(current)                  # avant tout audit : rien n'est écrit (EF-6.13)
            refusal = self._check_mfa_policy(conn, clean, actor, ip)    # E-2 : DANS la transaction d'écriture (BEGIN IMMEDIATE)
            if refusal is None:
                self._write_locked(conn, clean, now, actor, ip)
                self._written.version = self._current_version_in(conn)
        if refusal is not None:
            raise refusal
        self._after_write(conn)

    def _write_locked(self, conn, clean, now, actor, ip):
        """Écriture du lot, déjà SOUS la transaction de l'appelant."""
        smtp_before = None
        if any(k in SMTP_DESTINATION_KEYS for k in clean):
            smtp_before = self._smtp_pair_in(conn)
        for key, value in clean.items():
            setting = self._registry[key]
            old = self._effective_in(conn, setting)       # lu en base SOUS la transaction (T3-1), pas dans le cache
            self._clear_secrets_if_destination_changed(conn, setting, old, value, actor, ip)
            conn.execute(
                "INSERT INTO settings (key, value, updated_at, updated_by) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at, "
                "updated_by = excluded.updated_by", (key, json.dumps(value), now, _updated_by(actor)))
            audit.record_setting_change(conn, key, old, value, secret=setting.secret, actor=actor, ip=ip)
        if smtp_before is not None:
            self._clear_smtp_secret_if_changed(conn, smtp_before, actor, ip)
        self._bump(conn)

    def reset(self, conn, key: Optional[str] = None, *, actor: audit.Actor,
              ip: Optional[str] = None) -> int:
        """Supprime la ligne en base (ou toutes) : l'env, sinon le défaut, redevient effectif. Retourne le nombre supprimé."""
        if key is not None:
            self.setting(key)
        with write_transaction(conn):
            smtp_before = self._smtp_pair_in(conn)
            query = "SELECT key, value FROM settings" + ("" if key is None else " WHERE key = ?")
            previous = {}
            for row in conn.execute(query, () if key is None else (key,)).fetchall():
                setting = self._registry.get(row['key'])
                previous[row['key']] = MASK if setting is not None and setting.secret else _loads(row['value'])
            conn.execute("DELETE FROM settings" + ("" if key is None else " WHERE key = ?"), () if key is None else (key,))
            keys = list(previous)
            for removed in keys:
                setting = self._registry.get(removed)
                if setting is not None and setting.destination_service:
                    # La valeur effective redevient celle de l'env (ou le défaut) : si la destination change, les secrets
                    # liés sont effacés dans la même transaction (settings-reset compris)
                    env_value, _ = self._from_env(setting)
                    new = setting.default if env_value is None else env_value
                    self._clear_secrets_if_destination_changed(conn, setting, previous[removed], new, actor, ip)
            if any(k in SMTP_DESTINATION_KEYS for k in keys):
                self._clear_smtp_secret_if_changed(conn, smtp_before, actor, ip)
            if keys:
                self._bump(conn)
                self._written.version = self._current_version_in(conn)
                audit.record(conn, 'setting.reset', actor=actor, target_type='setting', target_id=key or '*', ip=ip,
                             details={'keys': sorted(keys), 'old': previous})
        self._after_write(conn)
        return len(keys)

    def _check_mfa_policy(self, conn, clean: Mapping, actor, ip):
        """
        EF-2.2, CA-13b, E-2 : `auth.mfa_policy = all` est REFUSÉ tant qu'un compte `password` ACTIF n'a aucun facteur actif (les
        codes de secours n'en sont pas un). Appelé SOUS la transaction d'écriture : aucun compte ne peut perdre son dernier
        facteur entre le contrôle et l'écriture. Le refus est audité (dans cette transaction, qui est validée) et RETOURNÉ :
        l'appelant le lève après la validation. Un lot qui le contient est rejeté EN ENTIER.
        """
        if clean.get('auth.mfa_policy') != 'all':
            return None
        from app.auth import factors
        missing = [r['id'] for r in conn.execute("SELECT id FROM users WHERE auth_method = 'password' AND is_active = 1")
                   if not factors.has_second_factor(conn, r['id'])]
        if not missing:
            return None
        audit.record(conn, 'setting.refused', actor=actor, target_type='setting', target_id='auth.mfa_policy', ip=ip,
                     details={'reason': 'comptes sans second facteur', 'accounts': len(missing)})
        return SettingsError('auth.mfa_policy', f"{len(missing)} compte(s) à mot de passe actif(s) n'ont aucun second facteur : "
                                                "ils doivent d'abord en enrôler un (les codes de secours n'en sont pas un).")

    def _check_destination(self, setting: Setting, value) -> None:
        """Une destination hors OUTBOUND_ALLOWED_HOSTS est refusée à l'écriture (400, rien n'est écrit)."""
        if setting.type != 'url' or not setting.destination_service or value == '' or self.destination_allowed is None:
            return
        try:
            destination = outbound.destination_of_url(value)
        except outbound.OutboundError as exc:
            raise SettingsError(setting.key, str(exc)) from None
        if not self.destination_allowed(setting.destination_service, destination):
            raise SettingsError(setting.key, "Destination non autorisée (OUTBOUND_ALLOWED_HOSTS)")

    def _clear_secrets_if_destination_changed(self, conn, setting: Setting, old, new, actor, ip) -> None:
        if setting.type != 'url' or not setting.destination_service or not setting.linked_secrets or self.secrets is None:
            return

        def destination(value):
            try:
                return outbound.destination_of_url(value) if value else None
            except outbound.OutboundError:
                return None

        if destination(old) != destination(new):
            self.secrets.clear_linked(conn, setting.linked_secrets, actor=actor, ip=ip)

    def _check_smtp(self, clean: Mapping) -> None:
        """
        Cohérence du bloc SMTP à l'écriture (EF-4.1, EF-6.8) : l'hôte et le port effectifs APRÈS le lot doivent être
        autorisés pour le service `smtp` ; `none` n'envoie aucune authentification, donc aucun identifiant avec lui.
        """
        if 'smtp.host' not in self._registry or not any(
                k in clean for k in (*SMTP_DESTINATION_KEYS, 'smtp.security', 'smtp.username')):
            return

        def value(key):
            return clean[key] if key in clean else self.get(key)

        host, port, security, username = value('smtp.host'), value('smtp.port'), value('smtp.security'), value('smtp.username')
        if host and self.destination_allowed is not None:
            try:
                destination = outbound.destination_of_smtp(outbound.parse_host_port(f"{host}:{port}")[0], port)
            except outbound.OutboundError as exc:
                raise SettingsError('smtp.host', str(exc)) from None
            if not self.destination_allowed('smtp', destination):
                raise SettingsError('smtp.host' if 'smtp.host' in clean else 'smtp.port',
                                    "Destination non autorisée (OUTBOUND_ALLOWED_HOSTS)")
        if security == 'none' and username:
            raise SettingsError('smtp.username' if 'smtp.username' in clean else 'smtp.security',
                                "Aucun identifiant avec la sécurité « none » (aucune authentification n'est envoyée)")

    def _check_map(self, clean: Mapping) -> None:
        """Fonds de carte (R1, CA-41) : chaque hôte effectif doit figurer dans `tiles=` ; le fond par défaut doit exister."""
        if 'map.basemaps' not in self._registry or not any(k in clean for k in ('map.basemaps', 'map.default_basemap')):
            return
        basemaps = clean['map.basemaps'] if 'map.basemaps' in clean else self.get('map.basemaps')
        if 'map.basemaps' in clean and self.tiles_allowed is not None:
            for index, basemap in enumerate(basemaps):
                hosts = outbound.tile_hosts_of_template(basemap['url'], basemap['subdomains'])
                if not self.tiles_allowed(hosts):
                    raise SettingsError('map.basemaps', f"Fond « {basemap['name']} » : hôte non autorisé par "
                                                        "OUTBOUND_ALLOWED_HOSTS (tiles=)", index)
        # Fond par défaut inexistant : contrôlé par la page d'admin ; le serveur ne le refuse plus (bandeau d'usage, EF-6.12)

    def _smtp_pair_in(self, conn):
        """(hôte, port) SMTP effectifs tels que la base les porte maintenant (même transaction)."""
        if 'smtp.host' not in self._registry or 'smtp.port' not in self._registry:
            return None
        return (self._effective_in(conn, self._registry['smtp.host']), self._effective_in(conn, self._registry['smtp.port']))

    def _clear_smtp_secret_if_changed(self, conn, before, actor, ip) -> None:
        if self.secrets is not None and before is not None and self._smtp_pair_in(conn) != before:
            self.secrets.clear_linked(conn, self._registry['smtp.host'].linked_secrets, actor=actor, ip=ip)

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
    def _current_version_in(conn) -> int:
        row = conn.execute("SELECT version FROM settings_meta WHERE id = 1").fetchone()
        return row['version'] if row else 0

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
