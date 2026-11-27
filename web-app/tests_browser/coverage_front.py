"""Scénarios du banc pour la mini-carte du panneau « Couverture en service » (admin-space EF-6.20 « mini-carte », lot Z3, CA-86).

Importé par `scenarios.py` (lancé comme `__main__`) quand l'instance « coverage » existe (ROUTING_ZONE_BUILD=1,
tuiles a, b, c.tiles.exemple.fr,
état amorcé par seed.py : zone active = pentagone ≠ zone demandée = rectangle par défaut, trois fonds de carte).
  (z1) CA-86 (a) fond par défaut autorisé : tuiles de ce fond seulement, attribution ; (c) tracés ; (d) légende ; (g) carte inerte
  (z2) CA-86 (b) sans fond autorisé (sous-domaine hors liste, hôte hors liste, liste vide, aucun fond par défaut) :
       aucune requête de tuile, fond
       neutre `rgb(24, 39, 32)` dans les deux thèmes, quadrillage à chaque degré entier ; (c) tracés
  (z3) CA-86 (e) `idle` : jaune pointillé encore visible sous le bleu
  (z4) CA-86 (f) réglages rechargés après le panneau : carte non recréée, tuiles ajoutées

Les mesures portent sur les styles CALCULÉS (couleur, épaisseur, pointillé), jamais sur des noms de classes :
une couleur posée par un attribut,
une classe ou un style en ligne donnerait le même résultat, sauf l'attribut `style`, interdit sur les tracés (EF-6.20, point 5).
"""

import math
import re
import sys
import urllib.parse

from playwright.sync_api import expect

from zone_front import guarded, merc, unmerc

host = sys.modules['__main__']

PANEL = '#coveragePanel'
MAP = f'{PANEL} .coverage-map'
ACTIVE = [[2, 49], [5, 49], [6, 50.5], [3.5, 52], [1, 50.5]]  # zone en service (seed.py)
REQUESTED = [[1.35, 48.83], [6.42, 48.83], [6.42, 51.55], [1.35, 51.55]]  # zone demandée : le défaut
TILE_HOSTS = {'a.tiles.exemple.fr', 'b.tiles.exemple.fr', 'c.tiles.exemple.fr'}
LEGEND = 'bleu plein : zone demandée · jaune pointillé : zone active (en service)'
NEUTRAL = 'rgb(24, 39, 32)'  # #182720
GRID = 'rgb(63, 84, 74)'  # #3f544a
HALO = 'rgb(12, 20, 17)'  # #0c1411
YELLOW = 'rgb(255, 212, 59)'  # #ffd43b
BLUE = 'rgb(13, 110, 253)'  # #0d6efd

SNAPSHOT = """() => {
    const panel = document.querySelector('#coveragePanel');
    const map = panel && panel.querySelector('.coverage-map');
    if (!map) return null;
    const paths = [...map.querySelectorAll('.leaflet-overlay-pane path')].map((p) => {
        const s = getComputedStyle(p);
        return { stroke: s.stroke, width: s.strokeWidth, dash: s.strokeDasharray, fill: s.fill, fillOpacity: s.fillOpacity,
                 hasStyle: p.hasAttribute('style'), d: p.getAttribute('d') || '', pointer: s.pointerEvents };
    });
    const attribution = map.querySelector('.leaflet-control-attribution');
    const box = map.getBoundingClientRect();
    const legend = [...panel.querySelectorAll('p, div, span')].find((e) => e.children.length === 0
        && /zone demandée/.test(e.textContent) && /zone active/.test(e.textContent));
    const legendBox = legend ? legend.getBoundingClientRect() : null;
    const yellow = paths.find((p) => p.stroke === 'rgb(255, 212, 59)');
    return {
        background: getComputedStyle(map).backgroundColor,
        paths,
        tiles: [...map.querySelectorAll('.leaflet-tile-pane img')].map((i) => i.currentSrc || i.src),
        attribution: attribution ? { text: attribution.textContent.trim(), children: attribution.children.length,
                                     visible: attribution.getClientRects().length > 0
                                         && getComputedStyle(attribution).visibility !== 'hidden' } : null,
        legend: legend ? { text: legend.textContent.replace(/\\s+/g, ' ').trim(), top: legendBox.top,
                           visible: legend.getClientRects().length > 0 } : null,
        box: { top: box.top, bottom: box.bottom, left: box.left, width: box.width, height: box.height },
        pane: map.querySelector('.leaflet-map-pane') ? map.querySelector('.leaflet-map-pane').style.transform : null,
        yellowD: yellow ? yellow.d : null,
    };
}"""


