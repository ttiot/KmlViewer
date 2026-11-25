"""Scénarios du banc pour RB-2.7 « Modifier les points sur le résultat affiché » (gpx-route-builder.md, CA-R41 à CA-R48).

Importé par `scenarios.py` (lancé comme `__main__`) : les outils communs (décorateur, interception réseau, URL,
        sortie) viennent de lui.

Principe : le résultat de départ est une génération RÉELLE (moteur de routage local), faite une seule fois puis gardée. Les
régénérations des gestes sont répondues par un double (`RouteMock`) bâti sur ce résultat : identifiant et longueur différents à
chaque réponse, même tracé. Cela rend les gestes déterministes (pas de 422 du moteur sur un point mal placé), permet de
RETARDER ou de FAIRE ÉCHOUER une réponse (429, 503, 504, coupure) et de compter exactement les requêtes. Les requêtes
réelles restent couvertes par (k) et par (v).
"""

import copy
import json
import math
import re
import sys

from playwright.sync_api import expect

host = sys.modules['__main__']

# Trois points à 600 à 800 m les uns des autres, dans la couverture du moteur (Lille)
POINTS = [(50.6350, 3.0550), (50.6395, 3.0640), (50.6310, 3.0690)]
CACHE = {}
FIRST = 'Point 1'


# -- double de POST /api/route-builder/route et de /save ----------------------------------------------------------------------
class RouteMock:
    def __init__(self, base=None, shift=(0.0, 0.0), loop_end_on=None):
        self.base, self.shift, self.loop_end_on = base, shift, loop_end_on
        self.posts, self.saves, self.ids = [], [], []
        self.mode = 'ok'                      # ok : double ; hold : réponse retenue ; real : moteur réel
        self.failure = None                   # (statut, message) ou 'network' : prochaine réponse en échec
        self.held = []
        self.inflight = self.peak = self.count = 0

    def clone(self):
        data = copy.deepcopy(self.base)
        self.count += 1
        meta = data['route_meta']
        meta['result_id'] = f'mock-{self.count}'
        meta['distance_m'] = self.base['route_meta']['distance_m'] + 100 * self.count
        if self.shift != (0.0, 0.0) or self.loop_end_on:
            moved = self.trace()
            for feature in data.get('features', []):
                if feature.get('type') == 'polyline':
                    feature['coordinates'] = [[m[0], m[1], *c[2:]] for m, c in zip(moved, feature['coordinates'])]
        self.ids.append(meta['result_id'])
        return data

    def trace(self):
        coords = [[c[0] + self.shift[0], c[1] + self.shift[1]] for f in self.base['features'] if f.get('type') == 'polyline'
                  for c in f['coordinates']]
        if self.loop_end_on:                  # boucle dont le départ est à 35 m de P1 et l'arrivée exactement sur P1
            coords[0] = [self.loop_end_on[0] + 0.0003, self.loop_end_on[1]]
            coords[-1] = list(self.loop_end_on)
        return coords

    def route(self, route, request):
        if request.method != 'POST':
            route.fallback()
            return
        self.posts.append(json.loads(request.post_data))
        self.inflight += 1
        self.peak = max(self.peak, self.inflight)
        if self.mode == 'real':
            answer = route.fetch()
            self.inflight -= 1
            self.base = answer.json()                                     # le dernier résultat réel sert de base
            route.fulfill(response=answer)
        elif self.mode == 'hold':
            self.held.append(route)
        else:
            self.answer(route)

    def answer(self, route):
        failure, self.failure = self.failure, None
        self.inflight -= 1
        if failure == 'network':
            route.abort('failed')
        elif failure:
            route.fulfill(status=failure[0], content_type='application/json', body=json.dumps({'success': False,
                    'error': failure[1]}))
        else:
            route.fulfill(status=200, content_type='application/json', body=json.dumps(self.clone()))

    def release(self, failure=None):
        self.failure = failure
        self.mode = 'ok'
        if not self.held:
            return False
        self.answer(self.held.pop(0))
        return True

    def save(self, route, request):
        if request.method != 'POST':
            route.fallback()
            return
        self.saves.append(json.loads(request.post_data))
        route.fulfill(status=200, content_type='application/json', body=json.dumps({'success': True, 'activity_id': 4242}))


def usable_geometry(view, mock):
    """Le résultat permet les appuis dont les scénarios ont besoin (une boucle repliée n'a pas de point isolé à 5 ou 40 px)."""
    trace, xy = screen_trace(view, mock), pins(view)
    needs = ((5, (1, 2)), (12, (1, 2)), (20, (1, 2)), (40, (1, 2)), (40, None), (5, None), (5, 'close'))
    return all(trace_at(trace, xy, wanted, between) is not None for wanted, between in needs)


def ensure_base(page):
    """Une génération réelle, une seule fois pour le banc RB-2.7 (autre proposition tant que la boucle est trop repliée)."""
    if 'base' in CACHE:
        return CACHE['base']
    import os
    saved = os.path.join(host.OUT, 'rb_base.json')                       # résultat réel gardé dans la sortie du banc (itérations)
    if os.path.exists(saved):
        with open(saved, encoding='utf-8') as handle:
            CACHE['base'] = json.load(handle)
        return CACHE['base']
    mock = RouteMock()
    mock.mode = 'real'
    view, _ = opened(page, mock, generate=True, patched=False)
    try:
        for _ in range(4):
            if usable_geometry(view, mock):
                break
            view.click('#rbAnother')                                      # autre proposition réelle (graine suivante)
            view.wait_for_function("() => document.getElementById('rbCancel').hidden", timeout=120000)
            view.wait_for_timeout(500)
    finally:
        view.context.close()
    CACHE['base'] = mock.base
    with open(saved, 'w', encoding='utf-8') as handle:
        json.dump(CACHE['base'], handle)
    return CACHE['base']


# -- contexte, points, résultat ---------------------------------------------------------------------------------------------
def to_page(view, latlng):
    return view.evaluate("""(ll) => { const r = Viewer.map.getContainer().getBoundingClientRect();
        const p = Viewer.map.latLngToContainerPoint(ll); return [r.left + p.x, r.top + p.y]; }""", list(latlng))


def to_latlng(view, x, y):
    return view.evaluate("""([x, y]) => { const r = Viewer.map.getContainer().getBoundingClientRect();
        const ll = Viewer.map.containerPointToLatLng([x - r.left, y - r.top]); return [ll.lat, ll.lng]; }""", [x, y])


def new_view(page, touch=False, size=(1280, 800)):
    context = page.context.browser.new_context(viewport={'width': size[0], 'height': size[1]}, locale='fr-FR',
                                               timezone_id='Europe/Paris', has_touch=touch)
    context.route('**/*', host.intercept)
    return context.new_page()


def opened(page, mock, *, touch=False, generate=True, distance='20', points=POINTS, patched=True, view=None,
           stored_points=False, zoom=14, result_zoom=15):
    """Constructeur ouvert avec `points` posés ; avec `generate`, « Terminer » : renvoie (page, mock) avec un résultat affiché."""
    view = view or new_view(page, touch)
    if patched:
        mock.base = mock.base or ensure_base(page)
    view.context.route('**/api/route-builder/route', mock.route)
    if patched:
        view.context.route('**/api/route-builder/save', mock.save)
    view.goto(host.BASE + '/')
    view.wait_for_selector('#map.leaflet-container')
    expect(view.locator('#emptyState')).to_be_visible()
    view.click('#routeBuilderOpen')
    expect(view.locator('#routeBuilderPanel')).to_be_visible()
    centre = [sum(p[0] for p in points) / len(points), sum(p[1] for p in points) / len(points)]
    view.evaluate('([c, z]) => Viewer.map.setView(c, z, { animate: false })', [centre, zoom])
    if stored_points:                                                     # petit écran : le panneau couvre la carte (pas de clic)
        view.evaluate("""async ([points, distance]) => {
            const id = (await (await fetch('/api/me')).json()).user.id;
            localStorage.setItem(`kmlviewer.routeBuilder.v1.${id}`, JSON.stringify({ points, activity: 'run', distance }));
        }""", [[list(p) for p in points], distance or ''])
        view.reload()
        view.wait_for_selector('#map.leaflet-container')
        expect(view.locator('#emptyState')).to_be_visible()
        view.click('#routeBuilderOpen')
        expect(view.locator('#routeBuilderPanel')).to_be_visible()
        view.evaluate('([c, z]) => Viewer.map.setView(c, z, { animate: false })', [centre, zoom])
    else:
        for point in points:
            x, y = to_page(view, point)
            view.mouse.click(x, y)
    try:
        expect(view.locator('#rbCount')).to_have_text(re.compile(rf'^{len(points)} point'), timeout=4000)
    except AssertionError:
        raise AssertionError(f"{len(points)} points attendus, compteur « {host.norm(view.locator('#rbCount').inner_text())} », "
                             f"message « {host.norm(view.locator('#rbMessage').inner_text())} », points {stored(view)}") from None
    if distance:
        view.fill('#rbDistance', distance)
    if generate:
        press(view, '#rbFinish')
        try:
            view.wait_for_selector('#rbResult', state='visible', timeout=120000)
        except Exception:  # noqa: BLE001
            shown = host.norm(view.locator('#rbMessage').inner_text())
            raise AssertionError(f"« Terminer » sans résultat : message « {shown} », "
                                 f"requêtes {len(mock.posts)}, mode {mock.mode}") from None
        view.evaluate('([c, z]) => Viewer.map.setView(c, z, { animate: false })', [centre, result_zoom])
        view.wait_for_timeout(300)
    return view, mock


