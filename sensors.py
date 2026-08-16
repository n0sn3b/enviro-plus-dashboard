"""
Sensor drivers for Pimoroni Enviro + Air Quality (PIM458).

Sensors on this board:
  - BME280    — Temperature, Humidity, Pressure (I2C 0x76)
  - LTR-559   — Ambient light + Proximity (I2C)
  - ADS1015   — 12-bit ADC (I2C 0x49), reads MICS-6814 gas channels
  - MICS-6814 — Analog NO2 / CO / NH3 gas sensor (via ADC, heater-controlled)

IMPORTANT: Install the dependencies in the project venv first:
  pip install -r requirements.txt

The Pimoroni libraries install under these module names:
  bme280           (package: pimoroni-bme280)
  ltr559           (package: ltr559)
  ads1015          (package: ads1015)
"""

import logging
import time

from calibration import (
    GasCalibration,
    compensate_humidity,
    compensate_temperature,
    qnh_pressure,
    quality_from_ppm,
)

logger = logging.getLogger("sensors")

# ---------------------------------------------------------------------------
# Pimoroni library imports
# ---------------------------------------------------------------------------
try:
    from bme280 import BME280
except ImportError:
    BME280 = None

try:
    from ltr559 import LTR559
except ImportError:
    LTR559 = None

try:
    from ads1015 import ADS1015
except ImportError:
    ADS1015 = None

# GPIO for MICS-6814 heater control (from schematic)
try:
    from gpiozero import OutputDevice
except ImportError:
    OutputDevice = None

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
I2C_BUS = 1  # Raspberry Pi I2C1

BME280_I2C_ADDR = 0x76
ADS1015_I2C_ADDR = 0x49  # verify on your board (may be 0x48)

# LTR-559 proximity is a raw 11-bit count (0-2047): a large value
# means something is covering the sensor. Adjust after calibration.
PROXIMITY_TAP_THRESHOLD = 400

# MICS-6814 gas channels on ADS1015 (from schematic)
CH_NO2 = 0   # AIN0 — NO2 (OX)
CH_CO = 1    # AIN1 — CO (RED)
CH_NH3 = 2   # AIN2 — NH3

# LDR on ADC channel 3
CH_LDR = 3   # AIN3 — light-dependent resistor

# MICS-6814 heater enable GPIO.
# Enviro+ (PIM458) routes the heater MOSFET to GPIO24 (per Pimoroni's
# own enviroplus gas driver). The heater MUST be on for gas readings.
HEATER_GPIO = 24

# Gas concentration thresholds for AQI-like classification
# These are simplified approximations — real AQI uses EPA breakpoints.
# The gas driver reports MICS-6814 SENSOR RESISTANCE (ohms): higher
# resistance = cleaner air. Calibrate these against known conditions.
NO2_GOOD = 15000      # ohms and above = clean
NO2_MODERATE = 8000   # ohms
CO_GOOD = 15000
CO_MODERATE = 8000
NH3_GOOD = 15000
NH3_MODERATE = 8000


