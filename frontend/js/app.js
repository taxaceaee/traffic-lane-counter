let BASE_URL = 'http://localhost:8000';
let activeTab = 'dashboard';
let camerasList = [], modelsList = [], jobsList = [];
const _loadedPages = new Set();

// Live session state
let _sessionCounts = {};
let _ws = null;
let _wsReconnectTimer = null;
let _liveMetricsTimer = null;
let _lastLiveCam = null;

// Health dashboard state
let _healthTimer = null;
let _healthHistory = [];

// ApexCharts instances
let chartHourly = null, chartVehicleTypes = null, chartMetrics = null;
let chartVolumes = null, chartAverages = null, chartHeatmap = null, chartCountingLanes = null;

// ── Route map: tabId → clean URL path ────────────────────────────────────
const ROUTE_MAP = {
    dashboard: '/',
    live: '/live',
    counting: '/counting',
    alerts: '/alerts',
    cameras: '/cameras',
    lanes: '/lanes',
    jobs: '/jobs',
    models: '/models',
    analytics: '/analytics',
    events: '/events',
    reports: '/reports',
    health: '/health',
    users: '/users',
    settings: '/settings',
};

function urlForTab(tabId) {
    return ROUTE_MAP[tabId] || '/' + tabId;
}

function tabFromPath(pathname) {
    if (!pathname || pathname === '/') return 'dashboard';
    const segment = pathname.replace(/^\/|\/$/g, '').split('/')[0];
    for (const [tabId, url] of Object.entries(ROUTE_MAP)) {
        if (url === '/' + segment) return tabId;
    }
    return 'dashboard';
}

// ── Page loading ──────────────────────────────────────────────────────────

async function loadPage(tabId) {
    const container = document.getElementById('page-content-area');
    if (!container) return;
    // Remove cached content so fresh HTML always loads
    const existing = document.getElementById('page-content-' + tabId);
    if (existing) existing.remove();
    _loadedPages.delete(tabId);
    try {
        const htmlResp = await fetch(`pages/${tabId}.html?t=${Date.now()}`);
        if (!htmlResp.ok) throw new Error('HTTP ' + htmlResp.status);
        const html = await htmlResp.text();
        container.insertAdjacentHTML('beforeend', html);
        _loadedPages.add(tabId);
        lucide.createIcons();
    } catch (e) {
        console.warn(`Failed to load page "${tabId}":`, e);
    }
}

function loadPageScript(tabId) {
    // No-op: app.js is a pre-concatenated bundle of core + all page scripts.
    // All code runs in the same module scope, so dynamic re-injection would
    // cause 'const' / 'let' redeclaration errors.
}

// ── Routing ───────────────────────────────────────────────────────────────

window.onload = async function() {
    const savedUrl = localStorage.getItem('api_url');
    if (savedUrl) { BASE_URL = savedUrl; const el = document.getElementById('settings-api-url'); if (el) el.value = BASE_URL; }
    lucide.createIcons();
    const tab = tabFromPath(window.location.pathname);
    await switchTab(tab);
};

window.addEventListener('popstate', () => {
    const tab = tabFromPath(window.location.pathname);
    if (tab !== activeTab) switchTab(tab);
});

window.addEventListener('unhandledrejection', (event) => {
    console.error('Unhandled rejection:', event.reason);
    if (typeof showToast === 'function') {
        showToast({severity: 'critical', title: 'System Error', message: 'An unexpected error occurred. Please refresh.'});
    }
});

async function switchTab(tabId) {
    activeTab = tabId;
    const isNewPage = !_loadedPages.has(tabId);

    const targetPath = urlForTab(tabId);
    if (window.location.pathname !== targetPath) {
        history.pushState(null, '', targetPath);
    }

    await loadPage(tabId);

    document.querySelectorAll('.tab-content').forEach(el => el.classList.add('hidden'));
    document.querySelectorAll('.nav-btn').forEach(el => el.classList.remove('nav-active'));

    const target = document.getElementById('page-content-' + tabId);
    if (target) target.classList.remove('hidden');
    const btn = document.getElementById('btn-' + tabId);
    if (btn) btn.classList.add('nav-active');

    document.title = 'TrafficFlow — ' + (tabId.charAt(0).toUpperCase() + tabId.slice(1));

    if (isNewPage) {
        await refreshData();
    }

    if (tabId === 'dashboard') {
        await renderDashboardCharts();
    }

    updatePageHeader(tabId);

    if (tabId === 'alerts') { await refreshAlerts(); }
    if (tabId === 'health') { startHealthPolling(); }
    if (tabId === 'analytics') { loadAnalyticsData(); }
    if (tabId === 'counting') { applyCountingFilter(); }
    if (tabId === 'reports') { loadReportsData(); }
    if (tabId === 'events') { loadEventsData(); }
    if (tabId === 'settings') { loadSettings(); }
    if (tabId === 'users') { loadCurrentUserRole(); loadUsersData(); loadAuditData(); }
    if (tabId === 'live') {
        // loadLiveCameraData is already called by refreshData() on first visit.
        // For re-visits (isNewPage=false), refreshData() is skipped so we call it here.
        if (!isNewPage) {
            if (camerasList.length) {
                populateSelectors();
                loadLiveCameraData();
            } else {
                await refreshData();
            }
        }
    }
    if (tabId === 'lanes') {
        // Ensure cameras data is available even if page was cached
        if (!camerasList.length) await refreshData();
        loadLanesConfigEditor();
    }

    if (tabId !== 'health') { stopHealthPolling(); }
}

function updatePageHeader(tabId) {
    const headers = {
        dashboard: ["Dashboard", "Traffic overview — today's KPIs, trends, and top cameras."],
        live:      ["Live Monitoring", "Real-time annotated video, lane occupancy, and AI inference metrics."],
        counting:  ["Vehicle Counting", "Count vehicles by lane, type, camera and time range. Export CSV."],
        alerts:    ["Alert System", "Active alerts and historical notification log."],
        cameras:   ["Camera Management", "Register, configure and inspect camera sources."],
        lanes:     ["Lane Configuration", "Edit lane polygon coordinates and counting lines per camera."],
        jobs:      ["Inference Jobs", "Launch and monitor video inference jobs."],
        models:    ["Model Management", "Review registered YOLO detection models and parameters."],
        analytics: ["Traffic Analytics", "Density heatmap, volume trends, and occupancy statistics."],
        events:    ["Lane-change Events", "Full log of vehicle lane-change events."],
        reports:   ["Reports", "Benchmark evaluation metrics: mAP, IDF1, counting accuracy."],
        health:    ["System Health", "API, database, GPU, and worker node status."],
        users:     ["Users & Audit", "User management and administration audit trail."],
        settings:  ["Settings", "API connection, AI model parameters, and output configuration."]
    };
    if (headers[tabId]) {
        document.getElementById('page-title').innerText = headers[tabId][0];
        document.getElementById('page-subtitle').innerText = headers[tabId][1];
    }
}

// ── API Client ────────────────────────────────────────────────────────────

async function apiRequest(path, options = {}) {
    try {
        const token = localStorage.getItem('access_token');
        const headers = options.headers || {};
        if (token) headers['Authorization'] = 'Bearer ' + token;
        const res = await fetch(BASE_URL + path, { ...options, headers });

        if (res.status === 401 && token) {
            const refreshed = await refreshToken();
            if (refreshed) {
                headers['Authorization'] = 'Bearer ' + localStorage.getItem('access_token');
                const retry = await fetch(BASE_URL + path, { ...options, headers });
                if (retry.ok) return await retry.json();
            } else {
                localStorage.removeItem('access_token');
                localStorage.removeItem('refresh_token');
                showToast({severity: 'warning', title: 'Session Expired', message: 'Please log in again.'});
                return null;
            }
        }

        if (!res.ok) throw new Error('HTTP ' + res.status);
        return await res.json();
    } catch (e) { return null; }
}

async function apiRequestWithHeaders(path, headers = {}) {
    try {
        const res = await fetch(BASE_URL + path, { headers });
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return await res.json();
    } catch (e) { return null; }
}

// ── Shared helpers ────────────────────────────────────────────────────────

function destroyChart(ref) { if (ref) { try { ref.destroy(); } catch(e){} } return null; }

function clearContainer(id) {
    const el = document.querySelector(id);
    if (el) el.innerHTML = '';
}

function populateSelectors() {
    const camOpts = camerasList.map(c => `<option value="${c.camera_id}">${c.camera_id} — ${c.name}</option>`).join('');
    const modelOpts = modelsList.map(m => `<option value="${m.model_id}">${m.model_id}</option>`).join('');
    ['overview-cam-select','live-cam-select','lanes-cam-select','analytics-cam-select'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.innerHTML = camOpts;
    });
    const cf = document.getElementById('count-filter-camera');
    if (cf) cf.innerHTML = '<option value="">— Select camera —</option>' + camOpts;
    const om = document.getElementById('overview-model-select');
    if (om) om.innerHTML = modelOpts;
}

// ── Data refresh (called once per page-load / manual refresh) ─────────────

async function refreshData() {
    const now = new Date();
    document.getElementById('last-refresh-time').innerText = 'Last sync: ' + now.toLocaleTimeString();

    const health = await apiRequest('/api/health');
    const connBadge = document.getElementById('conn-badge');
    if (!health) {
        connBadge.innerText = 'OFFLINE';
        connBadge.className = 'text-[9px] font-bold px-2 py-0.5 rounded bg-rose-500/10 text-rose-400 border border-rose-500/20';
        showConnectionError();
        return;
    }
    connBadge.innerText = 'ONLINE';
    connBadge.className = 'text-[9px] font-bold px-2 py-0.5 rounded bg-emerald-500/10 text-emerald-400 border border-emerald-500/20';
    const gpuBadge = document.getElementById('health-gpu-badge');
    const gpuText = document.getElementById('health-gpu-text');
    const gpuInfo = health && health.dependencies && health.dependencies.gpu;
    if (gpuBadge && gpuInfo && gpuInfo.available) {
        gpuBadge.className = 'px-2 py-0.5 rounded text-[10px] font-bold bg-emerald-500/10 text-emerald-400 border border-emerald-500/20';
        gpuBadge.innerText = '● ONLINE';
        if (gpuText) gpuText.innerText = gpuInfo.name || 'GPU Active';
    }

    const [cams, models, jobs] = await Promise.all([
        apiRequest('/api/cameras'),
        apiRequest('/api/models'),
        apiRequest('/api/jobs'),
    ]);
    camerasList = cams || [];
    modelsList = models || [];
    jobsList = jobs || [];

    if (!cams || !models || !jobs) {
        showPartialError('Some data sources are unavailable');
    }

    const kpiActiveCameras = document.getElementById('kpi-active-cameras');
    if (kpiActiveCameras) kpiActiveCameras.innerText = cams ? cams.filter(c => c.status === 'configured').length : '—';
    const kpiCamerasSub = document.getElementById('kpi-cameras-sub');
    if (kpiCamerasSub) kpiCamerasSub.innerText = (cams ? cams.length : 0) + ' total registered';

    updateAlertBadge();

    populateSelectors();
    renderJobsTable();
    renderCamerasGrid();
    renderModelsList();
    loadEventsData();
    loadLiveCameraData();
}

async function fullRefresh() {
    await refreshData();
    if (activeTab === 'dashboard') await renderDashboardCharts();
    if (activeTab === 'alerts') await refreshAlerts();
    if (activeTab === 'health') fetchHealthData();
}

// ── Connection Error UI ─────────────────────────────────────────────────

function showConnectionError() {
    const area = document.getElementById('page-content-area');
    if (!area) return;
    area.innerHTML = `
        <div class="flex flex-col items-center justify-center py-20">
            <i data-lucide="wifi-off" class="h-16 w-16 text-rose-500 mb-4"></i>
            <h2 class="text-xl font-bold text-white mb-2">Connection Lost</h2>
            <p class="text-slate-400 mb-6">Unable to reach the server at ${BASE_URL}</p>
            <button onclick="fullRefresh()" class="px-6 py-2 bg-indigo-600 text-white rounded-lg hover:bg-indigo-500 transition-all">
                Retry Connection
            </button>
        </div>
    `;
    lucide.createIcons();
}

function showPartialError(msg) {
    showToast({severity: 'warning', title: 'Data Issue', message: msg});
}

// ── Shared helpers ──────────────────────────────────────────────────────

async function downloadCSVLogs() {
    const camera_id = document.getElementById('events-cam-select')?.value;
    if (!camera_id) { showToast({severity:'warning', title:'No Camera', message:'Select a camera first to export real events.'}); return; }
    const url = BASE_URL + `/api/cameras/${camera_id}/lane-changes?limit=10000`;
    try {
        const r = await fetch(url, {headers: {'Authorization': 'Bearer ' + (localStorage.getItem('access_token') || '')}});
        if (!r.ok) { showToast({severity:'warning', title:'Export Failed', message:'HTTP ' + r.status}); return; }
        const data = await r.json();
        if (!data || !data.length) { showToast({severity:'info', title:'No Data', message:'No events to export.'}); return; }
        let csv = 'event_id,camera_id,track_id,class_name,from_lane,to_lane,frame_id\n';
        data.forEach(e => { csv += e.id + ',' + e.camera_id + ',' + e.track_id + ',' + (e.class_name||'') + ',' + (e.previous_lane_id||'') + ',' + (e.current_lane_id||'') + ',' + e.frame_id + '\n'; });
        const blob = new Blob([csv], {type:'text/csv'});
        const a = document.createElement('a');
        a.href = window.URL.createObjectURL(blob);
        a.download = camera_id + '_events_export.csv';
        a.click();
    } catch (e) { showToast({severity:'warning', title:'Export Error', message:e.message}); }
}

function showAddCameraModal() {
    if (typeof showAddCameraForm === 'function') {
        showAddCameraForm();
    } else {
        alert('Add Camera dialog loading...');
    }
}

async function doLogout() {
    await apiRequest('/api/auth/logout', { method: 'POST' });
    localStorage.removeItem('access_token');
    localStorage.removeItem('refresh_token');
    window.location.reload();
}

async function refreshToken() {
    const refresh = localStorage.getItem('refresh_token');
    if (!refresh) return false;
    try {
        const res = await fetch(BASE_URL + '/api/auth/refresh', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({refresh_token: refresh})
        });
        if (res.ok) {
            const data = await res.json();
            localStorage.setItem('access_token', data.access_token);
            localStorage.setItem('refresh_token', data.refresh_token);
            return true;
        }
    } catch(e) {}
    return false;
}
function showAlertToast(alert) {
    const container = document.getElementById('toast-container');
    if (!container) return;
    const severity = alert.severity || 'info';
    const colors = { critical: 'rose', warning: 'amber', info: 'blue' };
    const icons = { critical: 'alert-circle', warning: 'alert-triangle', info: 'info' };
    const color = colors[severity] || 'slate';
    const icon = icons[severity] || 'bell';
    const id = 'toast-' + Date.now() + '-' + Math.random().toString(36).slice(2, 6);

    const el = document.createElement('div');
    el.id = id;
    el.className = 'pointer-events-auto flex items-start gap-3 px-4 py-3 rounded-xl border shadow-2xl transition-all translate-x-full opacity-0';
    el.style.borderColor = 'rgba(var(--color-border), 0.2)';
    el.style.background = 'rgba(2, 6, 23, 0.95)';
    el.style.backdropFilter = 'blur(12px)';
    el.style.borderLeft = '3px solid ' + (severity === 'critical' ? '#ef4444' : severity === 'warning' ? '#f59e0b' : '#3b82f6');
    el.innerHTML = `
        <i data-lucide="${icon}" class="h-5 w-5 flex-shrink-0 mt-0.5 text-${color}-400"></i>
        <div class="flex-1 min-w-0">
            <p class="text-sm font-bold text-white truncate">${alert.title || 'Alert'}</p>
            <p class="text-xs text-slate-400 mt-0.5">${alert.message || ''}</p>
            ${alert.camera_id ? `<p class="text-[10px] text-slate-500 mt-0.5">${alert.camera_id}</p>` : ''}
        </div>
        <button onclick="document.getElementById('${id}').remove()" class="text-slate-500 hover:text-slate-300 flex-shrink-0">
            <i data-lucide="x" class="h-3.5 w-3.5"></i>
        </button>`;
    container.appendChild(el);
    lucide.createIcons();

    requestAnimationFrame(() => {
        el.classList.remove('translate-x-full', 'opacity-0');
        el.classList.add('translate-x-0', 'opacity-100');
    });

    setTimeout(() => {
        el.classList.add('translate-x-full', 'opacity-0');
        el.classList.remove('translate-x-0', 'opacity-100');
        setTimeout(() => el.remove(), 400);
    }, 8000);

    while (container.children.length > 5) {
        container.firstChild.remove();
    }
}

// showToast alias — every function in the codebase calls showToast()
function showToast(alert) { showAlertToast(alert); }

async function updateAlertBadge() {
    try {
        const countData = await apiRequest('/api/alerts/count');
        const count = countData ? countData.count : 0;
        const badge = document.getElementById('alert-count-badge');
        const topbar = document.getElementById('alert-topbar');
        const topbarText = document.getElementById('alert-topbar-text');

        if (badge) {
            badge.innerText = count;
            count > 0 ? badge.classList.remove('hidden') : badge.classList.add('hidden');
        }

        if (topbar && topbarText) {
            if (count > 0) {
                topbarText.innerText = count + ' Active Alert' + (count !== 1 ? 's' : '');
                topbar.classList.remove('hidden');
                topbar.classList.add('flex');
            } else {
                topbar.classList.add('hidden');
                topbar.classList.remove('flex');
            }
        }
    } catch (e) {}
}

