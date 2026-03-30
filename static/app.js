/* app.js — Water Controller frontend */

'use strict';

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
let currentConfig = null;
let sseSource = null;
let currentMode = 'sequence';  // tracks the active mode from config
let lastGpioStates = {};       // most recent gpio states from SSE/status

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------
document.addEventListener('DOMContentLoaded', async () => {
  await loadConfig();
  await refreshStatus();
  connectSSE();
});

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------
async function loadConfig() {
  try {
    const res = await fetch('/api/config');
    currentConfig = await res.json();
    renderConfigForm(currentConfig);
  } catch (e) {
    showFeedback('error', 'Could not load configuration: ' + e.message);
  }
}

function renderConfigForm(cfg) {
  document.getElementById('poll-interval').value       = cfg.poll_interval_ms ?? 500;
  document.getElementById('valve-inverted').checked    = cfg.valve_inverted ?? true;

  // Pin table (read-only)
  const table = document.getElementById('pin-table');
  table.innerHTML = '';
  table.appendChild(makePinRow('Sensor (drive)', cfg.sensor_drive_gpio, cfg.sensor_label ?? 'Sensor'));
  table.appendChild(makePinRow('Sensor (read)',  cfg.sensor_read_gpio,  cfg.sensor_label ?? 'Sensor'));
  const valves  = cfg.valve_gpios  ?? [];
  const vlabels = cfg.valve_labels ?? [];
  valves.forEach((pin, i) => {
    table.appendChild(makePinRow('Valve', pin, vlabels[i] ?? `Valve ${i + 1}`));
  });

  // Valve timings
  renderTimingTable(cfg);

  // Default state table
  renderDefaultStateTable(cfg);

  // Mode selector
  currentMode = cfg.mode ?? 'sequence';
  selectMode(currentMode);

  // Sequences (sequence mode)
  renderSequence('dump-seq', cfg.dump_sequence ?? [], cfg);
  renderSequence('idle-seq', cfg.idle_sequence ?? [], cfg);

  // Alternance sequences
  const alt = cfg.alternance ?? {};
  renderSequence('alt-seq-a', alt.sequence_a ?? [], cfg);
  renderSequence('alt-seq-b', alt.sequence_b ?? [], cfg);
  document.getElementById('alt-delay-a').value = alt.delay_a_to_b_ms ?? 5000;
  document.getElementById('alt-delay-b').value = alt.delay_b_to_a_ms ?? 5000;

  // Manual mode toggles (use last known states so toggles match the monitor)
  renderManualGrid(cfg, lastGpioStates);
}

function renderTimingTable(cfg) {
  const container = document.getElementById('timing-table');
  container.innerHTML = '';
  const valves  = cfg.valve_gpios  ?? [];
  const vlabels = cfg.valve_labels ?? [];
  const timings = cfg.valve_timings ?? [];

  valves.forEach((pin, i) => {
    const t    = timings[i] ?? { open_ms: 0, close_ms: 0 };
    const name = vlabels[i] ?? `Valve ${i + 1}`;
    const row  = document.createElement('div');
    row.className = 'timing-row';
    row.innerHTML = `
      <div>
        <div class="timing-label">${escapeHTML(name)}</div>
        <div class="timing-sub">GPIO ${pin}</div>
      </div>
      <div>
        <label style="font-size:.7rem;color:var(--muted)">Open (ms)</label>
        <input type="number" class="timing-open" data-index="${i}"
               min="0" max="30000" step="50" value="${t.open_ms}" />
      </div>
      <div>
        <label style="font-size:.7rem;color:var(--muted)">Close (ms)</label>
        <input type="number" class="timing-close" data-index="${i}"
               min="0" max="30000" step="50" value="${t.close_ms}" />
      </div>
    `;
    container.appendChild(row);
  });
}