class MICS6814Driver:
    """
    Drive the MICS-6814 analog gas sensor via the ADS1015 ADC.

    The MICS-6814 has three internal heating zones for NO2, CO, and NH3.
    Only one zone can be heated at a time, so we cycle through them.
    Each zone needs ~20 seconds of warmup before the first reading,
    then ~2 seconds between subsequent reads.
    """

    def __init__(self, ads1015):
        self.ads = ads1015
        self.heater = OutputDevice(HEATER_GPIO) if (OutputDevice and HEATER_GPIO) else None
        self._warmed_up = False
        self._warmup_start = None
        # Heater resistance selection (from schematic):
        # NO2 (OX): R3=62ohm, CO (RED): R8=28ohm, NH3: R15=36ohm
        self._heater_pins = {
            "no2": None,  # These would be individual heater control pins
            "co": None,
            "nh3": None,
        }

    def start_warmup(self):
        """Start the heater warmup sequence. Takes ~10 seconds."""
        if self.heater:
            self.heater.on()
        self._warmup_start = time.time()
        self._warmed_up = False

    @property
    def is_warmed_up(self):
        from config import Config
        if self._warmup_start is None:
            return False
        elapsed = time.time() - self._warmup_start
        self._warmed_up = elapsed >= Config.WARMUP_SECONDS
        return self._warmed_up

    @property
    def warmup_progress(self):
        """Return warmup progress as 0.0–1.0."""
        from config import Config
        if self._warmup_start is None:
            return 0.0
        if self._warmed_up:
            return 1.0
        elapsed = time.time() - self._warmup_start
        return min(elapsed / Config.WARMUP_SECONDS, 1.0)

    def _read_resistance(self, channel, ref_voltage=3.3, load_resistance=56000):
        """
        Read the resistance of a MICS-6814 channel.

        The sensor element sits in a divider with a 56 kOhm load resistor
        between 3.3 V and the ADC input. Reading its voltage lets us work
        back to the sensor element resistance:
            Rs = (V * RL) / (3.3 - V)
        """
        channel_names = {
            CH_NO2: "in0/gnd",
            CH_CO: "in1/gnd",
            CH_NH3: "in2/gnd",
        }
        volts = self.ads.get_voltage(channel_names[channel])
        volts = max(0.0, min(volts, ref_voltage - 0.05))
        if volts <= 0.001:
            return 0
        resistance = (volts * load_resistance) / (ref_voltage - volts)
        return int(round(resistance))

    def read_all(self):
        """
        Read all three gas channels. Returns dict of sensor resistances in ohms.
        Must be called after warmup is complete.
        """
        if not self.is_warmed_up:
            return {"no2": None, "co": None, "nh3": None}

        return {
            "no2": self._read_resistance(CH_NO2),
            "co": self._read_resistance(CH_CO),
            "nh3": self._read_resistance(CH_NH3),
        }

    def read_ldr(self):
        """Read the onboard LDR (light-dependent resistor) in ohms."""
        volts = self.ads.get_voltage("in3/gnd")
        volts = max(0.0, min(volts, 3.25))
        if volts <= 0.001:
            return 0
        # 100 kOhm reference resistor divider on the Enviro+ board
        return int(round((volts * 100000) / (3.3 - volts)))

    def shutdown(self):
        """Turn the heater off (power saving)."""
        if self.heater:
            try:
                self.heater.off()
            except Exception:
                pass


def adc_to_gas_quality(adc_value):
    """
    Convert a MICS-6814 sensor resistance reading to a quality indicator.

    The sensor element resistance (ohms) decreases as gas concentration
    increases, so higher resistance = cleaner air.

    This is a simplified model — for accurate ppm readings you need to
    calibrate against known gas concentrations.

    Returns: (label, color_key)
        label: "Good", "Moderate", "Poor", "Unhealthy"
        color_key: "green", "yellow", "orange", "red"
    """
    if adc_value is None:
        return "N/A", "grey"
    # Simplified thresholds in ohms — adjust after calibration
    if adc_value > 15000:
        return "Good", "green"
    elif adc_value > 8000:
        return "Moderate", "yellow"
    elif adc_value > 4000:
        return "Poor", "orange"
    else:
        return "Unhealthy", "red"