async function refreshAlerts() {
    const [active, history] = await Promise.all([
        apiRequest('/api/alerts'),
        apiRequest('/api/alerts/history?limit=50')
    ]);

    let crit = 0, warn = 0, info = 0;
    if (active) {
        active.forEach(a => {
            if (a.severity === 'critical') crit++;
            else if (a.severity === 'warning') warn++;
            else info++;
        });
    }
    const set = (id, val) => { const el = document.getElementById(id); if (el) el.innerText = val; };
    set('alerts-count-critical', crit);
    set('alerts-count-warning', warn);
    set('alerts-count-info', info);

    const container = document.getElementById('alerts-active-container');
    if (container) {
        if (active && active.length) {
            container.innerHTML = active.map(a => {
                const sev = a.severity || 'info';
                const icons = { critical: 'wifi-off', warning: 'traffic-cone', info: 'car' };
                const colors = { critical: 'rose', warning: 'amber', info: 'blue' };
                const ico = icons[sev] || 'bell';
                const color = colors[sev] || 'slate';
                const ts = a.timestamp ? new Date(a.timestamp).toLocaleTimeString() : '—';
                return `<div class="alert-${sev} bg-${color}-500/5 rounded-lg p-4 flex items-start gap-4">
                    <i data-lucide="${ico}" class="h-5 w-5 text-${color}-400 mt-0.5 flex-shrink-0"></i>
                    <div class="flex-1">
                        <div class="flex justify-between">
                            <p class="text-sm font-bold text-${color}-400">${a.title || 'Alert'}</p>
                            <span class="text-xs text-slate-500">${ts}</span>
                        </div>
                        <p class="text-xs text-slate-400 mt-1">${a.message || ''}</p>
                        ${a.camera_id ? `<p class="text-xs text-slate-500 mt-1">Camera: ${a.camera_id}</p>` : ''}
                    </div>
                    <button onclick="dismissAlert('${a.id}')" class="text-xs text-${color}-400 border border-${color}-500/30 rounded px-2 py-1 hover:bg-${color}-500/10 flex-shrink-0">Dismiss</button>
                </div>`;
            }).join('');
        } else {
            container.innerHTML = '<p class="text-xs text-slate-500 text-center py-4">No active alerts</p>';
        }
        lucide.createIcons();
    }

    const tbody = document.getElementById('alerts-history-tbody');
    if (tbody) {
        if (history && history.length) {
            tbody.innerHTML = history.map(a => {
                const sev = a.severity || 'info';
                const sevColors = {
                    critical: 'bg-rose-500/10 text-rose-400 border-rose-500/20',
                    warning: 'bg-amber-500/10 text-amber-400 border-amber-500/20',
                    info: 'bg-blue-500/10 text-blue-400 border-blue-500/20'
                };
                const sevClass = sevColors[sev] || 'bg-slate-500/10 text-slate-400 border-slate-500/20';
                const ts = a.timestamp ? new Date(a.timestamp).toLocaleTimeString() : '—';
                const resolved = a.resolved_at ? new Date(a.resolved_at).toLocaleTimeString() : (a.resolved ? 'Resolved' : 'Active');
                const resolvedClass = a.resolved || a.resolved_at ? 'text-emerald-400' : 'text-amber-400';
                return `<tr class="border-b border-slate-800/50 hover:bg-slate-900/20">
                    <td class="px-4 py-3 text-slate-500">${ts}</td>
                    <td class="px-4 py-3"><span class="text-[10px] font-bold px-2 py-0.5 rounded border ${sevClass}">${sev.toUpperCase()}</span></td>
                    <td class="px-4 py-3">${a.alert_type || '—'}</td>
                    <td class="px-4 py-3 text-white">${a.camera_id || '—'}</td>
                    <td class="px-4 py-3 text-slate-300">${a.title || ''}</td>
                    <td class="px-4 py-3 ${resolvedClass}">${resolved}</td>
                </tr>`;
            }).join('');
        } else {
            tbody.innerHTML = '<tr><td colspan="6" class="px-4 py-8 text-center text-slate-600 text-xs">No alert history</td></tr>';
        }
    }
}

async function dismissAlert(alertId) {
    const token = localStorage.getItem('access_token');
    if (!token) return;
    try {
        const res = await fetch(BASE_URL + '/api/alerts/' + alertId + '/resolve', {
            method: 'PATCH',
            headers: { 'Authorization': 'Bearer ' + token }
        });
        if (res.ok) {
            await refreshAlerts();
            await updateAlertBadge();
        }
    } catch (e) {
        console.warn('Failed to dismiss alert:', e);
    }
}
function loadAnalyticsData() {
    renderAnalyticsCharts('day');
}

function setAnalyticsPeriod(period) {
    ['day','week','month'].forEach(p => {
        const btn = document.getElementById('period-' + p);
        if (p === period) { btn.className = 'px-3 py-1.5 rounded text-xs font-semibold text-white bg-indigo-600'; }
        else { btn.className = 'px-3 py-1.5 rounded text-xs font-semibold text-slate-400 hover:text-white'; }
    });
    renderAnalyticsCharts(period);
}

async function renderAnalyticsCharts(period) {
    chartHeatmap = destroyChart(chartHeatmap);
    chartVolumes = destroyChart(chartVolumes);
    chartAverages = destroyChart(chartAverages);

    const camera_id = document.getElementById('analytics-cam-select').value;
    if (!camera_id) return;

    const now = new Date();
    let since;
    if (period === 'day') since = new Date(now.getTime() - 24*3600000);
    else if (period === 'week') since = new Date(now.getTime() - 7*24*3600000);
    else since = new Date(now.getTime() - 30*24*3600000);

    const sinceStr = since.toISOString();
    const untilStr = now.toISOString();

    const [summary, timeseries, occ] = await Promise.all([
        apiRequest(`/api/cameras/${camera_id}/counts/summary?since=${encodeURIComponent(sinceStr)}&until=${encodeURIComponent(untilStr)}`),
        apiRequest(`/api/cameras/${camera_id}/counts/timeseries?window=1hour&limit=168`),
        apiRequest(`/api/cameras/${camera_id}/occupancy/latest`),
    ]);

    const lanes = summary ? summary.lanes : [];
    const typeColors = ['#6366f1','#10b981','#f59e0b','#ef4444','#38bdf8','#a78bfa','#fb923c'];

    // Heatmap
    if (timeseries && timeseries.data && timeseries.data.length) {
        const ts = timeseries.data;
        const laneGroups = {};
        ts.forEach(d => {
            const lid = d.lane_id;
            if (!laneGroups[lid]) laneGroups[lid] = {};
            const h = d.timestamp ? d.timestamp.substring(11,13) + 'h' : '?';
            laneGroups[lid][h] = (laneGroups[lid][h] || 0) + (d.count || 0);
        });

        const allHours = Array.from({length:24}, (_,i) => String(i).padStart(2,'0') + 'h');
        const heatSeries = Object.entries(laneGroups).map(([lid, hours]) => ({
            name: lid,
            data: allHours.map(h => ({ x: h, y: hours[h] || 0 })),
        }));

        if (heatSeries.length) {
            const heatHeight = Math.max(280, 160 + heatSeries.length * 50);
            const heatEl = document.querySelector('#chart-heatmap');
            if (heatEl) heatEl.style.height = heatHeight + 'px';

            const heatOpts = {
                series: heatSeries,
                chart: { type:'heatmap', height:heatHeight, background:'transparent', toolbar:{show:false} },
                theme: { mode:'dark' },
                colors: ['#6366f1'],
                dataLabels: { enabled:false },
                xaxis: { labels: {style:{fontSize:'9px'}} },
                tooltip: { theme:'dark' },
                plotOptions: { heatmap: { shadeIntensity: 0.6 } }
            };
            chartHeatmap = new ApexCharts(heatEl, heatOpts);
            chartHeatmap.render();
        }
    }

    // Volume per lane
    if (lanes.length) {
        const volHeight = Math.max(210, 160 + Math.max(0, lanes.length - 3) * 36);
        const volEl = document.querySelector('#chart-analytics-volumes');
        if (volEl) volEl.style.height = volHeight + 'px';

        const volOpts = {
            series: [{ name: 'Vehicles', data: lanes.map(l => l.total || 0) }],
            chart: { type:'bar', height:volHeight, background:'transparent', toolbar:{show:false} },
            theme: { mode:'dark' },
            colors: [typeColors[0]],
            xaxis: {
                categories: lanes.map(l => l.lane_id),
                labels: { style:{fontSize:'10px'}, rotate: lanes.length > 6 ? -45 : 0, hideOverlappingLabels:true, maxHeight:80 },
            },
            yaxis: { min: 0, labels: { style: {fontSize:'10px'} } },
            grid: { borderColor:'#1e293b', strokeDashArray: 3 },
            tooltip: { theme:'dark' },
            dataLabels: { enabled: false },
            plotOptions: { bar: { borderRadius:4, columnWidth: Math.max(30, 80 - lanes.length * 4) + '%' } }
        };
        chartVolumes = new ApexCharts(volEl, volOpts);
        chartVolumes.render();
    }

    // Occupancy
    const occData = occ && occ.occupancy ? occ.occupancy : {};
    const occLanes = Object.keys(occData).filter(k => k !== 'no_recent_data');
    if (occLanes.length) {
        const occHeight = Math.max(210, 160 + Math.max(0, occLanes.length - 3) * 36);
        const occEl = document.querySelector('#chart-analytics-occupancy');
        if (occEl) occEl.style.height = occHeight + 'px';

        const occOpts = {
            series: [{ name: 'Occupancy', data: occLanes.map(k => occData[k] || 0) }],
            chart: { type:'bar', height:occHeight, background:'transparent', toolbar:{show:false} },
            theme: { mode:'dark' },
            colors: ['#10b981'],
            xaxis: {
                categories: occLanes,
                labels: { style:{fontSize:'10px'}, rotate: occLanes.length > 6 ? -45 : 0, hideOverlappingLabels:true, maxHeight:80 },
            },
            yaxis: { min: 0, labels: { style: {fontSize:'10px'} } },
            grid: { borderColor:'#1e293b', strokeDashArray: 3 },
            tooltip: { theme:'dark' },
            dataLabels: { enabled: false },
            plotOptions: { bar: { borderRadius:4, columnWidth: Math.max(30, 80 - occLanes.length * 4) + '%' } }
        };
        chartAverages = new ApexCharts(occEl, occOpts);
        chartAverages.render();
    }
}
function renderCamerasGrid() {
    const grid = document.getElementById('cameras-grid');
    if (!grid) return;
    grid.innerHTML = camerasList.map(c => `
        <div class="bg-slate-900/40 border border-slate-800 rounded-xl p-6">
            <div class="flex justify-between items-center mb-4">
                <h4 class="text-base font-bold text-white">${c.camera_id}</h4>
                <div class="flex items-center gap-2">
                    <span class="px-2 py-0.5 rounded text-[10px] font-bold ${c.status === 'configured' ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/20' : 'bg-rose-500/10 text-rose-400 border border-rose-500/20'}">
                        ${c.status.toUpperCase()}
                    </span>
                    <button onclick="deleteCamera('${c.camera_id}')" class="p-1 rounded hover:bg-rose-500/10 text-slate-500 hover:text-rose-400 transition-all" title="Delete camera">
                        <i data-lucide="trash-2" class="h-3.5 w-3.5"></i>
                    </button>
                </div>
            </div>
            <div class="space-y-2 text-sm text-slate-300">
                <p><span class="text-slate-500">Name:</span> ${c.name}</p>
                <p><span class="text-slate-500">Source:</span> <code class="bg-slate-950 px-1 py-0.5 rounded text-xs text-indigo-400 break-all">${c.source}</code></p>
                <p><span class="text-slate-500">Resolution:</span> ${c.frame_width}x${c.frame_height} @ ${c.fps} FPS</p>
            </div>
            <div class="mt-4 flex gap-2">
                <button onclick="testCameraConnection('${c.camera_id}')" class="flex-1 text-xs py-1.5 rounded-lg border border-slate-700 text-slate-300 hover:border-indigo-500 hover:text-indigo-400 transition-all">
                    <i data-lucide="cable" class="h-3 w-3 inline mr-1"></i> Test
                </button>
                <button onclick="viewSnapshot('${c.camera_id}')" class="flex-1 text-xs py-1.5 rounded-lg border border-slate-700 text-slate-300 hover:border-emerald-500 hover:text-emerald-400 transition-all">
                    <i data-lucide="camera" class="h-3 w-3 inline mr-1"></i> Snapshot
                </button>
                <button onclick="switchTab('lanes')" class="flex-1 text-xs py-1.5 rounded-lg bg-indigo-600/20 border border-indigo-500/30 text-indigo-400 hover:bg-indigo-600/30 transition-all">Config Lanes</button>
            </div>
        </div>
    `).join('');
    lucide.createIcons();
}

async function testCameraConnection(cameraId) {
    showToast({severity: 'info', title: 'Testing', message: `Testing connection to ${cameraId}...`});
    try {
        const snap = await fetch(BASE_URL + `/api/cameras/${cameraId}/snapshot`);
        if (snap.ok) {
            showToast({severity: 'info', title: 'Connection OK', message: `Camera ${cameraId}: snapshot received (${(snap.headers.get('content-length') || 0)} bytes)`});
        } else {
            showToast({severity: 'warning', title: 'Connection Failed', message: `Camera ${cameraId}: HTTP ${snap.status}`});
        }
    } catch (e) {
        showToast({severity: 'warning', title: 'Connection Error', message: `Camera ${cameraId}: ${e.message}`});
    }
}

function viewSnapshot(cameraId) {
    window.open(BASE_URL + `/api/cameras/${cameraId}/snapshot`, '_blank');
}

async function deleteCamera(cameraId) {
    if (!confirm(`Delete camera ${cameraId}? This cannot be undone.`)) return;
    // Fire DELETE, update UI immediately without waiting for refreshData()
    const res = await apiRequest(`/api/cameras/${cameraId}`, { method: 'DELETE' });
    if (!res) {
        showToast({severity: 'warning', title: 'Delete Failed', message: `Failed to delete camera ${cameraId}.`});
        return;
    }
    camerasList = camerasList.filter(c => c.camera_id !== cameraId);
    renderCamerasGrid();
    showToast({severity: 'info', title: 'Deleted', message: `Camera ${cameraId} deleted.`});
    // No refreshData() — avoids 4 extra network calls (health + cams + models + jobs)
}

function showAddCameraForm() {
    const existing = document.getElementById('add-camera-modal');
    if (existing) existing.remove();

    const modal = document.createElement('div');
    modal.id = 'add-camera-modal';
    modal.className = 'fixed inset-0 z-50 flex items-center justify-center bg-black/70';
    modal.innerHTML = `
        <div class="bg-slate-900 border border-slate-700 rounded-xl p-6 w-96 max-h-[90vh] overflow-y-auto">
            <h3 class="text-lg font-bold text-white mb-4">Add Camera</h3>
            <div class="space-y-3">
                <div>
                    <label class="text-xs font-semibold text-slate-400 mb-1 block">Camera ID</label>
                    <input id="new-cam-id" class="w-full bg-slate-800 border border-slate-700 rounded px-3 py-2 text-white text-sm focus:outline-none focus:border-indigo-500" placeholder="e.g. CAM_03">
                </div>
                <div>
                    <label class="text-xs font-semibold text-slate-400 mb-1 block">Name</label>
                    <input id="new-cam-name" class="w-full bg-slate-800 border border-slate-700 rounded px-3 py-2 text-white text-sm focus:outline-none focus:border-indigo-500" placeholder="e.g. Highway Cam 3">
                </div>
                <div>
                    <label class="text-xs font-semibold text-slate-400 mb-1 block">Source</label>
                    <input id="new-cam-source" class="w-full bg-slate-800 border border-slate-700 rounded px-3 py-2 text-white text-sm focus:outline-none focus:border-indigo-500" placeholder="RTSP URL or path">
                </div>
                <div>
                    <label class="text-xs font-semibold text-slate-400 mb-1 block">Source Type</label>
                    <select id="new-cam-type" class="w-full bg-slate-800 border border-slate-700 rounded px-3 py-2 text-white text-sm">
                        <option value="rtsp">RTSP</option>
                        <option value="image_dir">Image Directory</option>
                        <option value="video">Video File</option>
                        <option value="youtube">YouTube Live</option>
                    </select>
                </div>
                <div class="grid grid-cols-3 gap-2">
                    <div>
                        <label class="text-xs font-semibold text-slate-400 mb-1 block">FPS</label>
                        <input id="new-cam-fps" type="number" value="25" class="w-full bg-slate-800 border border-slate-700 rounded px-3 py-2 text-white text-sm">
                    </div>
                    <div>
                        <label class="text-xs font-semibold text-slate-400 mb-1 block">Width</label>
                        <input id="new-cam-width" type="number" value="1920" class="w-full bg-slate-800 border border-slate-700 rounded px-3 py-2 text-white text-sm">
                    </div>
                    <div>
                        <label class="text-xs font-semibold text-slate-400 mb-1 block">Height</label>
                        <input id="new-cam-height" type="number" value="1080" class="w-full bg-slate-800 border border-slate-700 rounded px-3 py-2 text-white text-sm">
                    </div>
                </div>
            </div>
            <div class="flex gap-2 mt-5">
                <button onclick="submitNewCamera()" class="flex-1 py-2 rounded-lg text-sm font-semibold bg-indigo-600 hover:bg-indigo-500 text-white transition-all">Add Camera</button>
                <button onclick="document.getElementById('add-camera-modal').remove()" class="flex-1 py-2 rounded-lg text-sm font-semibold bg-slate-700 hover:bg-slate-600 text-white transition-all">Cancel</button>
            </div>
        </div>
    `;
    document.body.appendChild(modal);
    lucide.createIcons();
}

async function submitNewCamera() {
    const getVal = (id) => { const el = document.getElementById(id); return el ? el.value : ''; };
    const body = {
        camera_id: getVal('new-cam-id'),
        name: getVal('new-cam-name'),
        source: getVal('new-cam-source'),
        source_type: getVal('new-cam-type'),
        fps: parseInt(getVal('new-cam-fps')) || 25,
        frame_width: parseInt(getVal('new-cam-width')) || 1920,
        frame_height: parseInt(getVal('new-cam-height')) || 1080,
    };
    if (!body.camera_id || !body.name || !body.source) {
        showToast({severity: 'warning', title: 'Missing Fields', message: 'Camera ID, Name, and Source are required.'});
        return;
    }
    const res = await apiRequest('/api/cameras', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body)
    });
    if (res) {
        document.getElementById('add-camera-modal').remove();
        camerasList = [...camerasList, res];
        renderCamerasGrid();
        showToast({severity: 'info', title: 'Camera Added', message: `Camera ${body.camera_id} registered.`});
        // No refreshData() — avoids 4 extra network calls
    } else {
        showToast({severity: 'warning', title: 'Failed', message: 'Could not create camera. Check server logs.'});
    }
}
async function applyCountingFilter() {
    const camera_id = document.getElementById('count-filter-camera').value;
    const dateEl = document.getElementById('count-filter-date').value;
    const fromEl = document.getElementById('count-filter-from').value;
    const toEl = document.getElementById('count-filter-to').value;
    const typeFilter = document.getElementById('count-filter-type').value;

    let since, until;
    if (dateEl) {
        since = new Date(dateEl + 'T' + (fromEl || '00:00') + ':00').toISOString();
        until = new Date(dateEl + 'T' + (toEl || '23:59') + ':00').toISOString();
    }

    if (!camera_id) {
        document.getElementById('counting-table-body').innerHTML = '<tr><td colspan="6" class="px-3 py-8 text-center text-slate-600 text-sm">Select a camera to view data</td></tr>';
        return;
    }

    let url = `/api/cameras/${camera_id}/counts/summary`;
    const params = [];
    if (since) params.push('since=' + encodeURIComponent(since));
    if (until) params.push('until=' + encodeURIComponent(until));
    if (params.length) url += '?' + params.join('&');

    const summary = await apiRequest(url);
    const total = summary ? summary.total : 0;
    const lanes = summary ? summary.lanes : [];

    let filteredLanes = lanes;
    if (typeFilter) {
        filteredLanes = lanes.map(l => {
            const types = {};
            const tVal = l.types[typeFilter] || 0;
            types[typeFilter] = tVal;
            return { lane_id: l.lane_id, types, total: tVal };
        });
    }

    const typeTotals = { car: 0, motorcycle: 0, truck: 0, bus: 0 };
    lanes.forEach(l => {
        Object.entries(l.types || {}).forEach(([t, c]) => {
            typeTotals[t] = (typeTotals[t] || 0) + c;
        });
    });
    const set = (id, val) => { const el = document.getElementById(id); if (el) el.innerText = val; };
    set('count-total', total.toLocaleString());
    set('count-cars', (typeTotals.car || 0).toLocaleString());
    set('count-motos', (typeTotals.motorcycle || 0).toLocaleString());
    set('count-heavy', ((typeTotals.truck || 0) + (typeTotals.bus || 0)).toLocaleString());

    const tbody = document.getElementById('counting-table-body');
    if (!filteredLanes.length) {
        tbody.innerHTML = '<tr><td colspan="6" class="px-3 py-8 text-center text-slate-600 text-sm">No data for this period</td></tr>';
    } else {
        tbody.innerHTML = filteredLanes.map(l => {
            const types = l.types || {};
            const laneTotal = l.total || Object.values(types).reduce((a, b) => a + b, 0);
            return `<tr class="border-b border-slate-800/50 hover:bg-slate-900/20">
                <td class="px-3 py-3 font-medium text-white">${l.lane_id}</td>
                <td class="px-3 py-3">${types.car || 0}</td>
                <td class="px-3 py-3">${types.motorcycle || 0}</td>
                <td class="px-3 py-3">${types.truck || 0}</td>
                <td class="px-3 py-3">${types.bus || 0}</td>
                <td class="px-3 py-3 font-bold text-white">${laneTotal}</td>
            </tr>`;
        }).join('') +
        `<tr class="bg-slate-950/40">
            <td class="px-3 py-3 font-bold text-white">TOTAL</td>
            <td class="px-3 py-3 font-bold text-indigo-400">${typeTotals.car || 0}</td>
            <td class="px-3 py-3 font-bold text-emerald-400">${typeTotals.motorcycle || 0}</td>
            <td class="px-3 py-3 font-bold text-amber-400">${typeTotals.truck || 0}</td>
            <td class="px-3 py-3 font-bold text-amber-400">${typeTotals.bus || 0}</td>
            <td class="px-3 py-3 font-bold text-white">${total}</td>
        </tr>`;
    }

    renderCountingChart(filteredLanes);
}

