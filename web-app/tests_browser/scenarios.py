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


@scenario("(a) Créer une trace : plus d'encart sur la carte, clic au centre = un point posé")
def builder(page):
    page.goto(BASE + '/')
    page.wait_for_selector('#map.leaflet-container')
    empty = page.locator('#panelEmpty')
    expect(empty).to_be_visible()                                        # sans fichier : les boutons sont dans le panneau
    expect(empty.locator('#emptyChooseButton')).to_be_visible()
    assert page.locator('#emptyState, .viewer-empty').count() == 0, "l'encart central existe encore dans la page"
    box = page.locator('#map').bounding_box()
    cx, cy = box['x'] + box['width'] / 2, box['y'] + box['height'] / 2   # le centre de la carte (l'ancien emplacement du popup)
    page.screenshot(path=f'{OUT}/01-visualiseur-etat-vide.png')
    page.click('#routeBuilderOpen')
    expect(page.locator('#routeBuilderPanel')).to_be_visible()
    on_map = page.evaluate(
        "([x, y]) => { const e = document.elementFromPoint(x, y); return !!e && !!e.closest('#map'); }", [cx, cy])
    assert on_map, "le centre de la carte n'est pas sur la carte (un élément le recouvre)"
    expect(page.locator('#rbCount')).to_have_text(re.compile(r'^0 point'))
    page.mouse.click(cx, cy)
    expect(page.locator('#rbCount')).to_have_text(re.compile(r'^1 point'))      # un point posé, pas un formulaire de point
    assert page.locator('#map .leaflet-marker-icon').count() >= 1
    page.screenshot(path=f'{OUT}/02-constructeur-point-pose.png')
    page.click('#rbClose')
    expect(page.locator('#routeBuilderPanel')).to_be_hidden()
    expect(empty).to_be_visible()                                        # fermeture sans trace : l'état vide du panneau est là
    page.screenshot(path=f'{OUT}/03-constructeur-ferme-etat-vide.png')


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


def tab_names(page, section):
    """Noms accessibles des pastilles de l'onglet (« 1 réglage modifié », « saisies non enregistrées », « alerte »…)."""
    return [n for n in page.locator(tab(section)).evaluate(
        "e => [...e.querySelectorAll('[aria-label]')].map((x) => x.getAttribute('aria-label'))")]


def has_unsaved(page, section):
    return 'saisies non enregistrées' in tab_names(page, section)


def badges(page, key):
    """Pastilles de la ligne d'un réglage (origine, redémarrage…), à côté du libellé."""
    return [norm(b.inner_text()) for b in page.locator(f'{row(key)} .setting-title .badge-soft').all()]


def hint_of(page, key):
    return norm(page.locator(f'{row(key)} .setting-hint').inner_text())


# Contraste calculé à partir des couleurs CALCULÉES (EF-6.18) : texte (opacité des ancêtres comprise) sur le fond effectif
CONTRAST = """(element) => {
    const toRGBA = (css) => {
        const canvas = document.createElement('canvas'); canvas.width = canvas.height = 1;
        const ctx = canvas.getContext('2d', { willReadFrequently: true });
        ctx.clearRect(0, 0, 1, 1); ctx.fillStyle = css; ctx.fillRect(0, 0, 1, 1);
        const d = ctx.getImageData(0, 0, 1, 1).data;
        return [d[0], d[1], d[2], d[3] / 255];
    };
    const over = (top, under) => top.slice(0, 3).map((c, i) => c * top[3] + under[i] * (1 - top[3]));
    let opacity = 1, backdrop = [255, 255, 255], layers = [];
    for (let node = element; node; node = node.parentElement) {
        const style = getComputedStyle(node);
        opacity *= parseFloat(style.opacity);
        const bg = toRGBA(style.backgroundColor);
        if (bg[3] > 0) { layers.push(bg); if (bg[3] >= 1) break; }
    }
    layers.reverse().forEach((layer) => { backdrop = over(layer, backdrop); });
    const fg = toRGBA(getComputedStyle(element).color);
    const text = over([fg[0], fg[1], fg[2], fg[3] * opacity], backdrop);
    const lin = (v) => { const c = v / 255; return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4; };
    const lum = (rgb) => { const [r, g, b] = rgb.map(lin); return 0.2126 * r + 0.7152 * g + 0.0722 * b; };
    const a = lum(text), b = lum(backdrop);
    return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
}"""


