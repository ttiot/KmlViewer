"""
Scénarios du banc navigateur (Playwright, Chromium réel). Lancé par run.sh dans l'image Playwright épinglée.
Chaque scénario vérifie ET photographie ; le code de sortie est non nul au premier écart. Captures dans /out (hors dépôt).
"""

import base64
import json
import os
import re
import sys
import time
import traceback
import urllib.parse
import urllib.request

from playwright.sync_api import expect, sync_playwright

BASE = os.environ['BENCH_URL'].rstrip('/')
OIDC = os.environ.get('BENCH_OIDC_URL', '').rstrip('/')
SETTINGS = os.environ.get('BENCH_SETTINGS_URL', '').rstrip('/')      # instance 3 : registre v1, état exact du CA-72
# instance 4 : registre de test (list, requires_restart, secret)
REGISTRY = os.environ.get('BENCH_REGISTRY_URL', '').rstrip('/')
DEM_APP = os.environ.get('BENCH_DEM_URL', '').rstrip('/')           # instance 5 : DEM_URL posé (service d'altitude factice)
DEM_SVC = os.environ.get('BENCH_DEM_SVC', '').rstrip('/')           # service d'altitude factice : /gate/open, /gate/close
NOPW = os.environ.get('BENCH_NOPW_URL', '').rstrip('/')            # mode oidc SANS SECRETS_KEY : invitations indisponibles
OUT = os.environ.get('BENCH_OUT', '/out')
results = []

# Aucune sortie réseau (EF-6.17) : toute requête du navigateur vers un autre hôte que les applications de test est
# INTERCEPTÉE (page.route), ENREGISTRÉE puis BLOQUÉE ; les tuiles des hôtes de tuiles reçoivent une image locale de 1 pixel.
# La liste sert de preuve (CA-76).
APP_HOSTS = {urllib.parse.urlsplit(u).netloc for u in (BASE, OIDC, SETTINGS, REGISTRY, DEM_APP, NOPW) if u}
TILE_HOST_SUFFIXES = ('tile.openstreetmap.org', 'tile.opentopomap.org', 'basemaps.cartocdn.com', 'server.arcgisonline.com',
                      'tiles.exemple.fr')
PIXEL = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=')
intercepted = []


def intercept(route):
    parts = urllib.parse.urlsplit(route.request.url)
    if parts.netloc in APP_HOSTS:
        route.continue_()
        return
    host = parts.hostname or ''
    if any(host == suffix or host.endswith('.' + suffix) for suffix in TILE_HOST_SUFFIXES):
        intercepted.append({'host': host, 'url': route.request.url[:160], 'action': 'tuile locale de 1 pixel'})
        route.fulfill(status=200, content_type='image/png', body=PIXEL)
        return
    intercepted.append({'host': host, 'url': route.request.url[:160], 'action': 'bloquée'})
    route.abort()


def foreign_requests(since=0, tiles=False):
    """Hôtes joints (hors applications de test) depuis la position `since`, avec ou sans les tuiles."""
    return [r for r in intercepted[since:] if tiles or r['action'] == 'bloquée']


def record(name, ok, detail=''):
    results.append({'scenario': name, 'ok': bool(ok), 'detail': detail})
    print(('OK   ' if ok else 'ECHEC'), name, detail)


def scenario(name):
    def wrap(fn):
        def run(page):
            try:
                fn(page)
                record(name, True)
            except Exception as exc:  # noqa: BLE001
                line = traceback.extract_tb(exc.__traceback__)[-1]
                record(name, False, f'{type(exc).__name__}: {str(exc)[:1200]} [scenarios.py:{line.lineno} {line.line}]')
                try:
                    page.screenshot(path=f'{OUT}/ECHEC-{re.sub(r"[^a-z0-9]+", "-", name.lower())}.png', full_page=True)
                except Exception:  # noqa: BLE001
                    pass
        run.__name__ = fn.__name__
        return run
    return wrap


@scenario("(a) Créer une trace : popup masqué, clic au centre = un point posé")
def builder(page):
    page.goto(BASE + '/')
    page.wait_for_selector('#map.leaflet-container')
    empty = page.locator('#emptyState')
    expect(empty).to_be_visible()                                        # sans fichier : le popup est là
    box = page.locator('#emptyState .viewer-empty-box').bounding_box()
    cx, cy = box['x'] + box['width'] / 2, box['y'] + box['height'] / 2   # l'endroit exact du popup
    page.screenshot(path=f'{OUT}/01-visualiseur-etat-vide.png')
    page.click('#routeBuilderOpen')
    expect(page.locator('#routeBuilderPanel')).to_be_visible()
    expect(empty).to_be_hidden()                                         # le popup ne couvre plus la carte
    on_map = page.evaluate(
        "([x, y]) => { const e = document.elementFromPoint(x, y); return !!e && !!e.closest('#map'); }", [cx, cy])
    assert on_map, "le centre de l'ancien popup n'est pas sur la carte (un élément le recouvre)"
    expect(page.locator('#rbCount')).to_have_text(re.compile(r'^0 point'))
    page.mouse.click(cx, cy)
    expect(page.locator('#rbCount')).to_have_text(re.compile(r'^1 point'))      # un point posé, pas un formulaire de point
    assert page.locator('#map .leaflet-marker-icon').count() >= 1
    page.screenshot(path=f'{OUT}/02-constructeur-point-pose.png')
    page.click('#rbClose')
    expect(page.locator('#routeBuilderPanel')).to_be_hidden()
    expect(empty).to_be_visible()                                        # fermeture sans trace : le popup revient
    page.screenshot(path=f'{OUT}/03-constructeur-ferme-popup-revenu.png')


@scenario("(b1) /admin : diagnostic du moteur lisible, jamais de JSON brut")
def admin_diagnostic(page):
    page.goto(BASE + '/admin#fonctions')
    page.wait_for_selector('#featuresBox .diagnostic', timeout=15000)
    text = page.locator('#featuresBox').inner_text()
    raw_epoch = re.search(r'\b1[6-9]\d{8}\b', text)
    assert '{"' not in text and 'available' not in text and 'checked_at' not in text and not raw_epoch, text[:300]
    assert re.search(r'Moteur (disponible|indisponible|non configuré)', text), text[:300]
    assert re.search(r'Vérifié|Pas encore vérifié', text)
    page.screenshot(path=f'{OUT}/04-admin-diagnostic.png', full_page=True)


@scenario("(b2) /admin : journal d'audit lisible")
def admin_audit(page):
    page.goto(BASE + '/admin#journal')
    page.wait_for_selector('#auditBody tr', timeout=15000)
    text = page.locator('#auditBody').inner_text()
    assert 'user.create' in text or 'setting' in text, text[:300]
    assert not re.search(r'^\s*[{\[]', text) and '[object Object]' not in text and 'undefined' not in text, text[:300]
    page.screenshot(path=f'{OUT}/05-admin-journal.png', full_page=True)


@scenario("(d) mode oidc + comptes à mot de passe : UNE page de connexion (SSO + formulaire), pas d'envoi d'office vers l'IdP")
def oidc_login(page):
    assert OIDC, "BENCH_OIDC_URL absent"
    response = page.goto(OIDC + '/auth/login?next=/dashboard')
    assert response.status == 200 and OIDC in page.url, page.url                 # aucune redirection vers l'IdP
    expect(page.locator('#ssoButton')).to_be_visible()
    expect(page.locator('#ssoButton')).to_have_text('Se connecter avec le SSO')
    expect(page.locator('#loginUsername')).to_be_visible()
    expect(page.locator('#loginPassword')).to_be_visible()
    href = page.locator('#ssoButton').get_attribute('href')
    next_value = urllib.parse.parse_qs(urllib.parse.urlsplit(href).query).get('next')
    assert next_value == ['/dashboard'] and 'sso=1' in href                      # `next` conservé par le bouton
    page.screenshot(path=f'{OUT}/06-connexion-oidc-page-unique.png')
    page.goto(OIDC + '/')                                    # une route protégée mène à la même page, pas à l'IdP
    assert OIDC in page.url, page.url
    assert page.locator('#ssoButton').count() == 1 and page.locator('#loginForm').count() == 1


@scenario("(e) /admin Réglages : bandeau « E-mail non configuré » quand le SMTP est absent")
def smtp_banner(page):
    page.goto(BASE + '/admin#reglages')
    page.wait_for_selector('#settingsAlerts .alert', timeout=15000)
    text = page.locator('#settingsAlerts').inner_text()
    assert 'E-mail non configuré' in text and 'mot de passe oublié' in text, text
    page.screenshot(path=f'{OUT}/07-admin-bandeau-smtp-absent.png', full_page=True)



# -- Page Réglages (admin-space §5.6b, CA-72 à CA-78) ---------------------------------------------------------------------------
# Les sélecteurs sont regroupés ici : si le DOM de la page change, c'est le seul endroit à adapter.
SECTIONS_ORDER = ['Sécurité', 'E-mail', 'Carte', 'Traces planifiées', 'Altitude', 'Dossier surveillé', 'Stockage', 'Général']
COUNTS = {'Sécurité': 11, 'E-mail': 6, 'Carte': 5, 'Traces planifiées': 16, 'Altitude': 7, 'Dossier surveillé': 3, 'Stockage': 1,
          'Général': 1}
