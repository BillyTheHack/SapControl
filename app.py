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
    "sensor_drive_gpio": 8,
    "sensor_read_gpio": 7,
    "valve_gpios": [27, 22],
    "sensor_label": "Water Level Sensor",
    "valve_labels": ["Inlet Valve", "Outlet Valve"],
    "poll_interval_ms": 500,
    "valve_timings": [
        {"open_ms": 500, "close_ms": 500},
        {"open_ms": 500, "close_ms": 500},
    ],
    "fill_sequence": [
        {"valve_index": 0, "state": 1, "delay_after_ms": 0},
        {"valve_index": 1, "state": 0, "delay_after_ms": 0},
    ],
    "idle_sequence": [
        {"valve_index": 0, "state": 0, "delay_after_ms": 0},
        {"valve_index": 1, "state": 1, "delay_after_ms": 0},
    ],
}


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
        poll_interval_ms  – int 100-10000
        valve_timings     – list of {open_ms, close_ms} per valve (int 0-30000)
        fill_sequence     – list of {valve_index, state, delay_after_ms}
        idle_sequence     – list of {valve_index, state, delay_after_ms}
    """
    try:
        existing = load_config()
        n_valves = len(existing["valve_gpios"])

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
                if not (0 <= delay <= 30000):
                    return None, f"{name}[{i}].delay_after_ms must be 0-30000"
                result.append({"valve_index": vi, "state": state, "delay_after_ms": delay})
            return result, None

        raw_fill = data.get("fill_sequence", existing.get("fill_sequence", []))
        fill_seq, err = _validate_sequence(raw_fill, "fill_sequence")
        if err:
            return None, err

        raw_idle = data.get("idle_sequence", existing.get("idle_sequence", []))
        idle_seq, err = _validate_sequence(raw_idle, "idle_sequence")
        if err:
            return None, err

        return {
            "sensor_drive_gpio": existing["sensor_drive_gpio"],
            "sensor_read_gpio":  existing["sensor_read_gpio"],
            "valve_gpios":       existing["valve_gpios"],
            "sensor_label":      existing["sensor_label"],
            "valve_labels":      existing["valve_labels"],
            "poll_interval_ms":  interval,
            "valve_timings":     valve_timings,
            "fill_sequence":     fill_seq,
            "idle_sequence":     idle_seq,
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
    if water_controller.is_running():
        water_controller.stop()

    data = request.get_json(force=True, silent=True)
    if data is None:
        return jsonify({"error": "Invalid JSON body"}), 400

    config, error = validate_config(data)
    if error:
        return jsonify({"error": error}), 422

    save_config(config)
    return jsonify({"ok": True, "config": config})


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
        "gpio_states": water_controller.get_gpio_states(),
    })


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
        last_states: dict = {}
        last_heartbeat = time.monotonic()

        while True:
            states = water_controller.get_gpio_states()
            running = water_controller.is_running()
            now = time.monotonic()

            payload = {"running": running, "gpio_states": states}

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
    # host="0.0.0.0" makes it reachable from the local network (other devices
    # on the same LAN as the Pi).  Change to "127.0.0.1" for local-only.
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
