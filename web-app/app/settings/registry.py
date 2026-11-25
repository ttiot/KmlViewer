"""
Registre des réglages administrables (admin-space EF-6.1, ADR-0008). Module PUR : ni base, ni env.

Chaque entrée déclare des BORNES OBLIGATOIRES, basse et haute (M7) : une valeur énorme désactive
une protection aussi sûrement qu'une valeur nulle. Une entrée sans bornes ne se construit pas.

Frontière (EF-6.6) : les réglages d'amorçage et de sécurité (SECRET_KEY, SECRETS_KEY, AUTH_MODE,
OIDC_*, PUBLIC_URL, WEBAUTHN_RP_ID, OUTBOUND_ALLOWED_HOSTS, TRUSTED_HOSTS, TRUST_PROXY*,
SESSION_*, DATA_DIR, DB_PATH, LOG_LEVEL) NE SONT PAS ici et ne doivent pas l'être. Tout ajout
qui touche l'authentification, les sessions, les limites dures ou une destination sortante
demande une revue sécurité (les seuils du limiteur y sont, bornés des deux côtés, EF-6.5).
"""

import math
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit

TYPES = ('int', 'float', 'bool', 'str', 'enum', 'list', 'url', 'email', 'timezone', 'basemaps', 'rectangle', 'polygon')
BASEMAP_KEYS = frozenset({'name', 'url', 'subdomains', 'attribution', 'max_zoom'})
# Sections du registre, dans l'ordre d'affichage des sous-onglets de l'admin (EF-6.9)
SECTIONS = ('Sécurité', 'E-mail', 'Carte', 'Traces planifiées', 'Altitude', 'Dossier surveillé', 'Stockage', 'Général')
_BOOL_TRUE, _BOOL_FALSE = {'1', 'true', 'yes', 'on'}, {'0', 'false', 'no', 'off'}
_KEY = re.compile(r'[A-Za-z][A-Za-z0-9_.]{0,63}')


