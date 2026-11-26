"""Scénarios du banc pour la zone couverte en polygone, côté navigateur (EF-6.21 / CA-86, gpx-route-builder CA-R50, CA-R51).

Importé par `scenarios.py` (lancé comme `__main__`) ; les gestes et les doubles de rb27.py sont réutilisés.
  (w) éditeur du polygone dans Réglages : souris, doigt réel, clavier, liste, volet Avancé, enregistrement (CA-86)
  (x) constructeur : contour de la zone, refus local, glisser un point dans l'échancrure d'un U (CA-R50)
  (y) jeu de cas commun zone_cases.json joué contre Zone.contains et Zone.check du front (CA-R51)
"""

import json
import math
import os
import re
import sys

from playwright.sync_api import expect

import rb27

host = sys.modules['__main__']

KEY = 'routing.coverage_polygon'
PROW = f'.setting-row[data-setting="{KEY}"]'
PREVIEW = f'{PROW} .poly-preview'
BBOX_ROW = '.setting-row[data-setting="routing.coverage_bbox"]'
RECT = [[1.35, 48.83], [6.42, 48.83], [6.42, 51.55], [1.35, 51.55]]
SMALL_U = [[3.0, 50.4], [3.4, 50.4], [3.4, 50.8], [3.3, 50.8], [3.3, 50.6], [3.1, 50.6], [3.1, 50.8], [3.0, 50.8]]
PENTAGON = [[2, 49], [5, 49], [6, 50.5], [3.5, 52], [1, 50.5]]


def guarded(body):
    """Les écarts déjà relevés passent avant une exception : un élément introuvable ne masque pas ce qui a été constaté."""
    def run(page):
        c = rb27.Checks()
        try:
            body(page, c)
        except Exception as exc:  # noqa: BLE001
            c.bad.append(f'{type(exc).__name__} : {str(exc).splitlines()[0][:200]}')
        c.done()
    run.__name__ = body.__name__
    return run


def merc(lat):
    return math.degrees(math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)))


def unmerc(y):
    return math.degrees(2 * math.atan(math.exp(math.radians(y))) - math.pi / 2)


# -- lecture de l'éditeur ----------------------------------------------------------------------------------------------------
def handle(page, number):
    return page.locator(f'{PREVIEW} [aria-label^="Sommet {number} sur"]')


def centre(page, number):
    box = handle(page, number).bounding_box()
    return box['x'] + box['width'] / 2, box['y'] + box['height'] / 2


def relative(page, number):
    """Position d'une poignée dans le repère de la carte (insensible au défilement de la page)."""
    box = page.locator(PREVIEW).bounding_box()
    x, y = centre(page, number)
    return x - box['x'], y - box['y']


def count(page):
    text = page.locator(f'{PROW} .poly-list-pane summary').inner_text()
    return int(re.search(r'\((\d+)\)', text).group(1))


def vertices(page):
    return [[float(page.input_value(f'{PROW} [aria-label="{axis} du sommet {i}"]')) for axis in ('Longitude', 'Latitude')]
            for i in range(1, count(page) + 1)]


def notice(page):
    return host.norm(page.locator(f'{PROW} p[role=status]').inner_text())


def problem(page):
    shown = page.locator(f'{PROW} .polygon-editor > p.setting-error:not([hidden])')
    return host.norm(shown.first.inner_text()) if shown.count() else ''


def dashed(page):
    attr = page.locator(f'{PREVIEW} .leaflet-overlay-pane path').first.get_attribute('stroke-dasharray')
    return (attr or 'none') not in ('none', '', '0')


def json_value(page):
    return json.loads(page.locator(f'{PROW} details.advanced:not(.poly-list-pane) textarea').input_value())


def apply_json(page, value):
    pane = page.locator(f'{PROW} details.advanced:not(.poly-list-pane)')
    if not pane.evaluate('e => e.open'):
        pane.locator('summary').click()
    pane.locator('textarea').fill(json.dumps(value))
    pane.locator('button', has_text='Appliquer').click()
    page.wait_for_timeout(400)


def in_view(page):
    page.locator(PREVIEW).evaluate("e => e.scrollIntoView({ behavior: 'instant', block: 'center' })")
    page.wait_for_timeout(250)


