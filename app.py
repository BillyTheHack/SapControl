"""
app.py - Flask web server for the water-level GPIO controller.

Endpoints:
    GET  /                      Serve the single-page UI
    GET  /api/config            Return current config
    POST /api/config            Save new config (stops task if running)
    POST /api/task/start        Start the background task
    POST /api/task/stop         Stop the background task
    GET  /api/task/status       Running state + current GPIO values
    GET  /api/gpio/stream       SSE stream of GPIO state updates
    POST /api/gpio/set          Set a single valve (manual mode only)
    POST /api/gpio/set-sensor   Set a sensor pin (manual mode only)
"""

import json
import logging
import os
import time
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request

import water_controller

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
app = Flask(__name__)

CONFIG_PATH = Path(__file__).parent / "config.json"

DEFAULT_CONFIG = {
    "mode": "sequence",
    "sensor_drive_gpio": 8,
    "sensor_read_gpio": 7,
    "valve_gpios": [27, 22],
    "sensor_label": "Water Level Sensor",
    "valve_labels": ["Inlet Valve", "Outlet Valve"],
    "poll_interval_ms": 500,
    "valve_inverted": True,
    "valve_timings": [
        {"open_ms": 500, "close_ms": 500},
        {"open_ms": 500, "close_ms": 500},
    ],
    "dump_sequence": [
        {"valve_index": 0, "state": 1, "delay_after_ms": 0},
        {"valve_index": 1, "state": 0, "delay_after_ms": 0},
    ],
    "idle_sequence": [
        {"valve_index": 0, "state": 0, "delay_after_ms": 0},
        {"valve_index": 1, "state": 1, "delay_after_ms": 0},
    ],
    "valve_default_state": [0, 0],
    "alternance": {
        "sequence_a": [],
        "sequence_b": [],
        "delay_a_to_b_ms": 5000,
        "delay_b_to_a_ms": 5000,
    },
}

VALID_MODES = ("sequence", "alternance", "manual")


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------
def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH) as f:
                return json.load(f)
        except Exception:
            logger.exception("Failed to read config.json, using defaults")
    return dict(DEFAULT_CONFIG)


def save_config(config: dict) -> None:
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)
    logger.info("Config saved to %s", CONFIG_PATH)


