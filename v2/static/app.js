/* app.js — Water Controller v2 frontend */

'use strict';

// =========================================================================
// State
// =========================================================================
let cfg = null;           // current config from server
let sseSource = null;
let currentMode = 'sequence';
let lastStatus = {};      // previous SSE payload for diff
let gpioGridRendered = false;

// =========================================================================
// Init
// =========================================================================
document.addEventListener('DOMContentLoaded', async () => {
  await loadConfig();
  await refreshStatus();
  connectSSE();
});

// =========================================================================
// Config — Load / Save
// =========================================================================
async function loadConfig() {
  try {
    const res = await fetch('/api/config');
    cfg = await res.json();
    renderConfigForm(cfg);
  } catch (e) {
    showFeedback('error', 'Could not load configuration: ' + e.message);
  }
}

async function saveConfig() {
  const data = readFormConfig();
  if (!data) return;
  try {
    const res = await fetch('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    });
    const result = await res.json();
    if (!res.ok) {
      showFeedback('error', result.error ?? 'Save failed');
      return;
    }
    cfg = result.config;
    renderConfigForm(cfg);
    await refreshStatus();
    showFeedback('ok', 'Configuration saved.');
  } catch (e) {
    showFeedback('error', 'Network error: ' + e.message);
  }
}

// =========================================================================
// Config — Render form
// =========================================================================
function renderConfigForm(c) {
  document.getElementById('poll-interval').value = c.settings?.poll_interval_ms ?? 500;
  document.getElementById('valve-inverted').checked = c.hardware?.valve_inverted ?? true;

  // Pin table
  const table = document.getElementById('pin-table');
  table.innerHTML = '';
  const sensor = c.hardware?.sensor ?? {};
  table.appendChild(makePinRow('Sensor (drive)', sensor.drive_gpio, sensor.label ?? 'Sensor'));
  table.appendChild(makePinRow('Sensor (read)', sensor.read_gpio, sensor.label ?? 'Sensor'));
  const valves = c.hardware?.valves ?? [];
  valves.forEach((v) => {
    table.appendChild(makePinRow('Valve', v.gpio, v.label));
  });

  // Timings
  renderTimingTable(c);

  // Default states
  renderDefaultStateTable(c);

  // Mode
  currentMode = c.mode ?? 'sequence';
  selectMode(currentMode);

  // Sequence mode
  renderNamedSequenceSection('seq-high-section', 'on_sensor_high',
    c.modes?.sequence?.on_sensor_high ?? {}, c, 'Runs when sensor reads HIGH');
  renderNamedSequenceSection('seq-low-section', 'on_sensor_low',
    c.modes?.sequence?.on_sensor_low ?? {}, c, 'Runs when sensor reads LOW');

  // Alternance mode
  renderAlternanceSequences(c);
}

function renderTimingTable(c) {
  const container = document.getElementById('timing-table');
  container.innerHTML = '';
  const valves = c.hardware?.valves ?? [];
  valves.forEach((v, i) => {
    const row = document.createElement('div');
    row.className = 'timing-row';
    row.innerHTML = `
      <div>
        <div class="timing-label">${esc(v.label)}</div>
        <div class="timing-sub">GPIO ${v.gpio}</div>
      </div>
      <div>
        <label style="font-size:.7rem;color:var(--muted)">Open (ms)</label>
        <input type="number" class="timing-open" data-index="${i}"
               min="0" max="30000" step="50" value="${v.open_ms ?? 0}" />
      </div>
      <div>
        <label style="font-size:.7rem;color:var(--muted)">Close (ms)</label>
        <input type="number" class="timing-close" data-index="${i}"
               min="0" max="30000" step="50" value="${v.close_ms ?? 0}" />
      </div>`;
    container.appendChild(row);
  });
}

function renderDefaultStateTable(c) {
  const container = document.getElementById('default-state-table');
  container.innerHTML = '';
  const valves = c.hardware?.valves ?? [];
  const defaults = c.settings?.default_valve_states ?? [];
  valves.forEach((v, i) => {
    const ds = defaults[i] ?? 0;
    const row = document.createElement('div');
    row.className = 'timing-row';
    row.style.gridTemplateColumns = '1fr auto';
    row.innerHTML = `
      <div>
        <div class="timing-label">${esc(v.label)}</div>
        <div class="timing-sub">GPIO ${v.gpio}</div>
      </div>
      <div>
        <select class="default-state-select" data-index="${i}"
                style="background:var(--surface);border:1px solid var(--border);border-radius:5px;
                       color:var(--text);font-size:.875rem;padding:6px 8px;outline:none">
          <option value="0" ${ds === 0 ? 'selected' : ''}>Closed</option>
          <option value="1" ${ds === 1 ? 'selected' : ''}>Open</option>
        </select>
      </div>`;
    container.appendChild(row);
  });
}

// =========================================================================
// Config — Named Sequence sections (sequence mode)
// =========================================================================
function renderNamedSequenceSection(containerId, key, seqObj, c, hint) {
  const container = document.getElementById(containerId);
  const name = seqObj.name ?? key;
  const steps = seqObj.steps ?? [];

  container.innerHTML = `
    <div style="margin-bottom:8px">
      <label style="display:block;margin-bottom:4px">Sequence name</label>
      <input type="text" class="seq-name-input" data-key="${key}" value="${esc(name)}" maxlength="60" />
      <p class="pin-hint">${esc(hint)}</p>
    </div>
    <div id="seq-steps-${key}" class="seq-block"></div>
    <button class="btn-add-step" onclick="addStep('seq-steps-${key}')">+ Add step</button>
  `;

  const stepsContainer = document.getElementById(`seq-steps-${key}`);
  steps.forEach((step, i) => appendStepRow(stepsContainer, i, step, c));
  renumberSteps(stepsContainer);
}

// =========================================================================
// Config — Alternance sequences (dynamic list)
// =========================================================================
function renderAlternanceSequences(c) {
  const container = document.getElementById('alt-sequences');
  container.innerHTML = '';
  const sequences = c.modes?.alternance?.sequences ?? [];
  sequences.forEach((seq, i) => addAlternanceSequenceEntry(container, i, seq, c));
}

function addAlternanceSequenceEntry(container, index, seq, c) {
  const div = document.createElement('div');
  div.className = 'alt-seq-entry';
  div.dataset.altIndex = index;

  const name = seq.name ?? `Sequence ${index + 1}`;
  const delay = seq.delay_after_ms ?? 5000;

  div.innerHTML = `
    <div class="alt-seq-header">
      <input type="text" class="alt-seq-name" value="${esc(name)}" maxlength="60" placeholder="Sequence name" />
      <div style="display:flex;flex-direction:column;gap:2px;min-width:110px">
        <label style="font-size:.65rem;color:var(--muted)">Delay after (ms)</label>
        <input type="number" class="alt-seq-delay" min="0" max="300000" step="100" value="${delay}" />
      </div>
      <button class="btn-remove-seq" onclick="removeAlternanceSequence(this)" title="Remove sequence">&#x2715;</button>
    </div>
    <div class="alt-seq-steps seq-block"></div>
    <button class="btn-add-step" onclick="addStep(this.previousElementSibling.id || assignAltStepsId(this))">+ Add step</button>
  `;

  // Assign ID to steps container
  const stepsEl = div.querySelector('.alt-seq-steps');
  stepsEl.id = `alt-steps-${index}`;

  // Fix the add step button to use the right ID
  const addBtn = div.querySelector('.btn-add-step');
  addBtn.setAttribute('onclick', `addStep('${stepsEl.id}')`);

  container.appendChild(div);

  // Render steps
  const steps = seq.steps ?? [];
  steps.forEach((step, i) => appendStepRow(stepsEl, i, step, c));
  renumberSteps(stepsEl);
}

function addAlternanceSequence() {
  const container = document.getElementById('alt-sequences');
  const count = container.querySelectorAll('.alt-seq-entry').length;
  addAlternanceSequenceEntry(container, count,
    { name: `Sequence ${count + 1}`, steps: [], delay_after_ms: 5000 }, cfg);
}

function removeAlternanceSequence(btn) {
  const entry = btn.closest('.alt-seq-entry');
  const container = entry.parentElement;
  if (container.querySelectorAll('.alt-seq-entry').length <= 2) {
    alert('Alternance mode requires at least 2 sequences.');
    return;
  }
  entry.remove();
  // Re-index
  container.querySelectorAll('.alt-seq-entry').forEach((el, i) => {
    el.dataset.altIndex = i;
    const stepsEl = el.querySelector('.alt-seq-steps');
    stepsEl.id = `alt-steps-${i}`;
    el.querySelector('.btn-add-step').setAttribute('onclick', `addStep('${stepsEl.id}')`);
  });
}

// =========================================================================
// Config — Sequence step builder (shared)
// =========================================================================
function appendStepRow(container, index, step, c) {
  const valves = (c ?? cfg)?.hardware?.valves ?? [];
  const row = document.createElement('div');
  row.className = 'seq-step';

  let opts = '';
  valves.forEach((v, i) => {
    opts += `<option value="${i}" ${step.valve_index === i ? 'selected' : ''}>${esc(v.label)}</option>`;
  });

  row.innerHTML = `
    <span class="seq-step-num">${index + 1}</span>
    <div class="seq-fields">
      <div class="seq-field seq-field-wide">
        <label>Valve</label>
        <select class="step-valve">${opts}</select>
      </div>
      <div class="seq-field">
        <label>State</label>
        <select class="step-state">
          <option value="1" ${step.state === 1 ? 'selected' : ''}>Open</option>
          <option value="0" ${step.state === 0 ? 'selected' : ''}>Close</option>
        </select>
      </div>
      <div class="seq-field">
        <label>Delay after (ms)</label>
        <input type="number" class="step-delay" min="0" max="300000" step="50"
               value="${step.delay_after_ms ?? 0}" />
      </div>
    </div>
    <button class="btn-icon" title="Remove step" onclick="removeStep(this)">&#x2715;</button>
  `;
  container.appendChild(row);
}

function addStep(containerId) {
  const container = document.getElementById(containerId);
  const count = container.querySelectorAll('.seq-step').length;
  appendStepRow(container, count, { valve_index: 0, state: 1, delay_after_ms: 0 }, null);
  renumberSteps(container);
}

function removeStep(btn) {
  const container = btn.closest('.seq-block');
  btn.closest('.seq-step').remove();
  renumberSteps(container);
}

function renumberSteps(container) {
  container.querySelectorAll('.seq-step').forEach((row, i) => {
    const num = row.querySelector('.seq-step-num');
    if (num) num.textContent = i + 1;
  });
}

// =========================================================================
// Config — Read form into object
// =========================================================================
function readFormConfig() {
  const interval = parseInt(document.getElementById('poll-interval').value, 10);
  const valve_inverted = document.getElementById('valve-inverted').checked;

  // Valve timings
  const valves_update = [];
  document.querySelectorAll('.timing-open').forEach((el, i) => {
    const closeEl = document.querySelector(`.timing-close[data-index="${i}"]`);
    valves_update.push({
      open_ms:  parseInt(el.value, 10) || 0,
      close_ms: parseInt(closeEl?.value, 10) || 0,
    });
  });

  // Default states
  const default_valve_states = [];
  document.querySelectorAll('.default-state-select').forEach(el => {
    default_valve_states.push(parseInt(el.value, 10));
  });

  // Read sequence steps from a container
  function readSteps(containerId) {
    const steps = [];
    document.querySelectorAll(`#${containerId} .seq-step`).forEach(row => {
      steps.push({
        valve_index:    parseInt(row.querySelector('.step-valve').value, 10),
        state:          parseInt(row.querySelector('.step-state').value, 10),
        delay_after_ms: parseInt(row.querySelector('.step-delay').value, 10) || 0,
      });
    });
    return steps;
  }

  // Sequence mode — named sequences
  function readNamedSequence(key) {
    const nameInput = document.querySelector(`.seq-name-input[data-key="${key}"]`);
    return {
      name: nameInput?.value?.trim() || key,
      steps: readSteps(`seq-steps-${key}`),
    };
  }

  // Alternance mode — dynamic list
  const altSequences = [];
  document.querySelectorAll('#alt-sequences .alt-seq-entry').forEach(entry => {
    const name = entry.querySelector('.alt-seq-name')?.value?.trim() || 'Unnamed';
    const delay = parseInt(entry.querySelector('.alt-seq-delay')?.value, 10) || 5000;
    const stepsEl = entry.querySelector('.alt-seq-steps');
    const steps = [];
    stepsEl.querySelectorAll('.seq-step').forEach(row => {
      steps.push({
        valve_index:    parseInt(row.querySelector('.step-valve').value, 10),
        state:          parseInt(row.querySelector('.step-state').value, 10),
        delay_after_ms: parseInt(row.querySelector('.step-delay').value, 10) || 0,
      });
    });
    altSequences.push({ name, steps, delay_after_ms: delay });
  });

  return {
    mode: currentMode,
    hardware: {
      valve_inverted,
      valves: valves_update,
    },
    settings: {
      poll_interval_ms: interval,
      default_valve_states,
    },
    modes: {
      sequence: {
        on_sensor_high: readNamedSequence('on_sensor_high'),
        on_sensor_low: readNamedSequence('on_sensor_low'),
      },
      alternance: {
        sequences: altSequences,
      },
    },
  };
}

// =========================================================================
// Mode selector
// =========================================================================
function selectMode(mode) {
  currentMode = mode;
  document.querySelectorAll('.mode-tab').forEach(tab => {
    tab.classList.toggle('active', tab.dataset.mode === mode);
  });
  ['sequence', 'alternance'].forEach(m => {
    const el = document.getElementById('mode-' + m);
    if (el) el.classList.toggle('active', m === mode);
  });
}

// =========================================================================
// Task control
// =========================================================================
async function taskAction(action) {
  try {
    const res = await fetch(`/api/task/${action}`, { method: 'POST' });
    const data = await res.json();
    if (!res.ok) alert(data.error ?? `${action} failed`);
    await refreshStatus();
  } catch (e) {
    alert('Network error: ' + e.message);
  }
}

async function refreshStatus() {
  try {
    const res = await fetch('/api/task/status');
    const data = await res.json();
    applyStatus(data);
  } catch (_) {}
}

// =========================================================================
// Manual override
// =========================================================================
async function toggleOverride(enabled) {
  try {
    const res = await fetch('/api/manual-override', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled }),
    });
    if (!res.ok) {
      const data = await res.json();
      alert(data.error ?? 'Override toggle failed');
      document.getElementById('override-toggle').checked = !enabled;
    }
  } catch (_) {
    document.getElementById('override-toggle').checked = !enabled;
  }
}

