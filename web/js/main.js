// Global variables to track state
let currentDocId = null;
let currentPairIndex = null;
let currentExample = null;
let currentDiscrepantKeys = new Set();
let currentSchema = null; // Schema received from backend: { fields: [{path, type}, ...] }
const CONTEXT_SIZE = 150;  // Number of characters to show before and after the event

// Track key states to prevent multiple triggers when holding
const keyStates = {
    'r': false,
    'm': false,
    'n': false,
    'enter': false,
    'backspace': false
};

// -----------------------------------------------------------------------
// Schema helpers
// -----------------------------------------------------------------------

/**
 * Resolve a '->'-separated path into a nested object.
 * Returns undefined if any key is missing.
 */
function getNestedValue(obj, path) {
    if (!obj || !path) return undefined;
    return path.split('->').reduce((cur, key) => (cur != null && typeof cur === 'object' ? cur[key] : undefined), obj);
}

/**
 * Set a value at a '->'-separated path, creating nested objects as needed.
 */
function setNestedValue(obj, path, value) {
    if (!obj || !path) return;
    const parts = path.split('->');
    const last = parts.pop();
    let current = obj;
    for (const part of parts) {
        if (current[part] === undefined || current[part] === null) {
            current[part] = {};
        }
        current = current[part];
    }
    current[last] = value;
}

/**
 * Delete a value at a '->'-separated path.
 */
function deleteNestedValue(obj, path) {
    if (!obj || !path) return;
    const parts = path.split('->');
    const last = parts.pop();
    let current = obj;
    for (const part of parts) {
        if (current[part] === undefined || current[part] === null) {
            return; // path doesn't exist, nothing to delete
        }
        current = current[part];
    }
    delete current[last];
}

/**
 * Return all field descriptors from the current schema.
 * Falls back to an empty array (auto-detect mode).
 */
function schemaFields() {
    return (currentSchema && currentSchema.fields) ? currentSchema.fields : [];
}

/**
 * Return only the span-type fields from the schema by analyzing real/model values.
 * No longer requires the schema to explicitly declare { type: 'span' }.
 */
function schemaSpanFields(realEvent, modelEvent) {
    const fields = schemaFields();
    return fields.filter(f => {
        const rVal = getNestedValue(realEvent, f.path);
        const mVal = getNestedValue(modelEvent, f.path);
        return isSpanValue(rVal) || isSpanValue(mVal);
    });
}

/**
 * Heuristic: does a value look like a span {text, begin/start, end}?
 */
function isSpanValue(val) {
    return val && typeof val === 'object' && 'text' in val &&
        (val.end != null || val.begin != null || val.start != null);
}

/**
 * Collect all comparable keys from an event object for checklist / diff purposes.
 * Respects schema when available; auto-detects otherwise.
 */
function getEventKeys(eventObj) {
    if (!eventObj) return [];
    const fields = schemaFields();
    if (fields.length > 0) {
        return fields.map(f => f.path);
    }
    return [];
}

/**
 * Generate a deterministic pastely HSL color from a string.
 */
function getColorForPath(path) {
    let hash = 0;
    for (let i = 0; i < path.length; i++) {
        hash = path.charCodeAt(i) + ((hash << 5) - hash);
    }
    const hue = Math.abs(hash % 360);
    // Use pastel colors: high lightness, distinct hues
    return `hsl(${hue}, 70%, 85%)`;
}

/**
 * Generate a slightly darker border color for checklist items.
 */
function getBorderColorForPath(path) {
    let hash = 0;
    for (let i = 0; i < path.length; i++) {
        hash = path.charCodeAt(i) + ((hash << 5) - hash);
    }
    const hue = Math.abs(hash % 360);
    return `hsl(${hue}, 60%, 50%)`;
}

// -----------------------------------------------------------------------
// Existing helpers — updated to be schema-aware
// -----------------------------------------------------------------------

function getEventBounds(eventBlock) {
    if (!eventBlock) return null;
    let starts = [];
    let ends = [];

    const spanPaths = [];
    const fields = schemaFields();
    if (fields.length > 0) {
        for (const fd of fields) {
            const v = getNestedValue(eventBlock, fd.path);
            if (isSpanValue(v)) spanPaths.push(fd.path);
        }
    }

    if (spanPaths.length > 0) {
        for (const path of spanPaths) {
            const v = getNestedValue(eventBlock, path);
            if (v && v.end != null) {
                const begin = v.begin !== undefined ? v.begin : v.start;
                if (begin != null) { starts.push(begin); ends.push(v.end); }
            }
        }
    }

    if (starts.length === 0) return { start: 0, end: 0 };
    return { start: Math.min(...starts), end: Math.max(...ends) };
}

