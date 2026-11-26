// Zone couverte : géométrie commune au constructeur de traces (RB-1.3b) et à l'éditeur de la page Réglages (EF-6.20, EF-6.21).
// Miroir exact de app/services/routing/zone.py : mêmes contrôles, mêmes messages, même règle du bord, même projection.
// Tout se calcule dans la projection de la carte (Web Mercator, en degrés) ; un sommet est un couple [longitude, latitude].
// Le jeu de cas commun (CA-R51) est joué contre `Zone.contains` ici et contre `zone.contains` côté serveur.
'use strict';

const Zone = (() => {
    const MAX_VERTICES = 50, MIN_VERTICES = 3, MAX_LAT = 85.0511, MAX_SIDE_M = 2_000_000;
    const EDGE_EPSILON = 1e-9, EARTH_RADIUS_M = 6_371_000;

    function project(lon, lat) {
        return [lon, (Math.log(Math.tan(Math.PI / 4 + (lat * Math.PI) / 360)) * 180) / Math.PI];
    }

    function unproject(x, y) {                          // inverse de project : latitude d'un y Web Mercator (degrés)
        return [x, ((2 * Math.atan(Math.exp((y * Math.PI) / 180)) - Math.PI / 2) * 180) / Math.PI];
    }

    // Un nombre fini ; ±Infinity (entier démesuré d'un JSON) passe : il échoue aux bornes, comme côté serveur
    const isNumber = (v) => typeof v === 'number' && !Number.isNaN(v);

    function haversine(lat1, lon1, lat2, lon2) {
        const rad = Math.PI / 180, p1 = lat1 * rad, p2 = lat2 * rad;
        const a = Math.sin((p2 - p1) / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(((lon2 - lon1) * rad) / 2) ** 2;
        return 2 * EARTH_RADIUS_M * Math.asin(Math.min(1, Math.sqrt(a)));
    }
    // Largeur le long du parallèle le plus proche de l'équateur, hauteur le long d'un méridien (P-TR5-1)
    function sidesOf(lonMin, latMin, lonMax, latMax) {
        const parallel = latMin <= 0 && latMax >= 0 ? 0 : Math.min(Math.abs(latMin), Math.abs(latMax));
        return { width: haversine(parallel, lonMin, parallel, lonMax), height: haversine(latMin, lonMin, latMax, lonMin) };
    }
    function envelope(vertices) {                       // [latMin, lonMin, latMax, lonMax]
        const lons = vertices.map((v) => v[0]), lats = vertices.map((v) => v[1]);
        return [Math.min(...lats), Math.min(...lons), Math.max(...lats), Math.max(...lons)];
    }
    function sides(vertices) {
        const [latMin, lonMin, latMax, lonMax] = envelope(vertices);
        return sidesOf(lonMin, latMin, lonMax, latMax);
    }

    const orientation = (a, b, c) => (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]);
    const onSegment = (a, b, p) => Math.min(a[0], b[0]) <= p[0] && p[0] <= Math.max(a[0], b[0])
        && Math.min(a[1], b[1]) <= p[1] && p[1] <= Math.max(a[1], b[1]);
    function segmentsTouch(a, b, c, d) {
        const o1 = orientation(a, b, c), o2 = orientation(a, b, d), o3 = orientation(c, d, a), o4 = orientation(c, d, b);
        if (((o1 > 0 && o2 < 0) || (o1 < 0 && o2 > 0)) && ((o3 > 0 && o4 < 0) || (o3 < 0 && o4 > 0))) return true;
        return (o1 === 0 && onSegment(a, b, c)) || (o2 === 0 && onSegment(a, b, d))
            || (o3 === 0 && onSegment(c, d, a)) || (o4 === 0 && onSegment(c, d, b));
    }
    function adjacentOverlap(a, b, c) {                 // arêtes voisines [a, b] et [b, c] : seul b leur est commun
        const u = [b[0] - a[0], b[1] - a[1]], v = [c[0] - b[0], c[1] - b[1]];
        if ((u[0] === 0 && u[1] === 0) || (v[0] === 0 && v[1] === 0)) return true;
        return orientation(a, b, c) === 0 && u[0] * v[0] + u[1] * v[1] < 0;
    }
    // Première paire d'arêtes qui se touchent ou se croisent : [[i, j], [k, l]] (sommets comptés à partir de 1) ; null : polygone simple
    function firstCrossing(vertices) {
        const points = vertices.map((v) => project(v[0], v[1]));
        const count = points.length;
        for (let e1 = 0; e1 < count; e1 += 1) {
            for (let e2 = e1 + 1; e2 < count; e2 += 1) {
                let bad;
                if (e2 === e1 + 1 || (e1 === 0 && e2 === count - 1)) {
                    const first = e2 === e1 + 1 ? e1 : e2, second = e2 === e1 + 1 ? e2 : e1;
                    bad = adjacentOverlap(points[first], points[(first + 1) % count], points[(second + 1) % count]);
                } else {
                    bad = segmentsTouch(points[e1], points[(e1 + 1) % count], points[e2], points[(e2 + 1) % count]);
                }
                if (bad) return [[e1 + 1, ((e1 + 1) % count) + 1], [e2 + 1, ((e2 + 1) % count) + 1]];
            }
        }
        return null;
    }

    // Contrôles de EF-6.20, dans le même ordre et avec les mêmes messages que le serveur : message, ou null si valide (`[]` refusé)
    function check(value) {
        if (!Array.isArray(value)) return 'Zone : de 3 à 50 sommets.';
        if (value.length < MIN_VERTICES || value.length > MAX_VERTICES) return 'Zone : de 3 à 50 sommets.';
        if (!value.every((v) => Array.isArray(v) && v.length === 2 && isNumber(v[0]) && isNumber(v[1]))) return 'Zone : chaque sommet est [longitude, latitude].';
        if (!value.every(([lon, lat]) => lon >= -180 && lon <= 180 && lat >= -MAX_LAT && lat <= MAX_LAT)) {
            return 'Zone : longitude de −180 à 180, latitude de −85,0511 à 85,0511.';
        }
        const [latMin, lonMin, latMax, lonMax] = envelope(value);
        const { width, height } = sidesOf(lonMin, latMin, lonMax, latMax);
        if (Math.max(width, height) > MAX_SIDE_M) return 'Zone : emprise de 2 000 km de côté au plus.';
        const crossing = firstCrossing(value);
        if (crossing) return `Zone : le contour se croise (arêtes ${crossing[0][0]}–${crossing[0][1]} et ${crossing[1][0]}–${crossing[1][1]}).`;
        return null;
    }

    function isAxisRectangle(vertices) {
        if (vertices.length !== 4) return false;
        return new Set(vertices.map((v) => v[0])).size === 2 && new Set(vertices.map((v) => v[1])).size === 2
            && new Set(vertices.map((v) => `${v[0]},${v[1]}`)).size === 4;
    }
    function distanceToSegment(px, py, a, b) {
        const dx = b[0] - a[0], dy = b[1] - a[1], length2 = dx * dx + dy * dy;
        const t = length2 === 0 ? 0 : Math.max(0, Math.min(1, ((px - a[0]) * dx + (py - a[1]) * dy) / length2));
        return Math.hypot(px - (a[0] + t * dx), py - (a[1] + t * dy));
    }
    // Le point est dans la zone : pré-filtre sur l'emprise ; rectangle aligné = comme avant (bords compris) ; sinon contour à 1e-9
    // près = dedans, ailleurs règle pair-impair
    function contains(vertices, lat, lon) {
        const [latMin, lonMin, latMax, lonMax] = envelope(vertices);
        if (!(lat >= latMin && lat <= latMax && lon >= lonMin && lon <= lonMax)) return false;
        if (isAxisRectangle(vertices)) return true;
        const [px, py] = project(lon, lat);
        const points = vertices.map((v) => project(v[0], v[1]));
        let inside = false;
        for (let i = 0; i < points.length; i += 1) {
            const a = points[i], b = points[(i + 1) % points.length];
            if (distanceToSegment(px, py, a, b) <= EDGE_EPSILON) return true;
            if ((a[1] > py) !== (b[1] > py) && px < a[0] + ((py - a[1]) * (b[0] - a[0])) / (b[1] - a[1])) inside = !inside;
        }
        return inside;
    }

    return { MAX_VERTICES, MIN_VERTICES, MAX_LAT, project, unproject, check, firstCrossing, envelope, sides, sidesOf, contains };
})();