function exportCountingCSV() {
    const camera_id = document.getElementById('count-filter-camera').value;
    if (!camera_id) { alert('Select a camera first'); return; }
    const dateEl = document.getElementById('count-filter-date').value;
    const fromEl = document.getElementById('count-filter-from').value;
    const toEl = document.getElementById('count-filter-to').value;
    const params = [];
    if (dateEl && fromEl) params.push('since=' + encodeURIComponent(new Date(dateEl + 'T' + fromEl + ':00').toISOString()));
    if (dateEl && toEl) params.push('until=' + encodeURIComponent(new Date(dateEl + 'T' + toEl + ':00').toISOString()));
    const qs = params.length ? '?' + params.join('&') : '';
    const a = document.createElement('a');
    a.href = BASE_URL + `/api/reports/${camera_id}/lanes/csv${qs}`;
    a.download = `${camera_id}_lane_report.csv`;
    a.click();
}

function renderCountingChart(lanes) {
    chartCountingLanes = destroyChart(chartCountingLanes);
    const el = document.querySelector('#chart-counting-lanes');
    if (!el || !lanes || !lanes.length) return;

    const cats = lanes.map(l => l.lane_id);
    const cars = lanes.map(l => (l.types && l.types.car) || 0);
    const motos = lanes.map(l => (l.types && l.types.motorcycle) || 0);
    const trucks = lanes.map(l => (l.types && l.types.truck) || 0);
    const buses = lanes.map(l => (l.types && l.types.bus) || 0);

    const chartHeight = Math.max(320, 280 + Math.max(0, lanes.length - 4) * 40);

    const opts = {
        series: [
            {name:'Cars', data:cars},
            {name:'Motorcycles', data:motos},
            {name:'Trucks', data:trucks},
            {name:'Buses', data:buses}
        ],
        chart: {
            type: 'bar', height: chartHeight, width: '100%',
            background: 'transparent', toolbar: {show: false},
            stacked: true, zoom: {enabled: false},
        },
        theme: { mode: 'dark' },
        colors: ['#6366f1','#10b981','#f59e0b','#ef4444'],
        xaxis: {
            categories: cats,
            labels: { style: {fontSize: '10px'}, rotate: lanes.length > 6 ? -45 : 0, hideOverlappingLabels: true, maxHeight: 80 },
        },
        yaxis: { labels: { style: {fontSize: '10px'} }, min: 0 },
        grid: { borderColor: '#1e293b', strokeDashArray: 3 },
        tooltip: { theme: 'dark' },
        legend: { show: true, position: 'top', fontSize: '11px', labels: {colors: '#94a3b8'} },
        plotOptions: { bar: { borderRadius: 3, columnWidth: Math.max(25, 80 - lanes.length * 4) + '%' } },
        dataLabels: { enabled: false },
    };
    chartCountingLanes = new ApexCharts(el, opts);
    chartCountingLanes.render();
}
async function renderDashboardCharts() {
    destroyChart(chartHourly); chartHourly = null;
    destroyChart(chartVehicleTypes); chartVehicleTypes = null;
    clearContainer('#chart-hourly-traffic');
    clearContainer('#chart-vehicle-types');

    const [dashData, hourlyData, alerts] = await Promise.all([
        apiRequest('/api/dashboard/summary'),
        apiRequest('/api/dashboard/hourly'),
        apiRequest('/api/alerts'),
    ]);

    const totalVeh = dashData ? dashData.total_vehicles : 0;
    const perCamera = dashData ? dashData.per_camera : [];
    const typeDist = dashData ? dashData.type_distribution || {} : {};
    const activeAlerts = dashData ? dashData.active_alerts : 0;
    const totalCam = dashData ? dashData.total_cameras : 0;
    const totalLanes = dashData ? dashData.total_lanes : 0;

    document.getElementById('kpi-total-vehicles').innerText = totalVeh ? totalVeh.toLocaleString() : '0';
    document.getElementById('kpi-active-cameras').innerText = totalCam || (camerasList.length || '—');
    document.getElementById('kpi-cameras-sub').innerText = totalCam ? totalCam + ' total registered' : (camerasList.length + ' total registered');
    document.getElementById('kpi-active-lanes').innerText = totalLanes ? totalLanes : '—';

    // Vehicle type donut
    const typeColors = ['#6366f1','#10b981','#f59e0b','#ef4444','#38bdf8','#a78bfa','#fb923c'];
    const typeLabels = Object.keys(typeDist);
    const typeValues = Object.values(typeDist);
    const typeTotal = typeValues.reduce((a, b) => a + b, 0) || 1;

    if (typeLabels.length) {
        const typeOpts = {
            series: typeValues,
            chart: { type: 'donut', height: 160, background: 'transparent' },
            colors: typeColors.slice(0, typeLabels.length),
            labels: typeLabels,
            legend: { show: false },
            plotOptions: { pie: { donut: { size: '70%' } } },
            dataLabels: { enabled: false },
            theme: { mode: 'dark' },
            tooltip: { theme: 'dark' }
        };
        clearContainer('#chart-vehicle-types');
        chartVehicleTypes = new ApexCharts(document.querySelector('#chart-vehicle-types'), typeOpts);
        chartVehicleTypes.render();

        const legendContainer = document.getElementById('type-legend-container');
        legendContainer.innerHTML = typeLabels.map((label, i) => {
            const pct = Math.round(typeValues[i] / typeTotal * 100);
            const color = typeColors[i % typeColors.length];
            return `<div class="flex justify-between text-xs">
                <span class="text-slate-400 flex items-center gap-1.5">
                    <span class="w-2 h-2 rounded-full" style="background:${color}"></span>${label}
                </span>
                <span class="text-white font-semibold">${pct}%</span>
            </div>`;
        }).join('');
    } else {
        document.getElementById('type-legend-container').innerHTML = '<p class="text-xs text-slate-500 text-center">No data yet</p>';
    }

    // Top busiest cameras
    const topCamList = document.getElementById('top-cameras-list');
    if (perCamera.length) {
        const maxTotal = Math.max(...perCamera.map(c => c.total), 1);
        topCamList.innerHTML = perCamera.map((c, i) => {
            const rank = i + 1;
            const pct = Math.round(c.total / maxTotal * 100);
            const rankColor = rank === 1 ? 'text-amber-400' : 'text-slate-400';
            const barOpacity = rank === 1 ? '' : rank === 2 ? '/60' : '/40';
            return `<div class="flex items-center justify-between">
                <div class="flex items-center gap-2">
                    <span class="text-xs font-bold ${rankColor} w-4">#${rank}</span>
                    <span class="text-sm text-slate-200">${c.camera_id}</span>
                </div>
                <div class="flex items-center gap-2">
                    <div class="w-24 bg-slate-800 rounded-full h-1.5">
                        <div class="bg-indigo-500${barOpacity} h-1.5 rounded-full" style="width:${pct}%"></div>
                    </div>
                    <span class="text-xs text-white font-semibold w-12 text-right">${c.total.toLocaleString()}</span>
                </div>
            </div>`;
        }).join('');
    } else {
        topCamList.innerHTML = '<p class="text-xs text-slate-500 text-center py-4">No data yet</p>';
    }

    // Peak hours — real data from backend
    if (hourlyData && hourlyData.peak_hours && hourlyData.peak_hours.length) {
        const peaks = hourlyData.peak_hours;
        document.getElementById('peak-morning').innerText =
            (peaks.find(p => p.label === 'morning_peak')?.count || '—').toLocaleString();
        document.getElementById('peak-evening').innerText =
            (peaks.find(p => p.label === 'evening_peak')?.count || '—').toLocaleString();
        document.getElementById('peak-offpeak').innerText =
            (hourlyData.offpeak_avg || 0).toLocaleString();
    } else {
        ['peak-morning','peak-evening','peak-offpeak'].forEach(id => {
            const el = document.getElementById(id);
            if (el) el.innerText = '—';
        });
    }

    // Active alerts
    const alertsList = document.getElementById('active-alerts-list');
    if (alerts && alerts.length) {
        const severityColors = { critical: 'rose', warning: 'amber', info: 'blue' };
        alertsList.innerHTML = alerts.slice(0, 5).map(a => {
            const sev = a.severity || 'info';
            const color = severityColors[sev] || 'slate';
            return `<div class="alert-${sev} bg-${color}-500/5 rounded-lg p-3">
                <p class="text-xs font-bold text-${color}-400">${a.title || a.camera_id || 'Alert'}</p>
                <p class="text-xs text-slate-500 mt-0.5">${a.message || ''}</p>
            </div>`;
        }).join('');
    } else if (activeAlerts > 0) {
        alertsList.innerHTML = `<p class="text-xs text-slate-500 text-center py-4">${activeAlerts} active alert(s)</p>`;
    } else {
        alertsList.innerHTML = '<p class="text-xs text-slate-500 text-center py-4">No active alerts</p>';
    }

    // Hourly chart
    let chartData = null;
    if (hourlyData && hourlyData.hourly) {
        chartData = hourlyData.hourly.map(h => h.count);
    }
    if (!chartData) {
        // Real-time note: no data from backend — show zeros instead of synthetic
        chartData = Array(24).fill(0);
    }

    const cats = Array.from({length: 24}, (_, i) => i + 'h');

    const hourlyOpts = {
        series: [{ name: 'Vehicles', data: chartData }],
        chart: { type: 'area', height: 210, toolbar: { show: false } },
        theme: { mode: 'dark' },
        stroke: { curve: 'smooth', width: 2 },
        fill: { type: 'gradient', gradient: { shadeIntensity: 1, opacityFrom: 0.35, opacityTo: 0.02, stops: [0, 100] } },
        colors: ['#6366f1'],
        xaxis: {
            type: 'category',
            categories: cats,
            tickAmount: 24,
            axisBorder: { show: false },
            labels: { style: { fontSize: '10px' }, hideOverlappingLabels: false, rotate: 0 },
        },
        yaxis: { labels: { style: { fontSize: '10px' } } },
        grid: { borderColor: '#1e293b', strokeDashArray: 3 },
        tooltip: { theme: 'dark' },
    };
    clearContainer('#chart-hourly-traffic');
    chartHourly = new ApexCharts(document.querySelector('#chart-hourly-traffic'), hourlyOpts);
    chartHourly.render();
    setTimeout(() => {
        if (!chartHourly) return;
        try { chartHourly.updateOptions(hourlyOpts, true, false); } catch (e) {}
    }, 100);
}
async function loadEventsData() {
    const camera_id = document.getElementById('events-cam-select')?.value;
    const tbody = document.getElementById('events-logs-tbody');
    const countEl = document.getElementById('events-count');
    if (!tbody) return;

    if (!camera_id) {
        tbody.innerHTML = '<tr><td colspan="8" class="px-4 py-8 text-center text-slate-600 text-sm">Select a camera to view events</td></tr>';
        if (countEl) countEl.innerText = '';
        return;
    }

    const data = await apiRequest(`/api/cameras/${camera_id}/lane-changes?limit=200`);
    if (!data || !data.length) {
        tbody.innerHTML = '<tr><td colspan="8" class="px-4 py-8 text-center text-slate-600 text-sm">No events found</td></tr>';
        if (countEl) countEl.innerText = '0 events';
        return;
    }

    if (countEl) countEl.innerText = data.length + ' events';
    tbody.innerHTML = data.map(e => `
        <tr class="border-b border-slate-800/50 hover:bg-slate-900/20">
            <td class="px-4 py-3 font-semibold text-indigo-400">#${e.id}</td>
            <td class="px-4 py-3 text-white">${e.camera_id}</td>
            <td class="px-4 py-3">#${e.track_id}</td>
            <td class="px-4 py-3 uppercase text-xs">${e.class_name || '—'}</td>
            <td class="px-4 py-3">${e.previous_lane_id || '—'}</td>
            <td class="px-4 py-3 font-semibold text-emerald-400">${e.current_lane_id || '—'}</td>
            <td class="px-4 py-3 text-slate-500">${e.frame_id}</td>
            <td class="px-4 py-3 text-slate-500 text-xs">${e.timestamp ? e.timestamp.replace('T',' ').substring(0,19) : '—'}</td>
        </tr>
    `).join('');
}

function exportEventsCSV() {
    const camera_id = document.getElementById('events-cam-select')?.value;
    if (!camera_id) { alert('Select a camera first'); return; }
    const url = BASE_URL + `/api/cameras/${camera_id}/lane-changes?limit=10000`;
    fetch(url)
        .then(r => r.json())
        .then(data => {
            if (!data || !data.length) { alert('No data to export'); return; }
            let csv = 'id,camera_id,track_id,class_name,previous_lane_id,current_lane_id,frame_id,timestamp\n';
            data.forEach(e => {
                csv += `${e.id},${e.camera_id},${e.track_id},${e.class_name || ''},${e.previous_lane_id || ''},${e.current_lane_id || ''},${e.frame_id},${e.timestamp || ''}\n`;
            });
            const blob = new Blob([csv], {type:'text/csv'});
            const a = document.createElement('a');
            a.href = window.URL.createObjectURL(blob);
            a.download = `${camera_id}_events_export.csv`;
            a.click();
        })
        .catch(() => alert('Failed to fetch events'));
}
function stopHealthPolling() {
    if (_healthTimer) { clearInterval(_healthTimer); _healthTimer = null; }
}

function startHealthPolling() {
    stopHealthPolling();
    fetchHealthData();
    _healthTimer = setInterval(fetchHealthData, 3000);
}

async function fetchHealthData() {
    const token = localStorage.getItem('access_token');
    if (!token) return;
    const headers = { 'Authorization': 'Bearer ' + token };

    const health = await apiRequestWithHeaders('/api/admin/system-health', headers);
    if (health) {
        updateHealthKPIs(health);
        updateHealthGauges(health);
        updateHealthSystemInfo(health);
        _healthHistory.push(health);
        if (_healthHistory.length > 120) _healthHistory.shift();
        renderTrendChart();
    }

    const cams = await apiRequest('/api/cameras') || [];
    updateHealthCamerasTable(cams);
}

function _badgeEl(text, isOk) {
    const cls = isOk
        ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/20'
        : 'bg-rose-500/10 text-rose-400 border border-rose-500/20';
    return `<span class="px-2 py-0.5 rounded text-[10px] font-bold ${cls}">● ${text}</span>`;
}

function updateHealthKPIs(h) {
    const apiBadge = document.getElementById('health-api-badge');
    apiBadge.className = 'px-2 py-0.5 rounded text-[10px] font-bold bg-emerald-500/10 text-emerald-400 border border-emerald-500/20';
    apiBadge.innerText = '● ONLINE';
    const set = (id, val) => { const el = document.getElementById(id); if (el) el.innerText = val; };
    set('health-api-text', 'Active');
    document.getElementById('health-api-sub').innerHTML = 'Uptime: <span class="text-slate-400">' + (h.uptime || '—') + '</span>';

    const dbOk = h.database_status === 'healthy';
    const dbBadge = document.getElementById('health-db-badge');
    dbBadge.className = dbOk
        ? 'px-2 py-0.5 rounded text-[10px] font-bold bg-emerald-500/10 text-emerald-400 border border-emerald-500/20'
        : 'px-2 py-0.5 rounded text-[10px] font-bold bg-rose-500/10 text-rose-400 border border-rose-500/20';
    dbBadge.innerText = dbOk ? '● CONNECTED' : '● DISCONNECTED';
    set('health-db-text', dbOk ? 'Connected' : 'Disconnected');
    document.getElementById('health-db-sub').innerHTML = 'Latency: <span class="text-slate-400">' + (h.database_latency_ms ? h.database_latency_ms + 'ms' : '—') + '</span>';

    const gpuOk = h.gpu && h.gpu.available;
    const gpuBadge = document.getElementById('health-gpu-badge');
    gpuBadge.className = gpuOk
        ? 'px-2 py-0.5 rounded text-[10px] font-bold bg-emerald-500/10 text-emerald-400 border border-emerald-500/20'
        : 'px-2 py-0.5 rounded text-[10px] font-bold bg-amber-500/10 text-amber-400 border border-amber-500/20';
    gpuBadge.innerText = gpuOk ? '● ONLINE' : '● CPU ONLY';
    set('health-gpu-text', gpuOk ? (h.gpu.name || 'GPU Active') : 'Fallback CPU');
    document.getElementById('health-gpu-sub').innerHTML = 'Utilization: <span class="text-slate-400">' + (gpuOk && h.gpu.util_pct >= 0 ? h.gpu.util_pct.toFixed(0) + '%' : 'N/A') + '</span>';

    const workersBadge = document.getElementById('health-workers-badge');
    workersBadge.className = 'px-2 py-0.5 rounded text-[10px] font-bold bg-emerald-500/10 text-emerald-400 border border-emerald-500/20';
    workersBadge.innerText = '● OK';
    set('health-workers-text', (h.active_jobs || 0) + ' Active');
    document.getElementById('health-workers-sub').innerHTML = 'Jobs: <span class="text-slate-400">' + (h.active_jobs || 0) + ' running / ' + (h.total_jobs || 0) + ' total</span>';
}

function updateHealthGauges(h) {
    const cpu = h.cpu_pct || 0;
    const cpuRing = document.getElementById('health-cpu-ring');
    if (cpuRing) { cpuRing.setAttribute('stroke-dashoffset', String(Math.max(0, 100 - cpu))); }
    const cpuPct = document.getElementById('health-cpu-pct');
    if (cpuPct) { cpuPct.innerText = Math.round(cpu) + '%'; }

    const mem = h.memory_pct || 0;
    const memRing = document.getElementById('health-mem-ring');
    if (memRing) { memRing.setAttribute('stroke-dashoffset', String(Math.max(0, 100 - mem))); }
    const memPct = document.getElementById('health-mem-pct');
    if (memPct) { memPct.innerText = Math.round(mem) + '%'; }

    const disk = h.disk_pct || 0;
    const diskRing = document.getElementById('health-disk-ring');
    if (diskRing) { diskRing.setAttribute('stroke-dashoffset', String(Math.max(0, 100 - disk))); }
    const diskPct = document.getElementById('health-disk-pct');
    if (diskPct) { diskPct.innerText = Math.round(disk) + '%'; }
    const diskSub = document.getElementById('health-disk-sub');
    if (diskSub) { diskSub.innerText = disk > 80 ? 'High Usage' : 'Normal'; }
}

