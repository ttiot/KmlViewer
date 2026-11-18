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
        document.getElementById(id).textContent = value ? `${prefix}${value}${suffix}` : '-';
    });
    const minutes = (analysis.duration || {}).estimated_duration_minutes;
    document.getElementById('totalDuration').textContent = minutes ? `${Math.round(minutes)} min` : '-';
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
            speedProfile.map((point, index) => index + 1),
            speedProfile.map((point) => point.speed_kmh),
            { colorToken: '--viz-2', softToken: '--viz-2-soft', xTitle: 'Point', yTitle: 'Vitesse (km/h)', unit: 'km/h' });
    }
}

function setAnalysisPanelVisible(visible) {
    setVisible('analysisPanel', visible);
    setTimeout(() => Viewer.map && Viewer.map.invalidateSize(), 300);
}

// Analyse le fichier actif. Le panneau reste affiché même si l'analyse échoue :
// il porte aussi l'édition et l'export.
async function analyzeTrajectory() {
    const features = Viewer.features();
    if (features.length === 0) {
        setAnalysisPanelVisible(false);
        return;
    }
    setAnalysisPanelVisible(true);
    const request = ++analysisRequest;
    try {
        const data = await App.api('/api/analysis/trajectory', { method: 'POST', body: { features } });
        if (request !== analysisRequest) return; // un autre fichier a été activé entre-temps
        Viewer.analysis = data.analysis;
    } catch (error) {
        if (request !== analysisRequest) return;
        console.error("Analyse impossible :", error);
        Viewer.analysis = {};
    }
    displayAnalysisResults(Viewer.analysis);
}

// ------------------------------------------------------------ Analyse avancée

function smallCard(title, lines) {
    const { el } = App;
    const body = el('small', { class: 'text-muted' });
    lines.forEach((line, index) => {
        if (index) body.append(el('br'));
        body.append(line);
    });
    return el('div', { class: 'card mb-2' },
        el('div', { class: 'card-body p-2' }, el('h6', { class: 'card-title mb-1' }, title), body));
}

function fillList(id, cards, emptyMessage) {
    const { el } = App;
    document.getElementById(id).replaceChildren(
        ...(cards.length ? cards : [el('p', { class: 'text-muted' }, emptyMessage)]));
}

const fixed = (value, digits) => (Number.isFinite(Number(value)) ? Number(value).toFixed(digits) : '-');

const TERRAIN_LABELS = { flat: 'Plat', gentle: 'Doux', moderate: 'Modéré', steep: 'Raide' };
const TERRAIN_BADGES = { flat: 'bg-success', gentle: 'bg-info', moderate: 'bg-warning', steep: 'bg-danger' };
const terrainLabel = (type) => TERRAIN_LABELS[type] || String(type || '-');
const terrainBadge = (type) => TERRAIN_BADGES[type] || 'bg-secondary';

function displayStops(stops) {
    document.getElementById('stopsCount').textContent = stops.length;
    fillList('stopsList', stops.map((stop, index) => smallCard(`Arrêt ${index + 1}`, [
        `Durée : ${Math.round(stop.duration_minutes)} min`,
        `Position : ${fixed(stop.lat, 6)}, ${fixed(stop.lng, 6)}`,
    ])), 'Aucun arrêt détecté');
}

function displaySegments(segments) {
    document.getElementById('segmentsCount').textContent = segments.length;
    fillList('segmentsList', segments.map((segment, index) => smallCard(`Segment ${index + 1}`, [
        `Distance : ${fixed(segment.distance_km, 2)} km`,
        `Vitesse moy. : ${fixed(segment.avg_speed_kmh, 1)} km/h`,
        `Type : ${segment.type}`,
    ])), 'Aucun segment détecté');
}

function displaySpeedZones(speedZones) {
    const { el } = App;
    const zones = (speedZones && speedZones.zones) || [];
    fillList('speedZonesList', zones.map((zone) => {
        const badge = el('span', { class: 'badge' }, zone.type);
        badge.style.backgroundColor = zone.color;
        return smallCard(badge, [
            `Moyenne : ${fixed(zone.avg_speed, 2)} km/h`,
            `Min : ${fixed(zone.min_speed, 1)} km/h`,
            `Max : ${fixed(zone.max_speed, 1)} km/h`,
        ]);
    }), 'Aucune zone de vitesse détectée');
}

function slopeClass(value) {
    if (value > 0) return 'text-success';
    return value < 0 ? 'text-danger' : '';
}

