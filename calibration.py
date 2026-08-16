"""
Sensor calibration helpers, adapted from roscoe81's enviro-monitor.

Reference: https://github.com/roscoe81/enviro-monitor
(Northcliff_AQI_Monitor_Gen.py)

Implements:
  - Cubic-polynomial temperature compensation (BME280 biased by Pi CPU heat).
  - Quadratic humidity compensation.
  - Altitude -> sea-level (QNH) pressure compensation.
  - MICS-6814 clean-air R0 baseline + proportional temp/hum/pressure
    compensation + SPEC datasheet ppm conversion.
  - Daily 7-day rolling R0 drift calibration, persisted to JSON.

NOTE: The polynomial/quadratic coefficients come from regression analysis
of roscoe81's specific unit with a display-mounted Enviro+. They may not
match your setup. Each mode is configurable in settings.ini — switch back
to "offset" if readings look wrong.
"""

import json
import logging
import math
import os
import threading
from datetime import date, datetime

logger = logging.getLogger("calibration")

# ---------------------------------------------------------------------------
# Temperature compensation (display-enabled coefficients)
#   comp = a*raw^3 + b*raw^2 + c*raw + d   (temp_offset folds into d)
# ---------------------------------------------------------------------------
TEMP_CUBIC_A = -0.0001
TEMP_CUBIC_B = 0.0037
TEMP_CUBIC_C = 1.00568
TEMP_CUBIC_D = -6.78291

# ---------------------------------------------------------------------------
# Humidity compensation (display-enabled coefficients)
#   comp = a*raw^2 + b*raw + c
# WARNING: These coefficients were fit to a sensor that read ~20-30% low.
# They are probably wrong for your unit; default is "offset".
# ---------------------------------------------------------------------------
HUM_QUAD_A = -0.0032
HUM_QUAD_B = 1.6931
HUM_QUAD_C = 0.9391

# ---------------------------------------------------------------------------
# Gas compensation factors (fraction of Rs per unit of T/H/P difference),
# derived by roscoe81 from long-term regression testing. Order:
#   temp_factor, humidity_factor, barometer_factor
# ---------------------------------------------------------------------------
GAS_COMP_FACTORS = {
    "co": (-0.015, 0.0125, -0.0053),    # RED (reducing gas / CO)
    "no2": (-0.017, 0.0115, -0.0072),   # OX (oxidising gas / NO2)
    "nh3": (-0.02695, 0.0094, 0.003254),
}

# SPEC datasheet log curves: ppm = f(ratio) where ratio = Rs / R0.
PPM_CURVES = {
    "co": lambda r: 10 ** (-1.25 * math.log10(r) + 0.64),
    "no2": lambda r: 10 ** (math.log10(r) - 0.8129),
    "nh3": lambda r: 10 ** (-1.8 * math.log10(r) - 0.163),
}

# ppm -> 4-level quality breakpoints (Good/Moderate/Poor/Unhealthy).
# Merged from roscoe81's 5-level thresholds [Great, OK, Alert, Poor, Bad].
PPM_QUALITY = {
    "no2": (0.2, 0.4, 0.8),
    "co": (6.0, 10.0, 50.0),
    "nh3": (1.0, 2.0, 10.0),
}

# Sanity bounds for R0 (ohms). Out-of-range baselines are ignored.
R0_MIN = 1000
R0_MAX = 2000000

DEFAULT_CAL_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "gas_calibration.json"
)


# ---------------------------------------------------------------------------
# Pure compensation functions
# ---------------------------------------------------------------------------
def compensate_temperature(raw, temp_offset=0.0, mode="polynomial"):
    """Compensate a raw BME280 temperature (°C).

    mode:
      "polynomial" — cubic fit (roscoe81 display-on coefficients)
      "offset"     — raw + temp_offset
    """
    if raw is None:
        return None
    if mode == "polynomial":
        t = (TEMP_CUBIC_A * raw ** 3 + TEMP_CUBIC_B * raw ** 2
             + TEMP_CUBIC_C * raw + TEMP_CUBIC_D + temp_offset)
        # Safety: polynomial is only valid ~0-40°C. Fall back to offset
        # if it diverges wildly from the raw reading.
        if not math.isfinite(t) or abs(t - raw) > 20.0:
            return raw + temp_offset
        return t
    return raw + temp_offset


