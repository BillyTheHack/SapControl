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
    "sensor_gpios": [17],
    "valve_gpios": [27, 22],
    "sensor_labels": ["Water Level Sensor"],
    "valve_labels": ["Inlet Valve", "Outlet Valve"],
    "poll_interval_ms": 500,
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

    The web UI may only submit poll_interval_ms.  All pin/label fields come
    from config.json directly and are kept as-is.
    """
    try:
        existing = load_config()
        interval = int(data.get("poll_interval_ms", existing.get("poll_interval_ms", 500)))

        if not (100 <= interval <= 10000):
            return None, "poll_interval_ms must be between 100 and 10000"

        # Preserve all pin/label fields from the existing config unchanged
        return {
            "sensor_gpios":        existing["sensor_gpios"],
            "valve_gpios":         existing["valve_gpios"],
            "sensor_labels":       existing["sensor_labels"],
            "valve_labels":        existing["valve_labels"],
            "default_valve_states": existing.get("default_valve_states",
                                        [0] * len(existing["valve_gpios"])),
            "poll_interval_ms": interval,
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
