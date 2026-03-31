"""
controller.py — Central controller for task lifecycle and shared state.

Thread-safe state management with manual override support.
The controller owns the background thread and all GPIO state visible to the UI.
"""

import logging
import threading

from gpio_driver import GpioDriver
from config_manager import get_valve_pins, get_valve_timings

logger = logging.getLogger(__name__)


class Controller:
    """Manages the background task, GPIO state, and manual override."""

    def __init__(self, gpio: GpioDriver):
        self._gpio = gpio
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

        # Shared state (protected by _lock)
        self._running = False
        self._mode: str = "sequence"
        self._phase: str | None = None
        self._manual_override = False
        self._gpio_states: dict[str, int] = {}

        # Pause/resume event for manual override.
        # Set = running normally, Clear = paused.
        self._pause_event = threading.Event()
        self._pause_event.set()

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

    def is_manual_override(self) -> bool:
        with self._lock:
            return self._manual_override

    def get_gpio_states(self) -> dict[str, int]:
        with self._lock:
            return dict(self._gpio_states)

    def get_status(self) -> dict:
        """Return a complete status snapshot for SSE."""
        with self._lock:
            return {
                "running": self._running,
                "mode": self._mode,
                "phase": self._phase,
                "manual_override": self._manual_override,
                "gpio_states": dict(self._gpio_states),
            }

    @property
    def gpio(self) -> GpioDriver:
        return self._gpio

    # ------------------------------------------------------------------
    # State mutation (thread-safe)
    # ------------------------------------------------------------------
    def set_phase(self, phase: str | None) -> None:
        with self._lock:
            self._phase = phase

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
            self._manual_override = False
            self._pause_event.set()
            self._stop_event.clear()

        self._thread = threading.Thread(
            target=self._run, args=(config,), daemon=True,
        )
        self._thread.start()
        logger.info("Background task started in %s mode", self._mode)
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
            self._manual_override = False

        # Wake up the mode runner if it's paused or sleeping
        self._stop_event.set()
        self._pause_event.set()

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._thread = None
        logger.info("Background task stopped")
        return True

    # ------------------------------------------------------------------
    # Manual override
    # ------------------------------------------------------------------
    def enable_manual_override(self) -> bool:
        """Enable manual override — pauses the mode runner."""
        with self._lock:
            if not self._running:
                return False
            self._manual_override = True
        self._pause_event.clear()
        logger.info("Manual override enabled")
        return True

    def disable_manual_override(self) -> bool:
        """Disable manual override — resumes the mode runner."""
        with self._lock:
            if not self._running:
                return False
            self._manual_override = False
        self._pause_event.set()
        logger.info("Manual override disabled")
        return True

    def set_manual_valve(self, valve_index: int, logical_state: int, config: dict) -> bool:
        """Directly set a valve GPIO pin (only when override is active)."""
        with self._lock:
            if not self._manual_override:
                return False

        valve_pins = get_valve_pins(config)
        inverted = config["hardware"].get("valve_inverted", True)

        if not 0 <= valve_index < len(valve_pins):
            return False

        pin = valve_pins[valve_index]
        physical = GpioDriver.valve_level(logical_state, inverted)
        self._gpio.write(pin, physical)
        self.set_gpio_state(pin, logical_state)
        logger.info("Manual: valve %d (GPIO%d) → %s",
                     valve_index, pin, "open" if logical_state else "closed")
        return True

    def set_manual_sensor(self, pin_role: str, state: int, config: dict) -> bool:
        """Set a sensor pin in manual override (mock only for read pin)."""
        with self._lock:
            if not self._manual_override:
                return False

        sensor = config["hardware"]["sensor"]

        if pin_role == "drive":
            pin = sensor["drive_gpio"]
            self._gpio.write(pin, state)
            self.set_gpio_state(pin, state)
            logger.info("Manual: sensor drive (GPIO%d) → %d", pin, state)
            return True
        elif pin_role == "read":
            if self._gpio.mock:
                pin = sensor["read_gpio"]
                self._gpio.write(pin, state)
                self.set_gpio_state(pin, state)
                logger.info("Manual: sensor read (GPIO%d) → %d (mock)", pin, state)
                return True
            else:
                logger.warning("Sensor read override ignored on real hardware")
                return False
        return False

    # ------------------------------------------------------------------
    # Helpers for mode runners
    # ------------------------------------------------------------------
    def should_stop(self) -> bool:
        """Check if the task should stop (non-blocking)."""
        return self._stop_event.is_set()

    def wait_if_paused(self, timeout: float = 0.1) -> bool:
        """Block if manual override is active.
        Returns True if we should stop, False if we can continue.
        """
        while not self._pause_event.is_set():
            if self.should_stop():
                return True
            self._pause_event.wait(timeout=0.1)
        return self.should_stop()

    def interruptible_sleep(self, seconds: float) -> bool:
        """Sleep in small increments, waking on stop signal.
        Returns True if interrupted (should stop).
        """
        return self._stop_event.wait(timeout=seconds)

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
        sensor = config["hardware"]["sensor"]

        # --- GPIO setup ---
        self._gpio.setup_output(sensor["drive_gpio"], initial=GpioDriver.HIGH)
        self._gpio.setup_input(sensor["read_gpio"])
        for pin in valve_pins:
            self._gpio.setup_output(pin, initial=GpioDriver.valve_level(0, inverted))

        # Apply default state
        self._apply_default_state(valve_pins, inverted, default_states)

        logger.info("Task running [%s] — valves: %s, inverted: %s",
                     mode, [f"GPIO{p}" for p in valve_pins], inverted)

        try:
            if mode == "alternance":
                AlternanceModeRunner(self, config).run()
            else:
                SequenceModeRunner(self, config).run()
        except Exception:
            logger.exception("Unhandled exception in background task")
        finally:
            self._apply_default_state(valve_pins, inverted, default_states)
            all_pins = [sensor["drive_gpio"], sensor["read_gpio"]] + valve_pins
            self._gpio.cleanup(all_pins)

            with self._lock:
                self._gpio_states[f"gpio_{sensor['drive_gpio']}"] = 0
                self._gpio_states[f"gpio_{sensor['read_gpio']}"] = 0
                for pin, ds in zip(valve_pins, default_states):
                    self._gpio_states[f"gpio_{pin}"] = ds
                self._running = False
                self._phase = None
                self._manual_override = False

            logger.info("Background task stopped, GPIO cleaned up")

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