# -- lecture de la page ---------------------------------------------------------------------------------------------------
def pins(view):
    """Centres (page) des points P1…Pn, dans l'ordre de leur numéro."""
    found = []
    for pin in view.locator('.rb-pin').all():
        box = pin.bounding_box()
        found.append((int(pin.inner_text().strip()), box['x'] + box['width'] / 2, box['y'] + box['height'] / 2))
    return [(x, y) for _, x, y in sorted(found)]


def pin_latlngs(view):
    """Position géographique des points affichés (insensible à un déplacement de la carte)."""
    return [to_latlng(view, x, y) for x, y in pins(view)]


def grab(view, number):
    """Point de prise d'un marqueur : 10 px à droite du centre (le centre est recouvert par le marqueur du fichier, (p1))."""
    x, y = pins(view)[number - 1]
    return x + 10, y


def pin(view, number):
    return view.locator('.rb-pin', has=view.locator('span', has_text=re.compile(rf'^{number}$'))).first


def stored(view):
    return view.evaluate("""() => { const key = Object.keys(localStorage).find((k) => k.startsWith('kmlviewer.routeBuilder.'));
        return key ? JSON.parse(localStorage.getItem(key)) : null; }""")


def files_shown(view):
    return view.evaluate('() => Viewer.files.size')


def summary(view):
    return host.norm(view.locator('#rbSummary').inner_text())


def map_centre(view):
    return view.evaluate('() => { const c = Viewer.map.getCenter(); return [c.lat, c.lng, Viewer.map.getZoom()]; }')


def screen_trace(view, mock):
    return [to_page(view, c) for c in mock.trace()]


# -- géométrie à l'écran ---------------------------------------------------------------------------------------------------
def nearest(points, spot):
    """Plus proche point d'une ligne brisée : (distance, rang du segment, abscisse le long de la ligne, point)."""
    best, along = None, 0.0
    for i in range(len(points) - 1):
        (x1, y1), (x2, y2) = points[i], points[i + 1]
        dx, dy = x2 - x1, y2 - y1
        length = math.hypot(dx, dy)
        t = 0 if length == 0 else max(0, min(1, ((spot[0] - x1) * dx + (spot[1] - y1) * dy) / (length ** 2)))
        px, py = x1 + t * dx, y1 + t * dy
        d = math.hypot(spot[0] - px, spot[1] - py)
        if best is None or d < best[0]:
            best = (d, i, along + t * length, (px, py))
        along += length
    return best


def alone(trace, spot, wanted):
    """Vrai si `spot` n'est proche que d'un seul passage de la trace (une boucle repasse souvent par les mêmes chemins)."""
    best = nearest(trace, spot)
    along = 0.0
    for i in range(len(trace) - 1):
        (x1, y1), (x2, y2) = trace[i], trace[i + 1]
        length = math.hypot(x2 - x1, y2 - y1)
        if abs(along - best[2]) > 80 and length:
            t = max(0, min(1, ((spot[0] - x1) * (x2 - x1) + (spot[1] - y1) * (y2 - y1)) / (length ** 2)))
            if math.hypot(spot[0] - (x1 + t * (x2 - x1)), spot[1] - (y1 + t * (y2 - y1))) < wanted + 3:
                return False
        along += length
    return True


def trace_at(trace, pins_xy, wanted, between, margin=30):
    """Un point à `wanted` pixels de la trace, entre deux points de passage (`between` = (i, j) rangs 0-based, 'close' après le
    dernier, None partout). Renvoie (x, y) ou None. Le point est loin de tout marqueur et dans la carte visible."""
    if not trace or len(trace) < 2:
        return None
    marks = [nearest(trace, p) for p in pins_xy]
    if between is None:
        low, high = 0.0, float('inf')
    elif between == 'close':
        low, high = marks[-1][2] + 30, float('inf')
    else:
        low, high = marks[between[0]][2] + 30, marks[between[1]][2] - 30
    total, cumulative = 0.0, []
    for i in range(len(trace) - 1):
        cumulative.append(total)
        total += math.hypot(trace[i + 1][0] - trace[i][0], trace[i + 1][1] - trace[i][1])
    candidates = []
    for i in range(len(trace) - 1):
        (x1, y1), (x2, y2) = trace[i], trace[i + 1]
        length = math.hypot(x2 - x1, y2 - y1)
        if length < 12:
            continue
        for t in (0.5, 0.3, 0.7, 0.2, 0.4, 0.6, 0.8, 0.1, 0.9):
            at = cumulative[i] + t * length
            if low <= at <= min(high, total):
                nx, ny = -(y2 - y1) / length, (x2 - x1) / length
                for side in (1, -1):
                    cx, cy = x1 + t * (x2 - x1) + side * nx * wanted, y1 + t * (y2 - y1) + side * ny * wanted
                    candidates.append((cx, cy))
    for cx, cy in candidates:
        if not (margin < cx < 1280 - margin and margin < cy < 800 - margin):
            continue
        hit = nearest(trace, (cx, cy))
        if abs(hit[0] - wanted) > 0.8 or not alone(trace, (cx, cy), wanted) or not low - 1 <= hit[2] <= min(high, total) + 1:
            continue
        if min(math.hypot(cx - px, cy - py) for px, py in pins_xy) < 40:
            continue
        return cx, cy
    return None


def app_positions(trace, pins_xy):
    """Positions des points le long de la trace comme la page les calcule : chacun cherché à partir du segment du précédent."""
    found, start = [], 0
    for spot in pins_xy:
        hit = nearest(trace[start:], spot)
        found.append(round(hit[2] + sum(math.hypot(trace[i + 1][0] - trace[i][0],
                trace[i + 1][1] - trace[i][1]) for i in range(start))))
        start += hit[1]
    return found


def mouse_drag(view, start, end, steps=10, release=True):
    view.mouse.move(*start)
    view.mouse.down()
    for i in range(1, steps + 1):
        view.mouse.move(start[0] + (end[0] - start[0]) * i / steps, start[1] + (end[1] - start[1]) * i / steps)
    if release:
        view.wait_for_timeout(80)                                  # Leaflet applique le dernier mouvement à l'image suivante
        view.mouse.up()


def finger(view, start, end=None, steps=10, hold=False):
    """Un doigt : appui en `start`, déplacé vers `end`, levé (sauf `hold` : renvoie la fonction qui lève le doigt)."""
    cdp = view.context.new_cdp_session(view)
    cdp.send('Input.dispatchTouchEvent', {'type': 'touchStart', 'touchPoints': [{'x': start[0], 'y': start[1]}]})
    for i in range(1, steps + 1 if end else 1):
        point = {'x': start[0] + (end[0] - start[0]) * i / steps, 'y': start[1] + (end[1] - start[1]) * i / steps}
        cdp.send('Input.dispatchTouchEvent', {'type': 'touchMove', 'touchPoints': [point]})
        view.wait_for_timeout(15)

    def lift():
        cdp.send('Input.dispatchTouchEvent', {'type': 'touchEnd', 'touchPoints': []})
        view.wait_for_timeout(150)
    if hold:
        return lift
    lift()
    return None


def same(a, b, tolerance=2e-5):
    return abs(a[0] - b[0]) <= tolerance and abs(a[1] - b[1]) <= tolerance


def press(view, selector):
    """Clic de l'utilisateur ; si un autre élément recouvre le bouton (écart relevé par (p0)), clic du script pour poursuivre."""
    try:
        view.click(selector, timeout=2500)
    except Exception:  # noqa: BLE001
        view.evaluate('(s) => document.querySelector(s).click()', selector)


def shot(view, name):
    view.screenshot(path=f'{host.OUT}/{name}.png', full_page=True)


def settle(view, ms=500):
    view.wait_for_timeout(ms)


class Checks:
    """Écarts rapportés ensemble : un écart ne masque pas la suite."""

    def __init__(self):
        self.bad, self.notes = [], []

    def note(self, label, value):
        self.notes.append(f'{label} = {value}')

    def __call__(self, label, ok, got=''):
        if not ok:
            self.bad.append(f'{label} : {got}' if got != '' else label)

    def done(self):
        assert not self.bad, ' | '.join(self.bad) + (' || ' + ' ; '.join(self.notes) if self.notes else '')


# ======================================================== CA-R41 : déplacer ====================================================
def requests_log(view):
    log = []
    view.on('request', lambda r: log.append((r.method, r.url)))
    return log


def drift(a, b):
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


