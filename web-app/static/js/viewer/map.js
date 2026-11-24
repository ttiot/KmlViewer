// Visualiseur : carte, styles KML et construction des couches d'un fichier.
'use strict';

const LINE_TYPES = ['polyline', 'track', 'multitrack'];
const METERS_PER_DEGREE = 111000; // 1° ≈ 111 km : ordre de grandeur, pas une mesure

function initMap() {
    // Zoom en haut à droite, au-dessus du bouton des réglages
    Viewer.map = L.map('map', { zoomControl: false }).setView([46.603354, 1.888334], 6); // centre de la France
    L.control.zoom({ position: 'topright' }).addTo(Viewer.map);

    Viewer.baseLayers = {
        osm: L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
            maxZoom: 19, attribution: '© OpenStreetMap contributors' }),
        satellite: L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', {
            maxZoom: 19, attribution: '© Esri' }),
        terrain: L.tileLayer('https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png', {
            maxZoom: 17, attribution: '© OpenTopoMap contributors' }),
        cartodb: L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png', {
            maxZoom: 20, attribution: '© OpenStreetMap contributors © CARTO' }),
        toner: L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
            maxZoom: 20, attribution: '© OpenStreetMap contributors © CARTO' }),
    };
    setBaseLayer('osm');            // fonds intégrés en attendant /api/map-config (applyMapConfig)

    // Toute action de l'utilisateur sur la carte (glisser, molette, zoom, clavier) rend son
    // cadrage : la carte ne sera plus recadrée d'office.
    ['pointerdown', 'wheel', 'keydown'].forEach((type) => {
        Viewer.map.getContainer().addEventListener(type, () => { Viewer.autoView = null; }, { passive: true });
    });

    // La carte change de taille avec le panneau voisin (résumé affiché, fenêtre redimensionnée) :
    // Leaflet doit recalculer sa taille, sinon une bande grise reste affichée.
    if (typeof ResizeObserver !== 'undefined') {
        new ResizeObserver(() => Viewer.map.invalidateSize()).observe(Viewer.map.getContainer());
    }
}

// Applique la configuration serveur (T16) : fonds autorisés, fond par défaut, cadrage. Sans réponse, les fonds intégrés restent ;
// une liste VIDE (aucun hôte autorisé) retire tout fond : aucun navigateur ne contacte un hôte non autorisé.
async function applyMapConfig() {
    const config = await App.mapConfig();
    if (!config || !Array.isArray(config.basemaps)) return;
    Object.values(Viewer.baseLayers).forEach((layer) => Viewer.map.removeLayer(layer));
    Viewer.baseLayers = {};
    config.basemaps.forEach((basemap) => { Viewer.baseLayers[basemap.name] = App.basemapLayer(basemap); });
    const fieldset = document.querySelector('#mapSettingsPanel fieldset');
    if (fieldset) {
        const legend = fieldset.querySelector('legend');
        fieldset.replaceChildren(legend, ...config.basemaps.map((basemap, index) => {
            const id = `baseLayer-${index}`;
            return App.el('div', { class: 'form-check' },
                App.el('input', { class: 'form-check-input', type: 'radio', name: 'baseLayer', id, value: basemap.name,
                    checked: basemap.name === config.default }),
                App.el('label', { class: 'form-check-label', for: id }, basemap.name));
        }));
    }
    if (config.default && Viewer.baseLayers[config.default]) setBaseLayer(config.default);
    if (Array.isArray(config.center) && Number.isFinite(config.zoom)) Viewer.map.setView(config.center, config.zoom);
}

// Affiche le fond de carte demandé (nom inconnu : sans effet). Retourne true s'il est affiché.
function setBaseLayer(name) {
    const next = Object.prototype.hasOwnProperty.call(Viewer.baseLayers, name) ? Viewer.baseLayers[name] : null;
    if (!next) return false;
    Object.values(Viewer.baseLayers).forEach((layer) => { if (layer !== next) Viewer.map.removeLayer(layer); });
    if (!Viewer.map.hasLayer(next)) next.addTo(Viewer.map);
    return true;
}

// ----------------------------------------------------------------- Styles KML

const DEFAULT_STYLES = {
    marker: { color: '#4ecdc4', fillColor: '#4ecdc4', lineColor: '#4ecdc4', width: 2 },
    line: { color: '#c2410c', fillColor: '#c2410c', lineColor: '#c2410c', width: 3.5 },
    polygon: { color: '#28a745', fillColor: '#28a745', lineColor: '#28a745', width: 2 },
};