// Initialize the application
async function initializeApp() {
    try {
        const example = await eel.get_initial_state()();

        if (example.error) {
            alert(example.error);
            return;
        }

        // Store schema before handling the first example
        if (example.schema) {
            currentSchema = example.schema;
        }

        // Display annotator badge if in per-annotator mode
        if (example.annotator_name) {
            const badge = document.getElementById('annotatorBadge');
            const nameSpan = document.getElementById('annotatorName');
            if (badge && nameSpan) {
                nameSpan.textContent = example.annotator_name;
                badge.style.display = 'block';
            }
        }

        handleExample(example);
    } catch (error) {
        console.error('Error initializing app:', error);
        document.body.innerHTML = `<div style="color:red; padding: 20px;"><h3>Failed to initialize the application</h3><pre>${error.stack}</pre></div>`;
        alert('Failed to initialize the application: ' + error.message);
    }
}

/**
 * Compare two events using the active schema.
 * Returns { diffs: string[], discrepantKeys: Set<string> }
 *
 * The 'key' used in discrepantKeys is always the top-level field path (first
 * segment before '->') so the checklists and highlights match the displayed JSON.
 */
function compareEvents(realEvent, modelEvent) {
    const diffs = [];
    const discrepantKeys = new Set();

    if (!realEvent && modelEvent) {
        getEventKeys(modelEvent).forEach(k => discrepantKeys.add(k));
        return { diffs: [`The ${getLabel('real')} event is missing completely. ${getLabel('model')} found this as a new event.`], discrepantKeys };
    }
    if (!modelEvent && realEvent) {
        getEventKeys(realEvent).forEach(k => discrepantKeys.add(k));
        return { diffs: [`The ${getLabel('model')} missed this event completely. ${getLabel('model')} did not extract anything here.`], discrepantKeys };
    }
    if (!realEvent && !modelEvent) {
        return { diffs: [], discrepantKeys };
    }

    const fields = schemaFields();

    if (fields.length > 0) {
        // Schema-driven comparison
        for (const fd of fields) {
            const path = fd.path;
            const rVal = getNestedValue(realEvent, path);
            const mVal = getNestedValue(modelEvent, path);

            if (rVal === undefined && mVal !== undefined) {
                diffs.push(`${getLabel('model')} added an extra property: <b>${path}</b>`);
                discrepantKeys.add(path);
            } else if (rVal !== undefined && mVal === undefined) {
                diffs.push(`${getLabel('model')} missed the property: <b>${path}</b>`);
                discrepantKeys.add(path);
            } else if (rVal === undefined && mVal === undefined) {
                // Neither side has it — skip
            } else if (isSpanValue(rVal) || isSpanValue(mVal)) {
                if (isSpanValue(rVal) && isSpanValue(mVal)) {
                    const rBegin = rVal.begin !== undefined ? rVal.begin : rVal.start;
                    const mBegin = mVal.begin !== undefined ? mVal.begin : mVal.start;
                    if (rVal.text !== mVal.text) {
                        diffs.push(`<b>${path}</b> text is different: ${getLabel('real')} says <code>"${rVal.text}"</code> but ${getLabel('model')} says <code>"${mVal.text}"</code>`);
                        discrepantKeys.add(path);
                    } else if (rBegin !== mBegin || rVal.end !== mVal.end) {
                        diffs.push(`<b>${path}</b> boundaries differ slightly: ${getLabel('real')} is [${rBegin}-${rVal.end}] but ${getLabel('model')} is [${mBegin}-${mVal.end}]`);
                        discrepantKeys.add(path);
                    }
                } else if (JSON.stringify(rVal) !== JSON.stringify(mVal)) {
                    diffs.push(`<b>${path}</b> is different`);
                    discrepantKeys.add(path);
                }
            } else {
                if (rVal !== mVal) {
                    diffs.push(`<b>${path}</b> is different: ${getLabel('real')} says <code>${rVal}</code> but ${getLabel('model')} says <code>${mVal}</code>`);
                    discrepantKeys.add(path);
                }
            }
        }
    }

    return { diffs, discrepantKeys };
}