def move_checks(c, view, mock, label, gesture):
    """Déplace P2 d'environ 1 km avec `gesture(start, end)` : une seule requête, au relâchement, avec les bons paramètres."""
    log = requests_log(view)
    activity = view.input_value('#rbActivity')
    seed = mock.base['route_meta'].get('seed')
    original = stored(view)['points']
    posts_before = len(mock.posts)
    view.fill('#rbDistance', '30')                                       # le formulaire change : le geste garde ceux du résultat
    p2 = grab(view, 2)
    delta = (230 if p2[0] < 640 else -230, -120)
    target = (p2[0] + delta[0], p2[1] + delta[1])
    centre = pins(view)[1]
    expected = to_latlng(view, centre[0] + delta[0], centre[1] + delta[1])             # le point suit le déplacement du pointeur
    release = gesture(p2, target)
    settle(view, 250)
    c(f'{label} : aucune requête pendant le glisser', len(mock.posts) == posts_before, len(mock.posts) - posts_before)
    mock.mode = 'hold'
    release()
    settle(view, 400)
    c(f'{label} : une requête au relâchement', len(mock.posts) == posts_before + 1, len(mock.posts) - posts_before)
    if len(mock.posts) == posts_before + 1:
        body = mock.posts[-1]
        c(f'{label} : 3 points, P2 déplacé', len(body['points']) == 3 and drift(body['points'][1], expected) < 1e-4,
          (body['points'], expected))
        c(f'{label} : P1 et P3 inchangés', drift(body['points'][0], original[0]) < 1e-6 and drift(body['points'][2],
                original[2]) < 1e-6)
        c(f'{label} : même activité', body.get('activity') == activity, body.get('activity'))
        c(f'{label} : distance du résultat (20), pas celle du formulaire (30)', body.get('distance_km') == 20,
                body.get('distance_km'))
        c(f'{label} : même graine', body.get('seed') == seed, (body.get('seed'), seed))
        c(f'{label} : pas de previous_result_id', 'previous_result_id' not in body, body)
    message = host.norm(view.locator('#rbMessage').inner_text())
    c(f'{label} : « Recalcul en cours… » pendant le calcul', 'Recalcul en cours' in message, message)
    c(f'{label} : Enregistrer inactif', view.locator('#rbSave').is_disabled())
    c(f'{label} : Autre proposition inactif', view.locator('#rbAnother').is_disabled())
    c(f'{label} : trace précédente toujours affichée', files_shown(view) == 1, files_shown(view))
    shown = summary(view)
    mock.release()
    settle(view, 600)
    c(f'{label} : une seule trace de résultat', files_shown(view) == 1, files_shown(view))
    c(f'{label} : résumé mis à jour', summary(view) != shown, (shown, summary(view)))
    c(f'{label} : « Distance voulue : 20 km »', host.norm(view.locator('#rbTarget').inner_text()) == 'Distance voulue : 20 km',
      view.locator('#rbTarget').inner_text())
    forbidden = [x for x in log if '/route-builder/save' in x[1] or (x[0] != 'GET' and '/api/activities' in x[1])]
    c(f'{label} : aucune requête save ni /api/activities', not forbidden, forbidden)


@host.scenario("(o) RB-2.7 : déplacer un point d'un résultat affiché, souris et doigt, une requête au relâchement (CA-R41)")
def rb_move(page):
    ensure_base(page)
    c = Checks()
    view, mock = opened(page, RouteMock())
    try:
        move_checks(c, view, mock, 'souris', lambda a, b: (mouse_drag(view, a, b, release=False), view.mouse.up)[1])
        # en édition (« Modifier les points ») : le point bouge, aucune requête
        shot(view, '45-rb-resultat-1280')
        press(view, '#rbEdit')
        posts, before = len(mock.posts), stored(view)['points'][2]
        p3 = grab(view, 3)
        mouse_drag(view, p3, (p3[0] - 80, p3[1] + 40))
        settle(view, 400)
        c('édition : aucune requête', len(mock.posts) == posts, len(mock.posts) - posts)
        c('édition : le point a bougé', drift(stored(view)['points'][2], before) > 1e-4,
          (before, stored(view)['points'][2], pins(view), host.norm(view.locator('#rbMessage').inner_text())))
    finally:
        view.context.close()
    view, mock = opened(page, RouteMock(), touch=True)
    try:
        def touch_gesture(a, b):
            return finger(view, a, b, hold=True)
        move_checks(c, view, mock, 'doigt', touch_gesture)
    finally:
        view.context.close()
    c.done()



# ================================================ panneau de résultat : boutons atteignables ===================================
PANEL_BUTTONS = ('rbSave', 'rbAnother', 'rbEdit', 'rbAddEnd')


def covered_buttons(view):
    """Boutons du panneau de résultat dont le centre est recouvert par un autre élément (après défilement jusqu'à eux)."""
    return view.evaluate("""(ids) => ids.map((id) => {
        const node = document.getElementById(id);
        node.scrollIntoView({ behavior: 'instant', block: 'center' });
        const r = node.getBoundingClientRect();
        const top = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
        return (top === node || node.contains(top)) ? null : { id,
                covered_by: (top && (top.id || top.className || top.tagName)) || 'rien' };
    }).filter(Boolean)""", list(PANEL_BUTTONS))


@host.scenario("(p0) RB-2.7 : boutons du panneau de résultat atteignables (Enregistrer, Autre proposition, Modifier, Ajouter)")
def rb_panel_reachable(page):
    ensure_base(page)
    c = Checks()
    for size in ((1280, 800), (1440, 900), (1920, 1080), (340, 680)):
        view = new_view(page, size=size)
        try:
            view, _ = opened(page, RouteMock(), view=view, stored_points=size[0] < 600)
            view.evaluate("() => document.getElementById('rbResult').scrollIntoView({ behavior: 'instant' })")
            covered = covered_buttons(view)
            c(f'{size[0]} × {size[1]} : boutons recouverts', not covered, covered)
            shot(view, f'46-rb-panneau-resultat-{size[0]}x{size[1]}')
        finally:
            view.context.close()
    c.done()



@host.scenario("(p1) RB-2.7 : chaque point P1…Pn se saisit sur toute sa zone de prise (24 × 24 px), pas seulement sur son anneau")
def rb_grab_zone(page):
    ensure_base(page)
    c = Checks()
    view, _ = opened(page, RouteMock())
    try:
        for number, (x, y) in enumerate(pins(view), start=1):
            blocked = []
            for dx, dy in ((0, 0), (-8, 0), (8, 0), (0, -8), (0, 8)):
                owner = view.evaluate("""([x, y]) => { const e = document.elementFromPoint(x, y);
                    return !!e && !!e.closest('.rb-pin'); }""", [x + dx, y + dy])
                if not owner:
                    blocked.append((dx, dy))
            c(f'P{number} : points de la zone de prise tenus par un autre élément (marqueur du fichier)', not blocked, blocked)
        shot(view, '47-rb-zone-de-prise')
    finally:
        view.context.close()
    c.done()


# ======================================================== CA-R42 : insérer sur la trace ========================================
def hovering(view):
    return view.evaluate("""() => { const map = document.querySelector('#map');
        return { over: map.classList.contains('rb-over-track'), cursor: getComputedStyle(map).cursor,
                 tip: [...document.querySelectorAll('.leaflet-tooltip')].map((t) => t.textContent) }; }""")


def inserted_checks(c, label, mock, posts_before, original, expected, index):
    c(f'{label} : une requête', len(mock.posts) == posts_before + 1, len(mock.posts) - posts_before)
    if len(mock.posts) != posts_before + 1:
        return
    got = mock.posts[-1]['points']
    c(f'{label} : {len(original) + 1} points', len(got) == len(original) + 1, len(got))
    if len(got) != len(original) + 1:
        return
    c(f'{label} : nouveau point au rang {index + 1}', drift(got[index], expected) < 1.5e-4, (got[index], expected))
    rest = got[:index] + got[index + 1:]
    c(f'{label} : les autres points dans l ordre', all(drift(a, b) < 1e-6 for a, b in zip(rest, original)), (rest, original))


