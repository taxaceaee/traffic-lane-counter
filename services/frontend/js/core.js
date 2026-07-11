function resolveBaseUrl() {
    const runtimeConfig = window.__TRAFFICFLOW_CONFIG__ || {};
    const configured = runtimeConfig.API_BASE_URL || localStorage.getItem('api_url');
    if (configured) return String(configured).replace(/\/$/, '');
    if (window.location.port === '8000') return window.location.origin;
    return 'http://localhost:8000';
}

// All server-provided strings rendered through template HTML must be escaped.
// Identifiers are validated by the API, but names, descriptions and messages
// are user/configuration controlled and must not become HTML or script.
function escapeHtml(value) {
    return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

let BASE_URL = resolveBaseUrl();
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
// Page-scoped chart handles (never share Apex instances across tabs).
let chartHourly = null, chartVehicleTypes = null, chartMetrics = null;
let chartReportsVolumes = null, chartReportsDirection = null, chartReportsOccupancy = null;
let chartReportsHeatmap = null;

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
    // Legacy bookmark: Analytics was merged into Reports.
    if (segment === 'analytics') return 'reports';
    for (const [tabId, url] of Object.entries(ROUTE_MAP)) {
        if (url === '/' + segment) return tabId;
    }
    return 'dashboard';
}

// ── Page loading ──────────────────────────────────────────────────────────

// In-memory HTML cache so tab switches stay instant even if the API is busy.
const _pageHtmlCache = Object.create(null);

async function loadPage(tabId) {
    const container = document.getElementById('page-content-area');
    if (!container) return;

    // Already in DOM — keep it (do not blank the UI while re-fetching).
    const existing = document.getElementById('page-content-' + tabId);
    if (existing) {
        _loadedPages.add(tabId);
        return;
    }

    const inject = (html) => {
        // Guard: never inject duplicate root nodes.
        if (document.getElementById('page-content-' + tabId)) return;
        container.insertAdjacentHTML('beforeend', html);
        _loadedPages.add(tabId);
        try { lucide.createIcons(); } catch (_) { /* ignore */ }
    };

    // Prefer memory cache (survives DOM wipe of other tabs, not this one).
    if (_pageHtmlCache[tabId]) {
        inject(_pageHtmlCache[tabId]);
        return;
    }

    try {
        const htmlResp = await fetch(`pages/${tabId}.html`, { cache: 'no-cache' });
        if (!htmlResp.ok) throw new Error('HTTP ' + htmlResp.status);
        const html = await htmlResp.text();
        if (!html || !html.includes('page-content-' + tabId)) {
            throw new Error('Invalid page markup for ' + tabId);
        }
        _pageHtmlCache[tabId] = html;
        inject(html);
    } catch (e) {
        console.warn(`Failed to load page "${tabId}":`, e);
        // Placeholder so the content area is never a black void.
        if (!document.getElementById('page-content-' + tabId)) {
            container.insertAdjacentHTML(
                'beforeend',
                `<div id="page-content-${tabId}" class="tab-content space-y-4">
                    <div class="rounded-xl border border-rose-500/30 bg-rose-500/5 p-6 text-center">
                        <p class="text-sm font-semibold text-rose-300">Failed to load this page</p>
                        <p class="text-xs text-slate-500 mt-1">${String(e && e.message ? e.message : e)}</p>
                        <button type="button" onclick="delete _pageHtmlCache['${tabId}']; _loadedPages.delete('${tabId}'); switchTab('${tabId}')"
                            class="mt-3 px-3 py-1.5 rounded-lg text-xs font-semibold bg-indigo-600 text-white">Retry</button>
                    </div>
                </div>`,
            );
            _loadedPages.add(tabId);
        }
    }
}

// ── Routing ───────────────────────────────────────────────────────────────

window.onload = async function() {
    BASE_URL = resolveBaseUrl();
    setupLogin();
    if (!localStorage.getItem('access_token')) {
        showLoginScreen();
        return;
    }
    const el = document.getElementById('settings-api-url');
    if (el) el.value = BASE_URL;
    lucide.createIcons();
    const tab = tabFromPath(window.location.pathname);
    await switchTab(tab);
};