def validate_config(data: dict) -> tuple[dict | None, str | None]:
    """Return (cleaned_config, error_message).

    Pin/label fields are read-only from the UI and always preserved from
    config.json.  The web UI may submit:
        mode              – "sequence", "alternance", or "manual"
        poll_interval_ms  – int 100-10000
        valve_timings     – list of {open_ms, close_ms} per valve (int 0-30000)
        dump_sequence     – list of {valve_index, state, delay_after_ms}
        idle_sequence     – list of {valve_index, state, delay_after_ms}
        alternance        – {sequence_a, sequence_b, delay_a_to_b_ms, delay_b_to_a_ms}
    """
    try:
        existing = load_config()
        n_valves = len(existing["valve_gpios"])

        # --- mode -------------------------------------------------------------
        mode = data.get("mode", existing.get("mode", "sequence"))
        if mode not in VALID_MODES:
            return None, f"mode must be one of {VALID_MODES}"

        # --- valve_inverted --------------------------------------------------
        valve_inverted = bool(data.get("valve_inverted", existing.get("valve_inverted", True)))

        # --- poll_interval_ms ------------------------------------------------
        interval = int(data.get("poll_interval_ms", existing.get("poll_interval_ms", 500)))
        if not (100 <= interval <= 10000):
            return None, "poll_interval_ms must be between 100 and 10000"

        # --- valve_timings ---------------------------------------------------
        raw_timings = data.get("valve_timings", existing.get("valve_timings", []))
        if len(raw_timings) != n_valves:
            return None, f"valve_timings must have {n_valves} entries (one per valve)"
        valve_timings = []
        for i, t in enumerate(raw_timings):
            open_ms  = int(t.get("open_ms",  0))
            close_ms = int(t.get("close_ms", 0))
            if not (0 <= open_ms  <= 30000):
                return None, f"valve_timings[{i}].open_ms must be 0-30000"
            if not (0 <= close_ms <= 30000):
                return None, f"valve_timings[{i}].close_ms must be 0-30000"
            valve_timings.append({"open_ms": open_ms, "close_ms": close_ms})

        # --- sequence helper -------------------------------------------------
        def _validate_sequence(seq, name):
            if not isinstance(seq, list):
                return None, f"{name} must be a list"
            result = []
            for i, step in enumerate(seq):
                vi    = int(step.get("valve_index", -1))
                state = int(step.get("state", 0))
                delay = int(step.get("delay_after_ms", 0))
                if not (0 <= vi < n_valves):
                    return None, f"{name}[{i}].valve_index {vi} out of range 0-{n_valves-1}"
                if state not in (0, 1):
                    return None, f"{name}[{i}].state must be 0 or 1"
                if not (0 <= delay <= 300000):
                    return None, f"{name}[{i}].delay_after_ms must be 0-300000"
                result.append({"valve_index": vi, "state": state, "delay_after_ms": delay})
            return result, None

        raw_dump = data.get("dump_sequence", existing.get("dump_sequence", []))
        dump_seq, err = _validate_sequence(raw_dump, "dump_sequence")
        if err:
            return None, err

        raw_idle = data.get("idle_sequence", existing.get("idle_sequence", []))
        idle_seq, err = _validate_sequence(raw_idle, "idle_sequence")
        if err:
            return None, err

        # --- alternance -------------------------------------------------------
        existing_alt = existing.get("alternance", {})
        raw_alt = data.get("alternance", existing_alt)

        raw_seq_a = raw_alt.get("sequence_a", existing_alt.get("sequence_a", []))
        seq_a, err = _validate_sequence(raw_seq_a, "alternance.sequence_a")
        if err:
            return None, err

        raw_seq_b = raw_alt.get("sequence_b", existing_alt.get("sequence_b", []))
        seq_b, err = _validate_sequence(raw_seq_b, "alternance.sequence_b")
        if err:
            return None, err

        delay_a = int(raw_alt.get("delay_a_to_b_ms", existing_alt.get("delay_a_to_b_ms", 5000)))
        delay_b = int(raw_alt.get("delay_b_to_a_ms", existing_alt.get("delay_b_to_a_ms", 5000)))
        if not (0 <= delay_a <= 300000):
            return None, "alternance.delay_a_to_b_ms must be 0-300000"
        if not (0 <= delay_b <= 300000):
            return None, "alternance.delay_b_to_a_ms must be 0-300000"

        alternance = {
            "sequence_a": seq_a,
            "sequence_b": seq_b,
            "delay_a_to_b_ms": delay_a,
            "delay_b_to_a_ms": delay_b,
        }

        # --- valve_default_state -----------------------------------------------
        raw_defaults = data.get("valve_default_state", existing.get("valve_default_state", [0] * n_valves))
        if len(raw_defaults) != n_valves:
            return None, f"valve_default_state must have {n_valves} entries (one per valve)"
        valve_default_state = []
        for i, v in enumerate(raw_defaults):
            s = int(v)
            if s not in (0, 1):
                return None, f"valve_default_state[{i}] must be 0 or 1"
            valve_default_state.append(s)

        # --- manual_states --------------------------------------------------------
        raw_manual = data.get("manual_states", existing.get("manual_states", [0] * n_valves))
        if len(raw_manual) != n_valves:
            return None, f"manual_states must have {n_valves} entries (one per valve)"
        manual_states = []
        for i, v in enumerate(raw_manual):
            s = int(v)
            if s not in (0, 1):
                return None, f"manual_states[{i}] must be 0 or 1"
            manual_states.append(s)

        return {
            "mode":              mode,
            "sensor_drive_gpio": existing["sensor_drive_gpio"],
            "sensor_read_gpio":  existing["sensor_read_gpio"],
            "valve_gpios":       existing["valve_gpios"],
            "sensor_label":      existing["sensor_label"],
            "valve_labels":      existing["valve_labels"],
            "poll_interval_ms":  interval,
            "valve_inverted":    valve_inverted,
            "valve_timings":     valve_timings,
            "dump_sequence":     dump_seq,
            "idle_sequence":     idle_seq,
            "valve_default_state": valve_default_state,
            "manual_states":     manual_states,
            "alternance":        alternance,
        }, None

    except (KeyError, TypeError, ValueError) as exc:
        return None, f"Invalid config: {exc}"


# ---------------------------------------------------------------------------
# Routes — UI
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


# ---------------------------------------------------------------------------
# Routes — Config
# ---------------------------------------------------------------------------
@app.route("/api/config", methods=["GET"])
def get_config():
    return jsonify(load_config())


@app.route("/api/config", methods=["POST"])
def post_config():
    data = request.get_json(force=True, silent=True)
    if data is None:
        return jsonify({"error": "Invalid JSON body"}), 400

    config, error = validate_config(data)
    if error:
        return jsonify({"error": error}), 422

    # Stop the task unless we're staying in manual mode
    if water_controller.is_running():
        new_mode = config.get("mode", "sequence")
        if not (water_controller.get_mode() == "manual" and new_mode == "manual"):
            water_controller.stop()

    save_config(config)
    return jsonify({"ok": True, "config": config})