STALE_MESSAGE = "Un autre admin a modifié ces réglages : recharge la page avant d'enregistrer."
TILE = 'https://a.tiles.exemple.fr/{z}/{x}/{y}.png'


def norm(text):
    """Texte à espaces simples et apostrophes droites (la page peut employer l'apostrophe typographique)."""
    return re.sub(r'\s+', ' ', (text or '')).replace('’', "'").strip()


def field(key):
    return f'[id="f-{key}"]'


def row(key):
    return f'.setting-row[data-setting="{key}"]'


def tab(section):
    return f'#settingsTabs [role=tab][data-section="{section}"]'


def tab_text(page, section):
    return norm(page.locator(tab(section)).inner_text())


def api(page, method, path, body=None):
    return page.evaluate("""async ([method, path, body]) => {
        const init = { method, headers: { 'Content-Type': 'application/json' } };
        if (body) init.body = JSON.stringify(body);
        const r = await fetch(path, init);
        return { status: r.status, body: await r.json().catch(() => null) };
    }""", [method, path, body])


def reset_state(page, base):
    """Remet l'instance dans l'état du CA-72 : en base seulement `map.zoom` (9), aucun secret configuré."""
    page.goto(base + '/admin')
    page.wait_for_selector('#adminTabs')
    for setting in api(page, 'GET', '/api/admin/settings')['body']['settings']:
        if setting['source'] == 'base':
            assert api(page, 'DELETE', f"/api/admin/settings/{setting['key']}")['status'] == 200
    info = api(page, 'GET', '/api/admin/secrets')['body'] or {}
    for secret in info.get('secrets', []):
        if secret.get('configured'):
            api(page, 'DELETE', f"/api/admin/secrets/{secret['name']}")
    assert api(page, 'PATCH', '/api/admin/settings', {'settings': {'map.zoom': 9}})['status'] == 200


def open_settings(page, base, sub=''):
    page.goto(f"{base}/admin#reglages{'/' + sub if sub else ''}")
    page.reload()
    page.wait_for_selector('#settingsTabs [role=tab]', timeout=15000)


def recorder(page):
    """Requêtes d'écriture (PATCH, DELETE) de la page vers /api/admin/settings."""
    writes = []
    page.on('request', lambda r: writes.append({'method': r.method, 'url': r.url, 'body': r.post_data})
            if r.method in ('PATCH', 'DELETE') and '/api/admin/settings' in r.url else None)
    return writes


def hits(page):
    found = {}
    for section in SECTIONS_ORDER:
        match = re.search(r'(\d+) / (\d+)', tab_text(page, section))
        assert match, f"{section} : compteur « k / n » absent : {tab_text(page, section)}"
        found[section] = int(match.group(1))
    return found


def selected_tab(page):
    return page.locator('#settingsTabs [role=tab][aria-selected=true]').get_attribute('data-section')


def basemap_value(page, base):
    return next(s for s in api(page, 'GET', '/api/admin/settings')['body']['settings'] if s['key'] == 'map.basemaps')['value']


@scenario("(c1) Réglages : 8 sous-onglets, compteurs, ancre, rôles ARIA, flèches, alerte variable invalide (CA-72)")
def settings_tabs(page):
    reset_state(page, SETTINGS)
    open_settings(page, SETTINGS)
    tabs = page.locator('#settingsTabs [role=tab]')
    assert tabs.count() == 8, tabs.count()
    found_tabs = [t.get_attribute('data-section') for t in tabs.all()]
    assert found_tabs == SECTIONS_ORDER, found_tabs
    for section in SECTIONS_ORDER:
        modified = section in ('Carte', 'Altitude')                      # map.zoom en base, DEM_TIMEOUT dans l'env
        pattern = re.escape(section) + f' {COUNTS[section]}' + (r'(?: ·)? 1 modifié' if modified else '')
        if section == 'Carte':
            # tiles=a.tiles.exemple.fr : les fonds par défaut sont hors liste (alerte voulue)
            pattern += ' !'
        assert re.fullmatch(pattern, tab_text(page, section)), tab_text(page, section)
    assert page.locator('#settingsTabs[role=tablist]').count() == 1, 'rôle tablist'
    assert page.locator('[role=tabpanel]').count() == 1, 'rôle tabpanel'
    assert page.locator('#settingsTabs [role=tab][aria-selected=true]').count() == 1, 'un seul onglet sélectionné'
    assert selected_tab(page) == 'Sécurité'                              # sans ancre : le premier onglet
    page.screenshot(path=f'{OUT}/09-reglages-sous-onglets.png', full_page=True)
    page.goto(f'{SETTINGS}/admin#reglages/carte')                        # ancre : ouvre Carte ; un rechargement rouvre Carte
    page.reload()
    page.wait_for_selector('#settingsTabs [role=tab]')
    assert selected_tab(page) == 'Carte', selected_tab(page)
    page.reload()
    page.wait_for_selector('#settingsTabs [role=tab]')
    assert selected_tab(page) == 'Carte', selected_tab(page)
    page.goto(f'{SETTINGS}/admin#reglages/inconnu')
    page.reload()
    page.wait_for_selector('#settingsTabs [role=tab]')
    assert selected_tab(page) == 'Sécurité', selected_tab(page)
    page.goto(f'{SETTINGS}/admin#reglages/traces-planifiees')            # section écrite sans accents, avec des tirets
    page.reload()
    page.wait_for_selector('#settingsTabs [role=tab]')
    assert selected_tab(page) == 'Traces planifiées', selected_tab(page)
    page.goto(f'{SETTINGS}/admin#reglages')                              # flèches du clavier
    page.reload()
    page.wait_for_selector('#settingsTabs [role=tab]')
    page.focus(tab('Sécurité'))
    page.keyboard.press('ArrowRight')
    assert selected_tab(page) == 'E-mail', selected_tab(page)
    page.keyboard.press('ArrowRight')
    assert selected_tab(page) == 'Carte', selected_tab(page)
    page.keyboard.press('ArrowLeft')
    assert selected_tab(page) == 'E-mail', selected_tab(page)
    # DEM_TIMEOUT=abc (instance 4) : pastille d'alerte sur Altitude seulement, qui nomme la variable sans sa valeur (CA-74)
    open_settings(page, REGISTRY, 'altitude')
    assert '!' in tab_text(page, 'Altitude'), tab_text(page, 'Altitude')
    assert '!' not in tab_text(page, 'Sécurité'), [tab_text(page, x) for x in SECTIONS_ORDER]       # Carte : alerte tiles= voulue
    panel = norm(page.locator('#settingsPanel').inner_text())
    assert 'DEM_TIMEOUT invalide' in panel and not re.search(r'\babc\b', panel), panel[:300]
    page.screenshot(path=f'{OUT}/10-reglages-variable-invalide.png', full_page=True)


@scenario("(c2) Réglages : recherche, compteurs k / n, filtre « modifiées », saisie gardée sous un filtre (CA-73)")
def settings_search(page):
    reset_state(page, SETTINGS)
    open_settings(page, SETTINGS)
    writes = recorder(page)
    page.fill('#settingsSearch', 'delai')                                # « Délai » : sans accents ni casse
    found = hits(page)
    assert {s for s, k in found.items() if k > 0} == {'Traces planifiées', 'Altitude', 'E-mail'}, found
    for section in SECTIONS_ORDER:
        opacity = float(page.locator(tab(section)).evaluate('e => getComputedStyle(e).opacity'))
        assert (opacity < 1) == (found[section] == 0), (section, opacity)    # les onglets sans résultat sont grisés
    assert selected_tab(page) == 'Sécurité'                              # l'onglet ouvert ne change jamais tout seul
    panel = norm(page.locator('#settingsPanel').inner_text())
    assert 'Aucun réglage ne correspond' in panel
    links = {norm(b.inner_text()) for b in page.locator('#settingsPanel button').all()}
    assert {'Traces planifiées', 'Altitude', 'E-mail'} <= links, links    # liens vers les onglets qui ont des résultats
    page.screenshot(path=f'{OUT}/11-reglages-recherche.png', full_page=True)
    page.locator('#settingsPanel button', has_text='Altitude').click()
    assert selected_tab(page) == 'Altitude' and page.locator(row('DEM_TIMEOUT')).count() == 1
    page.fill('#settingsSearch', 'DEM_URL')                              # nom de variable d'env
    assert {s for s, k in hits(page).items() if k > 0} == {'Altitude'}
    page.fill('#settingsSearch', 'smtp.host')                            # clé
    assert {s for s, k in hits(page).items() if k > 0} == {'E-mail'}
    page.fill('#settingsSearch', 'DÉLAI')                                # majuscules et accents : même résultat
    assert {s for s, k in hits(page).items() if k > 0} == {'Traces planifiées', 'Altitude', 'E-mail'}
    page.fill('#settingsSearch', 'zzz-introuvable')
    assert all(k == 0 for k in hits(page).values())
    page.fill('#settingsSearch', '')
    # interrupteur « seulement les valeurs modifiées » : 2 réglages (DEM_TIMEOUT dans l'env, map.zoom en base)
    page.check('#settingsOnlyModified')
    found = hits(page)
    assert sum(found.values()) == 2 and found['Altitude'] == 1 and found['Carte'] == 1, found
    page.click(tab('Carte'))
    assert page.locator('.setting-row').count() == 1 and page.locator(row('map.zoom')).count() == 1
    page.click(tab('Altitude'))
    assert page.locator('.setting-row').count() == 1 and page.locator(row('DEM_TIMEOUT')).count() == 1
    page.fill('#settingsSearch', 'zoom')                                 # combiné à la recherche : les deux conditions
    assert sum(hits(page).values()) == 1 and hits(page)['Carte'] == 1
    page.fill('#settingsSearch', '')
    page.uncheck('#settingsOnlyModified')
    # une saisie sur un réglage ensuite masqué est conservée, présente dans la confirmation et dans le PATCH
    page.click(tab('Carte'))
    page.fill(field('map.zoom'), '12')
    page.fill('#settingsSearch', 'delai')                                # masque map.zoom
    assert page.locator(row('map.zoom')).count() == 0
    assert 'non enregistré' in tab_text(page, 'Carte')
    assert page.locator('#settingsSave').is_enabled()                    # l'onglet ouvert peut encore être enregistré
    page.click('#settingsSave')
    expect(page.locator('#settingsConfirm')).to_be_visible()
    confirm = norm(page.locator('#settingsConfirm').inner_text())
    assert 'Zoom initial' in confirm and '12' in confirm, confirm
    page.screenshot(path=f'{OUT}/12-reglages-saisie-masquee.png', full_page=True)
    page.click('#settingsConfirmYes')
    page.wait_for_timeout(1500)
    assert len(writes) == 1 and writes[0]['method'] == 'PATCH'
    payload = json.loads(writes[0]['body'])
    assert payload['settings'] == {'map.zoom': 12} and isinstance(payload['version'], int), payload
    # ni l'URL ni le stockage du navigateur ne contiennent le texte recherché
    page.fill('#settingsSearch', 'texte-recherche-secret')
    page.check('#settingsOnlyModified')
    leaked = page.evaluate("() => JSON.stringify([location.href, {...localStorage}, {...sessionStorage}])")
    assert 'texte-recherche-secret' not in leaked and 'settingsOnlyModified' not in leaked, leaked