const MOON_SVG = `<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="currentColor" style="display:block;"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>`;
const SUN_SVG  = `<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="currentColor" style="display:block;"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><line x1="12" y1="21" x2="12" y2="23" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><line x1="1" y1="12" x2="3" y2="12" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><line x1="21" y1="12" x2="23" y2="12" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>`;

// ── Label reveal (eye button) ────────────────────────────────────────────────

let labelsRevealed = false;
let sideSwapped = false;

const LABELS = {
    default: { real: 'Annotation 1', model: 'Annotation 2' },
    reveal:  { real: 'Original annotation', model: 'Model annotation' },
};

function getLabel(side) {
    if (labelsRevealed) {
        return side === 'real' ? LABELS.reveal.real : LABELS.reveal.model;
    }
    // Positional: left=Annotation 1, right=Annotation 2
    if (side === 'real') return sideSwapped ? 'Annotation 2' : 'Annotation 1';
    return sideSwapped ? 'Annotation 1' : 'Annotation 2';
}

function applyLabels() {
    const set = (id, text) => { const el = document.getElementById(id); if (el) el.textContent = text; };
    if (labelsRevealed) {
        const R = LABELS.reveal;
        set('realTagsLabel',   R.real);
        set('modelTagsLabel',  R.model);
        set('realEventLabel',  R.real + ' Event');
        set('modelEventLabel', R.model + ' Event');
        set('diffSummaryLabel', `Detected Differences (${R.real} vs ${R.model}):`);
        const realBtn = document.getElementById('acceptRealBtn');
        if (realBtn) realBtn.textContent = 'All ' + R.real;
        const modelBtn = document.getElementById('acceptModelBtn');
        if (modelBtn) modelBtn.textContent = 'All ' + R.model;
    } else {
        // Labels are positional — left always Annotation 1, right always Annotation 2
        const realLabel  = sideSwapped ? 'Annotation 2' : 'Annotation 1';
        const modelLabel = sideSwapped ? 'Annotation 1' : 'Annotation 2';
        set('realTagsLabel',  realLabel);
        set('modelTagsLabel', modelLabel);
        set('realEventLabel',  realLabel + ' Event');
        set('modelEventLabel', modelLabel + ' Event');
        set('diffSummaryLabel', 'Detected Differences (Annotation 1 vs Annotation 2):');
        const realBtn = document.getElementById('acceptRealBtn');
        if (realBtn) realBtn.textContent = 'All ' + realLabel;
        const modelBtn = document.getElementById('acceptModelBtn');
        if (modelBtn) modelBtn.textContent = 'All ' + modelLabel;
    }
}

function applySideOrder() {
    const realOrd = sideSwapped ? 1 : 0;
    const modelOrd = sideSwapped ? 0 : 1;

    const realTextCol  = document.getElementById('realTagsLabel')?.closest('.text-column');
    const modelTextCol = document.getElementById('modelTagsLabel')?.closest('.text-column');
    if (realTextCol)  realTextCol.style.order  = realOrd;
    if (modelTextCol) modelTextCol.style.order = modelOrd;

    const realCheckCol  = document.getElementById('acceptRealBtn')?.parentElement;
    const modelCheckCol = document.getElementById('acceptModelBtn')?.parentElement;
    const flagCheckCol  = document.getElementById('rejectBtn')?.parentElement;
    if (realCheckCol)  realCheckCol.style.order  = realOrd;
    if (modelCheckCol) modelCheckCol.style.order = modelOrd;
    if (flagCheckCol)  flagCheckCol.style.order  = 2;

    const realJsonCol  = document.getElementById('realEventDisplay')?.parentElement;
    const modelJsonCol = document.getElementById('modelEventDisplay')?.parentElement;
    if (realJsonCol)  realJsonCol.style.order  = realOrd;
    if (modelJsonCol) modelJsonCol.style.order = modelOrd;
}

function applyJsonBtnStyle(visible) {
    const btn = document.getElementById('toggleJsonBtn');
    if (!btn) return;
    const dark = document.body.classList.contains('dark-mode');
    btn.textContent = visible ? 'Hide JSON' : 'Show JSON';
    if (dark) {
        btn.style.backgroundColor = visible ? '#1c2128' : '#373e47';
        btn.style.color = '#8b949e';
    } else {
        btn.style.backgroundColor = visible ? '#adb5bd' : '#6c757d';
        btn.style.color = visible ? '#495057' : 'white';
    }
}

