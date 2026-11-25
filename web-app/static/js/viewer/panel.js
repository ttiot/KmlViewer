// Visualiseur : panneau de l'activité (sélecteur de bibliothèque, titre, type de trace,
// mesures sous le curseur) et bandeau de profil lié à la carte.
'use strict';

const Panel = {
    strip: null,
    series: 'elevation',   // courbe affichée : 'elevation' | 'speed'
    profiles: {},          // { elevation: { x, y }, speed: { x, y } } tirés de l'analyse en cours
    path: null,            // trace du fichier actif : { lat, lon, cum } (distance cumulée en m)
    t: null,               // position du curseur le long de la trace (0 à 1), null si aucune
    cursorMarker: null,
    metaRequest: 0,
};

const SERIES = {
    elevation: { color: '--viz-1', fill: '--viz-1-soft', unit: 'm' },
    speed: { color: '--viz-2', fill: '--viz-2-soft', unit: 'km/h' },
};
const LIBRARY_PAGE = 500; // plafond de `limit` côté API

const finite = (value) => value !== null && value !== undefined && Number.isFinite(Number(value));

function haversineMeters(a, b) {
    const rad = Math.PI / 180;
    const dLat = (b[0] - a[0]) * rad, dLon = (b[1] - a[1]) * rad;
    const h = Math.sin(dLat / 2) ** 2 + Math.cos(a[0] * rad) * Math.cos(b[0] * rad) * Math.sin(dLon / 2) ** 2;
    return 2 * 6371000 * Math.asin(Math.min(1, Math.sqrt(h)));
}

// Trace du fichier : ses lignes mises bout à bout, avec la distance cumulée en chaque point.
// Rien n'est mesuré d'une ligne à la suivante (comme le fait le serveur).
function buildTrackPath(file) {
    const lat = [], lon = [], cum = [];
    let total = 0;
    (file ? file.data.features : []).forEach((feature) => {
        if (!feature || !LINE_TYPES.includes(feature.type) || !Array.isArray(feature.coordinates)) return;
        let previous = null;
        feature.coordinates.forEach((coord) => {
            if (!validPosition(coord)) return;
            if (previous) total += haversineMeters(previous, coord);
            previous = coord;
            lat.push(coord[0]); lon.push(coord[1]); cum.push(total);
        });
    });
    return lat.length >= 2 && total > 0 ? { lat, lon, cum, total } : null;
}

// Plus grand indice i tel que xs[i] <= x (xs croissant)
function lowerIndex(xs, x) {
    let low = 0, high = xs.length - 1;
    while (low < high) {
        const middle = (low + high + 1) >> 1;
        if (xs[middle] <= x) low = middle; else high = middle - 1;
    }
    return low;
}

function interpolate(profile, t) {
    if (!profile) return null;
    const { x, y } = profile;
    const target = t * x[x.length - 1];
    const i = lowerIndex(x, target);
    if (i >= x.length - 1) return y[x.length - 1];
    const span = x[i + 1] - x[i];
    return span > 0 ? y[i] + (y[i + 1] - y[i]) * (target - x[i]) / span : y[i];
}

// Position sur la carte et rang du point de trace le plus proche, à la fraction t de la trace
function pathAt(t) {
    const path = Panel.path;
    if (!path) return null;
    const target = t * path.total;
    const i = lowerIndex(path.cum, target);
    if (i >= path.cum.length - 1) return { latlng: [path.lat[i], path.lon[i]], index: i };
    const span = path.cum[i + 1] - path.cum[i];
    const u = span > 0 ? (target - path.cum[i]) / span : 0;
    return {
        latlng: [path.lat[i] + (path.lat[i + 1] - path.lat[i]) * u, path.lon[i] + (path.lon[i + 1] - path.lon[i]) * u],
        index: u < 0.5 ? i : i + 1,
    };
}

function updateCursorMarker(position) {
    const file = Viewer.activeFile();
    const shown = Boolean(position && file && file.visible);
    if (!shown) {
        if (Panel.cursorMarker) Viewer.map.removeLayer(Panel.cursorMarker);
        return;
    }
    if (!Panel.cursorMarker) {
        Panel.cursorMarker = L.marker(position.latlng, {
            icon: L.divIcon({ className: 'map-cursor', iconSize: [16, 16], iconAnchor: [8, 8] }),
            interactive: false, keyboard: false, zIndexOffset: 900,
        });
    }
    Panel.cursorMarker.setLatLng(position.latlng);
    if (!Viewer.map.hasLayer(Panel.cursorMarker)) Panel.cursorMarker.addTo(Viewer.map);
}

