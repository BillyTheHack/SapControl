"""
modes/alternance.py — Timed alternation between N named sequences.

Cycles through sequences[0] → delay → sequences[1] → delay → ... → repeat.
Per-pin overrides are transparent — handled by BaseModeRunner.
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

                logger.info("Alternance: running '%s'", name)
                task_log.info("SEQ     '%s' started", name)
                seq_start = time.monotonic()
                self.controller.set_phase(name)

                self.execute_sequence(steps, abort_on_sensor=None)
                self.update_shared_state()
                task_log.info("SEQ     '%s' completed (%.1fs)", name, time.monotonic() - seq_start)

                if self.controller.should_stop():
                    break

                self.controller.set_phase(name)
                logger.info("Alternance: waiting %d ms after '%s'", delay_ms, name)

                if self.controller.interruptible_sleep(delay_ms / 1000.0):
                    break

                self.update_shared_state()