function toggleJsonDisplay() {
    const section = document.getElementById('jsonDisplaySection');
    if (!section) return;
    const visible = section.style.display === 'flex';
    section.style.display = visible ? 'none' : 'flex';
    applyJsonBtnStyle(!visible);
    localStorage.setItem('jsonVisible', visible ? '0' : '1');
}

function toggleLabels() {
    labelsRevealed = !labelsRevealed;
    const btn = document.getElementById('previewBtn');
    if (btn) btn.style.opacity = labelsRevealed ? '1' : '0.5';
    applyLabels();
}

function toggleDarkMode() {
    const isDark = document.body.classList.toggle('dark-mode');
    document.documentElement.classList.toggle('dark-mode', isDark);
    localStorage.setItem('darkMode', isDark ? '1' : '0');
    const btn = document.getElementById('darkModeBtn');
    if (btn) btn.innerHTML = isDark ? SUN_SVG : MOON_SVG;
    const jsonVisible = document.getElementById('jsonDisplaySection')?.style.display === 'flex';
    applyJsonBtnStyle(jsonVisible);
}

window.addEventListener('load', () => {
    if (localStorage.getItem('darkMode') === '1') {
        document.body.classList.add('dark-mode');
        document.documentElement.classList.add('dark-mode');
        const btn = document.getElementById('darkModeBtn');
        if (btn) btn.innerHTML = SUN_SVG;
    }
    const jsonVisible = localStorage.getItem('jsonVisible') === '1';
    if (jsonVisible) {
        const section = document.getElementById('jsonDisplaySection');
        if (section) section.style.display = 'flex';
    }
    applyJsonBtnStyle(jsonVisible);
    initializeApp();
});

async function handleUndo() {
    try {
        const example = await eel.undo_last_validation()();
        handleExample(example);
    } catch (error) {
        console.error('Error in undo:', error);
        alert('Failed to undo last action');
    }
}

function updateProgress(progress) {
    if (!progress) return;

    const { total_remaining, total_tags, completed } = progress;
    const percentage = total_tags > 0 ? (completed / total_tags) * 100 : 100;

    const progressFill = document.getElementById('progressFill');
    progressFill.style.width = `${percentage}%`;

    const progressText = document.getElementById('progressText');
    progressText.textContent = `Validated ${completed} of ${total_tags} events`;
}

function checkDiscrepanciesResolved() {
    if (!currentDiscrepantKeys || currentDiscrepantKeys.size === 0) {
        return true;
    }
    for (let key of currentDiscrepantKeys) {
        const realChecked  = document.querySelector(`#realChecklist  input[data-field="${key}"]`)?.checked;
        const modelChecked = document.querySelector(`#modelChecklist input[data-field="${key}"]`)?.checked;
        const flagChecked  = document.querySelector(`#flagChecklist  input[data-field="${key}"]`)?.checked;
        if (!realChecked && !modelChecked && !flagChecked) {
            return false;
        }
    }
    return true;
}

function updateSubmitButtonsState() {
    const isResolved = checkDiscrepanciesResolved();
    const buttonsToToggle = ['submitHybridBtn'];
    
    buttonsToToggle.forEach(id => {
        const btn = document.getElementById(id);
        if (btn) {
            btn.disabled = !isResolved;
            if (!isResolved) {
                btn.style.opacity = '0.5';
                btn.style.cursor = 'not-allowed';
            } else {
                btn.style.opacity = '1';
                btn.style.cursor = 'pointer';
            }
        }
    });
}

function checkAll(side) {
    if (side === 'real') {
        const cbs = document.querySelectorAll('#realChecklist input[type="checkbox"]:not(:disabled)');
        cbs.forEach(cb => {
            cb.checked = true;
            const opposite = document.querySelector(`#modelChecklist input[data-field="${cb.dataset.field}"]`);
            if (opposite && !opposite.disabled) opposite.checked = false;
        });
    } else if (side === 'model') {
        const cbs = document.querySelectorAll('#modelChecklist input[type="checkbox"]:not(:disabled)');
        cbs.forEach(cb => {
            cb.checked = true;
            const opposite = document.querySelector(`#realChecklist input[data-field="${cb.dataset.field}"]`);
            if (opposite && !opposite.disabled) opposite.checked = false;
        });
    }
    updateSubmitButtonsState();
}

