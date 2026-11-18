'use strict';

(() => {
    const { api, el, icon, notify, number, duration, chrono, distance, speed, date } = App;
    const state = { period: 'week', metric: 'distance_km', type: '', stats: null, chart: null };

    const METRICS = {
        distance_km: { label: 'Distance (km)', format: (v) => `${number(v, 1)} km` },
        ascent_m: { label: 'Dénivelé positif (m)', format: (v) => `${number(v)} m` },
        time_s: { label: 'Temps (h)', format: (v) => duration(v), scale: (v) => v / 3600 },
    };

    function tile(label, value) {
        return el('div', { class: 'col-6 col-lg-3' },
            el('div', { class: 'card stat-tile h-100' },
                el('div', { class: 'card-body' },
                    el('div', { class: 'stat-value' }, value),
                    el('div', { class: 'stat-label' }, label))));
    }

    function renderTotals(stats) {
        const year = stats.current_year;
        document.getElementById('totals').replaceChildren(
            tile(`Distance ${year.year}`, `${number(year.distance_km, 1)} km`),
            tile(`D+ ${year.year}`, `${number(year.ascent_m)} m`),
            tile(`Temps ${year.year}`, duration(year.time_s)),
            tile(`Activités ${year.year} / total`, `${year.count} / ${stats.totals.count}`));
    }

    function renderChart() {
        if (!state.stats) return;
        const metric = METRICS[state.metric];
        const scale = metric.scale || ((v) => v);
        const series = state.stats.series;
        const config = {
            type: 'bar',
            data: {
                labels: series.map((s) => s.period),
                datasets: [{ label: metric.label, data: series.map((s) => scale(s[state.metric])),
                             backgroundColor: '#667eea', borderRadius: 3 }],
            },
            options: {
                maintainAspectRatio: false,
                plugins: {
                    legend: { display: false },
                    tooltip: { callbacks: { label: (ctx) => metric.format(series[ctx.dataIndex][state.metric]) } },
                },
                scales: { y: { beginAtZero: true, title: { display: true, text: metric.label } },
                          x: { grid: { display: false } } },
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
            list.replaceChildren(el('li', { class: 'list-group-item text-muted small px-0' }, 'Pas encore de record.'));
            return;
        }
        list.replaceChildren(...records.map((r) =>
            el('li', { class: 'list-group-item px-0' },
                el('div', { class: 'd-flex justify-content-between' },
                    el('span', {}, r.label), el('strong', {}, recordValue(r))),
                el('a', { class: 'small text-muted text-decoration-none', href: `/?activity=${r.activity_id}` },
                    `${r.activity_name} · ${date(r.date)}`))));
    }

    function activityRow(a) {
        const time = a.moving_s || a.duration_s;
        const ascent = el('td', { class: 'text-end' }, a.ascent_m ? `${number(a.ascent_m)} m ` : '— ');
        if (a.elev_source === 'dem') {
            ascent.append(el('span', { class: 'badge badge-dem', title: `Altitude corrigée par MNT (GPS : ${number(a.ascent_gps_m)} m)` }, 'MNT'));
        }
        const actions = el('td', { class: 'text-end text-nowrap' },
            el('a', { class: 'btn btn-sm btn-outline-primary me-1', href: `/?activity=${a.id}`, title: 'Voir sur la carte' }, icon('map')),
            el('button', { class: 'btn btn-sm btn-outline-secondary me-1', type: 'button', title: 'Renommer', onclick: () => renameActivity(a) }, icon('pen')),
            state.demEnabled ? el('button', { class: 'btn btn-sm btn-outline-secondary me-1', type: 'button', title: "Corriger l'altitude (MNT)", onclick: () => correctElevation(a) }, icon('mountain')) : null,
            el('button', { class: 'btn btn-sm btn-outline-danger', type: 'button', title: 'Supprimer', onclick: () => deleteActivity(a) }, icon('trash')));
        return el('tr', {},
            el('td', { class: 'text-nowrap' }, date(a.start_time)),
            el('td', {}, a.name),
            el('td', {}, a.activity_type || '—'),
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
