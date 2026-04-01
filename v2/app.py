"""
app.py — Flask web server for the water-level GPIO controller (v2).

Slim orchestrator: routes delegate to Controller and ConfigManager.

Endpoints:
    GET  /                      Serve the single-page UI
    GET  /api/config            Return current config
    POST /api/config            Save config (stops task if running)
    POST /api/task/start        Start the background task
    POST /api/task/stop         Stop the background task
    GET  /api/task/status       Status snapshot
    GET  /api/gpio/stream       SSE stream of GPIO state updates
    POST /api/override/pin      Override or release a specific GPIO pin
    POST /api/override/clear    Release all pin overrides
"""

import json
import logging
import time
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request

from config_manager import ConfigManager, get_sensor_top, get_sensor_bottom, get_valve_pins
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

    was_running = controller.is_running()
    restart = data.get("_restart", False)

    if was_running:
        controller.stop()

    config_manager.save(config)

    restarted = False
    if restart and was_running:
        restarted = controller.start(config)

    return jsonify({"ok": True, "config": config, "restarted": restarted})


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
# Routes — Per-pin override
# ---------------------------------------------------------------------------
@app.route("/api/override/pin", methods=["POST"])
def override_pin():
    """Override or release a specific GPIO pin.

    Body: {pin: int, override: bool, state?: 0|1}
    - override=true + state: lock the pin and set it to this value
    - override=false: release the pin (mode runner regains control)
    """
    if not controller.is_running():
        return jsonify({"error": "Task is not running"}), 409

    data = request.get_json(force=True, silent=True)
    if data is None:
        return jsonify({"error": "Invalid JSON body"}), 400

    try:
        pin = int(data["pin"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "Required: pin (int)"}), 422

    override = data.get("override")
    if not isinstance(override, bool):
        return jsonify({"error": "override must be a boolean"}), 422

    # Validate pin is a known pin
    config = config_manager.load()
    sensor_top = get_sensor_top(config)
    sensor_bottom = get_sensor_bottom(config)
    valid_pins = get_valve_pins(config) + [
        sensor_top["drive_gpio"], sensor_top["read_gpio"],
        sensor_bottom["drive_gpio"], sensor_bottom["read_gpio"],
    ]
    if pin not in valid_pins:
        return jsonify({"error": f"GPIO {pin} is not a configured pin"}), 422

    if override:
        try:
            state = int(data["state"])
        except (KeyError, TypeError, ValueError):
            return jsonify({"error": "Required when overriding: state (0 or 1)"}), 422
        if state not in (0, 1):
            return jsonify({"error": "state must be 0 or 1"}), 422

        if not controller.override_pin(pin, state, config):
            return jsonify({"error": "Override failed"}), 422
    else:
        controller.release_pin(pin)

    return jsonify({"ok": True, "overridden_pins": controller.get_overridden_pins()})


@app.route("/api/override/clear", methods=["POST"])
def override_clear():
    """Release all pin overrides."""
    controller.release_all_overrides()
    return jsonify({"ok": True, "overridden_pins": []})


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
    _startup_config = config_manager.load()
    controller.apply_initial_default_state(_startup_config)
    if _startup_config.get("settings", {}).get("auto_start", False):
        logger.info("Auto-start enabled — starting task on boot")
        controller.start(_startup_config)
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