function displayEventJson(eventObj, elementId) {
    const el = document.getElementById(elementId);
    if (!eventObj) {
        el.textContent = "No overlapping event found.";
        el.style.opacity = 0.5;
        return;
    }

    const cleanEv = { ...eventObj };
    delete cleanEv.event_id;

    function escapeHtml(text) {
        if (text === null || text === undefined) return '';
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    function formatValue(value, indentLevel = 0) {
        const indent = '  '.repeat(indentLevel);
        if (value && typeof value === 'object' && !Array.isArray(value)) {
            let innerIndent = '  '.repeat(indentLevel + 1);
            let result = '{\n';
            const entries = Object.entries(value);
            entries.forEach(([k, v], i) => {
                const comma = (i === entries.length - 1) ? '' : ',';
                const valStr = formatValue(v, indentLevel + 1);
                result += `${innerIndent}"${k}": ${valStr}${comma}\n`;
            });
            result += `${indent}}`;
            return result;
        } else if (typeof value === 'string') {
            return `"${escapeHtml(value)}"`;
        } else if (value === null) {
            return 'null';
        } else if (typeof value === 'number' || typeof value === 'boolean') {
            return String(value);
        } else {
            return '';
        }
    }

    let html = '{\n';
    
    // Create a map of active paths mapped to their colors
    const activePaths = new Map();
    schemaFields().forEach(f => {
        activePaths.set(f.path, getColorForPath(f.path));
    });

    // A helper to walk the JSON and highlight specifically at the leaf path
    function buildJsonHtml(obj, currentPath, indentLevel) {
        const indent = '  '.repeat(indentLevel);
        if (obj && typeof obj === 'object' && !Array.isArray(obj)) {
            let result = '{\n';
            const entries = Object.entries(obj).filter(([k, v]) => v !== undefined);
            
            entries.forEach(([k, v], i) => {
                const comma = (i === entries.length - 1) ? '' : ',';
                const nextPath = currentPath ? `${currentPath}->${k}` : k;
                
                // If this exact path is in activePaths, format the whole subtree in the span
                if (activePaths.has(nextPath)) {
                    const color = activePaths.get(nextPath);
                    const valStr = formatValue(v, indentLevel + 1);
                    const trimmedValStr = typeof v === 'object' && v !== null ? valStr.trimStart() : valStr;
                    result += `${indent}  <span class="highlight" style="background-color: ${color};">"${k}": ${trimmedValStr}</span>${comma}\n`;
                } else if (v && typeof v === 'object') {
                    // Not a leaf path, keep traversing
                    const subHtml = buildJsonHtml(v, nextPath, indentLevel + 1);
                    result += `${indent}  "${k}": ${subHtml.trimStart()}${comma}\n`;
                } else {
                    // Normal scalar, no highlight
                    const valStr = formatValue(v, indentLevel + 1);
                    result += `${indent}  "${k}": ${valStr}${comma}\n`;
                }
            });
            result += `${indent}}`;
            return result;
        } else {
            return formatValue(obj, indentLevel);
        }
    }

    html += String(buildJsonHtml(cleanEv, "", 0)).trimStart() + '\n}';
    
    // Clean up trailing commas before closing braces if any were missed by trim logic
    html = html.replace(/,\n}/g, '\n}');

    el.innerHTML = html;
    el.style.opacity = 1.0;
}

/**
 * Renders a checklist of fields for an event.
 * Uses schema field paths (top-level key) when available.
 */