@scenario("(c3) Réglages : pastilles d'origine, rétablissement avec confirmation dans la page, aide repliée (CA-74)")
def settings_origin(page):
    reset_state(page, SETTINGS)
    dialogs = []
    page.on('dialog', lambda d: (dialogs.append(d.message), d.dismiss()))
    open_settings(page, SETTINGS, 'altitude')
    writes = recorder(page)

    def origin(key):
        return norm(page.locator(f'{row(key)} .setting-head').inner_text())

    assert 'Environnement · DEM_TIMEOUT' in origin('DEM_TIMEOUT'), origin('DEM_TIMEOUT')
    assert 'Défaut' in origin('DEM_BATCH_SIZE'), origin('DEM_BATCH_SIZE')
    assert not page.locator(f"{row('DEM_TIMEOUT')} button", has_text='Rétablir').count(), 'bouton Rétablir sur une origine env'
    assert not page.locator(f"{row('DEM_BATCH_SIZE')} button", has_text='Rétablir').count(), 'Rétablir sur origine défaut'
    hint = norm(page.locator(f"{row('DEM_TIMEOUT')} .setting-hint").inner_text())
    assert hint == '1 à 120 s · variable DEM_TIMEOUT', hint
    page.click(tab('Carte'))
    assert 'Base' in origin('map.zoom'), origin('map.zoom')
    reset = page.locator(f"{row('map.zoom')} button", has_text='Rétablir')
    assert reset.count() == 1 and norm(reset.inner_text()) == 'Rétablir la valeur par défaut → 6', norm(reset.inner_text())
    helps = page.locator('details.setting-help')
    assert helps.count() >= 1, 'aucune aide `details.setting-help` sur la page'
    assert all(not h.evaluate('e => e.open') for h in helps.all()), "l'aide n'est pas repliée au chargement"
    # base qui surcharge une variable d'env : « remplace DEM_TIMEOUT » et « Rétablir la valeur d'environnement → 30 s »
    assert api(page, 'PATCH', '/api/admin/settings', {'settings': {'DEM_TIMEOUT': 45}})['status'] == 200
    open_settings(page, SETTINGS, 'altitude')
    # la préparation par l'API n'est pas une écriture de la page
    writes.clear()
    head = origin('DEM_TIMEOUT')
    assert 'Base' in head and 'remplace DEM_TIMEOUT' in head, head
    reset = page.locator(f"{row('DEM_TIMEOUT')} button", has_text='Rétablir')
    assert norm(reset.inner_text()) == "Rétablir la valeur d'environnement → 30 s", norm(reset.inner_text())
    page.screenshot(path=f'{OUT}/13-reglages-origines.png', full_page=True)
    page.fill(field('DEM_BATCH_SIZE'), '200')                            # une saisie d'un AUTRE réglage de l'onglet
    reset.click()                                                        # confirmation dans la page (jamais confirm())
    confirm_buttons = page.locator(f"{row('DEM_TIMEOUT')} button")
    assert confirm_buttons.filter(has_text='Confirmer').count() == 1, 'confirmation dans la page'
    assert confirm_buttons.filter(has_text='Annuler').count() == 1, 'annulation dans la page'
    confirm_buttons.filter(has_text='Annuler').click()                   # annuler : aucune requête
    assert writes == [] and dialogs == [], (writes, dialogs)
    page.locator(f"{row('DEM_TIMEOUT')} button", has_text='Rétablir').click()
    page.locator(f"{row('DEM_TIMEOUT')} button", has_text='Confirmer').click()
    page.wait_for_timeout(1500)
    assert [w['method'] for w in writes] == ['DELETE'] and writes[0]['url'].endswith('/api/admin/settings/DEM_TIMEOUT'), writes
    assert 'Environnement · DEM_TIMEOUT' in origin('DEM_TIMEOUT'), origin('DEM_TIMEOUT')        # la pastille d'origine a changé
    assert page.input_value(field('DEM_BATCH_SIZE')) == '200', 'saisie perdue après le rétablissement'
    assert 'non enregistré' in tab_text(page, 'Altitude') and dialogs == [], (tab_text(page, 'Altitude'), dialogs)
    stored = page.evaluate("() => JSON.stringify([{...localStorage}, {...sessionStorage}])")
    assert '200' not in stored, stored