function showLoginScreen() {
    const screen = document.getElementById('login-screen');
    if (screen) screen.classList.replace('hidden', 'flex');
}

function hideLoginScreen() {
    const screen = document.getElementById('login-screen');
    if (screen) screen.classList.replace('flex', 'hidden');
}

function setupLogin() {
    const form = document.getElementById('login-form');
    if (!form || form.dataset.bound) return;
    form.dataset.bound = 'true';
    form.addEventListener('submit', async (event) => {
        event.preventDefault();
        const error = document.getElementById('login-error');
        const button = document.getElementById('login-submit');
        if (error) error.classList.add('hidden');
        if (button) button.disabled = true;
        try {
            const response = await fetch(BASE_URL + '/api/auth/login', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    username: document.getElementById('login-username').value,
                    password: document.getElementById('login-password').value,
                }),
            });
            if (!response.ok) throw new Error('Invalid username or password');
            const data = await response.json();
            localStorage.setItem('access_token', data.access_token);
            localStorage.setItem('refresh_token', data.refresh_token);
            localStorage.setItem('user_role', data.user?.role || 'viewer');
            hideLoginScreen();
            await switchTab('dashboard');
        } catch (err) {
            if (error) {
                error.textContent = err.message || 'Sign in failed';
                error.classList.remove('hidden');
            }
        } finally {
            if (button) button.disabled = false;
        }
    });
}

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

    updatePageHeader(tabId);

    if (tabId === 'dashboard') {
        startDashboardPolling();
    } else {
        stopDashboardPolling();
    }

    if (tabId === 'alerts') {
        await refreshAlerts();
        startAlertPolling();
    } else {
        stopAlertPolling();
    }
    if (tabId === 'health') { startHealthPolling(); }
    else { stopHealthPolling(); }
    if (tabId === 'counting') {
        startCountingPolling();
    } else {
        stopCountingPolling();
    }
    // Live is expensive (MJPEG + YOLO encode slots) — tear down when leaving.
    if (tabId === 'live') {
        if (camerasList.length) {
            populateSelectors();
            if (typeof loadLiveCameraData === 'function') loadLiveCameraData();
        } else if (!isNewPage) {
            try { await refreshData(); } catch (_) { /* offline toast only */ }
        }
    } else if (typeof stopLivePage === 'function') {
        stopLivePage();
    }
    if (tabId === 'reports') { loadReportsData(); }
    if (tabId === 'events') { loadEventsData(); }
    if (tabId === 'settings') { loadSettings(); }
    if (tabId === 'users') { loadCurrentUserRole(); loadUsersData(); loadAuditData(); }
    if (tabId === 'lanes') {
        // Ensure cameras data is available even if page was cached
        if (!camerasList.length) await refreshData();
        loadLanesConfigEditor();
    }
    if (tabId === 'cameras') {
        if (!camerasList.length) await refreshData();
        if (typeof loadAlwaysOnStatus === 'function') await loadAlwaysOnStatus();
        if (typeof renderCamerasGrid === 'function') renderCamerasGrid();
    }
}

