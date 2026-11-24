// Visualiseur : initialisation. Les modules (core, prefs, map, settings, payload, points,
// panel, files, opener, analysis, editor, library) sont chargés avant ce fichier ; tous les
// écouteurs sont posés ici, aucun dans le HTML.
'use strict';

document.addEventListener('DOMContentLoaded', async () => {
    initMap();
    await applyMapConfig();
    setupMapSettings();
    setupPanel();
    setupOpening();
    setupPointsNavigation();
    setupAnalysis();
    setupEditor();
    setupLibrary();
});
