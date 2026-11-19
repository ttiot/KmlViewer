// Visualiseur : analyse de trajectoire (statistiques, graphiques, analyse avancée).
'use strict';

let analysisRequest = 0;

const STAT_FIELDS = [
    // [id de l'élément, section de l'analyse, champ, suffixe, préfixe]
    ['totalDistance', 'distance', 'total_distance_km', ' km', ''],
    ['avgSpeed', 'speed', 'avg_speed_kmh', ' km/h', ''],
    ['maxSpeed', 'speed', 'max_speed_kmh', ' km/h', ''],
    ['totalAscent', 'elevation', 'total_ascent', ' m', '+'],
    ['totalDescent', 'elevation', 'total_descent', ' m', '-'],
    ['minElevation', 'elevation', 'min_elevation', ' m', ''],
    ['maxElevation', 'elevation', 'max_elevation', ' m', ''],
    ['minSpeed', 'speed', 'min_speed_kmh', ' km/h', ''],
    ['avgSpeedDetail', 'speed', 'avg_speed_kmh', ' km/h', ''],
    ['maxSpeedDetail', 'speed', 'max_speed_kmh', ' km/h', ''],
];

function updateBasicStats(analysis) {
    STAT_FIELDS.forEach(([id, section, field, suffix, prefix]) => {
        const value = (analysis[section] || {})[field];
        // Un zéro mesuré (vitesse minimale à l'arrêt, altitude au niveau de la mer) s'affiche
        document.getElementById(id).textContent = known(value) ? `${prefix}${value}${suffix}` : '-';
    });
    // Durées réelles, lues dans les horodatages ; « - » pour une trace non horodatée
    const duration = analysis.duration || {};
    document.getElementById('totalDuration').textContent = formatDuration(duration.total_s);
    document.getElementById('movingDuration').textContent = formatDuration(duration.moving_s);
    const { el } = App;
    const notes = [...(analysis.notes || [])];
    if (analysis.basic_stats && analysis.basic_stats.total_points && !analysis.basic_stats.has_time_data) {
        notes.push(analysis.basic_stats.has_speed_data
            ? 'Trace non horodatée : pas de durée, vitesses lues dans le fichier.'
            : 'Trace non horodatée : ni durée ni vitesse.');
    }
    document.getElementById('basicNotes').replaceChildren(...notes.map((note) =>
        el('div', { class: 'small text-muted' }, el('i', { class: 'fas fa-circle-info me-1' }), String(note))));
}

// Courbe à série unique : trait fin, pas de marqueur par point (plusieurs milliers de
// points), grille en retrait, infobulle au survol de n'importe où sur la colonne.
function createLineChart(canvasId, labels, data, { colorToken, softToken, xTitle, yTitle, unit }) {
    return new Chart(document.getElementById(canvasId).getContext('2d'), {
        type: 'line',
        data: {
            labels,
            datasets: [{
                label: yTitle,
                data,
                // Couleurs lues dans les jetons à chaque rendu : suit le thème clair / sombre
                borderColor: () => AppTheme.color(colorToken),
                backgroundColor: () => AppTheme.color(softToken),
                borderWidth: 2,
                pointRadius: 0,
                pointHoverRadius: 4,
                pointHoverBackgroundColor: () => AppTheme.color(colorToken),
                fill: true,
                tension: 0.15,
            }],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: false,
            interaction: { mode: 'index', intersect: false },
            plugins: {
                legend: { display: false },
                tooltip: {
                    callbacks: {
                        title: (items) => `${xTitle} : ${items[0].label}`,
                        label: (item) => `${Number(item.parsed.y).toLocaleString('fr-FR', { maximumFractionDigits: 1 })} ${unit}`,
                    },
                },
            },
            scales: {
                x: {
                    grid: { display: false },
                    border: { display: false },
                    ticks: { maxTicksLimit: 10, maxRotation: 0 },
                    title: { display: true, text: xTitle },
                },
                y: { border: { display: false }, title: { display: true, text: yTitle } },
            },
        },
    });
}

function destroyCharts() {
    Object.keys(Viewer.charts).forEach((key) => {
        if (Viewer.charts[key]) Viewer.charts[key].destroy();
        Viewer.charts[key] = null;
    });
}