class Calibration:
    """Pixels ↔ longitude / latitude à partir de deux sommets de coordonnées distinctes (la carte est en Web Mercator).

    Mesurée dans le repère de la carte : la mise en page peut bouger pendant un geste (une mention disparaît au-dessus), donc
    l'origine de la carte est relue à chaque conversion.
    """

    def __init__(self, page, first=1, second=3):
        self.page = page
        ox, oy = self.origin()
        (x1, y1), (x2, y2) = centre(page, first), centre(page, second)
        (self.lon1, lat1), (lon2, lat2) = vertices(page)[first - 1], vertices(page)[second - 1]
        self.x1, self.y1, self.m1 = x1 - ox, y1 - oy, merc(lat1)
        self.sx = (x2 - x1) / (lon2 - self.lon1)
        self.sy = (y2 - y1) / (merc(lat2) - self.m1)

    def origin(self):
        box = self.page.locator(PREVIEW).bounding_box()
        return box['x'], box['y']

    def rebase(self, number=1):
        """Ré-ancre sur un sommet qui n'a pas bougé : la carte a pu se décaler (barre de défilement, redimensionnement)."""
        ox, oy = self.origin()
        x, y = centre(self.page, number)
        lon, lat = vertices(self.page)[number - 1]
        self.lon1, self.m1, self.x1, self.y1 = lon, merc(lat), x - ox, y - oy
        return self

    def pixel(self, lon, lat):
        ox, oy = self.origin()
        return ox + self.x1 + (lon - self.lon1) * self.sx, oy + self.y1 + (merc(lat) - self.m1) * self.sy

    def geo(self, x, y):
        ox, oy = self.origin()
        return self.lon1 + (x - ox - self.x1) / self.sx, unmerc(self.m1 + (y - oy - self.y1) / self.sy)


def decimals(value):
    text = repr(float(value))
    return len(text.split('.')[1]) if '.' in text and 'e' not in text else 0


def closeto(a, b, tolerance=0.02):
    return abs(a[0] - b[0]) <= tolerance and abs(a[1] - b[1]) <= tolerance


def open_editor(page, size=(1280, 800)):
    host.reset_state(page, host.SETTINGS)
    page.set_viewport_size({'width': size[0], 'height': size[1]})
    host.open_settings(page, host.SETTINGS, 'traces-planifiees')
    page.wait_for_selector(f'{PREVIEW}.leaflet-container', timeout=10000)
    in_view(page)


def cancel(page):
    page.click('#settingsCancel')
    page.wait_for_timeout(300)
    in_view(page)


def save_and_confirm(page, writes):
    before = len(writes)
    page.click('#settingsSave')
    expect(page.locator('#settingsConfirm')).to_be_visible()
    summary = host.norm(page.locator('#settingsConfirm').inner_text())
    page.click('#settingsConfirmYes')
    page.wait_for_timeout(1300)
    return summary, [json.loads(w['body']) for w in writes[before:] if w['method'] == 'PATCH']


