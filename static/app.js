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