function updateHealthSystemInfo(h) {
    const setText = (id, val) => { const el = document.getElementById(id); if (el) el.innerText = val || '—'; };
    setText('health-platform', h.platform || '—');
    setText('health-python', h.python_version || '—');
    setText('health-uptime', h.uptime || '—');
    const redisOk = h.redis_status === 'healthy';
    setText('health-redis-status', redisOk ? 'Connected' : (h.redis_status === 'unavailable' ? 'Unavailable' : 'Disconnected'));
    setText('health-active-cams', (h.active_cameras || 0) + ' / ' + (h.total_cameras || 0));
    setText('health-ws-conns', String(h.ws_connections || 0));
    setText('health-active-jobs', (h.active_jobs || 0) + ' active / ' + (h.total_jobs || 0) + ' total');
    setText('health-disk-usage', (h.disk_pct || 0) + '%');
}

function renderTrendChart() {
    const history = _healthHistory;
    if (!history || history.length < 2) {
        if (history.length === 1) {
            const h = history[0];
            _renderChartLines(
                ['Now'],
                [[h.cpu_pct || 0]],
                [[h.memory_pct || 0]],
                [[h.gpu && h.gpu.util_pct >= 0 ? h.gpu.util_pct : null]]
            );
        }
        return;
    }

    const labels = history.map((_, i) => {
        const secsAgo = (history.length - 1 - i) * 3;
        if (secsAgo === 0) return 'now';
        if (secsAgo < 60) return '-' + secsAgo + 's';
        return '-' + Math.round(secsAgo / 60) + 'm';
    });

    const cpuData = history.map(h => h.cpu_pct || 0);
    const memData = history.map(h => h.memory_pct || 0);
    const gpuData = history.map(h => (h.gpu && h.gpu.util_pct >= 0) ? h.gpu.util_pct : null);

    _renderChartLines(labels, [cpuData], [memData], [gpuData]);
}

function _renderChartLines(labels, cpuSeries, memSeries, gpuSeries) {
    chartMetrics = destroyChart(chartMetrics);

    const series = [
        { name: 'CPU %', data: cpuSeries[0] || [] },
        { name: 'Memory %', data: memSeries[0] || [] },
    ];
    const hasGpu = gpuSeries[0] && gpuSeries[0].some(v => v !== null && v !== undefined);
    if (hasGpu) {
        series.push({ name: 'GPU %', data: gpuSeries[0] });
    }

    const opts = {
        series,
        chart: { type: 'line', height: 210, background: 'transparent', toolbar: { show: false }, animations: { enabled: true, dynamicAnimation: { speed: 500 } } },
        theme: { mode: 'dark' },
        stroke: { curve: 'smooth', width: 2.5 },
        colors: ['#6366f1', '#10b981', '#f59e0b'],
        xaxis: { categories: labels, axisBorder: { show: false }, labels: { maxHeight: 20, style: { fontSize: '9px' } } },
        yaxis: { max: 100, labels: { style: { fontSize: '10px' } } },
        grid: { borderColor: '#1e293b' },
        tooltip: { theme: 'dark' },
        legend: { show: true, position: 'top', labels: { colors: '#94a3b8' } }
    };
    const el = document.querySelector('#chart-metrics-trend');
    if (el) {
        chartMetrics = new ApexCharts(el, opts);
        chartMetrics.render();
    }
}

function updateHealthCamerasTable(cams) {
    const tbody = document.getElementById('health-cameras-tbody');
    if (!tbody) return;
    tbody.innerHTML = cams.map(c => {
        const isConfigured = c.status === 'configured';
        const streamActive = c.camera_id === _lastLiveCam && _ws && _ws.readyState === WebSocket.OPEN;
        return `<tr class="border-b border-slate-800/50 hover:bg-slate-900/20">
            <td class="px-4 py-3.5 font-medium text-white">${c.camera_id}</td>
            <td class="px-4 py-3.5">${c.name}</td>
            <td class="px-4 py-3.5">${c.fps || '—'}</td>
            <td class="px-4 py-3.5">
                <span class="px-2 py-0.5 rounded text-[10px] font-bold ${streamActive ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/20' : 'bg-slate-500/10 text-slate-400 border border-slate-500/20'}">
                    ${streamActive ? '● LIVE' : '○ IDLE'}
                </span>
            </td>
            <td class="px-4 py-3.5">
                <span class="px-2 py-0.5 rounded text-[10px] font-bold ${isConfigured ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/20' : 'bg-rose-500/10 text-rose-400 border border-rose-500/20'}">
                    ${c.status.toUpperCase()}
                </span>
            </td>
        </tr>`;
    }).join('');
}
function renderJobsTable() {
    const tbody = document.getElementById('overview-jobs-tbody');
    if (!tbody) return;
    tbody.innerHTML = jobsList.map(j => {
        const prog = typeof j.progress === 'number' ? j.progress : (j.total_frames ? Math.round((j.processed_frames || 0) / j.total_frames * 100) : 0);
        const statusCls = j.status === 'completed' ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20'
            : j.status === 'running' ? 'bg-indigo-500/10 text-indigo-400 border-indigo-500/20'
            : 'bg-amber-500/10 text-amber-400 border-amber-500/20';
        return `<tr class="border-b border-slate-800/50 hover:bg-slate-900/20">
            <td class="px-4 py-3.5 font-mono text-xs text-indigo-400">${j.job_id.substring(0,15)}…</td>
            <td class="px-4 py-3.5 text-white">${j.camera_id}</td>
            <td class="px-4 py-3.5">${j.model_id}</td>
            <td class="px-4 py-3.5"><span class="px-2 py-0.5 rounded text-[10px] font-bold border ${statusCls}">${j.status.toUpperCase()}</span></td>
            <td class="px-4 py-3.5">
                <div class="flex items-center gap-2">
                    <div class="w-16 bg-slate-800 h-1 rounded-full"><div class="bg-indigo-500 h-1 rounded-full" style="width:${prog}%"></div></div>
                    <span class="text-xs">${prog}%</span>
                </div>
            </td>
            <td class="px-4 py-3.5">${j.fps ? j.fps.toFixed(1) : 0}</td>
            <td class="px-4 py-3.5 text-slate-500 text-xs">${j.created_at ? j.created_at.replace('T',' ') : '—'}</td>
        </tr>`;
    }).join('');
}

async function launchJob() {
    const camera_id = document.getElementById('overview-cam-select').value;
    const model_id = document.getElementById('overview-model-select').value;
    const res = await apiRequest('/api/infer/video', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({camera_id, model_id, save_annotated: true})
    });
    if (!res) { showToast({severity:'warning', title:'Launch Failed', message: 'Could not start job for ' + camera_id + '. Check server logs.'}); return; }
    showToast({severity:'info', title:'Job Started', message:'Job ' + res.job_id + ' launched for ' + camera_id});
    refreshData();
}
// ──────────────────────────────────────────────────────────────────────
// Lane Configuration — Canvas Polygon Editor (Zones + Lanes)
// Two modes:
//   zones  — draw detection zone polygon(s) for ROI crop
//   lanes  — draw lane polygons + counting lines inside zones
// ──────────────────────────────────────────────────────────────────────

// ── Constants ────────────────────────────────────────────────────────
const LANE_COLORS = [
    'rgba(99,102,241,0.35)','rgba(236,72,153,0.35)','rgba(52,211,153,0.35)',
    'rgba(251,191,36,0.35)','rgba(248,113,113,0.35)','rgba(129,140,248,0.35)',
    'rgba(45,212,191,0.35)','rgba(232,121,249,0.35)',
];
const LANE_STROKES = [
    '#818cf8','#f472b6','#34d399','#fbbf24','#f87171','#818cf8','#2dd4bf','#e879f9',
];

// ── State ────────────────────────────────────────────────────────────
let editorMode = 'zones';  // 'zones' | 'lanes'
let zonesData = [];        // [{zone_id, name, polygon}]
let lanesData = [];        // [{lane_id, name, polygon, counting_line}]
let selectedZoneIdx = -1;
let selectedLaneIdx = -1;
let drawPoints = [];
let isDrawing = false;

// Vertex / counting-line dragging
let dragTargetLaneIdx = -1;
let dragTargetZoneIdx = -1;
let dragVertexPointIdx = -1;
let dragCLPoint = null;
let isDragging = false;

// Canvas / snapshot
let canvasEl = null, ctx = null, bgImage = null;
let canvasW = 0, canvasH = 0, imgW = 0, imgH = 0;
let scaleX = 1, scaleY = 1;
let _canvasEventsSetup = false;

// Colors
const ZONE_COLOR = 'rgba(251,191,36,0.20)';
const ZONE_STROKE = '#fbbf24';

// ── Color helpers ────────────────────────────────────────────────────
function _laneColor(idx) {
    return idx >= 0 && idx < lanesData.length ? LANE_COLORS[idx % LANE_COLORS.length] : LANE_COLORS[0];
}
function _laneStroke(idx) {
    return idx >= 0 && idx < lanesData.length ? LANE_STROKES[idx % LANE_STROKES.length] : LANE_STROKES[0];
}

// ── Mode toggle ──────────────────────────────────────────────────────
function setEditorMode(mode) {
    if (mode === editorMode) return;
    editorMode = mode;
    isDrawing = false; drawPoints = [];
    selectedZoneIdx = -1; selectedLaneIdx = -1;

    document.getElementById('mode-btn-zones').className = 'px-3 py-1.5 text-xs font-medium transition-all ' +
        (mode === 'zones' ? 'bg-indigo-600/20 text-indigo-400 border-r border-slate-800' : 'text-slate-400 hover:text-slate-200');
    document.getElementById('mode-btn-lanes').className = 'px-3 py-1.5 text-xs font-medium transition-all ' +
        (mode === 'lanes' ? 'bg-indigo-600/20 text-indigo-400 border-l border-slate-800' : 'text-slate-400 hover:text-slate-200');

    // Toggle zone/lane buttons
    document.getElementById('btn-zone-add').classList.toggle('hidden', mode !== 'zones');
    document.getElementById('btn-zone-delete').classList.toggle('hidden', mode !== 'zones');
    document.getElementById('btn-lane-add').classList.toggle('hidden', mode !== 'lanes');
    document.getElementById('btn-lane-delete').classList.toggle('hidden', mode !== 'lanes');
    document.getElementById('btn-save-zones').classList.toggle('hidden', mode !== 'zones');
    document.getElementById('btn-save-lanes').classList.toggle('hidden', mode !== 'lanes');
    document.getElementById('zone-count-badge').classList.toggle('hidden', mode !== 'zones');
    document.getElementById('lane-count-badge').classList.toggle('hidden', mode !== 'lanes');

    const title = document.getElementById('side-panel-title');
    if (title) title.textContent = mode === 'zones' ? 'Detection Zones' : 'Lanes';

    hideCountingLinePanel();
    hideDetailPanel('zone');
    hideDetailPanel('lane');

    if (mode === 'zones') renderZoneList(); else renderLaneList();
    renderCanvas();
    updateButtons();
    setStatus('Mode: ' + (mode === 'zones' ? 'Detection Zones' : 'Lanes') + '. Click Add to draw a new polygon.');
}

// ── Load ─────────────────────────────────────────────────────────────
async function loadLanesConfigEditor() {
    const selectEl = document.getElementById('lanes-cam-select');
    if (!selectEl) return;
    // Re-populate select options if HTML was just re-created
    if (!selectEl.options.length && camerasList && camerasList.length) {
        selectEl.innerHTML = camerasList.map(c => `<option value="${c.camera_id}">${c.camera_id} — ${c.name}</option>`).join('');
    }
    const camera_id = selectEl.value;
    if (!camera_id) return;
    setStatus('Loading...');

    // Fire snapshot in parallel with zones/lanes — no need to wait
    const snapshotPromise = fetch(BASE_URL + `/api/cameras/${camera_id}/snapshot`, {
        headers: { 'Authorization': 'Bearer ' + (localStorage.getItem('access_token') || '') }
    });

    const [zonesRes, lanesRes] = await Promise.all([
        apiRequest(`/api/cameras/${camera_id}/zones`),
        apiRequest(`/api/cameras/${camera_id}/lanes`),
    ]);

    zonesData = (zonesRes && Array.isArray(zonesRes))
        ? zonesRes.map(z => ({ zone_id: z.zone_id, name: z.name || '', polygon: z.polygon || [] })) : [];
    lanesData = (lanesRes && Array.isArray(lanesRes))
        ? lanesRes.map(l => ({ lane_id: l.lane_id, name: l.name || '', polygon: l.polygon || [], counting_line: l.counting_line || null })) : [];

    selectedZoneIdx = -1; selectedLaneIdx = -1;
    drawPoints = []; isDrawing = false; isDragging = false;
    hideCountingLinePanel();
    hideDetailPanel('zone');
    hideDetailPanel('lane');

    canvasEl = document.getElementById('lanes-canvas');
    if (!canvasEl) return;
    ctx = canvasEl.getContext('2d');
    // Page HTML is re-created on each tab switch — must reset guard
    // so event listeners attach to the fresh canvas element.
    _canvasEventsSetup = false;
    setupCanvasEvents();

    // Load snapshot (request already started in parallel with zones/lanes)
    bgImage = null;
    let isPlaceholder = false;
    try {
        const resp = await snapshotPromise;
        if (resp.ok) {
            const blob = await resp.blob();
            const img = new Image();
            await new Promise((res, rej) => { img.onload = res; img.onerror = rej; img.src = URL.createObjectURL(blob); });
            bgImage = img;
            imgW = img.naturalWidth || img.width;
            imgH = img.naturalHeight || img.height;
            const wrapper = document.getElementById('canvas-wrapper');
            // Determine display size — fill available width, preserve aspect ratio
            canvasEl.style.width = '100%';
            canvasEl.style.height = 'auto';
            // Need to force layout so offsetWidth is accurate
            canvasEl.width = canvasEl.offsetWidth || wrapper?.clientWidth || 1200;
            const maxW = Math.min(1600, canvasEl.width);
            const aspect = imgW / imgH;
            canvasW = maxW; canvasH = maxW / aspect;
            canvasEl.width = canvasW; canvasEl.height = canvasH;
            scaleX = imgW / canvasW; scaleY = imgH / canvasH;
            isPlaceholder = resp.headers.get('X-Placeholder') === 'true';
        } else {
            imgW = 960; imgH = 540; canvasW = 960; canvasH = 540;
            canvasEl.width = canvasW; canvasEl.height = canvasH;
            scaleX = 1; scaleY = 1;
        }
    } catch (e) {
        imgW = 960; imgH = 540; canvasW = 960; canvasH = 540;
        canvasEl.width = canvasW; canvasEl.height = canvasH;
        scaleX = 1; scaleY = 1;
    }

    // Show placeholder badge on canvas
    const badge = document.getElementById('placeholder-badge');
    if (badge) badge.classList.toggle('hidden', !isPlaceholder);

    setEditorMode(editorMode);
    renderCanvas();
    if (editorMode === 'zones') renderZoneList(); else renderLaneList();
    updateButtons();
    setStatus('Loaded ' + zonesData.length + ' zones, ' + lanesData.length + ' lanes.');
}

// ── Render canvas ────────────────────────────────────────────────────
function renderCanvas() {
    if (!ctx) return;
    ctx.clearRect(0, 0, canvasW, canvasH);

    // Background
    if (bgImage) {
        ctx.drawImage(bgImage, 0, 0, canvasW, canvasH);
        ctx.fillStyle = 'rgba(2,6,23,0.15)'; ctx.fillRect(0, 0, canvasW, canvasH);
    } else {
        ctx.fillStyle = '#0f172a'; ctx.fillRect(0, 0, canvasW, canvasH);
        ctx.strokeStyle = 'rgba(51,65,85,0.3)'; ctx.lineWidth = 0.5;
        for (let x = 0; x < canvasW; x += 50) { ctx.beginPath(); ctx.moveTo(x,0); ctx.lineTo(x, canvasH); ctx.stroke(); }
        for (let y = 0; y < canvasH; y += 50) { ctx.beginPath(); ctx.moveTo(0,y); ctx.lineTo(canvasW, y); ctx.stroke(); }
    }

    // Always draw zones (semi-transparent)
    zonesData.forEach((zone, idx) => {
        if (editorMode === 'zones' && idx === selectedZoneIdx) return; // selected drawn on top
        drawZonePolygon(zone.polygon, idx, false);
    });
    if (zonesData.length > 0) {
        // Draw the union bounding box outline
        drawZoneUnionOutline();
    }

    // Draw lanes
    if (editorMode === 'lanes') {
        lanesData.forEach((lane, idx) => {
            if (idx === selectedLaneIdx) return;
            drawLanePolygon(lane.polygon, idx, false);
        });
        if (selectedLaneIdx >= 0 && selectedLaneIdx < lanesData.length) {
            drawLanePolygon(lanesData[selectedLaneIdx].polygon, selectedLaneIdx, true);
            drawCountingLine(lanesData[selectedLaneIdx]);
        }
    }

    // Draw selected zone on top
    if (editorMode === 'zones' && selectedZoneIdx >= 0 && selectedZoneIdx < zonesData.length) {
        drawZonePolygon(zonesData[selectedZoneIdx].polygon, selectedZoneIdx, true);
    }

    // Draw in-progress
    if (isDrawing && drawPoints.length > 0) {
        const pts = drawPoints.map(p => [p[0], p[1]]);
        ctx.beginPath();
        pts.forEach((p, i) => i === 0 ? ctx.moveTo(p[0], p[1]) : ctx.lineTo(p[0], p[1]));
        ctx.strokeStyle = '#a5b4fc'; ctx.lineWidth = 2; ctx.setLineDash([6,4]); ctx.stroke(); ctx.setLineDash([]);
        pts.forEach(p => { ctx.beginPath(); ctx.arc(p[0], p[1], 5, 0, Math.PI*2); ctx.fillStyle = '#a5b4fc'; ctx.fill(); ctx.strokeStyle = '#fff'; ctx.lineWidth = 1.5; ctx.stroke(); });
    }
}

function drawZonePolygon(polygon, idx, isSelected) {
    if (!polygon || polygon.length < 3) return;
    const pts = polygon.map(p => [p[0]/scaleX, p[1]/scaleY]);
    ctx.beginPath();
    pts.forEach((p, i) => i === 0 ? ctx.moveTo(p[0], p[1]) : ctx.lineTo(p[0], p[1]));
    ctx.closePath();
    ctx.fillStyle = isSelected ? 'rgba(251,191,36,0.35)' : ZONE_COLOR;
    ctx.fill();
    ctx.strokeStyle = isSelected ? '#f97316' : ZONE_STROKE;
    ctx.lineWidth = isSelected ? 4 : 2.5;
    ctx.setLineDash(isSelected ? [] : [8,4]);
    ctx.stroke();
    ctx.setLineDash([]);

    pts.forEach((p, pi) => {
        const highlight = isSelected;
        ctx.beginPath(); ctx.arc(p[0], p[1], highlight ? 7 : 5, 0, Math.PI*2);
        ctx.fillStyle = highlight ? '#f97316' : '#fbbf24'; ctx.fill();
        ctx.strokeStyle = '#fff'; ctx.lineWidth = highlight ? 2 : 1; ctx.stroke();
        if (highlight) { ctx.fillStyle = '#fff'; ctx.font = '9px monospace'; ctx.fillText(pi, p[0]+9, p[1]+3); }
    });
}