@host.scenario("(p) RB-2.7 : insérer un point par appui sur la trace, tolérance 10 px souris et 16 px doigt (CA-R42)")
def rb_insert(page):
    ensure_base(page)
    c = Checks()
    # -- souris : survol, clic, appui + glisser, fermeture, priorité du point, résultat à 1 point
    view, mock = opened(page, RouteMock())
    try:
        trace, xy = screen_trace(view, mock), pins(view)
        near, far = trace_at(trace, xy, 5, (1, 2)), trace_at(trace, xy, 20, (1, 2))
        c('géométrie : points à 5 et 20 px de la trace trouvés', near is not None and far is not None, (near, far))
        if near and far:
            view.mouse.move(*near)
            settle(view, 300)
            state = hovering(view)
            c('survol à 5 px : curseur pointer', state['cursor'] == 'pointer', state)
            c('survol à 5 px : infobulle', 'Clique ou glisse pour ajouter un point' in state['tip'], state)
            c('survol à 5 px : point fantôme',
                    view.evaluate("() => document.querySelectorAll('#map .leaflet-overlay-pane path').length") > 0)
            view.mouse.move(*far)
            settle(view, 300)
            state = hovering(view)
            c('survol à 20 px : rien', not state['over'] and not state['tip'] and state['cursor'] != 'pointer', state)
            original = stored(view)['points']
            posts = len(mock.posts)
            view.mouse.click(*near)
            settle(view, 400)
            c.note('positions P1..P3 (calcul de la page) / globales / appui',
                   (app_positions(trace, xy), [round(nearest(trace, p)[2]) for p in xy], round(nearest(trace, near)[2])))
            inserted_checks(c, 'clic à 5 px', mock, posts, original, to_latlng(view, *near), 2)
            shot(view, '48-rb-insertion-clic')
    finally:
        view.context.close()
    view, mock = opened(page, RouteMock())
    try:
        trace, xy = screen_trace(view, mock), pins(view)
        near = trace_at(trace, xy, 5, (1, 2))
        if near:
            original, posts = stored(view)['points'], len(mock.posts)
            end = (near[0] + (200 if near[0] < 640 else -200), near[1])
            expected = to_latlng(view, *end)
            mouse_drag(view, near, end, release=False)
            settle(view, 250)
            c('appui + glisser : aucune requête pendant le glisser', len(mock.posts) == posts, len(mock.posts) - posts)
            view.mouse.up()
            settle(view, 400)
            inserted_checks(c, 'appui + glisser 200 px', mock, posts, original, expected, 2)
    finally:
        view.context.close()
    view, mock = opened(page, RouteMock())
    try:
        trace, xy = screen_trace(view, mock), pins(view)
        closing = trace_at(trace, xy, 5, 'close')
        c('géométrie : point sur le segment de fermeture trouvé', closing is not None)
        if closing:
            original, posts = stored(view)['points'], len(mock.posts)
            view.mouse.click(*closing)
            settle(view, 400)
            inserted_checks(c, 'appui après P3 (fermeture)', mock, posts, original, to_latlng(view, *closing), 3)
    finally:
        view.context.close()
    view, mock = opened(page, RouteMock())
    try:
        original, posts = stored(view)['points'], len(mock.posts)
        start = grab(view, 2)                                             # sur la trace, dans la zone de prise de P2
        mouse_drag(view, start, (start[0] + 60, start[1] + 50))
        settle(view, 400)
        c('appui dans la zone de prise de P2 : P2 déplacé, pas d insertion',
          len(mock.posts) == posts + 1 and len(mock.posts[-1]['points']) == 3, [len(p['points']) for p in mock.posts[posts:]])
    finally:
        view.context.close()
    view, mock = opened(page, RouteMock(), points=POINTS[:1])
    try:
        trace, xy = screen_trace(view, mock), pins(view)
        closing = trace_at(trace, xy, 5, None)
        c('géométrie : point de la trace d un résultat à 1 point trouvé', closing is not None)
        if closing:
            original, posts = stored(view)['points'], len(mock.posts)
            view.mouse.click(*closing)
            settle(view, 400)
            inserted_checks(c, 'résultat à 1 point', mock, posts, original, to_latlng(view, *closing), 1)
    finally:
        view.context.close()
    # -- doigt : 16 px de tolérance, un glisser commencé sur la trace crée un point, à 40 px il déplace la carte
    view, mock = opened(page, RouteMock(), touch=True)
    try:
        trace, xy = screen_trace(view, mock), pins(view)
        at12, at40 = trace_at(trace, xy, 12, (1, 2)), trace_at(trace, xy, 40, (1, 2))
        c('géométrie : points à 12 et 40 px de la trace trouvés', at12 is not None and at40 is not None, (at12, at40))
        if at12:
            original, posts = stored(view)['points'], len(mock.posts)
            finger(view, at12)
            settle(view, 400)
            inserted_checks(c, 'toucher à 12 px', mock, posts, original, to_latlng(view, *at12), 2)
    finally:
        view.context.close()
    view, mock = opened(page, RouteMock(), touch=True)
    try:
        trace, xy = screen_trace(view, mock), pins(view)
        at12, at40 = trace_at(trace, xy, 12, (1, 2)), trace_at(trace, xy, 40, (1, 2))
        if at12:
            original, posts, centre = stored(view)['points'], len(mock.posts), map_centre(view)
            end = (at12[0] + (150 if at12[0] < 640 else -150), at12[1])
            expected = to_latlng(view, *end)
            finger(view, at12, end)
            settle(view, 400)
            inserted_checks(c, 'glisser doigt commencé à 12 px', mock, posts, original, expected, 2)
            c('glisser doigt à 12 px : la carte ne bouge pas', map_centre(view) == centre, (centre, map_centre(view)))
        if at40:
            view.reload()
            view.wait_for_selector('#map.leaflet-container')
    finally:
        view.context.close()
    view, mock = opened(page, RouteMock(), touch=True)
    try:
        trace, xy = screen_trace(view, mock), pins(view)
        at40 = trace_at(trace, xy, 40, (1, 2))
        if at40:
            posts, centre, original = len(mock.posts), map_centre(view), stored(view)['points']
            finger(view, at40, (at40[0] + (120 if at40[0] < 640 else -120), at40[1] + 30))
            settle(view, 500)
            c('glisser doigt commencé à 40 px : la carte se déplace', map_centre(view)[:2] != centre[:2], (centre,
                    map_centre(view)))
            c('glisser doigt à 40 px : aucune requête, aucun point',
                    len(mock.posts) == posts and stored(view)['points'] == original,
              (len(mock.posts) - posts, stored(view)['points']))
        else:
            c('géométrie : point à 40 px de la trace non trouvé', False)
    finally:
        view.context.close()
    view, mock = opened(page, RouteMock(), touch=True)
    try:
        posts = len(mock.posts)
        start = grab(view, 2)
        finger(view, start, (start[0] + 60, start[1] + 50))
        settle(view, 400)
        c('doigt dans la zone de prise de P2 : P2 déplacé, pas d insertion',
          len(mock.posts) == posts + 1 and len(mock.posts[-1]['points']) == 3, [len(p['points']) for p in mock.posts[posts:]])
    finally:
        view.context.close()
    c.done()


@host.scenario("(p2) RB-2.7 : boucle revenant exactement sur P1 : l'appui entre P2 et P3 insère entre P2 et P3 (RB-4.7)")
def rb_insert_loop(page):
    ensure_base(page)
    c = Checks()
    view, mock = opened(page, RouteMock(loop_end_on=POINTS[0]))
    try:
        trace, xy = screen_trace(view, mock), pins(view)
        near = trace_at(trace, xy, 5, (1, 2))
        c('géométrie : point à 5 px de la trace entre P2 et P3 trouvé', near is not None)
        if near:
            c.note('positions de P1, P2, P3 selon la page', app_positions(trace, xy))
            original, posts = stored(view)['points'], len(mock.posts)
            view.mouse.click(*near)
            settle(view, 400)
            inserted_checks(c, 'boucle revenant sur P1', mock, posts, original, to_latlng(view, *near), 2)
    finally:
        view.context.close()
    c.done()


# ===================================================== CA-R43 : ajouter hors trace et retirer ==================================
def menu_button(view, label):
    return view.locator('.rb-menu button', has_text=label)


def open_menu(view, number):
    view.mouse.click(*grab(view, number))
    view.wait_for_selector('.rb-menu', timeout=3000)


def ring_click(view, number, button='left'):
    view.mouse.click(*grab(view, number), button=button)


def posted(mock, since):
    return mock.posts[since:]