@scenario("(c4) Réglages : fonds de carte (cartes ordonnées, contrôle d'hôte, ordre, volet Avancé) (CA-75)")
def settings_basemaps(page):
    reset_state(page, SETTINGS)
    start = len(intercepted)
    page.goto(SETTINGS + '/admin')
    effective = basemap_value(page, SETTINGS)
    open_settings(page, SETTINGS, 'carte')
    cards = page.locator('.basemap-card')
    assert cards.count() == len(effective)
    for index, wanted in enumerate(effective):                           # les cartes reprennent les fonds effectifs, dans l'ordre
        assert page.input_value(f'#bm-{index}-name') == wanted['name'] and page.input_value(f'#bm-{index}-url') == wanted['url']
    # état de départ : un seul fond autorisé (a.tiles.exemple.fr)
    assert api(page, 'PATCH', '/api/admin/settings', {'settings': {
        'map.basemaps': [{'name': 'un', 'url': TILE, 'subdomains': 'abc', 'attribution': '© Exemple', 'max_zoom': 19}],
        'map.default_basemap': 'un'}})['status'] == 200
    open_settings(page, SETTINGS, 'carte')
    writes = recorder(page)

    def add_card(name, url, subdomains='abc'):
        index = page.locator('.basemap-card').count()
        page.click('button:has-text("Ajouter un fond")')
        page.fill(f'#bm-{index}-name', name)
        page.fill(f'#bm-{index}-url', url)
        page.fill(f'#bm-{index}-subdomains', subdomains)
        return index

    def error_of(index):
        return norm(page.locator(f'.basemap-card[data-index="{index}"] .setting-error').inner_text())

    # écarts à la spec : tous rapportés, aucun ne masque la suite
    problems = []

    def soft(label, check):
        try:
            check()
        except Exception as exc:  # noqa: BLE001
            problems.append(f'{label} : {type(exc).__name__}: {str(exc)[:160]}')
            if page.locator('#settingsConfirm').is_visible():
                page.click('#settingsConfirmNo')

    def refuse(index, needle):
        page.click('#settingsSave')
        expect(page.locator('#settingsConfirm')).to_be_hidden()          # la confirmation est bloquée
        message = error_of(index)
        assert message and needle in message, message
        assert writes == [], writes                                      # aucune requête
        return message

    # hôte absent de tiles= : le message nomme b.tiles.exemple.fr et tiles=
    index = add_card('deux', 'https://{s}.tiles.exemple.fr/{z}/{x}/{y}.png', 'ab')
    message = refuse(index, 'b.tiles.exemple.fr')
    assert 'tiles=' in message
    page.screenshot(path=f'{OUT}/14-reglages-fond-hote-refuse.png', full_page=True)
    page.fill(f'#bm-{index}-url', 'https://a.tiles.exemple.fr/{z}/{x}/tile.png')                 # sans {y}
    refuse(index, '{y}')
    page.fill(f'#bm-{index}-url', 'http://a.tiles.exemple.fr/{z}/{x}/{y}.png')                   # http://
    refuse(index, 'https://')
    page.fill(f'#bm-{index}-url', TILE)
    page.fill(f'#bm-{index}-name', 'mauvais<nom')                                                # balisage dans le nom
    soft("nom contenant « < » : erreur sous le nom avant l'envoi", lambda: refuse(index, ''))
    page.fill(f'#bm-{index}-name', 'UN')                                                         # doublon, casse ignorée
    refuse(index, 'déjà')
    page.fill(f'#bm-{index}-name', 'deux')
    page.fill(f'#bm-{index}-attribution', 'A & B')                                               # texte brut seulement
    refuse(index, 'texte brut')
    page.fill(f'#bm-{index}-attribution', '© Exemple')
    # Monter / Descendre puis enregistrer : le visualiseur reçoit les fonds dans le nouvel ordre
    page.locator(f'.basemap-card[data-index="{index}"] button', has_text='Monter').click()

    def moves_keep_typing():
        kept = page.input_value('#bm-0-attribution')
        assert kept == '© Exemple', f"Monter a perdu la saisie : attribution « {kept} » au lieu de « © Exemple »"

    soft("Monter / Descendre / Ajouter / Supprimer gardent les saisies faites depuis le dernier dessin", moves_keep_typing)
    page.fill('#bm-0-attribution', '© Exemple')
    names = [page.input_value(f'#bm-{i}-name') for i in range(2)]
    assert names == ['deux', 'un'], names
    page.click('#settingsSave')
    expect(page.locator('#settingsConfirm')).to_be_visible()
    summary = norm(page.locator('#settingsConfirm').inner_text())
    assert 'ajouté' in summary or 'ordre' in summary, summary
    page.click('#settingsConfirmYes')
    page.wait_for_timeout(1500)
    assert len(writes) == 1 and json.loads(writes[0]['body'])['settings'].keys() == {'map.basemaps'}
    config = api(page, 'GET', '/api/map-config')['body']
    assert [b['name'] for b in config['basemaps']] == ['deux', 'un'], config
    # fond par défaut : supprimer ou renommer le fond par défaut ⇒ erreur sous « Fond par défaut »
    options = [norm(o.inner_text()) for o in page.locator(f'{field("map.default_basemap")} option').all()]
    assert options == ['deux', 'un'], options                                                    # la liste montre les noms saisis
    page.fill('#bm-1-name', 'renomme')                                                           # « un » est le fond par défaut
    assert 'renomme' in [norm(o.inner_text()) for o in page.locator(f'{field("map.default_basemap")} option').all()]
    page.click('#settingsSave')
    expect(page.locator('#settingsConfirm')).to_be_hidden()
    assert 'fond par défaut' in norm(page.locator(f'{row("map.default_basemap")} .setting-error').inner_text()).lower()
    assert len(writes) == 1
    page.fill('#bm-1-name', 'un')
    # volet « Avancé » : JSON = valeur en cours de saisie ; modifié puis « Appliquer » ⇒ cartes à jour ; invalide ⇒ erreur
    page.fill('#bm-0-name', 'deux-bis')
    advanced = page.locator(f'{row("map.basemaps")} details.advanced')
    advanced.locator('summary').click()
    area = advanced.locator('textarea')
    shown = json.loads(area.input_value())

    def json_is_live():
        names = [b['name'] for b in shown]
        assert names == ['deux-bis', 'un'], f"JSON affiché {names} au lieu de ['deux-bis', 'un']"

    soft("volet Avancé : JSON = valeur en cours de saisie", json_is_live)
    shown[0]['name'] = 'via-json'
    area.fill(json.dumps(shown))
    advanced.locator('button', has_text='Appliquer').click()
    assert page.input_value('#bm-0-name') == 'via-json'
    pane = page.locator(f'{row("map.basemaps")} details.advanced')
    if not pane.evaluate('e => e.open'):
        pane.locator('summary').click()                                  # (la page referme le volet après « Appliquer »)
    area = pane.locator('textarea')
    area.fill('{ pas du json')
    page.locator(f'{row("map.basemaps")} details.advanced button', has_text='Appliquer').click()
    assert page.input_value('#bm-0-name') == 'via-json'                                          # éditeur structuré inchangé
    assert page.locator(f'{row("map.basemaps")} details.advanced .setting-error').is_visible()
    page.screenshot(path=f'{OUT}/15-reglages-fonds-avance.png', full_page=True)
    # « Ajouter » désactivé à 10 fonds (état posé par l'API, rechargement)
    ten = [{'name': f'f{i}', 'url': TILE, 'subdomains': 'abc', 'attribution': '', 'max_zoom': 19} for i in range(10)]
    state = {'settings': {'map.basemaps': ten, 'map.default_basemap': 'f0'}}
    assert api(page, 'PATCH', '/api/admin/settings', state)['status'] == 200
    # le rechargement des saisies est permis
    page.on('dialog', lambda d: d.accept())
    open_settings(page, SETTINGS, 'carte')
    assert page.locator('.basemap-card').count() == 10
    assert page.locator('button:has-text("Ajouter un fond")').is_disabled()
    # envoi forcé hors de la page d'un fond hors tiles= : 400 du serveur (CA-69)
    forced = api(page, 'PATCH', '/api/admin/settings', {'settings': {'map.basemaps': [
        {'name': 'x', 'url': 'https://evil.example/{z}/{x}/{y}.png', 'subdomains': 'abc', 'attribution': '', 'max_zoom': 19}]}})
    assert forced['status'] == 400 and forced['body']['field'] == 'map.basemaps' and forced['body']['index'] == 0, forced
    assert foreign_requests(start) == [], foreign_requests(start)        # aucune requête vers un hôte externe
    assert not problems, ' | '.join(problems)


@scenario("(c5) Réglages : rectangle de couverture (4 champs, dimensions, aperçu, contrôles) et requêtes interceptées (CA-76)")
def settings_rectangle(page):
    reset_state(page, SETTINGS)
    start = len(intercepted)
    open_settings(page, SETTINGS, 'traces-planifiees')
    writes = recorder(page)
    got = [page.input_value(f'#rect-{i}') for i in range(4)]
    assert got == ['1.35', '48.83', '6.42', '51.55'], got
    page.wait_for_selector('.rect-preview.leaflet-container', timeout=10000)
    dims = norm(page.locator('.rectangle-editor .hint').first.inner_text())
    assert dims == '≈ 371 km × 302 km', dims
    # sans fond autorisé (le fond par défaut « osm » est hors tiles=) : le rectangle seul, aucune tuile, aucune requête externe
    assert page.locator('.rect-preview .leaflet-tile').count() == 0, 'des tuiles alors que le fond par défaut est hors liste'
    path = page.locator('.rect-preview path.leaflet-interactive')
    assert path.count() == 1, f'rectangle de l aperçu : {path.count()} tracé(s)'
    before = path.get_attribute('d')
    page.screenshot(path=f'{OUT}/16-reglages-rectangle.png', full_page=True)
    page.fill('#rect-2', '5')                                            # l'aperçu suit chaque saisie valide
    page.wait_for_timeout(300)
    assert norm(page.locator('.rectangle-editor .hint').first.inner_text()) != dims, 'dimensions non mises à jour'
    assert page.locator('.rect-preview path.leaflet-interactive').get_attribute('d') != before, 'aperçu non mis à jour'
    page.fill('#rect-0', '7')                                            # longitude min ≥ longitude max
    err = norm(page.locator('.rectangle-editor .setting-error').first.inner_text())
    assert err, "une erreur est attendue sous le champ"
    page.click('#settingsSave')
    expect(page.locator('#settingsConfirm')).to_be_hidden()
    assert writes == [], writes
    for index, value in enumerate(['0', '40', '30', '51']):              # côté > 2 000 km : erreur avant la confirmation
        page.fill(f'#rect-{index}', value)
    assert norm(page.locator('.rectangle-editor .setting-error').first.inner_text()), 'pas d erreur pour un côté > 2 000 km'
    page.click('#settingsSave')
    expect(page.locator('#settingsConfirm')).to_be_hidden()
    assert writes == [], writes
    page.screenshot(path=f'{OUT}/17-reglages-rectangle-trop-grand.png', full_page=True)
    # volet « Avancé » : même comportement que pour les fonds
    for index, value in enumerate(['1.35', '48.83', '6.42', '51.55']):
        page.fill(f'#rect-{index}', value)
    advanced = page.locator('.rectangle-editor details.advanced')
    advanced.locator('summary').click()
    shown = json.loads(advanced.locator('textarea').input_value())
    problems = [] if shown == [1.35, 48.83, 6.42, 51.55] else [f"volet Avancé : JSON affiché {shown}, pas la valeur saisie"]
    advanced.locator('textarea').fill('[1, 48, 6, 51]')
    advanced.locator('button', has_text='Appliquer').click()
    assert [page.input_value(f'#rect-{i}') for i in range(4)] == ['1', '48', '6', '51']
    if not advanced.evaluate('e => e.open'):
        advanced.locator('summary').click()
    advanced.locator('textarea').fill('[1, 2]')
    advanced.locator('button', has_text='Appliquer').click()
    assert [page.input_value(f'#rect-{i}') for i in range(4)] == ['1', '48', '6', '51']          # forme invalide : inchangé
    assert advanced.locator('.setting-error').is_visible(), 'pas d erreur pour un JSON de forme invalide'
    assert foreign_requests(start) == [] and not [r for r in intercepted[start:] if r['action'] != 'bloquée'], intercepted[start:]
    # fond par défaut autorisé : l'aperçu demande des tuiles, mais SEULEMENT à l'hôte autorisé, et la page les reçoit en 1 pixel
    assert api(page, 'PATCH', '/api/admin/settings', {'settings': {
        'map.basemaps': [{'name': 'un', 'url': TILE, 'subdomains': 'abc', 'attribution': '', 'max_zoom': 19}],
        'map.default_basemap': 'un'}})['status'] == 200
    mark = len(intercepted)
    open_settings(page, SETTINGS, 'traces-planifiees')
    page.wait_for_selector('.rect-preview.leaflet-container', timeout=10000)
    page.wait_for_timeout(1500)
    tiles = intercepted[mark:]
    assert tiles and all(r['host'] == 'a.tiles.exemple.fr' and r['action'].startswith('tuile') for r in tiles), tiles
    assert foreign_requests(mark) == []
    page.screenshot(path=f'{OUT}/18-reglages-rectangle-fond-autorise.png', full_page=True)
    assert not problems, ' | '.join(problems)


