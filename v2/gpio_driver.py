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

    def __init__(self):
        self.mock = not _HAS_REAL_GPIO
        self._pins: dict[int, int] = {}  # mock pin state

        if _HAS_REAL_GPIO:
            self._gpio = _RPi
        else:
            logger.info("Mock GPIO driver active")

    def setup_output(self, pin: int, initial: int = 0) -> None:
        """Configure a pin as output with an initial value."""
        if self.mock:
            self._pins[pin] = initial
        else:
            self._gpio.setup(pin, self._gpio.OUT, initial=initial)

    def setup_input(self, pin: int, pull_up_down: int | None = None) -> None:
        """Configure a pin as input with optional pull-up/down."""
        if self.mock:
            self._pins.setdefault(pin, 0)
        else:
            pud = pull_up_down if pull_up_down is not None else self._gpio.PUD_DOWN
            self._gpio.setup(pin, self._gpio.IN, pull_up_down=pud)

    def read(self, pin: int) -> int:
        """Read the current value of a pin (0 or 1)."""
        if self.mock:
            return self._pins.get(pin, 0)
        return self._gpio.input(pin)

    def write(self, pin: int, value: int) -> None:
        """Write a value (0 or 1) to an output pin."""
        if self.mock:
            self._pins[pin] = value
        else:
            self._gpio.output(pin, value)

    def cleanup(self, pins: list[int] | None = None) -> None:
        """Release GPIO resources for the given pins (or all if None)."""
        if self.mock:
            if pins:
                for p in pins:
                    self._pins.pop(p, None)
            else:
                self._pins.clear()
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
