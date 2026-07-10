function renderModelsList() {
    const container = document.getElementById('models-list-container');
    if (!container) return;
    container.innerHTML = modelsList.map(m => `
        <div class="bg-slate-900/40 border border-slate-800 rounded-xl p-6">
            <div class="flex justify-between items-start mb-3">
                <div class="flex-1 min-w-0">
                    <div class="flex items-center gap-3 mb-1">
                        <h4 class="text-base font-bold text-white truncate">${escapeHtml(m.model_id)}</h4>
                        <span class="text-xs font-bold px-2.5 py-0.5 rounded-lg bg-indigo-500/10 text-indigo-400 border border-indigo-500/20 flex-shrink-0">${escapeHtml(m.class_mode)}</span>
                    </div>
                    <p class="text-xs text-slate-500 font-mono truncate">${escapeHtml(m.model_path)}</p>
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
            <p class="text-slate-400 text-sm mt-1 line-clamp-2">${escapeHtml(m.description || '—')}</p>
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
                    <textarea id="rename-model-desc" rows="2" class="w-full bg-slate-800 border border-slate-700 rounded px-3 py-2 text-white text-sm resize-none focus:outline-none focus:border-indigo-500">${escapeHtml(m.description || '')}</textarea>
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