def snapshot(page):
    return page.evaluate(SNAPSHOT)


# -- ouverture et journal des requêtes -----------------------------------------------------------------------------------------
class Log:
    """Requêtes du navigateur depuis l'ouverture : API de l'application (chemin + méthode) et autres hôtes."""

    def __init__(self, page):
        self.page, self.items = page, []
        self.handler = lambda r: self.items.append((r.method, urllib.parse.urlsplit(r.url)))
        page.on('request', self.handler)

    def stop(self):
        self.page.remove_listener('request', self.handler)

    def api(self, since=0):
        return [
            (m, u.path)
            for m, u in self.items[since:]
            if u.netloc == host.COVERAGE.split('//')[1] and u.path.startswith('/api/')
        ]

    def foreign(self, since=0):
        return [
            u for _, u in self.items[since:] if u.netloc != host.COVERAGE.split('//')[1] and u.scheme in ('http', 'https')
        ]


def set_default(page, name):
    """Fond par défaut de l'instance (écrit côté serveur : le front le lit dans les réglages chargés)."""
    answer = host.api(page, 'PATCH', '/api/admin/settings', {'settings': {'map.default_basemap': name}})
    assert answer['status'] == 200, answer


def open_panel(page, theme, default='banc', size=(1280, 800)):
    """Ouvre l'onglet « Traces planifiées » de l'instance coverage (thème, fond par défaut). Rend le journal des requêtes."""
    page.emulate_media(color_scheme=theme)
    page.set_viewport_size({'width': size[0], 'height': size[1]})
    page.goto('about:blank')
    page.goto(host.COVERAGE + '/admin')
    page.wait_for_selector('#adminTabs')
    set_default(page, default)
    page.goto('about:blank')
    log = Log(page)
    page.goto(f'{host.COVERAGE}/admin#reglages/traces-planifiees')
    page.wait_for_selector(f'{MAP}.leaflet-container', timeout=15000)
    page.wait_for_selector(f'{PANEL}:not([hidden])')
    page.locator(PANEL).scroll_into_view_if_needed()
    page.wait_for_timeout(1200)  # tuiles (image locale de 1 pixel) et rendu posés
    return log


def shot(page, name):
    page.locator(PANEL).screenshot(path=f'{host.OUT}/{name}.png')


# -- géométrie : pixels de la carte ↔ degrés -----------------------------------------------------------------------------------
def points(d):
    numbers = [float(n) for n in re.findall(r'-?\d+(?:\.\d+)?', d or '')]
    return list(zip(numbers[0::2], numbers[1::2]))


def calibrate(path_d, vertices):
    """Transformation lon → x, Mercator(lat) → y déduite d'un tracé aux sommets connus ; None si le tracé ne les porte pas."""
    pts = points(path_d)
    if len(pts) != len(vertices):
        return None, f'{len(pts)} points dans le tracé, {len(vertices)} attendus'
    lons, mercs = [v[0] for v in vertices], [merc(v[1]) for v in vertices]
    i0, i1 = lons.index(min(lons)), lons.index(max(lons))
    j0, j1 = mercs.index(min(mercs)), mercs.index(max(mercs))
    ax = (pts[i1][0] - pts[i0][0]) / (lons[i1] - lons[i0])
    ay = (pts[j1][1] - pts[j0][1]) / (mercs[j1] - mercs[j0])
    bx, by = pts[i0][0] - ax * lons[i0], pts[j0][1] - ay * mercs[j0]
    worst = max(max(abs(ax * lon + bx - x), abs(ay * m + by - y)) for (x, y), lon, m in zip(pts, lons, mercs))
    return ((ax, bx, ay, by), '') if worst <= 2.0 else (None, f'sommets à {worst:.1f} px de leur position attendue')


