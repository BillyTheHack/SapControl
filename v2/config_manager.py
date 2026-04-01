"""
config_manager.py — Configuration loading, saving, and validation.

Config structure:
    hardware   — GPIO pins, labels, valve timings (rarely changes)
    settings   — poll interval, default states (changes sometimes)
    mode       — active mode name ("sequence" or "alternance")
    modes      — per-mode configuration with named sequences
"""

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

VALID_MODES = ("sequence", "alternance")

DEFAULT_CONFIG = {
    "hardware": {
        "sensors": [
            {
                "drive_gpio": 8,
                "read_gpio": 7,
                "label": "Top Sensor",
            },
            {
                "drive_gpio": 9,
                "read_gpio": 11,
                "label": "Bottom Sensor",
            },
        ],
        "valves": [
            {"gpio": 27, "label": "Inlet Valve", "open_ms": 500, "close_ms": 500},
            {"gpio": 22, "label": "Outlet Valve", "open_ms": 500, "close_ms": 500},
        ],
        "valve_inverted": True,
    },
    "settings": {
        "poll_interval_ms": 500,
        "default_valve_states": [0, 0],
    },
    "mode": "sequence",
    "modes": {
        "sequence": {
            "on_sensor_high": {
                "name": "Dump",
                "steps": [
                    {"valve_index": 0, "state": 1, "delay_after_ms": 0},
                ],
            },
            "on_sensor_low": {
                "name": "Idle",
                "steps": [
                    {"valve_index": 0, "state": 0, "delay_after_ms": 0},
                ],
            },
        },
        "alternance": {
            "sequences": [
                {
                    "name": "Phase A",
                    "steps": [],
                    "delay_after_ms": 5000,
                },
                {
                    "name": "Phase B",
                    "steps": [],
                    "delay_after_ms": 5000,
                },
            ],
        },
    },
}


class ConfigManager:
    """Handles loading, saving, and validating the application config."""

    def __init__(self, config_path: Path):
        self._path = config_path

    def load(self) -> dict:
        """Load config from disk, falling back to defaults."""
        if self._path.exists():
            try:
                with open(self._path) as f:
                    return json.load(f)
            except Exception:
                logger.exception("Failed to read %s, using defaults", self._path)
        return _deep_copy(DEFAULT_CONFIG)

    def save(self, config: dict) -> None:
        """Write config to disk."""
        with open(self._path, "w") as f:
            json.dump(config, f, indent=2)
        logger.info("Config saved to %s", self._path)

    def validate(self, data: dict) -> tuple[dict | None, str | None]:
        """Validate and merge incoming data with existing config.

        Hardware fields are always preserved from the existing file.
        Returns (cleaned_config, None) on success or (None, error_msg) on failure.
        """
        try:
            existing = self.load()
            hw = existing["hardware"]
            n_valves = len(hw["valves"])

            # --- mode ---
            mode = data.get("mode", existing.get("mode", "sequence"))
            if mode not in VALID_MODES:
                return None, f"mode must be one of {VALID_MODES}"

            # --- settings ---
            settings_in = data.get("settings", existing.get("settings", {}))
            existing_settings = existing.get("settings", {})

            interval = int(settings_in.get(
                "poll_interval_ms",
                existing_settings.get("poll_interval_ms", 500),
            ))
            if not 100 <= interval <= 10000:
                return None, "settings.poll_interval_ms must be between 100 and 10000"

            raw_defaults = settings_in.get(
                "default_valve_states",
                existing_settings.get("default_valve_states", [0] * n_valves),
            )
            default_states, err = _validate_state_list(raw_defaults, n_valves, "settings.default_valve_states")
            if err:
                return None, err

            settings = {
                "poll_interval_ms": interval,
                "default_valve_states": default_states,
            }

            # --- hardware (editable: valve timings + valve_inverted) ---
            hw_in = data.get("hardware", {})
            valve_inverted = bool(hw_in.get("valve_inverted", hw.get("valve_inverted", True)))

            valves = []
            valves_in = hw_in.get("valves", [])
            for i, existing_valve in enumerate(hw["valves"]):
                v = dict(existing_valve)
                # Allow timing updates from UI
                if i < len(valves_in):
                    v_in = valves_in[i]
                    if "open_ms" in v_in:
                        open_ms = int(v_in["open_ms"])
                        if not 0 <= open_ms <= 30000:
                            return None, f"hardware.valves[{i}].open_ms must be 0-30000"
                        v["open_ms"] = open_ms
                    if "close_ms" in v_in:
                        close_ms = int(v_in["close_ms"])
                        if not 0 <= close_ms <= 30000:
                            return None, f"hardware.valves[{i}].close_ms must be 0-30000"
                        v["close_ms"] = close_ms
                valves.append(v)

            hardware = {
                "sensors": hw["sensors"],  # read-only
                "valves": valves,
                "valve_inverted": valve_inverted,
            }

            # --- modes.sequence ---
            modes_in = data.get("modes", existing.get("modes", {}))
            existing_modes = existing.get("modes", {})

            seq_in = modes_in.get("sequence", existing_modes.get("sequence", {}))
            existing_seq = existing_modes.get("sequence", {})

            on_high, err = _validate_named_sequence(
                seq_in.get("on_sensor_high", existing_seq.get("on_sensor_high", {})),
                n_valves, "modes.sequence.on_sensor_high",
            )
            if err:
                return None, err

            on_low, err = _validate_named_sequence(
                seq_in.get("on_sensor_low", existing_seq.get("on_sensor_low", {})),
                n_valves, "modes.sequence.on_sensor_low",
            )
            if err:
                return None, err

            # --- modes.alternance ---
            alt_in = modes_in.get("alternance", existing_modes.get("alternance", {}))
            existing_alt = existing_modes.get("alternance", {})

            raw_alt_seqs = alt_in.get("sequences", existing_alt.get("sequences", []))
            if not isinstance(raw_alt_seqs, list) or len(raw_alt_seqs) < 2:
                return None, "modes.alternance.sequences must be a list with at least 2 entries"

            alt_sequences = []
            for i, raw_seq in enumerate(raw_alt_seqs):
                ns, err = _validate_named_sequence(raw_seq, n_valves, f"modes.alternance.sequences[{i}]")
                if err:
                    return None, err
                delay = int(raw_seq.get("delay_after_ms", 5000))
                if not 0 <= delay <= 300000:
                    return None, f"modes.alternance.sequences[{i}].delay_after_ms must be 0-300000"
                ns["delay_after_ms"] = delay
                alt_sequences.append(ns)

            return {
                "hardware": hardware,
                "settings": settings,
                "mode": mode,
                "modes": {
                    "sequence": {
                        "on_sensor_high": on_high,
                        "on_sensor_low": on_low,
                    },
                    "alternance": {
                        "sequences": alt_sequences,
                    },
                },
            }, None

        except (KeyError, TypeError, ValueError) as exc:
            return None, f"Invalid config: {exc}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def get_sensor_top(config: dict) -> dict:
    """Return the top sensor config (index 0). HIGH = tank full."""
    return config["hardware"]["sensors"][0]