function renderDefaultStateTable(cfg) {
  const container = document.getElementById('default-state-table');
  container.innerHTML = '';
  const valves  = cfg.valve_gpios  ?? [];
  const vlabels = cfg.valve_labels ?? [];
  const defaults = cfg.valve_default_state ?? [];

  valves.forEach((pin, i) => {
    const ds   = defaults[i] ?? 0;
    const name = vlabels[i] ?? `Valve ${i + 1}`;
    const row  = document.createElement('div');
    row.className = 'timing-row';
    row.style.gridTemplateColumns = '1fr auto';
    row.innerHTML = `
      <div>
        <div class="timing-label">${escapeHTML(name)}</div>
        <div class="timing-sub">GPIO ${pin}</div>
      </div>
      <div>
        <select class="default-state-select" data-index="${i}"
                style="background:var(--surface);border:1px solid var(--border);border-radius:5px;color:var(--text);font-size:.875rem;padding:6px 8px;outline:none">
          <option value="0" ${ds === 0 ? 'selected' : ''}>Closed</option>
          <option value="1" ${ds === 1 ? 'selected' : ''}>Open</option>
        </select>
      </div>
    `;
    container.appendChild(row);
  });
}

function renderSequence(containerId, steps, cfg) {
  const container = document.getElementById(containerId);
  container.innerHTML = '';
  steps.forEach((step, i) => appendStepRow(container, i, step, cfg));
  renumberSteps(container);
}

function appendStepRow(container, index, step, cfg) {
  const cfg_ = cfg ?? currentConfig;
  const vlabels = cfg_?.valve_labels ?? [];
  const nValves = cfg_?.valve_gpios?.length ?? 0;

  const row = document.createElement('div');
  row.className = 'seq-step';

  // Valve selector options
  let opts = '';
  for (let i = 0; i < nValves; i++) {
    const sel = (step.valve_index === i) ? 'selected' : '';
    opts += `<option value="${i}" ${sel}>${escapeHTML(vlabels[i] ?? `Valve ${i + 1}`)}</option>`;
  }

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

function makePinRow(role, pin, name) {
  const div = document.createElement('div');
  div.className = 'pin-row';
  div.innerHTML = `
    <span class="pin-role">${escapeHTML(role)}</span>
    <span class="pin-num">GPIO ${pin}</span>
    <span class="pin-name">${escapeHTML(name)}</span>
  `;
  return div;
}

// ---------------------------------------------------------------------------
// Mode selector
// ---------------------------------------------------------------------------
function selectMode(mode) {
  currentMode = mode;

  // Tab highlight
  document.querySelectorAll('.mode-tab').forEach(tab => {
    tab.classList.toggle('active', tab.dataset.mode === mode);
  });

  // Show/hide sections
  ['sequence', 'alternance', 'manual'].forEach(m => {
    const el = document.getElementById('mode-' + m);
    if (el) el.classList.toggle('active', m === mode);
  });
}

// ---------------------------------------------------------------------------
// Manual mode
// ---------------------------------------------------------------------------
function renderManualGrid(cfg, states) {
  const container = document.getElementById('manual-grid');
  container.innerHTML = '';
  const st = states ?? {};

  // Sensor pins
  const sensorLabel = cfg.sensor_label ?? 'Sensor';

  // Sensor drive
  const driveOn = st[`gpio_${cfg.sensor_drive_gpio}`] === 1;
  const driveRow = document.createElement('div');
  driveRow.className = 'manual-row';
  driveRow.id = 'manual-sensor-drive';
  driveRow.innerHTML = `
    <div style="flex:1">
      <div class="valve-name">${escapeHTML(sensorLabel)} (drive)</div>
      <div class="valve-pin">GPIO ${cfg.sensor_drive_gpio}</div>
    </div>
    <span class="toggle-label ${driveOn ? 'on' : 'off'}" id="manual-label-sensor-drive">${driveOn ? 'ON' : 'OFF'}</span>
    <label class="toggle">
      <input type="checkbox" id="manual-toggle-sensor-drive" ${driveOn ? 'checked' : ''} onchange="manualSensorToggle('drive', this.checked)">
      <span class="slider"></span>
    </label>
  `;
  container.appendChild(driveRow);

  // Sensor read
  const readOn = st[`gpio_${cfg.sensor_read_gpio}`] === 1;
  const readRow = document.createElement('div');
  readRow.className = 'manual-row';
  readRow.id = 'manual-sensor-read';
  readRow.innerHTML = `
    <div style="flex:1">
      <div class="valve-name">${escapeHTML(sensorLabel)} (read)</div>
      <div class="valve-pin">GPIO ${cfg.sensor_read_gpio}</div>
    </div>
    <span class="toggle-label ${readOn ? 'on' : 'off'}" id="manual-label-sensor-read">${readOn ? 'ON' : 'OFF'}</span>
    <label class="toggle">
      <input type="checkbox" id="manual-toggle-sensor-read" ${readOn ? 'checked' : ''} onchange="manualSensorToggle('read', this.checked)">
      <span class="slider"></span>
    </label>
  `;
  container.appendChild(readRow);

  // Valve pins
  const valves  = cfg.valve_gpios  ?? [];
  const vlabels = cfg.valve_labels ?? [];
  const manualSt = cfg.manual_states ?? [];

  valves.forEach((pin, i) => {
    const name = vlabels[i] ?? `Valve ${i + 1}`;
    // Prefer saved manual_states when gpio state is unknown (task stopped)
    const gpioVal = st[`gpio_${pin}`];
    const isOn = gpioVal !== undefined ? gpioVal === 1 : (manualSt[i] === 1);
    const row = document.createElement('div');
    row.className = 'manual-row';
    row.id = `manual-valve-${i}`;
    row.innerHTML = `
      <div style="flex:1">
        <div class="valve-name">${escapeHTML(name)}</div>
        <div class="valve-pin">GPIO ${pin}</div>
      </div>
      <span class="toggle-label ${isOn ? 'on' : 'off'}" id="manual-label-${i}">${isOn ? 'ON' : 'OFF'}</span>
      <label class="toggle">
        <input type="checkbox" id="manual-toggle-${i}" ${isOn ? 'checked' : ''} onchange="manualToggle(${i}, this.checked)">
        <span class="slider"></span>
      </label>
    `;
    container.appendChild(row);
  });
}

async function manualToggle(valveIndex, checked) {
  const state = checked ? 1 : 0;
  const label = document.getElementById(`manual-label-${valveIndex}`);

  // Always update the UI immediately
  if (label) {
    label.textContent = checked ? 'ON' : 'OFF';
    label.className = `toggle-label ${checked ? 'on' : 'off'}`;
  }

  // Persist to config so it survives a reboot
  saveManualStates();

  // If the task is running in manual mode, also push to GPIO
  try {
    const res = await fetch('/api/gpio/set', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ valve_index: valveIndex, state }),
    });
    // Ignore 409 (task not running or not in manual mode) — that's fine
    if (res.ok) {
      await refreshStatus();
    }
  } catch (_) {}
}

