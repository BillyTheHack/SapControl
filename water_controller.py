"""
water_controller.py - Background GPIO monitoring and valve control task.

This module runs in a background thread managed by app.py.
GPIO state is exposed via the `get_gpio_states()` function so the web UI
can poll it. Configuration is reloaded each time the task starts.

--- YOUR IMPLEMENTATION ZONE ---
Look for the two comments:
    # >>> YOUR LOGIC HERE <<<
Those are the only places you need to add code.
"""

import threading
import time
import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# GPIO abstraction — swaps in a mock when RPi.GPIO is unavailable (dev mode)
# ---------------------------------------------------------------------------
try:
    import RPi.GPIO as GPIO
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    _MOCK = False
    logger.info("RPi.GPIO loaded — running on real hardware")
except ImportError:
    logger.warning("RPi.GPIO not found — using mock GPIO (development mode)")
    _MOCK = True

    class _MockGPIO:
        BCM = OUT = IN = HIGH = LOW = 0
        PUD_DOWN = PUD_UP = 1

        def __init__(self):
            self._pins: dict[int, int] = {}

        def setmode(self, mode): pass
        def setwarnings(self, flag): pass

        def setup(self, pin, direction, pull_up_down=None, initial=None):
            self._pins[pin] = initial if initial is not None else 0

        def input(self, pin) -> int:
            return self._pins.get(pin, 0)

        def output(self, pin, value):
            self._pins[pin] = value
            logger.debug(f"[MockGPIO] pin {pin} → {value}")

        def cleanup(self, pins=None):
            if pins:
                for p in (pins if hasattr(pins, '__iter__') else [pins]):
                    self._pins.pop(p, None)
            else:
                self._pins.clear()

    GPIO = _MockGPIO()


# ---------------------------------------------------------------------------
# Internal state — read by the Flask app via get_gpio_states()
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_gpio_states: dict[str, int] = {}   # {"gpio_17": 0, "gpio_27": 1, ...}
_running = False
_thread: threading.Thread | None = None


def get_gpio_states() -> dict[str, int]:
    """Return a snapshot of all monitored GPIO pin states (thread-safe)."""
    with _lock:
        return dict(_gpio_states)


def is_running() -> bool:
    return _running


# ---------------------------------------------------------------------------
# Task lifecycle
# ---------------------------------------------------------------------------
def start(config: dict) -> bool:
    """
    Start the background task with the given config dict.
    Returns False if already running.

    Expected config keys (set in config.json):
        sensor_gpios    : list  — BCM pin numbers for water-level sensors
        valve_gpios     : list  — BCM pin numbers for relay-controlled valves
        poll_interval_ms: int   — how often to sample GPIO (default 500)
    """
    global _running, _thread

    if _running:
        logger.warning("start() called but task is already running")
        return False

    _running = True
    _thread = threading.Thread(target=_run, args=(config,), daemon=True)
    _thread.start()
    logger.info("Background task started")
    return True


def stop() -> bool:
    """Signal the background task to stop. Returns False if not running."""
    global _running

    if not _running:
        logger.warning("stop() called but task is not running")
        return False

    _running = False
    logger.info("Background task stop requested")
    return True


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def _run(config: dict):
    global _running

    sensor_pins: list[int] = config["sensor_gpios"]
    valve_pins: list[int] = config["valve_gpios"]
    interval: float = config.get("poll_interval_ms", 500) / 1000.0

    # --- GPIO setup ---------------------------------------------------------
    for pin in sensor_pins:
        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
    for pin in valve_pins:
        GPIO.setup(pin, GPIO.OUT, initial=GPIO.LOW)

    logger.info(
        f"Task running — sensors: {['GPIO' + str(p) for p in sensor_pins]}, "
        f"valves: {['GPIO' + str(p) for p in valve_pins]}, "
        f"interval: {interval}s"
    )

    try:
        while _running:
            sensor_values = [GPIO.input(p) for p in sensor_pins]
            valve_values  = [GPIO.input(p) for p in valve_pins]

            # Update shared state (read by web UI)
            with _lock:
                for pin, val in zip(sensor_pins, sensor_values):
                    _gpio_states[f"gpio_{pin}"] = val
                for pin, val in zip(valve_pins, valve_values):
                    _gpio_states[f"gpio_{pin}"] = val

            # ----------------------------------------------------------------
            # >>> YOUR LOGIC HERE <<<
            #
            # You have access to:
            #   sensor_values       — list of int (0 or 1), one per sensor pin
            #   sensor_pins         — list of int, BCM pin numbers for sensors
            #   valve_pins          — list of int, BCM pin numbers for valves
            #   config              — full config dict from config.json
            #
            # To control a valve relay:
            #   GPIO.output(valve_pins[0], GPIO.HIGH)   # open / energise
            #   GPIO.output(valve_pins[0], GPIO.LOW)    # close / de-energise
            #
            # Example skeleton (single sensor):
            #   if sensor_values[0] == 1:   # sensor triggered
            #       GPIO.output(valve_pins[0], GPIO.HIGH)
            #   else:
            #       GPIO.output(valve_pins[0], GPIO.LOW)
            # ----------------------------------------------------------------

            time.sleep(interval)

    except Exception:
        logger.exception("Unhandled exception in background task")
    finally:
        # De-energise all valves and release pins on exit
        for pin in valve_pins:
            try:
                GPIO.output(pin, GPIO.LOW)
            except Exception:
                pass
        GPIO.cleanup(sensor_pins + valve_pins)
        _running = False
        logger.info("Background task stopped, GPIO cleaned up")
