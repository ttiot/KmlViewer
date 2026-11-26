"""
Banc de l'hôte (rôle test) : `routing/zone_update.py` et `routing/update.sh` (routing-zone-donnees ZD-8 à ZD-11, TZ2-c).

Écrit depuis la spec, indépendamment du banc de dev : le script est chargé tel quel et lancé EN PROCESSUS ; seules ses frontières sont simulées.
  - Docker : `subprocess.run` est remplacé. Les scripts du conteneur `routing-volume` et `routing-fetch` sont EXÉCUTÉS pour de vrai
    par `bash -euo pipefail -c` (comme l'entrypoint du compose), `/data` pointant un dossier temporaire ; `curl` est un faux (aucun réseau).
    osmium, GraphHopper et `dem-tiles` sont des faux qui laissent des fichiers et un journal.
  - Réseau de l'hôte (`urllib`) : `_OPENER.open` est remplacé par un faux Geofabrik (redirections, tailles, MD5) pour tester le VRAI `http()`.
  - Application : `docker compose exec … flask routing-zone` appelle la VRAIE commande `flask routing-zone` de l'application
    (`test_cli_runner`), avec une horloge commune. Un code scripté (2, 3, 137…) peut remplacer une réponse.
Aucune de ces simulations ne vérifie le contrat de l'hôte à la place du script : les assertions portent sur les fichiers laissés dans le
volume, sur le journal des appels et sur l'état de l'application.
"""

import email.message
import hashlib
import json
import math
import os
import re
import subprocess
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path

from tests.routing_zone_support import FROZEN, now_utc

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'routing' / 'zone_update.py'
UPDATE_SH = ROOT / 'routing' / 'update.sh'
COMPOSE_FILE = ROOT / 'docker-compose.yml'
REAL_RUN = subprocess.run                      # le vrai, gardé avant tout remplacement
ENV_NAMES = ('ROUTING_CATALOG_URL', 'ROUTING_CATALOG_ROOT', 'ROUTING_CATALOG_EXCLUDE', 'ROUTING_ALLOWED_HOSTS', 'ROUTING_MAX_REGIONS',
             'ROUTING_MAX_SOURCE_BYTES', 'DEM_MAX_TILES', 'ROUTING_MAX_IMPORT_BYTES', 'ROUTING_MAX_BUILDS_PER_DAY',
             'ROUTING_MAX_DOWNLOAD_BYTES_PER_DAY', 'ROUTING_CACHE_MAX_BYTES', 'ROUTING_KEEP_GRAPHS', 'ROUTING_HEALTH_TIMEOUT',
             'ROUTING_MONTHLY_DAYS', 'ROUTING_STEP_TIMEOUT', 'DEM_URL', 'DEM_BBOX', 'ROUTING_EXTRACT_URLS', 'ROUTING_ZONE_BUILD',
             'ROUTING_LOCK', 'ROUTING_FROM_TIMER')
OLD_GRAPH = 'graph-20261125120000'
FAKE_CURL = '''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ['FAKE_CURL_LOG'], 'a') as log:
    log.write(json.dumps(args) + chr(10))
url = args[-1]
remote = json.load(open(os.environ['FAKE_REMOTE']))
size = remote['actual'].get(url)
if size is None:
    sys.exit(22)
sys.stdout.buffer.write(b'x' * size)
'''


class Clock:
    def __init__(self):
        self.now = now_utc().replace(microsecond=0)

    def __call__(self):
        return self.now

    def advance(self, seconds=0, days=0):
        self.now += timedelta(seconds=seconds, days=days)


def yymmdd(when: datetime) -> str:
    return when.strftime('%y%m%d')