async function manualSensorToggle(pinRole, checked) {
  const state = checked ? 1 : 0;
  const label = document.getElementById(`manual-label-sensor-${pinRole}`);

  // Always update the UI immediately
  if (label) {
    label.textContent = checked ? 'ON' : 'OFF';
    label.className = `toggle-label ${checked ? 'on' : 'off'}`;
  }

  // If the task is running in manual mode, also push to GPIO
  try {
    const res = await fetch('/api/gpio/set-sensor', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pin: pinRole, state }),
    });
    if (res.ok) {
      await refreshStatus();
    }
  } catch (_) {}
}

// Persist current manual toggle positions to config (no task restart)
function saveManualStates() {
  const nValves = currentConfig?.valve_gpios?.length ?? 0;
  const states = [];
  for (let i = 0; i < nValves; i++) {
    const toggle = document.getElementById(`manual-toggle-${i}`);
    states.push(toggle ? (toggle.checked ? 1 : 0) : 0);
  }
  fetch('/api/manual-states', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ manual_states: states }),
  }).catch(() => {});
}

// Update manual toggles from SSE state
function updateManualToggles(cfg, states) {
  if (!cfg) return;

  // Sensor toggles
  _syncToggle('manual-toggle-sensor-drive', 'manual-label-sensor-drive', states[`gpio_${cfg.sensor_drive_gpio}`]);
  _syncToggle('manual-toggle-sensor-read',  'manual-label-sensor-read',  states[`gpio_${cfg.sensor_read_gpio}`]);

  // Valve toggles
  const valves = cfg.valve_gpios ?? [];
  valves.forEach((pin, i) => {
    _syncToggle(`manual-toggle-${i}`, `manual-label-${i}`, states[`gpio_${pin}`]);
  });
}