def contrast(locator):
    return locator.evaluate(CONTRAST)


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
        pill = page.locator(f'{tab(section)} .tab-count')                  # une seule pastille de nombre : les réglages trouvés
        found[section] = int(pill.inner_text()) if pill.count() else 0
        if found[section]:
            name = pill.get_attribute('aria-label')
            assert re.fullmatch(rf'{found[section]} réglages? trouvés?', name), name
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
        # EF-6.18 : le nombre seul, jamais de total ; Carte : alerte voulue (tiles=a.tiles.exemple.fr : fonds hors liste)
        pattern = re.escape(section) + (' 1' if modified else '') + (' !' if section == 'Carte' else '')
        assert re.fullmatch(pattern, tab_text(page, section)), tab_text(page, section)
        pills = page.locator(f'{tab(section)} .tab-count')
        assert pills.count() == (1 if modified else 0), f'{section} : {pills.count()} pastille(s) de nombre'
        if modified:
            assert pills.first.get_attribute('aria-label') == '1 réglage modifié', pills.first.get_attribute('aria-label')
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


@scenario("(c2) Réglages : recherche, pastilles de résultats, filtre « modifiées », saisie gardée sous un filtre (CA-73)")
def settings_search(page):
    reset_state(page, SETTINGS)
    open_settings(page, SETTINGS)
    writes = recorder(page)
    page.fill('#settingsSearch', 'delai')                                # « Délai » : sans accents ni casse
    found = hits(page)
    assert {s for s, k in found.items() if k > 0} == {'Traces planifiées', 'Altitude', 'E-mail'}, found
    for section in SECTIONS_ORDER:
        ratio = contrast(page.locator(tab(section)))
        state = 'grisé' if found[section] == 0 else 'avec résultats'
        assert ratio >= 4.5, f'{section} : contraste {ratio:.2f}:1 (onglet {state})'
    dimmed = [x for x in SECTIONS_ORDER if found[x] == 0]
    assert all(page.locator(tab(x)).evaluate("e => e.classList.contains('is-dim')") for x in dimmed), dimmed    # grisés
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
    assert has_unsaved(page, 'Carte'), tab_names(page, 'Carte')
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

    def no_variable_name(key):
        named = [b for b in badges(page, key) if re.search(r'[A-Z]{2,}_[A-Z_]+', b)]
        assert not named, named                                                                         # jamais dans une pastille

    assert badges(page, 'DEM_TIMEOUT') == ['Environnement'], badges(page, 'DEM_TIMEOUT')
    no_variable_name('DEM_TIMEOUT')
    assert badges(page, 'DEM_BATCH_SIZE') == [], badges(page, 'DEM_BATCH_SIZE')                          # aucune « Défaut »
    assert not page.locator(f"{row('DEM_TIMEOUT')} button", has_text='Rétablir').count(), 'bouton Rétablir sur une origine env'
    assert not page.locator(f"{row('DEM_BATCH_SIZE')} button", has_text='Rétablir').count(), 'Rétablir sur origine défaut'
    hint = hint_of(page, 'DEM_TIMEOUT')
    assert hint == '1 à 120 s · variable DEM_TIMEOUT', hint
    page.click(tab('Carte'))
    assert badges(page, 'map.zoom') == ['Base'], badges(page, 'map.zoom')
    reset = page.locator(f"{row('map.zoom')} button", has_text='Rétablir')
    assert reset.count() == 1 and norm(reset.inner_text()) == 'Rétablir la valeur par défaut → 6', norm(reset.inner_text())
    toggles = page.locator('.setting-help-toggle')
    assert toggles.count() >= 1, 'aucun bouton « ? » (`.setting-help-toggle`) sur la page'
    assert all(t.get_attribute('aria-expanded') == 'false' for t in toggles.all()), "description non repliée au chargement"
    assert all(not d.is_visible() for d in page.locator('.setting-desc').all()), 'description visible au chargement'
    first = toggles.first
    first.click()                                                        # « ? » : déplie la description du même réglage
    assert first.get_attribute('aria-expanded') == 'true'
    assert first.locator('xpath=ancestor::*[contains(@class,"setting-row")][1]').locator('.setting-desc').is_visible()
    first.click()
    assert first.get_attribute('aria-expanded') == 'false'
    # base qui surcharge une variable d'env : « remplace DEM_TIMEOUT » et « Rétablir la valeur d'environnement → 30 s »
    assert api(page, 'PATCH', '/api/admin/settings', {'settings': {'DEM_TIMEOUT': 45}})['status'] == 200
    open_settings(page, SETTINGS, 'altitude')
    # la préparation par l'API n'est pas une écriture de la page
    writes.clear()
    assert badges(page, 'DEM_TIMEOUT') == ['Base'], badges(page, 'DEM_TIMEOUT')
    overridden = hint_of(page, 'DEM_TIMEOUT')
    assert overridden == '1 à 120 s · variable DEM_TIMEOUT, remplacée par la valeur en base', overridden
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
    assert badges(page, 'DEM_TIMEOUT') == ['Environnement'], badges(page, 'DEM_TIMEOUT')                # l'origine a changé
    assert page.input_value(field('DEM_BATCH_SIZE')) == '200', 'saisie perdue après le rétablissement'
    assert has_unsaved(page, 'Altitude') and dialogs == [], (tab_names(page, 'Altitude'), dialogs)
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
    assert has_unsaved(page, 'Carte') and has_unsaved(page, 'Altitude')
    assert not has_unsaved(page, 'Sécurité')
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
    assert not has_unsaved(page, 'Carte') and has_unsaved(page, 'Altitude')
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