function renderChecklist(eventObj, containerId, otherEventObj = null, allowedKeys = null, discrepantKeys = null) {
    const container = document.getElementById(containerId);
    if (!container) return;

    container.innerHTML = '';

    let keys = new Set();
    const fields = schemaFields();

    if (fields.length > 0) {
        // Use schema-defined display keys
        for (const fd of fields) {
            const displayKey = fd.path.split('->')[0];
            if (!allowedKeys || allowedKeys.has(displayKey)) {
                keys.add(displayKey);
            }
        }
    }
    
    if (keys.size === 0) return;

    // Keep schema order
    let sortedKeys;
    if (fields.length > 0) {
        const schemaOrder = fields.map(f => f.path);
        sortedKeys = Array.from(keys).sort((a, b) => {
            const ia = schemaOrder.indexOf(a);
            const ib = schemaOrder.indexOf(b);
            if (ia !== -1 && ib !== -1) return ia - ib;
            if (ia !== -1) return -1;
            if (ib !== -1) return 1;
            return a.localeCompare(b);
        });
    } else {
        sortedKeys = Array.from(keys);
    }

    const isFlagColumn = containerId === 'flagChecklist';

    sortedKeys.forEach(key => {
        const isDiscrepant = !discrepantKeys || discrepantKeys.size === 0 || discrepantKeys.has(key);
        const lockAsMatch = !isDiscrepant && !isFlagColumn;

        const item = document.createElement('label');
        item.className = `checklist-item`;
        item.style.borderLeftColor = getBorderColorForPath(key);

        if (lockAsMatch) {
            item.style.opacity = '0.55';
            item.style.cursor = 'default';
        } else {
            if (!isDiscrepant) {
                item.style.opacity = '0.7';
            }
            item.addEventListener('mouseenter', () => {
                item.style.backgroundColor = getColorForPath(key);
            });
            item.addEventListener('mouseleave', () => {
                item.style.backgroundColor = '';
            });
        }

        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.dataset.field = key;

        if (lockAsMatch) {
            checkbox.checked = true;
            checkbox.disabled = true;
        } else {
            checkbox.addEventListener('change', (e) => {
                if (e.target.checked) {
                    if (containerId === 'realChecklist') {
                        const oppositeCb = document.querySelector(`#modelChecklist input[data-field="${key}"]`);
                        if (oppositeCb && !oppositeCb.disabled) oppositeCb.checked = false;
                    } else if (containerId === 'modelChecklist') {
                        const oppositeCb = document.querySelector(`#realChecklist input[data-field="${key}"]`);
                        if (oppositeCb && !oppositeCb.disabled) oppositeCb.checked = false;
                    }
                }
                updateSubmitButtonsState();
            });
        }

        const labelText = document.createElement('span');
        const rVal = eventObj ? getNestedValue(eventObj, key) : undefined;
        if (rVal === undefined) {
            labelText.textContent = key + ' (Missing)';
            labelText.style.color = '#888';
        } else {
            labelText.textContent = key;
        }
        if (!isDiscrepant) {
            labelText.style.textDecoration = 'line-through';
        }

        item.appendChild(checkbox);
        item.appendChild(labelText);
        container.appendChild(item);
    });
}

function handleExample(example) {
    console.log('handleExample called with:', example);

    if (example.error) {
        console.error('Error in example:', example.error);
        alert(example.error);
        return;
    }

    resetReviewCommentText();

    if (example.completed) {
        console.log('Validation completed');
        updateProgress({
            total_remaining: 0,
            total_tags: example.progress?.total_tags || 0,
            completed: example.progress?.total_tags || 0
        });
        showCompletionMessage(example.output_file);
        return;
    }

    // Store current state
    currentDocId = example.doc_id;
    currentPairIndex = example.pair_index;
    currentExample = example;

    sideSwapped = Math.random() < 0.5;
    applySideOrder();
    applyLabels();

    // Update schema if provided
    if (example.schema !== undefined) {
        currentSchema = example.schema;
    }

    updateProgress(example.progress);

    document.getElementById('docId').textContent = example.doc_id;
    document.getElementById('remaining').textContent = `${example.total_remaining} events pairs`;

    // Compute explicit differences and display them
    const diffSummaryCont = document.getElementById('diffSummaryContainer');
    const diffList = document.getElementById('diffSummaryList');
    const comparisonResult = compareEvents(example.real_event, example.model_event);
    const diffs = comparisonResult.diffs;
    const discrepantKeys = comparisonResult.discrepantKeys;

    if (diffs.length > 0) {
        diffSummaryCont.style.display = 'block';
        diffList.innerHTML = diffs.map(d => `<li>${d}</li>`).join('');
    } else {
        diffSummaryCont.style.display = 'none';
        diffList.innerHTML = '';
    }

    // Display JSON block representations
    displayEventJson(example.real_event, 'realEventDisplay');
    displayEventJson(example.model_event, 'modelEventDisplay');

    // Populate checklists — always show all schema fields; non-discrepant fields are crossed out
    renderChecklist(example.real_event, 'realChecklist', null, null, discrepantKeys);
    renderChecklist(example.model_event, 'modelChecklist', null, null, discrepantKeys);
    renderChecklist(example.real_event, 'flagChecklist', example.model_event, null, discrepantKeys);

    currentDiscrepantKeys = discrepantKeys;
    updateSubmitButtonsState();

    // Display text with context and highlights
    displayTaggedText(example);

    document.getElementById('undoBtn').disabled = false;
}

