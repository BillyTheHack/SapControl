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
        BCM = OUT = IN = 0
        HIGH = 1
        LOW = 0
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
# Valve helpers
# ---------------------------------------------------------------------------
def _set_valves(valve_pins: list[int], states: list[int]) -> None:
    """Write a HIGH/LOW value to each valve pin. len(states) must equal len(valve_pins)."""
    for pin, state in zip(valve_pins, states):
        GPIO.output(pin, state)


def _apply_default_state(valve_pins: list[int], config: dict) -> None:
    """Return all valves to the configured default state."""
    defaults = config.get("default_valve_states", [0] * len(valve_pins))
    # Pad with 0 if fewer defaults than valves
    defaults = list(defaults) + [0] * (len(valve_pins) - len(defaults))
    _set_valves(valve_pins, defaults)
    logger.info("Valves set to default state: %s", dict(zip(valve_pins, defaults)))


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

# State machine states
_IDLE    = "IDLE"     # waiting for top sensor to trigger
_FILLING = "FILLING"  # top triggered, waiting for bottom sensor


def _run(config: dict):
    global _running

    sensor_pins: list[int] = config["sensor_gpios"]
    valve_pins:  list[int] = config["valve_gpios"]
    interval:    float     = config.get("poll_interval_ms", 500) / 1000.0

    # Named pin references (by index matching config.json order)
    # sensor_pins[0] = top sensor    (GPIO 8)
    # sensor_pins[1] = bottom sensor (GPIO 7)
    # valve_pins[0]  = Valve Air         (GPIO 14)
    # valve_pins[1]  = Valve Vacuum      (GPIO 15)
    # valve_pins[2]  = Valve Water Pump  (GPIO 18)
    # valve_pins[3]  = Valve Maple Trees (GPIO 24)
    # valve_pins[4]  = Water Pump        (GPIO 25)

    # --- GPIO setup ---------------------------------------------------------
    for pin in sensor_pins:
        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
    for pin in valve_pins:
        GPIO.setup(pin, GPIO.OUT, initial=GPIO.LOW)

    logger.info(
        "Task running — sensors: %s, valves: %s, interval: %.3fs",
        [f"GPIO{p}" for p in sensor_pins],
        [f"GPIO{p}" for p in valve_pins],
        interval,
    )

    # Apply default state at startup
    _apply_default_state(valve_pins, config)

    state = _IDLE

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
            # State machine
            #
            # IDLE:
            #   Wait for top sensor (sensor_pins[0]) to trigger (HIGH).
            #   When triggered → switch valves for filling and enter FILLING.
            #
            # FILLING:
            #   Wait for bottom sensor (sensor_pins[1]) to trigger (HIGH).
            #   When triggered → switch valves back and return to IDLE.
            #   If _running is cleared while in FILLING the finally block
            #   handles the reset, so no special case is needed here.
            # ----------------------------------------------------------------

            sensor_top    = sensor_values[0]
            sensor_bottom = sensor_values[1]

            if state == _IDLE:
                if sensor_top == GPIO.HIGH:
                    logger.info("Top sensor triggered — starting fill sequence")
                    # valve_pins order: Air, Vacuum, Water Pump, Maple Trees, Water Pump relay
                    GPIO.output(valve_pins[3], GPIO.LOW)   # close Maple Trees
                    GPIO.output(valve_pins[2], GPIO.HIGH)  # open  Water Pump valve
                    GPIO.output(valve_pins[1], GPIO.LOW)   # close Vacuum
                    GPIO.output(valve_pins[0], GPIO.HIGH)  # open  Air
                    GPIO.output(valve_pins[4], GPIO.HIGH)  # enable Water Pump relay
                    state = _FILLING
                    logger.info("State → FILLING")

            elif state == _FILLING:
                if sensor_bottom == GPIO.HIGH:
                    logger.info("Bottom sensor triggered — ending fill sequence")
                    GPIO.output(valve_pins[4], GPIO.LOW)   # disable Water Pump relay
                    GPIO.output(valve_pins[0], GPIO.LOW)   # close Air
                    GPIO.output(valve_pins[2], GPIO.LOW)   # close Water Pump valve
                    GPIO.output(valve_pins[1], GPIO.HIGH)  # open  Vacuum
                    GPIO.output(valve_pins[3], GPIO.HIGH)  # open  Maple Trees
                    state = _IDLE
                    logger.info("State → IDLE")

            time.sleep(interval)

    except Exception:
        logger.exception("Unhandled exception in background task")
    finally:
        # Always restore valves to their configured default state on exit
        _apply_default_state(valve_pins, config)
        GPIO.cleanup(sensor_pins + valve_pins)
        _running = False
        logger.info("Background task stopped, GPIO cleaned up")
