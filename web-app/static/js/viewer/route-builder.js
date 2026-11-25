// Constructeur de traces (gpx-route-builder TR6) : points sur la carte, activité, distance, « Terminer » -> boucle routée.
// Règles : toute donnée du serveur passe par textContent (App.el) ; marqueurs = L.divIcon dont le contenu n'est que le numéro ;
// aucun style ni script en ligne ; aucune requête hors de l'origine ; les points restent dans ce navigateur (clé locale par compte,
// supprimée à la déconnexion) ; annuler / rétablir en mémoire seulement (100 états).
'use strict';

const RouteBuilder = (() => {
    const { api, el, notify } = App;
    const STORAGE_PREFIX = 'kmlviewer.routeBuilder.';
    const MAX_HISTORY = 100;
    const state = {
        active: false, options: null, userId: null, points: [], markers: [], undo: [], redo: [], layer: null, coverage: null,
        controller: null, result: null, nextSeed: null, lastRequest: null, preview: null, insertBefore: null, dirty: false,
    };
    let root = null;

    const $ = (id) => document.getElementById(id);
    const storageKey = () => `${STORAGE_PREFIX}v1.${state.userId}`;

    // -- stockage local (P-TR6-6) ------------------------------------------------------------------------------------
    function clearOtherAccounts() {
        try {
            Object.keys(window.localStorage).filter((k) => k.startsWith(STORAGE_PREFIX) && k !== storageKey())
                .forEach((k) => window.localStorage.removeItem(k));
        } catch (_) { /* stockage indisponible : le mode fonctionne sans */ }
    }
    function save() {
        try { window.localStorage.setItem(storageKey(), JSON.stringify({ points: state.points, activity: $('rbActivity').value,
            distance: $('rbDistance').value })); } catch (_) { /* ignoré */ }
    }
    function restore() {
        try {
            const saved = JSON.parse(window.localStorage.getItem(storageKey()) || 'null');
            if (!saved || !Array.isArray(saved.points)) return;
            state.points = saved.points.filter((p) => Array.isArray(p) && p.length === 2 && p.every(Number.isFinite)).slice(0, 200);
            if (typeof saved.activity === 'string') $('rbActivity').value = saved.activity;
            if (typeof saved.distance === 'string') $('rbDistance').value = saved.distance;
        } catch (_) { /* contenu illisible : ignoré */ }
    }
    // À l'envoi du formulaire de déconnexion : toutes les clés du mode sont effacées
    function wipeOnLogout() {
        document.querySelectorAll('form[action$="/auth/logout"]').forEach((form) => form.addEventListener('submit', () => {
            try {
                Object.keys(window.localStorage).filter((k) => k.startsWith(STORAGE_PREFIX)).forEach((k) => window.localStorage.removeItem(k));
            } catch (_) { /* ignoré */ }
        }));
    }

    // -- messages dans la page -------------------------------------------------------------------------------------------
    function say(text, kind = 'info') {
        const box = $('rbMessage');
        box.textContent = text;
        box.className = `hint rb-${kind}`;
    }

    // -- points ------------------------------------------------------------------------------------------------------------
    function inCoverage(lat, lon) {
        const box = state.options && state.options.coverage_bbox;
        if (!box) return true;
        const [lonMin, latMin, lonMax, latMax] = box;
        return lat >= latMin && lat <= latMax && lon >= lonMin && lon <= lonMax;
    }

    function pushHistory() {
        state.undo.push(JSON.stringify(state.points));
        if (state.undo.length > MAX_HISTORY) state.undo.shift();
        state.redo = [];
    }

    function numberedIcon(number) {
        return L.divIcon({ className: 'rb-pin', html: el('span', {}, String(number)), iconSize: [26, 26], iconAnchor: [13, 13] });
    }

    // Longueur à vol d'oiseau de la boucle fermée (prévisualisation locale, RB-2.5)
    function birdLength(points) {
        const rad = (d) => (d * Math.PI) / 180;
        let total = 0;
        for (let i = 0; i < points.length; i += 1) {
            const [a, b] = [points[i], points[(i + 1) % points.length]];
            const h = Math.sin(rad(b[0] - a[0]) / 2) ** 2 + Math.cos(rad(a[0])) * Math.cos(rad(b[0])) * Math.sin(rad(b[1] - a[1]) / 2) ** 2;
            total += 2 * 6371000 * Math.asin(Math.sqrt(h));
        }
        return total;
    }

    function redraw() {
        if (state.preview) { Viewer.map.removeLayer(state.preview); state.preview = null; }
        if (state.points.length >= 2) {            // boucle fermée : le dernier point revient au point 1 (RB-2.3)
            state.preview = L.polyline([...state.points, state.points[0]], { color: '#0d6efd', weight: 2, dashArray: '6 6',
                interactive: false }).addTo(Viewer.map);
        }
        state.markers.forEach((m) => Viewer.map.removeLayer(m));
        state.markers = state.points.map(([lat, lon], index) => {
            const marker = L.marker([lat, lon], { icon: numberedIcon(index + 1), draggable: true, keyboard: true,
                title: `Point ${index + 1} (clic droit : retirer)` });
            marker.on('dragend', () => {
                const at = marker.getLatLng();
                if (!inCoverage(at.lat, at.lng)) { say('Hors de la zone couverte.', 'warn'); redraw(); return; }
                pushHistory();
                state.points[index] = [at.lat, at.lng];
                after();
            });
            marker.on('contextmenu', () => removeAt(index));
            marker.on('click', (event) => {                       // clic / appui : retirer ou insérer avant (RB-2.2, tactile compris)
                L.DomEvent.stopPropagation(event);
                const menu = el('div', { class: 'rb-menu' },
                    el('button', { type: 'button', class: 'btn btn-sm btn-outline-danger me-1', onclick: () => { marker.closePopup(); removeAt(index); } },
                        'Retirer ce point'),
                    el('button', { type: 'button', class: 'btn btn-sm btn-outline-secondary', onclick: () => {
                        marker.closePopup();
                        state.insertBefore = index;
                        say(`Clique sur la carte pour insérer un point avant le point ${index + 1}.`, 'info');
                    } }, 'Insérer avant'));
                marker.bindPopup(menu).openPopup();
            });
            return marker.addTo(Viewer.map);
        });
        const n = state.points.length;
        $('rbCount').textContent = `${n} point${n > 1 ? 's' : ''}`
            + (n >= 2 ? ` · à vol d'oiseau ${(birdLength(state.points) / 1000).toFixed(1)} km` : '');
        updateButtons();
    }

    function removeAt(index) { pushHistory(); state.points.splice(index, 1); after(); }

    function after() { state.dirty = state.points.length > 0; redraw(); save(); say(''); }

    function updateButtons() {
        const ok = state.options && state.options.available;
        const n = state.points.length;
        const needsDistance = n === 1;
        $('rbFinish').disabled = !ok || n < 1 || (needsDistance && !$('rbDistance').value) || !!state.controller;
        $('rbUndo').disabled = state.undo.length === 0;
        $('rbRedo').disabled = state.redo.length === 0;
        $('rbClear').disabled = n === 0;
    }

    function addPoint(event) {
        if (!state.active) return;
        const { lat, lng } = event.latlng;
        if (state.points.length >= state.options.max_waypoints) { say(`${state.options.max_waypoints} points au plus.`, 'warn'); return; }
        if (!inCoverage(lat, lng)) { say('Hors de la zone couverte.', 'warn'); return; }
        pushHistory();
        const point = [Number(lat.toFixed(6)), Number(lng.toFixed(6))];
        if (state.insertBefore !== null) { state.points.splice(state.insertBefore, 0, point); state.insertBefore = null; }
        else state.points.push(point);
        after();
    }

    // -- génération -----------------------------------------------------------------------------------------------------------
    async function generate({ seed = null } = {}) {
        if (state.controller) return;
        const body = { points: state.points, activity: $('rbActivity').value };
        const distance = $('rbDistance').value;
        if (distance) body.distance_km = Number(distance);
        if (seed !== null) {                                   // « Autre proposition » : graine suivante ET empreinte de la précédente
            body.seed = seed;
            if (state.result) body.previous_result_id = state.result.meta.result_id;
        }
        state.lastRequest = { ...body };
        state.controller = new AbortController();
        $('rbCancel').hidden = false;
        say('Calcul en cours…', 'info');
        updateButtons();
        try {
            const result = await api('/api/route-builder/route', { method: 'POST', body, signal: state.controller.signal });
            show(result);
        } catch (error) {
            if (error.name !== 'AbortError') say(error.message, 'error');
            else say('Calcul abandonné (le serveur termine dans son budget de temps).', 'warn');
        } finally {
            state.controller = null;
            $('rbCancel').hidden = true;
            updateButtons();
        }
    }

    function show(result) {
        if (state.result && state.result.fileId) closeFile(state.result.fileId);
        const file = addUploadedFile(result.filename, result);
        const meta = result.route_meta;
        state.result = { meta, fileId: file.id };
        state.nextSeed = meta.next_seed;
        $('rbResult').hidden = false;
        const gap = meta.gap_pct === null ? '' : ` · écart ${meta.gap_pct > 0 ? '+' : ''}${meta.gap_pct} %`;
        $('rbSummary').textContent = `${(meta.distance_m / 1000).toFixed(1)} km`
            + (meta.ascent_m ? ` · D+ ${Math.round(meta.ascent_m)} m` : '') + gap;
        $('rbWarnings').replaceChildren(...meta.warnings.map((w) => el('li', {}, w.message)));
        $('rbName').value = result.filename;
        $('rbSaved').textContent = '';
        $('rbSave').disabled = false;
        say('');
    }

    async function saveResult() {
        if (!state.result) return;
        $('rbSave').disabled = true;
        const link = (id, text) => el('span', {}, text, el('a', { href: `/?activity=${id}` }, 'ouvrir dans la bibliothèque'));
        try {
            const response = await fetch('/api/route-builder/save', { method: 'POST', credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ result_id: state.result.meta.result_id, name: $('rbName').value }) });
            const data = await response.json().catch(() => ({}));
            if (response.ok && data.success) { $('rbSaved').replaceChildren(link(data.activity_id, 'Enregistrée : ')); return; }
            if (response.status === 409 && data.activity_id) { $('rbSaved').replaceChildren(link(data.activity_id, 'Déjà enregistrée : ')); return; }
            $('rbSave').disabled = false;
            say(data.error || "L'enregistrement a échoué.", 'error');
        } catch (_) {
            $('rbSave').disabled = false;
            say('Impossible de contacter le serveur.', 'error');
        }
    }

    // -- ouverture / fermeture du mode -----------------------------------------------------------------------------------------
    async function open() {
        try {
            state.options = await api('/api/route-builder/options');
            state.userId = (await api('/api/me')).user.id;
        } catch (error) { notify(error.message, 'danger'); return; }
        clearOtherAccounts();
        state.active = true;
        root.hidden = false;
        $('rbActivity').replaceChildren(...state.options.activities.map((a) => el('option', { value: a.key,
            selected: a.key === state.options.default_activity }, a.label)));
        $('rbDistance').min = state.options.min_km;
        $('rbDistance').max = state.options.max_km;
        $('rbDistance').step = state.options.step_km;
        restore();
        if (state.options.coverage_bbox) {
            const [lonMin, latMin, lonMax, latMax] = state.options.coverage_bbox;
            state.coverage = L.rectangle([[latMin, lonMin], [latMax, lonMax]], { color: '#0d6efd', weight: 1, dashArray: '6 6', fill: false,
                interactive: false }).addTo(Viewer.map);
        }
        if (!state.options.available) {
            say(state.options.state === 'starting' ? 'Service en cours de démarrage.' : 'Service momentanément indisponible.', 'warn');
        }
        redraw();
    }

    function close() {
        state.active = false;
        root.hidden = true;
        state.markers.forEach((m) => Viewer.map.removeLayer(m));
        state.markers = [];
        if (state.coverage) { Viewer.map.removeLayer(state.coverage); state.coverage = null; }
        if (state.preview) { Viewer.map.removeLayer(state.preview); state.preview = null; }
        state.insertBefore = null;
    }

    function reset() {
        pushHistory();
        state.points = [];
        after();
        $('rbResult').hidden = true;
    }

    function setup() {
        root = $('routeBuilderPanel');
        if (!root) return;                                  // fonction indisponible : aucun élément dans la page (EF-7.3)
        wipeOnLogout();
        $('routeBuilderOpen').addEventListener('click', () => (state.active ? close() : open()));
        $('rbClose').addEventListener('click', close);
        Viewer.map.on('click', addPoint);
        $('rbFinish').addEventListener('click', () => generate());
        $('rbAnother').addEventListener('click', () => generate({ seed: state.nextSeed }));
        $('rbEdit').addEventListener('click', () => { $('rbResult').hidden = true; });
        $('rbCancel').addEventListener('click', () => { if (state.controller) state.controller.abort(); });
        $('rbSave').addEventListener('click', saveResult);
        $('rbUndo').addEventListener('click', () => {
            if (!state.undo.length) return;
            state.redo.push(JSON.stringify(state.points));
            state.points = JSON.parse(state.undo.pop());
            after();
        });
        $('rbRedo').addEventListener('click', () => {
            if (!state.redo.length) return;
            state.undo.push(JSON.stringify(state.points));
            state.points = JSON.parse(state.redo.pop());
            after();
        });
        $('rbClear').addEventListener('click', () => {        // confirmation DANS la page, jamais une boîte du navigateur (RB-2.2)
            $('rbClear').hidden = true;
            $('rbClearConfirm').hidden = false;
        });
        $('rbClearYes').addEventListener('click', () => { reset(); $('rbClearConfirm').hidden = true; $('rbClear').hidden = false; });
        $('rbClearNo').addEventListener('click', () => { $('rbClearConfirm').hidden = true; $('rbClear').hidden = false; });
        // Ctrl+Z / Ctrl+Y (ou Ctrl+Maj+Z), hors champs de saisie
        document.addEventListener('keydown', (event) => {
            if (!state.active || !(event.ctrlKey || event.metaKey)) return;
            const tag = (event.target && event.target.tagName) || '';
            if (['INPUT', 'TEXTAREA', 'SELECT'].includes(tag)) return;
            const key = event.key.toLowerCase();
            if (key === 'z' && !event.shiftKey) { event.preventDefault(); $('rbUndo').click(); }
            else if (key === 'y' || (key === 'z' && event.shiftKey)) { event.preventDefault(); $('rbRedo').click(); }
        });
        // Points non générés : avertissement avant de quitter la page (RB-2.6)
        window.addEventListener('beforeunload', (event) => {
            if (state.active && state.dirty && !state.result) { event.preventDefault(); event.returnValue = ''; }
        });
        $('rbDistance').addEventListener('input', () => { save(); updateButtons(); });
        $('rbActivity').addEventListener('change', save);
    }

    return { setup };
})();
