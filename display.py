"""
Display driver for the 0.96" 160×80 SPI LCD on the Enviro + Air Quality board.

The display is an ST7735S-based color LCD driven via SPI.
Uses Pillow for drawing, rendered to a buffer then pushed to the display.

Enviro+ (PIM458) display wiring:
  - SPI0 CE1 (GPIO 7)  -> chip select
  - GPIO 9             -> DC
  - GPIO 12            -> backlight enable
  - SCLK (GPIO 11) / MOSI (GPIO 10)
"""

import io
import logging
import time

logger = logging.getLogger("display")

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    Image = ImageDraw = ImageFont = None

# Display dimensions
WIDTH = 160
HEIGHT = 80

# Color palette
COLORS = {
    "black": (0, 0, 0),
    "white": (255, 255, 255),
    "bg_dark": (10, 10, 30),
    "bg_mid": (20, 20, 50),
    "green": (0, 200, 80),
    "yellow": (220, 200, 0),
    "orange": (240, 140, 0),
    "red": (220, 40, 40),
    "blue": (80, 140, 255),
    "cyan": (0, 220, 220),
    "grey": (200, 200, 215),
    "dim": (145, 145, 175),
    "accent": (100, 180, 255),
}

# Icon glyphs (simple ASCII/unicode for sensor icons)
ICONS = {
    "temp": "\u26cc ",      # degree symbol
    "humidity": "\u26c5 ",  # umbrella (approximation for humidity)
    "pressure": "\u25cd ",  # white circle
    "light": "\u2605 ",     # star (for light)
    "no2": "N",
    "co": "C",
    "nh3": "A",
    "clock": "\u23f1",
}


