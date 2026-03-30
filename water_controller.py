"""
water_controller.py - Background GPIO monitoring and valve control task.

This module runs in a background thread managed by app.py.
GPIO state is exposed via the `get_gpio_states()` function so the web UI
can poll it. Configuration is reloaded each time the task starts.

Supported modes:
    sequence   – Sensor-driven state machine (dump/idle sequences)
    alternance – Timed alternation between two programmable sequences
    manual     – Direct per-pin control from the web UI
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
_mode: str = "sequence"             # active mode for the current run
_thread: threading.Thread | None = None

# Manual mode: pending pin commands from the web UI  {valve_index: logical_state}
_manual_commands: dict[int, int] = {}
# Manual mode: pending sensor overrides  {"drive": 0|1, "read": 0|1}
_manual_sensor_commands: dict[str, int] = {}


def get_gpio_states() -> dict[str, int]:
    """Return a snapshot of all monitored GPIO pin states (thread-safe)."""
    with _lock:
        return dict(_gpio_states)


def is_running() -> bool:
    return _running


def get_mode() -> str:
    return _mode


def set_manual_valve(valve_index: int, logical_state: int) -> None:
    """Queue a manual valve command (only effective in manual mode)."""
    with _lock:
        _manual_commands[valve_index] = logical_state


def set_manual_sensor(pin_role: str, state: int) -> None:
    """Queue a manual sensor override (only effective in manual mode).

    pin_role: "drive" or "read"
    state: 0 or 1
    """
    with _lock:
        _manual_sensor_commands[pin_role] = state


def apply_initial_default_state(config: dict) -> None:
    """Set valves to their configured default state without starting the task.

    Call once at app startup so that valves are in a safe state even before
    the water controller is started.
    """
    valve_pins: list[int] = config["valve_gpios"]
    inverted: bool = config.get("valve_inverted", True)
    default_states: list[int] = config.get("valve_default_state", [0] * len(valve_pins))

    def _valve_level(logical_state: int) -> int:
        return (1 - logical_state) if inverted else logical_state

    for pin in valve_pins:
        GPIO.setup(pin, GPIO.OUT, initial=_valve_level(0))

    _apply_default_state(valve_pins, _valve_level, default_states)

    with _lock:
        for pin, ds in zip(valve_pins, default_states):
            _gpio_states[f"gpio_{pin}"] = ds

    logger.info("Initial default valve state applied")


# ---------------------------------------------------------------------------
# Task lifecycle
# ---------------------------------------------------------------------------
def start(config: dict) -> bool:
    """
    Start the background task with the given config dict.
    Returns False if already running.
    """
    global _running, _thread, _mode

    if _running:
        logger.warning("start() called but task is already running")
        return False

    _mode = config.get("mode", "sequence")
    _running = True

    with _lock:
        _manual_commands.clear()
        _manual_sensor_commands.clear()

    _thread = threading.Thread(target=_run, args=(config,), daemon=True)
    _thread.start()
    logger.info("Background task started in %s mode", _mode)
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
    sensor_read: int,
    abort_sensor_value: int | None,
    running_flag_ref,
    valve_level=None,
) -> bool:
    """Execute an ordered list of valve actions with per-valve timing and inter-step delays.

    Each step: {"valve_index": int, "state": 0|1, "delay_after_ms": int}
    timings[i]: {"open_ms": int, "close_ms": int}  — how long the valve takes to actuate.

    Params:
        sensor_read         – BCM pin number to poll during waits
        abort_sensor_value  – if the sensor reads this value mid-sequence, abort
                              immediately and return True (interrupted).  Pass None
                              to disable mid-sequence sensor checking.
        running_flag_ref    – callable; returns False if the task was stopped.

    Returns True if the sequence was interrupted by a sensor change, False otherwise.
    """
    def _should_abort():
        if not running_flag_ref():
            return True
        if abort_sensor_value is not None:
            return GPIO.input(sensor_read) == abort_sensor_value
        return False

    def _interruptible_sleep(total_ms):
        """Sleep in small increments so we can react to sensor or stop events."""
        end = time.monotonic() + total_ms / 1000.0
        while time.monotonic() < end:
            if _should_abort():
                return
            time.sleep(min(0.02, end - time.monotonic()))

    for step in sequence:
        if _should_abort():
            return True

        vi           = step["valve_index"]
        logical      = step["state"]
        pin          = valve_pins[vi]
        physical     = valve_level(logical) if valve_level else logical
        GPIO.output(pin, physical)

        # Publish the logical state so the UI reflects open/close correctly
        with _lock:
            _gpio_states[f"gpio_{pin}"] = logical

        # Wait for physical valve to finish actuating
        t = timings[vi] if vi < len(timings) else {}
        actuation_ms = t.get("open_ms", 0) if logical == 1 else t.get("close_ms", 0)
        if actuation_ms > 0:
            _interruptible_sleep(actuation_ms)

        # Inter-step delay
        delay_ms = step.get("delay_after_ms", 0)
        if delay_ms > 0:
            _interruptible_sleep(delay_ms)

    return _should_abort() and abort_sensor_value is not None and GPIO.input(sensor_read) == abort_sensor_value


def _apply_default_state(valve_pins: list[int], valve_level=None, default_states: list[int] | None = None) -> None:
    """Set all valves to their configured default state — used on startup and shutdown.

    If *default_states* is provided it must be a list of logical values (0/1) with
    one entry per valve.  Otherwise every valve is closed (logical 0).
    """
    if default_states is None:
        default_states = [0] * len(valve_pins)
    for pin, logical in zip(valve_pins, default_states):
        physical = valve_level(logical) if valve_level else logical
        GPIO.output(pin, physical)
    labels = [f"GPIO{p}={'open' if s else 'closed'}" for p, s in zip(valve_pins, default_states)]
    logger.info("Valves set to default state: %s", ", ".join(labels))


# ---------------------------------------------------------------------------
# Interruptible sleep (used by alternance mode delays)
# ---------------------------------------------------------------------------
def _sleep_while_running(total_ms: float) -> bool:
    """Sleep in small increments, returning True if interrupted by stop."""
    end = time.monotonic() + total_ms / 1000.0
    while time.monotonic() < end:
        if not _running:
            return True
        time.sleep(min(0.05, max(0, end - time.monotonic())))
    return not _running


# ---------------------------------------------------------------------------
# Main loop — dispatches to mode-specific logic
# ---------------------------------------------------------------------------

# State machine states (sequence mode)
_IDLE    = "IDLE"     # waiting for top sensor to trigger
_DUMPING = "DUMPING"  # top triggered, waiting for bottom sensor


def _run(config: dict):
    global _running

    mode = config.get("mode", "sequence")

    sensor_drive: int        = config["sensor_drive_gpio"]
    sensor_read:  int        = config["sensor_read_gpio"]
    valve_pins:   list[int]  = config["valve_gpios"]
    interval:     float      = config.get("poll_interval_ms", 500) / 1000.0
    inverted:     bool       = config.get("valve_inverted", True)
    timings:      list[dict] = config.get("valve_timings", [])
    default_st:   list[int]  = config.get("valve_default_state", [0] * len(valve_pins))

    def _valve_level(logical_state: int) -> int:
        """Translate logical 1=open/0=close to the physical GPIO level."""
        return (1 - logical_state) if inverted else logical_state

    # --- GPIO setup ---------------------------------------------------------
    GPIO.setup(sensor_drive, GPIO.OUT, initial=GPIO.HIGH)  # always energised
    GPIO.setup(sensor_read,  GPIO.IN,  pull_up_down=GPIO.PUD_DOWN)
    for pin in valve_pins:
        GPIO.setup(pin, GPIO.OUT, initial=_valve_level(0))

    logger.info(
        "Task running [%s] — sensor drive: GPIO%s, sensor read: GPIO%s, valves: %s, interval: %.3fs, inverted: %s",
        mode,
        sensor_drive,
        sensor_read,
        [f"GPIO{p}" for p in valve_pins],
        interval,
        inverted,
    )

    # Apply default state at startup
    _apply_default_state(valve_pins, _valve_level, default_st)

    try:
        if mode == "alternance":
            _run_alternance(config, valve_pins, timings, sensor_drive, sensor_read, interval, _valve_level)
        elif mode == "manual":
            _run_manual(valve_pins, sensor_drive, sensor_read, interval, _valve_level)
        else:
            _run_sequence_mode(config, valve_pins, timings, sensor_drive, sensor_read, interval, _valve_level, default_st)

    except Exception:
        logger.exception("Unhandled exception in background task")
    finally:
        # Restore default valve state on exit
        _apply_default_state(valve_pins, _valve_level, default_st)
        GPIO.cleanup([sensor_drive, sensor_read] + valve_pins)
        with _lock:
            _gpio_states[f"gpio_{sensor_drive}"] = 0
            _gpio_states[f"gpio_{sensor_read}"]  = 0
            for pin, ds in zip(valve_pins, default_st):
                _gpio_states[f"gpio_{pin}"] = ds
        _running = False
        logger.info("Background task stopped, GPIO cleaned up")


# ---------------------------------------------------------------------------
# Mode: Sequence (original sensor-driven state machine)
# ---------------------------------------------------------------------------
def _run_sequence_mode(config, valve_pins, timings, sensor_drive, sensor_read, interval, _valve_level, default_st):
    dump_seq:  list[dict] = config.get("dump_sequence", [])
    idle_seq:  list[dict] = config.get("idle_sequence", [])

    state = _IDLE
    first_loop = True

    while _running:
        sensor_value = GPIO.input(sensor_read)
        valve_values = [GPIO.input(p) for p in valve_pins]

        with _lock:
            _gpio_states[f"gpio_{sensor_drive}"] = GPIO.HIGH
            _gpio_states[f"gpio_{sensor_read}"]  = sensor_value
            for pin, val in zip(valve_pins, valve_values):
                _gpio_states[f"gpio_{pin}"] = _valve_level(val)

        if state == _IDLE:
            if sensor_value == GPIO.HIGH or first_loop:
                first_loop = False
                logger.info("Sensor circuit closed (sap at top) — starting dump sequence")
                interrupted = _run_sequence(
                    valve_pins, dump_seq, timings,
                    sensor_read, GPIO.LOW,
                    lambda: _running,
                    _valve_level,
                )
                if interrupted:
                    logger.info("Dump sequence interrupted by sensor change — jumping to idle sequence")
                    _run_sequence(
                        valve_pins, idle_seq, timings,
                        sensor_read, GPIO.HIGH,
                        lambda: _running,
                        _valve_level,
                    )
                    state = _IDLE
                    logger.info("State → IDLE (interrupted)")
                else:
                    state = _DUMPING
                    logger.info("State → DUMPING")

        elif state == _DUMPING:
            if sensor_value == GPIO.LOW:
                logger.info("Sensor circuit open (sap at bottom) — ending dump sequence")
                interrupted = _run_sequence(
                    valve_pins, idle_seq, timings,
                    sensor_read, GPIO.HIGH,
                    lambda: _running,
                    _valve_level,
                )
                if interrupted:
                    logger.info("Idle sequence interrupted by sensor change — jumping to dump sequence")
                    _run_sequence(
                        valve_pins, dump_seq, timings,
                        sensor_read, GPIO.LOW,
                        lambda: _running,
                        _valve_level,
                    )
                    state = _DUMPING
                    logger.info("State → DUMPING (interrupted)")
                else:
                    state = _IDLE
                    logger.info("State → IDLE")

        time.sleep(interval)


# ---------------------------------------------------------------------------
# Mode: Alternance (timed alternation between two sequences)
# ---------------------------------------------------------------------------
def _run_alternance(config, valve_pins, timings, sensor_drive, sensor_read, interval, _valve_level):
    alt_cfg   = config.get("alternance", {})
    seq_a     = alt_cfg.get("sequence_a", [])
    seq_b     = alt_cfg.get("sequence_b", [])
    delay_a   = alt_cfg.get("delay_a_to_b_ms", 5000)
    delay_b   = alt_cfg.get("delay_b_to_a_ms", 5000)

    def _update_shared_state():
        sensor_value = GPIO.input(sensor_read)
        valve_values = [GPIO.input(p) for p in valve_pins]
        with _lock:
            _gpio_states[f"gpio_{sensor_drive}"] = GPIO.HIGH
            _gpio_states[f"gpio_{sensor_read}"]  = sensor_value
            for pin, val in zip(valve_pins, valve_values):
                _gpio_states[f"gpio_{pin}"] = _valve_level(val)

    while _running:
        # Run sequence A
        logger.info("Alternance: running sequence A")
        _run_sequence(
            valve_pins, seq_a, timings,
            sensor_read, None,
            lambda: _running,
            _valve_level,
        )
        _update_shared_state()
        if not _running:
            break

        # Wait delay A→B
        logger.info("Alternance: waiting %d ms (A→B)", delay_a)
        if _sleep_while_running(delay_a):
            break
        _update_shared_state()

        # Run sequence B
        logger.info("Alternance: running sequence B")
        _run_sequence(
            valve_pins, seq_b, timings,
            sensor_read, None,
            lambda: _running,
            _valve_level,
        )
        _update_shared_state()
        if not _running:
            break

        # Wait delay B→A
        logger.info("Alternance: waiting %d ms (B→A)", delay_b)
        if _sleep_while_running(delay_b):
            break
        _update_shared_state()


# ---------------------------------------------------------------------------
# Mode: Manual (direct per-pin control from the web UI)
# ---------------------------------------------------------------------------
def _run_manual(valve_pins, sensor_drive, sensor_read, interval, _valve_level):
    while _running:
        # Process any pending manual commands (valves + sensors)
        with _lock:
            valve_cmds = dict(_manual_commands)
            _manual_commands.clear()
            sensor_cmds = dict(_manual_sensor_commands)
            _manual_sensor_commands.clear()

        for vi, logical in valve_cmds.items():
            if 0 <= vi < len(valve_pins):
                pin = valve_pins[vi]
                physical = _valve_level(logical)
                GPIO.output(pin, physical)
                logger.info("Manual: valve %d (GPIO%d) → %s", vi, pin, "open" if logical else "closed")

        # Sensor overrides
        if "drive" in sensor_cmds:
            val = sensor_cmds["drive"]
            GPIO.output(sensor_drive, val)
            logger.info("Manual: sensor drive (GPIO%d) → %d", sensor_drive, val)

        if "read" in sensor_cmds:
            if _MOCK:
                # In mock mode, writing to the pin fakes the sensor input
                GPIO.output(sensor_read, sensor_cmds["read"])
                logger.info("Manual: sensor read (GPIO%d) → %d (mock)", sensor_read, sensor_cmds["read"])
            else:
                logger.warning("Manual: sensor read override ignored on real hardware (pin is INPUT)")

        # Read current pin states and publish for the UI
        sensor_drive_val = GPIO.input(sensor_drive)
        sensor_read_val  = GPIO.input(sensor_read)
        valve_values     = [GPIO.input(p) for p in valve_pins]

        with _lock:
            _gpio_states[f"gpio_{sensor_drive}"] = sensor_drive_val
            _gpio_states[f"gpio_{sensor_read}"]  = sensor_read_val
            for pin, val in zip(valve_pins, valve_values):
                _gpio_states[f"gpio_{pin}"] = _valve_level(val)

        time.sleep(interval)