def box_of(locator):
    return locator.evaluate("""e => { const r = e.getBoundingClientRect();
        return { top: r.top, bottom: r.bottom, left: r.left, right: r.right, width: r.width, height: r.height }; }""")


def style_of(locator, *props):
    return locator.evaluate("(e, props) => Object.fromEntries(props.map((p) => [p, getComputedStyle(e)[p]]))", list(props))


def reading_problems(page, theme):
    """Mesures EF-6.18 sur la page Réglages (fenêtre 1280 × 800) dans un thème : liste des écarts à la spec."""
    page.emulate_media(color_scheme=theme)
    reset_state(page, SETTINGS)
    page.set_viewport_size({'width': 1280, 'height': 800})
    open_settings(page, SETTINGS, 'altitude')
    bad = []

    def check(label, ok, got):
        if not ok:
            bad.append(f'[{theme}] {label} : {got}')

    tops = {round(box_of(page.locator(tab(x)))['top']) for x in SECTIONS_ORDER}
    check('8 onglets sur une ligne', len(tops) == 1, sorted(tops))
    pills = {x: page.locator(f'{tab(x)} .tab-count').count() for x in SECTIONS_ORDER}
    with_pill = {x for x, n in pills.items() if n}
    check('pastille de nombre seulement sur Carte et Altitude', with_pill == {'Carte', 'Altitude'}, pills)
    timeout = page.locator(row('DEM_TIMEOUT'))
    hint, label = timeout.locator('.setting-hint'), timeout.locator('.setting-label')
    size = float(style_of(hint, 'fontSize')['fontSize'][:-2])
    check('aide : taille ≥ 13 px', size >= 13, size)
    check('aide : contraste ≥ 7:1', contrast(hint) >= 7, round(contrast(hint), 2))
    look = style_of(label, 'fontWeight', 'fontSize')
    check('libellé : graisse ≥ 600', int(look['fontWeight']) >= 600, look['fontWeight'])
    check('libellé plus grand que l aide', float(look['fontSize'][:-2]) > size, (look['fontSize'], size))
    check('libellé : contraste ≥ 7:1', contrast(label) >= 7, round(contrast(label), 2))
    page.locator('.setting-help-toggle').first.click()
    desc = page.locator('.setting-row:has(.setting-help-toggle[aria-expanded=true]) .setting-desc').first
    desc_size = float(style_of(desc, 'fontSize')['fontSize'][:-2])
    check('description dépliée : taille ≥ 13 px', desc_size >= 13, desc_size)
    check('description dépliée : contraste ≥ 7:1', contrast(desc) >= 7, round(contrast(desc), 2))
    page.locator('.setting-help-toggle[aria-expanded=true]').first.click()
    for selector, label_, floor in (('.tab-count', 'pastille de nombre', 4.5), ('.settings-tab', 'onglet', 4.5),
                                    ('.badge-soft', 'pastille', 4.5)):
        for item in page.locator(selector).all():
            ratio = contrast(item)
            check(f'{label_} : contraste ≥ {floor}:1', ratio >= floor, f'{round(ratio, 2)} ({norm(item.inner_text())})')
    check('aucune pastille « Défaut »', not [t for t in page.locator('.badge-soft').all_inner_texts() if norm(t) == 'Défaut'], '')
    digits = style_of(page.locator(f'{field("DEM_TIMEOUT")}'), 'fontVariantNumeric')['fontVariantNumeric']
    check('chiffres en chasse fixe', 'tabular-nums' in digits, digits)
    # largeur de la boîte de l'éditeur, unité comprise
    for key, section, limit in (('DEM_TIMEOUT', 'altitude', 192), ('smtp.security', 'e-mail', 320), ('smtp.host', 'e-mail', 448),
                                ('DEM_URL', 'altitude', 640)):
        open_settings(page, SETTINGS, section)
        width = box_of(page.locator(f'{row(key)} .setting-control'))['width']
        check(f'{key} ≤ {limit} px', width <= limit + 0.5, round(width, 1))
    # en-tête et éditeur côte à côte
    open_settings(page, SETTINGS, 'altitude')
    head, editor = box_of(timeout.locator('.setting-head')), box_of(timeout.locator('.setting-editor'))
    line = box_of(timeout)
    check('en-tête et éditeur alignés en haut', abs(head['top'] - editor['top']) <= 4, (round(head['top']), round(editor['top'])))
    check('écart ≥ 24 px entre les colonnes', editor['left'] - head['right'] >= 24, round(editor['left'] - head['right']))
    share = head['width'] / line['width']
    check('en-tête = 35 à 45 % de la ligne', 0.35 <= share <= 0.45, round(share, 3))
    check('ligne ≤ 80 px de haut', line['height'] <= 80, round(line['height'], 1))
    title, name = box_of(timeout.locator('.setting-title')), box_of(label)
    for kind, target in (('« ? »', timeout.locator('.setting-help-toggle')), ('pastille', timeout.locator('.badge-soft'))):
        if target.count():
            spot = box_of(target.first)
            middle = (spot['top'] + spot['bottom']) / 2
            on_line = name['top'] - 2 <= middle <= name['bottom'] + 2 and spot['top'] >= title['top'] - 2
            check(f'{kind} sur la ligne du libellé', on_line,
                  (round(middle), round(name['top']), round(name['bottom'])))
    # espacement : 12 px entre deux réglages, séparateur, libellé à moins de 4 px de l'aide
    rows = page.locator('.setting-row').all()
    first, second = box_of(rows[0]), box_of(rows[1])
    space = (second['top'] - first['bottom'] + float(style_of(rows[0], 'paddingBottom')['paddingBottom'][:-2])
             + float(style_of(rows[1], 'paddingTop')['paddingTop'][:-2]))        # espace entre le contenu des deux réglages
    check('≥ 12 px entre deux réglages', space >= 12, round(space, 1))
    edges = style_of(rows[1], 'borderTopWidth', 'borderBottomWidth')
    previous = style_of(rows[0], 'borderBottomWidth')
    separated = float(edges['borderTopWidth'][:-2]) > 0 or float(previous['borderBottomWidth'][:-2]) > 0
    check('séparateur entre deux réglages', separated, edges)
    gap = box_of(timeout.locator('.setting-hint'))['top'] - name['bottom']
    check('libellé à ≤ 4 px de l aide', gap <= 4, round(gap, 1))
    # barre d'outils : recherche et interrupteur sur une ligne ; version dans le pied
    search = box_of(page.locator('#settingsSearch'))
    toggle = box_of(page.locator('#settingsOnlyModified'))
    centres = abs((search['top'] + search['bottom']) / 2 - (toggle['top'] + toggle['bottom']) / 2)
    check('recherche et interrupteur sur une ligne', centres <= 40,
          (round(search['top']), round(toggle['top'])))
    in_toolbar = page.locator('.settings-toolbar #settingsVersion').count()
    check('version absente de la barre d outils', in_toolbar == 0, 'dans la barre')
    check('version dans le pied', page.locator('.settings-footer #settingsVersion').count() == 1 and
          'Version des réglages' in page.locator('#settingsVersion').inner_text(), page.locator('#settingsVersion').inner_text())
    # grisés par une recherche sans résultat : contraste ≥ 4,5:1
    page.fill('#settingsSearch', 'DEM_URL')
    for x in SECTIONS_ORDER:
        if x != 'Altitude':
            ratio = contrast(page.locator(tab(x)))
            check(f'onglet grisé {x} : contraste ≥ 4,5:1', ratio >= 4.5, round(ratio, 2))
    page.screenshot(path=f'{OUT}/39-reglages-lisibilite-{theme}.png', full_page=True)
    return bad