function renderManualGrid(c) {
  const container = document.getElementById('manual-grid');
  container.innerHTML = '';
  if (!c) return;

  const sensor = c.hardware?.sensor ?? {};
  const valves = c.hardware?.valves ?? [];
  const states = lastStatus.gpio_states ?? {};

  // Sensor drive
  const driveOn = states[`gpio_${sensor.drive_gpio}`] === 1;
  container.appendChild(makeManualRow(
    `${sensor.label ?? 'Sensor'} (drive)`, `GPIO ${sensor.drive_gpio}`,
    'manual-sensor-drive', driveOn,
    `manualSensorToggle('drive', this.checked)`,
  ));

  // Sensor read
  const readOn = states[`gpio_${sensor.read_gpio}`] === 1;
  container.appendChild(makeManualRow(
    `${sensor.label ?? 'Sensor'} (read)`, `GPIO ${sensor.read_gpio}`,
    'manual-sensor-read', readOn,
    `manualSensorToggle('read', this.checked)`,
  ));

  // Valves
  valves.forEach((v, i) => {
    const isOn = states[`gpio_${v.gpio}`] === 1;
    container.appendChild(makeManualRow(
      v.label, `GPIO ${v.gpio}`,
      `manual-valve-${i}`, isOn,
      `manualValveToggle(${i}, this.checked)`,
    ));
  });
}

