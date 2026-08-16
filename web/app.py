"""
Flask web dashboard for the Enviro sensor system.

Endpoints:
  GET  /              — Current sensor readings
  GET  /history       — Historical charts
  GET  /settings      — Configuration page
  GET  /api/current   — JSON current readings (for AJAX refresh)
  GET  /api/chart?sensor=temperature&hours=24  — JSON chart data
  GET  /export?hours=24  — CSV download
  POST /settings      — Save settings
  POST /calibrate     — Save calibration offsets
"""

import csv
import io
import logging
import os
import time

from flask import (
    Flask, jsonify, render_template, request, redirect, url_for
)

# Make parent dir importable
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config, reload
from database import SensorDatabase

logger = logging.getLogger(__name__)

app = Flask(__name__)

# Will be set by main.py at startup
db = None
sensors = None
sync_service = None


def init_web(db_instance, sensors_instance, sync_instance=None):
    """Initialize global references (called from main.py)."""
    global db, sensors, sync_service
    db = db_instance
    sensors = sensors_instance
    sync_service = sync_instance


@app.route("/")
def current():
    """Current sensor readings page."""
    return render_template(
        "current.html",
        refresh_interval=Config.WEB_REFRESH,
    )


@app.route("/history")
def history():
    """Historical charts page."""
    return render_template("history.html")


@app.route("/settings")
def settings_page():
    """Settings page."""
    return render_template(
        "settings.html",
        config=Config,
        sync_enabled=sync_service.is_synced if sync_service else False,
        db_path=Config.DB_PATH,
        reading_count=db.reading_count() if db else 0,
    )


@app.route("/api/current")
def api_current():
    """
    JSON endpoint for current readings.

    Returns:
    {
        "timestamp": "2025-01-01T12:00:00",
        "temperature": 22.5,
        "temperature_display": "22.5°C",
        "humidity": 45.2,
        "pressure": 1013.25,
        "light": 500,
        "no2_quality": "Good",
        "co_quality": "Good",
        "nh3_quality": "Good",
        "no2_raw": 6500,
        "co_raw": 7200,
        "nh3_raw": 8000,
        "ldr": 7500,
        "unit": "C",
        "daily": {
            "temp_high": 24.0, "temp_low": 18.0,
            ...
        },
        "gas_warmed_up": true,
        "warmup_progress": 1.0,
    }
    """
    if sensors is None:
        return jsonify({"error": "Sensors not initialized"}), 503

    raw = sensors.read_all()
    daily = db.daily_high_low() if db else {}

    from datetime import datetime, timezone
    data = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "temperature": raw.get("temperature"),
        "temperature_display": sensors.temperature_display(),
        "humidity": raw.get("humidity"),
        "pressure": raw.get("pressure"),
        "light": raw.get("light"),
        "no2_quality": raw.get("no2_quality", "N/A"),
        "co_quality": raw.get("co_quality", "N/A"),
        "nh3_quality": raw.get("nh3_quality", "N/A"),
        "no2_raw": raw.get("no2_raw"),
        "co_raw": raw.get("co_raw"),
        "nh3_raw": raw.get("nh3_raw"),
        "no2_ppm": raw.get("no2_ppm"),
        "co_ppm": raw.get("co_ppm"),
        "nh3_ppm": raw.get("nh3_ppm"),
        "no2_r0": raw.get("no2_r0"),
        "co_r0": raw.get("co_r0"),
        "nh3_r0": raw.get("nh3_r0"),
        "gas_calibrated": raw.get("gas_calibrated"),
        "altitude": Config.ALTITUDE,
        "ldr": raw.get("ldr"),
        "unit": Config.UNIT,
        "daily": daily,
        "gas_warmed_up": sensors.gas_is_warmed_up,
        "warmup_progress": sensors.warmup_progress,
    }
    return jsonify(data)


@app.route("/api/chart")
def api_chart():
    """
    JSON chart data for a single sensor.

    Query params:
        sensor: sensor name (temperature, humidity, pressure, light,
                no2_raw, co_raw, nh3_raw, ldr)
        hours:  how many hours of history (default 24)

    Returns:
    {
        "sensor": "temperature",
        "hours": 24,
        "data": [
            {"timestamp": "2025-01-01T12:00:00", "value": 22.5},
            ...
        ]
    }
    """
    sensor = request.args.get("sensor", "temperature")
    hours = int(request.args.get("hours", "24"))

    if db is None:
        return jsonify({"error": "Database not initialized"}), 503

    data = db.readings_for_chart(sensor, hours=hours)
    return jsonify({"sensor": sensor, "hours": hours, "data": data})


@app.route("/export")
def export():
    """Export readings as CSV download."""
    hours = int(request.args.get("hours", "24"))
    if db is None:
        return "Database not initialized", 503

    csv_data = db.export_csv(hours=hours)
    if not csv_data:
        return "No data to export", 404

    response = app.make_response(csv_data)
    response.headers["Content-Type"] = "text/csv"
    response.headers["Content-Disposition"] = (
        f"attachment; filename=enviro_data_{time.strftime('%Y%m%d')}.csv"
    )
    return response