function displayAnalysisResults(analysis) {
    updateBasicStats(analysis);
    destroyCharts();
    const elevationProfile = analysis.elevation && analysis.elevation.elevation_profile;
    if (elevationProfile && elevationProfile.length) {
        Viewer.charts.elevation = createLineChart('elevationChart',
            elevationProfile.map((point) => (point.distance / 1000).toFixed(1)),
            elevationProfile.map((point) => point.elevation),
            { colorToken: '--viz-1', softToken: '--viz-1-soft', xTitle: 'Distance (km)', yTitle: 'Altitude (m)', unit: 'm' });
    }
    const speedProfile = analysis.speed && analysis.speed.speed_profile;
    if (speedProfile && speedProfile.length) {
        Viewer.charts.speed = createLineChart('speedChart',
            speedProfile.map((point) => (point.distance / 1000).toFixed(1)),
            speedProfile.map((point) => point.speed_kmh),
            { colorToken: '--viz-2', softToken: '--viz-2-soft', xTitle: 'Distance (km)', yTitle: 'Vitesse (km/h)', unit: 'km/h' });
    }
}

function setAnalysisPanelVisible(visible) {
    setVisible('analysisPanel', visible);
    setTimeout(() => Viewer.map && Viewer.map.invalidateSize(), 300);
}

// Analyse le fichier actif. Le panneau reste affiché même si l'analyse échoue :
// il porte aussi l'édition et l'export.
async function analyzeTrajectory() {
    const file = Viewer.activeFile();
    if (!file || file.data.features.length === 0) {
        // Plus rien à analyser (fichier vide, dernier fichier fermé) : une réponse encore
        // en route ne doit pas réafficher les résultats d'un autre fichier
        analysisRequest += 1;
        Viewer.analysis = null;
        destroyCharts();
        setAnalysisPanelVisible(false);
        return;
    }
    setAnalysisPanelVisible(true);
    const request = ++analysisRequest;
    try {
        const data = await App.api('/api/analysis/trajectory', { method: 'POST', body: analysisPayload(file.data) });
        if (request !== analysisRequest) return; // un autre fichier a été activé entre-temps
        Viewer.analysis = data.analysis;
    } catch (error) {
        if (request !== analysisRequest) return;
        console.error("Analyse impossible :", error);
        // Sans ce message, l'échec ne se voit qu'à des statistiques restées vides
        showAlert(`Analyse impossible : ${error.message}`, 'warning');
        Viewer.analysis = {};
    }
    displayAnalysisResults(Viewer.analysis);
}

// ------------------------------------------------------------ Analyse avancée
// Le serveur renvoie des synthèses compactes ; chaque élément porte de quoi être
// montré sur la carte (coordinates, bounds ou path). Une analyse indisponible est
// expliquée par `summary.unavailable` plutôt que remplacée par une estimation.

let advancedRequest = 0;

const fixed = (value, digits) => (
    value !== null && value !== undefined && Number.isFinite(Number(value)) ? Number(value).toFixed(digits) : '-');
const signed = (value, digits) => (Number(value) > 0 ? `+${fixed(value, digits)}` : fixed(value, digits));
const known = (value) => value !== null && value !== undefined && Number.isFinite(Number(value));

// Durées au format commun (common.js) ; « - » pour une durée inconnue, comme les autres
// statistiques du visualiseur.
const formatDuration = (seconds) => App.duration(seconds, '-');

function formatClock(iso) {
    const date = iso ? new Date(iso) : null;
    if (!date || Number.isNaN(date.getTime())) return null;
    return date.toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit' });
}

const TERRAIN_LABELS = { flat: 'Plat', gentle: 'Doux', moderate: 'Modéré', steep: 'Raide' };
const TERRAIN_RANGES = { flat: '≤ 5 %', gentle: '5 – 15 %', moderate: '15 – 30 %', steep: '> 30 %' };
const TERRAIN_BADGES = { flat: 'bg-success', gentle: 'bg-info', moderate: 'bg-warning', steep: 'bg-danger' };
const terrainLabel = (type) => TERRAIN_LABELS[type] || String(type || '-');
const terrainBadge = (type) => TERRAIN_BADGES[type] || 'bg-secondary';

