"""
controller.py — Central controller for task lifecycle and shared state.

Thread-safe state management with per-pin manual override support.
The controller owns the background thread and all GPIO state visible to the UI.

Override model:
    Individual pins can be overridden independently. An overridden pin is
    "locked" — the mode runner cannot write to it. The user controls its
    value directly. The mode runner keeps running, just skipping writes
    to overridden pins. This lets you e.g. lock the sensor read pin to
    test a sequence, or lock one valve while the rest operate normally.
"""

import logging
import threading

from gpio_driver import GpioDriver
from config_manager import get_sensor_top, get_sensor_bottom, get_valve_pins, get_valve_timings
from task_logger import task_log

logger = logging.getLogger(__name__)


class Controller:
    """Manages the background task, GPIO state, and per-pin overrides."""

    def __init__(self, gpio: GpioDriver):
        self._gpio = gpio
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

        # Shared state (protected by _lock)
        self._running = False
        self._mode: str = "sequence"
        self._phase: str | None = None
        self._gpio_states: dict[str, int] = {}
        self._hold_total: float = 0.0      # min_run hold duration in seconds
        self._hold_remaining: float = 0.0  # seconds left in current hold

        # Per-pin override: set of GPIO pin numbers currently locked
        self._overridden_pins: set[int] = set()

        # Stop event — cleared when running, set to signal stop.
        self._stop_event = threading.Event()
        self._stop_event.set()

    # ------------------------------------------------------------------
    # Public read API (thread-safe)
    # ------------------------------------------------------------------
    def is_running(self) -> bool:
        with self._lock:
            return self._running

    def get_mode(self) -> str:
        with self._lock:
            return self._mode

    def get_phase(self) -> str | None:
        with self._lock:
            return self._phase

    def get_gpio_states(self) -> dict[str, int]:
        with self._lock:
            return dict(self._gpio_states)

    def get_overridden_pins(self) -> list[int]:
        """Return sorted list of currently overridden GPIO pin numbers."""
        with self._lock:
            return sorted(self._overridden_pins)

    def is_pin_overridden(self, pin: int) -> bool:
        """Check if a specific GPIO pin is currently overridden."""
        with self._lock:
            return pin in self._overridden_pins

    def has_any_override(self) -> bool:
        """Check if any pin is currently overridden."""
        with self._lock:
            return len(self._overridden_pins) > 0

    def get_status(self) -> dict:
        """Return a complete status snapshot for SSE."""
        with self._lock:
            status = {
                "running": self._running,
                "mode": self._mode,
                "phase": self._phase,
                "overridden_pins": sorted(self._overridden_pins),
                "gpio_states": dict(self._gpio_states),
            }
            if self._hold_total > 0:
                status["hold_total"] = round(self._hold_total, 1)
                status["hold_remaining"] = round(max(0, self._hold_remaining), 1)
            return status

    @property
    def gpio(self) -> GpioDriver:
        return self._gpio

    # ------------------------------------------------------------------
    # State mutation (thread-safe)
    # ------------------------------------------------------------------
    def set_phase(self, phase: str | None) -> None:
        with self._lock:
            self._phase = phase

    def set_hold(self, total: float, remaining: float) -> None:
        """Update the min-run hold progress (seconds)."""
        with self._lock:
            self._hold_total = total
            self._hold_remaining = remaining

    def clear_hold(self) -> None:
        """Clear the hold indicator."""
        with self._lock:
            self._hold_total = 0.0
            self._hold_remaining = 0.0

    def set_gpio_state(self, pin: int, logical_value: int) -> None:
        """Update the tracked GPIO state for a single pin."""
        with self._lock:
            self._gpio_states[f"gpio_{pin}"] = logical_value

    def set_gpio_states_bulk(self, updates: dict[str, int]) -> None:
        """Batch update tracked GPIO states."""
        with self._lock:
            self._gpio_states.update(updates)

    # ------------------------------------------------------------------
    # Task lifecycle
    # ------------------------------------------------------------------
    def start(self, config: dict) -> bool:
        """Start the background task. Returns False if already running."""
        with self._lock:
            if self._running:
                logger.warning("start() called but task is already running")
                return False
            self._running = True
            self._mode = config.get("mode", "sequence")
            self._phase = None
            self._hold_total = 0.0
            self._hold_remaining = 0.0
            self._overridden_pins.clear()
            self._stop_event.clear()

        self._thread = threading.Thread(
            target=self._run, args=(config,), daemon=True,
        )
        self._thread.start()
        logger.info("Background task started in %s mode", self._mode)
        task_log.info("TASK    started in %s mode", self._mode)
        return True

    def stop(self) -> bool:
        """Stop the background task and wait for it to finish.
        Returns False if not running.
        """
        with self._lock:
            if not self._running:
                logger.warning("stop() called but task is not running")
                return False
            self._running = False
            self._overridden_pins.clear()

        self._stop_event.set()

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._thread = None
        logger.info("Background task stopped")
        task_log.info("TASK    stopped")
        return True

    # ------------------------------------------------------------------
    # Per-pin override
    # ------------------------------------------------------------------
    def override_pin(self, pin: int, state: int, config: dict) -> bool:
        """Add a pin to the override set and write its value.

        The mode runner will skip this pin until it is released.
        Works for both valve pins and sensor pins.
        """
        with self._lock:
            if not self._running:
                return False
            self._overridden_pins.add(pin)

        inverted = config["hardware"].get("valve_inverted", True)
        valve_pins = get_valve_pins(config)
        sensor_top = get_sensor_top(config)
        sensor_bottom = get_sensor_bottom(config)
        sensor_read_pins = {sensor_top["read_gpio"], sensor_bottom["read_gpio"]}

        # Determine if this is a valve pin (needs inversion) or sensor pin
        if pin in valve_pins:
            physical = GpioDriver.valve_level(state, inverted)
            self._gpio.write(pin, physical)
            self.set_gpio_state(pin, state)
            label = next((v["label"] for v in config["hardware"]["valves"] if v["gpio"] == pin), f"GPIO{pin}")
            logger.info("Override: %s (GPIO%d) → %s", label, pin, "open" if state else "closed")
            task_log.info("OVERRIDE  %s (GPIO%d) → %s", label, pin, "open" if state else "closed")
        else:
            # Sensor pin — write directly (mock only for read pin)
            if pin in sensor_read_pins and not self._gpio.mock:
                logger.warning("Sensor read override ignored on real hardware (input pin)")
                # Still add to override set so mode runner uses overridden value
                self.set_gpio_state(pin, state)
                task_log.info("OVERRIDE  sensor read (GPIO%d) → %s (virtual)", pin, "HIGH" if state else "LOW")
            else:
                self._gpio.write(pin, state)
                self.set_gpio_state(pin, state)
                logger.info("Override: sensor GPIO%d → %d", pin, state)
                task_log.info("OVERRIDE  sensor (GPIO%d) → %s", pin, "HIGH" if state else "LOW")
        return True

    def release_pin(self, pin: int) -> bool:
        """Remove a pin from the override set. The mode runner regains control."""
        with self._lock:
            if pin not in self._overridden_pins:
                return False
            self._overridden_pins.discard(pin)
        logger.info("Released override: GPIO%d", pin)
        task_log.info("OVERRIDE  GPIO%d released", pin)
        return True

    def release_all_overrides(self) -> None:
        """Release all pin overrides."""
        with self._lock:
            count = len(self._overridden_pins)
            self._overridden_pins.clear()
        logger.info("All overrides released")
        if count > 0:
            task_log.info("OVERRIDE  all released (%d pins)", count)

    # ------------------------------------------------------------------
    # Mode runner helpers
    # ------------------------------------------------------------------
    def should_stop(self) -> bool:
        """Check if the task should stop (non-blocking)."""
        return self._stop_event.is_set()

    def interruptible_sleep(self, seconds: float) -> bool:
        """Sleep in small increments, waking on stop signal.
        Returns True if interrupted (should stop).
        """
        return self._stop_event.wait(timeout=seconds)

    def write_pin_if_not_overridden(self, pin: int, logical: int, inverted: bool) -> bool:
        """Write to a valve pin only if it is not overridden.

        Returns True if the write was performed, False if the pin is locked.
        Always updates the tracked gpio_state regardless (so SSE shows
        what the mode *wants* for non-overridden pins, and the user's
        value for overridden pins).
        """
        with self._lock:
            if pin in self._overridden_pins:
                return False
        physical = GpioDriver.valve_level(logical, inverted)
        self._gpio.write(pin, physical)
        self.set_gpio_state(pin, logical)
        return True

    def read_sensor(self, pin: int) -> int:
        """Read the sensor pin. If it's overridden, return the overridden value
        from gpio_states instead of reading real hardware.
        """
        with self._lock:
            if pin in self._overridden_pins:
                return self._gpio_states.get(f"gpio_{pin}", 0)
        return self._gpio.read(pin)

    # ------------------------------------------------------------------
    # Main run loop
    # ------------------------------------------------------------------
    def _run(self, config: dict):
        """Entry point for the background thread."""
        from modes.sequence import SequenceModeRunner
        from modes.alternance import AlternanceModeRunner

        mode = config.get("mode", "sequence")
        valve_pins = get_valve_pins(config)
        inverted = config["hardware"].get("valve_inverted", True)
        default_states = config["settings"].get("default_valve_states", [0] * len(valve_pins))
        sensor_top = get_sensor_top(config)
        sensor_bottom = get_sensor_bottom(config)

        # --- GPIO setup ---
        for sensor in (sensor_top, sensor_bottom):
            self._gpio.setup_output(sensor["drive_gpio"], initial=GpioDriver.HIGH)
            self._gpio.setup_input(sensor["read_gpio"])
        for pin in valve_pins:
            self._gpio.setup_output(pin, initial=GpioDriver.valve_level(0, inverted))

        # Apply default state
        self._apply_default_state(valve_pins, inverted, default_states)

        logger.info("Task running [%s] — valves: %s, inverted: %s, top sensor: GPIO%d/GPIO%d, bottom sensor: GPIO%d/GPIO%d",
                     mode, [f"GPIO{p}" for p in valve_pins], inverted,
                     sensor_top["drive_gpio"], sensor_top["read_gpio"],
                     sensor_bottom["drive_gpio"], sensor_bottom["read_gpio"])

        try:
            if mode == "alternance":
                AlternanceModeRunner(self, config).run()
            else:
                SequenceModeRunner(self, config).run()
        except Exception:
            logger.exception("Unhandled exception in background task")
        finally:
            self._apply_default_state(valve_pins, inverted, default_states)
            # Only cleanup sensor pins — valve pins keep their default state
            sensor_pins = [sensor_top["drive_gpio"], sensor_top["read_gpio"],
                           sensor_bottom["drive_gpio"], sensor_bottom["read_gpio"]]
            self._gpio.cleanup(sensor_pins)

            with self._lock:
                for sensor in (sensor_top, sensor_bottom):
                    self._gpio_states[f"gpio_{sensor['drive_gpio']}"] = 0
                    self._gpio_states[f"gpio_{sensor['read_gpio']}"] = 0
                for pin, ds in zip(valve_pins, default_states):
                    self._gpio_states[f"gpio_{pin}"] = ds
                self._running = False
                self._phase = None
                self._hold_total = 0.0
                self._hold_remaining = 0.0
                self._overridden_pins.clear()

            logger.info("Background task stopped")

    def _apply_default_state(self, valve_pins: list[int], inverted: bool, default_states: list[int]) -> None:
        """Set all valves to their configured default state."""
        for pin, logical in zip(valve_pins, default_states):
            physical = GpioDriver.valve_level(logical, inverted)
            self._gpio.write(pin, physical)
            self.set_gpio_state(pin, logical)
        labels = [f"GPIO{p}={'open' if s else 'closed'}" for p, s in zip(valve_pins, default_states)]
        logger.info("Valves set to default: %s", ", ".join(labels))

    def apply_initial_default_state(self, config: dict) -> None:
        """Set valves to defaults at app startup (before any task runs)."""
        valve_pins = get_valve_pins(config)
        inverted = config["hardware"].get("valve_inverted", True)
        default_states = config["settings"].get("default_valve_states", [0] * len(valve_pins))

        for pin in valve_pins:
            self._gpio.setup_output(pin, initial=GpioDriver.valve_level(0, inverted))

        self._apply_default_state(valve_pins, inverted, default_states)
        logger.info("Initial default valve state applied")