function updatePageHeader(tabId) {
    const headers = {
        dashboard: ["Dashboard", "Fleet ops — live cameras, lane occupancy, class mix, and infrastructure."],
        live:      ["Live Monitoring", "Real-time annotated video, lane occupancy, and AI inference metrics."],
        counting:  ["Vehicle Counting", "Realtime track+lane counts, direction/rate, and live session tracks per camera."],
        alerts:    ["Alert System", "Active incidents, remediation steps, and alert history."],
        cameras:   ["Camera Management", "Sources, always-on toggles, connectivity, Live / Lanes."],
        lanes:     ["Lane Configuration", "Edit lane polygon coordinates and counting lines per camera."],
        jobs:      ["Inference Jobs", "Launch batch jobs, inspect progress, and open annotated video."],
        models:    ["Model Management", "YOLO weight registry — upload, rename, and remove models."],
        events:    ["Events Log", "Lane-change and line-crossing event ledger with export."],
        reports:   ["Reports & Analytics", "Period lane reports, direction split, heatmap, and CSV export."],
        health:    ["System Health", "API, database, GPU, workers, and host resource telemetry."],
        users:     ["Users & Audit", "User management and administration audit trail."],
        settings:  ["Settings", "Runtime defaults: detection, storage, system, and notifications."]
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
        const headers = { ...(options.headers || {}) };
        if (token) headers['Authorization'] = 'Bearer ' + token;
        if (options.body && !headers['Content-Type'] && !headers['content-type']) {
            headers['Content-Type'] = 'application/json';
        }
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
                showLoginScreen();
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

async function downloadProtectedFile(path, filename) {
    const token = localStorage.getItem('access_token');
    if (!token) {
        showToast({severity: 'warning', title: 'Unauthorized', message: 'Please log in again.'});
        return false;
    }
    try {
        const res = await fetch(BASE_URL + path, {
            headers: { Authorization: 'Bearer ' + token }
        });
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const blob = await res.blob();
        const objectUrl = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = objectUrl;
        a.download = filename;
        a.click();
        setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
        return true;
    } catch (e) {
        showToast({severity: 'warning', title: 'Download Failed', message: 'Could not download report file.'});
        return false;
    }
}

// ── Shared helpers ────────────────────────────────────────────────────────

function destroyChart(ref) { if (ref) { try { ref.destroy(); } catch(e){} } return null; }

function clearContainer(id) {
    const el = document.querySelector(id);
    if (el) el.innerHTML = '';
}

function populateSelectors() {
    const camOpts = camerasList.map(c => `<option value="${escapeHtml(c.camera_id)}">${escapeHtml(c.camera_id)} — ${escapeHtml(c.name)}</option>`).join('');
    const modelOpts = modelsList.map(m => `<option value="${escapeHtml(m.model_id)}">${escapeHtml(m.model_id)}</option>`).join('');
    // Every camera dropdown on every page (except optional empty first option).
    const camSelectIds = [
        'overview-cam-select', 'live-cam-select', 'lanes-cam-select',
        'reports-cam-select', 'events-cam-select', 'count-filter-camera',
    ];
    camSelectIds.forEach((id) => {
        const el = document.getElementById(id);
        if (!el) return;
        const prev = el.value;
        if (id === 'count-filter-camera' || id === 'events-cam-select') {
            el.innerHTML = '<option value="">— Select camera —</option>' + camOpts;
        } else {
            el.innerHTML = camOpts;
        }
        if (prev && Array.from(el.options).some((o) => o.value === prev)) el.value = prev;
    });
    const liveSelect = document.getElementById('live-cam-select');
    if (liveSelect && camerasList.length) {
        const storedLiveCam = localStorage.getItem('live_camera_id');
        const livePreferred = camerasList.find(c => c.camera_id === _lastLiveCam)
            || camerasList.find(c => c.camera_id === storedLiveCam)
            || camerasList.find(c => ['rtsp', 'youtube_live'].includes(c.source_type))
            || camerasList[0];
        if (livePreferred) liveSelect.value = livePreferred.camera_id;
    }
    const om = document.getElementById('overview-model-select');
    if (om) om.innerHTML = modelOpts;
}

// ── Data refresh (called once per page-load / manual refresh) ─────────────

async function refreshData() {
    const now = new Date();
    document.getElementById('last-refresh-time').innerText = 'Last sync: ' + now.toLocaleTimeString();

    // Liveness only proves that the process exists. The dashboard connection
    // badge must reflect dependency readiness so DB outages are visible.
    const health = await apiRequest('/api/readyz');
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
    // Never wipe page-content-area — that left Live/Dashboard as a black void
    // when /api/readyz timed out under GPU load. Toast + badge only.
    if (typeof showToast === 'function') {
        showToast({
            severity: 'critical',
            title: 'Connection issue',
            message: `Unable to reach API at ${BASE_URL}. Retry or refresh.`,
        });
    }
    const connBadge = document.getElementById('conn-badge');
    if (connBadge) {
        connBadge.innerText = 'OFFLINE';
        connBadge.className = 'text-[9px] font-bold px-2 py-0.5 rounded bg-rose-500/10 text-rose-400 border border-rose-500/20';
    }
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
    localStorage.removeItem('user_role');
    showLoginScreen();
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
            localStorage.setItem('user_role', data.user?.role || 'viewer');
            return true;
        }
    } catch(e) {}
    return false;
}