def compensate_humidity(raw, humidity_offset=0.0, mode="offset"):
    """Compensate a raw BME280 humidity (%).

    mode:
      "quadratic" — quadratic fit (roscoe81 coefficients; unit-specific!)
      "offset"    — raw + humidity_offset
    """
    if raw is None:
        return None
    if mode == "quadratic":
        h = HUM_QUAD_A * raw ** 2 + HUM_QUAD_B * raw + HUM_QUAD_C
        h += humidity_offset
    else:
        h = raw + humidity_offset
    return max(0.0, min(100.0, h))


def qnh_pressure(raw_hpa, temp_c, altitude_m):
    """Convert raw barometric pressure (hPa) to sea-level pressure.

    Uses the same ISA formula as roscoe81. Returns raw when altitude is 0.
    """
    if raw_hpa is None or not altitude_m:
        return raw_hpa
    factor = math.pow(
        1 - (0.0065 * altitude_m / (temp_c + 0.0065 * altitude_m + 273.15)),
        -5.257,
    )
    return raw_hpa * factor


def quality_from_ppm(ppm, gas):
    """Map a ppm value to a 4-level quality (label, color_key)."""
    if ppm is None:
        return "N/A", "grey"
    good, moderate, poor = PPM_QUALITY.get(gas, (0.05, 0.1, 0.5))
    if ppm < good:
        return "Good", "green"
    if ppm < moderate:
        return "Moderate", "yellow"
    if ppm < poor:
        return "Poor", "orange"
    return "Unhealthy", "red"


