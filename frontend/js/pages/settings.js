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
