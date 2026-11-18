// Visualiseur : initialisation. Les modules (core, map, points, files, analysis, editor)
// sont chargés avant ce fichier ; tous les écouteurs sont posés ici, aucun dans le HTML.
'use strict';

document.addEventListener('DOMContentLoaded', () => {
    initMap();
    setupFileHandling();
    setupSidebar();
    setupPointsNavigation();
    setupAnalysis();
    setupEditor();
    loadSampleFiles();
});