@dataclass(frozen=True)
class Setting:
    key: str
    type: str
    default: object
    section: str
    label: str
    help: str = ''
    env: Optional[str] = None          # variable d'environnement qui sert de valeur initiale et de repli
    min: Optional[float] = None        # int / float : obligatoires
    max: Optional[float] = None
    max_length: Optional[int] = None   # str : longueur ; list : longueur de chaque élément (obligatoire)
    choices: Optional[tuple] = None    # enum : obligatoire
    max_items: Optional[int] = None    # list : obligatoire
    requires_restart: bool = False
    secret: bool = False               # valeur masquée dans l'audit et le diagnostic
    schemes: Optional[tuple] = None    # url : schémas permis (obligatoire)
    destination_service: Optional[str] = None   # url : service sortant (outbound.SERVER_SERVICES) dont c'est la destination
    linked_secrets: tuple = ()         # secrets effacés quand cette destination change (EF-5.5)
    unit: Optional[str] = None         # int / float : obligatoire (chaîne vide permise) ; absent du libellé (EF-6.11)

    def __post_init__(self):
        if not _KEY.fullmatch(self.key) or self.type not in TYPES:
            raise ValueError(f"Réglage mal déclaré : {self.key!r}")
        if self.section not in SECTIONS:
            raise ValueError(f"Réglage {self.key!r} : section {self.section!r} absente de SECTIONS")
        if self.type in ('int', 'float') and self.unit is None:
            raise ValueError(f"Réglage {self.key!r} : unité obligatoire (chaîne vide permise)")
        missing = {
            'int': self.min is None or self.max is None, 'float': self.min is None or self.max is None,
            'str': not self.max_length, 'enum': not self.choices,
            'list': not self.max_items or not self.max_length, 'bool': False,
            'url': not self.max_length or not self.schemes, 'email': not self.max_length,
            'timezone': not self.max_length, 'basemaps': not self.max_items, 'rectangle': False, 'polygon': False,
        }[self.type]
        if missing:
            raise ValueError(f"Réglage {self.key!r} : bornes obligatoires manquantes")
        if self.min is not None and self.min > self.max:
            raise ValueError(f"Réglage {self.key!r} : min > max")
        self.validate(self.default)         # un défaut hors bornes est une erreur de code

    def validate(self, value):
        """Valeur normalisée (stricte : types JSON exacts) ou ValueError(message affichable)."""
        kind = self.type
        if kind == 'int':
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError("Entier attendu")
            return self._bounded(value)
        if kind == 'float':
            try:
                finite = (not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value))
            except OverflowError:               # entier démesuré (un JSON peut en porter)
                finite = False
            if not finite:
                raise ValueError("Nombre attendu")
            return self._bounded(float(value))
        if kind == 'bool':
            if not isinstance(value, bool):
                raise ValueError("Booléen attendu")
            return value
        if kind == 'str':
            return self._string(value, self.max_length)
        if kind == 'timezone':
            return self._timezone(value)
        if kind == 'basemaps':
            return self._basemaps(value)
        if kind == 'rectangle':
            return self._rectangle(value)
        if kind == 'polygon':
            return self._polygon(value)
        if kind == 'url':
            return self._url(value)
        if kind == 'email':
            return self._email(value)
        if kind == 'enum':
            if value not in self.choices:
                raise ValueError("Valeur attendue parmi : " + ', '.join(map(str, self.choices)))
            return value
        if not isinstance(value, (list, tuple)) or len(value) > self.max_items:
            raise ValueError(f"Liste de {self.max_items} éléments au plus attendue")
        return [self._string(v, self.max_length) for v in value]

    def parse_env(self, raw):
        """Valeur d'environnement (texte, ou valeur déjà typée venant de la configuration) validée, ou ValueError."""
        if not isinstance(raw, str):
            return self.validate(raw)
        text = raw.strip()
        if self.type == 'int':
            if not re.fullmatch(r'[+-]?\d{1,18}', text):
                raise ValueError("Entier attendu")
            return self.validate(int(text))
        if self.type == 'float':
            try:
                return self.validate(float(text))
            except ValueError:
                raise ValueError("Nombre attendu") from None
        if self.type == 'bool':
            if text.lower() in _BOOL_TRUE:
                return True
            if text.lower() in _BOOL_FALSE:
                return False
            raise ValueError("Booléen attendu")
        if self.type == 'list':
            return self.validate([item.strip() for item in text.split(',') if item.strip()])
        return self.validate(text)

    def _url(self, value):
        """URL de service : vide (non configuré) ou schéma permis, hôte, sans identifiants ni blancs ; « / » final retiré."""
        text = self._string(value, self.max_length)
        if text == '':
            return ''
        if text != text.strip() or any(c.isspace() for c in text):
            raise ValueError("URL sans espace attendue")
        try:
            parts = urlsplit(text)
            parts.port  # noqa: B018 - lève ValueError si le port est invalide
        except ValueError:
            raise ValueError("URL invalide") from None
        if parts.scheme not in self.schemes or not parts.hostname:
            raise ValueError("URL " + '/'.join(self.schemes) + " attendue")
        if parts.username is not None or parts.password is not None:
            raise ValueError("Pas d'identifiants dans l'URL")
        if parts.fragment:
            raise ValueError("Pas de fragment dans l'URL")
        return text.rstrip('/')

    def _timezone(self, value):
        """Fuseau IANA connu de la base `zoneinfo` (jamais un chemin : le nom est validé avant d'être résolu)."""
        text = self._string(value, self.max_length)
        if not re.fullmatch(r'[A-Za-z0-9_+\-]+(/[A-Za-z0-9_+\-]+){0,2}', text):
            raise ValueError("Fuseau horaire invalide")
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            ZoneInfo(text)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            raise ValueError("Fuseau horaire inconnu") from None
        return text

    def _basemaps(self, value):
        """
        Fonds de carte (EF-6.5, R1) : ≤ max_items ; chacun `{name ≤ 40, url, subdomains, attribution ≤ 200, max_zoom 1–22}`.
        L'URL est un gabarit https `{z}/{x}/{y}` (≤ 500) ; les hôtes effectifs sont contrôlés contre `tiles=` par le store.
        """
        from app import outbound
        if not isinstance(value, (list, tuple)) or len(value) > self.max_items:
            raise ValueError(f"{self.max_items} fonds au plus")
        seen, out = set(), []
        for index, item in enumerate(value):
            try:
                out.append(self._basemap(item, seen, outbound))
            except ValueError as exc:
                exc.index = index                      # rang du fond en cause : l'admin place l'erreur sur sa carte (EF-6.13)
                raise
        return out

    def _basemap(self, item, seen, outbound):
        if not isinstance(item, dict) or set(item) - BASEMAP_KEYS or not {'name', 'url'} <= set(item):
            raise ValueError("Fond de carte : name et url obligatoires, avec subdomains, attribution, max_zoom en option")
        name = self._no_markup(self._string(item['name'], 40)).strip()
        if not name or name.casefold() in seen:
            raise ValueError("Nom de fond vide ou en double")
        seen.add(name.casefold())
        subdomains = self._string(item.get('subdomains', 'abc'), 4)
        zoom = item.get('max_zoom', 19)
        if isinstance(zoom, bool) or not isinstance(zoom, int) or not 1 <= zoom <= 22:
            raise ValueError("Zoom maximal : 1 à 22")
        url = self._string(item['url'], 500)
        try:
            outbound.tile_hosts_of_template(url, subdomains)
        except outbound.OutboundError as exc:
            raise ValueError(str(exc)) from None
        return {'name': name, 'url': url, 'subdomains': subdomains,
                'attribution': self._no_markup(self._string(item.get('attribution', ''), 200)), 'max_zoom': zoom}

    def _rectangle(self, value):
        """
        Rectangle géographique `[lon_min, lat_min, lon_max, lat_max]` (RB-1.3, P-TR5-1) : 4 nombres finis, lon dans [-180, 180],
        lat dans [-90, 90], min < max, côtés ≤ 2 000 km (largeur le long du parallèle le plus proche de l'équateur, hauteur le
        long d'un méridien, en haversine).
        """
        if not isinstance(value, (list, tuple)) or len(value) != 4:
            raise ValueError("Rectangle : [lon_min, lat_min, lon_max, lat_max]")
        for number in value:
            # Un entier démesuré n'est jamais converti en float (OverflowError) : il est borné tel quel (T-TR5-B)
            if isinstance(number, bool) or not isinstance(number, (int, float)) or (isinstance(number, float)
                                                                                    and not math.isfinite(number)):
                raise ValueError("Rectangle : quatre nombres finis")
        lon_min, lat_min, lon_max, lat_max = value
        if not (-180 <= lon_min < lon_max <= 180 and -90 <= lat_min < lat_max <= 90):
            raise ValueError("Rectangle : longitudes dans [-180, 180], latitudes dans [-90, 90], minimum < maximum")
        lon_min, lat_min, lon_max, lat_max = (float(v) for v in value)
        from app.services import geo
        parallel = 0.0 if lat_min <= 0 <= lat_max else min(abs(lat_min), abs(lat_max))
        width = float(geo.haversine(parallel, lon_min, parallel, lon_max))
        height = float(geo.haversine(lat_min, lon_min, lat_max, lon_min))
        if max(width, height) > MAX_RECTANGLE_SIDE_M:
            raise ValueError("Rectangle : côté de 2 000 km au plus")
        return [lon_min, lat_min, lon_max, lat_max]

    @staticmethod
    def _polygon(value):
        """Zone couverte en polygone (EF-6.20) : `[]` (aucun polygone) ou 3 à 50 sommets `[lon, lat]` ; contrôles dans `zone.validate`."""
        from app.services.routing import zone
        return zone.validate(value)

    def _email(self, value):
        """Une seule adresse, sans blanc, ni saut de ligne, ni séparateur ou caractère de liste d'en-têtes."""
        text = self._string(value, self.max_length)
        if text == '':
            return ''
        from app.auth import emails                                              # import tardif : app.auth dépend du registre
        try:
            if text != text.strip():
                raise ValueError
            emails.validate_email(text)                                          # validateur unique (T10-n3)
        except ValueError:
            raise ValueError("Une seule adresse e-mail attendue") from None
        return text

    def _bounded(self, number):
        if not self.min <= number <= self.max:
            raise ValueError(f"Valeur entre {self.min:g} et {self.max:g} attendue")
        return number

    @staticmethod
    def _no_markup(text):
        """Nom et attribution sont du TEXTE (T16-1) : Leaflet insère l'attribution en innerHTML, aucun balisage accepté."""
        if any(c in text for c in '<>&"\''):
            raise ValueError("Texte brut attendu : les caractères < > & \" ' sont interdits")
        return text

    @staticmethod
    def _string(value, max_length):
        if not isinstance(value, str):
            raise ValueError("Texte attendu")
        if len(value) > max_length:
            raise ValueError(f"{max_length} caractères au plus")
        if not all(c.isprintable() for c in value):
            raise ValueError("Caractères de contrôle interdits")
        return value