function _syncToggle(toggleId, labelId, val) {
  const toggle = document.getElementById(toggleId);
  const label  = document.getElementById(labelId);
  if (!toggle) return;

  const isOn = val === 1;
  toggle.checked = isOn;
  if (label) {
    label.textContent = isOn ? 'ON' : 'OFF';
    label.className = `toggle-label ${isOn ? 'on' : 'off'}`;
  }
}

// ---------------------------------------------------------------------------
// Save / read config
// ---------------------------------------------------------------------------
async function saveConfig() {
  const cfg = readFormConfig();
  if (!cfg) return;

  try {
    const res = await fetch('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(cfg),
    });
    const data = await res.json();
    if (!res.ok) {
      showFeedback('error', data.error ?? 'Save failed');
      return;
    }
    currentConfig = data.config;
    renderConfigForm(currentConfig);
    await refreshStatus();
    showFeedback('ok', 'Configuration saved.');
  } catch (e) {
    showFeedback('error', 'Network error: ' + e.message);
  }
}

function readFormConfig() {
  const interval        = parseInt(document.getElementById('poll-interval').value, 10);
  const valve_inverted  = document.getElementById('valve-inverted').checked;

  // Valve timings
  const valve_timings = [];
  document.querySelectorAll('.timing-open').forEach((el, i) => {
    const closeEl = document.querySelector(`.timing-close[data-index="${i}"]`);
    valve_timings.push({
      open_ms:  parseInt(el.value, 10)       || 0,
      close_ms: parseInt(closeEl?.value, 10) || 0,
    });
  });

  // Default state
  const valve_default_state = [];
  document.querySelectorAll('.default-state-select').forEach(el => {
    valve_default_state.push(parseInt(el.value, 10));
  });

  // Helper: read a sequence from a container
  function readSequence(containerId) {
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

  // Alternance
  const alternance = {
    sequence_a:      readSequence('alt-seq-a'),
    sequence_b:      readSequence('alt-seq-b'),
    delay_a_to_b_ms: parseInt(document.getElementById('alt-delay-a').value, 10) || 5000,
    delay_b_to_a_ms: parseInt(document.getElementById('alt-delay-b').value, 10) || 5000,
  };

  // Manual toggle states
  const manual_states = [];
  const nValves = currentConfig?.valve_gpios?.length ?? 0;
  for (let i = 0; i < nValves; i++) {
    const toggle = document.getElementById(`manual-toggle-${i}`);
    manual_states.push(toggle ? (toggle.checked ? 1 : 0) : 0);
  }

  return {
    mode: currentMode,
    poll_interval_ms: interval,
    valve_inverted,
    valve_timings,
    valve_default_state,
    manual_states,
    dump_sequence: readSequence('dump-seq'),
    idle_sequence: readSequence('idle-seq'),
    alternance,
  };
}

function addStep(containerId) {
  const container = document.getElementById(containerId);
  const existing  = container.querySelectorAll('.seq-step').length;
  appendStepRow(container, existing, { valve_index: 0, state: 1, delay_after_ms: 0 }, null);
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

// ---------------------------------------------------------------------------
// Task control
// ---------------------------------------------------------------------------
async function taskAction(action) {
  try {
    const res = await fetch(`/api/task/${action}`, { method: 'POST' });
    const data = await res.json();
    if (!res.ok) {
      alert(data.error ?? `${action} failed`);
    }

    // In manual mode on start, push the current toggle states to the backend
    // so the GPIOs match what the UI shows.
    if (action === 'start' && currentMode === 'manual' && currentConfig) {
      // Sensor toggles
      for (const role of ['drive', 'read']) {
        const toggle = document.getElementById(`manual-toggle-sensor-${role}`);
        if (toggle) {
          await fetch('/api/gpio/set-sensor', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ pin: role, state: toggle.checked ? 1 : 0 }),
          });
        }
      }
      // Valve toggles
      const valves = currentConfig.valve_gpios ?? [];
      for (let i = 0; i < valves.length; i++) {
        const toggle = document.getElementById(`manual-toggle-${i}`);
        if (toggle) {
          await fetch('/api/gpio/set', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ valve_index: i, state: toggle.checked ? 1 : 0 }),
          });
        }
      }
    }

    await refreshStatus();
  } catch (e) {
    alert('Network error: ' + e.message);
  }
}