@host.scenario("(q) RB-2.7 : ajouter hors de la trace, retirer (menu, clic droit), minimum 1 point, maximum 50 points (CA-R43)")
def rb_add_remove(page):
    ensure_base(page)
    c = Checks()
    view, mock = opened(page, RouteMock())
    try:
        trace, xy = screen_trace(view, mock), pins(view)
        far = trace_at(trace, xy, 40, None)
        c('géométrie : point à 40 px de la trace trouvé', far is not None)
        if far:
            original, posts = stored(view)['points'], len(mock.posts)
            view.mouse.click(*far)
            settle(view, 400)
            sent = posted(mock, posts)
            c('clic à 40 px de la trace : une requête avec P1, P2, P3, nouveau',
                    len(sent) == 1 and len(sent[0]['points']) == 4 and
              all(drift(a, b) < 1e-6 for a, b in zip(sent[0]['points'], original)) and drift(sent[0]['points'][3],
                      to_latlng(view, *far)) < 1.5e-4,
              [s_['points'] for s_ in sent])
            c('clic sur la carte : distance conservée', bool(sent) and sent[0].get('distance_km') == 20,
                    sent[0].get('distance_km') if sent else None)
    finally:
        view.context.close()
    view, mock = opened(page, RouteMock())
    try:
        trace, xy = screen_trace(view, mock), pins(view)
        far = trace_at(trace, xy, 40, None)
        if far:
            posts, centre = len(mock.posts), map_centre(view)
            mouse_drag(view, far, (far[0] + (120 if far[0] < 640 else -120), far[1] + 30))
            settle(view, 500)
            c('glisser sur la carte loin de la trace : la carte se déplace', map_centre(view)[:2] != centre[:2], (centre,
                    map_centre(view)))
            c('glisser sur la carte : aucune requête', len(mock.posts) == posts, len(mock.posts) - posts)
    finally:
        view.context.close()
    view, mock = opened(page, RouteMock())
    try:
        original, posts = stored(view)['points'], len(mock.posts)
        open_menu(view, 2)
        menu_button(view, 'Retirer ce point').click()
        settle(view, 500)
        sent = posted(mock, posts)
        c('« Retirer ce point » sur P2 : une requête sans P2', len(sent) == 1 and len(sent[0]['points']) == 2 and
          drift(sent[0]['points'][0], original[0]) < 1e-6 and drift(sent[0]['points'][1],
                  original[2]) < 1e-6, [s_['points'] for s_ in sent])
        posts = len(mock.posts)
        ring_click(view, 1, 'right')                                      # clic droit sur un point : retiré
        settle(view, 500)
        sent = posted(mock, posts)
        c('clic droit sur un point : une requête sans ce point', len(sent) == 1 and len(sent[0]['points']) == 1,
                [s_['points'] for s_ in sent])
        open_menu(view, 1)
        c('résultat à 1 point : « Retirer ce point » inactif', menu_button(view, 'Retirer ce point').is_disabled())
    finally:
        view.context.close()
    view, mock = opened(page, RouteMock())
    try:
        original, posts = stored(view)['points'], len(mock.posts)
        open_menu(view, 1)
        menu_button(view, 'Retirer ce point').click()
        settle(view, 500)
        sent = posted(mock, posts)
        c('retirer P1 : l ancien P2 est envoyé en premier', len(sent) == 1 and drift(sent[0]['points'][0], original[1]) < 1e-6,
          [s_['points'] for s_ in sent])
        c('retirer P1 : l ancien P2 est numéroté 1', pins(view) and len(pins(view)) == 2)
    finally:
        view.context.close()
    view, mock = opened(page, RouteMock(), points=POINTS[:2], distance='')
    try:
        posts = len(mock.posts)
        open_menu(view, 2)
        menu_button(view, 'Retirer ce point').click()
        settle(view, 400)
        message = host.norm(view.locator('#rbMessage').inner_text())
        c('2 points sans distance voulue : retrait refusé, sans requête', len(mock.posts) == posts, len(mock.posts) - posts)
        c('2 points sans distance voulue : message « Un seul point demande une distance voulue »',
          'Un seul point demande une distance voulue' in message, message)
    finally:
        view.context.close()
    # 50 points : appui sur la trace ou clic sur la carte refusés
    view, mock = opened(page, RouteMock(), generate=False)
    try:
        centre = [sum(p[0] for p in POINTS) / 3, sum(p[1] for p in POINTS) / 3]
        ring = [[round(centre[0] + 0.012 * math.sin(2 * math.pi * i / 50), 6),
                round(centre[1] + 0.018 * math.cos(2 * math.pi * i / 50), 6)]
                for i in range(50)]
        view.evaluate("""(points) => { const key = Object.keys(localStorage).find((k) => k.startsWith('kmlviewer.routeBuilder.'));
            const saved = JSON.parse(localStorage.getItem(key)); saved.points = points; localStorage.setItem(key,
                    JSON.stringify(saved)); }""", ring)
        view.reload()
        view.wait_for_selector('#map.leaflet-container')
        expect(view.locator('#emptyState')).to_be_visible()
        view.click('#routeBuilderOpen')
        expect(view.locator('#rbCount')).to_have_text(re.compile(r'^50 points'))
        view.evaluate('(c) => Viewer.map.setView(c, 14, { animate: false })', centre)
        view.fill('#rbDistance', '20')
        view.click('#rbFinish')
        view.wait_for_selector('#rbResult', state='visible', timeout=20000)
        settle(view, 300)
        trace, xy = screen_trace(view, mock), pins(view)
        posts = len(mock.posts)
        on_trace = trace_at(trace, xy, 5, None)
        off_trace = trace_at(trace, xy, 40, None)
        c('géométrie : points à 5 et 40 px de la trace trouvés (50 points)', on_trace is not None and off_trace is not None,
                (on_trace, off_trace))
        for label, spot in (('appui sur la trace', on_trace), ('clic sur la carte', off_trace)):
            if spot:
                view.mouse.click(*spot)
                settle(view, 300)
                message = host.norm(view.locator('#rbMessage').inner_text())
                c(f'50 points, {label} : « 50 points au plus. »', message == '50 points au plus.', message)
                c(f'50 points, {label} : aucune requête, aucun point créé', len(mock.posts) == posts and len(pins(view)) == 50,
                  (len(mock.posts) - posts, len(pins(view))))
    finally:
        view.context.close()
    c.done()


# ============================================================ CA-R44 : clavier ================================================
def active_label(view):
    return view.evaluate("() => document.activeElement.getAttribute('aria-label') || ''")


def focus_point(view, number):
    pin(view, number).focus()
    c_label = active_label(view)
    return c_label


@host.scenario("(r) RB-2.7 : clavier : flèches, Entrée, Échap, perte du focus, Suppr, menu, Ajouter après / à la fin (CA-R44)")
def rb_keyboard(page):
    ensure_base(page)
    c = Checks()
    view, mock = opened(page, RouteMock())
    try:
        focus_point(view, 1)
        view.keyboard.press('Tab')
        c('Tab depuis « Point 1 » mène à « Point 2 »', active_label(view).startswith('Point 2'), active_label(view))
        name = 'Point 2 : flèches pour déplacer, Entrée pour valider ou ouvrir le menu, Suppr pour retirer'
        c('nom accessible du point', active_label(view) == name, active_label(view))
        before, posts, original = pins(view)[1], len(mock.posts), stored(view)['points']
        for _ in range(3):
            view.keyboard.press('ArrowRight')
        settle(view, 200)
        after = pins(view)[1]
        c('3 flèches droite : environ 30 px', abs(after[0] - before[0] - 30) <= 4 and abs(after[1] - before[1]) <= 3, (before,
                after))
        c('3 flèches : aucune requête', len(mock.posts) == posts, len(mock.posts) - posts)
        view.keyboard.press('Enter')
        settle(view, 500)
        sent = posted(mock, posts)
        c('Entrée : une requête, P2 déplacé', len(sent) == 1 and len(sent[0]['points']) == 3 and drift(sent[0]['points'][1],
                original[1]) > 1e-4,
          [s_['points'] for s_ in sent])
    finally:
        view.context.close()
    for label, key in (('Échap', 'Escape'), ('perte du focus (Tab)', 'Tab')):
        view, mock = opened(page, RouteMock())
        try:
            focus_point(view, 2)
            before, posts = pins(view)[1], len(mock.posts)
            for _ in range(3):
                view.keyboard.press('ArrowRight')
            view.keyboard.press(key)
            settle(view, 400)
            c(f'{label} : le point est revenu', drift(pins(view)[1], before) < 1.5, (before, pins(view)[1]))
            c(f'{label} : aucune requête', len(mock.posts) == posts, len(mock.posts) - posts)
        finally:
            view.context.close()
    view, mock = opened(page, RouteMock())
    try:
        focus_point(view, 2)
        original, posts = stored(view)['points'], len(mock.posts)
        view.keyboard.press('Delete')
        settle(view, 500)
        sent = posted(mock, posts)
        c('Suppr sur « Point 2 » : une requête sans P2', len(sent) == 1 and len(sent[0]['points']) == 2 and
          drift(sent[0]['points'][1], original[2]) < 1e-6, [s_['points'] for s_ in sent])
    finally:
        view.context.close()
    for ending in ('Enter', 'Escape'):
        view, mock = opened(page, RouteMock())
        try:
            focus_point(view, 2)
            original, posts = stored(view)['points'], len(mock.posts)
            view.keyboard.press('Enter')                                  # sans déplacement en attente : le menu
            view.wait_for_selector('.rb-menu', timeout=3000)
            names = []
            for _ in range(3):
                names.append(host.norm(view.evaluate('() => document.activeElement.textContent')))
                view.keyboard.press('Tab')
            c('menu : boutons atteignables par Tab', names[:3] == ['Retirer ce point', 'Insérer avant',
                    'Ajouter un point après celui-ci'], names)
            for _ in range(2):
                view.keyboard.press('Shift+Tab')
            while host.norm(view.evaluate('() => document.activeElement.textContent')) != 'Ajouter un point après celui-ci':
                view.keyboard.press('Tab')
            p2, p3 = pins(view)[1], pins(view)[2]
            view.keyboard.press('Enter')
            settle(view, 300)
            c('« Ajouter un point après » : le nouveau point est focalisé', active_label(view).startswith('Point 3'),
                    active_label(view))
            middle = ((p2[0] + p3[0]) / 2, (p2[1] + p3[1]) / 2)
            fresh = pins(view)[2]
            c('nouveau point au milieu de P2 et P3', math.hypot(fresh[0] - middle[0], fresh[1] - middle[1]) < 4, (fresh, middle))
            if ending == 'Enter':
                view.keyboard.press('ArrowUp')
                view.keyboard.press('Enter')
                settle(view, 500)
                sent = posted(mock, posts)
                c('flèche haut puis Entrée : une requête avec le point entre P2 et P3',
                  len(sent) == 1 and len(sent[0]['points']) == 4 and drift(sent[0]['points'][1], original[1]) < 1e-6 and
                  drift(sent[0]['points'][3], original[2]) < 1e-6, [s_['points'] for s_ in sent])
            else:
                view.keyboard.press('Escape')
                settle(view, 300)
                c('Échap : le point temporaire est supprimé', len(pins(view)) == 3, len(pins(view)))
                c('Échap : aucune requête', len(mock.posts) == posts, len(mock.posts) - posts)
        finally:
            view.context.close()
    view, mock = opened(page, RouteMock())
    try:
        original, posts = stored(view)['points'], len(mock.posts)
        press(view, '#rbAddEnd')
        settle(view, 300)
        c('« Ajouter un point à la fin » : un point temporaire, focalisé',
                len(pins(view)) == 4 and active_label(view).startswith('Point 4'),
          (len(pins(view)), active_label(view)))
        view.keyboard.press('Enter')
        settle(view, 500)
        sent = posted(mock, posts)
        c('Entrée : une requête avec le point après P3', len(sent) == 1 and len(sent[0]['points']) == 4 and
          drift(sent[0]['points'][2], original[2]) < 1e-6, [s_['points'] for s_ in sent])
    finally:
        view.context.close()
    view, mock = opened(page, RouteMock())
    try:
        press(view, '#rbEdit')
        focus_point(view, 2)
        before, posts = stored(view)['points'][1], len(mock.posts)
        for _ in range(3):
            view.keyboard.press('ArrowRight')
        view.keyboard.press('Enter')
        settle(view, 400)
        c('édition : les touches modifient les points sans requête', len(mock.posts) == posts and
          drift(stored(view)['points'][1], before) > 1e-4, (len(mock.posts) - posts, before, stored(view)['points'][1]))
    finally:
        view.context.close()
    c.done()


