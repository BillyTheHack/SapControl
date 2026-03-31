"""
modes/sequence.py — Sensor-driven state machine mode.

Two-state machine:
    IDLE    → sensor HIGH triggers on_sensor_high sequence → ACTIVE
    ACTIVE  → sensor LOW triggers on_sensor_low sequence  → IDLE

Sequences are interruptible: if the sensor changes mid-sequence,
the opposite sequence runs immediately.

Per-pin overrides are transparent: the mode runner keeps running but
skips writing to overridden pins, and reads the overridden sensor value
when the sensor pin is locked.
"""

import logging

from modes.base import BaseModeRunner

logger = logging.getLogger(__name__)

_IDLE = "IDLE"
_ACTIVE = "ACTIVE"


class SequenceModeRunner(BaseModeRunner):

    def run(self) -> None:
        seq_cfg = self.config.get("modes", {}).get("sequence", {})
        high_seq = seq_cfg.get("on_sensor_high", {})
        low_seq = seq_cfg.get("on_sensor_low", {})

        high_name = high_seq.get("name", "On Sensor High")
        low_name = low_seq.get("name", "On Sensor Low")
        high_steps = high_seq.get("steps", [])
        low_steps = low_seq.get("steps", [])

        state = _IDLE
        first_loop = True

        while not self.controller.should_stop():
            sensor_value = self.read_sensor()
            self.update_shared_state()

            if state == _IDLE:
                if sensor_value == 1 or first_loop:
                    first_loop = False
                    logger.info("Sensor HIGH — running '%s'", high_name)
                    self.controller.set_phase(high_name)

                    interrupted = self.execute_sequence(
                        high_steps, abort_on_sensor=0,
                    )
                    self.update_shared_state()

                    if interrupted:
                        logger.info("'%s' interrupted — running '%s'", high_name, low_name)
                        self.controller.set_phase(low_name)
                        self.execute_sequence(low_steps, abort_on_sensor=1)
                        self.update_shared_state()
                        state = _IDLE
                        self.controller.set_phase(None)
                        logger.info("State → IDLE (interrupted)")
                    else:
                        state = _ACTIVE
                        self.controller.set_phase(None)
                        logger.info("State → ACTIVE")

            elif state == _ACTIVE:
                if sensor_value == 0:
                    logger.info("Sensor LOW — running '%s'", low_name)
                    self.controller.set_phase(low_name)

                    interrupted = self.execute_sequence(
                        low_steps, abort_on_sensor=1,
                    )
                    self.update_shared_state()

                    if interrupted:
                        logger.info("'%s' interrupted — running '%s'", low_name, high_name)
                        self.controller.set_phase(high_name)
                        self.execute_sequence(high_steps, abort_on_sensor=0)
                        self.update_shared_state()
                        state = _ACTIVE
                        self.controller.set_phase(None)
                        logger.info("State → ACTIVE (interrupted)")
                    else:
                        state = _IDLE
                        self.controller.set_phase(None)
                        logger.info("State → IDLE")

            self.controller.interruptible_sleep(self.interval)
