// Visualiseur : navigation entre les points et liste des points du fichier actif.
'use strict';

function updateNavigationInfo() {
    const info = document.getElementById('navigationInfo');
    const points = Viewer.points();
    const point = points[Viewer.currentPointIndex];
    if (!point) {
        info.hidden = true;
        return;
    }
    const { el } = App;
    info.replaceChildren(
        el('strong', {}, `Point ${Viewer.currentPointIndex + 1}/${points.length}`), el('br'),
        el('small', {}, point.name || ''));
    info.hidden = false;
}

// ------------------------------------------------------------ Liste fenêtrée
// Une trace GPX compte un point par position enregistrée : plusieurs dizaines de milliers.
// Seule une fenêtre de la liste est donc présente dans le DOM ; deux espaceurs tiennent la
// place des lignes absentes, pour que la barre de défilement reflète toute la liste.

const POINTS_WINDOW = 300;       // lignes présentes dans le DOM
const POINTS_WINDOW_MARGIN = 50; // en deçà de cette réserve, la fenêtre est recentrée
const pointsView = { fileId: null, start: 0, end: 0, rowHeight: 76, measured: false, frame: 0 };

function pointsContent() {
    return document.getElementById('pointsListContent');
}

function pointItem(point, index, total) {
    const { el } = App;
    const description = descriptionNodes(pointDescription(point));
    const tracePoint = String(point.visibility) === '0'; // point de trace, non dessiné sur la carte
    return el('div', { class: `point-item${index === Viewer.currentPointIndex ? ' active' : ''}`, dataset: { pointIndex: index } },
        el('div', { class: 'd-flex align-items-start' },
            el('div', { class: 'me-3 mt-1' },
                el('div', { class: `point-dot ${tracePoint ? '' : 'annotation'}` })),
            el('div', { class: 'flex-grow-1' },
                el('div', { class: 'point-name' }, point.name || `Point ${index + 1}`),
                description.length ? el('div', { class: 'point-description' }, description) : null,
                el('div', { class: 'point-meta' },
                    el('span', { class: `badge ${tracePoint ? 'bg-info' : 'bg-warning'} point-badge me-2` },
                        tracePoint ? 'Point de trace' : 'Repère'),
                    el('small', { class: 'text-muted' }, `Point ${index + 1}/${total}`),
                    point.altitude
                        ? el('small', { class: 'text-muted ms-2' }, `Alt : ${Math.round(point.altitude)} m`) : null))));
}

function pointsSpacer(rows) {
    const spacer = document.createElement('div');
    spacer.setAttribute('aria-hidden', 'true');
    spacer.style.height = `${Math.max(0, rows) * pointsView.rowHeight}px`;
    return spacer;
}

// Met dans le DOM les POINTS_WINDOW lignes à partir de `start` (ramené dans les bornes).
function renderPointsWindow(start) {
    const points = Viewer.points();
    const content = pointsContent();
    const first = Math.max(0, Math.min(Math.floor(start), points.length - POINTS_WINDOW));
    const last = Math.min(points.length, first + POINTS_WINDOW);
    const items = [];
    for (let index = first; index < last; index++) items.push(pointItem(points[index], index, points.length));
    const top = pointsSpacer(first);
    const bottom = pointsSpacer(points.length - last);
    content.replaceChildren(top, ...items, bottom);
    pointsView.start = first;
    pointsView.end = last;

    // Hauteur réelle d'une ligne, mesurée une fois par liste (nulle tant que le panneau est masqué)
    if (!pointsView.measured && items.length) {
        const height = (bottom.getBoundingClientRect().top - items[0].getBoundingClientRect().top) / items.length;
        if (height > 0) {
            pointsView.rowHeight = height;
            pointsView.measured = true;
            top.style.height = `${first * height}px`;
            bottom.style.height = `${(points.length - last) * height}px`;
        }
    }
}