def classify(path):
    stroke, width = path['stroke'], path['width']
    dash = re.sub(r'px|,', '', path['dash']).strip()
    if stroke == GRID:
        return 'grid'
    if stroke == HALO and width == '9px':
        return 'halo-active'
    if stroke == HALO and width == '6px':
        return 'halo-requested'
    if stroke == YELLOW and width == '5px' and dash == '8 6':
        return 'core-active'
    if stroke == BLUE and width == '2px' and dash in ('none', '', '0'):
        return 'core-requested'
    return f'autre({stroke} {width} {dash})'


def check_traces(c, label, snap, active=ACTIVE, requested=REQUESTED, both=True):
    """(c) : cœurs, halos, ordre de dessin, aucun style en ligne, aucun remplissage, sommets à leur place."""
    kinds = [(classify(p), p) for p in snap['paths']]
    zones = [(k, p) for k, p in kinds if k != 'grid']
    names = [k for k, _ in zones]
    c(
        f'{label} : tracés (halos, cœur jaune, cœur bleu)',
        sorted(names[:2]) == ['halo-active', 'halo-requested'] and names[2:] == ['core-active', 'core-requested'],
        names,
    )
    first_zone = next((i for i, (k, _) in enumerate(kinds) if k != 'grid'), len(kinds))
    c(
        f'{label} : le quadrillage est dessous les zones',
        all(k == 'grid' for k, _ in kinds[:first_zone]) and all(k != 'grid' for k, _ in kinds[first_zone:]),
        [k for k, _ in kinds],
    )
    c(
        f'{label} : aucun attribut style sur les tracés',
        not any(p['hasStyle'] for p in snap['paths']),
        [classify(p) for p in snap['paths'] if p['hasStyle']],
    )
    c(
        f'{label} : aucun remplissage',
        all(p['fill'] == 'none' or p['fillOpacity'] == '0' for p in snap['paths']),
        [p['fill'] for p in snap['paths']],
    )
    c(
        f'{label} : tracés non interactifs',
        all(p['pointer'] == 'none' for p in snap['paths']),
        [p['pointer'] for p in snap['paths']],
    )
    by_kind = {k: p for k, p in zones}
    for kind, vertices in (
        ('core-active', active),
        ('halo-active', active),
        ('core-requested', requested),
        ('halo-requested', requested),
    ):
        if kind in by_kind:
            fit, why = calibrate(by_kind[kind]['d'], vertices)
            c(f'{label} : {kind} suit les sommets de sa zone', fit is not None, why)
    return by_kind