const SEGMENT_TYPES = {
    ascent: { label: 'Montée', icon: 'arrow-trend-up', color: '#e74c3c' },
    descent: { label: 'Descente', icon: 'arrow-trend-down', color: '#3498db' },
    flat: { label: 'Plat', icon: 'arrow-right-long', color: '#7f8c8d' },
};
const POI_TYPES = {
    highest: { icon: 'mountain', color: '#e74c3c' },
    lowest: { icon: 'water', color: '#3498db' },
    summit: { icon: 'caret-up', color: '#e67e22' },
    valley: { icon: 'caret-down', color: '#2980b9' },
    turn: { icon: 'rotate-right', color: '#8e44ad' },
};
const DIRECTION_ICONS = { up: 'arrow-trend-up', down: 'arrow-trend-down', flat: 'arrow-right-long' };

function clearAdvancedHighlight() {
    if (Viewer.advancedHighlight) Viewer.map.removeLayer(Viewer.advancedHighlight);
    Viewer.advancedHighlight = null;
}

// Montre un élément de l'analyse sur la carte : tracé, emprise ou simple position.
function focusOnMap(item, color = '#e74c3c') {
    if (!Viewer.map || !item) return;
    clearAdvancedHighlight();
    const group = L.layerGroup();
    const valid = (c) => Array.isArray(c) && known(c[0]) && known(c[1]);
    const path = (item.path || []).filter(valid);
    if (path.length >= 2) {
        const line = L.polyline(path, { color, weight: 8, opacity: 0.55, interactive: false }).addTo(group);
        Viewer.map.fitBounds(line.getBounds(), { padding: [30, 30], maxZoom: 17 });
    } else if (Array.isArray(item.bounds) && item.bounds.length === 2 && item.bounds.every(valid) && !valid(item.coordinates)) {
        L.rectangle(item.bounds, { color, weight: 2, fillOpacity: 0.08, interactive: false }).addTo(group);
        Viewer.map.fitBounds(item.bounds, { padding: [30, 30], maxZoom: 17 });
    } else if (valid(item.coordinates)) {
        L.circleMarker(item.coordinates, { color, radius: 11, weight: 3, fillOpacity: 0.25, interactive: false }).addTo(group);
        Viewer.map.setView(item.coordinates, Math.max(Viewer.map.getZoom(), 16));
    } else {
        return;
    }
    Viewer.advancedHighlight = group.addTo(Viewer.map);
    document.getElementById('map').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

// Rend un nœud activable à la souris et au clavier.
function clickable(node, action) {
    node.classList.add('advanced-item');
    node.setAttribute('role', 'button');
    node.setAttribute('tabindex', '0');
    node.title = 'Voir sur la carte';
    node.addEventListener('click', action);
    node.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); action(); }
    });
    return node;
}

function smallCard(title, lines, action) {
    const { el } = App;
    const body = el('small', { class: 'text-muted' });
    lines.filter(Boolean).forEach((line, index) => {
        if (index) body.append(el('br'));
        body.append(line);
    });
    const card = el('div', { class: 'card mb-2' },
        el('div', { class: 'card-body p-2' }, el('h6', { class: 'card-title mb-1' }, title), body));
    return action ? clickable(card, action) : card;
}

function fillList(id, nodes, emptyMessage) {
    const { el } = App;
    document.getElementById(id).replaceChildren(
        ...(nodes.length ? nodes : [el('p', { class: 'text-muted mb-0' }, emptyMessage)]));
}