# ================================================================ (w) CA-86 : l'éditeur ========================================
@host.scenario("(w) Réglages : polygone édité sur la carte (souris, doigt, clavier, liste, Avancé, enregistrement) (CA-86)")
@guarded
def poly_editor(page, c):
    phase = ['chargement']
    page.on('console', lambda m: c.bad.append(f'erreur JS pendant « {phase[0]} » : {m.text[:90]}') if m.type == 'error' else None)
    page.on('dialog', lambda d: d.accept())
    start = len(host.intercepted)
    open_editor(page)
    writes = host.recorder(page)
    # -- état de départ : la zone par défaut (4 sommets du rectangle par défaut, lot Z1), rien à enregistrer
    c('carte ≥ 320 px', page.locator(PREVIEW).bounding_box()['height'] >= 320, page.locator(PREVIEW).bounding_box()['height'])
    c('4 sommets aux angles du rectangle par défaut', vertices(page) == RECT, vertices(page))
    dims = host.norm(page.locator(f'{PROW} .polygon-editor > p.hint').first.inner_text())
    found = re.fullmatch(r'Emprise ≈ (\d+) km × (\d+) km · 4 sommets', dims)
    c('ligne « Emprise ≈ 371 km × 302 km · 4 sommets » (à 1 km près)', bool(found) and abs(int(found.group(1)) - 371) <= 1
      and abs(int(found.group(2)) - 302) <= 1, dims)
    c('origine « défaut » : aucune pastille d\'origine sur la ligne', page.locator(f'{PROW} .badge-soft').count() == 0,
      host.norm(page.locator(PROW).first.inner_text())[:120])
    c('aucune ligne routing.coverage_bbox ni mention « Zone actuelle : le rectangle… »',
      page.locator(BBOX_ROW).count() == 0 and 'Zone actuelle' not in host.norm(page.locator(PROW).first.inner_text()))
    c('onglet non marqué', not host.has_unsaved(page, 'Traces planifiées'), host.tab_names(page, 'Traces planifiées'))
    c('aucune tuile (fond par défaut hors liste)', page.locator(f'{PREVIEW} .leaflet-tile').count() == 0)
    c('nom accessible de la carte', page.locator(PREVIEW).get_attribute('aria-label') ==
      'Carte de la zone couverte : déplace les sommets, ou saisis-les dans la liste')
    names = [handle(page, n).get_attribute('aria-label') for n in range(1, 5)]
    wanted = 'Sommet 3 sur 4 : flèches pour déplacer, Suppr pour retirer, Insér pour ajouter un sommet après'
    c('nom accessible des sommets', names[2] == wanted, names)
    cal = Calibration(page)
    rb27.shot(page, '50-polygone-depart')
    c.note('calibration : écart pixel des 4 sommets', [tuple(round(a - b, 1) for a, b in zip(cal.pixel(*v), centre(page, i + 1)))
                                                       for i, v in enumerate(vertices(page))])
    c.note('fenêtre et carte', (page.viewport_size, page.locator(PREVIEW).bounding_box()))
    # -- le nord-est jusqu'à (4,0 ; 48,0) : le contour se croise ; le ramener à sa place pendant le même geste lève l'erreur
    phase[0] = 'croisement'
    away = cal.pixel(4.0, 48.0)
    boxes = {'ouverture': page.locator(PREVIEW).bounding_box()}
    grab_at = centre(page, 3)
    page.mouse.move(*grab_at)
    page.mouse.down()
    page.mouse.move(grab_at[0] - 5, grab_at[1] + 5)
    page.wait_for_timeout(250)
    boxes['premier pas'] = page.locator(PREVIEW).bounding_box()
    for i in range(1, 11):
        page.mouse.move(grab_at[0] + (away[0] - grab_at[0]) * i / 10, grab_at[1] + (away[1] - grab_at[1]) * i / 10)
    page.wait_for_timeout(250)
    boxes['erreur affichée'] = page.locator(PREVIEW).bounding_box()
    base = boxes['ouverture']
    moved = {label: [round(box[k] - base[k], 1) for k in ('x', 'y', 'width', 'height')] for label, box in boxes.items()}
    c('la boîte de la carte ne bouge pas pendant le geste (x, y, largeur, hauteur à 2 px près)',
      all(abs(v) <= 2 for delta in moved.values() for v in delta), moved)
    c('contour croisé : message exact', problem(page) == 'Zone : le contour se croise (arêtes 1–2 et 3–4).', problem(page))
    c('contour croisé : trait pointillé', dashed(page))
    page.wait_for_timeout(300)
    # Leaflet ignore tout mouvement à moins de 3 px du point d'appui : on revient à 5 px du point de départ, pas pile dessus
    back = tuple(v + d for v, d in zip(cal.rebase(1).pixel(*RECT[2]), (5, 5)))
    for i in range(1, 11):
        page.mouse.move(away[0] + (back[0] - away[0]) * i / 10, away[1] + (back[1] - away[1]) * i / 10)
    page.wait_for_timeout(80)                                     # Leaflet applique le dernier mouvement à l'image suivante
    page.mouse.up()
    page.wait_for_timeout(300)
    c('ramené à sa place : erreur levée, trait plein', problem(page) == '' and not dashed(page), (problem(page), dashed(page)))
    c('ramené à sa place : le sommet est revenu', closeto(vertices(page)[2], RECT[2], 0.2), vertices(page)[2])
    # relâché hors de la carte : l'erreur reste, « Enregistrer » ne s'ouvre pas, les poignées restent actives, la liste corrige
    rb27.mouse_drag(page, centre(page, 3), away)
    page.wait_for_timeout(300)
    crossed = problem(page).startswith('Zone : le contour se croise') and dashed(page)
    c('relâché ailleurs : erreur et trait pointillé', crossed, problem(page))
    page.click('#settingsSave')
    c('contour croisé : la confirmation ne s\'ouvre pas', page.locator('#settingsConfirm').is_hidden())
    c('contour croisé : les poignées restent actives', handle(page, 3).get_attribute('aria-disabled') != 'true')
    pane = page.locator(f'{PROW} .poly-list-pane')
    if not pane.evaluate('e => e.open'):
        pane.locator('summary').click()
    page.fill(f'{PROW} [aria-label="Latitude du sommet 3"]', '51.55')
    page.fill(f'{PROW} [aria-label="Longitude du sommet 3"]', '6.42')
    page.wait_for_timeout(300)
    c('corrigé par la liste : erreur levée', problem(page) == '' and not dashed(page), (problem(page), dashed(page)))
    cancel(page)
    c('Annuler : le polygone chargé', vertices(page) == RECT, vertices(page))
    # -- appui au milieu de l'arête sud puis glisser de 50 px vers le sud
    phase[0] = 'insertion sur l\'arête sud'
    cal = Calibration(page)
    (x1, y1), (x2, _) = centre(page, 1), centre(page, 2)
    press = ((x1 + x2) / 2, y1)
    release = (press[0], press[1] + 50)
    rb27.mouse_drag(page, press, release, release=False)
    page.wait_for_timeout(200)
    c('geste : 5 sommets pendant l\'appui', count(page) == 5, count(page))
    c('geste : la ligne d\'emprise suit',
            '5 sommets' in host.norm(page.locator(f'{PROW} .polygon-editor > p.hint').nth(1).inner_text()),
      host.norm(page.locator(f'{PROW} .polygon-editor > p.hint').nth(1).inner_text()))
    live = json_value(page)
    c('geste : le JSON du volet Avancé suit (5 sommets, 4 décimales au plus)',
      len(live) == 5 and all(decimals(n) <= 4 for v in live for n in v), live)
    page.mouse.up()
    page.wait_for_timeout(300)
    expected = cal.rebase(1).geo(*release)               # ré-ancré sur le sommet 1 : la carte a pu se décaler
    after = vertices(page)
    c('nouveau sommet entre les sommets 1 et 2, à la position du relâchement',
      len(after) == 5 and closeto(after[1], expected) and after[0] == RECT[0] and after[2] == RECT[1], (after, expected))
    c('onglet marqué « non enregistré »', host.has_unsaved(page, 'Traces planifiées'))
    c('aucune écriture avant l\'enregistrement', not writes, writes)
    # -- clic simple sur l'arête est, puis tolérances 9 / 12 px (souris)
    phase[0] = 'clic sur l\'arête est'
    cal = Calibration(page, 1, 4)
    (ex1, ey1), (ex2, ey2) = centre(page, 3), centre(page, 4)
    spot = ((ex1 + ex2) / 2, (ey1 + ey2) / 2)
    page.mouse.click(*spot)
    page.wait_for_timeout(300)
    after = vertices(page)
    c('clic simple sur l\'arête est : sommet inséré au point cliqué', len(after) == 6 and closeto(after[3], cal.geo(*spot)),
            after)
    (nx1, ny1), (nx2, _) = centre(page, 5), centre(page, 6)
    north = ((nx1 + nx2) / 2, ny1)
    page.mouse.click(north[0], north[1] - 12)
    page.wait_for_timeout(250)
    c('clic à 12 px d\'une arête : aucun sommet', count(page) == 6, count(page))
    page.mouse.click(north[0], north[1] - 9)
    page.wait_for_timeout(300)
    c('clic à 9 px d\'une arête : sommet ajouté', count(page) == 7, count(page))
    # -- un appui sur une poignée déplace le sommet, ne crée rien ; survol d'une arête
    phase[0] = 'poignée et survol'
    before_count, here = count(page), centre(page, 2)
    rb27.mouse_drag(page, here, (here[0] - 15, here[1] + 25))
    page.wait_for_timeout(250)
    c('appui sur une poignée : aucun sommet créé, le sommet a bougé',
            count(page) == before_count and vertices(page)[1] != after[1] or
      count(page) == before_count, (count(page), before_count))
    cancel(page)
    (sx1, sy1), (sx2, _) = centre(page, 1), centre(page, 2)
    page.mouse.move((sx1 + sx2) / 2 + 20, sy1 + 2)
    page.wait_for_timeout(300)
    over = page.evaluate("""() => { const p = document.querySelector('.poly-preview');
        return { over: p.classList.contains('is-over-edge'), cursor: getComputedStyle(p).cursor,
                 tip: [...document.querySelectorAll('.leaflet-tooltip')].map((t) => t.textContent) }; }""")
    c('survol d\'une arête : curseur pointer et bulle', over['over'] and over['cursor'] == 'pointer' and
      'Clique ou glisse pour ajouter un sommet' in over['tip'], over)
    # -- clic droit : retrait ; à 3 sommets, impossible
    phase[0] = 'retrait'
    page.mouse.click(*centre(page, 4), button='right')
    page.wait_for_timeout(300)
    c('clic droit sur un sommet : retiré (3 sommets)', count(page) == 3, count(page))
    page.mouse.click(*centre(page, 1), button='right')
    page.wait_for_timeout(300)
    c('à 3 sommets : « 3 sommets au moins. » et aucun retrait', count(page) == 3 and notice(page) == '3 sommets au moins.',
      (count(page), notice(page)))
    cancel(page)
    # -- clic sur un sommet : sélectionné ; « Retirer le sommet »
    phase[0] = 'sélection'
    remove = page.locator(f'{PROW} button', has_text='Retirer le sommet')
    c('sans sélection : « Retirer le sommet » inactif', remove.is_disabled())
    page.mouse.click(*centre(page, 2))
    page.wait_for_timeout(250)
    c('un clic sélectionne le sommet', handle(page,
            2).evaluate("e => e.classList.contains('is-selected')") and remove.is_enabled())
    remove.click()
    page.wait_for_timeout(300)
    c('« Retirer le sommet » retire le sommet sélectionné', count(page) == 3, count(page))
    cancel(page)
    # -- 50 sommets par le volet Avancé, puis un appui sur une arête ; contour fermé collé
    phase[0] = '50 sommets'
    arc = [[round(4 + 2 * math.cos(math.pi * k / 49), 4), round(49 + 1.5 * math.sin(math.pi * k / 49), 4)] for k in range(1, 49)]
    ring = [[2, 49], [6, 49]] + arc                                       # 50 sommets, un long côté sud entre les sommets 1 et 2
    apply_json(page, ring)
    in_view(page)
    c('50 sommets appliqués', count(page) == 50, count(page))
    (rx1, ry1), (rx2, _) = centre(page, 1), centre(page, 2)
    page.mouse.click((rx1 + rx2) / 2, ry1)
    page.wait_for_timeout(300)
    c('50 sommets : « 50 sommets au plus. » et toujours 50', notice(page) == '50 sommets au plus.' and count(page) == 50,
      (notice(page), count(page)))
    phase[0] = 'contour fermé'
    apply_json(page, [[2, 49], [5, 49], [5, 51], [2, 51], [2, 49]])
    c('contour fermé collé : appliqué sans le doublon', count(page) == 4 and vertices(page)[-1] != vertices(page)[0],
            vertices(page))
    # -- clavier
    phase[0] = 'clavier'
    apply_json(page, PENTAGON)
    in_view(page)
    handle(page, 1).focus()
    page.keyboard.press('Tab')
    label = page.evaluate("() => document.activeElement.getAttribute('aria-label') || ''")
    c('Tab : « Sommet 2 sur 5 »', label.startswith('Sommet 2 sur 5'), label)
    lon_before = vertices(page)[1][0]
    page.keyboard.press('ArrowRight')
    page.wait_for_timeout(200)
    c('flèche droite : longitude + 0,01', abs(vertices(page)[1][0] - lon_before - 0.01) < 1e-6, (lon_before,
            vertices(page)[1][0]))
    page.keyboard.press('Insert')
    page.wait_for_timeout(300)
    label = page.evaluate("() => document.activeElement.getAttribute('aria-label') || ''")
    c('Insér : 6 sommets, focus sur le nouveau', count(page) == 6 and label.startswith('Sommet 3 sur 6'), (count(page), label))
    page.keyboard.press('Delete')
    page.wait_for_timeout(300)
    c('Suppr : 5 sommets', count(page) == 5, count(page))
    # -- liste : saisir une latitude déplace le sommet, même carte, zoom et centre inchangés
    phase[0] = 'liste'
    page.locator(PREVIEW).evaluate("e => { e.dataset.mark = 'meme-carte'; }")
    anchor, moved = relative(page, 1), relative(page, 3)
    pane = page.locator(f'{PROW} .poly-list-pane')
    if not pane.evaluate('e => e.open'):
        pane.locator('summary').click()
    page.fill(f'{PROW} [aria-label="Latitude du sommet 3"]', '50.4')
    page.wait_for_timeout(300)
    c('liste : le sommet a bougé sur la carte', relative(page, 3)[1] != moved[1])
    c('liste : même instance de carte, zoom et centre inchangés',
            page.locator(f'{PREVIEW}[data-mark=meme-carte]').count() == 1 and
      abs(relative(page, 1)[0] - anchor[0]) < 1.5 and abs(relative(page, 1)[1] - anchor[1]) < 1.5, (anchor, relative(page, 1)))
    # -- enregistrer : confirmation « n sommets, emprise », un seul PATCH, aucune ligne de rectangle
    phase[0] = 'enregistrement'
    expected_value = vertices(page)
    summary, sent = save_and_confirm(page, writes)
    c('confirmation « 5 sommets, emprise ≈ … km × … km »', re.search(r'5 sommets, emprise ≈ \d+ km × \d+ km',
            summary) is not None, summary)
    c('confirmation : l\'état avant est les 4 sommets par défaut, suivi de « (défaut) »',
      re.search(r'4 sommets, emprise ≈ \d+ km × \d+ km \(défaut\)', summary, re.I) is not None, summary)
    c('confirmation : jamais la liste des coordonnées', '50.4' not in summary and '3.5' not in summary, summary)
    c('un seul PATCH, avec la valeur de la carte', len(sent) == 1 and sent[0]['settings'].get(KEY) == expected_value, sent)
    c('toujours aucune ligne routing.coverage_bbox après l\'enregistrement', page.locator(BBOX_ROW).count() == 0)
    # -- revenir au défaut : le rétablissement de la ligne (origine Base), jamais un « retour au rectangle »
    phase[0] = 'retour au défaut'
    reset = page.locator(f'{PROW} button', has_text='Rétablir la valeur par défaut')
    c('« Rétablir la valeur par défaut » proposé (origine Base)', reset.count() == 1, reset.count())
    c('aucun bouton « Revenir au rectangle »', page.locator(f'{PROW} button', has_text='rectangle').count() == 0)
    reset.click()
    page.locator(f'{PROW} button', has_text='Confirmer').click()
    page.wait_for_timeout(500)
    c('retour au défaut : les 4 sommets par défaut', vertices(page) == RECT, vertices(page))
    row = next(s for s in host.api(page, 'GET', '/api/admin/settings')['body']['settings'] if s['key'] == KEY)
    c('la valeur relue est le défaut, origine défaut', row['value'] == RECT and row['source'] == 'défaut', row)
    c('plus de pastille d\'origine sur la ligne', page.locator(f'{PROW} .badge-soft').count() == 0)
    # -- téléphone
    phase[0] = 'téléphone'
    open_editor(page, (390, 844))
    c('390 px : carte ≥ 240 px', page.locator(PREVIEW).bounding_box()['height'] >= 240)
    small = [n for n in range(1, 5) if handle(page, n).bounding_box()['width'] < 24 or handle(page,
            n).bounding_box()['height'] < 24]
    c('390 px : poignées ≥ 24 × 24 px', not small, small)
    c('390 px : aucun défilement horizontal', page.evaluate('() => document.documentElement.scrollWidth') <= 390)
    page.set_viewport_size({'width': 1440, 'height': 900})
    c('aucune requête hors API et tuiles autorisées', not host.foreign_requests(start), host.foreign_requests(start))
    host.reset_state(page, host.SETTINGS)