def phone_problems(page):
    """Fenêtre 390 × 844 : barre d'onglets défilable sur une ligne, page sans défilement horizontal, en-tête sur l'éditeur."""
    page.set_viewport_size({'width': 390, 'height': 844})
    reset_state(page, SETTINGS)
    open_settings(page, SETTINGS, 'altitude')
    bad = []
    bar = page.locator('#settingsTabs')
    sizes = bar.evaluate('e => ({ scroll: e.scrollWidth, client: e.clientWidth })')
    if not sizes['scroll'] > sizes['client']:
        bad.append(f'barre non défilable : {sizes}')
    tops = {round(box_of(page.locator(tab(x)))['top']) for x in SECTIONS_ORDER}
    if len(tops) != 1:
        bad.append(f'barre d onglets sur plusieurs lignes : {sorted(tops)}')
    wide = page.evaluate('() => document.documentElement.scrollWidth')
    if wide > 390:
        bad.append(f'défilement horizontal de la page : scrollWidth {wide}')
    page.focus(tab('Altitude'))
    for _ in range(3):                                                   # Altitude → Dossier surveillé → Stockage → Général
        page.keyboard.press('ArrowRight')
    inside = page.evaluate("""() => { const b = document.getElementById('settingsTabs').getBoundingClientRect();
        const t = document.querySelector('#settingsTabs [aria-selected=true]').getBoundingClientRect();
        const chosen = document.querySelector('#settingsTabs [aria-selected=true]');
        return { ok: t.left >= b.left - 1 && t.right <= b.right + 1, section: chosen.dataset.section, tab: [t.left, t.right],
            bar: [b.left, b.right] }; }""")
    if inside['section'] != 'Général' or not inside['ok']:
        bad.append(f'onglet « Général » hors de la partie visible : {inside}')
    row_ = page.locator('.setting-row').first
    head, editor = box_of(row_.locator('.setting-head')), box_of(row_.locator('.setting-editor'))
    if editor['top'] < head['bottom'] - 1:
        bad.append(f'en-tête pas au-dessus de l éditeur : {head} / {editor}')
    page.screenshot(path=f'{OUT}/40-reglages-lisibilite-telephone.png', full_page=True)
    page.set_viewport_size({'width': 1440, 'height': 900})
    return bad