function displayAdvancedSummary(summary) {
    const { el } = App;
    const stat = (value, label) => el('div', { class: 'col-6 col-md-4 col-xl-2' },
        el('div', { class: 'stat-card' }, el('div', { class: 'stat-value' }, value), el('div', { class: 'stat-label' }, label)));
    const stats = [];
    if (known(summary.moving_s)) stats.push(stat(formatDuration(summary.moving_s), 'En mouvement'));
    if (known(summary.stopped_s)) stats.push(stat(formatDuration(summary.stopped_s), "À l'arrêt"));
    if (known(summary.avg_moving_speed_kmh)) stats.push(stat(`${fixed(summary.avg_moving_speed_kmh, 1)} km/h`, 'Vitesse moy. en mouvement'));
    if (known(summary.max_speed_kmh)) stats.push(stat(`${fixed(summary.max_speed_kmh, 1)} km/h`, 'Vitesse de pointe'));
    if (known(summary.ascent_m)) stats.push(stat(`+${fixed(summary.ascent_m, 0)} m`, 'Dénivelé positif'));
    if (known(summary.descent_m)) stats.push(stat(`-${fixed(summary.descent_m, 0)} m`, 'Dénivelé négatif'));
    const notes = (summary.notes || []).map((note) => el('div', { class: 'small text-muted' },
        el('i', { class: 'fas fa-circle-info me-1' }), String(note)));
    document.getElementById('advancedSummary').replaceChildren(
        ...(stats.length ? [el('div', { class: 'row g-2 mb-2' }, stats)] : []), ...notes);
}

function displayStops(stops, unavailable) {
    document.getElementById('stopsCount').textContent = stops.length;
    fillList('stopsList', stops.map((stop, index) => {
        const clock = formatClock(stop.start_time);
        const duration = known(stop.duration_s)
            ? `Durée : ${formatDuration(stop.duration_s)}`
            : `Durée inconnue (trace non horodatée, ${stop.points_count} points)`;
        return smallCard(`Arrêt ${index + 1}`, [
            duration,
            `Au km ${fixed(stop.distance_km, 2)}${clock ? ` · ${clock}` : ''}`,
            `Position : ${fixed(stop.lat, 6)}, ${fixed(stop.lng, 6)}`,
        ], () => focusOnMap({ coordinates: stop.coordinates }, '#e67e22'));
    }), unavailable || 'Aucun arrêt détecté');
}

function displaySegments(segments, unavailable) {
    const { el } = App;
    document.getElementById('segmentsCount').textContent = segments.length;
    const counters = {};
    fillList('segmentsList', segments.map((segment) => {
        const type = SEGMENT_TYPES[segment.type] || SEGMENT_TYPES.flat;
        counters[type.label] = (counters[type.label] || 0) + 1;
        const title = el('span', {}, el('i', { class: `fas fa-${type.icon} me-2` }), `${type.label} ${counters[type.label]}`);
        title.firstChild.style.color = type.color;
        const speed = known(segment.avg_speed_kmh) ? `Vitesse moy. : ${fixed(segment.avg_speed_kmh, 1)} km/h` : null;
        const duration = known(segment.duration_s) ? `Durée : ${formatDuration(segment.duration_s)}` : null;
        return smallCard(title, [
            `${fixed(segment.distance_km, 2)} km · ${signed(segment.elevation_change, 0)} m · ${fixed(segment.avg_slope, 1)} % en moyenne`
                + (segment.type !== 'flat' && known(segment.max_slope) ? ` (max ${fixed(segment.max_slope, 1)} %)` : ''),
            `Du km ${fixed(segment.start_km, 2)} (${fixed(segment.start_elevation, 0)} m) au km ${fixed(segment.end_km, 2)} (${fixed(segment.end_elevation, 0)} m)`,
            [speed, duration].filter(Boolean).join(' · '),
        ], () => focusOnMap(segment, type.color));
    }), unavailable || 'Aucun segment détecté');
}

// Barre de répartition : le texte reste hors de la barre pour rester lisible à 0 %.
function shareBar(percent, color, badgeClass) {
    const { el } = App;
    const bar = el('div', { class: `progress-bar ${badgeClass || ''}`, role: 'progressbar',
        'aria-valuenow': fixed(percent, 0), 'aria-valuemin': 0, 'aria-valuemax': 100 });
    bar.style.width = `${Math.max(0, Math.min(100, Number(percent) || 0))}%`;
    if (color) bar.style.backgroundColor = color;
    return el('div', { class: 'progress advanced-share' }, bar);
}

