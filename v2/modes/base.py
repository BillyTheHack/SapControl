"""
modes/base.py — Abstract base class for mode runners.

Mode runners use controller.write_pin_if_not_overridden() for valve writes
and controller.read_sensor() for sensor reads, so overridden pins are
automatically respected without any mode-specific override logic.
"""

import logging
import time
from abc import ABC, abstractmethod

from gpio_driver import GpioDriver
from config_manager import get_sensor_top, get_sensor_bottom, get_valve_pins, get_valve_timings

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
        sensor_top = get_sensor_top(config)
        sensor_bottom = get_sensor_bottom(config)
        self.sensor_top_drive = sensor_top["drive_gpio"]
        self.sensor_top_read = sensor_top["read_gpio"]
        self.sensor_bottom_drive = sensor_bottom["drive_gpio"]
        self.sensor_bottom_read = sensor_bottom["read_gpio"]

    @abstractmethod
    def run(self) -> None:
        """Main loop — called by controller._run() in background thread."""
        ...

    def read_sensor_top(self) -> int:
        """Read top sensor pin, respecting overrides. HIGH = tank full."""
        return self.controller.read_sensor(self.sensor_top_read)

    def read_sensor_bottom(self) -> int:
        """Read bottom sensor pin, respecting overrides. HIGH = tank empty."""
        return self.controller.read_sensor(self.sensor_bottom_read)

    def execute_sequence(self, steps: list[dict], abort_on_sensor: int | None = None,
                         hold_info: tuple[float, float] | None = None) -> bool:
        """Execute an ordered list of valve actions.

        Each step: {"valve_index": int, "state": 0|1, "delay_after_ms": int}
        Overridden pins are skipped (the step runs but the GPIO write is a no-op).
        Sensor reads go through controller.read_sensor() which returns the
        overridden value when the sensor is locked.

        Args:
            steps: List of step dicts.
            abort_on_sensor: If set, abort if sensor reads this value mid-sequence.
            hold_info: Optional (bar_total, bar_end_time) to update hold progress
                       bar during sequence execution.

        Returns True if interrupted by sensor change, False otherwise.
        """
        for step in steps:
            if self.controller.should_stop():
                return False

            if abort_on_sensor is not None and self.read_sensor() == abort_on_sensor:
                return True

            vi = step["valve_index"]
            logical = step["state"]
            pin = self.valve_pins[vi]

            # Write only if the pin is not overridden
            self.controller.write_pin_if_not_overridden(pin, logical, self.inverted)

            # Update hold bar during step execution
            if hold_info:
                bar_total, bar_end = hold_info
                self.controller.set_hold(bar_total, max(0, bar_end - time.monotonic()))

            # Wait for physical actuation
            timing = self.timings[vi] if vi < len(self.timings) else {}
            actuation_ms = timing.get("open_ms", 0) if logical == 1 else timing.get("close_ms", 0)
            if actuation_ms > 0:
                if self._interruptible_sleep_ms(actuation_ms, abort_on_sensor, hold_info):
                    return True

            # Inter-step delay
            delay_ms = step.get("delay_after_ms", 0)
            if delay_ms > 0:
                if self._interruptible_sleep_ms(delay_ms, abort_on_sensor, hold_info):
                    return True

        # Final check
        if abort_on_sensor is not None and self.read_sensor() == abort_on_sensor:
            return True
        return False

    def update_shared_state(self) -> None:
        """Read all pins and push to controller for SSE.

        For overridden pins, preserve the user's value in gpio_states
        rather than reading from hardware.
        """
        top_val = self.read_sensor_top()
        bottom_val = self.read_sensor_bottom()
        updates = {
            f"gpio_{self.sensor_top_drive}": GpioDriver.HIGH,
            f"gpio_{self.sensor_top_read}": top_val,
            f"gpio_{self.sensor_bottom_drive}": GpioDriver.HIGH,
            f"gpio_{self.sensor_bottom_read}": bottom_val,
        }
        for pin in self.valve_pins:
            if self.controller.is_pin_overridden(pin):
                # Don't overwrite — the user's value is already in gpio_states
                continue
            raw = self.gpio.read(pin)
            updates[f"gpio_{pin}"] = GpioDriver.valve_level(raw, self.inverted)
        self.controller.set_gpio_states_bulk(updates)

    def _interruptible_sleep_ms(self, ms: float, abort_on_sensor: int | None = None,
                               hold_info: tuple[float, float] | None = None) -> bool:
        """Sleep in 20ms increments, checking for stop and sensor.
        Returns True if interrupted by sensor change.
        """
        end = time.monotonic() + ms / 1000.0
        while time.monotonic() < end:
            if self.controller.should_stop():
                return False
            if abort_on_sensor is not None and self.read_sensor() == abort_on_sensor:
                return True
            if hold_info:
                bar_total, bar_end = hold_info
                self.controller.set_hold(bar_total, max(0, bar_end - time.monotonic()))
            remaining = end - time.monotonic()
            if remaining > 0:
                self.controller.interruptible_sleep(min(0.02, remaining))
        return False