def check_grid(c, label, snap):
    """(b) : une ligne à chaque degré entier dans l'étendue des zones, et aucune ailleurs."""
    by_kind = {classify(p): p for p in snap['paths'] if classify(p) != 'grid'}
    fit, why = calibrate(by_kind['core-active']['d'], ACTIVE) if 'core-active' in by_kind else (None, 'pas de cœur jaune')
    if fit is None:
        c(f'{label} : repère de la carte', False, why)
        return
    ax, bx, ay, by = fit
    vertical, horizontal = set(), set()  # longitudes (degrés) et latitudes (degrés) des lignes tracées
    for p in snap['paths']:
        if classify(p) != 'grid':
            continue
        pts = points(p['d'])
        for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
            if abs(x1 - x2) < 0.6 and abs(y1 - y2) > 1:
                vertical.add((x1 - bx) / ax)
            elif abs(y1 - y2) < 0.6 and abs(x1 - x2) > 1:
                horizontal.add(unmerc((y1 - by) / ay))
    c(f'{label} : lignes du quadrillage tracées', bool(vertical) and bool(horizontal), (vertical, horizontal))

    def off_x(lon):  # écart en pixels à la longitude entière la plus proche
        return abs(lon - round(lon)) * abs(ax)

    def off_y(lat):  # écart en pixels à la latitude entière la plus proche
        return abs(merc(lat) - merc(round(lat))) * abs(ay)

    lons = [v[0] for v in ACTIVE + REQUESTED]
    lats = [v[1] for v in ACTIVE + REQUESTED]
    want_lon = [k for k in range(math.floor(min(lons)), math.ceil(max(lons)) + 1) if min(lons) < k < max(lons)]
    want_lat = [k for k in range(math.floor(min(lats)), math.ceil(max(lats)) + 1) if min(lats) < k < max(lats)]
    seen_lon = {round(v) for v in vertical if off_x(v) <= 2.5}
    seen_lat = {round(v) for v in horizontal if off_y(v) <= 2.5}
    c(f'{label} : une ligne à chaque longitude entière {want_lon}', set(want_lon) <= seen_lon, sorted(seen_lon))
    c(f'{label} : une ligne à chaque latitude entière {want_lat}', set(want_lat) <= seen_lat, sorted(seen_lat))
    c(
        f'{label} : aucune ligne hors des degrés entiers (à 2,5 px près)',
        all(off_x(v) <= 2.5 for v in vertical) and all(off_y(v) <= 2.5 for v in horizontal),
        (sorted(v for v in vertical if off_x(v) > 2.5), sorted(v for v in horizontal if off_y(v) > 2.5)),
    )


def check_legend(c, label, snap):
    legend = snap['legend']
    c(f'{label} : légende présente', legend is not None and legend['visible'], legend)
    if legend:
        c(f'{label} : texte de la légende', host.norm(legend['text']) == LEGEND, legend['text'])
        c(
            f'{label} : légende sous la carte',
            legend['top'] >= snap['box']['bottom'] - 1,
            (legend['top'], snap['box']['bottom']),
        )


def check_inert(c, label, page):
    """(g) : glisser ou molette sur la mini-carte ⇒ ni déplacement ni zoom."""
    # carte au milieu de la fenêtre : hors de la barre de navigation fixe
    page.locator(MAP).evaluate("e => e.scrollIntoView({ behavior: 'instant', block: 'center' })")
    page.wait_for_timeout(300)
    before = snapshot(page)
    box = page.locator(MAP).bounding_box()
    cx, cy = box['x'] + box['width'] / 2, box['y'] + box['height'] / 2
    probe = '([x, y]) => !!(document.elementFromPoint(x, y) || document.body).closest("#coveragePanel .coverage-map")'
    reached = page.evaluate(probe, [cx, cy])
    c(f'{label} : le centre de la carte reçoit les gestes (rien ne le recouvre)', reached, (cx, cy))
    page.mouse.move(cx, cy)
    page.mouse.down()
    page.mouse.move(cx + 80, cy + 50, steps=8)
    page.mouse.up()
    page.mouse.dblclick(cx, cy)
    page.wait_for_timeout(500)
    mid = snapshot(page)
    still = mid['pane'] == before['pane'] and mid['yellowD'] == before['yellowD']
    c(f'{label} : le glisser et le double clic ne déplacent ni ne zooment', still, (before['pane'], mid['pane']))
    # la molette sur une carte inerte fait défiler la page : la carte est remise sous le pointeur avant chaque mesure
    page.locator(MAP).evaluate("e => e.scrollIntoView({ behavior: 'instant', block: 'center' })")
    page.wait_for_timeout(300)
    box = page.locator(MAP).bounding_box()
    page.mouse.move(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)
    page.mouse.wheel(0, -300)  # un seul sens : une molette qui zoome ne doit pas être annulée par la suivante
    page.wait_for_timeout(500)
    page.locator(MAP).evaluate('e => e.focus()')  # carte focalisable ou non : les flèches ne la déplacent pas
    for key in ('ArrowRight', 'ArrowDown', 'Equal'):
        page.keyboard.press(key)
    page.wait_for_timeout(400)
    after = snapshot(page)
    c(
        f'{label} : ni déplacement ni zoom (volet de la carte)',
        before['pane'] == after['pane'],
        (before['pane'], after['pane']),
    )
    c(
        f'{label} : ni déplacement ni zoom (tracé de la zone)',
        before['yellowD'] == after['yellowD'],
        (before['yellowD'], after['yellowD']),
    )
    c(f'{label} : mêmes tuiles', before['tiles'] == after['tiles'], (before['tiles'][:2], after['tiles'][:2]))