function slopeSegmentItem(segment, index) {
    const { el } = App;
    const slopes = segment.slopes || [];
    const labelled = (label, value) => el('div', { class: 'col-4' },
        el('small', { class: 'text-muted' }, label), el('div', { class: 'fw-bold' }, value));

    let details = el('p', { class: 'text-muted' }, 'Aucun détail de pente disponible');
    if (slopes.length) {
        const rows = slopes.slice(0, 10).map((slope) => {
            const c = slope.coordinates;
            return el('tr', {},
                el('td', {}, slope.index || 0),
                el('td', {}, c ? `${fixed(c[0], 6)}, ${fixed(c[1], 6)}` : 'N/A'),
                el('td', {}, c && c[2] ? `${fixed(c[2], 1)} m` : 'N/A'),
                el('td', {}, slope.horizontal_distance ? `${fixed(slope.horizontal_distance, 1)} m` : 'N/A'),
                el('td', { class: slopeClass(slope.slope_degrees) }, `${fixed(slope.slope_degrees || 0, 1)}°`),
                el('td', { class: slopeClass(slope.slope_percent) }, `${fixed(slope.slope_percent || 0, 1)}%`));
        });
        if (slopes.length > 10) {
            rows.push(el('tr', {}, el('td', { colspan: 6, class: 'text-center text-muted' },
                el('small', {}, `… et ${slopes.length - 10} autres points`))));
        }
        details = el('div', { class: 'table-responsive' },
            el('table', { class: 'table table-sm table-striped' },
                el('thead', {}, el('tr', {}, ['Point', 'Coordonnées', 'Altitude', 'Distance H.', 'Pente (°)', 'Pente (%)']
                    .map((title) => el('th', {}, title)))),
                el('tbody', {}, rows)));
    }

    const collapseId = `slopeSegment${index}`;
    return el('div', { class: 'accordion-item' },
        el('h2', { class: 'accordion-header' },
            el('button', { class: 'accordion-button collapsed', type: 'button',
                'data-bs-toggle': 'collapse', 'data-bs-target': `#${collapseId}` },
            el('span', { class: `badge ${terrainBadge(segment.type)} me-2` }, terrainLabel(segment.type)),
            `Segment ${index + 1} - ${segment.length || 0} points - Pente moy. : ${fixed(segment.avg_slope || 0, 1)}%`)),
        el('div', { id: collapseId, class: 'accordion-collapse collapse', 'data-bs-parent': '#slopeSegmentsAccordion' },
            el('div', { class: 'accordion-body' },
                el('div', { class: 'row g-2 mb-3' },
                    labelled('Type :', terrainLabel(segment.type)),
                    labelled('Longueur :', `${segment.length || 0} points`),
                    labelled('Pente moyenne :', `${fixed(segment.avg_slope || 0, 1)}%`)),
                details)));
}

function displayTerrainAnalysis(terrain) {
    const { el } = App;
    const container = document.getElementById('terrainAnalysis');
    if (!terrain) {
        container.replaceChildren(el('p', { class: 'text-muted' }, "Aucune donnée d'analyse de terrain disponible"));
        return;
    }
    const stat = (value, label, extra = '') => el('div', { class: 'col-4' },
        el('div', { class: 'stat-card' },
            el('div', { class: `stat-value ${extra}` }, `${fixed(value || 0, 1)}%`),
            el('div', { class: 'stat-label' }, label)));
    const blocks = [el('div', { class: 'row g-2 mb-3' },
        stat(terrain.avg_slope, 'Pente moyenne'),
        stat(terrain.max_slope, 'Pente max', 'text-success'),
        stat(terrain.min_slope, 'Pente min', 'text-danger'))];

    const summary = terrain.terrain_analysis;
    if (summary) {
        const distribution = summary.terrain_distribution;
        blocks.push(el('div', { class: 'card mb-3' }, el('div', { class: 'card-body p-3' },
            el('h6', { class: 'card-title mb-2' }, 'Résumé du terrain'),
            el('div', { class: 'row g-2' },
                el('div', { class: 'col-6' }, el('small', { class: 'text-muted' }, 'Terrain dominant :'),
                    el('div', { class: 'fw-bold text-primary' }, terrainLabel(summary.dominant_terrain))),
                el('div', { class: 'col-6' }, el('small', { class: 'text-muted' }, 'Points analysés :'),
                    el('div', { class: 'fw-bold' }, summary.total_points || 0))),
            distribution ? el('div', { class: 'mt-3' },
                el('small', { class: 'text-muted d-block mb-2' }, 'Distribution du terrain :'),
                el('div', { class: 'row g-1' }, Object.keys(TERRAIN_LABELS).map((type) =>
                    el('div', { class: 'col-3 text-center' },
                        el('div', { class: `badge ${terrainBadge(type)}` }, distribution[type] || 0),
                        el('small', { class: 'd-block' }, terrainLabel(type)))))) : null)));
    }

    const segments = terrain.slope_segments || [];
    if (segments.length) {
        blocks.push(el('div', { class: 'card' }, el('div', { class: 'card-body p-3' },
            el('h6', { class: 'card-title mb-3' }, `Segments de pente (${segments.length})`),
            el('div', { class: 'accordion', id: 'slopeSegmentsAccordion' }, segments.map(slopeSegmentItem)))));
    }
    container.replaceChildren(el('div', { class: 'mb-4' }, blocks));
}

async function runAdvancedAnalysis() {
    if (!Viewer.hasData()) {
        showAlert('Aucune trace chargée', 'warning');
        return;
    }
    showAlert('Analyse avancée en cours…', 'info', { spinner: true });
    try {
        const data = await App.api('/api/analysis/advanced', {
            method: 'POST', body: { features: Viewer.features(), points: Viewer.points() } });
        const analysis = data.analysis || {};
        setVisible('advancedResults', true);
        displayStops(analysis.stops || []);
        displaySegments(analysis.segments || []);
        displaySpeedZones(analysis.speed_zones);
        displayTerrainAnalysis(analysis.terrain);
        showAlert('Analyse avancée terminée', 'success');
    } catch (error) {
        showAlert(`Analyse avancée impossible : ${error.message}`, 'danger');
    }
}

function clearAdvancedAnalysis({ silent = false } = {}) {
    setVisible('advancedResults', false);
    if (!silent) showAlert('Analyse avancée effacée', 'info');
}

function setupAnalysis() {
    document.getElementById('runAdvancedBtn').addEventListener('click', runAdvancedAnalysis);
    document.getElementById('clearAdvancedBtn').addEventListener('click', () => clearAdvancedAnalysis());
    // Grille, axes et infobulles prennent leurs couleurs à la création du graphique :
    // au changement de thème, on les recrée à partir de l'analyse déjà chargée.
    document.addEventListener('themechange', () => {
        if (Viewer.analysis && Viewer.hasData()) displayAnalysisResults(Viewer.analysis);
    });
}