function drawZoneUnionOutline() {
    if (!zonesData.length) return;
    let x1 = Infinity, y1 = Infinity, x2 = -Infinity, y2 = -Infinity;
    zonesData.forEach(z => {
        (z.polygon || []).forEach(pt => {
            const px = pt[0];
            const py = pt[1];
            if (px < x1) x1 = px; if (py < y1) y1 = py;
            if (px > x2) x2 = px; if (py > y2) y2 = py;
        });
    });
    if (x1 === Infinity) return;
    ctx.strokeStyle = 'rgba(251,191,36,0.5)';
    ctx.lineWidth = 1; ctx.setLineDash([4,6]);
    ctx.strokeRect(x1/scaleX, y1/scaleY, (x2-x1)/scaleX, (y2-y1)/scaleY);
    ctx.setLineDash([]);
    ctx.fillStyle = 'rgba(251,191,36,0.6)'; ctx.font = '9px monospace';
    ctx.fillText('ROI Crop: ' + Math.round(x2-x1) + 'x' + Math.round(y2-y1), x1/scaleX + 4, y1/scaleY - 4);
}

function drawLanePolygon(polygon, idx, isSelected) {
    if (!polygon || polygon.length < 3) return;
    const pts = polygon.map(p => [p[0]/scaleX, p[1]/scaleY]);
    ctx.beginPath();
    pts.forEach((p, i) => i === 0 ? ctx.moveTo(p[0], p[1]) : ctx.lineTo(p[0], p[1]));
    ctx.closePath();
    ctx.fillStyle = _laneColor(idx); ctx.fill();
    ctx.strokeStyle = isSelected ? '#fbbf24' : _laneStroke(idx);
    ctx.lineWidth = isSelected ? 3 : 2; ctx.stroke();

    pts.forEach((p, pi) => {
        const highlight = isSelected;
        ctx.beginPath(); ctx.arc(p[0], p[1], highlight ? 6 : 4, 0, Math.PI*2);
        ctx.fillStyle = highlight ? '#fbbf24' : '#fff'; ctx.fill();
        ctx.strokeStyle = isSelected ? '#fbbf24' : _laneStroke(idx);
        ctx.lineWidth = highlight ? 2 : 1; ctx.stroke();
        if (highlight) { ctx.fillStyle = '#fbbf24'; ctx.font = '9px monospace'; ctx.fillText(pi, p[0]+8, p[1]+3); }
    });
}

function drawCountingLine(lane) {
    if (!lane || !lane.counting_line) return;
    const cl = lane.counting_line;
    if (!cl.start || !cl.end) return;
    const s = [cl.start[0]/scaleX, cl.start[1]/scaleY];
    const e = [cl.end[0]/scaleX, cl.end[1]/scaleY];
    ctx.beginPath(); ctx.moveTo(s[0], s[1]); ctx.lineTo(e[0], e[1]);
    ctx.strokeStyle = '#f59e0b'; ctx.lineWidth = 3; ctx.stroke();
    ctx.beginPath(); ctx.arc(s[0], s[1], 6, 0, Math.PI*2); ctx.fillStyle = '#10b981'; ctx.fill();
    ctx.fillStyle = '#fff'; ctx.font = '7px monospace'; ctx.fillText('S', s[0]-3, s[1]+2.5);
    ctx.beginPath(); ctx.arc(e[0], e[1], 6, 0, Math.PI*2); ctx.fillStyle = '#ef4444'; ctx.fill();
    ctx.fillStyle = '#fff'; ctx.font = '7px monospace'; ctx.fillText('E', e[0]-3, e[1]+2.5);

    const ref = cl.direction_ref ? [cl.direction_ref[0]/scaleX, cl.direction_ref[1]/scaleY] : null;
    if (ref) {
        ctx.beginPath(); ctx.arc(ref[0], ref[1], 5, 0, Math.PI*2); ctx.fillStyle = '#f59e0b'; ctx.fill();
        const mx = (s[0]+e[0])/2, my = (s[1]+e[1])/2;
        const dx = ref[0]-mx, dy = ref[1]-my;
        const len = Math.sqrt(dx*dx + dy*dy);
        if (len > 0) {
            const nx = dx/len, ny = dy/len;
            ctx.beginPath(); ctx.moveTo(mx, my); ctx.lineTo(mx+nx*20, my+ny*20);
            ctx.strokeStyle = '#f59e0b'; ctx.lineWidth = 1.5; ctx.setLineDash([3,3]); ctx.stroke(); ctx.setLineDash([]);
            ctx.beginPath(); ctx.moveTo(mx+nx*20, my+ny*20);
            ctx.lineTo(mx+nx*20 - nx*6 - ny*4, my+ny*20 - ny*6 + nx*4);
            ctx.lineTo(mx+nx*20 - nx*6 + ny*4, my+ny*20 - ny*6 - nx*4);
            ctx.closePath(); ctx.fillStyle = '#f59e0b'; ctx.fill();
        }
    }
    ctx.fillStyle = '#fbbf24'; ctx.font = 'bold 10px monospace';
    ctx.fillText('COUNTING LINE', s[0], s[1]-12);
}

// ── Zone list ────────────────────────────────────────────────────────
function renderZoneList() {
    const list = document.getElementById('lanes-list'); if (!list) return;
    const badge = document.getElementById('zone-count-badge'); if (badge) badge.textContent = zonesData.length;
    list.innerHTML = '';
    zonesData.forEach((zone, idx) => {
        const isSel = idx === selectedZoneIdx;
        const div = document.createElement('div');
        div.className = 'flex items-center gap-2 px-3 py-2 rounded-lg cursor-pointer text-xs transition-all border ' +
            (isSel ? 'bg-amber-500/15 border-amber-500/30 text-amber-300' : 'bg-slate-950/50 border-slate-800/50 text-slate-400 hover:bg-slate-800/50 hover:text-slate-300');
        div.innerHTML = `<span class="w-2.5 h-2.5 rounded border-2 border-amber-400 flex-shrink-0"></span>
            <span class="flex-1 truncate">${zone.zone_id}</span>
            <span class="text-slate-600 mr-1">${zone.polygon.length} pts</span>
            <button onclick="event.stopPropagation(); deleteZoneAt(${idx})" class="text-slate-600 hover:text-rose-400 transition-colors p-0.5" title="Delete zone"><i data-lucide="x" class="h-3 w-3"></i></button>`;
        div.onclick = () => selectZone(idx);
        list.appendChild(div);
    });
    if (isDrawing && editorMode === 'zones') {
        const d = document.createElement('div');
        d.className = 'flex items-center gap-2 px-3 py-2 rounded-lg text-xs bg-amber-500/10 border border-amber-500/30 text-amber-300';
        d.innerHTML = `<span class="w-2.5 h-2.5 rounded-full bg-amber-400 animate-pulse flex-shrink-0"></span><span class="flex-1 truncate">Drawing zone... (${drawPoints.length} pts)</span>`;
        list.appendChild(d);
    }
    if (typeof lucide !== 'undefined') lucide.createIcons();
}

// ── Lane list ────────────────────────────────────────────────────────
function renderLaneList() {
    const list = document.getElementById('lanes-list'); if (!list) return;
    const badge = document.getElementById('lane-count-badge'); if (badge) badge.textContent = lanesData.length;
    list.innerHTML = '';
    lanesData.forEach((lane, idx) => {
        const isSel = idx === selectedLaneIdx;
        const div = document.createElement('div');
        div.className = 'flex items-center gap-2 px-3 py-2 rounded-lg cursor-pointer text-xs transition-all border ' +
            (isSel ? 'bg-indigo-500/15 border-indigo-500/30 text-indigo-300' : 'bg-slate-950/50 border-slate-800/50 text-slate-400 hover:bg-slate-800/50 hover:text-slate-300');
        div.innerHTML = `
            <span class="w-2.5 h-2.5 rounded-full flex-shrink-0" style="background:${_laneStroke(idx)}"></span>
            <span class="flex-1 truncate">${lane.lane_id}</span>
            <span class="text-slate-600 mr-1">${lane.polygon.length} pts</span>
            ${lane.counting_line ? '<i data-lucide="maximize-2" class="h-3 w-3 text-amber-400 flex-shrink-0"></i>' : ''}
            <button onclick="event.stopPropagation(); deleteLaneAt(${idx})" class="text-slate-600 hover:text-rose-400 transition-colors p-0.5" title="Delete lane"><i data-lucide="x" class="h-3 w-3"></i></button>`;
        div.onclick = () => selectLane(idx);
        list.appendChild(div);
    });
    if (isDrawing && editorMode === 'lanes') {
        const d = document.createElement('div');
        d.className = 'flex items-center gap-2 px-3 py-2 rounded-lg text-xs bg-emerald-500/10 border border-emerald-500/30 text-emerald-300';
        d.innerHTML = `<span class="w-2.5 h-2.5 rounded-full bg-emerald-400 animate-pulse flex-shrink-0"></span><span class="flex-1 truncate">Drawing lane... (${drawPoints.length} pts)</span>`;
        list.appendChild(d);
    }
    if (typeof lucide !== 'undefined') lucide.createIcons();
}

// ── Selection ────────────────────────────────────────────────────────
function selectZone(idx) {
    if (isDrawing) { isDrawing = false; drawPoints = []; }
    selectedZoneIdx = idx; selectedLaneIdx = -1;
    const zone = zonesData[idx];
    const panel = document.getElementById('zone-detail-panel');
    if (panel && zone) {
        panel.classList.remove('hidden');
        document.getElementById('zone-detail-order').textContent = '#'+(idx+1)+' / '+zonesData.length;
        document.getElementById('zone-detail-id').value = zone.zone_id;
        document.getElementById('zone-detail-name').value = zone.name || '';
    }
    hideDetailPanel('lane');
    renderCanvas(); renderZoneList(); updateButtons();
    setStatus('Editing zone: ' + zone.zone_id);
}
function selectLane(idx) {
    if (isDrawing) { isDrawing = false; drawPoints = []; }
    selectedLaneIdx = idx; selectedZoneIdx = -1;
    const lane = lanesData[idx];
    updateDetailPanel('lane', lane, idx);
    updateCountingLinePanel(lane);
    hideDetailPanel('zone');
    renderCanvas(); renderLaneList(); updateButtons();
    setStatus('Editing lane: ' + lane.lane_id);
}
function updateDetailPanel(type, data, idx) {
    const panel = document.getElementById(type + '-detail-panel');
    if (!panel || !data) { if (panel) panel.classList.add('hidden'); return; }
    panel.classList.remove('hidden');
    const arr = type === 'lane' ? lanesData : zonesData;
    document.getElementById(type + '-detail-order').textContent = '#'+(idx+1)+' / '+arr.length;
    document.getElementById(type + '-detail-id').value = data.zone_id || data.lane_id;
    document.getElementById(type + '-detail-name').value = data.name || '';
}
function hideDetailPanel(type) {
    const p = document.getElementById(type + '-detail-panel'); if (p) p.classList.add('hidden');
}

function updateSelectedZoneDetail() {
    if (selectedZoneIdx < 0) return;
    const z = zonesData[selectedZoneIdx];
    const newId = document.getElementById('zone-detail-id').value.trim();
    const newName = document.getElementById('zone-detail-name').value.trim();
    if (newId && newId !== z.zone_id) {
        if (zonesData.some((x,i) => i !== selectedZoneIdx && x.zone_id === newId)) {
            setStatus('Zone ID "'+newId+'" exists.'); document.getElementById('zone-detail-id').value = z.zone_id; return;
        }
        z.zone_id = newId;
    }
    z.name = newName; renderCanvas(); renderZoneList();
}
function updateSelectedLaneDetail() {
    if (selectedLaneIdx < 0) return;
    const l = lanesData[selectedLaneIdx];
    const newId = document.getElementById('lane-detail-id').value.trim();
    const newName = document.getElementById('lane-detail-name').value.trim();
    if (newId && newId !== l.lane_id) {
        if (lanesData.some((x,i) => i !== selectedLaneIdx && x.lane_id === newId)) {
            setStatus('Lane ID "'+newId+'" exists.'); document.getElementById('lane-detail-id').value = l.lane_id; return;
        }
        l.lane_id = newId;
    }
    l.name = newName; renderCanvas(); renderLaneList();
}

function moveZoneUp() { if (selectedZoneIdx <= 0) return; swap(zonesData, selectedZoneIdx, selectedZoneIdx-1); selectedZoneIdx--; selectZone(selectedZoneIdx); }
function moveZoneDown() { if (selectedZoneIdx < 0 || selectedZoneIdx >= zonesData.length-1) return; swap(zonesData, selectedZoneIdx, selectedZoneIdx+1); selectedZoneIdx++; selectZone(selectedZoneIdx); }
function moveLaneUp() { if (selectedLaneIdx <= 0) return; swap(lanesData, selectedLaneIdx, selectedLaneIdx-1); selectedLaneIdx--; selectLane(selectedLaneIdx); }
function moveLaneDown() { if (selectedLaneIdx < 0 || selectedLaneIdx >= lanesData.length-1) return; swap(lanesData, selectedLaneIdx, selectedLaneIdx+1); selectedLaneIdx++; selectLane(selectedLaneIdx); }
function swap(arr, i, j) { const t = arr[i]; arr[i] = arr[j]; arr[j] = t; }

// ── Drawing (shared) ────────────────────────────────────────────────
function startAddZone() {
    if (isDrawing) { isDrawing = false; drawPoints = []; }
    selectedZoneIdx = -1; hideDetailPanel('zone');
    isDrawing = true; drawPoints = [];
    renderCanvas(); renderZoneList(); updateButtons();
    setStatus('Click on canvas to place zone vertices. Double-click to finish.');
}
function startAddLane() {
    if (isDrawing) { isDrawing = false; drawPoints = []; }
    selectedLaneIdx = -1; hideDetailPanel('lane');
    isDrawing = true; drawPoints = [];
    renderCanvas(); renderLaneList(); updateButtons();
    setStatus('Click on canvas to place lane polygon vertices. Double-click to finish.');
}
function finishDrawing() {
    if (drawPoints.length < 3) { setStatus('Need at least 3 points.'); return; }
    const polygon = drawPoints.map(p => [Math.round(p[0]*scaleX), Math.round(p[1]*scaleY)]);
    if (editorMode === 'zones') {
        const zoneId = 'zone_'+(zonesData.length+1);
        zonesData.push({ zone_id: zoneId, name: '', polygon });
        isDrawing = false; drawPoints = [];
        selectedZoneIdx = zonesData.length-1; selectZone(selectedZoneIdx);
        setStatus('Zone '+zoneId+' added.');
    } else {
        const laneId = 'lane_'+(lanesData.length+1);
        lanesData.push({ lane_id: laneId, name: '', polygon, counting_line: null });
        isDrawing = false; drawPoints = [];
        selectedLaneIdx = lanesData.length-1; selectLane(selectedLaneIdx);
        setStatus('Lane '+laneId+' added.');
    }
    renderCanvas();
    if (editorMode === 'zones') renderZoneList(); else renderLaneList();
    updateButtons();
}
function undoLastPoint() {
    if (isDrawing && drawPoints.length > 0) { drawPoints.pop(); renderCanvas(); updateButtons(); }
}

function deleteSelectedZone() {
    if (selectedZoneIdx < 0) return;
    deleteZoneAt(selectedZoneIdx);
}
function deleteZoneAt(idx) {
    if (idx < 0 || idx >= zonesData.length) return;
    const z = zonesData[idx];
    if (!confirm('Delete zone "'+z.zone_id+'"?')) return;
    zonesData.splice(idx,1);
    if (selectedZoneIdx === idx) { selectedZoneIdx = -1; hideDetailPanel('zone'); }
    else if (selectedZoneIdx > idx) selectedZoneIdx--;
    renderCanvas(); renderZoneList(); updateButtons();
}
function deleteSelectedLane() {
    if (selectedLaneIdx < 0) return;
    deleteLaneAt(selectedLaneIdx);
}
function deleteLaneAt(idx) {
    if (idx < 0 || idx >= lanesData.length) return;
    const l = lanesData[idx];
    if (!confirm('Delete lane "'+l.lane_id+'"?')) return;
    lanesData.splice(idx,1);
    if (selectedLaneIdx === idx) { selectedLaneIdx = -1; hideCountingLinePanel(); hideDetailPanel('lane'); }
    else if (selectedLaneIdx > idx) selectedLaneIdx--;
    renderCanvas(); renderLaneList(); updateButtons();
}

// ── Buttons ──────────────────────────────────────────────────────────
function updateButtons() {
    const dZ = document.getElementById('btn-zone-delete'); if (dZ) dZ.disabled = (selectedZoneIdx < 0 || isDrawing);
    const dL = document.getElementById('btn-lane-delete'); if (dL) dL.disabled = (selectedLaneIdx < 0 || isDrawing);
    const u = document.getElementById('btn-lane-undo'); if (u) u.disabled = !(isDrawing && drawPoints.length > 0);
}

// ── Resize handler ──────────────────────────────────────────────────
let _resizeTimer = null;
function handleCanvasResize() {
    if (!canvasEl || !bgImage) return;
    clearTimeout(_resizeTimer);
    _resizeTimer = setTimeout(() => {
        const wrapper = document.getElementById('canvas-wrapper');
        if (!wrapper) return;
        const newW = Math.min(1600, wrapper.clientWidth || 1200);
        if (Math.abs(newW - canvasW) < 20) return; // no meaningful change
        const aspect = imgW / imgH;
        canvasW = newW; canvasH = newW / aspect;
        canvasEl.width = canvasW; canvasEl.height = canvasH;
        scaleX = imgW / canvasW; scaleY = imgH / canvasH;
        renderCanvas();
    }, 150);
}
if (typeof window !== 'undefined') {
    window.addEventListener('resize', handleCanvasResize);
}
function setStatus(msg) { const el = document.getElementById('lane-status-text'); if (el) el.textContent = msg; }

// ── Counting line ────────────────────────────────────────────────────
function updateCountingLinePanel(lane) {
    const panel = document.getElementById('counting-line-panel');
    if (!panel || !lane) { hideCountingLinePanel(); return; }
    panel.classList.remove('hidden');
    const toggle = document.getElementById('counting-line-toggle');
    const hasCL = !!lane.counting_line;
    toggle.checked = hasCL;
    document.getElementById('cl-start-x').value = hasCL ? lane.counting_line.start[0] : '';
    document.getElementById('cl-start-y').value = hasCL ? lane.counting_line.start[1] : '';
    document.getElementById('cl-end-x').value = hasCL ? lane.counting_line.end[0] : '';
    document.getElementById('cl-end-y').value = hasCL ? lane.counting_line.end[1] : '';
    document.getElementById('cl-ref-x').value = hasCL ? lane.counting_line.direction_ref[0] : '';
    document.getElementById('cl-ref-y').value = hasCL ? lane.counting_line.direction_ref[1] : '';
    document.querySelectorAll('#counting-line-panel input[type="number"]').forEach(el => el.disabled = !hasCL);
}
function hideCountingLinePanel() { const p = document.getElementById('counting-line-panel'); if (p) p.classList.add('hidden'); }
function toggleCountingLine() {
    const toggle = document.getElementById('counting-line-toggle'); if (!toggle) return;
    if (selectedLaneIdx < 0) return;
    const lane = lanesData[selectedLaneIdx];
    if (toggle.checked) lane.counting_line = { start: [0,0], end: [0,0], direction_ref: [0,0] };
    else lane.counting_line = null;
    updateCountingLinePanel(lane); renderCanvas();
}
function updateCountingLineFromInputs() {
    if (selectedLaneIdx < 0) return;
    const lane = lanesData[selectedLaneIdx]; if (!lane.counting_line) return;
    lane.counting_line.start[0] = parseFloat(document.getElementById('cl-start-x').value)||0;
    lane.counting_line.start[1] = parseFloat(document.getElementById('cl-start-y').value)||0;
    lane.counting_line.end[0] = parseFloat(document.getElementById('cl-end-x').value)||0;
    lane.counting_line.end[1] = parseFloat(document.getElementById('cl-end-y').value)||0;
    lane.counting_line.direction_ref[0] = parseFloat(document.getElementById('cl-ref-x').value)||0;
    lane.counting_line.direction_ref[1] = parseFloat(document.getElementById('cl-ref-y').value)||0;
    renderCanvas();
}

