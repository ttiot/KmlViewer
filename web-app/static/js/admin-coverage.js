// Panneau « Couverture en service » de l'onglet « Traces planifiées » (routing-zone-donnees ZD-12, EF-6.22, CA-88).
// DOM + textContent uniquement ; aucune boîte du navigateur : les confirmations se font dans la page.
// Le panneau n'existe que si ROUTING_ZONE_BUILD=1 : le serveur répond 404 sinon et le front ne montre rien.
// Rendu : le squelette est créé une fois ; seules les parties dont les données ont changé sont refaites (le focus clavier et la
// région de statut restent stables malgré le rafraîchissement de 30 s).
const AdminCoverage = (() => {
    const { api, el, notify } = App;
    const URL_BASE = '/api/admin/routing/coverage';
    const POLL_MS = 30000;
    const C = { enabled: null, data: null, confirm: null, busy: false, timer: null, map: null, focus: null, sig: {} };

    // Motifs émis par l'application (CHECK de la table d'état) et codes d'échec (FAILURE_CODES) : libellés en clair.
    const REASONS = { zone: 'zone modifiée', mensuel: 'mise à jour mensuelle', activation: 'activation' };
    const FAILURES = {
        catalog: 'catalogue des données indisponible', cap: 'zone au-delà des plafonds', budget: 'budget du jour atteint',
        download: 'téléchargement impossible', merge: 'fusion des extraits en échec', extract: 'recadrage des extraits en échec',
        import: 'import du graphe en échec', incomplete: 'graphe produit incomplet', dem: "téléchargement des dalles d'altitude en échec",
        disk: 'espace disque insuffisant', timeout: 'étape trop longue', health: 'moteur de routage non sain après la bascule',
        other: 'erreur inattendue',
    };

    const title = el('h3', { class: 'h6 mb-2', id: 'coverageTitle' }, 'Couverture en service');
    const statusNode = el('p', { class: 'coverage-status', role: 'status', tabindex: '-1' });
    const actionsNode = el('div', { class: 'mb-2' });
    const errorNode = el('p', { class: 'setting-error', role: 'alert', hidden: true });
    const mapNode = el('div', { class: 'coverage-map', role: 'img', 'aria-label': 'Zone demandée (bleu) et zone en service (gris pointillé)' });
    const hintNode = el('p', { class: 'hint', hidden: true }, 'Zone en service : sources historiques (aucune zone enregistrée).');
    const factsNode = el('dl', { class: 'coverage-facts' });
    const box = el('section', { id: 'coveragePanel', class: 'coverage-panel', role: 'region', 'aria-labelledby': 'coverageTitle', hidden: true },
        title, statusNode, actionsNode, errorNode, mapNode, hintNode, factsNode);

    function parse(text) {
        if (!text) return null;
        const date = new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(text) ? text : `${text.replace(' ', 'T')}Z`);
        return Number.isNaN(date.getTime()) ? null : date;
    }
    const minutesSince = (text) => { const d = parse(text); return d ? Math.max(0, Math.floor((Date.now() - d.getTime()) / 60000)) : null; };
    const gio = (bytes) => (bytes === null || bytes === undefined ? '—' : `${(bytes / 2 ** 30).toLocaleString('fr-FR', { maximumFractionDigits: 1 })} Gio`);
    const plural = (n, word) => `${n} ${word}${n > 1 ? 's' : ''}`;

    function statusLine(d) {
        const reason = REASONS[d.reason] || d.reason || '';
        const monthly = d.reason === 'mensuel';
        switch (d.status) {
            case 'idle': return { text: 'À jour.', tone: 'ok' };
            case 'pending': {
                const waited = minutesSince(d.requested_at);
                const left = waited === null ? null : Math.max(0, Math.ceil(d.settle_s / 60) - waited);
                return { text: `En attente${reason ? ` (${reason})` : ''} — ${left ? `prise dans ${plural(left, 'min')}` : "prise au prochain passage de l'hôte"}.`, tone: 'info' };
            }
            case 'building': {
                const since = minutesSince(d.claimed_at);
                const base = monthly ? 'Mise à jour mensuelle en cours' : 'Reconstruction en cours';
                return { text: `${base}${since === null ? '' : ` depuis ${plural(since, 'min')}`}.`, tone: 'info' };
            }
            case 'starting': return { text: 'Redémarrage du service de routage.', tone: 'info' };
            case 'failed': {
                const label = FAILURES[d.failure_code] || 'construction en échec';
                const code = d.failure_code ? ` (code ${d.failure_code})` : '';
                return { text: `Échec : ${label}${code} — la couverture actuelle reste en service.`, tone: 'bad' };
            }
            default: return { text: d.status, tone: 'info' };
        }
    }

    // -- mini-carte : zone demandée en bleu, zone active en gris dessous (refaite seulement si une zone change) ---------------
    function drawMap(d) {
        if (C.map) { C.map.remove(); C.map = null; }
        if (typeof L === 'undefined' || !mapNode.isConnected) return;
        const shapes = [];
        const map = L.map(mapNode, { zoomControl: false, attributionControl: false, dragging: false, scrollWheelZoom: false, doubleClickZoom: false,
            boxZoom: false, keyboard: false, touchZoom: false });
        const add = (vertices, style) => {
            if (!Array.isArray(vertices) || vertices.length < 3) return;
            shapes.push(L.polygon(vertices.map(([lon, lat]) => [lat, lon]), { weight: 2, fill: false, interactive: false, ...style }).addTo(map));
        };
        add(d.active_zone, { color: '#6c757d', dashArray: '4 4' });
        add(d.requested_zone, { color: '#0d6efd' });
        if (!shapes.length) { map.remove(); return; }
        map.fitBounds(L.featureGroup(shapes).getBounds(), { padding: [8, 8] });
        C.map = map;
    }

    function usageLine(d) {
        const u = d.usage || {};
        const parts = [`${gio(u.bytes)} / ${gio(u.max_source_bytes)}`, `${u.regions} / ${u.max_regions} régions`];
        if (u.max_tiles && u.tiles !== null && u.tiles !== undefined) parts.push(`${u.tiles} / ${u.max_tiles} dalles`);
        return parts.join(' · ');
    }

    // -- actions, avec confirmation dans la page ---------------------------------------------------------------------------
    async function run(action, message) {
        C.busy = true;
        render();
        try {
            await api(`${URL_BASE}/${action}`, { method: 'POST', body: {} });
            C.confirm = null;
            notify(message, 'info');
        } catch (failure) {
            C.confirm = { action: null, error: failure.message };
        }
        C.busy = false;
        C.focus = 'status';                                   // après l'action, le focus revient au statut (annoncé par le lecteur d'écran)
        await refresh();
    }
    const ACTIONS = {
        retry: { label: 'Réessayer', question: 'Relancer la construction avec la zone demandée ?', done: 'Reconstruction relancée.' },
        revert: { label: 'Revenir à la zone active', question: 'Remplacer la zone demandée par la zone en service ?', done: 'Zone active rétablie.' },
        release: { label: 'Tâche bloquée ?', question: "Abandonner la tâche en cours ? L'hôte la reprendra au prochain passage.", done: 'Tâche libérée.' },
    };
    function actionButton(name) {
        const spec = ACTIONS[name];
        if (C.confirm && C.confirm.action === name) {
            return el('span', { class: 'coverage-confirm', role: 'group', 'aria-label': spec.label }, spec.question, ' ',
                el('button', { type: 'button', class: 'btn btn-sm btn-danger me-1', 'data-coverage': 'confirm', disabled: C.busy, onclick: () => run(name, spec.done) }, 'Confirmer'),
                el('button', { type: 'button', class: 'btn btn-sm btn-secondary', 'data-coverage': 'cancel',
                    onclick: () => { C.confirm = null; C.focus = name; render(); } }, 'Annuler'));
        }
        return el('button', { type: 'button', class: 'btn btn-sm btn-outline-secondary me-1', 'data-coverage': name, disabled: C.busy,
            onclick: () => { C.confirm = { action: name }; C.focus = 'confirm'; render(); } }, spec.label);
    }

    // -- rendu par parties ---------------------------------------------------------------------------------------------------
    function part(name, signature, build) {                  // ne refait une partie que si sa signature a changé
        if (C.sig[name] === signature) return;
        C.sig[name] = signature;
        build();
    }

    function render() {
        const d = C.data;
        if (!C.enabled || !d) { box.hidden = true; return; }
        box.hidden = false;
        const status = statusLine(d);
        if (statusNode.textContent !== status.text) statusNode.textContent = status.text;      // région de statut stable : le texte seul change
        statusNode.className = `coverage-status is-${status.tone}`;
        const names = [];
        if (d.status === 'failed') { names.push('retry'); if (!d.historic) names.push('revert'); }
        if (d.stuck) names.push('release');
        part('actions', JSON.stringify([names, C.confirm, C.busy]), () => actionsNode.replaceChildren(...names.map(actionButton)));
        actionsNode.hidden = !names.length;
        const error = C.confirm && C.confirm.error ? C.confirm.error : '';
        if (errorNode.textContent !== error) errorNode.textContent = error;
        errorNode.hidden = !error;
        hintNode.hidden = !d.historic;
        part('facts', JSON.stringify([d.active_regions, d.data_date, d.dem_tiles, d.usage, d.budget]), () => {
            factsNode.replaceChildren(
                el('dt', {}, 'Régions'), el('dd', {}, d.active_regions && d.active_regions.length ? d.active_regions.join(', ') : '—'),
                el('dt', {}, 'Date des données'), el('dd', {}, d.data_date || '—'),
                el('dt', {}, "Dalles d'altitude"), el('dd', {}, String(d.dem_tiles)),
                el('dt', {}, 'Plafonds'), el('dd', {}, usageLine(d)),
                el('dt', {}, 'Budget du jour'), el('dd', {}, d.budget ? `${plural(d.budget.builds || 0, 'construction')} · ${gio(d.budget.bytes || 0)}` : '—'));
        });
        part('map', JSON.stringify([d.active_zone, d.requested_zone]), () => drawMap(d));
        applyFocus();
    }
    function applyFocus() {                                   // le focus suit l'action : confirmation, bouton d'origine, ou statut
        const wanted = C.focus;
        C.focus = null;
        if (!wanted) return;
        const target = wanted === 'status' ? statusNode : actionsNode.querySelector(`[data-coverage="${wanted}"]`);
        if (target) target.focus();
    }
    function reflow() {                                       // panneau remis dans la page (changement d'onglet) : la carte reprend sa taille
        if (C.map) C.map.invalidateSize();
        else if (C.data) { C.sig.map = null; render(); }
    }

    // -- chargement --------------------------------------------------------------------------------------------------------
    async function refresh() {
        try {
            const data = await api(URL_BASE);
            C.enabled = true;
            C.data = data.coverage;
        } catch (error) {
            if (error.status === 404) { C.enabled = false; C.data = null; } else if (C.enabled === null) C.enabled = false;
        }
        render();
    }
    function start() {
        stop();
        C.timer = window.setInterval(() => { if (!document.hidden && C.enabled && !C.busy) refresh(); }, POLL_MS);
    }
    function stop() { if (C.timer) { window.clearInterval(C.timer); C.timer = null; } }

    // Aperçu ZD-4.4 pour la confirmation d'enregistrement de la zone (400 serveur ⇒ erreur à afficher).
    async function preview(polygon) {
        if (!C.enabled) return null;
        const data = await api(`${URL_BASE}/preview`, { method: 'POST', body: { polygon } });
        const p = data.preview;
        const parts = [];
        if (p.added && p.added.length) parts.push(`régions ajoutées : ${p.added.join(', ')}`);
        if (p.removed && p.removed.length) parts.push(`retirées : ${p.removed.join(', ')}`);
        if (!parts.length) parts.push('mêmes régions');
        parts.push(`${gio(p.download_bytes)} à télécharger (${gio(p.total_bytes)} au total)`);
        parts.push(`${plural(p.new_tiles || 0, 'dalle')} d'altitude nouvelle${(p.new_tiles || 0) > 1 ? 's' : ''}`);
        if (p.settle_s) parts.push(`prise en compte dans ${plural(Math.ceil(p.settle_s / 60), 'min')}`);
        return `Aperçu : ${parts.join(' · ')}.`;
    }

    return { node: () => box, refresh, reflow, start, stop, preview, enabled: () => C.enabled === true };
})();
