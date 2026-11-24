"""
Destinations sortantes (admin-space EF-6.8, ADR-0011, B3, R1) : liste blanche par SERVICE, normalisation, résultat de
test neutre, hôtes de tuiles pour la CSP.

`OUTBOUND_ALLOWED_HOSTS` (env, hors registre) : entrées préfixées par service, séparées par des virgules :
    routing=graphhopper:8989,dem=opentopodata:5000,smtp=relay.example.com:587,tiles=a.tile.openstreetmap.org
  - `routing`, `dem`, `smtp` : `hôte:port`, port OBLIGATOIRE (variable invalide, démarrage refusé, sinon), comparé
    exactement au port effectif (défaut du schéma compris : `https://moteur` => 443) ; littéral IPv6 ENTRE CROCHETS ;
  - `tiles` : hôte seul (https et 443 implicites) ;
  - une entrée n'autorise que SON service : un hôte autorisé pour `dem` ne vaut pas pour `smtp` ;
  - la destination définie par l'ENV (DEM_URL, ROUTING_BASE_URL) est implicitement autorisée pour son seul service ;
  - sans entrée `tiles=`, les hôtes des fonds embarqués ; la première entrée `tiles=` remplace cette liste ;
  - comparaison sur le NOM, sans résolution DNS ; redirections jamais suivies (par les appelants).
Une destination refusée à l'usage est journalisée dans l'audit, dédoublonnée : un sondage de fond répété ne peut pas
inonder le journal.
"""

import ipaddress
import logging
import re
import threading
import time
from collections import OrderedDict
from typing import Optional
from urllib.parse import urlsplit

from app import audit

logger = logging.getLogger(__name__)

SERVER_SERVICES = ('routing', 'dem', 'smtp')
TILES = 'tiles'
SERVICES = (*SERVER_SERVICES, TILES)
DEFAULT_PORTS = {'http': 80, 'https': 443}
# Hôtes des fonds de carte embarqués (static/js/viewer/map.js, segments.js) : valeur par défaut des tuiles
DEFAULT_TILE_HOSTS = frozenset({
    'a.tile.openstreetmap.org', 'b.tile.openstreetmap.org', 'c.tile.openstreetmap.org', 'server.arcgisonline.com',
    'a.tile.opentopomap.org', 'b.tile.opentopomap.org', 'c.tile.opentopomap.org',
    'a.basemaps.cartocdn.com', 'b.basemaps.cartocdn.com', 'c.basemaps.cartocdn.com',
})
MAX_TILE_URL = 500
_HOSTNAME = re.compile(r'(?=.{1,253}\Z)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*')
_SUBDOMAINS = re.compile(r'[a-z]{1,4}')


class OutboundError(ValueError):
    """Destination ou entrée de liste blanche invalide ; le message est affichable (sans valeur secrète)."""


class DestinationRefused(Exception):
    """Destination hors liste blanche à l'usage : l'appel n'est pas fait. Message générique, sans détail."""

    def __init__(self):
        super().__init__("Destination non autorisée")


def _host_key(host: str) -> str:
    """
    Forme comparée d'un hôte : minuscules, UN point final au plus retiré (« hôte. » = « hôte », forme absolue du DNS) ; un
    littéral IPv6 est ramené à sa forme canonique.
    """
    host = host.strip().lower()
    if host.endswith('.'):
        host = host[:-1]
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return host


