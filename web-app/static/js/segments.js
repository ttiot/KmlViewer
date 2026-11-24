'use strict';

(() => {
    const { api, el, notify, number, chrono, distance, speed, date, setFigure } = App;
    const ACTIVITY_PAGE = 500; // plafond de `limit` côté API
    // startIdx / endIdx : positions dans le tracé affiché (allégé), null tant que rien n'est choisi
    const state = { map: null, trackLayer: null, selectionLayer: null, segmentLayer: null, markers: [],
                    track: null, startIdx: null, endIdx: null, current: null, strip: null,
                    trackRequest: 0,      // numéro du dernier chargement de trace demandé
                    activitiesLoaded: 0 };

    function initMap() {
        state.map = L.map('segmentMap', { zoomControl: false }).setView([46.6, 2.5], 6);
        L.control.zoom({ position: 'topright' }).addTo(state.map);
        // Fond et cadrage viennent de /api/map-config (fonds autorisés seulement) ; le fond intégré sert d'attente
        const fallback = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
            maxZoom: 19, attribution: '© OpenStreetMap',
        }).addTo(state.map);
        App.mapConfig().then((config) => {
            if (!config || !Array.isArray(config.basemaps)) return;
            state.map.removeLayer(fallback);
            const chosen = config.basemaps.find((b) => b.name === config.default) || config.basemaps[0];
            if (chosen) App.basemapLayer(chosen).addTo(state.map);
        });
    }

    function clearLayer(key) {
        if (state[key]) { state.map.removeLayer(state[key]); state[key] = null; }
    }

    // Trait doublé d'un liseré blanc, lisible sur n'importe quel fond de carte
    function casedLine(coordinates, colorToken, weight) {
        return L.featureGroup([
            L.polyline(coordinates, { color: '#fff', weight: weight + 4, interactive: false }),
            L.polyline(coordinates, { color: AppTheme.color(colorToken), weight }),
        ]);
    }

    function pin(latlng, letter) {
        return L.marker(latlng, {
            // html reçoit un élément (jamais une chaîne) : Leaflet l'insère tel quel
            icon: L.divIcon({ className: 'map-pin', html: el('span', {}, letter), iconSize: [24, 24], iconAnchor: [12, 12] }),
            interactive: false, keyboard: false,
        });
    }

    // Le tracé affiché peut être allégé : `indices` donne la position de chaque point
    // dans la trace complète, seule référence comprise par l'API.
    function fullIndex(i) {
        return state.track.indices ? state.track.indices[i] : i;
    }

    function nearestIndex(latlng) {
        let best = 0, bestDist = Infinity;
        state.track.coordinates.forEach(([lat, lon], i) => {
            const d = (lat - latlng.lat) ** 2 + ((lon - latlng.lng) * Math.cos(lat * Math.PI / 180)) ** 2;
            if (d < bestDist) { bestDist = d; best = i; }
        });
        return best;
    }

    const selectionReady = () => state.track !== null && state.startIdx !== null && state.endIdx !== null;

    // « km 87,7 · 1 250 m » pour un point du tracé affiché
    function boundText(index) {
        if (!state.track || index === null) return '—';
        const km = `km ${number((state.track.distances_m[index] || 0) / 1000, 1)}`;
        const altitude = state.track.elevations[index];
        return Number.isFinite(altitude) ? `${km} · ${number(altitude)} m` : km;
    }

    // Dénivelé positif de la sélection, sommé sur le tracé affiché : un ordre de grandeur
    // pour choisir ses bornes, pas la mesure de référence (celle-ci est faite à la création).
    function selectionAscent() {
        let gain = 0;
        const elevations = state.track.elevations;
        for (let i = state.startIdx + 1; i <= state.endIdx; i++) {
            const step = elevations[i] - elevations[i - 1];
            if (step > 0) gain += step;
        }
        return gain;
    }

    // Met la carte, les curseurs, le profil et les mesures en accord avec la sélection
    function renderSelection() {
        const track = state.track;
        const ready = selectionReady();
        const last = track ? track.coordinates.length - 1 : 1;
        const start = document.getElementById('startSlider'), end = document.getElementById('endSlider');
        [start, end].forEach((slider) => { slider.max = last; slider.disabled = !track; });
        start.value = state.startIdx === null ? 0 : state.startIdx;
        end.value = state.endIdx === null ? last : state.endIdx;
        document.getElementById('startReadout').textContent = boundText(state.startIdx);
        document.getElementById('endReadout').textContent = boundText(state.endIdx);

        state.markers.forEach((m) => state.map.removeLayer(m));
        state.markers = [];
        clearLayer('selectionLayer');
        if (track) {
            [[state.startIdx, 'A'], [state.endIdx, 'B']].forEach(([index, letter]) => {
                if (index !== null) state.markers.push(pin(track.coordinates[index], letter).addTo(state.map));
            });
        }

        const info = document.getElementById('selectionInfo');
        const length = document.getElementById('selectionLength'), ascent = document.getElementById('selectionAscent');
        const total = track ? track.distances_m[last] : 0;
        if (ready) {
            const from = track.distances_m[state.startIdx], to = track.distances_m[state.endIdx];
            state.selectionLayer = casedLine(track.coordinates.slice(state.startIdx, state.endIdx + 1), '--map-ink', 5).addTo(state.map);
            state.selectionLayer.on('click', onTrackClick);
            setFigure(length, number((to - from) / 1000, 1), { unit: 'km' });
            setFigure(ascent, number(selectionAscent()), { unit: 'm' });
            if (state.strip && total > 0) state.strip.setBand(from / total, to / total);
            document.getElementById('stripReadout').textContent =
                `A km ${number(from / 1000, 1)} → B km ${number(to / 1000, 1)}`;
            info.textContent = 'Tu peux aussi cliquer sur la trace : premier clic pour le départ, second pour l\'arrivée.';
        } else {
            length.textContent = '—';
            ascent.textContent = '—';
            if (state.strip) state.strip.setBand(null, null);
            document.getElementById('stripReadout').textContent = '';
            if (!track) info.textContent = 'Choisis une activité, puis règle le départ et l\'arrivée.';
            else if (state.startIdx === null) info.textContent = 'Déplace les curseurs, ou clique sur la trace pour poser le départ.';
            else info.textContent = 'Départ posé. Clique sur la trace pour poser l\'arrivée.';
        }
        document.getElementById('createButton').disabled = !ready;
    }

    function resetSelection() {
        state.startIdx = state.endIdx = null;
        renderSelection();
    }

    function onTrackClick(event) {
        L.DomEvent.stopPropagation(event);
        const idx = nearestIndex(event.latlng);
        if (state.startIdx === null || state.endIdx !== null) {
            state.startIdx = idx;
            state.endIdx = null;
        } else if (idx === state.startIdx) {
            return;
        } else {
            [state.startIdx, state.endIdx] = idx > state.startIdx ? [state.startIdx, idx] : [idx, state.startIdx];
        }
        renderSelection();
    }

    // Curseur déplacé : l'autre borne prend l'extrémité de la trace si elle n'était pas posée,
    // et le départ reste toujours avant l'arrivée.
    function onSlider(bound, value) {
        if (!state.track) return;
        const last = state.track.coordinates.length - 1;
        if (state.startIdx === null) state.startIdx = 0;
        if (state.endIdx === null) state.endIdx = last;
        if (bound === 'start') state.startIdx = Math.max(0, Math.min(value, state.endIdx - 1));
        else state.endIdx = Math.min(last, Math.max(value, state.startIdx + 1));
        renderSelection();
    }

    function renderStrip() {
        const track = state.track;
        if (!track) { state.strip.clear('Le profil de la trace choisie s\'affichera ici.'); return; }
        const usable = track.elevations.some((value) => Number.isFinite(value) && value !== 0);
        if (!usable) { state.strip.clear('Cette trace ne porte pas d\'altitude.'); return; }
        state.strip.setSeries(track.distances_m, track.elevations, { color: '--viz-1', fill: '--viz-1-soft', unit: 'm' });
    }

    // Deux activités choisies coup sur coup : seule la réponse du dernier choix est
    // affichée. Sans ce jeton, une réponse tardive laisserait une trace orpheline sur la
    // carte et un tronçon serait créé sur une autre activité que celle affichée.
    async function loadTrack(activityId) {
        const request = ++state.trackRequest;
        clearLayer('trackLayer');
        state.track = null;
        resetSelection();
        renderStrip();
        if (!activityId) return;
        try {
            const track = await api(`/api/activities/${activityId}/track`);
            if (request !== state.trackRequest) return;
            clearLayer('trackLayer');
            state.track = { ...track, distances_m: track.distances_m || [], elevations: track.elevations || [] };
            state.trackLayer = casedLine(track.coordinates, '--trace-muted', 3).addTo(state.map);
            state.trackLayer.on('click', onTrackClick);
            state.map.fitBounds(state.trackLayer.getBounds(), { padding: [20, 20] });
        } catch (error) {
            if (request !== state.trackRequest) return;
            notify(error.message, 'danger');
        }
        renderStrip();
        renderSelection();
    }

    function renderDetail(segment) {
        state.current = segment;
        clearLayer('segmentLayer');
        state.segmentLayer = casedLine(segment.coordinates, '--accent', 5).addTo(state.map);
        document.querySelectorAll('.segment-item').forEach((item) =>
            item.classList.toggle('active', Number(item.dataset.id) === segment.id));
        state.map.fitBounds(state.segmentLayer.getBounds(), { padding: [40, 40] });

        document.getElementById('segmentDetail').hidden = false;
        document.getElementById('detailTitle').textContent = `${segment.name} · ${distance(segment.distance_m)}`;
        document.getElementById('efforts').replaceChildren(...segment.efforts.map((e) =>
            el('tr', {},
                el('td', {}, el('span', { class: `rank-badge ${e.rank === 1 ? 'is-first' : ''}` }, e.rank)),
                el('td', {}, el('strong', {}, chrono(e.elapsed_s))),
                el('td', {}, speed(segment.distance_m, e.elapsed_s)),
                el('td', { class: 'text-nowrap' }, date(e.start_time)),
                el('td', {}, el('a', { class: 'activity-name', href: `/?activity=${e.activity_id}` }, e.activity_name)))));
        document.getElementById('efforts').closest('.table-responsive').hidden = segment.efforts.length === 0;
        document.getElementById('effortsEmpty').hidden = segment.efforts.length > 0;
    }

    async function showSegment(id) {
        try {
            renderDetail((await api(`/api/segments/${id}`)).segment);
        } catch (error) { notify(error.message, 'danger'); }
    }

    async function loadSegments() {
        const { segments } = await api('/api/segments');
        document.getElementById('segmentList').replaceChildren(...segments.map((s) =>
            el('button', { type: 'button', class: `segment-item ${state.current && state.current.id === s.id ? 'active' : ''}`,
                           dataset: { id: s.id }, onclick: () => showSegment(s.id) },
                el('div', { class: 'd-flex justify-content-between align-items-baseline gap-2' },
                    el('strong', {}, s.name), el('span', { class: 'segment-best' }, s.best_s ? chrono(s.best_s) : '—')),
                el('span', { class: 'hint' }, `${distance(s.distance_m)} · ${s.nb_efforts} passage(s)`))));
        document.getElementById('segmentsEmpty').hidden = segments.length > 0;
    }

    // Ajoute au sélecteur la page suivante d'activités ; le bouton « plus anciennes » reste
    // affiché tant que la dernière page reçue était pleine.
    async function loadActivities() {
        const params = new URLSearchParams({ limit: ACTIVITY_PAGE, offset: state.activitiesLoaded });
        const { activities } = await api(`/api/activities?${params}`);
        state.activitiesLoaded += activities.length;
        document.getElementById('activitySelect').append(...activities.map((a) =>
            el('option', { value: a.id }, `${date(a.start_time)} · ${a.name} (${distance(a.distance_m)})`)));
        document.getElementById('moreActivitiesButton').hidden = activities.length < ACTIVITY_PAGE;
    }

    async function createSegment() {
        const name = document.getElementById('segmentName').value.trim();
        if (!name) { notify('Donne un nom au tronçon.', 'warning'); return; }
        if (!selectionReady()) return;
        try {
            const data = await api('/api/segments', { method: 'POST', body: {
                name, activity_id: state.track.id,
                start_idx: fullIndex(state.startIdx), end_idx: fullIndex(state.endIdx) } });
            notify(`Tronçon créé : ${data.segment.efforts.length} passage(s) trouvé(s).`, 'success');
            document.getElementById('segmentName').value = '';
            resetSelection();
            await loadSegments();
            renderDetail(data.segment);
        } catch (error) { notify(error.message, 'danger'); }
    }

    async function deleteCurrent() {
        if (!state.current || !window.confirm(`Supprimer le tronçon « ${state.current.name} » et ses temps ?`)) return;
        try {
            await api(`/api/segments/${state.current.id}`, { method: 'DELETE' });
            clearLayer('segmentLayer');
            document.getElementById('segmentDetail').hidden = true;
            state.current = null;
            await loadSegments();
        } catch (error) { notify(error.message, 'danger'); }
    }

    async function rescanCurrent() {
        if (!state.current) return;
        try {
            const data = await api(`/api/segments/${state.current.id}/rescan`, { method: 'POST' });
            renderDetail(data.segment);
            await loadSegments();
        } catch (error) { notify(error.message, 'danger'); }
    }

    document.addEventListener('DOMContentLoaded', () => {
        initMap();
        state.strip = ProfileStrip.create(document.getElementById('segmentStrip'),
            { small: true, label: 'Profil d\'élévation avec le tronçon en surbrillance' });
        renderStrip();
        renderSelection();
        document.getElementById('activitySelect').addEventListener('change', (e) => loadTrack(e.target.value));
        document.getElementById('startSlider').addEventListener('input', (e) => onSlider('start', Number(e.target.value)));
        document.getElementById('endSlider').addEventListener('input', (e) => onSlider('end', Number(e.target.value)));
        document.getElementById('resetButton').addEventListener('click', resetSelection);
        document.getElementById('createButton').addEventListener('click', createSegment);
        document.getElementById('deleteButton').addEventListener('click', deleteCurrent);
        document.getElementById('rescanButton').addEventListener('click', rescanCurrent);
        document.getElementById('moreActivitiesButton').addEventListener('click', () => {
            loadActivities().catch((error) => notify(error.message, 'danger'));
        });
        Promise.all([loadSegments(), loadActivities()]).catch((error) => notify(error.message, 'danger'));
    });
})();