MAX_RECTANGLE_SIDE_M = 2_000_000.0


# Fonds de carte embarqués aujourd'hui (static/js/viewer/map.js) : valeur par défaut du registre
DEFAULT_BASEMAPS = (
    {'name': 'osm', 'url': 'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', 'subdomains': 'abc',
     'attribution': '© OpenStreetMap contributors', 'max_zoom': 19},
    {'name': 'satellite', 'url': 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
     'subdomains': 'abc', 'attribution': '© Esri', 'max_zoom': 19},
    {'name': 'terrain', 'url': 'https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png', 'subdomains': 'abc',
     'attribution': '© OpenTopoMap contributors', 'max_zoom': 17},
    {'name': 'cartodb', 'url': 'https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png', 'subdomains': 'abc',
     'attribution': '© OpenStreetMap contributors © CARTO', 'max_zoom': 20},
    {'name': 'toner', 'url': 'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', 'subdomains': 'abc',
     'attribution': '© OpenStreetMap contributors © CARTO', 'max_zoom': 20},
)


def _int(key, default, low, high, section, label, **kw):
    return Setting(key, 'int', default, section, label, min=low, max=high, **kw)


_ENTRIES = (
    _int('USER_QUOTA_MB', 2048, 0, 102_400, 'Stockage', "Quota de stockage par utilisateur", unit='Mo', env='USER_QUOTA_MB',
         help="0 = illimité (conservé pour compatibilité)."),
    _int('audit.retention_days', 90, 30, 3650, 'Sécurité', "Rétention du journal d'audit", unit='jours'),
    # Comptes et sécurité (EF-6.5) : revue sécurité exigée pour tout ajout (EF-6.6)
    Setting('OIDC_AUTO_ACTIVATE', 'bool', False, 'Sécurité', "Activer d'office les nouveaux comptes SSO",
            env='OIDC_AUTO_ACTIVATE'),
    _int('auth.invite_ttl_hours', 168, 1, 720, 'Sécurité', "Validité d'une invitation", unit='h', help="1 h à 30 j."),
    Setting('auth.mfa_policy', 'enum', 'admins', 'Sécurité', "Second facteur obligatoire pour", choices=('admins', 'all'),
            help="Jamais en dessous de « admins »."),
    Setting('auth.forgot_enabled', 'bool', True, 'Sécurité', "Mot de passe oublié (nécessite le SMTP)"),
    # Dossier surveillé, altitude, général
    Setting('INBOX_DEFAULT_USER', 'str', '', 'Dossier surveillé', "Propriétaire des fichiers à la racine",
            env='INBOX_DEFAULT_USER', max_length=254),
    _int('DEM_BATCH_SIZE', 100, 1, 1000, 'Altitude', "Points par requête MNT", unit='points', env='DEM_BATCH_SIZE'),
    Setting('DEM_TIMEOUT', 'float', 15.0, 'Altitude', "Délai d'une requête MNT", unit='s', env='DEM_TIMEOUT', min=1, max=120),
    Setting('DEM_TOTAL_TIMEOUT', 'float', 90.0, 'Altitude', "Budget d'une correction complète", unit='s',
            env='DEM_TOTAL_TIMEOUT', min=10, max=600),
    Setting('DEM_REQUEST_DELAY', 'float', 0.0, 'Altitude', "Pause entre deux requêtes MNT", unit='s',
            env='DEM_REQUEST_DELAY', min=0, max=10),
    _int('DEM_BACKFILL_BATCH', 25, 1, 1000, 'Altitude', "Activités corrigées par passage", unit='', env='DEM_BACKFILL_BATCH'),
    Setting('APP_TIMEZONE', 'timezone', 'Europe/Paris', 'Général', "Fuseau des regroupements par semaine et mois",
            env='APP_TIMEZONE', max_length=64),
    # Carte (R1) : les hôtes de chaque fond doivent figurer dans OUTBOUND_ALLOWED_HOSTS (tiles=), sinon 400
    Setting('map.basemaps', 'basemaps', list(DEFAULT_BASEMAPS), 'Carte', "Fonds de carte", max_items=10,
            help="Pas de clé privée dans l'URL : elle part au navigateur de chaque utilisateur."),
    Setting('map.default_basemap', 'str', 'osm', 'Carte', "Fond par défaut", max_length=40),
    Setting('map.center_lat', 'float', 46.603354, 'Carte', "Latitude initiale", unit='°', min=-85, max=85),
    Setting('map.center_lon', 'float', 1.888334, 'Carte', "Longitude initiale", unit='°', min=-180, max=180),
    _int('map.zoom', 6, 1, 22, 'Carte', "Zoom initial", unit=''),
    # Traces planifiées (gpx-route-builder §5.6) : bornes basse et haute
    Setting('routing.timeout_s', 'float', 20.0, 'Traces planifiées', "Délai d'un appel au moteur", unit='s', min=1, max=60),
    Setting('routing.total_budget_s', 'float', 30.0, 'Traces planifiées', "Budget d'une génération", unit='s', min=5, max=120),
    _int('routing.max_calls', 15, 1, 30, 'Traces planifiées', "Appels au moteur par génération", unit=''),
    _int('routing.max_waypoints', 50, 2, 100, 'Traces planifiées', "Points au plus", unit=''),
    _int('routing.max_distance_km', 300, 1, 1000, 'Traces planifiées', "Distance maximale", unit='km'),
    _int('routing.distance_tolerance_pct', 15, 1, 100, 'Traces planifiées', "Tolérance sur la distance cible", unit='%'),
    _int('routing.snap_tolerance_m', 50, 10, 500, 'Traces planifiées', "Accroche des points au réseau", unit='m'),
    _int('routing.overlap_warning_pct', 25, 1, 100, 'Traces planifiées', "Seuil d'avertissement de recouvrement", unit='%'),
    _int('routing.overlap_warning_pct_2pts', 40, 1, 100, 'Traces planifiées', "Seuil de recouvrement à 2 points", unit='%'),
    Setting('routing.detour_strategy', 'enum', 'hybrid', 'Traces planifiées', "Stratégie de détours",
            choices=('s1', 's2', 'hybrid')),
    _int('routing.seed_retries', 2, 0, 2, 'Traces planifiées', "Essais de graine supplémentaires", unit=''),
    Setting('routing.coverage_bbox', 'rectangle', [1.35, 48.83, 6.42, 51.55], 'Traces planifiées',
            "Zone couverte (lon min, lat min, lon max, lat max)"),
    Setting('routing.coverage_polygon', 'polygon', [], 'Traces planifiées', "Zone couverte (polygone)"),
    _int('routing.max_concurrent', 2, 1, 4, 'Traces planifiées', "Générations simultanées sur l'instance", unit=''),
    # Tâches de fond (EF-6.5) : relues à chaque tour de l'ordonnanceur, aucun redémarrage
    Setting('INBOX_ENABLED', 'bool', True, 'Dossier surveillé', "Surveiller le dossier d'import", env='INBOX_ENABLED'),
    _int('INBOX_SCAN_INTERVAL', 30, 5, 3600, 'Dossier surveillé', "Intervalle de scrutation du dossier", unit='s',
         env='INBOX_SCAN_INTERVAL'),
    _int('DEM_BACKFILL_INTERVAL', 300, 30, 3600, 'Altitude', "Intervalle du balayage de correction d'altitude", unit='s',
         env='DEM_BACKFILL_INTERVAL'),
    _int('routing.health_interval_s', 60, 15, 600, 'Traces planifiées', "Intervalle du sondage du moteur", unit='s'),
    # E-mail (EF-4.1, ADR-0005) : le mot de passe SMTP est un SECRET (app/secrets_store.py), lié à `smtp.host:port`
    Setting('smtp.host', 'str', '', 'E-mail', "Serveur SMTP", max_length=253, destination_service='smtp',
            linked_secrets=('smtp.password',),
            help="Vide : aucun e-mail. Hôte et port doivent figurer dans OUTBOUND_ALLOWED_HOSTS (smtp=)."),
    _int('smtp.port', 587, 1, 65535, 'E-mail', "Port SMTP", unit=''),
    Setting('smtp.security', 'enum', 'starttls', 'E-mail', "Sécurité SMTP", choices=('starttls', 'tls', 'none'),
            help="« none » : aucune authentification n'est envoyée. Jamais de repli en clair."),
    Setting('smtp.username', 'str', '', 'E-mail', "Identifiant SMTP", max_length=254),
    Setting('smtp.sender', 'email', '', 'E-mail', "Adresse d'expédition", max_length=254),
    _int('smtp.timeout_s', 10, 1, 60, 'E-mail', "Délai SMTP", unit='s'),
    # Destinations sortantes (EF-6.8, B3) : soumises à OUTBOUND_ALLOWED_HOSTS à l'écriture ET à l'usage ; changer
    # la destination efface les secrets liés (EF-5.5)
    Setting('DEM_URL', 'url', '', 'Altitude', "URL du service d'altitude (MNT)", env='DEM_URL', max_length=500,
            schemes=('http', 'https'), destination_service='dem',
            help="Vide : correction d'altitude désactivée. Hôte et port doivent figurer dans OUTBOUND_ALLOWED_HOSTS (dem=)."),
    Setting('routing.base_url', 'url', '', 'Traces planifiées', "URL du moteur de routage", env='ROUTING_BASE_URL',
            max_length=500, schemes=('http', 'https'), destination_service='routing', linked_secrets=('routing.api_key',),
            help="Hôte et port doivent figurer dans OUTBOUND_ALLOWED_HOSTS (routing=)."),
    # Seuils du limiteur (ADR-0006, EF-6.5) : lus à chaud, sans remise à zéro des compteurs
    _int('ratelimit.password_per_identifier', 5, 3, 20, 'Sécurité', "Tentatives de mot de passe par identifiant",
         unit='/ 15 min'),
    _int('ratelimit.password_per_ip', 20, 10, 100, 'Sécurité', "Tentatives de mot de passe par IP", unit='/ 15 min'),
    _int('ratelimit.mfa_per_account', 5, 3, 10, 'Sécurité', "Essais de second facteur par compte", unit='/ 15 min'),
    _int('ratelimit.forgot_per_identifier', 3, 1, 10, 'Sécurité', "Mots de passe oubliés par identifiant", unit='/ h'),
    _int('ratelimit.forgot_per_ip', 10, 10, 100, 'Sécurité', "Mots de passe oubliés par IP", unit='/ h'),
    _int('ratelimit.token_per_ip', 10, 5, 30, 'Sécurité', "Essais de jeton par IP", unit='/ 15 min'),
    _int('routing.rate_limit_per_user', 30, 1, 100, 'Traces planifiées', "Générations par utilisateur", unit='/ h'),
)

REGISTRY = {s.key: s for s in _ENTRIES}
assert len(REGISTRY) == len(_ENTRIES), "clé de réglage en double"