def tile_hosts(snap):
    return {urllib.parse.urlsplit(u).hostname for u in snap['tiles']}


# ===================================== (z1) fond autorisé ==================================
@host.scenario(
    '(z1) Mini-carte « Couverture en service » : fond par défaut autorisé, tracés, légende, carte inerte (CA-86 a, c, d, g)'
)
@guarded
def coverage_with_tiles(page, c):
    for theme in ('light', 'dark'):
        label = f'[{theme}]'
        start = len(host.intercepted)
        log = open_panel(page, theme, 'banc')
        snap = snapshot(page)
        c(
            f'{label} la mini-carte est dessinée',
            snap is not None and snap['paths'] != [],
            None if snap is None else len(snap['paths']),
        )
        if snap is None:
            log.stop()
            continue
        # (a) tuiles du fond par défaut seulement
        c(f'{label} des tuiles du fond par défaut sont affichées', len(snap['tiles']) > 0, snap['tiles'])
        c(
            f"{label} tuiles d'hôtes de `tiles=` seulement",
            tile_hosts(snap) <= TILE_HOSTS and tile_hosts(snap) != set(),
            sorted(tile_hosts(snap)),
        )
        c(f'{label} tuiles du fond « banc » seulement', all('/banc/' in u for u in snap['tiles']), snap['tiles'][:3])
        foreign = [u for u in log.foreign() if u.hostname not in TILE_HOSTS]
        c(f'{label} aucun hôte hors `tiles=` joint', not foreign, [u.geturl()[:80] for u in foreign])
        c(
            f'{label} aucune requête de tuile hors du fond par défaut',
            all('/banc/' in r['url'] for r in host.intercepted[start:]),
            [r['url'] for r in host.intercepted[start:] if '/banc/' not in r['url']][:3],
        )
        settings_gets = [x for x in log.api() if x == ('GET', '/api/admin/settings')]
        c(
            f'{label} une seule lecture des réglages (la carte lit les réglages déjà chargés)',
            len(settings_gets) == 1,
            log.api(),
        )
        # attribution, en texte brut
        attribution = snap['attribution']
        c(
            f'{label} attribution visible',
            attribution is not None and attribution['visible'] and attribution['text'] == '© Fond du banc',
            attribution,
        )
        c(
            f'{label} attribution en texte brut (aucun élément)',
            attribution is not None and attribution['children'] == 0,
            attribution,
        )
        check_traces(c, label, snap)
        check_legend(c, label, snap)
        mark = len(log.items)
        check_inert(c, label, page)
        # la carte redessinée (changement d'onglet) ne refait aucune requête vers l'API ni vers un hôte hors liste
        before_api = len(log.api())
        page.locator(host.tab('Carte')).click()
        page.wait_for_timeout(300)
        page.locator(host.tab('Traces planifiées')).click()
        page.wait_for_selector(f'{MAP}.leaflet-container')
        page.wait_for_timeout(1000)
        c(
            f"{label} aucune requête vers l'API en revenant sur le panneau",
            len(log.api()) == before_api,
            log.api()[before_api:],
        )
        c(
            f'{label} aucun hôte hors `tiles=` après le geste et le retour',
            all(u.hostname in TILE_HOSTS for u in log.foreign(mark)),
            [u.geturl()[:80] for u in log.foreign(mark) if u.hostname not in TILE_HOSTS],
        )
        shot(page, f'cov-z1-{theme}')
        log.stop()
    page.emulate_media(color_scheme='light')