class EnviroDisplay:
    """
    160×80 color LCD display driver.

    Renders 4 screen layouts:
      0 — Temperature + Humidity
      1 — Pressure (light is shown on the web only)
      2 — Air Quality (NO2, CO, NH3 ppm)
      3 — Gas detail (ppm + LDR + proximity)
    """

    def __init__(self):
        self.display = None
        self.image = None
        self.draw = None
        self._current_screen = 0
        self._font_large = None
        self._font_medium = None
        self._font_small = None
        self._font_tiny = None
        self._initialized = False

    def init(self):
        """Initialize the display hardware and font resources."""
        if self._initialized:
            return

        # Try to initialize the ST7735 display
        self._init_display()
        self._load_fonts()

        # Create offscreen buffer
        if Image:
            self.image = Image.new("RGB", (WIDTH, HEIGHT), COLORS["bg_dark"])
            self.draw = ImageDraw.Draw(self.image)

        self._initialized = True

    def _init_display(self):
        """
        Initialize the ST7735 SPI display.

        Uses the `st7735` library (pip package `st7735`):
          from st7735 import ST7735

        Enviro+ (PIM458) pinout (see pinout.xyz/pinout/enviro_plus):
          port=0, cs=1 (SPI0 CE1), DC=GPIO9, backlight=GPIO12, rotation=270
        """
        try:
            from st7735 import ST7735

            self.display = ST7735(
                port=0,
                cs=1,
                dc="GPIO9",
                backlight="GPIO12",
                rotation=270,
                spi_speed_hz=10000000,
            )
        except Exception as e:
            logger.warning(f"ST7735 init failed: {e}")
            # Fallback: try luma.library
            try:
                from luma.lcd import st7735 as luma_st7735
                from luma.core.interface.serial import spi
                from luma.core.render import canvas

                serial = spi(port=0, device=1, gpio_DC=9)
                self.display = luma_st7735(serial, width=WIDTH, height=HEIGHT)
                self.display.set_mode("RGB")
            except Exception as e2:
                logger.warning(f"luma.st7735 init failed: {e2}")
                self.display = None

        # Push an initial blank frame
        if self.display is not None and Image is not None:
            try:
                self.display.display(
                    Image.new("RGB", (WIDTH, HEIGHT), COLORS["bg_dark"])
                )
            except Exception:
                pass

    def _load_fonts(self):
        """Load fonts at different sizes. Falls back to default if unavailable."""
        if not ImageFont:
            return

        # Try common font paths on Raspberry Pi
        font_paths = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        ]

        font_path = None
        for path in font_paths:
            try:
                ImageFont.truetype(path, 10)
                font_path = path
                break
            except (FileNotFoundError, IOError):
                continue

        if font_path:
            try:
                self._font_large = ImageFont.truetype(font_path, 22)
            except Exception:
                self._font_large = ImageFont.load_default()
            try:
                self._font_medium = ImageFont.truetype(font_path, 14)
            except Exception:
                self._font_medium = ImageFont.load_default()
            try:
                self._font_small = ImageFont.truetype(font_path, 10)
            except Exception:
                self._font_small = ImageFont.load_default()
            try:
                self._font_tiny = ImageFont.truetype(font_path, 8)
            except Exception:
                self._font_tiny = ImageFont.load_default()
        else:
            self._font_large = ImageFont.load_default()
            self._font_medium = self._font_large
            self._font_small = self._font_large
            self._font_tiny = self._font_large

    def clear(self):
        """Clear the screen to dark background."""
        if self.draw:
            self.draw.rectangle(
                [0, 0, WIDTH - 1, HEIGHT - 1],
                fill=COLORS["bg_dark"],
                outline=None,
            )

    def render_warmup(self, progress):
        """
        Render the warmup screen shown while MICS-6814 heats up.

        Args:
            progress: float 0.0–1.0 warmup progress
        """
        if not self.draw:
            return

        self.clear()

        # Title
        self._text_centered("Warming Up...", 18, COLORS["yellow"], "medium")

        # Progress bar
        bar_width = 100
        bar_height = 10
        bar_x = (WIDTH - bar_width) // 2
        bar_y = 35
        fill_width = int(bar_width * progress)

        self._text_centered("Gas Sensor", 55, COLORS["grey"], "small")

        # Bar outline
        self.draw.rectangle(
            [bar_x, bar_y, bar_x + bar_width - 1, bar_y + bar_height - 1],
            fill=COLORS["bg_mid"],
            outline=COLORS["grey"],
        )
        # Bar fill
        if fill_width > 0:
            self.draw.rectangle(
                [bar_x, bar_y, bar_x + fill_width - 1, bar_y + bar_height - 1],
                fill=COLORS["cyan"],
            )

        # Percentage
        pct = int(progress * 100)
        self._text_centered(f"{pct}%", bar_y + bar_height + 4, COLORS["white"], "small")

        self.update()

    def render_screen_0_temp_humidity(self, data):
        """
        Screen 0: Temperature + Humidity with daily H/L.

        Layout for 160x80 (big values, full-width halves):
        +------------------+------------------+
        |  TEMP            |  HUMIDITY        |
        |  22.5°C          |  45%             |
        |  H:24.5 L:18.2   |  H:55% L:38%     |
        | 23:45:01   clock |            . . . |
        +------------------+------------------+
        """
        self.clear()

        temp = data.get("temperature")
        hum = data.get("humidity")
        temp_h = data.get("temp_high")
        temp_l = data.get("temp_low")
        hum_h = data.get("hum_high")
        hum_l = data.get("hum_low")
        clock = data.get("clock", "")

        # --- Temperature (left half) ---
        self._text("TEMP", 3, 2, COLORS["dim"], "small")
        # Big number + small unit (keeps it the same size as humidity)
        if temp is None:
            temp_num, temp_unit = "N/A", ""
        elif data.get("unit") == "F":
            temp_num = f"{temp * 9 / 5 + 32:.1f}"
            temp_unit = "°F"
        else:
            temp_num = f"{temp:.1f}"
            temp_unit = "°C"
        used = self._text_auto(temp_num, 3, 16, COLORS["white"], max_width=70)
        self._text(temp_unit, 3 + self._text_width(temp_num, used) + 3, 20, COLORS["grey"], "small")

        hl_text = ""
        if temp_h is not None and temp_l is not None:
            if data.get("unit") == "F":
                hl_text = f"H:{temp_h:.0f} L:{temp_l:.0f}"
            else:
                hl_text = f"H:{temp_h:.1f} L:{temp_l:.1f}"
        self._text(hl_text, 3, 46, COLORS["grey"], "small")

        # --- Humidity (right half) ---
        self._text("HUMIDITY", 85, 2, COLORS["dim"], "small")
        hum_num = f"{hum:.0f}" if hum is not None else "N/A"
        used = self._text_auto(hum_num, 85, 16, COLORS["cyan"], max_width=66)
        self._text("%", 85 + self._text_width(hum_num, used) + 3, 20, COLORS["grey"], "small")

        hl_text = ""
        if hum_h is not None and hum_l is not None:
            hl_text = f"H:{hum_h:.0f} L:{hum_l:.0f}"
        self._text(hl_text, 85, 46, COLORS["grey"], "small")

        # Separator
        self.draw.line([82, 4, 82, 62], fill=COLORS["dim"])

        self._footer(clock, 0)
        self.update()

    def render_screen_1_pressure(self, data):
        """
        Screen 1: Pressure (full width) with daily H/L.

        Light is shown on the web dashboard only. Layout:
        +----------------------------------------+
        |              PRESSURE                  |
        |             1013.2 hPa                 |
        |           H:1015.2 L:1008.1            |
        | 23:45:01   clock |            . . .    |
        +----------------------------------------+
        """
        self.clear()

        pressure = data.get("pressure")
        pres_h = data.get("pressure_high")
        pres_l = data.get("pressure_low")
        clock = data.get("clock", "")

        self._text_centered("PRESSURE", 3, COLORS["dim"], "small")

        pres_str = f"{pressure:.1f}" if pressure is not None else "N/A"
        unit = "hPa"
        total = (self._text_width(pres_str, "large")
                 + 6 + self._text_width(unit, "small"))
        x = (WIDTH - total) // 2
        self._text(pres_str, x, 22, COLORS["white"], "large")
        self._text(unit, x + self._text_width(pres_str, "large") + 6, 30,
                   COLORS["grey"], "small")

        if pres_h is not None and pres_l is not None:
            self._text_centered(
                f"H:{pres_h:.1f} L:{pres_l:.1f}", 56, COLORS["grey"], "small"
            )

        self._footer(clock, 1)
        self.update()

    def render_screen_2_air_quality(self, data):
        """
        Screen 2: Air Quality (NO2, CO, NH3) with color-coded status.

        Layout:
        +----------------------------------------+
        |  AIR QUALITY                  GOOD     |
        |  NO2  0.021 ppm               GOOD     |
        |  CO   1.20 ppm                GOOD     |
        |  NH3  0.80 ppm                GOOD     |
        | 23:45:01   clock |            . . .    |
        +----------------------------------------+
        """
        self.clear()

        no2_q = data.get("no2_quality", "N/A")
        co_q = data.get("co_quality", "N/A")
        nh3_q = data.get("nh3_quality", "N/A")
        no2_c = COLORS.get(data.get("no2_color", "grey"), COLORS["grey"])
        co_c = COLORS.get(data.get("co_color", "grey"), COLORS["grey"])
        nh3_c = COLORS.get(data.get("nh3_color", "grey"), COLORS["grey"])
        clock = data.get("clock", "")

        # Title + overall status
        self._text("AIR QUALITY", 3, 2, COLORS["dim"], "small")
        overall = self._overall_aqi(no2_q, co_q, nh3_q)
        overall_color = COLORS.get(overall[1], COLORS["grey"])
        self._text_right(overall[0], 2, overall_color, "small")

        # Rows: label / ppm / status
        row_h = 19
        rows = [
            ("NO2", self._fmt_ppm(data.get("no2_ppm"), "no2"), no2_q, no2_c),
            ("CO", self._fmt_ppm(data.get("co_ppm"), "co"), co_q, co_c),
            ("NH3", self._fmt_ppm(data.get("nh3_ppm"), "nh3"), nh3_q, nh3_c),
        ]
        y = 15
        for label, value, quality, color in rows:
            self._text(label, 3, y, COLORS["white"], "small")
            self._text(value, 38, y, COLORS["grey"], "small")
            self._text_right(quality, y, color, "medium")
            y += row_h

        self._footer(clock, 2)
        self.update()

    def render_screen_3_gas_detail(self, data):
        """
        Screen 3: Detailed gas sensor readings + calibration info.

        Layout:
        +----------------------------------------+
        |  GAS DETAIL                 CAL        |
        |  NO2  0.021 ppm                        |
        |  CO   1.20 ppm                         |
        |  NH3  0.80 ppm                         |
        |  LDR  12.3k   PROX  432                |
        | 23:45:01   clock |            . . .    |
        +----------------------------------------+
        """
        self.clear()

        clock = data.get("clock", "")

        self._text("GAS DETAIL", 3, 2, COLORS["dim"], "small")
        cal_state = "CAL" if data.get("gas_calibrated") else "UNCAL"
        cal_color = COLORS["green"] if data.get("gas_calibrated") else COLORS["yellow"]
        self._text_right(cal_state, 2, cal_color, "small")

        y = 12
        rows = [
            ("NO2", self._fmt_ppm(data.get("no2_ppm"), "no2")),
            ("CO", self._fmt_ppm(data.get("co_ppm"), "co")),
            ("NH3", self._fmt_ppm(data.get("nh3_ppm"), "nh3")),
        ]
        for label, value in rows:
            self._text(label, 3, y, COLORS["white"], "medium")
            self._text(value, 42, y, COLORS["accent"], "medium")
            y += 16

        # LDR + live proximity (useful for tap-threshold calibration)
        prox = data.get("proximity")
        prox_str = "N/A" if prox is None else f"{prox}"
        ldr_str = self._fmt_resistance(data.get("ldr"))
        self._text(f"LDR {ldr_str}   PROX {prox_str}", 3, 56, COLORS["grey"], "small")

        self._footer(clock, 3)
        self.update()

    def _fmt_ppm(self, ppm, gas=None):
        """Format a ppm value compactly (e.g. 0.021, 1.20, 12.3)."""
        if ppm is None:
            return "N/A"
        try:
            ppm = float(ppm)
        except (TypeError, ValueError):
            return "N/A"
        if ppm < 0.1:
            return f"{ppm:.3f}"
        if ppm < 10:
            return f"{ppm:.2f}"
        if ppm < 100:
            return f"{ppm:.1f}"
        return f"{ppm:.0f}"

    def _fmt_resistance(self, ohms):
        """Format a resistance in ohms compactly (e.g. 123456 -> 123k)."""
        if ohms is None:
            return "N/A"
        try:
            ohms = float(ohms)
        except (TypeError, ValueError):
            return "N/A"
        if ohms >= 1000000:
            return f"{ohms / 1000000:.1f}M"
        if ohms >= 10000:
            return f"{ohms / 1000:.0f}k"
        if ohms >= 1000:
            return f"{ohms / 1000:.1f}k"
        return f"{ohms:.0f}"

    # -----------------------------------------------------------------------
    # Helper methods
    # -----------------------------------------------------------------------

    def _overall_aqi(self, no2, co, nh3):
        """Determine overall air quality from individual readings."""
        levels = {"Good": 0, "Moderate": 1, "Poor": 2, "Unhealthy": 3}
        reverse = {v: k for k, v in levels.items()}

        vals = []
        for q in [no2, co, nh3]:
            if q in levels:
                vals.append(levels[q])

        if not vals:
            return "N/A", "grey"

        worst = max(vals)
        label = reverse[worst]
        color_key = {"Good": "green", "Moderate": "yellow",
                      "Poor": "orange", "Unhealthy": "red"}[label]
        return label, color_key

    def _text(self, text, x, y, color, size="small"):
        """Draw text at position."""
        if not self.draw or not text:
            return
        font_map = {
            "large": self._font_large,
            "medium": self._font_medium,
            "small": self._font_small,
            "tiny": self._font_tiny,
        }
        font = font_map.get(size, self._font_small)
        self.draw.text((x, y), str(text), fill=color, font=font)

    def _text_right(self, text, y, color, size="small"):
        """Draw text right-aligned."""
        if not self.draw or not text:
            return
        font_map = {
            "large": self._font_large,
            "medium": self._font_medium,
            "small": self._font_small,
            "tiny": self._font_tiny,
        }
        font = font_map.get(size, self._font_small)
        tw = self._text_width(text, size)
        self.draw.text((WIDTH - tw - 3, y), str(text), fill=color, font=font)

    def _text_auto(self, text, x, y, color, max_width, preferred="large"):
        """
        Draw text, shrinking the font until it fits within max_width.

        Returns the font size actually used.
        """
        if not self.draw or not text:
            return preferred
        order = [preferred, "medium", "small", "tiny"]
        for size in order:
            if self._text_width(text, size) <= max_width:
                self._text(text, x, y, color, size)
                return size
        self._text(text, x, y, color, preferred)
        return preferred

    def _text_centered(self, text, y, color, size="small"):
        """Draw text centered horizontally."""
        if not self.draw or not text:
            return
        font_map = {
            "large": self._font_large,
            "medium": self._font_medium,
            "small": self._font_small,
            "tiny": self._font_tiny,
        }
        font = font_map.get(size, self._font_small)
        tw = self._text_width(text, size)
        x = (WIDTH - tw) // 2
        self.draw.text((x, y), str(text), fill=color, font=font)

    def _text_width(self, text, size="small"):
        """Get text width in pixels."""
        if not ImageDraw:
            return len(text) * 6
        font_map = {
            "large": self._font_large,
            "medium": self._font_medium,
            "small": self._font_small,
            "tiny": self._font_tiny,
        }
        font = font_map.get(size, self._font_small)
        dummy_draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
        bbox = dummy_draw.textbbox((0, 0), str(text), font=font)
        return bbox[2] - bbox[0]

    def _quality_bar(self, x, y, color, quality):
        """Draw a small color bar indicating quality level."""
        bar_len = {"Good": 25, "Moderate": 18, "Poor": 12, "Unhealthy": 6}
        length = bar_len.get(quality, 0)
        if length > 0 and self.draw:
            self.draw.rectangle(
                [x, y, x + length - 1, y + 4],
                fill=color,
            )

    def _screen_indicator(self, current, total):
        """Draw screen position dots at the bottom-right."""
        dot_size = 4
        spacing = 7
        start_x = WIDTH - 3 - total * spacing
        dot_y = 73

        for i in range(total):
            x = start_x + i * spacing
            color = COLORS["accent"] if i == current else COLORS["dim"]
            self.draw.rectangle([x, dot_y, x + dot_size - 1, dot_y + dot_size - 1], fill=color)

    def _footer(self, clock, screen_index):
        """Shared footer: tiny clock bottom-left, screen dots bottom-right."""
        self._text(clock, 3, 71, COLORS["accent"], "tiny")
        self._screen_indicator(screen_index, 4)

    def update(self):
        """Push the offscreen buffer to the physical display."""
        if self.display and self.image:
            try:
                # Try Pimoroni display interface
                if hasattr(self.display, "set_image"):
                    self.display.set_image(self.image)
                elif hasattr(self.display, "display"):
                    self.display.display(self.image)
                else:
                    # Fallback: use luma-style canvas
                    from luma.core.render import canvas
                    with canvas(self.display) as draw:
                        draw.image(self.image.rotate(90, expand=True))
            except Exception:
                pass

    def shutdown(self):
        """Turn off the display."""
        if self.display:
            try:
                self.display.set_backlight(False)
            except Exception:
                pass
            try:
                self.display.display_off()
            except Exception:
                pass