function displayTaggedText(example) {
    const realBounds = getEventBounds(example.real_event);
    const modelBounds = getEventBounds(example.model_event);

    let minStart = Number.MAX_SAFE_INTEGER;
    let maxEnd = 0;

    if (realBounds) { minStart = Math.min(minStart, realBounds.start); maxEnd = Math.max(maxEnd, realBounds.end); }
    if (modelBounds) { minStart = Math.min(minStart, modelBounds.start); maxEnd = Math.max(maxEnd, modelBounds.end); }

    if (minStart > maxEnd) {
        minStart = 0;
        maxEnd = Math.min(200, example.text.length);
    }

    const contextStart = Math.max(0, minStart - CONTEXT_SIZE);
    const contextEnd = Math.min(example.text.length, maxEnd + CONTEXT_SIZE);
    const contextText = example.text.substring(contextStart, contextEnd);

    const realHighlighter = new TextHighlighter(contextText);
    const modelHighlighter = new TextHighlighter(contextText);

    /**
     * Add highlighting spans for all span-type fields in an event based on schema.
     */
    function addEventSpans(eventObj, highlighterInstance) {
        if (!eventObj) return;

        const fields = schemaFields();

        if (fields.length > 0) {
            for (const fd of fields) {
                const val = getNestedValue(eventObj, fd.path);
                if (!val || val.end == null || !isSpanValue(val)) continue;
                const begin = val.begin !== undefined ? val.begin : val.start;
                if (begin == null) continue;
                if (val.end > contextStart && begin < contextEnd) {
                    const preferredStart = Math.max(0, begin - contextStart);
                    const preferredEnd = val.end - contextStart;
                    const resolvedRange = resolveSpanRange(
                        contextText,
                        preferredStart,
                        preferredEnd,
                        val.text
                    );
                    if (!resolvedRange) continue;
                    const adjStart = resolvedRange.start;
                    const adjEnd = resolvedRange.end;
                    const pathColor = getColorForPath(fd.path);
                    highlighterInstance.addSpan(new TextSpan(
                        adjStart, adjEnd,
                        contextText.substring(adjStart, adjEnd),
                        `highlight`, // Removed hardcoded class
                        {
                            backgroundColor: pathColor,
                            originalStart: begin,
                            originalEnd: val.end,
                        }
                    ));
                }
            }
        }
    }

    addEventSpans(example.real_event, realHighlighter);
    addEventSpans(example.model_event, modelHighlighter);

    document.getElementById('realTagsDisplay').innerHTML =
        `<p class="context-text">${realHighlighter.highlightText()}</p>`;
    document.getElementById('modelTagsDisplay').innerHTML =
        `<p class="context-text">${modelHighlighter.highlightText()}</p>`;

    setEventTypeCaption('realEventTypeLine', example.real_event);
    setEventTypeCaption('modelEventTypeLine', example.model_event);
}

function setEventTypeCaption(elementId, eventObj) {
    const el = document.getElementById(elementId);
    if (!el) return;
    if (!eventObj) {
        el.innerHTML = '';
        return;
    }
    const val = getNestedValue(eventObj, 'eventType');
    if (val === undefined || val === null) {
        el.innerHTML = '';
        return;
    }
    const text = typeof val === 'object' ? (val.text || '') : String(val);
    if (!text) {
        el.innerHTML = '';
        return;
    }
    const escaped = text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    el.innerHTML = `Event type: <strong>${escaped}</strong>`;
}

function getReviewCommentText() {
    return document.getElementById('flagCommentText')?.value || '';
}

function resetReviewCommentText() {
    const textarea = document.getElementById('flagCommentText');
    if (textarea) {
        textarea.value = '';
    }
}

function isTypingTarget(target) {
    if (!target) return false;
    const tagName = target.tagName?.toLowerCase();
    return tagName === 'input' || tagName === 'textarea' || target.isContentEditable;
}

