// Constructeur de traces (gpx-route-builder TR6) : points sur la carte, activité, distance, « Terminer » -> boucle routée.
// RB-2.7 : avec un résultat affiché, déplacer / insérer / ajouter / retirer un point régénère la trace au relâchement ; en édition, les
// mêmes gestes ne génèrent rien. Une seule génération à la fois ; en cas d'échec ou d'annulation, retour exact à l'état précédent.
// Règles : toute donnée du serveur passe par textContent (App.el) ; marqueurs = L.divIcon dont le contenu n'est que le numéro ;
// aucun style ni script en ligne ; aucune requête hors de l'origine ; les points restent dans ce navigateur (clé locale par compte,
// supprimée à TOUTE déconnexion, script commun : common.js) ; annuler / rétablir en mémoire seulement (100 états, 10 résultats).
'use strict';

const RouteBuilder = (() => {
    const { api, el, notify } = App;
    const STORAGE_PREFIX = 'kmlviewer.routeBuilder.';
    const MAX_HISTORY = 100;
    const MAX_RESULTS_KEPT = 10;            // résultats gardés dans l'historique ; au-delà, l'état ne porte que ses points
    const MIN_GAP_M = 5;                    // déplacement négligeable / point trop proche d'un voisin
    const TOLERANCE_MOUSE = 10, TOLERANCE_TOUCH = 16;     // « sur la trace », en pixels d'écran
    const KEY_STEP = 10, KEY_STEP_SHIFT = 50;
    const WAIT = 'Calcul en cours : attends le résultat ou annule.';
    const state = {
        active: false, options: null, userId: null, points: [], markers: [], undo: [], redo: [], layer: null, coverage: null,
        controller: null, result: null, nextSeed: null, lastRequest: null, preview: null, insertBefore: null, dirty: false,
        kbd: null, pendingInsert: null, refocus: null, redrawing: false, suppressClick: 0, gesture: null, hover: null,
    };
    let root = null;

    const $ = (id) => document.getElementById(id);
    const storageKey = () => `${STORAGE_PREFIX}v1.${state.userId}`;
    const resultShown = () => !!state.result && !$('rbResult').hidden;

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
    // -- messages dans la page -------------------------------------------------------------------------------------------
    function say(text, kind = 'info') {
        const box = $('rbMessage');
        box.textContent = text;
        box.className = `hint rb-${kind}`;
        if (!text) $('rbAlert').textContent = '';
    }
    function alertText(text) { $('rbAlert').textContent = text; }       // role="alert" : échec d'un recalcul (RB-2.7)

    // -- géométrie ---------------------------------------------------------------------------------------------------------
    // Zone en vigueur : sommets [lon, lat] du polygone, sinon les 4 angles du rectangle (RB-1.3b) ; même fonction que le serveur
    function coverageVertices() {
        const options = state.options || {};
        if (Array.isArray(options.coverage_polygon) && options.coverage_polygon.length >= 3) return options.coverage_polygon;
        return options.coverage_bbox ? Zone.rectangleVertices(options.coverage_bbox) : null;
    }
    function inCoverage(lat, lon) {
        const vertices = coverageVertices();
        return !vertices || Zone.contains(vertices, lat, lon);
    }
    const rad = (d) => (d * Math.PI) / 180;
    function dist(a, b) {
        const h = Math.sin(rad(b[0] - a[0]) / 2) ** 2 + Math.cos(rad(a[0])) * Math.cos(rad(b[0])) * Math.sin(rad(b[1] - a[1]) / 2) ** 2;
        return 2 * 6371000 * Math.asin(Math.sqrt(h));
    }
    // Longueur à vol d'oiseau de la boucle fermée (prévisualisation locale, RB-2.5)
    function birdLength(points) {
        let total = 0;
        for (let i = 0; i < points.length; i += 1) total += dist(points[i], points[(i + 1) % points.length]);
        return total;
    }
    const round6 = (v) => Number(v.toFixed(6));

    // Trace du résultat affiché : coordonnées + longueur cumulée (pour situer un appui par rapport aux points)
    function traceOf(result) {
        if (!result.trace) {
            const coords = (result.payload.features || []).filter((f) => f.type === 'polyline').flatMap((f) => f.coordinates)
                .filter((c) => Array.isArray(c) && Number.isFinite(c[0]) && Number.isFinite(c[1]));
            const cum = [0];
            for (let i = 1; i < coords.length; i += 1) cum.push(cum[i - 1] + dist(coords[i - 1], coords[i]));
            result.trace = { coords, cum };
        }
        return result.trace;
    }
    // Point de la trace le plus proche d'un point d'écran : { distance px, position le long de la trace, latlng }
    function nearestOnTrace(containerPoint, fromSegment = 0, firstWithin = null) {
        const { coords, cum } = traceOf(state.result);
        let best = null, first = null;
        let previous = Viewer.map.latLngToContainerPoint(coords[fromSegment] || coords[0]);
        const hits = [];
        for (let i = Math.max(fromSegment, 0); i < coords.length - 1; i += 1) {
            const next = Viewer.map.latLngToContainerPoint(coords[i + 1]);
            const dx = next.x - previous.x, dy = next.y - previous.y;
            const len2 = dx * dx + dy * dy;
            const t = len2 === 0 ? 0 : Math.max(0, Math.min(1, ((containerPoint.x - previous.x) * dx + (containerPoint.y - previous.y) * dy) / len2));
            const px = previous.x + t * dx, py = previous.y + t * dy;
            const d = Math.hypot(containerPoint.x - px, containerPoint.y - py);
            const hit = { distance: d, segment: i, position: cum[i] + t * (cum[i + 1] - cum[i]), x: px, y: py };
            if (!best || d < best.distance) best = hit;
            if (firstWithin !== null) hits.push(hit);
            previous = next;
        }
        // firstWithin : premier passage (dans l'ordre de la trace) à firstWithin px du meilleur ; une boucle repasse parfois au même endroit
        if (firstWithin !== null && best) first = hits.find((h) => h.distance <= best.distance + firstWithin) || best;
        const chosen = first || best;
        if (!chosen) return null;
        return { distance: chosen.distance, segment: chosen.segment, position: chosen.position,
            latlng: Viewer.map.containerPointToLatLng([chosen.x, chosen.y]) };
    }
    // Position de chaque point P1…Pn le long de la trace : P1 au début, puis croissante : chaque point est cherché en avant du
    // précédent (RB-4.7) ; la fermeture de la boucle sur P1 est la fin de la trace, jamais la position de P1
    function pointPositions() {
        const positions = [];
        let from = 0, last = 0;
        state.points.forEach((point, index) => {
            if (index === 0) { positions.push(0); return; }
            const hit = nearestOnTrace(Viewer.map.latLngToContainerPoint(point), from, 3);
            last = hit ? Math.max(last, hit.position) : last;
            positions.push(last);
            if (hit) from = hit.segment;
        });
        return positions;
    }
    // Rang d'insertion pour un appui à la position x : entre Pi et Pi+1, sinon après Pn (fermeture)
    function insertionIndex(position) {
        const positions = pointPositions();
        let index = state.points.length;
        for (let i = 0; i < positions.length - 1; i += 1) if (positions[i] <= position && position < positions[i + 1]) { index = i + 1; break; }
        return index;
    }

    // -- historique : états { points, résultat } ----------------------------------------------------------------------------
    function snapshot() {
        const shown = resultShown();
        return { points: state.points.map((p) => [...p]), payload: shown ? state.result.payload : null,
            params: shown ? { ...state.result.params } : null };
    }
    function trimHistory() {                              // 10 résultats au plus : les plus anciens ne gardent que leurs points
        [state.undo, state.redo].forEach((stack) => {
            let kept = 0;
            for (let i = stack.length - 1; i >= 0; i -= 1) {
                if (!stack[i].payload) continue;
                kept += 1;
                if (kept > MAX_RESULTS_KEPT) { stack[i].payload = null; stack[i].params = null; }
            }
        });
    }
    function pushUndo(entry) {
        state.undo.push(entry);
        if (state.undo.length > MAX_HISTORY) state.undo.shift();
        state.redo = [];
        trimHistory();
    }
    function pushHistory() { pushUndo(snapshot()); }

    function numberedIcon(number) {
        return L.divIcon({ className: 'rb-pin', html: el('span', {}, String(number)), iconSize: [26, 26], iconAnchor: [13, 13] });
    }

    // -- dessin des points -------------------------------------------------------------------------------------------------
    function clearGhost() {
        if (state.hover) { Viewer.map.removeLayer(state.hover); state.hover = null; }
        Viewer.map.getContainer().classList.remove('rb-over-track');
    }

    function openMenu(marker, index, byKeyboard = false) {
        const n = state.points.length;
        const locked = !!state.controller;
        const inResult = resultShown();
        const menu = el('div', { class: 'rb-menu' },
            el('button', { type: 'button', class: 'btn btn-sm btn-outline-danger me-1', disabled: locked || (inResult && n === 1),
                onclick: () => { marker.closePopup(); removePoint(index); } }, 'Retirer ce point'),
            el('button', { type: 'button', class: 'btn btn-sm btn-outline-secondary me-1', disabled: locked, onclick: () => {
                marker.closePopup();
                state.insertBefore = index;
                say(`Clique sur la carte pour insérer un point avant le point ${index + 1}.`, 'info');
            } }, 'Insérer avant'),
            el('button', { type: 'button', class: 'btn btn-sm btn-outline-secondary', disabled: locked,
                onclick: () => { marker.closePopup(); startInsertAfter(index); } }, 'Ajouter un point après celui-ci'));
        marker.bindPopup(menu).openPopup();
        if (byKeyboard) { const first = menu.querySelector('button:not([disabled])'); if (first) first.focus(); }
    }

    function redraw() {
        state.redrawing = true;
        clearGhost();
        if (state.preview) { Viewer.map.removeLayer(state.preview); state.preview = null; }
        if (state.points.length >= 2) {            // boucle fermée : le dernier point revient au point 1 (RB-2.3)
            state.preview = L.polyline([...state.points, state.points[0]], { color: '#0d6efd', weight: 2, dashArray: '6 6',
                interactive: false }).addTo(Viewer.map);
        }
        state.markers.forEach((m) => Viewer.map.removeLayer(m));
        state.markers = state.points.map(([lat, lon], index) => {
            const marker = L.marker([lat, lon], { icon: numberedIcon(index + 1), draggable: !state.controller, keyboard: true,
                zIndexOffset: 10000,                 // au-dessus du repère du fichier de résultat (16 px au même endroit)
                title: `Point ${index + 1} (clic droit : retirer)` });
            marker.on('dragend', () => commitMove(index, marker.getLatLng()));
            marker.on('contextmenu', () => removePoint(index));
            marker.on('click', (event) => {                       // clic / appui : retirer, insérer avant ou après (RB-2.2, tactile compris)
                L.DomEvent.stopPropagation(event);
                openMenu(marker, index);
            });
            marker.addTo(Viewer.map);
            const node = marker.getElement();
            if (node) {
                node.setAttribute('role', 'button');
                node.setAttribute('aria-label', `Point ${index + 1} : flèches pour déplacer, Entrée pour valider ou ouvrir le menu, Suppr pour retirer`);
                node.addEventListener('keydown', (event) => onPointKey(event, index, marker));
                node.addEventListener('blur', () => { if (!state.redrawing) revertKeyboard(); });
            }
            return marker;
        });
        state.redrawing = false;
        if (state.refocus !== null) {
            const target = state.markers[state.refocus];
            state.refocus = null;
            if (target && target.getElement()) target.getElement().focus();
        }
        const n = state.points.length;
        $('rbCount').textContent = `${n} point${n > 1 ? 's' : ''}`
            + (n >= 2 ? ` · à vol d'oiseau ${(birdLength(state.points) / 1000).toFixed(1)} km` : '');
        updateButtons();
    }

    function after() { state.dirty = state.points.length > 0; redraw(); save(); say(''); }

    function updateButtons() {
        const ok = state.options && state.options.available;
        const n = state.points.length;
        const needsDistance = n === 1;
        const busy = !!state.controller;
        $('rbFinish').disabled = !ok || n < 1 || (needsDistance && !$('rbDistance').value) || busy;
        $('rbUndo').disabled = state.undo.length === 0 || busy;
        $('rbRedo').disabled = state.redo.length === 0 || busy;
        $('rbClear').disabled = n === 0 || busy;
        if (state.result) { $('rbSave').disabled = busy; $('rbAnother').disabled = busy; }
        $('rbAddEnd').disabled = busy || n === 0;
        state.markers.forEach((m) => { if (m.dragging) m.dragging[busy ? 'disable' : 'enable'](); });
        Viewer.map.getContainer().classList.toggle('rb-recomputing', busy && !!state.result);
    }

    // -- gestes sur les points ------------------------------------------------------------------------------------------------
    // Applique un nouveau jeu de points : en édition, sans génération ; avec un résultat affiché, par régénération (RB-2.7)
    function mutate(next) {
        if (resultShown()) return regenerate(next);
        pushHistory();
        state.points = next;
        after();
        return Promise.resolve();
    }
    function tooClose(next, index, inserted = false) {                      // deux voisins de la boucle à moins de 5 m : le serveur dédoublonnerait (RB-S1b)
        const n = next.length;
        if (n < 2) return false;
        for (const other of new Set([(index - 1 + n) % n, (index + 1) % n])) {
            if (other !== index && dist(next[index], next[other]) < MIN_GAP_M) { say(`Trop près du point ${inserted && other > index ? other : other + 1}.`, 'warn'); return true; }
        }
        return false;
    }
    function blockedByCalculation() {
        if (!state.controller) return false;
        say(WAIT, 'warn');
        return true;
    }

    function commitMove(index, latlng) {
        state.kbd = null;
        if (blockedByCalculation()) { redraw(); return; }
        const old = state.points[index];
        const point = [round6(latlng.lat), round6(latlng.lng)];
        if (!inCoverage(point[0], point[1])) { say('Hors de la zone couverte.', 'warn'); redraw(); return; }
        if (resultShown() && dist(old, point) < MIN_GAP_M) { redraw(); return; }       // déplacement négligeable : rien ne se passe
        const next = state.points.map((p) => [...p]);
        next[index] = point;
        if (resultShown() && tooClose(next, index)) { redraw(); return; }
        state.refocus = state.markers[index] && document.activeElement === state.markers[index].getElement() ? index : null;
        mutate(next);
    }

    function removePoint(index) {
        if (blockedByCalculation()) return;
        const n = state.points.length;
        if (resultShown()) {
            if (n === 1) return;                           // il reste toujours au moins 1 point
            if (n === 2 && state.result.params.distance_km === null) {
                say('Un seul point demande une distance voulue : indique-la puis « Terminer ».', 'warn');
                return;
            }
        }
        mutate(state.points.filter((_, i) => i !== index));
    }

    function addPointAt(lat, lng, index = null) {          // ajout ou insertion d'un point (clic sur la carte, appui sur la trace, clavier)
        if (blockedByCalculation()) return false;
        if (state.points.length >= state.options.max_waypoints) { say(`${state.options.max_waypoints} points au plus.`, 'warn'); return false; }
        if (!inCoverage(lat, lng)) { say('Hors de la zone couverte.', 'warn'); return false; }
        const point = [round6(lat), round6(lng)];
        const next = state.points.map((p) => [...p]);
        const at = index === null ? next.length : index;
        next.splice(at, 0, point);
        if (resultShown() && tooClose(next, at, true)) return false;
        mutate(next);
        return true;
    }

    function addPoint(event) {                             // clic sur la carte (hors trace et hors point)
        if (!state.active || Date.now() < state.suppressClick) return;
        const { lat, lng } = event.latlng;
        let index = null;
        if (state.insertBefore !== null) { index = state.insertBefore; state.insertBefore = null; }
        addPointAt(lat, lng, index);
    }

    // Insertion au clavier : le point temporaire (ni historique, ni stockage) se déplace aux flèches ; Entrée l'insère, Échap le supprime
    function startInsertAfter(index) {
        if (blockedByCalculation()) return;
        const n = state.points.length;
        if (n >= state.options.max_waypoints) { say(`${state.options.max_waypoints} points au plus.`, 'warn'); return; }
        let spot;
        if (n === 1) {
            const east = Viewer.map.containerPointToLatLng(Viewer.map.latLngToContainerPoint(state.points[0]).add([50, 0]));
            spot = [round6(east.lat), round6(east.lng)];
        } else {
            const a = state.points[index], b = state.points[(index + 1) % n];
            spot = [round6((a[0] + b[0]) / 2), round6((a[1] + b[1]) / 2)];
        }
        revertKeyboard();
        state.points.splice(index + 1, 0, spot);
        state.pendingInsert = index + 1;
        state.refocus = index + 1;
        redraw();
        say('Déplace le nouveau point avec les flèches, Entrée pour l\'insérer, Échap pour l\'abandonner.', 'info');
    }
    function revertKeyboard() {                            // Échap, perte du focus : point revenu ou point temporaire supprimé, sans requête
        if (state.pendingInsert !== null) {
            state.points.splice(state.pendingInsert, 1);
            state.pendingInsert = null;
            state.kbd = null;
            redraw();
            say('');
        } else if (state.kbd) {
            const { index } = state.kbd;
            state.kbd = null;
            const marker = state.markers[index];
            if (marker) marker.setLatLng(state.points[index]);
        }
    }
    function onPointKey(event, index, marker) {
        const arrows = { ArrowRight: [1, 0], ArrowLeft: [-1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] }[event.key];
        if (arrows) {
            event.preventDefault(); event.stopPropagation();                 // la carte ne doit pas se déplacer
            if (blockedByCalculation()) return;
            const step = event.shiftKey ? KEY_STEP_SHIFT : KEY_STEP;
            const at = Viewer.map.latLngToContainerPoint(marker.getLatLng()).add([arrows[0] * step, arrows[1] * step]);
            marker.setLatLng(Viewer.map.containerPointToLatLng(at));
            if (!state.kbd) state.kbd = { index };
        } else if (event.key === 'Enter') {
            event.preventDefault(); event.stopPropagation();
            if (blockedByCalculation()) return;
            if (state.pendingInsert === index) {
                const spot = marker.getLatLng();
                state.points.splice(index, 1);                               // l'état « d'avant » ne contient pas le point temporaire
                state.pendingInsert = null; state.kbd = null;
                state.refocus = index;
                if (!addPointAt(spot.lat, spot.lng, index)) { state.refocus = null; redraw(); }
            } else if (state.kbd && state.kbd.index === index) commitMove(index, marker.getLatLng());
            else openMenu(marker, index, true);
        } else if (event.key === 'Escape') {
            event.preventDefault(); event.stopPropagation();
            revertKeyboard();
        } else if (event.key === 'Delete' || event.key === 'Backspace') {
            event.preventDefault(); event.stopPropagation();
            if (state.pendingInsert === index) revertKeyboard(); else removePoint(index);
        }
    }

    // -- appui sur la trace : insertion (souris, toucher, stylet) ---------------------------------------------------------
    function toleranceFor(event) { return event.pointerType === 'mouse' || !event.pointerType ? TOLERANCE_MOUSE : TOLERANCE_TOUCH; }
    function onPointerDown(event) {
        if (!state.active || !resultShown() || (event.button !== undefined && event.button > 0) || state.gesture) return;
        if (event.target.closest && event.target.closest('.leaflet-marker-icon, .leaflet-popup, .leaflet-control')) return;   // un point passe avant la trace
        const hit = traceOf(state.result).coords.length > 1 ? nearestOnTrace(Viewer.map.mouseEventToContainerPoint(event)) : null;
        if (!hit || hit.distance > toleranceFor(event)) return;           // hors tolérance : un appui sur la carte
        if (state.controller) { say(WAIT, 'warn'); return; }
        if (state.points.length >= state.options.max_waypoints) {
            say(`${state.options.max_waypoints} points au plus.`, 'warn');
            state.suppressClick = Date.now() + 400;
            return;
        }
        Viewer.map.dragging.disable();                                    // le glisser crée un point au lieu de déplacer la carte
        clearGhost();
        const index = insertionIndex(hit.position);                       // le segment est celui de l'appui, même si le point est glissé ailleurs
        const ghost = L.marker(Viewer.map.mouseEventToLatLng(event), { icon: numberedIcon(index + 1), interactive: false, keyboard: false }).addTo(Viewer.map);
        state.gesture = { ghost, index };
        const move = (e) => ghost.setLatLng(Viewer.map.mouseEventToLatLng(e));
        const finish = (e, commit) => {
            document.removeEventListener('pointermove', move);
            document.removeEventListener('pointerup', up);
            document.removeEventListener('pointercancel', cancel);
            Viewer.map.removeLayer(ghost);
            state.gesture = null;
            Viewer.map.dragging.enable();
            state.suppressClick = Date.now() + 400;                       // le « clic » qui suit le relâchement n'ajoute rien
            if (commit) {
                const at = Viewer.map.mouseEventToLatLng(e);
                addPointAt(at.lat, at.lng, index);
            }
        };
        const up = (e) => finish(e, true);
        const cancel = (e) => finish(e, false);
        document.addEventListener('pointermove', move);
        document.addEventListener('pointerup', up);
        document.addEventListener('pointercancel', cancel);
        event.preventDefault();
    }
    // Survol à la souris : point fantôme sur la trace, curseur et infobulle (RB-2.7)
    function onPointerMove(event) {
        if (event.pointerType !== 'mouse' || state.gesture) return;
        const container = Viewer.map.getContainer();
        if (!state.active || !resultShown() || state.controller || (event.target.closest && event.target.closest('.leaflet-marker-icon, .leaflet-popup, .leaflet-control'))) { clearGhost(); return; }
        const hit = traceOf(state.result).coords.length > 1 ? nearestOnTrace(Viewer.map.mouseEventToContainerPoint(event)) : null;
        if (!hit || hit.distance > TOLERANCE_MOUSE) { clearGhost(); return; }
        if (!state.hover) {
            state.hover = L.circleMarker(hit.latlng, { radius: 5, color: '#fff', weight: 2, fillColor: '#0d6efd', fillOpacity: 1, interactive: false }).addTo(Viewer.map);
            state.hover.bindTooltip('Clique ou glisse pour ajouter un point', { direction: 'top', offset: [0, -6] });
            state.hover.openTooltip();
            container.classList.add('rb-over-track');
        }
        state.hover.setLatLng(hit.latlng);
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
            show(result, { activity: body.activity, distance_km: body.distance_km === undefined ? null : body.distance_km }, { fit: true });
        } catch (error) {
            if (error.name !== 'AbortError') say(error.message, 'error');
            else say('Calcul abandonné (le serveur termine dans son budget de temps).', 'warn');
        } finally {
            state.controller = null;
            $('rbCancel').hidden = true;
            updateButtons();
        }
    }

    // Régénération après un geste (RB-2.7) : mêmes paramètres et même graine que la trace affichée, sans previous_result_id
    async function regenerate(next) {
        if (state.controller) { say(WAIT, 'warn'); return; }
        const before = snapshot();
        const keepFocus = state.refocus;                        // le point manipulé au clavier garde le focus après chaque redessin
        const params = state.result.params;
        const body = { points: next, activity: params.activity };
        if (params.distance_km !== null) body.distance_km = params.distance_km;
        if (state.result.meta.seed !== undefined && state.result.meta.seed !== null) body.seed = state.result.meta.seed;
        state.lastRequest = { ...body };
        state.controller = new AbortController();
        state.points = next;                                   // affichés tout de suite ; stockés seulement si le recalcul aboutit
        $('rbCancel').hidden = false;
        alertText('');
        say('Recalcul en cours…', 'info');
        redraw();
        try {
            const result = await api('/api/route-builder/route', { method: 'POST', body, signal: state.controller.signal });
            pushUndo(before);
            show(result, { ...params }, { fit: false });
            state.dirty = true;
            save();
            say('');
        } catch (error) {
            state.points = before.points;                      // retour exact : points, numérotation, stockage (jamais réécrit)
            state.refocus = null;
            const aborted = error.name === 'AbortError';
            say('');
            alertText(aborted ? 'Recalcul abandonné. La trace précédente est conservée.'
                : `Recalcul impossible : ${String(error.message).replace(/[.\s]+$/, '')}. La trace précédente est conservée.`);
        } finally {
            state.controller = null;
            $('rbCancel').hidden = true;
            state.refocus = keepFocus;
            redraw();
        }
    }

    function show(result, params, { fit = true } = {}) {
        if (state.result && state.result.fileId) closeFile(state.result.fileId);
        const file = addUploadedFile(result.filename, result, { fit });
        const meta = result.route_meta;
        state.result = { meta, fileId: file.id, payload: result, params, trace: null };
        state.nextSeed = meta.next_seed;
        $('rbResult').hidden = false;
        const gap = meta.gap_pct === null ? '' : ` · écart ${meta.gap_pct > 0 ? '+' : ''}${meta.gap_pct} %`;
        $('rbSummary').textContent = `${(meta.distance_m / 1000).toFixed(1)} km`
            + (meta.ascent_m ? ` · D+ ${Math.round(meta.ascent_m)} m` : '') + gap;
        $('rbTarget').textContent = params.distance_km !== null && params.distance_km !== undefined ? `Distance voulue : ${params.distance_km} km` : '';
        $('rbWarnings').replaceChildren(...meta.warnings.map((w) => el('li', {}, w.message)));
        $('rbName').value = result.filename;
        $('rbSaved').textContent = '';
        $('rbSave').disabled = false;
        $('rbAnother').disabled = false;
        say('');
        updateButtons();
    }

    // Un résultat quitté (« Tout effacer », retour à l'édition par Annuler) : panneau masqué, trace retirée de la carte
    function dropResult() {
        if (state.result && state.result.fileId) closeFile(state.result.fileId);
        state.result = null;
        $('rbResult').hidden = true;
    }

    // Trace enregistrée (ou « déjà enregistrée ») : le travail est fini. Points, historique et clé de stockage sont vidés, le panneau est
    // prêt pour une nouvelle trace ; la trace enregistrée reste affichée et son lien est conservé (RB-2.6, CA-R40).
    function startFresh(linkNode) {
        try { window.localStorage.removeItem(storageKey()); } catch (_) { /* ignoré */ }
        state.points = []; state.undo = []; state.redo = [];
        state.result = null; state.nextSeed = null; state.lastRequest = null; state.dirty = false; state.insertBefore = null;
        $('rbDistance').value = '';
        $('rbName').value = '';
        $('rbResult').hidden = true;
        redraw();                                  // sans save() : rien n'est réécrit tant que l'utilisateur ne modifie rien
        say('');
        $('rbSaved').replaceChildren(linkNode);
    }

    async function saveResult() {
        if (!state.result || state.controller) return;
        $('rbSave').disabled = true;
        const link = (id, text) => el('span', {}, text, el('a', { href: `/?activity=${id}` }, 'ouvrir dans la bibliothèque'));
        try {
            const response = await fetch('/api/route-builder/save', { method: 'POST', credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ result_id: state.result.meta.result_id, name: $('rbName').value }) });
            const data = await response.json().catch(() => ({}));
            if (response.ok && data.success) { startFresh(link(data.activity_id, 'Enregistrée : ')); return; }
            if (response.status === 409 && data.activity_id) { startFresh(link(data.activity_id, 'Déjà enregistrée : ')); return; }
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
        if (typeof cancelEditing === 'function') cancelEditing();       // E-6 : l'éditeur de points est fermé à l'ouverture du constructeur
        clearOtherAccounts();
        state.active = true;
        root.hidden = false;
        document.body.classList.add('rb-active');          // masque la barre d'actions de la trace tant que le constructeur est ouvert
        $('rbActivity').replaceChildren(...state.options.activities.map((a) => el('option', { value: a.key,
            selected: a.key === state.options.default_activity }, a.label)));
        $('rbDistance').min = state.options.min_km;
        $('rbDistance').max = state.options.max_km;
        $('rbDistance').step = state.options.step_km;
        restore();
        const zone = coverageVertices();
        if (zone) {                                        // contour discret de la zone couverte : polygone ou rectangle (RB-1.3b)
            state.coverage = L.polygon(zone.map(([lon, lat]) => [lat, lon]), { color: '#0d6efd', weight: 1, dashArray: '6 6', fill: false,
                interactive: false }).addTo(Viewer.map);
        }
        if (!state.options.available) {
            say(state.options.state === 'starting' ? 'Service en cours de démarrage.' : 'Service momentanément indisponible.', 'warn');
        }
        redraw();
    }

    function close() {
        if (!state.active) return;
        state.active = false;
        root.hidden = true;
        document.body.classList.remove('rb-active');
        clearGhost();
        state.markers.forEach((m) => Viewer.map.removeLayer(m));
        state.markers = [];
        if (state.coverage) { Viewer.map.removeLayer(state.coverage); state.coverage = null; }
        if (state.preview) { Viewer.map.removeLayer(state.preview); state.preview = null; }
        state.insertBefore = null;
        state.pendingInsert = null; state.kbd = null;
    }

    // « Tout effacer » : revient à l'édition ; avec un résultat affiché, l'état quitté est gardé dans l'historique (annulable sans requête)
    function reset() {
        if (state.controller) { say(WAIT, 'warn'); return; }
        pushHistory();
        state.points = [];
        if (resultShown()) dropResult();
        after();
    }

    function restoreSnapshot(entry) {
        revertKeyboard();
        state.points = entry.points.map((p) => [...p]);
        if (entry.payload) show(entry.payload, { ...entry.params }, { fit: false });          // réaffichée sans génération
        else if (resultShown()) dropResult();                                               // plus ancien : retour à l'édition
        after();
    }
    function undo() {
        if (state.controller || !state.undo.length) return;
        state.redo.push(snapshot());
        trimHistory();
        restoreSnapshot(state.undo.pop());
    }
    function redo() {
        if (state.controller || !state.redo.length) return;
        state.undo.push(snapshot());
        trimHistory();
        restoreSnapshot(state.redo.pop());
    }

    function setup() {
        root = $('routeBuilderPanel');
        if (!root) return;                                  // fonction indisponible : aucun élément dans la page (EF-7.3)
        $('routeBuilderOpen').addEventListener('click', () => (state.active ? close() : open()));
        $('rbClose').addEventListener('click', close);
        Viewer.map.on('click', addPoint);
        const container = Viewer.map.getContainer();
        container.addEventListener('pointerdown', onPointerDown, true);           // avant Leaflet : une trace appuyée ne déplace pas la carte
        container.addEventListener('pointermove', onPointerMove);
        container.addEventListener('pointerleave', clearGhost);
        container.addEventListener('click', (event) => {                          // le clic qui suit une insertion ne doit rien déclencher d'autre
            if (Date.now() < state.suppressClick) { event.stopPropagation(); event.preventDefault(); }
        }, true);
        $('rbFinish').addEventListener('click', () => generate());
        $('rbAnother').addEventListener('click', () => generate({ seed: state.nextSeed }));
        $('rbEdit').addEventListener('click', () => { $('rbResult').hidden = true; });
        $('rbAddEnd').addEventListener('click', () => { if (state.points.length) startInsertAfter(state.points.length - 1); });
        $('rbCancel').addEventListener('click', () => { if (state.controller) state.controller.abort(); });
        $('rbSave').addEventListener('click', saveResult);
        $('rbUndo').addEventListener('click', undo);
        $('rbRedo').addEventListener('click', redo);
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

    return { setup, close };
})();