# ============================================== CA-R45 : échec = retour exact à l'état précédent ==============================
TOO_FAR = "Le point 2 est trop loin d'une voie praticable : rapproche-le d'un chemin."
FAILURES = (('422', (422, TOO_FAR)), ('429', (429, 'Trop de générations : réessaie dans un instant.')),
            ('503', (503, 'Service momentanément indisponible.')), ('504', (504,
                    'Le calcul a pris trop de temps.')), ('coupure réseau', 'network'))


def gesture_move(view):
    start = grab(view, 2)
    mouse_drag(view, start, (start[0] + (150 if start[0] < 640 else -150), start[1] + 60))


def gesture_insert(view, mock):
    trace, xy = screen_trace(view, mock), pins(view)
    spot = trace_at(trace, xy, 5, (1, 2))
    view.mouse.click(*spot)


def gesture_add(view, mock):
    trace, xy = screen_trace(view, mock), pins(view)
    view.mouse.click(*trace_at(trace, xy, 40, None))


def gesture_remove(view):
    open_menu(view, 2)
    menu_button(view, 'Retirer ce point').click()


@host.scenario("(s) RB-2.7 : échec du recalcul (422, 429, 503, 504, coupure) ou Annuler : retour exact, rien d'écrit (CA-R45)")
def rb_failure(page):
    ensure_base(page)
    c = Checks()
    gestures = (('déplacement', lambda v, m: gesture_move(v)), ('insertion', gesture_insert), ('ajout', gesture_add),
                ('retrait', lambda v, m: gesture_remove(v)))
    plan = [(name, g, '422', FAILURES[0][1]) for name, g in gestures] + [('déplacement', gestures[0][1], n, f) for n,
            f in FAILURES[1:]]
    for name, gesture, label, failure in plan:
        view, mock = opened(page, RouteMock())
        tag = f'{name} / {label}'
        try:
            before = {'pins': pins(view), 'stored': stored(view), 'undo': view.locator('#rbUndo').is_disabled(),
                    'id': mock.ids[-1],
                      'summary': summary(view), 'shown': files_shown(view)}
            posts = len(mock.posts)
            mock.failure = failure
            gesture(view, mock)
            settle(view, 700)
            c(f'{tag} : une requête envoyée', len(mock.posts) == posts + 1, len(mock.posts) - posts)
            alert = host.norm(view.locator('#rbAlert').inner_text())
            reason = None if failure == 'network' else failure[1].rstrip('.')
            expected = None if reason is None else f"Recalcul impossible : {reason}. La trace précédente est conservée."
            c(f'{tag} : message dans le panneau',
                    alert.startswith('Recalcul impossible :') and alert.endswith('La trace précédente est conservée.')
              and (expected is None or alert == expected), alert)
            c(f'{tag} : annoncé aux lecteurs d écran (role=alert)', view.locator('#rbAlert').get_attribute('role') == 'alert')
            c(f'{tag} : points revenus à leur place', len(pins(view)) == len(before['pins']) and all(
                abs(a[0] - b[0]) < 1.5 and abs(a[1] - b[1]) < 1.5 for a, b in zip(pins(view),
                        before['pins'])), (before['pins'], pins(view)))
            c(f'{tag} : stockage égal à celui d avant le geste', stored(view) == before['stored'], (before['stored'],
                    stored(view)))
            c(f'{tag} : trace précédente active', files_shown(view) == before['shown'] and summary(view) == before['summary'],
              (before['summary'], summary(view)))
            c(f'{tag} : aucune entrée d historique', view.locator('#rbUndo').is_disabled() == before['undo'],
                    view.locator('#rbUndo').is_disabled())
            c(f'{tag} : Enregistrer et Autre proposition actifs', not view.locator('#rbSave').is_disabled() and
              not view.locator('#rbAnother').is_disabled())
            if name == 'déplacement' and label == '422':
                press(view, '#rbSave')
                settle(view, 300)
                c('Enregistrer envoie le result_id précédent',
                        bool(mock.saves) and mock.saves[-1].get('result_id') == before['id'],
                  (mock.saves, before['id']))
        finally:
            view.context.close()
    # « Annuler » pendant un calcul retardé
    view, mock = opened(page, RouteMock())
    try:
        before = {'pins': pins(view), 'stored': stored(view), 'undo': view.locator('#rbUndo').is_disabled()}
        mock.mode = 'hold'
        gesture_move(view)
        settle(view, 400)
        c('calcul retardé : bouton Annuler visible', view.locator('#rbCancel').is_visible())
        view.click('#rbCancel')
        settle(view, 400)
        alert = host.norm(view.locator('#rbAlert').inner_text())
        c('Annuler : « Recalcul abandonné. La trace précédente est conservée. »',
          alert == 'Recalcul abandonné. La trace précédente est conservée.', alert)
        c('Annuler : points revenus, stockage inchangé', stored(view) == before['stored'] and all(
            abs(a[0] - b[0]) < 1.5 and abs(a[1] - b[1]) < 1.5 for a, b in zip(pins(view),
                    before['pins'])), (before['pins'], pins(view)))
        c('Annuler : aucune entrée d historique', view.locator('#rbUndo').is_disabled() == before['undo'],
          {'undo_disabled': view.locator('#rbUndo').is_disabled(), 'redo_disabled': view.locator('#rbRedo').is_disabled(),
            'cancel_visible': view.locator('#rbCancel').is_visible(),
            'message': host.norm(view.locator('#rbMessage').inner_text())})
    finally:
        for held in mock.held:
            try:
                held.abort()
            except Exception:  # noqa: BLE001
                pass
        view.context.close()
    c.done()


# ============================================================ CA-R46 : historique ======================================
def undo_key(view):
    view.evaluate('() => document.activeElement && document.activeElement.blur()')
    view.keyboard.press('Control+z')
    settle(view, 350)


def redo_key(view):
    view.evaluate('() => document.activeElement && document.activeElement.blur()')
    view.keyboard.press('Control+y')
    settle(view, 350)


def state_of(view, mock):
    return {'points': stored(view)['points'], 'summary': summary(view), 'id': mock.ids[-1] if mock.ids else None}


def key_move(view, number, arrow, times=3):
    pin(view, number).focus()
    for _ in range(times):
        view.keyboard.press(arrow)
    view.keyboard.press('Enter')
    settle(view, 450)