function makeManualRow(name, pinText, id, isOn, onchangeStr) {
  const row = document.createElement('div');
  row.className = 'manual-row';
  row.id = id;
  row.innerHTML = `
    <div style="flex:1">
      <div class="valve-name">${esc(name)}</div>
      <div class="valve-pin">${esc(pinText)}</div>
    </div>
    <span class="toggle-label ${isOn ? 'on' : 'off'}" id="${id}-label">${isOn ? 'ON' : 'OFF'}</span>
    <label class="toggle">
      <input type="checkbox" id="${id}-toggle" ${isOn ? 'checked' : ''} onchange="${onchangeStr}">
      <span class="slider"></span>
    </label>
  `;
  return row;
}

async function manualValveToggle(index, checked) {
  updateToggleLabel(`manual-valve-${index}`, checked);
  try {
    await fetch('/api/gpio/set', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ valve_index: index, state: checked ? 1 : 0 }),
    });
  } catch (_) {}
}

async function manualSensorToggle(role, checked) {
  updateToggleLabel(`manual-sensor-${role}`, checked);
  try {
    await fetch('/api/gpio/set-sensor', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pin: role, state: checked ? 1 : 0 }),
    });
  } catch (_) {}
}

function updateToggleLabel(rowId, isOn) {
  const label = document.getElementById(`${rowId}-label`);
  if (label) {
    label.textContent = isOn ? 'ON' : 'OFF';
    label.className = `toggle-label ${isOn ? 'on' : 'off'}`;
  }
}

