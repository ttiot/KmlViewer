'use strict';

(() => {
    const { api, el, icon, notify, number, duration, chrono, distance, speed, date } = App;
    const state = { period: 'week', metric: 'distance_km', type: '', stats: null, chart: null };

    const METRICS = {
        distance_km: { label: 'Distance (km)', title: 'Distance', format: (v) => `${number(v, 1)} km` },
        ascent_m: { label: 'Dénivelé positif (m)', title: 'Dénivelé positif', format: (v) => `${number(v)} m` },
        time_s: { label: 'Temps (h)', title: 'Temps', format: (v) => duration(v), scale: (v) => v / 3600 },
    };

    function tile(iconName, label, value) {
        return el('div', { class: 'col-6 col-lg-3' },
            el('div', { class: 'card stat-tile' },
                el('div', { class: 'card-body' },
                    el('span', { class: 'stat-icon' }, icon(iconName)),
                    el('div', {},
                        el('div', { class: 'stat-value' }, value),
                        el('div', { class: 'stat-label' }, label)))));
    }

    function renderTotals(stats) {
        const year = stats.current_year;
        document.getElementById('totals').replaceChildren(
            tile('road', `Distance ${year.year}`, `${number(year.distance_km, 1)} km`),
            tile('mountain', `Dénivelé + ${year.year}`, `${number(year.ascent_m)} m`),
            tile('clock', `Temps ${year.year}`, duration(year.time_s)),
            tile('flag-checkered', `Activités ${year.year} · total ${stats.totals.count}`, String(year.count)));
    }

    function renderChart() {
        if (!state.stats) return;
        const metric = METRICS[state.metric];
        const scale = metric.scale || ((v) => v);
        const series = state.stats.series;
        const periodLabel = { week: 'semaine', month: 'mois', year: 'année' }[state.period];
        document.getElementById('chartTitle').textContent = `${metric.title} par ${periodLabel}`;
        const config = {
            type: 'bar',
            data: {
                labels: series.map((s) => s.period),
                datasets: [{
                    label: metric.label,
                    data: series.map((s) => scale(s[state.metric])),
                    // Couleurs lues dans les jetons à chaque rendu : suit le thème clair / sombre
                    backgroundColor: () => AppTheme.color('--viz-1'),
                    hoverBackgroundColor: () => AppTheme.color('--viz-1'),
                    // Barres fines, extrémité de donnée arrondie, base carrée sur l'axe
                    maxBarThickness: 24,
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
                    x: { grid: { display: false }, border: { display: false } },
                },
            },
        };
        if (state.chart) state.chart.destroy();
        state.chart = new Chart(document.getElementById('volumeChart'), config);
    }

    function recordValue(record) {
        if (record.kind === 'distance') return distance(record.value);
        if (record.kind === 'ascent') return `${number(record.value)} m`;
        return record.label.startsWith('Meilleur') ? chrono(record.value) : duration(record.value);
    }

    function renderRecords(records) {
        const list = document.getElementById('records');
        if (!records.length) {
            list.replaceChildren(el('div', { class: 'empty-state' },
                icon('trophy'), el('strong', {}, 'Pas encore de record'),
                el('span', {}, 'Ils apparaîtront dès ta première activité.')));
            return;
        }
        const recordIcon = (r) => (r.kind === 'distance' ? 'road' : r.kind === 'ascent' ? 'mountain'
            : r.label.startsWith('Meilleur') ? 'bolt' : 'clock');
        list.replaceChildren(...records.map((r) =>
            el('div', { class: 'record-item' },
                el('span', { class: 'record-icon' }, icon(recordIcon(r))),
                el('div', { class: 'min-w-0' },
                    el('div', { class: 'record-label' }, r.label),
                    el('a', { class: 'record-source', href: `/?activity=${r.activity_id}` },
                        `${r.activity_name} · ${date(r.date)}`)),
                el('span', { class: 'record-value' }, recordValue(r)))));
    }

    function activityRow(a) {
        const time = a.moving_s || a.duration_s;
        const ascent = el('td', { class: 'text-end' }, a.ascent_m ? `${number(a.ascent_m)} m ` : '— ');
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
        const typeIcon = { running: 'person-running', cycling: 'person-biking', hiking: 'person-hiking',
                           walking: 'person-walking', swimming: 'person-swimming' }[(a.activity_type || '').toLowerCase()];
        return el('tr', {},
            el('td', { class: 'text-nowrap text-muted' }, date(a.start_time)),
            el('td', {}, el('a', { class: 'activity-name', href: `/?activity=${a.id}` }, a.name)),
            el('td', {}, a.activity_type
                ? el('span', { class: 'type-chip' }, typeIcon ? icon(typeIcon) : null, a.activity_type) : '—'),
            el('td', { class: 'text-end' }, distance(a.distance_m)),
            ascent,
            el('td', { class: 'text-end' }, time ? duration(time) : '—'),
            el('td', { class: 'text-end' }, speed(a.distance_m, time)),
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

    async function loadActivities() {
        const query = state.type ? `?type=${encodeURIComponent(state.type)}` : '';
        const data = await api(`/api/activities${query}`);
        state.demEnabled = data.dem_enabled;
        document.getElementById('activities').replaceChildren(...data.activities.map(activityRow));
        document.getElementById('activitiesEmpty').hidden = data.activities.length > 0;
        document.getElementById('activitiesTable').hidden = data.activities.length === 0;

        const select = document.getElementById('typeFilter');
        select.replaceChildren(el('option', { value: '' }, 'Tous les types'),
            ...data.types.map((t) => el('option', { value: t, selected: t === state.type }, t)));
    }

    async function loadDashboard() {
        const params = new URLSearchParams({ period: state.period });
        if (state.type) params.set('type', state.type);
        const data = await api(`/api/dashboard?${params}`);
        state.stats = data.stats;
        renderTotals(data.stats);
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
            document.querySelectorAll(`#${id} button`).forEach((b) => b.classList.toggle('active', b === button));
            state[key] = button.dataset[attr];
            after();
        });
    }

    document.addEventListener('DOMContentLoaded', () => {
        bindSwitch('periodSwitch', 'period', 'period', () => loadDashboard().catch((e) => notify(e.message, 'danger')));
        bindSwitch('metricSwitch', 'metric', 'metric', renderChart);
        // Grille, axes et infobulles sont résolus à la création : on recrée au changement de thème
        document.addEventListener('themechange', renderChart);
        document.getElementById('typeFilter').addEventListener('change', (event) => {
            state.type = event.target.value;
            refresh();
        });
        const input = document.getElementById('importInput');
        document.getElementById('importButton').addEventListener('click', () => input.click());
        input.addEventListener('change', () => {
            if (input.files.length) importFiles(input.files).finally(() => { input.value = ''; });
        });
        refresh();
    });
})();