// Déplace le curseur : profil, réglette, mesures « sous le curseur » et repère sur la carte.
function setProfileCursor(t) {
    const { number } = App;
    Panel.t = finite(t) ? Math.min(1, Math.max(0, Number(t))) : null;
    const known = Panel.t !== null;
    const set = (id, text) => { document.getElementById(id).textContent = text; };

    const reference = Panel.profiles[Panel.series] || Panel.profiles.elevation || Panel.profiles.speed;
    const total = reference ? reference.x[reference.x.length - 1] : (Panel.path ? Panel.path.total : 0);
    const altitude = known ? interpolate(Panel.profiles.elevation, Panel.t) : null;
    const speed = known ? interpolate(Panel.profiles.speed, Panel.t) : null;
    const position = known ? pathAt(Panel.t) : null;

    const km = known && total ? `${number(Panel.t * total / 1000, 1)} km` : '—';
    const altitudeText = finite(altitude) ? `${number(altitude)} m` : '—';
    const speedText = finite(speed) ? `${number(speed, 1)} km/h` : '—';
    set('cursorDistance', km);
    set('cursorAltitude', altitudeText);
    set('cursorSpeed', speedText);
    set('cursorPosition', position ? `${position.latlng[0].toFixed(4)}°, ${position.latlng[1].toFixed(4)}°` : '—');
    set('cursorPoint', position ? `${number(position.index + 1)} / ${number(Panel.path.cum.length)}` : '—');
    const readout = known ? [`km ${km.replace(' km', '')}`,
        finite(altitude) ? altitudeText : null, finite(speed) ? speedText : null].filter(Boolean).join(' · ') : '';
    set('stripReadout', readout || 'Survole le profil pour lire une position.');

    if (Panel.strip) Panel.strip.setCursor(Panel.t, readout);
    updateCursorMarker(position);
}

function setProfileSeries(name) {
    if (!SERIES[name]) return;
    Panel.series = name;
    document.querySelectorAll('#seriesSwitch button').forEach((button) => {
        button.setAttribute('aria-pressed', button.dataset.series === name ? 'true' : 'false');
    });
    const profile = Panel.profiles[name];
    if (profile) Panel.strip.setSeries(profile.x, profile.y, SERIES[name]);
    else Panel.strip.clear(name === 'speed' ? 'Cette trace ne porte pas de vitesse.' : 'Cette trace ne porte pas d\'altitude.');
    setProfileCursor(Panel.t);
}

function profileOf(points, field) {
    const usable = (points || []).filter((point) => point && finite(point.distance) && finite(point[field]));
    return usable.length >= 2 && usable[usable.length - 1].distance > 0
        ? { x: usable.map((point) => Number(point.distance)), y: usable.map((point) => Number(point[field])) }
        : null;
}

// Le profil vient d'apparaître : la carte a perdu de la hauteur et la trace peut déborder
// par le bas. Si la vue est encore celle du cadrage automatique, on recadre ; si
// l'utilisateur a déplacé ou zoomé la carte entre-temps, on n'y touche pas.
function refitAfterResize() {
    Viewer.map.invalidateSize({ pan: false });
    if (Viewer.autoView) Viewer.map.fitBounds(Viewer.autoView, { padding: [20, 20], animate: false });
}

// Affiche le profil de l'analyse en cours (null : plus de trace à l'écran).
function renderProfile(analysis) {
    const section = document.getElementById('profileSection');
    Panel.path = analysis ? buildTrackPath(Viewer.activeFile()) : null;
    Panel.profiles = analysis ? {
        elevation: profileOf(analysis.elevation && analysis.elevation.elevation_profile, 'elevation'),
        speed: profileOf(analysis.speed && analysis.speed.speed_profile, 'speed_kmh'),
    } : {};
    const any = Boolean(Panel.profiles.elevation || Panel.profiles.speed);
    const appeared = any && section.hidden;
    section.hidden = !any;
    Panel.t = null;
    if (!any) { updateCursorMarker(null); return; }
    document.querySelectorAll('#seriesSwitch button').forEach((button) => {
        button.disabled = !Panel.profiles[button.dataset.series];
    });
    setProfileSeries(Panel.profiles[Panel.series] ? Panel.series : (Panel.profiles.elevation ? 'elevation' : 'speed'));
    // Après le dessin de la courbe : c'est lui qui donne au profil sa hauteur définitive
    if (appeared) refitAfterResize();
}

// ------------------------------------------------------- Panneau de l'activité

// Activité de bibliothèque d'un fichier ouvert (date, type) : lue une fois, à la demande.
async function loadActivityMeta(file) {
    if (!file || !file.activityId || file.activity) return;
    const request = ++Panel.metaRequest;
    try {
        const data = await App.api(`/api/activities/${file.activityId}`);
        file.activity = data.activity;
    } catch (error) {
        console.warn("Lecture de l'activité impossible :", error);
        return;
    }
    if (request === Panel.metaRequest && Viewer.activeFileId === file.id) updatePanel();
}