# ===================================== (z2) sans fond autorisé =============================
def hold_settings(page, edit):
    """Réponse de `GET /api/admin/settings` retouchée par `edit(settings_by_key)` (le serveur réel ne produit pas ces états)."""

    def handler(route):
        if route.request.method != 'GET':
            route.fallback()
            return
        response = route.fetch()
        data = response.json()
        edit({s['key']: s for s in data['settings']})
        route.fulfill(response=response, json=data)

    page.route(re.compile(r'/api/admin/settings$'), handler)


NO_TILES_CASES = [
    ('sous-domaine du fond hors liste (d.tiles.exemple.fr)', 'partiel', None),
    ('hôte du fond hors liste', 'etranger', None),
    ('liste tiles= vide', 'banc', lambda by: by['map.basemaps'].update(allowlist=[])),
    ('aucun fond par défaut', 'banc', lambda by: by['map.default_basemap'].update(value='')),
    ('fond par défaut absent de la liste', 'banc', lambda by: by['map.default_basemap'].update(value='introuvable')),
]


@host.scenario(
    '(z2) Mini-carte : sans fond autorisé, aucune requête de tuile, fond neutre, quadrillage à chaque degré (CA-86 b, c)'
)
@guarded
def coverage_without_tiles(page, c):
    for name, default, edit in NO_TILES_CASES:
        for theme in ('light', 'dark'):
            label = f'[{name} / {theme}]'
            page.unroute(re.compile(r'/api/admin/settings$'))
            if edit:
                hold_settings(page, edit)
            start = len(host.intercepted)
            log = open_panel(page, theme, default)
            snap = snapshot(page)
            c(
                f'{label} la mini-carte est dessinée',
                snap is not None and snap['paths'] != [],
                None if snap is None else len(snap['paths']),
            )
            if snap is None:
                log.stop()
                continue
            c(f'{label} aucune tuile affichée', snap['tiles'] == [], snap['tiles'][:3])
            c(
                f'{label} aucune requête de tuile ni vers un hôte hors application',
                not host.intercepted[start:] and not log.foreign(),
                [r['url'] for r in host.intercepted[start:]][:3],
            )
            c(f'{label} fond neutre {NEUTRAL}', snap['background'] == NEUTRAL, snap['background'])
            attribution = snap['attribution']
            c(f'{label} aucune attribution de fond', attribution is None or attribution['text'] == '', attribution)
            check_traces(c, label, snap)
            check_grid(c, label, snap)
            check_legend(c, label, snap)
            check_inert(c, label, page)
            if name == NO_TILES_CASES[0][0] or name == NO_TILES_CASES[2][0]:
                shot(page, f'cov-z2-{default}-{theme}')
            log.stop()
    page.unroute(re.compile(r'/api/admin/settings$'))
    page.emulate_media(color_scheme='light')