function defaultStyle(featureType) {
    if (LINE_TYPES.includes(featureType)) return DEFAULT_STYLES.line;
    return DEFAULT_STYLES[featureType] || DEFAULT_STYLES.marker;
}

// Couleur KML « aabbggrr » -> CSS « #rrggbb ». Toute valeur inattendue retombe sur `fallback` :
// la couleur vient du fichier, elle ne doit jamais pouvoir porter autre chose qu'une couleur.
function parseKmlColor(kmlColor, fallback) {
    const value = String(kmlColor || '').trim().replace(/^#/, '');
    if (/^[0-9a-f]{8}$/i.test(value)) {
        return `#${value.substring(6, 8)}${value.substring(4, 6)}${value.substring(2, 4)}`;
    }
    if (/^[0-9a-f]{6}$/i.test(value)) return `#${value}`;
    return fallback;
}

function getFeatureStyle(feature, metadata) {
    const base = defaultStyle(feature.type);
    const styles = metadata && metadata.styles;
    if (!feature.style || !styles) return base;
    const style = styles[String(feature.style).replace('#', '')];
    if (!style) return base;
    const width = parseInt(style.line_width, 10);
    return {
        color: parseKmlColor(style.line_color || style.poly_color, base.color),
        fillColor: parseKmlColor(style.poly_color, base.fillColor),
        lineColor: parseKmlColor(style.line_color, base.lineColor),
        width: Number.isFinite(width) && width > 0 ? Math.min(width, 12) : base.width,
    };
}

function markerIcon(color) {
    const dot = document.createElement('div');
    dot.className = 'marker-dot';
    dot.style.backgroundColor = color;
    return L.divIcon({ className: 'custom-marker', html: dot, iconSize: [16, 16], iconAnchor: [8, 8] });
}

// ------------------------------------------------------------------ Polygones

function outerRing(coordinates) {
    if (!Array.isArray(coordinates) || coordinates.length === 0) return null;
    const ring = Array.isArray(coordinates[0]) && Array.isArray(coordinates[0][0]) ? coordinates[0] : coordinates;
    return Array.isArray(ring) && ring.length >= 3 ? ring : null;
}

// Surface et périmètre approchés (1° ≈ 111 km) : ordre de grandeur, pas une mesure.
function polygonMetrics(coordinates) {
    const ring = outerRing(coordinates);
    if (!ring) return { area: null, perimeter: null };
    let area = 0, perimeter = 0;
    for (let i = 0; i < ring.length; i++) {
        const j = (i + 1) % ring.length;
        area += ring[i][0] * ring[j][1] - ring[j][0] * ring[i][1];
        perimeter += Math.hypot(ring[j][0] - ring[i][0], ring[j][1] - ring[i][1]);
    }
    return { area: Math.abs(area) / 2 * METERS_PER_DEGREE ** 2, perimeter: perimeter * METERS_PER_DEGREE };
}

function formatArea(area) {
    if (area < 1e6) return `${Math.round(area)} m²`;
    return `${(area / 1e6).toFixed(area < 1e9 ? 2 : 0)} km²`;
}

function buildPolygon(feature, style) {
    const coordinates = feature.coordinates;
    if (!Array.isArray(coordinates) || coordinates.length === 0) return null;
    let rings = null;
    if (Array.isArray(coordinates[0]) && Array.isArray(coordinates[0][0])) {
        // [contour extérieur, trou, trou…]
        rings = coordinates[0].length >= 3 ? coordinates : null;
    } else if (Array.isArray(coordinates[0]) && typeof coordinates[0][0] === 'number') {
        const holes = (Array.isArray(feature.holes) ? feature.holes : [])
            .filter((hole) => Array.isArray(hole) && hole.length >= 3);
        rings = coordinates.length >= 3 ? [coordinates, ...holes] : null;
    }
    if (!rings) return null;

    const rest = { color: style.lineColor, fillColor: style.fillColor, weight: style.width, opacity: 0.8, fillOpacity: 0.4 };
    let polygon;
    try {
        polygon = L.polygon(rings, rest);
    } catch (error) {
        console.warn('Polygone ignoré :', error);
        return null;
    }
    polygon.on('mouseover', () => polygon.setStyle({ weight: style.width + 1, opacity: 1, fillOpacity: 0.6 }));
    polygon.on('mouseout', () => polygon.setStyle(rest));

    const { el } = App;
    const { area, perimeter } = polygonMetrics(coordinates);
    const stats = el('small', { class: 'text-muted' }, el('i', { class: 'fas fa-draw-polygon me-1' }), 'Polygone');
    if (area) stats.append(el('br'), el('i', { class: 'fas fa-expand-arrows-alt me-1' }), `Surface : ~${formatArea(area)}`);
    if (perimeter) stats.append(el('br'), el('i', { class: 'fas fa-ruler me-1' }), `Périmètre : ~${App.distance(perimeter)}`);
    const description = descriptionNodes(feature.description);
    polygon.bindPopup(el('div', { class: 'popup-content' },
        el('strong', {}, feature.name || 'Polygone'),
        description.length ? el('div', { class: 'mb-2' }, description) : null,
        el('div', {}, stats),
        el('div', { class: 'mt-2' },
            el('button', { type: 'button', class: 'btn btn-sm btn-outline-primary',
                onclick: () => Viewer.map.fitBounds(polygon.getBounds(), { padding: [20, 20] }) },
            el('i', { class: 'fas fa-crosshairs' }), ' Centrer'))));
    return polygon;
}

// ------------------------------------------------------- Couches d'un fichier

function pointPopup(file, index) {
    const { el } = App;
    const point = file.data.points[index];
    const description = descriptionNodes(pointDescription(point));
    return el('div', { class: 'popup-content' },
        el('strong', {}, point.name || 'Point'), el('br'),
        description.length ? description : "Point d'intérêt", el('br'),
        point.altitude ? [el('small', {}, `Altitude : ${Math.round(point.altitude)} m`), el('br')] : null,
        el('small', {}, `Point ${index + 1}/${file.data.points.length}`),
        el('div', { class: 'mt-2' },
            el('button', { type: 'button', class: 'btn btn-sm btn-outline-primary',
                onclick: () => { setActiveFile(file.id); navigateToPoint(index); } },
            el('i', { class: 'fas fa-crosshairs' }), ' Centrer')));
}

// Construit le groupe Leaflet d'un fichier. Le k-ième marqueur des features correspond
// à points[k] ; les marqueurs masqués (points de trace GPX) ne sont pas dessinés.
function buildFileLayer(file) {
    const { el } = App;
    const data = file.data;
    const group = L.layerGroup();
    const bounds = L.latLngBounds();
    const markers = [];
    let pointIndex = -1;

    data.features.forEach((feature) => {
        const style = getFeatureStyle(feature, data.metadata);
        let layer = null;

        if (LINE_TYPES.includes(feature.type) && Array.isArray(feature.coordinates) && feature.coordinates.length) {
            // Liseré blanc sous le trait : la trace reste lisible sur n'importe quel fond de carte
            group.addLayer(L.polyline(feature.coordinates, { color: '#fff', weight: style.width + 3.5, interactive: false }));
            layer = L.polyline(feature.coordinates, { color: style.lineColor, weight: style.width, opacity: 0.95 });
            const description = descriptionNodes(feature.description);
            layer.bindPopup(el('div', { class: 'popup-content' },
                el('strong', {}, feature.name || 'Trace'), el('br'),
                description.length ? description : 'Trace GPS'));
        } else if (feature.type === 'polygon') {
            layer = buildPolygon(feature, style);
        } else if (feature.type === 'marker') {
            pointIndex += 1;
            const index = pointIndex;
            if (String(feature.visibility) === '0' || !data.points[index]) return;
            layer = L.marker(feature.coordinates, { icon: markerIcon(style.color) });
            layer.bindPopup(() => pointPopup(file, index));
            layer.on('click', () => onMarkerClick(file, index));
            markers[index] = layer;
        }

        if (!layer) return;
        group.addLayer(layer);
        try {
            bounds.extend(layer.getBounds ? layer.getBounds() : layer.getLatLng());
        } catch (error) { /* géométrie vide : sans effet sur l'emprise */ }
    });

    return { group, bounds, markers };
}

// (Re)dessine un fichier à partir de ses données, en conservant sa visibilité.
function renderFile(file, { fit = false } = {}) {
    if (file.layer) Viewer.map.removeLayer(file.layer);
    const { group, bounds, markers } = buildFileLayer(file);
    file.layer = group;
    file.markers = markers;
    if (file.visible) group.addTo(Viewer.map);
    if (fit && bounds.isValid()) {
        // Sans animation : un recadrage demandé pendant une animation de zoom serait écrasé
        // à la fin de celle-ci
        Viewer.map.fitBounds(bounds, { padding: [20, 20], animate: false });
        // Cadrage automatique : refait si la carte change de taille avant que l'utilisateur
        // y ait touché (apparition du profil, qui arrive avec l'analyse)
        Viewer.autoView = bounds;
    }
}