class EnviroSensors:
    """
    Main sensor interface for the Enviro + Air Quality board.

    Usage:
        sensors = EnviroSensors()
        sensors.init()
        sensors.start_warmup()   # Start MICS-6814 heater

        while not sensors.gas_is_warmed_up:
            time.sleep(1)

        readings = sensors.read_all()
        # readings = {
        #     "temperature": 22.5,      # °C
        #     "humidity": 45.2,          # %
        #     "pressure": 1013.25,       # hPa
        #     "light": 500,              # lux (LTR-559)
        #     "proximity": 0.85,         # 0.0–1.0 (LTR-559)
        #     "ldr": 75000,              # ohms (onboard LDR)
        #     "no2_raw": 65000,          # ohms (MICS-6814 sensor resistance)
        #     "co_raw": 72000,           # ohms
        #     "nh3_raw": 80000,          # ohms
        #     "no2_quality": "Good",     # human-readable
        #     "co_quality": "Good",
        #     "nh3_quality": "Good",
        # }
    """

    def __init__(self):
        self.bme = None
        self.ltr = None
        self.ads = None
        self.mics = None
        self._last_read = {}
        self._last_read_time = 0
        from config import Config
        self.calibration = GasCalibration(Config.GAS_CAL_FILE)

    def init(self):
        """Initialize all sensors. Call once at startup."""

        # BME280 — Temperature, Humidity, Pressure
        if BME280:
            try:
                self.bme = BME280(i2c_addr=BME280_I2C_ADDR)
                self.bme.setup(mode="forced")
                logger.info(f"BME280 OK (0x{BME280_I2C_ADDR:02x})")
            except Exception as e:
                logger.warning(f"BME280 init failed: {e}")
                self.bme = None
        else:
            logger.warning("pimoroni_bme280 not installed")

        # LTR-559 — Ambient Light + Proximity
        if LTR559:
            try:
                self.ltr = LTR559()
                self.ltr.set_light_options(active=True, gain=4)
                logger.info("LTR559 OK")
            except Exception as e:
                logger.warning(f"LTR559 init failed: {e}")
                self.ltr = None
        else:
            logger.warning("ltr559 not installed")

        # ADS1015 — 12-bit ADC for MICS-6814 + LDR
        if ADS1015:
            try:
                self.ads = ADS1015(i2c_addr=ADS1015_I2C_ADDR)
                # 6.144 V gain: MICS-6814 outputs up to ~3.3 V. With the
                # default 2.048 V gain every input >2 V clips to 2047.
                self.ads.set_programmable_gain(6.144)
                self.ads.set_mode("single")
                self.ads.set_sample_rate(1600)
                self.mics = MICS6814Driver(self.ads)
                logger.info(f"ADS1015 OK (0x{ADS1015_I2C_ADDR:02x})")
            except Exception as e:
                logger.warning(f"ADS1015 init failed: {e}")
                self.ads = None
        else:
            logger.warning("ads1015 not installed")

    def start_warmup(self):
        """Start MICS-6814 heater warmup. Non-blocking."""
        if self.mics:
            self.mics.start_warmup()

        # One-shot reset: clear any stored gas baseline so the next warm
        # read takes a fresh clean-air baseline in the current environment.
        from config import Config
        if Config.RESET_GAS_CALIBRATION:
            logger.info("RESET_GAS_CALIBRATION=true — clearing stored baseline")
            self.calibration.reset()

    @property
    def gas_is_warmed_up(self):
        """Check if MICS-6814 heater has warmed up."""
        if self.mics is None:
            return True  # No gas sensor = always ready
        return self.mics.is_warmed_up

    @property
    def warmup_progress(self):
        """Warmup progress 0.0–1.0."""
        if self.mics is None:
            return 1.0
        return self.mics.warmup_progress

    def read_proximity(self):
        """Read the LTR-559 proximity fresh, bypassing the read cache.

        Returns the raw 11-bit proximity count (0-2047); higher = closer.
        """
        if self.ltr:
            try:
                return self.ltr.get_proximity()
            except Exception:
                return None
        return None

    def shutdown(self):
        """Shut down sensors (turn off MICS-6814 heater)."""
        if self.mics:
            self.mics.shutdown()

    def read_all(self):
        """
        Read all sensors and return a dict of values.
        Results are cached for READ_INTERVAL seconds.

        Includes (in addition to the examples above):
          "no2_ppm"/"co_ppm"/"nh3_ppm" — calibrated ppm (None until the
              clean-air baseline is established after warmup).
          "gas_calibrated" — whether a clean-air R0 baseline exists.
          "no2_r0"/"co_r0"/"nh3_r0" — current baseline resistances (ohms).
        """
        from config import Config
        now = time.time()

        # Return cached data if fresh enough
        if now - self._last_read_time < Config.READ_INTERVAL:
            return self._last_read

        self._last_read_time = now
        readings = {}

        # Temperature, Humidity, Pressure
        if self.bme:
            self.bme.update_sensor()
            raw_temp = self.bme.temperature
            raw_pressure = self.bme.pressure
            raw_humidity = self.bme.humidity
            temp = compensate_temperature(raw_temp, Config.TEMP_OFFSET, Config.TEMP_COMP_MODE)
            humidity = compensate_humidity(raw_humidity, Config.HUMIDITY_OFFSET, Config.HUM_COMP_MODE)
            pressure = qnh_pressure(
                raw_pressure + Config.PRESSURE_OFFSET, temp, Config.ALTITUDE
            )
            readings["temperature"] = round(temp, 1)
            readings["pressure"] = round(pressure, 2)
            readings["humidity"] = round(humidity, 1)
            # Raw values exposed for diagnostics / compensation
            readings["temperature_raw"] = round(raw_temp, 2)
            readings["humidity_raw"] = round(raw_humidity, 1)
            readings["pressure_raw"] = round(raw_pressure, 2)

        # Light + Proximity
        if self.ltr:
            readings["light"] = round(self.ltr.get_lux() + Config.LIGHT_OFFSET, 1)
            readings["proximity"] = self.ltr.get_proximity()

        # Gas sensors
        if self.mics:
            gas = self.mics.read_all()
            raw_gas = {"co": gas.get("co"), "no2": gas.get("no2"), "nh3": gas.get("nh3")}
            readings["no2_raw"] = raw_gas.get("no2")
            readings["co_raw"] = raw_gas.get("co")
            readings["nh3_raw"] = raw_gas.get("nh3")

            if self.gas_is_warmed_up and any(raw_gas.values()):
                # Establish a clean-air baseline on the first warm read
                # (or after a reset), then run daily drift calibration.
                # Use RAW temp/hum/pressure: compensation compares raw-to-raw.
                if not self.calibration.calibrated:
                    self.calibration.take_baseline(
                        raw_gas, readings.get("temperature_raw"),
                        readings.get("humidity_raw"), readings.get("pressure_raw"),
                    )
                self.calibration.maybe_daily_calibrate(
                    raw_gas, readings.get("temperature_raw"),
                    readings.get("humidity_raw"), readings.get("pressure_raw"),
                    Config.GAS_CALIBRATION_HOUR,
                )
                comp = self.calibration.compensate(
                    raw_gas, readings.get("temperature_raw"),
                    readings.get("humidity_raw"), readings.get("pressure_raw"),
                )
                ppm = self.calibration.to_ppm(comp)
                readings["no2_ppm"] = round(ppm.get("no2"), 3) if ppm.get("no2") is not None else None
                readings["co_ppm"] = round(ppm.get("co"), 2) if ppm.get("co") is not None else None
                readings["nh3_ppm"] = round(ppm.get("nh3"), 2) if ppm.get("nh3") is not None else None
            else:
                readings["no2_ppm"] = readings["co_ppm"] = readings["nh3_ppm"] = None

            readings["gas_calibrated"] = self.calibration.calibrated
            readings["no2_r0"] = self.calibration.oxi_r0
            readings["co_r0"] = self.calibration.red_r0
            readings["nh3_r0"] = self.calibration.nh3_r0

            # Quality from ppm when calibrated, else ohms-based fallback.
            if readings["no2_ppm"] is not None:
                readings["no2_quality"], readings["no2_color"] = quality_from_ppm(
                    readings["no2_ppm"], "no2")
            else:
                readings["no2_quality"], readings["no2_color"] = \
                    adc_to_gas_quality(raw_gas.get("no2"))
            if readings["co_ppm"] is not None:
                readings["co_quality"], readings["co_color"] = quality_from_ppm(
                    readings["co_ppm"], "co")
            else:
                readings["co_quality"], readings["co_color"] = \
                    adc_to_gas_quality(raw_gas.get("co"))
            if readings["nh3_ppm"] is not None:
                readings["nh3_quality"], readings["nh3_color"] = quality_from_ppm(
                    readings["nh3_ppm"], "nh3")
            else:
                readings["nh3_quality"], readings["nh3_color"] = \
                    adc_to_gas_quality(raw_gas.get("nh3"))

            # LDR
            readings["ldr"] = self.mics.read_ldr()

        self._last_read = readings
        return readings

    def temperature_display(self, celsius=None):
        """Return temperature string in configured unit."""
        from config import Config
        if celsius is None:
            celsius = self._last_read.get("temperature")
        if celsius is None:
            return "N/A"
        if Config.UNIT == "F":
            f = celsius * 9 / 5 + 32
            return f"{f:.1f}°F"
        return f"{celsius:.1f}°C"
