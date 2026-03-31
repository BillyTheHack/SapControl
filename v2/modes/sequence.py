"""
modes/sequence.py — Sensor-driven state machine mode.

Two-state machine:
    IDLE    → sensor HIGH triggers on_sensor_high sequence → ACTIVE
    ACTIVE  → sensor LOW triggers on_sensor_low sequence  → IDLE

Each sequence has a configurable min_run_seconds. After a sequence finishes
executing its steps, the mode runner waits until min_run_seconds have elapsed
(from when the sequence started) before allowing the sensor to trigger the
opposite sequence. During the hold period the sensor is ignored.

Sequences are still interruptible by sensor changes *during step execution*
(abort_on_sensor), but only if the minimum run time has already elapsed.
"""

import logging
import time

from modes.base import BaseModeRunner
from task_logger import task_log

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
        high_min_run = high_seq.get("min_run_seconds", 0)
        low_min_run = low_seq.get("min_run_seconds", 0)

        state = _IDLE
        first_loop = True
        hold_until = 0.0  # monotonic time until which sensor is ignored
        last_sensor = -1  # track sensor changes for logging

        while not self.controller.should_stop():
            sensor_value = self.read_sensor()
            self.update_shared_state()

            # Log sensor state changes
            if sensor_value != last_sensor:
                task_log.info("SENSOR  %s", "HIGH" if sensor_value == 1 else "LOW")
                last_sensor = sensor_value

            now = time.monotonic()
            in_hold = now < hold_until

            if state == _IDLE:
                if sensor_value == 1 or first_loop:
                    if in_hold:
                        self.controller.interruptible_sleep(self.interval)
                        continue

                    skip_hold = first_loop
                    first_loop = False
                    seq_start = time.monotonic()
                    logger.info("Sensor HIGH — running '%s'", high_name)
                    task_log.info("SEQ     '%s' started", high_name)
                    self.controller.set_phase(high_name)

                    abort_val = 0 if (high_min_run == 0 or skip_hold) else None
                    interrupted = self.execute_sequence(high_steps, abort_on_sensor=abort_val)
                    self.update_shared_state()
                    duration = time.monotonic() - seq_start

                    if interrupted:
                        task_log.info("SEQ     '%s' interrupted after %.1fs — running '%s'", high_name, duration, low_name)
                        logger.info("'%s' interrupted — running '%s'", high_name, low_name)
                        self.controller.set_phase(low_name)
                        low_start = time.monotonic()
                        self.execute_sequence(low_steps, abort_on_sensor=1)
                        self.update_shared_state()
                        task_log.info("SEQ     '%s' completed (%.1fs)", low_name, time.monotonic() - low_start)
                        state = _IDLE
                        hold_until = seq_start + low_min_run if not skip_hold else 0
                        self.controller.set_phase(low_name)
                        logger.info("State → IDLE (interrupted)")
                    else:
                        task_log.info("SEQ     '%s' completed (%.1fs)", high_name, duration)
                        state = _ACTIVE
                        if not skip_hold and high_min_run > 0:
                            hold_until = seq_start + high_min_run
                            remaining = hold_until - time.monotonic()
                            if remaining > 0:
                                task_log.info("HOLD    '%s' holding for %.1fs", high_name, remaining)
                                logger.info("'%s' hold: %.1fs remaining", high_name, remaining)
                                self.controller.set_phase(f"{high_name} (hold)")
                                self._hold_wait(high_min_run, remaining)
                                task_log.info("HOLD    '%s' hold ended", high_name)
                        self.controller.set_phase(high_name)
                        self.controller.clear_hold()
                        logger.info("State → ACTIVE")

            elif state == _ACTIVE:
                if sensor_value == 0:
                    if in_hold:
                        self.controller.interruptible_sleep(self.interval)
                        continue

                    seq_start = time.monotonic()
                    logger.info("Sensor LOW — running '%s'", low_name)
                    task_log.info("SEQ     '%s' started", low_name)
                    self.controller.set_phase(low_name)

                    abort_val = 1 if low_min_run == 0 else None
                    interrupted = self.execute_sequence(low_steps, abort_on_sensor=abort_val)
                    self.update_shared_state()
                    duration = time.monotonic() - seq_start

                    if interrupted:
                        task_log.info("SEQ     '%s' interrupted after %.1fs — running '%s'", low_name, duration, high_name)
                        logger.info("'%s' interrupted — running '%s'", low_name, high_name)
                        self.controller.set_phase(high_name)
                        high_start = time.monotonic()
                        self.execute_sequence(high_steps, abort_on_sensor=0)
                        self.update_shared_state()
                        task_log.info("SEQ     '%s' completed (%.1fs)", high_name, time.monotonic() - high_start)
                        state = _ACTIVE
                        hold_until = seq_start + high_min_run
                        self.controller.set_phase(high_name)
                        logger.info("State → ACTIVE (interrupted)")
                    else:
                        task_log.info("SEQ     '%s' completed (%.1fs)", low_name, duration)
                        state = _IDLE
                        hold_until = seq_start + low_min_run
                        if low_min_run > 0:
                            remaining = hold_until - time.monotonic()
                            if remaining > 0:
                                task_log.info("HOLD    '%s' holding for %.1fs", low_name, remaining)
                                logger.info("'%s' hold: %.1fs remaining", low_name, remaining)
                                self.controller.set_phase(f"{low_name} (hold)")
                                self._hold_wait(low_min_run, remaining)
                                task_log.info("HOLD    '%s' hold ended", low_name)
                        self.controller.set_phase(low_name)
                        self.controller.clear_hold()
                        logger.info("State → IDLE")

            self.controller.interruptible_sleep(self.interval)

    def _hold_wait(self, total: float, remaining: float) -> None:
        """Wait for the hold period, pushing progress to the controller."""
        end = time.monotonic() + remaining
        while time.monotonic() < end:
            if self.controller.should_stop():
                return
            left = max(0, end - time.monotonic())
            self.controller.set_hold(total, left)
            self.update_shared_state()
            if left > 0:
                self.controller.interruptible_sleep(min(0.2, left))