async function refreshStatus() {
  try {
    const res  = await fetch('/api/task/status');
    const data = await res.json();
    applyStatus(data.running, data.gpio_states, data.mode);
  } catch (_) {}
}

function applyStatus(running, gpioStates, mode) {
  const badge = document.getElementById('task-badge');
  const dot   = document.getElementById('task-dot');
  const label = document.getElementById('task-label');
  const btnStart = document.getElementById('btn-start');
  const btnStop  = document.getElementById('btn-stop');

  if (running) {
    badge.className = 'badge badge-running';
    dot.className   = 'dot dot-green';
    label.textContent = 'Running';
    btnStart.disabled = true;
    btnStop.disabled  = false;
  } else {
    badge.className = 'badge badge-stopped';
    dot.className   = 'dot dot-red';
    label.textContent = 'Stopped';
    btnStart.disabled = false;
    btnStop.disabled  = true;
  }

  // Mode badge
  const modeBadge = document.getElementById('mode-badge');
  if (modeBadge) {
    const m = mode ?? currentMode;
    modeBadge.dataset.mode = m;
    modeBadge.textContent = m.charAt(0).toUpperCase() + m.slice(1);
  }

  if (gpioStates) lastGpioStates = gpioStates;

  if (currentConfig) {
    renderGpioGrid(currentConfig, gpioStates ?? {});
    updateDiagram(currentConfig, gpioStates ?? {}, running);
    // Only sync manual toggles from GPIO when the task is running in manual
    // mode — otherwise preserve the user's toggle choices.
    if (running && currentMode === 'manual') {
      updateManualToggles(currentConfig, gpioStates ?? {});
    }
  }
}

// ---------------------------------------------------------------------------
// GPIO grid
// ---------------------------------------------------------------------------
function renderGpioGrid(cfg, states) {
  const grid = document.getElementById('gpio-grid');
  grid.innerHTML = '';

  const label = cfg.sensor_label ?? 'Sensor';
  grid.appendChild(makeGpioItem('Sensor (drive)', label, cfg.sensor_drive_gpio, states[`gpio_${cfg.sensor_drive_gpio}`]));
  grid.appendChild(makeGpioItem('Sensor (read)',  label, cfg.sensor_read_gpio,  states[`gpio_${cfg.sensor_read_gpio}`]));

  const valves  = cfg.valve_gpios  ?? [];
  const vlabels = cfg.valve_labels ?? [];
  valves.forEach((pin, i) => {
    grid.appendChild(makeGpioItem(
      'Valve',
      vlabels[i] ?? `Valve ${i + 1}`,
      pin,
      states[`gpio_${pin}`],
    ));
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
    <span class="gpio-role">${escapeHTML(role)}</span>
    <span class="gpio-label">${escapeHTML(label)}</span>
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

  let stateClass, stateText;
  if (value === 1) {
    stateClass = 'state-high'; stateText = 'HIGH';
  } else {
    stateClass = 'state-low'; stateText = 'LOW';
  }
  stateEl.className = `gpio-state ${stateClass}`;
  stateEl.textContent = stateText;
}

// ---------------------------------------------------------------------------
// SSE connection
// ---------------------------------------------------------------------------
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
      applyStatus(data.running, data.gpio_states, data.mode);

      // Granular pin updates (avoid full re-render on every tick)
      const states = data.gpio_states ?? {};
      Object.entries(states).forEach(([key, val]) => {
        const pin = parseInt(key.replace('gpio_', ''), 10);
        updateGpioItem(pin, val);
      });
      updateDiagram(currentConfig, states, data.running);
    } catch (_) {}
  };

  sseSource.onerror = () => {
    indicator.textContent = 'Disconnected — reconnecting…';
    indicator.style.color = 'var(--yellow)';
    sseSource.close();
    setTimeout(connectSSE, 3000);
  };
}

// ---------------------------------------------------------------------------
// System diagram
// ---------------------------------------------------------------------------