// ── Canvas events ────────────────────────────────────────────────────
function setupCanvasEvents() {
    const c = document.getElementById('lanes-canvas');
    if (!c || _canvasEventsSetup) return;
    _canvasEventsSetup = true;
    c.addEventListener('mousedown', onMouseDown);
    c.addEventListener('mousemove', onMouseMove);
    c.addEventListener('mouseup', onMouseUp);
    c.addEventListener('dblclick', onDblClick);
    c.addEventListener('mouseleave', onMouseLeave);
}
function getCoords(e) {
    const rect = canvasEl.getBoundingClientRect();
    return { x: (e.clientX-rect.left)*(canvasEl.width/rect.width), y: (e.clientY-rect.top)*(canvasEl.height/rect.height) };
}
function getOriginalCoords(e) {
    const c = getCoords(e);
    return { x: Math.round(c.x*scaleX), y: Math.round(c.y*scaleY), cx: c.x, cy: c.y };
}

function onMouseDown(e) {
    const { cx, cy, x, y } = getOriginalCoords(e);
    setStatus('Click at ('+x+', '+y+')');

    if (isDrawing) {
        // Check if clicking near the first point → auto-close polygon
        if (drawPoints.length >= 3) {
            const first = drawPoints[0];
            const dist = Math.hypot(cx - first[0], cy - first[1]);
            if (dist < 15) { finishDrawing(); return; }
        }
        drawPoints.push([cx, cy]);
        renderCanvas(); updateButtons();
        setStatus('Point '+drawPoints.length+'. Click the first point or double-click to finish.');
        return;
    }

    // Try drag on vertices of selected item
    if (editorMode === 'zones' && selectedZoneIdx >= 0) {
        const z = zonesData[selectedZoneIdx];
        const th = 10;
        for (let i = 0; i < (z.polygon||[]).length; i++) {
            if (Math.hypot(cx - z.polygon[i][0]/scaleX, cy - z.polygon[i][1]/scaleY) < th) {
                isDragging = true; dragTargetZoneIdx = selectedZoneIdx; dragTargetLaneIdx = -1; dragVertexPointIdx = i; return;
            }
        }
    }
    if (editorMode === 'lanes' && selectedLaneIdx >= 0) {
        const l = lanesData[selectedLaneIdx];
        // Check counting line drag first
        if (l.counting_line && l.counting_line.start) {
            const th = 12;
            const s = [l.counting_line.start[0]/scaleX, l.counting_line.start[1]/scaleY];
            const e = [l.counting_line.end[0]/scaleX, l.counting_line.end[1]/scaleY];
            const ref = l.counting_line.direction_ref ? [l.counting_line.direction_ref[0]/scaleX, l.counting_line.direction_ref[1]/scaleY] : null;
            if (Math.hypot(cx-s[0], cy-s[1]) < th) { isDragging = true; dragCLPoint = 'start'; return; }
            if (Math.hypot(cx-e[0], cy-e[1]) < th) { isDragging = true; dragCLPoint = 'end'; return; }
            if (ref && Math.hypot(cx-ref[0], cy-ref[1]) < th) { isDragging = true; dragCLPoint = 'direction_ref'; return; }
        }
        const th = 10;
        for (let i = 0; i < (l.polygon||[]).length; i++) {
            if (Math.hypot(cx - l.polygon[i][0]/scaleX, cy - l.polygon[i][1]/scaleY) < th) {
                isDragging = true; dragTargetLaneIdx = selectedLaneIdx; dragTargetZoneIdx = -1; dragVertexPointIdx = i; return;
            }
        }
    }

    // Click to select zone/lane
    if (editorMode === 'zones') {
        for (let i = zonesData.length-1; i >= 0; i--) {
            const z = zonesData[i]; if (!z.polygon || z.polygon.length < 3) continue;
            if (pointInPolygon([cx, cy], z.polygon.map(p => [p[0]/scaleX, p[1]/scaleY]))) { selectZone(i); return; }
        }
        if (selectedZoneIdx >= 0) { selectedZoneIdx = -1; hideDetailPanel('zone'); renderCanvas(); renderZoneList(); updateButtons(); setStatus('Deselected.'); }
    } else {
        for (let i = lanesData.length-1; i >= 0; i--) {
            const l = lanesData[i]; if (!l.polygon || l.polygon.length < 3) continue;
            if (pointInPolygon([cx, cy], l.polygon.map(p => [p[0]/scaleX, p[1]/scaleY]))) { selectLane(i); return; }
        }
        if (selectedLaneIdx >= 0) { selectedLaneIdx = -1; hideCountingLinePanel(); hideDetailPanel('lane'); renderCanvas(); renderLaneList(); updateButtons(); setStatus('Deselected.'); }
    }
}

function onMouseMove(e) {
    const { cx, cy, x, y } = getOriginalCoords(e);
    const tooltip = document.getElementById('canvas-tooltip');
    if (tooltip) { tooltip.style.left = (e.offsetX+15)+'px'; tooltip.style.top = (e.offsetY-10)+'px'; tooltip.textContent = x+', '+y; tooltip.classList.remove('hidden'); }
    if (!ctx) return;

    if (isDragging && dragTargetZoneIdx >= 0 && editorMode === 'zones') {
        const z = zonesData[dragTargetZoneIdx];
        if (z && dragVertexPointIdx < z.polygon.length) { z.polygon[dragVertexPointIdx] = [Math.round(cx*scaleX), Math.round(cy*scaleY)]; renderCanvas(); }
        return;
    }
    if (isDragging && dragTargetLaneIdx >= 0 && editorMode === 'lanes') {
        const l = lanesData[dragTargetLaneIdx];
        if (dragVertexPointIdx >= 0 && l && dragVertexPointIdx < l.polygon.length) {
            l.polygon[dragVertexPointIdx] = [x, y]; renderCanvas(); return;
        }
        if (dragCLPoint && l && l.counting_line) {
            l.counting_line[dragCLPoint] = [x, y];
            const sfx = dragCLPoint === 'start' ? '-start' : dragCLPoint === 'end' ? '-end' : '-ref';
            document.getElementById('cl'+sfx+'-x').value = x; document.getElementById('cl'+sfx+'-y').value = y;
            renderCanvas(); return;
        }
    }
}

function onMouseUp(e) {
    if (isDragging) { isDragging = false; dragTargetZoneIdx = -1; dragTargetLaneIdx = -1; dragVertexPointIdx = -1; dragCLPoint = null; }
}
function onDblClick(e) {
    if (isDrawing && drawPoints.length >= 3) finishDrawing();
    else if (isDrawing) setStatus('Need at least 3 points. Have '+drawPoints.length+'.');
}
function onMouseLeave() { const t = document.getElementById('canvas-tooltip'); if (t) t.classList.add('hidden'); isDragging = false; }

function pointInPolygon(pt, polygon) {
    let inside = false;
    for (let i = 0, j = polygon.length-1; i < polygon.length; j = i++) {
        const xi = polygon[i][0], yi = polygon[i][1], xj = polygon[j][0], yj = polygon[j][1];
        if ((yi > pt[1]) !== (yj > pt[1]) && pt[0] < (xj-xi)*(pt[1]-yi)/(yj-yi)+xi) inside = !inside;
    }
    return inside;
}

// ── Save ─────────────────────────────────────────────────────────────
async function saveZonesConfig() {
    const camera_id = document.getElementById('lanes-cam-select').value;
    if (!camera_id) return;
    if (!zonesData.length) { setStatus('No zones to save.'); return; }
    for (const z of zonesData) { if (!z.polygon || z.polygon.length < 3) { setStatus('Zone '+z.zone_id+' needs >=3 points.'); return; } }
    const payload = { zones: zonesData.map(z => ({ zone_id: z.zone_id, name: z.name||'', polygon: z.polygon })) };
    try {
        const res = await apiRequest(`/api/cameras/${camera_id}/zones`, { method: 'PUT', headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload) });
        if (res) {
            setStatus('Saved '+zonesData.length+' zones.');
            if (typeof showToast === 'function') showToast({severity:'success', title:'Zones Saved', message:zonesData.length+' zones for '+camera_id});
        } else { setStatus('Save failed.'); }
    } catch(e) { setStatus('Save error: '+e.message); }
}

async function saveLanesConfig() {
    const camera_id = document.getElementById('lanes-cam-select').value;
    if (!camera_id) return;
    if (!lanesData.length) { setStatus('No lanes to save.'); return; }
    for (const l of lanesData) { if (!l.polygon || l.polygon.length < 3) { setStatus('Lane '+l.lane_id+' needs >=3 points.'); return; } }
    const payload = { lanes: lanesData.map(l => ({ lane_id: l.lane_id, name: l.name||'', polygon: l.polygon, counting_line: l.counting_line||undefined })) };
    try {
        const res = await apiRequest(`/api/cameras/${camera_id}/lanes`, { method: 'PUT', headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload) });
        if (res) {
            setStatus('Saved '+lanesData.length+' lanes.');
            if (typeof showToast === 'function') showToast({severity:'success', title:'Lanes Saved', message:lanesData.length+' lanes for '+camera_id});
        } else { setStatus('Save failed.'); }
    } catch(e) { setStatus('Save error: '+e.message); }
}
function renderLiveOccupancy(data) {
    const occList = document.getElementById('live-occupancy-list');
    if (!occList) return;
    if (!data || !Object.keys(data).length) {
        occList.innerHTML = '<p class="text-xs text-slate-500 text-center py-4">No vehicles detected</p>';
        return;
    }
    const entries = Object.entries(data).sort((a, b) => a[0].localeCompare(b[0]));
    const maxCount = Math.max(...Object.values(data), 1);
    occList.innerHTML = entries.map(([lane, count]) => {
        const pct = Math.round(count / maxCount * 100);
        return `<div class="bg-slate-950 p-3 rounded-lg border border-slate-800">
            <div class="flex justify-between mb-1.5">
                <span class="text-xs font-medium text-slate-300">${lane}</span>
                <span class="text-xs font-bold text-white">${count} veh</span>
            </div>
            <div class="w-full bg-slate-800 h-1 rounded-full">
                <div class="bg-indigo-500 h-1 rounded-full" style="width:${pct}%"></div>
            </div>
        </div>`;
    }).join('');
}

function updateVehicleTypeDisplay() {
    const car = (_sessionCounts.car || 0);
    const moto = (_sessionCounts.motorcycle || 0) + (_sessionCounts.motorbike || 0);
    const truck = (_sessionCounts.truck || 0);
    const bus = (_sessionCounts.bus || 0);
    const total = car + moto + truck + bus || 1;

    const setEl = (id, val) => { const el = document.getElementById(id); if (el) el.innerText = val; };
    setEl('live-type-car', car);
    setEl('live-type-moto', moto);
    setEl('live-type-truck', truck);
    setEl('live-type-bus', bus);

    const barEl = (id, val) => { const el = document.getElementById(id); if (el) el.style.width = val; };
    barEl('live-type-car-bar', (car / total * 100) + '%');
    barEl('live-type-moto-bar', (moto / total * 100) + '%');
    barEl('live-type-truck-bar', (truck / total * 100) + '%');
    barEl('live-type-bus-bar', (bus / total * 100) + '%');
}

async function loadLiveCameraData() {
    const selectEl = document.getElementById('live-cam-select');
    if (!selectEl) return;
    const camera_id = selectEl.value;
    if (!camera_id) return;
    _lastLiveCam = camera_id;

    _sessionCounts = { car: 0, motorcycle: 0, motorbike: 0, truck: 0, bus: 0 };
    updateVehicleTypeDisplay();

    // Step 1: Show camera snapshot immediately while data loads
    const container = document.getElementById('live-video-container');
    if (container) {
        _showSnapshot(container, camera_id);
    }

    // Step 2: Fetch occupancy, summary, lane changes in parallel
    const [occ, summary, changes] = await Promise.all([
        apiRequest(`/api/cameras/${camera_id}/occupancy/latest`),
        apiRequest(`/api/cameras/${camera_id}/counts/summary`),
        apiRequest(`/api/cameras/${camera_id}/lane-changes?limit=5`),
    ]);

    renderLiveOccupancy(occ ? occ.occupancy : null);

    if (summary && summary.lanes) {
        summary.lanes.forEach(l => {
            Object.entries(l.types || {}).forEach(([type, count]) => {
                _sessionCounts[type] = (_sessionCounts[type] || 0) + count;
            });
        });
        updateVehicleTypeDisplay();
    }

    // Step 3: Render lane changes (uses previous_lane_id/current_lane_id from backend)
    const tbody = document.getElementById('live-lane-changes-tbody');
    if (tbody) {
        if (changes && changes.length) {
            tbody.innerHTML = changes.map(e => `
                <tr class="border-b border-slate-800/50">
                    <td class="py-1.5 text-white">#${e.track_id}</td>
                    <td class="py-1.5 text-slate-400">${e.previous_lane_id || '—'}</td>
                    <td class="py-1.5 text-emerald-400 font-semibold">${e.current_lane_id || '—'}</td>
                    <td class="py-1.5 text-slate-500">${e.frame_id}</td>
                </tr>
            `).join('');
        } else {
            tbody.innerHTML = '<tr><td colspan="4" class="py-2 text-center text-slate-600 text-xs">No events yet</td></tr>';
        }
    }

    // Step 4: Start MJPEG — insert <img> directly, pipeline auto-starts on browser request
    _startMJPEGStream(camera_id);
    startLiveMetricsPolling(camera_id);
    connectLiveWS(camera_id);
    _monitorStream(camera_id);
}

// ── Snapshot (shown immediately, before pipeline starts) ──────────────

function _showSnapshot(container, cameraId) {
    const snapshotUrl = BASE_URL + `/api/cameras/${cameraId}/snapshot`;
    container.innerHTML = `
        <div class="relative w-full h-full flex flex-col items-center justify-center">
            <img class="w-full h-full rounded-xl object-contain bg-slate-950"
                 src="${snapshotUrl}"
                 alt="Camera snapshot"
                 id="live-snapshot-img"
                 onerror="this.style.display='none'">
            <div class="absolute bottom-3 left-3 flex items-center gap-2 bg-slate-950/80 px-3 py-1.5 rounded-lg">
                <span class="w-2 h-2 rounded-full bg-amber-500 inline-block animate-pulse"></span>
                <span class="text-xs text-amber-400">Connecting to live stream...</span>
            </div>
        </div>`;
    lucide.createIcons();

    // Pre-warm snapshot by loading it (browser caches it for instant display)
    const preload = new Image();
    preload.src = snapshotUrl;
}

// ── MJPEG stream ──────────────────────────────────────────────────────
// Pipeline auto-starts on first request to /live/{id}/stream.mjpg.
// We insert an <img> tag pointing to the MJPEG URL — browser sends GET,
// server starts pipeline if not already running, returns multipart stream.
// If pipeline start fails or source is invalid, the server returns an
// error response (404/500), which triggers img onerror → fallback.

let _mjpegRetryTimer = null;
let _streamHealthTimer = null;
let _mjpegCameraId = null;

function _startMJPEGStream(cameraId) {
    const container = document.getElementById('live-video-container');
    if (!container) return;
    _mjpegCameraId = cameraId;
    clearTimeout(_mjpegRetryTimer);

    const liveUrl = BASE_URL + '/live/' + encodeURIComponent(cameraId) + '/stream.mjpg';

    // Direct <img> tag — browser requests MJPEG, pipeline auto-starts on server side
    // This is the simplest and most reliable approach.
    const existingSnapshot = container.querySelector('#live-snapshot-img');

    if (existingSnapshot) {
        // Replace snapshot src with MJPEG URL — same <img>, just change src
        // If that doesn't work (browser may not switch from JPEG to multipart),
        // fall back to replacing the whole inner HTML.
        existingSnapshot.onerror = () => _showStreamError(cameraId, container);
        existingSnapshot.src = liveUrl;
        existingSnapshot.id = 'live-mjpeg-img';

        // Update status badge
        const badge = container.querySelector('.absolute.bottom-3');
        if (badge) {
            badge.innerHTML = `
                <span class="w-2 h-2 rounded-full bg-emerald-500 inline-block animate-pulse"></span>
                <span class="text-xs text-emerald-400">LIVE</span>
            `;
        }
        lucide.createIcons();

        // If after 10s the img hasn't updated (MJPEG stream not started), fallback
        clearTimeout(_mjpegRetryTimer);
        _mjpegRetryTimer = setTimeout(() => {
            // Check if still showing snapshot (old image) — if so, pipeline failed
            if (_mjpegCameraId !== cameraId) return;
            _showStreamError(cameraId, container);
        }, 10000);
    } else {
        // No existing snapshot img (shouldn't happen) — create fresh
        container.innerHTML = `
            <div class="relative w-full h-full">
                <img class="w-full h-full rounded-xl object-contain bg-slate-950"
                     src="${liveUrl}"
                     alt="Live stream"
                     id="live-mjpeg-img"
                     onerror="_showStreamError('${cameraId}', document.getElementById('live-video-container'))">
                <div class="absolute top-3 left-3 flex items-center gap-2 bg-slate-950/80 px-3 py-1.5 rounded-lg">
                    <span class="w-2 h-2 rounded-full bg-emerald-500 inline-block animate-pulse"></span>
                    <span class="text-xs text-emerald-400">LIVE</span>
                </div>
            </div>`;
        lucide.createIcons();
    }
}

function _showStreamError(cameraId, container) {
    if (!container) container = document.getElementById('live-video-container');
    if (!container || _mjpegCameraId !== cameraId) return;
    clearTimeout(_mjpegRetryTimer);
    _mjpegCameraId = null;

    // Clean broken img
    const img = container.querySelector('#live-mjpeg-img');
    if (img) { img.onerror = null; img.src = ''; img.remove(); }

    // Show snapshot + error
    const snapshotUrl = BASE_URL + `/api/cameras/${cameraId}/snapshot`;
    container.innerHTML = `
        <div class="relative w-full h-full flex flex-col items-center justify-center">
            <img class="w-full h-full rounded-xl object-contain bg-slate-950"
                 src="${snapshotUrl}"
                 alt="Camera snapshot"
                 onerror="this.style.display='none'">
            <div class="absolute bottom-3 left-3 flex items-center gap-2 bg-slate-950/80 px-3 py-1.5 rounded-lg">
                <i data-lucide="video-off" class="h-3.5 w-3.5 text-rose-400"></i>
                <span class="text-xs text-rose-400">Stream: starting...</span>
                <button onclick="loadLiveCameraData()" class="text-xs text-indigo-400 hover:text-indigo-300 ml-2">Retry</button>
            </div>
        </div>`;
    lucide.createIcons();

    // Auto-retry after 10s (maybe pipeline just needs more time)
    _mjpegRetryTimer = setTimeout(() => {
        if (_lastLiveCam !== cameraId) return;
        _startMJPEGStream(cameraId);
    }, 10000);
}

function _monitorStream(cameraId) {
    if (_streamHealthTimer) clearInterval(_streamHealthTimer);
    _streamHealthTimer = setInterval(async () => {
        if (_lastLiveCam !== cameraId) {
            clearInterval(_streamHealthTimer);
            return;
        }
        try {
            const m = await apiRequest('/live/' + encodeURIComponent(cameraId) + '/metrics');
            const statusEl = document.getElementById('live-stream-status');
            if (!statusEl) return;
            if (m && m.fps !== undefined && m.fps > 0) {
                statusEl.textContent = m.fps.toFixed(1) + ' FPS';
            } else if (m && m.fps !== undefined) {
                statusEl.textContent = 'Pipeline starting...';
            } else {
                statusEl.textContent = 'Idle';
            }
        } catch (e) {}
    }, 3000);
}