@scenario("(m) Réglages : lisibilité à 1280 px (thèmes clair et sombre) et à 390 px : onglets, contraste, largeurs (CA-82)")
def settings_reading(page):
    problems = []
    for theme in ('light', 'dark'):
        problems += reading_problems(page, theme)
    problems += phone_problems(page)
    page.emulate_media(color_scheme='light')
    assert not problems, ' | '.join(problems)


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


def sso_page(page, account, path, ready):
    """Page de l'instance oidc ouverte avec le cookie signé d'un compte du seed (contexte à part)."""
    with open('/sess/oidc.json', encoding='utf-8') as handle:
        cookie = json.load(handle)[account]
    context = page.context.browser.new_context(viewport={'width': 1280, 'height': 900}, locale='fr-FR')
    context.route('**/*', intercept)
    context.add_cookies([{'name': 'session', 'value': cookie, 'url': OIDC}])
    view = context.new_page()
    view.goto(OIDC + path)
    view.wait_for_selector(ready, timeout=15000)
    return view


@scenario("(l) /admin Invitations : colonne « Émise par », statuts français, compte créé, comptes supprimés, texte seul (CA-84)")
def admin_invitations(page):
    view = sso_page(page, 'sso-admin', '/admin#invitations', '#invitesBody tr')
    try:
        view.wait_for_function("() => document.querySelectorAll('#invitesBody tr').length >= 8", timeout=15000)
        heads = [norm(h.text_content()) for h in view.locator('#tab-invitations thead th').all()]
        assert heads[:3] == ['Adresse', 'Émise par', 'Statut'], heads

        def line(address):
            rows = view.locator('#invitesBody tr', has_text=address)
            assert rows.count() == 1, f'{address} : {rows.count()} ligne(s)'
            cells = rows.first.locator('td')
            return {'issuer': norm(cells.nth(1).inner_text()), 'status': norm(cells.nth(2).inner_text()),
                    'button': rows.first.locator('button').count(), 'row': rows.first}

        addresses = ('x@exemple.fr', 'y@exemple.fr', 'revoquee@exemple.fr', 'expiree@exemple.fr', 'acceptee@exemple.fr',
                     'supprimee@exemple.fr', 'orpheline@exemple.fr', 'balise@exemple.fr', 'dave@exemple.fr')
        found = {a: line(a) for a in addresses}
        view.screenshot(path=f'{OUT}/37-admin-invitations.png', full_page=True)
        failures = []

        def expect_that(label, ok, got):
            if not ok:
                failures.append(f'{label} : {got!r}')

        expect_that('x émise par sso-issuer', found['x@exemple.fr']['issuer'] == 'sso-issuer', found['x@exemple.fr']['issuer'])
        expect_that('y émise par l admin', found['y@exemple.fr']['issuer'] == 'sso-admin', found['y@exemple.fr']['issuer'])
        expect_that('x en attente', found['x@exemple.fr']['status'] == 'En attente', found['x@exemple.fr']['status'])
        expect_that('révoquée', found['revoquee@exemple.fr']['status'] == 'Révoquée', found['revoquee@exemple.fr']['status'])
        expect_that('expirée', found['expiree@exemple.fr']['status'] == 'Expirée', found['expiree@exemple.fr']['status'])
        accepted = found['acceptee@exemple.fr']['status']                      # acceptée PUIS révoquée en base : reste Acceptée
        expect_that('acceptée + compte créé bob', accepted.startswith('Acceptée') and 'compte créé : bob' in accepted, accepted)
        expect_that('compte créé supprimé', 'compte créé : compte supprimé' in found['supprimee@exemple.fr']['status'],
                    found['supprimee@exemple.fr']['status'])
        orphan = found['orpheline@exemple.fr']['issuer']
        expect_that('émetteur supprimé', orphan == 'compte supprimé', orphan)
        expect_that('« Révoquer » sur l en attente seule',
                    [found[a]['button'] for a in found] == [1 if a in ('x@exemple.fr', 'y@exemple.fr', 'orpheline@exemple.fr',
                                                                        'balise@exemple.fr') else 0 for a in found],
                    {a: found[a]['button'] for a in found})
        markup = found['balise@exemple.fr']
        expect_that('identifiant à balises affiché tel quel', markup['issuer'] == '<b>x</b>', markup['issuer'])
        expect_that('aucune balise interprétée', markup['row'].locator('b').count() == 0, markup['row'].inner_html())
        body = norm(view.locator('#tab-invitations').inner_text())
        expect_that('« Utilisée » nulle part', not re.search(r'utilis[ée]e', body, re.I), body[:300])
        assert not failures, ' | '.join(failures)
    finally:
        view.context.close()
    # le profil de l'émetteur : mêmes mots, pas de colonne « Émise par »
    view = sso_page(page, 'sso-issuer', '/profile', '#p-inv')
    try:
        view.wait_for_function("() => (document.querySelector('#invList') || {}).innerText?.includes('@')", timeout=15000)
        listing = norm(view.locator('#invList').inner_text())
        view.screenshot(path=f'{OUT}/38-profil-emetteur-invitations.png', full_page=True)
        assert re.search(r'dave@exemple\.fr.*accept', listing, re.I), listing
        assert not re.search(r'utilis[ée]e', listing, re.I), listing
        assert 'En attente' in listing or 'en attente' in listing, listing
        assert 'Émise par' not in listing and 'compte créé' not in listing, listing
    finally:
        view.context.close()


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


