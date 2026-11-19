// Visualiseur : corps des requêtes d'analyse et d'export.
// Le navigateur garde pour l'affichage bien plus que ce que le serveur lit : un marqueur
// complet par point de trace (~450 octets). Renvoyer ces données telles quelles dépasse
// la taille de requête acceptée dès ~20 000 points ; on n'envoie donc que l'utile.
'use strict';

const ISO_STAMP = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$/;

const validPosition = (coord) => Array.isArray(coord)
    && typeof coord[0] === 'number' && typeof coord[1] === 'number'
    && Math.abs(coord[0]) <= 90 && Math.abs(coord[1]) <= 180; // faux pour NaN

function lineTimestamps(line) {
    const stamps = line.time_info && line.time_info.timestamps;
    return Array.isArray(stamps) && stamps.length ? stamps : line.timestamps;
}

// Vrai si les traces portent à elles seules un temps exploitable : chaque point valide,
// horodaté, dans l'ordre, et le temps qui avance à l'intérieur d'au moins une trace (le
// serveur juge l'horloge trace par trace). Il ne consulte alors jamais les points de
// mesure (track_series), inutile de les lui envoyer.
// Critère volontairement strict : au moindre doute il est faux et les points sont envoyés,
// ce qui coûte de la place mais ne change jamais le résultat.
function linesCarryTime(lines) {
    let previous = -Infinity, advances = false;
    for (const line of lines) {
        const stamps = lineTimestamps(line);
        if (line.type === 'multitrack' || !Array.isArray(line.coordinates)
            || !Array.isArray(stamps) || stamps.length !== line.coordinates.length) return false;
        for (let i = 0; i < stamps.length; i++) {
            if (!validPosition(line.coordinates[i])) return false;
            if (typeof stamps[i] !== 'string' || !ISO_STAMP.test(stamps[i])) return false;
            const time = Date.parse(stamps[i]);
            if (!Number.isFinite(time) || time < previous) return false;
            if (i > 0 && time > previous) advances = true; // entre deux points de la même trace
            previous = time;
        }
    }
    return advances;
}

// Point de mesure réduit aux champs lus par l'analyse (position, altitude, vitesse, temps).
function measurePoint(point) {
    const info = point.parsed_info || {};
    const time = point.time_info || {};
    return {
        type: 'marker', coordinates: point.coordinates, altitude: point.altitude, visibility: point.visibility,
        parsed_info: { speed_kmh: info.speed_kmh }, time_info: { timestamp: time.timestamp },
    };
}

// Corps des analyses (de base et avancée) d'un fichier : ses traces, et ses points de
// mesure seulement quand les traces ne suffisent pas.
function analysisPayload(data) {
    const lines = data.features.filter((feature) => feature && LINE_TYPES.includes(feature.type));
    return { features: lines, points: linesCarryTime(lines) ? [] : data.points.map(measurePoint) };
}

// Features à exporter : les points de trace masqués ne sont jamais écrits par l'export
// (la trace elle-même les porte déjà), seuls les repères visibles le sont.
function exportFeatures(data) {
    return data.features.filter((feature) => !(feature && feature.type === 'marker' && String(feature.visibility) === '0'));
}