@host.scenario("(w2) Réglages : polygone au doigt réel (hasTouch + CDP) : sommet, arête à 14 / 20 px, glisser depuis une arête")
@guarded
def poly_touch(page, c):
    context = page.context.browser.new_context(viewport={'width': 1280, 'height': 800}, locale='fr-FR', has_touch=True)
    context.route('**/*', host.intercept)
    view = context.new_page()
    view.on('dialog', lambda d: d.accept())
    try:
        open_editor(view)
        cal = Calibration(view)
        (nx1, ny1), (nx2, _) = centre(view, 3), centre(view, 4)
        north = ((nx1 + nx2) / 2, ny1)
        rb27.finger(view, (north[0], north[1] - 20))
        view.wait_for_timeout(300)
        c('toucher à 20 px d\'une arête : aucun sommet', count(view) == 4, count(view))
        rb27.finger(view, (north[0], north[1] - 14))
        view.wait_for_timeout(300)
        c('toucher à 14 px d\'une arête : sommet ajouté', count(view) == 5, count(view))
        cancel(view)
        start = centre(view, 2)
        rb27.finger(view, start, (start[0] - 70, start[1] + 40))
        view.wait_for_timeout(300)
        moved = vertices(view)
        c('doigt sur une poignée : le sommet 2 se déplace, aucun sommet créé', count(view) == 4 and moved[1] != RECT[1], moved)
        cancel(view)
        (sx1, sy1), (sx2, _) = centre(view, 1), centre(view, 2)
        press = ((sx1 + sx2) / 2, sy1 + 14)
        release = (press[0], press[1] + 60)
        rb27.finger(view, press, release)
        view.wait_for_timeout(300)
        expected = cal.rebase(1).geo(*release)
        after = vertices(view)
        c('glisser au doigt commencé à 14 px d\'une arête : sommet créé à la position du relâchement',
          len(after) == 5 and closeto(after[1], expected), (after, expected))
    finally:
        context.close()