class Response:
    def __init__(self, length, body=b''):
        self.headers = {'Content-Length': str(length)} if length is not None else {}
        self._body = body

    def read(self, n=-1):
        return self._body if n < 0 else self._body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Web:
    """Faux Geofabrik : `-latest` redirige vers le fichier daté ; tailles, dates et MD5 réglables par région."""

    def __init__(self, clock):
        self.clock = clock
        self.calls = []                    # (méthode, URL)
        self.dates = {}                    # région -> AAMMJJ (défaut : la veille)
        self.sizes = {}                    # région -> taille annoncée par HEAD
        self.actual = {}                   # région -> octets réellement servis (défaut : la taille annoncée)
        self.default_size = 2000
        self.bad_md5 = set()
        self.override = {}                 # URL -> ('302', Location) | ('200', longueur)
        self.index = FROZEN.read_bytes()
        self.index_override = None
        self.host_calls = []

    @staticmethod
    def parse(url):
        match = re.fullmatch(r'(.*/)(.+)-(latest|\d{6})\.osm\.pbf(\.md5)?', url)
        return match.groups() if match else None

    def day(self, rid):
        return self.dates.get(rid, yymmdd(self.clock.now - timedelta(days=1)))

    def size(self, rid):
        return self.sizes.get(rid, self.default_size)

    def stream(self, rid):
        return self.actual.get(rid, self.size(rid))

    def dated(self, url):
        prefix, rid, _, _ = self.parse(url)
        return f"{prefix}{rid}-{self.day(rid)}.osm.pbf"

    def remote_map(self, urls):
        out = {}
        for url in urls:
            prefix, rid, kind, _ = self.parse(url)
            out[url] = self.stream(rid)
        return out

    def open(self, request, timeout=None):
        url, method = request.full_url, request.get_method()
        self.calls.append((method, url))
        headers = email.message.Message()
        if url in self.override:
            kind, value = self.override[url]
            if kind == '302':
                headers['Location'] = value
                raise urllib.error.HTTPError(url, 302, 'Found', headers, None)
            return Response(int(value))
        if url.endswith('/index-v1.json'):
            body = self.index_override if self.index_override is not None else self.index
            return Response(len(body), body if method == 'GET' else b'')
        parsed = self.parse(url)
        if not parsed:
            raise urllib.error.HTTPError(url, 404, 'Not Found', headers, None)
        prefix, rid, kind, md5 = parsed
        if kind == 'latest':
            headers['Location'] = f"{prefix}{rid}-{self.day(rid)}.osm.pbf"
            raise urllib.error.HTTPError(url, 302, 'Found', headers, None)
        if kind != self.day(rid) and not md5:
            raise urllib.error.HTTPError(url, 404, 'Not Found', headers, None)
        if md5:
            digest = hashlib.md5(b'x' * self.stream(rid)).hexdigest()
            if rid in self.bad_md5:
                digest = '0' * 32
            body = f"{digest}  {rid}-{kind}.osm.pbf\n".encode()
            return Response(len(body), body)
        return Response(self.size(rid))