def get_sensor_bottom(config: dict) -> dict:
    """Return the bottom sensor config (index 1). HIGH = tank empty."""
    return config["hardware"]["sensors"][1]


def get_valve_pins(config: dict) -> list[int]:
    """Extract ordered list of valve GPIO pin numbers from config."""
    return [v["gpio"] for v in config["hardware"]["valves"]]


def get_valve_labels(config: dict) -> list[str]:
    """Extract ordered list of valve labels from config."""
    return [v["label"] for v in config["hardware"]["valves"]]


def get_valve_timings(config: dict) -> list[dict]:
    """Extract list of {open_ms, close_ms} dicts per valve."""
    return [{"open_ms": v.get("open_ms", 0), "close_ms": v.get("close_ms", 0)}
            for v in config["hardware"]["valves"]]


def _validate_steps(steps: list, n_valves: int, path: str) -> tuple[list | None, str | None]:
    """Validate a list of sequence steps."""
    if not isinstance(steps, list):
        return None, f"{path} must be a list"
    result = []
    for i, step in enumerate(steps):
        vi = int(step.get("valve_index", -1))
        state = int(step.get("state", 0))
        delay = int(step.get("delay_after_ms", 0))
        if not 0 <= vi < n_valves:
            return None, f"{path}[{i}].valve_index {vi} out of range 0-{n_valves - 1}"
        if state not in (0, 1):
            return None, f"{path}[{i}].state must be 0 or 1"
        if not 0 <= delay <= 300000:
            return None, f"{path}[{i}].delay_after_ms must be 0-300000"
        result.append({"valve_index": vi, "state": state, "delay_after_ms": delay})
    return result, None


def _validate_named_sequence(data: dict, n_valves: int, path: str) -> tuple[dict | None, str | None]:
    """Validate a named sequence object {name, steps, min_run_seconds, ...}."""
    if not isinstance(data, dict):
        return None, f"{path} must be an object"
    name = str(data.get("name", "")).strip()
    if not name:
        return None, f"{path}.name is required"
    if len(name) > 60:
        return None, f"{path}.name must be 60 characters or fewer"

    steps, err = _validate_steps(data.get("steps", []), n_valves, f"{path}.steps")
    if err:
        return None, err

    min_run = int(data.get("min_run_seconds", 0))
    if not 0 <= min_run <= 3600:
        return None, f"{path}.min_run_seconds must be 0-3600"

    min_run_extra = bool(data.get("min_run_extra", True))

    return {"name": name, "steps": steps, "min_run_seconds": min_run, "min_run_extra": min_run_extra}, None


def _validate_state_list(raw: list, n_valves: int, path: str) -> tuple[list | None, str | None]:
    """Validate a list of 0/1 values with one entry per valve."""
    if not isinstance(raw, list) or len(raw) != n_valves:
        return None, f"{path} must have {n_valves} entries"
    result = []
    for i, v in enumerate(raw):
        s = int(v)
        if s not in (0, 1):
            return None, f"{path}[{i}] must be 0 or 1"
        result.append(s)
    return result, None


def _deep_copy(obj):
    """Simple deep copy for JSON-compatible structures."""
    return json.loads(json.dumps(obj))