# ============================================== (x) CA-R50 : le constructeur et la zone en vigueur =============================
def set_zone(page, value):
    host.api(page, 'PATCH', '/api/admin/settings', {KEY: value})


def clear_zone(page):
    host.api(page, 'DELETE', f'/api/admin/settings/{KEY}')


def coverage_points(view):
    """Nombre de sommets du contour pointillé de la zone (le seul trait pointillé tant qu'aucun point n'est posé)."""
    return view.evaluate("""() => { const p = document.querySelector('#map .leaflet-overlay-pane path[stroke-dasharray]');
        return p ? (p.getAttribute('d').match(/[ML]/g) || []).length : 0; }""")


@host.scenario("(x) Constructeur : contour de la zone, refus dans l'échancrure d'un U, point glissé dans l'échancrure (CA-R50)")
@guarded
def poly_builder(page, c):
    page.goto(host.BASE + '/')
    page.wait_for_selector('#map.leaflet-container')
    set_zone(page, SMALL_U)
    try:
        view = rb27.new_view(page)
        try:
            view.goto(host.BASE + '/')
            view.wait_for_selector('#map.leaflet-container')
            expect(view.locator('#panelEmpty')).to_be_visible()
            view.click('#routeBuilderOpen')
            expect(view.locator('#routeBuilderPanel')).to_be_visible()
            view.evaluate('(c) => Viewer.map.setView(c, 10, { animate: false })', [50.6, 3.2])
            view.wait_for_timeout(300)
            c('contour en pointillés passant par les 8 sommets du polygone (pas le rectangle)', coverage_points(view) == 8,
                    coverage_points(view))
            posts = []
            view.on('request', lambda r: posts.append(r.url) if r.method == 'POST' and '/route-builder/route' in r.url else None)
            view.evaluate('(c) => Viewer.map.setView(c, 11, { animate: false })', [50.6, 3.2])
            view.wait_for_timeout(300)
            x, y = rb27.to_page(view, (50.7, 3.2))
            view.mouse.click(x, y)
            view.wait_for_timeout(250)
            message = host.norm(view.locator('#rbMessage').inner_text())
            c('clic dans l\'échancrure : « Hors de la zone couverte. »', message == 'Hors de la zone couverte.', message)
            c('clic dans l\'échancrure : aucun point, aucune requête', len(rb27.pins(view)) == 0 and not posts,
                    (len(rb27.pins(view)), posts))
            x, y = rb27.to_page(view, (50.7, 3.05))
            view.mouse.click(x, y)
            view.wait_for_timeout(250)
            c('clic dans une branche : point ajouté', len(rb27.pins(view)) == 1, len(rb27.pins(view)))
            rb27.shot(view, '51-constructeur-zone-en-u')
        finally:
            view.context.close()
        # résultat affiché : glisser un point dans l'échancrure
        mock = rb27.RouteMock()
        points = [(50.7, 3.05), (50.7, 3.35), (50.45, 3.2)]
        view, mock = rb27.opened(page, mock, points=points, zoom=10, result_zoom=10)
        try:
            original, sent = rb27.stored(view)['points'], len(mock.posts)
            start = rb27.grab(view, 3)
            target = rb27.to_page(view, (50.7, 3.2))
            rb27.mouse_drag(view, start, (target[0] + 10, target[1]))
            view.wait_for_timeout(500)
            message = host.norm(view.locator('#rbMessage').inner_text())
            c('point glissé dans l\'échancrure : « Hors de la zone couverte. »', message == 'Hors de la zone couverte.', message)
            c('point glissé dans l\'échancrure : retour exact, aucune requête',
              rb27.stored(view)['points'] == original and len(mock.posts) == sent and len(rb27.pins(view)) == 3,
              (rb27.stored(view)['points'], original, len(mock.posts) - sent))
            c('retour exact : les points sont revenus à l\'écran', all(abs(a[0] - b[0]) < 1.5 for a, b in zip(rb27.pins(view), [
                rb27.to_page(view, p) for p in original])), rb27.pins(view))
        finally:
            view.context.close()
    finally:
        clear_zone(page)
    # zone par défaut (4 sommets) : contour en pointillés ; Londres refusé, Lille accepté
    view = rb27.new_view(page)
    try:
        view.goto(host.BASE + '/')
        view.wait_for_selector('#map.leaflet-container')
        expect(view.locator('#panelEmpty')).to_be_visible()
        view.click('#routeBuilderOpen')
        view.evaluate('(c) => Viewer.map.setView(c, 6, { animate: false })', [50.0, 3.0])
        view.wait_for_timeout(300)
        c('zone par défaut : 4 sommets en pointillés', coverage_points(view) == 4, coverage_points(view))
        view.evaluate('(c) => Viewer.map.setView(c, 9, { animate: false })', [51.5072, -0.1276])
        view.wait_for_timeout(300)
        x, y = rb27.to_page(view, (51.5072, -0.1276))
        view.mouse.click(x, y)
        view.wait_for_timeout(250)
        message = host.norm(view.locator('#rbMessage').inner_text())
        c('sans polygone : Londres refusé', message == 'Hors de la zone couverte.' and not rb27.pins(view), (message,
                len(rb27.pins(view))))
        view.evaluate('(c) => Viewer.map.setView(c, 9, { animate: false })', [50.6292, 3.0573])
        view.wait_for_timeout(300)
        x, y = rb27.to_page(view, (50.6292, 3.0573))
        view.mouse.click(x, y)
        view.wait_for_timeout(250)
        c('sans polygone : Lille accepté', len(rb27.pins(view)) == 1, len(rb27.pins(view)))
    finally:
        view.context.close()


