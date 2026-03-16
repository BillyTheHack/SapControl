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
  document.getElementById('sensor-label').value = cfg.sensor_label ?? '';
  document.getElementById('sensor-gpio').value  = cfg.sensor_gpio ?? 17;
  document.getElementById('poll-interval').value = cfg.poll_interval_ms ?? 500;

  const valves = cfg.valve_gpios ?? [];
  const labels = cfg.valve_labels ?? [];
  const list = document.getElementById('valve-list');
  list.innerHTML = '';
  valves.forEach((pin, i) => addValve(pin, labels[i] ?? `Valve ${i + 1}`));
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
  const sensorGpio  = parseInt(document.getElementById('sensor-gpio').value, 10);
  const sensorLabel = document.getElementById('sensor-label').value.trim() || 'Sensor';
  const interval    = parseInt(document.getElementById('poll-interval').value, 10);

  const valveEntries = document.querySelectorAll('.valve-entry');
  const valveGpios  = [];
  const valveLabels = [];

  for (const entry of valveEntries) {
    const pin   = parseInt(entry.querySelector('.valve-pin').value, 10);
    const label = entry.querySelector('.valve-label').value.trim() || `Valve ${valveGpios.length + 1}`;
    if (isNaN(pin)) {
      showFeedback('error', 'All valve GPIO pins must be valid numbers.');
      return null;
    }
    valveGpios.push(pin);
    valveLabels.push(label);
  }

  if (!valveGpios.length) {
    showFeedback('error', 'Add at least one valve.');
    return null;
  }

  return {
    sensor_gpio: sensorGpio,
    sensor_label: sensorLabel,
    valve_gpios: valveGpios,
    valve_labels: valveLabels,
    poll_interval_ms: interval,
  };
}

// ---------------------------------------------------------------------------
// Valve form rows
// ---------------------------------------------------------------------------
function addValve(pin = '', label = '') {
  const list = document.getElementById('valve-list');
  const div = document.createElement('div');
  div.className = 'valve-entry';

  const idx = list.children.length + 1;
  div.innerHTML = `
    <span class="small-label">Valve ${idx}</span>
    <input class="valve-label" type="text"   placeholder="Label"    value="${escapeAttr(label)}" />
    <input class="valve-pin"  type="number" placeholder="GPIO pin" value="${escapeAttr(String(pin))}" min="1" max="27" />
    <button class="btn-remove-valve" onclick="removeValve(this)" title="Remove">&#x2715;</button>
  `;
  list.appendChild(div);
}

function removeValve(btn) {
  btn.closest('.valve-entry').remove();
  // Re-number labels
  document.querySelectorAll('.valve-entry .small-label').forEach((el, i) => {
    el.textContent = `Valve ${i + 1}`;
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
  }
}

// ---------------------------------------------------------------------------
// GPIO grid
// ---------------------------------------------------------------------------
function renderGpioGrid(cfg, states) {
  const grid = document.getElementById('gpio-grid');
  grid.innerHTML = '';

  const sensorPin = cfg.sensor_gpio;
  grid.appendChild(makeGpioItem(
    'Sensor',
    cfg.sensor_label ?? 'Sensor',
    sensorPin,
    states[`gpio_${sensorPin}`],
  ));

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

function escapeAttr(str) {
  return String(str).replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