DRAFT_POINTS = """() => Object.keys(window.localStorage).filter((k) => k.startsWith('kmlviewer.routeBuilder.'))
    .map((k) => (JSON.parse(window.localStorage.getItem(k)) || {}).points || []).filter((points) => points.length)"""


def fresh_builder(page):
    """Contexte neuf (stockage vide), constructeur ouvert, trois points posés : renvoie la page."""
    context = page.context.browser.new_context(viewport={'width': 1440, 'height': 900}, locale='fr-FR',
                                               timezone_id='Europe/Paris')
    context.route('**/*', intercept)
    view = context.new_page()
    view.goto(BASE + '/')
    view.wait_for_selector('#map.leaflet-container')
    expect(view.locator('#panelEmpty')).to_be_visible()                  # la page est prête (comme le scénario (a))
    view.click('#routeBuilderOpen')
    expect(view.locator('#routeBuilderPanel')).to_be_visible()
    view.evaluate('() => Viewer.map.setView([50.635, 3.055], 14, { animate: false })')       # dans la couverture du moteur
    box = view.locator('#map').bounding_box()
    cx, cy = box['x'] + box['width'] / 2, box['y'] + box['height'] / 2
    for dx, dy in ((0, 0), (70, 10), (35, -60)):
        view.mouse.click(cx + dx, cy + dy)
    expect(view.locator('#rbCount')).to_have_text(re.compile(r'^3 point'))
    return view