// Première ligne visible de la fenêtre : { index, offset (px) par rapport au haut de la liste }.
function firstVisiblePoint(content) {
    const box = content.getBoundingClientRect();
    const items = content.querySelectorAll('.point-item');
    let low = 0, high = items.length;
    while (low < high) { // première ligne dont le bas dépasse le haut de la zone visible
        const middle = (low + high) >> 1;
        if (items[middle].getBoundingClientRect().bottom > box.top) high = middle; else low = middle + 1;
    }
    if (low >= items.length) return null;
    const top = items[low].getBoundingClientRect().top;
    return top < box.bottom ? { index: Number(items[low].dataset.pointIndex), offset: top - box.top } : null;
}

// Au défilement : recentre la fenêtre quand la zone visible approche de l'un de ses bords,
// en gardant à l'écran la ligne qui s'y trouvait.
function syncPointsWindow() {
    const content = pointsContent();
    const total = Viewer.points().length;
    if (total <= POINTS_WINDOW) return;
    const anchor = firstVisiblePoint(content);
    // Aucune ligne visible : l'ascenseur a été déposé dans un espaceur, on estime la position
    const index = anchor ? anchor.index
        : Math.min(total - 1, Math.floor(content.scrollTop / pointsView.rowHeight));
    const visibleRows = Math.ceil(content.clientHeight / pointsView.rowHeight);
    const nearStart = pointsView.start > 0 && index - pointsView.start < POINTS_WINDOW_MARGIN;
    const nearEnd = pointsView.end < total && pointsView.end - index - visibleRows < POINTS_WINDOW_MARGIN;
    if (anchor && !nearStart && !nearEnd) return;
    renderPointsWindow(index - (POINTS_WINDOW - visibleRows) / 2);
    const node = content.querySelector(`.point-item[data-point-index="${index}"]`);
    if (node) {
        content.scrollTop += node.getBoundingClientRect().top - content.getBoundingClientRect().top
            - (anchor ? anchor.offset : 0);
    }
}

function onPointsScroll() {
    if (pointsView.frame) return;
    pointsView.frame = requestAnimationFrame(() => {
        pointsView.frame = 0;
        syncPointsWindow();
    });
}

function updatePointsListHighlight() {
    const content = pointsContent();
    content.querySelectorAll('.point-item.active').forEach((item) => item.classList.remove('active'));
    const index = Viewer.currentPointIndex;
    if (index < 0) return;
    // Le point choisi (carte, clavier) peut être hors de la fenêtre : on l'y amène d'abord
    const outside = index < pointsView.start || index >= pointsView.end;
    if (outside) renderPointsWindow(index - POINTS_WINDOW / 2);
    const active = content.querySelector(`.point-item[data-point-index="${index}"]`);
    if (active) {
        active.classList.add('active');
        active.scrollIntoView(outside ? { block: 'center' } : { behavior: 'smooth', block: 'nearest' });
    }
}

// Repère du point sélectionné sur la carte. Indispensable pour les points de trace (GPX),
// qui n'ont pas de marqueur : sans lui, rien ne distingue le point choisi sur la trace.
// Un seul repère, déplacé à chaque sélection ; il ne capte pas les clics.
function updateSelectionMarker() {
    const file = Viewer.activeFile();
    const point = Viewer.points()[Viewer.currentPointIndex];
    const coordinates = point && point.coordinates;
    const shown = Boolean(file && file.visible && Array.isArray(coordinates)
        && Number.isFinite(Number(coordinates[0])) && Number.isFinite(Number(coordinates[1])));
    if (!shown) {
        if (Viewer.selectionMarker) Viewer.map.removeLayer(Viewer.selectionMarker);
        return;
    }
    if (!Viewer.selectionMarker) {
        Viewer.selectionMarker = L.marker(coordinates, {
            icon: L.divIcon({ className: 'selection-marker', iconSize: [26, 26], iconAnchor: [13, 13] }),
            interactive: false, keyboard: false, zIndexOffset: 1000,
        });
    }
    Viewer.selectionMarker.setLatLng(coordinates);
    if (!Viewer.map.hasLayer(Viewer.selectionMarker)) Viewer.selectionMarker.addTo(Viewer.map);
}

function selectPoint(index) {
    if (index < 0 || index >= Viewer.points().length) return;
    Viewer.currentPointIndex = index;
    updateNavigationInfo();
    updatePointsListHighlight();
    updateSelectionMarker();
}