// ── Metrics polling ──────────────────────────────────────────────────

function startLiveMetricsPolling(cameraId) {
    if (_liveMetricsTimer) clearInterval(_liveMetricsTimer);
    _liveMetricsTimer = setInterval(async () => {
        if (_lastLiveCam !== cameraId) { clearInterval(_liveMetricsTimer); return; }
        try {
            const m = await apiRequest('/live/' + encodeURIComponent(cameraId) + '/metrics');
            const set = (id, val) => { const el = document.getElementById(id); if (el) el.innerText = val; };
            if (!m || (m.fps === undefined && !m.avg_latency_ms)) {
                set('live-fps', 'Idle');
                set('live-latency', 'No pipeline');
                set('live-gpu', '—');
                return;
            }
            set('live-fps', m.fps > 0 ? m.fps.toFixed(1) : 'Starting...');
            set('live-latency', m.avg_latency_ms > 0 ? m.avg_latency_ms.toFixed(0) + 'ms' : '—');
            set('live-gpu', m.gpu_available && m.gpu_util_pct >= 0 ? m.gpu_util_pct.toFixed(0) + '%' : 'N/A');
        } catch(e) {}
    }, 2000);
}

// ── WebSocket ────────────────────────────────────────────────────────

function connectLiveWS(cameraId) {
    if (_ws) { _ws.close(); _ws = null; }
    if (_wsReconnectTimer) { clearTimeout(_wsReconnectTimer); _wsReconnectTimer = null; }

    const token = localStorage.getItem('access_token');
    if (!token) return;

    const wsProto = BASE_URL.startsWith('https') ? 'wss' : 'ws';
    const wsHost = BASE_URL.replace(/^https?:\/\//, '');
    try {
        _ws = new WebSocket(wsProto + '://' + wsHost + '/ws/live');
        _ws.onopen = () => {
            _ws.send(JSON.stringify({ token, cameras: cameraId }));
        };
        _ws.onmessage = (event) => {
            try {
                const msg = JSON.parse(event.data);
                if (msg.type === 'ping') { _ws.send(JSON.stringify({type:'pong'})); return; }
                if (msg.type === 'connected') return;

                if (msg.type === 'occupancy_update' && activeTab === 'live') {
                    const occData = msg.data && msg.data.occupancy ? msg.data.occupancy : msg.data;
                    renderLiveOccupancy(occData);
                }

                if (msg.type === 'count_event' && activeTab === 'live') {
                    const cls = (msg.data.class_name || '').toLowerCase();
                    if (cls) {
                        _sessionCounts[cls] = (_sessionCounts[cls] || 0) + 1;
                        updateVehicleTypeDisplay();
                    }
                }

                if (msg.type === 'lane_change_event' && activeTab === 'live') {
                    const selectEl = document.getElementById('live-cam-select');
                    if (selectEl) {
                        apiRequest(`/api/cameras/${selectEl.value}/lane-changes?limit=5`).then(changes => {
                            const tbody = document.getElementById('live-lane-changes-tbody');
                            if (!tbody || !changes || !changes.length) return;
                            tbody.innerHTML = changes.map(e => `
                                <tr class="border-b border-slate-800/50">
                                    <td class="py-1.5 text-white">#${e.track_id}</td>
                                    <td class="py-1.5 text-slate-400">${e.previous_lane_id || '—'}</td>
                                    <td class="py-1.5 text-emerald-400 font-semibold">${e.current_lane_id || '—'}</td>
                                    <td class="py-1.5 text-slate-500">${e.frame_id}</td>
                                </tr>
                            `).join('');
                        });
                    }
                }

                if (msg.type === 'alert' && msg.data) {
                    showAlertToast(msg.data);
                    updateAlertBadge();
                    if (activeTab === 'alerts') refreshAlerts();
                }
            } catch(e) {}
        };
        _ws.onclose = () => {
            _wsReconnectTimer = setTimeout(() => connectLiveWS(cameraId), 10000);
        };
        _ws.onerror = () => { if (_ws) _ws.close(); };
    } catch(e) {}
}
function renderModelsList() {
    const container = document.getElementById('models-list-container');
    if (!container) return;
    container.innerHTML = modelsList.map(m => `
        <div class="bg-slate-900/40 border border-slate-800 rounded-xl p-6">
            <div class="flex justify-between items-start mb-3">
                <div class="flex-1 min-w-0">
                    <div class="flex items-center gap-3 mb-1">
                        <h4 class="text-base font-bold text-white truncate">${m.model_id}</h4>
                        <span class="text-xs font-bold px-2.5 py-0.5 rounded-lg bg-indigo-500/10 text-indigo-400 border border-indigo-500/20 flex-shrink-0">${m.class_mode}</span>
                    </div>
                    <p class="text-xs text-slate-500 font-mono truncate">${m.model_path}</p>
                </div>
                <div class="flex items-center gap-1 flex-shrink-0 ml-3">
                    <button onclick="renameModel('${m.model_id}')" class="p-1.5 rounded hover:bg-amber-500/10 text-slate-500 hover:text-amber-400 transition-all" title="Rename model">
                        <i data-lucide="pencil" class="h-3.5 w-3.5"></i>
                    </button>
                    <button onclick="deleteModel('${m.model_id}')" class="p-1.5 rounded hover:bg-rose-500/10 text-slate-500 hover:text-rose-400 transition-all" title="Delete model">
                        <i data-lucide="trash-2" class="h-3.5 w-3.5"></i>
                    </button>
                </div>
            </div>
            <p class="text-slate-400 text-sm mt-1 line-clamp-2">${m.description || '—'}</p>
        </div>
    `).join('');
    if (typeof lucide !== 'undefined') lucide.createIcons();
}

function showAddModelForm() {
    const existing = document.getElementById('add-model-modal');
    if (existing) existing.remove();

    const modal = document.createElement('div');
    modal.id = 'add-model-modal';
    modal.className = 'fixed inset-0 z-50 flex items-center justify-center bg-black/70';
    modal.innerHTML = `
        <div class="bg-slate-900 border border-slate-700 rounded-xl p-6 w-96 max-h-[90vh] overflow-y-auto">
            <h3 class="text-lg font-bold text-white mb-4">Add Model</h3>
            <div class="space-y-3">
                <div>
                    <label class="text-xs font-semibold text-slate-400 mb-1 block">Model ID</label>
                    <input id="new-model-id" class="w-full bg-slate-800 border border-slate-700 rounded px-3 py-2 text-white text-sm focus:outline-none focus:border-indigo-500" placeholder="e.g. my_custom_model">
                </div>
                <div>
                    <label class="text-xs font-semibold text-slate-400 mb-1 block">Weights File</label>
                    <input id="new-model-file" type="file" accept=".pt,.pth,.onnx,.engine,.trt,.torchscript" class="w-full text-sm text-slate-300 file:mr-3 file:py-1.5 file:px-3 file:rounded-lg file:border-0 file:text-xs file:font-semibold file:bg-indigo-600 file:text-white hover:file:bg-indigo-500 file:cursor-pointer bg-slate-800 border border-slate-700 rounded px-3 py-1.5">
                    <p class="text-[10px] text-slate-600 mt-1">Supports: .pt .pth .onnx .engine .trt .torchscript</p>
                </div>
                <div>
                    <label class="text-xs font-semibold text-slate-400 mb-1 block">Class Mode</label>
                    <select id="new-model-class" class="w-full bg-slate-800 border border-slate-700 rounded px-3 py-2 text-white text-sm">
                        <option value="coco_pretrained">COCO Pretrained</option>
                        <option value="custom">Custom</option>
                    </select>
                </div>
                <div>
                    <label class="text-xs font-semibold text-slate-400 mb-1 block">Description</label>
                    <textarea id="new-model-desc" rows="2" class="w-full bg-slate-800 border border-slate-700 rounded px-3 py-2 text-white text-sm resize-none focus:outline-none focus:border-indigo-500" placeholder="Optional description"></textarea>
                </div>
            </div>
            <div class="flex gap-2 mt-5">
                <button onclick="submitNewModel()" class="flex-1 py-2 rounded-lg text-sm font-semibold bg-indigo-600 hover:bg-indigo-500 text-white transition-all">Upload &amp; Register</button>
                <button onclick="document.getElementById('add-model-modal').remove()" class="flex-1 py-2 rounded-lg text-sm font-semibold bg-slate-700 hover:bg-slate-600 text-white transition-all">Cancel</button>
            </div>
        </div>
    `;
    document.body.appendChild(modal);
    if (typeof lucide !== 'undefined') lucide.createIcons();
}

async function submitNewModel() {
    const fileInput = document.getElementById('new-model-file');
    const modelId = document.getElementById('new-model-id').value.trim();
    const classMode = document.getElementById('new-model-class').value;
    const desc = document.getElementById('new-model-desc').value.trim();

    if (!modelId) {
        showToast({severity: 'warning', title: 'Missing Fields', message: 'Model ID is required.'});
        return;
    }
    if (!fileInput || !fileInput.files || !fileInput.files[0]) {
        showToast({severity: 'warning', title: 'Missing Fields', message: 'Please select a weights file to upload.'});
        return;
    }

    // Show uploading state
    const btn = document.querySelector('#add-model-modal button:first-of-type');
    if (btn) { btn.disabled = true; btn.innerText = 'Uploading...'; }

    const fd = new FormData();
    fd.append('file', fileInput.files[0]);
    fd.append('model_id', modelId);
    fd.append('class_mode', classMode);
    fd.append('description', desc);

    const token = localStorage.getItem('access_token');
    try {
        const res = await fetch(BASE_URL + '/api/models/upload', {
            method: 'POST',
            headers: token ? {'Authorization': 'Bearer ' + token} : {},
            body: fd,
        });
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            throw new Error(err.detail || 'HTTP ' + res.status);
        }
        const model = await res.json();
        document.getElementById('add-model-modal').remove();
        // Optimistic update
        modelsList = [...modelsList, model];
        renderModelsList();
        populateSelectors();
        showToast({severity: 'info', title: 'Model Added', message: `Model ${modelId} registered.`});
    } catch (e) {
        showToast({severity: 'warning', title: 'Upload Failed', message: e.message});
        if (btn) { btn.disabled = false; btn.innerText = 'Upload & Register'; }
    }
}

function renameModel(modelId) {
    const m = modelsList.find(x => x.model_id === modelId);
    if (!m) return;

    const existing = document.getElementById('rename-model-modal');
    if (existing) existing.remove();

    const modal = document.createElement('div');
    modal.id = 'rename-model-modal';
    modal.className = 'fixed inset-0 z-50 flex items-center justify-center bg-black/70';
    modal.innerHTML = `
        <div class="bg-slate-900 border border-slate-700 rounded-xl p-6 w-96">
            <h3 class="text-lg font-bold text-white mb-4">Rename Model</h3>
            <div class="space-y-3">
                <div>
                    <label class="text-xs font-semibold text-slate-400 mb-1 block">Current ID</label>
                    <p class="text-sm text-white font-mono bg-slate-800 px-3 py-2 rounded border border-slate-700">${modelId}</p>
                </div>
                <div>
                    <label class="text-xs font-semibold text-slate-400 mb-1 block">New Model ID</label>
                    <input id="rename-model-id" value="${modelId}" class="w-full bg-slate-800 border border-slate-700 rounded px-3 py-2 text-white text-sm focus:outline-none focus:border-indigo-500">
                </div>
                <div>
                    <label class="text-xs font-semibold text-slate-400 mb-1 block">Description</label>
                    <textarea id="rename-model-desc" rows="2" class="w-full bg-slate-800 border border-slate-700 rounded px-3 py-2 text-white text-sm resize-none focus:outline-none focus:border-indigo-500">${m.description || ''}</textarea>
                </div>
            </div>
            <div class="flex gap-2 mt-5">
                <button onclick="submitRenameModel('${modelId}')" class="flex-1 py-2 rounded-lg text-sm font-semibold bg-amber-600 hover:bg-amber-500 text-white transition-all">Save</button>
                <button onclick="document.getElementById('rename-model-modal').remove()" class="flex-1 py-2 rounded-lg text-sm font-semibold bg-slate-700 hover:bg-slate-600 text-white transition-all">Cancel</button>
            </div>
        </div>
    `;
    document.body.appendChild(modal);
    document.getElementById('rename-model-id').focus();
    if (typeof lucide !== 'undefined') lucide.createIcons();
}

async function submitRenameModel(oldId) {
    const newId = document.getElementById('rename-model-id').value.trim();
    const desc = document.getElementById('rename-model-desc').value.trim();

    if (!newId) {
        showToast({severity: 'warning', title: 'Missing Fields', message: 'Model ID is required.'});
        return;
    }

    const body = {description: desc};
    if (newId !== oldId) body.model_id = newId;

    const res = await apiRequest(`/api/models/${oldId}`, {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body),
    });
    if (!res) {
        showToast({severity: 'warning', title: 'Rename Failed', message: 'Could not update model. Check server logs.'});
        return;
    }

    document.getElementById('rename-model-modal').remove();
    // Optimistic update
    const idx = modelsList.findIndex(x => x.model_id === oldId);
    if (idx !== -1) {
        modelsList[idx] = res;
        // If renamed, also fix any other ref that might point here
        if (newId !== oldId) {
            const entry = modelsList.splice(idx, 1)[0];
            modelsList.push(entry);
            modelsList.sort((a, b) => a.model_id.localeCompare(b.model_id));
        }
    }
    renderModelsList();
    populateSelectors();
    showToast({severity: 'info', title: 'Model Updated', message: `Model ${oldId} updated.`});
}

async function deleteModel(modelId) {
    if (!confirm(`Delete model "${modelId}"?\nThis removes the registration. The weights file stays on disk.`)) return;
    const res = await apiRequest(`/api/models/${modelId}`, { method: 'DELETE' });
    if (!res) {
        showToast({severity: 'warning', title: 'Delete Failed', message: `Failed to delete model ${modelId}.`});
        return;
    }
    // Optimistic update
    modelsList = modelsList.filter(m => m.model_id !== modelId);
    renderModelsList();
    populateSelectors();
    showToast({severity: 'info', title: 'Model Deleted', message: `Model ${modelId} deleted.`});
}
async function loadReportsData() {
    const camera_id = document.getElementById('reports-cam-select')?.value;
    if (!camera_id) {
        const tbody = document.getElementById('reports-table-body');
        if (tbody) tbody.innerHTML = '<tr><td colspan="10" class="px-4 py-8 text-center text-slate-600 text-sm">Select a camera to view report</td></tr>';
        return;
    }
    const fromEl = document.getElementById('reports-filter-from')?.value;
    const toEl = document.getElementById('reports-filter-to')?.value;
    const params = [];
    if (fromEl) params.push('since=' + encodeURIComponent(new Date(fromEl + 'T00:00:00').toISOString()));
    if (toEl) params.push('until=' + encodeURIComponent(new Date(toEl + 'T23:59:59').toISOString()));
    const qs = params.length ? '?' + params.join('&') : '';

    const data = await apiRequest(`/api/reports/${camera_id}/lanes${qs}`);
    if (!data) {
        const tbody = document.getElementById('reports-table-body');
        if (tbody) tbody.innerHTML = '<tr><td colspan="10" class="px-4 py-8 text-center text-slate-600 text-sm">No data available</td></tr>';
        return;
    }

    const lanes = data.lanes || [];
    const total = data.total || 0;

    const typeTotals = { car: 0, motorcycle: 0, truck: 0, bus: 0 };
    lanes.forEach(l => {
        Object.entries(l.types || {}).forEach(([t, c]) => {
            typeTotals[t] = (typeTotals[t] || 0) + c;
        });
    });
    const set = (id, val) => { const el = document.getElementById(id); if (el) el.innerText = val; };
    set('report-total-vehicles', total.toLocaleString());
    set('report-total-cars', (typeTotals.car || 0).toLocaleString());
    set('report-total-motos', (typeTotals.motorcycle || 0).toLocaleString());
    set('report-total-trucks', (typeTotals.truck || 0).toLocaleString());
    set('report-total-buses', (typeTotals.bus || 0).toLocaleString());
    set('reports-lane-count', lanes.length + ' lane(s)');

    const tbody = document.getElementById('reports-table-body');
    if (tbody) {
        tbody.innerHTML = lanes.map(l => {
            const types = l.types || {};
            const dirs = l.direction || {};
            return `<tr class="border-b border-slate-800/50 hover:bg-slate-900/20">
                <td class="px-4 py-3 font-mono text-xs text-indigo-400">${l.lane_id}</td>
                <td class="px-4 py-3 text-white">${l.name}</td>
                <td class="px-4 py-3">${types.car || 0}</td>
                <td class="px-4 py-3">${types.motorcycle || 0}</td>
                <td class="px-4 py-3">${types.truck || 0}</td>
                <td class="px-4 py-3">${types.bus || 0}</td>
                <td class="px-4 py-3 font-bold text-white">${l.total}</td>
                <td class="px-4 py-3">${dirs.forward || 0}</td>
                <td class="px-4 py-3">${dirs.backward || 0}</td>
                <td class="px-4 py-3">${l.occupancy || 0}</td>
            </tr>`;
        }).join('');
    }

    renderReportsCharts(lanes);
}

function renderReportsCharts(lanes) {
    chartVolumes = destroyChart(chartVolumes);
    chartCountingLanes = destroyChart(chartCountingLanes);
    chartAverages = destroyChart(chartAverages);

    if (!lanes || !lanes.length) return;

    // Volume by lane
    const volumeHeight = Math.max(280, 240 + Math.max(0, lanes.length - 4) * 36);
    const volEl = document.querySelector('#chart-reports-volumes');
    if (volEl) volEl.style.height = volumeHeight + 'px';

    const cats = lanes.map(l => l.name || l.lane_id);
    const vals = lanes.map(l => l.total || 0);
    const volOpts = {
        series: [{ name: 'Vehicles', data: vals }],
        chart: { type: 'bar', height: volumeHeight, background: 'transparent', toolbar: { show: false } },
        theme: { mode: 'dark' },
        colors: ['#6366f1'],
        xaxis: {
            categories: cats,
            labels: { style: { fontSize: '11px' }, rotate: lanes.length > 6 ? -45 : 0, hideOverlappingLabels: true, maxHeight: 80 },
        },
        yaxis: { min: 0, labels: { style: { fontSize: '10px' } } },
        grid: { borderColor: '#1e293b', strokeDashArray: 3 },
        tooltip: { theme: 'dark' },
        dataLabels: { enabled: false },
        plotOptions: { bar: { borderRadius: 4, columnWidth: Math.max(30, 80 - lanes.length * 4) + '%' } }
    };
    chartVolumes = new ApexCharts(volEl, volOpts);
    chartVolumes.render();

    // Direction donut
    const totalForward = lanes.reduce((s, l) => s + ((l.direction && l.direction.forward) || 0), 0);
    const totalBackward = lanes.reduce((s, l) => s + ((l.direction && l.direction.backward) || 0), 0);
    if (totalForward || totalBackward) {
        const dirEl = document.querySelector('#chart-reports-direction');
        const dirOpts = {
            series: [totalForward, totalBackward],
            chart: { type: 'donut', height: 210, background: 'transparent' },
            labels: ['Forward', 'Backward'],
            colors: ['#10b981', '#f59e0b'],
            legend: { show: true, position: 'bottom', labels: { colors: '#94a3b8' } },
            theme: { mode: 'dark' },
            tooltip: { theme: 'dark' },
            plotOptions: { pie: { donut: { size: '65%' } } },
            dataLabels: { enabled: false }
        };
        chartCountingLanes = new ApexCharts(dirEl, dirOpts);
        chartCountingLanes.render();
    }

    // Occupancy
    const occLanes = lanes.filter(l => l.occupancy > 0);
    if (occLanes.length) {
        const occHeight = Math.max(210, 180 + Math.max(0, occLanes.length - 4) * 30);
        const occEl = document.querySelector('#chart-reports-occupancy');
        if (occEl) occEl.style.height = occHeight + 'px';

        const occOpts = {
            series: [{ name: 'Occupancy', data: occLanes.map(l => l.occupancy) }],
            chart: { type: 'bar', height: occHeight, background: 'transparent', toolbar: { show: false } },
            theme: { mode: 'dark' },
            colors: ['#38bdf8'],
            xaxis: {
                categories: occLanes.map(l => l.name || l.lane_id),
                labels: { style: { fontSize: '10px' }, rotate: occLanes.length > 6 ? -45 : 0, hideOverlappingLabels: true, maxHeight: 80 },
            },
            yaxis: { min: 0, labels: { style: { fontSize: '10px' } } },
            grid: { borderColor: '#1e293b', strokeDashArray: 3 },
            tooltip: { theme: 'dark' },
            dataLabels: { enabled: false },
            plotOptions: { bar: { borderRadius: 4, columnWidth: Math.max(30, 80 - occLanes.length * 4) + '%' } }
        };
        chartAverages = new ApexCharts(occEl, occOpts);
        chartAverages.render();
    }
}