def generate_result(view):
    view.fill('#rbDistance', '20')
    view.click('#rbFinish')
    view.wait_for_selector('#rbResult', state='visible', timeout=90000)


def reopen_builder(view):
    view.reload()
    view.wait_for_selector('#map.leaflet-container')
    expect(view.locator('#panelEmpty')).to_be_visible()
    view.click('#routeBuilderOpen')
    expect(view.locator('#routeBuilderPanel')).to_be_visible()


@scenario("(k) Constructeur : points effacés du stockage local après l'enregistrement (ou « déjà enregistrée »), gardés sinon")
def builder_draft_storage(page):
    # 1. enregistrement réussi : plus aucun point en local, rien à restaurer au rechargement
    view = fresh_builder(page)
    try:
        assert view.evaluate(DRAFT_POINTS), 'les points posés devraient être gardés en local avant l enregistrement'
        generate_result(view)
        assert view.evaluate(DRAFT_POINTS), 'après « Terminer » mais avant l enregistrement : le brouillon reste gardé'
        with view.expect_response(lambda r: '/api/route-builder/save' in r.url) as saved:
            view.click('#rbSave')
        assert saved.value.status == 200, saved.value.status
        view.wait_for_timeout(500)
        left = view.evaluate(DRAFT_POINTS)
        assert left == [], f"points encore en stockage local après l'enregistrement : {left}"
        view.screenshot(path=f'{OUT}/35-constructeur-apres-enregistrement.png', full_page=True)
        reopen_builder(view)
        expect(view.locator('#rbCount')).to_have_text(re.compile(r'^0 point'))      # aucun point restauré
        assert view.evaluate("() => document.querySelectorAll('#map .leaflet-marker-icon').length") == 0
    finally:
        view.context.close()
    # 2. « déjà enregistrée » (409) : même effet. La réponse du serveur est simulée : seul le comportement de la page est vérifié
    view = fresh_builder(page)
    try:
        generate_result(view)
        view.route('**/api/route-builder/save', lambda r: r.fulfill(
            status=409, content_type='application/json',
            body=json.dumps({'success': False, 'error': 'Cette trace est déjà enregistrée.', 'activity_id': 1})))
        view.click('#rbSave')
        expect(view.locator('#rbSaved')).to_contain_text('Déjà enregistrée')
        view.wait_for_timeout(500)
        left = view.evaluate(DRAFT_POINTS)
        assert left == [], f"points encore en stockage local après « déjà enregistrée » : {left}"
        reopen_builder(view)
        expect(view.locator('#rbCount')).to_have_text(re.compile(r'^0 point'))
    finally:
        view.context.close()
    # 3. trace non enregistrée : les points sont toujours restaurés au rechargement (P-TR6-6)
    view = fresh_builder(page)
    try:
        reopen_builder(view)
        expect(view.locator('#rbCount')).to_have_text(re.compile(r'^3 point'))
        assert view.evaluate("() => document.querySelectorAll('#map .leaflet-marker-icon').length") >= 3
        view.screenshot(path=f'{OUT}/36-constructeur-brouillon-restaure.png', full_page=True)
        # génération faite mais pas enregistrée : les points reviennent aussi
        generate_result(view)
        reopen_builder(view)
        expect(view.locator('#rbCount')).to_have_text(re.compile(r'^3 point'))
    finally:
        view.context.close()