function syncManualToggles(states) {
  if (!cfg) return;
  const sensor = cfg.hardware?.sensor ?? {};
  syncOneToggle('manual-sensor-drive', states[`gpio_${sensor.drive_gpio}`]);
  syncOneToggle('manual-sensor-read', states[`gpio_${sensor.read_gpio}`]);
  const valves = cfg.hardware?.valves ?? [];
  valves.forEach((v, i) => {
    syncOneToggle(`manual-valve-${i}`, states[`gpio_${v.gpio}`]);
  });
}

function syncOneToggle(rowId, val) {
  const toggle = document.getElementById(`${rowId}-toggle`);
  const label = document.getElementById(`${rowId}-label`);
  if (!toggle) return;
  const isOn = val === 1;
  toggle.checked = isOn;
  if (label) {
    label.textContent = isOn ? 'ON' : 'OFF';
    label.className = `toggle-label ${isOn ? 'on' : 'off'}`;
  }
}

// =========================================================================
// SSE
// =========================================================================
function connectSSE() {
  if (sseSource) sseSource.close();
  sseSource = new EventSource('/api/gpio/stream');
  const indicator = document.getElementById('conn-indicator');

  sseSource.onopen = () => {
    indicator.textContent = 'Live';
    indicator.style.color = 'var(--green)';
  };

  sseSource.onmessage = (evt) => {
    try {
      const data = JSON.parse(evt.data);
      applyStatus(data);
    } catch (_) {}
  };

  sseSource.onerror = () => {
    indicator.textContent = 'Disconnected — reconnecting…';
    indicator.style.color = 'var(--yellow)';
    sseSource.close();
    setTimeout(connectSSE, 3000);
  };
}

