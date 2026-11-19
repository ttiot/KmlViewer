// Visualiseur : réglages de la carte (fond de carte, unités d'affichage).
// Un bouton posé sur la carte, en haut à droite, ouvre un petit panneau de boutons radio.
// Le balisage vient du gabarit (viewer/map_settings.html) ; il est placé ici dans un
// contrôle Leaflet. Les deux choix sont mémorisés dans le navigateur.
'use strict';

const BASE_LAYER_PREF = 'kmlviewer-base-layer';
const DISPLAY_MODE_PREF = 'kmlviewer-display-mode';
const DISPLAY_MODES = ['double', 'simple'];

function checkRadio(name, value) {
    document.querySelectorAll(`input[name="${name}"]`).forEach((radio) => {
        radio.checked = radio.value === value;
    });
}

function setMapSettingsOpen(open) {
    const toggle = document.getElementById('mapSettingsToggle');
    document.getElementById('mapSettingsPanel').hidden = !open;
    toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
}

function mapSettingsOpen() {
    return !document.getElementById('mapSettingsPanel').hidden;
}

function setupMapSettings() {
    const container = document.getElementById('mapSettings');
    const toggle = document.getElementById('mapSettingsToggle');
    if (!container || !toggle || !Viewer.map) return;

    // Choix mémorisés, relus avec liste blanche ; sinon les cases cochées du gabarit
    const layer = Prefs.read(BASE_LAYER_PREF, Object.keys(Viewer.baseLayers));
    if (layer && setBaseLayer(layer)) checkRadio('baseLayer', layer);
    const mode = Prefs.read(DISPLAY_MODE_PREF, DISPLAY_MODES);
    if (mode) checkRadio('displayMode', mode);

    const SettingsControl = L.Control.extend({
        options: { position: 'topright' },
        onAdd() {
            // Un clic, un double-clic ou la molette dans le panneau ne doivent pas atteindre
            // la carte : en mode « ajouter un point », un point serait posé sous le panneau.
            L.DomEvent.disableClickPropagation(container);
            L.DomEvent.disableScrollPropagation(container);
            container.classList.add('leaflet-control');
            container.hidden = false;
            return container;
        },
    });
    new SettingsControl().addTo(Viewer.map);

    toggle.addEventListener('click', () => {
        const open = !mapSettingsOpen();
        setMapSettingsOpen(open);
        if (open) {
            const checked = container.querySelector('input[name="baseLayer"]:checked');
            if (checked) checked.focus();
        }
    });

    // Fermeture au clic en dehors (pointerdown : un glisser de la carte ferme aussi) et à Échap
    document.addEventListener('pointerdown', (event) => {
        if (mapSettingsOpen() && !container.contains(event.target)) setMapSettingsOpen(false);
    });
    // Phase de capture : Échap ferme le panneau sans annuler en plus une édition en cours
    document.addEventListener('keydown', (event) => {
        if (event.key !== 'Escape' || !mapSettingsOpen()) return;
        event.stopPropagation();
        setMapSettingsOpen(false);
        toggle.focus();
    }, true);

    container.querySelectorAll('input[name="baseLayer"]').forEach((radio) => {
        radio.addEventListener('change', () => {
            if (radio.checked && setBaseLayer(radio.value)) Prefs.write(BASE_LAYER_PREF, radio.value);
        });
    });
    container.querySelectorAll('input[name="displayMode"]').forEach((radio) => {
        radio.addEventListener('change', () => {
            if (!radio.checked || !DISPLAY_MODES.includes(radio.value)) return;
            Prefs.write(DISPLAY_MODE_PREF, radio.value);
            // Les descriptions sont composées à l'affichage : liste et popups suivent aussitôt
            Viewer.map.closePopup();
            displayPointsList();
        });
    });
}