def main():
    os.makedirs(OUT, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(args=['--disable-dev-shm-usage'])
        context = browser.new_context(viewport={'width': 1440, 'height': 900}, locale='fr-FR', timezone_id='Europe/Paris')
        # aucune sortie réseau : les requêtes externes sont enregistrées puis bloquées
        context.route('**/*', intercept)
        errors = []
        runs = [builder, admin_diagnostic, admin_audit, oidc_login, smtp_banner, confirm_secret_erase,
                external_request_is_blocked, action_bar_mobile, action_bar_desktop, builder_draft_storage]
        if NOPW:
            runs += [sso_invitations]
        if OIDC:
            runs += [admin_invitations]
        if DEM_APP and DEM_SVC:
            runs += [dem_correction_reaches_the_viewer]
        if SETTINGS and REGISTRY:
            runs += [settings_tabs, settings_search, settings_origin, settings_basemaps, settings_save,
                     settings_registry, settings_hidden_error, settings_reading]
        import rb27                                              # RB-2.7 : CA-R41 à CA-R48 (tests_browser/rb27.py)
        runs += rb27.RUNS
        if SETTINGS:
            import zone_front                                    # polygone : CA-86, CA-R50, CA-R51 (tests_browser/zone_front.py)
            runs += zone_front.RUNS
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
