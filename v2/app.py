"""
app.py — Flask web server for the water-level GPIO controller (v2).

Slim orchestrator: routes delegate to Controller and ConfigManager.

Endpoints:
    GET  /                      Serve the single-page UI
    GET  /api/config            Return current config
    POST /api/config            Save config (stops task unless mode unchanged)
    POST /api/task/start        Start the background task
    POST /api/task/stop         Stop the background task
    GET  /api/task/status       Status snapshot
    GET  /api/gpio/stream       SSE stream of GPIO state updates
    POST /api/manual-override   Enable/disable manual override
    POST /api/gpio/set          Set a valve pin (manual override only)
    POST /api/gpio/set-sensor   Set a sensor pin (manual override only)
"""

import json
import logging
import time
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request

from config_manager import ConfigManager
from controller import Controller
from gpio_driver import GpioDriver

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
config_manager = ConfigManager(CONFIG_PATH)
gpio = GpioDriver()
controller = Controller(gpio)


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
    return jsonify(config_manager.load())


@app.route("/api/config", methods=["POST"])
def post_config():
    data = request.get_json(force=True, silent=True)
    if data is None:
        return jsonify({"error": "Invalid JSON body"}), 400

    config, error = config_manager.validate(data)
    if error:
        return jsonify({"error": error}), 422

    # Stop the task if mode changed or config changed while running
    if controller.is_running():
        controller.stop()

    config_manager.save(config)
    return jsonify({"ok": True, "config": config})


# ---------------------------------------------------------------------------
# Routes — Task control
# ---------------------------------------------------------------------------
@app.route("/api/task/start", methods=["POST"])
def task_start():
    config = config_manager.load()
    if not controller.start(config):
        return jsonify({"error": "Task is already running"}), 409
    return jsonify({"ok": True, "running": True})


@app.route("/api/task/stop", methods=["POST"])
def task_stop():
    if not controller.stop():
        return jsonify({"error": "Task is not running"}), 409
    return jsonify({"ok": True, "running": False})


@app.route("/api/task/status", methods=["GET"])
def task_status():
    return jsonify(controller.get_status())


# ---------------------------------------------------------------------------
# Routes — Manual override
# ---------------------------------------------------------------------------
@app.route("/api/manual-override", methods=["POST"])
def manual_override():
    data = request.get_json(force=True, silent=True)
    if data is None:
        return jsonify({"error": "Invalid JSON body"}), 400

    enabled = data.get("enabled")
    if not isinstance(enabled, bool):
        return jsonify({"error": "enabled must be a boolean"}), 422

    if not controller.is_running():
        return jsonify({"error": "Task is not running"}), 409

    if enabled:
        controller.enable_manual_override()
    else:
        controller.disable_manual_override()

    return jsonify({"ok": True, "manual_override": enabled})


@app.route("/api/gpio/set", methods=["POST"])
def gpio_set():
    """Set a single valve in manual override mode."""
    if not controller.is_running():
        return jsonify({"error": "Task is not running"}), 409
    if not controller.is_manual_override():
        return jsonify({"error": "Manual override is not active"}), 409

    data = request.get_json(force=True, silent=True)
    if data is None:
        return jsonify({"error": "Invalid JSON body"}), 400

    try:
        vi = int(data["valve_index"])
        state = int(data["state"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "Required: valve_index (int), state (0 or 1)"}), 422

    if state not in (0, 1):
        return jsonify({"error": "state must be 0 or 1"}), 422

    config = config_manager.load()
    if not controller.set_manual_valve(vi, state, config):
        return jsonify({"error": "Failed to set valve"}), 422

    return jsonify({"ok": True})


@app.route("/api/gpio/set-sensor", methods=["POST"])
def gpio_set_sensor():
    """Set a sensor pin in manual override mode."""
    if not controller.is_running():
        return jsonify({"error": "Task is not running"}), 409
    if not controller.is_manual_override():
        return jsonify({"error": "Manual override is not active"}), 409

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

    config = config_manager.load()
    if not controller.set_manual_sensor(pin_role, state, config):
        return jsonify({"error": "Sensor override not available"}), 422

    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Routes — SSE stream
# ---------------------------------------------------------------------------
@app.route("/api/gpio/stream")
def gpio_stream():
    """Server-Sent Events — pushes status on change or every 5s heartbeat."""

    def event_generator():
        last = controller.get_status()
        yield f"data: {json.dumps(last)}\n\n"
        last_heartbeat = time.monotonic()

        while True:
            current = controller.get_status()
            now = time.monotonic()

            if current != last or now - last_heartbeat >= 5:
                yield f"data: {json.dumps(current)}\n\n"
                last = current
                last_heartbeat = now

            time.sleep(0.2)

    return Response(
        event_generator(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    controller.apply_initial_default_state(config_manager.load())
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