// Sélectionne un point, centre la carte dessus et ouvre son popup : celui de son marqueur
// s'il est dessiné, sinon un popup posé à sa position (point de trace).
function navigateToPoint(index) {
    const points = Viewer.points();
    if (index < 0 || index >= points.length) return;
    selectPoint(index);
    const file = Viewer.activeFile();
    const coordinates = points[index].coordinates;
    Viewer.map.setView(coordinates, Math.max(Viewer.map.getZoom(), 15));
    if (!file || !file.visible) return;
    const marker = file.markers[index];
    if (marker) {
        marker.openPopup();
    } else {
        L.popup({ offset: [0, -8] }).setLatLng(coordinates).setContent(pointPopup(file, index)).openOn(Viewer.map);
    }
}

function navigateToNextPoint() {
    const count = Viewer.points().length;
    if (count) navigateToPoint((Viewer.currentPointIndex + 1) % count);
}

function navigateToPreviousPoint() {
    const count = Viewer.points().length;
    if (count) navigateToPoint(Viewer.currentPointIndex <= 0 ? count - 1 : Viewer.currentPointIndex - 1);
}

// Clic sur un marqueur de la carte : l'éditeur a la priorité s'il attend un point.
function onMarkerClick(file, index) {
    if (Viewer.activeFileId !== file.id) setActiveFile(file.id);
    if (!handleEditorPointClick(index)) selectPoint(index);
}

function displayPointsList() {
    const points = Viewer.points();
    const content = pointsContent();
    document.getElementById('pointsCount').textContent = points.length;
    setVisible('pointsList', points.length > 0);
    setVisible('navigationControls', points.length > 0);
    if (Viewer.currentPointIndex >= points.length) Viewer.currentPointIndex = -1;

    // Autre fichier : la liste repart du début. Même fichier (édition, changement d'unités) :
    // elle reste là où elle était.
    const sameFile = pointsView.fileId === Viewer.activeFileId;
    pointsView.fileId = Viewer.activeFileId;
    pointsView.measured = false;
    renderPointsWindow(sameFile ? pointsView.start : 0);
    if (!sameFile) content.scrollTop = 0;
    updateNavigationInfo();
    updatePointsListHighlight();
    updateSelectionMarker();
}

// Éléments qui utilisent eux-mêmes les flèches gauche / droite quand ils ont le focus
const ARROW_KEY_OWNERS = 'input, textarea, select, [contenteditable], [role="tab"], [role="tablist"], [role="slider"], '
    + '.dropdown-menu, .dropdown-toggle, .leaflet-container';

function setupPointsNavigation() {
    document.getElementById('prevPointBtn').addEventListener('click', navigateToPreviousPoint);
    document.getElementById('nextPointBtn').addEventListener('click', navigateToNextPoint);

    pointsContent().addEventListener('scroll', onPointsScroll, { passive: true });

    // La liste vit dans un onglet : masquée, ses lignes n'ont pas de hauteur mesurable.
    // À l'ouverture de l'onglet, la fenêtre est redessinée et la hauteur de ligne mesurée.
    document.getElementById('points-tab').addEventListener('shown.bs.tab', () => {
        pointsView.measured = false;
        renderPointsWindow(pointsView.start);
        updatePointsListHighlight();
    });

    // Un seul écouteur pour toute la liste (plusieurs milliers de points pour une trace GPX)
    pointsContent().addEventListener('click', (event) => {
        const item = event.target.closest('.point-item');
        if (!item) return;
        const index = Number(item.dataset.pointIndex);
        if (!handleEditorPointClick(index)) navigateToPoint(index);
    });

    document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') { cancelEditing(); return; }
        if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
        if (event.altKey || event.ctrlKey || event.metaKey) return;
        // Les flèches gardent leur rôle habituel dans les champs de saisie, sur les onglets
        // et les menus (navigation clavier de Bootstrap) et sur la carte (déplacement Leaflet)
        const target = event.target instanceof Element ? event.target : null;
        if (target && target.closest(ARROW_KEY_OWNERS)) return;
        if (Viewer.points().length === 0) return;
        event.preventDefault();
        if (event.key === 'ArrowLeft') navigateToPreviousPoint(); else navigateToNextPoint();
    });
}