// =========================================================================
// Status — apply incoming status (diff-based)
// =========================================================================
function applyStatus(data) {
  const running = data.running;
  const mode = data.mode ?? currentMode;
  const override = data.manual_override ?? false;
  const phase = data.phase;
  const states = data.gpio_states ?? {};

  // Task badges
  const badge = document.getElementById('task-badge');
  const dot = document.getElementById('task-dot');
  const label = document.getElementById('task-label');
  const btnStart = document.getElementById('btn-start');
  const btnStop = document.getElementById('btn-stop');

  if (running) {
    badge.className = 'badge badge-running';
    dot.className = 'dot dot-green';
    label.textContent = 'Running';
    btnStart.disabled = true;
    btnStop.disabled = false;
  } else {
    badge.className = 'badge badge-stopped';
    dot.className = 'dot dot-red';
    label.textContent = 'Stopped';
    btnStart.disabled = false;
    btnStop.disabled = true;
  }

  // Mode badge
  const modeBadge = document.getElementById('mode-badge');
  modeBadge.dataset.mode = mode;
  modeBadge.textContent = mode.charAt(0).toUpperCase() + mode.slice(1);

  // Override badge + toggle
  const overrideBadge = document.getElementById('override-badge');
  const overrideToggle = document.getElementById('override-toggle');
  overrideBadge.classList.toggle('active', override);
  overrideToggle.checked = override;
  overrideToggle.disabled = !running;

  // Manual controls visibility
  const manualControls = document.getElementById('manual-controls');
  if (override) {
    manualControls.classList.add('active');
    if (!manualControls.dataset.rendered) {
      renderManualGrid(cfg);
      manualControls.dataset.rendered = '1';
    }
    syncManualToggles(states);
  } else {
    manualControls.classList.remove('active');
    manualControls.dataset.rendered = '';
  }

  // GPIO grid — render once, then diff-update
  if (!gpioGridRendered && cfg) {
    renderGpioGrid(cfg, states);
    gpioGridRendered = true;
  } else {
    // Diff update
    const prev = lastStatus.gpio_states ?? {};
    for (const [key, val] of Object.entries(states)) {
      if (prev[key] !== val) {
        const pin = parseInt(key.replace('gpio_', ''), 10);
        updateGpioItem(pin, val);
      }
    }
  }

  // Diagram
  if (cfg) updateDiagram(cfg, states, running, phase);

  lastStatus = data;
}