class FakeHost:
    """`docker` et `curl` simulés autour d'un dossier de volume ; journal ordonné des événements."""

    def __init__(self, tmp, clock, web, runner):
        self.tmp, self.clock, self.web, self.runner = tmp, clock, web, runner
        self.vol, self.state, self.dem, self.bin = tmp / 'vol', tmp / 'state', tmp / 'dem', tmp / 'bin'     # /data, /state, /dem
        for folder in (self.vol / 'graphs', self.state / 'extracts', self.dem, self.bin):
            folder.mkdir(parents=True)
        (self.bin / 'curl').write_text(FAKE_CURL)
        (self.bin / 'curl').chmod(0o755)
        self.curl_log = tmp / 'curl.log'
        self.remote = tmp / 'remote.json'
        self.log = []                       # événements : ('app', sous-commande, *arguments, code) ; ('fetch', id) ; ('extract', id) …
        self.app_script = {}                # sous-commande -> [code | (code, sortie) | None, …] consommés dans l'ordre
        self.fail = set()                   # 'extract:<id>', 'merge', 'import', 'dem', 'build', 'init'
        self.merged_size = None
        self.health = 'healthy'
        self.incomplete = False
        self.stdin = []                     # (sous-commande, entrée)
        self.shell_used = []
        self.image_missing = False
        self.unlisted = []                  # commandes non reconnues (doit rester vide)
        self.compose_text = COMPOSE_FILE.read_text() if COMPOSE_FILE.exists() else ''
        self.state_meta_file = tmp / 'state.meta'
        self.state_meta_file.write_text(json.dumps({'uid': 0, 'mode': 0o755}))            # volume neuf : propriétaire root

    # -- volume ---------------------------------------------------------------------------------------------------------------
    def path(self, text):
        return str(text).replace('/data', str(self.vol)).replace('/state', str(self.state))

    def bash(self, script, envs, stdin, extra=None):
        env = {'PATH': f"{self.bin}:{os.environ['PATH']}", 'HOME': str(self.tmp), 'FAKE_REMOTE': str(self.remote),
               'FAKE_CURL_LOG': str(self.curl_log)}
        env.update({k: self.path(v) for k, v in envs.items()})
        env.update(extra or {})
        done = REAL_RUN(['bash', '-euo', 'pipefail', '-c', self.path(script)], input=stdin, capture_output=True, env=env, timeout=120)
        done.stdout = done.stdout.replace(str(self.state).encode(), b'/state').replace(str(self.vol).encode(), b'/data')   # ce que verrait l'hôte
        return done

    def seed_graph(self, name=OLD_GRAPH, zone=None, current=True):
        folder = self.vol / 'graphs' / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'properties').write_text('x')
        if zone is not None:
            (folder / 'zone.json').write_text(json.dumps(zone))
        if current:
            link = self.vol / 'graphs' / 'current'
            link.unlink(missing_ok=True)
            link.symlink_to(name)

    def where(self, relative):
        return (self.vol if str(relative).startswith('graphs/') else self.state) / relative

    def read_json(self, relative):
        path = self.where(relative)
        return json.loads(path.read_text()) if path.exists() else None

    def write_json(self, relative, obj):
        path = self.where(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(obj))

    def current(self):
        link = self.vol / 'graphs' / 'current'
        return os.readlink(link) if link.is_symlink() else None

    def graphs(self):
        return sorted(p.name for p in (self.vol / 'graphs').glob('graph-*'))

    def extracts(self):
        return sorted(p.name[:-8] for p in (self.state / 'extracts').glob('*.osm.pbf'))

    def events(self, kind):
        return [e for e in self.log if e[0] == kind]

    def apps(self, sub=None):
        return [e for e in self.log if e[0] == 'app' and (sub is None or e[1] == sub)]

    def builds(self):
        """Tout ce qui ressemble à une construction : téléchargement, recadrage, fusion, import, dalles."""
        return [e for e in self.log if e[0] in ('fetch', 'extract', 'merge', 'import', 'dem')]

    # -- application ----------------------------------------------------------------------------------------------------------
    def app_call(self, args, stdin):
        sub = args[0]
        queue = self.app_script.get(sub)
        self.stdin.append((sub, stdin))
        if queue:
            scripted = queue.pop(0)
            if isinstance(scripted, tuple) and scripted[0] == 'real_then':              # l'application applique, la réponse se perd
                self.runner.invoke(args=['routing-zone', *args], input=stdin or b'')
                self.log.append(('app', *args, scripted[1]))
                return scripted[1], b''
            if scripted is not None:
                code, out = scripted if isinstance(scripted, tuple) else (scripted, b'')
                self.log.append(('app', *args, code))
                return code, out
        result = self.runner.invoke(args=['routing-zone', *args], input=stdin or b'')
        self.log.append(('app', *args, result.exit_code))
        return result.exit_code, result.stdout_bytes

    # -- dispatcher de `subprocess.run` ---------------------------------------------------------------------------------------
    def run(self, cmd, input=None, capture_output=True, timeout=None, env=None, check=False, **kwargs):
        if kwargs.get('shell') or not isinstance(cmd, list) or not all(isinstance(a, str) for a in cmd):
            self.shell_used.append(cmd)
        done = lambda rc=0, out=b'': subprocess.CompletedProcess(cmd, rc, out, b'')       # noqa: E731
        if cmd[:1] == ['sleep']:
            return done()
        if cmd[0] != 'docker':                                           # pas une commande simulée : le vrai `subprocess.run`
            return REAL_RUN(cmd, input=input, capture_output=capture_output, timeout=timeout, env=env, check=check, **kwargs)
        if cmd[:3] == ['docker', 'image', 'inspect']:
            return done(1 if self.image_missing else 0)
        if cmd[:2] == ['docker', 'inspect']:
            return done(0, (self.health + '\n').encode())
        if cmd[:3] == ['docker', 'rm', '-f']:
            self.log.append(('rm-container', cmd[3]))
            return done()
        if cmd[:2] != ['docker', 'compose']:
            self.unlisted.append(cmd)
            return done(127)
        rest, verb = cmd[2:], None
        while rest and rest[0] == '--profile':
            rest = rest[2:]
        verb, rest = rest[0], rest[1:]
        if verb == 'build':
            return done(1 if 'build' in self.fail else 0)
        if verb == 'ps':
            return done(0, b'cid123\n')
        if verb == 'up':
            self.log.append(('up', rest[-1]))
            return done()
        if verb == 'exec':
            assert rest[:5] == ['-T', 'kml-viewer', 'flask', 'routing-zone', rest[4]], rest
            code, out = self.app_call(rest[4:], input)
            return done(code, out)
        if verb == 'run':
            return self.compose_run(cmd, rest, input, env, done)
        self.unlisted.append(cmd)
        return done(127)

    def compose_run(self, cmd, rest, stdin, env, done):
        assert rest[:2] == ['--rm', '-T'], rest
        rest = rest[2:]
        if rest[0] == '--entrypoint':                                        # dalles : liste ou suppression
            entry, service, args = rest[1], rest[2], rest[3:]
            if entry == 'sh':
                return done(0, ''.join(f"{p.name}\n" for p in sorted(self.dem.glob('*.tif'))).encode())
            if entry == 'rm' and service == 'dem-tiles':
                target = self.dem / Path(args[-1]).name
                assert args[-1].startswith('/dem/copernicus30/'), args
                self.log.append(('rm-tile', target.name))
                target.unlink(missing_ok=True)
                return done()
            self.unlisted.append(cmd)
            return done(127)
        assert rest[0] == '--name', rest
        rest, envs = rest[2:], {}
        while rest[0] == '-e':
            key, _, value = rest[1].partition('=')
            envs[key] = value
            rest = rest[2:]
        service, args = rest[0], rest[1:]
        if service == 'routing-state-init':
            return done(1 if 'state-init' in self.fail else self.state_init())
        if service == 'routing-init':
            return done(1 if 'init' in self.fail else 0)
        if service in ('routing-volume', 'routing-fetch', 'routing-merge', 'routing-import') and not self.state_writable():
            return done(1)                                                          # /state : Permission denied (propriétaire root)
        if service == 'routing-volume':
            res = self.bash(args[0], envs, stdin)
            return done(res.returncode, res.stdout)
        if service == 'routing-fetch':
            url = envs['URL']
            rid = Web.parse(url)[1]
            self.remote.write_text(json.dumps({'actual': self.web.remote_map([url])}))
            self.log.append(('fetch', rid, int(envs['CAP'])))
            res = self.bash(args[0], envs, stdin)
            return done(res.returncode, res.stdout)
        if service == 'routing-merge':
            return self.osmium(args, done)
        if service == 'routing-import':
            return self.graph_import(env or {}, done)
        if service == 'dem-tiles':
            return self.dem_tiles(envs, done)
        self.unlisted.append(cmd)
        return done(127)

    @property
    def state_meta(self):
        return json.loads(self.state_meta_file.read_text())

    def state_writable(self):
        meta = self.state_meta
        return (meta['uid'] == 10002 and meta['mode'] & 0o200) or bool(meta['mode'] & 0o002)

    def state_init(self):
        """`routing-state-init` : entrypoint, utilisateur et capacités lus dans le VRAI compose, outils faux qui appliquent les capacités."""
        block = re.search(r'^  routing-state-init:\n(.*?)(?=^  \S|^\S|\Z)', self.compose_text, re.M | re.S).group(1)
        entry = json.loads(re.search(r'entrypoint: (\[.*\])', block).group(1))
        user = int((re.search(r'user: "?(\d+)', block) or [0, 10002])[1])
        drop_all = re.search(r'cap_drop:\n\s+- ALL', block) is not None
        added = re.search(r'cap_add:\n((?:      - .*\n)+)', block)
        caps = [c.strip()[2:] for c in added.group(1).splitlines()] if added else []
        if not drop_all:
            caps += ['CHOWN', 'FOWNER', 'DAC_OVERRIDE']
        tools = self.tmp / 'statebin'
        tools.mkdir(exist_ok=True)
        for name in ('chown', 'chmod', 'stat'):
            (tools / name).write_text(FAKE_STATE_TOOL)
            (tools / name).chmod(0o755)
        env = {'PATH': f"{tools}:{os.environ['PATH']}", 'FAKE_STATE_META': str(self.state_meta_file), 'FAKE_CAPS': ','.join(caps),
               'FAKE_UID': str(user)}
        done = REAL_RUN(entry[:2] + [entry[2]], capture_output=True, env=env, timeout=60)
        return done.returncode

    def osmium(self, args, done):
        out = Path(self.path(args[args.index('-o') + 1]))
        out.parent.mkdir(parents=True, exist_ok=True)
        if args[0] == 'extract':
            source = Path(self.path(args[3]))
            rid = source.name[:-8]
            self.log.append(('extract', rid, args[2], str(source.parent.name)))
            if f"extract:{rid}" in self.fail or not source.exists():
                return done(1)
            out.write_bytes(b'c' * max(1, source.stat().st_size // 2))
            return done()
        inputs = [Path(self.path(a)) for a in args[1:args.index('-o')]]
        self.log.append(('merge', len(inputs)))
        if 'merge' in self.fail or not all(p.exists() for p in inputs):
            return done(1)
        size = self.merged_size if self.merged_size is not None else sum(p.stat().st_size for p in inputs)
        with open(out, 'wb') as handle:
            handle.truncate(size)
        return done()

    def graph_import(self, env, done):
        pbf, graph = Path(self.path(env['ROUTING_IMPORT_PBF'])), Path(self.path(env['ROUTING_IMPORT_GRAPH']))
        self.log.append(('import', pbf.name, graph.name))
        if 'import-timeout' in self.fail:
            raise subprocess.TimeoutExpired(cmd='import', timeout=1)
        if 'import' in self.fail or not pbf.exists():
            return done(1)
        graph.mkdir(parents=True)
        (graph / 'properties').write_text('' if self.incomplete else 'x')
        return done()

    def dem_tiles(self, envs, done):
        lat_min, lon_min, lat_max, lon_max = [float(n) for n in envs['DEM_BBOX'].split(',')]
        self.log.append(('dem', envs['DEM_BBOX']))
        if 'dem' in self.fail:
            return done(1)
        partial = 'dem-partial' in self.fail
        for lat in range(math.floor(lat_min), math.ceil(lat_max)):
            for lon in range(math.floor(lon_min), math.ceil(lon_max)):
                name = f"{'N' if lat >= 0 else 'S'}{abs(lat):02d}{'E' if lon >= 0 else 'W'}{abs(lon):03d}.tif"
                if not (self.dem / name).exists():
                    (self.dem / name).write_bytes(b't')
                    if partial:
                        return done(1)                                       # une dalle téléchargée, puis la panne
        return done()


# -- Compose simulé pour `update.sh` en mode historique (`ROUTING_ZONE_BUILD=0`) ----------------------------------------------------
# `docker` est un script Python posé dans le PATH ; il lit les VRAIS montages du docker-compose.yml du dépôt : un chemin n'existe dans un
# service que si son volume y est monté (sinon : dossier vide en lecture seule, comme le système de fichiers du conteneur). Le volume
# d'état est root tant que `routing-state-init` n'a pas tourné. Les scripts `bash -euo pipefail -c` des services sont exécutés pour de
# vrai ; osmium, GraphHopper et curl sont des faux.
FAKE_COMPOSE_DOCKER = r'''#!/usr/bin/env python3
import hashlib, json, os, re, subprocess, sys
root = os.environ['FAKE_ROOT']
args = sys.argv[1:]
with open(os.path.join(root, 'docker.log'), 'a') as log:
    log.write(' '.join(args) + chr(10))
compose = open('docker-compose.yml').read()


def section(service):
    match = re.search(r'^  ' + re.escape(service) + r':\n(.*?)(?=^  \S|^\S|\Z)', compose, re.M | re.S)
    if not match:
        sys.exit('service inconnu : ' + service)
    return match.group(1)


def mounts(service):
    block = re.search(r'^    volumes:\n((?:      - .*\n)+)', section(service), re.M)
    out = {}
    for line in (block.group(1).splitlines() if block else []):
        name, target, *mode = line.strip()[2:].split(':')
        out[target] = (name, 'ro' in mode)
    return out


def real(service, path):
    """Chemin vu par le service -> chemin réel, ou dossier vide en lecture seule s'il n'est pas monté."""
    for target in ('/data', '/state'):
        if path == target or path.startswith(target + '/'):
            if target in mounts(service):
                return root + target + path[len(target):]
            return root + '/unmounted' + path
    return path


def rewrite(service, text):
    for target in ('/data', '/state'):
        text = text.replace(target, real(service, target))
    return text


if args[:2] == ['inspect', '--format']:
    print('healthy'); sys.exit(0)
if args[:1] != ['compose']:
    sys.exit(0)
rest = args[1:]
while rest and rest[0] == '--profile':
    rest = rest[2:]
verb, rest = rest[0], rest[1:]
if verb == 'ps':
    print('cid'); sys.exit(0)
if verb != 'run':
    sys.exit(0)                                                      # build, up : sans effet
assert rest[:2] == ['--rm', '-T'], rest
rest = rest[2:]
env = dict(os.environ)
while rest[0] in ('-e', '--name'):
    if rest[0] == '-e':
        key, _, value = rest[1].partition('=')
        env[key] = value
    rest = rest[2:]
service, cargs = rest[0], rest[1:]
if service == 'routing-state-init':
    open(root + '/state/.inited', 'w').close()
    os.chmod(root + '/state', 0o755)
    sys.exit(0)
entry = re.search(r'entrypoint: (\[.*\])', section(service))
entry = json.loads(entry.group(1)) if entry else None
if entry and entry[:4] == ['bash', '-euo', 'pipefail', '-c']:
    done = subprocess.run(['bash', '-euo', 'pipefail', '-c', rewrite(service, cargs[0])], env=env)
    sys.exit(done.returncode)
if entry and entry[0] == 'mkdir':
    paths = [real(service, p) for p in entry[2:]]
    done = subprocess.run(['mkdir', '-p', *paths])
    sys.exit(done.returncode)
if 'osmium' in section(service):
    out = real(service, cargs[cargs.index('-o') + 1])
    inputs = [real(service, p) for p in (cargs[1:cargs.index('-o')] if cargs[0] == 'merge' else [cargs[3]])]
    for path in inputs:
        if not os.path.exists(path):
            sys.exit('osmium : fichier introuvable : ' + path)
    if cargs[cargs.index('-o') + 1].startswith('/data') and ('/data' not in mounts(service)):
        sys.exit('osmium : /data absent')
    try:
        with open(out, 'wb') as handle:
            handle.write(b'm' * 100)
    except OSError as exc:
        sys.exit('osmium : ' + str(exc))
    sys.exit(0)
if service == 'routing-import':
    default = {k: v for k, v in re.findall(r'(GH_PBF|GH_GRAPH): \$\{(?:\w+):-([^}]*)\}', section(service))}
    pbf = env.get('ROUTING_IMPORT_PBF', default.get('GH_PBF'))
    graph = env.get('ROUTING_IMPORT_GRAPH', default.get('GH_GRAPH'))
    if not os.path.exists(real(service, pbf)):
        sys.exit('import : fichier absent du conteneur : ' + pbf)
    if not graph.startswith('/data/') or '/data' not in mounts(service):
        sys.exit('import : dossier de sortie hors volume : ' + graph)
    os.makedirs(real(service, graph))
    open(real(service, graph) + '/properties', 'w').write('x')
    sys.exit(0)
sys.exit('service non simulé : ' + service)
'''
FAKE_COMPOSE_CURL = r'''#!/usr/bin/env python3
import hashlib, sys
args = sys.argv[1:]
url = args[-1]
content = b'x' * 1000
if url.endswith('.md5'):
    data = (hashlib.md5(content).hexdigest() + '  ' + url.rsplit('/', 1)[1][:-4] + chr(10)).encode()
else:
    data = content
if '-o' in args:
    open(args[args.index('-o') + 1], 'wb').write(data)
else:
    sys.stdout.buffer.write(data)
'''


# -- Volume d'état et capacités (TZ2c-B1) -----------------------------------------------------------------------------------------
# `routing-state-init` est exécuté avec les options RÉELLES du compose (`user`, `cap_drop`, `cap_add`) : `chown`, `chmod` et `stat` sont
# des faux qui appliquent les règles du noyau (chown : CAP_CHOWN ; chmod : propriétaire, sinon CAP_FOWNER), sur un volume neuf
# (propriétaire root, 755). Les conteneurs en 10002 n'écrivent dans /state que s'ils en sont propriétaires (ou si le dossier est ouvert).
FAKE_STATE_TOOL = '''#!/usr/bin/env python3
import json, os, sys
name = os.path.basename(sys.argv[0])
args = sys.argv[1:]
meta_path, caps, uid = os.environ['FAKE_STATE_META'], os.environ['FAKE_CAPS'].split(','), int(os.environ['FAKE_UID'])
meta = json.load(open(meta_path))
if args[-1] != '/state':
    sys.exit(2)
if name == 'stat':
    print('%d:%o' % (meta['uid'], meta['mode'])); sys.exit(0)
if name == 'chown':
    if 'CHOWN' not in caps:
        sys.exit("chown: changing ownership of '/state': Operation not permitted")
    meta['uid'] = int(args[0].split(':')[0])
elif name == 'chmod':
    if uid != meta['uid'] and 'FOWNER' not in caps:
        sys.exit("chmod: changing permissions of '/state': Operation not permitted")
    meta['mode'] = int(args[0], 8)
else:
    sys.exit(2)
json.dump(meta, open(meta_path, 'w'))
'''
