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
        sensor_drive_gpio: int  — BCM output pin that powers the sensor circuit
        sensor_read_gpio : int  — BCM input pin; HIGH = circuit closed (water at top)
        valve_gpios      : list — BCM pin numbers for relay-controlled valves
        poll_interval_ms : int  — how often to sample GPIO (default 500)
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


def _run_sequence(
    valve_pins: list[int],
    sequence: list[dict],
    timings: list[dict],
    running_flag_ref,
) -> None:
    """Execute an ordered list of valve actions with per-valve timing and inter-step delays.

    Each step: {"valve_index": int, "state": 0|1, "delay_after_ms": int}
    timings[i]: {"open_ms": int, "close_ms": int}  — how long the valve takes to actuate.

    running_flag_ref is a callable that returns the current _running bool so the
    sequence can abort early if the task is stopped.
    """
    for step in sequence:
        if not running_flag_ref():
            break
        vi    = step["valve_index"]
        state = step["state"]
        pin   = valve_pins[vi]
        GPIO.output(pin, state)

        # Wait for physical valve to finish actuating
        t = timings[vi] if vi < len(timings) else {}
        actuation_ms = t.get("open_ms", 0) if state == 1 else t.get("close_ms", 0)
        if actuation_ms > 0:
            time.sleep(actuation_ms / 1000.0)

        # Inter-step delay
        delay_ms = step.get("delay_after_ms", 0)
        if delay_ms > 0 and running_flag_ref():
            time.sleep(delay_ms / 1000.0)


def _apply_default_state(valve_pins: list[int]) -> None:
    """Close all valves (LOW) — used on startup and emergency stop."""
    for pin in valve_pins:
        GPIO.output(pin, GPIO.LOW)
    logger.info("All valves set LOW (default)")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

# State machine states
_IDLE    = "IDLE"     # waiting for top sensor to trigger
_FILLING = "FILLING"  # top triggered, waiting for bottom sensor


def _run(config: dict):
    global _running

    sensor_drive: int       = config["sensor_drive_gpio"]
    sensor_read:  int       = config["sensor_read_gpio"]
    valve_pins:   list[int] = config["valve_gpios"]
    interval:     float     = config.get("poll_interval_ms", 500) / 1000.0
    timings:      list[dict] = config.get("valve_timings", [])
    fill_seq:     list[dict] = config.get("fill_sequence", [])
    idle_seq:     list[dict] = config.get("idle_sequence", [])

    # --- GPIO setup ---------------------------------------------------------
    GPIO.setup(sensor_drive, GPIO.OUT, initial=GPIO.HIGH)  # always energised
    GPIO.setup(sensor_read,  GPIO.IN,  pull_up_down=GPIO.PUD_DOWN)
    for pin in valve_pins:
        GPIO.setup(pin, GPIO.OUT, initial=GPIO.LOW)

    logger.info(
        "Task running — sensor drive: GPIO%s, sensor read: GPIO%s, valves: %s, interval: %.3fs",
        sensor_drive,
        sensor_read,
        [f"GPIO{p}" for p in valve_pins],
        interval,
    )

    # Apply default state at startup
    _apply_default_state(valve_pins)

    state = _IDLE

    try:
        while _running:
            sensor_value = GPIO.input(sensor_read)
            valve_values = [GPIO.input(p) for p in valve_pins]

            # Update shared state (read by web UI)
            with _lock:
                _gpio_states[f"gpio_{sensor_drive}"] = GPIO.HIGH  # always driven
                _gpio_states[f"gpio_{sensor_read}"]  = sensor_value
                for pin, val in zip(valve_pins, valve_values):
                    _gpio_states[f"gpio_{pin}"] = val

            # ----------------------------------------------------------------
            # State machine
            #
            # IDLE:
            #   sensor_read HIGH = water circuit closed = tank full at top.
            #   Execute fill_sequence then enter FILLING.
            #
            # FILLING:
            #   sensor_read LOW = circuit open = water dropped to bottom level.
            #   Execute idle_sequence and return to IDLE.
            # ----------------------------------------------------------------

            if state == _IDLE:
                if sensor_value == GPIO.HIGH:
                    logger.info("Sensor circuit closed (water at top) — starting fill sequence")
                    _run_sequence(valve_pins, fill_seq, timings, lambda: _running)
                    state = _FILLING
                    logger.info("State → FILLING")

            elif state == _FILLING:
                if sensor_value == GPIO.LOW:
                    logger.info("Sensor circuit open (water at bottom) — ending fill sequence")
                    _run_sequence(valve_pins, idle_seq, timings, lambda: _running)
                    state = _IDLE
                    logger.info("State → IDLE")

            time.sleep(interval)

    except Exception:
        logger.exception("Unhandled exception in background task")
    finally:
        # Always close all valves on exit
        _apply_default_state(valve_pins)
        GPIO.cleanup([sensor_drive, sensor_read] + valve_pins)
        _running = False
        logger.info("Background task stopped, GPIO cleaned up")