// =========================================================================
// GPIO grid
// =========================================================================
function renderGpioGrid(c, states) {
  const grid = document.getElementById('gpio-grid');
  grid.innerHTML = '';
  const sensor = c.hardware?.sensor ?? {};
  grid.appendChild(makeGpioItem('Sensor (drive)', sensor.label ?? 'Sensor',
    sensor.drive_gpio, states[`gpio_${sensor.drive_gpio}`]));
  grid.appendChild(makeGpioItem('Sensor (read)', sensor.label ?? 'Sensor',
    sensor.read_gpio, states[`gpio_${sensor.read_gpio}`]));
  const valves = c.hardware?.valves ?? [];
  valves.forEach(v => {
    grid.appendChild(makeGpioItem('Valve', v.label, v.gpio, states[`gpio_${v.gpio}`]));
  });
}

function makeGpioItem(role, label, pin, value) {
  const div = document.createElement('div');
  div.className = 'gpio-item';
  div.id = `gpio-item-${pin}`;
  let stateClass, stateText;
  if (value === undefined || value === null) {
    stateClass = 'state-unknown'; stateText = 'Unknown';
  } else if (value === 1) {
    stateClass = 'state-high'; stateText = 'HIGH';
  } else {
    stateClass = 'state-low'; stateText = 'LOW';
  }
  div.innerHTML = `
    <span class="gpio-role">${esc(role)}</span>
    <span class="gpio-label">${esc(label)}</span>
    <span class="gpio-pin">GPIO ${pin} (BCM)</span>
    <span class="gpio-state ${stateClass}">${stateText}</span>
  `;
  return div;
}

function updateGpioItem(pin, value) {
  const item = document.getElementById(`gpio-item-${pin}`);
  if (!item) return;
  const stateEl = item.querySelector('.gpio-state');
  if (!stateEl) return;
  if (value === 1) {
    stateEl.className = 'gpio-state state-high';
    stateEl.textContent = 'HIGH';
  } else {
    stateEl.className = 'gpio-state state-low';
    stateEl.textContent = 'LOW';
  }
}

