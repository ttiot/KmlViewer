#!/usr/bin/env python3
"""
Tâche unique de l'hôte pour la couverture pilotée par la zone (routing-zone-donnees ZD-8 à ZD-11, TZ2-c).

Lancée par `routing/update.sh` (sous son verrou) quand ROUTING_ZONE_BUILD=1, depuis la racine du projet, une fois par minute
(minuterie systemd). Python standard seulement (3.9+), aucun paquet tiers. Seul acteur qui touche à Docker et au réseau de
téléchargement : l'application ne sait que répondre aux sous-commandes `flask routing-zone` (ZD-10).

Principes :
  - la sortie de `claim` est NON FIABLE : lue comme JSON, chaque champ contrôlé, plafonds recalculés avec la copie locale du
    catalogue et l'environnement de l'hôte ; rien n'est jamais passé à un shell (listes d'arguments, scripts constants,
    valeurs variables par `-e`) ;
  - les URL viennent de la copie locale du catalogue, jamais de l'application ; chaque redirection est recontrôlée ;
  - aucune coordonnée dans les journaux ;
  - l'état de l'hôte (catalogue, cache des extraits, budget du jour, étape en suspens, dalles de la tâche) vit dans le volume
    de routage (/data), jamais sur le système de fichiers de l'hôte.

Codes de sortie : 0 (passage terminé, y compris « rien à faire »), 2 (configuration invalide), 1 (erreur inattendue).
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

# -- Constantes ---------------------------------------------------------------------------------------------------------------

GRAPHHOPPER_IMAGE = 'kml-viewer-graphhopper:11.0'
GRAPH_RE = re.compile(r'graph-\d{14}')
ID_RE = re.compile(r'[a-z0-9][a-z0-9_-]{0,63}')
URL_RE = re.compile(r'^https://([A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?)(/[A-Za-z0-9._/-]{1,200})$')
PUBLISHED_RE = re.compile(r'-(\d{6})\.osm\.pbf$')
TILE_RE = re.compile(r'^([NS])(\d{2})([EW])(\d{3})\.tif$')
CLAIM_EXPIRY_S = 2 * 3600                    # une étape en suspens dont la prise date de plus de 2 h est abandonnée (ZD-10)
CATALOG_MAX_AGE_S = 7 * 86400
CATALOG_RETRY_S = 3600
EXTRACT_MAX_AGE_DAYS = 35                    # règle du même jour (ZD-9.5) et purge du cache (ZD-11)
MAX_CATALOG_BYTES = 16 * 1024 * 1024         # = la borne de l'application
MAX_ZONE_JSON_BYTES = 64 * 1024
REASONS = ('zone', 'mensuel', 'activation')
EXIT_BAD_CONFIG = 2
HTTP_UA = 'routing-update'                   # aucun identifiant du projet ni de l'exploitant

# Scripts exécutés dans le conteneur `routing-volume` (aucun réseau, utilisateur 10002) : CONSTANTS, les valeurs variables
# passent par -e. Les fichiers d'état sont écrits par rename (jamais un fichier à moitié écrit).
SCRIPT_SNAPSHOT = r'''
emit() { printf '@@%s\n' "$1"; if [ -f "$2" ]; then head -c 262144 -- "$2"; fi; printf '\n'; }
emit budget /state/budget.json
emit pending /state/pending-step.json
emit catalog_meta /state/catalog-meta.json
emit last /state/last-success.json
emit tiles /state/task-tiles.json
cur="$(readlink /data/graphs/current 2>/dev/null || true)"
printf '@@current\n%s\n' "$cur"
if [ -n "$cur" ] && [ -f "/data/graphs/$cur/zone.json" ]; then
  printf '@@zone\n'; head -c 65537 -- "/data/graphs/$cur/zone.json"; printf '\n'
fi
printf '@@extracts\n'
for f in /state/extracts/*.json; do [ -f "$f" ] && { head -c 4096 -- "$f"; printf '\n'; }; done
printf '@@files\n'
for f in /state/extracts/*.osm.pbf; do [ -f "$f" ] && stat -c '%n %s' -- "$f"; done
printf '@@end\n'
exit 0
'''
SCRIPT_WRITE = 'cat > "$F.tmp" && mv -f -- "$F.tmp" "$F"'
SCRIPT_REMOVE_FILE = 'rm -f -- "$F"'
SCRIPT_READ_CATALOG = 'head -c 33554432 -- /state/catalog.json'
SCRIPT_MKDIRS = 'mkdir -p -- "/state/work/$STAMP/cropped" /state/merged /state/extracts'
SCRIPT_STAT = 'stat -c %s -- "$F"'
SCRIPT_CLEAN_BUILD = ('rm -rf -- "/state/work/$STAMP"; '
                      '[ -z "$NEW" ] || rm -rf -- "/data/graphs/$NEW"; [ -z "$MERGED" ] || rm -f -- "/state/merged/$MERGED"')
SCRIPT_GRAPH_COMPLETE = 'test -s "/data/graphs/$NEW/properties"'
SCRIPT_SWITCH = r'''
cd /data/graphs
# Un moteur démarré avant le premier graphe crée un dossier current VIDE : le retirer (rmdir échoue s'il est non vide).
if [ -d current ] && [ ! -L current ]; then rmdir current; fi
test -d "$NEW" && ln -sfn "$NEW" current.tmp && mv -T current.tmp current
'''
SCRIPT_ROLLBACK = 'cd /data/graphs && test -d "$PREV" && ln -sfn "$PREV" current.tmp && mv -T current.tmp current'
SCRIPT_CLEANUP = r'''
cd /data/graphs
cur="$(readlink current || true)"
{ ls -1dt graph-* 2>/dev/null | grep -vxF -- "$cur" | tail -n +"$KEEP" || true; } | xargs -r rm -rf --
cd /state/merged 2>/dev/null && { ls -1t sources-*.osm.pbf 2>/dev/null | tail -n +2 || true; } | xargs -r rm -f --
rm -rf -- /state/work
find /state/extracts -maxdepth 1 -name '*.part' -mmin +1440 -delete 2>/dev/null || true      # reste d'un téléchargement interrompu (ZD-11)
exit 0
'''
SCRIPT_REMOVE_EXTRACT = 'rm -f -- "/state/extracts/$ID.osm.pbf" "/state/extracts/$ID.json"'
# Téléchargement (seule tâche avec réseau) : PAS de -L (aucune redirection : l'URL est déjà résolue et contrôlée par l'hôte),
# octets lus bornés par CAP (le Content-Length n'est pas fiable), somme MD5 contrôlée, rename à la fin.
SCRIPT_FETCH = r'''
cd /state/extracts
LIM=$((CAP + 1))
set +e
timeout "$TMO" curl -fsS --proto =https --connect-timeout 20 -- "$URL" | head -c "$LIM" > "$NAME.part"
rc=${PIPESTATUS[0]}
set -e
size="$(stat -c %s -- "$NAME.part")"
echo "$size"
if [ "$size" -gt "$CAP" ]; then rm -f -- "$NAME.part"; exit 40; fi
if [ "$rc" != 0 ]; then rm -f -- "$NAME.part"; exit 41; fi
actual="$(md5sum -- "$NAME.part" | cut -d' ' -f1)"
if [ "$actual" != "$MD5" ]; then rm -f -- "$NAME.part"; exit 42; fi
mv -f -- "$NAME.part" "$NAME"
'''


class ConfigError(Exception):
    pass


class PassEnd(Exception):
    """Réponse hors contrat (code ≠ 0/2/3 de l'application, désactivé…) : fin du passage, journalisée (ZD-10)."""


class BuildFailed(Exception):
    """Échec d'une tâche prise : `code` est l'un des codes de `failed` (ZD-9)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def log(message: str) -> None:
    print(f"{dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} routing-zone: {message}", flush=True)


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def iso(when: dt.datetime) -> str:
    return when.strftime('%Y-%m-%dT%H:%M:%SZ')


def parse_iso(text) -> dt.datetime | None:
    try:
        return dt.datetime.strptime(text, '%Y-%m-%dT%H:%M:%SZ')
    except (TypeError, ValueError):
        return None


# -- Configuration : environnement > .env > défaut (le .env n'est JAMAIS exécuté) ----------------------------------------------

def dotenv(key: str) -> str:
    try:
        with open('.env', encoding='utf-8') as handle:
            lines = [line for line in handle.read().splitlines() if line.startswith(key + '=')]
    except OSError:
        return ''
    value = lines[-1].split('=', 1)[1].strip() if lines else ''
    if value[:1] in ('"', "'"):                              # entre guillemets : jusqu'au guillemet fermant, le reste est ignoré
        end = value.find(value[0], 1)
        return value[1:end] if end > 0 else value
    return re.split(r'\s+#', value, maxsplit=1)[0].strip()      # commentaire en fin de ligne, comme compose


def cfg(key: str, default: str = '') -> str:
    value = os.environ[key] if key in os.environ else dotenv(key)
    return value if value != '' else default


def cfg_int(key: str, default: int, low: int, high: int) -> int:
    raw = cfg(key, str(default))
    if not re.fullmatch(r'[0-9]{1,15}', raw) or not low <= int(raw) <= high:
        raise ConfigError(f"{key} doit être un entier de {low} à {high}")
    return int(raw)


def cfg_list(key: str, default: str) -> list:
    return [item for item in re.split(r'[\s,]+', cfg(key, default)) if item]


class Config:
    def __init__(self) -> None:
        self.catalog_url = cfg('ROUTING_CATALOG_URL', 'https://download.geofabrik.de/index-v1.json')
        self.root = cfg('ROUTING_CATALOG_ROOT', 'europe').strip()
        self.exclude = set(cfg_list('ROUTING_CATALOG_EXCLUDE',
                                    'alps,britain-and-ireland,dach,great-britain,guadeloupe,martinique,mayotte,reunion'))
        self.hosts = cfg_list('ROUTING_ALLOWED_HOSTS', 'download.geofabrik.de')
        self.max_regions = cfg_int('ROUTING_MAX_REGIONS', 20, 1, 50)
        self.max_source_bytes = cfg_int('ROUTING_MAX_SOURCE_BYTES', 4 * 2 ** 30, 1, 2 ** 40)
        self.max_tiles = cfg_int('DEM_MAX_TILES', 60, 1, 1000)
        self.max_import_bytes = cfg_int('ROUTING_MAX_IMPORT_BYTES', 2_400_000_000, 1, 2 ** 40)
        self.max_builds_per_day = cfg_int('ROUTING_MAX_BUILDS_PER_DAY', 6, 1, 100)
        self.max_bytes_per_day = cfg_int('ROUTING_MAX_DOWNLOAD_BYTES_PER_DAY', 12 * 2 ** 30, 1, 2 ** 45)
        self.cache_max_bytes = cfg_int('ROUTING_CACHE_MAX_BYTES', 8 * 2 ** 30, 0, 2 ** 45)
        self.keep_graphs = cfg_int('ROUTING_KEEP_GRAPHS', 2, 2, 20)
        self.health_timeout = cfg_int('ROUTING_HEALTH_TIMEOUT', 300, 5, 3600)
        self.monthly_days = cfg_int('ROUTING_MONTHLY_DAYS', 30, 1, 365)
        self.step_timeout = cfg_int('ROUTING_STEP_TIMEOUT', 7200, 60, 86400)      # recadrage, fusion, import, dalles : par étape
        # Étape MNT : seulement si DEM_URL désigne le service opentopodata LOCAL du compose (jamais l'API publique) ;
        # même règle que coverage_state.dem_managed côté application
        self.dem_enabled = (urllib.parse.urlsplit(cfg('DEM_URL')).hostname or '') == 'opentopodata'
        for host in self.hosts:
            if not re.fullmatch(r'[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?', host):
                raise ConfigError("ROUTING_ALLOWED_HOSTS : hôte invalide")
        if not self.hosts:
            raise ConfigError("ROUTING_ALLOWED_HOSTS est vide")
        if not ID_RE.fullmatch(self.root):
            raise ConfigError("ROUTING_CATALOG_ROOT invalide")
        if not all(ID_RE.fullmatch(e) for e in self.exclude):
            raise ConfigError("ROUTING_CATALOG_EXCLUDE invalide")
        check_url(self.catalog_url, self.hosts, suffix=None)


def check_url(url, hosts, suffix: str | None = '.osm.pbf') -> str:
    """https, hôte exact de la liste, chemin simple (ni `..` ni `//`), extension attendue ; lève ValueError."""
    match = URL_RE.fullmatch(url) if isinstance(url, str) and len(url) <= 300 else None
    if not match or match.group(1) not in hosts:
        raise ValueError("URL refusée (https, hôte autorisé, chemin simple)")
    path = match.group(3)
    if '..' in path or '//' in path or (suffix and not path.endswith(suffix)):
        raise ValueError("URL refusée (chemin invalide)")
    return url


# -- Docker --------------------------------------------------------------------------------------------------------------------

class Docker:
    """Toutes les commandes : listes d'arguments, jamais de shell."""

    def __init__(self, config: Config, stamp: str):
        self.config = config
        self.stamp = stamp
        profiles = ['routing', 'routing-tools'] + (['dem'] if config.dem_enabled else [])
        self.compose = ['docker', 'compose'] + [a for p in profiles for a in ('--profile', p)]

    def run(self, args, *, input: bytes | None = None, timeout: int | None = 300, env: dict | None = None):
        full_env = dict(os.environ, **(env or {}))
        return subprocess.run(args, input=input, capture_output=True, timeout=timeout, env=full_env, check=False)

    def tool(self, service: str, args=(), *, env: dict | None = None, input: bytes | None = None, timeout: int | None = None,
             compose_env: dict | None = None, name: str | None = None):
        """Tâche ponctuelle : `docker compose run --rm -T [-e K=V …] service args` ; arrêtée et supprimée au dépassement."""
        container = f"kml-routing-{name or service}-{self.stamp}"
        cmd = self.compose + ['run', '--rm', '-T', '--name', container]
        for key, value in (env or {}).items():
            cmd += ['-e', f"{key}={value}"]
        cmd += [service] + list(args)
        try:
            return self.run(cmd, input=input, timeout=timeout or self.config.step_timeout, env=compose_env)
        except subprocess.TimeoutExpired:
            self.run(['docker', 'rm', '-f', container], timeout=60)
            raise

    @staticmethod
    def tail(done, lines: int = 5) -> str:
        """Fin de la sortie d'erreur d'un conteneur pour le journal, sans nombre décimal (coordonnées)."""
        text = (done.stderr or done.stdout or b'').decode('utf-8', 'replace')
        kept = [re.sub(r'-?\d+\.\d+', '#', line.strip())[:200] for line in text.splitlines() if line.strip()][-lines:]
        return ' | '.join(kept)

    def vol(self, script: str, env: dict | None = None, input: bytes | None = None, timeout: int = 600):
        return self.tool('routing-volume', [script], env=env, input=input, timeout=timeout, name='volume')

    def vol_ok(self, script: str, env: dict | None = None, input: bytes | None = None, timeout: int = 600) -> bytes:
        done = self.vol(script, env, input, timeout)
        if done.returncode != 0:
            raise RuntimeError("opération sur le volume en échec")
        return done.stdout

    def write_state(self, path: str, obj) -> None:
        self.vol_ok(SCRIPT_WRITE, {'F': path}, json.dumps(obj, separators=(',', ':')).encode())

    def remove_state(self, path: str) -> None:
        self.vol_ok(SCRIPT_REMOVE_FILE, {'F': path})

    def app(self, *args, input: bytes | None = None):
        """`flask routing-zone …` dans le conteneur de l'application ; (code, sortie standard). Code 255 : pas de réponse."""
        cmd = ['docker', 'compose', 'exec', '-T', 'kml-viewer', 'flask', 'routing-zone'] + [str(a) for a in args]
        try:
            done = self.run(cmd, input=input, timeout=180)
        except subprocess.TimeoutExpired:
            log(f"routing-zone {args[0]} : délai dépassé")
            return 255, ''
        if done.returncode not in (0, 2, 3, 4):
            log(f"routing-zone {args[0]} : code {done.returncode}")
        return done.returncode, done.stdout.decode('utf-8', 'replace')


# -- Réseau (hôte) -------------------------------------------------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):          # les redirections sont suivies À LA MAIN (http)
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def http(url: str, hosts, *, method: str = 'GET', limit: int = 1024, suffix: str | None = '.osm.pbf', redirects: int = 2):
    """(URL finale, Content-Length ou None, corps). Au plus 2 redirections, https, hôte recontrôlé à CHAQUE saut."""
    for _ in range(redirects + 1):
        check_url(url, hosts, suffix=None)
        request = urllib.request.Request(url, method=method, headers={'User-Agent': HTTP_UA})
        try:
            with _OPENER.open(request, timeout=30) as response:
                length = response.headers.get('Content-Length')
                body = response.read(limit + 1) if method == 'GET' else b''
                if len(body) > limit:
                    raise ValueError("réponse trop volumineuse")
                if suffix:
                    check_url(url, hosts, suffix=suffix)
                return url, (int(length) if length and length.isdigit() else None), body
        except urllib.error.HTTPError as exc:
            location = exc.headers.get('Location') if 300 <= exc.code < 400 else None
            if not location:
                raise
            url = urllib.parse.urljoin(url, location)
    raise ValueError("trop de redirections")


def published_date(url: str) -> str:
    match = PUBLISHED_RE.search(url)
    if not match:
        raise ValueError("date de publication absente de l'URL")
    return match.group(1)


def date_of(yymmdd: str) -> dt.date:
    return dt.datetime.strptime('20' + yymmdd, '%Y%m%d').date()


# -- Catalogue (copie locale de l'hôte, ZD-9.2) --------------------------------------------------------------------------------

def candidates_of(document, root: str, exclude, hosts):
    """
    (candidates {id: url}, noeuds {id: (parent, feature)}) : feuilles du sous-arbre `root` moins `exclude`. Lève ValueError.
    Un cycle ou un identifiant double refuse le catalogue ; une feuille candidate sans `shp` est signalée (indice d'agrégat).
    """
    features = document.get('features') if isinstance(document, dict) else None
    if not isinstance(features, list) or not features or len(features) > 5000:
        raise ValueError("catalogue : liste de régions attendue")
    nodes, children = {}, {}
    for feature in features:
        props = feature.get('properties') if isinstance(feature, dict) else None
        rid = props.get('id') if isinstance(props, dict) else None
        if not isinstance(rid, str) or not ID_RE.fullmatch(rid) or rid in nodes:
            raise ValueError("catalogue : identifiant invalide ou double")
        parent = props.get('parent')
        nodes[rid] = (parent if isinstance(parent, str) else None, feature)
    for rid, (parent, _) in nodes.items():
        children.setdefault(parent, []).append(rid)
    if root not in nodes:
        raise ValueError("catalogue : racine absente")
    leaves, subtree, stack = [], {root}, [root]
    while stack:
        for child in children.get(stack.pop(), []):
            if child in subtree:
                raise ValueError("catalogue : arborescence invalide (cycle)")
            subtree.add(child)
            (stack if child in children else leaves).append(child)
    out, no_shp = {}, []
    for rid in sorted(set(leaves) - set(exclude)):
        urls = nodes[rid][1]['properties'].get('urls')
        url = urls.get('pbf') if isinstance(urls, dict) else None
        out[rid] = check_url(url, hosts)
        if not (isinstance(urls, dict) and urls.get('shp')):
            no_shp.append(rid)
    if no_shp:                                             # indice d'un agrégat : une seule ligne, jamais une par région
        log(f"AVERTISSEMENT : {len(no_shp)} région(s) sans fichier shp, agrégat possible à exclure "
            f"(ROUTING_CATALOG_EXCLUDE) : {', '.join(no_shp[:10])}{', …' if len(no_shp) > 10 else ''}")
    if not out:
        raise ValueError("catalogue : aucune région candidate")
    return out, nodes, subtree


def head_size(url: str, hosts) -> int:
    last = None
    for _ in range(2):
        try:
            _, length, _ = http(url, hosts, method='HEAD')
            if length is not None and length >= 0:
                return length
            last = ValueError("taille absente")
        except (OSError, ValueError, urllib.error.URLError) as exc:
            last = exc
    raise ValueError(f"taille illisible ({type(last).__name__})")


def refresh_catalog(docker: Docker, config: Config, now: dt.datetime) -> None:
    """Index Geofabrik + tailles (HEAD) -> contrôle -> copie locale -> `catalog -`. Ancien catalogue gardé en cas d'échec."""
    _, _, body = http(config.catalog_url, config.hosts, limit=MAX_CATALOG_BYTES, suffix=None)
    document = json.loads(body)
    candidates, nodes, subtree = candidates_of(document, config.root, config.exclude, config.hosts)
    with ThreadPoolExecutor(max_workers=4) as pool:
        sizes = dict(zip(candidates, pool.map(lambda rid: head_size(candidates[rid], config.hosts), candidates)))
    features = []
    for rid in sorted(subtree):
        parent, feature = nodes[rid]
        props = {'id': rid, 'parent': parent}
        geometry = None
        if rid in candidates:
            props.update(urls={'pbf': candidates[rid]}, size=sizes[rid])
            geometry = feature.get('geometry')
        features.append({'properties': props, 'geometry': geometry})
    local = {'fetched_at': iso(now), 'regions': {rid: {'url': candidates[rid], 'size': sizes[rid]} for rid in candidates}}
    payload = json.dumps({'features': features}, separators=(',', ':')).encode()
    if len(payload) > MAX_CATALOG_BYTES:
        raise ValueError("catalogue trop volumineux pour l'application")
    code, _ = docker.app('catalog', '-', input=payload)
    if code in (3, 4) or code not in (0, 2):
        raise PassEnd(f"catalog : code {code}")
    if code != 0:
        raise ValueError("catalogue refusé par l'application (contenu invalide, ancien gardé)")
    docker.write_state('/state/catalog.json', local)                       # copie locale : seulement après l'accord de l'application
    docker.write_state('/state/catalog-meta.json', {'loaded_at': iso(now), 'attempt_at': iso(now)})
    log(f"catalogue chargé : {len(candidates)} régions")


# -- État de l'hôte dans le volume --------------------------------------------------------------------------------------------

def snapshot(docker: Docker) -> dict:
    out = docker.vol_ok(SCRIPT_SNAPSHOT).decode('utf-8', 'replace')
    sections, name = {}, None
    for line in out.splitlines():
        if line.startswith('@@'):
            name = line[2:]
            sections[name] = []
        elif name is not None:
            sections[name].append(line)

    def loads(key, default):
        text = '\n'.join(sections.get(key, [])).strip()
        try:
            value = json.loads(text) if text else default
        except ValueError:
            return default
        return value if isinstance(value, type(default)) else default

    extracts = {}
    for line in sections.get('extracts', []):
        try:
            meta = json.loads(line)
            if isinstance(meta, dict) and ID_RE.fullmatch(str(meta.get('id'))):
                extracts[meta['id']] = meta
        except ValueError:
            continue
    sizes = {}
    for line in sections.get('files', []):
        match = re.fullmatch(r'/state/extracts/([a-z0-9][a-z0-9_-]{0,63})\.osm\.pbf (\d+)', line)
        if match:
            sizes[match.group(1)] = int(match.group(2))
    current = ''.join(sections.get('current', [])).strip()
    zone = '\n'.join(sections['zone']).encode() if 'zone' in sections else None
    return {'budget': loads('budget', {}), 'pending': loads('pending', {}), 'catalog_meta': loads('catalog_meta', {}),
            'last': loads('last', {}), 'tiles': loads('tiles', {}), 'current': current if GRAPH_RE.fullmatch(current) else '',
            'zone': zone, 'extracts': extracts, 'files': sizes}


def budget_today(snap: dict, now: dt.datetime) -> tuple:
    budget = snap['budget']
    if budget.get('day') != now.strftime('%Y-%m-%d'):
        return 0, 0
    builds, size = budget.get('builds'), budget.get('bytes')
    return (builds if isinstance(builds, int) else 0), (size if isinstance(size, int) else 0)


# -- Dalles (ZD-7, ZD-8) -------------------------------------------------------------------------------------------------------

def tile_range(low: float, high: float, last: int) -> range:
    first = math.floor(low)
    return range(first, min(max(first, math.ceil(high) - 1), last) + 1)


def tiles_for(bbox) -> list:
    """Dalles 1° × 1° (coin sud-ouest [lat, lon]) coupant l'emprise [lon_min, lat_min, lon_max, lat_max] (= dem_tiles)."""
    return [[lat, lon] for lat in tile_range(bbox[1], bbox[3], 89) for lon in tile_range(bbox[0], bbox[2], 179)]


def tile_name(lat: int, lon: int) -> str:
    return f"{'N' if lat >= 0 else 'S'}{abs(lat):02d}{'E' if lon >= 0 else 'W'}{abs(lon):03d}.tif"


def tile_of(name: str):
    match = TILE_RE.fullmatch(name)
    if not match:
        return None
    lat, lon = int(match.group(2)), int(match.group(4))
    return [lat if match.group(1) == 'N' else -lat, lon if match.group(3) == 'E' else -lon]


# -- Sortie de `claim` : non fiable --------------------------------------------------------------------------------------------

def _finite(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def validate_claim(raw: str, local_regions: dict, config: Config) -> dict:
    """
    JSON lu par un outil, chaque champ contrôlé, plafonds recalculés avec la copie locale et l'environnement de l'hôte.
    Lève BuildFailed('other') pour une sortie invalide, BuildFailed('cap') pour un plafond dépassé.
    """
    try:
        data = json.loads(raw)
        request_id = data['request_id']
        if isinstance(request_id, bool) or not isinstance(request_id, int) or request_id < 1:
            raise ValueError("numéro")
        if data['reason'] not in REASONS:
            raise ValueError("motif")
        margin = data['margin_km']
        if isinstance(margin, bool) or not isinstance(margin, int) or not 0 <= margin <= 50:
            raise ValueError("marge")
        polygon = data['polygon']
        if (not isinstance(polygon, list) or not 3 <= len(polygon) <= 50
                or not all(isinstance(p, list) and len(p) == 2 and all(_finite(n) for n in p) and abs(p[0]) <= 180
                           and abs(p[1]) <= 90 for p in polygon)):
            raise ValueError("polygone")
        bbox = data['extract_bbox']
        if (not isinstance(bbox, list) or len(bbox) != 4 or not all(_finite(n) for n in bbox)
                or not (-180 <= bbox[0] < bbox[2] <= 180 and -90 <= bbox[1] < bbox[3] <= 90)):
            raise ValueError("emprise")
        regions = data['regions']
        if (not isinstance(regions, list) or not regions or len(set(regions)) != len(regions)
                or not all(isinstance(r, str) and ID_RE.fullmatch(r) and r in local_regions for r in regions)):
            raise ValueError("régions")
    except (ValueError, KeyError, TypeError):
        raise BuildFailed('other', "sortie de claim invalide") from None
    tiles = tiles_for(bbox)
    total = sum(local_regions[r]['size'] for r in regions)
    if (len(regions) > config.max_regions or total > config.max_source_bytes
            or (config.dem_enabled and len(tiles) > config.max_tiles)):          # plafond des dalles : seulement si la tâche les gère
        raise BuildFailed('cap', "plafonds de l'hôte dépassés")
    return {'request_id': request_id, 'reason': data['reason'], 'margin_km': margin, 'polygon': polygon,
            'bbox': [float(n) for n in bbox], 'regions': sorted(regions), 'tiles': tiles}


# -- La tâche ------------------------------------------------------------------------------------------------------------------

class Task:
    def __init__(self, docker: Docker, config: Config, stamp: str):
        self.docker, self.config, self.stamp = docker, config, stamp
        self.new_graph = f"graph-{stamp}"
        self.merged_name = ''
        self.local_catalog: dict = {}

    # -- extraits : même jour, cache, téléchargement ----------------------------------------------------------------------
    def resolve(self, claim: dict, snap: dict, monthly: bool) -> dict:
        """HEAD de chaque extrait : (URL datée, taille, date) ; règle du même jour (ZD-9.5)."""
        local = self.local_catalog
        found, today = {}, utcnow().date()
        for rid in claim['regions']:
            try:
                url, length, _ = http(local[rid]['url'], self.config.hosts, method='HEAD')
                if length is None:
                    raise ValueError("taille absente")
                found[rid] = {'url': url, 'size': length, 'date': published_date(url)}
            except (OSError, ValueError, urllib.error.URLError):
                raise BuildFailed('download', f"extrait {rid} injoignable ou redirigé hors des hôtes autorisés") from None
        dates = {f['date'] for f in found.values()}
        if len(dates) != 1:
            raise BuildFailed('download', "extraits publiés à des dates différentes")
        day = dates.pop()
        try:
            age = (today - date_of(day)).days
        except ValueError:
            raise BuildFailed('download', "date de publication illisible") from None
        if age > EXTRACT_MAX_AGE_DAYS or age < -1:
            raise BuildFailed('download', "extraits trop anciens")
        cached = {rid: snap['extracts'][rid] for rid in claim['regions']
                  if rid in snap['extracts'] and snap['files'].get(rid) == snap['extracts'][rid].get('size')}
        reuse_ok = not monthly and all(m.get('date') == day for m in cached.values())
        for rid, info in found.items():
            info['fetch'] = not (reuse_ok and rid in cached and cached[rid].get('date') == day
                                 and cached[rid].get('size') == info['size'])
        return {'day': day, 'regions': found}

    def fetch(self, plan: dict, account=None) -> int:
        """Télécharge ce qui manque, par fichier ET en cumulé contre ROUTING_MAX_SOURCE_BYTES ; retourne les octets reçus."""
        received = 0
        for rid in sorted(plan['regions']):
            info = plan['regions'][rid]
            if not info['fetch']:
                continue
            cap = min(info['size'] + 1048576, self.config.max_source_bytes - received)
            if info['size'] > cap:
                raise BuildFailed('cap', "total des extraits au-delà de ROUTING_MAX_SOURCE_BYTES")
            try:
                _, _, body = http(info['url'] + '.md5', self.config.hosts, suffix='.osm.pbf.md5', limit=1024)
                md5 = body.decode('ascii', 'replace').split()[0].lower()
            except (OSError, ValueError, IndexError, urllib.error.URLError):
                raise BuildFailed('download', f"somme MD5 de {rid} illisible") from None
            if not re.fullmatch(r'[0-9a-f]{32}', md5):
                raise BuildFailed('download', f"somme MD5 de {rid} invalide")
            log(f"téléchargement de {rid}")
            done = None
            for _ in range(2):
                done = self.docker.tool('routing-fetch', [SCRIPT_FETCH], name='fetch', timeout=self.config.step_timeout + 600,
                                        env={'URL': info['url'], 'NAME': f"{rid}.osm.pbf", 'CAP': str(cap), 'MD5': md5,
                                             'TMO': str(self.config.step_timeout)})
                received += self._seen(done)                    # octets réellement reçus, essais compris (budget)
                if account:
                    account(received)
                if done.returncode not in (41, 42):
                    break
            if done.returncode == 40:
                raise BuildFailed('cap', f"extrait {rid} au-delà de la taille attendue")
            if done.returncode != 0:
                log(f"fetch {rid} : {self.docker.tail(done)}")
                raise BuildFailed('download', f"téléchargement ou somme de contrôle de {rid}")
            size = self._seen(done)
            self.docker.write_state(f"/state/extracts/{rid}.json", {'id': rid, 'date': info['date'], 'md5': md5, 'size': size,
                                                                   'fetched_at': iso(utcnow())})
        return received

    @staticmethod
    def _seen(done) -> int:
        try:
            return int(done.stdout.decode().split()[-1])
        except (ValueError, IndexError):
            return 0

    # -- recadrage, fusion, import ------------------------------------------------------------------------------------------
    def crop_merge(self, claim: dict, day: str) -> None:
        bbox = ','.join(f"{n:.4f}" for n in claim['bbox'])
        for rid in claim['regions']:                      # chaque extrait AVANT la fusion : jamais l'inverse (« Node ID twice »)
            log(f"recadrage de {rid}")
            done = self.docker.tool('routing-merge', ['extract', '-b', bbox, f"/state/extracts/{rid}.osm.pbf", '-o',
                                                      f"/state/work/{self.stamp}/cropped/{rid}.osm.pbf", '--overwrite'],
                                    name='extract')
            if done.returncode != 0:
                log(f"osmium extract {rid} : {self.docker.tail(done)}")
                raise BuildFailed('extract', f"recadrage de {rid}")
        self.merged_name = f"sources-20{day}.osm.pbf"
        log("fusion des extraits recadrés")
        inputs = [f"/state/work/{self.stamp}/cropped/{rid}.osm.pbf" for rid in claim['regions']]
        done = self.docker.tool('routing-merge', ['merge'] + inputs + ['-o', f"/state/merged/{self.merged_name}", '--overwrite'],
                                name='merge')
        if done.returncode != 0:
            log(f"osmium merge : {self.docker.tail(done)}")
            raise BuildFailed('merge', "fusion osmium")
        self.docker.vol_ok('rm -rf -- "/state/work/$STAMP"', {'STAMP': self.stamp})
        try:
            size = int(self.docker.vol_ok(SCRIPT_STAT, {'F': f"/state/merged/{self.merged_name}"}).decode().strip())
        except ValueError:
            raise BuildFailed('merge', "fichier fusionné illisible") from None
        if size > self.config.max_import_bytes:
            raise BuildFailed('cap', "fichier fusionné au-delà de ROUTING_MAX_IMPORT_BYTES")

    def import_graph(self, claim: dict, day: str) -> None:
        env = {'ROUTING_IMPORT_PBF': f"/state/merged/{self.merged_name}", 'ROUTING_IMPORT_GRAPH': f"/data/graphs/{self.new_graph}"}
        log(f"import -> graphs/{self.new_graph}")
        done = self.docker.tool('routing-import', name='import', compose_env=env)
        if done.returncode != 0:
            log(f"import : {self.docker.tail(done)}")
            raise BuildFailed('import', "import GraphHopper")
        if self.docker.vol(SCRIPT_GRAPH_COMPLETE, {'NEW': self.new_graph}).returncode != 0:
            raise BuildFailed('incomplete', "graphe produit incomplet")
        zone = {'polygon': claim['polygon'], 'extract_bbox': claim['bbox'], 'margin_km': claim['margin_km'],
                'regions': claim['regions'], 'data_date': f"20{day[:2]}-{day[2:4]}-{day[4:]}", 'request_id': claim['request_id']}
        self.docker.write_state(f"/data/graphs/{self.new_graph}/zone.json", zone)

    # -- dalles ---------------------------------------------------------------------------------------------------------------
    def list_tiles(self) -> set:
        done = self.docker.run(self.docker.compose + ['run', '--rm', '-T', '--entrypoint', 'sh', 'dem-tiles', '-c',
                                                      'ls -1 /dem/copernicus30 2>/dev/null || true'], timeout=300)
        if done.returncode != 0:
            raise BuildFailed('dem', "liste des dalles illisible")
        return {tuple(t) for t in map(tile_of, done.stdout.decode().split()) if t}

    def fetch_tiles(self, claim: dict, snap: dict) -> dict:
        """Télécharge les dalles manquantes ; retourne {'added': [...], 'owned': registre à jour}. Étape sautée sans MNT."""
        owned = {tuple(t[:2]): t[2] for t in snap['tiles'].get('tiles', []) if isinstance(t, list) and len(t) == 3}
        if not self.config.dem_enabled:
            return {'added': [], 'owned': owned}
        before = self.list_tiles()
        lat_min, lon_min, lat_max, lon_max = claim['bbox'][1], claim['bbox'][0], claim['bbox'][3], claim['bbox'][2]
        log(f"dalles MNT : {len(claim['tiles'])} dans l'emprise")
        done = self.docker.tool('dem-tiles', name='dem', env={'DEM_BBOX': f"{lat_min:.4f},{lon_min:.4f},{lat_max:.4f},{lon_max:.4f}"})
        if done.returncode != 0:
            log(f"dem-tiles : {self.docker.tail(done)}")
            try:                                           # les dalles déjà reçues sont à la tâche : registre à jour avant d'échouer
                got = sorted(self.list_tiles() - before)
                for tile in got:
                    owned[tile] = iso(utcnow())
                if got:
                    self.docker.write_state('/state/task-tiles.json', {'tiles': [[t[0], t[1], d] for t, d in sorted(owned.items())]})
            except (BuildFailed, RuntimeError, OSError, subprocess.SubprocessError):
                log("avertissement : dalles partielles non enregistrées")
            raise BuildFailed('dem', "téléchargement des dalles")
        after = self.list_tiles()
        added = sorted(after - before)
        now = iso(utcnow())
        for tile in added:
            owned[tile] = now                              # ZD-8 : seules les dalles créées par la tâche sont à elle
        return {'added': [list(t) for t in added], 'owned': owned}

    # -- nettoyage d'un échec avant bascule ------------------------------------------------------------------------------------
    def discard(self) -> None:
        try:
            self.docker.vol(SCRIPT_CLEAN_BUILD, {'STAMP': self.stamp, 'NEW': self.new_graph, 'MERGED': self.merged_name})
        except Exception as exc:  # noqa: BLE001 - le nettoyage ne doit jamais masquer l'échec d'origine
            log(f"avertissement : nettoyage incomplet ({type(exc).__name__})")


# -- Bascule, santé, done (ZD-9.9, ZD-9.10) --------------------------------------------------------------------------------------

def wait_healthy(docker: Docker, service: str, timeout_s: int) -> bool:
    for _ in range(max(1, timeout_s // 5)):
        done = docker.run(docker.compose + ['ps', '-q', service], timeout=60)
        cid = done.stdout.decode().strip().splitlines()
        if cid:
            state = docker.run(['docker', 'inspect', '--format',
                                '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}', cid[0]], timeout=60)
            if state.stdout.decode().strip() == 'healthy':
                return True
        subprocess.run(['sleep', '5'], check=False)
    return False


def restart_engine(docker: Docker) -> None:
    docker.run(docker.compose + ['up', '-d', '--no-deps', '--force-recreate', 'graphhopper'], timeout=300)


def purge_plan(config: Config, regions: list, extracts: dict, owned: dict, bbox: list, now: dt.datetime):
    """ZD-8 (dalles) et ZD-11 (extraits) : listes à supprimer ; l'actif n'est jamais touché."""
    keep_tiles = {tuple(t) for t in tiles_for(bbox)}
    surplus = len(owned) - config.max_tiles
    tiles = []
    if config.dem_enabled and surplus > 0:                                       # seules les dalles de la tâche, hors de la zone, les plus anciennes d'abord
        tiles = [t for t, _ in sorted(((t, d) for t, d in owned.items() if t not in keep_tiles), key=lambda x: (x[1], x[0]))][:surplus]
    old, total = [], sum(m.get('size', 0) for m in extracts.values())
    limit = now.date() - dt.timedelta(days=EXTRACT_MAX_AGE_DAYS)
    removable = sorted((m for rid, m in extracts.items() if rid not in regions), key=lambda m: (m.get('fetched_at') or '', m['id']))
    def is_expired(meta) -> bool:
        try:
            return date_of(meta['date']) < limit
        except (KeyError, ValueError):
            return True
    for meta in removable:                                 # d'abord tous les extraits expirés (> 35 jours)…
        if is_expired(meta):
            old.append(meta['id'])
            total -= meta.get('size', 0)
    for meta in removable:                                 # …puis les plus anciens tant que le plafond est dépassé
        if total <= config.cache_max_bytes:
            break
        if meta['id'] not in old:
            old.append(meta['id'])
            total -= meta.get('size', 0)
    return tiles, old


def finish(docker: Docker, config: Config, step: dict, snap: dict) -> str:
    """
    Reprend une étape en suspens (ou enchaîne après la construction) : `switching` -> bascule -> santé -> `done`.
    Retourne 'done', 'stale', 'disabled', 'retry' (transitoire : fin du passage, étape gardée) ou 'failed'.
    Journal d'appels : jamais `done` sans `switching` à 0 pour la même demande.
    """
    request_id, new = step['request_id'], step['graph']
    if not GRAPH_RE.fullmatch(new) or not (isinstance(step.get('previous'), str)
                                           and (step['previous'] == '' or GRAPH_RE.fullmatch(step['previous']))):
        raise RuntimeError("étape en suspens invalide")

    stamp = step.get('stamp') if re.fullmatch(r'\d{14}', str(step.get('stamp'))) else 'none'
    merged = step.get('merged') if re.fullmatch(r'sources-\d{8}\.osm\.pbf', str(step.get('merged'))) else ''
    tiles_added, purged_tiles = step.get('tiles_added') or [], step.get('purged_tiles') or []
    owned_all = [t for t in (step.get('owned') or []) if isinstance(t, list) and len(t) == 3]
    purged_ids = [r for r in (step.get('purged_extract_ids') or []) if isinstance(r, str) and ID_RE.fullmatch(r)]

    def discard_new() -> None:
        docker.vol(SCRIPT_CLEAN_BUILD, {'STAMP': stamp, 'NEW': new, 'MERGED': merged})

    if step['stage'] == 'switching':
        code, _ = docker.app('switching', request_id)
        if code in (3, 4):
            if snap['current'] != new:                     # jamais le graphe en service (passage tué après la bascule)
                discard_new()
            docker.remove_state('/state/pending-step.json')
            log("demande dépassée : graphe neuf supprimé" if code == 3 else "désactivé : graphe neuf supprimé")
            return 'stale' if code == 3 else 'disabled'
        if code != 0:
            log("switching : réponse transitoire, étape gardée pour le prochain passage")
            return 'retry'
        log(f"bascule : current -> {new} (précédent : {step['previous'] or 'aucun'})")
        if docker.vol(SCRIPT_SWITCH, {'NEW': new}).returncode != 0:
            # erreur locale, qui ne se corrige pas seule : rien n'a basculé ; l'application (en `starting`) doit être libérée
            log("bascule impossible (graphs/current est un dossier non vide ?)")
            # le conteneur a pu échouer APRÈS le mv : ne jamais supprimer le graphe désormais en service.
            # Limite connue (TZ2c-n8, non traitée, cas très rare) : si current désigne déjà le graphe neuf, on garde le graphe mais
            # `failed --code other` part sans redémarrer le moteur ; il chargera le neuf à son prochain redémarrage.
            if docker.vol('[ "$(readlink /data/graphs/current 2>/dev/null)" != "$NEW" ]', {'NEW': new}).returncode == 0:
                discard_new()
            docker.remove_state('/state/pending-step.json')
            code, _ = docker.app('failed', request_id, '--code', 'other')
            if code not in (0, 3):
                log(f"failed : code {code}, la prise expirera au bout de 2 h")
            return 'failed'
        restart_engine(docker)
        if tiles_added and config.dem_enabled:
            docker.run(docker.compose + ['up', '-d', '--no-deps', '--force-recreate', 'opentopodata'], timeout=300)
        if not wait_healthy(docker, 'graphhopper', config.health_timeout):
            log(f"le moteur n'est pas sain après {config.health_timeout} s : retour arrière")
            if step['previous']:
                if docker.vol(SCRIPT_ROLLBACK, {'PREV': step['previous']}).returncode == 0:
                    restart_engine(docker)
                    discard_new()
                else:
                    log("retour arrière impossible : graphe neuf conservé pour analyse")
            docker.remove_state('/state/pending-step.json')
            code, _ = docker.app('failed', request_id, '--code', 'health')
            if code not in (0, 3):
                log(f"failed : code {code}, la prise expirera au bout de 2 h")
            return 'failed'
        step = dict(step, stage='done')
        docker.write_state('/state/pending-step.json', step)
    # stage 'done' : le graphe est en service (santé atteinte) ; rejouable tant que l'application ne répond pas 0, 3 ou 4
    kept = []
    for tile in purged_tiles:                              # ZD-8 : dalles de la tâche, hors zone, après la bascule
        done = docker.run(docker.compose + ['run', '--rm', '-T', '--entrypoint', 'rm', 'dem-tiles', '-f',
                                            f"/dem/copernicus30/{tile_name(*tile)}"], timeout=120)
        if done.returncode != 0:
            kept.append(list(tile))                        # non supprimée : reste au registre
    if owned_all:                                          # le registre ne perd une dalle qu'APRÈS sa suppression
        gone = {tuple(t) for t in purged_tiles} - {tuple(t) for t in kept}
        docker.write_state('/state/task-tiles.json', {'tiles': [t for t in owned_all if (t[0], t[1]) not in gone]})
    payload = {'added': tiles_added, 'purged': purged_tiles, 'purged_extracts': len(purged_ids)}
    code, _ = docker.app('done', request_id, '--graph', new, '--data-date', step['data_date'], '--new-tiles', '-',
                         input=json.dumps(payload, separators=(',', ':')).encode())
    if code in (0, 3, 4):
        for rid in purged_ids:
            docker.vol(SCRIPT_REMOVE_EXTRACT, {'ID': rid})
        docker.vol(SCRIPT_CLEANUP, {'KEEP': str(config.keep_graphs)})
        docker.remove_state('/state/pending-step.json')
        if code == 0:
            docker.write_state('/state/last-success.json', {'at': iso(utcnow()), 'request_id': request_id})
        log("tâche close" if code == 0 else "done : demande dépassée ou désactivé, rien à défaire (report réalignera)")
        return 'done'
    log("done : réponse transitoire, graphe en service gardé, étape rejouée au prochain passage")
    return 'retry'


# -- Un passage ----------------------------------------------------------------------------------------------------------------

def abandon_stale_step(docker: Docker, snap: dict, now: dt.datetime) -> bool:
    """Étape en suspens dont la prise date de plus de 2 h : abandonnée (ZD-10). Vrai si elle l'a été."""
    step = snap['pending']
    claimed = parse_iso(step.get('claimed_at'))
    if not claimed or (now - claimed).total_seconds() < CLAIM_EXPIRY_S:
        return False
    new = step.get('graph')
    if step.get('stage') == 'switching':
        if isinstance(new, str) and GRAPH_RE.fullmatch(new) and snap['current'] != new:
            docker.vol(SCRIPT_CLEAN_BUILD, {'STAMP': 'none', 'NEW': new, 'MERGED': ''})      # jamais basculé : graphe neuf supprimé
        rid = step.get('request_id')
        # déjà basculé (passage tué pendant la santé) : `report` réaligne la zone, pas de `failed` ; sinon libère l'application
        if snap['current'] != new and isinstance(rid, int) and not isinstance(rid, bool):
            code, _ = docker.app('failed', rid, '--code', 'timeout')
            if code not in (0, 3):
                log(f"failed : code {code}, la prise expirera au bout de 2 h")
    docker.remove_state('/state/pending-step.json')
    log("étape en suspens abandonnée (prise de plus de 2 h)")
    return True


def monthly_due(snap: dict, config: Config, now: dt.datetime) -> bool:
    last = parse_iso(snap['last'].get('at'))
    return bool(last) and (now - last).days >= config.monthly_days


def do_report(docker: Docker, snap: dict, now: dt.datetime) -> str:
    """`report` du graphe en service. 'ok', 'skip' (aucun graphe) ou 'stop' (fin du passage)."""
    if not snap['current']:
        return 'skip'
    builds, size = budget_today(snap, now)
    args = ['report', '--graph', snap['current'], '--budget', f"{builds},{size}"]
    if snap['zone'] is not None:
        code, _ = docker.app(*args, '--zone', '-', input=snap['zone'])
    else:
        code, _ = docker.app(*args, '--historic')
    if code == 0 or code == 2:                             # 2 : zone rapportée invalide, ignorée côté application (alerte)
        return 'ok'
    return 'stop'


def run_pass(config: Config) -> int:
    now = utcnow()
    stamp = now.strftime('%Y%m%d%H%M%S')
    docker = Docker(config, stamp)
    if docker.run(['docker', 'image', 'inspect', GRAPHHOPPER_IMAGE], timeout=60).returncode != 0:
        log("image du moteur absente : construction")
        if docker.run(docker.compose + ['build', 'graphhopper'], timeout=1800).returncode != 0:
            log("ECHEC : construction de l'image du moteur")
            return 1
    if docker.tool('routing-state-init', timeout=120).returncode != 0:      # volume d'état (ZD-9.0) : propriétaire 10002, idempotent
        log("ECHEC : initialisation du volume d'état")
        return 1
    snap = snapshot(docker)

    # 0. étape en suspens : rejouée AVANT `report` et avant toute prise (ZD-9 étape 0)
    if snap['pending']:
        if abandon_stale_step(docker, snap, now):
            snap = snapshot(docker)
        else:
            outcome = finish(docker, config, snap['pending'], snap)
            if outcome == 'retry':
                return 0
            snap = snapshot(docker)

    # 1. rapport du graphe en service
    if do_report(docker, snap, now) == 'stop':
        return 0

    # 2. catalogue
    meta = snap['catalog_meta']
    loaded, attempted = parse_iso(meta.get('loaded_at')), parse_iso(meta.get('attempt_at'))
    stale = loaded is None or (now - loaded).total_seconds() > CATALOG_MAX_AGE_S
    if stale and (attempted is None or (now - attempted).total_seconds() > CATALOG_RETRY_S):
        try:
            refresh_catalog(docker, config, now)
        except PassEnd as exc:
            log(f"fin du passage ({exc})")
            return 0
        except (OSError, ValueError, urllib.error.URLError, RuntimeError) as exc:
            log(f"catalogue non rafraîchi : {type(exc).__name__}: {exc}")
            docker.write_state('/state/catalog-meta.json', {'loaded_at': meta.get('loaded_at'), 'attempt_at': iso(now)})

    # 3. prise
    builds, received = budget_today(snap, now)
    monthly = monthly_due(snap, config, now)
    if monthly and (builds >= config.max_builds_per_day or received >= config.max_bytes_per_day):
        monthly = False                                    # mensuelle refusée par le budget : retentée le lendemain, sans `failed`
    code, out = docker.app('claim', *(['--monthly'] if monthly else []))
    if code != 0:
        return 0
    if out.strip() in ('', '{}'):
        return 0
    try:
        raw = json.loads(out)
    except ValueError:
        raw = None
    request_id = raw.get('request_id') if isinstance(raw, dict) else None
    if not (isinstance(request_id, int) and not isinstance(request_id, bool) and request_id >= 1):
        log("ECHEC : sortie de claim illisible, aucune demande identifiable (la prise expirera au bout de 2 h)")
        return 0
    return build(docker, config, snap, out, request_id, monthly, stamp, now)


def build(docker: Docker, config: Config, snap: dict, raw_claim: str, request_id: int, monthly: bool, stamp: str,
          now: dt.datetime) -> int:
    task = Task(docker, config, stamp)
    held = True                                            # demande prise, `switching` pas encore à 0
    try:
        try:
            local = json.loads(docker.vol_ok(SCRIPT_READ_CATALOG)).get('regions')
            if not isinstance(local, dict):
                raise TypeError("regions")
        except (ValueError, TypeError, RuntimeError):
            raise BuildFailed('catalog', "copie locale du catalogue absente ou illisible") from None
        task.local_catalog = local
        claim = validate_claim(raw_claim, local, config)
        log(f"demande {claim['request_id']} prise ({claim['reason']}) : {len(claim['regions'])} régions")
        builds, received = budget_today(snap, now)
        if builds >= config.max_builds_per_day:
            raise BuildFailed('budget', "budget du jour atteint (tâches)")
        plan = task.resolve(claim, snap, monthly or claim['reason'] == 'mensuel')
        planned = sum(i['size'] for i in plan['regions'].values() if i['fetch'])
        if received + planned > config.max_bytes_per_day:
            raise BuildFailed('budget', "budget du jour atteint (octets)")
        day_iso = now.strftime('%Y-%m-%d')
        # octets RÉSERVÉS avant le téléchargement (prévus), remplacés par les octets reçus, essais compris, dès qu'ils les dépassent
        docker.write_state('/state/budget.json', {'day': day_iso, 'builds': builds + 1, 'bytes': received + planned})
        if docker.run(docker.compose + ['build', 'graphhopper'], timeout=1800).returncode != 0:
            raise BuildFailed('other', "construction de l'image du moteur")
        if docker.tool('routing-init', name='init', timeout=120).returncode != 0:
            raise BuildFailed('other', "initialisation du volume")
        docker.vol_ok(SCRIPT_MKDIRS, {'STAMP': stamp})
        got = task.fetch(plan, lambda total: docker.write_state(
            '/state/budget.json', {'day': day_iso, 'builds': builds + 1, 'bytes': received + max(total, planned)}))
        docker.write_state('/state/budget.json', {'day': day_iso, 'builds': builds + 1, 'bytes': received + got})
        task.crop_merge(claim, plan['day'])
        task.import_graph(claim, plan['day'])
        tiles = task.fetch_tiles(claim, snap)
        # ZD-8 / ZD-11 : ce qui sera supprimé APRÈS la bascule, annoncé à `done`
        extracts_after = dict(snap['extracts'])
        for rid in claim['regions']:
            extracts_after[rid] = {'id': rid, 'date': plan['day'], 'size': plan['regions'][rid]['size'], 'fetched_at': iso(utcnow())}
        purged_tiles, purged_extracts = purge_plan(config, claim['regions'], extracts_after, tiles['owned'], claim['bbox'], now)
        owned_all = [[t[0], t[1], d] for t, d in sorted(tiles['owned'].items())]     # purgées comprises : retirées à `done`, après suppression
        reported = sorted({tuple(t) for t in tiles['added']})                         # dalles réellement nouvelles seulement
        docker.write_state('/state/task-tiles.json', {'tiles': owned_all})
        previous = snap['current']
        step = {'request_id': claim['request_id'], 'stage': 'switching', 'graph': task.new_graph, 'previous': previous,
                'stamp': stamp, 'merged': task.merged_name, 'data_date': f"20{plan['day'][:2]}-{plan['day'][2:4]}-{plan['day'][4:]}",
                'tiles_added': [list(t) for t in reported], 'purged_tiles': [list(t) for t in purged_tiles],
                'purged_extract_ids': purged_extracts, 'claimed_at': iso(now), 'owned': owned_all}
        docker.write_state('/state/pending-step.json', step)
        held = False                                       # à partir d'ici, finish() répond de la demande
        outcome = finish(docker, config, step, snap)
        log(f"passage terminé : {outcome}")
        return 0
    except BuildFailed as exc:
        log(f"ECHEC ({exc.code}) : {exc}")
        code = exc.code
    except subprocess.TimeoutExpired:
        log("ECHEC (timeout) : étape trop longue")
        code = 'timeout'
    except Exception as exc:  # noqa: BLE001 - tout échec inattendu : `failed --code other`, graphe en service intact
        log(f"ECHEC (other) : {type(exc).__name__}: {exc}")
        code = 'other'
    if held:
        task.discard()
        reply, _ = docker.app('failed', request_id, '--code', code)
        if reply not in (0, 3):
            log(f"failed : code {reply}, la prise expirera au bout de 2 h")
    return 0


def main() -> int:
    if not os.path.isfile('docker-compose.yml'):
        print("docker-compose.yml introuvable : lancer depuis la racine du projet", file=sys.stderr)
        return 1
    try:
        config = Config()
    except (ConfigError, ValueError) as exc:
        print(f"routing-zone: configuration invalide : {exc}", file=sys.stderr)
        return EXIT_BAD_CONFIG
    try:
        return run_pass(config)
    except Exception as exc:  # noqa: BLE001 - Docker absent, volume illisible… : journal propre, code 1 pour systemd
        log(f"ECHEC : {type(exc).__name__}: {exc}")
        return 1


if __name__ == '__main__':
    sys.exit(main())
