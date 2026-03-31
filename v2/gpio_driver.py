"""
gpio_driver.py — GPIO abstraction layer.

Wraps RPi.GPIO on real hardware or provides a mock for development.
All GPIO access in the application goes through a GpioDriver instance.
"""

import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Detect hardware
# ---------------------------------------------------------------------------
try:
    import RPi.GPIO as _RPi
    _RPi.setmode(_RPi.BCM)
    _RPi.setwarnings(False)
    _HAS_REAL_GPIO = True
    logger.info("RPi.GPIO loaded — running on real hardware")
except ImportError:
    _HAS_REAL_GPIO = False
    logger.warning("RPi.GPIO not found — using mock GPIO (development mode)")


class GpioDriver:
    """Unified GPIO interface — real hardware or mock."""

    OUT = 0
    IN = 1
    HIGH = 1
    LOW = 0
    PUD_DOWN = 21  # matches RPi.GPIO constant

    # Pin directions for mock: tracks whether a pin is configured as output
    _DIR_OUTPUT = "out"
    _DIR_INPUT = "in"

    def __init__(self):
        self.mock = not _HAS_REAL_GPIO
        self._pins: dict[int, int] = {}          # mock pin values
        self._pin_dirs: dict[int, str] = {}       # mock pin directions

        if _HAS_REAL_GPIO:
            self._gpio = _RPi
        else:
            logger.info("Mock GPIO driver active")

    def setup_output(self, pin: int, initial: int = 0) -> None:
        """Configure a pin as output with an initial value."""
        if self.mock:
            self._pins[pin] = initial
            self._pin_dirs[pin] = self._DIR_OUTPUT
        else:
            self._gpio.setup(pin, self._gpio.OUT, initial=initial)

    def setup_input(self, pin: int, pull_up_down: int | None = None) -> None:
        """Configure a pin as input with optional pull-up/down.

        On real hardware this resets the pin to input mode — any previous
        output value is lost. The mock mirrors this: the pin reads as 0
        (pull-down default) unless externally driven via write().
        """
        if self.mock:
            self._pins[pin] = 0  # pull-down default, replaces any previous value
            self._pin_dirs[pin] = self._DIR_INPUT
        else:
            pud = pull_up_down if pull_up_down is not None else self._gpio.PUD_DOWN
            self._gpio.setup(pin, self._gpio.IN, pull_up_down=pud)

    def read(self, pin: int) -> int:
        """Read the current value of a pin (0 or 1)."""
        if self.mock:
            return self._pins.get(pin, 0)
        return self._gpio.input(pin)

    def write(self, pin: int, value: int) -> None:
        """Write a value (0 or 1) to a pin.

        On real hardware, writing to an input pin is ignored.
        The mock mirrors this: only output pins accept writes.
        For input pins, write() is allowed only in mock mode to simulate
        external signals (e.g. sensor read override).
        """
        if self.mock:
            if self._pin_dirs.get(pin) == self._DIR_OUTPUT:
                self._pins[pin] = value
            else:
                # Allow writes to input pins in mock to simulate external signals
                self._pins[pin] = value
        else:
            self._gpio.output(pin, value)

    def cleanup(self, pins: list[int] | None = None) -> None:
        """Release GPIO resources for the given pins (or all if None).

        On real hardware, cleanup resets pins to input mode (floating).
        The mock mirrors this: cleaned-up pins revert to input reading 0.
        The pin still exists (can be read) but is no longer configured as output.
        """
        if self.mock:
            targets = pins if pins else list(self._pins.keys())
            for p in targets:
                self._pins[p] = 0            # reset to pull-down default
                self._pin_dirs[p] = self._DIR_INPUT  # reverts to input
        else:
            if pins:
                self._gpio.cleanup(pins)
            else:
                self._gpio.cleanup()

    @staticmethod
    def valve_level(logical_state: int, inverted: bool) -> int:
        """Translate logical valve state to physical GPIO level.

        logical_state: 1 = open, 0 = closed
        inverted: True for active-low relay boards (most common)
        """
        return (1 - logical_state) if inverted else logical_state