# ===================================== (z3) idle ===========================================
@host.scenario('(z3) Mini-carte : zones égales (idle), le jaune pointillé reste visible sous le bleu (CA-86 e)')
@guarded
def coverage_idle(page, c):
    for default in ('banc', 'etranger'):  # avec et sans fond autorisé
        for theme in ('light', 'dark'):
            label = f'[idle / {default} / {theme}]'
            page.unroute(re.compile(r'/api/admin/routing/coverage$'))

            def idle(route):
                response = route.fetch()
                data = response.json()
                data['coverage'].update(status='idle', reason=None, active_zone=data['coverage']['requested_zone'])
                route.fulfill(response=response, json=data)

            page.route(re.compile(r'/api/admin/routing/coverage$'), idle)
            log = open_panel(page, theme, default)
            snap = snapshot(page)
            c(
                f'{label} la mini-carte est dessinée',
                snap is not None and snap['paths'] != [],
                None if snap is None else len(snap['paths']),
            )
            if snap is None:
                log.stop()
                continue
            c(f'{label} statut « À jour »', 'À jour' in page.locator(f'{PANEL} .coverage-status').inner_text(), None)
            kinds = [classify(p) for p in snap['paths'] if classify(p) != 'grid']
            c(
                f'{label} halos, cœur jaune puis cœur bleu',
                sorted(kinds[:2]) == ['halo-active', 'halo-requested'] and kinds[2:] == ['core-active', 'core-requested'],
                kinds,
            )
            zones = {classify(p): p for p in snap['paths']}
            yellow, blue = zones.get('core-active'), zones.get('core-requested')
            c(
                f'{label} cœur jaune et cœur bleu sur le même contour',
                bool(yellow and blue and yellow['d'] == blue['d']),
                (yellow and yellow['d'], blue and blue['d']),
            )
            c(
                f'{label} le jaune est dessiné avant le bleu (il reste visible dessous, 1,5 px de chaque côté)',
                bool(yellow and blue) and snap['paths'].index(yellow) < snap['paths'].index(blue),
                None,
            )
            c(
                f'{label} épaisseurs : jaune 5 px sous bleu 2 px',
                bool(yellow and blue and yellow['width'] == '5px' and blue['width'] == '2px'),
                (yellow and yellow['width'], blue and blue['width']),
            )
            check_legend(c, label, snap)
            if theme == 'dark':
                shot(page, f'cov-z3-{default}')
            log.stop()
    page.unroute(re.compile(r'/api/admin/routing/coverage$'))
    page.emulate_media(color_scheme='light')


# ===================================== (z4) réglages rechargés après le panneau ===========
def reload_through_stale_save(page):
    """Le réglage a changé côté serveur (autre admin) : l'enregistrement est refusé (409) et « Recharger » relit les réglages."""
    expect(page.locator('#settingsSave')).to_be_enabled()
    page.click('#settingsSave')
    expect(page.locator('#settingsConfirm')).to_be_visible()
    page.click('#settingsConfirmYes')
    reload_button = page.locator('#settingsConfirm button', has_text='Recharger')
    expect(reload_button).to_be_visible()
    reload_button.click()
    page.wait_for_timeout(2000)


MARK = "() => { const e = document.querySelector('#coveragePanel .leaflet-map-pane'); e.__bench = 'meme-carte'; return !!e; }"
SAME = "() => { const e = document.querySelector('#coveragePanel .leaflet-map-pane'); return e ? e.__bench || null : 'aucune'; }"


@host.scenario('(z4) Mini-carte : réglages rechargés après le panneau, carte non recréée et tuiles ajoutées (CA-86 f)')
@guarded
def coverage_settings_reloaded(page, c):
    log = open_panel(page, 'light', 'etranger')  # fond non autorisé : fond neutre et quadrillage
    before = snapshot(page)
    c(
        'sans fond autorisé au départ : aucune tuile, fond neutre',
        before['tiles'] == [] and before['background'] == NEUTRAL,
        (before['tiles'][:2], before['background']),
    )
    c('la carte porte une marque (même élément Leaflet)', page.evaluate(MARK))
    # un autre admin (ou l'API) change le fond par défaut ; cet admin enregistre un réglage : les réglages sont rechargés
    set_default(page, 'banc')
    field = page.locator(host.field('routing.zone_margin_km'))
    current = int(field.input_value())
    field.fill(str(current + 1 if current < 50 else current - 1))
    reload_through_stale_save(page)
    after = snapshot(page)
    c('après le rechargement, la carte existe encore', after is not None, None)
    if after:
        c('carte non recréée (même élément Leaflet)', page.evaluate(SAME) == 'meme-carte', page.evaluate(SAME))
        c(
            'les tuiles du fond par défaut sont ajoutées',
            len(after['tiles']) > 0 and tile_hosts(after) <= TILE_HOSTS,
            sorted(tile_hosts(after)),
        )
        c(
            'attribution du fond affichée',
            bool(after['attribution']) and after['attribution']['text'] == '© Fond du banc',
            after['attribution'],
        )
        c(
            'zoom et centre conservés (même tracé de la zone)',
            after['yellowD'] == before['yellowD'],
            (before['yellowD'], after['yellowD']),
        )
        check_traces(c, '[après rechargement]', after)
    # retour au fond non autorisé : les tuiles disparaissent, toujours sans recréer la carte
    set_default(page, 'etranger')
    page.locator(host.field('routing.zone_margin_km')).fill(str(current + 2 if current < 49 else current - 2))
    reload_through_stale_save(page)
    back = snapshot(page)
    if back:
        c(
            'fond de nouveau non autorisé : plus de tuile, fond neutre',
            back['tiles'] == [] and back['background'] == NEUTRAL,
            (back['tiles'][:2], back['background']),
        )
        c('toujours la même carte', page.evaluate(SAME) == 'meme-carte', page.evaluate(SAME))
    log.stop()
    set_default(page, 'banc')