function displaySpeedZones(speedZones, unavailable) {
    const { el } = App;
    const zones = (speedZones && speedZones.zones) || [];
    const rows = zones.map((zone) => {
        const badge = el('span', { class: 'badge me-2' }, zone.label || zone.type);
        badge.style.backgroundColor = zone.color;
        const details = [
            known(zone.time_s) ? formatDuration(zone.time_s) : null,
            `${fixed(zone.distance_km, 2)} km`,
            known(zone.avg_speed) ? `moy. ${fixed(zone.avg_speed, 1)} km/h` : null,
        ].filter(Boolean).join(' · ');
        return el('div', { class: 'mb-3' },
            el('div', { class: 'd-flex justify-content-between align-items-baseline mb-1' },
                el('span', {}, badge, `${fixed(zone.min_speed, 1)} – ${fixed(zone.max_speed, 1)} km/h`),
                el('strong', {}, `${fixed(zone.percent, 0)} %`)),
            shareBar(zone.percent, zone.color),
            el('small', { class: 'text-muted' }, details));
    });
    if (rows.length) {
        rows.unshift(el('p', { class: 'small text-muted' }, speedZones.basis === 'time'
            ? 'Part du temps en mouvement dans chaque tiers de la vitesse de pointe (arrêts exclus).'
            : 'Part des points en mouvement dans chaque tiers de la vitesse de pointe (trace non horodatée).'));
    }
    fillList('speedZonesList', rows, unavailable || 'Aucune vitesse exploitable');
}

function slopeSectionsTable(sections) {
    const { el } = App;
    const shown = sections.slice(0, 100);
    const rows = shown.map((section) => clickable(el('tr', {},
        el('td', {}, `${fixed(section.start_km, 2)} – ${fixed(section.end_km, 2)}`),
        el('td', {},
            el('span', { class: `badge ${terrainBadge(section.type)} me-1` }, terrainLabel(section.type)),
            el('i', { class: `fas fa-${DIRECTION_ICONS[section.direction] || DIRECTION_ICONS.flat}` })),
        el('td', { class: 'text-end' }, `${fixed(section.distance_m, 0)} m`),
        el('td', { class: 'text-end' }, `${signed(section.avg_slope, 1)} %`),
        el('td', { class: 'text-end' }, `${signed(section.elevation_change, 0)} m`)),
    () => focusOnMap({ bounds: section.bounds })));
    if (sections.length > shown.length) {
        rows.push(el('tr', {}, el('td', { colspan: 5, class: 'text-center text-muted' },
            `… et ${sections.length - shown.length} autres tronçons`)));
    }
    return el('div', { class: 'table-responsive' },
        el('table', { class: 'table table-sm table-hover align-middle mb-0' },
            el('thead', {}, el('tr', {},
                el('th', {}, 'Km'), el('th', {}, 'Pente'), el('th', { class: 'text-end' }, 'Longueur'),
                el('th', { class: 'text-end' }, 'Moyenne'), el('th', { class: 'text-end' }, 'Dénivelé'))),
            el('tbody', {}, rows)));
}

function displayTerrainAnalysis(terrain, unavailable) {
    const { el } = App;
    const container = document.getElementById('terrainAnalysis');
    const summary = terrain && terrain.terrain_analysis;
    if (!summary) {
        container.replaceChildren(el('p', { class: 'text-muted mb-0' },
            unavailable || "Aucune donnée d'analyse de terrain disponible"));
        return;
    }
    const stat = (value, label) => el('div', { class: 'col-4' },
        el('div', { class: 'stat-card' },
            el('div', { class: 'stat-value' }, `${signed(value, 1)} %`),
            el('div', { class: 'stat-label' }, label)));
    const blocks = [el('div', { class: 'row g-2 mb-3' },
        stat(terrain.avg_slope, 'Pente moyenne'),
        stat(terrain.max_slope, 'Montée la plus raide'),
        stat(terrain.min_slope, 'Descente la plus raide'))];

    const distribution = summary.terrain_distribution || {};
    blocks.push(el('div', { class: 'card mb-3' }, el('div', { class: 'card-body p-3' },
        el('h6', { class: 'card-title mb-1' }, 'Répartition des pentes'),
        el('p', { class: 'small text-muted' },
            `En distance, sur un profil lissé (pentes mesurées sur 100 m). Terrain dominant : ${terrainLabel(summary.dominant_terrain).toLowerCase()}.`),
        Object.keys(TERRAIN_LABELS).map((type) => {
            const share = distribution[type] || {};
            return el('div', { class: 'mb-2' },
                el('div', { class: 'd-flex justify-content-between small mb-1' },
                    el('span', {}, el('span', { class: `badge ${terrainBadge(type)} me-2` }, terrainLabel(type)), TERRAIN_RANGES[type]),
                    el('span', {}, `${fixed(share.percent, 0)} % · ${fixed(share.distance_km, 2)} km`)),
                shareBar(share.percent, null, terrainBadge(type)));
        }))));

    const sections = terrain.slope_segments || [];
    if (sections.length) {
        blocks.push(el('div', { class: 'card' }, el('div', { class: 'card-body p-3' },
            el('h6', { class: 'card-title mb-2' }, `Tronçons de pente homogène (${sections.length})`),
            slopeSectionsTable(sections))));
    }
    container.replaceChildren(...blocks);
}