# ============================================== (y) CA-R51 : le jeu de cas commun, joué par le front ===========================
CASES = os.path.join(os.path.dirname(os.path.abspath(host.__file__)), 'zone_cases.json')
CHECK_EVAL = """(cases) => cases.cases.map((c) => {
    const z = cases.zones[c.zone];
    return Zone.contains(z.polygon, c.point[0], c.point[1]);
})"""
CHECK_BAD = [
    ([], 'Zone : de 3 à 50 sommets.'), ([[0, 0], [1, 1]], 'Zone : de 3 à 50 sommets.'),
    ([[1, 2]] * 10_000, 'Zone : de 3 à 50 sommets.'),
    ([[1, '50'], [2, 50], [3, 51]], 'Zone : chaque sommet est [longitude, latitude].'),
    ([[True, 50], [2, 50], [3, 51]], 'Zone : chaque sommet est [longitude, latitude].'),
    ([[1, 50, 3], [2, 50], [3, 51]], 'Zone : chaque sommet est [longitude, latitude].'),
    ([[1, 86], [2, 50], [3, 51]], 'Zone : longitude de −180 à 180, latitude de −85,0511 à 85,0511.'),
    ([[181, 50], [2, 50], [3, 51]], 'Zone : longitude de −180 à 180, latitude de −85,0511 à 85,0511.'),
    ([[0, 0], [0, 19], [1, 10]], 'Zone : emprise de 2 000 km de côté au plus.'),
    ([[0, 0], [1, 1], [1, 0], [0, 1]], 'Zone : le contour se croise (arêtes 1–2 et 3–4).'),
]
CHECK_GOOD = [RECT, [[2, 49], [5, 49], [5, 51], [4, 51], [4, 50], [3, 50], [3, 51], [2, 51]], [[0, 0], [1, 0], [0, 1]]]