@scenario("(c6) Réglages : enregistrement par onglet, avant/après, 400, version périmée, rien ne se perd (CA-77)")
def settings_save(page):
    reset_state(page, SETTINGS)
    dialogs = []
    page.on('dialog', lambda d: (dialogs.append((d.type, d.message)), d.accept()))
    open_settings(page, SETTINGS, 'carte')
    writes = recorder(page)
    audits_before = len(api(page, 'GET', '/api/admin/audit?action=setting.update&limit=200')['body']['entries'])
    assert page.locator('#settingsSave').is_disabled()                   # inactif sans changement
    page.fill(field('map.zoom'), '9.0')                                  # même valeur (9 = 9.0) : pas un changement
    assert page.locator('#settingsSave').is_disabled()
    page.fill(field('map.zoom'), '10')
    page.fill(field('map.center_lat'), '45')
    page.click(tab('Altitude'))
    page.fill(field('DEM_BATCH_SIZE'), '200')
    assert 'non enregistré' in tab_text(page, 'Carte') and 'non enregistré' in tab_text(page, 'Altitude')
    assert 'non enregistré' not in tab_text(page, 'Sécurité')
    page.click(tab('Carte'))
    page.click('#settingsSave')
    expect(page.locator('#settingsConfirm')).to_be_visible()
    items = page.locator('#settingsConfirm li')
    assert items.count() == 2
    text = norm(page.locator('#settingsConfirm').inner_text())
    assert 'Zoom initial' in text and 'Latitude initiale' in text and '→' in text and 'Base' in text, text
    page.screenshot(path=f'{OUT}/19-reglages-confirmation-avant-apres.png', full_page=True)
    page.click('#settingsConfirmNo')                                     # Annuler : aucune requête
    assert writes == []
    page.click('#settingsSave')
    page.click('#settingsConfirmYes')
    page.wait_for_timeout(1500)
    assert len(writes) == 1
    payload = json.loads(writes[0]['body'])
    assert payload['settings'].keys() == {'map.zoom', 'map.center_lat'} and isinstance(payload['version'], int), payload
    entries = api(page, 'GET', '/api/admin/audit?action=setting.update&limit=200')['body']['entries']
    assert len(entries) == audits_before + 2, (audits_before, len(entries))                    # un audit par clé modifiée
    assert 'non enregistré' not in tab_text(page, 'Carte') and 'non enregistré' in tab_text(page, 'Altitude')
    page.click(tab('Altitude'))
    assert page.input_value(field('DEM_BATCH_SIZE')) == '200'            # Altitude garde sa saisie et sa pastille
    page.click('#adminTabs a[data-tab="journal"]')                       # changer d'onglet d'administration, puis revenir
    page.click('#adminTabs a[data-tab="reglages"]')
    page.wait_for_selector('#settingsTabs [role=tab]')
    page.click(tab('Altitude'))
    assert page.input_value(field('DEM_BATCH_SIZE')) == '200'
    stored = page.evaluate("() => JSON.stringify([{...localStorage}, {...sessionStorage}])")
    assert 'DEM_BATCH_SIZE' not in stored and 'map.zoom' not in stored and '"200"' not in stored, stored
    # deux enregistrements successifs dans la même page : aucun 409
    page.click(tab('Carte'))
    page.fill(field('map.zoom'), '11')
    page.click('#settingsSave')
    page.click('#settingsConfirmYes')
    page.wait_for_timeout(1200)
    page.fill(field('map.zoom'), '12')
    page.click('#settingsSave')
    page.click('#settingsConfirmYes')
    page.wait_for_timeout(1200)
    assert [json.loads(w['body'])['settings'] for w in writes[1:]] == [{'map.zoom': 11}, {'map.zoom': 12}], writes
    assert api(page, 'GET', '/api/map-config')['body']['zoom'] == 12
    # 400 du serveur : message sous le champ, aria-invalid, focus, saisies gardées, rien d'écrit
    # (l'hôte SMTP n'est pas contrôlé par la page)
    page.click(tab('E-mail'))
    page.fill(field('smtp.host'), 'evil.example')
    page.click('#settingsSave')
    page.click('#settingsConfirmYes')
    page.wait_for_timeout(1200)
    holder = page.locator(f'{row("smtp.host")} .setting-error')
    assert norm(holder.first.inner_text()), "message attendu sous le champ en cause"
    assert page.get_attribute(field('smtp.host'), 'aria-invalid') == 'true'
    assert page.evaluate('() => document.activeElement && document.activeElement.id') == 'f-smtp.host'
    assert page.input_value(field('smtp.host')) == 'evil.example'                                # saisie gardée
    assert next(s for s in api(page, 'GET', '/api/admin/settings')['body']['settings'] if s['key'] == 'smtp.host')['value'] == ''
    page.screenshot(path=f'{OUT}/20-reglages-erreur-400.png', full_page=True)
    page.click('#settingsCancel')                                        # annuler les modifications de l'onglet
    assert page.input_value(field('smtp.host')) == '' and page.locator('#settingsSave').is_disabled()
    # version périmée : un autre admin enregistre entre-temps
    page.click(tab('Carte'))
    page.fill(field('map.zoom'), '10')
    # l'autre admin (sans version)
    assert api(page, 'PATCH', '/api/admin/settings', {'settings': {'map.zoom': 14}})['status'] == 200
    sent = len(writes)
    page.click('#settingsSave')
    page.click('#settingsConfirmYes')
    page.wait_for_timeout(1200)
    confirm = norm(page.locator('#settingsConfirm').inner_text())
    assert STALE_MESSAGE in confirm and page.locator('#settingsConfirm button', has_text='Recharger').count() == 1, confirm
    assert len(writes) == sent + 1                                       # une seule tentative, pas de nouvel essai automatique
    assert api(page, 'GET', '/api/map-config')['body']['zoom'] == 14     # rien n'est écrit
    page.screenshot(path=f'{OUT}/21-reglages-version-perimee.png', full_page=True)
    page.locator('#settingsConfirm button', has_text='Recharger').click()
    page.wait_for_timeout(800)
    assert page.input_value(field('map.zoom')) == '10'                   # la saisie est gardée
    page.click('#settingsSave')                                          # les nouveaux « avant » : 14
    text = norm(page.locator('#settingsConfirm').inner_text())
    assert '14' in text and '10' in text, text
    page.click('#settingsConfirmYes')
    page.wait_for_timeout(1200)
    assert api(page, 'GET', '/api/map-config')['body']['zoom'] == 10
    # rechargement avec des saisies : avertissement natif du navigateur (beforeunload)
    page.fill(field('map.zoom'), '13')
    # le gestionnaire est posé tant qu'il y a des saisies
    assert page.evaluate("() => typeof window.onbeforeunload") == 'function'
    page.reload()
    page.wait_for_selector('#settingsTabs [role=tab]')
    assert page.input_value(field('map.zoom')) == '10'                   # après rechargement : valeur du serveur, plus de saisie


@scenario("(c7) Réglages : éditeur de liste, redémarrage nécessaire, secret absent — registre de test (CA-78, CA-79)")
def settings_registry(page):
    reset_state(page, REGISTRY)
    dialogs = []
    page.on('dialog', lambda d: (dialogs.append((d.type, d.message)), d.accept()))
    open_settings(page, REGISTRY, 'general')
    writes = recorder(page)
    page_text = norm(page.locator('#settingsPanel').inner_text())
    assert 'Secret du banc' not in page_text and page.locator(row('bench.secret')).count() == 0   # un secret n'a pas d'éditeur
    assert 'Secret du banc' not in norm(page.locator('main, body').first.inner_text())
    box = '[aria-label="Ajouter à Étiquettes (banc)"]'
    add = f'{row("bench.tags")} button:has-text("Ajouter")'

    def chips():
        return [norm(c.inner_text()).rstrip('×').strip() for c in page.locator(f'{row("bench.tags")} .chip').all()]

    page.fill(box, 'a')
    page.press(box, 'Enter')                                             # ajout par Entrée
    assert chips() == ['a']
    page.fill(box, 'b')
    page.click(add)                                                      # ajout par le bouton
    assert chips() == ['a', 'b']
    page.fill(box, 'a')
    page.press(box, 'Enter')                                             # doublon : erreur
    assert norm(page.locator(f'{row("bench.tags")} .setting-error').first.inner_text()) and chips() == ['a', 'b']
    page.fill(box, 'c')
    page.press(box, 'Enter')
    assert chips() == ['a', 'b', 'c'] and page.locator(add).is_disabled()                         # désactivé à 3 éléments
    page.screenshot(path=f'{OUT}/22-reglages-liste.png', full_page=True)
    page.click(f'{row("bench.tags")} [aria-label="Retirer b"]')          # la croix retire un élément
    assert chips() == ['a', 'c'] and page.locator(add).is_enabled()
    page.fill(box, 'd')
    page.press(box, 'Enter')
    # réglage requires_restart : pastille, mention dans la confirmation, bandeau après l'enregistrement
    assert 'Redémarrage nécessaire' in norm(page.locator(f'{row("bench.restart")} .setting-head').inner_text())
    assert 'prochain redémarrage' not in norm(page.locator('#settingsPanel').inner_text())
    page.fill(field('bench.restart'), '9')
    page.click('#settingsSave')
    confirm = norm(page.locator('#settingsConfirm').inner_text())
    assert 'prochain redémarrage' in confirm and 'a, c, d' in confirm, confirm
    page.click('#settingsConfirmYes')
    page.wait_for_timeout(1500)
    assert len(writes) == 1
    sent = json.loads(writes[0]['body'])['settings']
    # l'enregistrement envoie la liste
    assert sent == {'bench.tags': ['a', 'c', 'd'], 'bench.restart': 9}, sent
    banner = norm(page.locator('#settingsPanel').inner_text())
    assert 'Valeur enregistrée, prise en compte au prochain redémarrage' in banner
    page.screenshot(path=f'{OUT}/23-reglages-redemarrage.png', full_page=True)
    page.reload()
    page.wait_for_selector('#settingsTabs [role=tab]')
    # tant que restart_pending est vrai
    assert 'prochain redémarrage' in norm(page.locator('#settingsPanel').inner_text())
    assert 'Secret du banc' not in norm(page.locator('body').inner_text())


