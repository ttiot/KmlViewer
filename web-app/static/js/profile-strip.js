// Bandeau de profil : une courbe le long de la trace (altitude ou vitesse en fonction de la
// distance), avec un curseur et, au besoin, une plage sélectionnée. Partagé par la carte et
// la page Tronçons. Le SVG est construit nœud par nœud : aucune donnée ne passe par du HTML.
'use strict';

const ProfileStrip = (() => {
    const SVG_NS = 'http://www.w3.org/2000/svg';
    const WIDTH = 1000, HEIGHT = 160;
    const MAX_POINTS = 1200; // au-delà, la courbe est allégée : inutile de tracer plus fin que l'écran

    function svg(tag, attrs = {}) {
        const node = document.createElementNS(SVG_NS, tag);
        Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, value));
        return node;
    }

    // Pas « rond » (1, 2, 5 × 10ⁿ) au moins égal à `raw`
    function niceStep(raw) {
        const power = 10 ** Math.floor(Math.log10(raw));
        const ratio = raw / power;
        return (ratio <= 1 ? 1 : ratio <= 2 ? 2 : ratio <= 5 ? 5 : 10) * power;
    }

    // Crée le bandeau dans `container`. `onPick(t)` : position (0 à 1) désignée à la souris
    // ou au doigt. `small` : hauteur réduite.
    function create(container, { onPick = null, small = false, label = 'Profil le long de la trace' } = {}) {
        const { el } = App;
        const yAxis = el('div', { class: 'strip-yaxis', 'aria-hidden': 'true' });
        const canvas = el('div', { class: `strip-canvas${small ? ' is-small' : ''}` });
        const xAxis = el('div', { class: 'strip-xaxis', 'aria-hidden': 'true' });
        const empty = el('p', { class: 'strip-empty', hidden: true });
        const plot = el('div', { class: 'strip-plot' }, yAxis, canvas);

        const root = svg('svg', { viewBox: `0 0 ${WIDTH} ${HEIGHT}`, preserveAspectRatio: 'none', role: 'img', 'aria-label': label });
        const line = (cls) => svg('line', { class: cls, 'stroke-width': 1, 'vector-effect': 'non-scaling-stroke' });
        const grid = [line('strip-grid'), line('strip-grid')];
        const axis = line('strip-axis');
        const band = svg('rect', { class: 'strip-band', y: 0, height: HEIGHT });
        const area = svg('path');
        const curve = svg('path', { fill: 'none', 'stroke-width': 2, 'stroke-linejoin': 'round', 'vector-effect': 'non-scaling-stroke' });
        const bandEdges = [line('strip-cursor'), line('strip-cursor')];
        const cursor = line('strip-cursor');
        [axis, cursor, ...bandEdges].forEach((node) => node.setAttribute('stroke-width', 2));
        axis.setAttribute('stroke-width', 1);
        [...grid, axis].forEach((node) => { node.setAttribute('x1', 0); node.setAttribute('x2', WIDTH); });
        axis.setAttribute('y1', HEIGHT); axis.setAttribute('y2', HEIGHT);
        [cursor, ...bandEdges].forEach((node) => { node.setAttribute('y1', 0); node.setAttribute('y2', HEIGHT); });
        root.append(band, ...grid, axis, area, curve, ...bandEdges, cursor);
        canvas.append(root);
        container.replaceChildren(plot, xAxis, empty);

        const state = { total: 0 };
        const hide = (node, hidden) => { node.style.display = hidden ? 'none' : ''; };
        const vertical = (node, t) => {
            node.setAttribute('x1', (t * WIDTH).toFixed(1));
            node.setAttribute('x2', (t * WIDTH).toFixed(1));
        };
        [band, cursor, ...bandEdges].forEach((node) => hide(node, true));

        function clear(message) {
            state.total = 0;
            plot.hidden = true;
            xAxis.hidden = true;
            empty.hidden = !message;
            empty.textContent = message || '';
        }

        // xs : distances cumulées (m), ys : valeurs. color / fill : jetons CSS. unit : « m », « km/h ».
        function setSeries(xs, ys, { color, fill, unit }) {
            const count = Math.min(xs.length, ys.length);
            const total = count ? xs[count - 1] : 0;
            if (count < 2 || !(total > 0)) { clear('Pas de données pour cette courbe.'); return; }
            state.total = total;
            plot.hidden = false;
            xAxis.hidden = false;
            empty.hidden = true;

            let low = Infinity, high = -Infinity;
            for (let i = 0; i < count; i++) {
                if (ys[i] < low) low = ys[i];
                if (ys[i] > high) high = ys[i];
            }
            // Base à zéro, sauf si la courbe ne vivrait que dans le haut du cadre (trace d'altitude)
            const step = niceStep(Math.max(high - (low < 0.4 * high ? Math.min(0, low) : low), 1e-9) / 2.2);
            const base = low < 0.4 * high ? Math.min(0, Math.floor(low / step) * step) : Math.floor(low / step) * step;
            const top = Math.max(high, base + 2 * step) + step * 0.1;
            const y = (value) => HEIGHT - (value - base) / (top - base) * HEIGHT;

            const stride = Math.max(1, Math.ceil(count / MAX_POINTS));
            const parts = [];
            for (let i = 0; i < count; i += stride) {
                parts.push(`${parts.length ? 'L' : 'M'}${(xs[i] / total * WIDTH).toFixed(1)} ${y(ys[i]).toFixed(1)}`);
            }
            if ((count - 1) % stride) parts.push(`L${WIDTH} ${y(ys[count - 1]).toFixed(1)}`);
            const path = parts.join(' ');
            curve.setAttribute('d', path);
            curve.style.stroke = `var(${color})`;
            area.setAttribute('d', `${path} L${WIDTH} ${HEIGHT} L0 ${HEIGHT} Z`);
            area.style.fill = `var(${fill})`;

            const ticks = [base + 2 * step, base + step];
            ticks.forEach((value, index) => {
                grid[index].setAttribute('y1', y(value).toFixed(1));
                grid[index].setAttribute('y2', y(value).toFixed(1));
            });
            const digits = step < 1 ? 1 : 0;
            yAxis.replaceChildren(
                ...ticks.map((value) => {
                    const tick = el('span', {}, `${App.number(value, digits)} ${unit}`);
                    tick.style.top = `${(y(value) / HEIGHT * 100).toFixed(1)}%`;
                    return tick;
                }),
                el('span', {}, App.number(base, digits)));

            const km = total / 1000;
            const kmDigits = km < 20 ? 1 : 0;
            xAxis.replaceChildren(...[0, 0.25, 0.5, 0.75, 1].map((t) =>
                el('span', {}, App.number(km * t, t ? kmDigits : 0) + (t === 0 || t === 1 ? ' km' : ''))));
        }

        function setCursor(t) {
            const shown = state.total > 0 && Number.isFinite(t);
            hide(cursor, !shown);
            if (shown) vertical(cursor, Math.min(1, Math.max(0, t)));
        }

        function setBand(a, b) {
            const shown = state.total > 0 && Number.isFinite(a) && Number.isFinite(b) && b > a;
            [band, ...bandEdges].forEach((node) => hide(node, !shown));
            if (!shown) return;
            band.setAttribute('x', (a * WIDTH).toFixed(1));
            band.setAttribute('width', ((b - a) * WIDTH).toFixed(1));
            vertical(bandEdges[0], a);
            vertical(bandEdges[1], b);
        }

        if (onPick) {
            const pick = (event) => {
                const box = canvas.getBoundingClientRect();
                if (!box.width || !state.total) return;
                onPick(Math.min(1, Math.max(0, (event.clientX - box.left) / box.width)));
            };
            canvas.addEventListener('pointermove', pick);
            canvas.addEventListener('pointerdown', pick);
        }

        clear('');
        return { setSeries, setCursor, setBand, clear, total: () => state.total };
    }

    return { create };
})();