MARK_TILES = "() => document.querySelectorAll('#coveragePanel .leaflet-tile-pane img').forEach((i) => { i.__bench = 1; })"
SAME_TILES = (
    "() => [...document.querySelectorAll('#coveragePanel .leaflet-tile-pane img')].filter((i) => i.__bench).length"
)
COUNT_TILES = "() => document.querySelectorAll('#coveragePanel .leaflet-tile-pane img').length"


@host.scenario("(z5) Mini-carte : ni la carte ni ses tuiles recréées à la frappe en recherche ni à la saisie (CA-86 f)")
@guarded
def coverage_not_recreated_on_typing(page, c):
    for default in ('banc', 'etranger'):  # avec tuiles, puis fond neutre et quadrillage
        label = f'[{default}]'
        log = open_panel(page, 'light', default)
        before = snapshot(page)
        c(f'{label} la mini-carte est dessinée', before is not None, None)
        if before is None:
            log.stop()
            continue
        page.evaluate(MARK)
        page.evaluate(MARK_TILES)
        tiles = page.evaluate(COUNT_TILES)
        requests = len(host.intercepted)
        api = len(log.api())
        for step, typed in enumerate('marge', 1):  # une frappe à la fois : chaque frappe refait le rendu des réglages
            page.type('#settingsSearch', typed, delay=60)
            page.wait_for_timeout(250)
            now = snapshot(page)
            ok = now is not None and page.evaluate(SAME) == 'meme-carte'
            c(f'{label} frappe {step} : même carte (même élément Leaflet)', ok, page.evaluate(SAME))
            if now is not None:
                same = now['yellowD'] == before['yellowD'] and now['tiles'] == before['tiles']
                c(f'{label} frappe {step} : même tracé, mêmes tuiles', same, (now['tiles'][:2], before['tiles'][:2]))
        c(f'{label} les éléments de tuile sont les mêmes (aucune tuile recréée)',
          page.evaluate(SAME_TILES) == tiles, (page.evaluate(SAME_TILES), tiles))
        c(f'{label} aucune nouvelle requête de tuile', len(host.intercepted) == requests, host.intercepted[requests:][:3])
        c(f'{label} aucune requête vers l\'API', len(log.api()) == api, log.api()[api:])
        # effacer la recherche, puis une saisie dans un réglage : même carte encore
        page.fill('#settingsSearch', '')
        page.wait_for_timeout(300)
        field = page.locator(host.field('routing.zone_margin_km'))
        field.fill(str(int(field.input_value()) + 1))
        page.wait_for_timeout(400)
        c(f'{label} saisie d\'un réglage : même carte', page.evaluate(SAME) == 'meme-carte', page.evaluate(SAME))
        c(f'{label} saisie d\'un réglage : mêmes tuiles', page.evaluate(SAME_TILES) == tiles, page.evaluate(SAME_TILES))
        c(f'{label} saisie d\'un réglage : aucune nouvelle requête de tuile', len(host.intercepted) == requests, None)
        log.stop()


RUNS = [coverage_with_tiles, coverage_without_tiles, coverage_idle, coverage_settings_reloaded, coverage_not_recreated_on_typing]