@host.scenario("(t) RB-2.7 : Annuler / Rétablir sans requête, Tout effacer, onzième geste (plafond de 10 résultats) (CA-R46)")
def rb_history(page):
    ensure_base(page)
    c = Checks()
    view, mock = opened(page, RouteMock())
    try:
        states = [{'points': stored(view)['points'], 'summary': summary(view), 'id': mock.ids[-1]}]
        posts = len(mock.posts)
        gesture_move(view)                                                # déplacement
        settle(view, 500)
        states.append({'points': stored(view)['points'], 'summary': summary(view), 'id': mock.ids[-1]})
        trace, xy = screen_trace(view, mock), pins(view)
        spot = trace_at(trace, xy, 5, None)
        c('géométrie : point à 5 px de la trace trouvé pour l insertion', spot is not None)
        if spot:
            view.mouse.click(*spot)                                       # insertion
        settle(view, 500)
        states.append({'points': stored(view)['points'], 'summary': summary(view), 'id': mock.ids[-1]})
        trace, xy = screen_trace(view, mock), pins(view)
        spot = trace_at(trace, xy, 40, None)
        c('géométrie : point à 40 px de la trace trouvé pour l ajout', spot is not None)
        if spot:
            view.mouse.click(*spot)                                       # ajout
        settle(view, 500)
        states.append({'points': stored(view)['points'], 'summary': summary(view), 'id': mock.ids[-1]})
        open_menu(view, 1)                                                # retrait
        menu_button(view, 'Retirer ce point').click()
        settle(view, 500)
        states.append({'points': stored(view)['points'], 'summary': summary(view), 'id': mock.ids[-1]})
        c('4 gestes réussis : 4 requêtes', len(mock.posts) == posts + 4, len(mock.posts) - posts)
        c('4 gestes : points différents à chaque étape', len({json.dumps(s_['points']) for s_ in states}) == 5)
        posts = len(mock.posts)
        for k in range(3, -1, -1):
            undo_key(view)
            now = state_of(view, mock)
            c(f'Ctrl+Z vers l état {k} : points d avant', now['points'] == states[k]['points'], (now['points'],
                    states[k]['points']))
            c(f'Ctrl+Z vers l état {k} : trace d avant', now['summary'] == states[k]['summary'] and files_shown(view) == 1,
              (now['summary'], states[k]['summary'], files_shown(view)))
        c('4 Ctrl+Z : aucune requête', len(mock.posts) == posts, len(mock.posts) - posts)
        for k in range(1, 5):
            redo_key(view)
            now = state_of(view, mock)
            c(f'Ctrl+Y vers l état {k}', now['points'] == states[k]['points'] and now['summary'] == states[k]['summary'],
              (now['points'], states[k]['points']))
        c('4 Ctrl+Y : aucune requête', len(mock.posts) == posts, len(mock.posts) - posts)
        undo_key(view)                                                    # « Enregistrer » après un Ctrl+Z : le result_id affiché
        press(view, '#rbSave')
        settle(view, 300)
        c('Enregistrer après Ctrl+Z : save avec le result_id de la trace affichée',
                bool(mock.saves) and mock.saves[-1].get('result_id') == states[3]['id'],
          (mock.saves, states[3]['id']))
    finally:
        view.context.close()
    view, mock = opened(page, RouteMock())
    try:
        before, posts = state_of(view, mock), len(mock.posts)
        press(view, '#rbClear')
        press(view, '#rbClearYes')
        settle(view, 400)
        c('Tout effacer : panneau de résultat masqué', view.locator('#rbResult').is_hidden())
        c('Tout effacer : aucune trace de résultat sur la carte', files_shown(view) == 0, files_shown(view))
        c('Tout effacer : aucune requête', len(mock.posts) == posts, len(mock.posts) - posts)
        undo_key(view)
        now = state_of(view, mock)
        c('Ctrl+Z après Tout effacer : points et trace réaffichés',
                now['points'] == before['points'] and now['summary'] == before['summary'] and
          files_shown(view) == 1 and view.locator('#rbResult').is_visible(), (now, before, files_shown(view)))
        c('Ctrl+Z après Tout effacer : aucune requête', len(mock.posts) == posts, len(mock.posts) - posts)
    finally:
        view.context.close()
    # un échec n'ajoute aucune entrée : réussite, échec, réussite, puis trois Ctrl+Z
    view, mock = opened(page, RouteMock())
    try:
        initial = state_of(view, mock)
        edition_points = len(initial['points']) - 1
        key_move(view, 2, 'ArrowRight')
        after_first = state_of(view, mock)
        mock.failure = (422, TOO_FAR)
        key_move(view, 2, 'ArrowLeft')
        settle(view, 400)
        c('échec au milieu : le point est revenu', state_of(view, mock)['points'] == after_first['points'], state_of(view,
                mock)['points'])
        key_move(view, 3, 'ArrowRight')
        undo_key(view)
        c('Ctrl+Z 1 : état d après le premier geste', stored(view)['points'] == after_first['points'], stored(view)['points'])
        undo_key(view)
        c('Ctrl+Z 2 : état initial du résultat (pas de doublon dû à l échec)', stored(view)['points'] == initial['points'],
                stored(view)['points'])
        undo_key(view)
        c('Ctrl+Z 3 : retour à l édition (avant Terminer)',
                view.locator('#rbResult').is_hidden() and len(stored(view)['points']) == edition_points,
          (view.locator('#rbResult').is_hidden(), len(stored(view)['points'])))
    finally:
        view.context.close()
    # onze gestes réussis, onze Ctrl+Z : les dix premiers réaffichent une trace, le onzième revient à l'édition
    view, mock = opened(page, RouteMock())
    try:
        posts = len(mock.posts)
        for i in range(11):
            key_move(view, 1, 'ArrowRight' if i % 2 == 0 else 'ArrowLeft')
        c('onze gestes : onze requêtes', len(mock.posts) == posts + 11, len(mock.posts) - posts)
        posts = len(mock.posts)
        shown = []
        for _ in range(11):
            undo_key(view)
            shown.append(view.locator('#rbResult').is_visible() and files_shown(view) == 1)
        c('onze Ctrl+Z : les dix premiers réaffichent une trace', shown[:10] == [True] * 10, shown)
        c('onze Ctrl+Z : le onzième revient à l édition', shown[10] is False, shown)
        c('onze Ctrl+Z : aucune requête', len(mock.posts) == posts, len(mock.posts) - posts)
    finally:
        view.context.close()
    c.done()


# ============================================== CA-R47 : verrouillage et points limites =======================================
def coverage_east(page):
    import urllib.request
    with urllib.request.urlopen(host.BASE + '/api/route-builder/options', timeout=10) as answer:
        return json.load(answer)['coverage_bbox'][2]


@host.scenario("(u) RB-2.7 : verrouillage pendant le calcul, hors zone, trop près, déplacement de 3 m, un seul point (CA-R47)")
def rb_limits(page):
    ensure_base(page)
    c = Checks()
    peaks = []
    # -- verrouillage pendant un calcul retardé
    view, mock = opened(page, RouteMock())
    try:
        trace, xy = screen_trace(view, mock), pins(view)
        near, far = trace_at(trace, xy, 5, (1, 2)), trace_at(trace, xy, 40, None)
        mock.mode = 'hold'
        gesture_move(view)
        settle(view, 500)
        c('calcul retardé : une requête en cours', len(mock.held) == 1, len(mock.held))
        posts, locked = len(mock.posts), pin_latlngs(view)
        start = grab(view, 3)
        mouse_drag(view, start, (start[0] - 90, start[1] + 50))
        settle(view, 300)
        c('verrouillé : glisser un point ne le déplace pas', all(drift(a, b) < 3e-5 for a, b in zip(pin_latlngs(view), locked)),
          (locked, pin_latlngs(view)))
        if near:
            view.mouse.move(*near)
            settle(view, 300)
            c('verrouillé : survol de la trace sans indication', not hovering(view)['over'] and not hovering(view)['tip'],
                    hovering(view))
            view.mouse.click(*near)
            settle(view, 300)
        if far:
            view.mouse.click(*far)
            settle(view, 300)
        pin(view, 3).focus()
        view.keyboard.press('ArrowRight')
        view.keyboard.press('Delete')
        view.evaluate('() => document.activeElement && document.activeElement.blur()')
        view.keyboard.press('Control+z')
        settle(view, 300)
        c('verrouillé : appui, clic, flèches, Suppr et Ctrl+Z sans effet (aucune requête de plus)', len(mock.posts) == posts and
          all(drift(a, b) < 3e-5 for a, b in zip(pin_latlngs(view), locked)), (len(mock.posts) - posts, locked,
                  pin_latlngs(view)))
        c('verrouillé : Annuler le dernier / Rétablir inactifs',
                view.locator('#rbUndo').is_disabled() and view.locator('#rbRedo').is_disabled())
        message = host.norm(view.locator('#rbMessage').inner_text())
        c('verrouillé : message « Calcul en cours : attends le résultat ou annule. »',
          message in ('Calcul en cours : attends le résultat ou annule.', 'Recalcul en cours…'), message)
        mock.release()
        settle(view, 500)
        peaks.append(mock.peak)
    finally:
        for held in mock.held:
            try:
                held.abort()
            except Exception:  # noqa: BLE001
                pass
        view.context.close()
    # -- hors de la zone couverte : trace et points au bord est de la zone
    east = coverage_east(page)
    dlon = (east - 0.008) - max(p[1] for p in POINTS)
    edge = [(p[0], round(p[1] + dlon, 6)) for p in POINTS]
    shifted = RouteMock(shift=(0.0, dlon))
    view, mock = opened(page, shifted, points=edge)
    try:
        trace, xy = screen_trace(view, mock), pins(view)
        original, posts = stored(view)['points'], len(mock.posts)
        start = grab(view, 3)
        mouse_drag(view, start, (start[0] + 320, start[1]))
        settle(view, 400)
        message = host.norm(view.locator('#rbMessage').inner_text())
        c('déplacement relâché hors zone : « Hors de la zone couverte. »', message == 'Hors de la zone couverte.', message)
        c('déplacement hors zone : point revenu, aucune requête', len(mock.posts) == posts and stored(view)['points'] == original,
          (len(mock.posts) - posts, stored(view)['points'], original))
        spot = trace_at(trace, xy, 5, (1, 2))
        if spot:
            border = to_page(view, [edge[0][0], east])[0]
            view.mouse.move(*spot)
            view.mouse.down()
            for i in range(1, 11):
                view.mouse.move(spot[0] + (border + 120 - spot[0]) * i / 10, spot[1])
            view.mouse.up()
            settle(view, 400)
            message = host.norm(view.locator('#rbMessage').inner_text())
            c('insertion relâchée hors zone : « Hors de la zone couverte. »', message == 'Hors de la zone couverte.', message)
            c('insertion hors zone : aucun point créé, aucune requête', len(mock.posts) == posts and len(pins(view)) == 3,
              (len(mock.posts) - posts, len(pins(view))))
        else:
            c('géométrie : point sur la trace du bord est non trouvé', False)
        view.mouse.click(xy[2][0] + 300, xy[2][1] + 10)
        settle(view, 400)
        message = host.norm(view.locator('#rbMessage').inner_text())
        c('clic sur la carte hors zone : « Hors de la zone couverte. »',
                message == 'Hors de la zone couverte.' and len(mock.posts) == posts,
          (message, len(mock.posts) - posts))
        peaks.append(mock.peak)
    finally:
        view.context.close()
    # -- insertion relâchée à 3 m de P3
    view, mock = opened(page, RouteMock())
    try:
        trace, xy = screen_trace(view, mock), pins(view)
        spot = trace_at(trace, xy, 5, (1, 2))
        posts = len(mock.posts)
        if spot:
            target = pins(view)[2]
            view.evaluate('(p) => { Viewer.map.setView(Viewer.map.getCenter(), Viewer.map.getZoom(), { animate: false }); }', 0)
            view.mouse.move(*spot)
            view.mouse.down()
            for i in range(1, 11):
                view.mouse.move(spot[0] + (target[0] + 1 - spot[0]) * i / 10, spot[1] + (target[1] - spot[1]) * i / 10)
            view.mouse.up()
            settle(view, 400)
            message = host.norm(view.locator('#rbMessage').inner_text())
            c('insertion relâchée à 3 m de P3 : « Trop près du point 3. »', message == 'Trop près du point 3.', message)
            c('insertion trop près : aucune requête, aucun point', len(mock.posts) == posts and len(pins(view)) == 3,
              (len(mock.posts) - posts, len(pins(view))))
        else:
            c('géométrie : point à 5 px de la trace trouvé pour l insertion trop près de P3', False)
    finally:
        view.context.close()
    # -- déplacement de 3 m : rien (zoom 18 : 8 px = 3 m)
    view, mock = opened(page, RouteMock())
    try:
        centre = stored(view)['points'][1]
        view.evaluate('(c) => Viewer.map.setView(c, 18, { animate: false })', centre)
        settle(view, 300)
        undo_before, posts = view.locator('#rbUndo').is_disabled(), len(mock.posts)
        start = grab(view, 2)
        mouse_drag(view, start, (start[0] + 8, start[1]))
        settle(view, 400)
        c('déplacement de 3 m : aucune requête', len(mock.posts) == posts, len(mock.posts) - posts)
        c('déplacement de 3 m : aucune entrée d historique', view.locator('#rbUndo').is_disabled() == undo_before)
    finally:
        view.context.close()
    # -- un seul point : glisser P1
    view, mock = opened(page, RouteMock(), points=POINTS[:1])
    try:
        posts = len(mock.posts)
        start = grab(view, 1)
        mouse_drag(view, start, (start[0] + 80, start[1] + 60))
        settle(view, 500)
        sent = posted(mock, posts)
        c('un seul point : glisser P1 envoie 1 point et la distance du résultat',
          len(sent) == 1 and len(sent[0]['points']) == 1 and sent[0].get('distance_km') == 20, [(len(s_['points']),
                  s_.get('distance_km')) for s_ in sent])
    finally:
        view.context.close()
    c('jamais plus d une requête en cours', all(p <= 1 for p in peaks), peaks)
    c.done()


