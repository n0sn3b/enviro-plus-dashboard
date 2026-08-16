import configparser
import os

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(PROJECT_ROOT, "settings.ini")


def _load():
    config = configparser.ConfigParser()
    config.read(SETTINGS_PATH)
    return config


_config = _load()


class Config:
    SETTINGS_PATH = SETTINGS_PATH

    # Display
    TAP_DEBOUNCE = float(_config.get("display", "tap_debounce", fallback="0.4"))
    # LTR-559 proximity count above which a "tap" is registered (0-2047)
    TAP_THRESHOLD = int(_config.get("display", "tap_threshold", fallback="300"))

    # Sensors
    UNIT = _config.get("sensors", "unit", fallback="C").upper()
    WARMUP_SECONDS = int(_config.get("sensors", "warmup_seconds", fallback="10"))
    READ_INTERVAL = float(_config.get("sensors", "read_interval", fallback="2"))

    # Calibration offsets (added to raw readings)
    TEMP_OFFSET = float(_config.get("sensors", "temp_offset", fallback="0.0"))
    HUMIDITY_OFFSET = float(_config.get("sensors", "humidity_offset", fallback="0.0"))
    PRESSURE_OFFSET = float(_config.get("sensors", "pressure_offset", fallback="0.0"))
    LIGHT_OFFSET = float(_config.get("sensors", "light_offset", fallback="0.0"))

    # Compensation modes: temperature "polynomial" (cubic, roscoe81) or
    # "offset"; humidity "quadratic" (unit-specific!) or "offset".
    TEMP_COMP_MODE = _config.get("sensors", "temp_comp_mode", fallback="offset").lower()
    HUM_COMP_MODE = _config.get("sensors", "hum_comp_mode", fallback="offset").lower()

    # Altitude in meters for sea-level (QNH) pressure compensation.
    ALTITUDE = float(_config.get("sensors", "altitude", fallback="0.0"))

    # Gas calibration: hour (0-23) for daily 7-day rolling R0 drift
    # calibration, and a one-shot flag to clear the stored baseline.
    GAS_CALIBRATION_HOUR = int(_config.get("sensors", "gas_calibration_hour", fallback="3"))
    RESET_GAS_CALIBRATION = _config.get("sensors", "reset_gas_calibration", fallback="false").lower() == "true"
    GAS_CAL_FILE = os.path.join(PROJECT_ROOT, "gas_calibration.json")

    # Logging
    RECORD_INTERVAL = int(_config.get("logging", "record_interval", fallback="60"))
    DB_PATH = os.path.join(
        PROJECT_ROOT,
        _config.get("logging", "db_path", fallback="enviro_data.db"),
    )

    # Sync
    SYNC_ENABLED = _config.get("sync", "enabled", fallback="false").lower() == "true"
    SYNC_REMOTE = _config.get("sync", "remote", fallback="")
    SYNC_INTERVAL = int(_config.get("sync", "interval", fallback="300"))
    SYNC_MAX_RETRIES = int(_config.get("sync", "max_retries", fallback="3"))

    # Web
    WEB_HOST = _config.get("web", "host", fallback="0.0.0.0")
    WEB_PORT = int(_config.get("web", "port", fallback="5000"))
    WEB_REFRESH = int(_config.get("web", "refresh_interval", fallback="5"))


def reload():
    """Reload settings from disk (used after user edits settings.ini)."""
    global _config
    _config = _load()