// Met le panneau en accord avec le fichier actif. Appelée à chaque changement de fichier
// actif et quand un fichier vient d'être rattaché à sa bibliothèque.
function updatePanel() {
    const file = Viewer.activeFile();
    setVisible('fileSummary', Boolean(file));
    setVisible('panelEmpty', !file);
    const select = document.getElementById('activitySelect');
    const libraryId = file && file.activityId ? String(file.activityId) : '';
    select.value = Array.from(select.options).some((option) => option.value === libraryId) ? libraryId : '';
    if (!file) return;

    document.getElementById('fileTitle').textContent = file.name;
    const activity = file.activity || null;
    const count = file.data.features.reduce((sum, feature) => sum
        + (feature && LINE_TYPES.includes(feature.type) && Array.isArray(feature.coordinates) ? feature.coordinates.length : 0), 0)
        || file.data.points.length;
    const parts = [];
    if (activity && activity.start_time) {
        parts.push(new Date(activity.start_time).toLocaleDateString('fr-FR', { day: 'numeric', month: 'long', year: 'numeric' })
            + (activity.start_time_known === false ? ' (date d\'import)' : ''));
    }
    parts.push(`${App.number(count)} point${count > 1 ? 's' : ''}`);
    if (file.data.dem_pending) parts.push('altitude en cours de calcul, rouvre la trace dans un instant');   // correction MNT pas encore faite
    document.getElementById('fileMeta').textContent = parts.join(' · ');

    const type = document.getElementById('typeSelect');
    type.disabled = !activity;
    type.value = activity && activity.activity_type ? activity.activity_type : '';
    document.getElementById('typeHint').textContent = file.activityId
        ? 'Le type décide des records et des allures calculés.'
        : 'Enregistre la trace dans ta bibliothèque pour pouvoir la classer.';
    loadActivityMeta(file);
}

async function changeActivityType(value) {
    const file = Viewer.activeFile();
    if (!file || !file.activityId || !file.activity) return;
    const type = document.getElementById('typeSelect');
    try {
        await App.api(`/api/activities/${file.activityId}`, { method: 'PATCH', body: { activity_type: value || null } });
        file.activity.activity_type = value || null;
    } catch (error) {
        showAlert(`Type non enregistré : ${error.message}`, 'danger');
        if (Viewer.activeFileId === file.id) type.value = file.activity.activity_type || '';
    }
}

// Remplit le sélecteur avec les activités de la bibliothèque (les plus récentes d'abord).
async function loadLibraryOptions() {
    const select = document.getElementById('activitySelect');
    try {
        const { activities } = await App.api(`/api/activities?limit=${LIBRARY_PAGE}`);
        select.replaceChildren(
            App.el('option', { value: '' }, activities.length ? 'Choisir dans ma bibliothèque…' : 'Bibliothèque vide'),
            ...activities.map((a) => App.el('option', { value: a.id }, `${App.date(a.start_time)} · ${a.name}`)));
    } catch (error) {
        console.warn('Liste des activités indisponible :', error);
    }
    const file = Viewer.activeFile();
    const libraryId = file && file.activityId ? String(file.activityId) : '';
    select.value = Array.from(select.options).some((option) => option.value === libraryId) ? libraryId : '';
}

// Choix dans le sélecteur : le fichier déjà ouvert pour cette activité redevient actif,
// sinon l'activité est chargée depuis la bibliothèque.
function onActivityChosen(value) {
    if (!/^\d+$/.test(value)) { updatePanel(); return; }
    const open = Array.from(Viewer.files.values()).find((file) => String(file.activityId) === value);
    if (open) setActiveFile(open.id);
    else openLibraryActivity(value);
}

// Ouvre l'onglet d'outils demandé (boutons du panneau) et l'amène à l'écran.
function showTool(name) {
    const button = document.getElementById(`${name}-tab`);
    if (!button) return;
    bootstrap.Tab.getOrCreateInstance(button).show();
    document.getElementById('analysisPanel').scrollIntoView({ behavior: 'smooth', block: 'start' });
    button.focus({ preventScroll: true });
}

function setupPanel() {
    Panel.strip = ProfileStrip.create(document.getElementById('profileStrip'), { onPick: setProfileCursor });
    document.getElementById('seriesSwitch').addEventListener('click', (event) => {
        const button = event.target.closest('button[data-series]');
        if (button && !button.disabled) setProfileSeries(button.dataset.series);
    });
    document.getElementById('typeSelect').addEventListener('change', (event) => changeActivityType(event.target.value));
    document.getElementById('activitySelect').addEventListener('change', (event) => onActivityChosen(event.target.value));
    document.querySelectorAll('[data-tool]').forEach((button) => {
        button.addEventListener('click', () => showTool(button.dataset.tool));
    });
    updatePanel();
    loadLibraryOptions();
}