// choice: 'real', 'model', 'reject', or 'hybrid'
async function submitAnswer(choice) {
    if (currentDocId === null || currentPairIndex === null) {
        return;
    }

    let acceptedEvent = null;
    if (choice === 'real') {
        if (currentExample.real_event) {
            acceptedEvent = JSON.parse(JSON.stringify(currentExample.real_event));
        }
    } else if (choice === 'unaligned') {
        // Annotations are misaligned (they describe different spans, e.g. one at the
        // start of the text and the other at the end). Keep the original annotation
        // verbatim as the corrected output, ignoring the model and any flags.
        if (currentExample.real_event) {
            acceptedEvent = JSON.parse(JSON.stringify(currentExample.real_event));
        }
        await sendValidation(acceptedEvent, [], getReviewCommentText());
        return;
    } else if (choice === 'model') {
        if (currentExample.model_event) {
            acceptedEvent = JSON.parse(JSON.stringify(currentExample.model_event));
        }
    } else if (choice === 'hybrid') {
        // If every discrepancy is only flagged (no real/model selection), skip this pair
        const anyRealOrModelChecked = currentDiscrepantKeys &&
            [...currentDiscrepantKeys].some(key =>
                document.querySelector(`#realChecklist  input[data-field="${key}"]`)?.checked ||
                document.querySelector(`#modelChecklist input[data-field="${key}"]`)?.checked
            );

        if (!anyRealOrModelChecked) {
            // All discrepancies are flagged — accept nothing (skip)
            acceptedEvent = null;
        } else if (currentExample.real_event) {
            acceptedEvent = JSON.parse(JSON.stringify(currentExample.real_event));

            const modelChecklist = document.getElementById('modelChecklist');
            if (modelChecklist) {
                const overrides = modelChecklist.querySelectorAll('input:checked');
                overrides.forEach(cb => {
                    const field = cb.dataset.field; // This is now a full path like A->B
                    const modelVal = getNestedValue(currentExample.model_event, field);
                    if (modelVal !== undefined) {
                        setNestedValue(acceptedEvent, field, modelVal);
                    } else {
                        deleteNestedValue(acceptedEvent, field);
                    }
                });
            }
        } else if (currentExample.model_event) {
            acceptedEvent = JSON.parse(JSON.stringify(currentExample.model_event));
            const realChecklist = document.getElementById('realChecklist');
            if (realChecklist) {
                const overrides = realChecklist.querySelectorAll('input:checked');
                overrides.forEach(cb => {
                    const field = cb.dataset.field;
                    const realVal = getNestedValue(currentExample.real_event, field);
                    if (realVal !== undefined) {
                        setNestedValue(acceptedEvent, field, realVal);
                    } else {
                        deleteNestedValue(acceptedEvent, field); // fallback delete logic
                    }
                });
            }
        }
        if (acceptedEvent && Object.keys(acceptedEvent).length === 0) {
            acceptedEvent = null;
        }
    } else if (choice === 'reject') {
        const flagChecklist = document.getElementById('flagChecklist');
        if (flagChecklist) {
            const checkboxes = flagChecklist.querySelectorAll('input[type="checkbox"]');
            checkboxes.forEach(cb => cb.checked = true);
        }
        updateSubmitButtonsState();
        return;
    }

    let flaggedFields = [];
    const flagChecklist = document.getElementById('flagChecklist');
    if (flagChecklist) {
        const checkedFlags = flagChecklist.querySelectorAll('input:checked');
        checkedFlags.forEach(cb => {
            flaggedFields.push(cb.dataset.field);
        });
    }

    await sendValidation(acceptedEvent, flaggedFields, getReviewCommentText());
}

async function sendValidation(acceptedEvent, flaggedFields, comment) {
    try {
        const example = await eel.submit_validation(
            currentDocId, currentPairIndex, acceptedEvent, flaggedFields, comment
        )();
        handleExample(example);
    } catch (error) {
        console.error('Error in submitAnswer:', error);
    }
}

function showCompletionMessage(outputFile) {
    document.querySelector('.example-container').style.display = 'none';
    const completionMessage = document.getElementById('completionMessage');
    const outputFileSpan = document.getElementById('outputFile');
    outputFileSpan.textContent = outputFile || "Saved directly to output dir.";
    completionMessage.style.display = 'block';
}

document.addEventListener('keydown', (event) => {
    const key = event.key.toLowerCase();

    if (isTypingTarget(event.target)) {
        return;
    }

    if (keyStates[key] ||
        document.getElementById('editModal')?.style.display === 'block') {
        return;
    }

    keyStates[key] = true;

    if (key === 'backspace') {
        event.preventDefault();
        document.getElementById('undoBtn').click();
    }
});

document.addEventListener('keyup', (event) => {
    const key = event.key.toLowerCase();
    keyStates[key] = false;
});
