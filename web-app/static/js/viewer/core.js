// Visualiseur : état partagé et utilitaires d'affichage.
// Règle : toute donnée venant d'un fichier ou du serveur est insérée en nœud texte
// (App.el / textContent), jamais en HTML. Un nom de point reste donc du texte.
'use strict';

const Viewer = {
    map: null,
    baseLayers: {},
    // id -> { id, name, data, layer, markers, visible, activityId, edited, advanced }
    //   activityId : activité de la bibliothèque correspondante (null sinon)
    //   edited     : la trace a été modifiée dans l'éditeur depuis son chargement
    //   revision   : compteur de modifications (un calcul lancé avant une modification est périmé)
    //   advanced   : dernière analyse avancée de ce fichier { analysis, computedAt, saved }
    files: new Map(),
    libraryIds: new Map(),   // nom de fichier -> id d'activité, pour les envois enregistrés en bibliothèque
    fileCounter: 0,
    activeFileId: null,
    currentPointIndex: -1,
    analysis: null,          // dernière analyse de base (pour redessiner au changement de thème)
    charts: { elevation: null, speed: null },
    advancedHighlight: null, // surbrillance sur la carte d'un résultat de l'analyse avancée

    activeFile() {
        return this.activeFileId ? this.files.get(this.activeFileId) || null : null;
    },
    points() {
        const file = this.activeFile();
        return file ? file.data.points : [];
    },
    features() {
        const file = this.activeFile();
        return file ? file.data.features : [];
    },
    hasData() {
        return this.features().length > 0;
    },
    displayMode() {
        const checked = document.querySelector('input[name="displayMode"]:checked');
        return checked ? checked.value : 'double';
    },
};

const ALERT_ICONS = { success: 'check', danger: 'triangle-exclamation', warning: 'triangle-exclamation', info: 'circle-info' };

function repositionAlerts() {
    let bottomOffset = 20;
    document.querySelectorAll('.alert-popup').forEach((alert) => {
        alert.style.bottom = `${bottomOffset}px`;
        bottomOffset += alert.offsetHeight + 10;
    });
}

// Notification empilée en bas à droite. `message` est du texte brut.
function showAlert(message, type = 'info', { spinner = false } = {}) {
    const container = document.getElementById('alertContainer');
    if (!container) return;
    const { el } = App;
    const iconClass = spinner ? 'fas fa-spinner fa-spin me-2' : `fas fa-${ALERT_ICONS[type] || ALERT_ICONS.info} me-2`;
    const alert = el('div', { class: `alert-popup alert-${type}`, role: 'status' },
        el('div', { class: 'alert-content' }, el('i', { class: iconClass }), String(message)),
        el('button', { type: 'button', class: 'btn-close', 'aria-label': 'Fermer',
            onclick: () => { alert.remove(); repositionAlerts(); } }, '×'));
    container.appendChild(alert);
    repositionAlerts();
    setTimeout(() => alert.classList.add('show'), 50);
    setTimeout(() => {
        alert.classList.remove('show');
        setTimeout(() => { alert.remove(); repositionAlerts(); }, 300);
    }, spinner ? 2000 : 4000);
}

// Texte brut d'un fragment potentiellement HTML (descriptions KML) : le document créé
// par DOMParser est inerte, rien n'y est exécuté ni chargé.
function plainText(value) {
    const text = String(value === null || value === undefined ? '' : value);
    if (!/[<&]/.test(text)) return text;
    return new DOMParser().parseFromString(text, 'text/html').body.textContent || '';
}

// Description affichable : seuls les sauts de ligne <br> sont conservés, le reste devient du texte.
function descriptionNodes(description) {
    const nodes = [];
    String(description || '').split(/<br\s*\/?>/i).forEach((part) => {
        const text = plainText(part).trim();
        if (!text) return;
        if (nodes.length) nodes.push(document.createElement('br'));
        nodes.push(document.createTextNode(text));
    });
    return nodes;
}

// Description affichée d'un point. Un point de mesure (vitesse ou cap connus : traceur GPS,
// point de trace horodaté) est présenté dans les unités choisies ; tout autre point garde
// la description de son fichier. Calculé à l'affichage : changer d'unités suffit à rafraîchir.
function pointDescription(point) {
    const info = point.parsed_info || {};
    const has = (value) => value !== null && value !== undefined && Number.isFinite(Number(value));
    if (!has(info.speed_kmh) && !has(info.heading)) return point.description || '';
    const double = Viewer.displayMode() === 'double';
    const lines = [];
    if (has(info.speed_kmh)) {
        const knots = has(info.speed_kts) ? info.speed_kts : Math.round(info.speed_kmh / 1.852 * 10) / 10;
        lines.push(`Vitesse : ${info.speed_kmh} km/h${double ? ` (${knots} kts)` : ''}`);
    }
    const altitude = has(point.altitude) ? Number(point.altitude) : Number(info.altitude_m);
    if (altitude) {
        const metres = `${Math.round(altitude)} m`;
        lines.push(`Altitude : ${double ? `${Math.round(altitude * 3.28084)} ft (${metres})` : metres}`);
    }
    if (Array.isArray(point.coordinates)) {
        lines.push(`Position : ${Number(point.coordinates[0]).toFixed(6)}°, ${Number(point.coordinates[1]).toFixed(6)}°`);
    }
    if (has(info.heading)) lines.push(`Cap : ${info.heading}°`);
    return lines.join('<br>');
}

function setVisible(id, visible, display = 'block') {
    const node = document.getElementById(id);
    if (node) node.style.display = visible ? display : 'none';
}
