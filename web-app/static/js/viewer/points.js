// Visualiseur : navigation entre les points et liste des points du fichier actif.
'use strict';

function updateNavigationInfo() {
    const info = document.getElementById('navigationInfo');
    const points = Viewer.points();
    const point = points[Viewer.currentPointIndex];
    if (!point) {
        info.style.display = 'none';
        return;
    }
    const { el } = App;
    info.replaceChildren(
        el('strong', {}, `Point ${Viewer.currentPointIndex + 1}/${points.length}`), el('br'),
        el('small', {}, point.name || ''));
    info.style.display = 'block';
}

function updatePointsListHighlight() {
    document.querySelectorAll('.point-item.active').forEach((item) => item.classList.remove('active'));
    if (Viewer.currentPointIndex < 0) return;
    const active = document.querySelector(`.point-item[data-point-index="${Viewer.currentPointIndex}"]`);
    if (active) {
        active.classList.add('active');
        active.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
}

function selectPoint(index) {
    if (index < 0 || index >= Viewer.points().length) return;
    Viewer.currentPointIndex = index;
    updateNavigationInfo();
    updatePointsListHighlight();
}

// Sélectionne un point, centre la carte dessus et ouvre son popup s'il est dessiné.
function navigateToPoint(index) {
    const points = Viewer.points();
    if (index < 0 || index >= points.length) return;
    selectPoint(index);
    Viewer.map.setView(points[index].coordinates, Math.max(Viewer.map.getZoom(), 15));
    const file = Viewer.activeFile();
    const marker = file && file.markers[index];
    if (marker && file.visible) marker.openPopup();
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
    const { el } = App;
    const points = Viewer.points();
    const content = document.getElementById('pointsListContent');
    document.getElementById('pointsCount').textContent = points.length;
    setVisible('pointsList', points.length > 0);
    setVisible('navigationControls', points.length > 0);
    if (Viewer.currentPointIndex >= points.length) Viewer.currentPointIndex = -1;

    const fragment = document.createDocumentFragment();
    points.forEach((point, index) => {
        const description = descriptionNodes(pointDescription(point));
        const tracePoint = String(point.visibility) === '0'; // point de trace, non dessiné sur la carte
        fragment.appendChild(el('div', { class: 'point-item', dataset: { pointIndex: index } },
            el('div', { class: 'd-flex align-items-start' },
                el('div', { class: 'me-3 mt-1' },
                    el('div', { class: `point-dot ${tracePoint ? '' : 'annotation'}` })),
                el('div', { class: 'flex-grow-1' },
                    el('div', { class: 'point-name' }, point.name || `Point ${index + 1}`),
                    description.length ? el('div', { class: 'point-description' }, description) : null,
                    el('div', { class: 'point-meta' },
                        el('span', { class: `badge ${tracePoint ? 'bg-info' : 'bg-warning'} point-badge me-2` },
                            tracePoint ? 'Point de trace' : 'Repère'),
                        el('small', { class: 'text-muted' }, `Point ${index + 1}/${points.length}`),
                        point.altitude
                            ? el('small', { class: 'text-muted ms-2' }, `Alt : ${Math.round(point.altitude)} m`) : null)))));
    });
    content.replaceChildren(fragment);
    updateNavigationInfo();
    updatePointsListHighlight();
}

function setupPointsNavigation() {
    document.getElementById('prevPointBtn').addEventListener('click', navigateToPreviousPoint);
    document.getElementById('nextPointBtn').addEventListener('click', navigateToNextPoint);

    // Un seul écouteur pour toute la liste (plusieurs milliers de points pour une trace GPX)
    document.getElementById('pointsListContent').addEventListener('click', (event) => {
        const item = event.target.closest('.point-item');
        if (!item) return;
        const index = Number(item.dataset.pointIndex);
        if (!handleEditorPointClick(index)) navigateToPoint(index);
    });

    document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') { cancelEditing(); return; }
        if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
        // Les flèches gardent leur rôle habituel dans les champs de saisie
        if (event.target.closest('input, textarea, select, [contenteditable]')) return;
        if (Viewer.points().length === 0) return;
        event.preventDefault();
        if (event.key === 'ArrowLeft') navigateToPreviousPoint(); else navigateToNextPoint();
    });
}
