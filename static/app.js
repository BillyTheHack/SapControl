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

  const table = document.getElementById('pin-table');
  table.innerHTML = '';

  const sensors = cfg.sensor_gpios  ?? [];
  const slabels = cfg.sensor_labels ?? [];
  sensors.forEach((pin, i) => {
    table.appendChild(makePinRow('Sensor', pin, slabels[i] ?? `Sensor ${i + 1}`));
  });

  const valves  = cfg.valve_gpios  ?? [];
  const vlabels = cfg.valve_labels ?? [];
  valves.forEach((pin, i) => {
    table.appendChild(makePinRow('Valve', pin, vlabels[i] ?? `Valve ${i + 1}`));
  });
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
  // Only poll_interval_ms is user-editable; everything else is preserved server-side.
  const interval = parseInt(document.getElementById('poll-interval').value, 10);
  return { poll_interval_ms: interval };
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

  const sensors = cfg.sensor_gpios  ?? [];
  const slabels = cfg.sensor_labels ?? [];
  sensors.forEach((pin, i) => {
    grid.appendChild(makeGpioItem(
      'Sensor',
      slabels[i] ?? `Sensor ${i + 1}`,
      pin,
      states[`gpio_${pin}`],
    ));
  });

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

  const sensorPins  = cfg.sensor_gpios  ?? [];
  const valvePins   = cfg.valve_gpios   ?? [];
  const valveLabels = cfg.valve_labels  ?? [];
  const sensorLabels= cfg.sensor_labels ?? [];

  // ── Sensors ──────────────────────────────────────────────────────────────
  const sensorIds = ['d-sensor-top', 'd-sensor-bot'];
  const labelIds  = ['d-label-sensor-top', 'd-label-sensor-bot'];
  sensorPins.forEach((pin, i) => {
    const val = states[`gpio_${pin}`];
    const on  = val === 1;
    _diagClass(sensorIds[i],  on ? 'on' : '');
    _diagClass(labelIds[i],   on ? 'on' : '');
  });

  // Determine fill phase from sensor states for water level animation
  const topTriggered = states[`gpio_${sensorPins[0]}`] === 1;
  const botTriggered = states[`gpio_${sensorPins[1]}`] === 1;

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