@scenario("(c8) Réglages : une erreur sur un réglage masqué par la recherche reste visible (L3-1, EF-6.13)")
def settings_hidden_error(page):
    reset_state(page, SETTINGS)
    open_settings(page, SETTINGS, 'carte')
    state = {'settings': {'map.basemaps': [{'name': 'un', 'url': TILE, 'subdomains': 'abc', 'attribution': '', 'max_zoom': 19}],
                          'map.default_basemap': 'un'}}
    assert api(page, 'PATCH', '/api/admin/settings', state)['status'] == 200
    open_settings(page, SETTINGS, 'carte')
    writes = recorder(page)

    def visible_messages():
        """Tout texte d'erreur visible dans la page Réglages (sous un champ ou dans la zone de confirmation)."""
        found = [norm(n.inner_text()) for n in page.locator('#settingsPanel .setting-error:visible').all()]
        found += [norm(n.inner_text()) for n in page.locator('#settingsConfirm:visible').all()]
        return [m for m in found if m]

    # 1. erreur de la PAGE : URL en http://, puis recherche « zoom » qui masque la carte des fonds ; Enregistrer reste actif
    page.fill('#bm-0-url', 'http://a.tiles.exemple.fr/{z}/{x}/{y}.png')
    page.fill('#settingsSearch', 'zoom')
    assert page.locator(row('map.basemaps')).count() == 0, 'le réglage des fonds devrait être masqué par la recherche'
    assert page.locator('#settingsSave').is_enabled(), 'les saisies masquées comptent (EF-6.10)'
    page.click('#settingsSave')
    messages = visible_messages()
    assert messages, "clic sur Enregistrer sans aucun message visible : l'erreur du réglage masqué est invisible"
    assert any('Fonds de carte' in m or 'https://' in m for m in messages), messages     # le réglage en cause est nommé
    assert writes == [], writes
    page.screenshot(path=f'{OUT}/24-reglages-erreur-reglage-masque.png', full_page=True)
    # 2. refus du SERVEUR (400) sur un réglage masqué : hôte SMTP hors liste, recherche qui masque smtp.host
    page.fill('#settingsSearch', '')
    page.click('#settingsCancel')
    page.click(tab('E-mail'))
    page.fill(field('smtp.host'), 'evil.example')
    page.fill('#settingsSearch', 'timeout')
    assert page.locator(row('smtp.host')).count() == 0, 'smtp.host devrait être masqué par la recherche'
    page.click('#settingsSave')
    if page.locator('#settingsConfirmYes').count():
        page.click('#settingsConfirmYes')
        page.wait_for_timeout(1200)
    messages = visible_messages()
    assert writes and writes[-1]['method'] == 'PATCH', writes
    assert messages, "refus 400 du serveur sans aucun message visible : le réglage masqué n'est pas signalé"
    assert any('smtp' in m.lower() or 'serveur' in m.lower() for m in messages), messages


GPX = """<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="banc" xmlns="http://www.topografix.com/GPX/1/1"><trk><name>Trace du banc</name><trkseg>
""" + "\n".join(
    f'<trkpt lat="{50.630 + i * 0.002:.4f}" lon="{3.050 + i * 0.002:.4f}"><ele>{20 + i}</ele>'
    f'<time>2026-06-01T08:{i:02d}:00Z</time></trkpt>'
    for i in range(12)) + "\n</trkseg></trk></gpx>"

# Barre d'actions de la trace (« Exporter » / « Éditer la trace ») : masquée, ou ni sous le panneau du constructeur ni rognée
BAR_PROBE = """() => {
  const panel = document.getElementById('routeBuilderPanel').getBoundingClientRect();
  return ['export', 'editor'].map((tool) => {
    const b = document.querySelector(`[data-tool="${tool}"]`);
    if (!b) return { tool, hidden: true, overlap: false, covered: false, rect: [] };
    let hidden = b.closest('[hidden]') !== null;
    for (let n = b; n; n = n.parentElement) {
      const cs = getComputedStyle(n);
      if (cs.display === 'none' || cs.visibility === 'hidden') hidden = true;
    }
    const r = b.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) hidden = true;
    const apart = r.bottom <= panel.top || r.top >= panel.bottom || r.right <= panel.left || r.left >= panel.right;
    const overlap = !hidden && !apart;
    let covered = false;
    let blocker = '';
    if (!hidden) {
      // la page défile en douceur par défaut : `instant` donne la mesure d'après
      b.scrollIntoView({ block: 'center', behavior: 'instant' });
      const q = b.getBoundingClientRect();
      covered = [[q.left + q.width / 2, q.top + q.height / 2], [q.left + q.width / 2, q.bottom - 2]].some(([x, y]) => {
        const e = document.elementFromPoint(x, y);
        // les bulles passagères (#alertContainer) ne comptent pas
        const hit = !!(e && (e === b || b.contains(e) || e.closest('#alertContainer')));
        if (!hit) {
          blocker = e ? `${e.tagName.toLowerCase()}#${e.id}.${String(e.className).slice(0, 40)}`
            : `hors écran (${Math.round(y)})`;
        }
        return !hit;
      });
    }
    return { tool, hidden, overlap, covered, blocker, rect: [r.left, r.top, r.width, r.height].map(Math.round) };
  });
}"""


# Bulles de notification (#alertContainer) : aucune ne doit recouvrir le champ Distance ni un bouton du panneau du constructeur
BUBBLE_PROBE = """() => {
  const rect = (n) => n.getBoundingClientRect();
  const popups = [...document.querySelectorAll('#alertContainer .alert-popup.show')].filter((n) => rect(n).width > 0);
  const field = document.getElementById('rbDistance');
  field.scrollIntoView({ block: 'center', behavior: 'instant' });
  const targets = [['Distance', field], ...[...document.querySelectorAll('#routeBuilderPanel button')]
    .filter((b) => rect(b).width > 0).map((b) => [b.textContent.trim() || b.id, b])];
  const hits = [];
  popups.forEach((popup) => {
    const p = rect(popup);
    targets.forEach(([name, node]) => {
      const t = rect(node);
      if (!(p.bottom <= t.top || p.top >= t.bottom || p.right <= t.left || p.left >= t.right)) {
        hits.push(`${popup.textContent.trim().slice(0, 40)} recouvre ${name}`);
      }
    });
  });
  return { popups: popups.length, hits };
}"""


def watch_bubbles(view, seen):
    """Relève les bulles visibles et celles qui recouvrent la cible ; `seen` cumule (bulles vues, recouvrements)."""
    result = view.evaluate(BUBBLE_PROBE)
    seen['popups'] = max(seen['popups'], result['popups'])
    seen['hits'] += [hit for hit in result['hits'] if hit not in seen['hits']]


def bar_state(page):
    return {item['tool']: item for item in page.evaluate(BAR_PROBE)}


def bar_is_fine(state):
    """Masquée, ou visible SANS chevaucher le panneau et sans être rognée."""
    return all(item['hidden'] or (not item['overlap'] and not item['covered']) for item in state.values())


def bar_is_present(state):
    return all(not item['hidden'] and not item['covered'] for item in state.values())