@app.route("/api/manual-states", methods=["POST"])
def post_manual_states():
    """Save manual toggle states to config without stopping the task."""
    data = request.get_json(force=True, silent=True)
    if data is None:
        return jsonify({"error": "Invalid JSON body"}), 400

    raw = data.get("manual_states")
    if not isinstance(raw, list):
        return jsonify({"error": "manual_states must be a list"}), 422

    config = load_config()
    n_valves = len(config.get("valve_gpios", []))
    if len(raw) != n_valves:
        return jsonify({"error": f"manual_states must have {n_valves} entries"}), 422

    manual_states = []
    for i, v in enumerate(raw):
        s = int(v)
        if s not in (0, 1):
            return jsonify({"error": f"manual_states[{i}] must be 0 or 1"}), 422
        manual_states.append(s)

    config["manual_states"] = manual_states
    save_config(config)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Routes — Task control
# ---------------------------------------------------------------------------
@app.route("/api/task/start", methods=["POST"])
def task_start():
    config = load_config()
    started = water_controller.start(config)
    if not started:
        return jsonify({"error": "Task is already running"}), 409
    return jsonify({"ok": True, "running": True})


@app.route("/api/task/stop", methods=["POST"])
def task_stop():
    stopped = water_controller.stop()
    if not stopped:
        return jsonify({"error": "Task is not running"}), 409
    return jsonify({"ok": True, "running": False})


@app.route("/api/task/status", methods=["GET"])
def task_status():
    return jsonify({
        "running": water_controller.is_running(),
        "mode": water_controller.get_mode(),
        "gpio_states": water_controller.get_gpio_states(),
    })


# ---------------------------------------------------------------------------
# Routes — Manual valve control
# ---------------------------------------------------------------------------
@app.route("/api/gpio/set", methods=["POST"])
def gpio_set():
    """Set a single valve in manual mode.  Body: {valve_index: int, state: 0|1}"""
    if not water_controller.is_running():
        return jsonify({"error": "Task is not running"}), 409
    if water_controller.get_mode() != "manual":
        return jsonify({"error": "Manual valve control is only available in manual mode"}), 409

    data = request.get_json(force=True, silent=True)
    if data is None:
        return jsonify({"error": "Invalid JSON body"}), 400

    try:
        vi = int(data["valve_index"])
        state = int(data["state"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "Required: valve_index (int), state (0 or 1)"}), 422

    cfg = load_config()
    n_valves = len(cfg.get("valve_gpios", []))
    if not (0 <= vi < n_valves):
        return jsonify({"error": f"valve_index out of range 0-{n_valves - 1}"}), 422
    if state not in (0, 1):
        return jsonify({"error": "state must be 0 or 1"}), 422

    water_controller.set_manual_valve(vi, state)
    return jsonify({"ok": True})


@app.route("/api/gpio/set-sensor", methods=["POST"])
def gpio_set_sensor():
    """Set a sensor pin in manual mode.  Body: {pin: "drive"|"read", state: 0|1}"""
    if not water_controller.is_running():
        return jsonify({"error": "Task is not running"}), 409
    if water_controller.get_mode() != "manual":
        return jsonify({"error": "Sensor control is only available in manual mode"}), 409

    data = request.get_json(force=True, silent=True)
    if data is None:
        return jsonify({"error": "Invalid JSON body"}), 400

    pin_role = data.get("pin")
    if pin_role not in ("drive", "read"):
        return jsonify({"error": "pin must be 'drive' or 'read'"}), 422

    try:
        state = int(data["state"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "Required: state (0 or 1)"}), 422

    if state not in (0, 1):
        return jsonify({"error": "state must be 0 or 1"}), 422

    water_controller.set_manual_sensor(pin_role, state)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Routes — SSE stream
# ---------------------------------------------------------------------------
@app.route("/api/gpio/stream")
def gpio_stream():
    """
    Server-Sent Events endpoint.
    Pushes a JSON object of GPIO states whenever they change,
    and a heartbeat every 5 s to keep the connection alive.
    """
    def event_generator():
        # Always send current state immediately on connect
        states = water_controller.get_gpio_states()
        running = water_controller.is_running()
        mode = water_controller.get_mode()
        payload = {"running": running, "mode": mode, "gpio_states": states}
        yield f"data: {json.dumps(payload)}\n\n"

        last_states = dict(states)
        last_heartbeat = time.monotonic()

        while True:
            states = water_controller.get_gpio_states()
            running = water_controller.is_running()
            now = time.monotonic()

            mode = water_controller.get_mode()
            payload = {"running": running, "mode": mode, "gpio_states": states}

            if states != last_states or now - last_heartbeat >= 5:
                yield f"data: {json.dumps(payload)}\n\n"
                last_states = dict(states)
                last_heartbeat = now

            time.sleep(0.2)

    return Response(
        event_generator(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # disable nginx buffering if behind proxy
        },
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # Apply default valve state before starting the server so valves are
    # in a safe state even before the water controller task is started.
    water_controller.apply_initial_default_state(load_config())

    # host="0.0.0.0" makes it reachable from the local network (other devices
    # on the same LAN as the Pi).  Change to "127.0.0.1" for local-only.
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
