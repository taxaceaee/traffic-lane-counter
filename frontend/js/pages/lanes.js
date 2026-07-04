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