def viewer_with_trace_and_builder(page, label, size, bubbles=None):
    """Contexte au format voulu, trace ouverte, constructeur ouvert : renvoie (page, états relevés)."""
    context = page.context.browser.new_context(viewport=size, locale='fr-FR', timezone_id='Europe/Paris')
    context.route('**/*', intercept)
    view = context.new_page()
    try:
        view.goto(BASE + '/')
        view.wait_for_selector('#map.leaflet-container')
        view.set_input_files('#fileInput', files=[{'name': 'trace-du-banc.gpx', 'mimeType': 'application/gpx+xml',
                                                    'buffer': GPX.encode('utf-8')}])
        view.wait_for_selector('[data-tool="export"]', state='visible', timeout=20000)
        states = {'avant': bar_state(view)}
        view.click('#routeBuilderOpen')
        expect(view.locator('#routeBuilderPanel')).to_be_visible()
        states['constructeur ouvert'] = bar_state(view)
        view.screenshot(path=f'{OUT}/25-barre-actions-{label}-constructeur-ouvert.png', full_page=True)
        box = view.locator('#map').bounding_box()
        view.locator('#map').scroll_into_view_if_needed()
        box = view.locator('#map').bounding_box()
        cx, cy = box['x'] + box['width'] / 2, box['y'] + min(box['height'], 500) / 2
        for dx, dy in ((0, 0), (70, 10), (35, -60)):
            view.mouse.click(cx + dx, cy + dy)
        expect(view.locator('#rbCount')).to_have_text(re.compile(r'^3 point'))
        states['points posés'] = bar_state(view)
        view.fill('#rbDistance', '20')
        view.click('#rbFinish')
        for _ in range(300):                                   # génération : bulles relevées jusqu'au résultat (90 s au plus)
            if bubbles is not None:
                watch_bubbles(view, bubbles)
            if view.locator('#rbResult').is_visible():
                break
            view.wait_for_timeout(300)
        view.wait_for_selector('#rbResult', state='visible', timeout=5000)
        if bubbles is not None:
            watch_bubbles(view, bubbles)
        states['résultat'] = bar_state(view)
        view.screenshot(path=f'{OUT}/26-barre-actions-{label}-resultat.png', full_page=True)
        view.click('#rbClose')
        expect(view.locator('#routeBuilderPanel')).to_be_hidden()
        states['après fermeture'] = bar_state(view)
        view.screenshot(path=f'{OUT}/27-barre-actions-{label}-apres-fermeture.png', full_page=True)
        return states
    finally:
        view.context.close()


def check_action_bar(page, label, size, bubbles=None):
    states = viewer_with_trace_and_builder(page, label, size, bubbles)
    assert bar_is_present(states['avant']), f"{label} : la barre devrait être présente avant le constructeur : {states['avant']}"
    problems = [f"{etape} : {states[etape]}" for etape in ('constructeur ouvert', 'points posés', 'résultat')
                if not bar_is_fine(states[etape])]
    assert not problems, f"{label} : la barre Exporter / Éditer la trace gêne le constructeur : " + ' | '.join(problems)
    after = states['après fermeture']
    assert bar_is_present(after), f"{label} : barre absente ou rognée après fermeture : {after}"
    if bubbles is not None:
        assert bubbles['popups'] > 0, "aucune bulle observée : le contrôle serait vide"
        assert not bubbles['hits'], f"{label} : des bulles recouvrent le panneau du constructeur : {bubbles['hits']}"


@scenario("(h1) Constructeur, mobile 340 px : ni la barre Exporter / Éditer ni les bulles ne gênent le panneau")
def action_bar_mobile(page):
    check_action_bar(page, 'mobile', {'width': 340, 'height': 680}, bubbles={'popups': 0, 'hits': []})


@scenario("(h2) Constructeur, bureau 1440 px : la barre Exporter / Éditer la trace ne gêne pas le panneau, revient après")
def action_bar_desktop(page):
    check_action_bar(page, 'bureau', {'width': 1440, 'height': 900})


def dem_gate(action):
    return json.load(urllib.request.urlopen(f'{DEM_SVC}/gate/{action}', timeout=10))


def activity_meta(page, activity_id):
    return api(page, 'GET', f'/api/activities/{activity_id}')['body']['activity']


def open_from_library(page, activity_id):
    page.goto(f'{DEM_APP}/?activity={activity_id}')
    page.wait_for_function("() => document.getElementById('totalDistance').textContent.trim() !== '—'", timeout=30000)
    page.wait_for_timeout(2500)                                           # analyse de base et profil


def figures(page):
    return page.evaluate("""() => {
      const t = (id) => (document.getElementById(id) || {}).textContent || '';
      const strip = document.getElementById('profileStrip');
      return { ascent: t('totalAscent').trim(), meta: t('stripMeta').trim(), cursor: t('cursorAltitude').trim(),
               profileHidden: document.getElementById('profileSection').hidden,
               profilePaths: strip ? strip.querySelectorAll('path').length : 0 };
    }""")


@scenario("(i) Altitude MNT : D+, D−, profil, altitude sous le curseur après correction puis réouverture (retour du 25/11)")
def dem_correction_reaches_the_viewer(page):
    problems = []

    def soft(label, check):
        try:
            check()
        except Exception as exc:  # noqa: BLE001
            problems.append(f'{label} : {type(exc).__name__}: {str(exc)[:220]}')

    dem_gate('close')                                    # la correction de fond échoue et réessaie tant que la porte est fermée
    page.goto(DEM_APP + '/')
    page.wait_for_selector('#map.leaflet-container')
    page.click('#routeBuilderOpen')
    page.wait_for_timeout(2000)
    box = page.locator('#map').bounding_box()
    cx, cy = box['x'] + box['width'] / 2, box['y'] + min(box['height'], 500) / 2
    for dx, dy in ((0, 0), (70, 10), (35, -60)):
        page.mouse.click(cx + dx, cy + dy)
    page.wait_for_function("() => /^3 point/.test(document.getElementById('rbCount').textContent)", timeout=10000)
    page.fill('#rbDistance', '20')
    page.click('#rbFinish')
    page.wait_for_selector('#rbResult', state='visible', timeout=90000)
    page.click('#rbSave')
    page.wait_for_selector('#rbSaved a', timeout=30000)
    activity_id = int(re.search(r'activity=(\d+)', page.locator('#rbSaved a').first.get_attribute('href')).group(1))
    assert activity_meta(page, activity_id)['elev_source'] == 'gps', 'la correction ne devrait pas être faite porte fermée'
    # avant la correction : l'utilisateur lance l'analyse avancée, qui s'enregistre (sans altitude)
    open_from_library(page, activity_id)

    def waiting_message():
        text = norm(page.locator('#fileMeta').inner_text())
        assert 'altitude en cours de calcul' in text, f"message d'attente absent avant la correction : « {text} »"
    soft('indication « altitude en cours de calcul » avant la correction', waiting_message)
    page.click('[data-tool="advanced"]')
    page.click('#runAdvancedBtn')
    page.wait_for_function("() => /enregistrée/.test(document.getElementById('advancedStatus').textContent)", timeout=60000)
    page.screenshot(path=f'{OUT}/28-altitude-avant-correction.png', full_page=True)
    # la porte s'ouvre : la tâche de fond corrige (30 s au plus par cycle)
    dem_gate('open')
    deadline = time.time() + 150
    while time.time() < deadline and activity_meta(page, activity_id)['dem_status'] != 'ok':
        time.sleep(3)
    meta = activity_meta(page, activity_id)
    assert meta['dem_status'] == 'ok' and meta['elev_source'] == 'dem' and meta['ascent_m'] > 0, meta
    # réouverture : le panneau montre D+, D−, profil et altitude sous le curseur
    open_from_library(page, activity_id)
    shown = figures(page)
    gone = norm(page.locator('#fileMeta').inner_text())
    soft("plus d'indication d'attente après la correction", lambda: 'en cours de calcul' not in gone or problems.append(
        f"l'indication d'attente reste affichée après la correction : « {gone} »"))
    page.screenshot(path=f'{OUT}/29-altitude-apres-correction-reouverture.png', full_page=True)

    def panel_ascent():
        assert re.search(r'\d', shown['ascent']), f"Dénivelé + : « {shown['ascent']} »"

    def strip_meta():
        text = shown['meta']
        assert re.search(r'Dénivelé − \d', text) and re.search(r'altitude de \d', text), f"stripMeta : « {text} »"

    soft('Dénivelé + du panneau', panel_ascent)
    soft("Dénivelé − et plage d'altitude sous le profil", strip_meta)

    def profile():
        assert not shown['profileHidden'] and shown['profilePaths'] > 0, shown
    soft('profil sous la carte', profile)

    def cursor():
        strip = page.locator('#profileStrip').bounding_box()
        assert strip, 'pas de zone de profil'
        page.mouse.move(strip['x'] + strip['width'] / 2, strip['y'] + strip['height'] / 2)
        page.wait_for_timeout(500)
        text = page.evaluate("() => document.getElementById('cursorAltitude').textContent.trim()")
        assert re.search(r'\d', text), f"altitude sous le curseur : « {text} »"
    soft('altitude sous le curseur', cursor)
    # analyse avancée enregistrée AVANT la correction : à jour (ou absente puis relancée), jamais l'instantané sans altitude
    saved = api(page, 'GET', f'/api/activities/{activity_id}/analysis')['body']

    def saved_is_fresh():
        if saved['analysis'] is not None:
            summary = saved['analysis']['summary']
            assert summary['has_elevation'] is True and summary['ascent_m'] > 0, summary
    soft('analyse enregistrée à jour', saved_is_fresh)
    page.click('[data-tool="advanced"]')
    page.wait_for_timeout(1500)
    if page.locator('#advancedSummary').inner_text().strip() == '' or 'Lancer' in page.locator('#runAdvancedLabel').inner_text():
        page.click('#runAdvancedBtn')
        page.wait_for_function("() => /enregistrée/.test(document.getElementById('advancedStatus').textContent)", timeout=60000)
    summary_text = norm(page.locator('#advancedSummary').inner_text())

    def advanced_has_ascent():
        assert re.search(r'\+\d', summary_text), f"résumé : « {summary_text[:160]} »"

    soft('analyse avancée affichée : D+ présent', advanced_has_ascent)
    page.screenshot(path=f'{OUT}/30-altitude-analyse-avancee.png', full_page=True)
    assert not problems, ' | '.join(problems)