@app.route("/settings", methods=["POST"])
def save_settings():
    """Save web dashboard settings to settings.ini."""
    data = request.get_json(silent=True) or request.form.to_dict()

    config_values = {
        "display": {
            "tap_debounce": data.get("tap_debounce", "0.4"),
            "tap_threshold": data.get("tap_threshold", "300"),
        },
        "sensors": {
            "unit": data.get("unit", "C"),
            "warmup_seconds": data.get("warmup_seconds", "10"),
            "read_interval": data.get("read_interval", "2"),
            "temp_comp_mode": data.get("temp_comp_mode", "polynomial"),
            "hum_comp_mode": data.get("hum_comp_mode", "offset"),
            "altitude": data.get("altitude", "0.0"),
            "gas_calibration_hour": data.get("gas_calibration_hour", "3"),
            "reset_gas_calibration": str(data.get("reset_gas_calibration", False)).lower(),
        },
        "logging": {
            "record_interval": data.get("record_interval", "60"),
        },
        "sync": {
            "enabled": str(data.get("sync_enabled", False)).lower(),
            "remote": data.get("sync_remote", ""),
            "interval": data.get("sync_interval", "300"),
        },
        "web": {
            "port": data.get("port", "5000"),
            "refresh_interval": data.get("refresh_interval", "5"),
        },
    }

    # Write to settings.ini
    import configparser
    config = configparser.ConfigParser()
    for section, keys in config_values.items():
        config.add_section(section)
        for key, value in keys.items():
            config.set(section, key, str(value))

    with open(Config.SETTINGS_PATH, "w") as f:
        config.write(f)

    reload()
    return jsonify({"status": "ok", "message": "Settings saved"})


@app.route("/calibrate", methods=["POST"])
def save_calibration():
    """Save calibration offsets to settings.ini."""
    data = request.get_json(silent=True) or request.form.to_dict()

    import configparser
    config = configparser.ConfigParser()
    config.read(Config.SETTINGS_PATH)

    if not config.has_section("sensors"):
        config.add_section("sensors")

    cal_fields = {
        "temp_offset": "temp_offset",
        "humidity_offset": "humidity_offset",
        "pressure_offset": "pressure_offset",
        "light_offset": "light_offset",
        "altitude": "altitude",
    }

    for form_key, ini_key in cal_fields.items():
        val = data.get(form_key, "0")
        try:
            config.set("sensors", ini_key, str(float(val)))
        except ValueError:
            pass

    with open(Config.SETTINGS_PATH, "w") as f:
        config.write(f)

    reload()
    return jsonify({"status": "ok", "message": "Calibration saved"})


@app.route("/api/gas/calibrate", methods=["POST"])
def gas_calibrate():
    """Take a fresh clean-air R0 baseline immediately.

    Put the sensor in clean/fresh air before calling this. The heater must
    be warmed up, otherwise the baseline will be unreliable.
    """
    if sensors is None:
        return jsonify({"error": "Sensors not initialized"}), 503
    if not sensors.gas_is_warmed_up:
        return jsonify({"error": "Gas sensor still warming up"}), 400

    raw = sensors.read_all()
    gas = {"co": raw.get("co_raw"), "no2": raw.get("no2_raw"), "nh3": raw.get("nh3_raw")}
    ok = sensors.calibration.take_baseline(
        gas, raw.get("temperature_raw"), raw.get("humidity_raw"), raw.get("pressure_raw")
    )
    if ok:
        return jsonify({
            "status": "ok",
            "message": "Clean-air baseline updated",
            "r0": {
                "no2": sensors.calibration.oxi_r0,
                "co": sensors.calibration.red_r0,
                "nh3": sensors.calibration.nh3_r0,
            },
        })
    return jsonify({"error": "Could not establish baseline (bad readings?)"}), 400


@app.route("/sync/force", methods=["POST"])
def force_sync():
    """Manually trigger a sync to NAS."""
    if sync_service is None:
        return jsonify({"error": "Sync not configured"}), 503
    try:
        sync_service.force_sync()
        return jsonify({"status": "ok", "message": "Sync complete"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/purge", methods=["POST"])
def purge_old():
    """Purge old readings (keep last N days)."""
    if db is None:
        return jsonify({"error": "Database not initialized"}), 503
    keep_days = int(request.args.get("days", "90"))
    deleted = db.purge_old(keep_days=keep_days)
    return jsonify({"status": "ok", "deleted": deleted})


# ---------------------------------------------------------------------------
# Navigation templates
# ---------------------------------------------------------------------------

NAV_HTML = """
<nav class="nav">
    <a href="/">Current</a>
    <a href="/history">History</a>
    <a href="/settings">Settings</a>
</nav>
"""