// Maps config.json label substrings (lowercase) → diagram element IDs.
// Keeps the diagram decoupled from exact pin numbers.
const VALVE_DIAGRAM_MAP = [
  { match: 'air',        valve: 'd-valve-air',     pipe: 'd-pipe-air',     label: 'd-label-air'     },
  { match: 'vacuum',     valve: 'd-valve-vacuum',  pipe: 'd-pipe-vacuum',  label: 'd-label-vacuum'  },
  { match: 'water pump', valve: 'd-valve-wp',      pipe: 'd-pipe-wp-valve',label: 'd-label-wp-valve'},
  { match: 'maple',      valve: 'd-valve-maple',   pipe: 'd-pipe-maple',   label: 'd-label-maple'   },
  { match: 'pump',       pump:  'd-pump',          pipe: 'd-pipe-pump',    label: 'd-label-pump', icon: 'd-pump-icon' },
];

function updateDiagram(cfg, states, running) {
  if (!cfg) return;

  const sensorReadPin = cfg.sensor_read_gpio;
  const valvePins     = cfg.valve_gpios  ?? [];
  const valveLabels   = cfg.valve_labels ?? [];

  // ── Sensor ───────────────────────────────────────────────────────────────
  const sensorVal     = states[`gpio_${sensorReadPin}`];
  const circuitClosed = sensorVal === 1;  // water at top
  _diagClass('d-sensor-top', circuitClosed ? 'on' : '');
  _diagClass('d-label-sensor-top', circuitClosed ? 'on' : '');
  _diagClass('d-sensor-bot', !circuitClosed && running ? 'on' : '');
  _diagClass('d-label-sensor-bot', !circuitClosed && running ? 'on' : '');

  // Water level animation
  const topTriggered = circuitClosed;

  const waterEl = document.getElementById('d-water');
  if (waterEl) {
    let waterY, waterH;
    if (!running)         { waterY = 288; waterH = 10;  }  // stopped — empty
    else if (topTriggered){ waterY = 84;  waterH = 214; }  // top sensor on — tank full
    else                  { waterY = 260; waterH = 38;  }  // idle — tank low
    waterEl.setAttribute('y', waterY);
    waterEl.setAttribute('height', waterH);
  }

  // ── Valves & pump ────────────────────────────────────────────────────────
  valvePins.forEach((pin, i) => {
    const label = (valveLabels[i] ?? '').toLowerCase();
    const val   = states[`gpio_${pin}`];
    const isOn  = val === 1;

    // Find matching diagram entry
    const entry = VALVE_DIAGRAM_MAP.find(e => {
      if (e.match === 'pump')       return label === 'water pump';
      if (e.match === 'water pump') return label.includes('water pump') && label !== 'water pump';
      return label.includes(e.match);
    });
    if (!entry) return;

    if (entry.pump) {
      _diagClass(entry.pump,  isOn ? 'active' : '');
      _diagClass(entry.label, isOn ? 'pump-active' : '');
      _diagClass(entry.pipe,  isOn ? 'active' : '');
      const icon = document.getElementById(entry.icon);
      if (icon) icon.style.fill = isOn ? '#22c55e' : '#475569';
    } else {
      _diagClass(entry.valve, isOn ? 'open' : (val === 0 ? 'closed' : ''));
      _diagClass(entry.label, isOn ? 'open' : (val === 0 ? 'closed' : ''));
      _diagClass(entry.pipe,  isOn ? 'active' : '');
    }
  });

  // ── Phase banner ─────────────────────────────────────────────────────────
  const phase = document.getElementById('d-phase');
  if (phase) {
    if (!running) {
      phase.textContent = 'Stopped';
      phase.className   = 'idle';
    } else if (topTriggered) {
      phase.textContent = 'Dumping…';
      phase.className   = 'dumping';
    } else {
      phase.textContent = 'Idle — waiting for top sensor';
      phase.className   = 'idle';
    }
  }
}

function _diagClass(id, cls) {
  const el = document.getElementById(id);
  if (!el) return;
  // Remove all state classes then add the new one
  el.classList.remove('open', 'closed', 'active', 'on', 'pump-active');
  if (cls) el.classList.add(cls);
}

// ---------------------------------------------------------------------------
// UI helpers
// ---------------------------------------------------------------------------
function showFeedback(type, msg) {
  const el = document.getElementById('config-feedback');
  el.className = `feedback ${type}`;
  el.textContent = msg;
  if (type === 'ok') setTimeout(() => { el.className = 'feedback'; }, 4000);
}

function escapeHTML(str) {
  return String(str)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
