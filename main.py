"""
Enviro + Air Quality Dashboard — Main Entry Point

Runs three concurrent services:
  1. Display loop — reads sensors, renders on the 160×80 LCD
  2. Database logger — records readings to SQLite every N seconds
  3. Web dashboard — Flask server on port 5000

Usage:
  python main.py

Signal handling:
  Ctrl+C — graceful shutdown (saves state, turns off display)
"""

import logging
import signal
import sys
import threading
import time
from datetime import datetime, timezone

from config import Config
from sensors import EnviroSensors
from display import EnviroDisplay
from database import SensorDatabase
from sync import NasSync
from web.app import app, init_web

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("main")

# Silence noisy libraries
logging.getLogger("flask").setLevel(logging.WARNING)
logging.getLogger("werkzeug").setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------
running = True
sensors = None
display = None
db = None
sync_service = None

# Screen management
current_screen = 0  # Default: Temperature + Humidity screen
last_tap_time = 0
TOTAL_SCREENS = 4  # 0=Temp/Hum, 1=Pressure, 2=AQ, 3=Gas Detail


def signal_handler(signum, frame):
    """Handle Ctrl+C for graceful shutdown."""
    global running
    logger.info("Shutdown signal received...")
    running = False


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


def init():
    """Initialize all subsystems."""
    global sensors, display, db, sync_service

    # Sensors
    logger.info("Initializing sensors...")
    sensors = EnviroSensors()
    sensors.init()

    # Display
    logger.info("Initializing display...")
    display = EnviroDisplay()
    display.init()

    # Database
    logger.info(f"Initializing database: {Config.DB_PATH}")
    db = SensorDatabase(Config.DB_PATH)

    # NAS Sync
    if Config.SYNC_ENABLED:
        sync_service = NasSync(
            db_path=Config.DB_PATH,
            remote=Config.SYNC_REMOTE,
            interval=Config.SYNC_INTERVAL,
            max_retries=Config.SYNC_MAX_RETRIES,
            enabled=True,
        )
        sync_service.start()
    else:
        sync_service = NasSync(
            db_path=Config.DB_PATH,
            remote="",
            enabled=False,
        )

    # Web dashboard
    init_web(db, sensors, sync_service)
    logger.info(f"Web dashboard: http://{Config.WEB_HOST}:{Config.WEB_PORT}")

    # Start gas sensor warmup
    sensors.start_warmup()
    logger.info(f"Gas sensor warming up ({Config.WARMUP_SECONDS}s)...")


def build_display_data(raw, screen):
    """Build the data dict for the display renderer."""
    from datetime import datetime
    daily = db.daily_high_low() if db else {}

    data = {
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
        "gas_calibrated": raw.get("gas_calibrated"),
        "ldr": raw.get("ldr"),
        "proximity": sensors.read_proximity(),
        "clock": datetime.now().strftime("%H:%M:%S"),
        "unit": Config.UNIT,
    }

    # Daily H/L
    if Config.UNIT == "F":
        th = daily.get("temp_high")
        tl = daily.get("temp_low")
        data["temp_high"] = (th * 9/5 + 32) if th is not None else None
        data["temp_low"] = (tl * 9/5 + 32) if tl is not None else None
    else:
        data["temp_high"] = daily.get("temp_high")
        data["temp_low"] = daily.get("temp_low")

    data["hum_high"] = daily.get("hum_high")
    data["hum_low"] = daily.get("hum_low")
    data["pressure_high"] = daily.get("pressure_high")
    data["pressure_low"] = daily.get("pressure_low")
    data["light_high"] = daily.get("light_high")
    data["light_low"] = daily.get("light_low")

    return data


def display_loop():
    """Main loop: read sensors, render display, detect taps."""
    global current_screen, last_tap_time, running

    prev_tap = 0
    prev_covered = False
    double_tap_window = 0.8  # seconds

    while running:
        try:
            raw = sensors.read_all()

            # Read proximity fresh each iteration (bypasses the read cache)
            proximity = sensors.read_proximity()
            covered = proximity is not None and proximity > Config.TAP_THRESHOLD

            # Rising edge = a tap. A held finger fires once, not repeatedly.
            if covered and not prev_covered:
                now = time.time()
                if now - prev_tap > Config.TAP_DEBOUNCE:
                    if prev_tap > 0 and now - prev_tap <= double_tap_window:
                        # Double-tap: toggle unit
                        import configparser
                        config = configparser.ConfigParser()
                        config.read(Config.SETTINGS_PATH)
                        current = config.get("sensors", "unit", fallback="C")
                        new = "F" if current.upper() == "C" else "C"
                        config.set("sensors", "unit", new)
                        with open(Config.SETTINGS_PATH, "w") as f:
                            config.write(f)
                        Config.UNIT = new
                        logger.info(f"Temperature unit toggled to {new}")
                    else:
                        # Single tap: switch screen
                        current_screen = (current_screen + 1) % TOTAL_SCREENS
                        logger.info(f"Screen → {current_screen}")
                    prev_tap = now
            prev_covered = covered

            # Render the current screen
            if not sensors.gas_is_warmed_up:
                display.render_warmup(sensors.warmup_progress)
            else:
                data = build_display_data(raw, current_screen)
                renderers = [
                    display.render_screen_0_temp_humidity,
                    display.render_screen_1_pressure,
                    display.render_screen_2_air_quality,
                    display.render_screen_3_gas_detail,
                ]
                if current_screen < len(renderers):
                    renderers[current_screen](data)

        except Exception as e:
            logger.error(f"Display loop error: {e}")

        time.sleep(0.5)  # 500ms loop interval


def logging_loop():
    """Background loop: record sensor readings to SQLite."""
    global running
    last_record = 0
    boot_time = time.time()

    while running:
        try:
            now = time.time()
            if now - last_record >= Config.RECORD_INTERVAL:
                if not sensors.gas_is_warmed_up:
                    logger.debug("Skipping recording — gas sensor still warming up")
                elif now - boot_time < 60:
                    logger.debug("Skipping recording — sensor stabilization period")
                else:
                    raw = sensors.read_all()
                    db.insert_reading(raw)
                    last_record = now
                    logger.debug(f"Recorded reading (total: {db.reading_count()})")
        except Exception as e:
            logger.error(f"Logging loop error: {e}")
        time.sleep(1)


def web_loop():
    """Run the Flask web server."""
    logger.info(f"Starting web server on {Config.WEB_HOST}:{Config.WEB_PORT}")
    app.run(
        host=Config.WEB_HOST,
        port=Config.WEB_PORT,
        debug=False,
        use_reloader=False,
        threaded=True,
    )


def main():
    """Entry point."""
    init()

    # Start background threads
    threads = [
        threading.Thread(target=logging_loop, daemon=True, name="logger"),
        threading.Thread(target=web_loop, daemon=True, name="web"),
    ]

    for t in threads:
        t.start()

    logger.info("All systems go. Press Ctrl+C to stop.")

    # Run display loop in main thread (so Ctrl+C interrupts it)
    try:
        display_loop()
    except KeyboardInterrupt:
        pass
    finally:
        logger.info("Shutting down...")
        if display:
            display.shutdown()
        if sensors:
            sensors.shutdown()
        if sync_service:
            sync_service.stop()
        logger.info("Goodbye.")


if __name__ == "__main__":
    main()