function exportReportsCSV() {
    const camera_id = document.getElementById('reports-cam-select')?.value;
    if (!camera_id) { alert('Select a camera first'); return; }
    const fromEl = document.getElementById('reports-filter-from')?.value;
    const toEl = document.getElementById('reports-filter-to')?.value;
    const params = [];
    if (fromEl) params.push('since=' + encodeURIComponent(new Date(fromEl + 'T00:00:00').toISOString()));
    if (toEl) params.push('until=' + encodeURIComponent(new Date(toEl + 'T23:59:59').toISOString()));
    const qs = params.length ? '?' + params.join('&') : '';
    const a = document.createElement('a');
    a.href = BASE_URL + `/api/reports/${camera_id}/lanes/csv${qs}`;
    a.download = `${camera_id}_lane_report.csv`;
    a.click();
}
// ── Settings state ──────────────────────────────────────────────────────
let _settingsDirty = false;
let _currentTab = 'general';

// ── Tab switching ──────────────────────────────────────────────────────
function switchSettingsTab(tabId) {
    _currentTab = tabId;
    document.querySelectorAll('.settings-panel').forEach(el => el.classList.add('hidden'));
    document.querySelectorAll('.settings-tab').forEach(el => {
        el.classList.remove('settings-active', 'bg-indigo-500/10', 'text-indigo-400', 'border-indigo-500/30');
        el.classList.add('text-slate-400', 'hover:text-slate-200', 'hover:bg-slate-800/30');
    });

    const panel = document.getElementById('settings-panel-' + tabId);
    if (panel) panel.classList.remove('hidden');

    const tabBtn = document.getElementById('settings-tab-' + tabId);
    if (tabBtn) {
        tabBtn.classList.add('settings-active', 'bg-indigo-500/10', 'text-indigo-400', 'border-indigo-500/30');
        tabBtn.classList.remove('text-slate-400', 'hover:text-slate-200', 'hover:bg-slate-800/30');
    }
}

// ── Load ───────────────────────────────────────────────────────────────
async function loadSettings() {
    const data = await apiRequest('/api/settings');
    if (!data) {
        updateStatus('Failed to load settings from server. Using defaults.', 'warning');
        return;
    }
    applySettings(data);
    updateSavedTimestamp();
    document.getElementById('settings-status').textContent = 'Settings loaded from server';
}

function applySettings(s) {
    const setVal = (id, val) => {
        const el = document.getElementById(id);
        if (el && val !== undefined && val !== null) {
            if (el.type === 'checkbox') el.checked = !!val;
            else el.value = String(val);
        }
    };
    const setRangeDisplay = (rangeId, displayId) => {
        const el = document.getElementById(rangeId);
        const display = document.getElementById(displayId);
        if (el && display) display.textContent = parseFloat(el.value).toFixed(2);
    };

    // General
    setVal('s-api-url', s.api_url || BASE_URL);
    if (s.appearance) {
        setVal('s-refresh-interval', s.appearance.refresh_interval_s || 30);
        setVal('s-timezone', s.appearance.timezone || 'UTC');
        setVal('s-chart-animations', s.appearance.chart_animations);
    }

    // Detection
    if (s.detection) {
        setVal('s-conf', s.detection.confidence || 0.35);
        setRangeDisplay('s-conf', 's-conf-val');
        setVal('s-iou', s.detection.iou || 0.5);
        setRangeDisplay('s-iou', 's-iou-val');
        setVal('s-imgsz', s.detection.imgsz || 640);
        setVal('s-detect-every', s.detection.detect_every_n_frames || 2);
        setVal('s-tracker', s.detection.tracker || 'bytetrack');
        setVal('s-track-buffer', s.detection.track_buffer || 30);
        setVal('s-max-det', s.detection.max_detections || 300);
        setVal('s-half', s.detection.half !== false);
        setVal('s-roi-crop', s.detection.roi_crop !== false);
    }

    // Storage
    if (s.storage) {
        setVal('s-output-dir', s.storage.output_dir || './output');
        setVal('s-retention', s.storage.data_retention_days || 7);
        setVal('s-crop-format', s.storage.crop_format || 'jpg');
        setVal('s-crop-quality', s.storage.crop_quality || 80);
        document.getElementById('s-crop-quality-val').textContent = s.storage.crop_quality || 80;
        setVal('s-crop-max-px', s.storage.crop_max_px || 320);
    }

    // System
    if (s.system) {
        setVal('s-max-workers', s.system.max_workers || 4);
        setVal('s-max-streams', s.system.max_streams || 16);
        setVal('s-log-level', s.system.log_level || 'INFO');
        setVal('s-memory-threshold', s.system.memory_threshold_mb || 0);
        setVal('s-db-pool', s.system.db_pool_size || 10);
        setVal('s-db-overflow', s.system.db_pool_overflow || 5);
    }

    // Notifications
    if (s.notifications) {
        setVal('s-bp-warn', s.notifications.backpressure_warn_threshold || 512);
        setVal('s-bp-crit', s.notifications.backpressure_crit_threshold || 1024);
        setVal('s-dl-max', s.notifications.dead_letter_max || 10000);
        setVal('s-hb-interval', s.notifications.heartbeat_interval_s || 30);
        setVal('s-hb-timeout', s.notifications.heartbeat_timeout_s || 90);
    }

    _settingsDirty = false;
}

// ── Save ───────────────────────────────────────────────────────────────
async function saveSettings() {
    const getVal = (id) => { const el = document.getElementById(id); return el ? el.value : null; };
    const getNum = (id) => { const v = getVal(id); const n = parseFloat(v); return isNaN(n) ? null : n; };
    const getBool = (id) => { const el = document.getElementById(id); return el ? el.checked : null; };

    const payload = {
        appearance: {
            refresh_interval_s: getNum('s-refresh-interval') || 30,
            timezone: getVal('s-timezone') || 'UTC',
            chart_animations: getBool('s-chart-animations'),
        },
        detection: {
            confidence: getNum('s-conf') || 0.35,
            iou: getNum('s-iou') || 0.5,
            imgsz: getNum('s-imgsz') || 640,
            half: getBool('s-half'),
            detect_every_n_frames: getNum('s-detect-every') || 2,
            tracker: getVal('s-tracker') || 'bytetrack',
            track_buffer: getNum('s-track-buffer') || 30,
            max_detections: getNum('s-max-det') || 300,
            roi_crop: getBool('s-roi-crop'),
        },
        storage: {
            output_dir: getVal('s-output-dir') || './output',
            data_retention_days: getNum('s-retention') || 7,
            crop_format: getVal('s-crop-format') || 'jpg',
            crop_quality: getNum('s-crop-quality') || 80,
            crop_max_px: getNum('s-crop-max-px') || 320,
        },
        system: {
            max_workers: getNum('s-max-workers') || 4,
            max_streams: getNum('s-max-streams') || 16,
            log_level: getVal('s-log-level') || 'INFO',
            memory_threshold_mb: getNum('s-memory-threshold') || 0,
            db_pool_size: getNum('s-db-pool') || 10,
            db_pool_overflow: getNum('s-db-overflow') || 5,
        },
        notifications: {
            backpressure_warn_threshold: getNum('s-bp-warn') || 512,
            backpressure_crit_threshold: getNum('s-bp-crit') || 1024,
            dead_letter_max: getNum('s-dl-max') || 10000,
            heartbeat_interval_s: getNum('s-hb-interval') || 30,
            heartbeat_timeout_s: getNum('s-hb-timeout') || 90,
        },
    };

    // Also persist API URL locally immediately
    const apiUrl = getVal('s-api-url');
    if (apiUrl) {
        localStorage.setItem('api_url', apiUrl);
        BASE_URL = apiUrl;
    }

    const res = await apiRequest('/api/settings', {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload),
    });

    if (res) {
        updateStatus('All settings saved successfully.', 'success');
        updateSavedTimestamp();
        _settingsDirty = false;
        showToast({severity: 'success', title: 'Settings Saved', message: 'All parameters persisted to configs/settings.json'});

        // Apply refresh_interval to dashboard auto-refresh if this tab is active
        const interval = payload.appearance.refresh_interval_s || 30;
        if (typeof window._dashboardRefreshTimer !== 'undefined') {
            clearInterval(window._dashboardRefreshTimer);
        }
        if (activeTab === 'dashboard' || activeTab === 'live') {
            window._dashboardRefreshTimer = setInterval(() => {
                if (activeTab === 'dashboard') renderDashboardCharts();
                else if (activeTab === 'live') loadLiveCameraData();
            }, interval * 1000);
        }
    } else {
        updateStatus('Server save failed. Settings saved locally only.', 'warning');
        showToast({severity: 'warning', title: 'Saved Locally', message: 'Settings saved to localStorage. Server unreachable.'});
    }
}

async function resetSettings() {
    if (!confirm('Reset all settings to factory defaults? This cannot be undone.')) return;
    const res = await apiRequest('/api/settings', {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
            detection: {}, storage: {}, notifications: {}, system: {}, appearance: {}
        }),
    });
    // Server ignores empty dicts per our merge strategy, so we send explicit defaults
    const defaultsPayload = {
        detection: { confidence: 0.35, iou: 0.5, imgsz: 640, half: true, detect_every_n_frames: 2, tracker: 'bytetrack', track_buffer: 30, max_detections: 300, roi_crop: true },
        storage: { output_dir: './output', data_retention_days: 7, crop_format: 'jpg', crop_quality: 80, crop_max_px: 320 },
        notifications: { backpressure_warn_threshold: 512, backpressure_crit_threshold: 1024, dead_letter_max: 10000, heartbeat_interval_s: 30, heartbeat_timeout_s: 90 },
        system: { max_workers: 4, max_streams: 16, log_level: 'INFO', memory_threshold_mb: 0, db_pool_size: 10, db_pool_overflow: 5 },
        appearance: { refresh_interval_s: 30, timezone: 'UTC', chart_animations: true },
    };
    const res2 = await apiRequest('/api/settings', {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(defaultsPayload),
    });
    if (res2) {
        applySettings(res2.settings || defaultsPayload);
        updateStatus('Settings reset to factory defaults.', 'success');
        showToast({severity: 'info', title: 'Settings Reset', message: 'All settings restored to defaults.'});
    } else {
        updateStatus('Reset failed.', 'error');
    }
}

// ── Status helpers ─────────────────────────────────────────────────────
function updateStatus(msg, severity) {
    const el = document.getElementById('settings-status');
    if (!el) return;
    el.textContent = msg;
    const colors = { success: 'text-emerald-400', warning: 'text-amber-400', error: 'text-rose-400', info: 'text-slate-500' };
    el.className = 'text-xs ' + (colors[severity] || 'text-slate-500');
}

function updateSavedTimestamp() {
    const el = document.getElementById('settings-last-saved');
    if (el) el.textContent = 'Last loaded: ' + new Date().toLocaleTimeString();
}
let _currentUserRole = null;

async function loadCurrentUserRole() {
    const me = await apiRequest('/api/users/me');
    if (me) _currentUserRole = me.role;
}

async function loadUsersData() {
    const tbody = document.getElementById('users-table-body');
    if (!tbody) return;
    const users = await apiRequest('/api/users');
    if (!users || !users.length) {
        tbody.innerHTML = '<tr><td colspan="6" class="px-4 py-8 text-center text-slate-600 text-sm">No users found</td></tr>';
        return;
    }
    const isAdmin = _currentUserRole === 'admin';
    tbody.innerHTML = users.map(u => {
        const roleColors = {
            admin: 'bg-rose-500/10 text-rose-400 border-rose-500/20',
            operator: 'bg-indigo-500/10 text-indigo-400 border-indigo-500/20',
            viewer: 'bg-slate-700 text-slate-300 border-slate-600',
        };
        const roleCls = roleColors[u.role] || roleColors.viewer;
        const actions = isAdmin
            ? `<button onclick="toggleUser('${u.id}')" class="text-xs px-2 py-1 rounded border ${u.is_active ? 'border-amber-500/30 text-amber-400 hover:bg-amber-500/10' : 'border-emerald-500/30 text-emerald-400 hover:bg-emerald-500/10'} transition-all">
                   ${u.is_active ? 'Deactivate' : 'Activate'}
               </button>`
            : '<span class="text-xs text-slate-600">—</span>';
        return `<tr class="border-b border-slate-800/50">
            <td class="px-4 py-3 font-medium text-white">${u.username}</td>
            <td class="px-4 py-3 text-slate-300">${u.email || '—'}</td>
            <td class="px-4 py-3"><span class="px-2 py-0.5 rounded text-[10px] font-bold border ${roleCls}">${u.role.toUpperCase()}</span></td>
            <td class="px-4 py-3">
                <span class="${u.is_active ? 'text-emerald-400' : 'text-rose-400'} font-semibold">${u.is_active ? 'Active' : 'Inactive'}</span>
            </td>
            <td class="px-4 py-3 text-slate-500 text-xs">${u.last_login ? new Date(u.last_login).toLocaleString() : 'Never'}</td>
            <td class="px-4 py-3">${actions}</td>
        </tr>`;
    }).join('');
    // Show/hide Add User button based on role
    const addBtn = document.getElementById('btn-add-user');
    if (addBtn) addBtn.style.display = isAdmin ? 'inline-flex' : 'none';
}

async function loadAuditData() {
    const tbody = document.getElementById('audit-table-body');
    if (!tbody) return;
    const logs = await apiRequest('/api/audit?limit=100');
    if (!logs || !logs.length) {
        tbody.innerHTML = '<tr><td colspan="5" class="px-4 py-8 text-center text-slate-600 text-sm">No audit entries</td></tr>';
        return;
    }
    tbody.innerHTML = logs.map(l => `
        <tr class="border-b border-slate-800/50">
            <td class="px-4 py-3 text-slate-500 text-xs">${l.timestamp ? new Date(l.timestamp).toLocaleString() : '—'}</td>
            <td class="px-4 py-3 text-white font-medium">${l.username}</td>
            <td class="px-4 py-3"><code class="text-xs bg-slate-800 px-1.5 py-0.5 rounded text-indigo-300">${l.action}</code></td>
            <td class="px-4 py-3 text-slate-300 text-xs">${l.resource || '—'}</td>
            <td class="px-4 py-3 text-slate-400 text-xs">${l.detail || '—'}</td>
        </tr>
    `).join('');
}

async function toggleUser(userId) {
    // Fetch current state first
    const users = await apiRequest('/api/users');
    if (!users) return;
    const u = users.find(x => x.id === userId);
    if (!u) return;
    const newState = !u.is_active;
    const res = await apiRequest(`/api/users/${userId}`, {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({is_active: newState})
    });
    if (res) {
        showToast({severity: 'info', title: 'User Updated', message: `User ${u.username} ${newState ? 'activated' : 'deactivated'}.`});
        await loadUsersData();
    } else {
        showToast({severity: 'warning', title: 'Failed', message: 'Could not update user. Admin role required.'});
    }
}

function showAddUserForm() {
    if (_currentUserRole !== 'admin') {
        showToast({severity: 'warning', title: 'Access Denied', message: 'Admin role required to create users.'});
        return;
    }
    const existing = document.getElementById('add-user-modal');
    if (existing) existing.remove();

    const modal = document.createElement('div');
    modal.id = 'add-user-modal';
    modal.className = 'fixed inset-0 z-50 flex items-center justify-center bg-black/70';
    modal.innerHTML = `
        <div class="bg-slate-900 border border-slate-700 rounded-xl p-6 w-96">
            <h3 class="text-lg font-bold text-white mb-4">Add User</h3>
            <div class="space-y-3">
                <div>
                    <label class="text-xs font-semibold text-slate-400 mb-1 block">Username</label>
                    <input id="new-user-name" class="w-full bg-slate-800 border border-slate-700 rounded px-3 py-2 text-white text-sm">
                </div>
                <div>
                    <label class="text-xs font-semibold text-slate-400 mb-1 block">Email</label>
                    <input id="new-user-email" type="email" class="w-full bg-slate-800 border border-slate-700 rounded px-3 py-2 text-white text-sm">
                </div>
                <div>
                    <label class="text-xs font-semibold text-slate-400 mb-1 block">Password</label>
                    <input id="new-user-pass" type="password" class="w-full bg-slate-800 border border-slate-700 rounded px-3 py-2 text-white text-sm">
                </div>
                <div>
                    <label class="text-xs font-semibold text-slate-400 mb-1 block">Role</label>
                    <select id="new-user-role" class="w-full bg-slate-800 border border-slate-700 rounded px-3 py-2 text-white text-sm">
                        <option value="viewer">Viewer</option>
                        <option value="operator">Operator</option>
                        <option value="admin">Admin</option>
                    </select>
                </div>
            </div>
            <div class="flex gap-2 mt-5">
                <button onclick="submitNewUser()" class="flex-1 py-2 rounded-lg text-sm font-semibold bg-indigo-600 hover:bg-indigo-500 text-white transition-all">Create User</button>
                <button onclick="document.getElementById('add-user-modal').remove()" class="flex-1 py-2 rounded-lg text-sm font-semibold bg-slate-700 hover:bg-slate-600 text-white transition-all">Cancel</button>
            </div>
        </div>
    `;
    document.body.appendChild(modal);
}

async function submitNewUser() {
    const body = {
        username: document.getElementById('new-user-name')?.value,
        email: document.getElementById('new-user-email')?.value || '',
        password: document.getElementById('new-user-pass')?.value,
        role: document.getElementById('new-user-role')?.value || 'viewer',
    };
    if (!body.username || !body.password) {
        showToast({severity: 'warning', title: 'Missing Fields', message: 'Username and password required.'});
        return;
    }
    const res = await apiRequest('/api/users', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body)
    });
    if (res) {
        document.getElementById('add-user-modal').remove();
        showToast({severity: 'info', title: 'User Created', message: `User ${body.username} created.`});
        await loadUsersData();
    } else {
        showToast({severity: 'warning', title: 'Failed', message: 'Could not create user. Admin role required.'});
    }
}
