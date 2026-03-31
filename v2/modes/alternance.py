"""
modes/alternance.py — Timed alternation between N named sequences.

Cycles through sequences[0] → delay → sequences[1] → delay → ... → repeat.
Per-pin overrides are transparent — handled by BaseModeRunner.
Shows a progress bar for each sequence (steps + delay_after).
"""

import logging
import time

from modes.base import BaseModeRunner
from task_logger import task_log

logger = logging.getLogger(__name__)


class AlternanceModeRunner(BaseModeRunner):

    def run(self) -> None:
        alt_cfg = self.config.get("modes", {}).get("alternance", {})
        sequences = alt_cfg.get("sequences", [])

        if len(sequences) < 2:
            logger.error("Alternance mode requires at least 2 sequences, got %d", len(sequences))
            return

        while not self.controller.should_stop():
            for i, seq in enumerate(sequences):
                if self.controller.should_stop():
                    break

                name = seq.get("name", f"Sequence {i + 1}")
                steps = seq.get("steps", [])
                delay_ms = seq.get("delay_after_ms", 5000)

                # Calculate total time for progress bar: steps + delay
                steps_est = self._estimate_steps_duration(steps)
                delay_s = delay_ms / 1000.0
                bar_total = steps_est + delay_s
                seq_start = time.monotonic()
                bar_end = seq_start + bar_total

                logger.info("Alternance: running '%s'", name)
                task_log.info("SEQ     '%s' started", name)
                self.controller.set_phase(name)

                # Show progress bar from the start
                hi = (bar_total, bar_end) if bar_total > 0 else None
                if hi:
                    self.controller.set_hold(bar_total, bar_total)

                self.execute_sequence(steps, abort_on_sensor=None, hold_info=hi)
                self.update_shared_state()
                task_log.info("SEQ     '%s' completed (%.1fs)", name, time.monotonic() - seq_start)

                if self.controller.should_stop():
                    break

                # Wait delay, continuing the progress bar
                self.controller.set_phase(name)
                logger.info("Alternance: waiting %d ms after '%s'", delay_ms, name)

                if delay_s > 0:
                    self._delay_wait(bar_total, bar_end)

                self.controller.clear_hold()
                self.update_shared_state()

                if self.controller.should_stop():
                    break

    def _estimate_steps_duration(self, steps: list[dict]) -> float:
        """Estimate total duration of steps in seconds."""
        total_ms = 0
        for step in steps:
            vi = step["valve_index"]
            logical = step["state"]
            timing = self.timings[vi] if vi < len(self.timings) else {}
            total_ms += timing.get("open_ms", 0) if logical == 1 else timing.get("close_ms", 0)
            total_ms += step.get("delay_after_ms", 0)
        return total_ms / 1000.0

    def _delay_wait(self, bar_total: float, bar_end: float) -> None:
        """Wait until bar_end, updating the progress bar."""
        while time.monotonic() < bar_end:
            if self.controller.should_stop():
                return
            left = max(0, bar_end - time.monotonic())
            self.controller.set_hold(bar_total, left)
            self.update_shared_state()
            if left > 0:
                self.controller.interruptible_sleep(min(0.2, left))
