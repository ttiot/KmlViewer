'use strict';

(() => {
    const { api, el, notify, chrono, distance, speed, date } = App;
    const ACTIVITY_PAGE = 500; // plafond de `limit` côté API
    const state = { map: null, trackLayer: null, selectionLayer: null, segmentLayer: null, markers: [],
                    track: null, startIdx: null, endIdx: null, current: null,
                    trackRequest: 0,      // numéro du dernier chargement de trace demandé
                    activitiesLoaded: 0 };

    function initMap() {
        state.map = L.map('segmentMap').setView([46.6, 2.5], 6);
        L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
            maxZoom: 19, attribution: '© OpenStreetMap',
        }).addTo(state.map);
    }

    function clearLayer(key) {
        if (state[key]) { state.map.removeLayer(state[key]); state[key] = null; }
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

    function resetSelection() {
        state.startIdx = state.endIdx = null;
        state.markers.forEach((m) => state.map.removeLayer(m));
        state.markers = [];
        clearLayer('selectionLayer');
        updateSelectionInfo();
    }

    function updateSelectionInfo() {
        const info = document.getElementById('selectionInfo');
        const ready = state.startIdx !== null && state.endIdx !== null;
        if (!state.track) info.textContent = 'Choisis une activité, puis clique deux fois sur la trace.';
        else if (state.startIdx === null) info.textContent = 'Clique sur la trace pour poser le départ.';
        else if (!ready) info.textContent = "Départ posé. Clique pour poser l'arrivée.";
        else info.textContent = `Sélection : ${fullIndex(state.endIdx) - fullIndex(state.startIdx) + 1} points.`;
        document.getElementById('createButton').disabled = !ready;
    }

    function onTrackClick(event) {
        const idx = nearestIndex(event.latlng);
        if (state.startIdx === null || state.endIdx !== null) {
            resetSelection();
            state.startIdx = idx;
        } else if (idx === state.startIdx) {
            return;
        } else {
            [state.startIdx, state.endIdx] = idx > state.startIdx ? [state.startIdx, idx] : [idx, state.startIdx];
            const coords = state.track.coordinates.slice(state.startIdx, state.endIdx + 1);
            state.selectionLayer = L.polyline(coords, { color: AppTheme.color('--accent'), weight: 6 }).addTo(state.map);
        }
        state.markers.forEach((m) => state.map.removeLayer(m));
        state.markers = [state.startIdx, state.endIdx].filter((i) => i !== null).map((i) =>
            L.circleMarker(state.track.coordinates[i], { radius: 7, color: '#fff', weight: 2,
                fillColor: AppTheme.color('--accent'), fillOpacity: 1 }).addTo(state.map));
        updateSelectionInfo();
    }

    // Deux activités choisies coup sur coup : seule la réponse du dernier choix est
    // affichée. Sans ce jeton, une réponse tardive laisserait une trace orpheline sur la
    // carte et un tronçon serait créé sur une autre activité que celle affichée.
    async function loadTrack(activityId) {
        const request = ++state.trackRequest;
        clearLayer('trackLayer');
        resetSelection();
        state.track = null;
        if (!activityId) { updateSelectionInfo(); return; }
        try {
            const track = await api(`/api/activities/${activityId}/track`);
            if (request !== state.trackRequest) return;
            clearLayer('trackLayer');
            state.track = track;
            state.trackLayer = L.polyline(track.coordinates, { color: AppTheme.color('--viz-1'), weight: 4 }).addTo(state.map);
            state.trackLayer.on('click', onTrackClick);
            state.map.fitBounds(state.trackLayer.getBounds(), { padding: [20, 20] });
        } catch (error) {
            if (request !== state.trackRequest) return;
            notify(error.message, 'danger');
        }
        updateSelectionInfo();
    }

    function renderDetail(segment) {
        state.current = segment;
        clearLayer('segmentLayer');
        state.segmentLayer = L.polyline(segment.coordinates, { color: AppTheme.color('--accent'), weight: 6 }).addTo(state.map);
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
                el('td', {}, date(e.start_time)),
                el('td', {}, el('a', { href: `/?activity=${e.activity_id}` }, e.activity_name)))));
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
                el('div', { class: 'd-flex justify-content-between gap-2' },
                    el('strong', {}, s.name), el('span', { class: 'segment-best' }, s.best_s ? chrono(s.best_s) : '—')),
                el('small', { class: 'text-muted' }, `${distance(s.distance_m)} · ${s.nb_efforts} passage(s)`))));
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
        if (!state.track || state.startIdx === null || state.endIdx === null) return;
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
        document.getElementById('activitySelect').addEventListener('change', (e) => loadTrack(e.target.value));
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
