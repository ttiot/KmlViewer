"""
Scénarios du banc navigateur (Playwright, Chromium réel). Lancé par run.sh dans l'image Playwright épinglée.
Chaque scénario vérifie ET photographie ; le code de sortie est non nul au premier écart. Captures dans /out (hors dépôt).
"""

import json
import os
import re
import sys
import time
import urllib.parse

from playwright.sync_api import expect, sync_playwright

BASE = os.environ['BENCH_URL'].rstrip('/')
OIDC = os.environ.get('BENCH_OIDC_URL', '').rstrip('/')
OUT = os.environ.get('BENCH_OUT', '/out')
results = []


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
                record(name, False, f'{type(exc).__name__}: {str(exc)[:400]}')
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


@scenario("(f) /admin Réglages : confirmation DANS LA PAGE avant d'effacer un secret lié (jamais confirm())")
def confirm_secret_erase(page):
    dialogs = []
    page.on('dialog', lambda d: (dialogs.append(d.message), d.dismiss()))
    page.goto(BASE + '/admin#reglages')
    page.wait_for_selector('#settingsBox [data-key="smtp.host"]', timeout=15000)

    def api(method, path, body=None):
        return page.evaluate("""async ([method, path, body]) => {
            const init = { method, headers: { 'Content-Type': 'application/json' } };
            if (body) init.body = JSON.stringify(body);
            const r = await fetch(path, init);
            return { status: r.status, body: await r.json().catch(() => null) };
        }""", [method, path, body])
    assert api('PATCH', '/api/admin/settings', {'settings': {'smtp.host': '127.0.0.1', 'smtp.port': 2525, 'smtp.security': 'none',
                                                              'smtp.sender': 'noreply@example.org'}})['status'] == 200
    assert api('PUT', '/api/admin/secrets/smtp.password', {'value': 'mot-de-passe-du-banc'})['status'] == 200
    page.goto(BASE + '/admin#reglages')
    page.reload()
    page.wait_for_selector('#settingsBox [data-key="smtp.host"]', timeout=15000)
    patches = []
    page.on('request', lambda r: patches.append(r.url) if r.method == 'PATCH' and '/api/admin/settings' in r.url else None)
    page.fill('#settingsBox [data-key="smtp.port"]', '2526')
    page.click('#settingsForm button[type=submit]')
    expect(page.locator('#settingsConfirm')).to_be_visible()                      # la confirmation est dans la page
    assert 'efface le secret' in page.locator('#settingsConfirmText').inner_text()
    assert patches == [] and dialogs == []                                        # rien n'est parti, aucune boîte du navigateur
    page.screenshot(path=f'{OUT}/08-admin-confirmation-effacement-secret.png', full_page=True)
    page.click('#settingsConfirmNo')                                              # annuler : toujours aucune requête
    expect(page.locator('#settingsConfirm')).to_be_hidden()
    assert patches == []
    assert api('GET', '/api/admin/secrets')['body']['secrets'][0] is not None
    still = [s for s in api('GET', '/api/admin/secrets')['body']['secrets'] if s['name'] == 'smtp.password'][0]
    assert still['configured'] is True                                            # le secret est intact
    page.click('#settingsForm button[type=submit]')
    expect(page.locator('#settingsConfirm')).to_be_visible()
    page.click('#settingsConfirmYes')                                             # confirmer : PATCH et secret effacé
    page.wait_for_timeout(1500)
    assert len(patches) == 1 and dialogs == []
    cleared = [s for s in api('GET', '/api/admin/secrets')['body']['secrets'] if s['name'] == 'smtp.password'][0]
    assert cleared['configured'] is False, cleared


def main():
    os.makedirs(OUT, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(args=['--disable-dev-shm-usage'])
        context = browser.new_context(viewport={'width': 1440, 'height': 900}, locale='fr-FR', timezone_id='Europe/Paris')
        errors = []
        for run in (builder, admin_diagnostic, admin_audit, oidc_login, smtp_banner, confirm_secret_erase):
            page = context.new_page()                            # une page par scénario : un plantage ne contamine pas les autres
            page.on('console', lambda m: errors.append(m.text) if m.type == 'error' else None)
            page.on('pageerror', lambda e: errors.append(str(e)))
            run(page)
            page.close()
        browser.close()
    benign = [e for e in errors if 'tile' in e.lower() or 'ERR_' in e or 'net::' in e or '404' in e]
    real = [e for e in errors if e not in benign]
    record("aucune erreur JavaScript dans la console", not real, '; '.join(real)[:300])
    with open(f'{OUT}/resultat.json', 'w', encoding='utf-8') as handle:
        json.dump({'when': time.strftime('%Y-%m-%dT%H:%M:%S'), 'results': results, 'console_ignored': benign}, handle,
                  ensure_ascii=False, indent=2)
    uid, gid = os.environ.get('BENCH_UID'), os.environ.get('BENCH_GID')
    if uid and gid:                                          # les fichiers appartiennent à l'utilisateur qui a lancé run.sh
        for name in os.listdir(OUT):
            os.chown(os.path.join(OUT, name), int(uid), int(gid))
    return 0 if all(r['ok'] for r in results) else 1


if __name__ == '__main__':
    sys.exit(main())