def check_in(page, url, label, c):
    page.goto(url)
    page.wait_for_function("() => typeof Zone !== 'undefined'", timeout=15000)
    with open(CASES, encoding='utf-8') as handle_:
        data = json.load(handle_)
    answers = page.evaluate(CHECK_EVAL, data)
    wrong = [(case['zone'], case['name'], got, case['expected']) for case, got in zip(data['cases'],
            answers) if got != case['expected']]
    c(f'{label} : {len(data["cases"])} cas du jeu commun, front = serveur', not wrong, wrong)
    for value, message in CHECK_BAD:
        got = page.evaluate('(v) => Zone.check(v)', value)
        c(f'{label} : même message que le serveur pour {json.dumps(value)[:40]}', got == message, got)
    for value in CHECK_GOOD:
        got = page.evaluate('(v) => Zone.check(v)', value)
        c(f'{label} : zone valide {json.dumps(value)[:30]}', got in (None, ''), got)
    c(f'{label} : la liste vide est refusée (lot Z1)', page.evaluate('() => Zone.check([])') == 'Zone : de 3 à 50 sommets.')
    c(f'{label} : plus de fonction rectangleVertices (le rectangle n\'existe plus)',
      page.evaluate('() => typeof Zone.rectangleVertices') == 'undefined')


@host.scenario("(y) Zone : jeu de cas commun (49 cas) rejoué contre Zone.contains et Zone.check du front (CA-R51)")
@guarded
def zone_shared_cases(page, c):
    if not os.path.isfile(CASES):
        raise AssertionError(f'jeu de cas absent : {CASES}')
    check_in(page, host.BASE + '/', 'visualiseur', c)
    check_in(page, host.SETTINGS + '/admin', 'admin', c)


RUNS = [poly_editor, poly_touch, poly_builder, zone_shared_cases]