# ===================================== CA-R48 : trace enregistrée intacte (moteur et serveur réels) ============================
def fetch_json(view, path):
    return view.evaluate("""async (path) => {
        const r = await fetch(path); return { status: r.status, text: await r.text() }; }""", path)


@host.scenario("(v) RB-2.7 : trace enregistrée intacte pendant les gestes ; Enregistrer crée une activité neuve (CA-R48)")
def rb_saved_intact(page):
    c = Checks()
    mock = RouteMock()
    mock.mode = 'real'
    view, mock = opened(page, mock, patched=False, generate=True)
    try:
        log = requests_log(view)
        listing_before = json.loads(fetch_json(view, '/api/activities?limit=200')['text'])
        with view.expect_response(lambda r: '/api/route-builder/save' in r.url) as saved:
            press(view, '#rbSave')
        first = saved.value.json()
        c('enregistrement réel de la première trace', saved.value.status == 200 and first.get('activity_id'),
                (saved.value.status, first))
        n = first.get('activity_id')
        meta_before = fetch_json(view, f'/api/activities/{n}')
        parsed_before = fetch_json(view, f'/api/activities/{n}/parsed')
        c('lecture de l activité enregistrée', meta_before['status'] == 200 and parsed_before['status'] == 200,
                (meta_before['status'], parsed_before['status']))
        # nouvelle trace, réelle, puis gestes réels
        second_points = [(p[0] - 0.02, p[1] - 0.01) for p in POINTS]         # ailleurs : les marqueurs captent les clics
        centre = [sum(p[0] for p in second_points) / 3, sum(p[1] for p in second_points) / 3]
        view.evaluate('(c) => Viewer.map.setView(c, 14, { animate: false })', centre)
        for point in second_points:
            x, y = to_page(view, point)
            view.mouse.click(x, y)
        view.fill('#rbDistance', '20')
        view.click('#rbFinish')
        try:
            view.wait_for_selector('#rbResult', state='visible', timeout=90000)
        except Exception:  # noqa: BLE001
            message = host.norm(view.locator('#rbMessage').inner_text())
            raise AssertionError(f"la deuxième génération réelle n'a pas abouti : {message}") from None
        view.evaluate('(c) => Viewer.map.setView(c, 15, { animate: false })', centre)
        settle(view, 300)
        mark = len(log)
        start = grab(view, 2)
        mouse_drag(view, start, (start[0] + 120, start[1] + 70))               # assez loin pour que la trace change vraiment
        view.wait_for_function("() => !document.getElementById('rbCancel') || document.getElementById('rbCancel').hidden",
                timeout=120000)
        settle(view, 500)
        alert = host.norm(view.locator('#rbAlert').inner_text())
        c('geste réel : régénération acceptée par le moteur', not alert, alert)
        c('geste réel : mention « Enregistrée » absente', 'Enregistrée' not in host.norm(view.locator('#rbSaved').inner_text()))
        writes = [x for x in log[mark:] if x[0] in ('PATCH', 'DELETE') or '/route-builder/save' in x[1]]
        c('aucune requête PATCH, DELETE ni save pendant les gestes', not writes, writes)
        meta_after = fetch_json(view, f'/api/activities/{n}')
        parsed_after = fetch_json(view, f'/api/activities/{n}/parsed')
        c('activité N inchangée (fiche)', meta_after == meta_before, (meta_before['text'][:120], meta_after['text'][:120]))
        c('activité N inchangée (points)', parsed_after == parsed_before)
        with view.expect_response(lambda r: '/api/route-builder/save' in r.url) as second:
            press(view, '#rbSave')
        other = second.value.json()
        m = other.get('activity_id')
        c('Enregistrer : nouvelle activité différente de N', second.value.status == 200 and m and m != n, (n, other))
        listing_after = json.loads(fetch_json(view, '/api/activities?limit=200')['text'])
        count = lambda data: len(data.get('activities', data if isinstance(data, list) else []))  # noqa: E731
        c('la bibliothèque compte deux traces planifiées de plus', count(listing_after) == count(listing_before) + 2,
          (count(listing_before), count(listing_after)))
    finally:
        view.context.close()
    c.done()


@host.scenario("(v2) RB-2.7 : après « Enregistrer », constructeur neuf, lien vers la trace conservé (CA-R40, CA-R48)")
def rb_saved_stays(page):
    ensure_base(page)
    c = Checks()
    view, mock = opened(page, RouteMock())
    try:
        press(view, '#rbSave')
        settle(view, 500)
        c('constructeur neuf : panneau de résultat masqué', view.locator('#rbResult').is_hidden())
        c('constructeur neuf : aucun point, aucune trace de résultat', len(pins(view)) == 0 and stored(view) is None or
          (stored(view) or {}).get('points') == [], (len(pins(view)), stored(view)))
        c('constructeur neuf : compteur à 0 point', host.norm(view.locator('#rbCount').inner_text()).startswith('0 point'),
          view.locator('#rbCount').inner_text())
        saved = host.norm(view.locator('#rbSaved').inner_text())
        c('« Enregistrée » et le lien vers la trace restent affichés', 'Enregistrée' in saved and
          view.locator('#rbSaved a').count() == 1, saved)
        # un nouveau travail repart de zéro et se stocke de nouveau (CA-R40)
        x, y = to_page(view, POINTS[0])
        view.mouse.click(x + 200, y + 100)
        settle(view, 300)
        c('un point ajouté ensuite est de nouveau stocké', bool(stored(view)) and len(stored(view)['points']) == 1, stored(view))
    finally:
        view.context.close()
    c.done()



RUNS = [rb_panel_reachable, rb_grab_zone, rb_move, rb_insert, rb_insert_loop, rb_add_remove, rb_keyboard, rb_failure,
        rb_history, rb_limits, rb_saved_intact, rb_saved_stays]