def _valid_host(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return bool(_HOSTNAME.fullmatch(host))


def parse_host_port(text: str):
    """`hôte:port` ou `[ipv6]:port` => (hôte canonique, port). Port obligatoire ; IPv6 sans crochets refusée."""
    if not isinstance(text, str) or not text.strip():
        raise OutboundError("hôte:port attendu")
    text = text.strip()
    if text.startswith('['):
        match = re.fullmatch(r'\[([0-9a-fA-F:.]+)\]:(\d{1,5})', text)
        if not match:
            raise OutboundError("littéral IPv6 : [adresse]:port attendu")
        host, port = match.group(1), int(match.group(2))
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            raise OutboundError("adresse IPv6 invalide") from None
    else:
        if text.count(':') != 1:      # plusieurs « : » sans crochets : IPv6 nue (ambiguë) ; aucun : port manquant
            raise OutboundError("port obligatoire (hôte:port), et littéral IPv6 entre crochets")
        host, port_text = text.rsplit(':', 1)
        if not re.fullmatch(r'\d{1,5}', port_text):
            raise OutboundError("port invalide")
        port = int(port_text)
        if host.endswith('..') or not _valid_host(host.lower().removesuffix('.')):
            raise OutboundError("nom d'hôte invalide")
    if not 1 <= port <= 65535:
        raise OutboundError("port hors bornes")
    return _host_key(host), port


def normalize_url(url: str):
    """
    URL http(s) de service => (schéma, hôte canonique, port effectif). Refuse identifiants, schéma autre, hôte
    absent ou invalide. Le chemin et la query ne comptent pas dans la destination.
    """
    # Aucun blanc ni caractère de contrôle, AUTOUR comme dedans (espace, tab, \n, U+2028, U+00A0…) : l'appelant utilise la
    # chaîne brute, elle doit être celle qu'on a contrôlée
    if not isinstance(url, str) or not url or any(c.isspace() or ord(c) < 0x20 or ord(c) == 0x7f for c in url):
        raise OutboundError("URL invalide")
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        raise OutboundError("URL invalide") from None
    if parts.scheme not in DEFAULT_PORTS or not host or parts.username is not None or parts.password is not None:
        raise OutboundError("URL http(s) sans identifiants attendue")
    if host.endswith('..') or not _valid_host(host.lower().removesuffix('.')):      # un seul point final au plus
        raise OutboundError("nom d'hôte invalide")
    return parts.scheme, _host_key(host), port if port is not None else DEFAULT_PORTS[parts.scheme]


def destination_of_url(url: str) -> str:
    """Destination effective normalisée `scheme://hôte:port` (c'est ce que `bound_to` mémorise avec un secret)."""
    scheme, host, port = normalize_url(url)
    return f"{scheme}://{f'[{host}]' if ':' in host else host}:{port}"


def destination_of_smtp(host: str, port: int) -> str:
    return f"smtp://{f'[{_host_key(host)}]' if ':' in _host_key(host) else _host_key(host)}:{int(port)}"


class Allowlist:
    """Entrées explicites de `OUTBOUND_ALLOWED_HOSTS` (services serveur : {(hôte, port)} ; tuiles : hôtes)."""

    def __init__(self, servers: Optional[dict] = None, tiles: Optional[frozenset] = None):
        self.servers = {s: frozenset((servers or {}).get(s, ())) for s in SERVER_SERVICES}
        self.explicit_tiles = frozenset(tiles) if tiles else frozenset()

    @classmethod
    def parse(cls, text: Optional[str]) -> 'Allowlist':
        """Analyse stricte de la variable. Toute entrée invalide lève OutboundError : démarrage refusé (CA-42, CA-52)."""
        servers, tiles = {s: set() for s in SERVER_SERVICES}, set()
        for raw in (text or '').split(','):
            entry = raw.strip()
            if not entry:
                continue
            service, sep, value = entry.partition('=')
            service = service.strip().lower()
            if not sep or service not in SERVICES:
                raise OutboundError(f"OUTBOUND_ALLOWED_HOSTS : entrée « {entry[:40]} » invalide "
                                    f"(service attendu : {', '.join(SERVICES)})")
            try:
                if service == TILES:
                    host = value.strip().lower()
                    if not host or not _valid_host(host) or ':' in host:
                        raise OutboundError("hôte seul attendu pour tiles")
                    tiles.add(_host_key(host))
                else:
                    servers[service].add(parse_host_port(value))
            except OutboundError as exc:
                raise OutboundError(f"OUTBOUND_ALLOWED_HOSTS : entrée {service}= invalide : {exc}") from None
        return cls(servers, frozenset(tiles))

    def tile_hosts(self) -> frozenset:
        """Hôtes de tuiles autorisés : les entrées `tiles=` si présentes (elles REMPLACENT la liste par défaut)."""
        return self.explicit_tiles or DEFAULT_TILE_HOSTS

    def allows(self, service: str, url_or_destination: str, *, env_url: Optional[str] = None) -> bool:
        """
        Vrai si la destination est autorisée POUR CE SERVICE : entrée explicite (hôte ET port exacts), ou destination
        définie par l'env (`env_url`), implicitement autorisée pour son seul service.
        """
        if service not in SERVER_SERVICES or not isinstance(url_or_destination, str):
            return False
        try:
            _scheme, host, port = _parts(url_or_destination)
        except OutboundError:
            return False
        if (host, port) in self.servers[service]:
            return True
        if env_url and isinstance(env_url, str):
            try:
                _s, env_host, env_port = _parts(env_url)
            except OutboundError:
                return False
            return (host, port) == (env_host, env_port)
        return False


def _parts(value: str):
    if value.startswith('smtp://'):
        match = re.fullmatch(r'smtp://(\[[^\]]+\]|[^:/\[\]]+):(\d{1,5})', value)
        if not match:
            raise OutboundError("destination SMTP invalide")
        return 'smtp', _host_key(match.group(1).strip('[]')), int(match.group(2))
    return normalize_url(value)


ENV_DESTINATION_KEYS = {'dem': 'DEM_URL', 'routing': 'ROUTING_BASE_URL'}


def allowlist_of(cfg) -> Allowlist:
    """Liste blanche de l'application (`cfg['OUTBOUND']`), ou celle de l'env seule pour une configuration nue."""
    found = cfg.get('OUTBOUND')
    return found if isinstance(found, Allowlist) else Allowlist.parse(cfg.get('OUTBOUND_ALLOWED_HOSTS') or '')


def allowed(cfg, service: str, destination: str) -> bool:
    """Destination autorisée pour ce service, l'env de `cfg` comprise (lu à chaque appel : il peut changer en test)."""
    env_key = ENV_DESTINATION_KEYS.get(service)
    return allowlist_of(cfg).allows(service, destination, env_url=(cfg.get(env_key) or None) if env_key else None)


# -- Fonds de carte (R1) ----------------------------------------------------------------------------------------

def tile_hosts_of_template(template: str, subdomains: str = 'abc') -> frozenset:
    """
    Hôtes effectifs d'un fond de carte : l'hôte de l'URL, `{s}` remplacé par CHAQUE sous-domaine. Lève OutboundError
    si l'URL n'est pas un gabarit de tuiles `https://` valable (`{z}`, `{x}`, `{y}`, ≤ 500 caractères, sans identifiants).
    """
    if not isinstance(template, str) or len(template) > MAX_TILE_URL:
        raise OutboundError(f"URL de fond de carte : {MAX_TILE_URL} caractères au plus")
    if not all(token in template for token in ('{z}', '{x}', '{y}')):
        raise OutboundError("URL de fond de carte : {z}, {x} et {y} attendus")
    if not isinstance(subdomains, str) or not subdomains or not all(_SUBDOMAINS.fullmatch(c) for c in subdomains) \
            or len(subdomains) > 4 or len(set(subdomains)) != len(subdomains):
        raise OutboundError("sous-domaines : 1 à 4 lettres minuscules distinctes")
    variants = [template.replace('{s}', c) for c in subdomains] if '{s}' in template else [template]
    hosts = set()
    for variant in variants:
        candidate = variant.replace('{r}', '').replace('{z}', '0').replace('{x}', '0').replace('{y}', '0')
        scheme, host, port = normalize_url(candidate)
        if scheme != 'https' or port != 443:
            raise OutboundError("fond de carte : https sur le port 443 seulement")
        hosts.add(host)
    return frozenset(hosts)


def check_tile_template(allowlist: Allowlist, template: str, subdomains: str = 'abc') -> frozenset:
    """Valide un fond de carte contre les hôtes de tuiles autorisés (CA-41) ; retourne ses hôtes ou lève OutboundError."""
    hosts = tile_hosts_of_template(template, subdomains)
    refused = hosts - allowlist.tile_hosts()
    if refused:
        raise OutboundError("fond de carte : hôte non autorisé par OUTBOUND_ALLOWED_HOSTS (tiles=)")
    return hosts


def img_src_hosts(allowlist: Allowlist) -> list:
    """Sources `https://hôte` pour la CSP `img-src` (R1) : exactement les hôtes de tuiles autorisés, triés."""
    return [f"https://{host}" for host in sorted(allowlist.tile_hosts())]


# -- Refus à l'usage, audités sans inonder le journal ---------------------------------------------------------------

class RefusalLog:
    """Dédoublonne les lignes d'audit de refus : une par (service, destination) et par fenêtre, mémoire bornée."""

    def __init__(self, window_s: float = 3600, max_entries: int = 256, clock=time.monotonic):
        self.window_s, self.max_entries, self._clock = window_s, max_entries, clock
        self._seen: OrderedDict = OrderedDict()
        self._lock = threading.Lock()

    def due(self, service: str, destination: str) -> bool:
        """Vrai si un refus pour cette destination doit être écrit (rien n'est marqué)."""
        with self._lock:
            last = self._seen.get((service, destination))
            return last is None or self._clock() - last >= self.window_s

    def mark(self, service: str, destination: str) -> None:
        """Marque la destination comme tracée : à appeler APRÈS la validation de la ligne d'audit."""
        with self._lock:
            self._seen[(service, destination)] = self._clock()
            self._seen.move_to_end((service, destination))
            while len(self._seen) > self.max_entries:
                self._seen.popitem(last=False)

    def should_record(self, service: str, destination: str) -> bool:
        """due() puis mark() d'un coup (usage hors transaction)."""
        if not self.due(service, destination):
            return False
        self.mark(service, destination)
        return True


_refusals = RefusalLog()


def refuse(conn, service: str, destination: str, *, ip: Optional[str] = None, log: Optional[RefusalLog] = None) -> None:
    """Trace un refus (`outbound.refused`, acteur système), au plus une fois par fenêtre pour la même destination."""
    shown = ''.join(c for c in str(destination) if c.isprintable())[:200]
    refusals = log or _refusals
    if refusals.due(service, shown):
        audit.record(conn, 'outbound.refused', actor=audit.Actor.system(), target_type='service', target_id=service, ip=ip,
                     details={'destination': shown})
        # Marqué SEULEMENT si la ligne est validée : dans la transaction d'un appelant (pas encore commitée), une annulation
        # ne doit pas faire taire ce refus pendant toute la fenêtre
        if not conn.in_transaction:
            refusals.mark(service, shown)
    logger.warning("Destination refusée pour le service %s (hors OUTBOUND_ALLOWED_HOSTS)", service)


def guard(conn, cfg, service: str, destination: str, *, ip: Optional[str] = None) -> None:
    """À appeler juste avant un appel sortant : lève DestinationRefused (et audite) si la destination n'est pas autorisée."""
    if not allowed(cfg, service, destination):
        refuse(conn, service, destination, ip=ip)
        raise DestinationRefused()


# -- « Tester » neutre (EF-5.6, r4) -------------------------------------------------------------------------------

def _checked_timeout(value, low: float = 0.05, high: float = 120.0) -> float:
    """Délai du service : un nombre fini et strictement positif, borné ; sinon ValueError (jamais une réponse trompeuse)."""
    infinite = isinstance(value, float) and value in (float('inf'), float('-inf'))
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or infinite or value <= 0:
        raise ValueError("délai de test invalide")
    return min(high, max(low, float(value)))


def run_neutral_test(probe, timeout_s: float, *, clock=time.monotonic, sleep=time.sleep) -> dict:
    """
    Exécute `probe(timeout_s)` et répond `{'ok': bool}` et RIEN d'autre : ni code, ni message, ni délai distinctif. La
    réponse est rendue à l'échéance `timeout_s`, que la sonde réussisse tout de suite, soit refusée ou ne réponde
    jamais : un ok immédiat et un ko lent ne se distinguent pas par la durée (pas de sonde de ports interne).
    """
    timeout_s = _checked_timeout(timeout_s)
    started, outcome = clock(), {'ok': False}

    def work():
        try:
            outcome['ok'] = bool(probe(timeout_s))
        except Exception:  # noqa: BLE001 - toute panne de la sonde est un « ko » neutre
            outcome['ok'] = False

    worker = threading.Thread(target=work, name='neutral-test', daemon=True)
    worker.start()
    worker.join(timeout_s)
    ok = (not worker.is_alive()) and outcome['ok']
    remaining = timeout_s - (clock() - started)
    if remaining > 0:
        sleep(remaining)
    return {'ok': ok}
