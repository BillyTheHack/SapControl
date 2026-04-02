"""
task_logger.py — Dedicated task event log.

Writes only task-relevant events to task.log:
    - Sensor state changes
    - Sequence start/end with duration
    - Manual override changes
    - Task start/stop

Separate from the application logger — no Flask noise, no GPIO debug.
"""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOG_PATH = Path(__file__).parent / "task.log"

# Create a dedicated logger that only writes to the task log file
task_log = logging.getLogger("task_events")
task_log.setLevel(logging.INFO)
task_log.propagate = False  # don't bubble up to root logger

# Rotate at 50 MB, keep 7 backups (roughly one week of history)
_handler = RotatingFileHandler(_LOG_PATH, maxBytes=50 * 1024 * 1024, backupCount=7)
_handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
task_log.addHandler(_handler)