// =========================================================================
// Diagram
// =========================================================================
const VALVE_DIAGRAM_MAP = [
  { match: 'air',        valve: 'd-valve-air',    pipe: 'd-pipe-air',      label: 'd-label-air' },
  { match: 'vacuum',     valve: 'd-valve-vacuum', pipe: 'd-pipe-vacuum',   label: 'd-label-vacuum' },
  { match: 'water pump', valve: 'd-valve-wp',     pipe: 'd-pipe-wp-valve', label: 'd-label-wp-valve' },
  { match: 'maple',      valve: 'd-valve-maple',  pipe: 'd-pipe-maple',    label: 'd-label-maple' },
  { match: 'pump',       pump:  'd-pump',         pipe: 'd-pipe-pump',     label: 'd-label-pump', icon: 'd-pump-icon' },
];

function updateDiagram(c, states, running, phase) {
  if (!c) return;
  const sensor = c.hardware?.sensor ?? {};
  const valves = c.hardware?.valves ?? [];
  const sensorVal = states[`gpio_${sensor.read_gpio}`];
  const circuitClosed = sensorVal === 1;

  // Sensors
  diagClass('d-sensor-top', circuitClosed ? 'on' : '');
  diagClass('d-label-sensor-top', circuitClosed ? 'on' : '');
  diagClass('d-sensor-bot', !circuitClosed && running ? 'on' : '');
  diagClass('d-label-sensor-bot', !circuitClosed && running ? 'on' : '');

  // Water level
  const waterEl = document.getElementById('d-water');
  if (waterEl) {
    let waterY, waterH;
    if (!running)          { waterY = 288; waterH = 10; }
    else if (circuitClosed){ waterY = 84;  waterH = 214; }
    else                   { waterY = 260; waterH = 38; }
    waterEl.setAttribute('y', waterY);
    waterEl.setAttribute('height', waterH);
  }

  // Valves & pump
  valves.forEach(v => {
    const lbl = (v.label ?? '').toLowerCase();
    const val = states[`gpio_${v.gpio}`];
    const isOn = val === 1;

    const entry = VALVE_DIAGRAM_MAP.find(e => {
      if (e.match === 'pump')       return lbl === 'water pump';
      if (e.match === 'water pump') return lbl.includes('water pump') && lbl !== 'water pump';
      return lbl.includes(e.match);
    });
    if (!entry) return;

    if (entry.pump) {
      diagClass(entry.pump, isOn ? 'active' : '');
      diagClass(entry.label, isOn ? 'pump-active' : '');
      diagClass(entry.pipe, isOn ? 'active' : '');
      const icon = document.getElementById(entry.icon);
      if (icon) icon.style.fill = isOn ? '#22c55e' : '#475569';
    } else {
      diagClass(entry.valve, isOn ? 'open' : (val === 0 ? 'closed' : ''));
      diagClass(entry.label, isOn ? 'open' : (val === 0 ? 'closed' : ''));
      diagClass(entry.pipe, isOn ? 'active' : '');
    }
  });

  // Phase banner
  const phaseEl = document.getElementById('d-phase');
  if (phaseEl) {
    if (!running) {
      phaseEl.textContent = 'Stopped';
      phaseEl.className = 'idle';
    } else if (phase) {
      phaseEl.textContent = phase + '…';
      phaseEl.className = 'active';
    } else {
      phaseEl.textContent = 'Waiting';
      phaseEl.className = 'idle';
    }
  }
}

function diagClass(id, cls) {
  const el = document.getElementById(id);
  if (!el) return;
  el.classList.remove('open', 'closed', 'active', 'on', 'pump-active');
  if (cls) el.classList.add(cls);
}

// =========================================================================
// Helpers
// =========================================================================
function makePinRow(role, pin, name) {
  const div = document.createElement('div');
  div.className = 'pin-row';
  div.innerHTML = `
    <span class="pin-role">${esc(role)}</span>
    <span class="pin-num">GPIO ${pin}</span>
    <span class="pin-name">${esc(name)}</span>
  `;
  return div;
}

function showFeedback(type, msg) {
  const el = document.getElementById('config-feedback');
  el.className = `feedback ${type}`;
  el.textContent = msg;
  if (type === 'ok') setTimeout(() => { el.className = 'feedback'; }, 4000);
}

function esc(str) {
  return String(str)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
