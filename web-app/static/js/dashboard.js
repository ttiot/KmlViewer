'use strict';

(() => {
    const { api, el, icon, notify, number, duration, chrono, distance, speed, date, setFigure, durationParts } = App;
    const PAGE_SIZE = 100;  // activités demandées par page
    const MAX_LIMIT = 500;  // plafond de `limit` côté API
    // year : année affichée, null pour laisser le serveur choisir (année en cours, sinon la
    // dernière qui porte des données)
    const state = { period: 'year', metric: 'distance_km', type: '', year: null, stats: null, chart: null,
                    demEnabled: false, activities: [], hasMore: false, total: null,
                    // Numéro de la dernière requête de chaque liste : une réponse plus ancienne,
                    // arrivée en retard, ne doit pas écraser l'affichage du filtre courant
                    requests: { dashboard: 0, activities: 0 }, typeLabels: {} };

    const METRICS = {
        distance_km: { label: 'Distance (km)', title: 'Distance', format: (v) => `${number(v, 1)} km` },
        ascent_m: { label: 'Dénivelé positif (m)', title: 'Dénivelé positif', format: (v) => `${number(v)} m` },
        time_s: { label: 'Temps (h)', title: 'Temps', format: (v) => duration(v), scale: (v) => v / 3600 },
    };

    function tile(label, ...parts) {
        return el('div', {}, el('dt', { class: 'eyebrow' }, label), setFigure(el('dd', { class: 'figure' }), ...parts));
    }

    function renderTotals(stats) {
        const year = stats.current_year;
        document.getElementById('totals').replaceChildren(
            tile(`Distance ${year.year}`, number(year.distance_km, 1), { unit: 'km' }),
            tile(`Dénivelé + ${year.year}`, number(year.ascent_m), { unit: 'm' }),
            // Aucune activité horodatée : pas de « 0 s », la durée est inconnue
            tile(`Temps ${year.year}`, ...((year.time_s > 0 && durationParts(year.time_s)) || ['—'])),
            tile(`Activités ${year.year}`, year.count, { unit: `sur ${stats.totals.count} au total` }));
        document.getElementById('activitiesTitle').textContent = `Activités · ${stats.totals.count} au total`;
    }

    // Bandeau affiché quand l'année montrée n'est pas l'année en cours, ou que celle-ci est
    // vide alors qu'une autre porte des données.
    function renderYearNotice(stats) {
        const shown = stats.current_year.year, calendar = stats.calendar_year, latest = stats.latest_year;
        const notice = document.getElementById('yearNotice');
        const text = document.getElementById('yearNoticeText');
        const button = document.getElementById('yearNoticeButton');
        if (shown !== calendar) {
            text.replaceChildren(`Aucune activité en ${calendar}. Tu vois `, el('strong', {}, shown),
                ', la dernière année avec des données.');
            button.textContent = `Revenir à ${calendar}`;
            button.dataset.year = calendar;
        } else if (stats.current_year.count === 0 && latest && latest !== calendar) {
            text.replaceChildren(`Aucune activité en ${calendar}.`);
            button.textContent = `Voir ${latest}`;
            button.dataset.year = '';
        } else {
            notice.hidden = true;
            return;
        }
        notice.hidden = false;
    }

    // Libellé d'une période de la série : « 2024-06 » -> « juin », « 2026-S47 » -> « S47 »
    function periodLabel(key) {
        const month = /^(\d{4})-(\d{2})$/.exec(key);
        if (!month) return key.replace(/^\d{4}-/, '');
        const label = new Date(Number(month[1]), Number(month[2]) - 1, 1).toLocaleDateString('fr-FR', { month: 'short' });
        return state.period === 'year' ? label : `${label} ${month[1].slice(2)}`;
    }

    function renderChart() {
        if (!state.stats) return;
        const metric = METRICS[state.metric];
        const scale = metric.scale || ((v) => v);
        const series = state.stats.series;
        document.getElementById('chartTitle').textContent = state.period === 'week'
            ? `${metric.title} par semaine`
            : state.period === 'month' ? `${metric.title} par mois`
                : `${metric.title} par mois · ${state.stats.current_year.year}`;
        const config = {
            type: 'bar',
            data: {
                labels: series.map((s) => periodLabel(s.period)),
                datasets: [{
                    label: metric.label,
                    data: series.map((s) => scale(s[state.metric])),
                    // Couleurs lues dans les jetons à chaque rendu : suit le thème clair / sombre
                    backgroundColor: () => AppTheme.color('--accent'),
                    hoverBackgroundColor: () => AppTheme.color('--accent-hover'),
                    // Extrémité de donnée arrondie, base carrée sur l'axe
                    maxBarThickness: 72,
                    borderRadius: { topLeft: 4, topRight: 4 },
                    borderSkipped: 'bottom',
                }],
            },
            options: {
                maintainAspectRatio: false,
                interaction: { mode: 'index', intersect: false },
                plugins: {
                    legend: { display: false },
                    tooltip: { callbacks: { label: (ctx) => metric.format(series[ctx.dataIndex][state.metric]) } },
                },
                scales: {
                    y: { beginAtZero: true, border: { display: false },
                         title: { display: true, text: metric.label } },
                    x: { grid: { display: false }, border: { color: AppTheme.color('--border-strong') } },
                },
            },
        };
        if (state.chart) state.chart.destroy();
        state.chart = new Chart(document.getElementById('volumeChart'), config);
    }

    function recordValue(record) {
        if (record.kind === 'distance') return distance(record.value);
        if (record.kind === 'ascent') return `${number(record.value)} m`;
        if (record.kind === 'speed') return `${number(record.value * 3.6, 1)} km/h`;
        return record.kind === 'best_effort' ? chrono(record.value) : duration(record.value);
    }

    function renderRecords(records) {
        const list = document.getElementById('records');
        // Les meilleurs temps sur distance n'existent que pour la course à pied
        document.getElementById('bestEffortsHint').hidden = records.some((r) => r.kind === 'best_effort');
        if (!records.length) {
            list.replaceChildren(el('div', { class: 'empty-state' },
                el('strong', {}, 'Pas encore de record.'),
                el('span', {}, 'Ils apparaîtront dès ta première activité.')));
            return;
        }
        list.replaceChildren(...records.map((r) =>
            el('div', { class: 'record-item' },
                el('div', { class: 'record-body' },
                    el('div', { class: 'record-label' }, r.label),
                    el('a', { class: 'record-source', href: `/?activity=${r.activity_id}` },
                        `${r.activity_name} · ${date(r.date)}`)),
                el('div', { class: 'figure record-value' }, recordValue(r)))));
    }

    function activityRow(a) {
        const time = a.moving_s || a.duration_s;
        const ascent = el('td', { class: 'text-end text-nowrap' }, a.ascent_m ? `${number(a.ascent_m)} m ` : '— ');
        if (a.elev_source === 'dem') {
            ascent.append(el('span', { class: 'badge-soft is-info', title: `Altitude corrigée par MNT (GPS : ${number(a.ascent_gps_m)} m)` }, 'MNT'));
        }
        const action = (iconName, title, handler, extra = '') => el('button', {
            class: `icon-btn ${extra}`, type: 'button', title, 'aria-label': title, onclick: handler }, icon(iconName));
        const actions = el('td', { class: 'text-end text-nowrap' },
            el('span', { class: 'row-actions' },
                el('a', { class: 'icon-btn', href: `/?activity=${a.id}`, title: 'Voir sur la carte', 'aria-label': 'Voir sur la carte' }, icon('map')),
                action('pen', 'Renommer', () => renameActivity(a)),
                state.demEnabled ? action('mountain', "Corriger l'altitude (MNT)", () => correctElevation(a)) : null,
                action('trash', 'Supprimer', () => deleteActivity(a), 'is-danger')));
        return el('tr', {},
            el('td', { class: 'text-nowrap' }, date(a.start_time), a.start_time_known === false
                ? el('span', { class: 'badge-soft ms-1', title: "Fichier sans date : rangé à sa date d'import" }, 'import') : null),
            el('td', {}, el('a', { class: 'activity-name', href: `/?activity=${a.id}` }, a.name)),
            el('td', {}, el('span', { class: 'chip' },
                state.typeLabels[a.activity_type] || state.typeLabels.unclassified)),
            el('td', { class: 'text-end text-nowrap' }, distance(a.distance_m)),
            ascent,
            el('td', { class: 'text-end text-nowrap' }, time ? duration(time) : '—'),
            el('td', { class: 'text-end text-nowrap' }, speed(a.distance_m, time)),
            actions);
    }

    async function renameActivity(activity) {
        const name = window.prompt("Nom de l'activité", activity.name);
        if (!name || name.trim() === activity.name) return;
        try {
            await api(`/api/activities/${activity.id}`, { method: 'PATCH', body: { name: name.trim() } });
            await refresh();
        } catch (error) { notify(error.message, 'danger'); }
    }

    async function deleteActivity(activity) {
        if (!window.confirm(`Supprimer « ${activity.name} » ? Les temps sur tronçons associés seront perdus.`)) return;
        try {
            await api(`/api/activities/${activity.id}`, { method: 'DELETE' });
            await refresh();
        } catch (error) { notify(error.message, 'danger'); }
    }

    async function correctElevation(activity) {
        try {
            const result = await api(`/api/activities/${activity.id}/elevation`, { method: 'POST' });
            notify(`D+ corrigé : ${number(result.ascent_m)} m (GPS : ${number(result.ascent_gps_m)} m)`, 'success');
            await refresh();
        } catch (error) { notify(error.message, 'danger'); }
    }

    function renderActivities() {
        const shown = state.activities.length;
        document.getElementById('activities').replaceChildren(...state.activities.map(activityRow));
        document.getElementById('activitiesEmpty').hidden = shown > 0;
        document.getElementById('activitiesTable').parentElement.hidden = shown === 0;
        document.getElementById('activitiesMore').hidden = shown === 0;
        document.getElementById('loadMoreButton').hidden = !state.hasMore;
        // Total : celui de l'API s'il est fourni, sinon connu seulement une fois la liste épuisée
        const total = state.total !== null ? state.total : (state.hasMore ? null : shown);
        document.getElementById('activitiesCount').textContent = total === null
            ? `${shown} activités affichées`
            : `${shown} activité(s) affichée(s) sur ${total}`;
    }

    // Charge la liste des activités. `more` ajoute la page suivante ; sinon la liste est
    // relue depuis le début, sur la même longueur que celle déjà affichée (après un
    // renommage ou une suppression, on ne revient pas à la première page).
    async function loadActivities({ more = false } = {}) {
        const request = ++state.requests.activities;
        const offset = more ? state.activities.length : 0;
        const limit = more ? PAGE_SIZE : Math.min(MAX_LIMIT, Math.max(PAGE_SIZE, state.activities.length));
        const params = new URLSearchParams({ limit, offset });
        if (state.type) params.set('type', state.type);
        const data = await api(`/api/activities?${params}`);
        if (request !== state.requests.activities) return;

        state.demEnabled = data.dem_enabled;
        state.activities = more ? state.activities.concat(data.activities) : data.activities;
        state.hasMore = data.activities.length === limit;
        state.total = Number.isInteger(data.total) ? data.total : null;
        renderActivities();
    }

    async function loadDashboard() {
        const request = ++state.requests.dashboard;
        const period = state.period;
        const params = new URLSearchParams({ period });
        if (state.type) params.set('type', state.type);
        if (state.year) params.set('year', state.year);
        const data = await api(`/api/dashboard?${params}`);
        if (request !== state.requests.dashboard) return;
        state.stats = data.stats;
        renderTotals(data.stats);
        renderYearNotice(data.stats);
        renderChart();
        renderRecords(data.records);
    }

    async function refresh() {
        try {
            await Promise.all([loadDashboard(), loadActivities()]);
        } catch (error) { notify(error.message, 'danger'); }
    }

    async function importFiles(files) {
        const form = new FormData();
        Array.from(files).forEach((f) => form.append('files', f));
        notify(`Import de ${files.length} fichier(s)…`);
        try {
            const data = await api('/api/activities', { method: 'POST', body: form });
            const count = (status) => data.results.filter((r) => r.status === status).length;
            const errors = data.results.filter((r) => r.status === 'error');
            let message = `${count('created')} importé(s), ${count('duplicate')} déjà présent(s)`;
            if (errors.length) message += `, ${errors.length} en erreur (${errors[0].filename} : ${errors[0].message})`;
            notify(message, errors.length ? 'warning' : 'success');
            await refresh();
        } catch (error) { notify(error.message, 'danger'); }
    }

    function bindSwitch(id, key, attr, after) {
        document.getElementById(id).addEventListener('click', (event) => {
            const button = event.target.closest('button');
            if (!button) return;
            document.querySelectorAll(`#${id} button`).forEach((b) =>
                b.setAttribute('aria-pressed', b === button ? 'true' : 'false'));
            state[key] = button.dataset[attr];
            after();
        });
    }

    document.addEventListener('DOMContentLoaded', () => {
        bindSwitch('periodSwitch', 'period', 'period', () => loadDashboard().catch((e) => notify(e.message, 'danger')));
        bindSwitch('metricSwitch', 'metric', 'metric', renderChart);
        // Grille, axes et infobulles sont résolus à la création : on recrée au changement de thème
        document.addEventListener('themechange', renderChart);
        state.typeLabels = App.typeLabels(document.getElementById('typeFilter'));
        document.getElementById('yearNoticeButton').addEventListener('click', (event) => {
            state.year = Number(event.currentTarget.dataset.year) || null;
            loadDashboard().catch((e) => notify(e.message, 'danger'));
        });
        document.getElementById('typeFilter').addEventListener('change', (event) => {
            state.type = event.target.value;
            state.activities = []; // autre filtre : la liste repart de la première page
            refresh();
        });
        document.getElementById('loadMoreButton').addEventListener('click', () => {
            loadActivities({ more: true }).catch((e) => notify(e.message, 'danger'));
        });
        const input = document.getElementById('importInput');
        document.getElementById('importButton').addEventListener('click', () => input.click());
        input.addEventListener('change', () => {
            if (input.files.length) importFiles(input.files).finally(() => { input.value = ''; });
        });
        refresh();
    });
})();
