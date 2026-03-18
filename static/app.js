/* app.js — Water Controller frontend */

'use strict';

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
let currentConfig = null;
let sseSource = null;

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------
document.addEventListener('DOMContentLoaded', async () => {
  await loadConfig();
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
  document.getElementById('poll-interval').value = cfg.poll_interval_ms ?? 500;

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

  // Sequences
  renderSequence('fill-seq', cfg.fill_sequence ?? [], cfg);
  renderSequence('idle-seq', cfg.idle_sequence ?? [], cfg);
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
    <select class="step-valve">${opts}</select>
    <select class="step-state">
      <option value="1" ${step.state === 1 ? 'selected' : ''}>Open (1)</option>
      <option value="0" ${step.state === 0 ? 'selected' : ''}>Close (0)</option>
    </select>
    <input type="number" class="step-delay" min="0" max="30000" step="50"
           value="${step.delay_after_ms ?? 0}" />
    <button class="btn-icon" title="Remove step" onclick="removeStep(this)">✕</button>
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
    renderGpioGrid(currentConfig, {});
    showFeedback('ok', 'Configuration saved.');
  } catch (e) {
    showFeedback('error', 'Network error: ' + e.message);
  }
}

function readFormConfig() {
  const interval = parseInt(document.getElementById('poll-interval').value, 10);

  // Valve timings
  const valve_timings = [];
  document.querySelectorAll('.timing-open').forEach((el, i) => {
    const closeEl = document.querySelector(`.timing-close[data-index="${i}"]`);
    valve_timings.push({
      open_ms:  parseInt(el.value, 10)       || 0,
      close_ms: parseInt(closeEl?.value, 10) || 0,
    });
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

  return {
    poll_interval_ms: interval,
    valve_timings,
    fill_sequence: readSequence('fill-seq'),
    idle_sequence: readSequence('idle-seq'),
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
    // SSE will update the status shortly; force a quick poll too
    await refreshStatus();
  } catch (e) {
    alert('Network error: ' + e.message);
  }
}

async function refreshStatus() {
  try {
    const res  = await fetch('/api/task/status');
    const data = await res.json();
    applyStatus(data.running, data.gpio_states);
  } catch (_) {}
}

function applyStatus(running, gpioStates) {
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

  if (currentConfig) {
    renderGpioGrid(currentConfig, gpioStates ?? {});
    updateDiagram(currentConfig, gpioStates ?? {}, running);
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
      applyStatus(data.running, data.gpio_states);

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
  // Circuit closed (HIGH) = water at top = filling about to start
  // Circuit open  (LOW)   = water dropped to bottom = draining
  const sensorVal     = states[`gpio_${sensorReadPin}`];
  const circuitClosed = sensorVal === 1;  // water at top
  _diagClass('d-sensor-top', circuitClosed ? 'on' : '');
  _diagClass('d-label-sensor-top', circuitClosed ? 'on' : '');
  _diagClass('d-sensor-bot', !circuitClosed && running ? 'on' : '');
  _diagClass('d-label-sensor-bot', !circuitClosed && running ? 'on' : '');

  // Water level animation: circuit closed = full, open = low
  const topTriggered = circuitClosed;
  const botTriggered = !circuitClosed && running;

  // Water level: low by default, mid when filling (top sensor on), full when bottom on
  const waterEl = document.getElementById('d-water');
  if (waterEl) {
    let waterY, waterH;
    if (!running)        { waterY = 288; waterH = 10; }  // empty-ish
    else if (botTriggered){ waterY = 84;  waterH = 214; } // full
    else if (topTriggered){ waterY = 150; waterH = 148; } // mid-fill
    else                  { waterY = 270; waterH = 28;  } // low
    waterEl.setAttribute('y', waterY);
    waterEl.setAttribute('height', waterH);
  }

  // ── Valves & pump ────────────────────────────────────────────────────────
  valvePins.forEach((pin, i) => {
    const label = (valveLabels[i] ?? '').toLowerCase();
    const val   = states[`gpio_${pin}`];
    const isOn  = val === 1;

    // Find matching diagram entry — "pump" substring must not match "water pump valve"
    const entry = VALVE_DIAGRAM_MAP.find(e => {
      if (e.match === 'pump')       return label === 'water pump';   // exact relay
      if (e.match === 'water pump') return label.includes('water pump valve') || label.includes('water pump v');
      return label.includes(e.match);
    });
    if (!entry) return;

    if (entry.pump) {
      // Pump relay
      _diagClass(entry.pump,  isOn ? 'active' : '');
      _diagClass(entry.label, isOn ? 'pump-active' : '');
      _diagClass(entry.pipe,  isOn ? 'active' : '');
      const icon = document.getElementById(entry.icon);
      if (icon) icon.style.fill = isOn ? '#22c55e' : '#475569';
    } else {
      // Regular valve
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
    } else if (topTriggered && !botTriggered) {
      phase.textContent = 'Filling…';
      phase.className   = 'filling';
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

