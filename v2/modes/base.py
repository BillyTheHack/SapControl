"""
modes/base.py — Abstract base class for mode runners.
"""

import logging
from abc import ABC, abstractmethod

from gpio_driver import GpioDriver
from config_manager import get_valve_pins, get_valve_timings

logger = logging.getLogger(__name__)


class BaseModeRunner(ABC):
    """Base class for all mode runners (sequence, alternance)."""

    def __init__(self, controller, config: dict):
        self.controller = controller
        self.config = config
        self.gpio: GpioDriver = controller.gpio

        self.valve_pins = get_valve_pins(config)
        self.timings = get_valve_timings(config)
        self.inverted = config["hardware"].get("valve_inverted", True)
        self.interval = config["settings"].get("poll_interval_ms", 500) / 1000.0
        self.sensor_drive = config["hardware"]["sensor"]["drive_gpio"]
        self.sensor_read = config["hardware"]["sensor"]["read_gpio"]

    @abstractmethod
    def run(self) -> None:
        """Main loop — called by controller._run() in background thread."""
        ...

    def execute_sequence(self, steps: list[dict], abort_on_sensor: int | None = None) -> bool:
        """Execute an ordered list of valve actions.

        Each step: {"valve_index": int, "state": 0|1, "delay_after_ms": int}

        Args:
            steps: List of step dicts.
            abort_on_sensor: If set, abort if sensor reads this value mid-sequence.

        Returns True if interrupted by sensor change, False otherwise.
        """
        for step in steps:
            if self.controller.should_stop():
                return False
            if self.controller.wait_if_paused():
                return False

            if abort_on_sensor is not None and self.gpio.read(self.sensor_read) == abort_on_sensor:
                return True

            vi = step["valve_index"]
            logical = step["state"]
            pin = self.valve_pins[vi]
            physical = GpioDriver.valve_level(logical, self.inverted)
            self.gpio.write(pin, physical)
            self.controller.set_gpio_state(pin, logical)

            # Wait for physical actuation
            timing = self.timings[vi] if vi < len(self.timings) else {}
            actuation_ms = timing.get("open_ms", 0) if logical == 1 else timing.get("close_ms", 0)
            if actuation_ms > 0:
                if self._interruptible_sleep_ms(actuation_ms, abort_on_sensor):
                    return True

            # Inter-step delay
            delay_ms = step.get("delay_after_ms", 0)
            if delay_ms > 0:
                if self._interruptible_sleep_ms(delay_ms, abort_on_sensor):
                    return True

        # Final check
        if abort_on_sensor is not None and self.gpio.read(self.sensor_read) == abort_on_sensor:
            return True
        return False

    def update_shared_state(self) -> None:
        """Read all pins and push to controller for SSE."""
        sensor_val = self.gpio.read(self.sensor_read)
        updates = {
            f"gpio_{self.sensor_drive}": GpioDriver.HIGH,
            f"gpio_{self.sensor_read}": sensor_val,
        }
        for pin in self.valve_pins:
            raw = self.gpio.read(pin)
            # Convert physical to logical for display
            logical = GpioDriver.valve_level(raw, self.inverted) if self.inverted else raw
            # Actually: if inverted, physical LOW = logical HIGH (open)
            # valve_level(logical, inverted) gives physical. To reverse:
            # logical = valve_level(physical, inverted) works because it's symmetric for bool inversion
            updates[f"gpio_{pin}"] = GpioDriver.valve_level(raw, self.inverted)
        self.controller.set_gpio_states_bulk(updates)

    def _interruptible_sleep_ms(self, ms: float, abort_on_sensor: int | None = None) -> bool:
        """Sleep in 20ms increments, checking for stop/pause/sensor.
        Returns True if interrupted by sensor change.
        """
        import time
        end = time.monotonic() + ms / 1000.0
        while time.monotonic() < end:
            if self.controller.should_stop():
                return False
            if self.controller.wait_if_paused():
                return False
            if abort_on_sensor is not None and self.gpio.read(self.sensor_read) == abort_on_sensor:
                return True
            remaining = end - time.monotonic()
            if remaining > 0:
                self.controller.interruptible_sleep(min(0.02, remaining))
        return False