def sso_profile(page, base, account):
    """Ouvre /profile d'un compte SSO de test (cookie signé par l'application, posé sans passer par l'IdP), contexte à part."""
    with open(f"/sess/{'oidc' if base == OIDC else 'nopw'}.json", encoding='utf-8') as handle:
        cookie = json.load(handle)[account]
    context = page.context.browser.new_context(viewport={'width': 1280, 'height': 900}, locale='fr-FR')
    context.route('**/*', intercept)
    context.add_cookies([{'name': 'session', 'value': cookie, 'url': base}])
    view = context.new_page()
    view.goto(base + '/profile')
    view.wait_for_selector('h1')
    return view


@scenario("(j) Profil d'un compte SSO : section Invitations (formulaire, création, crédit décrémenté), sans crédit, indisponible")
def sso_invitations(page):
    problems = []

    def soft(label, check):
        try:
            check()
        except Exception as exc:  # noqa: BLE001
            problems.append(f'{label} : {type(exc).__name__}: {str(exc)[:200]}')

    # 1. compte SSO non admin avec 1 crédit : section, formulaire, invitation créée, crédit décrémenté
    view = sso_profile(page, OIDC, 'sso-credit')
    try:
        def with_credit():
            expect(view.locator('#p-inv')).to_be_visible()
            expect(view.locator('#invCredits')).to_have_text('Crédits restants : 1', timeout=10000)
            expect(view.locator('#invForm')).to_be_visible()
            assert view.locator('#passwordForm').count() == 0, 'bloc mot de passe visible pour un compte SSO'
            view.screenshot(path=f'{OUT}/31-profil-sso-avec-credit.png', full_page=True)
            view.fill('#invEmail', 'ami@example.org')
            view.click('#invForm button[type=submit]')
            expect(view.locator('#invOneShot')).to_be_visible(timeout=10000)
            assert OIDC in view.locator('#invOneShotValue').inner_text()
            expect(view.locator('#invCredits')).to_have_text('Crédits restants : 0', timeout=10000)
            expect(view.locator('#invForm')).to_be_hidden()
            assert 'ami@example.org' in view.locator('#invList').inner_text()
            view.screenshot(path=f'{OUT}/32-profil-sso-invitation-creee.png', full_page=True)
        soft('SSO avec 1 crédit', with_credit)
    finally:
        view.context.close()
    # 2. compte SSO sans crédit : section présente, formulaire absent, texte des crédits
    view = sso_profile(page, OIDC, 'sso-nocredit')
    try:
        def without_credit():
            expect(view.locator('#p-inv')).to_be_visible()
            expect(view.locator('#invCredits')).to_have_text('Crédits restants : 0', timeout=10000)
            expect(view.locator('#invForm')).to_be_hidden()
            view.screenshot(path=f'{OUT}/33-profil-sso-sans-credit.png', full_page=True)
        soft('SSO sans crédit', without_credit)
    finally:
        view.context.close()
    # 3. invitations indisponibles (mode oidc sans SECRETS_KEY) : section absente
    view = sso_profile(page, NOPW, 'sso-credit')
    try:
        def unavailable():
            assert view.locator('#p-inv').count() == 0, 'section présente sans invitations'
            assert view.locator('#invForm').count() == 0, 'formulaire présent sans invitations'
            view.screenshot(path=f'{OUT}/34-profil-sso-invitations-indisponibles.png', full_page=True)
        soft('invitations indisponibles', unavailable)
    finally:
        view.context.close()
    assert not problems, ' | '.join(problems)


@scenario("(f) /admin Réglages : confirmation DANS LA PAGE avant d'effacer un secret lié (jamais confirm())")
def confirm_secret_erase(page):
    dialogs = []
    page.on('dialog', lambda d: (dialogs.append(d.message), d.dismiss()))
    page.goto(BASE + '/admin#reglages/e-mail')
    page.wait_for_selector(field('smtp.host'), timeout=15000)
    smtp = {'smtp.host': '127.0.0.1', 'smtp.port': 2525, 'smtp.security': 'none', 'smtp.sender': 'noreply@example.org'}
    assert api(page, 'PATCH', '/api/admin/settings', {'settings': smtp})['status'] == 200
    assert api(page, 'PUT', '/api/admin/secrets/smtp.password', {'value': 'mot-de-passe-du-banc'})['status'] == 200
    page.reload()
    page.wait_for_selector(field('smtp.host'), timeout=15000)
    writes = recorder(page)
    page.fill(field('smtp.port'), '2526')
    page.click('#settingsSave')
    expect(page.locator('#settingsConfirm')).to_be_visible()                      # la confirmation est dans la page
    confirm_text = norm(page.locator('#settingsConfirm').inner_text())
    assert re.search(r'secret.*effacé', confirm_text), confirm_text
    assert writes == [] and dialogs == []                                         # rien n'est parti, aucune boîte du navigateur
    page.screenshot(path=f'{OUT}/08-admin-confirmation-effacement-secret.png', full_page=True)
    page.click('#settingsConfirmNo')                                              # annuler : toujours aucune requête
    expect(page.locator('#settingsConfirm')).to_be_hidden()
    assert writes == []
    still = [s for s in api(page, 'GET', '/api/admin/secrets')['body']['secrets'] if s['name'] == 'smtp.password'][0]
    assert still['configured'] is True                                            # le secret est intact
    page.click('#settingsSave')
    expect(page.locator('#settingsConfirm')).to_be_visible()
    page.click('#settingsConfirmYes')                                             # confirmer : PATCH et secret effacé
    page.wait_for_timeout(1500)
    assert len(writes) == 1 and dialogs == []
    cleared = [s for s in api(page, 'GET', '/api/admin/secrets')['body']['secrets'] if s['name'] == 'smtp.password'][0]
    assert cleared['configured'] is False, cleared


@scenario("(g) aucune sortie réseau : une requête vers un hôte externe est interceptée, enregistrée et bloquée (EF-6.17, CA-80)")
def external_request_is_blocked(page):
    mark = len(intercepted)
    try:
        # la CSP bloque déjà les appels de script : on navigue
        page.goto('https://exemple-externe.invalid/sonde', timeout=10000)
        reached = True
    except Exception:  # noqa: BLE001
        reached = False
    assert not reached, "la navigation vers un hôte externe n'aurait pas dû aboutir"
    blocked = [r for r in intercepted[mark:] if r['host'] == 'exemple-externe.invalid']
    assert blocked and blocked[0]['action'] == 'bloquée', intercepted[mark:]


def main():
    os.makedirs(OUT, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(args=['--disable-dev-shm-usage'])
        context = browser.new_context(viewport={'width': 1440, 'height': 900}, locale='fr-FR', timezone_id='Europe/Paris')
        # aucune sortie réseau : les requêtes externes sont enregistrées puis bloquées
        context.route('**/*', intercept)
        errors = []
        runs = [builder, admin_diagnostic, admin_audit, oidc_login, smtp_banner, confirm_secret_erase,
                external_request_is_blocked, action_bar_mobile, action_bar_desktop]
        if NOPW:
            runs += [sso_invitations]
        if DEM_APP and DEM_SVC:
            runs += [dem_correction_reaches_the_viewer]
        if SETTINGS and REGISTRY:
            runs += [settings_tabs, settings_search, settings_origin, settings_basemaps, settings_rectangle, settings_save,
                     settings_registry, settings_hidden_error]
        only = os.environ.get('BENCH_ONLY')                      # sous-chaîne du nom de la fonction : ne rejoue que ces scénarios
        if only:
            runs = [r for r in runs if only in r.__name__]
        for run in runs:
            page = context.new_page()                            # une page par scénario : un plantage ne contamine pas les autres
            page.on('console', lambda m: errors.append(m.text) if m.type == 'error' else None)
            page.on('pageerror', lambda e: errors.append(str(e)))
            run(page)
            page.close()
        browser.close()
    benign = [e for e in errors if 'tile' in e.lower() or 'ERR_' in e or 'net::' in e or '404' in e
              or 'status of 400' in e or 'status of 409' in e]      # refus d'API VOULUS par les scénarios (400, 409)
    real = [e for e in errors if e not in benign]
    record("aucune erreur JavaScript dans la console", not real, '; '.join(real)[:300])
    with open(f'{OUT}/resultat.json', 'w', encoding='utf-8') as handle:
        json.dump({'when': time.strftime('%Y-%m-%dT%H:%M:%S'), 'results': results, 'console_ignored': benign,
                   'intercepted_requests': intercepted}, handle,
                  ensure_ascii=False, indent=2)
    uid, gid = os.environ.get('BENCH_UID'), os.environ.get('BENCH_GID')
    if uid and gid:                                          # les fichiers appartiennent à l'utilisateur qui a lancé run.sh
        for name in os.listdir(OUT):
            os.chown(os.path.join(OUT, name), int(uid), int(gid))
    return 0 if all(r['ok'] for r in results) else 1


if __name__ == '__main__':
    sys.exit(main())