# ---------------------------------------------------------------------------
# Persistent gas calibration state
# ---------------------------------------------------------------------------
class GasCalibration:
    """Clean-air R0 baseline + daily drift calibration for MICS-6814.

    State is persisted to a JSON file so R0 survives restarts.

    Usage:
        cal = GasCalibration(cal_file)
        cal.take_baseline(resistances, temp, hum, bar)   # clean air!
        cal.maybe_daily_calibrate(resistances, temp, hum, bar)
        comp = cal.compensate(resistances, temp, hum, bar)   # ohms
        ppm = cal.to_ppm(comp)
    """

    def __init__(self, cal_file=DEFAULT_CAL_FILE):
        self.cal_file = cal_file
        self._lock = threading.Lock()
        self.red_r0 = None
        self.oxi_r0 = None
        self.nh3_r0 = None
        self.reds_r0 = []
        self.oxis_r0 = []
        self.nh3s_r0 = []
        self.calib_temp = None
        self.calib_hum = None
        self.calib_bar = None
        self.last_calibration_date = None
        self._load()

    @property
    def calibrated(self):
        """True when a clean-air baseline has been established."""
        return all(r is not None for r in (self.red_r0, self.oxi_r0, self.nh3_r0))

    @property
    def r0(self):
        return {
            "no2": self.oxi_r0,
            "co": self.red_r0,
            "nh3": self.nh3_r0,
        }

    # -- persistence -------------------------------------------------------
    def _load(self):
        try:
            with open(self.cal_file) as f:
                d = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        self.red_r0 = d.get("red_r0")
        self.oxi_r0 = d.get("oxi_r0")
        self.nh3_r0 = d.get("nh3_r0")
        self.reds_r0 = d.get("reds_r0", [])
        self.oxis_r0 = d.get("oxis_r0", [])
        self.nh3s_r0 = d.get("nh3s_r0", [])
        self.calib_temp = d.get("calib_temp")
        self.calib_hum = d.get("calib_hum")
        self.calib_bar = d.get("calib_bar")
        self.last_calibration_date = d.get("last_calibration_date")

    def save(self):
        with self._lock:
            data = {
                "red_r0": self.red_r0,
                "oxi_r0": self.oxi_r0,
                "nh3_r0": self.nh3_r0,
                "reds_r0": self.reds_r0,
                "oxis_r0": self.oxis_r0,
                "nh3s_r0": self.nh3s_r0,
                "calib_temp": self.calib_temp,
                "calib_hum": self.calib_hum,
                "calib_bar": self.calib_bar,
                "last_calibration_date": self.last_calibration_date,
            }
            tmp = self.cal_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, self.cal_file)

    def reset(self):
        """Clear calibration state (next warm read re-baselines)."""
        with self._lock:
            self.red_r0 = self.oxi_r0 = self.nh3_r0 = None
            self.reds_r0 = self.oxis_r0 = self.nh3s_r0 = []
            self.calib_temp = self.calib_hum = self.calib_bar = None
            self.last_calibration_date = None
        try:
            os.remove(self.cal_file)
        except OSError:
            pass
        logger.info("Gas calibration reset")

    # -- baseline -----------------------------------------------------------
    def take_baseline(self, resistances, temp, hum, bar):
        """Set the clean-air R0 baseline from a spot reading.

        Should be called in clean (fresh) air after the heater has warmed up.
        """
        raw = [resistances.get(g) for g in ("co", "no2", "nh3")]
        if any(r is None or not (R0_MIN <= r <= R0_MAX) for r in raw):
            logger.warning(
                f"Baseline skipped: out-of-range resistances {dict(resistances)}"
            )
            return False
        with self._lock:
            self.red_r0 = int(raw[0])
            self.oxi_r0 = int(raw[1])
            self.nh3_r0 = int(raw[2])
            self.reds_r0 = [self.red_r0] * 7
            self.oxis_r0 = [self.oxi_r0] * 7
            self.nh3s_r0 = [self.nh3_r0] * 7
            self.calib_temp = round(temp, 1) if temp is not None else None
            self.calib_hum = round(hum, 1) if hum is not None else None
            self.calib_bar = round(bar, 1) if bar is not None else None
            self.last_calibration_date = date.today().isoformat()
        self.save()
        logger.info(
            f"Clean-air baseline set: CO {self.red_r0}Ω, NO2 {self.oxi_r0}Ω, "
            f"NH3 {self.nh3_r0}Ω @ T={self.calib_temp} H={self.calib_hum} "
            f"P={self.calib_bar}"
        )
        return True

    # -- daily drift calibration --------------------------------------------
    def maybe_daily_calibrate(self, resistances, temp, hum, bar, hour):
        """Roll the 7-day R0 average once per day at the configured hour."""
        if not self.calibrated:
            return False
        today = date.today().isoformat()
        if self.last_calibration_date == today:
            return False
        if datetime.now().hour != hour:
            return False

        raw = [resistances.get(g) for g in ("co", "no2", "nh3")]
        if any(r is None or not (R0_MIN <= r <= R0_MAX) for r in raw):
            return False

        with self._lock:
            self.reds_r0 = self.reds_r0[1:] + [int(raw[0])]
            self.red_r0 = round(sum(self.reds_r0) / float(len(self.reds_r0)))
            self.oxis_r0 = self.oxis_r0[1:] + [int(raw[1])]
            self.oxi_r0 = round(sum(self.oxis_r0) / float(len(self.oxis_r0)))
            self.nh3s_r0 = self.nh3s_r0[1:] + [int(raw[2])]
            self.nh3_r0 = round(sum(self.nh3s_r0) / float(len(self.nh3s_r0)))
            if temp is not None and hum is not None and bar is not None:
                self.calib_temp = round(temp, 1)
                self.calib_hum = round(hum, 1)
                self.calib_bar = round(bar, 1)
            self.last_calibration_date = today
        self.save()
        logger.info(
            f"Daily gas calibration @ {datetime.now().strftime('%H:%M')}: "
            f"CO {self.red_r0}Ω, NO2 {self.oxi_r0}Ω, NH3 {self.nh3_r0}Ω"
        )
        return True

    # -- compensation + ppm --------------------------------------------------
    def compensate(self, resistances, temp, hum, bar):
        """Apply proportional temp/hum/pressure compensation to Rs.

        comp_Rs = raw_Rs - (fT*raw_Rs*dT + fH*raw_Rs*dH + fP*raw_Rs*dP)

        Returns raw resistances (ohms) unchanged if baseline/env is missing.
        """
        if not self.calibrated or self.calib_temp is None:
            return dict(resistances)
        dT = (temp or 0.0) - self.calib_temp
        dH = (hum or 0.0) - self.calib_hum
        dP = (bar or 0.0) - self.calib_bar

        comp = {}
        for gas, (fT, fH, fP) in GAS_COMP_FACTORS.items():
            raw_rs = resistances.get(gas)
            if raw_rs is None:
                comp[gas] = None
                continue
            comp[gas] = round(
                raw_rs - (fT * raw_rs * dT + fH * raw_rs * dH + fP * raw_rs * dP),
                0,
            )
        return comp

    def to_ppm(self, comp):
        """Convert compensated resistances (ohms) to ppm via SPEC curves."""
        ppm = {}
        for gas, curve in PPM_CURVES.items():
            r0 = self.r0.get(gas)
            rs = comp.get(gas)
            if rs is None or not r0 or r0 <= 0 or rs <= 0:
                ppm[gas] = None
                continue
            ratio = rs / r0
            try:
                ppm[gas] = curve(max(ratio, 0.0001))
            except (ValueError, OverflowError):
                ppm[gas] = None
        return ppm