function displayPointsOfInterest(pois) {
    const { el } = App;
    document.getElementById('poiCount').textContent = pois.length;
    fillList('poiList', pois.map((poi) => {
        const type = POI_TYPES[poi.type] || POI_TYPES.turn;
        const icon = el('i', { class: `fas fa-${type.icon} me-2` });
        icon.style.color = type.color;
        return clickable(el('div', { class: 'd-flex justify-content-between align-items-center border-bottom py-1 px-1' },
            el('span', {}, icon, String(poi.description || poi.type)),
            el('small', { class: 'text-muted' }, `km ${fixed(poi.distance_km, 2)}`)),
        () => focusOnMap({ coordinates: poi.coordinates }, type.color));
    }), 'Aucun point remarquable détecté');
}

// ------------------------------------------- Conservation de l'analyse avancée
// Le résultat est gardé pour ne pas relancer le calcul à chaque fois :
// - activité de la bibliothèque non modifiée : enregistré côté serveur (calculé depuis le
//   fichier d'origine, sans renvoyer la trace) et relu à chaque ouverture ;
// - tout autre fichier, ou trace modifiée dans l'éditeur : gardé en mémoire le temps de
//   la session, pour retrouver le résultat en changeant de fichier.
// « Relancer » recalcule et remplace ; « Effacer » supprime le résultat, enregistré ou non.

const savedAnalysisUrl = (file) => `/api/activities/${file.activityId}/analysis`;
const usesSavedAnalysis = (file) => Boolean(file && file.activityId && !file.edited);

function renderAdvancedAnalysis(analysis) {
    const summary = analysis.summary || {};
    const unavailable = summary.unavailable || {};
    clearAdvancedHighlight();
    displayAdvancedSummary(summary);
    displayStops(analysis.stops || [], unavailable.stops);
    displaySegments(analysis.segments || [], unavailable.segments);
    displaySpeedZones(analysis.speed_zones, unavailable.speed_zones);
    displayTerrainAnalysis(analysis.terrain, unavailable.terrain);
    displayPointsOfInterest(analysis.points_of_interest || []);
    setVisible('advancedResults', true);
}

// Met les boutons et la ligne d'état en accord avec l'état du fichier actif : pendant un
// calcul, ni relance ni effacement (effacer puis voir le calcul réenregistrer son résultat
// laisserait l'écran et le serveur en désaccord).
function updateAdvancedControls() {
    const file = Viewer.activeFile();
    const result = file && file.advanced;
    const computing = Boolean(file && file.computing);
    document.getElementById('runAdvancedLabel').textContent = result ? "Relancer l'analyse" : "Lancer l'analyse avancée";
    document.getElementById('runAdvancedBtn').disabled = computing;
    document.getElementById('clearAdvancedBtn').disabled = !result || computing;
    let status = '';
    if (computing) {
        status = 'Calcul en cours…';
    } else if (result) {
        const when = result.computedAt ? App.dateTime(result.computedAt) : null;
        if (result.saved) status = `Analyse enregistrée${when ? ` le ${when}` : ''}.`;
        else if (file.edited) status = 'Trace modifiée : résultat conservé pour cette session, non enregistré.';
        else status = 'Résultat conservé pour cette session.';
        status += ' Cliquez sur un résultat pour le voir sur la carte.';
    }
    document.getElementById('advancedStatus').textContent = status;
}

function showAdvancedResult(file) {
    if (file && file.advanced) renderAdvancedAnalysis(file.advanced.analysis);
    else {
        clearAdvancedHighlight();
        setVisible('advancedResults', false);
    }
    updateAdvancedControls();
}

// À chaque changement de fichier actif (ou de ses données) : réaffiche le résultat déjà
// connu, sinon va chercher celui enregistré pour l'activité. Ne lance jamais de calcul.
async function restoreAdvancedAnalysis() {
    const request = ++advancedRequest; // une réponse encore en route sera ignorée
    const file = Viewer.activeFile();
    showAdvancedResult(file);
    // Calcul en cours : son résultat arrive, inutile d'aller lire celui qu'il va remplacer
    if (!file || file.advanced || file.computing || !usesSavedAnalysis(file)) return;
    try {
        const data = await App.api(savedAnalysisUrl(file));
        if (request !== advancedRequest || !data.analysis) return;
        if (file.advanced || file.computing || !usesSavedAnalysis(file)) return;
        file.advanced = { analysis: data.analysis, computedAt: data.computed_at, saved: true };
        showAdvancedResult(file);
    } catch (error) {
        // Pas bloquant : l'analyse reste relançable à la main
        console.warn("Lecture de l'analyse enregistrée impossible :", error);
    }
}

// Le calcul appartient au fichier analysé (file.computing), pas à l'écran : son résultat
// est conservé même si l'utilisateur change de fichier ou si le rattachement à la
// bibliothèque arrive entre-temps, et il est affiché dès que ce fichier est à l'écran.
async function runAdvancedAnalysis() {
    const file = Viewer.activeFile();
    if (!file || !Viewer.hasData()) {
        showAlert('Aucune trace chargée', 'warning');
        return;
    }
    if (file.computing) return;
    const saved = usesSavedAnalysis(file);
    const revision = file.revision;
    file.computing = true;
    updateAdvancedControls();
    showAlert('Analyse avancée en cours…', 'info', { spinner: true });
    let failure = null;
    let stale = false;
    try {
        const data = saved
            ? await App.api(savedAnalysisUrl(file), { method: 'POST' })
            : await App.api('/api/analysis/advanced', { method: 'POST', body: analysisPayload(file.data) });
        // Périmé si la trace a été modifiée pendant le calcul
        stale = revision !== file.revision;
        if (!stale) {
            file.advanced = { analysis: data.analysis || {}, saved,
                computedAt: data.computed_at || new Date().toISOString() };
        }
    } catch (error) {
        failure = error;
    } finally {
        file.computing = false;
    }
    if (!Viewer.files.has(file.id)) return; // fichier fermé pendant le calcul
    if (Viewer.activeFileId === file.id) showAdvancedResult(file);
    if (failure) showAlert(`Analyse avancée de ${file.name} impossible : ${failure.message}`, 'danger');
    else if (stale) showAlert('Trace modifiée pendant le calcul : relance l\'analyse avancée', 'warning');
    else showAlert(saved ? 'Analyse avancée terminée et enregistrée' : 'Analyse avancée terminée', 'success');
}

async function clearAdvancedAnalysis() {
    const file = Viewer.activeFile();
    if (file && file.computing) return;
    advancedRequest += 1; // une lecture encore en route sera ignorée
    const wasSaved = Boolean(file && file.advanced && file.advanced.saved);
    if (file) file.advanced = null;
    showAdvancedResult(file);
    if (wasSaved && usesSavedAnalysis(file)) {
        try {
            await App.api(savedAnalysisUrl(file), { method: 'DELETE' });
        } catch (error) {
            showAlert(`Analyse masquée, mais toujours enregistrée : ${error.message}`, 'warning');
            return;
        }
    }
    showAlert('Analyse avancée effacée', 'info');
}

function setupAnalysis() {
    document.getElementById('runAdvancedBtn').addEventListener('click', runAdvancedAnalysis);
    document.getElementById('clearAdvancedBtn').addEventListener('click', clearAdvancedAnalysis);
    updateAdvancedControls();
    // Grille, axes et infobulles prennent leurs couleurs à la création du graphique :
    // au changement de thème, on les recrée à partir de l'analyse déjà chargée.
    document.addEventListener('themechange', () => {
        if (Viewer.analysis && Viewer.hasData()) displayAnalysisResults(Viewer.analysis);
    });
}
